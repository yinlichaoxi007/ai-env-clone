"""提权模块（``elevate.py``）测试——全部打桩，**绝不真的弹 UAC**。

钉住方案 6.1.1 的三条边界：

1. **已提权却仍不可写 ⇒ 不该再提权**（不是权限问题：只读卷 / 组策略 / 杀软锁定）；
2. **用户取消 UAC**（``ShellExecuteW`` 返回 ≤ 32）⇒ 不是成功受理，但也不是错误——
   调用方应静默回到对话框；
3. **源码模式不提供提权按钮**（``sys.executable`` 是 python.exe，重建命令没有意义）；
   frozen 下重建的命令必须带 ``--resume-update="<路径>"`` 且路径加引号（含空格）。
"""

import os
import sys
import unittest
from unittest import mock

from ai_env_clone import elevate


class TestIsFrozen(unittest.TestCase):
    def test_source_mode_is_not_frozen(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertFalse(elevate.is_frozen())

    def test_frozen_attr_means_frozen(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(elevate.is_frozen())


class TestResumeCommand(unittest.TestCase):
    def test_frozen_rebuilds_command_with_quoted_path(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "C:\\Program Files\\x\\AiEnvClone.exe"):
            exe, params = elevate.resume_command("C:\\Users\\a b\\update\\AiEnvClone-windows.exe")
        self.assertEqual(exe, "C:\\Program Files\\x\\AiEnvClone.exe")
        self.assertTrue(params.startswith("--resume-update=\""))
        self.assertTrue(params.endswith(".exe\""))
        self.assertIn("a b", params)            # 含空格的路径必须整体加引号

    def test_source_mode_raises(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            with self.assertRaises(RuntimeError):
                elevate.resume_command("x")


class TestRequestElevation(unittest.TestCase):
    def test_source_mode_never_elevates(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(elevate, "_shell_execute_runas") as runas:
            self.assertFalse(elevate.request_elevation("x"))
        runas.assert_not_called()

    def test_accepted_when_shell_returns_above_32(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(elevate, "_shell_execute_runas", return_value=42) as runas:
            self.assertTrue(elevate.request_elevation("C:\\x\\new.exe"))
        exe, params = runas.call_args[0]
        self.assertTrue(exe.endswith(".exe"))
        self.assertIn("--resume-update", params)

    def test_user_declined_uac_is_not_success(self) -> None:
        """典型返回 5（用户拒绝 UAC）⇒ ``False``，调用方静默回对话框。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(elevate, "_shell_execute_runas", return_value=5):
            self.assertFalse(elevate.request_elevation("C:\\x\\new.exe"))

    def test_real_impl_never_runs_outside_windows(self) -> None:
        """真实现（未打桩）在非 Windows 上必须安全返回 ``False``/``0``。"""
        if sys.platform.startswith("win"):
            self.skipTest("仅非 Windows 平台验证")
        self.assertFalse(elevate._is_user_admin_impl())
        self.assertEqual(elevate._shell_execute_runas("x", ""), 0)


class TestIsUserAdmin(unittest.TestCase):
    def test_patched_impl(self) -> None:
        with mock.patch.object(elevate, "_is_user_admin_impl", return_value=True):
            self.assertTrue(elevate.is_user_admin())
        with mock.patch.object(elevate, "_is_user_admin_impl", return_value=False):
            self.assertFalse(elevate.is_user_admin())


if __name__ == "__main__":
    unittest.main()
