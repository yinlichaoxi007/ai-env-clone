"""
CodeBuddy 适配器测试用例。

运行：
    python -m unittest tests.test_codebuddy -v
或：
    python tests/test_codebuddy.py

备份范围铁律（用户 2026-08-07 明确）：**只备份不在项目文件夹下的用户级/全局数据**。
所有条目 path 均在公共根 ``~`` 之下。本项目级 ``.codebuddy`` **绝不**出现在清单中。
用户级规则 ``test.mdc`` 整体备份（不排除任何特定文件）。
集中会话/检查点（``Data/<uuid>/CodeBuddyIDE/<uuid>/``）需备份，``history/`` ``check-point/``
``plan-task/`` 默认勾选；工程索引缓存 ``file-tree/`` 不列入备份选项。
"""

from __future__ import annotations

import os
import shutil
import sys
import json
import tempfile
import unittest
from unittest import mock

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.adapters import codebuddy as cb_mod
from ai_env_clone.core import BackupItem
from ai_env_clone.adapters.codebuddy import (
    CodeBuddyAdapter,
    _codebuddy_extension_data_root,
    _rule_item,
    build_items,
    detect_current_uid,
    detect_global_root,
    detect_public_memories_root,
    detect_session_root,
    detect_user_rules_root,
    detect_user_uids,
    short_uid,
)


