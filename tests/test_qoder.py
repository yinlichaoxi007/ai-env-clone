"""
Qoder 备份迁移工具 —— 全面测试用例

运行：
    python -m unittest tests.test_qoder -v
或：
    python tests/test_qoder.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

from ai_env_clone.core import (
    MANIFEST_NAME,
    BackupError,
    BackupItem,
    DEFAULT_EXCLUDES,
    export_backup,
    import_backup,
    inspect_backup,
    is_critical,
    is_excluded,
    scan_items,
    snapshot_sqlite,
    safe_target,
)
from ai_env_clone.adapters.qoder import (
    QoderPaths,
    build_items,
    detect_qoder_root,
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
        self.uid = "13166325"  # 当前用户 UID
        self._w("settings.json", b'{"theme":"dark"}')
        # 用户级规则（根目录 rules/）
        self._w("rules/test.md", b"# test rule\n")
        # 记忆区：shared_client/memories/<uid>/{global,projects}
        self._w("shared_client/memories/%s/global/pref.md" % self.uid, b"# memory\n")
        self._w("shared_client/memories/%s/projects/p1/note.md" % self.uid, b"# note\n")
        # 记忆区：根 memories/<uid>/projects（IDE 活跃数据）
        self._w("memories/%s/projects/p1/ide.md" % self.uid, b"# ide\n")
        # 第二个用户（用于"其他用户记忆区"分支）
        self.other_uid = "99887766"
        self._w("shared_client/memories/%s/global/pref.md" % self.other_uid, b"# om\n")
        self._w("memories/%s/projects/p2/ide.md" % self.other_uid, b"# oide\n")
        # 项目级会话历史（IDE 写入）
        self._w("cache/projects/proj1/conversation-history/c1.jsonl", b"hello\n")
        self._w("shared_client/repowiki/wiki.md", b"# wiki\n")
        # 噪音文件，应被默认规则排除
        self._w("shared_client/cache/db.7z", b"A" * 2048)
        self._w("shared_client/cache/diagnosis.bin", b"B" * 2048)
        self._w("shared_client/logs/run.log", b"log")
        self._w("cache/projects/proj1/other.tmp", b"tmp")
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
        from ai_env_clone.adapters.qoder import QoderPaths

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
                "ai_env_clone.core").DEFAULT_EXCLUDES), bad)

    def test_keeps_real_data(self):
        for good in (
            "shared_client/memories/%s/global/pref.md" % self.uid,
            "settings.json",
            "shared_client/cache/db/local.db",
            "rules/test.md",
        ):
            self.assertFalse(is_excluded(good, __import__(
                "ai_env_clone.core").DEFAULT_EXCLUDES), good)

    def test_scan_filters_and_counts(self):
        r = scan_items(self.existing_items(), self.root)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/memories/%s/global/pref.md" % self.uid, rels)
        self.assertIn("settings.json", rels)
        self.assertIn("rules/test.md", rels)
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
        self.assertIn(
            "shared_client/memories/%s/projects/p1/note.md" % self.uid, rels
        )

    def test_no_duplicates_when_items_overlap(self):
        """cache/db 与 cache 目录重叠，不应重复打包。"""
        r = scan_items(self.existing_items(), self.root)
        rels = [rel for _, rel in r.files]
        self.assertEqual(len(rels), len(set(rels)))

    def test_max_file_size_limit(self):
        self._w("shared_client/memories/%s/big.md" % self.uid, b"Z" * 4096)
        small = scan_items(self.existing_items(), self.root, max_file_mb=0.001)
        big = scan_items(self.existing_items(), self.root, max_file_mb=None)
        self.assertLess(small.file_count, big.file_count)

    def test_missing_items_recorded(self):
        """build_items 中路径实际不存在的条目应被记为缺失（如 code_index 未生成）。"""
        r = scan_items(self.items(), self.root)
        # 测试脚手架未创建 shared_client/index，故 code_index 必缺失
        self.assertIn("code_index", r.missing_keys)

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
        self._w("shared_client/memories/%s/huge.md" % self.uid, b"Z" * (50 * 1024 * 1024))
        r = scan_items(self.existing_items(), self.root, max_file_mb=None)
        rels = {rel for _, rel in r.files}
        self.assertIn("shared_client/memories/%s/huge.md" % self.uid, rels)

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
        from ai_env_clone.adapters.qoder import QoderPaths, build_items
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
        from ai_env_clone.adapters.qoder import QoderPaths, build_items
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
            self.assertIsNone(safe_target(base, evil), evil)

    def test_safe_target_allows_normal(self):
        base = os.path.realpath(self.tmp)
        t = safe_target(base, "shared_client/memories/a.md")
        self.assertIsNotNone(t)
        self.assertTrue(os.path.abspath(t).startswith(base))

    def test_prefix_confusion_not_allowed(self):
        """`/root_evil` 不应被误判为 `/root` 的子路径。"""
        base = os.path.realpath(os.path.join(self.tmp, "root"))
        os.makedirs(base, exist_ok=True)
        self.assertIsNone(safe_target(base, "../root_evil/x.txt"))

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

        good = os.path.join(
            dst, "shared_client", "memories", self.uid, "global", "pref.md"
        )
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
            "shared_client/memories/%s/global/pref.md" % self.uid,
            "shared_client/memories/%s/projects/p1/note.md" % self.uid,
            "cache/projects/proj1/conversation-history/c1.jsonl",
            "rules/test.md",
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
                                        self.uid, "global", "pref.md"))
        )

    def test_unicode_paths(self):
        self._w("shared_client/memories/%s/中文 记忆.md" % self.uid, "内容".encode("utf-8"))
        z = os.path.join(self.tmp, "u.zip")
        export_backup(z, self.existing_items(), self.root)
        dst = os.path.join(self.tmp, "u_restored")
        import_backup(z, dst, make_rollback=False)
        p = os.path.join(dst, "shared_client", "memories", self.uid, "中文 记忆.md")
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
        out = os.path.join(dst, "shared_client", "memories", self.uid, "global", "pref.md")
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
        mem = os.path.join(pc_b, "shared_client", "memories", self.uid, "global", "pref.md")
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

        from ai_env_clone import __main__ as gui

        # 无头模式：隐藏导入流程中弹出的备份浏览器子窗口，避免测试闪窗。
        gui.HEADLESS = True
        self.addCleanup(lambda: setattr(gui, "HEADLESS", False))

        # 隔离用户偏好缓存：默认工具固定走注册序第一个（qoder），
        # 且不写真实用户缓存目录，保证测试确定性与不污染用户数据。
        self._load_patch = mock.patch.object(gui, "_load_last_tool", return_value=None)
        self._save_patch = mock.patch.object(gui, "_save_last_tool", return_value=None)
        self._load_patch.start()
        self._save_patch.start()
        self.addCleanup(self._save_patch.stop)
        self.addCleanup(self._load_patch.stop)

        self.app = gui.QoderBackupApp(self.tk_root)
        # 指向测试目录（主目录语义：.qoder-cn 在其下），避免动用真实 Qoder 数据
        self.app.root_dir = self.tmp
        # 显式指定当前用户，保证 memories_current 项存在且可测
        self.app.items = self.app.adapter.build_items(self.tmp, current_uid=self.uid)
        self.app._refresh_items()
        self.app._refresh_uid_combo()
        self.tk_root.update_idletasks()

        # 全程 mock 弹窗与文件对话框，避免无头测试期间弹出真实窗口
        from ai_env_clone import __main__ as gui
        self._orig_msg = dict(gui.messagebox.__dict__)
        self._orig_fd = dict(gui.filedialog.__dict__)
        for _n in ("showinfo", "showerror", "showwarning", "askyesno", "askquestion", "askokcancel"):
            setattr(gui.messagebox, _n, lambda *a, **k: None)
        # 文件对话框返回路径可由单个测试改写，避免局部 mock 还原竞态
        self._save_path = os.path.join(self.tmp, "gui_export.zip")
        self._open_path = os.path.join(self.tmp, "gui_import.zip")
        gui.filedialog.asksaveasfilename = lambda **k: self._save_path
        gui.filedialog.askopenfilename = lambda **k: self._open_path

    def tearDown(self):
        from ai_env_clone import __main__ as gui
        gui.messagebox.__dict__.update(self._orig_msg)
        gui.filedialog.__dict__.update(self._orig_fd)
        super().tearDown()

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
        self._save_path = z  # 文件对话框返回此路径（全局 mock，无需局部还原）

        self.app.on_export()
        self.assertTrue(self._run_until_idle(), "导出任务超时")

        self.assertEqual(offenders, [], "后台线程访问了 Tk 变量：%s" % offenders)
        self.assertTrue(os.path.exists(z))

    def test_import_never_touches_tk_from_thread(self):
        z = os.path.join(self.tmp, "gui2.zip")
        export_backup(z, self.existing_items(), self.root)
        self._open_path = z  # 文件对话框返回此路径（全局 mock，无需局部还原）

        offenders = self._watch_tk_vars()

        self.app.on_import()
        self.assertTrue(self._run_until_idle(), "恢复任务超时")

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

    # ----------------------------------------------------------------------- #
    # 无头交互：记忆区双分组 + 其他用户 UID 多选（不依赖真实显示）
    # ----------------------------------------------------------------------- #
    def test_backup_list_frame_lays_out_with_width(self):
        """布局断言：用 root.update() 驱动一次完整布局后，
        备份内容区（canvas 内嵌 list_frame）必须真正铺开宽度，
        否则双列会被压成一条竖线（肉眼看即'什么都没有'）。"""
        # 保持窗口 withdrawn，仅用 update_idletasks() 驱动几何计算（无需 deiconify，
        # 避免双屏下窗口落入第二屏可见区而闪窗）。
        self.tk_root.geometry("720x640")
        self.tk_root.update_idletasks()
        self.tk_root.after_idle(self.app._sync_canvas_width)
        self.tk_root.update_idletasks()
        width = self.app.list_frame.winfo_width()
        self.assertGreater(width, 200, "备份内容区宽度塌缩，双列不可见（实测 %dpx）" % width)
        # 每行由 [勾选+标题, 说明] 两个 grid 控件组成；验证首行左右均铺开且对齐
        rows = [w for w in self.app.list_frame.winfo_children()
                if w.winfo_class() == "TFrame" and w.winfo_children()]
        self.assertTrue(rows, "未生成任何备份项行")
        first = rows[0]
        cb, lbl = first.winfo_children()[:2]
        self.assertGreater(cb.winfo_width(), 20, "标题/勾选列未铺开")
        self.assertGreater(lbl.winfo_width(), 50, "说明列未铺开")

    def test_uid_combo_defaults_to_current_user(self):
        vals = list(self.app.uid_combo["values"])
        self.assertIn(self.uid, vals)
        self.assertIn(self.other_uid, vals)
        # 默认选中自动检测到的当前用户
        self.assertEqual(self.app.uid_var.get(), self.uid)

    def test_uid_combo_force_clear_when_dir_unrecognized(self):
        # 回归：目录未识别（冻结）时，即使 self.items 仍残留上一次有效的 UID，
        # 当前用户下拉也必须清空并禁用，不能残留旧用户名。
        self.assertIn(self.uid, self.app.uid_combo["values"])  # 前置：有效时本有值
        self.app._refresh_uid_combo(force_clear=True)
        self.assertEqual(self.app.uid_var.get(), "")
        self.assertEqual(str(self.app.uid_combo.cget("state")), "disabled")

    def test_redetect_invalid_path_clears_uid_combo(self):
        # 回归：目录未识别（冻结）时当前用户下拉必须清空禁用，
        # 即便 self.items 仍残留上一次有效的 UID（冻结分支不重建清单）。
        self.assertIn(self.uid, self.app.uid_combo["values"])
        self.app.root_var.set("C:/__no_such_qoder_dir__")
        self.app._redetect()
        self.assertEqual(self.app.uid_var.get(), "")
        self.assertEqual(str(self.app.uid_combo.cget("state")), "disabled")

    def test_selected_items_only_current_user_by_default(self):
        sel = self.app._selected_items()
        keys = {i.key for i in sel}
        # 默认只含"当前用户记忆区"（聚合前缀），不含任何"其他用户记忆区"项
        self.assertTrue(
            any(k.startswith("memories_current") for k in keys),
            "默认应含当前用户记忆区",
        )
        self.assertFalse(
            any(k.startswith("memories_others") for k in keys),
            "默认不应包含其他用户记忆区",
        )

    def test_others_block_hidden_until_checked(self):
        # 未勾选主项时，底部 UID 区块不应渲染出子项
        self.assertFalse(self.app.others_master_var.get())
        self.assertEqual(len(getattr(self.app, "other_vars", {})), 0)

        # 勾选主项后，应展开其余 UID 且默认全选
        self.app.others_master_var.set(True)
        self.app._on_others_toggled()
        self.assertIn(self.other_uid, self.app.other_vars)
        self.assertTrue(self.app.other_vars[self.other_uid].get())

    def test_others_uid_filter_applied_on_export(self):
        # 勾选其他用户主项，但只选 other_uid
        self.app.others_master_var.set(True)
        self.app._on_others_toggled()
        # 取消当前用户记忆区，仅保留其他用户
        self.app.current_var.set(False)
        sel = self.app._selected_items()
        others = [i for i in sel if i.key.startswith("memories_others")]
        self.assertTrue(others)
        self.assertTrue(all(i.uid == self.other_uid for i in others))
        self.assertFalse(any(i.key.startswith("memories_current") for i in sel))

    def test_uid_switch_rebuilds_memory_items(self):
        # 切换当前用户下拉为 other_uid，记忆区应重建
        self.app.uid_var.set(self.other_uid)
        self.app._on_uid_selected()
        sel = self.app._selected_items()
        cur = [i for i in sel if i.key.startswith("memories_current")]
        self.assertTrue(cur)
        self.assertEqual(cur[0].uid, self.other_uid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
