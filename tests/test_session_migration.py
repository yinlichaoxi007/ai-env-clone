"""跨工具会话迁移单元测试。

验证：
1. Reasonix 会话可被解析（含 reasoning_content / tool_calls）。
2. CodeBuddy 会话可被解析（含内层 message 双重编码展开）。
3. Reasonix -> CodeBuddy 原生复刻写入，目标工具能像原生一样读回。
4. CodeBuddy -> Reasonix 原生复刻写入，目标工具能像原生一样读回。
5. 迁移不覆盖目标已有会话（生成全新 id）。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ai_env_clone.session_migration import (
    Session,
    SessionMessage,
    SessionParser,
    SessionWriter,
    _dsh_sync_projcache,
    _dsh_zstd_backend,
    _register_dsh_session,
    migrate_session,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
RX_SESSION = os.path.join(
    FIX, "reasonix_sessions", "d--project-demo", "sessions",
    "20260810-100000.123456789-deepseek-v4-flash-session.jsonl",
)
CB_SESSION_DIR = os.path.join(
    FIX, "codebuddy_sessions", "history",
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "11111111-2222-3333-4444-555555555555",
)


class TestParseReasonix(unittest.TestCase):
    def test_parse_messages_count_and_roles(self):
        s = SessionParser.parse_reasonix(RX_SESSION)
        self.assertEqual(s.source_tool, "reasonix")
        self.assertEqual(len(s.messages), 4)
        self.assertEqual([m.role for m in s.messages],
                         ["user", "assistant", "user", "assistant"])

    def test_parse_reasoning_and_toolcalls(self):
        s = SessionParser.parse_reasonix(RX_SESSION)
        # 第二条 assistant 含 reasoning_content
        self.assertIn("递归实现", s.messages[1].reasoning_content)
        # 第四条 assistant 含 tool_calls
        self.assertTrue(s.messages[3].tool_calls)
        self.assertEqual(s.messages[3].tool_calls[0]["name"], "edit_file")

    def test_parse_meta_title(self):
        # 不传入 meta，由解析器按命名规则自动推导 ``<id>.jsonl.meta``
        s = SessionParser.parse_reasonix(RX_SESSION)
        self.assertEqual(s.title, "快速排序实现")
        self.assertEqual(s.scope, "d--project-demo")


class TestParseCodeBuddy(unittest.TestCase):
    def test_parse_count_and_roles(self):
        s = SessionParser.parse_codebuddy(CB_SESSION_DIR)
        self.assertEqual(s.source_tool, "codebuddy")
        self.assertEqual(len(s.messages), 2)
        self.assertEqual([m.role for m in s.messages], ["user", "assistant"])

    def test_parse_inner_message_unwrapped(self):
        s = SessionParser.parse_codebuddy(CB_SESSION_DIR)
        # 内层 message 双重编码应已展开为纯文本
        self.assertIn("闭包是指", s.messages[1].content)
        # reasoning_content 应被提取
        self.assertIn("定义再给示例", s.messages[1].reasoning_content)

    def test_title_falls_back_to_first_message(self):
        """index.json 无 title 时，解析标题与列表阶段取同一条兜底（首条用户消息）。"""
        tmp = tempfile.mkdtemp(prefix="cb_title_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sdir = os.path.join(tmp, "history", "wid", "sid")
        os.makedirs(os.path.join(sdir, "messages"))
        with open(os.path.join(sdir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"messages": [
                {"id": "m1", "type": "message", "role": "user", "isComplete": True},
            ]}, fh)
        with open(os.path.join(sdir, "messages", "m1.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "role": "user",
                "message": json.dumps({"role": "user",
                                       "content": [{"type": "text", "text": "帮我重构这个模块"}]}),
            }, fh)
        s = SessionParser.parse_codebuddy(sdir)
        self.assertEqual(s.title, "帮我重构这个模块")
        self.assertEqual(len(s.messages), 1)


class TestScanCodeBuddyResolvesWorkspace(unittest.TestCase):
    """扫描 CodeBuddy 会话时，必须把 ``workspaceId`` 反查成项目路径填进 ``cwd``。

    否则迁移到 WorkBuddy / DSH 时会被判成「源会话没有工作区」，落到工具默认落点
    （WorkBuddy 是 ``~/WorkBuddy/<时间戳>``），而**不是**会话原本的工程工作区。
    """

    def _scan(self, index):
        from ai_env_clone import session_migration as sm
        with mock.patch("ai_env_clone.workspace_plan.codebuddy_workspace_path_index",
                        return_value=index):
            return sm.list_source_sessions(
                "codebuddy", os.path.join(FIX, "codebuddy_sessions", "history"))

    def test_cwd_resolved_from_index(self):
        wid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        items = self._scan({wid: r"D:\project\ai-env-clone"})
        self.assertTrue(items)
        for it in items:
            self.assertEqual(it["workspace_id"], wid)
            self.assertEqual(it["cwd"], r"D:\project\ai-env-clone")
            self.assertIn(r"D:\project\ai-env-clone", it["detail"])

    def test_cwd_empty_when_unresolved(self):
        """反查不到 -> 仍然给出条目，``cwd`` 留空（由落点判定退回默认落点并说明成因）。"""
        items = self._scan({})
        self.assertTrue(items)
        self.assertEqual(items[0]["cwd"], "")
        self.assertEqual(items[0]["workspace_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class TestMigrateRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reasonix_to_codebuddy(self):
        hist_root = os.path.join(self.tmp, "history")
        wid = "ffffffff-0000-1111-2222-333333333333"
        # 预置一个已有会话，验证不被覆盖
        os.makedirs(os.path.join(hist_root, wid, "existing-sid"), exist_ok=True)
        with open(os.path.join(hist_root, wid, "existing-sid", "index.json"), "w", encoding="utf-8") as f:
            json.dump({"messages": []}, f)

        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="codebuddy", target_root=hist_root, workspace_id=wid,
        )
        # 新会话 id 不与已有冲突
        self.assertNotEqual(new_id, "existing-sid")
        new_dir = os.path.join(hist_root, wid, new_id)
        self.assertTrue(os.path.isdir(new_dir))
        # 原生格式：index.json + messages/
        with open(os.path.join(new_dir, "index.json"), encoding="utf-8") as f:
            idx = json.load(f)
        self.assertEqual(len(idx["messages"]), 4)
        # messages 目录下应有 4 个散文件 + 聚合 index.json
        msg_files = [f for f in os.listdir(os.path.join(new_dir, "messages"))
                     if f.endswith(".json") and f != "index.json"]
        self.assertEqual(len(msg_files), 4)
        # 读回内容，按 createdAt 排序后确认第一条为用户提问且无损
        loaded = []
        for mf in msg_files:
            with open(os.path.join(new_dir, "messages", mf), encoding="utf-8") as f:
                loaded.append(json.load(f))
        loaded.sort(key=lambda o: o.get("createdAt", ""))
        first = loaded[0]
        inner = json.loads(first["message"])
        self.assertEqual(first["role"], "user")
        self.assertIn("快速排序", inner["content"][0]["text"])
        # 原有会话仍在
        self.assertTrue(os.path.exists(os.path.join(hist_root, wid, "existing-sid", "index.json")))

    def test_codebuddy_to_reasonix(self):
        sessions_parent = os.path.join(self.tmp, "projects")
        scope = "d--project-target"
        # 预置一个已有会话，验证不被覆盖
        existing = os.path.join(sessions_parent, scope, "sessions", "existing-session-session.jsonl")
        os.makedirs(os.path.dirname(existing), exist_ok=True)
        with open(existing, "w", encoding="utf-8") as f:
            f.write('{"role":"user","content":"old"}\n')

        new_id = migrate_session(
            source_tool="codebuddy", source_path=CB_SESSION_DIR,
            target_tool="reasonix", target_root=sessions_parent, scope=scope,
        )
        self.assertNotEqual(new_id, "existing-session")
        out_jsonl = os.path.join(sessions_parent, scope, "sessions", f"{new_id}-session.jsonl")
        out_meta = os.path.join(sessions_parent, scope, "sessions", f"{new_id}.jsonl.meta")
        self.assertTrue(os.path.isfile(out_jsonl))
        self.assertTrue(os.path.isfile(out_meta))
        # 读回验证无损
        with open(out_jsonl, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("闭包是指", lines[1]["content"])
        # reasoning 也应保留
        self.assertIn("定义再给示例", lines[1]["reasoning_content"])
        # 原有会话仍在
        self.assertTrue(os.path.isfile(existing))

    def test_reasonix_to_reasonix_fusion_no_overwrite(self):
        sessions_parent = os.path.join(self.tmp, "projects")
        scope = "d--project-demo"
        existing = os.path.join(sessions_parent, scope, "sessions",
                                "20260810-100000.123456789-deepseek-v4-flash-session.jsonl")
        os.makedirs(os.path.dirname(existing), exist_ok=True)
        shutil.copy(RX_SESSION, existing)
        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="reasonix", target_root=sessions_parent, scope=scope,
        )
        # 不应覆盖原文件（新 id 不同）
        self.assertNotEqual(new_id, "20260810-100000.123456789-deepseek-v4-flash")
        self.assertTrue(os.path.isfile(existing))


class TestMigrateWarnings(unittest.TestCase):
    """迁移时的提示逻辑（warn 回调）验证：仅当位置可能「读不到」时才触发。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self):
        msgs = []
        return msgs, lambda m: msgs.append(m)

    def test_warn_target_root_not_current_user(self):
        # 目标根设为一个明显非当前用户根的临时目录 -> 应触发落点警告
        msgs, warn = self._collect()
        sessions_parent = os.path.join(self.tmp, "projects_not_current")
        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="reasonix", target_root=sessions_parent,
            scope="d--project-demo", warn=warn,
        )
        self.assertTrue(new_id)
        self.assertTrue(any("不是当前登录用户" in m for m in msgs),
                        "落点非当前用户应触发警告，实际：%r" % msgs)

    def test_warn_workspace_id_missing(self):
        # 不传 workspace_id -> 生成随机 wid，应触发「未指定 workspaceId」警告
        msgs, warn = self._collect()
        hist_root = os.path.join(self.tmp, "history")
        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="codebuddy", target_root=hist_root, warn=warn,
        )
        self.assertTrue(new_id)
        self.assertTrue(any("未指定目标 workspaceId" in m for m in msgs),
                        "未指定 workspaceId 应触发警告，实际：%r" % msgs)

    def test_warn_workspace_not_exist(self):
        # 显式传入目标机不存在的 workspaceId -> 应触发「工作区不存在」警告
        msgs, warn = self._collect()
        hist_root = os.path.join(self.tmp, "history")
        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="codebuddy", target_root=hist_root,
            workspace_id="ffffffff-ffff-ffff-ffff-ffffffffffff", warn=warn,
        )
        self.assertTrue(new_id)
        self.assertTrue(any("目标工作区" in m and "不存在" in m for m in msgs),
                        "目标工作区不存在应触发警告，实际：%r" % msgs)

    def test_no_warn_when_workspace_exists(self):
        # 目标工作区已存在 -> 不应触发「工作区不存在」警告（落点警告可能仍取决于根）
        msgs, warn = self._collect()
        hist_root = os.path.join(self.tmp, "history")
        wid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        os.makedirs(os.path.join(hist_root, wid), exist_ok=True)
        new_id = migrate_session(
            source_tool="reasonix", source_path=RX_SESSION,
            target_tool="codebuddy", target_root=hist_root,
            workspace_id=wid, warn=warn,
        )
        self.assertTrue(new_id)
        self.assertFalse(any("目标工作区" in m and "不存在" in m for m in msgs),
                          "目标工作区已存在不应触发不存在警告，实际：%r" % msgs)