class TempEnv(unittest.TestCase):
    """构造仿真的 CodeBuddy 用户级 / 全局目录树（绝不碰任何项目级 .codebuddy）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="codebuddy_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # 用户级跨项目记忆（对应 %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/.memories）
        self.memories_root = os.path.join(self.tmp, "memories")
        # 用户级规则（对应 ~/.codebuddy/rules）
        self.rules_root = os.path.join(self.tmp, "rules")
        # 用户级 .codebuddy 根（含 settings/skills/mcp/inspiration/expert/plugins）
        self.cb_home = os.path.join(self.tmp, "cb_home")
        # 全局 IDE 配置（对应 ~/.codebuddycn）
        self.global_root = os.path.join(self.tmp, "codebuddycn")
        # 集中会话根（对应 Data/<uuid>/CodeBuddyIDE/<uuid>）
        self.uuid = "9ae9129b-c0e9-4158-b4cd-983fac049c6d"
        self.session_root = os.path.join(
            self.tmp, "data", self.uuid, "CodeBuddyIDE", self.uuid
        )
        self._make_tree()

    def _w(self, base: str, rel: str, content: str = "x") -> str:
        p = os.path.join(base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _make_tree(self) -> None:
        # 用户级跨项目记忆
        self._w(self.memories_root, ".memory-global-config.json", '{"enabled":true}')
        self._w(self.memories_root, "66049367.mdc", "# 用户偏好：默认中文\n")
        # 用户级规则：一条真实规则 + 测试规则 test.mdc（整体备份，不排除）
        self._w(self.rules_root, "coding-style.mdc", "# 编码风格\n")
        self._w(self.rules_root, "test.mdc", "# 测试\n")
        # 用户级 .codebuddy 根
        self._w(self.cb_home, "settings.json", '{"enabledPlugins":[]}')
        self._w(self.cb_home, "skills-marketplace/SKILL.md", "# skill\n")
        self._w(self.cb_home, "mcp.json", "{}")
        self._w(self.cb_home, "inspiration/default/cards/c1.md", "card")
        self._w(self.cb_home, "expert-history.json", '{"sessions":{}}')
        self._w(self.cb_home, "plugins/ext/main.js", "// ext")
        # 全局 ~/.codebuddycn
        self._w(self.global_root, "argv.json", '{"locale":"zh-cn"}')
        self._w(self.global_root, "extensions/ext/main.js", "// ext")
        # 集中会话/检查点（Data/<uuid>/CodeBuddyIDE/<uuid>/）
        self._w(self.session_root, "history/index.json", '{"sessions":[]}')
        self._w(self.session_root, "history/messages/1.json", "{}")
        self._w(self.session_root, "check-point/1.json", "{}")
        self._w(self.session_root, "plan-task/1.json", "{}")

        # 让适配器内部探测函数指向临时目录（真实环境里 LOCALAPPDATA 也在 ~ 下，
        # 这里用 monkeypatch 保证所有条目 path 都在 self.tmp 之下，便于断言）。
        patcher = mock.patch.object(
            cb_mod, "detect_public_memories_root", return_value=self.memories_root
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(
            cb_mod, "detect_user_rules_root", return_value=self.rules_root
        )
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = mock.patch.object(
            cb_mod, "detect_global_root", return_value=self.global_root
        )
        patcher3.start()
        self.addCleanup(patcher3.stop)
        patcher4 = mock.patch.object(
            cb_mod, "detect_session_root", return_value=self.session_root
        )
        patcher4.start()
        self.addCleanup(patcher4.stop)
        # 固定当前用户与用户列表，避免测试时去真实机器 AppData 探测
        patcher5 = mock.patch.object(
            cb_mod, "detect_current_uid", return_value=self.uuid
        )
        patcher5.start()
        self.addCleanup(patcher5.stop)
        patcher6 = mock.patch.object(
            cb_mod, "detect_user_uids", return_value=[self.uuid]
        )
        patcher6.start()
        self.addCleanup(patcher6.stop)


class TestDetect(unittest.TestCase):
    def test_memories_root_under_localappdata(self) -> None:
        root = detect_public_memories_root()
        self.assertIn("CodeBuddyExtension", root)
        self.assertIn(".memories", root)

    def test_rules_root_under_home(self) -> None:
        self.assertTrue(detect_user_rules_root().endswith(os.path.join(".codebuddy", "rules")))

    def test_global_root_is_home(self) -> None:
        self.assertTrue(detect_global_root().endswith(".codebuddycn"))

    def test_detect_root_is_home(self) -> None:
        a = get_adapter("codebuddy")
        self.assertEqual(a.detect_root(), os.path.expanduser("~"))


class TestBuildItems(TempEnv):
    def setUp(self) -> None:
        super().setUp()
        self.items = build_items(self.tmp, self.global_root)
        self.by_key = {it.key: it for it in self.items}
        self.prefixes = {it.key.split(":", 1)[0] for it in self.items}

    def test_all_paths_under_root(self) -> None:
        # 所有 item 都应在公共根 self.tmp 之下（归档相对路径干净）
        for it in self.items:
            self.assertTrue(
                os.path.normpath(it.path).startswith(os.path.normpath(self.tmp)),
                "path 不在公共根下: %s" % it.path,
            )

    def test_no_project_level_codebuddy(self) -> None:
        # 绝不应出现项目级 <project>/.codebuddy 的内容标记（如 memory/ 等）
        self.assertNotIn("memory", self.by_key)
        self.assertNotIn("settings", self.by_key)  # 项目级 settings 不存在；用户级用的是 user_skill:settings

    def test_expected_prefixes(self) -> None:
        expected = {
            "user_memories",
            "user_rules",
            "user_skill",
            "user_mcp",
            "global_argv",
            "global_extensions",
            "user_sessions",
            "user_inspiration",
            "user_expert_history",
            "user_plugins",
            "user_models",
        }
        self.assertEqual(self.prefixes, expected)

    def test_recommended_defaults(self) -> None:
        # 默认勾选：用户记忆 / 用户规则 / 集中会话（history/check-point/plan-task）
        self.assertTrue(self.by_key["user_memories"].recommended)
        # 规则项（单一 key user_rules）默认勾选
        self.assertTrue(self.by_key["user_rules"].recommended)
        # 集中会话：history/check-point/plan-task 默认勾选
        self.assertTrue(self.by_key["user_sessions:history"].recommended)
        self.assertTrue(self.by_key["user_sessions:checkpoint"].recommended)
        self.assertTrue(self.by_key["user_sessions:plan_task"].recommended)
        # 默认不勾：设置类（argv）、skill 设置/本体、mcp、扩展、灵感、专家历史、插件
        self.assertFalse(self.by_key["global_argv"].recommended)
        self.assertFalse(self.by_key["user_skill:settings"].recommended)
        self.assertFalse(self.by_key["user_skill:skills"].recommended)
        self.assertFalse(self.by_key["user_mcp"].recommended)
        self.assertFalse(self.by_key["global_extensions"].recommended)
        self.assertFalse(self.by_key["user_inspiration"].recommended)
        self.assertFalse(self.by_key["user_expert_history"].recommended)
        self.assertFalse(self.by_key["user_plugins"].recommended)
        # 自定义模型配置：默认不勾（含明文 apiKey，属敏感设置类）
        self.assertFalse(self.by_key["user_models"].recommended)
        self.assertTrue(self.by_key["user_models"].sensitive)

    def test_user_skill_merged(self) -> None:
        # settings.json 与 skills-marketplace/ 共享聚合前缀 user_skill（GUI 合成一行）
        self.assertIn("user_skill:settings", self.by_key)
        self.assertIn("user_skill:skills", self.by_key)
        self.assertEqual(self.by_key["user_skill:settings"].label,
                         self.by_key["user_skill:skills"].label)

    def test_sessions_have_current_uid(self) -> None:
        # 集中会话项必须带当前用户 uid（供 GUI 当前用户下拉与绑定）
        session_items = [it for it in self.items if it.key.startswith("user_sessions:")]
        self.assertTrue(session_items, "应存在集中会话项")
        for it in session_items:
            self.assertEqual(it.uid, self.uuid)
        # 用户级/全局项（记忆、规则、灵感、技能、mcp、专家历史、扩展、argv）无 uid 维度
        no_uid_keys = (
            "user_memories",
            "user_rules",
            "global_argv",
            "user_inspiration",
            "user_skill:settings",
            "user_skill:skills",
            "user_mcp",
            "user_expert_history",
            "user_plugins",
            "global_extensions",
        )
        for key in no_uid_keys:
            if key in self.by_key:
                self.assertIsNone(self.by_key[key].uid, "%s 不应带 uid" % key)

    def test_no_other_users_when_single_uid(self) -> None:
        # 单用户（detect_user_uids 仅当前 uuid）：不应生成 user_sessions_others 项
        others = [it for it in self.items if it.key.startswith("user_sessions_others:")]
        self.assertEqual(others, [])

    def test_paths_resolved(self) -> None:
        self.assertEqual(self.by_key["user_memories"].path, self.memories_root)
        self.assertEqual(self.by_key["global_argv"].path,
                         os.path.join(self.global_root, "argv.json"))
        self.assertTrue(self.by_key["user_skill:settings"].path.endswith("settings.json"))
        self.assertTrue(self.by_key["user_skill:skills"].path.endswith("skills-marketplace"))
        # 集中会话各项指向 session_root 下的子目录
        self.assertEqual(self.by_key["user_sessions:history"].path,
                         os.path.join(self.session_root, "history"))


class TestModelsJsonRedaction(unittest.TestCase):
    """
    CodeBuddy 自定义模型配置（models.json）可能含明文敏感凭证（apiKey、token、私人令牌等）。
    备份时须脱敏：敏感字段替换为占位符，且配置结构（模型条目/url 等）保留，
    使恢复后模型仍可见、仅凭证失效需用户手动补填。覆盖两种取值形态：
    明文凭证 与 环境变量引用（${ENV}），后者不应被改写。
    """

    SAMPLE = {
        "models": [
            {
                "name": "my-deepseek",
                "url": "https://api.deepseek.com/v1",
                "apiKey": "sk-real-secret-0123456789abcdef",
                "enabled": True,
            },
            {
                "name": "token-model",
                "url": "https://api.example.com/v1",
                "token": "tk-real-secret-abcdef123456",
                "enabled": True,
            },
            {
                "name": "env-model",
                "url": "https://api.example.com/v1",
                "apiKey": "${MY_API_KEY}",
                "enabled": False,
            },
        ],
        "version": 1,
    }

    def setUp(self) -> None:
        self.adp = CodeBuddyAdapter()

    def test_export_transform_paths_declares_models_json(self) -> None:
        paths = self.adp.export_transform_paths()
        self.assertIsNotNone(paths)
        self.assertIn("models.json", paths)

    def test_transform_redacts_plaintext_apikey(self) -> None:
        cb = self.adp.export_transform()
        self.assertIsNotNone(cb)
        raw = json.dumps(self.SAMPLE, ensure_ascii=False).encode("utf-8")
        out = cb("C__Users_x/.codebuddy/models.json", raw)
        data = json.loads(out.decode("utf-8"))
        # 明文 apiKey 被脱敏
        self.assertEqual(data["models"][0]["apiKey"], "***REDACTED***")
        # 其余字段保留
        self.assertEqual(data["models"][0]["url"], "https://api.deepseek.com/v1")
        self.assertEqual(data["models"][0]["name"], "my-deepseek")
        self.assertEqual(data["version"], 1)

    def test_transform_redacts_other_sensitive_field(self) -> None:
        # 非 apiKey 的敏感字段（token / 私人令牌等）同样脱敏，不写死 apiKey
        cb = self.adp.export_transform()
        raw = json.dumps(self.SAMPLE, ensure_ascii=False).encode("utf-8")
        out = cb("C__Users_x/.codebuddy/models.json", raw)
        data = json.loads(out.decode("utf-8"))
        self.assertEqual(data["models"][1]["token"], "***REDACTED***")
        self.assertEqual(data["models"][1]["url"], "https://api.example.com/v1")

    def test_transform_keeps_env_reference(self) -> None:
        # 环境变量引用形式（${...}）不属于明文凭证，不应被改写
        cb = self.adp.export_transform()
        raw = json.dumps(self.SAMPLE, ensure_ascii=False).encode("utf-8")
        out = cb("C__Users_x/.codebuddy/models.json", raw)
        data = json.loads(out.decode("utf-8"))
        self.assertEqual(data["models"][2]["apiKey"], "${MY_API_KEY}")

    def test_transform_no_apikey_returns_original(self) -> None:
        # 无 apiKey 字段时原样返回（字节一致）
        cb = self.adp.export_transform()
        sample = {"models": [{"name": "x", "url": "https://u"}]}
        raw = json.dumps(sample, ensure_ascii=False).encode("utf-8")
        out = cb("a/b/models.json", raw)
        self.assertEqual(out, raw)

    def test_transform_non_json_returns_original(self) -> None:
        cb = self.adp.export_transform()
        raw = b"this is not json"
        self.assertEqual(cb("a/b/models.json", raw), raw)

    def test_export_round_trip_redacts_in_zip(self) -> None:
        # 端到端：导出后 zip 内 models.json 应为脱敏版，明文密钥不落盘
        tmp = tempfile.mkdtemp(prefix="cb_models_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cb_home = os.path.join(tmp, "cb_home")
        os.makedirs(cb_home, exist_ok=True)
        models_path = os.path.join(cb_home, "models.json")
        with open(models_path, "w", encoding="utf-8") as f:
            json.dump(self.SAMPLE, f, ensure_ascii=False)

        # 构造仅含 user_models 的条目并导出
        items = [
            BackupItem(
                key="user_models",
                label="自定义模型配置",
                path=models_path,
                description="",
                recommended=False,
                sensitive=True,
            )
        ]
        zip_path = os.path.join(tmp, "out.zip")
        self.adp.export(zip_path, tmp, items, progress=lambda *a: None)

        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            arc = [n for n in zf.namelist() if n.endswith("models.json")][0]
            content = zf.read(arc).decode("utf-8")
        self.assertNotIn("sk-real-secret-0123456789abcdef", content)
        self.assertIn("***REDACTED***", content)
        self.assertIn("${MY_API_KEY}", content)


class TestMultiUser(unittest.TestCase):
    """多 UUID 用户：其他用户生成 user_sessions_others 项，且 UUID 列表/当前用户探测正确。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cb_multi_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 构造一个临时 Data 根，含多个 UUID 目录（不同 mtime 区分当前用户）
        self.data_root = os.path.join(self.tmp, "Data")
        os.makedirs(self.data_root, exist_ok=True)
        self.uids = [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ]
        for i, uid in enumerate(self.uids):
            d = os.path.join(self.data_root, uid)
            os.makedirs(os.path.join(d, "CodeBuddyIDE", uid), exist_ok=True)
            # 显式设置 Data/<uid> 目录 mtime，避免子目录创建改掉父目录 mtime 造成误判。
            # 让中间的 uid（index 1）mtime 最新 -> 判定为当前用户。
            mtime = 2000 if i == 1 else 1000 + i
            os.utime(d, (mtime, mtime))
        # 当前用户设为中间的 uid（最新 mtime）
        self.current = self.uids[1]

    def _build_with(self, current_uid=None):
        with mock.patch.object(cb_mod, "detect_public_memories_root",
                               return_value=os.path.join(self.tmp, "memories")), \
             mock.patch.object(cb_mod, "detect_user_rules_root",
                               return_value=os.path.join(self.tmp, "rules")), \
             mock.patch.object(cb_mod, "detect_global_root",
                               return_value=os.path.join(self.tmp, "codebuddycn")), \
             mock.patch.object(cb_mod, "_codebuddy_extension_data_root",
                               return_value=self.data_root):
            return build_items(self.tmp, current_uid=current_uid)

    def test_detect_user_uids_scans_data(self) -> None:
        with mock.patch.object(cb_mod, "_codebuddy_extension_data_root",
                               return_value=self.data_root):
            self.assertEqual(sorted(detect_user_uids()), sorted(self.uids))

    def test_detect_current_uid_picks_newest(self) -> None:
        with mock.patch.object(cb_mod, "_codebuddy_extension_data_root",
                               return_value=self.data_root):
            self.assertEqual(detect_current_uid(), self.current)

    def test_other_users_generated(self) -> None:
        items = self._build_with(current_uid=self.current)
        others = [it for it in items if it.key.startswith("user_sessions_others:")]
        # 除当前用户外的 2 个 uuid 各生成 4 个 sub 项
        self.assertEqual(len(others), 2 * 4)
        other_uids = {it.uid for it in others}
        self.assertEqual(other_uids, {self.uids[0], self.uids[2]})
        # 当前用户不应出现在 others 里
        self.assertNotIn(self.current, other_uids)
        # 当前用户 sessions 项 uid 正确
        cur_sessions = [it for it in items if it.key.startswith("user_sessions:")]
        self.assertTrue(all(it.uid == self.current for it in cur_sessions))

    def test_short_uid_truncates(self) -> None:
        self.assertEqual(short_uid(self.uids[0]), self.uids[0][:8] + "…")
        # 短串回退：不足长度原样
        self.assertEqual(short_uid("abc"), "abc")

    def test_preview_path_rewrite_detects_different_user(self) -> None:
        # 归档来自另一台电脑（源 UUID 与当前不同）-> will_rewrite=True
        src = self.uids[0]  # 非当前用户
        entries = [
            "CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/history/abc/index.json"
            % (src, src),
        ]
        with mock.patch.object(cb_mod, "_codebuddy_extension_data_root",
                               return_value=self.data_root), \
             mock.patch.object(cb_mod, "detect_current_uid",
                               return_value=self.current):
            adapter = get_adapter("codebuddy")
            preview = adapter.preview_path_rewrite(entries)
        self.assertTrue(preview["will_rewrite"])
        self.assertEqual(preview["source_uids"], [src])
        self.assertEqual(preview["current_uid"], self.current)

    def test_preview_path_rewrite_same_user_no_rewrite(self) -> None:
        # 归档来自同一用户（源 UUID == 当前）-> will_rewrite=False
        entries = [
            "CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/history/abc/index.json"
            % (self.current, self.current),
        ]
        with mock.patch.object(cb_mod, "_codebuddy_extension_data_root",
                               return_value=self.data_root), \
             mock.patch.object(cb_mod, "detect_current_uid",
                               return_value=self.current):
            adapter = get_adapter("codebuddy")
            preview = adapter.preview_path_rewrite(entries)
        self.assertFalse(preview["will_rewrite"])


