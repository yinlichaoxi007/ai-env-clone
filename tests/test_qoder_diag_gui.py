"""Qoder「历史会话诊断」行：可见性随工具切换 + 检测链路端到端（headless）。

headless 说明：``winfo_ismapped()`` 在 withdraw 窗口下不可靠，用 ``winfo_manager()``
（"pack"=已 pack，""=已 pack_forget）判断可见性。诊断结果经「后台线程 → 消息队列 →
主线程渲染」全链路；``HEADLESS`` 下自定义报告弹窗退化为被 mock 的 ``messagebox``。
"""

import os
import sys
import time
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import __main__ as gui  # noqa: E402
from ai_env_clone.__main__ import QoderBackupApp  # noqa: E402

_ORIG_HEADLESS = None


def setUpModule() -> None:
    global _ORIG_HEADLESS
    _ORIG_HEADLESS = gui.HEADLESS
    gui.HEADLESS = True


def tearDownModule() -> None:
    if _ORIG_HEADLESS is not None:
        gui.HEADLESS = _ORIG_HEADLESS


def _make_app(tool: str):
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _packed(w) -> bool:
    return w.winfo_manager() == "pack"


def _fake_report(**over):
    base = {
        "electron_root": r"C:\Users\x\AppData\Roaming\com.qodercn.app.stable",
        "electron_exists": True,
        "new_sessions": 0,
        "new_messages": 0,
        "import_jobs": 0,
        "import_history": 0,
        "legacy_db": r"C:\Users\x\.qoder-cn\shared_client\cache\db\local.db",
        "legacy_sessions": 12,
        "legacy_messages": 340,
        "cn_migration_done": True,
        "findings": ["新版会话库 main.sqlite 的 chat_sessions 表为空 —— 历史不可见"],
    }
    base.update(over)
    return base


class TestQoderDiagRow(unittest.TestCase):
    def test_hidden_for_non_qoder(self):
        """非 qoder 工具不显示该行（不破坏其他工具布局）。"""
        for tool in ("codebuddy", "dsh", "workbuddy", "trae-cn", "zcode"):
            with self.subTest(tool=tool):
                root, app = _make_app(tool)
                try:
                    self.assertFalse(_packed(app.qoder_diag_frame))
                finally:
                    root.destroy()

    def test_shown_for_qoder(self):
        root, app = _make_app("qoder")
        try:
            self.assertTrue(_packed(app.qoder_diag_frame))
            self.assertEqual(
                str(app.qoder_diag_btn.cget("state")), "normal"
            )
        finally:
            root.destroy()

    def test_switch_tool_toggles_row(self):
        """切换到 qoder 显示、切走隐藏（幂等）。"""
        root, app = _make_app("codebuddy")
        try:
            self.assertFalse(_packed(app.qoder_diag_frame))
            with mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
                disp = next(d for d, n in app._tool_display.items() if n == "qoder")
                app.tool_var.set(disp)
                app._on_switch_tool()
            self.assertTrue(_packed(app.qoder_diag_frame))
            with mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
                disp = next(d for d, n in app._tool_display.items() if n == "dsh")
                app.tool_var.set(disp)
                app._on_switch_tool()
            self.assertFalse(_packed(app.qoder_diag_frame))
        finally:
            root.destroy()

    def test_report_renders_facts_and_findings(self):
        """报告正文包含两处数据根、会话/账本计数与结论列表。"""
        root, app = _make_app("qoder")
        try:
            with mock.patch.object(
                QoderBackupApp, "_show_plain_report_dialog"
            ) as dlg:
                app._render_qoder_report(_fake_report())
            dlg.assert_called_once()
            # 绑定方法：call_args 去掉 self 后为 (title, text)
            title, text = dlg.call_args[0][0], dlg.call_args[0][1]
            self.assertIn("新版会话库为空", title)
            for token in ("com.qodercn.app.stable", "local.db", "会话 0 个",
                          "import_job 0", "会话 12 个",
                          "历史不可见", "列级密文"):
                self.assertIn(token, text)
        finally:
            root.destroy()

    def test_report_handles_unreadable_counts(self):
        """计数读取失败（None）显示为「读取失败」而非 None。"""
        root, app = _make_app("qoder")
        try:
            with mock.patch.object(
                QoderBackupApp, "_show_plain_report_dialog"
            ) as dlg:
                app._render_qoder_report(
                    _fake_report(new_sessions=None, import_jobs=None)
                )
            text = dlg.call_args[0][1]
            self.assertNotIn("None", text)
            self.assertIn("读取失败", text)
        finally:
            root.destroy()

    def test_check_button_end_to_end(self):
        """点「检测历史会话」→ 后台线程 → 队列 → 主线程渲染全链路。"""
        root, app = _make_app("qoder")
        try:
            with mock.patch(
                "ai_env_clone.adapters.qoder.diagnose_history",
                return_value=_fake_report(),
            ), mock.patch("ai_env_clone.__main__.messagebox.showinfo") as showinfo:
                app._qoder_diag()
                deadline = time.time() + 5
                while time.time() < deadline:
                    app._drain_queue()
                    if showinfo.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(showinfo.called)
                body = showinfo.call_args[0][1]
                self.assertIn("历史不可见", body)
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
