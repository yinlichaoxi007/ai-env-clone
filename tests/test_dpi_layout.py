"""DPI 适配与备份内容区/主窗高度单元测试。

覆盖两点：
1. 进程 DPI 感知声明（Windows Per-Monitor v2）不抛异常，确保高分屏
   （缩放 > 100%）下 tk 几何/字体缩放与系统一致，窗口相对屏幕大小
   恒定、不再被虚化放大导致显示不全。
2. 备份内容区（canvas）在**量不到窗口可用高度**时（headless / 未映射）
   退回自然高度 285，不随内容 reqheight 变化——这条是「三工具观感一致」
   的兜底契约。真实窗口下的等比缩放与按需滚动条行为另见
   ``tests/test_window_fit.py``。
3. 首次打开的默认窗口高度 = 屏幕高 × 0.75（``TestDefaultWindowHeight``），
   与内容多少、工具类型都无关，且按比例取值故任意 DPI 缩放下占屏恒定。
"""
import os
import sys
import unittest

if "tkinter" not in sys.modules:
    import tkinter as tk  # noqa: F401  (ensure available for headless tests)

# 在导入被测模块前把项目根加入 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock

from ai_env_clone import __main__ as m


class TestDpiAwareness(unittest.TestCase):
    def test_enable_dpi_awareness_no_raise(self):
        # 不应抛异常；非 Windows 直接跳过内部逻辑
        try:
            m._enable_dpi_awareness()
        except Exception as e:  # pragma: no cover
            self.fail("`_enable_dpi_awareness` raised: %r" % e)

    def test_enable_dpi_awareness_win32_calls_api(self):
        if sys.platform != "win32":
            self.skipTest("Windows-only DPI API")
        with mock.patch("ctypes.windll.shcore.SetProcessDpiAwareness",
                        return_value=0) as spda:
            m._enable_dpi_awareness()
            spda.assert_called_once_with(2)


class TestFitLayoutCaps(unittest.TestCase):
    def _make_app(self):
        import tkinter as tk
        from ai_env_clone.__main__ import QoderBackupApp
        # 隔离用户偏好缓存：默认工具固定走注册序第一个，且不写真实缓存
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
            root = tk.Tk()
            root.withdraw()
            app = QoderBackupApp(root)
        return root, app

    def test_canvas_height_uniform_285_when_content_huge(self):
        """三工具统一：内容过多时 canvas 仍为固定 285（不贴合 reqheight）。"""
        root, app = self._make_app()
        try:
            with mock.patch.object(app.list_frame, "winfo_reqheight",
                                   return_value=99999):
                with mock.patch.object(root, "winfo_screenheight",
                                       return_value=1080):
                    with mock.patch.object(root, "winfo_reqheight",
                                           return_value=5000):
                        with mock.patch.object(root, "winfo_width",
                                               return_value=738):
                            app._fit_layout()
            canvas_h = int(app._canvas.cget("height"))
            self.assertEqual(canvas_h, 285,
                             "canvas 高度应统一为固定值 285，与项数/请求高度无关")
            # 主窗口默认高度（屏幕比例取值 / 三工具一致 / DPI 无关）见
            # TestDefaultWindowHeight——那里用注入的屏幕高做精确断言，
            # 不依赖运行测试这台机器的真实分辨率。
        finally:
            app._cancel_after()
            root.destroy()

    def test_canvas_height_uniform_285_when_content_small(self):
        """内容少时 canvas 仍统一为 285（不贴合 reqheight），三工具完全一致。"""
        root, app = self._make_app()
        try:
            with mock.patch.object(app.list_frame, "winfo_reqheight",
                                   return_value=150):
                with mock.patch.object(root, "winfo_screenheight",
                                       return_value=1080):
                    with mock.patch.object(root, "winfo_reqheight",
                                           return_value=560):
                        with mock.patch.object(root, "winfo_width",
                                               return_value=738):
                            app._fit_layout()
            canvas_h = int(app._canvas.cget("height"))
            self.assertEqual(canvas_h, 285,
                             "canvas 高度应统一为 285，不因 reqheight 小而缩短")
        finally:
            app._cancel_after()
            root.destroy()

    def test_canvas_initial_height_fixed_285(self):
        """canvas 在 _build_main 创建时即固定 285，避免被 mid expand 撑成内容高。

        根因：此前 canvas 初始无 height，pack(fill=BOTH, expand) 在 _fit_layout
        设高前把它撑成 list_frame 请求高度（CodeBuddy 项多可达 500+），导致
        mid/主窗随内容变高、三工具主窗不一致。此处从源头验证固定生效。
        """
        root, app = self._make_app()
        try:
            self.assertEqual(int(app._canvas.cget("height")), 285,
                             "canvas 初始高度必须固定为 285")
        finally:
            app._cancel_after()
            root.destroy()