class TestRuleItem(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cb_rules_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rules_root = os.path.join(self.tmp, "rules")
        os.makedirs(self.rules_root, exist_ok=True)
        with open(os.path.join(self.rules_root, "coding-style.mdc"), "w", encoding="utf-8") as f:
            f.write("# 编码风格\n")
        # test.mdc 不再排除：整体备份 rules/ 目录（之前仅为测试要求排除，非真实需求）
        with open(os.path.join(self.rules_root, "test.mdc"), "w", encoding="utf-8") as f:
            f.write("# 测试\n")

    def test_rule_item_backs_up_whole_dir(self) -> None:
        it = _rule_item(self.rules_root)
        self.assertEqual(it.key, "user_rules")
        self.assertEqual(it.path, self.rules_root)
        self.assertTrue(it.recommended)
        # 整体目录备份（不枚举单个 .mdc，不排除 test.mdc）
        self.assertTrue(it.exists)

    def test_rule_item_missing_dir(self) -> None:
        missing = os.path.join(self.tmp, "no_rules_dir")
        it = _rule_item(missing)
        self.assertEqual(it.key, "user_rules")
        self.assertFalse(it.exists)


class TestStructureStability(unittest.TestCase):
    """路径不存在时，选项结构仍稳定（路径识别失败也不让清单忽有忽无）。"""

    def test_missing_root_still_lists_all_prefixes(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), "no_such_home_xyz")
        with mock.patch.object(cb_mod, "detect_public_memories_root",
                               return_value=os.path.join(missing, "memories")), \
             mock.patch.object(cb_mod, "detect_user_rules_root",
                               return_value=os.path.join(missing, "rules")), \
             mock.patch.object(cb_mod, "detect_global_root",
                               return_value=os.path.join(missing, "codebuddycn")), \
             mock.patch.object(cb_mod, "detect_session_root",
                               return_value=os.path.join(missing, "data", "x",
                                                         "CodeBuddyIDE", "x")):
            items = build_items(missing, os.path.join(missing, "codebuddycn"),
                                None, os.path.join(missing, "data", "x",
                                                   "CodeBuddyIDE", "x"))
        prefixes = {it.key.split(":", 1)[0] for it in items}
        self.assertIn("user_memories", prefixes)
        self.assertIn("user_rules", prefixes)
        self.assertIn("user_skill", prefixes)
        self.assertIn("user_sessions", prefixes)
        # 路径不存在 -> exists 为 False（GUI 显示未找到、不勾选，但选项不消失）
        self.assertFalse(items[0].exists)


