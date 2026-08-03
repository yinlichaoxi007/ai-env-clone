"""
Qoder 备份迁移工具 —— 全面测试用例

运行：
    python -m unittest test_qoder_backup -v
或：
    python test_qoder_backup.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile

from qoder_backup_core import (
    MANIFEST_NAME,
    BackupError,
    BackupItem,
    build_items,
    detect_qoder_root,
    export_backup,
    import_backup,
    inspect_backup,
    is_critical,
    is_excluded,
    scan_items,
    snapshot_sqlite,
    _safe_target,
)


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #
class TempEnv(unittest.TestCase):
    """构造一个仿真的 Qoder 目录树。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="qoder_test_")
        self.root = os.path.join(self.tmp, ".qoder-cn")
        self.shared = os.path.join(self.root, "shared_client")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._make_tree()

    def _w(self, rel: str, content: bytes = b"x") -> str:
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def _make_tree(self) -> None:
        self._w("settings.json", b'{"theme":"dark"}')
        self._w("shared_client/memories/global/pref.md", b"# memory\n")
        self._w("shared_client/memories/projects/p1/note.md", b"# note\n")
        self._w("cache/projects/proj1/data.json", b"{}")
        self._w("shared_client/repowiki/wiki.md", b"# wiki\n")
        # 噪音文件，应被默认规则排除
        self._w("shared_client/cache/db.7z", b"A" * 2048)
        self._w("shared_client/cache/diagnosis.bin", b"B" * 2048)
        self._w("shared_client/logs/run.log", b"log")
        self._w("cache - 副本/old.json", b"{}")
        # 真实 SQLite 库
        self.db = os.path.join(self.shared, "cache", "db", "local.db")
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        con = sqlite3.connect(self.db)
        con.execute("create table chat_message(id integer primary key, body text)")
        con.executemany(
            "insert into chat_message(body) values(?)",
            [("消息 %d" % i,) for i in range(50)],
        )
        con.commit()
        con.close()

    def items(self):
        from qoder_backup_core import QoderPaths

        return build_items(QoderPaths(root=self.root, shared=self.shared))

    def existing_items(self):
        return [i for i in self.items() if i.exists]


# --------------------------------------------------------------------------- #
# 1. 路径探测
# --------------------------------------------------------------------------- #
class TestDetect(TempEnv):
    def test_explicit_root(self):
        p = detect_qoder_root(self.root)
        self.assertEqual(os.path.normpath(p.root), os.path.normpath(self.root))
        self.assertTrue(p.exists)

    def test_detects_shared_client_underscore(self):
        """必须识别真实存在的 shared_client（原代码写死 sharedclient 是致命 bug）。"""
        p = detect_qoder_root(self.root)
        self.assertTrue(os.path.isdir(p.shared))
        self.assertEqual(os.path.basename(p.shared), "shared_client")

    def test_detects_legacy_sharedclient(self):
        """兼容旧版无下划线命名。"""
        legacy_root = os.path.join(self.tmp, "legacy")
        os.makedirs(os.path.join(legacy_root, "sharedclient", "memories"))
        p = detect_qoder_root(legacy_root)
        self.assertEqual(os.path.basename(p.shared), "sharedclient")

    def test_missing_root_is_graceful(self):
        p = detect_qoder_root(os.path.join(self.tmp, "nope"))
        self.assertFalse(p.exists)
        self.assertFalse(any(i.exists for i in build_items(p)))

    def test_auto_detect_no_crash(self):
        self.assertIsInstance(detect_qoder_root().root, str)


