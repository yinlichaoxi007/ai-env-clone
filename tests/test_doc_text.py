"""使用说明的文本处理测试（钉住「构建期派生无图版」这条设计）。

覆盖 ``ai_env_clone/doctext.py`` 与 ``ai_env_clone/resources.py``：

- ``strip_images()`` **幂等**（这是「构建期派生 + 运行期兜底都调它」的前提）；
- 对**仓库真文档** ``docs/使用说明.md`` 跑一遍：不含 ``![``、不含 ``images/``、
  **行数差 == 2**（1 行图片 + 1 个折叠空行；写成 1 是常见误判）、无连续空行；
- 三种图片形态：独立成行 / 行内 / 被链接包裹（``[![alt](img)](url)``）；
- **行内标记只删标记、保留内容**（``TestInlineMarkers``）——Tk ``Text`` 与终端都不
  渲染 markdown，原样留着用户看到的就是一堆 ``**``、`` ` ``；
- **代码片段必须先占位、后还原**（``TestInlineMarkers.test_code_span_protects_asterisks``）：
  真文档里有 `` `***REDACTED***` ``、`` `messages/*.json` ``，先剥反引号会把它们吃坏；
- **表格渲染成对齐纯文本**（``TestTableRendering``）：丢掉 ``|---|`` 分隔行、按显示
  宽度（东亚字符算 2 列）对齐、表头下补等宽横线；
- **下划线一律不当强调**（``ai_env_clone`` / ``__init__.py`` / ``OPENAI_API_KEY``）；
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


class TestInlineMarkers(unittest.TestCase):
    """★ 行内标记只删标记、保留内容（Tk ``Text`` 与终端都不渲染 markdown）。"""

    def _one(self, src: str) -> str:
        paras = doctext.md_to_paragraphs(src)
        self.assertEqual(len(paras), 1, "应只产出一个段落：%r" % paras)
        return paras[0][0]

    def test_bold_italic_strike_code_stripped(self) -> None:
        self.assertEqual(self._one("前 **粗体** 后\n"), "前 粗体 后")
        self.assertEqual(self._one("前 *斜体* 后\n"), "前 斜体 后")
        self.assertEqual(self._one("前 ~~删除~~ 后\n"), "前 删除 后")
        self.assertEqual(self._one("前 `代码` 后\n"), "前 代码 后")

    def test_code_span_protects_asterisks(self) -> None:
        """★ 代码片段先占位后还原：里面的 ``*`` 不得被强调规则吃掉。

        真文档里就有 `` `***REDACTED***` `` 与 `` `messages/*.json` ``：
        先剥反引号会把前者吃成 ``REDACTED``、后者吃成 ``messages/.json``。
        """
        self.assertEqual(self._one("占位 `***REDACTED***` 结束\n"),
                         "占位 ***REDACTED*** 结束")
        self.assertEqual(self._one("正文 `messages/*.json` 快照\n"),
                         "正文 messages/*.json 快照")
        self.assertEqual(self._one("路径 `a\\b\\*\\c.json` 命中\n"),
                         "路径 a\\b\\*\\c.json 命中")

    def test_underscore_never_treated_as_emphasis(self) -> None:
        """★ 下划线一律不处理：识别符远多于真正的 ``_斜体_``。

        宽松匹配会把 ``ai_env_clone`` 劈成 ``aienv_clone``、把
        ``__init__.py`` 劈成 ``init.py`` —— 那是直接改坏命令与路径。
        """
        for src in ("python -m ai_env_clone\n",
                    "读 ai_env_clone/__init__.py 里的常量\n",
                    "环境变量 OPENAI_API_KEY 与 MY_KEY_ENV\n",
                    "字段 qoder_backup_ 与 import_migration\n"):
            self.assertEqual(self._one(src), src.rstrip("\n"))

    def test_link_keeps_text_only(self) -> None:
        self.assertEqual(self._one("见 [项目主页](https://example.com) 详情。\n"),
                         "见 项目主页 详情。")

    def test_empty_code_span(self) -> None:
        """`` `` `` 这种空代码片段不得留下反引号。"""
        self.assertNotIn("`", self._one("前 `` 后\n"))

    def test_real_doc_has_no_marker_left(self) -> None:
        """真文档跑完后：无成对加粗、无反引号、无原始表格行。

        只允许两处**故意的内容**里出现 ``**``：脱敏占位 ``***REDACTED***`` 与
        通配模式 ``history/**``（它们本来就该原样显示）。
        """
        paras = doctext.md_to_paragraphs(_read_help_source())
        joined = "\n".join(text for text, _ in paras)
        self.assertNotIn("`", joined, "反引号必须全部剥掉")
        self.assertNotIn("~~", joined)
        for line in joined.split("\n"):
            if "**" in line:
                self.assertTrue(
                    "REDACTED" in line or "history/**" in line,
                    "只允许脱敏占位与通配模式保留 **：%r" % line[:120],
                )
        self.assertNotIn("|---", joined, "表格分隔行必须丢弃")


class TestTableRendering(unittest.TestCase):
    """★ 表格渲染成**对齐的纯文本**（纯文本控件画不出表格线）。"""

    SRC = (
        "| 工具 | 备注 |\n"
        "| --- | --- |\n"
        "| Qoder | 会话主库加密 |\n"
        "| AB | 短 |\n"
    )

    def _table(self, src: str = SRC) -> str:
        paras = doctext.md_to_paragraphs(src)
        self.assertEqual(len(paras), 1, "整个表格应是**一个**段落：%r" % paras)
        self.assertEqual(paras[0][1], "table")
        return paras[0][0]

    def test_separator_row_dropped(self) -> None:
        """``|---|`` 这一行必须消失：纯文本画不出表格线，留着只是多一串横杠。

        注意**只有表头下那一条**横线是允许的（见 ``test_header_underlined``），
        故这里断言的是「原始竖线没了 + 横线恰好一条」，而不是「不含 ``-``」。
        """
        out = self._table()
        self.assertNotIn("|", out, "原始竖线不应出现")
        dash_lines = [l for l in out.split("\n") if set(l) <= set("- ")]
        self.assertEqual(len(dash_lines), 1,
                         "只应有表头下这一条横线：%r" % out.split("\n"))

    def test_header_underlined(self) -> None:
        """表头下补一条等宽横线，保住「哪行是表头」这个信息。"""
        lines = self._table().split("\n")
        self.assertEqual(len(lines), 4, "表头 + 横线 + 2 行数据：%r" % lines)
        self.assertRegex(lines[1], r"^-+(\s+-+)*$", "横线行只含横线与分隔空格")
        self.assertEqual(lines[0].split()[0], "工具")

    def test_cjk_column_width_pads_ascii_rows(self) -> None:
        """★ 按**显示宽度**对齐：中文算 2 列，按 ``len()`` 算会整体左偏。

        ``工具`` 显示宽度 4（2 个字符 × 2 列），列间留 2 空格 ⇒ 第二列从第 6 个
        **显示列**开始；``AB`` 只有 2 列宽，必须补 2 个空格才对齐。注意比较的必须
        是显示列而非字符下标——``工具`` 只占 2 个字符却是 4 列宽。
        """
        out = self._table("| 工具 | 备注 |\n| --- | --- |\n| AB | 短 |\n")
        lines = out.split("\n")

        def col(line: str, token: str) -> int:
            return doctext._disp_width(line[:line.index(token)])

        self.assertEqual(col(lines[0], "备注"), 6, lines)
        self.assertEqual(col(lines[2], "短"), col(lines[0], "备注"),
                         "ASCII 单元格必须按显示宽度补齐：%r" % lines)

    def test_cjk_width_counted_as_two(self) -> None:
        self.assertEqual(doctext._disp_width("工具"), 4)
        self.assertEqual(doctext._disp_width("AB"), 2)

    def test_table_without_separator_has_no_rule(self) -> None:
        """非标准写法（无分隔行）→ 原样对齐、不加横线。"""
        out = self._table("| a | b |\n| c | d |\n")
        self.assertEqual(len(out.split("\n")), 2, out)

    def test_empty_middle_cell_does_not_shift_columns(self) -> None:
        """中间空单元格不能让后面的列错位。"""
        out = self._table("| a | b | c |\n| --- | --- | --- |\n| 1 |  | 3 |\n")
        lines = out.split("\n")
        self.assertEqual(lines[0].index("c"), lines[2].index("3"), lines)

    def test_missing_trailing_cells_do_not_raise(self) -> None:
        """行尾缺列（表格写法不完整）时补齐即可，不得抛异常。"""
        out = self._table("| a | b | c |\n| --- | --- | --- |\n| 1 |\n")
        self.assertEqual(len(out.split("\n")), 3, out)


class TestListAndQuote(unittest.TestCase):
    def test_code_fence_marker_not_emitted(self) -> None:
        """★ 栅栏行本身不产出段落：查看器里显示 `` ```powershell `` 纯属噪音。"""
        paras = doctext.md_to_paragraphs("```powershell\npython -m x\n```\n")
        self.assertEqual(paras, [("python -m x", "code")])

    def test_ordered_list_keeps_number(self) -> None:
        """★ 有序列表的编号是内容的一部分，不能丢。"""
        paras = doctext.md_to_paragraphs("1. 第一步\n2. 第二步\n")
        self.assertEqual([t for t, _ in paras], ["1. 第一步", "2. 第二步"])
        self.assertEqual([tag for _, tag in paras], ["li", "li"])

    def test_bare_quote_line_produces_no_paragraph(self) -> None:
        """引用块里的空续行（单独一个 ``>``）只是换行，不该产出空段落。"""
        paras = doctext.md_to_paragraphs("> 甲\n>\n> 乙\n")
        self.assertEqual([t for t, _ in paras], ["甲", "乙"])


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
