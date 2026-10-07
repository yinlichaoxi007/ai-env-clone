"""子代理关系修复（把旧版导入留下的顶层子代理会话改造成 DSH 原生子代理会话）测试。

全部用**合成数据**：临时目录里造一个 DSH 主目录（workspace.json + 会话日志 + 投影缓存）
与一个 ZCode 库（1 个父会话 + 2 个子代理会话），不触碰任何真实用户数据。
缺少 zstd 后端时整组跳过。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_env_clone import dsh_repair, subagent_relink  # noqa: E402
from ai_env_clone import session_migration as sm  # noqa: E402


def _zstd_pair():
    try:
        import zstandard  # type: ignore
    except ImportError:
        return None
    return (lambda b: zstandard.ZstdCompressor().compress(b),
            lambda b: zstandard.ZstdDecompressor().stream_reader(
                __import__("io").BytesIO(b)).read())


ZSTD = _zstd_pair()
PARENT_TITLE = "父会话标题"
CHILD_TITLES = ["子代理任务 A", "子代理任务 B"]


def _make_source_db(root: str) -> str:
    """造一个 ZCode 数据根（1 父 + 2 子），标题与首条用户正文与 DSH 侧一致。"""
    db_dir = os.path.join(root, "cli", "db")
    os.makedirs(db_dir, exist_ok=True)
    db = os.path.join(db_dir, "db.sqlite")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, title TEXT, directory TEXT, parent_id TEXT,
            task_type TEXT, time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT, sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER
        );
        """
    )
    rows = [("s_parent", PARENT_TITLE, "", "interactive")]
    rows += [("s_child_%d" % i, t, "s_parent", "subagent_child")
             for i, t in enumerate(CHILD_TITLES)]
    for i, (sid, title, parent, task) in enumerate(rows):
        con.execute("insert into session values(?,?,?,?,?,?,?)",
                    (sid, title, r"D:\project\Demo", parent, task, 1000 + i, 2000 + i))
        con.execute("insert into message values(?,?,?,?,?,?)",
                    ("m_" + sid, sid, 1000 + i, 1000 + i,
                     json.dumps({"role": "user", "time": {"created": 1000 + i}},
                                ensure_ascii=False), 0))
        con.execute("insert into part values(?,?,?,?,?,?,?)",
                    ("p_" + sid, "m_" + sid, sid, 1000 + i, 1000 + i,
                     json.dumps({"type": "text", "text": "提问 " + title},
                                ensure_ascii=False), 0))
        con.execute("insert into message values(?,?,?,?,?,?)",
                    ("a_" + sid, sid, 1100 + i, 1100 + i,
                     json.dumps({"role": "assistant", "time": {"created": 1100 + i}},
                                ensure_ascii=False), 1))
        con.execute("insert into part values(?,?,?,?,?,?,?)",
                    ("pa_" + sid, "a_" + sid, sid, 1100 + i, 1100 + i,
                     json.dumps({"type": "text", "text": "回答 " + title},
                                ensure_ascii=False), 0))
    con.commit()
    con.close()
    return db


