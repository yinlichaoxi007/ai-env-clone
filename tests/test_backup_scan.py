"""``ai_env_clone.backup_scan``（备份包递归扫描 + 所属工具识别）测试。

覆盖方案（``docs/local/从备份包导入方案.md`` §8）要求的点：
- 递归能发现 2~3 层下的包；深度上限生效；
- ``max_dirs`` / ``max_hits`` / ``budget`` 触顶后**返回而非抛错**且有截断标记；
- 跳过 ``node_modules`` / ``.git`` 等剪枝目录；
- **不跟进目录联接 / 符号链接**；
- 取消令牌生效；
- 排序稳定（mtime 倒序）；
- 空目录 / 不存在目录；
- 文件名不合规时**不打开 zip**（关掉 L2 后打桩 ``zipfile`` 断言未被调用）；
- L1 / L2 / ``identify_tool`` 的识别优先级。
"""

import json
import os
import tempfile
import shutil
import threading
import time
import unittest
import zipfile
from unittest import mock

from ai_env_clone import backup_scan as bs
from ai_env_clone.core import MANIFEST_NAME


def _mk_zip(path, tool="qoder", kind="backup", entries=("data/a.txt",), manifest=True):
    """造一个备份包；``manifest=False`` 时只放业务条目（用于结构指纹场景）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if manifest:
            mf = {
                "version": 2, "kind": kind, "created_at": "2026-10-01T00:00:00",
                "source_root": "C:/src", "platform": os.name, "items": [],
                "file_count": len(entries), "total_bytes": 1,
                "bytes_by_ext": {}, "bytes_by_ext_compressed": {},
            }
            if tool:
                mf["tool"] = tool
            zf.writestr(MANIFEST_NAME, json.dumps(mf, ensure_ascii=False))
        for name in entries:
            zf.writestr(name, "x")


class TestScanBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paths(self, result):
        return [os.path.relpath(r.path, self.tmp).replace("\\", "/") for r in result.rows]

    def test_recursive_finds_packages_in_deep_layers(self):
        _mk_zip(os.path.join(self.tmp, "qoder_backup_l0.zip"))
        _mk_zip(os.path.join(self.tmp, "a", "codebuddy_backup_l1.zip"), tool="codebuddy")
        _mk_zip(os.path.join(self.tmp, "a", "b", "workbuddy_rollback_l2.zip"),
                tool="workbuddy", kind="rollback")
        res = bs.scan(self.tmp, max_depth=3)
        self.assertEqual(
            sorted(self._paths(res)),
            ["a/b/workbuddy_rollback_l2.zip", "a/codebuddy_backup_l1.zip", "qoder_backup_l0.zip"],
        )
        self.assertFalse(res.truncated)
        self.assertEqual([r.tool for r in res.rows if r.name.startswith("codebuddy")], ["codebuddy"])

    def test_depth_limit(self):
        """深度 1 只到第 1 层：``a/b/`` 下的包扫不到。"""
        _mk_zip(os.path.join(self.tmp, "a", "b", "deep_backup.zip"))
        self.assertEqual(bs.scan(self.tmp, max_depth=0).rows, [])
        self.assertEqual(bs.scan(self.tmp, max_depth=1).rows, [])
        self.assertEqual(len(bs.scan(self.tmp, max_depth=2).rows), 1)

    def test_prune_dirs_skipped(self):
        for junk in ("node_modules", ".git", "__pycache__"):
            _mk_zip(os.path.join(self.tmp, junk, "inner", "qoder_backup_x.zip"))
        self.assertEqual(bs.scan(self.tmp, max_depth=3).rows, [])

    def test_does_not_follow_symlink_or_junction(self):
        real = os.path.join(self.tmp, "real")
        _mk_zip(os.path.join(real, "qoder_backup_in_link.zip"))
        link = os.path.join(self.tmp, "link")
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target, exist_ok=True)
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境无法创建目录符号链接（Windows 需开发者模式/管理员）")
        res = bs.scan(self.tmp, max_depth=3)
        self.assertEqual([r.tool for r in res.rows], [])
        self.assertTrue(os.path.isdir(link))

    def test_missing_and_empty_dir(self):
        res = bs.scan(os.path.join(self.tmp, "nope"))
        self.assertEqual(res.rows, [])
        self.assertFalse(res.truncated)
        self.assertEqual(bs.scan(self.tmp).rows, [])

    def test_sort_by_mtime_desc(self):
        old = os.path.join(self.tmp, "qoder_backup_old.zip")
        new = os.path.join(self.tmp, "qoder_backup_new.zip")
        _mk_zip(old)
        _mk_zip(new)
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual([r.name for r in bs.scan(self.tmp).rows],
                         ["qoder_backup_new.zip", "qoder_backup_old.zip"])

    def test_cancel_token(self):
        for i in range(3):
            os.makedirs(os.path.join(self.tmp, "d%d" % i, "e"), exist_ok=True)
        _mk_zip(os.path.join(self.tmp, "d0", "qoder_backup_c.zip"))
        ev = threading.Event()
        ev.set()
        res = bs.scan(self.tmp, cancel=ev, max_depth=3)
        # 第 0 层不设预算/取消检查（首屏的一部分），第 1 层起立即退出
        self.assertTrue(res.truncated)
        self.assertEqual(res.reason, "cancelled")

    def test_max_dirs_truncates_without_raise(self):
        for i in range(6):
            _mk_zip(os.path.join(self.tmp, "d%d" % i, "qoder_backup_%d.zip" % i))
        res = bs.scan(self.tmp, max_depth=3, max_dirs=3)
        self.assertTrue(res.truncated)
        self.assertEqual(res.reason, "dirs")
        self.assertLessEqual(res.dirs_scanned, 4)   # 允许最后一个目录越界一格

    def test_max_hits_truncates_without_raise(self):
        for i in range(6):
            _mk_zip(os.path.join(self.tmp, "d%d" % i, "qoder_backup_%d.zip" % i))
        res = bs.scan(self.tmp, max_depth=3, max_hits=2)
        self.assertTrue(res.truncated)
        self.assertEqual(res.reason, "hits")

    def test_budget_truncates_without_raise(self):
        for i in range(6):
            _mk_zip(os.path.join(self.tmp, "d%d" % i, "qoder_backup_%d.zip" % i))
        res = bs.scan(self.tmp, max_depth=3, budget_s=0.0)
        self.assertTrue(res.truncated)
        self.assertEqual(res.reason, "budget")

    def test_on_layer_reports_cumulative_rows(self):
        _mk_zip(os.path.join(self.tmp, "qoder_backup_l0.zip"))
        _mk_zip(os.path.join(self.tmp, "a", "qoder_backup_l1.zip"))
        seen = []
        bs.scan(self.tmp, max_depth=3, on_layer=lambda rows, p: seen.append((len(rows), p.layers)))
        self.assertIn((1, 1), seen)
        self.assertEqual(seen[-1][0], 2)


class TestNoZipOpen(unittest.TestCase):
    """L1 是 0 成本：不做 L2 时，扫描绝不打开任何 zip。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_disabled_never_opens_zip(self):
        # 命名完全不合规 + 无关 zip：都不该触发解压
        with zipfile.ZipFile(os.path.join(self.tmp, "random.zip"), "w") as zf:
            zf.writestr("src/main.py", "print(1)")
        with zipfile.ZipFile(os.path.join(self.tmp, "backup_20260101.zip"), "w") as zf:
            zf.writestr("x.txt", "x")
        with mock.patch("ai_env_clone.backup_scan.zipfile.ZipFile") as m:
            res = bs.scan(self.tmp, max_depth=3, manifest_limit=0)
            self.assertEqual(m.call_count, 0)
        self.assertEqual(len(res.rows), 2)
        # 没有 L2 时，非约定命名认不出工具，但旧版 backup_ 前缀仍能定出「类型」
        by_name = {r.name: r for r in res.rows}
        self.assertEqual(by_name["random.zip"].kind, "unknown")
        self.assertFalse(by_name["random.zip"].identifiable)
        self.assertEqual(by_name["backup_20260101.zip"].kind, "backup")


