"""Trae 家族（TraeCode CN / TraeWork CN）适配器单元测试。

覆盖两件 2026-10-01 实测暴露出来的事：

1. **条目 key 前缀绝不能带冒号**。GUI 按 ``key.split(":", 1)[0]`` 聚合成一行渲染
   （``__main__.QoderBackupApp._agg_prefix``），原先的 ``"trae_cn:"`` 让整个适配器
   共用同一个前缀 —— 实测 20 个条目在界面上只渲染出 **1 行**（对照：CodeBuddy
   16 条 -> 12 行）。这里用「聚合前缀数 == 条目数」把它钉死，等价于界面行数，
   且**不导入 GUI 模块**（避免依赖 tkinter，托管 Python 下也能跑）。
2. **备份的是本机 skill 数据，不是 skill 市场数据**：``skill-config.json``（本机
   启用/禁用状态）默认勾选；``builtin_skills/``（随产品分发）与任何市场镜像都不进。

另外覆盖「IDE 工作区记录」条目：Trae 的会话库是产品侧加密的、挖不出路径，
``workspaceStorage/*/workspace.json`` 是库外唯一的结构化「工作区 -> 工程路径」线索。

TraeWork CN（安装目录 ``TRAE SOLO CN``）复用家族实现，因此单独测它**替换掉的那三件**：
扩展目录回退（本机没有 ``.trae-solo-cn`` 时共用 ``.trae-cn``）、沙箱虚拟机 ``vm/``
不列入备份、以及结构指纹必须把同族两个形态分清（否则拿 TraeCode CN 的包能往
TraeWork CN 上还原）。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.adapters import trae_cn as tc
from ai_env_clone.adapters import trae_solo_cn as ts


def _agg(key: str) -> str:
    """GUI 聚合前缀的等价实现（与 ``QoderBackupApp._agg_prefix`` 同一规则）。"""
    return key.split(":", 1)[0]


def _family(key_prefix: str = tc.KEY_PREFIX_TRAE_CN,
            *,
            has_vm: bool = False,
            workspace_buckets=(),
            ui_misc_dirs=(),
            tmp: str | None = None):
    """在临时目录里搭一份 Trae 数据布局，返回 ``(items, user_data, ext_dir)``。

    ``ModularData/ai-agent/database.db`` 会真实创建，否则会话库条目按存在性
    探测不到（``_db_with_companions`` 只返回存在的文件）。
    """
    root = tmp or tempfile.mkdtemp(prefix="trae-test-")
    user_data = os.path.join(root, "AppData", "Roaming", "Trae CN")
    ext_dir = os.path.join(root, ".trae-cn")
    os.makedirs(os.path.join(user_data, "ModularData", "ai-agent"), exist_ok=True)
    os.makedirs(ext_dir, exist_ok=True)
    with open(os.path.join(user_data, "ModularData", "ai-agent", "database.db"), "wb") as f:
        f.write(b"\x00fake-encrypted-db\x00")
    for bucket in workspace_buckets:
        d = os.path.join(user_data, "User", "workspaceStorage", bucket)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "workspace.json"), "w", encoding="utf-8") as f:
            f.write('{"folder": "file:///d%3A/project/demo"}')
    for sub in ui_misc_dirs:
        os.makedirs(os.path.join(user_data, sub), exist_ok=True)
    items = tc.build_family_items(root, user_data, ext_dir,
                                  key_prefix=key_prefix, has_vm=has_vm)
    return items, user_data, ext_dir


def _by_key(items) -> dict:
    return {it.key: it for it in items}


class TestRegistration(unittest.TestCase):
    def test_both_family_members_registered(self) -> None:
        names = list_adapters()
        self.assertIn("trae-cn", names)
        self.assertIn("trae-solo-cn", names)
        self.assertIsInstance(get_adapter("trae-cn"), tc.TraeCnAdapter)


class TestKeyPrefixNeverBreaksGrouping(unittest.TestCase):
    """回归：key 前缀带冒号会把整个适配器压成界面上一行。"""

    def test_prefix_constants_have_no_colon(self) -> None:
        self.assertNotIn(":", tc.KEY_PREFIX_TRAE_CN)
        self.assertNotIn(":", tc.KEY_PREFIX_TRAE_SOLO_CN)

    def test_every_key_carries_prefix(self) -> None:
        items, _ud, _ed = _family()
        for it in items:
            self.assertTrue(it.key.startswith(tc.KEY_PREFIX_TRAE_CN), it.key)

    def test_each_item_gets_its_own_row(self) -> None:
        """无「子项聚合」时：界面行数必须等于条目数（18 条 -> 18 行）。

        若有人把前缀改回 ``"trae_cn:"``，这里会立刻变成 1 行。
        """
        items, _ud, _ed = _family()
        prefixes = {_agg(it.key) for it in items}
        self.assertEqual(len(items), 18)
        self.assertEqual(len(prefixes), len(items),
                         "条目被聚合到同一行了，检查 key_prefix 是否含冒号")

    def test_wal_and_shm_still_aggregate_with_main_db(self) -> None:
        """子项聚合仍要生效：database.db 主库 / -wal / -shm 共享一个前缀（一行）。"""
        root = tempfile.mkdtemp(prefix="trae-test-")
        user_data = os.path.join(root, "AppData", "Roaming", "Trae CN")
        ext_dir = os.path.join(root, ".trae-cn")
        aa = os.path.join(user_data, "ModularData", "ai-agent")
        os.makedirs(aa, exist_ok=True)
        os.makedirs(ext_dir, exist_ok=True)
        for name in ("database.db", "database.db-wal", "database.db-shm"):
            with open(os.path.join(aa, name), "wb") as f:
                f.write(b"x")
        items = tc.build_family_items(root, user_data, ext_dir,
                                      key_prefix=tc.KEY_PREFIX_TRAE_CN)
        db_rows = [it for it in items if _agg(it.key) == "trae_cn_ai_agent_db"]
        self.assertEqual(len(db_rows), 3)
        self.assertEqual({it.label for it in db_rows}, {"AI 智能体会话库（database.db）"})


class TestSkillDataOnly(unittest.TestCase):
    """只备份本机 skill 数据，不备份市场数据 / 随产品分发的内置技能。"""

    def test_skill_config_is_checked(self) -> None:
        items, _ud, ext_dir = _family()
        it = _by_key(items)["trae_cn_skill_config"]
        self.assertTrue(it.recommended)
        self.assertEqual(it.path, os.path.join(ext_dir, "skill-config.json"))
        self.assertIn("skill-config.json", it.description)

    def test_builtin_skills_not_checked(self) -> None:
        items, _ud, _ext = _family()
        self.assertFalse(_by_key(items)["trae_cn_builtin_skills"].recommended)

    def test_no_marketplace_or_cache_entries(self) -> None:
        items, _ud, _ed = _family()
        for it in items:
            low = it.path.replace("\\", "/").lower()
            for bad in ("skills-marketplace", "marketplaces", "/cache", "hub_event_cache"):
                self.assertNotIn(bad, low, "不该备份市场/缓存数据：%s" % it.key)


class TestIdeWorkspaceRecords(unittest.TestCase):
    """Trae 会话库加密（挖不出路径），workspace.json 是库外唯一的路径线索。"""

    def test_absent_when_no_workspace_json(self) -> None:
        items, _ud, _ed = _family()
        self.assertEqual([it for it in items if "ide_workspace_records" in it.key], [])

    def test_detect_returns_only_existing_records(self) -> None:
        root = tempfile.mkdtemp(prefix="trae-test-")
        user_data = os.path.join(root, "ud")
        # 目录存在但没有 workspace.json（本机实测就是这种「空窗口」形态）
        os.makedirs(os.path.join(user_data, "User", "workspaceStorage", "1789814270529"))
        self.assertEqual(tc.detect_ide_workspace_records(user_data), [])
        self.assertEqual(tc.detect_ide_workspace_records(os.path.join(root, "nope")), [])
        # 补上 workspace.json 后被认出
        wj = os.path.join(user_data, "User", "workspaceStorage", "1789814270529",
                          "workspace.json")
        with open(wj, "w", encoding="utf-8") as f:
            f.write('{"folder": "file:///d%3A/project/demo"}')
        self.assertEqual(tc.detect_ide_workspace_records(user_data), [wj])

    def test_records_are_checked_and_aggregate_into_one_row(self) -> None:
        items, _ud, _ed = _family(workspace_buckets=("aaa", "bbb"))
        rows = [it for it in items if "ide_workspace_records" in it.key]
        self.assertEqual(len(rows), 2)
        self.assertEqual({_agg(it.key) for it in rows},
                         {"trae_cn_ide_workspace_records"})
        for it in rows:
            self.assertTrue(it.recommended)
            self.assertTrue(it.path.endswith("workspace.json"))
            self.assertTrue(it.carries_origin, "应在详情里说明携带源设备路径")

    def test_row_count_grows_by_one_for_the_group(self) -> None:
        base, _ud, _ed = _family()
        withrec, _ud2, _ed2 = _family(workspace_buckets=("aaa", "bbb"))
        self.assertEqual(len({_agg(it.key) for it in withrec}),
                         len({_agg(it.key) for it in base}) + 1)


class TestUiMiscRowsAreDistinct(unittest.TestCase):
    """ui_misc 三项性质不同，必须各自成行（否则界面只显示第一个的 label）。"""

    def test_three_dirs_yield_three_rows_with_distinct_labels(self) -> None:
        items, _ud, _ed = _family(ui_misc_dirs=("Workspaces", "remote-widgets", "solo-lite"))
        rows = [it for it in items if "ui_misc" in it.key]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({_agg(it.key) for it in rows}), 3)
        self.assertEqual(len({it.label for it in rows}), 3)


class TestTraeWorkCnSolo(unittest.TestCase):
    """TraeWork CN（``TRAE SOLO CN``）：复用家族实现，只测它替换掉的那几处。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="trae-solo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.adp = ts.TraeSoloCnAdapter()
        self.home = os.path.join(self.tmp, "home")
        self.appdata = os.path.join(self.tmp, "appdata")
        self.user_data = os.path.join(self.appdata, ts.TRAE_SOLO_PRODUCT)
        os.makedirs(os.path.join(self.user_data, "ModularData", "ai-agent"), exist_ok=True)
        with open(os.path.join(self.user_data, "ModularData", "ai-agent", "database.db"),
                  "wb") as f:
            f.write(b"\x00encrypted-not-sqlite\x00")
        os.makedirs(self.home, exist_ok=True)
        # 主目录与 %APPDATA% 都指向临时目录：适配器所有路径都由这两处派生
        mock.patch.object(tc, "_home", return_value=self.home).start()
        mock.patch.object(tc, "appdata_dir",
                          side_effect=lambda name: os.path.join(self.appdata, name)).start()
        self.addCleanup(mock.patch.stopall)

    def _mk_ext(self, product_dir: str) -> str:
        d = os.path.join(self.home, "." + product_dir)
        os.makedirs(d, exist_ok=True)
        return d

    # --- 1. 扩展目录解析（本产品目录优先，缺了就退回家族共用） ------------------- #

    def test_ext_dir_prefers_own_product_dir(self) -> None:
        own = self._mk_ext(ts.TRAE_SOLO_PRODUCT_DIR)
        self._mk_ext(ts.FAMILY_SHARED_PRODUCT_DIR)
        self.assertEqual(ts._solo_ext_dir(), own)

    def test_ext_dir_falls_back_to_shared_family_dir(self) -> None:
        """本机实测：SOLO 形态常常不建 ``.trae-solo-cn``，扩展装在共用的 ``.trae-cn``。"""
        shared = self._mk_ext(ts.FAMILY_SHARED_PRODUCT_DIR)
        self.assertEqual(ts._solo_ext_dir(), shared)

    def test_ext_dir_keeps_own_path_when_neither_exists(self) -> None:
        """两处都没有时仍返回本产品目录：条目显示「本机未找到」，行结构不随安装状态漂移。"""
        self.assertEqual(ts._solo_ext_dir(),
                         os.path.join(self.home, "." + ts.TRAE_SOLO_PRODUCT_DIR))
        by_key = {it.key: it for it in self.adp.build_items(self.home)}
        self.assertFalse(by_key["trae_solo_cn_extensions"].exists)

    def test_shared_ext_dir_is_announced_in_status(self) -> None:
        self._mk_ext(ts.FAMILY_SHARED_PRODUCT_DIR)
        rows = {r["rel"].replace("\\", "/"): r for r in self.adp.detect_data_roots(self.home)}
        shared = [r for r in rows.values() if r["rel"].replace("\\", "/").endswith(".trae-cn")]
        self.assertTrue(shared)
        self.assertIn("与 Trae CN 共用扩展目录", shared[0]["note"])
        self.assertTrue(shared[0]["exists"], "共用目录确实存在，不该显示「未找到」")

    def test_own_ext_dir_is_announced_as_own(self) -> None:
        """有独立扩展目录时不标注共用，状态区显示的就是它自己。"""
        self._mk_ext(ts.TRAE_SOLO_PRODUCT_DIR)
        self._mk_ext(ts.FAMILY_SHARED_PRODUCT_DIR)
        rows = {r["rel"].replace("\\", "/"): r for r in self.adp.detect_data_roots(self.home)}
        own = rows["." + ts.TRAE_SOLO_PRODUCT_DIR]
        self.assertNotIn("共用", own["note"])

    def test_items_follow_the_shared_ext_dir(self) -> None:
        """条目路径必须跟着解析结果走，否则备的是空目录、还原后扩展全丢。"""
        shared = self._mk_ext(ts.FAMILY_SHARED_PRODUCT_DIR)
        by_key = {it.key: it for it in self.adp.build_items(self.home)}
        self.assertEqual(os.path.dirname(by_key["trae_solo_cn_extensions"].path), shared)

    # --- 2. 沙箱虚拟机：运行态，不进备份 -------------------------------------- #

    def test_vm_dir_is_never_a_backup_item(self) -> None:
        os.makedirs(os.path.join(self.user_data, "ModularData", "ai-agent", "vm"),
                    exist_ok=True)
        for it in self.adp.build_items(self.home):
            self.assertNotIn("ai-agent/vm", it.path.replace("\\", "/"), it.key)

    def test_vm_dir_is_listed_as_runtime_in_status(self) -> None:
        os.makedirs(os.path.join(self.user_data, "ModularData", "ai-agent", "vm"),
                    exist_ok=True)
        notes = " ".join(r["note"] for r in self.adp.detect_data_roots(self.home))
        self.assertIn("沙箱虚拟机", notes)
        self.assertIn("不列入备份", notes)

    def test_status_lists_vm_only_when_present(self) -> None:
        rels = [r["rel"].replace("\\", "/") for r in self.adp.detect_data_roots(self.home)]
        self.assertFalse([p for p in rels if p.endswith("/vm")], rels)

    # --- 3. 同族隔离：key 前缀与结构指纹都不能互相认错 -------------------------- #

    def test_display_name_is_the_product_name(self) -> None:
        """安装目录叫 TRAE SOLO CN，界面必须叫用户认识的 TraeWork CN。"""
        self.assertEqual(self.adp.name, "trae-solo-cn")
        self.assertEqual(self.adp.display_name, "TraeWork CN")

    def test_item_keys_never_collide_with_trae_cn(self) -> None:
        """两个形态同屏勾选：key 相同会让一边覆盖另一边的勾选状态与还原目标。"""
        solo = {it.key for it in self.adp.build_items(self.home)}
        cn = tc.TraeCnAdapter().build_items(self.home)
        cn_keys = {it.key for it in cn}
        self.assertTrue(solo and cn_keys)
        self.assertEqual(solo & cn_keys, set())

    def test_prefix_does_not_break_row_aggregation(self) -> None:
        """前缀改用下划线后，聚合仍按冒号切：主库 / -wal / -shm 合成一行。"""
        aa = os.path.join(self.user_data, "ModularData", "ai-agent")
        for name in ("database.db-wal", "database.db-shm"):
            with open(os.path.join(aa, name), "wb") as f:
                f.write(b"x")
        solo = self.adp.build_items(self.home)
        rows = [it for it in solo if _agg(it.key) == "trae_solo_cn_ai_agent_db"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({it.label for it in rows}), 1)
        for it in solo:
            self.assertTrue(it.key.startswith(ts.KEY_PREFIX_TRAE_SOLO_CN), it.key)

    def test_match_structure_separates_the_two_forms(self) -> None:
        solo_db = "h/TRAE SOLO CN/ModularData/ai-agent/database.db"
        cn_db = "h/Trae CN/ModularData/ai-agent/database.db"
        self.assertTrue(self.adp.match_structure([solo_db])[0])
        self.assertFalse(self.adp.match_structure([cn_db])[0],
                         "TraeCode CN 的包不能被认成 TraeWork CN")
        self.assertFalse(self.adp.match_structure([])[0])
        self.assertTrue(tc.TraeCnAdapter().match_structure([cn_db])[0])
        self.assertFalse(tc.TraeCnAdapter().match_structure([solo_db])[0])

    def test_restore_library_files_is_database_db(self) -> None:
        self.assertEqual(self.adp.RESTORE_LIBRARY_FILES, ("database.db",))


if __name__ == "__main__":
    unittest.main()