@unittest.skipUnless(ZSTD is not None, "未安装 zstandard，跳过子代理关系修复用例")
class RelinkFixture(unittest.TestCase):
    """公共脚手架：一个 DSH 主目录（父 + 2 个旧子代理 + 可选已发布 v4）。"""

    publish_parent_v4 = False

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="relink_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.zc_root = os.path.join(self.tmp, "zcode")
        self.db = _make_source_db(self.zc_root)
        self.home = os.path.join(self.tmp, "dsh")
        self.ws_dir = "--D-project-Demo--"
        os.makedirs(os.path.join(self.home, "storages", "session_projcache", "sessions"))
        self.parent_id = "session-parent-0001"
        self.child_ids = ["session-child-0001", "session-child-0002"]
        self._write_session(self.parent_id, PARENT_TITLE, "提问 " + PARENT_TITLE)
        for sid, title in zip(self.child_ids, CHILD_TITLES):
            self._write_session(sid, title, "提问 " + title)
        self._write_index()
        for sid in [self.parent_id] + self.child_ids:
            with open(os.path.join(self.home, "storages", "session_projcache",
                                   "sessions", sid + ".json"), "w", encoding="utf-8") as f:
                json.dump({"version": 7, "record": {"identity": {}, "rows": {}}}, f)
        if self.publish_parent_v4:
            src = self._log_path(self.parent_id)
            shutil.copy2(src, os.path.join(os.path.dirname(src),
                                           "session.v4.jsonl.zstd"))

    def _log_path(self, sid: str) -> str:
        return os.path.join(self.home, "sessions", self.ws_dir, sid,
                            "session.v3.jsonl.zstd")

    def _write_session(self, sid: str, title: str, first_user: str) -> None:
        sess = sm.Session(source_tool="zcode", title=title, scope=r"D:\project\Demo",
                          messages=[sm.SessionMessage(role="user", content=first_user),
                                    sm.SessionMessage(role="assistant", content="回答 " + title)])
        text, _ms, _t = sm._dsh_v3_text(sid, sess, r"D:\project\Demo")
        sm._dsh_write_text(self._log_path(sid), text)

    def _write_index(self) -> None:
        doc = {"unit": {"name": "workspace", "version": 2},
               "global": {"initialized": True, "workspaceIds": ["ws-1"],
                          "archivedSessionIds": [], "pinnedSessionIds": []},
               "tables": {"workspaces": {"ws-1": {
                   "path": r"D:\project\Demo", "title": "Demo",
                   "sessionIds": [self.parent_id] + self.child_ids,
                   "createdAt": "2026-10-01T00:00:00.000Z",
                   "updatedAt": "2026-10-01T00:00:00.000Z"}}}}
        with open(os.path.join(self.home, "storages", "workspace.json"), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)

    def read_log(self, path: str) -> list:
        with open(path, "rb") as fh:
            raw = ZSTD[1](fh.read())
        return [json.loads(l) for l in raw.decode("utf-8").split("\n") if l.strip()]


class TestHealthSummaryCounts(unittest.TestCase):
    """健康检测的摘要必须**每一类都给数量**（含缓存映射与旧导入子代理），且只给数量。"""

    def test_every_category_listed_even_when_zero(self) -> None:
        result = dsh_repair.DetectResult()
        result.sessions_total = 3
        text = "\n".join(result.summary_lines())
        for keyword in ("未分组会话：0 个", "索引问题：0 处", "旧格式会话", "重复调用 id 会话",
                        "子代理描述符版本不合规会话", "投影缓存陈旧会话（缓存映射",
                        "旧版导入的子代理会话：0 个", "需注意项合计：0 处"):
            self.assertIn(keyword, text)
        self.assertEqual(result.attention_total, 0)

    def test_attention_total_counts_relink_and_cache(self) -> None:
        result = dsh_repair.DetectResult()
        result.sessions_total = 1
        result.projcache_stale = [dsh_repair.StaleProjcache(session_id="s1", path="p",
                                                            reason="r", hidden=True)]
        result.relink_targets = [object(), object()]
        text = "\n".join(result.summary_lines())
        self.assertIn("投影缓存陈旧会话（缓存映射与日志不符", text)
        self.assertIn("旧版导入的子代理会话（当前是顶层会话", text)
        self.assertEqual(result.attention_total, 3)
        self.assertFalse(result.healthy)