class TestIdentify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tool_from_filename_and_dirname(self):
        self.assertEqual(bs.tool_from_filename("codebuddy_backup_1.zip"), "codebuddy")
        self.assertEqual(bs.tool_from_filename("TRAE-CN_rollback_1.zip"), "trae-cn")
        self.assertEqual(bs.tool_from_filename("backup_20260101.zip"), "")
        self.assertEqual(bs.tool_from_parent_dir(os.path.join("x", "qoder", "a.zip")), "qoder")
        self.assertEqual(bs.tool_from_parent_dir(os.path.join("x", "y", "a.zip")), "")

    def test_l2_manifest_identifies_non_conforming_name(self):
        _mk_zip(os.path.join(self.tmp, "plain.zip"), tool="dsh")
        res = bs.scan(self.tmp, max_depth=1)
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0].tool, "dsh")
        self.assertEqual(res.rows[0].tool_source, "manifest")
        self.assertTrue(res.rows[0].identifiable)

    def test_identify_prefers_manifest_then_named_tool(self):
        entries = [".workbuddy/workbuddy.db"]
        self.assertEqual(bs.identify_tool(entries, {"tool": "qoder"}), ("qoder", "manifest"))
        self.assertEqual(bs.identify_tool(entries, {}), ("workbuddy", "structure"))
        self.assertEqual(bs.identify_tool(["nothing/here"], {}), ("", ""))

    def test_identify_prefer_keeps_legacy_self_package(self):
        """无声明但本来就是当前工具的老包：``prefer`` 让当前工具优先命中。"""
        entries = [".qoder-cn/x.json"]
        self.assertEqual(bs.identify_tool(entries, {}, prefer="qoder"), ("qoder", "structure"))

    def test_display_name_falls_back_to_raw(self):
        self.assertEqual(bs.display_name("codebuddy"), "CodeBuddy")
        self.assertEqual(bs.display_name("no-such-tool"), "no-such-tool")
        self.assertEqual(bs.display_name(""), "")

    def test_is_importable_for(self):
        mk = lambda tool: bs.BackupEntry("p", "n", 0, 0, "backup", tool)
        self.assertTrue(bs.is_importable_for(mk("codebuddy"), "workbuddy"))
        self.assertFalse(bs.is_importable_for(mk("workbuddy"), "workbuddy"))   # 本工具
        self.assertFalse(bs.is_importable_for(mk("qoder"), "workbuddy"))       # 加密
        self.assertFalse(bs.is_importable_for(mk(""), "workbuddy"))            # 未识别
        self.assertFalse(bs.is_importable_for(mk("workbuddy"), "qoder"))       # 目标是加密工具
        self.assertFalse(bs.is_importable_for(mk("codebuddy"), "zcode"))       # 写出未实测

    def test_read_manifest_bad_zip_returns_none(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"not a zip")
        self.assertIsNone(bs.read_manifest(bad))
        self.assertIsNone(bs.read_manifest(os.path.join(self.tmp, "missing.zip")))

    def test_cache_avoids_second_open(self):
        _mk_zip(os.path.join(self.tmp, "plain.zip"), tool="dsh")
        cache = {}
        bs.scan(self.tmp, max_depth=1, cache=cache)
        self.assertTrue(cache)
        with mock.patch("ai_env_clone.backup_scan.read_manifest") as m:
            bs.scan(self.tmp, max_depth=1, cache=cache)
            self.assertEqual(m.call_count, 0)


if __name__ == "__main__":
    unittest.main()
