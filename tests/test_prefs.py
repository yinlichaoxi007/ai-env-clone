"""偏好持久化（``prefs.json``）测试：读-改-写、默认值、频率判定。

重点是那条容易被回归掉的性质：**保存 ``last_tool`` 不能抹掉 ``update`` 段**。
旧实现是 ``json.dump({"last_tool": name})`` 整文件覆盖，一旦有人照抄回去，
「切一次工具就会把自动检查开关、代理设置全清空」这条 bug 就会复现。

全部用例隔离到临时目录（mock ``compress_estimate.cache_dir``），不触碰真实用户缓存。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from ai_env_clone import compress_estimate, prefs


def _mock_cache_dir(tmp: str):
    return mock.patch.object(compress_estimate, "cache_dir", return_value=tmp)


class TestReadModifyWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="prefs_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _read_raw(self) -> dict:
        with open(prefs.prefs_path(), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_save_last_tool_keeps_update_section(self) -> None:
        """★ 核心回归钉：保存 last_tool 不得抹掉 update 段。"""
        with _mock_cache_dir(self.tmp):
            prefs.save_update_settings(auto_check=True, proxy="127.0.0.1:7897")
            prefs.save_last_tool("dsh")
            self.assertEqual(prefs.update_settings()["auto_check"], True)
            self.assertEqual(prefs.update_settings()["proxy"], "127.0.0.1:7897")
            self.assertEqual(self._read_raw()["last_tool"], "dsh")

    def test_save_update_keeps_last_tool(self) -> None:
        with _mock_cache_dir(self.tmp):
            prefs.save_last_tool("qoder")
            prefs.save_update_settings(interval="weekly")
            self.assertEqual(prefs.load_last_tool(), "qoder")
            self.assertEqual(prefs.update_settings()["interval"], "weekly")

    def test_partial_update_patch_keeps_other_keys(self) -> None:
        with _mock_cache_dir(self.tmp):
            prefs.save_update_settings(auto_check=True, interval="monthly", proxy="p:1")
            prefs.save_update_settings(interval="daily")
            cfg = prefs.update_settings()
            self.assertEqual(cfg["auto_check"], True)
            self.assertEqual(cfg["proxy"], "p:1")
            self.assertEqual(cfg["interval"], "daily")

    def test_missing_file_returns_defaults(self) -> None:
        with _mock_cache_dir(self.tmp):
            cfg = prefs.update_settings()
            self.assertFalse(cfg["auto_check"])
            self.assertEqual(cfg["interval"], "daily")
            self.assertIsNone(prefs.load_last_tool())

    def test_corrupt_json_falls_back(self) -> None:
        with _mock_cache_dir(self.tmp):
            with open(prefs.prefs_path(), "w", encoding="utf-8") as f:
                f.write("{not valid json")
            self.assertIsNone(prefs.load_last_tool())
            self.assertFalse(prefs.update_settings()["auto_check"])

    def test_wrong_types_fall_back(self) -> None:
        with _mock_cache_dir(self.tmp):
            with open(prefs.prefs_path(), "w", encoding="utf-8") as f:
                json.dump({"last_tool": 123, "update": "not-a-dict"}, f)
            self.assertIsNone(prefs.load_last_tool())
            cfg = prefs.update_settings()
            self.assertIsInstance(cfg["auto_check"], bool)
            self.assertIn(cfg["interval"], prefs.CHECK_INTERVALS)

    def test_unknown_interval_falls_back(self) -> None:
        with _mock_cache_dir(self.tmp):
            prefs.save_update_settings(interval="every-hour")
            self.assertEqual(prefs.update_settings()["interval"], "daily")

    def test_write_failure_is_silent(self) -> None:
        with _mock_cache_dir(self.tmp):
            with mock.patch("builtins.open", side_effect=OSError("模拟写失败")):
                self.assertFalse(prefs.save_prefs({"last_tool": "qoder"}))

    def test_save_prefs_rejects_non_dict(self) -> None:
        with _mock_cache_dir(self.tmp):
            self.assertFalse(prefs.save_prefs("nope"))  # type: ignore[arg-type]


class TestIntervalDecision(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="prefs_int_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_disabled_never_checks(self) -> None:
        with _mock_cache_dir(self.tmp):
            self.assertFalse(prefs.should_check_now({"auto_check": False, "interval": "startup"}))

    def test_startup_always_checks(self) -> None:
        with _mock_cache_dir(self.tmp):
            self.assertTrue(
                prefs.should_check_now(
                    {"auto_check": True, "interval": "startup", "last_check": datetime.now().isoformat()}
                )
            )

    def test_daily_not_due_yet(self) -> None:
        with _mock_cache_dir(self.tmp):
            recent = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
            self.assertFalse(
                prefs.should_check_now(
                    {"auto_check": True, "interval": "daily", "last_check": recent}
                )
            )

    def test_daily_due_after_a_day(self) -> None:
        with _mock_cache_dir(self.tmp):
            old = (datetime.now() - timedelta(days=1, minutes=5)).isoformat(timespec="seconds")
            self.assertTrue(
                prefs.should_check_now(
                    {"auto_check": True, "interval": "daily", "last_check": old}
                )
            )

    def test_monthly_not_due_after_a_week(self) -> None:
        with _mock_cache_dir(self.tmp):
            week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
            self.assertFalse(
                prefs.should_check_now(
                    {"auto_check": True, "interval": "monthly", "last_check": week}
                )
            )

    def test_unparsable_last_check_is_due(self) -> None:
        with _mock_cache_dir(self.tmp):
            self.assertTrue(
                prefs.should_check_now(
                    {"auto_check": True, "interval": "weekly", "last_check": "not-a-date"}
                )
            )
            self.assertTrue(
                prefs.should_check_now({"auto_check": True, "interval": "weekly", "last_check": ""})
            )

    def test_record_check_updates_timestamp(self) -> None:
        with _mock_cache_dir(self.tmp):
            prefs.save_update_settings(auto_check=True, interval="daily")
            prefs.record_check()
            cfg = prefs.update_settings()
            self.assertTrue(cfg["last_check"])
            self.assertFalse(prefs.should_check_now(cfg))


class TestPrereleaseChannelDefault(unittest.TestCase):
    """更新通道默认值必须跟随「当前版本是否预发布」。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="prefs_ch_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_default_matches_current_version(self) -> None:
        from ai_env_clone.version import is_prerelease

        with _mock_cache_dir(self.tmp):
            self.assertEqual(
                prefs.update_settings()["include_prerelease"], is_prerelease()
            )

    def test_user_choice_is_persisted(self) -> None:
        from ai_env_clone.version import is_prerelease

        with _mock_cache_dir(self.tmp):
            prefs.save_update_settings(include_prerelease=not is_prerelease())
            self.assertEqual(
                prefs.update_settings()["include_prerelease"], not is_prerelease()
            )

    def test_interval_labels_cover_all_values(self) -> None:
        self.assertEqual(set(prefs.CHECK_INTERVALS), set(prefs.INTERVAL_LABELS))


if __name__ == "__main__":
    unittest.main()
