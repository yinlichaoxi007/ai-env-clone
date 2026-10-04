"""菜单与界面：菜单入口、行高不变式、关于对话框、更新设置对话框。

背景（2026-10-03 定案）：**菜单放在「AI 工具」那一行的右侧**，不用系统菜单栏。
理由只有一个但很硬——那一行右侧本来就空着，加两个按钮**不改变行高**，
于是 ``_fit_layout()`` 的高度基准不用重算；``root.config(menu=…)`` 会独占一行、
必须重新校准基准（macOS 上还会被收进系统应用菜单，三平台表现不一致）。

因此本文件里**两条最关键的断言**是：

1. ★ ``tool_row`` 的**请求高度不因加菜单而变化**（等于同行下拉框的请求高）；
2. ★ 源码里**没有** ``config(menu=…)``（防止有人改回系统菜单栏，把定案推翻）。

其余覆盖：菜单项结构、``F1`` 绑定、关于对话框（版本来自 ``about_lines()``、
二维码缺图整块隐藏、有图时**持引用**）、更新设置对话框（中文标签 → 持久化键）。

★ GUI 测试共享一个窗口实例（``setUpClass``）：每个用例各建一次真实 Tk 窗口会明显变慢。
"""
import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import version as _version
from ai_env_clone.__main__ import AboutDialog, QoderBackupApp, UpdateSettingsDialog


def _read_main_src() -> str:
    with open(os.path.join(ROOT, "ai_env_clone", "__main__.py"), "r", encoding="utf-8") as f:
        return f.read()


def _config_menu_calls(src: str) -> list:
    """源码里 ``xxx.config(menu=…)`` / ``xxx.configure(menu=…)`` 调用的行号。

    ★ 必须走 **AST** 而不是字符串搜索：源码里恰好有两处**注释/文档**写着
    「不用 ``root.config(menu=…)``」来说明这条定案，字符串搜索会把它们当成违规。
    （``mb["menu"] = menu`` 这种**给 Menubutton 挂下拉**的写法是合法的，
    不在本函数判定范围内——它用的是下标赋值，不是 ``config(menu=)``。）
    """
    hits = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("config", "configure")
                and any(kw.arg == "menu" for kw in node.keywords)):
            hits.append(node.lineno)
    return hits


class _AppFixture(unittest.TestCase):
    """共享一个「未映射」的窗口实例：不扫真实数据目录、不写偏好缓存。"""

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


