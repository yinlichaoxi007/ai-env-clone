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

from ai_env_clone.session_migration import (
    SessionParser,
    SessionWriter,
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


if __name__ == "__main__":
    unittest.main()
