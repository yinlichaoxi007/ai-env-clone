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
"""

import os
import tempfile
import unittest

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.adapters import trae_cn as tc


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


if __name__ == "__main__":
    unittest.main()
