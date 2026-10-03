"""版本号一致性测试（``ai_env_clone/version.py`` 是唯一来源）。

版本号属于「必须可核证」的东西：窗口标题、``--version`` 输出、打包产物的 exe 版本
资源、README 里写的版本，都必须与 ``ai_env_clone/version.py`` 同源。这里把这几条
不变式固化成用例，避免再出现「文档说 A、程序说 B」。

注意：本文件**不导入 tkinter**（受管 Python 未装），所以对 ``__main__.py`` 的检查
一律读源码文本，而不是 import 它。这也是把标题模板从 GUI 挪到 version.py 的原因。
"""

import ast
import io
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ai_env_clone import version as v

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 版本号形态：``<major>.<minor>.<patch>``，可带预发布标识（``-rc.1``）与构建元数据。
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.\-]*)?(?:\+[0-9A-Za-z.\-]+)?$"
)

#: 允许硬编码版本字符串的文件（唯一来源）。其它文件一律不得写死。
_VERSION_SOURCE = "ai_env_clone/version.py"


def _read(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _exact_tag_at_head():
    """HEAD 上的 ``v*`` tag；不在 tag 上或环境无 git 时返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "tag", "--points-at", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        tag = line.strip()
        if tag.startswith("v"):
            return tag
    return None


class TestVersionSource(unittest.TestCase):
    """版本号本身与其派生值。"""

    def test_version_format(self) -> None:
        self.assertRegex(
            v.__version__,
            _VERSION_RE,
            "版本号须为 <major>.<minor>.<patch>（可带 -rc.N 等预发布标识），"
            "否则 PyInstaller / 包管理器无法解析",
        )

    def test_package_reexports_same_version(self) -> None:
        import ai_env_clone

        self.assertEqual(ai_env_clone.__version__, v.__version__)
        self.assertIn("__version__", ai_env_clone.__all__)

    def test_version_literal_lives_only_in_version_module(self) -> None:
        """版本字面量只许出现在 version.py —— 否则一定会与代码漂移。"""
        targets = ["build_exe.py"]
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("__pycache__", ".git", "build", "dist", ".workbuddy", "docs")
            ]
            for fn in filenames:
                if fn.endswith(".py"):
                    targets.append(
                        os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
                    )
        offenders = [
            rel for rel in sorted(set(targets))
            if rel != _VERSION_SOURCE and v.__version__ in _read(rel)
        ]
        self.assertEqual(
            offenders, [],
            "以下文件硬编码了版本号 %s，请改为从 ai_env_clone.version 引用：%s"
            % (v.__version__, offenders),
        )

    def test_title_and_version_line_carry_version(self) -> None:
        title = v.app_title("CodeBuddy CN")
        self.assertIn("CodeBuddy CN", title)
        self.assertTrue(
            title.endswith("v" + v.__version__),
            "窗口标题须以 v<版本> 结尾，实际为 %r" % title,
        )
        self.assertIn(v.__version__, v.version_line())
        self.assertIn(v.APP_NAME, v.version_line())

    def test_release_tuple_pads_and_drops_prerelease(self) -> None:
        """数字段只放整数：预发布标识无法表达，故只取发布段并补零到四位。"""
        with mock.patch.object(v, "__version__", "1.2.3-rc.4"):
            self.assertEqual(v.release_tuple(), (1, 2, 3, 0))
        with mock.patch.object(v, "__version__", "2.0"):
            self.assertEqual(v.release_tuple(), (2, 0, 0, 0))
        with mock.patch.object(v, "__version__", "3.4.5"):
            self.assertEqual(v.release_tuple(), (3, 4, 5, 0))
        # 真实版本同样要能解析成四段整数
        self.assertEqual(len(v.release_tuple()), 4)

    def test_windows_version_info(self) -> None:
        text = v.windows_version_info("AiEnvClone-windows.exe")
        # PyInstaller 用 eval() 反序列化该文件，所以必须是合法的 Python 字面量
        ast.parse(text)
        self.assertIn(v.__version__, text)
        self.assertIn("AiEnvClone-windows.exe", text)
        self.assertIn(repr(v.release_tuple()), text)
        # 含中文字段，必须声明编码，否则 PyInstaller 解码会出错
        self.assertIn("coding: utf-8", text)

    def test_windows_version_info_default_original_name(self) -> None:
        self.assertIn("%s.exe" % v.APP_NAME, v.windows_version_info())

    def test_windows_version_info_loadable_by_pyinstaller(self) -> None:
        try:
            from PyInstaller.utils.win32.versioninfo import (
                VSVersionInfo,
                load_version_info_from_text_file,
            )
        except Exception as exc:  # pragma: no cover - 未装 PyInstaller 时跳过
            self.skipTest("PyInstaller 不可用：%r" % exc)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "version_info.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(v.windows_version_info("AiEnvClone.exe"))
            info = load_version_info_from_text_file(path)
        self.assertIsInstance(info, VSVersionInfo)


class TestVersionFlag(unittest.TestCase):
    """``--version`` / ``-V`` 的行为。"""

    def test_long_flag_prints_and_reports_handled(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(v.handle_version_flag(["--version"]))
        self.assertIn(v.__version__, buf.getvalue())

    def test_short_flag_prints_and_reports_handled(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(v.handle_version_flag(["-V"]))
        self.assertIn(v.__version__, buf.getvalue())

    def test_other_args_are_not_handled(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertFalse(v.handle_version_flag([]))
            self.assertFalse(v.handle_version_flag(["--help"]))
            self.assertFalse(v.handle_version_flag(["--backup"]))
        self.assertEqual(buf.getvalue(), "", "非版本参数不得有任何输出")


class TestGuiWiring(unittest.TestCase):
    """GUI 接线（读源码校验，因为受管 Python 没装 tkinter）。"""

    def test_version_flag_handled_before_tkinter_import(self) -> None:
        src = _read("ai_env_clone/__main__.py")
        call = src.index("handle_version_flag(sys.argv[1:])")
        tk = src.index("import tkinter as tk")
        self.assertLess(
            call, tk,
            "版本查询必须排在 import tkinter 之前：否则没装 tkinter 的环境"
            "（CI / 最小化容器 / 受管 Python）执行 --version 会直接崩",
        )

    def test_window_title_uses_version_module(self) -> None:
        src = _read("ai_env_clone/__main__.py")
        self.assertIn("app_title(", src, "主窗口标题须由 version.app_title() 生成")
        self.assertNotIn("APP_TITLE_TPL =", src, "标题模板不得再写死在 GUI 里")


class TestVersionDocumented(unittest.TestCase):
    """版本号出现在用户看得到的地方，且与代码一致（与工具版本表的守卫同一思路）。"""

    def test_readme_states_current_version(self) -> None:
        readme = _read("README.md")
        self.assertIn(
            v.__version__, readme,
            "README 必须写明当前版本号 %s（CLI 章节的 --version 示例输出即为落点）；"
            "改版本号时同步更新 README" % v.__version__,
        )


class TestReleaseTagMatchesCode(unittest.TestCase):
    """HEAD 正好落在一个 ``v*`` tag 上时，tag 必须等于 ``v`` + ``__version__``。

    发布流程是「先改版本 → 提交 → 推送 → 打 tag」，这道闸能立刻发现
    「tag 打了、但代码里的版本号还是旧的」——那时 CI 会照着旧版本号打包发布。
    """

    def test_tag_matches_version_if_head_is_tagged(self) -> None:
        tag = _exact_tag_at_head()
        if tag is None:
            self.skipTest("当前 HEAD 不在 v* tag 上（或环境无 git），跳过")
        self.assertEqual(
            tag[1:], v.__version__,
            "tag %s 与代码版本 %s 不一致：请先改 ai_env_clone/version.py 再打 tag"
            % (tag, v.__version__),
        )


if __name__ == "__main__":
    unittest.main()
