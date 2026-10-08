"""ZCode 适配器单元测试。

ZCode 是「同一台机器上可能有两套目录布局、两个数据根」的特殊适配器，此前**完全没有
测试文件**（Trae / WorkBuddy / DSH 等都有）。这里钉住四件只有它会踩的事：

1. **HOME 与操作系统主目录不一致**（Git Bash 把 ``HOME`` 指向 ``C:\\Home`` 这类位置）
   时数据会分散在两个 ``.zcode``：公共根必须上移到两者的最近公共祖先，条目 key 既要
   各自唯一、又要仍能按 ``key.split(":", 1)[0]`` 成行（否则界面上两处数据会挤成一行、
   用户只看到其中一边）。
2. **跨盘符**时公共根覆盖不到的那一处必须**跳过**，而不是生成非法的归档相对路径。
3. 条目契约：会话库 / 任务库 / 设置类默认勾选，插件、证书、工作区、凭证默认不勾；
   ``config.json`` / ``provider_config.json`` / ``credentials.json`` 标为敏感。
4. 导出脱敏的**边界**：既要抹掉真凭证，也不能把配置弄坏——引用键（``apiKeyEnv``）与
   ``${ENV}`` 占位必须原样保留，``author`` 不该被当成凭证（口径与 :mod:`redact` 一致）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.adapters import zcode as zc


def _agg(key: str) -> str:
    """GUI 聚合前缀的等价实现（与 ``QoderBackupApp._agg_prefix`` 同一规则）。"""
    return key.split(":", 1)[0]


def _write(path: str, text: str = "x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _mk_zcode_home(tmp: str, *,
                   with_v2: bool = True,
                   companions: bool = True,
                   with_cli_db: bool = True,
                   with_workspace: bool = False) -> str:
    """在 ``tmp`` 下搭一个 ``.zcode`` 数据根，返回该根路径。

    ``with_cli_db=False`` 时不创建 ``db.sqlite``（条目仍应出现、只是 ``exists`` 为假——
    界面要能显示「本机未找到」，而不是整行消失）。
    """
    home = os.path.join(tmp, ".zcode")
    if with_cli_db:
        _write(os.path.join(home, "cli", "db", "db.sqlite"), b"SQLite format 3\x00fake".decode("latin-1"))
    else:
        os.makedirs(os.path.join(home, "cli", "db"), exist_ok=True)
    if companions:
        for suffix in ("-wal", "-shm"):
            _write(os.path.join(home, "cli", "db", "db.sqlite") + suffix, "x")
    os.makedirs(os.path.join(home, "cli", "plugins"), exist_ok=True)
    if with_v2:
        v2 = os.path.join(home, "v2")
        _write(os.path.join(v2, "tasks-index.sqlite"), "x")
        if companions:
            for suffix in ("-wal", "-shm"):
                _write(os.path.join(v2, "tasks-index.sqlite") + suffix, "x")
        for fname in ("config.json", "setting.json", "provider_config.json", "credentials.json"):
            _write(os.path.join(v2, fname), "{}")
        os.makedirs(os.path.join(v2, "certs"), exist_ok=True)
    if with_workspace:
        os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
    return home


def _by_key(items) -> dict:
    return {it.key: it for it in items}


class TestRegistration(unittest.TestCase):
    def test_registered_with_user_visible_name(self) -> None:
        self.assertIn("zcode", list_adapters())
        adp = get_adapter("zcode")
        self.assertIsInstance(adp, zc.ZCodeAdapter)
        self.assertEqual(adp.display_name, "ZCode")

    def test_declares_library_files_for_overwrite_wording(self) -> None:
        """会话集中在整库文件里 ⇒ 还原提示必须说「整库覆盖」，靠这两个文件名判定。"""
        self.assertEqual(set(zc.ZCodeAdapter.RESTORE_LIBRARY_FILES),
                         {"db.sqlite", "tasks-index.sqlite"})


class TestHomeResolution(unittest.TestCase):
    """HOME 与操作系统主目录不一致时的两处数据根。"""

    def test_candidates_dedupe_when_homes_coincide(self) -> None:
        with mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"), \
             mock.patch.dict(os.environ, {"HOME": r"C:\Users\tester"}):
            self.assertEqual(zc.zcode_home_candidates(), [r"C:\Users\tester\.zcode"])

    def test_candidates_list_env_home_after_os_home(self) -> None:
        with mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"), \
             mock.patch.dict(os.environ, {"HOME": r"C:\Home"}):
            self.assertEqual(zc.zcode_home_candidates(),
                             [r"C:\Users\tester\.zcode", r"C:\Home\.zcode"])

    def test_candidates_without_home_env_var(self) -> None:
        with mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(zc.zcode_home_candidates(), [r"C:\Users\tester\.zcode"])

    def test_existing_keeps_only_real_dirs(self) -> None:
        tmp = tempfile.mkdtemp(prefix="zcode-homes-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        a = _mk_zcode_home(os.path.join(tmp, "A"), with_v2=False)
        with mock.patch.object(zc, "zcode_home_candidates",
                               return_value=[a, os.path.join(tmp, "B", ".zcode")]):
            self.assertEqual(zc.existing_zcode_homes(), [a])

    def test_resolve_root_single_home_returns_its_parent(self) -> None:
        home = r"C:\Users\tester\.zcode"
        with mock.patch.object(zc, "existing_zcode_homes", return_value=[home]):
            self.assertEqual(zc.resolve_root(), r"C:\Users\tester")

    def test_resolve_root_two_homes_moves_up_to_common_ancestor(self) -> None:
        with mock.patch.object(zc, "existing_zcode_homes",
                               return_value=[r"C:\Users\tester\.zcode", r"C:\Home\.zcode"]):
            self.assertEqual(os.path.abspath(zc.resolve_root()), os.path.abspath("C:\\"))

    @unittest.skipUnless(sys.platform.startswith("win"), "跨盘符只在 Windows 出现")
    def test_resolve_root_cross_drive_falls_back_to_os_home(self) -> None:
        """无法求公共祖先（不同盘符）：退回操作系统主目录，至少覆盖主布局。"""
        with mock.patch.object(zc, "existing_zcode_homes",
                               return_value=[r"C:\Users\tester\.zcode", r"D:\data\.zcode"]), \
             mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"):
            self.assertEqual(zc.resolve_root(), r"C:\Users\tester")

    def test_resolve_root_no_home_returns_os_home(self) -> None:
        with mock.patch.object(zc, "existing_zcode_homes", return_value=[]), \
             mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"):
            self.assertEqual(zc.resolve_root(), r"C:\Users\tester")

    def test_adapter_detect_root_follows_resolve_root(self) -> None:
        with mock.patch.object(zc, "resolve_root", return_value=r"C:\\"):
            self.assertEqual(zc.ZCodeAdapter().detect_root(), r"C:\\")

    def test_build_default_root_is_os_home(self) -> None:
        with mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"):
            self.assertEqual(zc.ZCodeAdapter().build_default_root(), r"C:\Users\tester")


class TestItemContract(unittest.TestCase):
    """单数据根下的条目清单：key、勾选默认值、敏感标记。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zcode-items-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = _mk_zcode_home(self.tmp)
        self.items = zc.build_items(zcode_home=self.home)
        self.by_key = _by_key(self.items)

    def test_key_set(self) -> None:
        self.assertEqual(set(self.by_key), {
            "cli_db", "cli_db:wal", "cli_db:shm",
            "cli_plugins",
            "v2_tasks", "v2_tasks:wal", "v2_tasks:shm",
            "v2_config:config.json", "v2_config:setting.json",
            "v2_config:provider_config.json",
            "v2_credentials", "v2_certs", "workspace",
        })

    def test_recommended_defaults(self) -> None:
        """核心数据与设置类默认勾选；可重下载/运行态/凭证默认不勾。"""
        checked = {k for k, v in self.by_key.items() if v.recommended}
        self.assertEqual(checked, {
            "cli_db", "cli_db:wal", "cli_db:shm",
            "v2_tasks", "v2_tasks:wal", "v2_tasks:shm",
            "v2_config:config.json", "v2_config:setting.json",
            "v2_config:provider_config.json",
        })
        for key in ("cli_plugins", "v2_credentials", "v2_certs", "workspace"):
            self.assertFalse(self.by_key[key].recommended, key)

    def test_sensitive_flags(self) -> None:
        self.assertTrue(self.by_key["v2_config:config.json"].sensitive)
        self.assertTrue(self.by_key["v2_config:provider_config.json"].sensitive)
        self.assertTrue(self.by_key["v2_credentials"].sensitive)
        self.assertFalse(self.by_key["v2_config:setting.json"].sensitive)

    def test_paths_are_absolute_and_under_home(self) -> None:
        for it in self.items:
            self.assertTrue(os.path.isabs(it.path), it.key)
            self.assertTrue(zc._under(it.path, self.tmp), "%s 不在公共根之下" % it.key)

    def test_wal_and_shm_aggregate_with_main_db(self) -> None:
        """配套文件必须与主库同前缀（界面上是一行的子项，不是三行）。"""
        for prefix, main in (("cli_db", "cli_db"), ("v2_tasks", "v2_tasks")):
            rows = [it for it in self.items if _agg(it.key) == prefix]
            self.assertEqual(len(rows), 3, prefix)
            self.assertEqual(len({it.label for it in rows}), 1, prefix)
            self.assertIn(main, {it.key for it in rows})

    def test_setting_files_share_one_row(self) -> None:
        """三个设置文件同属「旧版桌面设置（v2/）」一行（靠 : 子项聚合）。"""
        rows = [it for it in self.items if _agg(it.key) == "v2_config"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({it.label for it in rows}), 1)

    def test_missing_db_still_listed_as_not_found(self) -> None:
        """库不存在时条目不能消失：界面要显示「本机未找到」，勾选逻辑才稳定。"""
        tmp = tempfile.mkdtemp(prefix="zcode-empty-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = _mk_zcode_home(tmp, with_cli_db=False, companions=False, with_v2=False)
        by_key = _by_key(zc.build_items(zcode_home=home))
        self.assertIn("cli_db", by_key)
        self.assertFalse(by_key["cli_db"].exists)
        self.assertNotIn("cli_db:wal", by_key)

    def test_no_v2_dir_yields_only_cli_rows(self) -> None:
        tmp = tempfile.mkdtemp(prefix="zcode-nov2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = _mk_zcode_home(tmp, with_v2=False)
        keys = {it.key for it in zc.build_items(zcode_home=home)}
        self.assertFalse({k for k in keys if k.startswith("v2_")}, keys)
        self.assertIn("workspace", keys)

    def test_names_tell_the_user_what_is_backed_up(self) -> None:
        """条目名（label + description）里要点出实际文件/目录名，勾选时才看得出备的是什么。"""
        for key, needle in (("cli_db", "db.sqlite"),
                            ("v2_tasks", "tasks-index.sqlite"),
                            ("v2_credentials", "credentials.json"),
                            ("cli_plugins", "cli/plugins"),
                            ("workspace", "workspace/")):
            joined = (self.by_key[key].label + " " +
                      self.by_key[key].description).replace("\\", "/")
            self.assertIn(needle, joined, key)


class TestMultiHomeItems(unittest.TestCase):
    """两个 ``.zcode`` 根：key 唯一、成行不合并、标签标出来源。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zcode-two-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.a = _mk_zcode_home(os.path.join(self.tmp, "Users", "tester"))
        self.b = _mk_zcode_home(os.path.join(self.tmp, "Home"))
        mock.patch.object(zc, "existing_zcode_homes",
                          return_value=[self.a, self.b]).start()
        self.addCleanup(mock.patch.stopall)
        self.items = zc.build_items(root=self.tmp)
        self.by_key = _by_key(self.items)

    def test_keys_are_unique_per_home(self) -> None:
        self.assertIn("cli_db@0", self.by_key)
        self.assertIn("cli_db@1", self.by_key)
        self.assertEqual(len(self.items), len(self.by_key), "key 必须唯一，否则勾选会串行")

    def test_rows_do_not_merge_across_homes(self) -> None:
        """两处数据必须各自成行：聚合前缀不能相同。"""
        self.assertNotEqual(_agg("cli_db@0"), _agg("cli_db@1"))
        self.assertIn("v2_config@1:config.json", self.by_key)

    def test_companions_stay_with_their_own_home_db(self) -> None:
        """回归：多根时 ``-wal``/``-shm`` 曾拼成 ``cli_db:wal@0``，聚合前缀掉回
        ``cli_db``，于是主库一行、配套另一行，且两个根的配套挤进同一行（勾一条误选两条）。
        """
        for idx in (0, 1):
            rows = [it for it in self.items if _agg(it.key) == "cli_db@%d" % idx]
            self.assertEqual({it.key for it in rows},
                             {"cli_db@%d" % idx, "cli_db@%d:wal" % idx, "cli_db@%d:shm" % idx},
                             "第 %d 个根的会话库应自成一行" % idx)
            self.assertEqual(len({it.label for it in rows}), 1)
        self.assertEqual([it for it in self.items if _agg(it.key) == "cli_db"], [])
        for idx in (0, 1):
            self.assertEqual(len([it for it in self.items
                                  if _agg(it.key) == "v2_tasks@%d" % idx]), 3)

    def test_paths_point_to_their_own_home(self) -> None:
        self.assertEqual(self.by_key["cli_db@0"].path,
                         os.path.join(self.a, "cli", "db", "db.sqlite"))
        self.assertEqual(self.by_key["cli_db@1"].path,
                         os.path.join(self.b, "cli", "db", "db.sqlite"))

    def test_second_home_is_tagged_in_label(self) -> None:
        """标签只加在第二个根上（第一个是默认位置，加了反而干扰阅读）。"""
        base = "CLI 会话库（cli/db/db.sqlite）"
        self.assertEqual(self.by_key["cli_db@0"].label, base)
        self.assertTrue(self.by_key["cli_db@1"].label.startswith(base))
        self.assertNotEqual(self.by_key["cli_db@1"].label, base)
        self.assertIn(os.path.basename(os.path.dirname(self.b)),
                      self.by_key["cli_db@1"].label)

    def test_home_tag_sources(self) -> None:
        homes = [r"C:\Users\tester\.zcode", r"C:\Home\.zcode"]
        self.assertEqual(zc._home_tag(0, homes), "")
        self.assertEqual(zc._home_tag(1, homes), "（C:\\Home）")
        self.assertEqual(zc._home_tag(0, [homes[0]]), "", "单根不加标签")
        with mock.patch.dict(os.environ, {"HOME": r"C:\Home"}):
            self.assertEqual(zc._home_tag(1, homes), "（HOME 环境变量指向）")

    def test_suffix_for_only_when_multiple(self) -> None:
        self.assertEqual(zc._suffix_for(0, ["a"]), "")
        self.assertEqual(zc._suffix_for(1, ["a", "b"]), "@1")

    @unittest.skipUnless(sys.platform.startswith("win"), "跨盘符只在 Windows 出现")
    def test_cross_drive_home_is_skipped(self) -> None:
        """公共根覆盖不到（跨盘符）：跳过该根，绝不生成非法归档路径。"""
        covered = zc.build_items(root=self.tmp, zcode_home=self.a)
        self.assertTrue(covered)
        items = zc.build_items(root=r"C:\Outside",
                               zcode_home=os.path.join("D:\\", "Zhome", ".zcode"))
        self.assertEqual(items, [])

    @unittest.skipUnless(sys.platform.startswith("win"), "跨盘符只在 Windows 出现")
    def test_every_item_path_is_under_the_common_root(self) -> None:
        """归档按相对公共根落位：任何一条越过公共根都会变成非法路径。"""
        for it in self.items:
            self.assertTrue(zc._under(it.path, self.tmp), "%s -> %s" % (it.key, it.path))


class TestRedaction(unittest.TestCase):
    """导出脱敏：抹掉凭证，但不弄坏配置。"""

    def test_sensitive_key_detection_follows_redact_module(self) -> None:
        from ai_env_clone import redact
        for key in ("apiKey", "api_key", "access_token", "clientSecret",
                    "password", "private_key", "credential", "cookie",
                    "oauth:bigmodel:access_token", "authorization"):
            self.assertTrue(zc.ZCodeAdapter._looks_sensitive(key), key)
        # 与 redact 同口径：刻意不含宽泛的 auth（会误伤 author）
        for key in ("author", "model", "name", "baseUrl", "theme"):
            self.assertFalse(zc.ZCodeAdapter._looks_sensitive(key), key)
        self.assertFalse(redact.key_looks_sensitive("author"))

    def test_numeric_count_values_survive(self) -> None:
        """已知边界：``max_tokens`` 这类计数键**按名字**仍算敏感，但脱敏只替换非空字符串值，
        JSON 里它是数字 ⇒ 原样保留（换成占位符会让产品解析失败）。
        """
        self.assertTrue(zc.ZCodeAdapter._looks_sensitive("max_tokens"))
        cb = zc.ZCodeAdapter().export_transform()
        out = json.loads(cb(".zcode/v2/config.json",
                            json.dumps({"max_tokens": 4096}).encode("utf-8")).decode("utf-8"))
        self.assertEqual(out["max_tokens"], 4096)

    def test_reference_keys_are_preserved(self) -> None:
        """``apiKeyEnv`` 存的是环境变量名：抹掉它 = 还原后指向不存在的变量。"""
        self.assertFalse(zc.ZCodeAdapter._looks_sensitive("apiKeyEnv"))
        self.assertFalse(zc.ZCodeAdapter._looks_sensitive("keyFile"))

    def test_transform_redacts_nested_and_keeps_placeholders(self) -> None:
        """直接测回调：嵌套 dict/list 都要覆盖，未命中时原字节返回（不重排 JSON）。"""
        cb = zc.ZCodeAdapter().export_transform()
        src = {
            "top": {"apiKey": "sk-plain-value", "note": "keep"},
            "list": [{"access_token": "tok-1"}, {"name": "public"}],
            "env_ref": {"apiKeyEnv": "Z_CODE_KEY"},
            "placeholder": {"apiKey": "${Z_CODE_KEY}"},
            "empty": {"apiKey": ""},
        }
        out = json.loads(cb(".zcode/v2/config.json",
                            json.dumps(src, ensure_ascii=False).encode("utf-8")).decode("utf-8"))
        self.assertEqual(out["top"]["apiKey"], "***REDACTED***")
        self.assertEqual(out["top"]["note"], "keep")
        self.assertEqual(out["list"][0]["access_token"], "***REDACTED***")
        self.assertEqual(out["list"][1]["name"], "public")
        self.assertEqual(out["env_ref"]["apiKeyEnv"], "Z_CODE_KEY")
        self.assertEqual(out["placeholder"]["apiKey"], "${Z_CODE_KEY}")
        self.assertEqual(out["empty"]["apiKey"], "")

    def test_transform_passthrough(self) -> None:
        cb = zc.ZCodeAdapter().export_transform()
        raw = b"this is not json at all {"
        self.assertEqual(cb(".zcode/v2/config.json", raw), raw)
        clean = json.dumps({"theme": "dark"}).encode("utf-8")
        self.assertEqual(cb(".zcode/v2/setting.json", clean), clean,
                         "无可脱敏内容时应返回原字节，避免无谓重排")

    def test_export_transform_paths(self) -> None:
        self.assertEqual(sorted(zc.ZCodeAdapter().export_transform_paths()),
                         ["config.json", "credentials.json", "provider_config.json"])

    def test_export_transform_end_to_end_redacts_zip(self) -> None:
        """导出后包内：config.json 已脱敏、setting.json 原样、引用与环境变量占位保留。"""
        tmp = tempfile.mkdtemp(prefix="zcode-export-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = _mk_zcode_home(tmp)
        v2 = os.path.join(home, "v2")
        cfg = {
            "providers": [
                {"name": "BigModel", "apiKey": "sk-real-secret-0123456789abcdef",
                 "apiKeyEnv": "ZCODE_BIGMODEL_API_KEY"},
                {"name": "Keep", "apiKey": "${ZCODE_KEEP_KEY}"},
            ],
            "author": "tester",
            "default_model": "glm-4.6",
        }
        _write(os.path.join(v2, "config.json"), json.dumps(cfg, ensure_ascii=False))
        _write(os.path.join(v2, "setting.json"), json.dumps({"theme": "dark"}))

        items = [it for it in zc.build_items(zcode_home=home)
                 if _agg(it.key) == "v2_config"]
        zip_path = os.path.join(tmp, "out.zip")
        zc.ZCodeAdapter().export(zip_path, tmp, items, progress=lambda *a: None)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            cfg_text = zf.read([n for n in names if n.endswith("v2/config.json")][0]).decode("utf-8")
            set_text = zf.read([n for n in names if n.endswith("v2/setting.json")][0]).decode("utf-8")

        self.assertNotIn("sk-real-secret-0123456789abcdef", cfg_text)
        self.assertIn("***REDACTED***", cfg_text)
        self.assertIn("ZCODE_BIGMODEL_API_KEY", cfg_text)   # 引用必须留着
        self.assertIn("${ZCODE_KEEP_KEY}", cfg_text)        # 环境变量占位留着
        self.assertIn('"author": "tester"', cfg_text)       # 不是凭证，别乱抹
        self.assertIn("glm-4.6", cfg_text)
        self.assertIn("dark", set_text)


class TestMatchStructure(unittest.TestCase):
    def test_cli_layout_matches(self) -> None:
        ok, missing = zc.ZCodeAdapter().match_structure(
            ["C__Users_tester/.zcode/cli/db/db.sqlite"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_legacy_v2_layout_matches(self) -> None:
        ok, _missing = zc.ZCodeAdapter().match_structure(
            ["C__Users_tester/.zcode/v2/tasks-index.sqlite"])
        self.assertTrue(ok)

    def test_backslash_names_normalised(self) -> None:
        ok, _missing = zc.ZCodeAdapter().match_structure(
            ["C__Users_tester\\.zcode\\cli\\db\\db.sqlite"])
        self.assertTrue(ok)

    def test_unrelated_package_reports_reason(self) -> None:
        adp = zc.ZCodeAdapter()
        for names in ([], ["a/b/c.json"], [".zcode/cli/db/db.sqlite-x"]):
            ok, missing = adp.match_structure(names)
            self.assertFalse(ok, names)
            self.assertTrue(missing)


class TestDetectDataRoots(unittest.TestCase):
    def test_lists_homes_with_content_notes(self) -> None:
        tmp = tempfile.mkdtemp(prefix="zcode-roots-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = _mk_zcode_home(tmp, with_workspace=True)
        with mock.patch.object(zc, "existing_zcode_homes", return_value=[home]), \
             mock.patch.object(zc, "_os_home", return_value=tmp):
            roots = zc.ZCodeAdapter().detect_data_roots(tmp)
        self.assertEqual(roots[0]["rel"], ".zcode")
        self.assertTrue(roots[0]["exists"])
        self.assertIn("db.sqlite", roots[0]["note"])
        self.assertIn("v2/", roots[0]["note"])

    def test_runtime_dirs_are_flagged_not_backed_up(self) -> None:
        tmp = tempfile.mkdtemp(prefix="zcode-roots2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = _mk_zcode_home(tmp, with_v2=False)
        with mock.patch.object(zc, "existing_zcode_homes", return_value=[home]), \
             mock.patch.object(zc, "_os_home", return_value=tmp):
            roots = zc.ZCodeAdapter().detect_data_roots(tmp)
        notes = " ".join(r["note"] for r in roots[1:])
        self.assertIn("桌面运行态，不列入备份", notes)
        self.assertIn("更新器，不列入备份", notes)

    def test_display_rel_prefers_home_relative(self) -> None:
        """展示口径：优先主目录相对，其次探测基准相对，两处都不在才给绝对路径。

        否则 ZCode 会在状态区显示 ``Users\\<用户>\\.zcode`` 这种其它适配器从不出现的
        写法（它们都给 ``.workbuddy`` / ``AppData\\Roaming\\...``）。
        """
        with mock.patch.object(zc, "_os_home", return_value=r"C:\Users\tester"):
            self.assertEqual(zc._display_rel(r"C:\Users\tester\.zcode", "C:\\"), ".zcode")
            self.assertEqual(zc._display_rel(r"C:\Home\.zcode", "C:\\"),
                             os.path.join("Home", ".zcode"))
            self.assertEqual(zc._display_rel(r"D:\data\.zcode", "C:\\"), r"D:\data\.zcode")

    def test_no_zcode_dir_still_lists_expected_root(self) -> None:
        """没装也用过的机器：状态区仍应显示「预期位置」，而不是空列表。"""
        tmp = tempfile.mkdtemp(prefix="zcode-none-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(zc, "existing_zcode_homes", return_value=[]), \
             mock.patch.object(zc, "_os_home", return_value=tmp):
            roots = zc.ZCodeAdapter().detect_data_roots(tmp)
        self.assertEqual(roots[0]["rel"], ".zcode")
        self.assertFalse(roots[0]["exists"])


if __name__ == "__main__":
    unittest.main()
