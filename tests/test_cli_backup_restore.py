"""命令行入口（``tools`` / ``inspect`` / ``backup`` / ``restore``）的行为测试。

关注三点：
1. **不依赖 tkinter**：命令行分支必须能在没装 GUI 库的环境跑起来（与 ``--version``
   / ``--docs`` 同一约定），否则 CI / 服务器场景就只能开界面。
2. **危险动作要显式确认**：含不可脱敏原文的备份、以及任何写盘的还原，缺 ``--yes``
   一律**不执行**并以退出码 2 结束（「忘了加参数」应失败，不能静默做完危险的事）。
3. **与 GUI 同一套实质逻辑**：同一个适配器、同一份导出脱敏、同一条「包必须属于
   本工具」的还原闸门——命令行不允许比界面少任何一道检查。

全程在临时目录里跑，不触碰本机真实工具数据。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import cli  # noqa: E402
from ai_env_clone.core import (  # noqa: E402
    BackupItem,
    TRANSCRIPT_PLAINTEXT_NOTE,
    inspect_backup,
)


def _item(key: str, label: str = "", *, recommended: bool = True,
           note: str = "") -> BackupItem:
    return BackupItem(key=key, label=label or key,
                      path=os.path.join(tempfile.gettempdir(), key),
                      description="", recommended=recommended,
                      pre_export_notice=note)


class Collector:
    """``emit`` 的替身：收行，顺带断言措辞。"""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
# 1) 不依赖 tkinter
# --------------------------------------------------------------------------- #
class TestNoGuiDependency(unittest.TestCase):
    def test_cli_imports_without_tkinter(self):
        """``import ai_env_clone.cli`` 不得拖进 tkinter（子进程里让 tkinter 导入即失败）。"""
        code = (
            "import sys\n"
            "sys.modules['tkinter'] = None\n"
            "import ai_env_clone.cli\n"
            "print('ok')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("ok", r.stdout)

    def test_dispatch_only_handles_known_subcommands(self):
        """未命中子命令时必须返回 None——否则裸 ``python -m ai_env_clone`` 开不了界面。"""
        self.assertIsNone(cli.handle_backup_restore_flags([]))
        self.assertIsNone(cli.handle_backup_restore_flags(["--version"]))
        self.assertIsNone(cli.handle_backup_restore_flags(["bogus"]))
        with mock.patch("builtins.print"):   # 命中时真的会执行并输出，别把结果打进测试日志
            self.assertIsNotNone(cli.handle_backup_restore_flags(["tools"]))

    def test_subcommands_cover_cli_surface(self):
        self.assertEqual(set(cli.SUBCOMMANDS),
                         {"tools", "inspect", "backup", "restore"})

    def test_windowed_exe_without_console_refuses_to_act(self):
        """打包版是 ``--windowed``（``sys.stdout`` 为 None）：所有结论都无处可去。

        备份 / 还原都要人看清结果才知道下一步，所以此时**不静默执行**，直接失败退出。
        """
        with mock.patch.object(sys, "stdout", None):
            self.assertEqual(cli.handle_backup_restore_flags(["tools"]), cli.EXIT_ERROR)
            # 但没命中子命令时仍然放行——界面照常启动，与控制台无关
            self.assertIsNone(cli.handle_backup_restore_flags([]))


# --------------------------------------------------------------------------- #
# 2) 纯函数：条目挑选与文案
# --------------------------------------------------------------------------- #
class TestPureHelpers(unittest.TestCase):
    def test_parse_item_tokens_splits_and_ignores_blanks(self):
        """``--items`` 是**一个字符串**：按逗号/空格切开；按字符迭代会把每个字母
        都当成一个标识去匹配（真实回归：曾报「无法识别的：m、e、m、o、r、y」）。"""
        self.assertEqual(cli.parse_item_tokens("session_db,projects"),
                         ["session_db", "projects"])
        self.assertEqual(cli.parse_item_tokens(" memory , models "), ["memory", "models"])
        self.assertEqual(cli.parse_item_tokens(""), [])
        self.assertEqual(cli.parse_item_tokens(None), [])

    def test_default_selection_is_full_backup_scope(self):
        """不给 --items ⇒ 完整备份口径＝默认勾选，且不含「其他用户记忆」聚合项。"""
        items = [_item("session_db"), _item("plugins", recommended=False),
                 _item("memories_current"), _item("memories_others:u1")]
        picked, unknown = cli.select_items(items, [])
        self.assertEqual([i.key for i in picked], ["session_db", "memories_current"])
        self.assertEqual(unknown, [])

    def test_prefix_selects_all_rows_of_one_logical_item(self):
        """聚合前缀一次带走主库与 -wal / -shm（它们是同一逻辑项的多行）。"""
        items = [_item("session_db"), _item("session_db:wal"), _item("session_db:shm"),
                 _item("memory")]
        picked, _ = cli.select_items(items, ["session_db"])
        self.assertEqual([i.key for i in picked],
                         ["session_db", "session_db:wal", "session_db:shm"])

    def test_full_key_selects_only_that_row(self):
        items = [_item("session_db"), _item("session_db:wal")]
        picked, _ = cli.select_items(items, ["session_db:wal"])
        self.assertEqual([i.key for i in picked], ["session_db:wal"])

    def test_unknown_tokens_are_reported_not_silently_dropped(self):
        items = [_item("memory")]
        picked, unknown = cli.select_items(items, ["memory", "nope"])
        self.assertEqual([i.key for i in picked], ["memory"])
        self.assertEqual(unknown, ["nope"])

    def test_backup_plan_marks_unredactable_and_how_to_proceed(self):
        lines = cli.backup_plan_lines([_item("projects", note=TRANSCRIPT_PLAINTEXT_NOTE)],
                                      [TRANSCRIPT_PLAINTEXT_NOTE])
        text = "\n".join(lines)
        self.assertIn(TRANSCRIPT_PLAINTEXT_NOTE, text)
        self.assertIn("--yes", text)

    def test_backup_plan_flags_missing_on_this_machine(self):
        """本机没有的项要当场标出来——不然用户会以为包里含它。"""
        it = _item("memory")
        it.path = os.path.join(tempfile.gettempdir(), "definitely-missing-item")
        text = "\n".join(cli.backup_plan_lines([it], []))
        self.assertIn("本机未找到", text)

    def test_restore_plan_states_mode_target_and_confirmation(self):
        from ai_env_clone.adapters.workbuddy import WorkBuddyAdapter
        info = {"file_count": 3, "total_bytes": 1024, "manifest": {}}
        lines = cli.restore_plan_lines(WorkBuddyAdapter(), info,
                                       r"C:\tmp\root", "merge", r"C:\tmp\a.zip")
        text = "\n".join(lines)
        self.assertIn("合并", text)
        self.assertIn(r"C:\tmp\root", text)
        self.assertIn("--yes", text)


# --------------------------------------------------------------------------- #
# 3) 端到端：临时目录里的备份 / 还原
# --------------------------------------------------------------------------- #
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli_e2e_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.src_root = os.path.join(self.tmp, "src")
        self.dst_root = os.path.join(self.tmp, "dst")
        os.makedirs(self.src_root)
        os.makedirs(self.dst_root)

        # WorkBuddy 的条目路径由 _workbuddy_home() 决定；把它指到临时目录，
        # 于是「真实适配器 + 真实 core + 真实落盘规则」全部照常参与，只换根。
        self.wb_home = os.path.join(self.src_root, ".workbuddy")
        for sub in ("memory", "projects"):
            os.makedirs(os.path.join(self.wb_home, sub), exist_ok=True)
        with open(os.path.join(self.wb_home, "memory", "uid_memory.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# 记忆\n用户级记忆内容\n")
        with open(os.path.join(self.wb_home, "projects", "s1.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "message", "role": "user",
                                 "content": [{"type": "text", "text": "sk-SECRET-abc"}]},
                                ensure_ascii=False) + "\n")
        self.patch = mock.patch("ai_env_clone.adapters.workbuddy._workbuddy_home",
                                return_value=self.wb_home)
        self.patch.start()
        self.addCleanup(self.patch.stop)

        self.zip = os.path.join(self.tmp, "wb.zip")

    def _run(self, argv) -> "tuple[int, str]":
        c = Collector()
        rc = cli.run(argv, emit=c)
        return rc, c.text

    def _backup(self, items: str, *extra: str) -> "tuple[int, str]":
        return self._run(["backup", "--tool", "workbuddy", "--root", self.src_root,
                          "--items", items, "--out", self.zip, *extra])

    # -- backup --
    def test_backup_of_transcript_requires_yes_and_writes_nothing(self):
        """勾选转录项而没 --yes ⇒ 退出码 2 且不产生包文件（与 GUI 同一道闸门）。"""
        rc, text = self._backup("projects")
        self.assertEqual(rc, cli.EXIT_DECLINED)
        self.assertIn(TRANSCRIPT_PLAINTEXT_NOTE, text)
        self.assertFalse(os.path.exists(self.zip), "未确认时不该生成备份包")

    def test_backup_with_yes_writes_package(self):
        rc, text = self._backup("memory", "--yes")
        self.assertEqual(rc, cli.EXIT_OK, text)
        self.assertTrue(os.path.exists(self.zip))
        self.assertIn("备份完成", text)
        mf = inspect_backup(self.zip)["manifest"]
        self.assertEqual(mf["tool"], "workbuddy")
        self.assertIn("memory", mf["items"])

    def test_backup_unknown_item_token_is_actionable_error(self):
        rc, text = self._backup("nope")
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertIn("无法识别", text)
        self.assertIn("projects", text, "报错要顺手列出可取的标识")

    def test_backup_unknown_tool_fails_cleanly(self):
        rc, text = self._run(["backup", "--tool", "nonexistent-tool",
                              "--root", self.src_root])
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertIn("未找到适配器", text)

    # -- inspect --
    def test_inspect_reports_tool_and_counts(self):
        self._backup("memory", "--yes")
        rc, text = self._run(["inspect", "--package", self.zip])
        self.assertEqual(rc, cli.EXIT_OK, text)
        self.assertIn("workbuddy", text)
        self.assertIn("文件数：1", text)

    def test_inspect_unreadable_package_is_error_not_traceback(self):
        rc, text = self._run(["inspect", "--package", os.path.join(self.tmp, "no.zip")])
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertIn("无法读取", text)

    # -- restore --
    def _restore(self, *extra: str) -> "tuple[int, str]":
        return self._run(["restore", "--tool", "workbuddy", "--package", self.zip,
                          "--root", self.dst_root, *extra])

    def test_restore_without_yes_touches_nothing(self):
        self._backup("memory", "--yes")
        rc, text = self._restore()
        self.assertEqual(rc, cli.EXIT_DECLINED, text)
        self.assertIn("还原目标", text)
        self.assertEqual(os.listdir(self.dst_root), [], "未确认时不该写任何文件")

    def test_restore_with_yes_lands_files(self):
        self._backup("memory", "--yes")
        rc, text = self._restore("--yes", "--no-rollback")
        self.assertEqual(rc, cli.EXIT_OK, text)
        landed = os.path.join(self.dst_root, ".workbuddy", "memory", "uid_memory.md")
        self.assertTrue(os.path.exists(landed), text)
        self.assertIn("请重启", text)

    def test_restore_refuses_package_from_another_tool(self):
        """跨工具的包一律拒绝：路径改写 / 索引登记 / 结构校验全都按错误的工具规则执行，
        结果是「文件在、界面里看不见」——命令行不允许绕过这条闸门。"""
        self._backup("memory", "--yes")
        rc, text = self._run(["restore", "--tool", "codebuddy", "--package", self.zip,
                              "--root", self.dst_root, "--yes"])
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertIn("属于", text)
        self.assertIn("导入会话", text)
        self.assertEqual(os.listdir(self.dst_root), [], "被拒绝的还原不该写盘")

    def test_restore_rejects_rollback_snapshot_as_backup(self):
        """回滚快照不是备份包：``expected_kind`` 闸门必须照样生效。"""
        self._backup("memory", "--yes")
        snap = os.path.join(self.tmp, "rollback.zip")
        with zipfile.ZipFile(self.zip) as zin, zipfile.ZipFile(snap, "w") as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if n.endswith("ai_env_clone_manifest.json"):
                    m = json.loads(data.decode("utf-8"))
                    m["kind"] = "rollback"
                    data = json.dumps(m, ensure_ascii=False).encode("utf-8")
                zout.writestr(n, data)
        rc, text = self._run(["restore", "--tool", "workbuddy", "--package", snap,
                              "--root", self.dst_root, "--yes", "--no-rollback"])
        self.assertEqual(rc, cli.EXIT_ERROR, text)
        self.assertIn("还原失败", text)
        self.assertEqual(os.listdir(self.dst_root), [], "类型不符不该写盘")

    def test_restore_mode_merge_rejected_when_unsupported(self):
        """不支持合并的工具被显式要求 --mode merge ⇒ 说清楚并让用户改用 replace。"""
        self._backup("memory", "--yes")
        with mock.patch("ai_env_clone.adapters.workbuddy.WorkBuddyAdapter"
                        ".restore_merge_supported", False):
            rc, text = self._restore("--mode", "merge", "--yes")
        self.assertEqual(rc, cli.EXIT_ERROR, text)
        self.assertIn("不支持合并", text)

    def test_tools_listing_is_read_only(self):
        rc, text = self._run(["tools"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("workbuddy", text)
        self.assertIn("--tool", text)


# --------------------------------------------------------------------------- #
# 4) 默认包目录：与 GUI 的存放约定一致
# --------------------------------------------------------------------------- #
class TestPackageDir(unittest.TestCase):
    def test_source_mode_lands_under_repo_backup_dir(self):
        got = cli.package_dir("workbuddy")
        self.assertTrue(got.endswith(os.path.join("backup", "workbuddy")), got)
        self.assertEqual(os.path.dirname(os.path.dirname(got)), ROOT)

    def test_default_package_name_carries_tool_and_timestamp(self):
        p = cli.default_package_path("workbuddy")
        name = os.path.basename(p)
        self.assertTrue(name.startswith("workbuddy_backup_"), name)
        self.assertTrue(name.endswith(".zip"), name)


class TestRestoreVersionNotice(unittest.TestCase):
    """「包比本机新」的提示必须从 CLI 输出：还原成功却不提示，症状会被当成工具坏了。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli_notice_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = os.path.join(self.tmp, "dst")
        os.makedirs(os.path.join(self.root, ".workbuddy"))
        self.zip = os.path.join(self.tmp, "wb.zip")

    def _db(self, path, session_ids, meta_keys):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("CREATE TABLE workspaces(path TEXT PRIMARY KEY, last_opened_at INTEGER)")
        conn.execute("CREATE TABLE session_usage(session_id TEXT PRIMARY KEY, tokens INTEGER)")
        conn.execute("CREATE TABLE migration_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO sessions VALUES (?, 't')",
                         [(i,) for i in session_ids])
        conn.executemany("INSERT INTO migration_meta VALUES (?, 'v')",
                         [(k,) for k in meta_keys])
        conn.commit()
        conn.close()

    def _package(self, meta_keys):
        src = os.path.join(self.tmp, "src.db")
        self._db(src, ["s1", "s2"], meta_keys)
        with open(src, "rb") as fh:
            db_bytes = fh.read()
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("ai_env_clone_manifest.json", json.dumps({
                "version": 2, "kind": "backup", "tool": "workbuddy",
                "created_at": "2026-10-08T00:00:00", "source_root": self.tmp,
                "platform": os.name, "items": ["session_db"], "file_count": 1,
                "total_bytes": len(db_bytes),
            }, ensure_ascii=False))
            zf.writestr(".workbuddy/workbuddy.db", db_bytes)

    def _run(self, argv) -> "tuple[int, str]":
        c = Collector()
        return cli.run(argv, emit=c), c.text

    def test_notice_printed_and_package_rows_merged(self):
        local = os.path.join(self.root, ".workbuddy", "workbuddy.db")
        self._db(local, ["s1"], ["m1"])            # 本机只跑过 m1
        self._package(["m1", "m2"])                # 包来自跑过 m2 的更新版本
        rc, text = self._run(["restore", "--tool", "workbuddy", "--package", self.zip,
                              "--root", self.root, "--mode", "merge",
                              "--yes", "--no-rollback"])
        self.assertEqual(rc, cli.EXIT_OK, text)
        self.assertIn("⚠️", text)
        self.assertIn("migration_meta 有 1 项", text)
        import sqlite3
        conn = sqlite3.connect(local)
        try:
            self.assertEqual([r[0] for r in conn.execute(
                "SELECT id FROM sessions ORDER BY id")], ["s1", "s2"])
            # 迁移记录属本机：提示归提示，绝不替本机写迁移状态
            self.assertEqual([r[0] for r in conn.execute(
                "SELECT key FROM migration_meta")], ["m1"])
        finally:
            conn.close()

    def test_no_notice_when_local_already_current(self):
        local = os.path.join(self.root, ".workbuddy", "workbuddy.db")
        self._db(local, ["s1"], ["m1", "m2", "m3"])
        self._package(["m1", "m2"])
        rc, text = self._run(["restore", "--tool", "workbuddy", "--package", self.zip,
                              "--root", self.root, "--mode", "merge",
                              "--yes", "--no-rollback"])
        self.assertEqual(rc, cli.EXIT_OK, text)
        self.assertNotIn("⚠️", text)


if __name__ == "__main__":
    unittest.main()
