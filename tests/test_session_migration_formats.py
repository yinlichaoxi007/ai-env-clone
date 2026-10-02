"""新增会话格式（WorkBuddy / ZCode / DSH）解析与写出测试。

所有测试都在临时目录里**构造合成数据**，不触碰真实用户数据；
DSH 相关用例在缺少 zstd 后端时自动跳过（``skipUnless``）。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_env_clone import session_migration as sm


def _zstd_pair():
    """返回 (compress, decompress) 或 None（无后端时跳过 DSH 用例）。"""
    try:
        import zstandard  # type: ignore
    except ImportError:
        return None
    return (
        (lambda b: zstandard.ZstdCompressor().compress(b)),
        (lambda b: zstandard.ZstdDecompressor().stream_reader(__import__("io").BytesIO(b)).read()),
    )


ZSTD = _zstd_pair()


# --------------------------------------------------------------------------- #
# WorkBuddy
# --------------------------------------------------------------------------- #
class TestWorkBuddyParse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wb_parse_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write_jsonl(self, events) -> str:
        p = os.path.join(self.tmp, "session.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_parse_roles_and_content(self) -> None:
        p = self._write_jsonl([
            {"type": "ai-title", "aiTitle": "测试标题", "timestamp": 1000},
            {"type": "message", "role": "user", "timestamp": 1001,
             "content": [{"type": "input_text", "text": "你好"}]},
            {"type": "reasoning", "timestamp": 1002,
             "rawContent": [{"type": "reasoning_text", "text": "让我想想"}]},
            {"type": "function_call", "timestamp": 1003, "callId": "c1",
             "name": "Read", "arguments": '{"path":"a.txt"}'},
            {"type": "function_call_result", "timestamp": 1004, "callId": "c1",
             "name": "Read", "output": {"type": "text", "text": "文件内容"}},
            {"type": "message", "role": "assistant", "timestamp": 1005,
             "content": [{"type": "output_text", "text": "这是回答"}]},
        ])
        s = sm.SessionParser.parse_workbuddy(p)
        self.assertEqual(s.source_tool, "workbuddy")
        self.assertEqual(s.title, "测试标题")
        self.assertEqual([m.role for m in s.messages], ["user", "assistant"])
        self.assertEqual(s.messages[0].content, "你好")
        a = s.messages[1]
        self.assertIn("这是回答", a.content)
        self.assertIn("让我想想", a.reasoning_content)
        self.assertEqual(len(a.tool_calls), 1)
        self.assertEqual(a.tool_calls[0]["name"], "Read")

    def test_parse_skips_broken_lines(self) -> None:
        p = os.path.join(self.tmp, "broken.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json}\n")
            f.write(json.dumps({"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "ok"}]}) + "\n")
        s = sm.SessionParser.parse_workbuddy(p)
        self.assertEqual(len(s.messages), 1)
        self.assertEqual(s.messages[0].content, "ok")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(OSError):
            sm.SessionParser.parse_workbuddy(os.path.join(self.tmp, "nope.jsonl"))


class TestWorkBuddyWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wb_write_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.wb = os.path.join(self.tmp, "wb")
        os.makedirs(self.wb)

    def _make_db(self) -> str:
        """建一个与实测 schema 等价的精简 workbuddy.db。"""
        db = os.path.join(self.wb, "workbuddy.db")
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
                title TEXT, custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                last_activity_at INTEGER, deleted_at INTEGER,
                is_playground INTEGER NOT NULL DEFAULT 0, source_mode TEXT,
                is_background_automation INTEGER, mode TEXT, model TEXT
            );
            CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER NOT NULL);
            INSERT INTO sessions(id,cwd,user_id,title,status,created_at,updated_at,is_playground)
                VALUES('existing','C:\\\\x','u-1','旧会话','completed',1,2,0);
            """
        )
        con.commit()
        con.close()
        return db

    def _session(self) -> "sm.Session":
        return sm.Session(source_tool="reasonix", title="导入的会话", messages=[
            sm.SessionMessage(role="user", content="问题", created_at="2026-09-22T10:00:00.000Z"),
            sm.SessionMessage(role="assistant", content="回答",
                              reasoning_content="推理", created_at="2026-09-22T10:00:05.000Z"),
        ])

    def test_writes_jsonl_and_registers_db(self) -> None:
        self._make_db()
        warns = []
        sid = sm.SessionWriter.write_workbuddy(
            self._session(), self.wb, workspace_slug="demo",
            cwd=r"D:\project\Demo", warn=warns.append,
        )
        self.assertEqual(warns, [])
        jsonl = os.path.join(self.wb, "projects", "demo", sid + ".jsonl")
        self.assertTrue(os.path.isfile(jsonl))

        back = sm.SessionParser.parse_workbuddy(jsonl)
        self.assertEqual([m.role for m in back.messages], ["user", "assistant"])
        self.assertEqual(back.title, "导入的会话")
        self.assertEqual(back.messages[1].reasoning_content, "推理")

        con = sqlite3.connect(os.path.join(self.wb, "workbuddy.db"))
        try:
            row = con.execute("select cwd, user_id, title, status from sessions where id=?",
                              (sid,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[1], "u-1")           # 沿用已有 user_id
            self.assertEqual(row[2], "导入的会话")
            # 既有会话未被改写
            self.assertEqual(con.execute("select count(*) from sessions").fetchone()[0], 2)
            # workspaces 登记
            self.assertIsNotNone(con.execute(
                "select 1 from workspaces where path=?", (r"D:\project\Demo",)).fetchone())
        finally:
            con.close()

    def test_missing_db_only_warns(self) -> None:
        warns = []
        sid = sm.SessionWriter.write_workbuddy(
            self._session(), self.wb, workspace_slug="demo", cwd="D:/p", warn=warns.append,
        )
        self.assertTrue(os.path.isfile(
            os.path.join(self.wb, "projects", "demo", sid + ".jsonl")))
        self.assertTrue(warns, "缺 workbuddy.db 时应给出提示")


# --------------------------------------------------------------------------- #
# ZCode（只读解析）
# --------------------------------------------------------------------------- #
class TestZCodeParse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zc_parse_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "db.sqlite")
        con = sqlite3.connect(self.db)
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER
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
        con.execute("insert into session values('s1','会话标题','D:\\proj',1000,2000)")
        con.execute("insert into message values('m1','s1',1000,1000,?,0)",
                    (json.dumps({"role": "user", "time": {"created": 1000}}, ensure_ascii=False),))
        con.execute("insert into part values('p1','m1','s1',1000,1000,?,0)",
                    (json.dumps({"type": "text", "text": "用户问题"}, ensure_ascii=False),))
        con.execute("insert into message values('m2','s1',1100,1100,?,1)",
                    (json.dumps({"role": "assistant", "time": {"created": 1100}}, ensure_ascii=False),))
        con.execute("insert into part values('p2','m2','s1',1100,1100,?,0)",
                    (json.dumps({"type": "reasoning", "text": "推理过程"}, ensure_ascii=False),))
        con.execute("insert into part values('p3','m2','s1',1100,1100,?,1)",
                    (json.dumps({"type": "text", "text": "助手回答"}, ensure_ascii=False),))
        con.execute("insert into part values('p4','m2','s1',1100,1100,?,2)",
                    (json.dumps({"type": "tool", "tool": "Read", "callID": "c9",
                                 "state": {"status": "completed"}}, ensure_ascii=False),))
        con.commit()
        con.close()

    def test_parse_session(self) -> None:
        s = sm.SessionParser.parse_zcode(self.db, "s1")
        self.assertEqual(s.source_tool, "zcode")
        self.assertEqual(s.title, "会话标题")
        self.assertEqual(s.scope, r"D:\proj")
        self.assertEqual([m.role for m in s.messages], ["user", "assistant"])
        self.assertEqual(s.messages[0].content, "用户问题")
        self.assertIn("助手回答", s.messages[1].content)
        self.assertIn("推理过程", s.messages[1].reasoning_content)
        self.assertEqual(len(s.messages[1].tool_calls), 1)
        self.assertEqual(s.messages[1].tool_calls[0]["name"], "Read")

    def test_parse_unknown_session_returns_empty(self) -> None:
        s = sm.SessionParser.parse_zcode(self.db, "does-not-exist")
        self.assertEqual(s.messages, [])

    def test_parse_does_not_modify_source(self) -> None:
        before = os.path.getsize(self.db)
        sm.SessionParser.parse_zcode(self.db, "s1")
        self.assertEqual(os.path.getsize(self.db), before)


