"""使用说明的文本处理测试（钉住「构建期派生无图版」这条设计）。

覆盖 ``ai_env_clone/doctext.py`` 与 ``ai_env_clone/resources.py``：

- ``strip_images()`` **幂等**（这是「构建期派生 + 运行期兜底都调它」的前提）；
- 对**仓库真文档** ``docs/使用说明.md`` 跑一遍：不含 ``![``、不含 ``images/``、
  **行数差 == 2**（1 行图片 + 1 个折叠空行；写成 1 是常见误判）、无连续空行；
- 三种图片形态：独立成行 / 行内 / 被链接包裹（``[![alt](img)](url)``）；
- 纯函数**不导入 tkinter**（受管 Python 没装 tkinter，导入了测试直接 ImportError）；
- ``resources.read_help_doc()`` 在 **frozen 与源码两种模式**下返回**逐字相同**的文本
  （用 monkeypatch 模拟 ``sys.frozen`` / ``sys._MEIPASS``）。
"""

import os
import sys
import tempfile
import unittest

from ai_env_clone import doctext, resources

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP_DOC = os.path.join(REPO_ROOT, "docs", "使用说明.md")


def _read_help_source() -> str:
    with open(HELP_DOC, "r", encoding="utf-8") as f:
        return f.read()


class TestStripImagesShapes(unittest.TestCase):
    """三种图片形态的删除规则。"""

    def test_standalone_line_removed_and_gap_collapsed(self) -> None:
        src = "第 7 项\n\n![主界面](images/main_window.png)\n\n---\n"
        out = doctext.strip_images(src)
        self.assertNotIn("![", out)
        self.assertNotIn("images/", out)
        # 图片行被删、其后紧邻的空行被折叠 ⇒ 只剩一个空行
        self.assertNotIn("\n\n\n", out)
        self.assertEqual(out, "第 7 项\n\n---\n")

    def test_inline_image_removed_without_double_space(self) -> None:
        src = "点 这里 ![图标](a.png) 即可打开设置面板。\n"
        out = doctext.strip_images(src)
        self.assertNotIn("![", out)
        # 不得残留双空格（中文会被视觉上粘在一起）
        self.assertNotIn("  ", out)
        self.assertIn("点 这里 即可打开设置面板。", out)

    def test_linked_image_removes_outer_link(self) -> None:
        """``[![alt](img)](url)`` 只删内层会留下 ``[](url)`` 坏链接。"""
        src = "[![badge](a.png)](https://example.com)\n"
        out = doctext.strip_images(src)
        self.assertNotIn("![", out)
        self.assertNotIn("example.com", out, "外层链接必须一并删除，不能留 [](url)")

    def test_two_consecutive_image_lines_collapse(self) -> None:
        src = "前\n\n![a](1.png)\n\n![b](2.png)\n\n后\n"
        out = doctext.strip_images(src)
        self.assertNotIn("![", out)
        self.assertNotIn("\n\n\n", out)
        self.assertEqual(out, "前\n\n后\n")

    def test_code_fence_left_untouched(self) -> None:
        """代码块内原样保留：缩进与空行是有语义的，不能折叠。"""
        src = "```\n\n  ![not-an-image](x.png)\n\n```\n"
        out = doctext.strip_images(src)
        self.assertIn("![not-an-image](x.png)", out)
        self.assertIn("\n\n", out)

    def test_empty_input(self) -> None:
        self.assertEqual(doctext.strip_images(""), "")


class TestStripImagesIdempotent(unittest.TestCase):
    def test_idempotent_on_synthetic(self) -> None:
        src = "a ![x](1.png) b\n\n![y](2.png)\n\nc\n"
        once = doctext.strip_images(src)
        self.assertEqual(doctext.strip_images(once), once)

    def test_idempotent_on_real_doc(self) -> None:
        once = doctext.strip_images(_read_help_source())
        self.assertEqual(doctext.strip_images(once), once)


