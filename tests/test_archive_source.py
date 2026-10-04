"""``ai_env_clone.archive_source``（备份包内定位 + 按需解包 + 临时目录生命周期）。

覆盖方案（``docs/local/从备份包导入方案.md`` §8）要求：

- 5 个工具的**包内定位规则**（codebuddy / reasonix / workbuddy / dsh / zcode）；
- **前缀无关**：ZCode 的 ``Users/<用户名>/`` 必须命中，不能写死盘符前缀；
- **按需解包不落全量**：会话子树之外的条目不得被解出；
- 与会话根同级的**伴随文件**（``session-workspaces.json``）要被一并取出；
- 定位失败时的**回退**：有界搜索 + 试跑扫描函数命中；
- 临时目录**清理**（显式 ``cleanup`` / 上下文管理器 / 失败路径不留垃圾 / 过期残留）；
- 坏包 / 空包 / 非本工具包 → :class:`ArchiveSourceError`。
"""

import os
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from ai_env_clone import archive_source as asrc
from ai_env_clone import session_migration


def _mk_zip(path, entries):
    """造一个 zip（内容无关紧要，只关心条目名）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name in entries:
            zf.writestr(name, "x")
    return path


class TestLocate(unittest.TestCase):
    """``locate_session_root`` 只读条目名，不解包。"""

    def test_codebuddy(self):
        entries = ["root/history/w1/s1/index.json",
                   "root/history/w1/s1/messages/m1.json"]
        # 返回的是 **history 根**（scan_codebuddy 的入参口径）
        self.assertEqual(asrc.locate_session_root(entries, "codebuddy"), "root/history")

    def test_reasonix(self):
        entries = ["root/projects/p1/sessions/abc-session.jsonl"]
        self.assertEqual(asrc.locate_session_root(entries, "reasonix"), "root")

    def test_workbuddy_by_projects_and_by_db(self):
        self.assertEqual(
            asrc.locate_session_root(["Users/u/.workbuddy/projects/abc/s1.jsonl"],
                                     "workbuddy"),
            "Users/u/.workbuddy")
        self.assertEqual(
            asrc.locate_session_root(["Users/u/.workbuddy/workbuddy.db"],
                                     "workbuddy"),
            "Users/u/.workbuddy")

    def test_dsh(self):
        self.assertEqual(
            asrc.locate_session_root(["Users/u/.dsh/sessions/ws/s/x.jsonl"], "dsh"),
            "Users/u/.dsh")

    def test_zcode(self):
        self.assertEqual(
            asrc.locate_session_root(["Users/u/.zcode/cli/db/db.sqlite"], "zcode"),
            "Users/u/.zcode")

    def test_zcode_prefix_independent(self):
        """ZCode 的会话根是**盘根**，包内会多一层 ``Users/<用户名>/``。

        定位必须按路径段，不能写死 ``C:/`` 这样的绝对前缀——换台机器、
        换个用户名，包内前缀就变了。
        """
        for prefix in ("Users/wangqian", "Users/someone-else", "home/deep/nested"):
            with self.subTest(prefix=prefix):
                entries = ["%s/.zcode/cli/db/db.sqlite" % prefix, "%s/.zcode/cli/db/x.db" % prefix]
                self.assertEqual(asrc.locate_session_root(entries, "zcode"),
                                 "%s/.zcode" % prefix)

    def test_dot_dir_ignores_unrelated_entries(self):
        """同一根下混有无关条目时，仍定位到同一个 ``.workbuddy`` 根。"""
        entries = ["Users/u/.workbuddy/projects/y/s.jsonl",
                   "Users/u/.workbuddy/workbuddy.db",
                   "Users/u/Documents/readme.md"]
        self.assertEqual(asrc.locate_session_root(entries, "workbuddy"),
                         "Users/u/.workbuddy")

    def test_returns_none_when_not_found(self):
        self.assertIsNone(asrc.locate_session_root([], "codebuddy"))
        self.assertIsNone(asrc.locate_session_root(["a/b/c.txt"], "codebuddy"))
        self.assertIsNone(asrc.locate_session_root(["a/b/c.txt"], "dsh"))
        # 不能定位的工具（如加密工具）一律 None
        self.assertIsNone(asrc.locate_session_root(["a/history/w/s/index.json"], "qoder"))


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zips = os.path.join(self.tmp, "zips")
        os.makedirs(self.zips, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self, name, entries):
        return _mk_zip(os.path.join(self.zips, name), entries)

    def test_extracts_subtree_only(self):
        """会话子树之外的条目**不得**被解出（CodeBuddy 的 history 包可能几百 MB）。"""
        path = self._zip("cb.zip", [
            "Users/u/CodeBuddyIDE/history/w1/s1/index.json",
            "Users/u/CodeBuddyIDE/history/w1/s1/messages/m1.json",
            "Users/u/CodeBuddyIDE/session-workspaces.json",   # 伴随文件（history 的上一层）
            "Users/u/CodeBuddyIDE/other/huge.bin",            # 子树外，不该解
        ])
        try:
            src = asrc.extract_session_root(path, "codebuddy")
        except asrc.ArchiveSourceError as exc:  # pragma: no cover
            self.fail("应能解出 CodeBuddy 会话：%s" % exc)
        try:
            self.assertEqual(src.reason, "path")
            self.assertEqual(src.prefix, "Users/u/CodeBuddyIDE/history")
            self.assertEqual(src.total, 4)
            self.assertFalse(src.fully_extracted)
            self.assertEqual(src.extracted, 3)
            self.assertTrue(os.path.isfile(os.path.join(
                src.root, "w1", "s1", "index.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                src.temp_dir, "Users", "u", "CodeBuddyIDE", "session-workspaces.json")))
            self.assertFalse(os.path.exists(os.path.join(
                src.temp_dir, "Users", "u", "CodeBuddyIDE", "other", "huge.bin")))
        finally:
            src.cleanup()

    def test_relative_layout_feeds_existing_scanner(self):
        """解出的相对布局要能被既有扫描函数直接消费（复用导入链路的前提）。"""
        path = self._zip("wb.zip", [
            "Users/u/.workbuddy/projects/proj/s1.jsonl",
            "Users/u/.workbuddy/workbuddy.db",
        ])
        with asrc.extract_session_root(path, "workbuddy") as src:
            rows = session_migration.list_source_sessions("workbuddy", src.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("s1.jsonl"))

    def test_fallback_probe_finds_scannable_root(self):
        """路径段规则未命中（无 ``.workbuddy``）→ 回退有界搜索 + 试跑扫描函数。"""
        path = self._zip("wb_named.zip", [
            "data/projects/proj/s1.jsonl",
        ])
        with asrc.extract_session_root(path, "workbuddy") as src:
            self.assertEqual(src.reason, "fallback")
            self.assertEqual(src.root, os.path.join(src.temp_dir, "data"))
            self.assertTrue(os.path.isdir(src.root))

    def test_cleanup_removes_temp_dir(self):
        path = self._zip("dsh.zip", ["Users/u/.dsh/sessions/ws/s/x.jsonl"])
        src = asrc.extract_session_root(path, "dsh")
        temp = src.temp_dir
        self.assertTrue(os.path.isdir(temp))
        src.cleanup()
        self.assertFalse(os.path.exists(temp))
        src.cleanup()   # 幂等，可重复调用

    def test_context_manager_cleans_up(self):
        path = self._zip("dsh2.zip", ["Users/u/.dsh/sessions/ws/s/x.jsonl"])
        with asrc.extract_session_root(path, "dsh") as src:
            temp = src.temp_dir
            self.assertTrue(os.path.isdir(temp))
        self.assertFalse(os.path.exists(temp))

    def test_failure_leaves_no_temp_dir(self):
        """解包失败路径必须先删掉已建的临时目录，不留垃圾。"""
        path = self._zip("junk.zip", ["a.txt"])
        fake = os.path.join(self.tmp, "fake_tmp")
        with mock.patch("ai_env_clone.archive_source.tempfile.mkdtemp",
                        return_value=fake):
            with self.assertRaises(asrc.ArchiveSourceError):
                asrc.extract_session_root(path, "dsh")
        self.assertFalse(os.path.exists(fake))

    def test_missing_file(self):
        with self.assertRaises(asrc.ArchiveSourceError):
            asrc.extract_session_root(os.path.join(self.zips, "nope.zip"), "dsh")

    def test_not_a_zip(self):
        bad = os.path.join(self.zips, "bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"not a zip")
        with self.assertRaises(asrc.ArchiveSourceError):
            asrc.extract_session_root(bad, "dsh")

    def test_empty_zip(self):
        empty = os.path.join(self.zips, "empty.zip")
        with zipfile.ZipFile(empty, "w"):
            pass
        with self.assertRaises(asrc.ArchiveSourceError):
            asrc.extract_session_root(empty, "dsh")

    def test_wrong_tool_package(self):
        """包内没有该工具的会话数据（且回退也扫不出）→ 报错。"""
        path = self._zip("other.zip", ["docs/readme.md", "src/main.py"])
        with self.assertRaises(asrc.ArchiveSourceError):
            asrc.extract_session_root(path, "dsh")


class TestCleanupStale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, name, age_s):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        old = time.time() - age_s
        os.utime(path, (old, old))
        return path

    def test_only_stale_prefixed_dirs_removed(self):
        stale = self._mk(asrc.TEMP_PREFIX + "old", asrc.STALE_AFTER_S + 60)
        fresh = self._mk(asrc.TEMP_PREFIX + "new", 10)
        other = self._mk("unrelated_dir", asrc.STALE_AFTER_S + 60)
        with mock.patch("ai_env_clone.archive_source.tempfile.gettempdir",
                        return_value=self.tmp):
            removed = asrc.cleanup_stale()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(fresh))    # 未超时：可能是别的实例在用
        self.assertTrue(os.path.isdir(other))    # 非本模块前缀：不动

    def test_missing_temp_root_is_safe(self):
        with mock.patch("ai_env_clone.archive_source.tempfile.gettempdir",
                        return_value=os.path.join(self.tmp, "nope")):
            self.assertEqual(asrc.cleanup_stale(), 0)


if __name__ == "__main__":
    unittest.main()