class TestRegisterDshSessionRequiredFields(unittest.TestCase):
    """导入会话写 DSH 工作区索引时，记录必须齐备 zod 必填字段。

    回归背景：旧版本创建的工作区记录缺 ``createdAt``/``updatedAt``，DSH 在存储
    边界用 zod 强校验 ``workspaceRecord``（这些字段无默认值），任一条记录缺字段
    会让整个 workspace 域解析失败，桌面端一个工作区、一个会话都看不到。
    """

    REQUIRED = ("path", "title", "sessionIds", "createdAt", "updatedAt")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reg_dsh_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dsh = os.path.join(self.tmp, ".dsh")
        os.makedirs(os.path.join(self.dsh, "storages"), exist_ok=True)
        self.ws_file = os.path.join(self.dsh, "storages", "workspace.json")
        # 既有索引：一条旧记录缺时间戳（历史导入写坏），一条合法
        with open(self.ws_file, "w", encoding="utf-8") as fh:
            json.dump({
                "unit": {"name": "workspace", "version": 2},
                "global": {"initialized": True,
                           "workspaceIds": ["ws-old", "ws-ok"],
                           "archivedSessionIds": []},
                "tables": {"workspaces": {
                    "ws-old": {"path": "D:\\project\\old", "title": "old",
                               "sessionIds": ["session-a"]},
                    "ws-ok": {"path": "D:\\project\\ok", "title": "ok",
                              "sessionIds": [], "createdAt": "t1", "updatedAt": "t1"},
                }},
            }, fh)

    def _read(self):
        with open(self.ws_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_new_record_and_backfill_have_all_required_fields(self):
        warnings = []
        _register_dsh_session(self.dsh, "session-new", "--D-project-new--",
                              "D:\\project\\new", "新会话", 123, warnings.append)
        data = self._read()
        for wid, rec in data["tables"]["workspaces"].items():
            for key in self.REQUIRED:
                self.assertIn(key, rec, "工作区 %s 缺必填字段 %s" % (wid, key))
        # 新工作区已建，且新会话登记其中
        new_ids = [wid for wid, rec in data["tables"]["workspaces"].items()
                   if rec["path"] == "D:\\project\\new"]
        self.assertEqual(len(new_ids), 1)
        self.assertIn("session-new", data["tables"]["workspaces"][new_ids[0]]["sessionIds"])
        # 旧记录的会话保留（只增不改），时间戳被补齐
        self.assertEqual(data["tables"]["workspaces"]["ws-old"]["sessionIds"], ["session-a"])
        self.assertIn("pinnedSessionIds", data["global"])
        self.assertEqual(warnings, [])

    def test_existing_workspace_appends_session(self):
        warnings = []
        _register_dsh_session(self.dsh, "session-b", "--D-project-ok--",
                              "D:\\project\\ok", "新会话", 1, warnings.append)
        data = self._read()
        self.assertEqual(data["tables"]["workspaces"]["ws-ok"]["sessionIds"], ["session-b"])
        self.assertEqual(data["tables"]["workspaces"]["ws-ok"]["updatedAt"] is not None, True)


class TestEmptySessionSkipped(unittest.TestCase):
    """源会话不含任何消息时不得导入。

    回归背景：CodeBuddy 备份里存在 ``index.json`` 为 ``{"messages": [], "requests": []}``
    且无消息文件的空会话；旧行为「仍写出空会话」会在目标工具（如 DSH）留下
    「有标题、无内容」的幽灵会话（用户实测：只看到工作区标题、点开没有会话数据）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="empty_sess_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_codebuddy_session_is_skipped(self):
        sdir = os.path.join(self.tmp, "history", "wsid", "sessid")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"messages": [], "requests": []}, fh)
        dsh = os.path.join(self.tmp, ".dsh")
        os.makedirs(os.path.join(dsh, "storages"))
        with self.assertRaises(ValueError) as cm:
            migrate_session(source_tool="codebuddy", source_path=sdir,
                            target_tool="dsh", target_root=dsh,
                            workspace_id="D:\\project\\x")
        self.assertIn("空会话", str(cm.exception))
        # 未写出任何会话目录（不留幽灵会话）
        self.assertFalse(os.path.isdir(os.path.join(dsh, "sessions")))


def _zstd_ready() -> bool:
    return _dsh_zstd_backend()[1] is not None


class TestDshProjcacheSync(unittest.TestCase):
    """导入会话必须同步 DSH 投影缓存 ``storages/session_projcache/sessions/<sid>.json``。

    回归背景：DSH 侧边栏对**冷会话**（未打开过的）只读该缓存里的
    ``sessionListMetadata`` 投影——``ui-workspace.sessionVisible()`` 会隐藏
    ``blank`` 为真的行，标题也来自同一缓存。旧版导入只写会话日志 +
    ``workspace.json``；更糟的是，早期 DSH 拒绝过导入日志时落下的陈旧检查点
    （``{blank: true, seq: 3}``）会因 ``identity`` 仍绑定 header 的**不变字段**而被
    继续采信 —— 界面表现就是「工作区有会话、却看不到 / 未命名空会话」。
    """

    SID = "session-11111111-2222-3333-4444-555555555555"
    CWD = r"D:\project\demo"
    CREATED_MS = 1700000000000
    PROMPT_MS = 1786116263224

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="projcache_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dsh = os.path.join(self.tmp, ".dsh")
        self.cache_dir = os.path.join(
            self.dsh, "storages", "session_projcache", "sessions")

    @property
    def cache_fp(self):
        return os.path.join(self.cache_dir, self.SID + ".json")

    def _text(self, *, has_turn=True, title="导入会话（来自 codebuddy）"):
        def ev(etype, ms, data):
            return json.dumps({"type": etype, "seq": 0, "time": ms, "data": data},
                              ensure_ascii=False)
        lines = [json.dumps({"type": "session", "version": 4, "id": self.SID,
                             "createdAt": self.CREATED_MS, "cwd": self.CWD},
                            ensure_ascii=False)]
        if has_turn:
            lines.append(ev("turn/start", self.CREATED_MS + 1, {"turn": 1}))
        lines.append(ev("session/title", self.CREATED_MS + 2, {"title": title}))
        lines.append(ev("user/message", self.PROMPT_MS, {"source": {"kind": "user"}}))
        return "\n".join(lines) + "\n"

    def _read(self):
        with open(self.cache_fp, encoding="utf-8") as fh:
            return json.load(fh)

    def test_new_record_is_visible_with_title(self):
        _dsh_sync_projcache(self.dsh, self.SID, self.CREATED_MS, self.CWD,
                            False, self._text(), lambda _m: None)
        doc = self._read()
        self.assertEqual(doc["version"], 7)
        rows = doc["record"]["rows"]
        meta = rows["sessionListMetadata"]["val"]
        self.assertFalse(meta["blank"], "有 turn/start 的会话不得标为空会话（否则侧边栏隐藏）")
        self.assertEqual(meta["lastPromptAt"], self.PROMPT_MS)
        self.assertEqual(rows["title"]["val"], "导入会话（来自 codebuddy）")
        ident = doc["record"]["identity"]
        self.assertEqual(ident["formatVersion"], 4)
        self.assertEqual(ident["createdAt"], self.CREATED_MS)
        self.assertEqual(ident["cwd"], self.CWD)

    def test_stale_record_corrected_in_place_keeping_other_rows(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        other_row = {"ver": 2, "seq": 3, "val": {"turns": 0}}
        stale = {"version": 7, "record": {
            "identity": {"formatVersion": 4, "createdAt": self.CREATED_MS,
                         "cwd": self.CWD, "isSeeded": False,
                         "inheritedEventCount": 0},
            "rows": {
                "title": {"ver": 1, "seq": 3, "val": None},
                "sessionListMetadata": {"ver": 1, "seq": 3,
                                        "val": {"blank": True, "lastPromptAt": None}},
                "sessionStats": other_row,
            },
        }}
        with open(self.cache_fp, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)

        _dsh_sync_projcache(self.dsh, self.SID, self.CREATED_MS, self.CWD,
                            False, self._text(), lambda _m: None)
        rows = self._read()["record"]["rows"]
        self.assertFalse(rows["sessionListMetadata"]["val"]["blank"])
        self.assertEqual(rows["title"]["val"], "导入会话（来自 codebuddy）")
        # 其余行连同各自 ver 原样保留（陈旧但合法，真正打开时会从头重折叠）
        self.assertEqual(rows["sessionStats"], other_row)
        # 改写前留了备份
        baks = [n for n in os.listdir(self.cache_dir)
                if n.startswith(self.SID + ".json.bak-")]
        self.assertTrue(baks, "就地修正缓存记录前必须先备份原文件")

    def test_session_without_turn_stays_blank(self):
        """没有 ``turn/start`` 的会话（真正无对话内容）保持 blank，不伪造可见性。"""
        _dsh_sync_projcache(self.dsh, self.SID, self.CREATED_MS, self.CWD,
                            False, self._text(has_turn=False), lambda _m: None)
        self.assertTrue(self._read()["record"]["rows"]["sessionListMetadata"]["val"]["blank"])


@unittest.skipUnless(_zstd_ready(), "需要真实 zstd 后端")
class TestWriteDshSyncsProjcache(unittest.TestCase):
    """``write_dsh`` 端到端：导入一条会话后，投影缓存必须已同步且可显示。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh_write_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "storages"))
        with open(os.path.join(self.tmp, "storages", "workspace.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"unit": {"name": "workspace", "version": 2},
                       "global": {"initialized": True, "workspaceIds": []},
                       "tables": {"workspaces": {}}}, fh)

    def test_write_dsh_creates_visible_cache_record(self):
        ses = Session(source_tool="unit", title="端到端导入",
                      messages=[
                          SessionMessage(role="user", content="你好",
                                         created_at="2026-01-01T00:00:00"),
                          SessionMessage(role="assistant", content="回答",
                                         created_at="2026-01-01T00:00:01"),
                      ])
        warnings = []
        sid = SessionWriter.write_dsh(ses, self.tmp, cwd=r"D:\project\e2e",
                                     warn=warnings.append)
        self.assertEqual(warnings, [])
        cache_fp = os.path.join(self.tmp, "storages", "session_projcache",
                                "sessions", sid + ".json")
        with open(cache_fp, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc["record"]["rows"]
        self.assertFalse(rows["sessionListMetadata"]["val"]["blank"])
        self.assertEqual(rows["title"]["val"], "端到端导入")
        self.assertEqual(doc["record"]["identity"]["cwd"], r"D:\project\e2e")


if __name__ == "__main__":
    unittest.main()
