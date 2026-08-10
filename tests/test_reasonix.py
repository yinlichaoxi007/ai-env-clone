"""Reasonix 适配器单元测试。

覆盖：注册、跨平台路径探测、build_items 路径均在公共根 ~ 之下、推荐默认、
detect_data_roots 展示。路径布局来自用户本机 Windows 实测 + Reasonix 官方
CONFIG_PATHS 文档三平台约定（见 adapters/reasonix.py 模块 docstring），不臆测。
"""

import os
import sys
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
import ai_env_clone.adapters.reasonix as rx_mod


class TestRegistration(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("reasonix", list_adapters())
        self.assertIsInstance(get_adapter("reasonix"), rx_mod.ReasonixAdapter)

    def test_adapter_interface(self) -> None:
        a = get_adapter("reasonix")
        self.assertTrue(hasattr(a, "detect_root"))
        self.assertTrue(hasattr(a, "build_default_root"))
        self.assertTrue(hasattr(a, "detect_data_roots"))
        self.assertTrue(hasattr(a, "build_items"))


class TestCrossPlatform(unittest.TestCase):
    def _env(self, platform: str) -> dict:
        home = os.path.expanduser("~")
        if platform == "win32":
            return {
                "APPDATA": os.path.join(home, "AppData", "Roaming"),
                "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
            }
        return {}

    def test_windows_roots(self) -> None:
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(os.environ, self._env("win32"), clear=False):
            self.assertEqual(
                rx_mod._roaming_root(),
                os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "reasonix"),
            )
            self.assertEqual(
                rx_mod._local_root(),
                os.path.join(os.path.expanduser("~"), "AppData", "Local", "reasonix"),
            )

    def test_darwin_roots(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            home = os.path.expanduser("~")
            self.assertEqual(
                rx_mod._roaming_root(),
                os.path.join(home, "Library", "Application Support", "reasonix"),
            )
            self.assertEqual(
                rx_mod._local_root(),
                os.path.join(home, "Library", "Caches", "reasonix"),
            )

    def test_linux_roots(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            home = os.path.expanduser("~")
            self.assertEqual(rx_mod._roaming_root(), os.path.join(home, ".config", "reasonix"))
            self.assertEqual(rx_mod._local_root(), os.path.join(home, ".cache", "reasonix"))


class TestBuildItems(unittest.TestCase):
    def setUp(self) -> None:
        # 用临时目录模拟 home，保证所有条目 path 都在 root 之下，便于断言。
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="rx_test_")
        self.roam = os.path.join(self.tmp, "Roaming", "reasonix")
        self.local = os.path.join(self.tmp, "Local", "reasonix")
        os.makedirs(os.path.join(self.roam, "memory", "global"), exist_ok=True)
        os.makedirs(os.path.join(self.roam, "projects", "d--proj", "memory"), exist_ok=True)
        os.makedirs(os.path.join(self.roam, "projects", "d--proj", "sessions"), exist_ok=True)
        os.makedirs(os.path.join(self.roam, "plugins"), exist_ok=True)
        with open(os.path.join(self.roam, "config.toml"), "w", encoding="utf-8") as f:
            f.write("")
        os.makedirs(os.path.join(self.local, "updates"), exist_ok=True)

    def _items(self):
        return rx_mod.build_items(self.tmp, self.roam, self.local)

    def test_all_paths_under_root(self) -> None:
        for it in self._items():
            self.assertTrue(
                os.path.abspath(it.path).startswith(os.path.abspath(self.tmp)),
                "条目 %s 的 path 不在公共根之下: %s" % (it.key, it.path),
            )

    def test_keys_present(self) -> None:
        keys = {it.key for it in self._items()}
        self.assertEqual(
            keys,
            {"global_memory", "projects", "global_config", "plugins"},
        )

    def test_recommended_defaults(self) -> None:
        items = {it.key: it for it in self._items()}
        self.assertTrue(items["global_memory"].recommended)
        self.assertTrue(items["projects"].recommended)
        self.assertFalse(items["global_config"].recommended)
        self.assertFalse(items["plugins"].recommended)

    def test_exists_when_present(self) -> None:
        for it in self._items():
            self.assertTrue(it.exists, "条目 %s 应存在: %s" % (it.key, it.path))

    def test_missing_root_still_lists_all(self) -> None:
        # 即使 root 下没有任何 reasonix 目录，也应列出全部 4 项（供 GUI 显示未找到）。
        items = rx_mod.build_items(self.tmp, os.path.join(self.tmp, "nope_r"),
                                   os.path.join(self.tmp, "nope_l"))
        self.assertEqual(len(items), 4)
        self.assertTrue(all(not it.exists for it in items))


class TestDetectDataRoots(unittest.TestCase):
    def _patch_env(self, tmp: str):
        # mock 平台为 Windows 并提供 APPDATA/LOCALAPPDATA 指向 tmp 内，使 _roaming_root()
        # /_local_root() 落在 tmp 之下（与 build_items 真实行为一致）。
        return mock.patch.multiple(
            sys, platform="win32"
        ), mock.patch.dict(
            os.environ,
            {
                "APPDATA": os.path.join(tmp, "Roaming"),
                "LOCALAPPDATA": os.path.join(tmp, "Local"),
            },
            clear=False,
        )

    def test_detect_data_roots_lists_two(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="rx_dr_")
        roam = os.path.join(tmp, "Roaming", "reasonix")
        local = os.path.join(tmp, "Local", "reasonix")
        os.makedirs(roam, exist_ok=True)
        os.makedirs(local, exist_ok=True)
        a = get_adapter("reasonix")
        with self._patch_env(tmp)[0], self._patch_env(tmp)[1]:
            roots = a.detect_data_roots(tmp)
        self.assertEqual(len(roots), 2)
        rels = {r["rel"] for r in roots}
        self.assertIn(os.path.relpath(roam, tmp), rels)
        self.assertIn(os.path.relpath(local, tmp), rels)
        self.assertTrue(all(r["exists"] for r in roots))

    def test_detect_data_roots_missing(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="rx_dr2_")
        a = get_adapter("reasonix")
        with self._patch_env(tmp)[0], self._patch_env(tmp)[1]:
            roots = a.detect_data_roots(tmp)
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(not r["exists"] for r in roots))


if __name__ == "__main__":
    unittest.main()
