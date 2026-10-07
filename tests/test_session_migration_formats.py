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
            self.assertEqual(row[1], "")              # 空串＝WorkBuddy 对导入行的约定，侧栏才可见
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

    def test_user_fork_session_is_not_subagent(self) -> None:
        """DSH「分叉按钮」＝派生新会话：header 带 ``parentSession`` 但
        ``delegationDepth: 0`` 且**无 ``origin``**（2026-10-07 真实样本
        session-18005063 回归钉）。它是用户自己的会话，**不得**按子代理处理
        ——旧判定「有 parentSession 即子代理」会把它从导入列表里吞掉。"""
        lines = [
            json.dumps({"type": "session", "version": 4, "id": "session-18005063",
                        "createdAt": 1791343777195, "cwd": "D:\\project\\TeaVision",
                        "parentSession": "session-79b5aaee", "isSeeded": True,
                        "delegationDepth": 0, "agentPreset": "standard"}),
            json.dumps({"type": "user/message", "seq": 0, "time": 1791343719746,
                        "data": {"content": [{"type": "text", "text": "hello"}]}}),
            json.dumps({"type": "assistant/message", "seq": 1, "time": 1791343720000,
                        "data": {"message": {"content": [
                            {"type": "output_text", "text": "Hi!"}]}}}),
        ]
        p = os.path.join(self.tmp, "fork-session.v4.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # 明文夹具：给解压函数打恒等桩（真实文件是 zstd，但判定逻辑与压缩无关）
        with mock.patch.object(sm, "_dsh_decompress_bytes",
                               side_effect=lambda b: b):
            s = sm.SessionParser.parse_dsh(p)
        self.assertFalse(s.is_subagent)
        self.assertEqual(s.parent_source_id, "session-79b5aaee")   # 父会话线索保留
        self.assertEqual([m.content for m in s.messages], ["hello", "Hi!"])

    def test_written_log_has_single_line_header_frame(self) -> None:
        """DSH 只解**首帧**取 header，且要求首帧明文恰好一行 ⇒ 必须多帧写。

        若把整份文本压成单帧，DSH 判首帧非法（``SessionPersistenceCorruptionError``）
        并在 ``listArtifacts`` 里静默跳过该会话：会话列表与工作区归属都看不到它，
        界面表现为「工作区有标题、里面没有会话」。
        """
        from ai_env_clone import dsh_repair as dr

        sid = sm.SessionWriter.write_dsh(
            self._session(), self.dsh, cwd=r"D:\project\Demo")
        f = os.path.join(self.dsh, "sessions", "--D-project-Demo--", sid,
                         "session.v3.jsonl.zstd")
        with open(f, "rb") as fh:
            raw = fh.read()
        _, decompress = ZSTD
        text = decompress(raw).decode("utf-8")
        frames = dr._scan_zstd_frames(raw)
        # 首帧 = 仅 header 一行；事件行在后续帧里拼接
        self.assertGreaterEqual(len(frames), 2)
        first = decompress(raw[frames[0][0]:frames[0][1]]).decode("utf-8")
        self.assertEqual(first.count("\n"), 1)
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(json.loads(first)["type"], "session")
        # 事件行跨帧拼接后仍完整
        rest = decompress(raw[frames[1][0]:]).decode("utf-8")
        self.assertIn('"user/message"', rest)
        self.assertIn('"assistant/message"', rest)
        self.assertEqual(len(text.rstrip("\n").split("\n")),
                         first.count("\n") + rest.count("\n"))

    def _written_rows(self, session) -> "list":
        """写出会话并解压成事件行列表（``rows[0]`` 是 header）。"""
        sid = sm.SessionWriter.write_dsh(session, self.dsh, cwd=r"D:\project\Demo")
        f = os.path.join(self.dsh, "sessions", "--D-project-Demo--", sid,
                         "session.v3.jsonl.zstd")
        with open(f, "rb") as fh:
            _, decompress = ZSTD
            text = decompress(fh.read()).decode("utf-8")
        return [json.loads(l) for l in text.split("\n") if l.strip()]

    def test_written_log_satisfies_dsh_relationship_rules(self) -> None:
        """回归：旧 writer 的 v3 不满足 DSH 关系校验 → 加载器静默丢弃成空壳 v4。

        逐条锁定 DSH 官方 session-format 迁移+校验链要求的关系；任一条不成立，
        会话都会被改写成「有标题、无内容」的空壳（用户实测的空会话根因）。
        """
        rows = self._written_rows(self._session())
        header, events = rows[0], rows[1:]
        # ① header 无 seq；事件 seq == 数组下标（0 基、稠密），旧实现从 1 起必被拒
        self.assertNotIn("seq", header)
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        # ② 首个 surface 事件必须是受保护的系统头
        surface = [e for e in events if e.get("surfaceOp")]
        self.assertTrue(surface)
        self.assertEqual(surface[0]["type"], "system/message")
        self.assertEqual(surface[0]["data"]["message"]["role"], "system")
        self.assertEqual(surface[0]["data"]["message"]["source"],
                         {"kind": "plugin", "plugin": "@deepseek-ai/dsh-system-prompt"})
        # ③ 预设之后必须 turn / step 成对开合
        self.assertEqual(events[0]["type"], "permission/preset")
        kinds = [e["type"] for e in events]
        self.assertIn("turn/start", kinds)
        self.assertIn("turn/end", kinds)
        self.assertEqual(kinds.count("step/start"), kinds.count("step/end"))
        # ④ 助手消息必须带 stream 数组 + model 来源（缺一即被关系校验拒绝）
        am = [e for e in events if e["type"] == "assistant/message"]
        self.assertTrue(am)
        for e in am:
            self.assertIsInstance(e["data"]["stream"], list)
            src = e["data"]["message"]["source"]
            self.assertEqual(src["kind"], "model")
            self.assertTrue(src.get("provider"))
            self.assertTrue(src.get("model"))
        # ⑤ 标题必须引用一条更早的 user/message（空 messageSeqs 只在无用户消息时合法）
        title = [e for e in events if e["type"] == "session/title"][-1]
        self.assertTrue(title["data"]["messageSeqs"])
        user_seqs = {e["seq"] for e in events if e["type"] == "user/message"}
        for s in title["data"]["messageSeqs"]:
            self.assertIn(s, user_seqs)
            self.assertLess(s, title["seq"])
        # ⑥ 以 end-seed 收尾
        self.assertEqual(events[-1]["type"], "session/end-seed")

    def test_empty_assistant_message_is_skipped(self) -> None:
        """空助手消息不写出（否则界面出现无内容气泡）。"""
        ses = sm.Session(source_tool="workbuddy", title="t", messages=[
            sm.SessionMessage(role="user", content="问"),
            sm.SessionMessage(role="assistant", content="   "),
            sm.SessionMessage(role="assistant", content="答"),
        ])
        rows = self._written_rows(ses)
        am = [e for e in rows if e.get("type") == "assistant/message"]
        self.assertEqual(len(am), 1)
        self.assertEqual(am[0]["data"]["message"]["content"][0]["text"], "答")

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

    def test_projcache_titles_reads_per_record_tree(self) -> None:
        """投影缓存换成 per-record 目录树后，标题仍须能读到。

        DSH 的 ``storage-json`` 后端把 ``single`` 迁移成 ``per-record`` 时**保持源文件
        不变**，所以升级过的机器上「旧单文件 + 活目录树」并存，而旧文件 mtime 冻结在升级
        那一刻。只读旧单文件会读到过期/空数据 ⇒ 两种布局都要读，目录树优先。
        """
        root = os.path.join(self.tmp, "dsh2")
        tree = os.path.join(root, "storages", "session_projcache", "sessions")
        os.makedirs(tree)
        with open(os.path.join(tree, "session-new.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 7,
                       "record": {"identity": {"cwd": "D:\\p"},
                                  "rows": {"title": {"ver": 1, "seq": 9,
                                                     "val": "目录树里的标题"}}}}, f)
        # 旧单文件里同一会话是**过期**标题，另一会话只有单文件里有
        with open(os.path.join(root, "storages", "session_projcache.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"unit": {"name": "session_projcache", "version": 3},
                       "tables": {"sessions": {
                           "session-new": {"rows": {"title": {"val": "过期标题"}}},
                           "session-old": {"rows": {"title": {"val": "仅单文件有"}}},
                       }}}, f)

        titles = sm._dsh_projcache_titles(root)
        self.assertEqual(titles["session-new"], "目录树里的标题",
                         "目录树的值应优先于旧单文件")
        self.assertEqual(titles["session-old"], "仅单文件有",
                         "旧单文件里独有的会话仍须读到（更老的机器只有它）")

    def test_projcache_titles_tolerates_missing_everything(self) -> None:
        """两种布局都不存在时返回空 dict，不抛异常。"""
        root = os.path.join(self.tmp, "dsh3")
        os.makedirs(os.path.join(root, "storages"))
        self.assertEqual(sm._dsh_projcache_titles(root), {})


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
