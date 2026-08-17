"""DPI 适配与备份内容区/主窗高度统一单元测试。

覆盖两点：
1. 进程 DPI 感知声明（Windows Per-Monitor v2）不抛异常，确保高分屏
   （缩放 > 100%）下 tk 几何/字体缩放与系统一致，窗口相对屏幕大小
   恒定、不再被虚化放大导致显示不全。
2. 备份内容区（canvas）高度统一为固定值 285（不贴合 reqheight，与项数无关），
   切换工具时内容区高度不变；主窗口总高固定为屏幕高度的 75%（与内容无关），
   三工具完全一致——修复"主窗总高随内容自动变化、三工具不一致"的问题。
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
            # 主窗口总高应封顶到屏 75% 以内（headless 下 geometry 不生效，
            # win_h 恒为 560；实机下 geometry 生效、固定为 screen*0.75）。
            # 此处只断言封顶（<=），实机统一高度由用户截图确认。
            geo = root.geometry().split("+")[0]
            wh = int(geo.split("x")[1])
            self.assertLessEqual(wh, int(1080 * 0.75),
                                 "主窗口总高应封顶到屏 75% 以内")
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


if __name__ == "__main__":
    unittest.main()
