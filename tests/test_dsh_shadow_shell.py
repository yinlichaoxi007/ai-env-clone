"""DSH「空壳高代际遮挡正文」的检测与修复测试（关系校验类受损的固化）。

背景：DSH 对不满足关系校验的日志会**静默丢弃**并改写成只有几行 seed 的空壳 v4，
原低代际日志的正文仍在磁盘上，但加载器按最高代际取用 ⇒ 界面是空会话。
修复 = 把低代际正文经 ``session_migration._dsh_v3_text`` 重生成合法 v3
（同 sid、保留原 ``createdAt``），并把空壳高代际日志与其投影缓存**移走**（不代写）。

判据只认**可证明的遮挡**：最高代际无 ``turn/start`` 且低代际确有 ``turn/start``；
只有一份日志、或低代际也没有正文的「真空会话」不算（没有可恢复的内容）。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import ai_env_clone.dsh_repair as dr
import ai_env_clone.session_migration as sm


def _make_ws_index(workspaces: dict) -> dict:
    return {
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": True,
                   "workspaceIds": list(workspaces.keys()),
                   "archivedSessionIds": []},
        "tables": {"workspaces": workspaces},
    }


def _write_json(path: str, obj) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


class _FakeDshHome:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh_shell_")
        self.dsh = os.path.join(self.tmp, ".dsh")
        self.sessions = os.path.join(self.dsh, "sessions")
        os.makedirs(self.sessions, exist_ok=True)

    def write_log(self, project_key: str, session_id: str,
                  name: str, lines: list) -> str:
        path = os.path.join(self.sessions, project_key, session_id)
        os.makedirs(path, exist_ok=True)
        full = os.path.join(path, name)
        with open(full, "wb") as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))
        return full

    def write_index(self, workspaces: dict) -> None:
        _write_json(os.path.join(self.dsh, "storages", "workspace.json"),
                    _make_ws_index(workspaces))

    def cache_record(self, session_id: str, blank: bool = False) -> str:
        path = os.path.join(self.dsh, "storages", "session_projcache",
                            "sessions", session_id + ".json")
        _write_json(path, {"version": 7, "record": {"identity": {}, "rows": {
            "title": {"ver": 1, "seq": 3, "val": "空壳标题"},
            "sessionListMetadata": {"ver": 1, "seq": 3,
                                    "val": {"blank": blank, "lastPromptAt": None}},
        }}})
        return path

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _header(version=3, seeded=False):
    return json.dumps({"type": "session", "version": version,
                       "id": "session-x", "createdAt": 1728000000000,
                       "cwd": "D:\\project\\a", "delegationDepth": 0,
                       "isSeeded": seeded, "agentPreset": "standard"})


def _body_lines():
    return [_header(3),
            json.dumps({"type": "session/title", "seq": 1,
                        "data": {"title": "旧标题"}}),
            json.dumps({"type": "turn/start", "seq": 2, "data": {"turn": 1}}),
            json.dumps({"type": "user/message", "seq": 3, "time": 1728000001000,
                        "data": {"content": "你好世界"}}),
            json.dumps({"type": "assistant/message", "seq": 4, "time": 1728000002000,
                        "data": {"message": {"content": [{"type": "text",
                                                          "text": "回复正文"}]}}}),
            json.dumps({"type": "turn/end", "seq": 5, "data": {"turn": 1}})]


def _shell_lines():
    return [_header(4, seeded=True),
            json.dumps({"type": "session/end-seed", "seq": 0, "data": {}}),
            json.dumps({"type": "permission/preset", "seq": 1, "data": {}}),
            json.dumps({"type": "sandbox/mode", "seq": 2, "data": {}}),
            json.dumps({"type": "approval/policy", "seq": 3, "data": {}})]


class TestDetectShadowedShells(unittest.TestCase):
    def setUp(self):
        _enable = dr._set_zstd_backend
        self._orig = (dr._scan_zstd_frames, dr._scan_zstd_frames_with_tail)
        _enable(lambda b: b, "fake")
        dr._scan_zstd_frames = lambda b: [(0, len(b))] if b else []
        dr._scan_zstd_frames_with_tail = \
            lambda b: ([(0, len(b))], None) if b else ([], 0)
        self.fx = _FakeDshHome()
        self.addCleanup(self._teardown)

    def _teardown(self):
        dr._reset_zstd_backend()
        dr._scan_zstd_frames, dr._scan_zstd_frames_with_tail = self._orig
        self.fx.cleanup()

    def test_shadowed_shell_detected(self) -> None:
        """低代际有正文、高代际是无 turn/start 的空壳 ⇒ 检出。"""
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v3.jsonl.zstd", _body_lines())
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v4.jsonl.zstd", _shell_lines())
        hits = dr.detect_shadowed_shells(self.fx.dsh, decompress=lambda b: b)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].session_id, "session-x")
        self.assertTrue(hits[0].shadow_file.endswith("session.v4.jsonl.zstd"))
        self.assertTrue(hits[0].body_file.endswith("session.v3.jsonl.zstd"))

    def test_genuinely_empty_session_not_flagged(self) -> None:
        """只有一份空壳（没有低代际正文）＝真空会话 ⇒ 不报（没有可恢复内容）。"""
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v4.jsonl.zstd", _shell_lines())
        self.assertEqual(dr.detect_shadowed_shells(self.fx.dsh,
                                                   decompress=lambda b: b), [])

    def test_body_without_turn_not_flagged(self) -> None:
        """低代际也没有正文（两份都是空壳）⇒ 不报。"""
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v3.jsonl.zstd", _shell_lines())
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v4.jsonl.zstd", _shell_lines())
        self.assertEqual(dr.detect_shadowed_shells(self.fx.dsh,
                                                   decompress=lambda b: b), [])

    def test_healthy_session_not_flagged(self) -> None:
        """最高代际本身有正文 ⇒ 不报。"""
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v4.jsonl.zstd", _body_lines())
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v3.jsonl.zstd", _shell_lines())
        self.assertEqual(dr.detect_shadowed_shells(self.fx.dsh,
                                                   decompress=lambda b: b), [])


class TestShellFixPlanAndApply(unittest.TestCase):
    def setUp(self):
        self._orig = (dr._scan_zstd_frames, dr._scan_zstd_frames_with_tail)
        dr._set_zstd_backend(lambda b: b, "fake", compress=lambda b: b)
        dr._scan_zstd_frames = lambda b: [(0, len(b))] if b else []
        dr._scan_zstd_frames_with_tail = \
            lambda b: ([(0, len(b))], None) if b else ([], 0)
        # 会话重生成走 session_migration 的写出器：同样注入恒等压缩后端，
        # 产物 = 明文 JSONL（恒等解压下仍可被本模块读取）。
        self._sm_patcher = mock.patch.object(
            sm, "_dsh_zstd_backend",
            return_value=(lambda b: b, lambda b: b, "fake"))
        self._sm_patcher.start()
        self.fx = _FakeDshHome()
        self.addCleanup(self._teardown)
        self.fx.write_log("--D-project-a--", "session-x",
                          "session.v3.jsonl.zstd", _body_lines())
        self.shadow = self.fx.write_log("--D-project-a--", "session-x",
                                        "session.v4.jsonl.zstd", _shell_lines())
        self.body = os.path.join(os.path.dirname(self.shadow),
                                 "session.v3.jsonl.zstd")
        self.cache = self.fx.cache_record("session-x", blank=True)
        self.fx.write_index({"ws-a": {"path": "D:\\project\\a", "title": "a",
                                      "sessionIds": ["session-x"],
                                      "createdAt": "t1", "updatedAt": "t1"}})

    def _teardown(self):
        self._sm_patcher.stop()
        dr._reset_zstd_backend()
        dr._scan_zstd_frames, dr._scan_zstd_frames_with_tail = self._orig
        self.fx.cleanup()

    def test_plan_lists_fix_and_apply_repairs(self) -> None:
        plan = dr.plan_dsh_repair(self.fx.dsh)
        self.assertEqual(len(plan.shell_fixes), 1)
        self.assertTrue(any("重生成会话 session-x" in line
                            for line in plan.describe()))

        outcome = dr.apply_dsh_repair(self.fx.dsh, plan)
        shells = outcome["shells"]
        self.assertEqual(len(shells), 1)
        self.assertTrue(shells[0]["ok"], shells[0])
        self.assertEqual(len(shells[0]["backups"]), 2)     # 两份日志都备份

        # 空壳高代际已被移走：原路径不存在、目录里出现 .bak- 改名件
        self.assertFalse(os.path.isfile(self.shadow))
        moved = [n for n in os.listdir(os.path.dirname(self.shadow))
                 if n.startswith("session.v4.jsonl.zstd.bak-")]
        self.assertEqual(len(moved), 1)

        # 正文已重生成：turn/start 与用户/助手内容都还在（保留原 createdAt 语义）
        with open(self.body, "rb") as fh:
            text = fh.read().decode("utf-8")
        self.assertIn("turn/start", text)
        self.assertIn("你好世界", text)
        self.assertIn("回复正文", text)
        self.assertIn("1728000000000", text)               # createdAt 保留
        session = sm._parse_dsh_text(text)
        self.assertEqual([m.content for m in session.messages if m.role == "user"],
                         ["你好世界"])

        # 该会话的投影缓存记录一并被移走（原路径不在、.bak- 存在）
        self.assertFalse(os.path.isfile(self.cache))
        moved_cache = [n for n in os.listdir(os.path.dirname(self.cache))
                       if n.startswith("session-x.json.bak-")]
        self.assertEqual(len(moved_cache), 1)

        # 修复后复检：空壳遮挡清零
        self.assertEqual(dr.detect_shadowed_shells(self.fx.dsh,
                                                   decompress=lambda b: b), [])

    def test_apply_without_backup(self) -> None:
        plan = dr.plan_dsh_repair(self.fx.dsh)
        outcome = dr.apply_dsh_repair(self.fx.dsh, plan, backup=False)
        self.assertTrue(outcome["shells"][0]["ok"])
        self.assertEqual(outcome["shells"][0]["backups"], [])
        # 备份关闭时也不残留 .bak 文件
        leftovers = [n for n in os.listdir(os.path.dirname(self.shadow))
                     if ".bak" in n and n.startswith("session.")]
        self.assertEqual(leftovers, [n for n in leftovers if ".bak-" in n])


class TestDetectResultSummary(unittest.TestCase):
    def test_summary_counts_and_attention_total(self) -> None:
        result = dr.DetectResult()
        self.assertIn("被空壳遮挡的会话：0 个", result.summary_lines())
        result.shadowed_shells = [dr.ShadowedShell(
            session_id="s", session_dir="d", shadow_file="a", shadow_version=4,
            body_file="b", body_version=3)]
        self.assertTrue(any("空壳遮挡的会话（" in line
                            for line in result.summary_lines()))
        self.assertEqual(result.attention_total, 1)
        self.assertFalse(result.healthy)


if __name__ == "__main__":
    unittest.main()
