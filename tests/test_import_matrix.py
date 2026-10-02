"""导入能力矩阵（``ai_env_clone.import_matrix``）测试。

覆盖：
- 每个已注册适配器都有矩阵条目（新增工具后不会漏登记）；
- 明文工具（reasonix / codebuddy / workbuddy / dsh）至少有一个「支持导入」来源；
- 加密工具（qoder / trae-cn / trae-solo-cn）只能「仅备份/还原」；
- ``describe()`` 返回结构完整（界面直接消费，字段缺失会渲染异常）；
- ``format_lines()`` 能产出非空文本（弹窗消费）；
- 标注为 SUPPORTED 的来源，其 ``parser`` 在 session_migration 中确实存在；
  且其 ``writer``（若非空）同样存在；
- **版本号与 README 的「支持的工具」版本表一致**（防「界面说 A、文档说 B」）。
"""

import os
import re
import unittest

from ai_env_clone import import_matrix as im
from ai_env_clone import session_migration
from ai_env_clone.adapters import list_adapters

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class TestVersionStringsMatchReadme(unittest.TestCase):
    """版本号必须与 README「支持的工具」版本表一致（用户 2026-10-02 定的要求）。

    版本号是**承诺**：README 说支持某版本、界面又显示另一个号，用户就无法判断
    「这个包到底是在哪个版本上测的」。两处口径必须同一来源（见 README 版本说明段
    与 ``import_matrix`` 顶部的取值口径注释），故用测试把漂移钉死。

    判定方式刻意选「宽松但有效」：取每个 ``V_*`` 常量的**首个版本号形态**
    （如 ``1.106.1`` / ``0.2.0-rc.2``），断言它出现在 README 里 ——
    既不要求两处字符串完全等同（README 用 Markdown 加粗、常量带补充说明），
    又能捕捉「改了常量忘了改 README」这类真实漂移。
    """

    VER_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?")

    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = os.path.join(ROOT, "README.md")
        with open(cls.readme, encoding="utf-8") as fh:
            cls.text = fh.read()

    def _version_constants(self):
        return {
            name: value
            for name, value in vars(im).items()
            if name.startswith("V_") and isinstance(value, str)
        }

    def test_readme_exists(self) -> None:
        self.assertTrue(os.path.isfile(self.readme), "README.md 缺失，无法校验版本一致性")

    def test_every_version_constant_appears_in_readme(self) -> None:
        consts = self._version_constants()
        self.assertTrue(consts, "未找到任何 V_* 版本常量（口径变了就更新本用例）")
        missing = []
        for name, value in sorted(consts.items()):
            m = self.VER_RE.search(value)
            self.assertIsNotNone(m, "%s=%r 里没有版本号形态，无法校验" % (name, value))
            token = m.group(0)
            if token not in self.text:
                missing.append((name, token, value))
        self.assertEqual(
            missing, [],
            "以下版本号只存在于代码、README 版本表里没有（改了一处要同步另一处）: %s"
            % missing,
        )

    def test_dsh_version_reflects_settings_yaml_removal(self) -> None:
        """DSH 版本号必须 ≥ 移除 ``settings.yaml`` 的那一版，且 README 讲清后果。

        ``0.2.0-rc.2`` 起 ``$DSH_HOME/settings.yaml`` 被移除 ⇒ 备份条目随之变化。
        若有人把版本号改回旧值（或 README 不提这件事），这条会失败。
        """
        self.assertIn("settings.yaml", im.V_DSH + self.text,
                      "DSH 条目与 README 都应交代 settings.yaml 的处置")
        self.assertIn("0.2.0-rc.2", im.V_DSH)
        self.assertIn("0.2.0-rc.2", self.text)
        # README 必须写明「不再把 settings.yaml 当备份条目」
        self.assertIn("不再把 `settings.yaml` 列为备份条目", self.text)


if __name__ == "__main__":
    unittest.main()
