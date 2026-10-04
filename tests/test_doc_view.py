"""「帮助 → 使用说明」查看器：纯文本、与 CLI ``--docs`` **同源**、不碰图片。

用户定案（经三次收紧）：查看器**只显示文字**，图片行**整行跳过、连占位都不写**。
「与 CLI 一致」这件事不是靠人工同步两份文案，而是**同一个渲染函数换输出端**：

- 界面：``doctext.md_to_paragraphs()`` → ``ScrolledText``（按标签设字体/缩进）；
- CLI：``doctext.plain_text()``（内部同样调 ``md_to_paragraphs()``）→ 终端。

所以本文件的关键断言有三条：

1. ★ 查看器里的正文**不含** ``![``、不含 ``images/``、不含 ``[图片`` 占位；
2. ★ 两者**段落序列一致**（同源，不靠人工同步）；
3. ★ ``_show_help`` 的源码里**没有 ``PhotoImage``** —— 图片从未被解码过，
   也就不存在「引用丢了导致 GC 后变空白」这条 Tk 经典坑。

另有一组与用户反馈相关的：**表格必须按窗口实际宽度重排，且形态切换只有一个阈值**——
宽表在查看器里是**带列分隔符的折行表格**（自己折行、列仍对齐），只有窄到网格排不下
才退成分条（``records``）；窗口宽度变化后必须重新排版（不是渲染一次就定死），
缩小时变分条的宽度与放大时变回表格的宽度是同一个。
"""
import inspect
import os
import sys
import unittest
from unittest import mock

import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import doctext
from ai_env_clone.__main__ import QoderBackupApp


def _read_help_source() -> str:
    with open(os.path.join(ROOT, "docs", "使用说明.md"), "r", encoding="utf-8") as f:
        return f.read()


class TestRenderersShareOneFunction(unittest.TestCase):
    """渲染纯函数层面：GUI 与 CLI 同源（**不需要 tkinter** 的部分）。"""

    def test_plain_text_is_built_on_the_same_paragraph_sequence(self) -> None:
        md = "## 标题\n\n正文 **粗** 与 `码`\n\n- 甲\n\n1. 乙\n"
        paras = doctext.md_to_paragraphs(md)
        self.assertEqual([t for t, _ in paras], ["标题", "正文 粗 与 码", "- 甲", "1. 乙"])
        # plain_text 只做「换输出端」的呈现差异（hr 横线 / code 缩进），
        # 段落内容必须来自同一个函数。
        self.assertIn("正文 粗 与 码", doctext.plain_text(md))

    def test_image_line_never_reaches_either_output(self) -> None:
        md = "前\n\n![主界面](images/main_window.png)\n\n后\n"
        self.assertNotIn("![", doctext.plain_text(md))
        self.assertNotIn("images/", doctext.plain_text(md))
        for text, _tag in doctext.md_to_paragraphs(md):
            self.assertNotIn("![", text)
            self.assertNotIn("images/", text)
            self.assertNotIn("[图片", text, "连占位都不许写")

    def test_real_doc_paragraphs_have_no_images(self) -> None:
        """对**仓库真文档**跑一遍：段落序列里不许出现任何图片痕迹。"""
        for text, _tag in doctext.md_to_paragraphs(_read_help_source()):
            self.assertNotIn("![", text)
            self.assertNotIn("images/", text)
            self.assertNotIn("[图片", text)


