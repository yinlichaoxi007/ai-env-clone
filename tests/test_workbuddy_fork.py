"""WorkBuddy 编辑重发分叉选边 + ``<user_query>`` 提纯的单元测试。

判据全部来自真实备份包（dist/backup/workbuddy 2026-10-05，4 文件 5 处
``resend-fork-notice``）与 W 文档 §5.2：

- 同一对话线的 jsonl 是树：被编辑消息及其回复（被放弃的旧分支）与当前分支并存，
  记录靠 ``id`` / ``parentId`` 链接；真实样本的 notice 只有 ``editedUserItemId``
  （rewind 式），选边**不依赖**它的具体形态——以最后一条消息为叶子回溯祖先链；
- 选边口径＝**保留当前分支**（链外即弃用），多叉天然成立；
- 链不完整（叶子无 parentId / 断链）退回线性读——宁可多带旧分支也不丢前文；
- 没有 fork 标记的文件（含本工具写出的导入会话）零行为变化；
- 用户消息只取 ``<user_query>…</user_query>`` 标签内的真实提问，标签外是系统注入。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone.session_migration import (  # noqa: E402
    SessionParser,
    SessionWriter,
    Session,
    SessionMessage,
    workbuddy_project_slug,
    _workbuddy_active_branch_ids,
    count_workbuddy_forks,
)


def _rec(rid, parent, role=None, text=None, **extra):
    r = {"id": rid, "parentId": parent, "timestamp": 1728000000000,
         "type": "message" if role else extra.pop("type", "message")}
    if role:
        r["role"] = role
        r["content"] = [{"type": "input_text" if role == "user" else "output_text",
                         "text": text}]
    r.update(extra)
    return r


def _write(records):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")
    return path


def _users(session):
    return [m.content for m in session.messages if m.role == "user"]


class TestForkSelection(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.remove(p) for p in self.paths])

    def _path(self, records):
        p = _write(records)
        self.paths.append(p)
        return p

    def test_fork_keeps_active_branch_only(self):
        """W 文档 §5.2 实测形态：分叉父节点 → 被放弃旧分支 → notice → 新分支。"""
        records = [
            _rec("u1", None, "user", "第一问"),
            _rec("a1", "u1", "assistant", "第一答"),
            _rec("u2-edit", "a1", "user", "被编辑后放弃的那问"),
            _rec("a2-old", "u2-edit", "assistant", "Interrupted by user"),
            {"id": "notice-1", "timestamp": 1728000000001,
             "type": "resend-fork-notice", "editedUserItemId": "root-rewind-1"},
            _rec("u3", "a1", "user", "重发后的新问"),
            _rec("a3", "u3", "assistant", "新答"),
        ]
        s = SessionParser.parse_workbuddy(self._path(records))
        self.assertEqual(_users(s), ["第一问", "重发后的新问"])
        self.assertEqual([m.content for m in s.messages if m.role == "assistant"],
                         ["第一答", "新答"])          # 旧分支的 Interrupted 不带

    def test_multi_fork_uses_final_leaf(self):
        """两处分叉：以最后一条消息的祖先链为准，两段旧分支都弃用。"""
        records = [
            _rec("u1", None, "user", "问1"),
            _rec("a1", "u1", "assistant", "答1"),
            _rec("u2-edit", "a1", "user", "旧分支2"),
            {"id": "n1", "type": "resend-fork-notice", "timestamp": 1},
            _rec("u2", "a1", "user", "问2"),
            _rec("a2", "u2", "assistant", "答2"),
            _rec("u3-edit", "a2", "user", "旧分支3"),
            {"id": "n2", "type": "resend-fork-notice", "timestamp": 2},
            _rec("u3", "a2", "user", "问3"),
            _rec("a3", "u3", "assistant", "答3"),
        ]
        s = SessionParser.parse_workbuddy(self._path(records))
        self.assertEqual(_users(s), ["问1", "问2", "问3"])

    def test_tool_records_of_abandoned_branch_dropped(self):
        """旧分支的推理/工具记录也不许混进正文（同一聚合窗内会污染 assistant 内容）。"""
        records = [
            _rec("u1", None, "user", "问1"),
            _rec("a1", "u1", "assistant", "答1"),
            _rec("u2-edit", "a1", "user", "旧分支"),
            {"id": "r-old", "parentId": "u2-edit", "timestamp": 1, "type": "reasoning",
             "rawContent": [{"type": "reasoning_text", "text": "旧分支推理"}]},
            {"id": "n1", "type": "resend-fork-notice", "timestamp": 2},
            _rec("u2", "a1", "user", "新问"),
            _rec("a2", "u2", "assistant", "新答"),
        ]
        s = SessionParser.parse_workbuddy(self._path(records))
        assistants = [m for m in s.messages if m.role == "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertNotIn("旧分支推理", "".join(m.content for m in assistants))

    def test_broken_chain_falls_back_to_linear(self):
        """叶子没有 parentId（断链）⇒ 退回线性读，宁可多带旧分支也不丢前文。"""
        records = [
            _rec("u1", None, "user", "前文"),
            {"id": "leaf", "timestamp": 2, "type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "叶子（链断）"}]},
            {"id": "n1", "type": "resend-fork-notice", "timestamp": 3},
        ]
        self.assertIsNone(_workbuddy_active_branch_ids(records))
        s = SessionParser.parse_workbuddy(self._path(records))
        self.assertIn("前文", _users(s)[0])

    def test_no_notice_means_zero_change(self):
        """没有 fork 标记的文件（含本工具写出的导入会话）线性读，零行为变化。"""
        records = [
            _rec("u1", None, "user", "问1"),
            _rec("a1", "u1", "assistant", "答1"),
        ]
        s = SessionParser.parse_workbuddy(self._path(records))
        self.assertEqual(len(s.messages), 2)


class TestUserQueryExtraction(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.remove(p) for p in self.paths])

    def _path(self, records):
        p = _write(records)
        self.paths.append(p)
        return p

    def _parse_user(self, text):
        records = [_rec("u1", None, "user", text),
                   _rec("a1", "u1", "assistant", "答")]
        p = _write(records)
        self.paths.append(p)
        s = SessionParser.parse_workbuddy(p)
        return _users(s)[0]

    def test_tag_content_extracted_injection_dropped(self):
        text = ('<system-reminder data-role="user-context">\n<current_time>'
                'Sunday, August 9, 2026</current_time>\n</system-reminder>\n'
                '<user_query>帮我翻译这个 PDF</user_query>\n')
        self.assertEqual(self._parse_user(text), "帮我翻译这个 PDF")

    def test_no_tag_keeps_whole_text(self):
        """本工具写出的导入会话没有标签：正文本来就是干净的用户话，原样保留。"""
        self.assertEqual(self._parse_user("普通导入的一问"), "普通导入的一问")

    def test_empty_tag_falls_back(self):
        """标签内容为空 ⇒ 回退原文（提纯绝不把消息变空串）。"""
        text = "<user_query></user_query>只有注入没有提问"
        self.assertEqual(self._parse_user(text), text)

    def test_count_forks(self):
        records = [_rec("u1", None, "user", "问"),
                   {"id": "n1", "type": "resend-fork-notice", "timestamp": 1},
                   {"id": "n2", "type": "resend-fork-notice", "timestamp": 2}]
        p = self._path(records)
        self.assertEqual(count_workbuddy_forks(p), 2)
        self.assertEqual(count_workbuddy_forks(os.path.join(p, "nope")), 0)


class TestRoundtripUnaffected(unittest.TestCase):
    def test_writer_output_parses_identically(self):
        """写出器产物（无 parentId / 无 fork 标记）解析结果必须与输入一致——
        分叉选边对自家格式零影响。"""
        src = Session(source_tool="workbuddy", title="往返",
                      messages=[SessionMessage(role="user", content="问"),
                                SessionMessage(role="assistant", content="答",
                                               reasoning_content="思考",
                                               tool_calls=[])])
        tmp = tempfile.mkdtemp(prefix="wb_rt_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        sid = SessionWriter.write_workbuddy(src, tmp, workspace_slug="demo",
                                            cwd=r"D:\demo")
        path = os.path.join(tmp, "projects",
                            workbuddy_project_slug("demo"),   # 写出器优先用 workspace_slug
                            sid + ".jsonl")
        out = SessionParser.parse_workbuddy(path)
        self.assertEqual([(m.role, m.content) for m in out.messages],
                         [("user", "问"), ("assistant", "答")])


if __name__ == "__main__":
    unittest.main()
