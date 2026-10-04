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
  宽度（东亚字符算 2 列）对齐、列间画 ``|`` 分隔符、表头下补等宽横线；
- **表格按调用方实测的可用宽度自适应**（``TestTableAdaptiveWidth``）：够宽就铺成网格
  （装得下不折行、装不下**自己折行但仍保持列对齐**），极窄才退成分条，且**两者之间
  只有一个切换阈值**；宽度判据用显示列而非 ``len()``；
- **折行按显示宽度、优先在可断字符之后断**（``TestWrapDisplay``）；
- **下划线一律不当强调**（``ai_env_clone`` / ``__init__.py`` / ``OPENAI_API_KEY``）；
- 纯函数**不导入 tkinter**（受管 Python 没装 tkinter，导入了测试直接 ImportError）；
- ``resources.read_help_doc()`` 在 **frozen 与源码两种模式**下返回**逐字相同**的文本
  （用 monkeypatch 模拟 ``sys.frozen`` / ``sys._MEIPASS``）。
"""

import os
import re
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
        """``|---|`` 分隔行必须消失，换成**按列宽画的横线**（不是照抄原文）。

        ★ 这里**不能**再断言「输出不含 ``|``」：列间分隔符本身就是 ``|``
        （``_TABLE_SEP``，见 :func:`doctext._layout_grid`）。判据改成「横线段数 =
        列数，且不是原文那三个横杠」——那正是「按列宽重画」的证据。
        """
        lines = self._table().split("\n")
        dashes = re.findall(r"-+", lines[1])
        self.assertEqual(len(dashes), 2, "两列 ⇒ 两条横线段：%r" % lines[1])
        self.assertNotEqual(dashes, ["---", "---"],
                            "不能照抄原文的 ``|---|``：%r" % lines[1])

    def test_header_underlined(self) -> None:
        """表头下补一条等宽横线，保住「哪行是表头」这个信息。"""
        lines = self._table().split("\n")
        self.assertEqual(len(lines), 4, "表头 + 横线 + 2 行数据：%r" % lines)
        self.assertRegex(lines[1], r"^-+( \| -+)+$", "横线行只含横线与列分隔符")
        self.assertEqual(lines[0].split("|")[0].strip(), "工具")

    def test_columns_separated_by_bar(self) -> None:
        """★ 列间画 ``|``：这是网格形态**唯一**的视觉标识。

        用户实测的反馈是「折行后的表看着像逐项列出」——根因就是纯文本网格不画
        表格线，折出来的续行与相邻列挤成一坨。画上竖线后，无论折成几行都能看出
        这是一张表，表格与分条之间也就只剩一个切换阈值。
        """
        out = self._table()
        self.assertEqual(out.count("|"), 4, "2 列 ⇒ 每行 1 条竖线，4 行共 4 条：%r" % out)
        for line in out.split("\n"):
            self.assertIn("|", line, "每一行都要有列分隔符：%r" % line)

    def test_cjk_column_width_pads_ascii_rows(self) -> None:
        """★ 按**显示宽度**对齐：中文算 2 列，按 ``len()`` 算会整体左偏。

        ``工具`` 显示宽度 4（2 个字符 × 2 列），列间是 ``" | "``（宽 3）⇒ 第二列从
        第 7 个**显示列**开始；``AB`` 只有 2 列宽，必须补 2 个空格才对齐。注意比较的
        必须是显示列而非字符下标——``工具`` 只占 2 个字符却是 4 列宽。
        """
        out = self._table("| 工具 | 备注 |\n| --- | --- |\n| AB | 短 |\n")
        lines = out.split("\n")

        def col(line: str, token: str) -> int:
            return doctext._disp_width(line[:line.index(token)])

        self.assertEqual(col(lines[0], "备注"), 7, lines)
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


class TestTableAdaptiveWidth(unittest.TestCase):
    """★ 表格形态由**调用方实测传入的可用宽度**决定，且**只有一个切换阈值**。

    这是用户三轮反馈的落点，逐条记下来免得再退回去：

    1. 「表格显示效果不好」—— 当时可用宽度是写死的 ``96`` 列，超过就分条；
    2. 「窗口拉到最大还是逐项列出」—— 宽度虽然变成参数了，但渲染结果仍与窗口无关；
    3. ★「宽度变小变回逐项列出的阈值，远小于宽度变大后变回表格的宽度阈值，应该
       一样」—— 当时判据有两条（``≥ 自然宽`` 走不折行网格、``≥ 每列最小宽`` 走折行
       网格、其余分条），于是**两个方向的切换点不同**：4 列表缩小时 64 列就变分条、
       放大时要到 169 列才不再折行。而中间那档折行网格没有表格的视觉标识（纯文本
       网格不画竖线），被用户读成了「逐项列出」，看起来就像两个错开的阈值。

    现在的契约只有两条，且共用一个阈值 :func:`doctext._table_min_total`：

    - **可用宽度 ≥ 阈值** → ``table``：列间画 ``|`` 的等宽网格，放不下自然宽的列
      **自己折行**（按行号并排 ⇒ 列边界始终竖直对齐）。真文档两张表的自然宽是
      172 / 340 显示列，任何屏幕都装不下，所以折行是主路径；**交给控件折行**才是
      灾难（续行从最左起排、列与表头全错位）。
    - **低于阈值** → ``records``（分条）：每列不足 ``_TABLE_FIT_MIN_COL`` 列，网格里
      的字会被切碎，不如把长句整段交给控件。
    """

    LIMIT = doctext._TABLE_MAX_WIDTH
    MIN_W = doctext._TABLE_MIN_WIDTH

    def _paras(self, src: str, width=None):
        paras = doctext.md_to_paragraphs(src, width)
        self.assertEqual(len(paras), 1, "整张表仍是**一个**段落：%r" % paras)
        return paras[0]

    def _wide_src(self) -> str:
        """构造一张远超默认宽度的表（靠一个长值撑开）。"""
        return ("| 工具 | 备注 |\n| --- | --- |\n| 甲 | %s |\n" % ("x" * self.LIMIT))

    @staticmethod
    def _col_spans(text: str) -> list[tuple[int, int]]:
        """从表头下的等宽横线反推各列的 ``[起, 止)`` 区间（折行与否都成立）。

        横线行只由 ``-``、空格、``|`` 组成（全 ASCII）⇒ 它的**字符下标就等于显示列
        下标**，可以放心拿这里的区间去切 :meth:`_disp_cells` 摊出来的显示列。
        """
        sep = text.split("\n")[1]
        return [(m.start(), m.end()) for m in re.finditer(r"-+", sep)]

    @staticmethod
    def _disp_cells(line: str) -> list[str]:
        """把一行摊成「每个**显示列**一格」的列表（东亚宽字符占两格）。

        ★ 不能拿显示列下标去切 Python 字符串：``工具`` 是 2 个字符但 4 个显示列，
        下标会整体错位（这正是「按显示宽度对齐」在代码里最容易写错的地方）。
        """
        cells: list[str] = []
        for ch in line:
            cells.append(ch)
            if doctext._disp_width(ch) == 2:
                cells.append("")      # 该字符占用的第二列
        return cells

    def _assert_columns_aligned(self, text: str) -> None:
        """★ 不变式：所有行在**列间空隙**处必须全是空格 ⇒ 列边界竖直对齐。

        这是「折行之后还算不算表格」的判据。校验「空隙」而不是「列内容」，
        是因为内容本身可以折成多行、也可以为空；而空隙只有在列错位时才会被占用。
        """
        spans = self._col_spans(text)
        self.assertGreaterEqual(len(spans), 2, "至少两列才谈得上对齐：%r" % text)
        gaps = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
        for line in text.split("\n"):
            cells = self._disp_cells(line)
            for a, b in gaps:
                # 列间空隙里只允许出现空格与列分隔符 ``|``（见 ``doctext._TABLE_SEP``）；
                # 出现别的字符就说明内容越到了列边界上 ⇒ 列没对齐。
                self.assertTrue(
                    set("".join(cells[a:b])) <= {" ", "|"},
                    "列边界处有内容 ⇒ 列没对齐：%r" % line,
                )

    def test_fitting_table_is_not_wrapped(self) -> None:
        """装得下就不折：折行会让行数变多，能一行放完就一行。"""
        text, tag = self._paras(
            "| 甲 | b |\n| --- | --- |\n| 1 | %s |\n" % ("x" * 40), self.LIMIT)
        self.assertEqual(tag, "table")
        self.assertEqual(len(text.split("\n")), 3, "两列都放得下 ⇒ 不折行")

    def test_exactly_at_the_limit_is_not_wrapped(self) -> None:
        """刚好不超上限不折行（边界不多不少）。"""
        # 两列：首列 "甲"（2 列）+ 列间 3 列 ⇒ 值取 LIMIT - 5 恰在线上
        text, tag = self._paras(
            "| 甲 | b |\n| --- | --- |\n| 1 | %s |\n" % ("x" * (self.LIMIT - 5)),
            self.LIMIT)
        self.assertEqual(tag, "table")
        self.assertEqual(len(text.split("\n")), 3)

    def test_wide_table_wraps_instead_of_reflowing(self) -> None:
        """★ 宽表也仍是**表格形态**（自己折行），不再退成「列名：值」的分条。"""
        text, tag = self._paras(self._wide_src(), self.LIMIT)
        self.assertEqual(tag, "table", "超宽应当是折行网格，而不是分条")
        self.assertGreater(len(text.split("\n")), 3, "超宽 ⇒ 必须折行")
        self._assert_columns_aligned(text)

    def test_grid_never_exceeds_the_available_width(self) -> None:
        """★ 不变式：``table`` 形态的每一行都不超过传入的可用宽度（否则等于没做）。"""
        for width in (self.MIN_W, 60, self.LIMIT, 118, 169, 400):
            for src in (self._wide_src(), _read_help_source()):
                for text, tag in doctext.md_to_paragraphs(src, width):
                    if tag != "table":
                        continue
                    widest = max(doctext._disp_width(l) for l in text.split("\n"))
                    self.assertLessEqual(
                        widest, width, "宽度 %d 下仍超宽：%r" % (width, text[:60]))

    def test_wider_available_width_means_fewer_lines(self) -> None:
        """★ **用户诉求本身**：可用宽度变大 → 同一张表行数变少（跟着窗口变宽）。"""
        src = _read_help_source()
        # 88 = 880 px 查看器的实测列数；300 = 拉宽后。两者都 ≥ 唯一的切换阈值
        # （doctext._table_min_total = 68），所以两张表都是网格形态，可以直接比行数。
        narrow = [t for t, tag in doctext.md_to_paragraphs(src, 88) if tag == "table"]
        wide = [t for t, tag in doctext.md_to_paragraphs(src, 300) if tag == "table"]
        self.assertEqual(len(narrow), len(wide), "两张表在两种宽度下都该是表格形态")
        self.assertEqual(len(wide), 2)
        for n, w in zip(narrow, wide):
            self.assertGreater(len(n.split("\n")), len(w.split("\n")),
                               "可用宽度变大后行数必须更少（折得更少）")

    def test_real_doc_tables_are_tables_at_viewer_width(self) -> None:
        """真文档两张表在查看器宽度（约 118 列）下必须是**表格**形态且列对齐。"""
        paras = [(t, tag) for t, tag in doctext.md_to_paragraphs(_read_help_source(), 118)
                 if tag in ("table", "records")]
        self.assertEqual([tag for _t, tag in paras], ["table", "table"])
        for text, _tag in paras:
            self._assert_columns_aligned(text)

    def test_very_narrow_falls_back_to_records(self) -> None:
        """窄到网格排不下（低于 ``_TABLE_MIN_WIDTH``）才退成分条。"""
        text, tag = self._paras(self._wide_src(), self.MIN_W - 1)
        self.assertEqual(tag, "records", "极窄时必须退成分条")
        self.assertNotIn("|", text, "原始竖线不该出现")

    def test_min_width_is_still_a_grid(self) -> None:
        """正好到下限仍走网格：下限的定义就是「再窄一格才分条」。"""
        _text, tag = self._paras(
            "| 甲 | b |\n| --- | --- |\n| 1 | %s |\n" % ("x" * 200), self.MIN_W)
        self.assertEqual(tag, "table")

    def test_records_layout_shape(self) -> None:
        """（极窄时）首列当标题、其余列 ``列名：值`` 且缩进两格；条目之间空一行。

        注意填充串要**长于可用宽度**：分条的触发条件是「自然宽装不下 **且** 宽度
        低于下限」，短表即使窗口很窄也还是网格（它一行放得下）。
        """
        src = ("| 工具 | 备注 |\n| --- | --- |\n"
               "| 甲 | %s |\n| 乙 | %s |\n" % ("x" * 60, "y" * 60))
        text, tag = self._paras(src, self.MIN_W - 1)
        self.assertEqual(tag, "records")
        lines = text.split("\n")
        self.assertEqual(lines[0], "甲")
        self.assertEqual(lines[1], "  备注：" + "x" * 60)
        self.assertEqual(lines[2], "")
        self.assertEqual(lines[3], "乙")
        self.assertEqual(lines[4], "  备注：" + "y" * 60)

    def test_records_skip_empty_values(self) -> None:
        """值为空的列直接跳过，否则会留下一串 ``备注：`` 这种光杆标签。"""
        src = ("| 工具 | 中间 | 备注 |\n| --- | --- | --- |\n"
               "| 甲 |  | %s |\n" % ("x" * 60))
        text, _tag = self._paras(src, self.MIN_W - 1)
        self.assertIn("  备注：" + "x" * 60, text)
        self.assertNotIn("中间", text)

    def test_records_without_first_column_keeps_fields(self) -> None:
        """首列为空的行不写标题行，但字段要照常输出。"""
        src = ("| 工具 | 备注 |\n| --- | --- |\n"
               "|  | %s |\n" % ("x" * 60))
        text, _tag = self._paras(src, self.MIN_W - 1)
        self.assertEqual(text, "  备注：" + "x" * 60)

    def test_short_table_is_a_grid_above_the_threshold(self) -> None:
        """窄窗口 + 短表 ⇒ 仍是网格（放得下自然宽就不折行）。"""
        src = "| 工具 | 备注 |\n| --- | --- |\n| 甲 | 小 |\n"
        text, tag = self._paras(src, self.MIN_W)
        self.assertEqual(tag, "table")
        self.assertIn("工具 | 备注", text)
        self.assertEqual(len(text.split("\n")), 3, "放得下就不折行")

    def test_one_threshold_for_every_table(self) -> None:
        """★ **一个阈值**：不论表长表短、哪张表，切换点都是同一个宽度。

        这正是用户反馈「应该一样才对，不需要这样的错开」要钉住的行为：判据只有
        :func:`doctext._table_min_total` 一个算式，变宽与变窄走的是同一条路。
        """
        short = "| 工具 | 备注 |\n| --- | --- |\n| 甲 | 小 |\n"
        long = "| 工具 | 备注 |\n| --- | --- |\n| 甲 | %s |\n" % ("x" * 200)
        for name, src in (("短表", short), ("长表", long)):
            self.assertEqual(self._paras(src, self.MIN_W)[1], "table",
                             "%s 在阈值处应为表格" % name)
            self.assertEqual(self._paras(src, self.MIN_W - 1)[1], "records",
                             "%s 在阈值下一格应为分条" % name)

    def test_threshold_does_not_shrink_with_fewer_columns(self) -> None:
        """★ 列数少的表**不再**有更低的分条阈值（否则同一宽度下形态混搭）。

        曾经的算式是 ``max(40, ncol × (14 + 2))``：2 列表阈值 40、4 列表 64 ⇒ 在
        40~63 列之间，同一窗口里一张表是表格、另一张已退成分条，看起来就是
        「阈值错开」。抬到统一的 :data:`doctext._TABLE_MIN_WIDTH` 后两者一致。
        """
        two = "| 甲 | 备注 |\n| --- | --- |\n| 1 | %s |\n" % ("x" * 200)
        four = ("| 甲 | 乙 | 丙 | 丁 |\n| --- | --- | --- | --- |\n"
                "| 1 | 2 | 3 | %s |\n" % ("x" * 200))
        for ncol, src in ((2, two), (4, four)):
            self.assertEqual(self._paras(src, self.MIN_W)[1], "table",
                             "%d 列表在阈值处应为表格" % ncol)
            self.assertEqual(self._paras(src, self.MIN_W - 1)[1], "records",
                             "%d 列表在阈值下一格应为分条" % ncol)

    def test_disp_width_used_not_len(self) -> None:
        """宽度判据必须是**显示列**（东亚字符 2 列），否则 96 列的中文表会算成 48。"""
        # 60 个汉字 = 120 显示列 > 96 ⇒ 必须折行；按 len() 算（60）会误判成「没超」
        src = ("| 甲 | b |\n| --- | --- |\n| 1 | %s |\n"
               % ("中" * (self.LIMIT // 2 + 12)))
        text, tag = self._paras(src, self.LIMIT)
        self.assertEqual(tag, "table")
        self.assertGreater(len(text.split("\n")), 3,
                           "按 len() 算会误判成「不超宽」而不折行")


class TestWrapDisplay(unittest.TestCase):
    """``_wrap_disp``：按显示宽度折行，优先在**可断字符之后**断。

    这一函数是「折行网格」的地基：折错了会出现「行还是超宽」或「把路径从中间劈开」。
    假定列宽远大于单个字符（表格列宽下限是 ``_TABLE_FIT_MIN_COL`` = 14 列），
    所以这里只测宽度 ≥ 4 的情况——比列宽下限宽裕得多。
    """

    def test_short_text_untouched(self) -> None:
        self.assertEqual(doctext._wrap_disp("abc", 10), ["abc"])

    def test_wraps_at_space(self) -> None:
        self.assertEqual(doctext._wrap_disp("hello world foo", 12),
                         ["hello world", "foo"])

    def test_prefers_a_break_over_splitting_a_word(self) -> None:
        """★ 有断点就断，哪怕它很靠前——劈开单词比多占一行糟得多。

        回归用例：``Qoder CN（JetBrains`` 在 14 列宽下唯一可用的断点是 ``Qoder CN（``
        （只占 10/14 列）。早先有一条「断点不足半行就硬断」的判据把它拒掉了，于是
        ``JetBrains`` 被劈成 ``JetB`` / ``rains``（实测出现在 14 列的窄列里）。
        """
        out = doctext._wrap_disp("Qoder CN（JetBrains", 14)
        self.assertEqual(out, ["Qoder CN（", "JetBrains"], out)

    def test_breaks_after_path_separator(self) -> None:
        """★ ``com.qodercn.app.stable`` 这类长串不该被从单词中间劈开。"""
        out = doctext._wrap_disp("com.qodercn.app.stable", 12)
        self.assertEqual(out[0], "com.qodercn.")
        self.assertEqual("".join(out), "com.qodercn.app.stable")

    def test_cjk_wraps_char_by_char(self) -> None:
        """中文没有词间空格 ⇒ 只能逐字断，且不得丢字符。"""
        s = "中文没有空格只能逐字折行"
        out = doctext._wrap_disp(s, 8)
        self.assertEqual("".join(out), s)
        self.assertTrue(all(doctext._disp_width(x) <= 8 for x in out))

    def test_hard_break_when_no_good_break_point(self) -> None:
        self.assertEqual([len(x) for x in doctext._wrap_disp("x" * 30, 10)],
                         [10, 10, 10])

    def test_never_exceeds_width(self) -> None:
        for width in (4, 7, 20):
            for s in ("a" * 25, "中" * 25, "a中b/中c.d-e f", "a" * 3):
                for line in doctext._wrap_disp(s, width):
                    self.assertLessEqual(doctext._disp_width(line), width,
                                         "%r @ 宽度 %d" % (line, width))


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
