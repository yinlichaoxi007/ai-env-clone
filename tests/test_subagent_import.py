"""子代理会话（subagent）识别与导入测试。

背景（2026-10-05 实测反馈）：从 ZCode 备份包导入 DSH 时，34 条会话里有 23 条其实是
ZCode 的**子代理会话**（``session.task_type = 'subagent_child'`` / ``parent_id`` 非空）。
它们的「用户消息」是父代理写下的任务提示词，正文只有 AI 干活过程，导入后既不是用户自己的
会话、又和父会话平级，界面里看起来就是一堆「只有 AI 过程」的会话。

本文件锁死三条不变式：

1. **默认不列、不导入**子代理会话（开关打开才列）；
2. 导入 DSH 时按**原生子代理结构**写出（裸 uuid 目录、``parentSession`` +
   ``origin: 'subagent'`` + ``delegationDepth: 1``、seq 0 是 ``subagent/descriptor``
   且 ``version`` 必须为 3），**不登记 workspace.json**；
3. 父会话日志里的 ``subagent/catalog`` 与子会话 header 严格对应
   （``childCreatedAt`` == 子会话 ``createdAt``、``childId`` == 子会话 id）。

DSH 相关用例在缺少 zstd 后端时自动跳过。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_env_clone import session_migration as sm  # noqa: E402


def _zstd_pair():
    try:
        import zstandard  # type: ignore
    except ImportError:
        return None
    import io
    return (
        (lambda b: zstandard.ZstdCompressor().compress(b)),
        (lambda b: zstandard.ZstdDecompressor().stream_reader(io.BytesIO(b)).read()),
    )


ZSTD = _zstd_pair()


def _make_zcode_root(tmp: str) -> str:
    """造一个含「1 个父会话 + 2 个子代理会话」的 ZCode 数据根。"""
    db_dir = os.path.join(tmp, "cli", "db")
    os.makedirs(db_dir)
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
    rows = [
        ("s_parent", "父会话标题", r"D:\proj", "", "interactive"),
        ("sess_subagent_agent_aaa", "子代理任务提示词 A", r"D:\proj", "s_parent",
         "subagent_child"),
        ("sess_subagent_agent_bbb", "子代理任务提示词 B", r"D:\proj", "s_parent",
         "subagent_child"),
    ]
    for i, (sid, title, directory, parent, task) in enumerate(rows):
        con.execute("insert into session values(?,?,?,?,?,?,?)",
                    (sid, title, directory, parent, task, 1000 + i, 2000 + i))
        con.execute("insert into message values(?,?,?,?,?,?)",
                    ("m_%s" % sid, sid, 1000 + i, 1000 + i,
                     json.dumps({"role": "user", "time": {"created": 1000 + i}},
                                ensure_ascii=False), 0))
        con.execute("insert into part values(?,?,?,?,?,?,?)",
                    ("p_%s" % sid, "m_%s" % sid, sid, 1000 + i, 1000 + i,
                     json.dumps({"type": "text", "text": "提问 %s" % title},
                                ensure_ascii=False), 0))
        con.execute("insert into message values(?,?,?,?,?,?)",
                    ("a_%s" % sid, sid, 1100 + i, 1100 + i,
                     json.dumps({"role": "assistant", "time": {"created": 1100 + i}},
                                ensure_ascii=False), 1))
        con.execute("insert into part values(?,?,?,?,?,?,?)",
                    ("pa_%s" % sid, "a_%s" % sid, sid, 1100 + i, 1100 + i,
                     json.dumps({"type": "text", "text": "回答 %s" % title},
                                ensure_ascii=False), 0))
    con.commit()
    con.close()
    return tmp


class TestZCodeSubagentScan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sub_scan_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _make_zcode_root(self.tmp)

    def test_subagents_hidden_by_default(self) -> None:
        items = sm.list_source_sessions("zcode", self.tmp)
        self.assertEqual([it["id"] for it in items], ["s_parent"])
        self.assertFalse(items[0]["subagent"])

    def test_subagents_listed_and_flagged_on_demand(self) -> None:
        items = sm.list_source_sessions("zcode", self.tmp, include_subagents=True)
        self.assertEqual([it["id"] for it in items],
                         ["s_parent", "sess_subagent_agent_aaa",
                          "sess_subagent_agent_bbb"])
        sub = items[1]
        self.assertTrue(sub["subagent"])
        self.assertEqual(sub["parent_id"], "s_parent")
        self.assertEqual(sub["parent_title"], "父会话标题")
        self.assertIn("子代理会话", sub["detail"])
        # 父会话本身不带 parent 线索
        self.assertEqual(items[0]["parent_id"], "")
        self.assertEqual(items[0]["parent_title"], "")

    def test_non_zcode_tools_always_carry_subagent_key(self) -> None:
        """其它来源工具的条目也带 ``subagent`` 键（界面统一用 ``.get`` 判断）。"""
        d = os.path.join(self.tmp, "projects", "ws1")
        os.makedirs(d)
        with open(os.path.join(d, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "标题A"}) + "\n")
        items = sm.list_source_sessions("workbuddy", self.tmp)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["subagent"])


class TestZCodeSubagentParse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sub_parse_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = _make_zcode_root(self.tmp)
        self.db = os.path.join(self.root, "cli", "db", "db.sqlite")

    def test_parent_session_is_not_subagent(self) -> None:
        s = sm.SessionParser.parse_zcode(self.db, "s_parent")
        self.assertFalse(s.is_subagent)
        self.assertEqual(s.parent_source_id, "")

    def test_subagent_session_carries_parent_clues(self) -> None:
        s = sm.SessionParser.parse_zcode(self.db, "sess_subagent_agent_aaa")
        self.assertTrue(s.is_subagent)
        self.assertEqual(s.parent_source_id, "s_parent")
        self.assertEqual(s.parent_title, "父会话标题")


class TestPlanSubagentImport(unittest.TestCase):
    """导入编排：补父会话、建挂接、排顺序（纯计算，不写盘）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sub_plan_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.all_items = sm.list_source_sessions(
            "zcode", _make_zcode_root(self.tmp), include_subagents=True)

    def test_parent_is_added_and_ordered_first(self) -> None:
        kids = [it for it in self.all_items if it["subagent"]]
        plan = sm.plan_dsh_import(kids, available=self.all_items)
        self.assertEqual([it["id"] for it in plan["extra_parents"]], ["s_parent"])
        self.assertEqual(plan["ordered"][0]["id"], "s_parent")
        self.assertEqual(plan["missing_parents"], [])
        parent_job = plan["jobs"][0]
        self.assertEqual(len(parent_job["subagent_catalog"]), 2)
        for job in plan["jobs"][1:]:
            self.assertEqual(job["parent_session_id"], parent_job["session_id"])
            self.assertFalse(job["session_id"].startswith("session-"))

    def test_catalog_ids_and_times_match_children(self) -> None:
        kids = [it for it in self.all_items if it["subagent"]]
        plan = sm.plan_dsh_import(kids, available=self.all_items)
        by_id = {job["session_id"]: job for job in plan["jobs"]}
        for entry in plan["jobs"][0]["subagent_catalog"]:
            child = by_id[entry["childId"]]
            self.assertEqual(entry["childCreatedAt"], child["created_ms"])
            self.assertEqual(entry["mode"], "one-shot")
            self.assertEqual(entry["label"], (child["item"]["title"] or "")[:120])

    def test_no_parent_evidence_is_reported_not_downgraded(self) -> None:
        """来源没记住父会话 id（如 DSH 自身的子代理条目）-> 跳过并上报，不降级成顶层会话。"""
        orphan = {"id": r"D:\x\<uuid>", "title": "子代理", "path": r"D:\x\<uuid>",
                  "detail": "子代理会话", "cwd": r"D:\proj", "workspace_id": "",
                  "subagent": True, "parent_id": "", "parent_title": ""}
        plan = sm.plan_dsh_import([orphan])
        self.assertEqual(plan["jobs"], [])
        self.assertEqual(plan["missing_parents"], [orphan])

    def test_plain_sessions_are_untouched(self) -> None:
        """普通会话：只分配 id / 时间，不带父子字段。"""
        plan = sm.plan_dsh_import([self.all_items[0]])
        job = plan["jobs"][0]
        self.assertEqual(job["parent_session_id"], "")
        self.assertEqual(job["subagent_catalog"], [])
        self.assertTrue(job["session_id"].startswith("session-"))