# --------------------------------------------------------------------------- #
# 2. 排除规则与扫描
# --------------------------------------------------------------------------- #
class TestScan(TempEnv):
    def test_excludes_noise(self):
        for bad in (
            "shared_client/cache/db.7z",
            "shared_client/cache/diagnosis.bin",
            "shared_client/logs/run.log",
            "cache - 副本/old.json",
            "a/b.db-wal",
        ):
            self.assertTrue(is_excluded(bad, __import__(
                "qoder_backup_core").DEFAULT_EXCLUDES), bad)

    def test_keeps_real_data(self):
        for good in (
            "shared_client/memories/global/pref.md",
            "settings.json",
            "shared_client/cache/db/local.db",
        ):
            self.assertFalse(is_excluded(good, __import__(
                "qoder_backup_core").DEFAULT_EXCLUDES), good)

    def test_scan_filters_and_counts(self):
        r = scan_items(self.existing_items(), self.root)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/memories/global/pref.md", rels)
        self.assertIn("settings.json", rels)
        self.assertNotIn("shared_client/cache/db.7z", rels)
        self.assertGreater(r.skipped_count, 0)
        self.assertGreater(r.total_bytes, 0)

    def test_arcname_is_root_relative(self):
        """归档路径必须相对 Qoder 根，这是修复恢复错位的关键。"""
        r = scan_items(self.existing_items(), self.root)
        for _, rel in r.files:
            self.assertFalse(rel.startswith("/"))
            self.assertNotIn("\\", rel)
            self.assertFalse(rel.startswith(".."))
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/memories/projects/p1/note.md", rels)

    def test_no_duplicates_when_items_overlap(self):
        """cache/db 与 cache 目录重叠，不应重复打包。"""
        r = scan_items(self.existing_items(), self.root)
        rels = [rel for _, rel in r.files]
        self.assertEqual(len(rels), len(set(rels)))

    def test_max_file_size_limit(self):
        self._w("shared_client/memories/big.md", b"Z" * 4096)
        small = scan_items(self.existing_items(), self.root, max_file_mb=0.001)
        big = scan_items(self.existing_items(), self.root, max_file_mb=None)
        self.assertLess(small.file_count, big.file_count)

    def test_missing_items_recorded(self):
        r = scan_items(self.items(), self.root)
        self.assertIn("session_env", r.missing_keys)

    def test_large_session_db_always_included(self):
        """回归：会话库体积远超上限也必须备份，否则备份毫无意义。"""
        with open(self.db, "ab") as f:
            f.write(b"\0" * (3 * 1024 * 1024))  # 撑到 3MB+
        r = scan_items(self.existing_items(), self.root, max_file_mb=0.05)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/cache/db/local.db", rels)

    def test_excluded_copy_db_still_excluded(self):
        """排除规则优先级高于关键文件豁免：`- 副本` 里的库不应被捞回来。"""
        self._w("shared_client/cache/db - 副本/local.db", b"stale")
        r = scan_items(self.existing_items(), self.root, max_file_mb=0.001)
        rels = {rel for _, rel in r.files}
        self.assertNotIn("shared_client/cache/db - 副本/local.db", rels)

    def test_custom_threshold_skips_below_limit(self):
        """自定义阈值（如 1MB）：超过该值且非关键文件被跳过。"""
        self._w("shared_client/memories/big.md", b"Z" * (2 * 1024 * 1024))
        r = scan_items(self.existing_items(), self.root, max_file_mb=1.0)
        rels = {rel for _, rel in r.files}
        self.assertNotIn("shared_client/memories/big.md", rels)
        self.assertIn("shared_client/cache/db/local.db", rels)  # 关键库豁免

    def test_threshold_none_includes_everything(self):
        """阈值 None 表示不限制，超大普通文件也纳入备份。"""
        self._w("shared_client/memories/huge.md", b"Z" * (50 * 1024 * 1024))
        r = scan_items(self.existing_items(), self.root, max_file_mb=None)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/memories/huge.md", rels)

    def test_sqlite_wal_shm_always_included(self):
        """SQLite 配套文件（.db-wal / .db-shm）属于关键文件，不被体积阈值跳过。"""
        # 在 cache/db 下放主库 + 配套文件
        self._w("shared_client/cache/db/local.db", b"SQLite format 3\x00" + b"A" * 100)
        self._w("shared_client/cache/db/local.db-wal", b"B" * (2 * 1024 * 1024))
        self._w("shared_client/cache/db/local.db-shm", b"C" * (2 * 1024 * 1024))
        # 阈值极低，普通大文件会被跳过，但关键配套必须保留
        r = scan_items(self.existing_items(), self.root, max_file_mb=0.001)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/cache/db/local.db", rels)
        self.assertIn("shared_client/cache/db/local.db-wal", rels)
        self.assertIn("shared_client/cache/db/local.db-shm", rels)
        # is_critical 直接判定
        self.assertTrue(is_critical("shared_client/cache/db/local.db-wal"))
        self.assertTrue(is_critical("shared_client/cache/db/local.db-shm"))

    def test_code_index_included(self):
        """shared_client/index 代码索引目录（.db/.bolt/.zap/.json）应被纳入备份。"""
        self._w("shared_client/index/repo1.db", b"SQLite format 3\x00" + b"X" * 100)
        self._w("shared_client/index/repo1.bolt", b"BOLT")
        self._w("shared_client/index/repo1.zap", b"ZAP")
        self._w("shared_client/index/meta.json", b"{}")
        # 构造 build_items 含 code_index 条目
        from qoder_backup_core import QoderPaths, build_items
        items = build_items(QoderPaths(root=self.root, shared=self.shared))
        keys = {i.key for i in items}
        self.assertIn("code_index", keys)
        r = scan_items(items, self.root)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/index/repo1.db", rels)
        self.assertIn("shared_client/index/repo1.bolt", rels)
        self.assertIn("shared_client/index/repo1.zap", rels)

    def test_index_copy_excluded(self):
        """index - 副本 不应被备份（冗余副本）。"""
        self._w("shared_client/index - 副本/repo.db", b"SQLite format 3\x00" + b"Y")
        from qoder_backup_core import QoderPaths, build_items
        # 构造一个含 index 目录的条目（模拟 code_index 已存在）
        items = build_items(QoderPaths(root=self.root, shared=self.shared))
        items.append(
            BackupItem(
                key="code_index",
                label="代码索引数据库",
                path=os.path.join(self.shared, "index"),
                description="",
            )
        )
        r = scan_items(items, self.root)
        rels = {rel for _, rel in r.files}
        self.assertNotIn("shared_client/index - 副本/repo.db", rels)


