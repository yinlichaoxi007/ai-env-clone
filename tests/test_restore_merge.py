"""增量合并还原（``mode="merge"``）测试：契约 / 并集 / 幂等 / 不越界 / 原子回滚 / 清单-实体一致。

方案见 ``docs/local/还原增量合并方案.md``；核心实现：``merge_plan``（纯计算）、
``core._run_merge_restore``（两阶段 + 失败整体回滚）、各适配器的 ``RESTORE_POLICY``。
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile

from ai_env_clone import merge_plan
from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.core import MANIFEST_NAME, BackupError, import_backup


ADAPTERS_MERGE_SUPPORTED = ("workbuddy", "codebuddy", "dsh", "reasonix")
ADAPTERS_NO_MERGE = ("qoder", "trae-cn", "trae-solo-cn", "zcode")


def _manifest(tool, **extra):
    mf = {
        "version": 2, "kind": "backup", "tool": tool,
        "created_at": "2026-10-04T00:00:00", "source_root": "C:/src",
        "platform": os.name, "items": [], "file_count": 1, "total_bytes": 1,
        "bytes_by_ext": {}, "bytes_by_ext_compressed": {},
    }
    mf.update(extra)
    return mf


class TestMergeContract(unittest.TestCase):
    """声明与实现必须一致（方案 §8 契约测试）。"""

    def test_policy_values_are_known(self):
        for name in list_adapters():
            ad = get_adapter(name)
            for suffix, policy in ad.RESTORE_POLICY.items():
                self.assertIn(policy, merge_plan.POLICIES,
                              "%s 的 %s 策略非法：%r" % (name, suffix, policy))

    def test_merge_policy_requires_hook(self):
        """声明了 ``merge`` 却没实现 restore_merge_target() ⇒ 契约失败。"""
        for name in list_adapters():
            ad = get_adapter(name)
            modes = set(ad.RESTORE_POLICY.values()) | {ad.RESTORE_MERGE_DEFAULT}
            if merge_plan.MERGE in modes:
                self.assertIsNotNone(ad.restore_merge_target(),
                                     "%s 声明了 merge 但未实现恢复钩子" % name)
                self.assertTrue(ad.restore_merge_supported)

    def test_support_matrix(self):
        for name in ADAPTERS_MERGE_SUPPORTED:
            self.assertTrue(get_adapter(name).restore_merge_supported, name)
        for name in ADAPTERS_NO_MERGE:
            self.assertFalse(get_adapter(name).restore_merge_supported, name)


class TestUnionJsonById(unittest.TestCase):
    """清单 / 索引并集：本机优先，按 id 补入包内独有项（方案 §3.4）。"""

    def test_local_first_then_source_only(self):
        local = {"messages": [{"id": "a"}, {"id": "c"}], "keep": 1}
        source = {"messages": [{"id": "a"}, {"id": "b"}], "extra": 2}
        out = json.loads(merge_plan.union_json_by_id(
            json.dumps(source).encode(), json.dumps(local).encode()).decode())
        ids = [m["id"] for m in out["messages"]]
        self.assertEqual(ids, ["a", "c", "b"])   # 本机在前、包内独有补入
        self.assertEqual(out["keep"], 1)
        self.assertEqual(out["extra"], 2)        # 本机缺的键补入

    def test_idempotent(self):
        local = {"messages": [{"id": "a"}, {"id": "b"}]}
        source = {"messages": [{"id": "a"}, {"id": "b"}]}
        once = merge_plan.union_json_by_id(
            json.dumps(source).encode(), json.dumps(local).encode())
        twice = merge_plan.union_json_by_id(source_bytes=json.dumps(source).encode(),
                                            local_bytes=once)
        self.assertEqual(once, twice)

    def test_bad_local_falls_back_to_source(self):
        src = json.dumps({"messages": [{"id": "x"}]}).encode()
        self.assertEqual(merge_plan.union_json_by_id(src, b"not json"), src)

    def test_bad_source_keeps_local(self):
        loc = json.dumps({"messages": [{"id": "x"}]}).encode()
        self.assertEqual(merge_plan.union_json_by_id(b"not json", loc), loc)


def _mk_db(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE workspaces(path TEXT PRIMARY KEY, last_opened_at INTEGER)")
    conn.execute("CREATE TABLE session_usage(session_id TEXT PRIMARY KEY, tokens INTEGER)")
    conn.execute("CREATE TABLE migration_meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE app_state(id INTEGER PRIMARY KEY, blob TEXT)")
    for table, values in rows.items():
        conn.executemany(
            "INSERT INTO %s VALUES (%s)" % (table, ", ".join("?" * len(values[0]))),
            values)
    conn.commit()
    conn.close()


class TestMergeSqliteTables(unittest.TestCase):
    """WorkBuddy 逐表并入：白名单内并集、白名单外**逐字段不变**、幂等（方案 §3.5 / §4）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, ".workbuddy", "workbuddy.db")
        self.source = os.path.join(self.tmp, "src", "workbuddy.db")
        _mk_db(self.target, {
            "sessions": [("s1", "本机标题"), ("s3", "本机独有")],
            "workspaces": [("w1", 50)],
            "session_usage": [("su1", 10)],
            "migration_meta": [("m", "local")],
            "app_state": [(1, "L")],
        })
        _mk_db(self.source, {
            "sessions": [("s1", "包内标题"), ("s2", "包内独有")],
            "workspaces": [("w1", 100), ("w2", 200)],
            "session_usage": [("su1", 99), ("su2", 20)],
            "migration_meta": [("m", "src")],
            "app_state": [(1, "S"), (2, "S2")],
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, table, order="rowid"):
        conn = sqlite3.connect(self.target)
        try:
            return conn.execute("SELECT * FROM %s ORDER BY %s" % (table, order)).fetchall()
        finally:
            conn.close()

    def _merge(self):
        with open(self.source, "rb") as fh:
            return merge_plan.merge_workbuddy_db(self.target, fh.read())

    def test_union_and_local_priority(self):
        self._merge()
        self.assertEqual(sorted(self._rows("sessions")),
                         [("s1", "本机标题"), ("s2", "包内独有"), ("s3", "本机独有")])
        self.assertEqual(sorted(self._rows("workspaces")), [("w1", 100), ("w2", 200)])
        self.assertEqual(sorted(self._rows("session_usage")), [("su1", 10), ("su2", 20)])

    def test_whitelist_only_outside_tables_untouched(self):
        self._merge()
        self.assertEqual(self._rows("migration_meta"), [("m", "local")])
        self.assertEqual(self._rows("app_state"), [(1, "L")])

    def test_idempotent_second_merge_adds_nothing(self):
        self._merge()
        before = {t: self._rows(t) for t in
                  ("sessions", "workspaces", "session_usage", "migration_meta", "app_state")}
        report = self._merge()
        after = {t: self._rows(t) for t in before}
        self.assertEqual(before, after)
        self.assertEqual(report["sessions"]["rows"], 0)
        self.assertEqual(report["session_usage"]["rows"], 0)

    def test_schema_drift_skips_columns_without_failing(self):
        """源库多列 ⇒ 跳过该列并报告，不得抛异常、不得写坏目标库（方案 §5 风险 3）。"""
        tgt = os.path.join(self.tmp, "drift", "workbuddy.db")
        src = os.path.join(self.tmp, "drift_src.db")
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        c = sqlite3.connect(tgt)
        c.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)")
        c.execute("INSERT INTO sessions VALUES ('s1', '本机')")
        c.commit(); c.close()
        s = sqlite3.connect(src)
        s.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, extra TEXT)")
        s.execute("INSERT INTO sessions VALUES ('s2', '包内', '新列')")
        s.commit(); s.close()

        with open(src, "rb") as fh:
            report = merge_plan.merge_sqlite_tables(tgt, fh.read(),
                                                    merge_plan.WORKBUDDY_MERGE_TABLES)
        conn = sqlite3.connect(tgt)
        rows = conn.execute("SELECT id, title FROM sessions ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(rows, [("s1", "本机"), ("s2", "包内")])
        self.assertIn("extra", report["sessions"]["skipped_columns"])
        self.assertEqual(report["workspaces"]["skipped"], "目标库无此表")


class TestCodebuddyMergeRoundtrip(unittest.TestCase):
    """core 全链路：``mode="merge"`` 下的清单并集 / keep_local / 清单-实体一致 / 幂等。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        self.ad = get_adapter("codebuddy")
        self.rel_dir = "CodeBuddyExtension/Data/u/CodeBuddyIDE/u/history/h"
        self.abs_dir = os.path.join(self.root, *self.rel_dir.split("/"))
        os.makedirs(self.abs_dir, exist_ok=True)
        self.zip_path = os.path.join(self.tmp, "cb.zip")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_local_index(self, ids):
        with open(os.path.join(self.abs_dir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"messages": [{"id": i} for i in ids]}, fh)

    def _make_zip(self, ids, msg_files):
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(_manifest("codebuddy"), ensure_ascii=False))
            zf.writestr(self.rel_dir + "/index.json",
                        json.dumps({"messages": [{"id": i} for i in ids]}))
            for name, content in msg_files.items():
                zf.writestr(self.rel_dir + "/messages/" + name, content)

    def _merge(self, **kw):
        return import_backup(
            self.zip_path, self.root, mode="merge",
            restore_policy_for=self.ad.restore_policy_for,
            restore_merge_target=self.ad.restore_merge_target(),
            **kw)

    def _index_ids(self):
        with open(os.path.join(self.abs_dir, "index.json"), encoding="utf-8") as fh:
            return [m["id"] for m in json.load(fh)["messages"]]

    def test_union_and_consistency(self):
        self._write_local_index(["m_local"])
        os.makedirs(os.path.join(self.abs_dir, "messages"), exist_ok=True)
        with open(os.path.join(self.abs_dir, "messages", "m_local.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        self._make_zip(["m_local", "m_src"], {"m_src.json": "{}"})
        self._merge()
        # 清单并集：本机 + 包内独有
        self.assertEqual(self._index_ids(), ["m_local", "m_src"])
        # 清单-实体一致：清单里的每个 id 都有实体文件，磁盘上没有孤儿
        disk = {n[:-5] for n in os.listdir(os.path.join(self.abs_dir, "messages"))
                if n.endswith(".json")}
        self.assertEqual(set(self._index_ids()), disk)

    def test_keep_local_does_not_overwrite_existing(self):
        self._write_local_index(["m_local"])
        local_msg = os.path.join(self.abs_dir, "messages")
        os.makedirs(local_msg, exist_ok=True)
        with open(os.path.join(local_msg, "m_local.json"), "w", encoding="utf-8") as fh:
            fh.write("LOCAL")
        self._make_zip(["m_src"], {"m_local.json": "FROM_SOURCE", "m_src.json": "{}"})
        r = self._merge()
        with open(os.path.join(local_msg, "m_local.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "LOCAL")     # 本机已有 ⇒ 不动
        self.assertTrue(os.path.isfile(os.path.join(local_msg, "m_src.json")))  # 缺的补入
        self.assertGreaterEqual(r["skipped"], 1)

    def test_idempotent_second_merge(self):
        self._write_local_index(["m_local"])
        self._make_zip(["m_local", "m_src"], {"m_src.json": "{}"})
        self._merge()
        index_path = os.path.join(self.abs_dir, "index.json")
        with open(index_path, "rb") as fh:
            index_after_first = fh.read()
        r2 = self._merge()
        with open(index_path, "rb") as fh:
            self.assertEqual(fh.read(), index_after_first)
        self.assertEqual(self._index_ids(), ["m_local", "m_src"])
        self.assertGreaterEqual(r2["skipped"], 1)     # 实体已存在 ⇒ keep_local 跳过


class TestMergeAtomicRollback(unittest.TestCase):
    """★ 原子性：阶段 3 中途失败 ⇒ 本机所有目标与合并前逐字段相同，且报错含「已回滚」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root, exist_ok=True)
        self.rollback_dir = os.path.join(self.tmp, "rb")
        self.zip_path = os.path.join(self.tmp, "m.zip")
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(_manifest("codebuddy")))
            zf.writestr("a.txt", "NEW-A")
            zf.writestr("b.txt", "NEW-B")
            zf.writestr("c.txt", "NEW-C")
        # 本机 a.txt 已存在（会被合并钩子覆盖），b/c 为新文件
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("OLD-A")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failure_rolls_back_everything(self):
        def hook(relpath, target, source_bytes):
            if relpath.endswith("b.txt"):
                raise RuntimeError("注入的写入失败")
            with open(target, "wb") as fh:
                fh.write(source_bytes)

        with self.assertRaises(BackupError) as ctx:
            import_backup(
                self.zip_path, self.root, mode="merge",
                make_rollback=True, rollback_dir=self.rollback_dir,
                restore_policy_for=lambda _rel: merge_plan.MERGE,
                restore_merge_target=hook,
            )
        self.assertIn("已回滚", str(ctx.exception))
        # a.txt 回到合并前；b/c 新建文件被清除
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "OLD-A")
        self.assertFalse(os.path.exists(os.path.join(self.root, "b.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "c.txt")))


if __name__ == "__main__":
    unittest.main()