class TestMenuPlacement(_AppFixture):
    """菜单入口的位置与「零新增行高」不变式。"""

    def test_menu_buttons_are_in_tool_row_and_packed(self) -> None:
        """两个菜单按钮必须与工具下拉**同一行**、且都是 pack 进去的。"""
        row = self.app.tool_row
        kids = [c for c in row.winfo_children()
                if c.winfo_class() == "TMenubutton"]
        self.assertEqual(len(kids), 2, "「AI 工具」行右侧应有两个菜单按钮")
        for btn in kids:
            self.assertEqual(str(btn.winfo_parent()), str(row))
            self.assertEqual(btn.winfo_manager(), "pack",
                             "该行只用 pack；改 grid 会让宽度基准不一致")

    def test_pack_order_makes_settings_left_of_help(self) -> None:
        """★ ``side=RIGHT`` 是「先 pack 的贴最右」⇒ 视觉顺序是「设置 ｜ 帮助」。

        打包顺序必须是「先帮助、后设置」；写反了这两个按钮会调个个儿。
        """
        row = self.app.tool_row
        buttons = [c for c in row.winfo_children()
                   if c.winfo_class() == "TMenubutton"]
        labels = [b["text"] for b in buttons]
        self.assertEqual(labels, ["帮助", "设置"],
                         "pack 顺序应为「帮助 → 设置」")

    def test_row_height_is_not_inflated_by_menus(self) -> None:
        """★ 核心不变式：加了菜单，这一行的请求高度仍等于同行的下拉框。

        ttk ``Menubutton`` 默认请求高 25px，比 ttk ``Combobox`` 的 23px 高 2px
        ⇒ 不加 ``padding`` 就会把行高顶到 25，「零新增高度」当场失效。
        实现里用 ``padding=(4, 3)`` 实测对齐到 23px；本用例把这条钉住。
        """
        self.assertEqual(
            self.app.tool_row.winfo_reqheight(),
            self.app.tool_combo.winfo_reqheight(),
            "菜单把「AI 工具」这一行撑高了 ⇒ 会连带动到高度基准",
        )

    def test_no_system_menubar_is_configured(self) -> None:
        """★ 源码层面禁止 ``config(menu=…)``：那是「独占一行」的老方案。"""
        hits = _config_menu_calls(_read_main_src())
        self.assertEqual(
            hits, [],
            "第 %s 行把菜单挂成了系统菜单栏：它会独占一行、需要重算高度基准" % hits,
        )

    def test_menu_items(self) -> None:
        menus = {}
        for btn in self.app._menu_buttons:
            menu = self.root.nametowidget(btn["menu"])
            entries = []
            for i in range(menu.index("end") + 1):
                entries.append("---" if menu.type(i) == "separator"
                               else menu.entrycget(i, "label"))
            menus[btn["text"]] = entries
        self.assertEqual(menus["帮助"], ["使用说明", "---", "关于"])
        self.assertEqual(menus["设置"], ["更新设置…"])

    def test_update_action_has_no_accelerator(self) -> None:
        """更新动作**不绑热键**：避免误触替换程序（只有使用说明挂 F1）。"""
        help_menu = self.root.nametowidget(self.app._menu_buttons[0]["menu"])
        self.assertEqual(help_menu.entrycget(0, "accelerator"), "F1")
        set_menu = self.root.nametowidget(self.app._menu_buttons[1]["menu"])
        self.assertEqual(set_menu.entrycget(0, "accelerator"), "")

    def test_f1_is_bound(self) -> None:
        """``F1`` 用 root 级绑定实现，不依赖菜单栏的 accelerator 显示。

        ★ 注意 Tk 会把 ``<F1>`` 规范化成 ``<Key-F1>`` 出现在 ``bind()`` 的结果里，
        所以要匹配的是 ``<Key-F1>``（实测：``"<F1>" in root.bind()`` 恒为 False）。
        """
        self.assertIn("<Key-F1>", self.root.bind())