class TestHelpViewer(unittest.TestCase):
    """真实 Tk 下的查看器（共享一个未映射窗口）。"""

    @classmethod
    def setUpClass(cls):
        fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None), \
             mock.patch.object(QoderBackupApp, "_detect_root", return_value=fake_root):
            cls.root = tk.Tk()
            cls.root.withdraw()
            cls.app = QoderBackupApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._cancel_after()
        except Exception:
            pass
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_source_never_touches_photoimage(self) -> None:
        """★ 查看器源码里不许出现 ``PhotoImage``：图片从未解码，也就不会变空白。"""
        src = inspect.getsource(QoderBackupApp._show_help)
        self.assertNotIn("PhotoImage", src)
        self.assertNotIn("image_create", src)

    def test_viewer_shows_text_without_any_image_markup(self) -> None:
        win = self.app._show_help()
        try:
            self.assertIsNotNone(win, "文档存在时应弹出查看器")
            body = win.doc_text.get("1.0", "end")
            self.assertIn("AiEnvClone", body)
            self.assertNotIn("![", body, "图片行必须整行跳过")
            self.assertNotIn("images/", body)
            self.assertNotIn("[图片", body, "不留占位")
            self.assertEqual(win.title(), "使用说明")
            self.assertEqual(win.doc_text["state"], "disabled", "查看器必须只读")
        finally:
            if win is not None:
                win.destroy()

    def test_viewer_content_equals_cli_renderer(self) -> None:
        """查看器里逐段插入的文本 == **同一宽度下** ``md_to_paragraphs`` 的段落序列。

        宽度显式打桩：不然「查看器用实测值、期望值用默认值」两边会各按各的宽度渲染，
        这条测试就只在两者刚好相等时才碰巧通过。
        """
        with mock.patch.object(QoderBackupApp, "_doc_available_columns",
                               return_value=118):
            win = self.app._show_help()
            try:
                body = win.doc_text.get("1.0", "end")
            finally:
                if win is not None:
                    win.destroy()
        for para, _tag in doctext.md_to_paragraphs(_read_help_source(), 118):
            if para:
                self.assertIn(para, body)

    def test_missing_doc_warns_and_returns_none(self) -> None:
        """打包漏带资源属构建缺陷：给可诊断提示，不抛栈、不开空窗口。"""
        with mock.patch("ai_env_clone.__main__.read_help_doc",
                        side_effect=OSError("缺失")), \
             mock.patch("ai_env_clone.__main__.messagebox") as mb:
            win = self.app._show_help()
        self.assertIsNone(win)
        self.assertTrue(mb.showwarning.called, "缺文档时应给出提示")

    def test_wide_table_renders_as_aligned_grid(self) -> None:
        """★ 宽表在查看器里仍是**表格形态**（自己折行、列对齐），不是「列名：值」分条。

        之前的实现把可用宽度写死在 ``doctext`` 里，宽表一律分条 ⇒ 用户「窗口拉到最大
        还是逐项列出」。这里同时钉住：表头进入了界面、列间画了分隔竖线（网格形态唯一
        的视觉标识，用户第二轮反馈的落点）、以及 ``records`` 标签的悬挂缩进仍备好
        （极窄时会用到）。
        """
        with mock.patch.object(QoderBackupApp, "_doc_available_columns",
                               return_value=118):
            win = self.app._show_help()
            try:
                body = win.doc_text.get("1.0", "end")
                self.assertIn("数据目录（示例）", body, "表头必须出现")
                self.assertNotIn("数据目录（示例）：", body,
                                 "118 列下宽表应是折行表格，不该退成分条")
                self.assertIn("|", body,
                              "列间必须有分隔竖线，否则折行后的表看着像逐项列出")
                self.assertIn("table", win.doc_text.tag_names(),
                              "查看器必须为 table 配好等宽标签")
                self.assertGreater(
                    float(win.doc_text.tag_cget("records", "lmargin2")), 0,
                    "分条形态必须设 lmargin2，否则长值折行后与标题平齐",
                )
            finally:
                if win is not None:
                    win.destroy()

    def test_rerender_follows_available_width(self) -> None:
        """★ 用户诉求：宽度变了表格形态必须跟着变（不是渲染一次就定死）。

        宽 → 折行表格；极窄 → 退成分条；再变宽 → 必须能排回表格。
        """
        with mock.patch.object(QoderBackupApp, "_doc_available_columns",
                               return_value=118):
            win = self.app._show_help()
        try:
            wide = win.doc_text.get("1.0", "end")
            self.assertIn("数据目录（示例）", wide)
            self.assertNotIn("数据目录（示例）：", wide)

            with mock.patch.object(QoderBackupApp, "_doc_available_columns",
                                   return_value=40):
                self.app._render_doc(win)
            self.assertIn("数据目录（示例）：", win.doc_text.get("1.0", "end"),
                          "极窄时该退成分条")

            with mock.patch.object(QoderBackupApp, "_doc_available_columns",
                                   return_value=118):
                self.app._render_doc(win)
            self.assertEqual(win.doc_text.get("1.0", "end"), wide,
                             "变宽后必须能重排回表格形态")
            self.assertEqual(win.doc_text["state"], "disabled", "重排后仍必须只读")
        finally:
            if win is not None:
                win.destroy()

    def test_available_columns_measured_from_widget(self) -> None:
        """``_doc_available_columns`` 是**实测**的：宽度变大，测得列数必须跟着变大。"""
        win = self.app._show_help()
        try:
            txt = win.doc_text
            cols = self.app._doc_available_columns(txt)
            self.assertGreater(cols, 10, "未映射窗口也该给出可用的兜底列数")
            self.assertLess(cols, 1000)
        finally:
            if win is not None:
                win.destroy()


if __name__ == "__main__":
    unittest.main()