class TestCrossPlatform(unittest.TestCase):
    """``_codebuddy_extension_data_root`` 在不同平台应映射到各自的 Extension 根。"""

    def _data_root_for(self, platform: str) -> str:
        with mock.patch.object(sys, "platform", platform):
            return _codebuddy_extension_data_root()

    def test_windows_data_root(self) -> None:
        got = self._data_root_for("win32")
        # LOCALAPPDATA 在本环境可能未设，回退 ~/AppData/Local/CodeBuddyExtension/Data
        self.assertTrue(got.replace("\\", "/").endswith(
            "CodeBuddyExtension/Data"))

    def test_darwin_data_root(self) -> None:
        got = self._data_root_for("darwin")
        self.assertEqual(
            got,
            os.path.join(os.path.expanduser("~"),
                         "Library", "Application Support",
                         "CodeBuddyExtension", "Data"),
        )

    def test_linux_data_root(self) -> None:
        got = self._data_root_for("linux")
        self.assertEqual(
            got,
            os.path.join(os.path.expanduser("~"), ".config",
                         "CodeBuddyExtension", "Data"),
        )

    def test_memories_uses_data_root(self) -> None:
        # memories 路径不应再重复拼 CodeBuddyExtension（历史 bug 回归防护）
        with mock.patch.object(sys, "platform", "linux"):
            mr = detect_public_memories_root()
        self.assertEqual(
            mr,
            os.path.join(os.path.expanduser("~"), ".config",
                         "CodeBuddyExtension", "Data", "Public", ".memories"),
        )
        self.assertNotIn("CodeBuddyExtension/CodeBuddyExtension",
                         mr.replace("\\", "/"))


