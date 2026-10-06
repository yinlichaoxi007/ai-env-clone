"""WorkBuddy 会话索引体检与修复（:mod:`ai_env_clone.workbuddy_repair`）。

覆盖 2026-10-05 的实测故障：导入行 ``user_id`` 被写成凭空发明的 ``imported``，
而 WorkBuddy 侧栏本地列表的谓词是
``transport='local' and deleted_at is null and (user_id = <当前 uid> or user_id = '')``
⇒ 工作区照常显示、会话一条都不见。

全部用临时目录里的假 ``.workbuddy``，不碰真实数据根。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import workbuddy_repair as wr  # noqa: E402

UID = "9ae9129b-c0e9-4158-b4cd-983fac049c6d"
OTHER_UID = "00000000-1111-2222-3333-444444444444"

_DB_ROW_COLS = ("id", "cwd", "user_id", "title", "status", "created_at", "updated_at",
                "deleted_at", "source_mode", "transport")


def _row(sid, cwd=r"D:\project\Demo", user_id="", title="会话", status="completed",
         deleted_at=None, source_mode="import", transport="local"):
    return (sid, cwd, user_id, title, status, 1000, 2000,
            deleted_at, source_mode, transport)


class WbHomeTestCase(unittest.TestCase):
    """提供假数据根：``workbuddy.db`` + ``projects/<slug>/<sid>.jsonl`` + uid 目录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wb_repair_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, ".workbuddy")
        os.makedirs(self.home)

    # ---- 建库 ----
    def make_db(self, rows=(), workspaces=(), with_transport=True) -> str:
        db = os.path.join(self.home, "workbuddy.db")
        con = sqlite3.connect(db)
        cols = ("id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,"
                " title TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,"
                " updated_at INTEGER NOT NULL, last_activity_at INTEGER,"
                " deleted_at INTEGER, is_playground INTEGER NOT NULL DEFAULT 0,"
                " source_mode TEXT, mode TEXT")
        if with_transport:
            cols += ", transport TEXT NOT NULL DEFAULT 'local'"
        con.execute("create table sessions (%s)" % cols)
        con.execute("create table workspaces (path TEXT PRIMARY KEY,"
                    " last_opened_at INTEGER NOT NULL)")
        picked = _DB_ROW_COLS if with_transport else _DB_ROW_COLS[:-1]
        con.executemany(
            "insert into sessions(%s) values(%s)"
            % (", ".join(picked), ", ".join("?" * len(picked))),
            [tuple(r[:len(picked)]) for r in rows])
        con.executemany("insert or ignore into workspaces(path, last_opened_at) values(?,?)",
                        [(p, 1) for p in workspaces])
        con.commit()
        con.close()
        return db

    def drop_workspaces_table(self) -> None:
        con = sqlite3.connect(os.path.join(self.home, "workbuddy.db"))
        con.execute("drop table workspaces")
        con.commit()
        con.close()

    # ---- 磁盘事件流 ----
    def make_jsonl(self, sid, cwd=r"D:\project\Demo", slug="demo", title="",
                   events=None, quickask=False) -> str:
        sdir = os.path.join(self.home, "projects", slug)
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, sid + ".jsonl")
        lines = events if events is not None else [
            {"type": "ai-title", "aiTitle": title or "导入的会话", "timestamp": 1730000000000},
            {"type": "message", "role": "user", "cwd": cwd,
             "content": [{"type": "text", "text": "问题"}], "timestamp": 1730000001000},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
        if quickask:
            open(os.path.join(sdir, sid + ".quickask"), "w", encoding="utf-8").close()
        return path

    def make_snapshot(self, ids=(), uid=UID) -> str:
        d = os.path.join(self.home, uid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, wr.SNAPSHOT_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1,
                       "items": [{"id": i} for i in ids]}, fh)
        return path

    def db_rows(self):
        con = sqlite3.connect(os.path.join(self.home, "workbuddy.db"))
        try:
            return {r[0]: r for r in con.execute(
                "select id, user_id, cwd, title, source_mode from sessions")}
        finally:
            con.close()

    def repair(self, backup=True) -> dict:
        return wr.apply_wb_repair(self.home, wr.plan_wb_repair(self.home), backup=backup)


