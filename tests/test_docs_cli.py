"""``--docs`` / ``--version`` 两个 CLI 分支的测试。

关键性质：**两者都必须生效在 ``import tkinter`` 之前**。
本项目的受管 Python 没装 tkinter，若顺序被挪到 tkinter 之后，
`--version` / `--docs` 在 CI 或无 GUI 环境下就会直接 ImportError。

因此这里的测试**用受管 Python（无 tkinter）跑子进程**来验证：
子进程能打印出内容，就证明这条路径确实不依赖 GUI 库。
"""

import os
import subprocess
import sys
import unittest

from ai_env_clone import doctext

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _has_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except ImportError:
        return False


class TestDocsFlagUnit(unittest.TestCase):
    """在进程内直接测 ``doctext.handle_docs_flag``（打桩输出，不打印真文）。

    ★ 用 ``doctext`` 而非 ``__main__``：``__main__`` 一 import 就拉起 tkinter，
    受管 Python（无 tkinter）下根本导不进来 —— 而这条 CLI 路径**本来就要求**
    不依赖 GUI 库，用 ``__main__`` 去测它等于自相矛盾。
    """

    def test_returns_false_without_flag(self) -> None:
        self.assertFalse(doctext.handle_docs_flag(["--version"]))
        self.assertFalse(doctext.handle_docs_flag([]))

    def test_prints_help_text_when_flag_present(self) -> None:
        import io

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            handled = doctext.handle_docs_flag(["--docs"])
        finally:
            sys.stdout = old
        self.assertTrue(handled)
        text = buf.getvalue()
        self.assertIn("AiEnvClone", text)
        self.assertNotIn("![", text, "CLI 口径同样不显示图片行")
        self.assertNotIn("[图片", text)

    def test_missing_help_file_degrades_gracefully(self) -> None:
        """读不到文档时给可诊断提示，不抛栈（打包漏 --add-data 属构建缺陷）。"""
        import io
        from unittest import mock

        import ai_env_clone.resources as res

        buf = io.StringIO()
        with mock.patch.object(res, "read_help_doc", side_effect=OSError("缺失")), \
             mock.patch.object(sys, "stdout", buf):
            self.assertTrue(doctext.handle_docs_flag(["--docs"]))
        self.assertIn("无法读取使用说明", buf.getvalue())


class TestDocsSubprocessWithoutTkinter(unittest.TestCase):
    """子进程实测：这条路径不依赖 tkinter。"""

    def _run(self, arg: str):
        return subprocess.run(
            [sys.executable, "-m", "ai_env_clone", arg],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    @unittest.skipIf(_has_tkinter(), "当前解释器装了 tkinter，无法证明「不依赖」")
    def test_docs_works_without_tkinter(self) -> None:
        res = self._run("--docs")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("AiEnvClone", res.stdout)

    def test_version_still_works(self) -> None:
        from ai_env_clone.version import __version__

        res = self._run("--version")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(__version__, res.stdout)

    def test_help_flag_comes_before_tkinter_import(self) -> None:
        """源码顺序：``handle_docs_flag`` 的调用必须在 ``import tkinter`` 之前。"""
        path = os.path.join(REPO_ROOT, "ai_env_clone", "__main__.py")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        docs_at = version_at = tkinter_at = None
        for i, line in enumerate(lines):
            if docs_at is None and "if handle_docs_flag(" in line:
                docs_at = i
            if version_at is None and "if handle_version_flag(" in line:
                version_at = i
            if tkinter_at is None and line.startswith("import tkinter as tk"):
                tkinter_at = i
        self.assertIsNotNone(docs_at, "未找到 handle_docs_flag 调用")
        self.assertIsNotNone(version_at)
        self.assertIsNotNone(tkinter_at)
        self.assertLess(docs_at, tkinter_at)
        self.assertLess(version_at, tkinter_at)


if __name__ == "__main__":
    unittest.main()
