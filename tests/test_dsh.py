"""DeepSeek Harness（DSH）适配器单元测试。

覆盖：注册、跨平台路径探测、build_items 路径均在公共根 ~ 之下、推荐默认、
detect_data_roots 展示。路径布局来自本机 Windows 实测 + DSH 官方文档约定
（见 adapters/dsh.py 模块 docstring），不臆测。
"""

import os
import sys
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
import ai_env_clone.adapters.dsh as dsh_mod


class TestRegistration(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("dsh", list_adapters())
        self.assertIsInstance(get_adapter("dsh"), dsh_mod.DSHAdapter)

    def test_adapter_interface(self) -> None:
        a = get_adapter("dsh")
        self.assertTrue(hasattr(a, "detect_root"))
        self.assertTrue(hasattr(a, "build_default_root"))
        self.assertTrue(hasattr(a, "detect_data_roots"))
        self.assertTrue(hasattr(a, "build_items"))


class TestDSHHome(unittest.TestCase):
    def test_default_dsh_home(self) -> None:
        """默认 DSH_HOME 应为 ~/.dsh。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_HOME", None)
            self.assertEqual(
                dsh_mod._dsh_home(),
                os.path.join(dsh_mod._home(), ".dsh"),
            )

    def test_dsh_home_env_override(self) -> None:
        """$DSH_HOME 环境变量可覆盖。"""
        test_path = r"D:\custom\dsh_data"
        with mock.patch.dict(os.environ, {"DSH_HOME": test_path}, clear=False):
            self.assertEqual(dsh_mod._dsh_home(), test_path)

    def test_helpers_under_dsh(self) -> None:
        """_dsh_sessions_root、_dsh_storages_root、_dsh_profiles_root 应都在 _dsh_home 之下。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_HOME", None)
            dsh = dsh_mod._dsh_home()
            self.assertTrue(dsh_mod._dsh_sessions_root().startswith(dsh))
            self.assertTrue(dsh_mod._dsh_storages_root().startswith(dsh))
            self.assertTrue(dsh_mod._dsh_profiles_root().startswith(dsh))


class TestBuildItems(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dsh_test_")
        self.dsh_dir = os.path.join(self.tmp, ".dsh")

        # 模拟 DSH 目录结构
        # sessions/
        ws_dir = os.path.join(self.dsh_dir, "sessions", "--D-project-test--")
        session_dir = os.path.join(ws_dir, "session-abc12345-0000-0000-0000-000000000001")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "session.jsonl.zstd"), "wb") as f:
            f.write(b"fake zstd data")

        # storages/
        storages_dir = os.path.join(self.dsh_dir, "storages")
        os.makedirs(storages_dir, exist_ok=True)
        with open(os.path.join(storages_dir, "workspace.json"), "w", encoding="utf-8") as f:
            f.write('{"unit": {"name": "workspace", "version": 2}}')
        # 会话投影缓存：现行 **per-record 目录树**布局（一条记录一份文件）。
        sp_dir = os.path.join(storages_dir, "session_projcache", "sessions")
        os.makedirs(sp_dir, exist_ok=True)
        with open(os.path.join(sp_dir, "session-abc12345.json"), "w", encoding="utf-8") as f:
            f.write('{"version": 7, "record": {"identity": {"cwd": "D:\\\\project\\\\test"}}}')
        # 旧 single 布局的单文件：storage-json 迁移时「保持源文件不变」，故它仍在盘上。
        # 夹具保留它，是为了让「条目必须指向目录树、不得指向这个死文件」的断言有意义。
        with open(os.path.join(storages_dir, "session_projcache.json"), "w", encoding="utf-8") as f:
            f.write('{"unit": {"name": "session_projcache", "version": 3}}')

        # AGENTS.md
        with open(os.path.join(self.dsh_dir, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# 测试用户全局指令")

        # profiles/<profile>/cordis.patch.yml —— 实时配置的落点
        # （2026-10-02 起替代已废弃的 ``settings.yaml``，后者不再生成条目）
        prof_dir = os.path.join(self.dsh_dir, "profiles", "desktop")
        os.makedirs(prof_dir, exist_ok=True)
        with open(os.path.join(prof_dir, "cordis.patch.yml"), "w", encoding="utf-8") as f:
            f.write("- id: ui-settings-general\n  config:\n    locale: zh\n")

        # 依赖树目录：**不是** profile，枚举时必须跳过（否则会多出一条假条目）
        pkg_dir = os.path.join(self.dsh_dir, "profiles", "node_modules", "some-pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "some-pkg", "version": "1.0.0"}\n')

        # .credentials.yaml
        with open(os.path.join(self.dsh_dir, ".credentials.yaml"), "w", encoding="utf-8") as f:
            f.write("api_key: test\n")

    def _items(self):
        return dsh_mod.build_items(self.tmp, self.dsh_dir)

    def test_all_paths_under_root(self) -> None:
        for it in self._items():
            self.assertTrue(
                os.path.abspath(it.path).startswith(os.path.abspath(self.tmp)),
                "条目 %s 的 path 不在公共根之下: %s" % (it.key, it.path),
            )

    def test_keys_present(self) -> None:
        keys = {it.key for it in self._items()}
        expected_keys = {
            "sessions",
            "storages_workspace",
            "storages_session_projcache",
            "user_agents",
            "profiles_patch:desktop",
            "credentials",
            "profiles",
        }
        self.assertEqual(keys, expected_keys)

    def test_deprecated_settings_yaml_is_not_an_item(self) -> None:
        """已废弃的 ``settings.yaml`` **不得**再作为备份条目出现。

        新版 DSH 已移除该文件（源码 ``SettingsForms.importLegacyDocument()`` 里
        ``if (!existsSync(path)) return`` 是唯一读写点，全仓无写入路径）⇒ 按
        「当前支持备份的版本中不存在的条目不列为备份选项」的规则移除。
        这里同时防运行期回归：夹具里连 ``settings.yaml`` 都没建，
        若哪天有人把条目加回来，本用例会失败。
        """
        keys = {it.key for it in self._items()}
        self.assertNotIn("settings", keys)
        for it in self._items():
            self.assertFalse(
                it.path.endswith("settings.yaml"),
                "条目 %s 指向了已废弃的 settings.yaml: %s" % (it.key, it.path),
            )

    def test_projcache_points_at_per_record_tree_not_the_dead_single_file(self) -> None:
        """投影缓存条目必须指向**现行 per-record 目录树**，不得指向旧的 single 单文件。

        背景：``storages/session_projcache.json`` 是该单元旧的 single 布局文档。
        storage-json 后端把它迁成 per-record 目录树时「保持源文件不变」⇒ 那个文件会
        一直留在盘上却不再被写入（本机实测 mtime 冻结在升级那一刻）。若条目仍指向它，
        备份会「成功但还原出来是空的」，且界面显示「已找到」完全看不出来。
        夹具里两种形态并存，正是为了让本断言有意义。
        """
        items = {it.key: it for it in self._items()}
        path = items["storages_session_projcache"].path
        self.assertTrue(
            os.path.isdir(path),
            "投影缓存条目应指向目录树，实际指向: %s" % path,
        )
        self.assertFalse(
            path.endswith("session_projcache.json"),
            "不得指向旧的 single 布局单文件: %s" % path,
        )
        self.assertTrue(
            os.path.exists(os.path.join(path, "sessions")),
            "per-record 布局的记录都在 sessions/ 子目录下: %s" % path,
        )

    def test_storages_items_do_not_collapse_into_one_gui_row(self) -> None:
        """两个 storages 单元必须是**两行**，不能被 GUI 聚合成一行。

        回归背景：GUI 按 ``key.split(":", 1)[0]`` 聚合成一行，且该行的默认勾选态取
        **组内首项**的 ``recommended``。若两个 key 共用 `storages:` 前缀，则
        「工作区索引默认勾、投影缓存默认不勾」会被合成一个勾选框——首项的 true 胜出，
        缓存照样被默认带上，且一行代表两种推荐态（自相矛盾）。同 Trae `ui_misc_*` 的处理。
        本断言不导入 GUI 模块（托管 Python 无 tkinter 也能跑）。
        """
        items = self._items()
        prefixes = {it.key.split(":", 1)[0] for it in items
                    if it.key.split(":", 1)[0].startswith("storages")}
        self.assertEqual(
            sorted(prefixes), ["storages_session_projcache", "storages_workspace"],
            "两个 storages 条目聚合成了同一前缀（界面只会渲染一行）",
        )
        # 二者的默认态必须不同，才能证明「拆开」是有意义的
        rec = {it.key: it.recommended for it in items
               if it.key.startswith("storages")}
        self.assertNotEqual(rec["storages_workspace"],
                            rec["storages_session_projcache"])

    def test_node_modules_is_not_treated_as_profile(self) -> None:
        """``profiles/node_modules`` 是依赖树，不是 profile，不得生成条目。"""
        keys = {it.key for it in self._items()}
        self.assertNotIn("profiles_patch:node_modules", keys)
        self.assertEqual(
            [k for k in keys if k.startswith("profiles_patch:")],
            ["profiles_patch:desktop"],
        )

    def test_recommended_defaults(self) -> None:
        items = {it.key: it for it in self._items()}
        # 核心数据默认勾选
        self.assertTrue(items["sessions"].recommended)
        self.assertTrue(items["storages_workspace"].recommended)
        self.assertTrue(items["user_agents"].recommended)
        # 「会话投影缓存」是**可从会话日志重建**的缓存（官方 README：「日志领先，缓存跟随」）
        # ⇒ 默认不勾（与 code_index / plugins 同类口径）。
        self.assertFalse(items["storages_session_projcache"].recommended)
        # 实时配置默认勾选（承接原 settings 条目「还原后立刻能开工」的定位），
        # ★ 但**不标 sensitive**：实测 cordis.patch.yml 只存 provider 的密钥**引用**
        #   （``apiKeyEnv: SENSENOVA_API_KEY``），真密钥在 .credentials.yaml。
        #   标了会让「备份后定位敏感文件」把用户带去该文件找一个不存在的明文密钥。
        self.assertTrue(items["profiles_patch:desktop"].recommended)
        self.assertFalse(items["profiles_patch:desktop"].sensitive)
        # 但必须带「配套条目」提醒：未勾 .credentials.yaml 时提示用户单独备份
        comp = items["profiles_patch:desktop"].companion
        self.assertIsNotNone(comp)
        self.assertEqual(comp[0], "credentials")
        # 凭证 / 配置文件仍默认不勾
        self.assertFalse(items["credentials"].recommended)
        self.assertFalse(items["profiles"].recommended)

    def test_credentials_is_companion_of_live_config(self) -> None:
        """``.credentials.yaml`` 是实时配置的配套文件，声明必须落在它身上。"""
        from ai_env_clone.core import companion_notes

        items = self._items()
        creds = next(it for it in items if it.key == "credentials")
        self.assertIsNone(creds.companion, "配套声明应写在触发方（profiles_patch:*）上")

        # 只勾实时配置（默认情形）→ 必须提醒
        only_patch = [it for it in items if it.key in ("profiles_patch:desktop", "sessions")]
        notes = companion_notes(only_patch)
        self.assertEqual(len(notes), 1)
        self.assertIn("credentials", notes[0])
        self.assertIn("单独备份", notes[0])

        # 实时配置 + credentials 都勾 → 不再提醒
        both = [it for it in items if it.key in ("profiles_patch:desktop", "credentials")]
        self.assertEqual(companion_notes(both), [])

    def test_exists_when_present(self) -> None:
        for it in self._items():
            self.assertTrue(it.exists, "条目 %s 应存在: %s" % (it.key, it.path))

    def test_patch_and_profiles_items_do_not_double_count(self) -> None:
        """``profiles_patch:*`` 与 ``profiles/`` 两条目并存时，实时配置只进包一次。

        两者路径重叠（一个是文件、一个是它的父目录），但归档按**相对路径去重**
        （``core.scan_items`` 的 ``seen``）⇒ 既不重复计数，也不影响「单勾实时配置」
        时的正确性。这条断言守住适配器注释里的那句「并存不冲突」。
        """
        from ai_env_clone import core

        items = self._items()
        rel_patch = ".dsh/profiles/desktop/cordis.patch.yml"

        # 只勾实时配置 → 文件在包里，依赖树不在
        only_patch = [it for it in items if it.key == "profiles_patch:desktop"]
        scan = core.scan_items(only_patch, self.tmp, max_file_mb=None)
        arcs = [rel for _, rel in scan.files]
        self.assertEqual(arcs, [rel_patch])

        # 两个都勾 → 该文件仍只出现一次（去重），依赖树额外进来
        both = [it for it in items if it.key in ("profiles_patch:desktop", "profiles")]
        scan2 = core.scan_items(both, self.tmp, max_file_mb=None)
        arcs2 = [rel for _, rel in scan2.files]
        self.assertEqual(
            arcs2.count(rel_patch), 1,
            "实时配置在「两个条目都勾」时被重复计数: %s" % arcs2,
        )
        self.assertIn(".dsh/profiles/node_modules/some-pkg/package.json", arcs2,
                      "勾了 profiles/ 应连带依赖树（便于验证两者确为不同范围）")

    def test_missing_root_still_lists_all(self) -> None:
        """即使 DSH 目录不存在，也应列出固定路径的全部 6 项（供 GUI 显示未找到）。

        注意：``profiles_patch:*`` 是**按磁盘枚举**出来的（profile 名由启动方决定，
        无单一默认名），磁盘上一个 profile 都没有时自然没有该条目 —— 这不是
        「漏项」，而是无可备份内容；固定路径的条目则一律保留以显示「未找到」。
        """
        items = dsh_mod.build_items(self.tmp, os.path.join(self.tmp, "nope_dsh"))
        self.assertEqual(len(items), 6)
        self.assertTrue(all(not it.exists for it in items))
        self.assertFalse([it for it in items if it.key.startswith("profiles_patch:")])

    def test_empty_sessions_dir(self) -> None:
        """sessions 目录为空时仍列出全部固定条目，仅会话项标记为不存在。"""
        empty_dsh = os.path.join(self.tmp, "empty_dsh")
        os.makedirs(empty_dsh, exist_ok=True)
        items = dsh_mod.build_items(self.tmp, empty_dsh)
        self.assertEqual(len(items), 6)
        sessions_item = next(it for it in items if it.key == "sessions")
        self.assertFalse(sessions_item.exists)


class TestDetectDataRoots(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dsh_dr_")
        self.dsh_dir = os.path.join(self.tmp, ".dsh")

        # 模拟完整 DSH 目录
        os.makedirs(os.path.join(self.dsh_dir, "sessions", "--D-project-test--",
                                  "session-abc"), exist_ok=True)
        os.makedirs(os.path.join(self.dsh_dir, "storages"), exist_ok=True)
        os.makedirs(os.path.join(self.dsh_dir, "profiles"), exist_ok=True)

    def _patch_dsh_home(self, dsh_dir: str):
        """把 DSH_HOME 指向 tmp 下的 .dsh，使 _dsh_home() 与 root 同盘，relpath 可算。"""
        return mock.patch.dict(os.environ, {"DSH_HOME": dsh_dir}, clear=False)

    def test_detect_data_roots_lists_one_root(self) -> None:
        """同一根目录只显示一行：DSH 各类数据都在 ~/.dsh 下，仅返回该根一行。"""
        a = get_adapter("dsh")
        with self._patch_dsh_home(self.dsh_dir):
            roots = a.detect_data_roots(self.tmp)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["rel"], os.path.relpath(self.dsh_dir, self.tmp))
        self.assertTrue(roots[0]["exists"])
        # 子目录（sessions/storages/profiles）不单独列出
        rel = roots[0]["rel"].replace("\\", "/")
        self.assertNotIn("sessions", rel)
        self.assertNotIn("storages", rel)
        self.assertNotIn("profiles", rel)

    def test_detect_data_roots_missing(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="dsh_dr2_")
        a = get_adapter("dsh")
        with self._patch_dsh_home(os.path.join(tmp, ".dsh")):
            roots = a.detect_data_roots(tmp)
        self.assertEqual(len(roots), 1)
        self.assertTrue(all(not r["exists"] for r in roots))

    def test_detect_data_roots_empty_sessions(self) -> None:
        a = get_adapter("dsh")
        # 创建 .dsh 但 sessions 为空：仍只显示 .dsh 一行，根存在为 True
        os.makedirs(os.path.join(self.dsh_dir, "sessions"), exist_ok=True)
        with self._patch_dsh_home(self.dsh_dir):
            roots = a.detect_data_roots(self.tmp)
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0]["exists"])
        self.assertEqual(roots[0]["rel"], os.path.relpath(self.dsh_dir, self.tmp))


if __name__ == "__main__":
    unittest.main()