class TestRegistration(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("codebuddy", list_adapters())
        self.assertIn("qoder", list_adapters())

    def test_adapter_interface(self) -> None:
        a = get_adapter("codebuddy")
        self.assertEqual(a.name, "codebuddy")
        self.assertEqual(a.display_name, "CodeBuddy")
        root = a.detect_root()
        self.assertEqual(root, os.path.expanduser("~"))
        items = a.build_items(root)
        self.assertIsInstance(items, list)
        self.assertGreater(len(items), 0)


@unittest.skipUnless(
    os.name == "nt",
    "长路径（>260 字符）超出 MAX_PATH 限制是 Windows 特有问题",
)
class TestLongPathSessionRoundTrip(TempEnv):
    """回归：CodeBuddy 会话消息文件层级深、绝对路径常超 260 字符，
    且消息体含 role 区分（user/assistant/tool）、parentId 树状关联、tool_calls 关联。
    修复前 scan_items 的 getsize/open 因 WinError 3 静默丢弃全部 messages，
    导致还原后「有会话列表、点开无内容」。本用例固化长路径文件能被完整备份+还原，
    且内容（逐字符 + 真实语义片段）原样往返。"""

    # 真实语义内容片段：还原后仍需能检索到，验证「内容未丢、非简单密文」
    REAL_USER_TEXT = "codebuddy恢复数据缺少会话详细内容的问题，建议你匹配一下我真实发的内容片段"
    REAL_ASSISTANT_TEXT = "会话内容以明文 JSON 存储，role 区分 user/assistant/tool，并用 parentId 串联成树"
    REAL_TOOL_OUTPUT = "scan_items 已用 _longpath 修复 Windows 260 长路径限制"

    def _make_deep_session(self):
        # 构造一条绝对路径显著超过 260 字符、且含真实角色/关联结构的会话
        from ai_env_clone.core import _longpath

        ws = "a" * 60  # 长目录名，保证绝对路径 > 260
        sid = "b" * 60
        hist_dir = os.path.join(self.session_root, "history", ws, sid)
        msg_dir = os.path.join(hist_dir, "messages")
        os.makedirs(_longpath(msg_dir), exist_ok=True)

        # index.json 描述会话元信息与消息树（标题可被列表显示）
        index = {
            "title": "恢复数据缺少会话详细内容排查",
            "messages": [
                {"id": "m1", "role": "user", "parentId": None},
                {"id": "m2", "role": "assistant", "parentId": "m1"},
                {"id": "m3", "role": "tool", "parentId": "m2", "toolCallId": "tc1"},
                {"id": "m4", "role": "assistant", "parentId": "m3", "toolCallId": "tc1"},
            ],
        }
        with open(_longpath(os.path.join(hist_dir, "index.json")), "w", encoding="utf-8") as f:
            f.write(json.dumps(index, ensure_ascii=False, indent=2))

        # messages/<id>.json：message 字段是二次 json.loads 的 JSON 字符串（明文，含 role/关联）
        bodies = {
            "m1": {"role": "user", "content": self.REAL_USER_TEXT, "parentId": None},
            "m2": {
                "role": "assistant", "content": self.REAL_ASSISTANT_TEXT,
                "parentId": "m1",
                "tool_calls": [{"id": "tc1", "type": "function",
                                "function": {"name": "grep", "arguments": '{"q":"_longpath"}'}}],
            },
            "m3": {
                "role": "tool", "content": self.REAL_TOOL_OUTPUT,
                "parentId": "m2", "tool_call_id": "tc1",
            },
            "m4": {
                "role": "assistant", "content": "已定位根因并修复，长路径会话消息完整还原。",
                "parentId": "m3", "tool_call_id": "tc1",
            },
        }
        sources = {}
        for mid, body in bodies.items():
            p = os.path.join(msg_dir, mid + ".json")
            with open(_longpath(p), "w", encoding="utf-8") as f:
                f.write(json.dumps({"id": mid, "message": json.dumps(body, ensure_ascii=False)},
                                   ensure_ascii=False, indent=2))
            sources[os.path.relpath(p, self.tmp)] = p
        sources[os.path.relpath(os.path.join(hist_dir, "index.json"), self.tmp)] = \
            os.path.join(hist_dir, "index.json")

        longest = max(len(p) for p in sources.values())
        return sources, longest

    def test_deep_messages_content_survives_round_trip(self) -> None:
        sources, longest = self._make_deep_session()
        self.assertGreater(
            longest, 260, "测试前置：会话消息绝对路径应超过 260 字符（实际 %d）" % longest
        )

        items = build_items(self.tmp, self.global_root)
        sel = [it for it in items if it.key == "user_sessions:history"]
        self.assertTrue(sel, "应存在 user_sessions:history 备份项")

        zip_path = os.path.join(self.tmp, "sess.zip")
        adp = get_adapter("codebuddy")
        adp.export(zip_path, self.tmp, sel, progress=lambda *a: None)
        self.assertTrue(os.path.exists(zip_path))

        restore_root = os.path.join(self.tmp, "restore")
        os.makedirs(restore_root, exist_ok=True)
        res = adp.restore(zip_path, restore_root, progress=lambda *a: None)

        # 还原报告不应有长路径文件被 blocked
        self.assertEqual(
            res["blocked"], [],
            "长路径会话消息不应被还原阻塞: %s" % res["blocked"][:3]
        )

        # 逐字符比对：每个源文件还原后内容完全一致（验证「内容未丢、非密文、关联结构 intact」）
        from ai_env_clone.core import _longpath
        for rel, src_path in sources.items():
            target = os.path.join(restore_root, rel)
            self.assertTrue(os.path.isfile(_longpath(target)),
                            "还原后缺失文件: %s" % rel)
            with open(_longpath(src_path), encoding="utf-8") as f:
                want = f.read()
            with open(_longpath(target), encoding="utf-8") as f:
                got = f.read()
            self.assertEqual(got, want, "内容不一致: %s" % rel)

        # 语义片段检索：把还原后的全部 messages 内容拼起来，确认真实话语原样存在
        blob_parts = []
        deep_dir = os.path.join(
            restore_root, "data", self.uuid, "CodeBuddyIDE", self.uuid, "history"
        )
        for dp, _, fs in os.walk(_longpath(deep_dir)):
            for fn in fs:
                if fn.endswith(".json"):
                    with open(_longpath(os.path.join(dp, fn)), encoding="utf-8") as f:
                        blob_parts.append(f.read())
        blob = "\n".join(blob_parts)
        for snippet in (self.REAL_USER_TEXT, self.REAL_ASSISTANT_TEXT, self.REAL_TOOL_OUTPUT):
            self.assertIn(snippet, blob, "真实语义片段未原样还原: %r" % snippet[:20])


if __name__ == "__main__":
    unittest.main(verbosity=2)