class TestNormalize(WbHomeTestCase):
    def test_workspace_path_normalization(self):
        self.assertEqual(wr.normalize_workspace_path("D:\\Project\\Demo\\"), "d:/project/demo")
        self.assertEqual(wr.normalize_workspace_path("/home/u/demo/"), "/home/u/demo")

    def test_auto_generated_workspace(self):
        self.assertTrue(wr.is_auto_generated_workspace(r"C:\Users\u\WorkBuddy\2026-10-05-21-38-00"))
        self.assertFalse(wr.is_auto_generated_workspace(r"D:\project\Demo"))

    def test_extract_session_info(self):
        path = self.make_jsonl("s1", title="起个标题")
        info = wr.extract_session_info(path)
        self.assertEqual(info["cwd"], r"D:\project\Demo")
        self.assertEqual(info["title"], "起个标题")
        self.assertEqual((info["created_ms"], info["updated_ms"]),
                         (1730000000000, 1730000001000))

    def test_uid_detected_from_snapshot_dir(self):
        self.make_snapshot()
        uid, note = wr.detect_current_uid(self.home)
        self.assertEqual(uid, UID)
        self.assertIn("侧栏快照目录", note)


class TestDetect(WbHomeTestCase):
    def test_imported_rows_with_invented_uid_are_wrong_owner(self):
        """故障现场复现：库里 52 行 user_id='imported'、正文都在 ⇒ 只报「归属写错」。"""
        self.make_db(rows=[_row("s%d" % i, user_id="imported") for i in range(3)],
                     workspaces=[r"D:\project\Demo"])
        for i in range(3):
            self.make_jsonl("s%d" % i)
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual([r.session_id for r in res.wrong_owner], ["s0", "s1", "s2"])
        self.assertEqual(res.unregistered, [])
        self.assertEqual(res.orphan_rows, [])
        self.assertFalse(res.healthy)
        self.assertEqual(res.fixable_total(), 3)
        self.assertIn("归属写错的导入会话（侧栏看不到）：3 条", "\n".join(res.summary_lines()))

    def test_healthy_when_rows_match_disk_and_owner_is_empty(self):
        self.make_db(rows=[_row("s1", user_id="")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        self.make_snapshot(ids=["s1"])
        res = wr.detect_wb_sessions(self.home)
        self.assertTrue(res.healthy, res.summary_lines())
        self.assertEqual(res.attention_total, 0)

    def test_foreign_and_cloud_rows_are_reported_not_fixed(self):
        """别人账号的行、云端行：只报告，绝不改归属（即便同批里有该修的导入行）。"""
        self.make_db(rows=[
            _row("mine", user_id=""),
            _row("bad", user_id="imported"),
            _row("foreign", user_id=OTHER_UID, source_mode=None),
            _row("cloud", user_id=OTHER_UID, transport="cloud"),
        ], workspaces=[r"D:\project\Demo"])
        for sid in ("mine", "bad", "foreign"):
            self.make_jsonl(sid)
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual([r.session_id for r in res.wrong_owner], ["bad"])
        self.assertEqual([r.session_id for r in res.foreign_owner], ["foreign"])
        self.assertEqual(res.cloud_rows, 1)
        self.assertEqual(res.unregistered, [])
        self.assertEqual(res.orphan_rows, [])

        outcome = self.repair()
        self.assertTrue(outcome["ok"], outcome["error"])
        self.assertEqual(self.db_rows()["bad"][1], "")
        self.assertEqual(self.db_rows()["foreign"][1], OTHER_UID)
        self.assertEqual(self.db_rows()["cloud"][1], OTHER_UID)

    def test_soft_deleted_rows_ignored(self):
        self.make_db(rows=[_row("gone", user_id="imported", deleted_at=5)],
                     workspaces=[r"D:\project\Demo"])
        self.make_jsonl("gone")
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.wrong_owner, [])
        self.assertEqual(res.orphan_rows, [])
        self.assertEqual(res.unregistered, [])

    def test_unregistered_jsonl_collected(self):
        """库里没登记的事件流：按正文补登记，标题/时间取自事件流。"""
        self.make_db(rows=[])
        self.make_jsonl("new1", slug="demo", cwd=r"D:\project\Demo", title="新会话")
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual([u.session_id for u in res.unregistered], ["new1"])
        un = res.unregistered[0]
        self.assertEqual((un.cwd, un.title, un.slug), (r"D:\project\Demo", "新会话", "demo"))
        self.assertEqual(un.created_ms, 1730000000000)
        self.assertIn(r"D:\project\Demo", res.missing_workspaces)

    def test_quickask_sidecar_skipped(self):
        self.make_db(rows=[])
        self.make_jsonl("qa1", quickask=True)
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.unregistered, [])

    def test_jsonl_without_cwd_skipped(self):
        self.make_db(rows=[])
        self.make_jsonl("nocwd", events=[{"type": "message", "role": "user",
                                          "content": [{"text": "hi"}]}])
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.unregistered, [])
        self.assertEqual(res.skipped_no_cwd, ["nocwd"])

    def test_orphan_row_reported_only(self):
        self.make_db(rows=[_row("ghost", user_id="")], workspaces=[r"D:\project\Demo"])
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual([o.session_id for o in res.orphan_rows], ["ghost"])
        self.assertFalse(res.healthy)
        self.assertEqual(res.fixable_total(), 0)
        self.repair()
        self.assertIn("ghost", self.db_rows())          # 不删行

    def test_missing_workspace_row_normalized(self):
        """workspaces 里已有同一路径的另一写法 ⇒ 不算缺行（不重复插行）。"""
        self.make_db(rows=[_row("s1", cwd="D:\\Project\\Demo\\", user_id="")],
                     workspaces=[r"d:/project/demo"])
        self.make_jsonl("s1")
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.missing_workspaces, [])

    def test_auto_generated_cwd_needs_no_workspace_row(self):
        cwd = r"C:\Users\u\WorkBuddy\2026-10-05-21-38-00"
        self.make_db(rows=[_row("s1", cwd=cwd, user_id="")])
        self.make_jsonl("s1", cwd=cwd)
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.missing_workspaces, [])

    def test_workspaces_table_absent_skips_that_category(self):
        self.make_db(rows=[_row("s1", user_id="")])
        self.drop_workspaces_table()
        self.make_jsonl("s1")
        res = wr.detect_wb_sessions(self.home)
        self.assertTrue(res.workspace_table_missing)
        self.assertEqual(res.missing_workspaces, [])
        self.assertIn("未检查", "\n".join(res.summary_lines()))

    def test_sessions_without_transport_column_still_read(self):
        """旧构建没有 transport / deleted_at 列：按 pragma 现取列，不能当成「零会话」。"""
        self.make_db(rows=[_row("s1", user_id="")], with_transport=False)
        self.make_jsonl("s1")
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(res.rows_total, 1)
        self.assertEqual(res.orphan_rows, [])
        self.assertEqual(res.cloud_rows, 0)

    def test_stale_empty_snapshot_detected(self):
        self.make_db(rows=[_row("s1", user_id="")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        path = self.make_snapshot(ids=[])
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual([s.path for s in res.stale_snapshot], [path])
        self.assertIn("快照为空", res.stale_snapshot[0].reason)

    def test_snapshot_matching_is_fresh(self):
        self.make_db(rows=[_row("s1", user_id="")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        self.make_snapshot(ids=["s1"])
        self.assertEqual(wr.detect_wb_sessions(self.home).stale_snapshot, [])

    def test_no_db(self):
        self.make_jsonl("s1")
        res = wr.detect_wb_sessions(self.home)
        self.assertFalse(res.db_exists)
        self.assertIn("未找到会话索引库", "\n".join(res.summary_lines()))
        outcome = self.repair()
        self.assertFalse(outcome["ok"])
        self.assertIn("未找到会话索引库", outcome["error"])


class TestApply(WbHomeTestCase):
    def test_ownership_fixed_and_snapshot_moved(self):
        self.make_db(rows=[_row("s1", user_id="imported"), _row("s2", user_id="imported")],
                     workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        self.make_jsonl("s2", slug="demo")
        snap = self.make_snapshot(ids=[])
        res = wr.detect_wb_sessions(self.home)
        self.assertEqual(len(res.wrong_owner), 2)

        outcome = self.repair()
        self.assertTrue(outcome["ok"], outcome["error"])
        self.assertEqual(outcome["ownership"], 2)
        self.assertEqual(self.db_rows()["s1"][1], "")
        self.assertEqual(self.db_rows()["s2"][1], "")
        self.assertFalse(os.path.exists(snap))
        self.assertTrue(any(name.startswith(os.path.basename(snap) + ".bak.")
                            for name in os.listdir(os.path.join(self.home, UID))))
        # 备份过库
        self.assertTrue([b for b in outcome["backups"]
                         if os.path.basename(b).startswith("workbuddy.db")])
        # 复检通过
        self.assertTrue(wr.detect_wb_sessions(self.home).healthy)

    def test_unregistered_and_workspace_inserted(self):
        self.make_db(rows=[])
        self.make_jsonl("new1", title="新会话")
        outcome = self.repair()
        self.assertTrue(outcome["ok"], outcome["error"])
        self.assertEqual(outcome["registered"], ["new1"])
        row = self.db_rows()["new1"]
        self.assertEqual((row[1], row[2], row[3], row[4]), ("", r"D:\project\Demo", "新会话", "import"))
        con = sqlite3.connect(os.path.join(self.home, "workbuddy.db"))
        try:
            self.assertIsNotNone(con.execute("select 1 from workspaces where path=?",
                                             (r"D:\project\Demo",)).fetchone())
        finally:
            con.close()
        self.assertTrue(wr.detect_wb_sessions(self.home).healthy)

    def test_empty_plan_changes_nothing(self):
        self.make_db(rows=[_row("s1", user_id="")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        self.make_snapshot(ids=["s1"])
        plan = wr.plan_wb_repair(self.home)
        self.assertTrue(plan.empty)
        outcome = self.repair()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["backups"], [])
        self.assertEqual(outcome["snapshots_moved"], [])

    def test_locked_db_refuses_without_touching(self):
        """拿不到写锁（WorkBuddy 正在运行）：整体不写，如实报错。"""
        self.make_db(rows=[_row("s1", user_id="imported")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        with mock.patch.object(wr, "_open_sqlite_rw",
                               side_effect=sqlite3.OperationalError("database is locked")):
            outcome = self.repair()
        self.assertFalse(outcome["ok"])
        self.assertIn("无法取得 workbuddy.db 写锁", outcome["error"])
        self.assertIn("退出 WorkBuddy", outcome["error"])
        self.assertEqual(self.db_rows()["s1"][1], "imported")

    def test_write_failure_rolls_back(self):
        """写库中途失败：整笔事务回滚，不留「归属改了、工作区没登记」的半改状态。

        计划生成后把 ``workspaces`` 表删掉，补工作区那一步必然失败。
        """
        self.make_db(rows=[_row("s1", user_id="imported")])      # 库里没有工作区行
        self.make_jsonl("s1")
        plan = wr.plan_wb_repair(self.home)
        self.assertEqual(plan.fix_owner_ids, ["s1"])
        self.assertEqual(plan.add_workspaces, [r"D:\project\Demo"])
        self.drop_workspaces_table()

        outcome = wr.apply_wb_repair(self.home, plan)
        self.assertFalse(outcome["ok"])
        self.assertIn("写入会话索引库失败", outcome["error"])
        self.assertEqual(self.db_rows()["s1"][1], "imported")     # 归属改动已回滚

    def test_snapshot_backup_failure_keeps_file(self):
        self.make_db(rows=[_row("s1", user_id="")], workspaces=[r"D:\project\Demo"])
        self.make_jsonl("s1")
        snap = self.make_snapshot(ids=[])
        with mock.patch.object(wr, "_backup_file",
                               side_effect=lambda p: "" if p.endswith(wr.SNAPSHOT_NAME)
                               else "%s.bak.x" % p):
            outcome = self.repair()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["snapshots_moved"], [])
        self.assertTrue(os.path.exists(snap), "备份失败时不该移走唯一现场")
        self.assertIn("备份失败", "\n".join(outcome["snapshot_errors"]))


if __name__ == "__main__":
    unittest.main()