class TestRelinkPlan(RelinkFixture):
    publish_parent_v4 = True

    def test_plan_finds_children_and_resolves_parent(self) -> None:
        plan = dsh_repair.plan_dsh_repair(self.home, relink_source=self.db)
        relink = plan.relink
        self.assertIsNotNone(relink)
        self.assertEqual(len(relink.targets), 2)
        self.assertEqual({t.child_id for t in relink.targets}, set(self.child_ids))
        self.assertEqual({t.parent_id for t in relink.targets}, {self.parent_id})
        self.assertTrue(all(not t.new_id.startswith("session-") for t in relink.targets))
        # 来源里的父会话本身不该被改造
        self.assertEqual([sid for sid, _r in relink.skipped], [self.parent_id])
        self.assertIn("父会话", relink.skipped[0][1])
        # 父会话已发布 v4 → 计划里标出要移走
        self.assertTrue(all(t.parent_published_v4 for t in relink.targets))

    def test_auto_discovers_live_source_without_asking(self) -> None:
        """不指定任何来源：自动找到本机 ZCode 数据根就够了（用户无需做任何事）。"""
        with mock.patch.object(subagent_relink, "_source_dirs", return_value=[]):
            plan = subagent_relink.plan_subagent_relink(
                self.home, live_roots={"zcode": self.zc_root})
        self.assertEqual(len(plan.targets), 2)
        self.assertEqual({t.parent_id for t in plan.targets}, {self.parent_id})
        self.assertIn("本机 ZCode 数据", plan.source_note)

    def test_without_any_source_reports_honestly(self) -> None:
        """来源找不到时只上报，绝不猜（一条也不改）。"""
        with mock.patch.object(subagent_relink, "_source_dirs", return_value=[]):
            # live_roots 必须显式给一个**不存在**的路径：空 dict 会让 discover_sources
            # 回退探测本机真实 ~/.zcode——在有 ZCode 数据的机器上「无来源」前提不成立
            # （本机实测踩过：报告成了「本机 ZCode 数据：1 条会话」）。
            plan = subagent_relink.plan_subagent_relink(
                self.home,
                live_roots={"zcode": os.path.join(self.tmp, "no-such-zcode")})
        self.assertEqual(plan.targets, [])
        self.assertFalse(plan.empty)
        self.assertTrue(plan.source_note)
        self.assertIn("无法判定", plan.source_note)

    def test_relink_section_always_planned(self) -> None:
        """「修复会话数据」不再要求指定来源：未显式给来源也照样规划这一项。"""
        with mock.patch.object(subagent_relink, "_source_dirs", return_value=[]):
            plan = dsh_repair.plan_dsh_repair(self.home)
        self.assertIsNotNone(plan.relink)

    def test_bad_source_is_reported_not_crashed(self) -> None:
        plan = subagent_relink.plan_subagent_relink(self.home, os.path.join(self.tmp, "nope"))
        self.assertEqual(plan.targets, [])
        self.assertTrue(plan.source_note)
        self.assertTrue(plan.empty is False)

    def test_plan_is_read_only(self) -> None:
        before = sorted(os.listdir(os.path.join(self.home, "sessions", self.ws_dir)))
        dsh_repair.plan_dsh_repair(self.home, relink_source=self.db)
        after = sorted(os.listdir(os.path.join(self.home, "sessions", self.ws_dir)))
        self.assertEqual(before, after)
        # 也不该改动来源库
        self.assertTrue(os.path.isfile(self.db))


