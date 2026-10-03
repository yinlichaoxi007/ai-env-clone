"""更新检测（只读部分）的测试——**全部离线**，HTTP 一律打桩，绝不联网。

⚠️ 本文件被 ``test_version.py`` 的「版本字面量只许出现在 version.py」守卫扫描：
**模块 docstring 里也**不得出现当前版本号（文档里提到它同样算硬编码），
故下文的示例一律用与当前版本不同的版本号。

重点钉住几条「想当然就会写错」的性质：

1. **GitHub 失败要能回落 Gitee**；两侧都失败时错误信息**要含两侧各自的原因**，
   不能笼统一句「检查更新失败」；
2. ★ **Gitee 的 release 列表顺序是错的**（实测按 id 升序，旧版本排在最新版本前面）
   ⇒ 必须自己按版本号排序；
3. ★ **源码包会混在同一个 release 的 assets 里**（Gitee 实测有 ``vX.Y.Z.zip`` 与
   ``vX.Y.Z.tar.gz``，GitHub 侧则没有）⇒ 必须按**资产名精确匹配**，
   「按版本号筛 release」**剔不掉**它；
4. ``SHA256SUMS`` 取不到时要**降级但明示**（不静默）；
5. 正式版用户遇到 rc 时要区分「已是最新」与「有新版但通道不含」；
6. HTTP 200 但内容不是 JSON ⇒ 失败，不得当成「无更新」。
"""

import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

from ai_env_clone import updater

WIN = "AiEnvClone-windows.exe"
_HASH = "a" * 64


class _Resp:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """按 URL 规则返回打桩响应；``fail_hosts`` 里的主机直接抛异常。"""

    def __init__(self, routes: dict, fail_hosts: tuple = (), fail_status: dict = None):
        self.routes = routes
        self.fail_hosts = fail_hosts
        self.fail_status = fail_status or {}
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        for host in self.fail_hosts:
            if host in url:
                raise urllib.error.URLError(TimeoutError("timed out"))
        for host, code in self.fail_status.items():
            if host in url:
                raise urllib.error.HTTPError(url, code, "err", None, None)
        for pattern, payload in self.routes.items():
            if pattern in url:
                if isinstance(payload, str):
                    return _Resp(payload.encode("utf-8"))
                return _Resp(json.dumps(payload).encode("utf-8"))
        raise AssertionError("未打桩的 URL：%s" % url)


def _release(tag, assets=None, prerelease=True, body=""):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "created_at": "2026-10-03T00:00:00Z",
        "body": body,
        "assets": [
            {"name": name, "browser_download_url": "https://host/%s" % name, "size": 10}
            for name in (assets if assets is not None else [WIN])
        ],
    }


def _run(opener, current="1.0.0-rc.1", include=True, **kw):
    return updater.check_update(
        current=current, include_prerelease=include,
        asset_name=WIN, opener=opener, sleep=lambda s: None, **kw
    )


class TestPlatformAsset(unittest.TestCase):
    def test_all_four_platforms(self) -> None:
        self.assertEqual(updater.platform_asset_name("win32", "AMD64"), WIN)
        self.assertEqual(updater.platform_asset_name("darwin", "arm64"),
                         "AiEnvClone-macos-arm64.zip")
        self.assertEqual(updater.platform_asset_name("darwin", "x86_64"),
                         "AiEnvClone-macos-x86_64.zip")
        self.assertEqual(updater.platform_asset_name("linux", "x86_64"),
                         "AiEnvClone-linux")

    def test_unknown_platform_returns_none(self) -> None:
        self.assertIsNone(updater.platform_asset_name("plan9", "sparc"))

    def test_current_platform_is_supported(self) -> None:
        self.assertIn(updater.platform_asset_name(), updater.ASSET_NAMES.values())


