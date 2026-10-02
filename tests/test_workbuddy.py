"""
WorkBuddy 适配器测试。

覆盖范围（对齐设计参考文档里两条最容易被漏掉的约定）：
- ``build_items`` 的条目集合与「无法从零重建的默认勾选」策略；
- 用户级记忆**两套机制各建一条**：``memory/``（服务端记忆画像）与 ``MEMORY.md``
  （本地硬性规则/约定，可被每会话注入），且 ``MEMORY.md`` 允许缺失（按存在性探测）；
- 导出脱敏：``models.json`` 之外的配置类文件（``settings.json`` / ``mcp.json`` /
  ``mcp-approvals.json`` / ``.connectors-marketplace.meta.json``）同样要脱敏，
  且 MCP ``headers`` 内的 ``X-Api-Key``（连字符写法）必须被抹掉；
- ``core.DEFAULT_EXCLUDES`` 对记忆类 ``.bak`` 快照的排除（快照可能仍含服务端已脱掉的
  明文口令 ⇒ 凭证不入包），同时保证活的 ``MEMORY.md`` / ``memory/*.md`` 照常入包；
- ``core.ALWAYS_INCLUDE`` 对 ``~/.workbuddy/workbuddy.db`` 三件套的豁免
  （数据库不在 ``*/cache/db/`` 下，若不豁免，主库会受单文件体积上限约束、
  ``-wal`` / ``-shm`` 会被 ``DEFAULT_EXCLUDES`` 静默过滤）。

运行：
    python -m unittest tests.test_workbuddy -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from ai_env_clone.core import DEFAULT_EXCLUDES, is_critical, is_excluded
from ai_env_clone.adapters.workbuddy import WorkBuddyAdapter, build_items


class TestWorkBuddyItems(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = WorkBuddyAdapter()

    def test_recommended_items_cover_irreplaceable_data(self):
        """默认勾选＝「无法从零重建 + 还原后立刻能开工」两类：
        会话事件流、会话索引库、记忆、人格画像，以及设置 / skill / 模型配置。"""
        items = self.adapter.build_items(None, None)
        rec = {i.key for i in items if i.recommended}
        for key in ("session_db", "projects", "memory", "memory_md", "storage",
                    "file_history", "workspace_sessions", "format_presets",
                    "skills", "settings", "models"):
            self.assertIn(key, rec, "会话/记忆/规则/设置类条目应默认勾选：%s" % key)
        for key in ("plugins", "connectors", "mcp", "mcp_approvals",
                    "connectors_marketplace", "local_storage", "tasks", "teams",
                    "artifact_index", "assistant_display"):
            self.assertNotIn(key, rec, "可重建/需重新授权条目不应默认勾选：%s" % key)

    def test_settings_row_label_is_distinct_from_mcp_rows(self):
        """设置默认勾选、MCP 默认不勾 ⇒ 三行标签必须可区分。

        它们按 key 前缀（settings / mcp / mcp_approvals）各占一行，若共用
        「设置与 MCP 配置」这个标签，界面会出现「三行同样文字、勾选态却不同」。
        """
        items = {i.key: i for i in self.adapter.build_items(None, None)}
        labels = {items[k].label for k in ("settings", "mcp", "mcp_approvals")}
        self.assertEqual(len(labels), 3, "三条标签必须互不相同：%r" % labels)
        self.assertIn("settings.json", items["settings"].label)
        self.assertIn("mcp.json", items["mcp"].label)

    def test_memory_md_is_its_own_entry(self):
        """``MEMORY.md``（用户级本地记忆）与 ``memory/``（服务端记忆画像）是两套机制，
        必须各建一条，不能合并——否则只会带过去其中一半。"""
        items = {i.key: i for i in self.adapter.build_items(None, None)}
        self.assertIn("memory_md", items)
        self.assertIn("memory", items)
        self.assertNotEqual(items["memory_md"].path, items["memory"].path)
        self.assertEqual(
            os.path.relpath(items["memory_md"].path, os.path.expanduser("~")).replace(os.sep, "/"),
            ".workbuddy/MEMORY.md",
        )
        self.assertTrue(items["memory_md"].recommended)

    def test_memory_md_absence_is_tolerated(self):
        """该文件并非每台机器都有：缺失时条目仍在、``exists`` 为假，不报错也不推断数据丢失。"""
        tmp = tempfile.mkdtemp(prefix="wb_items_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wb = os.path.join(tmp, ".workbuddy")
        os.makedirs(wb, exist_ok=True)
        items = {i.key: i for i in build_items(tmp, wb)}
        self.assertIn("memory_md", items)
        self.assertFalse(items["memory_md"].exists)

    def test_memory_md_present_when_file_exists(self):
        tmp = tempfile.mkdtemp(prefix="wb_items_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wb = os.path.join(tmp, ".workbuddy")
        os.makedirs(wb, exist_ok=True)
        with open(os.path.join(wb, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("# 用户级长期记忆\n")
        items = {i.key: i for i in build_items(tmp, wb)}
        self.assertTrue(items["memory_md"].exists)

    def test_all_paths_under_user_home(self):
        """入口是「公共根 = 用户主目录」，所有条目 path 都在 ~ 之下（还原按相对路径落回原位）。"""
        home = os.path.expanduser("~")
        items = self.adapter.build_items(None, None)
        for it in items:
            rel = os.path.relpath(it.path, home)
            self.assertFalse(rel.startswith(".."), "条目越出主目录：%s" % it.path)

    def test_no_credential_entries(self):
        """凭证/设备标识绝不入包（连条目都不建）。"""
        items = self.adapter.build_items(None, None)
        joined = " ".join(i.path for i in items).lower()
        for banned in ("keyblob", "device-id", "credentials", "edge-sync"):
            self.assertNotIn(banned, joined)

    def test_models_json_marked_sensitive(self):
        items = {i.key: i for i in self.adapter.build_items(None, None)}
        self.assertTrue(items["models"].sensitive)


class TestWorkBuddyRedaction(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = WorkBuddyAdapter()
        self.transform = self.adapter.export_transform()

    def test_paths_cover_all_credential_bearing_configs(self):
        paths = self.adapter.export_transform_paths()
        for frag in ("models.json", "settings.json", "mcp.json",
                     "mcp-approvals.json", ".connectors-marketplace.meta.json"):
            self.assertIn(frag, paths)

    def test_mcp_headers_redacted(self):
        """MCP 配置把密钥放在 headers 里，键名形态不固定，必须整块脱敏。"""
        src = json.dumps({"mcpServers": {"s": {
            "type": "http",
            "headers": {"X-Api-Key": "SECRET", "Authorization": "Bearer T0KEN"},
        }}}).encode("utf-8")
        out = json.loads(self.transform(".workbuddy/mcp.json", src).decode("utf-8"))
        hdrs = out["mcpServers"]["s"]["headers"]
        self.assertEqual(hdrs["X-Api-Key"], "***REDACTED***")
        self.assertEqual(hdrs["Authorization"], "***REDACTED***")
        self.assertEqual(out["mcpServers"]["s"]["type"], "http")

    def test_settings_redacted_but_other_fields_kept(self):
        src = json.dumps({"apiKey": "S", "theme": "dark",
                          "nested": {"refreshToken": "S2"}}).encode("utf-8")
        out = json.loads(self.transform(".workbuddy/settings.json", src).decode("utf-8"))
        self.assertEqual(out["apiKey"], "***REDACTED***")
        self.assertEqual(out["nested"]["refreshToken"], "***REDACTED***")
        self.assertEqual(out["theme"], "dark")

    def test_env_ref_and_non_json_preserved(self):
        src = json.dumps({"apiKey": "${MY_KEY}"}).encode("utf-8")
        out = json.loads(self.transform(".workbuddy/models.json", src).decode("utf-8"))
        self.assertEqual(out["apiKey"], "${MY_KEY}")
        raw = b"\x00\x01not-json"
        self.assertEqual(self.transform(".workbuddy/models.json", raw), raw)


class TestWorkBuddyDbExemption(unittest.TestCase):
    """workbuddy.db 不在 */cache/db/ 下，必须单独豁免，否则三件套不变量不成立。"""

    def test_db_trio_is_critical(self):
        for rel in (".workbuddy/workbuddy.db",
                    ".workbuddy/workbuddy.db-wal",
                    ".workbuddy/workbuddy.db-shm"):
            self.assertTrue(is_critical(rel), "%s 应豁免体积上限与排除规则" % rel)

    def test_run_state_dbs_not_exempt(self):
        """同步映射等运行态库及其副本不应被顺带豁免（否则白打进上百个文件）。"""
        for rel in (".workbuddy/edge-sync-mapping.db-wal",
                    ".workbuddy/workspace/sessions/x/modify_backup/"
                    "1.m.ed98f88f.edge-sync-mapping-v4.db-shm"):
            self.assertFalse(is_critical(rel), "%s 不应豁免" % rel)

    def test_memory_snapshots_excluded_but_live_memory_kept(self):
        """记忆类文件的 ``.bak`` 旧快照不入包（可能仍含服务端已脱掉的明文口令），
        而**活的**记忆与规则文件必须照常入包。"""
        uid = "9ae9129b-c0e9-4158-b4cd-983fac049c6d"
        self.assertTrue(
            is_excluded(".workbuddy/memory/%s_memory.md.bak" % uid, DEFAULT_EXCLUDES),
            "记忆画像的旧快照应被排除",
        )
        self.assertTrue(
            is_excluded(".workbuddy/MEMORY.md.bak", DEFAULT_EXCLUDES),
            "本地记忆的旧快照应被排除",
        )
        for kept in (".workbuddy/memory/%s_memory.md" % uid,
                     ".workbuddy/MEMORY.md",
                     ".workbuddy/SOUL.md"):
            self.assertFalse(is_excluded(kept, DEFAULT_EXCLUDES),
                             "活文件不应被误排除：%s" % kept)

    def test_wal_shm_still_excluded_by_default_rule(self):
        """排除规则本身不变：豁免靠 ALWAYS_INCLUDE，而不是放开 DEFAULT_EXCLUDES。"""
        self.assertTrue(is_excluded(".workbuddy/workbuddy.db-wal", DEFAULT_EXCLUDES))

    def test_build_items_uses_injected_home(self):
        """数据根可注入，便于测试与「数据目录」改到别处时不读写真实用户数据。"""
        tmp = tempfile.mkdtemp(prefix="wb_items_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wb = os.path.join(tmp, ".workbuddy")
        os.makedirs(os.path.join(wb, "projects"), exist_ok=True)
        # session_db 条目只在库文件存在时生成（与 Qoder 一致：无文件即无条目）
        with open(os.path.join(wb, "workbuddy.db"), "wb") as f:
            f.write(b"SQLite format 3\x00")
        items = build_items(tmp, wb)
        rels = {os.path.relpath(i.path, tmp).replace(os.sep, "/") for i in items}
        self.assertIn(".workbuddy/projects", rels)
        self.assertIn(".workbuddy/workbuddy.db", rels)

    def test_missing_db_yields_no_session_db_item(self):
        """库文件缺失时不生成 session_db 条目（避免为一处不存在的路径建条目）。"""
        tmp = tempfile.mkdtemp(prefix="wb_items_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wb = os.path.join(tmp, ".workbuddy")
        os.makedirs(wb, exist_ok=True)
        keys = {i.key for i in build_items(tmp, wb)}
        self.assertFalse(any(k.startswith("session_db") for k in keys))


if __name__ == "__main__":
    unittest.main(verbosity=2)
