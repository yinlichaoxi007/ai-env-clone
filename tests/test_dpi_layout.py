"""DPI 适配与备份内容区高度封顶单元测试。

覆盖两点：
1. 进程 DPI 感知声明（Windows Per-Monitor v2）不抛异常，确保高分屏
   （缩放 > 100%）下 tk 几何/字体缩放与系统一致，窗口相对屏幕大小
   恒定、不再被虚化放大导致显示不全。
2. 备份内容区（canvas）最大高度封顶到 300（与 Qoder 适配器一致），
   内容过多时主窗口高度受控、不超出屏幕可用高度的 90%。
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
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
        return root, app

    def test_canvas_height_capped_at_300_when_content_huge(self):
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
            self.assertLessEqual(canvas_h, 300,
                                 "内容区高度应封顶 300，避免主窗口过高")
            # 窗口高度不超过屏幕可用高度 90%
            geo = root.geometry().split("+")[0]
            wh = int(geo.split("x")[1])
            self.assertLessEqual(wh, int(1080 * 0.9))
        finally:
            app._cancel_after()
            root.destroy()

    def test_canvas_shrinks_to_content_when_small(self):
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
            # 150 在 [120, 300] 区间内，贴合内容
            self.assertEqual(canvas_h, 150)
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