class TestAboutDialog(_AppFixture):
    """「关于」对话框：版本来源、二维码缺图降级、引用持有、只读检查更新。"""

    def test_shows_version_from_about_lines(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            text = dlg.info.get("1.0", "end")
            self.assertIn(_version.__version__, text, "「关于」必须显示当前版本号")
            self.assertIn(_version.LEGAL_COPYRIGHT, text)
            self.assertIn(self.app.adapter.display_name, text)
            self.assertEqual(dlg.latest_var.get(), "最新版本：未检查")
        finally:
            dlg.top.destroy()

    def test_missing_qr_image_hides_block_silently(self) -> None:
        """★ 仓库当前并没有打赏二维码 ⇒ 缺图是**正常路径**，要整块隐藏、不报错。"""
        with mock.patch("ai_env_clone.__main__.resource_path", return_value=None):
            dlg = AboutDialog(self.app)
        try:
            self.assertIsNone(dlg._qr_image)
        finally:
            dlg.top.destroy()

    def test_qr_image_reference_is_kept(self) -> None:
        """★ ``PhotoImage`` 必须持引用，否则被 GC 后二维码显示为**空白**。"""
        with tempfile.TemporaryDirectory() as tmp:
            qr = os.path.join(tmp, "reward_qr.png")
            img = tk.PhotoImage(width=600, height=600)
            img.put("#ff0000", to=(0, 0, 600, 600))
            img.write(qr, format="png")
            with mock.patch("ai_env_clone.__main__.resource_path", return_value=qr):
                dlg = AboutDialog(self.app)
            try:
                self.assertIsNotNone(dlg._qr_image, "有图时必须挂引用，否则 GC 后空白")
                # subsample() 只支持整数倍 ⇒ 600 / ceil(600/240)=3 → 200
                self.assertEqual(dlg._qr_image.width(), 200)
            finally:
                dlg.top.destroy()

    def test_check_update_never_downloads_and_survives_errors(self) -> None:
        """检查更新是**只读**的：只调 ``check_update``；异常要落到界面、不许崩。"""
        dlg = AboutDialog(self.app)
        try:
            with mock.patch("ai_env_clone.__main__.check_update",
                            side_effect=RuntimeError("boom")) as m, \
                 mock.patch("ai_env_clone.__main__.platform_asset_name",
                            return_value="AiEnvClone-windows.exe"):
                dlg._check_worker()
                self.root.update()
            m.assert_called_once()
            self.assertIn("失败", dlg.latest_var.get())
            self.assertTrue(dlg.check_btn.instate(["!disabled"]), "检查后按钮要恢复可用")
        finally:
            dlg.top.destroy()

    def test_check_update_reports_new_version(self) -> None:
        from ai_env_clone import updater

        info = updater.UpdateInfo(version="9.9.9", tag="v9.9.9",
                                  asset_name="AiEnvClone-windows.exe",
                                  url="u", source="github", checksum_verified=True,
                                  notes="修了几个问题")
        dlg = AboutDialog(self.app)
        try:
            with mock.patch("ai_env_clone.__main__.check_update",
                            return_value=updater.UpdateResult(info=info)), \
                 mock.patch("ai_env_clone.__main__.platform_asset_name",
                            return_value="AiEnvClone-windows.exe"), \
                 mock.patch("ai_env_clone.__main__._prefs.record_check") as rec:
                dlg._check_worker()
                self.root.update()
            self.assertIn("9.9.9", dlg.latest_var.get())
            self.assertIn("修了几个问题", dlg.notes.get("1.0", "end"))
            rec.assert_called_once()
        finally:
            dlg.top.destroy()


class TestAboutProjectLinks(_AppFixture):
    """★ 「关于」里的项目地址要**可点、可复制**（用户反馈驱动）。

    原先是个 ``tk.Label``：文字**选不中**，地址只能手抄、更点不开。现在换成只读
    ``tk.Text`` —— 禁用后仍可拖选与 ``Ctrl+C``（Tk 常规行为），URL 挂 ``link``
    标签后单击即用系统浏览器打开。三条能力分别钉住：
    「能选」/「点了会开浏览器」/「右键能复制到剪贴板」。
    """

    GITHUB = "https://github.com/%s/%s" % (_version.PROJECT_OWNER,
                                          _version.PROJECT_REPO)
    GITEE = "https://gitee.com/%s/%s" % (_version.PROJECT_OWNER,
                                         _version.PROJECT_REPO)

    def test_info_block_is_selectable_readonly_text(self) -> None:
        """只读**不等于**不可选：不能选就复制不了，等于没修。"""
        dlg = AboutDialog(self.app)
        try:
            self.assertEqual(dlg.info.winfo_class(), "Text")
            self.assertEqual(dlg.info["state"], "disabled", "信息块必须只读")
            dlg._select_all()
            self.assertTrue(dlg.info.tag_ranges("sel"), "只读也必须能全选")
        finally:
            dlg.top.destroy()

    def test_info_text_is_not_clipped(self) -> None:
        """★ 不折行（``wrap="none"``）⇒ 宽度不足会**裁掉**右边的字。

        ``Text`` 的 ``width`` 单位是「平均字符宽」而非像素，只能实测反推；
        这里用同样的方式复算一遍，两边不一致就说明宽度算错了。
        """
        import tkinter.font as tkfont

        dlg = AboutDialog(self.app)
        try:
            lines = _version.about_lines(self.app.adapter.display_name)
            need = max(tkfont.nametofont("TkDefaultFont").measure(l) for l in lines)
            self.assertGreaterEqual(
                dlg.info.winfo_reqwidth(), need,
                "只读 Text 比最长的一行还窄 ⇒ 地址会被裁掉",
            )
            self.assertEqual(dlg.info["wrap"], "none")
        finally:
            dlg.top.destroy()

    def test_urls_are_tagged_as_links(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            rng = dlg.info.tag_ranges("link")
            self.assertTrue(rng, "项目地址必须挂 link 标签，否则没法点")
            got = [dlg.info.get(rng[i], rng[i + 1])
                   for i in range(0, len(rng), 2)]
            self.assertEqual(got, [self.GITHUB, self.GITEE])
        finally:
            dlg.top.destroy()

    def test_url_at_reads_the_line_under_the_index(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            line = dlg.info.search(self.GITEE, "1.0")
            self.assertTrue(line, "about_lines 里应有 gitee 地址")
            self.assertEqual(dlg._url_at(line), self.GITEE)
            self.assertEqual(dlg._url_at("1.0"), "", "普通行不该「蹭」到地址")
        finally:
            dlg.top.destroy()

    def test_click_opens_browser_with_that_url(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            with mock.patch("ai_env_clone.__main__.webbrowser.open") as op:
                dlg._open_url_at(dlg.info.search(self.GITEE, "1.0"))
            op.assert_called_once_with(self.GITEE)
        finally:
            dlg.top.destroy()

    def test_click_on_plain_line_does_nothing(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            with mock.patch("ai_env_clone.__main__.webbrowser.open") as op:
                dlg._open_url_at("1.0")
            op.assert_not_called()
        finally:
            dlg.top.destroy()

    def test_copy_writes_clipboard_and_flushes_it(self) -> None:
        """★ 写完必须刷一次：Tk 的剪贴板由本进程供服务，不刷就退出会丢内容。"""
        dlg = AboutDialog(self.app)
        try:
            with mock.patch.object(dlg.top, "clipboard_clear") as clr, \
                 mock.patch.object(dlg.top, "clipboard_append") as app_, \
                 mock.patch.object(dlg.top, "update_idletasks") as upd:
                dlg._copy_text(self.GITHUB)
            clr.assert_called_once()
            app_.assert_called_once_with(self.GITHUB)
            upd.assert_called_once()
        finally:
            dlg.top.destroy()

    def _labels(self, menu) -> list:
        return ["---" if menu.type(i) == "separator" else menu.entrycget(i, "label")
                for i in range(menu.index("end") + 1)]

    def test_context_menu_copies_the_url_under_the_cursor(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            line = dlg.info.search(self.GITHUB, "1.0")
            menu = dlg._build_context_menu(line)
            try:
                self.assertIn("复制链接地址", self._labels(menu))
                with mock.patch.object(dlg.top, "clipboard_append") as app_:
                    menu.invoke(0)
                app_.assert_called_once_with(self.GITHUB)
            finally:
                menu.destroy()
        finally:
            dlg.top.destroy()

    def test_context_menu_on_a_plain_line_hides_copy_link(self) -> None:
        dlg = AboutDialog(self.app)
        try:
            menu = dlg._build_context_menu("1.0")
            try:
                self.assertNotIn("复制链接地址", self._labels(menu))
                self.assertIn("全选", self._labels(menu), "全选永远可用")
            finally:
                menu.destroy()
        finally:
            dlg.top.destroy()


class TestUpdateSettingsDialog(_AppFixture):
    """「更新设置」对话框：中文标签到持久化键的翻译、保存走 prefs。"""

    def test_saves_interval_key_not_chinese_label(self) -> None:
        dlg = UpdateSettingsDialog(self.app)
        saved = {}
        try:
            dlg.interval_var.set("每周一次")
            dlg.auto_var.set(True)
            dlg.proxy_var.set(" http://127.0.0.1:7897 ")
            with mock.patch("ai_env_clone.__main__._prefs.save_update_settings",
                            side_effect=lambda **kw: saved.update(kw) or True):
                dlg.save()
        finally:
            try:
                dlg.top.destroy()
            except Exception:
                pass
        self.assertEqual(saved["interval"], "weekly",
                         "落盘的必须是键（weekly），不是中文标签")
        self.assertIs(saved["auto_check"], True)
        self.assertEqual(saved["proxy"], "http://127.0.0.1:7897", "首尾空白要 trim")

    def test_unknown_label_falls_back_to_daily(self) -> None:
        dlg = UpdateSettingsDialog(self.app)
        try:
            dlg.interval_var.set("不存在的频率")
            self.assertEqual(dlg._interval_key(), "daily")
        finally:
            dlg.top.destroy()

    def test_defaults_come_from_prefs(self) -> None:
        from ai_env_clone import prefs as _prefs_mod

        with mock.patch("ai_env_clone.__main__._prefs.update_settings",
                        return_value=_prefs_mod.default_update_settings()):
            dlg = UpdateSettingsDialog(self.app)
        try:
            self.assertEqual(dlg.interval_var.get(), "每天一次")
            self.assertIs(dlg.auto_var.get(), False,
                          "自动检查默认关闭（需代理的环境下静默检查会白等几秒）")
        finally:
            dlg.top.destroy()


if __name__ == "__main__":
    unittest.main()
