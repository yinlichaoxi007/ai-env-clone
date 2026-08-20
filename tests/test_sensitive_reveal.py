"""测试备份后敏感文件定位相关逻辑（脱敏字段行定位）。"""
import os
import tempfile
import unittest

from ai_env_clone.__main__ import QoderBackupApp


class TestFirstSensitiveLine(unittest.TestCase):
    """_first_sensitive_line 需在源文件（含明文敏感凭证）中定位到敏感字段行，不局限于 apiKey。"""

    def test_locates_plaintext_apikey_line(self):
        content = (
            "{\n"
            '  "models": [\n'
            "    {\n"
            '      "name": "m",\n'
            '      "url": "https://x",\n'
            '      "apiKey": "sk-secret-123"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            path = f.name
        try:
            # apiKey 在源文件第 6 行（1-based）
            self.assertEqual(QoderBackupApp._first_sensitive_line(path), 6)
        finally:
            os.remove(path)

    def test_locates_other_sensitive_field_line(self):
        # 私人令牌 / token 等非 apiKey 的敏感字段也应被定位
        content = (
            "{\n"
            '  "name": "m",\n'
            '  "url": "https://x",\n'
            '  "token": "tk-secret-456"\n'
            "}\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            path = f.name
        try:
            # token 在第 4 行（1-based）
            self.assertEqual(QoderBackupApp._first_sensitive_line(path), 4)
        finally:
            os.remove(path)

    def test_skips_redacted_line(self):
        content = (
            "{\n"
            '  "models": [\n'
            '    "apiKey": "***REDACTED***"\n'
            "  ]\n"
            "}\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            path = f.name
        try:
            # 仅含脱敏占位符的行不应被当作敏感字段
            self.assertIsNone(QoderBackupApp._first_sensitive_line(path))
        finally:
            os.remove(path)

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            QoderBackupApp._first_sensitive_line(
                os.path.join(tempfile.gettempdir(), "no_such_9f3a.json")
            )
        )


if __name__ == "__main__":
    unittest.main()
