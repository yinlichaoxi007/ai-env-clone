"""用户偏好缓存（记住上次选择的工具）与默认工具选择逻辑测试。

覆盖：
- ``list_adapters()`` 按注册顺序返回（第一个即默认工具，Qoder 在前）；
- ``_save_last_tool`` / ``_load_last_tool`` 缓存读写与失效回退（无缓存 /
  损坏 / 工具已注销 -> None，GUI 回退注册序第一个）；
- GUI 默认工具：有缓存 -> 沿用缓存；无缓存 -> 注册序第一个；
- GUI 切换工具后写入缓存（下次启动保持用户上次的选择）。

缓存读写均隔离到临时目录（mock ``compress_estimate.cache_dir``），
不触碰真实用户缓存。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone import __main__ as gui
from ai_env_clone import compress_estimate


def _mock_cache_dir(tmp: str):
    """把偏好缓存目录重定向到 tmp，返回 cache_dir 上下文管理器。"""
    return mock.patch.object(compress_estimate, "cache_dir", return_value=tmp)


class TestListAdaptersOrder(unittest.TestCase):
    def test_registration_order_first_is_qoder(self) -> None:
        """list_adapters() 按注册顺序返回，第一个为默认工具（Qoder 在前）。"""
        names = list_adapters()
        self.assertEqual(names[0], "qoder", "注册序第一个应为 qoder，实际：%s" % names)

    def test_all_registered(self) -> None:
        names = list_adapters()
        for n in ("qoder", "codebuddy", "reasonix", "dsh"):
            self.assertIn(n, names)


class TestLastToolCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="lasttool_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_no_cache_returns_none(self) -> None:
        with _mock_cache_dir(self.tmp):
            self.assertIsNone(gui._load_last_tool())

    def test_save_then_load_roundtrip(self) -> None:
        with _mock_cache_dir(self.tmp):
            gui._save_last_tool("dsh")
            self.assertEqual(gui._load_last_tool(), "dsh")
            # 文件落在缓存目录、内容正确
            with open(gui._prefs_path(), "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"last_tool": "dsh"})

    def test_overwrite_latest_choice(self) -> None:
        with _mock_cache_dir(self.tmp):
            gui._save_last_tool("qoder")
            gui._save_last_tool("reasonix")
            self.assertEqual(gui._load_last_tool(), "reasonix")

    def test_corrupt_cache_returns_none(self) -> None:
        with _mock_cache_dir(self.tmp):
            os.makedirs(self.tmp, exist_ok=True)
            with open(gui._prefs_path(), "w", encoding="utf-8") as f:
                f.write("{not valid json")
            self.assertIsNone(gui._load_last_tool())

    def test_unknown_tool_returns_none(self) -> None:
        with _mock_cache_dir(self.tmp):
            gui._save_last_tool("not_a_real_tool")
            self.assertIsNone(gui._load_last_tool())

    def test_save_failure_is_silent(self) -> None:
        """缓存写失败不应抛异常（不影响主流程）。"""
        with mock.patch("builtins.open", side_effect=OSError("模拟写失败")):
            gui._save_last_tool("qoder")  # 不应抛


class TestGuiDefaultTool(unittest.TestCase):
    def _make_app(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        app = gui.QoderBackupApp(root)
        return root, app

    def test_default_tool_first_registered_when_no_cache(self) -> None:
        with mock.patch.object(gui, "_load_last_tool", return_value=None), \
             mock.patch.object(gui, "_save_last_tool", return_value=None):
            root, app = self._make_app()
            try:
                self.assertEqual(app.adapter.name, list_adapters()[0])
            finally:
                app._cancel_after()
                root.destroy()

    def test_default_tool_keeps_last_choice(self) -> None:
        """有缓存时默认工具沿用用户上次选择（如 dsh）。"""
        with mock.patch.object(gui, "_load_last_tool", return_value="dsh"), \
             mock.patch.object(gui, "_save_last_tool", return_value=None):
            root, app = self._make_app()
            try:
                self.assertEqual(app.adapter.name, "dsh")
            finally:
                app._cancel_after()
                root.destroy()

    def test_switch_tool_saves_choice(self) -> None:
        """切换工具后写入缓存，下次启动保持用户上次的选择。"""
        with mock.patch.object(gui, "_load_last_tool", return_value=None), \
             mock.patch.object(gui, "_save_last_tool", return_value=None) as save:
            root, app = self._make_app()
            try:
                # 模拟用户在 GUI 下拉选择 Reasonix
                app.tool_var.set(get_adapter("reasonix").display_name)
                app._on_switch_tool()
                save.assert_called_once_with("reasonix")
                self.assertEqual(app.adapter.name, "reasonix")
            finally:
                app._cancel_after()
                root.destroy()


if __name__ == "__main__":
    unittest.main()