class TestRealDoc(unittest.TestCase):
    """对仓库真文档跑一遍——规则跑得通不等于在真文档上跑得通。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = _read_help_source()
        cls.out = doctext.strip_images(cls.src)

    def test_no_image_markup_left(self) -> None:
        self.assertNotIn("![", self.out)
        self.assertNotIn("images/", self.out)

    def test_no_consecutive_blank_lines(self) -> None:
        self.assertNotIn("\n\n\n", self.out)

    def test_line_count_delta_is_two(self) -> None:
        """实测 283 → 281：少 2 行（1 行图片 + 1 个被折叠的空行）。"""
        src_lines = self.src.rstrip("\n").split("\n")
        out_lines = self.out.rstrip("\n").split("\n")
        self.assertEqual(
            len(src_lines) - len(out_lines),
            2,
            "行数差应为 2（图片行 1 + 折叠空行 1），实际 src=%d out=%d"
            % (len(src_lines), len(out_lines)),
        )

    def test_structure_around_image_preserved(self) -> None:
        """图片位置前后的内容与分隔线必须完好。"""
        self.assertIn("## 5. 备份与还原", self.out)
        self.assertIn("---", self.out)
        self.assertIn("8. **仅特定工具显示的行**", self.out)


class TestMdToParagraphs(unittest.TestCase):
    def test_never_emits_image_lines(self) -> None:
        """即使传入带图的原文（源码模式），段落里也不得含图片。"""
        paras = doctext.md_to_paragraphs(_read_help_source())
        joined = "\n".join(text for text, _ in paras)
        self.assertNotIn("![", joined)
        self.assertNotIn("[图片", joined, "用户定案：连占位都不写")

    def test_headings_and_list_and_code(self) -> None:
        src = "# 标题\n\n正文一段。\n\n- 项目甲\n- 项目乙\n\n```\ncode line\n```\n"
        tags = [tag for _, tag in doctext.md_to_paragraphs(src)]
        self.assertIn("h1", tags)
        self.assertIn("p", tags)
        self.assertIn("li", tags)
        self.assertIn("code", tags)

    def test_links_render_as_plain_text(self) -> None:
        paras = doctext.md_to_paragraphs("见 [项目主页](https://example.com) 了解详情。\n")
        text = paras[0][0]
        self.assertIn("项目主页", text)
        self.assertNotIn("example.com", text)

    def test_plain_text_and_paragraphs_same_source(self) -> None:
        """CLI 与 GUI 共用渲染 ⇒ 段落数与纯文本的非空行数应一致。"""
        src = "# 标题\n\n正文。\n\n- 甲\n- 乙\n"
        paras = doctext.md_to_paragraphs(src)
        text = doctext.plain_text(src)
        self.assertEqual(len([l for l in text.split("\n") if l.strip()]), len(paras))


class TestNoTkinterDependency(unittest.TestCase):
    """纯文本处理不得依赖 GUI 库（受管 Python 没装 tkinter）。"""

    def test_modules_do_not_pull_tkinter(self) -> None:
        for mod in ("ai_env_clone.doctext", "ai_env_clone.resources"):
            code = (
                "import sys, importlib;"
                "sys.modules.pop('tkinter', None);"
                "importlib.import_module(%r);"
                "print('tkinter' in sys.modules)" % mod
            )
            import subprocess

            py = sys.executable
            res = subprocess.run([py, "-c", code], capture_output=True, text=True,
                                 cwd=REPO_ROOT)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.strip(), "False",
                             "%s 导入后不应出现 tkinter" % mod)


class TestReadHelpDocBothModes(unittest.TestCase):
    """源码模式与打包模式必须返回**逐字相同**的文本。"""

    def test_frozen_and_source_agree(self) -> None:
        expected = doctext.strip_images(_read_help_source())
        # 源码模式：直接读仓库内文档
        self.assertEqual(resources.read_help_doc(), expected)

        # 打包模式：造一个 _MEIPASS，内含「构建期派生的无图版」
        with tempfile.TemporaryDirectory() as tmp:
            derived = os.path.join(tmp, resources.HELP_DOC_NAME)
            with open(derived, "w", encoding="utf-8", newline="") as f:
                f.write(doctext.strip_images(_read_help_source()))
            saved = {k: getattr(sys, k, None) for k in ("frozen", "_MEIPASS")}
            try:
                sys.frozen = True          # type: ignore[attr-defined]
                sys._MEIPASS = tmp         # type: ignore[attr-defined]
                self.assertEqual(resources.read_help_doc(), expected)
            finally:
                for k, v in saved.items():
                    if v is None:
                        try:
                            delattr(sys, k)
                        except AttributeError:
                            pass
                    else:
                        setattr(sys, k, v)

    def test_help_doc_name_is_ascii(self) -> None:
        """包内名必须是 ASCII：--add-data 两段路径还要按 os.pathsep 切分。"""
        self.assertTrue(resources.HELP_DOC_NAME.isascii(), resources.HELP_DOC_NAME)
        self.assertNotIn(os.pathsep, resources.HELP_DOC_NAME)

    def test_resource_path_missing_returns_none(self) -> None:
        self.assertIsNone(resources.resource_path("docs/images/不存在.png"))


class TestBuildTimeDerivation(unittest.TestCase):
    """★ 契约：「构建期派生件的字节」== ``strip_images(源文档)``。

    不重复实现派生逻辑，只断言它与运行期用的是**同一份实现**（靠幂等保证两模式一致）。
    另钉住：包内名是 ASCII、不含 ``os.pathsep``（``--add-data`` 要按它切分两段路径）。
    """

    def test_derived_bytes_match_strip_images(self) -> None:
        sys.path.insert(0, REPO_ROOT)
        try:
            import build_exe
        finally:
            sys.path.pop(0)

        with build_exe.doc_text_file() as path:
            self.assertIsNotNone(path, "使用说明源文档存在时必须派生出文件")
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as f:
                data = f.read()
        expected = doctext.strip_images(_read_help_source()).encode("utf-8")
        self.assertEqual(data, expected)
        self.assertNotIn(b"![", data)
        self.assertNotIn(b"images/", data)

    def test_derived_name_is_safe_for_add_data(self) -> None:
        self.assertTrue(resources.HELP_DOC_NAME.isascii())
        self.assertNotIn(os.pathsep, resources.HELP_DOC_NAME)

    def test_add_data_targets_bundle_root(self) -> None:
        """--add-data 的目标目录写 ``"."`` ⇒ exe 内即根下 help.md。"""
        import re

        sys.path.insert(0, REPO_ROOT)
        try:
            with open(os.path.join(REPO_ROOT, "build_exe.py"), encoding="utf-8") as f:
                src = f.read()
        finally:
            sys.path.pop(0)
        self.assertIn('"--add-data"', src)
        # 必须是 + os.pathsep + "." 的形式，而不是手拼分隔符
        self.assertRegex(src, r"str\(doc\)\s*\+\s*os\.pathsep\s*\+\s*\"\.\"")
        self.assertIsNone(re.search(r'"\.;"|";\."', src), "分隔符必须用 os.pathsep 拼")


if __name__ == "__main__":
    unittest.main()
