"""「会话 -> 原始工作区路径」映射：生成、随包携带、还原后回读、详情展示。

背景（用户 2026-10-01 提出）：CodeBuddy 的会话目录名是 ``md5(项目路径)``，**不可逆**；
而 WorkBuddy / DSH 的落点**必须是路径**。跨机还原后若没有映射，会话只能落到工具默认
目录（``~/WorkBuddy/<时间戳>``）—— 用户既拿不回原位，也看不到「它原本属于哪里」。
故导出时把映射**显式落一份**（随包携带 + 本机留一份），还原后据此归位。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import core  # noqa: E402
from ai_env_clone import session_migration as sm  # noqa: E402
from ai_env_clone import workspace_plan as wp  # noqa: E402
from ai_env_clone.adapters import codebuddy as cb  # noqa: E402


def _write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _Env:
    """临时 CodeBuddy 会话根：``<tmp>/CodeBuddyIDE/<uid>/history/<wid>/<sid>/``。"""

    UID = "9ae9129b-c0e9-4158-b4cd-983fac049c6d"

    def __init__(self, test: unittest.TestCase):
        self.tmp = tempfile.mkdtemp(prefix="wsmap_")
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        test.addCleanup(wp._BACKUP_MAP_CACHE.clear)
        self.sroot = os.path.join(self.tmp, "CodeBuddyIDE", self.UID)
        self.hist = os.path.join(self.sroot, "history")

    def add_session(self, wid: str, sid: str, literals=()) -> str:
        """建一条会话；``literals`` 作为字面量写进 ``messages/*.json``（模拟正文里的路径）。"""
        d = os.path.join(self.hist, wid, sid)
        os.makedirs(os.path.join(d, "messages"), exist_ok=True)
        _write(os.path.join(d, "index.json"), '{"title": "T"}')
        # 每条路径写成独立字段，避免被当成同一个字符串
        body = ", ".join('"p%d": "%s"' % (i, p) for i, p in enumerate(literals))
        _write(os.path.join(d, "messages", "a.json"), "{%s}" % body)
        return d

    def write_map(self, data: dict) -> str:
        p = cb.session_workspace_map_path(self.sroot)
        _write(p, json.dumps(data, ensure_ascii=False))
        wp._BACKUP_MAP_CACHE.clear()
        return p


class TestBuildSessionWorkspaceMap(unittest.TestCase):
    def test_ide_record_wins_over_content(self):
        """IDE「已打开文件夹」记录是结构化映射，优先于正文推断。"""
        env = _Env(self)
        proj = r"D:\project\Demo"
        wid = wp.codebuddy_workspace_id(proj)
        env.add_session(wid, "s1", [r"d:\\project\\Other\\x.py"])
        m = cb.build_session_workspace_map(env.sroot, ide_paths=[proj])
        entry = m["workspaces"][wid]
        self.assertEqual(entry["path"], proj)
        self.assertEqual(entry["source"], "ide_record")
        self.assertEqual(m["stats"], {"total": 1, "resolved": 1, "unresolved": 0})

    def test_content_hash_recovers_path(self):
        """无 IDE 记录时，从会话正文挖候选 + md5 校验还原（可证明，不是猜）。"""
        env = _Env(self)
        proj = r"D:\project\Demo"
        wid = wp.codebuddy_workspace_id(proj)
        env.add_session(wid, "s1", [r"d:\\project\\Demo\\src\\x.py"])
        m = cb.build_session_workspace_map(env.sroot, ide_paths=[])
        entry = m["workspaces"][wid]
        self.assertEqual(entry["source"], "content_hash")
        self.assertEqual(entry["path"].lower(), proj.lower())

    def test_unresolved_keeps_candidates(self):
        """推不出路径时也要留下正文候选 —— 至少能告诉用户「它原本可能在哪」。"""
        env = _Env(self)
        wid = "0" * 32                      # 本机注定对不上的 id
        env.add_session(wid, "s1", [
            r"d:\\project\\Demo\\src\\a.py",
            r"d:\\project\\Demo\\src\\b.py",
            r"d:\\project\\Demo\\c.py",
        ])
        m = cb.build_session_workspace_map(env.sroot, ide_paths=[])
        entry = m["workspaces"][wid]
        self.assertEqual(entry["path"], "")
        self.assertEqual(entry["source"], "")
        self.assertTrue(entry.get("candidates"))
        self.assertEqual(m["stats"]["unresolved"], 1)

    def test_no_history_yields_empty(self):
        d = tempfile.mkdtemp(prefix="wsmap_empty_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        m = cb.build_session_workspace_map(d, ide_paths=[])
        self.assertEqual(m["stats"]["total"], 0)
        self.assertEqual(m["workspaces"], {})

    def test_load_is_silent_on_missing_or_broken(self):
        d = tempfile.mkdtemp(prefix="wsmap_bad_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertEqual(cb.load_session_workspace_map(d), {})
        _write(cb.session_workspace_map_path(d), "{ not json")
        self.assertEqual(cb.load_session_workspace_map(d), {})
        _write(cb.session_workspace_map_path(d), "[1, 2]")
        self.assertEqual(cb.load_session_workspace_map(d), {})


class TestExtraFilesInArchive(unittest.TestCase):
    """core 支持把「适配器生成的文件」写进包，且还原时落回原位。"""

    def _export(self, env, extra_files, extra_meta=None):
        rules = os.path.join(env.tmp, "rules")
        _write(os.path.join(rules, "a.mdc"), "x")
        item = core.BackupItem(key="user_rules", label="R", path=rules,
                               description="d")
        zp = os.path.join(env.tmp, "b.zip")
        mf = core.export_backup(zp, [item], env.tmp, tool_name="codebuddy",
                                extra_files=extra_files, extra_meta=extra_meta,
                                progress=lambda _x: None)
        return zp, mf

    def test_written_into_zip_counted_and_recorded(self):
        env = _Env(self)
        dest = cb.session_workspace_map_path(env.sroot)
        zp, mf = self._export(env, {dest: b'{"a": 1}'}, {"k": "v"})
        rel = os.path.relpath(dest, env.tmp).replace(os.sep, "/")
        with zipfile.ZipFile(zp) as zf:
            self.assertIn(rel, zf.namelist())
            self.assertEqual(zf.read(rel), b'{"a": 1}')
        self.assertEqual(mf["generated"], [rel])
        self.assertEqual(mf["extra"], {"k": "v"})
        self.assertEqual(mf["file_count"], 2)          # 1 个真实文件 + 1 个生成文件
        self.assertEqual(mf["bytes_by_ext"][".json"], len(b'{"a": 1}'))

    def test_restore_puts_generated_file_back(self):
        env = _Env(self)
        dest = cb.session_workspace_map_path(env.sroot)
        zp, _mf = self._export(env, {dest: b'{"a": 1}'})
        target = os.path.join(env.tmp, "restored")
        os.makedirs(target, exist_ok=True)
        core.import_backup(zp, target, make_rollback=False, overwrite=True)
        back = os.path.join(target, os.path.relpath(dest, env.tmp))
        self.assertTrue(os.path.isfile(back))
        with open(back, "rb") as f:
            self.assertEqual(f.read(), b'{"a": 1}')

    def test_path_outside_root_is_skipped(self):
        """根目录之外的目标路径不入包（防越界），但备份本身照常成功。"""
        env = _Env(self)
        outside = os.path.join(os.path.dirname(env.tmp), "evil.json")
        zp, mf = self._export(env, {outside: b"x"})
        self.assertNotIn("generated", mf)
        self.assertEqual(mf["file_count"], 1)


class TestAdapterGeneratesMap(unittest.TestCase):
    """适配器只在勾选了集中会话条目时才生成映射（否则毫无意义）。"""

    def _adapter(self):
        from ai_env_clone.adapters import get_adapter
        return get_adapter("codebuddy")

    def test_skips_when_no_session_items(self):
        ad = self._adapter()
        item = core.BackupItem(key="user_rules", label="R", path="x",
                               description="d")
        files, meta = ad.export_generated([item], os.path.expanduser("~"))
        self.assertIsNone(files)
        self.assertIsNone(meta)

    def test_generates_and_persists_when_session_items_present(self):
        env = _Env(self)
        proj = r"D:\project\Demo"
        wid = wp.codebuddy_workspace_id(proj)
        env.add_session(wid, "s1", [])
        ip = cb.CodeBuddyAdapter
        item = core.BackupItem(key="user_sessions:history", label="H",
                               path=env.hist, description="d")
        with mock.patch.object(cb, "detect_current_uid",
                                        return_value=env.UID), \
             mock.patch.object(cb, "detect_session_root",
                                        return_value=env.sroot):
            files, meta = ip().export_generated([item], env.tmp)
        self.assertEqual(list(files), [cb.session_workspace_map_path(env.sroot)])
        self.assertEqual(meta["session_workspaces"]["total"], 1)
        # 同时落一份到磁盘，供「导出后立刻导入」使用
        self.assertTrue(os.path.isfile(cb.session_workspace_map_path(env.sroot)))


class TestRecoverPrefersBackupMap(unittest.TestCase):
    """落点还原：备份包映射 > 本机 IDE 记录 > 正文挖掘。"""

    def test_recover_uses_backup_map(self):
        env = _Env(self)
        proj = r"D:\project\Demo"
        wid = wp.codebuddy_workspace_id(proj)
        sd = env.add_session(wid, "s1", [])
        env.write_map({"version": 1, "workspaces": {
            wid: {"path": proj, "source": "ide_record"}}})
        path, how = wp.codebuddy_recover_workspace_path(wid, sd)
        self.assertEqual(path, proj)
        self.assertIn("备份包", how)

    def test_backup_map_falls_back_to_default_root(self):
        """调用方给的会话根不对（如只给了 history 根）时，回退到默认会话根再试。"""
        env = _Env(self)
        wid = "a" * 32
        env.write_map({"version": 1, "workspaces": {
            wid: {"path": r"D:\p", "source": "ide_record"}}})
        with mock.patch.object(cb, "detect_current_uid",
                                        return_value=env.UID), \
             mock.patch.object(cb, "detect_session_root",
                                        return_value=env.sroot):
            wp._BACKUP_MAP_CACHE.clear()
            got = wp.codebuddy_backup_workspace_map(session_root=env.hist)
        self.assertEqual((got.get("workspaces") or {}).get(wid, {}).get("path"),
                         r"D:\p")


class TestScanCarriesCandidates(unittest.TestCase):
    """扫描出的条目要带上映射里的候选路径，界面才能提醒用户。"""

    def test_candidates_copied_to_item(self):
        env = _Env(self)
        wid = "b" * 32
        env.add_session(wid, "s1", [])
        env.write_map({"version": 1, "workspaces": {
            wid: {"path": "", "source": "", "candidates": [r"D:\a", r"D:\b"]}}})
        items = sm.list_source_sessions("codebuddy", env.hist)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cwd"], "")
        self.assertEqual(items[0]["workspace_candidates"], [r"D:\a", r"D:\b"])

    def test_resolved_map_fills_cwd(self):
        env = _Env(self)
        proj = r"D:\project\Demo"
        wid = wp.codebuddy_workspace_id(proj)
        env.add_session(wid, "s1", [])
        env.write_map({"version": 1, "workspaces": {
            wid: {"path": proj, "source": "ide_record"}}})
        items = sm.list_source_sessions("codebuddy", env.hist)
        self.assertEqual(items[0]["cwd"], proj)
        self.assertNotIn("workspace_candidates", items[0])
        pl = wp.plan_for("workbuddy", items[0], "", root=env.tmp)
        self.assertEqual(pl.mode, wp.MODE_SESSION)
        self.assertEqual(pl.value, proj)


class TestDetailLines(unittest.TestCase):
    def test_renders_summary(self):
        mf = {"extra": {"session_workspaces": {
            "file": "session-workspaces.json",
            "total": 21, "resolved": 1, "unresolved": 20}}}
        lines = core.session_workspaces_lines(mf)
        text = "\n".join(lines)
        self.assertIn("21", text)
        self.assertIn("20", text)
        self.assertIn("分享", text)          # 提醒「分享前留意」
        self.assertIn("session-workspaces.json", text)

    def test_old_or_empty_package_is_silent(self):
        self.assertEqual(core.session_workspaces_lines({}), [])
        self.assertEqual(core.session_workspaces_lines({"extra": {}}), [])
        self.assertEqual(core.session_workspaces_lines(
            {"extra": {"session_workspaces": {"total": 0}}}), [])
        self.assertEqual(core.session_workspaces_lines(None), [])


if __name__ == "__main__":
    unittest.main()
