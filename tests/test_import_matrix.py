"""导入能力矩阵（``ai_env_clone.import_matrix``）测试。

覆盖：
- 每个已注册适配器都有矩阵条目（新增工具后不会漏登记）；
- 明文工具（reasonix / codebuddy / workbuddy / dsh）至少有一个「支持导入」来源；
- 加密工具（qoder / trae-cn / trae-solo-cn）只能「仅备份/还原」；
- ``describe()`` 返回结构完整（界面直接消费，字段缺失会渲染异常）；
- ``format_lines()`` 能产出非空文本（弹窗消费）；
- 标注为 SUPPORTED 的来源，其 ``parser`` 在 session_migration 中确实存在；
  且其 ``writer``（若非空）同样存在。
"""

import unittest

from ai_env_clone import import_matrix as im
from ai_env_clone import session_migration
from ai_env_clone.adapters import list_adapters


class TestMatrixCoverage(unittest.TestCase):
    def test_every_adapter_has_entry(self) -> None:
        for name in list_adapters():
            with self.subTest(tool=name):
                self.assertIsNotNone(
                    im.matrix_for(name), "适配器 %s 未登记导入能力矩阵" % name
                )

    def test_known_targets_match_adapters(self) -> None:
        self.assertEqual(set(im.known_targets()), set(list_adapters()))

    def test_plain_tools_have_importable_sources(self) -> None:
        for tool in ("reasonix", "codebuddy", "workbuddy", "dsh"):
            with self.subTest(tool=tool):
                cap = im.matrix_for(tool)
                self.assertGreater(len(cap.importable), 0, "%s 应有可导入来源" % tool)

    def test_session_import_targets(self) -> None:
        self.assertEqual(
            set(im.session_import_targets()),
            {"reasonix", "codebuddy", "workbuddy", "dsh"},
        )

    def test_encrypted_tools_backup_only(self) -> None:
        for tool in ("qoder", "trae-cn", "trae-solo-cn"):
            with self.subTest(tool=tool):
                cap = im.matrix_for(tool)
                self.assertEqual(cap.importable, ())
                self.assertTrue(all(s.status == im.BACKUP_ONLY for s in cap.sources))

    def test_no_self_import(self) -> None:
        """目标工具不应把自己列为可导入来源。"""
        for tool in im.known_targets():
            cap = im.matrix_for(tool)
            for s in cap.sources:
                self.assertNotEqual(s.tool, tool, "%s 不应把自己列为来源" % tool)


class TestSourceMetadata(unittest.TestCase):
    def test_supported_sources_have_version_and_scope(self) -> None:
        for tool, cap in ((t, im.matrix_for(t)) for t in im.known_targets()):
            for s in cap.sources:
                if s.status != im.SUPPORTED:
                    continue
                with self.subTest(source=s.tool, target=tool):
                    self.assertTrue(s.versions, "%s 缺版本" % s.tool)
                    self.assertTrue(s.scope, "%s 缺可导入范围" % s.tool)
                    self.assertTrue(s.status_label)

    def test_supported_parser_exists(self) -> None:
        for name in im.known_targets():
            for s in im.matrix_for(name).sources:
                if not s.parser:
                    continue
                with self.subTest(parser=s.parser):
                    self.assertTrue(
                        hasattr(session_migration.SessionParser, s.parser),
                        "session_migration.SessionParser 缺少 %s" % s.parser,
                    )

    def test_declared_writers_exist(self) -> None:
        for name in im.known_targets():
            for s in im.matrix_for(name).sources:
                if not s.writer:
                    continue
                with self.subTest(writer=s.writer):
                    self.assertTrue(
                        hasattr(session_migration.SessionWriter, s.writer),
                        "session_migration.SessionWriter 缺少 %s" % s.writer,
                    )


class TestDescribeAndFormat(unittest.TestCase):
    def test_describe_shape(self) -> None:
        for tool in im.known_targets():
            info = im.describe(tool)
            with self.subTest(tool=tool):
                self.assertEqual(info["tool"], tool)
                self.assertIsInstance(info["has_importable"], bool)
                self.assertIsInstance(info["summary"], str)
                self.assertIsInstance(info["rows"], list)
                for row in info["rows"]:
                    for key in ("display", "versions", "scope", "status",
                                "status_label", "color", "note"):
                        self.assertIn(key, row)
                    self.assertIsInstance(row["scope"], list)

    def test_describe_unknown_tool(self) -> None:
        info = im.describe("not_a_tool")
        self.assertFalse(info["has_importable"])
        self.assertEqual(info["rows"], [])
        self.assertTrue(info["summary"])

    def test_format_lines_nonempty(self) -> None:
        for tool in im.known_targets():
            lines = im.format_lines(tool)
            with self.subTest(tool=tool):
                self.assertTrue(lines)
                self.assertTrue(lines[0])
                self.assertIn(im.matrix_for(tool).display, lines[0])

    def test_status_labels_defined(self) -> None:
        for st in (im.SUPPORTED, im.BACKUP_ONLY, im.PLANNED):
            self.assertIn(st, im.STATUS_LABELS)
            self.assertIn(st, im.STATUS_COLORS)


if __name__ == "__main__":
    unittest.main()
