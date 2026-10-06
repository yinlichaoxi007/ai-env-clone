"""更新下载 / 校验 / 原地替换 / CLI 分支的测试——**全部离线**，HTTP 一律打桩。

钉住方案（自动更新方案.md 第五、六、七部分）里「想当然就会写错」的性质：

1. **三道校验缺一不可**：体积、SHA256、结构魔数——任一不符必须拒绝，
   且「校验失败绝不落地」（失败文件不得以正式名留在磁盘上）；
2. **替换必须先同卷落地**，运行中的 exe 不能删但能改名 ⇒ ``target → .old → 新落位``；
   任一步失败要把 ``.old`` 改回原名**整体回滚**；
3. ``.old`` 由「成功启动的新版本」清理：被占用就留着（不算错误），绝不挡启动；
4. ``--resume-update``（提权续做）解析两种参数形态，且**只**做校验 → 替换 → 重启；
5. **取消**是中性结果（``UpdateCancelled``），不与失败混为一谈。
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from ai_env_clone import compress_estimate, updater

WIN_EXE = updater.ASSET_NAMES["windows"]


def _mock_cache_dir(tmp: str):
    return mock.patch.object(compress_estimate, "cache_dir", return_value=tmp)


def _resp_bytes(raw: bytes):
    class _Resp:
        def __init__(self, data: bytes):
            self._data = data

        def read(self, n=-1):
            if n < 0:
                out, self._data = self._data, b""
                return out
            out, self._data = self._data[:n], self._data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    return _Resp(raw)


class _FakeOpener:
    """按 URL 前缀路由的打桩 opener；``fail`` 里的 URL 前缀一律抛超时。"""

    def __init__(self, routes: dict, fail: tuple = ()):
        self.routes = routes
        self.fail = fail
        self.calls: list = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        for prefix in self.fail:
            if url.startswith(prefix):
                raise TimeoutError("timed out")
        for pattern, payload in self.routes.items():
            if pattern in url:
                return _resp_bytes(payload)
        raise AssertionError("未打桩的 URL：%s" % url)


def _info(url="https://host/AiEnvClone-windows.exe", sha256="", size=0,
          asset_name=WIN_EXE, tag="v9.9.9", source="github"):
    return updater.UpdateInfo(version="9.9.9", tag=tag, asset_name=asset_name,
                              url=url, size=size, sha256=sha256, source=source)


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="upd_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.dl_dir = os.path.join(self.tmp, "download")
        updater_patcher = _mock_cache_dir(self.tmp)
        updater_patcher.start()
        self.addCleanup(updater_patcher.stop)


class TestVerifyFile(_TmpCase):
    def _file(self, data: bytes, name="bin") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_size_mismatch_rejected(self) -> None:
        path = self._file(b"MZ" + b"x" * 10)
        ok, reason = updater.verify_file(path, _info(size=99))
        self.assertFalse(ok)
        self.assertIn("体积不符", reason)

    def test_size_absent_skipped(self) -> None:
        path = self._file(b"MZ" + b"x" * 10)
        ok, _ = updater.verify_file(path, _info(size=0))
        self.assertTrue(ok)

    def test_sha256_mismatch_rejected(self) -> None:
        path = self._file(b"MZ payload")
        ok, reason = updater.verify_file(path, _info(sha256="a" * 64))
        self.assertFalse(ok)
        self.assertIn("SHA256", reason)

    def test_sha256_match_accepted(self) -> None:
        import hashlib
        data = b"MZ payload"
        digest = hashlib.sha256(data).hexdigest()
        path = self._file(data)
        ok, reason = updater.verify_file(path, _info(sha256=digest))
        self.assertTrue(ok, reason)

    def test_magic_mismatch_rejected(self) -> None:
        """Windows 资产缺 ``MZ`` 魔数 ⇒ 不是本平台产物（防「下载到源码包」类事故）。"""
        path = self._file(b"PK\x03\x04 not a pe")
        ok, reason = updater.verify_file(path, _info())
        self.assertFalse(ok)
        self.assertIn("魔数", reason)

    def test_unknown_asset_name_skips_magic(self) -> None:
        path = self._file(b"whatever")
        ok, _ = updater.verify_file(path, _info(asset_name="future-asset.bin"))
        self.assertTrue(ok)


class TestDownload(_TmpCase):
    _PAYLOAD = b"MZ" + b"A" * 64

    def _route(self, payload: bytes, url="https://host/AiEnvClone-windows.exe"):
        return _FakeOpener({url: payload})

    def test_success_writes_file_and_reports_progress(self) -> None:
        pcts: list = []
        path = updater.download_update(
            _info(size=len(self._PAYLOAD)), opener=self._route(self._PAYLOAD),
            progress=pcts.append, dest_dir=self.dl_dir)
        self.assertEqual(os.path.basename(path), WIN_EXE)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self._PAYLOAD)
        self.assertEqual(pcts[-1], 100)
        self.assertFalse(os.path.exists(path + ".part"))  # 半截文件不残留

    def test_sha_mismatch_discards_file(self) -> None:
        with mock.patch.object(updater, "verify_file", return_value=(False, "SHA256 不符")):
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater.download_update(_info(), opener=self._route(self._PAYLOAD),
                                        dest_dir=self.dl_dir)
        self.assertIn("校验失败", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dl_dir, WIN_EXE)))
        self.assertFalse(os.path.exists(os.path.join(self.dl_dir, WIN_EXE) + ".part"))

    def test_cancel_is_neutral_and_retries_not_attempted(self) -> None:
        opener = self._route(self._PAYLOAD)

        def _cancel() -> bool:
            return True

        with self.assertRaises(updater.UpdateCancelled):
            updater.download_update(_info(), opener=opener, cancel=_cancel,
                                    dest_dir=self.dl_dir)
        self.assertEqual(len(opener.calls), 1)   # 取消不做无谓重试

    def test_failure_then_fallback_to_other_side(self) -> None:
        """主 URL 三次全败 → 换另一侧发布源重下同一资产（方案 3.3 ④）。"""
        import json
        other_url = "https://gitee-host/AiEnvClone-windows.exe"
        releases = [{"tag_name": "v9.9.9",
                     "assets": [{"name": WIN_EXE, "browser_download_url": other_url,
                                 "size": len(self._PAYLOAD)}]}]
        opener = _FakeOpener(
            {"releases?per_page=20": json.dumps(releases).encode("utf-8"),
             "gitee-host/AiEnvClone-windows.exe": self._PAYLOAD},
            fail=("https://host/",))
        path = updater.download_update(_info(), opener=opener,
                                       dest_dir=self.dl_dir, sleep=lambda _s: None)
        self.assertTrue(os.path.isfile(path))
        self.assertIn("gitee-host", opener.calls[-1])

    def test_all_sources_fail_reports_reason(self) -> None:
        opener = _FakeOpener({}, fail=("https://",))
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.download_update(_info(), opener=opener,
                                    dest_dir=self.dl_dir, sleep=lambda _s: None)
        self.assertIn("下载失败", str(ctx.exception))


class TestStageAndReplace(_TmpCase):
    def _make_target(self) -> str:
        target = os.path.join(self.tmp, "app", "AiEnvClone.exe")
        os.makedirs(os.path.dirname(target))
        with open(target, "wb") as f:
            f.write(b"MZold")
        return target

    def _make_download(self) -> str:
        path = os.path.join(self.tmp, "dl", WIN_EXE)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(b"MZnew")
        return path

    def test_full_dance_with_restart(self) -> None:
        target = self._make_target()
        downloaded = self._make_download()
        spawned: list = []
        old = updater.stage_and_replace(downloaded, target=target,
                                        popen=spawned.append)
        self.assertEqual(old, target + ".old")
        self.assertTrue(os.path.isfile(old))                # 旧版本留作回滚退路
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"MZnew")            # 新版本已落位
        self.assertEqual(spawned, [[target]])               # 重启的是新 exe
        marker = os.path.join(updater.update_download_dir(),
                              updater.PENDING_OLD_FILENAME)
        self.assertTrue(os.path.isfile(marker))             # 待清理标记已写
        with open(marker, "r", encoding="utf-8") as f:
            self.assertEqual(__import__("json").load(f)["old"], old)

    def test_unwritable_dir_aborts_without_elevation(self) -> None:
        """预检不可写 ⇒ 直接失败，**绝不静默提权**（方案 6.4 底线）。"""
        target = self._make_target()
        downloaded = self._make_download()
        with mock.patch.object(updater, "ensure_dir_writable", return_value=False):
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater.stage_and_replace(downloaded, target=target, popen=None)
        self.assertIn("不可写", str(ctx.exception))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"MZold")            # 原文件未动

    def test_failure_rolls_back(self) -> None:
        """新文件落位失败 ⇒ ``.old`` 改回原名，目录回到更新前状态。"""
        target = self._make_target()
        downloaded = self._make_download()
        real_replace = os.replace

        def _boom(src, dst):        # 只掐「staged → target」这一步
            if str(dst) == str(target):
                raise OSError(13, "locked")
            return real_replace(src, dst)

        with mock.patch.object(os, "replace", side_effect=_boom):
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater.stage_and_replace(downloaded, target=target, popen=None)
        self.assertIn("回滚", str(ctx.exception))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"MZold")
        self.assertFalse(os.path.exists(target + ".old"))

    def test_stale_old_gets_new_name(self) -> None:
        """上次的 ``.old`` 还被占用删不掉 ⇒ 换名保存，**不挡本次更新**。"""
        target = self._make_target()
        downloaded = self._make_download()
        stale = target + ".old"
        with open(stale, "wb") as f:
            f.write(b"MZancient")
        real_remove = os.remove

        def _busy(path, *a, **k):
            if str(path) == str(stale):
                raise PermissionError(32, "in use")
            return real_remove(path, *a, **k)

        with mock.patch.object(os, "remove", side_effect=_busy):
            old = updater.stage_and_replace(downloaded, target=target,
                                            popen=lambda _argv: None)
        self.assertNotEqual(old, stale)
        with open(stale, "rb") as f:
            self.assertEqual(f.read(), b"MZancient")        # 旧 .old 原样保留


class TestCleanupPendingOld(_TmpCase):
    def test_cleans_old_and_marker(self) -> None:
        marker = os.path.join(self.tmp, "pending_old.json")
        old = os.path.join(self.tmp, "AiEnvClone.exe.old")
        with open(old, "wb") as f:
            f.write(b"MZ")
        with open(marker, "w", encoding="utf-8") as f:
            __import__("json").dump({"old": old}, f)
        self.assertTrue(updater.cleanup_pending_old(marker))
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(marker))

    def test_locked_old_keeps_marker_quietly(self) -> None:
        """.old 还被占用 ⇒ 留待下次，不报错（新版本已经能跑，无害）。"""
        marker = os.path.join(self.tmp, "pending_old.json")
        old = os.path.join(self.tmp, "AiEnvClone.exe.old")
        with open(old, "wb") as f:
            f.write(b"MZ")
        with open(marker, "w", encoding="utf-8") as f:
            __import__("json").dump({"old": old}, f)
        real_remove = os.remove

        def _busy(path, *a, **k):
            if str(path) == str(old):
                raise PermissionError(32, "in use")
            return real_remove(path, *a, **k)

        with mock.patch.object(os, "remove", side_effect=_busy):
            self.assertFalse(updater.cleanup_pending_old(marker))
        self.assertTrue(os.path.exists(marker))             # 标记留着下次再试

    def test_missing_marker_is_noop(self) -> None:
        self.assertFalse(updater.cleanup_pending_old(
            os.path.join(self.tmp, "nope.json")))


class TestResumeFlagParsing(unittest.TestCase):
    def test_two_arg_forms(self) -> None:
        seen: list = []
        with mock.patch.object(updater, "_run_resume_update",
                               side_effect=lambda p: seen.append(p) or 0):
            self.assertEqual(updater.handle_update_flags(["--resume-update=C:\\a b\\x.exe"]), 0)
            self.assertEqual(updater.handle_update_flags(
                ["--resume-update", "C:\\a b\\x.exe"]), 0)
        self.assertEqual(seen, ["C:\\a b\\x.exe", "C:\\a b\\x.exe"])

    def test_unrelated_argv_returns_none(self) -> None:
        self.assertIsNone(updater.handle_update_flags(["--docs"]))
        self.assertIsNone(updater.handle_update_flags([]))


if __name__ == "__main__":
    unittest.main()