class TestConvertLog(RelinkFixture):
    def test_converted_log_is_native_subagent(self) -> None:
        old = self.read_log(self._log_path(self.child_ids[0]))
        with open(self._log_path(self.child_ids[0]), "rb") as fh:
            text = ZSTD[1](fh.read()).decode("utf-8")
        new_text = subagent_relink.convert_legacy_child_log(
            text, "11111111-2222-3333-4444-555555555555", self.parent_id)
        lines = [json.loads(l) for l in new_text.strip().split("\n")]
        header, events = lines[0], lines[1:]
        self.assertEqual(header["id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(header["origin"], "subagent")
        self.assertEqual(header["parentSession"], self.parent_id)
        self.assertEqual(header["delegationDepth"], 1)
        self.assertEqual(header["createdAt"], old[0]["createdAt"])   # 时间不变
        self.assertEqual(events[0]["type"], "subagent/descriptor")
        self.assertEqual(events[0]["data"]["version"], 3)
        self.assertEqual(events[0]["data"]["mode"], "one-shot")
        self.assertTrue(events[0]["data"]["provider"])
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        # 正文一字不改：原有的每条事件都在（只多出 seq 0 的 descriptor），类型顺序一致
        self.assertEqual([e["type"] for e in events[1:]],
                         [e["type"] for e in old[1:]])
        for before, after in zip(old[1:], events[1:]):
            # 同日志序号引用按设计后移，比较时剔除；其余字段必须逐字相同
            b = json.loads(json.dumps(before["data"]))
            a = json.loads(json.dumps(after["data"]))
            for key in ("messageSeqs", "sourceEventSeqs", "shadowedSeqs"):
                b.pop(key, None)
                a.pop(key, None)
            for key in ("startSeq", "endSeq", "sourceEventSeq"):
                b.pop(key, None)
                a.pop(key, None)
            self.assertEqual(b, a)
        # 同日志序号引用必须跟着后移
        for ev in events:
            if ev["type"] == "session/title":
                seqs = ev["data"].get("messageSeqs") or []
                self.assertTrue(all(0 <= s < len(events) for s in seqs), seqs)
                old_title = next(e for e in old if e["type"] == "session/title")
                old_seqs = old_title["data"].get("messageSeqs") or []
                self.assertEqual(seqs, [s + 1 for s in old_seqs])

    def test_rejects_garbage(self) -> None:
        for bad in ("", "not json\n", json.dumps({"type": "session"}) + "\n"):
            with self.assertRaises(ValueError):
                subagent_relink.convert_legacy_child_log(bad, "id", "parent")


class TestRelinkApply(RelinkFixture):
    publish_parent_v4 = True

    def _apply(self) -> dict:
        plan = dsh_repair.plan_dsh_repair(self.home, relink_source=self.db)
        self.assertEqual(len(plan.relink.targets), 2)
        return dsh_repair.apply_dsh_repair(self.home, plan, backup=True)

    def test_apply_moves_children_and_keeps_parent(self) -> None:
        outcome = self._apply()
        results = outcome["relink"]["targets"]
        self.assertTrue(all(r["ok"] for r in results), results)

        ws_path = os.path.join(self.home, "sessions", self.ws_dir)
        new_dirs = sorted(d for d in os.listdir(ws_path)
                          if os.path.isdir(os.path.join(ws_path, d))
                          and not d.startswith("session-"))
        self.assertEqual(len(new_dirs), 2)
        for name in new_dirs:
            events = self.read_log(os.path.join(ws_path, name, "session.v3.jsonl.zstd"))
            header, first = events[0], events[1]
            self.assertEqual(header["id"], name)
            self.assertEqual(header["origin"], "subagent")
            self.assertEqual(header["delegationDepth"], 1)
            self.assertEqual(header["parentSession"], self.parent_id)
            self.assertEqual(first["type"], "subagent/descriptor")
            self.assertEqual(first["data"]["version"], 3)
            self.assertEqual(first["seq"], 0)
        # 旧顶层会话目录被整移到 sessions/.removed/<工作区>/，且父会话目录仍在
        removed = os.path.join(self.home, "sessions", subagent_relink.REMOVED_DIR,
                               self.ws_dir)
        self.assertEqual(sorted(os.listdir(removed)), sorted(self.child_ids))
        self.assertTrue(os.path.isdir(os.path.join(ws_path, self.parent_id)))
        # 索引：子代理 id 摘除、父会话保留
        with open(os.path.join(self.home, "storages", "workspace.json"),
                  encoding="utf-8") as f:
            idx = json.load(f)
        listed = idx["tables"]["workspaces"]["ws-1"]["sessionIds"]
        self.assertEqual(listed, [self.parent_id])
        # 父会话已发布的 v4（以及它的投影缓存）被改名移走
        moved_v4 = [f for f in os.listdir(os.path.join(ws_path, self.parent_id))
                    if f.startswith("session.v4.jsonl.zstd.bak-")]
        self.assertEqual(len(moved_v4), 1)
        cache = os.path.join(self.home, "storages", "session_projcache", "sessions")
        backups = [f for f in os.listdir(cache) if ".bak-" in f]
        self.assertGreaterEqual(len(backups), 2)   # 2 条旧子代理缓存 + 父会话缓存

    def test_apply_is_idempotent_for_rerun(self) -> None:
        """再跑一次：旧会话已经不在原位，不会再产生一次改造。"""
        self._apply()
        plan = dsh_repair.plan_dsh_repair(self.home, relink_source=self.db)
        self.assertEqual(len(plan.relink.targets), 0)

    def test_unconvertible_log_is_reported_and_nothing_moved(self) -> None:
        """日志形态不对（这里是尾部多了半行坏 JSON）→ 该条失败、**不移动**旧目录。"""
        with open(self._log_path(self.child_ids[0]), "rb") as fh:
            text = ZSTD[1](fh.read()).decode("utf-8")
        sm._dsh_write_text(self._log_path(self.child_ids[0]), text + "{ 这不是 JSON\n")

        outcome = self._apply_first_only()
        result = outcome["relink"]["targets"][0]
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])
        # 失败的那条：旧目录仍在原位（没有半途移走）
        self.assertTrue(os.path.isdir(
            os.path.join(self.home, "sessions", self.ws_dir, self.child_ids[0])))

    def _apply_first_only(self) -> dict:
        plan = dsh_repair.plan_dsh_repair(self.home, relink_source=self.db)
        plan.relink.targets = [t for t in plan.relink.targets
                               if t.child_id == self.child_ids[0]]
        return dsh_repair.apply_dsh_repair(self.home, plan, backup=True)


if __name__ == "__main__":
    unittest.main()
