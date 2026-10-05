"""子代理描述符版本修复测试（v2 → v3）。

背景：旧构建写下的 ``subagent/descriptor.data.version = 2``（实测 TeaVision 的 6 个
v0 子代理会话）会被官方 released v0→v1 迁移**直接拒绝**
（``subagent/descriptor 0 uses unsupported descriptor version 2``），界面表现就是
「会话加载错误」。修复＝在载荷满足 v3 语义时只把 version 改成 3（改前备份）。

全部用合成数据；缺少 zstd 后端时整组跳过。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_env_clone import dsh_repair as dr  # noqa: E402


def _zstd_pair():
    try:
        import zstandard  # type: ignore
    except ImportError:
        return None
    return (lambda b: zstandard.ZstdCompressor().compress(b),
            lambda b: zstandard.ZstdDecompressor().stream_reader(
                __import__("io").BytesIO(b)).read())


ZSTD = _zstd_pair()

CONTINUABLE = {"version": 2, "mode": "continuable", "provider": "spawn",
               "label": "Write Services interfaces",
               "agentProvider": "deepseek-official", "agentModel": "deepseek-v4-flash"}


class TestDescriptorPayloadRule(unittest.TestCase):
    """载荷语义检查与官方 ``subagentDescriptorValue`` 同规则。"""

    def test_real_v2_payload_is_upgradable(self) -> None:
        self.assertEqual(dr._descriptor_v3_problem(dict(CONTINUABLE)), "")

    def test_one_shot_label_optional(self) -> None:
        self.assertEqual(
            dr._descriptor_v3_problem({"mode": "one-shot", "provider": "spawn"}), "")

    def test_rejects_payloads_that_cannot_be_v3(self) -> None:
        cases = {
            "provider 空": {**CONTINUABLE, "provider": ""},
            "provider 非串": {**CONTINUABLE, "provider": 7},
            "mode 未知": {**CONTINUABLE, "mode": "weird"},
            "continuable 无 label": {**CONTINUABLE, "label": ""},
            "agent 未成对": {**CONTINUABLE, "agentModel": None},
            "toolFilter 非法": {**CONTINUABLE, "toolFilter": {"other": ["x"]}},
            "toolFilter 空数组": {**CONTINUABLE, "toolFilter": {"allow": []}},
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                payload = {k: v for k, v in data.items() if v is not None}
                self.assertTrue(dr._descriptor_v3_problem(payload), name)


@unittest.skipUnless(ZSTD is not None, "未安装 zstandard，跳过描述符修复用例")
class TestDescriptorFileRepair(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="desc_fix_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "session.jsonl.zstd")

    def _write(self, descriptor: dict) -> None:
        header = {"type": "session", "version": 0, "id": "abc", "createdAt": 1,
                  "cwd": r"D:\project\Demo", "parentSession": "session-parent",
                  "origin": "subagent", "delegationDepth": 1}
        events = [
            {"type": "subagent/descriptor", "seq": 0, "time": 1, "data": descriptor},
            {"type": "session/title", "seq": 1, "time": 1,
             "data": {"title": "T", "messageSeqs": [], "source": {"kind": "user"}}},
            {"type": "session/end-seed", "seq": 2, "time": 1, "data": {}},
        ]
        text = "\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":"))
                         for x in [header] + events) + "\n"
        with open(self.path, "wb") as fh:
            fh.write(ZSTD[0](text.encode("utf-8")))

    def _descriptor_version(self, path: str) -> object:
        with open(path, "rb") as fh:
            raw = ZSTD[1](fh.read()).decode("utf-8")
        for line in raw.split("\n"):
            if line.strip() and "subagent/descriptor" in line:
                return json.loads(line)["data"]["version"]
        return None

    def test_detect_then_repair_bumps_version(self) -> None:
        self._write(dict(CONTINUABLE))
        before = dr.repair_session_file(self.path, apply=False)
        self.assertEqual(before["status"], "需要修复")
        self.assertEqual(before["actions"][0]["rule"], "子代理描述符版本升级")
        self.assertEqual(self._descriptor_version(self.path), 2)   # dry-run 不改盘

        after = dr.repair_session_file(self.path, apply=True)
        self.assertEqual(after["status"], "已修复")
        self.assertTrue(after["backup"])
        self.assertEqual(self._descriptor_version(self.path), 3)
        # 备份里仍是原值（可回退）
        self.assertEqual(self._descriptor_version(after["backup"]), 2)

    def test_other_content_is_untouched(self) -> None:
        """只动 version 这一个字段：其余字节级不变（除未命中的帧）。"""
        self._write(dict(CONTINUABLE))
        with open(self.path, "rb") as fh:
            before = json.loads(ZSTD[1](fh.read()).decode("utf-8").split("\n")[1])
        dr.repair_session_file(self.path, apply=True)
        with open(self.path, "rb") as fh:
            after = json.loads(ZSTD[1](fh.read()).decode("utf-8").split("\n")[1])
        self.assertEqual(before["data"]["version"], 2)
        self.assertEqual(after["data"], {**before["data"], "version": 3})
        with open(self.path, "rb") as fh:
            same_title = json.loads(ZSTD[1](fh.read()).decode("utf-8").split("\n")[2])
        self.assertEqual(same_title["data"]["title"], "T")

    def test_repair_is_idempotent(self) -> None:
        self._write(dict(CONTINUABLE))
        dr.repair_session_file(self.path, apply=True)
        again = dr.repair_session_file(self.path, apply=False)
        self.assertEqual(again["status"], "正常")
        self.assertEqual(again["actions"], [])

    def test_unfixable_payload_is_skipped_not_rewritten(self) -> None:
        bad = dict(CONTINUABLE, label="")      # continuable 缺 label → 不能升到 3
        self._write(bad)
        report = dr.repair_session_file(self.path, apply=True)
        self.assertEqual(self._descriptor_version(self.path), 2)
        self.assertEqual(report["status"], "需要修复")   # 有动作（跳过说明）但仍需人工
        rules = [a["rule"] for a in report["actions"]]
        self.assertIn("子代理描述符未升级（已保留原样）", rules)

    def test_already_v3_needs_nothing(self) -> None:
        self._write({**CONTINUABLE, "version": 3})
        report = dr.repair_session_file(self.path, apply=False)
        self.assertEqual(report["status"], "正常")
        self.assertFalse(report["descriptor_hits"])

    def test_compatibility_scanner_no_longer_flags_after_repair(self) -> None:
        """健康检测用的兼容性扫描：修完之后不应再判为「无法加载」。"""
        home = os.path.join(self.tmp, "dsh_home")
        sess_dir = os.path.join(home, "sessions", "--D-project-Demo--", "session-x")
        os.makedirs(sess_dir)
        # _write 写的是 self.path；连同目录一起搬到假 DSH 主目录里
        self.path = os.path.join(sess_dir, "session.jsonl.zstd")
        self._write(dict(CONTINUABLE))
        self.assertEqual(dr.detect_incompatible_descriptors(home), ["session-x"])
        dr.repair_session_file(self.path, apply=True)
        self.assertEqual(dr.detect_incompatible_descriptors(home), [])


if __name__ == "__main__":
    unittest.main()
