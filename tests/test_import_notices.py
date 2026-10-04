"""导入「变化预告」（``import_matrix.notices_for`` 等）的**契约测试**。

防「改了 writer / 能力矩阵，忘了同步界面文案」——这类漂移用户最先感知，
却最难在人工测试里发现：

- 每个 ``SUPPORTED`` 来源必须有**非空** ``changes``（否则界面「必然变化」区空着）；
- 「会丢工具调用」的写出口径必须在 :data:`import_matrix.TARGET_LOSSES` 里被声明。
  这里不看注释、不读源码字符串，而是**真跑一遍往返**（``write_*`` → ``parse_*``）：
  往返后工具调用没了 ⇒ 必须声明丢 ``tool_calls``；还在 ⇒ 不许声明丢。
- ``notices_for`` 的三段（must / warn / keep）内容与矩阵数据一致，不自相矛盾。
"""

import os
import shutil
import tempfile
import unittest

from ai_env_clone import import_matrix as im
from ai_env_clone import session_migration as sm

#: 有写出口径（可作导入目标）的工具
_WRITABLE_TARGETS = ("reasonix", "codebuddy", "workbuddy", "dsh")


def _sample_session():
    return sm.Session(
        source_tool="unit",
        title="契约测试会话",
        messages=[
            sm.SessionMessage(role="user", content="你好",
                              created_at="2026-01-01T00:00:00"),
            sm.SessionMessage(
                role="assistant", content="回答",
                tool_calls=[{"id": "c1", "name": "read_file",
                             "arguments": '{"path":"a.txt"}'}],
                created_at="2026-01-01T00:00:01"),
        ],
    )


def _roundtrip(target: str, tmp: str):
    """写出一条会话再解析回来，用于观察 writer 的真实行为。"""
    ses = _sample_session()
    W = sm.SessionWriter
    if target == "reasonix":
        sid = W.write_reasonix(ses, tmp, scope="proj")
        return sm.SessionParser.parse_reasonix(
            os.path.join(tmp, "proj", "sessions", "%s-session.jsonl" % sid))
    if target == "codebuddy":
        sid = W.write_codebuddy(ses, tmp, "wid")
        return sm.SessionParser.parse_codebuddy(os.path.join(tmp, "wid", sid))
    if target == "workbuddy":
        sid = W.write_workbuddy(ses, tmp, workspace_slug="proj", cwd="C:/proj")
        slug = sm.workbuddy_project_slug("proj")
        return sm.SessionParser.parse_workbuddy(
            os.path.join(tmp, "projects", slug, sid + ".jsonl"))
    if target == "dsh":
        sid = W.write_dsh(ses, tmp, cwd="C:/proj")
        return sm.SessionParser.parse_dsh(
            os.path.join(tmp, "sessions", sm._dsh_workspace_dirname("C:/proj"), sid))
    raise AssertionError("未登记的写出口径：%s" % target)


class TestChangesContract(unittest.TestCase):
    def test_supported_sources_have_nonempty_changes(self):
        for tool, cap in im.ALL_SOURCES.items():
            if cap.status != im.SUPPORTED:
                continue
            with self.subTest(source=tool):
                self.assertTrue(cap.changes,
                                "%s 是导入来源，必须有「必然变化」文案" % tool)

    def test_encrypted_sources_declare_no_changes(self):
        """仅备份/还原的来源不走导入通路，不应有「必然变化」文案误导用户。"""
        for tool, cap in im.ALL_SOURCES.items():
            if cap.status != im.BACKUP_ONLY:
                continue
            with self.subTest(source=tool):
                self.assertEqual(cap.changes, ())