# --------------------------------------------------------------------------- #
# 3. 导出
# --------------------------------------------------------------------------- #
class TestExport(TempEnv):
    def test_export_creates_valid_zip(self):
        z = os.path.join(self.tmp, "b.zip")
        mf = export_backup(z, self.existing_items(), self.root)
        self.assertTrue(zipfile.is_zipfile(z))
        self.assertGreater(mf["file_count"], 0)
        self.assertEqual(mf["zip_path"], z)

    def test_manifest_embedded(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        with zipfile.ZipFile(z) as zf:
            self.assertIn(MANIFEST_NAME, zf.namelist())
            mf = json.loads(zf.read(MANIFEST_NAME))
        self.assertEqual(mf["version"], 2)
        self.assertIn("created_at", mf)

    def test_export_empty_raises(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        item = BackupItem("x", "X", os.path.join(empty, "none"), "d")
        with self.assertRaises(BackupError):
            export_backup(os.path.join(self.tmp, "e.zip"), [item], empty)

    def test_no_partial_file_left_on_success(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        self.assertFalse(os.path.exists(z + ".part"))

    def test_progress_reported(self):
        seen = []
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root,
                      progress=lambda i: seen.append(i))
        self.assertTrue(seen)
        self.assertEqual(seen[-1].percent, 100.0)

    def test_overwrite_existing(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        first = os.path.getsize(z)
        export_backup(z, self.existing_items(), self.root)
        self.assertTrue(os.path.exists(z))
        self.assertGreater(first, 0)


# --------------------------------------------------------------------------- #
# 4. SQLite 一致性
# --------------------------------------------------------------------------- #
class TestSqlite(TempEnv):
    def test_snapshot_produces_readable_db(self):
        dst = os.path.join(self.tmp, "snap.db")
        self.assertTrue(snapshot_sqlite(self.db, dst))
        con = sqlite3.connect(dst)
        self.assertEqual(
            con.execute("select count(*) from chat_message").fetchone()[0], 50
        )
        con.close()

    def test_snapshot_while_open_connection(self):
        """Qoder 运行中（连接未关闭）也应能生成一致快照。"""
        live = sqlite3.connect(self.db)
        live.execute("insert into chat_message(body) values('运行中')")
        live.commit()
        dst = os.path.join(self.tmp, "snap2.db")
        ok = snapshot_sqlite(self.db, dst)
        live.close()
        self.assertTrue(ok)
        con = sqlite3.connect(dst)
        self.assertEqual(
            con.execute("select count(*) from chat_message").fetchone()[0], 51
        )
        con.close()

    def test_db_survives_roundtrip(self):
        """备份→恢复后数据库必须仍可查询且数据完整。"""
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        dst_root = os.path.join(self.tmp, "restored")
        import_backup(z, dst_root, make_rollback=False)
        out = os.path.join(dst_root, "shared_client", "cache", "db", "local.db")
        self.assertTrue(os.path.exists(out))
        con = sqlite3.connect(out)
        self.assertEqual(
            con.execute("select count(*) from chat_message").fetchone()[0], 50
        )
        con.close()

    def test_non_sqlite_untouched(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        with zipfile.ZipFile(z) as zf:
            self.assertEqual(zf.read("settings.json"), b'{"theme":"dark"}')


# --------------------------------------------------------------------------- #
# 5. 安全：Zip Slip
# --------------------------------------------------------------------------- #
class TestSecurity(TempEnv):
    def test_safe_target_blocks_traversal(self):
        base = os.path.realpath(self.tmp)
        for evil in (
            "../evil.txt",
            "../../evil.txt",
            "a/../../evil.txt",
            "/abs/evil.txt",
            "C:/Windows/evil.txt",
        ):
            self.assertIsNone(_safe_target(base, evil), evil)

    def test_safe_target_allows_normal(self):
        base = os.path.realpath(self.tmp)
        t = _safe_target(base, "shared_client/memories/a.md")
        self.assertIsNotNone(t)
        self.assertTrue(os.path.abspath(t).startswith(base))

    def test_prefix_confusion_not_allowed(self):
        """`/root_evil` 不应被误判为 `/root` 的子路径。"""
        base = os.path.realpath(os.path.join(self.tmp, "root"))
        os.makedirs(base, exist_ok=True)
        self.assertIsNone(_safe_target(base, "../root_evil/x.txt"))

    def test_malicious_zip_rejected(self):
        z = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../../pwned.txt", "bad")
        info = inspect_backup(z)
        self.assertTrue(info["unsafe"])
        with self.assertRaises(BackupError):
            import_backup(z, os.path.join(self.tmp, "dst"))

    def test_no_escape_on_disk(self):
        z = os.path.join(self.tmp, "evil2.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../../pwned2.txt", "bad")
        dst = os.path.join(self.tmp, "dst2")
        with self.assertRaises(BackupError):
            import_backup(z, dst)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned2.txt")))


# --------------------------------------------------------------------------- #
# 6. 校验
# --------------------------------------------------------------------------- #
class TestInspect(TempEnv):
    def test_inspect_reports_manifest(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        info = inspect_backup(z)
        self.assertTrue(info["has_manifest"])
        self.assertGreater(info["file_count"], 0)
        self.assertFalse(info["unsafe"])

    def test_manifest_not_counted_as_data(self):
        z = os.path.join(self.tmp, "b.zip")
        mf = export_backup(z, self.existing_items(), self.root)
        info = inspect_backup(z)
        self.assertEqual(info["file_count"], mf["file_count"])

    def test_missing_file(self):
        with self.assertRaises(BackupError):
            inspect_backup(os.path.join(self.tmp, "nope.zip"))

    def test_not_a_zip(self):
        p = os.path.join(self.tmp, "fake.zip")
        with open(p, "wb") as f:
            f.write(b"not a zip at all")
        with self.assertRaises(BackupError):
            inspect_backup(p)


# --------------------------------------------------------------------------- #
# 7. 导入 / 闭环
# --------------------------------------------------------------------------- #
class TestImport(TempEnv):
    def test_restores_to_correct_location(self):
        """核心回归：memories 必须落在 shared_client/memories，而非根下。"""
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "restored")
        import_backup(z, dst, make_rollback=False)

        good = os.path.join(dst, "shared_client", "memories", "global", "pref.md")
        wrong = os.path.join(dst, "memories", "global", "pref.md")
        self.assertTrue(os.path.exists(good), "记忆文件恢复位置错误")
        self.assertFalse(os.path.exists(wrong), "出现了旧版的错位路径")

    def test_full_roundtrip_content_identical(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "restored")
        import_backup(z, dst, make_rollback=False)

        for rel in (
            "settings.json",
            "shared_client/memories/global/pref.md",
            "shared_client/memories/projects/p1/note.md",
            "cache/projects/proj1/data.json",
        ):
            a = os.path.join(self.root, rel.replace("/", os.sep))
            b = os.path.join(dst, rel.replace("/", os.sep))
            self.assertTrue(os.path.exists(b), rel)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read(), rel)

    def test_rollback_snapshot_created(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        r = import_backup(z, self.root, make_rollback=True)
        self.assertIsNotNone(r["rollback"])
        self.assertTrue(os.path.exists(r["rollback"]))

    def test_rollback_can_restore_old_content(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        target = os.path.join(self.root, "settings.json")
        with open(target, "wb") as f:
            f.write(b'{"theme":"CHANGED"}')

        r = import_backup(z, self.root, make_rollback=True)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b'{"theme":"dark"}')

        # 用回滚快照还原被覆盖前的内容
        with zipfile.ZipFile(r["rollback"]) as zf:
            self.assertEqual(zf.read("settings.json"), b'{"theme":"CHANGED"}')

    def test_no_overwrite_mode(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        target = os.path.join(self.root, "settings.json")
        with open(target, "wb") as f:
            f.write(b"KEEP")
        r = import_backup(z, self.root, make_rollback=False, overwrite=False)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"KEEP")
        self.assertGreater(r["skipped"], 0)

    def test_import_into_empty_machine(self):
        """模拟迁移到全新电脑：目标目录完全不存在。"""
        z = os.path.join(self.tmp, "b.zip")
        mf = export_backup(z, self.existing_items(), self.root)
        fresh = os.path.join(self.tmp, "newpc", ".qoder-cn")
        r = import_backup(z, fresh, make_rollback=True)
        self.assertEqual(r["restored"], mf["file_count"])
        self.assertIsNone(r["rollback"])  # 无同名文件可回滚
        self.assertTrue(
            os.path.exists(os.path.join(fresh, "shared_client", "memories",
                                        "global", "pref.md"))
        )

    def test_unicode_paths(self):
        self._w("shared_client/memories/global/中文 记忆.md", "内容".encode("utf-8"))
        z = os.path.join(self.tmp, "u.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "u_restored")
        import_backup(z, dst, make_rollback=False)
        p = os.path.join(dst, "shared_client", "memories", "global", "中文 记忆.md")
        self.assertTrue(os.path.exists(p))
        with open(p, "rb") as f:
            self.assertEqual(f.read().decode("utf-8"), "内容")

    def test_progress_reported(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        seen = []
        import_backup(z, os.path.join(self.tmp, "r"), make_rollback=False,
                      progress=lambda i: seen.append(i))
        self.assertTrue(seen)

    def test_idempotent_double_import(self):
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "r")
        a = import_backup(z, dst, make_rollback=False)
        b = import_backup(z, dst, make_rollback=False)
        self.assertEqual(a["restored"], b["restored"])

    def test_restore_is_auto_extract_no_manual_unzip(self):
        """
        还原即「一键自动解压覆盖」：选定 zip 后，import_backup 直接把内容
        落地到目标目录，无需用户再手动解压或复制。验证 zip 内文件被直接
        写出到目标路径且内容正确。
        """
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "restored")

        # 目标目录初始不存在，模拟「全新还原」，且全程不经过任何解压工具
        r = import_backup(z, dst, make_rollback=False)
        self.assertEqual(r["restored"], r["restored"])  # 标量健全性
        self.assertGreater(r["restored"], 0)

        # zip 不再需要保留即可验证目标已完整还原
        out = os.path.join(dst, "shared_client", "memories", "global", "pref.md")
        self.assertTrue(os.path.exists(out))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"# memory\n")

    def test_restore_cleans_stray_wal_shm(self):
        """还原 SQLite 主库后，目标端残留的 -wal/-shm 应被清理，避免版本错乱。"""
        # 构造一个含真实 SQLite 主库的备份（用 export 内置快照生成）
        self._w("shared_client/cache/db/local.db", b"SQLite format 3\x00" + b"D" * 200)
        z = os.path.join(self.tmp, "b.zip")
        export_backup(z, self.existing_items(), self.root)

        dst = os.path.join(self.tmp, "restored")
        os.makedirs(os.path.join(dst, "shared_client", "cache", "db"), exist_ok=True)
        # 预先在目标端放置「旧的全家桶」：旧主库 + 旧 WAL + 旧 SHM
        old_db = os.path.join(dst, "shared_client", "cache", "db", "local.db")
        old_wal = old_db + "-wal"
        old_shm = old_db + "-shm"
        with open(old_db, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"OLD")
        with open(old_wal, "wb") as f:
            f.write(b"stale-wal")
        with open(old_shm, "wb") as f:
            f.write(b"stale-shm")

        import_backup(z, dst, make_rollback=False)

        # 主库已更新
        with open(old_db, "rb") as f:
            self.assertTrue(f.read().startswith(b"SQLite format 3"))
        # 残留的 WAL/SHM 已被清理（SQLite 会在下次打开时重建）
        self.assertFalse(os.path.exists(old_wal))
        self.assertFalse(os.path.exists(old_shm))


# --------------------------------------------------------------------------- #
# 8. 端到端迁移场景
# --------------------------------------------------------------------------- #
class TestMigrationScenario(TempEnv):
    def test_pc_to_pc_migration(self):
        """A 机导出 → B 机导入 → 数据、记忆、会话库全部可用。"""
        z = os.path.join(self.tmp, "migrate.zip")
        export_backup(z, self.existing_items(), self.root)

        pc_b = os.path.join(self.tmp, "pcB", ".qoder-cn")
        os.makedirs(os.path.join(pc_b, "shared_client"), exist_ok=True)
        result = import_backup(z, pc_b, make_rollback=False)
        self.assertGreater(result["restored"], 0)
        self.assertFalse(result["blocked"])

        # 记忆可读
        mem = os.path.join(pc_b, "shared_client", "memories", "global", "pref.md")
        with open(mem, "rb") as f:
            self.assertEqual(f.read(), b"# memory\n")

        # 会话库可查询
        db = os.path.join(pc_b, "shared_client", "cache", "db", "local.db")
        con = sqlite3.connect(db)
        rows = con.execute("select body from chat_message order by id limit 1").fetchone()
        con.close()
        self.assertEqual(rows[0], "消息 0")

        # 噪音未被带过去
        self.assertFalse(os.path.exists(os.path.join(pc_b, "shared_client", "cache", "db.7z")))


# --------------------------------------------------------------------------- #
# 9. GUI 线程安全（回归 "main thread is not in main loop"）
# --------------------------------------------------------------------------- #
class TestGuiThreadSafety(TempEnv):
    """
    后台线程一旦访问 Tk 控件/变量，就会抛
    ``RuntimeError: main thread is not in main loop``。
    这里通过监视 Tk 变量的读取线程来锁死该回归。
    """

    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover
            self.skipTest("环境无 tkinter")
        try:
            self.tk_root = tk.Tk()
        except tk.TclError:  # pragma: no cover
            self.skipTest("无图形环境，跳过 GUI 测试")
        self.tk_root.withdraw()
        self.addCleanup(self._destroy)

        from qoder_backup_core import QoderPaths
        import qoder_backup_tool as gui

        self.app = gui.QoderBackupApp(self.tk_root)
        # 指向测试目录，避免动用真实 Qoder 数据
        self.app.paths = QoderPaths(root=self.root, shared=self.shared)
        self.app.items = build_items(self.app.paths)
        self.app._refresh_items()
        self.tk_root.update_idletasks()

    def _destroy(self):
        try:
            self.app._closing = True
            self.app._cancel_after()
            self.tk_root.update_idletasks()
            self.tk_root.destroy()
        except Exception:
            pass

    # -- 工具：把 Tk 变量的 get() 包起来，记录调用线程 --
    def _watch_tk_vars(self) -> list[str]:
        import threading

        offenders: list[str] = []
        main_id = threading.main_thread().ident

        def wrap(var):
            orig = var.get

            def guarded():
                if threading.current_thread().ident != main_id:
                    offenders.append(
                        "%s 在后台线程 %s 被读取"
                        % (type(var).__name__, threading.current_thread().name)
                    )
                return orig()

            var.get = guarded

        wrap(self.app.skip_big)
        wrap(self.app.rollback_var)
        wrap(self.app.max_mb_var)
        for v in self.app.vars.values():
            wrap(v)
        return offenders

    def _run_until_idle(self, timeout: float = 30.0):
        import time

        end = time.time() + timeout
        while time.time() < end:
            self.tk_root.update()
            if not self.app.busy and self.app.msg_queue.empty():
                return True
            time.sleep(0.02)
        return False

    def test_estimate_never_touches_tk_from_thread(self):
        offenders = self._watch_tk_vars()
        self.app._estimate()
        self.assertTrue(self._run_until_idle(), "估算任务超时")
        self.assertEqual(offenders, [], "后台线程访问了 Tk 变量：%s" % offenders)

    def test_export_never_touches_tk_from_thread(self):
        offenders = self._watch_tk_vars()
        z = os.path.join(self.tmp, "gui.zip")

        # 绕过文件对话框，直接驱动 on_export 的后台部分
        import qoder_backup_tool as gui

        orig = gui.filedialog.asksaveasfilename
        gui.filedialog.asksaveasfilename = lambda **kw: z
        try:
            self.app.on_export()
            self.assertTrue(self._run_until_idle(), "导出任务超时")
        finally:
            gui.filedialog.asksaveasfilename = orig

        self.assertEqual(offenders, [], "后台线程访问了 Tk 变量：%s" % offenders)
        self.assertTrue(os.path.exists(z))

    def test_import_never_touches_tk_from_thread(self):
        z = os.path.join(self.tmp, "gui2.zip")
        export_backup(z, self.existing_items(), self.root)

        offenders = self._watch_tk_vars()
        import qoder_backup_tool as gui

        o1 = gui.filedialog.askopenfilename
        o2 = gui.messagebox.askyesno
        o3 = gui.messagebox.showinfo
        gui.filedialog.askopenfilename = lambda **kw: z
        gui.messagebox.askyesno = lambda *a, **k: True
        gui.messagebox.showinfo = lambda *a, **k: None
        try:
            self.app.on_import()
            self.assertTrue(self._run_until_idle(), "恢复任务超时")
        finally:
            gui.filedialog.askopenfilename = o1
            gui.messagebox.askyesno = o2
            gui.messagebox.showinfo = o3

        self.assertEqual(offenders, [], "后台线程访问了 Tk 变量：%s" % offenders)

    def test_worker_exception_surfaces_as_error_message(self):
        """后台异常必须转成队列消息，而不是让线程静默死掉。"""
        def boom():
            raise RuntimeError("模拟失败")

        self.app._run_bg(boom)
        import time

        end = time.time() + 5
        got = None
        while time.time() < end:
            self.tk_root.update_idletasks()
            if not self.app.msg_queue.empty():
                got = self.app.msg_queue.get_nowait()
                break
            time.sleep(0.02)
        self.assertIsNotNone(got, "未收到错误消息")
        self.assertEqual(got[0], "error")
        self.assertFalse(self.app.busy, "busy 标志未复位")

    def test_drain_queue_stops_after_closing(self):
        """标记关闭后不应再调度 after，避免访问已销毁的控件。"""
        self.app._closing = True
        self.app._drain_queue()  # 不应抛异常

    def test_max_mb_parsing(self):
        """超大文件阈值输入框的解析逻辑（仅主线程调用）。"""
        # 默认 200
        self.app.max_mb_var.set("200")
        self.app.skip_big.set(True)
        self.assertAlmostEqual(self.app._max_mb(), 200.0)

        # 用户自定义 50
        self.app.max_mb_var.set("50")
        self.assertAlmostEqual(self.app._max_mb(), 50.0)

        # 复选框关闭 -> 不限制
        self.app.skip_big.set(False)
        self.assertIsNone(self.app._max_mb())

        # 非法输入回退默认 200
        self.app.skip_big.set(True)
        self.app.max_mb_var.set("abc")
        self.assertAlmostEqual(self.app._max_mb(), 200.0)

        # 0 视为不限制
        self.app.max_mb_var.set("0")
        self.assertIsNone(self.app._max_mb())


if __name__ == "__main__":
    unittest.main(verbosity=2)
