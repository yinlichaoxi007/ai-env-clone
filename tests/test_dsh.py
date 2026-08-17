"""DeepSeek Harness（DSH）适配器单元测试。

覆盖：注册、跨平台路径探测、build_items 路径均在公共根 ~ 之下、推荐默认、
detect_data_roots 展示。路径布局来自本机 Windows 实测 + DSH 官方文档约定
（见 adapters/dsh.py 模块 docstring），不臆测。
"""

import os
import sys
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
import ai_env_clone.adapters.dsh as dsh_mod


class TestRegistration(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("dsh", list_adapters())
        self.assertIsInstance(get_adapter("dsh"), dsh_mod.DSHAdapter)

    def test_adapter_interface(self) -> None:
        a = get_adapter("dsh")
        self.assertTrue(hasattr(a, "detect_root"))
        self.assertTrue(hasattr(a, "build_default_root"))
        self.assertTrue(hasattr(a, "detect_data_roots"))
        self.assertTrue(hasattr(a, "build_items"))


class TestDSHHome(unittest.TestCase):
    def test_default_dsh_home(self) -> None:
        """默认 DSH_HOME 应为 ~/.dsh。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_HOME", None)
            self.assertEqual(
                dsh_mod._dsh_home(),
                os.path.join(dsh_mod._home(), ".dsh"),
            )

    def test_dsh_home_env_override(self) -> None:
        """$DSH_HOME 环境变量可覆盖。"""
        test_path = r"D:\custom\dsh_data"
        with mock.patch.dict(os.environ, {"DSH_HOME": test_path}, clear=False):
            self.assertEqual(dsh_mod._dsh_home(), test_path)

    def test_helpers_under_dsh(self) -> None:
        """_dsh_sessions_root、_dsh_storages_root、_dsh_profiles_root 应都在 _dsh_home 之下。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_HOME", None)
            dsh = dsh_mod._dsh_home()
            self.assertTrue(dsh_mod._dsh_sessions_root().startswith(dsh))
            self.assertTrue(dsh_mod._dsh_storages_root().startswith(dsh))
            self.assertTrue(dsh_mod._dsh_profiles_root().startswith(dsh))


class TestBuildItems(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dsh_test_")
        self.dsh_dir = os.path.join(self.tmp, ".dsh")

        # 模拟 DSH 目录结构
        # sessions/
        ws_dir = os.path.join(self.dsh_dir, "sessions", "--D-project-test--")
        session_dir = os.path.join(ws_dir, "session-abc12345-0000-0000-0000-000000000001")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "session.jsonl.zstd"), "wb") as f:
            f.write(b"fake zstd data")

        # storages/
        storages_dir = os.path.join(self.dsh_dir, "storages")
        os.makedirs(storages_dir, exist_ok=True)
        with open(os.path.join(storages_dir, "workspace.json"), "w", encoding="utf-8") as f:
            f.write('{"unit": {"name": "workspace", "version": 2}}')
        with open(os.path.join(storages_dir, "session_projcache.json"), "w", encoding="utf-8") as f:
            f.write('{"unit": {"name": "session_projcache", "version": 3}}')

        # AGENTS.md
        with open(os.path.join(self.dsh_dir, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# 测试用户全局指令")

        # settings.yaml
        with open(os.path.join(self.dsh_dir, "settings.yaml"), "w", encoding="utf-8") as f:
            f.write("locale:\n  preference: zh\n")

        # .credentials.yaml
        with open(os.path.join(self.dsh_dir, ".credentials.yaml"), "w", encoding="utf-8") as f:
            f.write("api_key: test\n")

        # profiles/
        os.makedirs(os.path.join(self.dsh_dir, "profiles", "web"), exist_ok=True)

    def _items(self):
        return dsh_mod.build_items(self.tmp, self.dsh_dir)

    def test_all_paths_under_root(self) -> None:
        for it in self._items():
            self.assertTrue(
                os.path.abspath(it.path).startswith(os.path.abspath(self.tmp)),
                "条目 %s 的 path 不在公共根之下: %s" % (it.key, it.path),
            )

    def test_keys_present(self) -> None:
        keys = {it.key for it in self._items()}
        expected_keys = {
            "sessions",
            "storages:workspace",
            "storages:session_projcache",
            "user_agents",
            "settings",
            "credentials",
            "profiles",
        }
        self.assertEqual(keys, expected_keys)

    def test_recommended_defaults(self) -> None:
        items = {it.key: it for it in self._items()}
        # 核心数据默认勾选
        self.assertTrue(items["sessions"].recommended)
        self.assertTrue(items["storages:workspace"].recommended)
        self.assertTrue(items["storages:session_projcache"].recommended)
        self.assertTrue(items["user_agents"].recommended)
        # 非核心数据默认不勾
        self.assertFalse(items["settings"].recommended)
        self.assertFalse(items["credentials"].recommended)
        self.assertFalse(items["profiles"].recommended)

    def test_exists_when_present(self) -> None:
        for it in self._items():
            self.assertTrue(it.exists, "条目 %s 应存在: %s" % (it.key, it.path))

    def test_missing_root_still_lists_all(self) -> None:
        """即使 DSH 目录不存在，也应列出全部 7 项（供 GUI 显示未找到）。"""
        items = dsh_mod.build_items(self.tmp, os.path.join(self.tmp, "nope_dsh"))
        self.assertEqual(len(items), 7)
        self.assertTrue(all(not it.exists for it in items))

    def test_empty_sessions_dir(self) -> None:
        """sessions 目录为空时仍列出全部条目，仅会话项标记为不存在。"""
        empty_dsh = os.path.join(self.tmp, "empty_dsh")
        os.makedirs(empty_dsh, exist_ok=True)
        items = dsh_mod.build_items(self.tmp, empty_dsh)
        self.assertEqual(len(items), 7)
        sessions_item = next(it for it in items if it.key == "sessions")
        self.assertFalse(sessions_item.exists)


class TestDetectDataRoots(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dsh_dr_")
        self.dsh_dir = os.path.join(self.tmp, ".dsh")

        # 模拟完整 DSH 目录
        os.makedirs(os.path.join(self.dsh_dir, "sessions", "--D-project-test--",
                                  "session-abc"), exist_ok=True)
        os.makedirs(os.path.join(self.dsh_dir, "storages"), exist_ok=True)
        os.makedirs(os.path.join(self.dsh_dir, "profiles"), exist_ok=True)

    def _patch_dsh_home(self, dsh_dir: str):
        """把 DSH_HOME 指向 tmp 下的 .dsh，使 _dsh_home() 与 root 同盘，relpath 可算。"""
        return mock.patch.dict(os.environ, {"DSH_HOME": dsh_dir}, clear=False)

    def test_detect_data_roots_lists_one_root(self) -> None:
        """同一根目录只显示一行：DSH 各类数据都在 ~/.dsh 下，仅返回该根一行。"""
        a = get_adapter("dsh")
        with self._patch_dsh_home(self.dsh_dir):
            roots = a.detect_data_roots(self.tmp)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["rel"], os.path.relpath(self.dsh_dir, self.tmp))
        self.assertTrue(roots[0]["exists"])
        # 子目录（sessions/storages/profiles）不单独列出
        rel = roots[0]["rel"].replace("\\", "/")
        self.assertNotIn("sessions", rel)
        self.assertNotIn("storages", rel)
        self.assertNotIn("profiles", rel)

    def test_detect_data_roots_missing(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="dsh_dr2_")
        a = get_adapter("dsh")
        with self._patch_dsh_home(os.path.join(tmp, ".dsh")):
            roots = a.detect_data_roots(tmp)
        self.assertEqual(len(roots), 1)
        self.assertTrue(all(not r["exists"] for r in roots))

    def test_detect_data_roots_empty_sessions(self) -> None:
        a = get_adapter("dsh")
        # 创建 .dsh 但 sessions 为空：仍只显示 .dsh 一行，根存在为 True
        os.makedirs(os.path.join(self.dsh_dir, "sessions"), exist_ok=True)
        with self._patch_dsh_home(self.dsh_dir):
            roots = a.detect_data_roots(self.tmp)
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0]["exists"])
        self.assertEqual(roots[0]["rel"], os.path.relpath(self.dsh_dir, self.tmp))


if __name__ == "__main__":
    unittest.main()