# --------------------------------------------------------------------------- #
# DSH（zstd）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(ZSTD is not None, "未安装 zstandard，跳过 DSH 用例")
class TestDsh(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dsh_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.dsh = os.path.join(self.tmp, "dsh")
        os.makedirs(os.path.join(self.dsh, "storages"))
        with open(os.path.join(self.dsh, "storages", "workspace.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"unit": {"name": "workspace", "version": 2},
                       "global": {"initialized": True, "workspaceIds": []},
                       "tables": {"workspaces": {}}}, f)
        with open(os.path.join(self.dsh, "storages", "session_projcache.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"unit": {"name": "session_projcache", "version": 3},
                       "global": None, "tables": {"sessions": {}}}, f)

    def _session(self) -> "sm.Session":
        return sm.Session(source_tool="workbuddy", title="DSH 标题", messages=[
            sm.SessionMessage(role="user", content="问题", created_at="2026-09-22T10:00:00.000Z"),
            sm.SessionMessage(role="assistant", content="回答", created_at="2026-09-22T10:00:05.000Z"),
        ])

    def test_write_then_parse(self) -> None:
        warns = []
        sid = sm.SessionWriter.write_dsh(
            self._session(), self.dsh, cwd=r"D:\project\Demo", warn=warns.append)
        self.assertTrue(sid.startswith("session-"))
        self.assertEqual(warns, [])
        # 落地文件 + 工作区登记
        wsdir = sm._dsh_workspace_dirname(r"D:\project\Demo")
        self.assertEqual(wsdir, "--D-project-Demo--")
        f = os.path.join(self.dsh, "sessions", wsdir, sid, "session.v3.jsonl.zstd")
        self.assertTrue(os.path.isfile(f))
        with open(os.path.join(self.dsh, "storages", "workspace.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        entries = list(data["tables"]["workspaces"].values())
        self.assertEqual(len(entries), 1)
        self.assertIn(sid, entries[0]["sessionIds"])

        back = sm.SessionParser.parse_dsh(f)
        self.assertEqual(back.title, "DSH 标题")
        self.assertEqual([m.role for m in back.messages], ["user", "assistant"])
        self.assertEqual(back.messages[0].content, "问题")

    def test_multiframe_decompression(self) -> None:
        """多帧 zstd 必须整体解压（否则大文件只解出首帧）。"""
        compress, _ = ZSTD
        header = json.dumps({"type": "session", "version": 3, "id": "session-x"},
                            ensure_ascii=False).encode("utf-8") + b"\n"
        event = json.dumps(
            {"type": "user/message", "seq": 1, "time": 1,
             "data": {"role": "user", "content": [{"type": "text", "text": "多帧内容"}]}},
            ensure_ascii=False).encode("utf-8") + b"\n"
        # 手工拼一个两帧文件（模拟 DSH 的分帧写入）
        payload = compress(header) + compress(event)
        d = os.path.join(self.tmp, "sessions", "--x--", "session-x")
        os.makedirs(d)
        f = os.path.join(d, "session.v3.jsonl.zstd")
        with open(f, "wb") as fh:
            fh.write(payload)
        s = sm.SessionParser.parse_dsh(f)
        self.assertEqual(len(s.messages), 1)
        self.assertEqual(s.messages[0].content, "多帧内容")

    def test_parse_dir_uses_highest_generation(self) -> None:
        compress, _ = ZSTD
        d = os.path.join(self.tmp, "sessions", "--y--", "session-y")
        os.makedirs(d)
        old = json.dumps({"type": "session", "version": 0, "id": "session-y"},
                         ensure_ascii=False).encode("utf-8") + b"\n"
        new = (
            json.dumps({"type": "session", "version": 3, "id": "session-y"},
                       ensure_ascii=False).encode("utf-8") + b"\n" +
            json.dumps(
                {"type": "user/message", "seq": 1, "time": 1,
                 "data": {"role": "user",
                          "content": [{"type": "text", "text": "最新代际"}]}},
                ensure_ascii=False).encode("utf-8") + b"\n"
        )
        with open(os.path.join(d, "session.jsonl.zstd"), "wb") as fh:
            fh.write(compress(old))
        with open(os.path.join(d, "session.v3.jsonl.zstd"), "wb") as fh:
            fh.write(compress(new))
        s = sm.SessionParser.parse_dsh(d)
        self.assertEqual([m.content for m in s.messages], ["最新代际"])

    def test_missing_workspace_json_only_warns(self) -> None:
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        warns = []
        sid = sm.SessionWriter.write_dsh(self._session(), empty, cwd="D:/p",
                                        warn=warns.append)
        self.assertTrue(sid)
        self.assertTrue(warns)


# --------------------------------------------------------------------------- #
# migrate_session 调度
# --------------------------------------------------------------------------- #
@unittest.skipUnless(ZSTD is not None, "未安装 zstandard，跳过调度用例")
class TestMigrateDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mig_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _workbuddy_source(self) -> str:
        d = os.path.join(self.tmp, "src_projects", "ws1")
        os.makedirs(d)
        p = os.path.join(d, "abc.jsonl")
        events = [
            {"type": "ai-title", "aiTitle": "源标题", "timestamp": 1000},
            {"type": "message", "role": "user", "timestamp": 1001,
             "content": [{"type": "input_text", "text": "源问题"}]},
            {"type": "message", "role": "assistant", "timestamp": 1002,
             "content": [{"type": "output_text", "text": "源回答"}]},
        ]
        with open(p, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_workbuddy_to_reasonix(self) -> None:
        src = self._workbuddy_source()
        root = os.path.join(self.tmp, "rx")
        sid = sm.migrate_session("workbuddy", src, "reasonix", root, scope="proj")
        path = os.path.join(root, "proj", "sessions", sid + "-session.jsonl")
        self.assertTrue(os.path.isfile(path))
        back = sm.SessionParser.parse_reasonix(path)
        self.assertEqual([m.content for m in back.messages], ["源问题", "源回答"])

    def test_workbuddy_to_dsh(self) -> None:
        src = self._workbuddy_source()
        dsh = os.path.join(self.tmp, "dsh")
        os.makedirs(os.path.join(dsh, "storages"))
        with open(os.path.join(dsh, "storages", "workspace.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"global": {"workspaceIds": []}, "tables": {"workspaces": {}}}, f)
        sid = sm.migrate_session("workbuddy", src, "dsh", dsh,
                                 workspace_id=r"D:\project\Demo", warn=lambda m: None)
        back = sm.SessionParser.parse_dsh(os.path.join(
            dsh, "sessions", "--D-project-Demo--", sid))
        self.assertEqual(len(back.messages), 2)

    def test_unknown_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            sm.migrate_session("nope", "x", "reasonix", self.tmp)

    def test_unknown_target_raises(self) -> None:
        with self.assertRaises(ValueError):
            sm.migrate_session("workbuddy", self._workbuddy_source(), "nope", self.tmp)

    def test_zcode_requires_session_id(self) -> None:
        with self.assertRaises(ValueError):
            sm.migrate_session("zcode", "db.sqlite", "reasonix", self.tmp)


# --------------------------------------------------------------------------- #
# 来源扫描 / 目标根
# --------------------------------------------------------------------------- #
class TestScanners(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="scan_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_default_source_root_nonempty(self) -> None:
        for tool in ("reasonix", "codebuddy", "workbuddy", "zcode", "dsh"):
            with self.subTest(tool=tool):
                self.assertTrue(sm.default_source_root(tool))

    def test_scan_workbuddy(self) -> None:
        d = os.path.join(self.tmp, "projects", "ws1")
        os.makedirs(d)
        with open(os.path.join(d, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "标题A"}) + "\n")
        items = sm.list_source_sessions("workbuddy", self.tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], os.path.join(d, "s1.jsonl"))

    def test_scan_missing_root_returns_empty(self) -> None:
        self.assertEqual(sm.list_source_sessions("workbuddy", os.path.join(self.tmp, "no")), [])

    def test_scan_unknown_tool_returns_empty(self) -> None:
        self.assertEqual(sm.list_source_sessions("nope", self.tmp), [])

    def test_default_target_root_nonempty(self) -> None:
        for tool in ("reasonix", "codebuddy", "workbuddy", "dsh"):
            with self.subTest(tool=tool):
                self.assertTrue(sm.default_target_root(tool))


if __name__ == "__main__":
    unittest.main()