@unittest.skipUnless(ZSTD is not None, "未安装 zstandard，跳过 DSH 用例")
class TestDshSubagentWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sub_dsh_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.dsh = os.path.join(self.tmp, "dsh_home")
        os.makedirs(os.path.join(self.dsh, "storages"))
        # 真实 DSH 主目录里必有 workspace.json；缺它时登记会被跳过（有 warn），
        # 而本文件要验的正是「父会话登记、子代理不登记」。
        with open(os.path.join(self.dsh, "storages", "workspace.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"unit": {"name": "workspace", "version": 2},
                       "global": {"initialized": True, "workspaceIds": [],
                                  "archivedSessionIds": [], "pinnedSessionIds": []},
                       "tables": {"workspaces": {}}}, f)
        self.src_root = _make_zcode_root(os.path.join(self.tmp, "zcode"))
        self.src_db = os.path.join(self.src_root, "cli", "db", "db.sqlite")

    def _read(self, path: str) -> list:
        with open(path, "rb") as fh:
            raw = ZSTD[1](fh.read())
        return [json.loads(l) for l in raw.decode("utf-8").split("\n") if l.strip()]

    def _session(self, title: str, subagent: bool = False,
                 parent_source_id: str = "s_parent"):
        return sm.Session(
            source_tool="zcode", title=title, scope=r"D:\proj",
            messages=[sm.SessionMessage(role="user", content="提示词"),
                      sm.SessionMessage(role="assistant", content="回答")],
            is_subagent=subagent, parent_source_id=parent_source_id,
            parent_title="父会话标题")

    def _paths(self, sid: str):
        ws_dir = sm._dsh_workspace_dirname(r"D:\proj")
        return (os.path.join(self.dsh, "sessions", ws_dir, sid,
                             "session.v3.jsonl.zstd"),
                os.path.join(self.dsh, "storages", "workspace.json"),
                os.path.join(self.dsh, "storages", "session_projcache",
                             "sessions", sid + ".json"))

    def test_parent_log_carries_catalog_and_child_header_matches(self) -> None:
        child_sid = sm._new_dsh_subagent_id()
        child_created = 1790000000123
        parent_sid = sm.SessionWriter.write_dsh(
            self._session("父会话"), self.dsh, cwd=r"D:\proj",
            session_id="session-parent-1", created_ms=1790000000000,
            subagent_catalog=[{"childId": child_sid,
                               "childCreatedAt": child_created,
                               "mode": "one-shot", "label": "子代理任务"}],
        )
        self.assertEqual(parent_sid, "session-parent-1")
        parent_log = self._read(self._paths(parent_sid)[0])
        self.assertEqual(parent_log[0]["delegationDepth"], 0)
        self.assertNotIn("origin", parent_log[0])
        catalog = [e for e in parent_log if e["type"] == "subagent/catalog"]
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["data"],
                         {"version": 0, "childId": child_sid,
                          "childCreatedAt": child_created, "mode": "one-shot",
                          "label": "子代理任务"})
        self.assertEqual([e["seq"] for e in parent_log[1:]],
                         list(range(len(parent_log) - 1)))

        child_sid2 = sm.SessionWriter.write_dsh(
            self._session("子代理任务", subagent=True), self.dsh, cwd=r"D:\proj",
            session_id=child_sid, created_ms=child_created,
            parent_session_id=parent_sid,
        )
        self.assertEqual(child_sid2, child_sid)
        child_path, ws_file, cache_file = self._paths(child_sid)
        child_log = self._read(child_path)
        header = child_log[0]
        self.assertEqual(header["parentSession"], parent_sid)
        self.assertEqual(header["origin"], "subagent")
        self.assertEqual(header["delegationDepth"], 1)
        self.assertEqual(header["createdAt"], child_created)
        self.assertFalse(os.path.basename(os.path.dirname(child_path))
                         .startswith("session-"))
        descriptor = child_log[1]
        self.assertEqual(descriptor["type"], "subagent/descriptor")
        self.assertEqual(descriptor["seq"], 0)
        self.assertEqual(descriptor["data"]["version"], 3)
        self.assertEqual(descriptor["data"]["mode"], "one-shot")
        self.assertTrue(descriptor["data"]["provider"])
        self.assertEqual(descriptor["data"]["label"], "子代理任务")
        self.assertEqual([e["seq"] for e in child_log[1:]],
                         list(range(len(child_log) - 1)))
        # 子代理**不**登记工作区、**不**预写投影缓存
        self.assertFalse(os.path.isfile(cache_file))
        with open(ws_file, encoding="utf-8") as f:
            data = json.load(f)
        ids = [sid for w in data["tables"]["workspaces"].values()
               for sid in w["sessionIds"]]
        self.assertIn(parent_sid, ids)
        self.assertNotIn(child_sid, ids)

    def test_parse_dsh_reads_subagent_header(self) -> None:
        """从 DSH 自身导入时，子代理身份由 header（origin / parentSession）判定。"""
        child_sid = sm._new_dsh_subagent_id()
        sm.SessionWriter.write_dsh(
            self._session("子代理任务", subagent=True), self.dsh, cwd=r"D:\proj",
            session_id=child_sid, parent_session_id="session-parent-9",
            created_ms=1790000000000)
        s = sm.SessionParser.parse_dsh(self._paths(child_sid)[0])
        self.assertTrue(s.is_subagent)
        self.assertEqual(s.parent_source_id, "session-parent-9")
        parent_sid = sm.SessionWriter.write_dsh(
            self._session("父会话"), self.dsh, cwd=r"D:\proj",
            session_id="session-parent-9")
        top = sm.SessionParser.parse_dsh(self._paths(parent_sid)[0])
        self.assertFalse(top.is_subagent)
        self.assertEqual(top.parent_source_id, "")

    def test_migrate_session_warns_when_source_is_not_subagent(self) -> None:
        """源会话不是子代理，却要求挂父会话 -> 只提示，并按顶层会话写出。"""
        warns: list = []
        sid = sm.migrate_session(
            "zcode", self.src_db, "dsh", self.dsh, source_session_id="s_parent",
            workspace_id=r"D:\proj", parent_session_id="session-whatever",
            session_id="session-warn-1",
            warn=warns.append,
        )
        self.assertEqual(sid, "session-warn-1")
        log = self._read(self._paths(sid)[0])
        self.assertEqual(log[0]["delegationDepth"], 0)
        self.assertNotIn("parentSession", log[0])
        self.assertTrue(any("不是子代理会话" in w for w in warns), warns)


if __name__ == "__main__":
    unittest.main()
