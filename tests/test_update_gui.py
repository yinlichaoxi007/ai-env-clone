"""更新 GUI 接线的测试：启动自动检查的门控、状态栏徽标、关于对话框的下载行。

headless 说明：模块期间打开 ``HEADLESS``（对话框内如遇 ``wait_window`` 型弹窗
退化为被 mock 的 messagebox）；网络一律打桩，绝不联网。
"""

import os
import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import compress_estimate, updater  # noqa: E402
from ai_env_clone import __main__ as gui  # noqa: E402
from ai_env_clone.__main__ import AboutDialog, QoderBackupApp  # noqa: E402

_ORIG_HEADLESS = None


def setUpModule() -> None:
    global _ORIG_HEADLESS
    _ORIG_HEADLESS = gui.HEADLESS
    gui.HEADLESS = True


def tearDownModule() -> None:
    if _ORIG_HEADLESS is not None:
        gui.HEADLESS = _ORIG_HEADLESS


def _make_app(tool: str = "qoder"):
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _info(version="9.9.9", tag="v9.9.9"):
    return updater.UpdateInfo(version=version, tag=tag,
                              asset_name=updater.ASSET_NAMES["windows"],
                              url="https://host/x", size=0, sha256="",
                              source="github")


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="updgui_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.object(compress_estimate, "cache_dir",
                                    return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestUpdateBadge(_TmpCase):
    def test_badge_hidden_until_update_found(self) -> None:
        """默认隐藏；自动检查真发现可装更新才亮（已是最新 / 失败都不打扰）。"""
        root, app = _make_app()
        try:
            self.assertEqual(app.update_badge.winfo_manager(), "")
            app._apply_auto_check(None)                    # 检查异常：静默
            self.assertEqual(app.update_badge.winfo_manager(), "")
            result = mock.Mock(error="", up_to_date=True, asset_missing=False,
                               info=None)
            app._apply_auto_check(result)                  # 已是最新：静默
            self.assertEqual(app.update_badge.winfo_manager(), "")
            result = mock.Mock(error="", up_to_date=False, asset_missing=False,
                               info=_info())
            with mock.patch.object(gui._prefs, "record_check"):
                app._apply_auto_check(result)
            self.assertEqual(app.update_badge.winfo_manager(), "grid")
            self.assertIn("v9.9.9", app.update_badge.cget("text"))
        finally:
            root.destroy()

    def test_maybe_auto_check_gated_by_settings(self) -> None:
        """开关关着（默认）就不该排检查任务；HEADLESS 由模块级 setUpModule 覆盖。"""
        root, app = _make_app()
        try:
            with mock.patch.object(gui._prefs, "update_settings",
                                   return_value={"auto_check": False,
                                                 "interval": "daily",
                                                 "last_check": ""}), \
                   mock.patch.object(app, "_auto_check_update") as auto:
                app._maybe_auto_check_update()
            auto.assert_not_called()
        finally:
            root.destroy()


class TestAboutActionRow(_TmpCase):
    def _dialog(self, root, app):
        dialog = AboutDialog(app)
        self.addCleanup(root.destroy)
        return dialog

    def _packed(self, widget) -> bool:
        return widget.winfo_manager() == "pack"

    def test_action_row_appears_only_with_update(self) -> None:
        root, app = _make_app()
        dialog = self._dialog(root, app)
        dialog._apply_check("已是最新版本", None, "", failed=False)
        self.assertFalse(self._packed(dialog.dl_row))
        dialog._apply_check("发现新版本 v9.9.9", _info(), "", failed=False)
        self.assertTrue(self._packed(dialog.dl_row))
        self.assertIn("下载", dialog.dl_btn.cget("text"))

    def test_skip_version_records_and_hides_row(self) -> None:
        root, app = _make_app()
        dialog = self._dialog(root, app)
        dialog._apply_check("发现新版本 v9.9.9", _info(), "", failed=False)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(gui._prefs, "save_update_settings") as save:
            dialog._skip_version()
        save.assert_called_once_with(skipped_version="v9.9.9")
        self.assertFalse(self._packed(dialog.dl_row))
        self.assertIn("已跳过", dialog.latest_var.get())

    def test_download_success_non_install_platform_shows_path(self) -> None:
        """源码模式（本测试环境）不可安装：下载成功只展示路径，不触发替换。"""
        root, app = _make_app()
        dialog = self._dialog(root, app)
        dialog._apply_check("发现新版本 v9.9.9", _info(), "", failed=False)
        self.assertFalse(dialog._can_install())
        path = os.path.join(self.tmp, updater.ASSET_NAMES["windows"])
        with open(path, "wb") as f:
            f.write(b"MZ")
        dialog._dl_downloaded(path)
        self.assertIn(path, dialog.dl_label.cget("text"))
        self.assertFalse(self._packed(dialog.dl_pbar))

    def test_install_declined_keeps_downloaded_state(self) -> None:
        """用户不立即安装 ⇒ 不替换，按钮转「立即安装」，文件路径保留。"""
        root, app = _make_app()
        dialog = self._dialog(root, app)
        dialog._apply_check("发现新版本 v9.9.9", _info(), "", failed=False)
        path = os.path.join(self.tmp, updater.ASSET_NAMES["windows"])
        with open(path, "wb") as f:
            f.write(b"MZ")
        dialog._downloaded_path = path
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            dialog._install(path)
        self.assertIn("立即安装", dialog.dl_btn.cget("text"))
        self.assertIsNone(dialog._dl_cancel)               # 没有下载在进行

    def test_install_error_not_writable_offers_manual_message(self) -> None:
        """替换失败且不可提权（源码模式）⇒ 错误弹窗，原文件不动。"""
        root, app = _make_app()
        dialog = self._dialog(root, app)
        dialog._apply_check("发现新版本 v9.9.9", _info(), "", failed=False)
        path = os.path.join(self.tmp, "no-such-file.exe")
        shown: list = []
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(gui.messagebox, "showerror",
                                  side_effect=lambda *a, **k: shown.append(a)):
            dialog._install(path)
        self.assertTrue(shown)
        self.assertIn("下载文件不存在", shown[0][1])


if __name__ == "__main__":
    unittest.main()
