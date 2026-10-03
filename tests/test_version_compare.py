"""版本号保序解析与比较测试（更新检查的判据）。

★ 为什么要单独测 :func:`parse_version` 而不复用 :func:`release_tuple`：
``release_tuple()`` 为了写 Windows VERSIONINFO 而**丢弃预发布段**（把
``X.Y.Z-rc.N`` 压成 ``(X, Y, Z, 0)``），拿它做更新比较会把预发布版与同号正式版
判成相等 ⇒ 用户永远看不到同号正式版的更新提示。
两者职责不同，本文件同时钉住「该保序的保序」与「release_tuple 仍按老规矩丢预发布段」。

⚠️ 本文件被 ``test_version.py`` 的「版本字面量只许出现在 version.py」守卫扫描，
故示例版本一律取**与当前版本不同**的字面量。
"""

import unittest

from ai_env_clone import version

# ⚠️ 本文件被 ``tests/test_version.py::test_version_literal_lives_only_in_version_module``
# 扫描：**不得出现当前版本号字面量**（否则会被判为「硬编码版本号」）。
# 下面用的都是与当前版本不同的示例版本。
_SAMPLES = ("1.2.3-rc.4", "1.2.3", "1.2.3-rc.5", "1.3.0", "2.0.0-rc.1")


class TestParseVersion(unittest.TestCase):
    def test_prerelease(self) -> None:
        num, pre = version.parse_version(_SAMPLES[0])
        self.assertEqual(num, (1, 2, 3))
        self.assertNotEqual(pre, version.FINAL)

    def test_final_has_empty_prerelease(self) -> None:
        num, pre = version.parse_version(_SAMPLES[1])
        self.assertEqual(num, (1, 2, 3))
        self.assertEqual(pre, version.FINAL)

    def test_v_prefix_optional(self) -> None:
        self.assertEqual(
            version.parse_version("v" + _SAMPLES[0]), version.parse_version(_SAMPLES[0])
        )

    def test_build_metadata_discarded(self) -> None:
        self.assertEqual(
            version.parse_version(_SAMPLES[1] + "+build.7"), version.parse_version(_SAMPLES[1])
        )

    def test_short_numeric_padded_to_three(self) -> None:
        self.assertEqual(version.parse_version("1.2"), version.parse_version("1.2.0"))

    def test_rc_without_dot_number(self) -> None:
        """历史 tag 用过「数字段直接粘标识」的写法（``0.1.0rc``）。"""
        a = version.parse_version("0.1.0rc")
        b = version.parse_version("0.1.0-rc")
        self.assertEqual(a, b)
        self.assertEqual(a[0], (0, 1, 0))

    def test_invalid_returns_none(self) -> None:
        for bad in ("", "not-a-version", "x.y.z", "1", "1.2.3-", "1.2.3-rc.x",
                    "1.2.3-..", None, 123):
            self.assertIsNone(version.parse_version(bad), bad)

    def test_final_sorts_after_every_prerelease(self) -> None:
        for tag in ("dev", "alpha", "beta", "rc", "preview", "zzz"):
            pre = version.parse_version("9.9.9-%s" % tag)
            fin = version.parse_version("9.9.9")
            self.assertGreater(fin[1], pre[1], tag)


class TestIsNewer(unittest.TestCase):
    def test_same_version_not_newer(self) -> None:
        self.assertFalse(version.is_newer(_SAMPLES[0], _SAMPLES[0]))

    def test_release_beats_same_number_prerelease(self) -> None:
        """rc 用户会被提示升级到同号正式版——这是对的。"""
        self.assertTrue(version.is_newer(_SAMPLES[1], _SAMPLES[0]))

    def test_prerelease_not_newer_than_release(self) -> None:
        self.assertFalse(version.is_newer(_SAMPLES[2], _SAMPLES[1]))

    def test_rc_sequence(self) -> None:
        self.assertTrue(version.is_newer(_SAMPLES[2], _SAMPLES[0]))
        self.assertFalse(version.is_newer(_SAMPLES[0], _SAMPLES[2]))

    def test_prerelease_tag_order(self) -> None:
        self.assertTrue(version.is_newer("1.2.3-rc.1", "1.2.3-beta.1"))
        self.assertTrue(version.is_newer("1.2.3-beta.1", "1.2.3-alpha.9"))

    def test_numeric_segmentwise(self) -> None:
        self.assertTrue(version.is_newer(_SAMPLES[3], "1.2.9"))
        self.assertTrue(version.is_newer(_SAMPLES[4], "0.99.99"))
        self.assertFalse(version.is_newer("1.2.9", _SAMPLES[3]))

    def test_legacy_tag_parsed_as_release(self) -> None:
        """历史 tag ``v0.1.0rc`` 没有 ``-``，按正式版 0.1.0 解析。"""
        self.assertFalse(version.is_newer("0.1.0rc", _SAMPLES[3]))
        self.assertTrue(version.is_newer(_SAMPLES[3], "0.1.0rc"))

    def test_defaults_to_current_version(self) -> None:
        self.assertFalse(version.is_newer(version.__version__))
        self.assertTrue(version.is_newer("999.0.0"))

    def test_unparsable_never_newer(self) -> None:
        self.assertFalse(version.is_newer("garbage", _SAMPLES[1]))
        self.assertFalse(version.is_newer(_SAMPLES[3], "garbage"))

    def test_never_raises_on_odd_input(self) -> None:
        for a, b in [(None, "1.0.0"), ("1.0.0", None), ("", ""), ("..", "1.0")]:
            self.assertIn(version.is_newer(a, b), (True, False))


class TestIsPrerelease(unittest.TestCase):
    def test_current_version(self) -> None:
        self.assertEqual(version.is_prerelease(), version.is_prerelease(version.__version__))

    def test_rc_and_release(self) -> None:
        self.assertTrue(version.is_prerelease(_SAMPLES[0]))
        self.assertFalse(version.is_prerelease(_SAMPLES[1]))

    def test_v_prefix_ignored(self) -> None:
        self.assertTrue(version.is_prerelease("v" + _SAMPLES[0]))
        self.assertFalse(version.is_prerelease("v" + _SAMPLES[1]))

    def test_garbage_is_not_prerelease(self) -> None:
        self.assertFalse(version.is_prerelease("garbage"))


class TestReleaseTupleStillDropsPrerelease(unittest.TestCase):
    """两个函数职责不同：release_tuple 仍按老规矩丢预发布段（写 exe 资源用）。"""

    def test_release_tuple_shape(self) -> None:
        t = version.release_tuple()
        self.assertEqual(len(t), 4)
        self.assertTrue(all(isinstance(x, int) for x in t))

    def test_release_tuple_loses_prerelease(self) -> None:
        self.assertEqual(
            version.release_tuple(),
            tuple(version.parse_version(version.__version__)[0]) + (0,),
        )

if __name__ == "__main__":
    unittest.main()