class TestDefaultWindowHeight(unittest.TestCase):
    """首次打开的默认窗口高度 = 屏幕高的固定比例（三工具一致、与 DPI 无关）。

    历史教训：曾把默认高度做成「贴合内容自然高」，结果内容少的工具（如 Qoder）
    默认窗口过矮、观感与其它工具不一致。现契约：默认高度只取决于屏幕高度，
    与内容多少、工具类型都无关；内容装不下时由内容区自己的滚动条承载
    （滚动与等比缩放行为见 ``tests/test_window_fit.py``）。
    """

    def _make_app(self):
        import tkinter as tk
        from ai_env_clone.__main__ import QoderBackupApp

        # 隔离真实数据目录：本用例只关心窗口几何，不需要扫描本机产品数据。
        fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None), \
             mock.patch.object(QoderBackupApp, "_detect_root",
                               return_value=fake_root):
            root = tk.Tk()
            root.withdraw()
            app = QoderBackupApp(root)
        return root, app

    def _autosize_with(self, app, root, screen, natural_h=None):
        """以注入的屏幕高（可再注入内容自然高）跑一次 autosize，返回设入的高度。"""
        captured = []
        with mock.patch.object(root, "winfo_screenheight", return_value=screen), \
             mock.patch.object(root, "geometry",
                               side_effect=lambda *a: captured.append(a[0])):
            if natural_h is None:
                app._autosize_window()
            else:
                # 刻意注入内容自然高：断言默认高度**不受**它影响
                with mock.patch.object(root, "winfo_reqheight",
                                       return_value=natural_h):
                    app._autosize_window()
        self.assertTrue(captured, "autosize 应至少设置一次 geometry")
        return int(captured[-1].split("x")[1])

    def test_default_height_is_fraction_of_screen(self):
        """默认高 = 屏幕高 × _WINDOW_H_DEFAULT_RATIO（比例恒定 → 任意 DPI 一致）。"""
        root, app = self._make_app()
        try:
            for screen in (768, 1080, 1440, 2160):
                got = self._autosize_with(app, root, screen)
                self.assertEqual(
                    got, int(screen * m._WINDOW_H_DEFAULT_RATIO),
                    "屏幕 %dpx 时默认高度应为屏高的固定比例" % screen,
                )
        finally:
            app._cancel_after()
            root.destroy()

    def test_default_height_independent_of_content(self):
        """与内容多少无关：内容自然高从 320 到 5000 得到同一默认高度。"""
        root, app = self._make_app()
        try:
            heights = {
                self._autosize_with(app, root, 1080, natural_h=nat)
                for nat in (320, 700, 5000)
            }
            self.assertEqual(
                len(heights), 1,
                "默认高度不应随内容自然高变化（三工具一致）：%r" % sorted(heights),
            )
        finally:
            app._cancel_after()
            root.destroy()

    def test_default_height_floored_on_tiny_screen(self):
        """屏幕极矮时退到下限，保证操作区仍可用。"""
        root, app = self._make_app()
        try:
            got = self._autosize_with(app, root, 500)  # 500*0.75=375 < _WINDOW_H_MIN
            self.assertEqual(got, m._WINDOW_H_MIN)
        finally:
            app._cancel_after()
            root.destroy()

    def test_default_height_never_exceeds_hard_cap(self):
        """任何屏幕高下都不越过硬上限（防被任务栏 / 屏幕边缘遮挡）。"""
        root, app = self._make_app()
        try:
            for screen in (600, 1080, 4320):
                got = self._autosize_with(app, root, screen)
                self.assertLessEqual(got, int(screen * m._WINDOW_H_SCREEN_RATIO))
        finally:
            app._cancel_after()
            root.destroy()

    def test_default_height_identical_across_tools(self):
        """各工具默认高度完全一致：逐个切换工具，同一屏幕高下取值相同。"""
        root, app = self._make_app()
        try:
            fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
            heights = {}
            with mock.patch.object(type(app), "_detect_root",
                                   return_value=fake_root), \
                 mock.patch("ai_env_clone.__main__._save_last_tool",
                            return_value=None):
                for disp in list(app._tool_display.keys()):
                    app.tool_var.set(disp)
                    app._on_switch_tool()
                    heights[disp] = self._autosize_with(app, root, 1080)
            self.assertGreaterEqual(len(heights), 2, "至少应覆盖两个工具")
            self.assertEqual(
                len(set(heights.values())), 1,
                "各工具默认高度必须完全一致：%r" % heights,
            )
        finally:
            app._cancel_after()
            root.destroy()

    def test_autosize_only_on_first_build(self):
        """常规重排（autosize=False）不改窗口尺寸——用户手调过的尺寸不被弹回。"""
        root, app = self._make_app()
        try:
            captured = []
            with mock.patch.object(root, "geometry",
                                   side_effect=lambda *a: captured.append(a[0])):
                app._fit_layout()
                app._fit_layout(autosize=False)
            self.assertEqual(captured, [], "非首次构建的重排不得改写 geometry")
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
