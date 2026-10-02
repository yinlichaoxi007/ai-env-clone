"""导出脱敏：``ai_env_clone/redact.py`` 的 YAML / TOML 行级脱敏。

背景（用户 2026-10-01 定策）：设置类条目改为**默认勾选**（
「还原后立刻能开工」），而 DSH 的 ``settings.yaml``（LLM 提供商）与 Reasonix 的
``config.toml``（含 MCP ``[[plugins]]`` 段）一旦含明文 apiKey / token 就有外泄风险。
备份包常被同步到网盘或转发他人 ⇒ 默认备份**绝不能**带明文密钥。

★ 但**引用 ≠ 密钥**：本机实测 DSH ``settings.yaml`` 里写的是
``apiKeyEnv: SENSENOVA_API_KEY``（真密钥在同目录 ``.credentials.yaml``）。
按「键名含 apikey 就脱敏」一刀切会把**引用名抹成占位符**，还原后 provider 指向
一个不存在的变量名、模型静默失效 —— 这是 2026-10-01 实际引入过并修复的 bug，
本模块用 ``test_reference_keys_*`` / ``test_dsh_real_settings_untouched`` 锁住。

本模块只测「脱敏本身」；「条目默认勾选」的断言在各适配器自己的测试里。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import redact  # noqa: E402
from ai_env_clone.adapters import dsh, reasonix  # noqa: E402


class TestKeyLooksSensitive(unittest.TestCase):
    def test_common_credential_keys(self) -> None:
        for key in ("apiKey", "api_key", "API-KEY", "token", "access_token",
                    "secret", "clientSecret", "password", "passwd",
                    "accessKeyId", "private_key", "credential", "Authorization",
                    "cookie", "session_key", '"apiKey"'):
            self.assertTrue(redact.key_looks_sensitive(key), "应判为凭证：%s" % key)

    def test_ordinary_keys_are_not_sensitive(self) -> None:
        for key in ("base_url", "model", "locale", "auto_detect", "name",
                    "disabled_skills", "author", "max_tokens_count_noop"):
            # max_tokens_count_noop 含 token ⇒ 按设计判为敏感（键名命中即脱敏，安全优先）
            if "token" in key.lower():
                continue
            self.assertFalse(redact.key_looks_sensitive(key), "不应判为凭证：%s" % key)

    def test_bare_auth_is_not_a_hint(self) -> None:
        """刻意不用宽泛的 ``auth``（会误伤 ``author``）：只有 ``authorization`` 命中。"""
        self.assertFalse(redact.key_looks_sensitive("author"))
        self.assertTrue(redact.key_looks_sensitive("authorization"))


class TestReferenceKeys(unittest.TestCase):
    """「引用」不是密钥：值若是环境变量名 / 文件名 / 路径，必须原样保留。

    否则还原后配置指向一个不存在的变量名 —— 用户完全看不出原因。
    """

    def test_reference_key_names_detected(self) -> None:
        for key in ("apiKeyEnv", "api_key_env", "APIKEYENV", "keyFile", "key_file",
                    "apiKeyPath", "tokenVar", "secretDir", "apiKeyName",
                    '"apiKeyEnv"'):
            self.assertTrue(redact.key_is_reference(key), "应判为引用：%s" % key)

    def test_plain_credential_keys_are_not_references(self) -> None:
        """这些是**真凭证**，不能被当作引用而漏脱敏。"""
        for key in ("apiKey", "api_key", "token", "secret", "password",
                    "refresh_token", "refreshToken", "access_token",
                    "session_key", "clientSecret"):
            self.assertFalse(redact.key_is_reference(key), "不应判为引用：%s" % key)

    def test_refresh_token_still_redacted(self) -> None:
        """回归锁：``refresh_token`` 含 ``ref``，但它是真凭证 ⇒ 必须仍然脱敏。

        （``ref`` 之所以**不**收进 REFERENCE_KEY_HINTS，就是因为这个键。）
        """
        out, changed = redact.redact_config_text("refresh_token: 1//abc-def\n")
        self.assertTrue(changed)
        self.assertNotIn("1//abc-def", out)

    def test_dsh_api_key_env_value_preserved(self) -> None:
        """本机 DSH 的真实形态：引用名必须逐字保留（2026-10-01 曾把它抹掉）。"""
        src = (
            "llm-pi-ai:\n"
            "  providers:\n"
            "    sensenova:\n"
            "      displayName: sensenova\n"
            "      apiKeyEnv: SENSENOVA_API_KEY\n"
            "      baseURL: https://api.sensenova.cn/v1\n"
        )
        out, changed = redact.redact_config_text(src)
        self.assertFalse(changed, "整份文件都不该被改动")
        self.assertEqual(out, src)
        self.assertIn("apiKeyEnv: SENSENOVA_API_KEY", out)

    def test_dsh_real_settings_untouched(self) -> None:
        """整份「真实形态」的 settings.yaml 经过脱敏后必须**逐字节不变**。"""
        src = (
            "ui-onboarding:\n"
            "  welcomeNoticeVersion: v0.2.3\n"
            "locale:\n"
            "  preference: zh\n"
            "llm-pi-ai:\n"
            "  providers:\n"
            "    opensquilla:\n"
            "      apiKeyEnv: MY_LLM_API_KEY\n"
            "      baseURL: https://api.example.com/v1\n"
            "agent-default-model:\n"
            "  provider: sensenova\n"
            "  model: DeepSeek-V3.2\n"
        ).encode("utf-8")
        self.assertEqual(redact.redact_config_bytes(src), src)

    def test_reference_key_with_inline_secret_still_skipped(self) -> None:
        """键名已声明是引用 ⇒ 按契约不脱敏（此时值本就该是名字/路径）。

        记录这一取舍：宁可漏脱敏这种自相矛盾的写法，也不冒「把路径抹掉」的风险。
        """
        out, changed = redact.redact_config_text('apiKeyFile: "C:\\\\keys\\\\id_rsa"\n')
        self.assertFalse(changed)
        self.assertIn("id_rsa", out)


class TestRedactYaml(unittest.TestCase):
    def test_flat_keys(self) -> None:
        src = 'locale: zh-CN\napi_key: sk-plain\nmodel: deepseek-chat\n'
        out, changed = redact.redact_config_text(src)
        self.assertTrue(changed)
        self.assertIn("locale: zh-CN", out)
        self.assertIn("model: deepseek-chat", out)
        self.assertIn("api_key: " + redact.REDACTED, out)
        self.assertNotIn("sk-plain", out)

    def test_quotes_preserved(self) -> None:
        """引号风格必须原样保留（TOML 的裸占位符是非法值）。"""
        out, _ = redact.redact_config_text(
            'apiKey: "sk-x"\napi_secret: \'sk-y\'\nsecret: sk-z\n'
        )
        self.assertIn('apiKey: "%s"' % redact.REDACTED, out)
        self.assertIn("api_secret: '%s'" % redact.REDACTED, out)
        self.assertIn("secret: %s" % redact.REDACTED, out)
        self.assertNotIn("sk-x", out)
        self.assertNotIn("sk-y", out)
        self.assertNotIn("sk-z", out)

    def test_ordinary_key_never_touched(self) -> None:
        """普通键即使值是 ``sk-`` 形态也不动（命中的判据只看键名）。"""
        out, changed = redact.redact_config_text("display_name: 'sk-not-a-key'\n")
        self.assertFalse(changed)
        self.assertEqual(out, "display_name: 'sk-not-a-key'\n")

    def test_list_items(self) -> None:
        src = "providers:\n  - name: openai\n    token: sk-list\n"
        out, _ = redact.redact_config_text(src)
        self.assertIn("- name: openai", out)
        self.assertIn("token: " + redact.REDACTED, out)
        self.assertNotIn("sk-list", out)

    def test_env_var_reference_kept_intact(self) -> None:
        """``${MY_KEY}`` 本身不含明文，必须**整段保留**。"""
        src = "api_key: ${MY_KEY}\n"
        out, changed = redact.redact_config_text(src)
        self.assertEqual(out, src)
        self.assertFalse(changed)

    def test_empty_value_not_matched(self) -> None:
        src = "api_key:\n"
        out, changed = redact.redact_config_text(src)
        self.assertEqual(out, src)
        self.assertFalse(changed)

    def test_empty_value_does_not_swallow_next_line(self) -> None:
        """回归：分隔符若用 ``\\s``（含换行），空值行会「吃掉」下一行当自己的值。"""
        src = "empty_secret:\nnote: keep me\napi_key: sk-x\n"
        out, _ = redact.redact_config_text(src)
        self.assertIn("empty_secret:", out)
        self.assertIn("note: keep me", out)
        self.assertNotIn("note: ***REDACTED***", out)
        self.assertIn("api_key: " + redact.REDACTED, out)
        self.assertEqual(len(out.splitlines()), 3)

    def test_numeric_and_boolean_values_skipped(self) -> None:
        """``max_tokens: 4096`` 若被换成占位符就成了字符串，会让产品解析配置失败。"""
        src = "max_tokens: 4096\ntoken_limit: 1.5\nsecret_flag: true\n"
        out, changed = redact.redact_config_text(src)
        self.assertEqual(out, src)
        self.assertFalse(changed)

    def test_nested_and_indented(self) -> None:
        src = "provider:\n  deepseek:\n    api_key: sk-nested\n"
        out, _ = redact.redact_config_text(src)
        self.assertIn("    api_key: " + redact.REDACTED, out)
        self.assertNotIn("sk-nested", out)

    def test_comment_line_untouched_structure(self) -> None:
        src = "# 我的设置\nbase_url: https://x.y/v1\n"
        out, changed = redact.redact_config_text(src)
        self.assertEqual(out, src)
        self.assertFalse(changed)


class TestRedactToml(unittest.TestCase):
    def test_equals_separator(self) -> None:
        src = 'model = "gpt-4"\napi_key = "sk-toml"\n'
        out, _ = redact.redact_config_text(src)
        self.assertIn('model = "gpt-4"', out)
        self.assertIn('api_key = "%s"' % redact.REDACTED, out)
        self.assertNotIn("sk-toml", out)

    def test_inline_table(self) -> None:
        """MCP 段常见写法：``env = { API_KEY = "…" }`` —— 内联表里的凭证也要脱敏。"""
        src = ('[[plugins]]\nname = "my-mcp"\n'
               'env = { API_KEY = "sk-inline", OTHER = "keep-me" }\n')
        out, _ = redact.redact_config_text(src)
        self.assertIn('name = "my-mcp"', out)
        self.assertIn('OTHER = "keep-me"', out)
        self.assertNotIn("sk-inline", out)
        self.assertIn(redact.REDACTED, out)

    def test_inline_table_authorization_header(self) -> None:
        src = 'headers = { "Authorization" = "Bearer sk-tok" }\n'
        out, changed = redact.redact_config_text(src)
        self.assertTrue(changed)
        self.assertNotIn("sk-tok", out)
        self.assertNotIn("Bearer", out)

    def test_array_value_on_sensitive_key(self) -> None:
        """数组值不参与脱敏（形态复杂、且凭证极少以数组出现），结构必须原样保留。"""
        src = "api_keys = [\"a\", \"b\"]\n"
        out, _ = redact.redact_config_text(src)
        self.assertEqual(out, src)


class TestRedactConfigBytes(unittest.TestCase):
    def test_no_change_returns_identical_object(self) -> None:
        src = b"locale: zh-CN\n"
        self.assertIs(redact.redact_config_bytes(src), src)

    def test_change_returns_new_bytes(self) -> None:
        src = b"api_key: sk-x\n"
        out = redact.redact_config_bytes(src)
        self.assertEqual(out, b"api_key: ***REDACTED***\n")

    def test_non_utf8_returned_asis(self) -> None:
        """宁可少脱敏，也不破坏用户的非 UTF-8 文件。"""
        src = b"api_key: \xff\xfe\n"
        self.assertIs(redact.redact_config_bytes(src), src)

    def test_crlf_preserved_after_redaction(self) -> None:
        """Windows 配置文件是 CRLF：脱敏后行尾不能变。"""
        src = b"api_key: sk-x\r\nlocale: zh-CN\r\n"
        out = redact.redact_config_bytes(src)
        self.assertIn(b"\r\n", out)
        self.assertEqual(out.count(b"\n"), 2)


class TestAdapterWiring(unittest.TestCase):
    """两个「设置默认勾选且可能含密钥」的条目必须真的挂上了脱敏回调。"""

    def test_dsh_settings_yaml_is_redacted(self) -> None:
        adapter = dsh.DSHAdapter()
        paths = adapter.export_transform_paths() or []
        self.assertIn("settings.yaml", list(paths))
        fn = adapter.export_transform()
        self.assertIsNotNone(fn)
        out = fn("C__Users_x/.dsh/settings.yaml", b"api_key: sk-dsh\n")
        self.assertNotIn(b"sk-dsh", out)
        self.assertIn(redact.REDACTED.encode(), out)

    def test_reasonix_config_toml_is_redacted(self) -> None:
        adapter = reasonix.ReasonixAdapter()
        paths = [p for p in (adapter.export_transform_paths() or [])]
        self.assertIn("config.toml", paths)
        self.assertIn("settings.json", paths)
        fn = adapter.export_transform()
        self.assertIsNotNone(fn)
        out = fn("AppData/Roaming/reasonix/config.toml",
                 b'env = { API_KEY = "sk-reasonix" }\n')
        self.assertNotIn(b"sk-reasonix", out)

    def test_dsh_settings_recommended_but_not_sensitive(self) -> None:
        """设置默认勾选，但**不标敏感** —— 明文密钥不在该文件里。

        标了 sensitive 会让「备份后定位敏感文件」把用户带到 ``settings.yaml``，
        去找一个根本不存在的明文密钥；真正的密钥在 ``.credentials.yaml``。
        """
        items = {i.key: i for i in dsh.build_items()}
        self.assertTrue(items["settings"].recommended)
        self.assertFalse(items["settings"].sensitive)
        # 凭证本身仍不进默认备份
        self.assertFalse(items["credentials"].recommended)

    def test_dsh_settings_carries_credentials_companion(self) -> None:
        """未勾 ``.credentials.yaml`` 时必须提醒「单独备份该文件」。"""
        items = {i.key: i for i in dsh.build_items()}
        comp = items["settings"].companion
        self.assertIsNotNone(comp, "settings.yaml 应声明配套条目")
        comp_key, note = comp
        self.assertEqual(comp_key, "credentials")
        self.assertIn("credentials", note)
        self.assertIn("单独备份", note)

    def test_prefixed_suffix_matching(self) -> None:
        """归档成员名带根占位前缀，后缀判定必须仍然命中（core 的匹配语义）。"""
        adapter = dsh.DSHAdapter()
        fn = adapter.export_transform()
        out = fn("D__home/.dsh/settings.yaml", b"token: sk-z\n")
        self.assertNotIn(b"sk-z", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
