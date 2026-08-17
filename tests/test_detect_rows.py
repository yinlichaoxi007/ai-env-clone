"""数据根识别状态区：最大约两行高，超出显示竖向滚动条，避免撑高主窗口。

注：headless 环境下 tkinter 的 winfo_reqheight() 与 winfo_ismapped()
均不可靠（withdraw 窗口不真正映射），故本测试：
- 直接 mock 内容请求高度与 _detect_max_h，验证「内容超过最大高度才显示
  滚动条、否则收起」的分支逻辑（实机下 winfo_reqheight 准确）；
- 用 winfo_manager()（headless 下仍可靠）判断 scrollbar 是否被 pack
  （"pack"=显示，""=已 pack_forget 收起）。
"""
import os
import sys
import tkinter as tk
import unittest

if "tkinter" not in sys.modules:
    import tkinter as tk  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock

from ai_env_clone import __main__ as m
from ai_env_clone.__main__ import QoderBackupApp


def _make_app():
    # 隔离用户偏好缓存：默认工具固定走注册序第一个，且不写真实缓存
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _fake_roots(n):
    return [
        {"rel": "root%d" % i, "exists": True, "note": "" if i % 2 == 0 else "备注"}
        for i in range(n)
    ]


def _packed(sb):
    return sb.winfo_manager() == "pack"


class TestDetectRowsScroll(unittest.TestCase):
    def _run(self, app, n_roots, content_h, max_h, ok=True):
        with mock.patch.object(app.adapter, "detect_data_roots",
                               return_value=_fake_roots(n_roots)):
            with mock.patch.object(app.detect_rows_frame, "winfo_reqheight",
                                   return_value=content_h):
                with mock.patch.object(app, "_detect_max_h", max_h):
                    with mock.patch.object(app.detect_rows_canvas, "update_idletasks",
                                           return_value=None):
                        app._refresh_detect_status(ok)

    def test_scrollbar_hidden_when_content_short(self):
        """内容高度 ≤ 最大高度（两行）时不显示竖向滚动条。"""
        root, app = _make_app()
        try:
            self._run(app, n_roots=1, content_h=20, max_h=40)
            self.assertFalse(_packed(app.detect_rows_sb),
                             "内容未超过两行时应隐藏竖向滚动条")
        finally:
            app._cancel_after()
            root.destroy()

    def test_scrollbar_shown_when_content_tall(self):
        """内容高度 > 最大高度（超过两行）时显示竖向滚动条。"""
        root, app = _make_app()
        try:
            self._run(app, n_roots=6, content_h=120, max_h=40)
            self.assertTrue(_packed(app.detect_rows_sb),
                            "内容超过两行时应显示竖向滚动条")
        finally:
            app._cancel_after()
            root.destroy()

    def test_scrollbar_hidden_after_error_reset(self):
        """识别失败（ok=False）时滚动条收起，不残留。"""
        root, app = _make_app()
        try:
            self._run(app, n_roots=6, content_h=120, max_h=40, ok=True)
            self.assertTrue(_packed(app.detect_rows_sb))
            self._run(app, n_roots=0, content_h=0, max_h=40, ok=False)
            self.assertFalse(_packed(app.detect_rows_sb),
                             "识别失败后竖向滚动条应收起，避免残留")
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