class TestOpenerUsesUrllibDefaultSSLContext(unittest.TestCase):
    """★ **不要自己造 SSLContext 递给 urllib**——Gitee 会因此返回 **403**。

    实测（同一 URL、同一请求头，交替各 3 次）：

    ============================================== ==========
    写法                                             Gitee
    ============================================== ==========
    ``HTTPSHandler(context=None)`` / 交给 urllib 默认 200
    ``HTTPSHandler(context=create_default_context())`` **403**
    ============================================== ==========

    GitHub 两种写法都得 200，所以只有 Gitee 判。

    ⚠️ 容易总结错的地方：**别把结论写成「不能有 context」**。urllib 3.13 自己
    构造的默认 ``HTTPSHandler`` 也是**带** context 的（``_context`` 非 None），
    且它与手搓 `create_default_context()` 在 ``verify_mode`` / ``options`` /
    加密套件 / ALPN / 证书库上**逐项完全相同**，却一个 200 一个 403。所以真正
    起作用的不是 context 的属性，而是「这个 context 是谁造的、何时造的」。

    这条**离线单测永远发现不了**（打桩的 HTTP 不会 403），只会在真实联网时静默
    毁掉「GitHub 失败回落 Gitee」这条兜底路径，故必须用契约测试钉住：
    **凡我们自己传给 ``build_opener`` 的 handler，都不得携带自定义 context。**
    """

    def _passed_handlers(self):
        """截获我们传给 ``build_opener`` 的 handler 列表（不真的建 opener）。"""
        calls = []
        with mock.patch.object(urllib.request, "build_opener",
                               side_effect=lambda *a, **k: calls.append(a) or mock.Mock()):
            updater.build_opener("")
            updater.build_opener("http://127.0.0.1:7897")
        return calls

    def test_never_passes_own_ssl_context(self) -> None:
        calls = self._passed_handlers()
        self.assertEqual(len(calls), 2, "空代理与显式代理两条路径都要覆盖")
        for args in calls:
            for h in args:
                if isinstance(h, urllib.request.HTTPSHandler):
                    self.assertIsNone(
                        h._context,
                        "不得把自定义 SSLContext 传给 build_opener："
                        "Gitee 对显式 context 返回 403（交给 urllib 默认的即可）",
                    )

    def test_still_returns_real_opener(self) -> None:
        """去掉自定义 context 不能顺手把代理也丢了（两者在同一个函数里）。"""
        op = updater.build_opener("http://127.0.0.1:7897")
        ps = [h for h in op.handlers
              if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(ps, "应存在 ProxyHandler")
        self.assertEqual(ps[0].proxies.get("https"), "http://127.0.0.1:7897")
        # urllib 默认那一个 HTTPSHandler 必须在（否则 https 直接不可用）
        self.assertTrue([h for h in op.handlers
                         if isinstance(h, urllib.request.HTTPSHandler)],
                        "应保留 urllib 默认的 HTTPSHandler")


class TestSourceFailover(unittest.TestCase):
    def test_github_fails_falls_back_to_gitee(self) -> None:
        opener = FakeOpener(
            {"gitee.com": [_release("v1.1.0")]},
            fail_hosts=("api.github.com",),
        )
        res = _run(opener)
        self.assertIsNotNone(res.info)
        self.assertEqual(res.info.source, "gitee")
        self.assertEqual(res.info.version, "1.1.0")
        self.assertEqual(res.source_errors.get("github"), "连接超时")

    def test_github_success_does_not_touch_gitee(self) -> None:
        opener = FakeOpener({"api.github.com": [_release("v1.1.0")]})
        res = _run(opener)
        self.assertEqual(res.info.source, "github")
        self.assertFalse(any("gitee.com" in c for c in opener.calls),
                         "GitHub 成功时不该再请求 Gitee")

    def test_both_fail_reports_both_reasons(self) -> None:
        opener = FakeOpener({}, fail_hosts=("api.github.com",),
                            fail_status={"gitee.com": 502})
        res = _run(opener)
        self.assertTrue(res.error)
        self.assertIn("GitHub", res.error)
        self.assertIn("Gitee", res.error)
        self.assertIn("502", res.error)
        self.assertIn("连接超时", res.error)

    def test_retries_three_times_per_source(self) -> None:
        opener = FakeOpener({}, fail_hosts=("api.github.com", "gitee.com"))
        _run(opener)
        gh = [c for c in opener.calls if "github" in c]
        gt = [c for c in opener.calls if "gitee.com" in c]
        self.assertEqual(len(gh), updater._RETRIES)
        self.assertEqual(len(gt), updater._RETRIES)

    def test_http_200_but_not_json_is_failure(self) -> None:
        """返回 200 但内容是 HTML（典型：代理拦截页）⇒ 失败，不得当成「无更新」。"""
        opener = FakeOpener({"api.github.com": "<html>blocked</html>"},
                            fail_hosts=("gitee.com",))
        res = _run(opener)
        self.assertTrue(res.error)
        self.assertIn("解析失败", res.error)


class TestReleaseSelection(unittest.TestCase):
    def test_gitee_wrong_order_still_picks_newest(self) -> None:
        """★ Gitee 顺序错乱（实测旧的在前）必须仍挑出最新。"""
        reversed_list = [_release("v1.0.0"), _release("v1.2.0"), _release("v1.1.0")]
        opener = FakeOpener({"gitee.com": reversed_list}, fail_hosts=("api.github.com",))
        self.assertEqual(_run(opener, current="0.9.0").info.version, "1.2.0")

    def test_non_version_tags_ignored(self) -> None:
        opener = FakeOpener({"api.github.com": [
            {"tag_name": "nightly", "assets": []},
            {"tag_name": "latest", "assets": []},
            _release("v1.5.0"),
        ]})
        self.assertEqual(_run(opener, current="1.0.0").info.version, "1.5.0")

    def test_same_version_is_up_to_date(self) -> None:
        opener = FakeOpener({"api.github.com": [_release("v1.0.0")]})
        res = _run(opener, current="1.0.0")
        self.assertTrue(res.up_to_date)
        self.assertIsNone(res.info)

    def test_older_release_is_up_to_date(self) -> None:
        opener = FakeOpener({"api.github.com": [_release("v0.9.0")]})
        self.assertTrue(_run(opener, current="1.0.0").up_to_date)

    def test_skipped_version_respected(self) -> None:
        opener = FakeOpener({"api.github.com": [_release("v1.4.0")]})
        res = _run(opener, current="1.0.0", skipped_version="1.4.0")
        self.assertTrue(res.up_to_date)
        self.assertTrue(res.skipped)

    def test_prerelease_channel_filtering(self) -> None:
        """正式通道看不到 rc，但必须知道「有 rc」而不是笼统说已是最新。"""
        opener = FakeOpener({"api.github.com": [_release("v1.5.0-rc.1")]})
        res = _run(opener, current="1.0.0", include=False)
        self.assertTrue(res.up_to_date)
        self.assertEqual(res.filtered_version, "v1.5.0-rc.1",
                         "必须区分『已是最新』与『通道不含』")

    def test_prerelease_channel_accepts_rc(self) -> None:
        opener = FakeOpener({"api.github.com": [_release("v1.5.0-rc.1")]})
        self.assertEqual(_run(opener, current="1.0.0", include=True).info.version, "1.5.0-rc.1")

    def test_legacy_glued_tag_sorted_correctly(self) -> None:
        """历史 tag ``v0.1.0rc`` 要排在 ``v0.2.0`` 之前（不能被当成乱码丢弃后错序）。"""
        opener = FakeOpener({"api.github.com": [_release("v0.1.0rc"), _release("v0.2.0")]})
        self.assertEqual(_run(opener, current="0.0.1").info.version, "0.2.0")


class TestAssetSelection(unittest.TestCase):
    def test_source_archives_never_selected(self) -> None:
        """★ 源码包就在同一个 release 的 assets 里，必须被资产名精确匹配排除。"""
        opener = FakeOpener({"gitee.com": [{
            "tag_name": "v1.1.0",
            "assets": [
                {"name": "v1.1.0.zip", "browser_download_url": "https://h/src.zip", "size": 1},
                {"name": "v1.1.0.tar.gz", "browser_download_url": "https://h/src.tgz", "size": 1},
                {"name": "repo-v1.1.0.zip", "browser_download_url": "https://h/s2.zip", "size": 1},
                {"name": WIN, "browser_download_url": "https://h/win.exe", "size": 99},
            ],
        }]}, fail_hosts=("api.github.com",))
        res = _run(opener)
        self.assertEqual(res.info.asset_name, WIN)
        self.assertEqual(res.info.url, "https://h/win.exe")
        self.assertNotIn(".zip", res.info.url)
        self.assertNotIn(".tar.gz", res.info.url)

    def test_missing_platform_asset_reported(self) -> None:
        opener = FakeOpener({"api.github.com": [{
            "tag_name": "v1.1.0",
            "assets": [{"name": "AiEnvClone-linux",
                        "browser_download_url": "https://h/l", "size": 1}],
        }]})
        res = _run(opener)
        self.assertTrue(res.asset_missing)
        self.assertIsNone(res.info)

    def test_unknown_platform_short_circuits(self) -> None:
        """显式告知「本平台无产物」时，应直接返回 asset_missing 而不报成功。"""
        opener = FakeOpener({"api.github.com": [_release("v1.1.0")]})
        res = updater.check_update(current="1.0.0", include_prerelease=True,
                                   asset_name=None, opener=opener, sleep=lambda s: None)
        self.assertTrue(res.asset_missing)


class TestChecksums(unittest.TestCase):
    def _with_sums(self, sums_text: str = None, tag="v1.1.0"):
        """构造一个**带 SHA256SUMS 资产**的 release（清单是 release 里的一个资产）。"""
        assets = [WIN, "SHA256SUMS"] if sums_text is not None else [WIN]
        rel = _release(tag, assets=assets)
        if sums_text is not None:
            for a in rel["assets"]:
                if a["name"] == "SHA256SUMS":
                    a["browser_download_url"] = "https://host/SHA256SUMS"
        opener = FakeOpener({
            "api.github.com": [rel],
            "SHA256SUMS": sums_text if sums_text is not None else "",
        })
        return opener

    def test_sums_provides_expected_hash(self) -> None:
        res = _run(self._with_sums("%s  %s\n" % (_HASH, WIN)))
        self.assertTrue(res.info.checksum_verified)
        self.assertEqual(res.info.sha256, _HASH)

    def test_sums_missing_degrades_but_is_visible(self) -> None:
        """★ 取不到清单要降级为「不校验」，但必须在文案里明示，不静默。"""
        opener = FakeOpener(
            {"api.github.com": [_release("v1.1.0", assets=[WIN, "SHA256SUMS"])]},
            fail_hosts=("SHA256SUMS",),
        )
        res = _run(opener)
        self.assertIsNotNone(res.info)
        self.assertFalse(res.info.checksum_verified)
        text = updater.describe_result(res, "1.0.0", WIN)
        self.assertIn("未做哈希校验", text)

    def test_sums_release_without_manifest_asset_degrades(self) -> None:
        """release 里**没有** SHA256SUMS 资产 ⇒ 降级并明示。"""
        res = _run(FakeOpener({"api.github.com": [_release("v1.1.0")]}))
        self.assertFalse(res.info.checksum_verified)
        self.assertIn("未做哈希校验", updater.describe_result(res, "1.0.0", WIN))

    def test_sums_without_this_asset_degrades(self) -> None:
        res = _run(self._with_sums("%s  AiEnvClone-linux\n" % _HASH))
        self.assertFalse(res.info.checksum_verified)

    def test_parse_sums_formats(self) -> None:
        text = (
            "%s  AiEnvClone-linux\n"          # GNU 双空格
            "%s *AiEnvClone-windows.exe\n"     # 二进制模式
            "# 注释行\n"
            "\n"
            "不是哈希行\n"
        ) % (_HASH, "b" * 64)
        parsed = updater.parse_sums(text)
        self.assertEqual(set(parsed), {"AiEnvClone-linux", WIN})
        self.assertEqual(parsed["AiEnvClone-linux"], _HASH)

    def test_parse_sums_ignores_short_hashes(self) -> None:
        self.assertEqual(updater.parse_sums("abc123  AiEnvClone-windows.exe\n"), {})

    def test_sums_acts_as_whitelist_source(self) -> None:
        """清单里出现过的资产名才有哈希；源码包不在其中 ⇒ 天然拿不到校验值。"""
        opener = FakeOpener({
            "gitee.com": [{
                "tag_name": "v1.1.0",
                "assets": [
                    {"name": WIN, "browser_download_url": "https://h/win", "size": 5},
                    {"name": "v1.1.0.zip", "browser_download_url": "https://h/src", "size": 5},
                    {"name": "SHA256SUMS", "browser_download_url": "https://h/SHA256SUMS", "size": 1},
                ],
            }],
            "SHA256SUMS": "%s  %s\n" % (_HASH, WIN),
        }, fail_hosts=("api.github.com",))
        res = _run(opener)
        self.assertTrue(res.info.checksum_verified)
        self.assertEqual(res.info.asset_name, WIN)
        # 源码包就在同一个 release 里，但它不在清单中 ⇒ 拿不到校验值
        self.assertNotIn("v1.1.0.zip", updater.parse_sums("%s  %s\n" % (_HASH, WIN)))


class TestDescribeResult(unittest.TestCase):
    """界面「关于」与 CLI 共用这段文案，故在这里钉住关键信息是否出现。"""

    def test_error_mentions_both_sources(self) -> None:
        res = updater.UpdateResult(error="GitHub：连接超时；Gitee：HTTP 502")
        text = updater.describe_result(res, "1.0.0", WIN)
        self.assertIn("GitHub", text)
        self.assertIn("Gitee", text)

    def test_up_to_date(self) -> None:
        res = updater.UpdateResult(up_to_date=True)
        self.assertIn("已是最新", updater.describe_result(res, "1.0.0", WIN))

    def test_filtered_channel_suggests_toggle(self) -> None:
        res = updater.UpdateResult(up_to_date=True, filtered_version="v1.9.0-rc.1")
        text = updater.describe_result(res, "1.0.0", WIN)
        self.assertIn("1.9.0-rc.1", text)
        self.assertIn("包含预发布", text)

    def test_new_version_mentions_source(self) -> None:
        res = updater.UpdateResult(
            info=updater.UpdateInfo(version="1.1.0", tag="v1.1.0", asset_name=WIN,
                                    url="u", source="gitee", checksum_verified=True)
        )
        text = updater.describe_result(res, "1.0.0", WIN)
        self.assertIn("1.1.0", text)
        self.assertIn("gitee", text)
        self.assertNotIn("未做哈希校验", text)

    def test_prerelease_flagged(self) -> None:
        res = updater.UpdateResult(
            info=updater.UpdateInfo(version="1.1.0-rc.1", tag="v1.1.0-rc.1", asset_name=WIN,
                                    url="u", source="github", prerelease=True,
                                    checksum_verified=True)
        )
        self.assertIn("预发布", updater.describe_result(res, "1.0.0", WIN))

    def test_asset_missing_message(self) -> None:
        res = updater.UpdateResult(asset_missing=True)
        self.assertIn("没有", updater.describe_result(res, "1.0.0", WIN))


class TestCheckUpdateFlag(unittest.TestCase):
    def test_returns_none_without_flag(self) -> None:
        self.assertIsNone(updater.handle_check_update_flag([]))
        self.assertIsNone(updater.handle_check_update_flag(["--version"]))


class TestNoTkinterDependency(unittest.TestCase):
    def test_updater_module_does_not_pull_tkinter(self) -> None:
        import subprocess
        import sys

        from tests.test_doc_text import REPO_ROOT

        code = ("import sys, importlib; sys.modules.pop('tkinter', None);"
                "importlib.import_module('ai_env_clone.updater');"
                "importlib.import_module('ai_env_clone.prefs');"
                "print('tkinter' in sys.modules)")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=REPO_ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