class TestLossDeclaration(unittest.TestCase):
    def test_losses_shape_and_targets(self):
        valid = im.session_import_targets()
        for target, losses in im.TARGET_LOSSES.items():
            with self.subTest(target=target):
                self.assertIn(target, valid, "%s 不是可导入目标" % target)
                for key, text in losses:
                    self.assertIn(key, ("tool_calls", "tool_results"))
                    self.assertTrue(isinstance(text, str) and text.strip())

    def test_grouping_and_landing_targets_are_valid(self):
        valid = set(im.session_import_targets())
        for mapping in (im.TARGET_GROUPING, im.TARGET_DEFAULT_LANDING):
            for target, value in mapping.items():
                with self.subTest(target=target):
                    self.assertIn(target, valid)
                    self.assertTrue(value)

    def test_writer_tool_call_behaviour_matches_declaration(self):
        """真跑往返：writer 是否丢工具调用，必须与 TARGET_LOSSES 声明一致。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for target in _WRITABLE_TARGETS:
            with self.subTest(target=target):
                try:
                    parsed = _roundtrip(target, tmp)
                except RuntimeError as exc:
                    # DSH 写 / 读都依赖 zstd 后端；缺失时跳过而非误判
                    if target == "dsh" and "zstd" in str(exc).lower():
                        self.skipTest("无 zstd 后端，跳过 DSH 往返")
                    raise
                has_calls = any(m.tool_calls for m in parsed.messages)
                declared = {k for k, _ in im.TARGET_LOSSES.get(target, ())}
                if has_calls:
                    self.assertNotIn(
                        "tool_calls", declared,
                        "%s 的 writer 保留了工具调用，却声明会丢（文案与实现不符）" % target)
                else:
                    self.assertIn(
                        "tool_calls", declared,
                        "%s 的 writer 丢了工具调用，但 TARGET_LOSSES 未声明" % target)


class TestNoticesFor(unittest.TestCase):
    def test_self_and_empty_return_nothing(self):
        for args in (("", "codebuddy"), ("codebuddy", ""),
                     ("codebuddy", "codebuddy")):
            self.assertEqual(im.notices_for(*args),
                             {"must": [], "warn": [], "keep": []})

    def test_must_equals_source_changes(self):
        src = im.ALL_SOURCES["codebuddy"]
        data = im.notices_for("codebuddy", "workbuddy")
        self.assertEqual(data["must"], list(src.changes))

    def test_every_importable_pair_has_must(self):
        """目标工具的每个可导入来源都应产出非空「必然变化」（否则预告区空着）。"""
        for target in im.session_import_targets():
            for cap in im.importable_sources_for(target):
                with self.subTest(source=cap.tool, target=target):
                    data = im.notices_for(cap.tool, target)
                    self.assertTrue(data["must"])
                    self.assertTrue(data["warn"])

    def test_warn_includes_target_losses(self):
        for target, losses in im.TARGET_LOSSES.items():
            if not losses:
                continue
            source = next(s.tool for s in im.importable_sources_for(target))
            with self.subTest(target=target):
                warn = im.notices_for(source, target)["warn"]
                for _key, text in losses:
                    self.assertIn(text, warn)

    def test_keep_reflects_tool_call_loss(self):
        """「不影响的」清单必须与目标工具实际丢什么一致，不能反着说。"""
        cb_keep = im.notices_for("reasonix", "codebuddy")["keep"]
        self.assertNotIn("工具调用的名称与参数保留", cb_keep)
        self.assertIn("工具执行结果保留", cb_keep)

        wb_keep = im.notices_for("reasonix", "workbuddy")["keep"]
        self.assertIn("工具调用的名称与参数保留", wb_keep)
        self.assertNotIn("工具执行结果保留", wb_keep)

        rx_keep = im.notices_for("codebuddy", "reasonix")["keep"]
        self.assertIn("工具调用的名称与参数保留", rx_keep)
        self.assertIn("工具执行结果保留", rx_keep)

    def test_importable_sources_for_excludes_encrypted_and_self(self):
        sources = im.importable_sources_for("workbuddy")
        tools = {s.tool for s in sources}
        self.assertNotIn("workbuddy", tools)     # 不导入自己
        self.assertNotIn("qoder", tools)         # 加密，不可导入
        self.assertNotIn("trae-cn", tools)
        self.assertTrue(all(s.status == im.SUPPORTED for s in sources))
        self.assertEqual(im.importable_sources_for("no-such-tool"), ())


if __name__ == "__main__":
    unittest.main()
