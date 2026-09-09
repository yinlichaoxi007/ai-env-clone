"""DSH 旧会话数据「未分组 / 无法加载」检测与修复单元测试。

覆盖（对照 deepseekharnessfix 修复措施）：
1. ``project_key``：与 DSH 官方 ``session-persistence-jsonl`` 的 projectKey 等价。
2. 未分组检测：磁盘会话不在 ``workspace.json`` 的 ``sessionIds`` 中即视为未分组。
3. 修复计划与应用：attach / 补建工作区 / 备份 / dry-run / 原子写 / 绝不删除。
4. 旧格式（扁平 replayState）检测：v0 会话含 ``{kind,...}`` 形态时标出，
   新包络 ``{response, blocks}`` 不标。
5. CLI 冒烟：``scan`` 子命令输出结构化结果。

zstd 说明：测试以「恒等解压 + 整文件单帧」fake 后端注入（``_set_zstd_backend``
+ 覆写 ``_scan_zstd_frames``），无需真实 zstd；目录级检测路径不依赖 zstd。
"""

import json
import os
import shutil
import tempfile
import unittest

import ai_env_clone.dsh_repair as dr


def _make_ws_index(workspaces: dict) -> dict:
    """按真实结构构造 workspace.json（unit/global/tables 三层）。"""
    return {
        "unit": {"name": "workspace", "version": 2},
        "global": {
            "initialized": True,
            "workspaceIds": list(workspaces.keys()),
            "archivedSessionIds": [],
        },
        "tables": {"workspaces": workspaces},
    }


def _write_json(path: str, obj) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


class _FakeDshHome:
    """构造假的 ~/.dsh 目录：会话目录 + 可选 workspace.json。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh_repair_")
        self.dsh = os.path.join(self.tmp, ".dsh")
        self.sessions = os.path.join(self.dsh, "sessions")
        os.makedirs(self.sessions, exist_ok=True)

    def session(self, project_key: str, session_id: str, content: bytes = b"fake") -> str:
        """创建会话目录（含 session.jsonl.zstd 占位文件），返回目录路径。"""
        path = os.path.join(self.sessions, project_key, session_id)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "session.jsonl.zstd"), "wb") as fh:
            fh.write(content)
        return path

    def write_index(self, index: dict) -> str:
        path = os.path.join(self.dsh, "storages", "workspace.json")
        _write_json(path, index)
        return path

    def cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


_ORIG_SCAN_FRAMES = dr._scan_zstd_frames
_ORIG_SCAN_FRAMES_TAIL = dr._scan_zstd_frames_with_tail


def _enable_fake_zstd(compress=None):
    """注入恒等解压后端 + 单帧切分，使明文 JSONL 可被当作 zstd 会话读取。

    ``compress`` 用于内容修复路径（默认 None：仅只读检测可用）。
    """
    dr._set_zstd_backend(lambda b: b, "fake", compress=compress)
    dr._scan_zstd_frames = lambda b: [(0, len(b))] if b else []
    dr._scan_zstd_frames_with_tail = lambda b: ([(0, len(b))], None) if b else ([], 0)


def _disable_fake_zstd():
    dr._reset_zstd_backend()
    dr._scan_zstd_frames = _ORIG_SCAN_FRAMES
    dr._scan_zstd_frames_with_tail = _ORIG_SCAN_FRAMES_TAIL


def _has_zstandard() -> bool:
    """是否可导入 zstandard（构造真实 DSH 帧形态需要它）。"""
    try:
        import zstandard  # noqa: F401

        return True
    except ImportError:
        return False


def _real_backend():
    """返回可用的真实后端三元组；不可用时返回 None（用于 skipUnless）。"""
    decompress, compress, name = dr.zstd_backend()
    return (decompress, compress, name) if decompress is not None and compress is not None else None


def _dsh_style_frame(line: str) -> bytes:
    """按真实 DSH 写盘形态压一帧：FCS=0、非单段、带校验和。

    实测真实 home 的 30587 帧全部为 ``descriptor=0x04``（FCS_Field_Size=0、
    single_segment=0、checksum=1），此处精确复刻该形态。
    """
    import zstandard

    compressor = zstandard.ZstdCompressor(write_content_size=False, write_checksum=True)
    return compressor.compress((line + "\n").encode("utf-8"))


class TestProjectKey(unittest.TestCase):
    """对照 DSH 官方 jsonl.spec.ts 的 projectKey 用例。"""

    def test_official_vectors(self):
        cases = [
            ("/Users/qyj/work/deepseek-harness", "--Users-qyj-work-deepseek-harness--"),
            ("C:\\work\\agent", "--C-work-agent--"),
            ("/开发/~agent", "--~5F00~53D1-~007Eagent--"),
            ("/", "--root--"),
        ]
        for cwd, expected in cases:
            self.assertEqual(dr.project_key(cwd), expected, cwd)

    def test_lossy_separators(self):
        self.assertEqual(dr.project_key("/a/b-c"), dr.project_key("/a-b/c"))

    def test_truncation_bounded(self):
        self.assertEqual(len(dr.project_key("/" + "x" * 1000)), 255)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            dr.project_key("")


class TestGenerationLogDiscovery(unittest.TestCase):
    """代际文件名解析与会话目录日志定位（对照 DSH jsonl.spec.ts 的 canonical 用例）。"""

    def test_official_parse_vectors(self):
        # 与 DSH parseGenerationLogFilename 的官方用例一致
        self.assertEqual(dr.parse_generation_log_filename("session.jsonl", "none"), 0)
        self.assertEqual(dr.parse_generation_log_filename("session.v1.jsonl", "none"), 1)
        self.assertEqual(dr.parse_generation_log_filename("session.v27.jsonl.zstd", "zstd"), 27)
        self.assertEqual(dr.parse_generation_log_filename("session.jsonl.zstd", "zstd"), 0)
        for name in [
            "session.v0.jsonl",               # 版本号 0 必须用不带 v 的名字
            "session.v01.jsonl",              # 前导零
            "session.V1.jsonl",               # 大写
            "session.v1.backup.jsonl",        # 多余后缀
            "session.migration.deadbeef.tmp.jsonl",
            "session.v1.jsonl.zstd",          # 压缩类型不匹配
            "session.v9007199254740992.jsonl",  # 超过 JS 安全整数
            "session.v12.jsonl.zstd",         # 压缩类型不匹配（参数为 none）
            "README.md",
        ]:
            self.assertIsNone(dr.parse_generation_log_filename(name, "none"), name)
        self.assertIsNone(dr.parse_generation_log_filename("session.v1.jsonl", "zstd"))

    def test_latest_generation_wins(self):
        d = tempfile.mkdtemp(prefix="gen_disc_")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for name in ("session.jsonl.zstd", "session.v2.jsonl.zstd", "session.v10.jsonl.zstd"):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(b"x")
        path, version = dr.find_generation_log(d)
        self.assertEqual(os.path.basename(path), "session.v10.jsonl.zstd")
        self.assertEqual(version, 10)
        # 去掉最高代际后退回次高（DSH 取目录内最大版本号）
        os.remove(os.path.join(d, "session.v10.jsonl.zstd"))
        path, version = dr.find_generation_log(d)
        self.assertEqual(os.path.basename(path), "session.v2.jsonl.zstd")

    def test_only_v2_generation_is_discovered(self):
        """只有 session.v2.jsonl.zstd（没有 v0）的会话目录也能被发现。"""
        d = tempfile.mkdtemp(prefix="gen_disc_")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        home = os.path.join(d, ".dsh")
        proj = os.path.join(home, "sessions", "--D-project-a--")
        sess = os.path.join(proj, "session-only-v2")
        os.makedirs(sess)
        with open(os.path.join(sess, "session.v2.jsonl.zstd"), "wb") as fh:
            fh.write(b"x")
        sessions = dr.scan_sessions(home)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "session-only-v2")
        self.assertEqual(sessions[0].log_version, 2)
        self.assertEqual(os.path.basename(sessions[0].log_file), "session.v2.jsonl.zstd")

    def test_noncanonical_files_are_ignored(self):
        d = tempfile.mkdtemp(prefix="gen_disc_")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        os.makedirs(os.path.join(d, "tmp"))
        with open(os.path.join(d, "tmp", "session.migration.deadbeef.tmp.jsonl.zstd"), "wb") as fh:
            fh.write(b"x")
        path, version = dr.find_generation_log(os.path.join(d, "tmp"))
        self.assertIsNone(path)
        self.assertEqual(version, -1)

    def test_plaintext_generation_fallback(self):
        d = tempfile.mkdtemp(prefix="gen_disc_")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "session.jsonl"), "wb") as fh:
            fh.write(b"x")
        path, version = dr.find_generation_log(d, compression="zstd")
        self.assertEqual(os.path.basename(path), "session.jsonl")
        self.assertEqual(version, 0)


class TestDetectUngrouped(unittest.TestCase):
    def setUp(self):
        self.fx = _FakeDshHome()
        self.addCleanup(self.fx.cleanup)
        # 工作区 a/b 已登记；cwd 路径与项目目录名对应
        self.index = _make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": ["session-x"],
                     "createdAt": "t1", "updatedAt": "t1"},
            "ws-b": {"path": "D:\\project\\b", "title": "b", "sessionIds": [],
                     "createdAt": "t1", "updatedAt": "t1"},
        })
        # session-x 已登记；session-y（a 目录）、session-z（b 目录）未登记 → 未分组
        self.fx.session("--D-project-a--", "session-x")
        self.fx.session("--D-project-a--", "session-y")
        self.fx.session("--D-project-b--", "session-z")
        self.fx.write_index(self.index)

    def test_ungrouped_detected(self):
        result = dr.detect_ungrouped(self.fx.dsh)
        ids = {s.session_id for s in result.ungrouped}
        self.assertEqual(ids, {"session-y", "session-z"})
        # 无 zstd 时按目录名匹配，两条都可归属
        self.assertEqual(result.ungrouped_attachable, 2)
        self.assertEqual(result.ungrouped_unattachable, 0)
        self.assertEqual(result.sessions_total, 3)
        self.assertFalse(result.healthy)

    def test_index_problems_reported(self):
        broken = _make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": ["session-x"],
                     "createdAt": "t1", "updatedAt": "t1"},
        })
        broken["global"]["workspaceIds"] = ["ws-a", "ws-missing"]
        self.fx.write_index(broken)
        result = dr.detect_ungrouped(self.fx.dsh)
        self.assertTrue(any("ws-missing" in p for p in result.index_problems))

    def test_healthy_when_all_registered(self):
        self.index["tables"]["workspaces"]["ws-a"]["sessionIds"] = ["session-x", "session-y"]
        self.index["tables"]["workspaces"]["ws-b"]["sessionIds"] = ["session-z"]
        self.fx.write_index(self.index)
        result = dr.detect_ungrouped(self.fx.dsh)
        self.assertTrue(result.healthy)
        self.assertEqual(result.ungrouped, [])

    def test_missing_index_flags_all(self):
        # 删除索引后：全部会话视为未分组且不可自动归属
        os.remove(os.path.join(self.fx.dsh, "storages", "workspace.json"))
        result = dr.detect_ungrouped(self.fx.dsh)
        self.assertFalse(result.index_exists)
        self.assertEqual(result.sessions_total, 3)
        self.assertEqual(result.ungrouped_unattachable, 3)


class TestRepairPlanAndApply(unittest.TestCase):
    def setUp(self):
        self.fx = _FakeDshHome()
        self.addCleanup(self.fx.cleanup)
        self.index = _make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": ["session-x"],
                     "createdAt": "t1", "updatedAt": "t1"},
            "ws-b": {"path": "D:\\project\\b", "title": "b", "sessionIds": [],
                     "createdAt": "t1", "updatedAt": "t1"},
        })
        self.fx.session("--D-project-a--", "session-x")
        self.fx.session("--D-project-a--", "session-y")
        self.fx.session("--D-project-b--", "session-z")
        self.fx.write_index(self.index)

    def test_plan_attaches_ungrouped(self):
        plan, idx = dr.plan_workspace_index_repair(self.fx.dsh)
        by_session = {m.session_id: m for m in plan.mutations}
        self.assertIn("session-y", by_session)
        self.assertEqual(by_session["session-y"].workspace_id, "ws-a")
        self.assertIn("session-z", by_session)
        self.assertEqual(by_session["session-z"].workspace_id, "ws-b")
        self.assertEqual(plan.skipped, [])

    def test_apply_dry_run_does_not_write(self):
        plan, idx = dr.plan_workspace_index_repair(self.fx.dsh)
        res = dr.apply_workspace_index_repair(self.fx.dsh, plan, dry_run=True, idx=idx)
        self.assertTrue(res.dry_run)
        self.assertTrue(res.ok)
        with open(os.path.join(self.fx.dsh, "storages", "workspace.json"), "r", encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after["tables"]["workspaces"]["ws-a"]["sessionIds"], ["session-x"])
        self.assertFalse(any(fn.startswith("workspace.json.bak") for fn in os.listdir(os.path.join(self.fx.dsh, "storages"))))

    def test_apply_writes_with_backup_and_never_deletes(self):
        plan, idx = dr.plan_workspace_index_repair(self.fx.dsh)
        res = dr.apply_workspace_index_repair(self.fx.dsh, plan, dry_run=False, backup=True, idx=idx)
        self.assertTrue(res.ok)
        self.assertEqual(res.applied, 2)
        # 备份存在
        backups = [fn for fn in os.listdir(os.path.join(self.fx.dsh, "storages")) if fn.startswith("workspace.json.bak")]
        self.assertEqual(len(backups), 1)
        # 写入结果：新会话置顶、旧会话保留（绝不删除）
        with open(os.path.join(self.fx.dsh, "storages", "workspace.json"), "r", encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after["tables"]["workspaces"]["ws-a"]["sessionIds"], ["session-y", "session-x"])
        self.assertEqual(after["tables"]["workspaces"]["ws-b"]["sessionIds"], ["session-z"])
        # 备份内容 = 修复前原样
        with open(os.path.join(self.fx.dsh, "storages", backups[0]), "r", encoding="utf-8") as fh:
            before = json.load(fh)
        self.assertEqual(before["tables"]["workspaces"]["ws-a"]["sessionIds"], ["session-x"])

    def test_apply_second_run_is_noop(self):
        plan, idx = dr.plan_workspace_index_repair(self.fx.dsh)
        dr.apply_workspace_index_repair(self.fx.dsh, plan, dry_run=False, backup=True, idx=idx)
        plan2, _ = dr.plan_workspace_index_repair(self.fx.dsh)
        self.assertTrue(plan2.empty)

    def test_create_workspace_for_unknown_cwd(self):
        """有 header cwd 但无工作区记录 → 补建工作区（等价官方 bootstrap）。"""
        _enable_fake_zstd()
        self.addCleanup(_disable_fake_zstd)
        header_line = json.dumps({
            "type": "session", "version": 0, "id": "session-new",
            "createdAt": 1, "cwd": "D:\\project\\new", "delegationDepth": 0,
        })
        self.fx.session("--D-project-new--", "session-new", content=header_line.encode("utf-8"))
        plan, idx = dr.plan_workspace_index_repair(self.fx.dsh)
        creates = [m for m in plan.mutations if m.action == "create-workspace"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].path, "D:\\project\\new")
        res = dr.apply_workspace_index_repair(self.fx.dsh, plan, dry_run=False, backup=True, idx=idx)
        self.assertTrue(res.ok)
        with open(os.path.join(self.fx.dsh, "storages", "workspace.json"), "r", encoding="utf-8") as fh:
            after = json.load(fh)
        ws = after["tables"]["workspaces"]
        new_record = ws.get(creates[0].workspace_id)
        self.assertIsNotNone(new_record)
        self.assertEqual(new_record["path"], "D:\\project\\new")
        self.assertEqual(new_record["sessionIds"], ["session-new"])
        self.assertIn(creates[0].workspace_id, after["global"]["workspaceIds"])


class TestLegacyReplayDetection(unittest.TestCase):
    def setUp(self):
        _enable_fake_zstd()
        self.addCleanup(_disable_fake_zstd)
        self.fx = _FakeDshHome()
        self.addCleanup(self.fx.cleanup)
        self.fx.write_index(_make_ws_index({}))
        self._header = json.dumps({
            "type": "session", "version": 0, "id": "session-replay",
            "createdAt": 1, "cwd": "D:\\project\\r", "delegationDepth": 0,
        })

    def _session_content(self, replay_state: dict) -> bytes:
        lines = [self._header]
        lines.append(json.dumps({
            "type": "assistant/chunk", "seq": 1, "time": 2,
            "data": {"chunk": {"type": "finish", "reason": {"kind": "stop"}, "replayState": replay_state}},
        }))
        return ("\n".join(lines) + "\n").encode("utf-8")

    def test_flat_legacy_replay_detected(self):
        flat = {"kind": "pi-ai", "version": 1, "api": "openai-completions",
                "provider": "m", "model": "m", "responseId": "r", "stopReason": "stop",
                "blocks": [{"type": "text"}]}
        self.fx.session("--D-project-r--", "session-replay", self._session_content(flat))
        result = dr.detect_ungrouped(self.fx.dsh, decompress=lambda b: b)
        self.assertEqual(result.legacy_replay_sessions, ["session-replay"])

    def test_envelope_replay_not_flagged(self):
        envelope = {"response": {"id": "r"}, "blocks": [{"type": "text"}]}
        self.fx.session("--D-project-r--", "session-replay", self._session_content(envelope))
        result = dr.detect_ungrouped(self.fx.dsh, decompress=lambda b: b)
        self.assertEqual(result.legacy_replay_sessions, [])

    def test_flat_replay_flagged_regardless_of_version(self):
        """与 deepseekharnessfix 一致：按数据形态判定，与格式版本无关。"""
        header_v1 = json.dumps({
            "type": "session", "version": 1, "id": "session-v1",
            "createdAt": 1, "cwd": "D:\\project\\r", "delegationDepth": 0,
        })
        flat = {"kind": "pi-ai", "version": 1}
        content = (
            header_v1
            + "\n"
            + json.dumps({"type": "assistant/chunk", "seq": 1, "time": 2,
                          "data": {"chunk": {"type": "finish", "reason": {"kind": "stop"},
                                             "replayState": flat}}})
            + "\n"
        ).encode("utf-8")
        self.fx.session("--D-project-r--", "session-v1", content)
        result = dr.detect_ungrouped(self.fx.dsh, decompress=lambda b: b)
        self.assertEqual(result.legacy_replay_sessions, ["session-v1"])

    def test_detection_agrees_with_repair_on_unicode_line_separators(self):
        """回归：正文含 U+2028 / U+0085 时检测不得漏报。

        ``str.splitlines()`` 会在 U+2028/U+2029/U+0085 处断行；这些字符
        ``JSON.stringify`` 不转义、会原样出现在 JSONL 正文里，若检测按
        ``splitlines()`` 切行就会把整行 JSON 切碎而静默跳过（漏报），
        而修复路径只按 ``\\n`` 切行、照常改写——两者口径必须一致。
        """
        flat = {"kind": "pi-ai", "version": 1}
        lines = [self._header]
        lines.append(json.dumps({
            "type": "assistant/chunk", "seq": 1, "time": 2,
            "data": {"chunk": {"type": "finish", "text": "前\u2028中\u0085后",
                               "replayState": flat}},
        }, ensure_ascii=False))
        content = ("\n".join(lines) + "\n").encode("utf-8")
        self.fx.session("--D-project-r--", "session-replay", content)

        result = dr.detect_ungrouped(self.fx.dsh, decompress=lambda b: b)
        self.assertEqual(result.legacy_replay_sessions, ["session-replay"])

        _, report = dr.repair_session_data(content)
        self.assertEqual(len(report["hits"]), 1)


@unittest.skipUnless(_real_backend(), "需要真实 zstd 后端")
class TestPlaintextLogDetection(unittest.TestCase):
    """明文会话日志（DSH ``compression: none``）的检测路径不得被静默跳过。

    回归：``_scan_file_content`` 原先无条件走 zstd 解压，对明文日志抛异常并被
    吞掉 → 整份日志被判为「无问题」，而修复路径却能正常改写。
    """

    def test_plaintext_session_content_is_scanned(self):
        fx = _FakeDshHome()
        self.addCleanup(fx.cleanup)
        fx.write_index(_make_ws_index({}))
        header = json.dumps({
            "type": "session", "version": 0, "id": "session-plain",
            "createdAt": 1, "cwd": "D:\\project\\p", "delegationDepth": 0,
        })
        chunk = json.dumps({
            "type": "assistant/chunk", "seq": 1, "time": 2,
            "data": {"chunk": {"type": "finish",
                               "replayState": {"kind": "pi-ai", "version": 1}}},
        })
        content = header + "\n" + chunk + "\n"
        path = os.path.join(fx.sessions, "--D-project-p--", "session-plain", "session.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

        result = dr.detect_ungrouped(fx.dsh)
        self.assertEqual(result.legacy_replay_sessions, ["session-plain"])
        # header 也要能读出来（否则无法按 cwd 归属工作区）
        self.assertEqual(result.ungrouped[0].header["id"], "session-plain")

        _, report = dr.repair_session_data(content.encode("utf-8"))
        self.assertEqual(len(report["hits"]), 1)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.fx = _FakeDshHome()
        self.addCleanup(self.fx.cleanup)
        self.fx.write_index(_make_ws_index({}))
        self.fx.session("--D-project-a--", "session-x")

    def test_scan_json(self):
        from io import StringIO
        import contextlib
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = dr.main(["scan", "--dsh-home", self.fx.dsh, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["sessions_total"], 1)
        self.assertEqual(data["ungrouped"][0]["session_id"], "session-x")

    def test_fix_requires_apply(self):
        """有可执行修改时，未加 --apply 只打印 dry-run、不写盘。"""
        from io import StringIO
        import contextlib
        # 建一个目录名可匹配的工作区 → 有真实的 attach 修改
        self.fx.write_index(_make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": [],
                     "createdAt": "t1", "updatedAt": "t1"},
        }))
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = dr.main(["fix", "--dsh-home", self.fx.dsh])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", buf.getvalue())
        self.assertIn("加入工作区", buf.getvalue())
        # 未加 --apply 不写盘
        with open(os.path.join(self.fx.dsh, "storages", "workspace.json"), "r", encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after["tables"]["workspaces"]["ws-a"]["sessionIds"], [])

    def test_fix_noop_when_only_skipped(self):
        """无工作区可匹配（且无 header）时，fix 是 no-op：提示跳过、不写盘。"""
        from io import StringIO
        import contextlib
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = dr.main(["fix", "--dsh-home", self.fx.dsh, "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("跳过", buf.getvalue())
        with open(os.path.join(self.fx.dsh, "storages", "workspace.json"), "r", encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after["tables"]["workspaces"], {})


class TestFlatReplayWrap(unittest.TestCase):
    """规则一：扁平 replayState 无损包装（对照 会话数据修复.mjs 的 包装扁平replayState）。"""

    def test_wraps_flat_and_keeps_blocks(self):
        flat = {"kind": "pi-ai", "version": 1, "api": "openai-completions",
                "provider": "p", "model": "m", "responseId": "r", "stopReason": "stop",
                "blocks": [{"type": "text"}]}
        original = dict(flat)
        new, wrapped = dr._wrap_flat_replay_state(flat)
        self.assertTrue(wrapped)
        self.assertEqual(set(new.keys()), {"response", "blocks"})
        self.assertEqual(new["response"], {k: v for k, v in original.items() if k != "blocks"})
        self.assertEqual(new["blocks"], original["blocks"])
        self.assertEqual(flat, original)  # 不修改入参

    def test_no_blocks_key(self):
        flat = {"kind": "k", "version": 1}
        new, wrapped = dr._wrap_flat_replay_state(flat)
        self.assertTrue(wrapped)
        self.assertEqual(new, {"response": {"kind": "k", "version": 1}})

    def test_envelope_is_idempotent(self):
        envelope = {"response": {"id": "r"}, "blocks": [{"type": "text"}]}
        new, wrapped = dr._wrap_flat_replay_state(envelope)
        self.assertFalse(wrapped)
        self.assertIs(new, envelope)

    def test_wrap_two_locations(self):
        chunk_event = {
            "type": "assistant/chunk", "seq": 1,
            "data": {"chunk": {"type": "finish", "replayState": {"kind": "pi-ai", "version": 1}}},
        }
        message_event = {
            "type": "assistant/message", "seq": 2,
            "data": {"message": {"source": {"callId": "c1",
                                            "replayState": {"kind": "pi-ai", "version": 1}}}},
        }
        # 检测模式：命中但不改
        new_event, changed, hits = dr.wrap_line_event(chunk_event, rewrite=False)
        self.assertFalse(changed)
        self.assertIs(new_event, chunk_event)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "assistant/chunk.data.chunk.replayState")
        self.assertEqual(hits[0]["fields"], ["kind", "version"])
        self.assertEqual(hits[0]["seq"], 1)

        # 修复模式：只改 replayState 键，其余字段保留
        new_event, changed, hits = dr.wrap_line_event(message_event, rewrite=True)
        self.assertTrue(changed)
        self.assertEqual(hits[0]["path"], "assistant/message.data.message.source.replayState")
        source = new_event["data"]["message"]["source"]
        self.assertEqual(source["replayState"], {"response": {"kind": "pi-ai", "version": 1}})
        self.assertEqual(source["callId"], "c1")
        self.assertEqual(message_event["data"]["message"]["source"]["replayState"]["kind"], "pi-ai")

    def test_non_json_line_passes_through(self):
        event, changed, hits = dr.wrap_line_event("not json", rewrite=True)
        self.assertFalse(changed)
        self.assertEqual(hits, [])


# --------------------------------------------------------------------------- #
# 规则二：重复 tool-call id 去重（用例取自 验证重复id修复.mjs）
# --------------------------------------------------------------------------- #
def _tool_call_block(cid):
    return {"type": "tool-call", "id": cid, "name": "read", "args": {"path": "a.txt"}}


def _assistant_message(seq, *ids):
    return {
        "type": "assistant/message", "seq": seq,
        "data": {"turn": 1, "step": 1,
                 "message": {"id": "msg-%d" % seq, "role": "assistant",
                             "content": [_tool_call_block(i) for i in ids]}},
    }


def _tool_call(seq, call_id):
    return {"type": "tool/call", "seq": seq, "data": {"callId": call_id, "name": "read", "args": {}}}


def _tool_result(seq, call_id):
    return {"type": "tool/result", "seq": seq,
            "data": {"turn": 1, "step": 1, "message": {"source": {"callId": call_id}, "content": []}}}


class TestDuplicateCallIdFix(unittest.TestCase):
    """与 dsh-session-surgeon 的 disambiguateDuplicateToolCallIds 同语义。"""

    def setUp(self):
        self.events = [
            {"type": "turn/start", "seq": 0, "data": {"turn": 1}},
            _assistant_message(1, "call_a"),
            _tool_call(2, "call_a"),
            _tool_result(3, "call_a"),
            _assistant_message(4, "call_a"),    # 同一步内重复宣告 → call_a#2
            _tool_call(5, "call_a"),
            _tool_result(6, "call_a"),
            _assistant_message(7, "call_a"),    # 再次重复 → call_a#3
            _tool_call(8, "call_a"),
            {"type": "step/end", "seq": 9, "data": {"step": 1}},
            _assistant_message(10, "call_a"),   # 新一步：重新计数，不再改写
            _tool_call(11, "call_a"),
            _tool_result(12, "call_a"),
            _assistant_message(13, "call_b", "call_b", ""),  # 同一消息内重复 + 空 id
            _tool_call(14, "call_b"),
            _tool_call(15, "call_b"),
        ]

    def test_canonical_case(self):
        rewrites, hits, count = dr.disambiguate_duplicate_call_ids(self.events)
        # 宣告级命中 3 处；事件级改写 7 处（与 deepseekharnessfix 脚本一致）
        self.assertEqual(len(hits), 3)
        self.assertEqual(count, len(rewrites))
        self.assertEqual(count, 7)
        self.assertEqual([h["new_id"] for h in hits], ["call_a#2", "call_a#3", "call_b#2"])
        # 首次宣告保留原 id，不产生改写
        for idx in (1, 2, 3):
            self.assertNotIn(idx, rewrites)
        # 重复宣告被加后缀
        self.assertEqual(rewrites[4]["data"]["message"]["content"][0]["id"], "call_a#2")
        self.assertEqual(rewrites[7]["data"]["message"]["content"][0]["id"], "call_a#3")
        # 按出现顺序重映射 tool/call 与 tool/result
        self.assertEqual(rewrites[5]["data"]["callId"], "call_a#2")
        self.assertEqual(rewrites[6]["data"]["message"]["source"]["callId"], "call_a#2")
        self.assertEqual(rewrites[8]["data"]["callId"], "call_a#3")
        # 同一消息内首次 call_b 不改，第二次改后缀
        self.assertEqual(rewrites[13]["data"]["message"]["content"][0]["id"], "call_b")
        self.assertEqual(rewrites[13]["data"]["message"]["content"][1]["id"], "call_b#2")
        self.assertEqual(rewrites[15]["data"]["callId"], "call_b#2")
        # step/end 后重新计数：不产生改写
        for idx in (10, 11, 12, 14):
            self.assertNotIn(idx, rewrites)
        # 空 id 永不凭空生成
        self.assertEqual(rewrites[13]["data"]["message"]["content"][2]["id"], "")
        # 不可变式改写：原事件不变
        self.assertEqual(self.events[4]["data"]["message"]["content"][0]["id"], "call_a")
        self.assertEqual(self.events[5]["data"]["callId"], "call_a")

    def test_detect_duplicate_advertised(self):
        hits = dr._duplicate_advertised_ids(self.events)
        self.assertEqual([h["callId"] for h in hits], ["call_a", "call_a", "call_b"])

    def test_no_duplicates_no_rewrites(self):
        events = [
            {"type": "turn/start", "seq": 0, "data": {}},
            _assistant_message(1, "call_a"),
            _tool_call(2, "call_a"),
            _tool_result(3, "call_a"),
        ]
        rewrites, hits, count = dr.disambiguate_duplicate_call_ids(events)
        self.assertEqual(rewrites, {})
        self.assertEqual(hits, [])
        self.assertEqual(count, 0)

    def test_detect_other_problems(self):
        problems = dr.detect_other_problems(self.events)
        self.assertTrue(any(p["code"] == "重复的tool-call id" for p in problems))
        # 存在紧凑行（seq 必然跳号）时不做序号缺口判定
        packed = {"type": "text-chunks", "seq": 5, "data": {}}
        self.assertFalse(any(p["code"] == "seq 不连续" for p in dr.detect_other_problems(
            [{"type": "turn/start", "seq": 1, "data": {}}, {"type": "x", "seq": 3, "data": {}}, packed])))
        # 无紧凑行时才报告序号缺口
        self.assertTrue(any(p["code"] == "seq 不连续" for p in dr.detect_other_problems(
            [{"type": "turn/start", "seq": 1, "data": {}}, {"type": "x", "seq": 3, "data": {}}])))


class TestSessionDataRepair(unittest.TestCase):
    """会话文件内容修复（明文 JSONL 分支，无需 zstd 后端）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh_data_repair_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _lines(self, *events):
        return ("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n").encode("utf-8")

    def _header(self):
        return {"type": "session", "version": 0, "id": "session-r", "createdAt": 1,
                "cwd": "D:\\project\\r", "delegationDepth": 0}

    def _flat_chunk(self, seq=1):
        return {"type": "assistant/chunk", "seq": seq, "time": seq * 10,
                "data": {"chunk": {"type": "finish", "reason": {"kind": "stop"},
                                   "replayState": {"kind": "pi-ai", "version": 1,
                                                   "provider": "p", "model": "m",
                                                   "blocks": [{"type": "text"}]}}}}

    def test_detect_reports_need_repair(self):
        data = self._lines(self._header(), self._flat_chunk(1),
                           {"type": "turn/start", "seq": 3, "data": {}})
        report = dr.detect_session_data(data)
        self.assertEqual(report["status"], "需要修复")
        self.assertTrue(report["plain"])
        self.assertEqual(report["format_version"], 0)
        self.assertEqual(report["session_id"], "session-r")
        self.assertEqual(len(report["hits"]), 1)
        self.assertEqual(report["actions"][0]["rule"], "扁平replayState包装")
        self.assertFalse(report["changed"])
        self.assertTrue(any(p["code"] == "seq 不连续" for p in report["problems"]))

    def test_repair_wraps_and_is_idempotent(self):
        data = self._lines(self._header(), self._flat_chunk(1))
        new_data, report = dr.repair_session_data(data)
        self.assertEqual(report["status"], "已修复")
        self.assertTrue(report["changed"])
        lines = new_data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)
        replay = json.loads(lines[1])["data"]["chunk"]["replayState"]
        self.assertEqual(set(replay.keys()), {"response", "blocks"})
        self.assertEqual(replay["response"]["kind"], "pi-ai")
        self.assertEqual(replay["response"]["provider"], "p")
        self.assertEqual(replay["blocks"], [{"type": "text"}])
        # 非命中行保持原字节
        self.assertEqual(lines[0], json.dumps(self._header(), ensure_ascii=False))
        # 幂等：第二次不再命中、不再改写
        again, report2 = dr.repair_session_data(new_data)
        self.assertEqual(report2["status"], "正常")
        self.assertEqual(report2["hits"], [])
        self.assertEqual(again, new_data)

    def _lines_compact(self, *events):
        return ("\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":"))
                          for e in events) + "\n").encode("utf-8")

    def test_rewrite_stays_compact_like_json_stringify(self):
        """重写必须保持 DSH 的紧凑 JSON 风格（无空格），否则体积膨胀 12%+。"""
        flat = {"kind": "pi-ai", "version": 1, "provider": "p", "model": "m"}
        chunk = {"type": "assistant/chunk", "seq": 1, "time": 10,
                 "data": {"chunk": {"type": "finish", "reason": {"kind": "stop"},
                                    "replayState": flat}}}
        data = self._lines_compact(self._header(), chunk)
        new_data, report = dr.repair_session_data(data)
        self.assertEqual(report["status"], "已修复")
        out = new_data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(out), 2)
        fixed = out[1]
        # 不允许出现默认 json.dumps 插入的空格分隔符
        self.assertNotIn('": "', fixed)
        self.assertNotIn('", "', fixed)
        self.assertTrue(fixed.startswith('{"type":"assistant/chunk"'))
        # 体积只增加信封本身（`{"response":` 12 字符 + `}` 1 字符 = 13），不因空格膨胀
        self.assertEqual(len(fixed) - len(self._lines_compact(chunk).rstrip(b"\n").decode("utf-8")), 13)

    def test_plain_branch_preserves_trailing_newline(self):
        data = self._lines(self._header(), self._flat_chunk(1))
        new_data, _ = dr.repair_session_data(data)
        self.assertTrue(new_data.endswith(b"\n"))
        new2, _ = dr.repair_session_data(data.rstrip(b"\n"))
        self.assertFalse(new2.endswith(b"\n"))

    def test_dup_ids_not_fixed_by_default(self):
        events = [
            self._header(),
            {"type": "turn/start", "seq": 1, "data": {}},
            _assistant_message(2, "call_a"),
            _tool_call(3, "call_a"),
            _assistant_message(4, "call_a"),
            _tool_call(5, "call_a"),
        ]
        data = self._lines(*events)
        # 默认关闭：只报告问题，不改写
        report = dr.detect_session_data(data)
        self.assertEqual(report["status"], "正常")
        self.assertEqual(report["dup_fixes"], [])
        self.assertTrue(any(p["code"] == "重复的tool-call id" for p in report["problems"]))
        # 显式开启：宣告加后缀 + 按顺序重映射
        new_data, report2 = dr.repair_session_data(data, fix_dup_ids=True)
        self.assertEqual(report2["status"], "已修复")
        self.assertEqual([h["new_id"] for h in report2["dup_fixes"]], ["call_a#2"])
        self.assertEqual(report2["dup_rewrite_fields"], 2)
        self.assertEqual(report2["actions"][0]["rule"], "重复tool-call id去重")
        lines = new_data.decode("utf-8").strip().split("\n")
        self.assertEqual(json.loads(lines[4])["data"]["message"]["content"][0]["id"], "call_a#2")
        self.assertEqual(json.loads(lines[5])["data"]["callId"], "call_a#2")

    def test_file_repair_dry_run_and_apply(self):
        path = os.path.join(self.tmp, "session.jsonl.zstd")  # 明文内容 + zstd 命名
        data = self._lines(self._header(), self._flat_chunk(1))
        with open(path, "wb") as fh:
            fh.write(data)

        report = dr.repair_session_file(path, apply=False)
        self.assertEqual(report["status"], "需要修复")
        self.assertEqual(report["backup"], "")
        self.assertEqual(report["file"], path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)  # dry-run 不写盘

        report = dr.repair_session_file(path, apply=True)
        self.assertEqual(report["status"], "已修复")
        self.assertTrue(report["backup"])
        self.assertTrue(os.path.basename(report["backup"]).startswith("session.jsonl.zstd.bak."))
        self.assertTrue(os.path.isfile(report["backup"]))
        with open(report["backup"], "rb") as fh:
            self.assertEqual(fh.read(), data)  # 备份 = 修复前原样
        with open(path, "rb") as fh:
            self.assertNotEqual(fh.read(), data)

        # 再次修复：无需修复，也不新增备份
        self.assertEqual(dr.repair_session_file(path, apply=True)["status"], "正常")
        backups = [f for f in os.listdir(self.tmp) if ".bak." in f]
        self.assertEqual(backups, [os.path.basename(report["backup"])])

    def test_missing_file_refused(self):
        report = dr.repair_session_file(os.path.join(self.tmp, "nope.jsonl.zstd"))
        self.assertEqual(report["status"], "拒绝")
        self.assertEqual(report["problems"][0]["code"], "读取失败")

    def test_root_walk_and_summary(self):
        root = os.path.join(self.tmp, "sessions")
        target = os.path.join(root, "--D-project-r--", "session-r", "session.jsonl.zstd")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        content = self._lines(self._header(), self._flat_chunk(1))
        with open(target, "wb") as fh:
            fh.write(content)
        # 名字不匹配的文件不纳入批量
        with open(os.path.join(root, "notes.jsonl"), "wb") as fh:
            fh.write(b"{}")

        summary = dr.repair_session_root(root, apply=True)
        self.assertEqual(summary["file_total"], 1)
        self.assertEqual(summary["repaired"], 1)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["results"][0]["status"], "已修复")
        with open(target, "rb") as fh:
            self.assertNotEqual(fh.read(), content)

        summary2 = dr.repair_session_root(root)
        self.assertEqual(summary2["need_repair"], 0)
        self.assertEqual(summary2["repaired"], 0)

    def test_zstd_backend_missing_refused(self):
        data = b"\x28\xb5\x2f\xfd" + b"truncated-zstd-payload"
        dr._set_zstd_backend(None, "none", compress=None)
        self.addCleanup(dr._reset_zstd_backend)
        report = dr.detect_session_data(data)
        self.assertEqual(report["status"], "拒绝")
        self.assertEqual(report["problems"][0]["code"], "缺少zstd")
        _, report2 = dr.repair_session_data(data)
        self.assertEqual(report2["problems"][0]["code"], "缺少zstd")


    def test_root_summary_separates_zstd_missing_from_corrupt(self):
        """无 zstd 后端时：zstd 文件计入「跳过」，不算损坏；明文文件仍可修复。"""
        root = os.path.join(self.tmp, "sessions")
        magic = b"\x28\xb5\x2f\xfd"
        zstd_path = os.path.join(root, "--D-project-a--", "session-zstd", "session.jsonl.zstd")
        plain_path = os.path.join(root, "--D-project-b--", "session-plain", "session.jsonl.zstd")
        for path in (zstd_path, plain_path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(zstd_path, "wb") as fh:
            fh.write(magic + b"opaque-payload")
        with open(plain_path, "wb") as fh:
            fh.write(self._lines(self._header(), self._flat_chunk(1)))

        dr._set_zstd_backend(None, "", compress=None)
        self.addCleanup(dr._reset_zstd_backend)
        summary = dr.repair_session_root(root, apply=True)
        self.assertEqual(summary["file_total"], 2)
        self.assertEqual(summary["skipped_no_zstd"], 1)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["repaired"], 1)
        by_status = {os.path.basename(os.path.dirname(r["file"])): r["status"] for r in summary["results"]}
        self.assertEqual(by_status["session-zstd"], "拒绝")
        self.assertEqual(by_status["session-plain"], "已修复")


class TestZstdFrameRepair(unittest.TestCase):
    """zstd 多帧分支：只重压缩命中的帧，其余帧保持原字节；torn 尾原样保留。"""

    def setUp(self):
        self._orig_scan = dr._scan_zstd_frames_with_tail

    def tearDown(self):
        dr._scan_zstd_frames_with_tail = self._orig_scan

    def test_only_touched_frame_recompressed_and_tail_kept(self):
        header_event = {"type": "session", "version": 0, "id": "s1"}
        chunk_event = {"type": "assistant/chunk", "seq": 1,
                       "data": {"chunk": {"type": "finish",
                                          "replayState": {"kind": "pi-ai", "version": 1}}}}
        frame1 = (json.dumps(header_event, ensure_ascii=False) + "\n").encode("utf-8")
        frame2 = (json.dumps(chunk_event, ensure_ascii=False) + "\n").encode("utf-8")
        wrapped, _ = dr._wrap_flat_replay_state(chunk_event["data"]["chunk"]["replayState"])
        expected_frame2 = (
            json.dumps({**chunk_event, "data": {"chunk": {"type": "finish", "replayState": wrapped}}},
                       ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        buffer = frame1 + frame2 + b"\x00TORN-TAIL"
        marker = b"~RECOMPRESSED~"
        dr._scan_zstd_frames_with_tail = lambda b: (
            [(0, len(frame1)), (len(frame1), len(frame1) + len(frame2))],
            len(frame1) + len(frame2),
        )
        new_data, report = dr._repair_zstd_buffer(
            buffer, True, False, lambda b: b, lambda b: b + marker
        )
        self.assertEqual(report["frames"], 2)
        self.assertTrue(report["torn_tail"])
        self.assertEqual(report["format_version"], 0)
        self.assertEqual(report["session_id"], "s1")
        self.assertEqual(report["status"], "已修复")
        self.assertEqual(len(report["hits"]), 1)
        # 未命中帧保持原字节；仅命中帧重压缩；torn 尾原样追加
        self.assertEqual(new_data, frame1 + expected_frame2 + marker + b"\x00TORN-TAIL")
        self.assertTrue(new_data.startswith(frame1))
        self.assertEqual(new_data.count(marker), 1)

    def test_detect_mode_does_not_rewrite_frames(self):
        frame1 = b'{"type":"session","version":0,"id":"s1"}\n'
        frame2 = (json.dumps({"type": "assistant/chunk", "seq": 1,
                              "data": {"chunk": {"type": "finish",
                                                 "replayState": {"kind": "pi-ai", "version": 1}}}})
                  + "\n").encode("utf-8")
        buffer = frame1 + frame2
        dr._scan_zstd_frames_with_tail = lambda b: (
            [(0, len(frame1)), (len(frame1), len(buffer))], None
        )
        new_data, report = dr._repair_zstd_buffer(
            buffer, False, False, lambda b: b, lambda b: b + b"X"
        )
        self.assertIsNone(new_data)
        self.assertEqual(report["status"], "需要修复")
        self.assertEqual(len(report["hits"]), 1)

    def test_refuses_broken_or_empty_or_undecodable(self):
        magic = b"\x28\xb5\x2f\xfd"
        # 帧头保留位被置位 → 帧损坏
        dr._scan_zstd_frames_with_tail = lambda b: ([], 4) if not b.startswith(magic + b"\x18") else (_ for _ in ()).throw(ValueError("帧头保留位被置位，偏移 4"))
        _, report = dr._repair_zstd_buffer(magic + b"\x18", True, False, lambda b: b, lambda b: b)
        self.assertEqual(report["status"], "拒绝")
        self.assertEqual(report["problems"][0]["code"], "帧损坏")

        # 无完整帧
        dr._scan_zstd_frames_with_tail = lambda b: ([], 0)
        _, report = dr._repair_zstd_buffer(magic + b"\x00", True, False, lambda b: b, lambda b: b)
        self.assertEqual(report["status"], "拒绝")
        self.assertEqual(report["problems"][0]["code"], "无有效帧")

        # 某帧解压失败
        dr._scan_zstd_frames_with_tail = lambda b: ([(0, len(b))], None)
        _, report = dr._repair_zstd_buffer(
            b"abc", True, False, lambda b: (_ for _ in ()).throw(OSError("解压失败")), lambda b: b
        )
        self.assertEqual(report["status"], "拒绝")
        self.assertEqual(report["problems"][0]["code"], "帧解压失败")


@unittest.skipUnless(_real_backend(), "需要真实 zstd 后端（zstandard / pyzstd / zstd 命令）")
class TestRealZstdFrames(unittest.TestCase):
    """真实 zstd 帧路径（**不 stub** 帧扫描器与后端）。

    其余 zstd 测试为隔离业务逻辑都用 fake 后端 + 单帧 stub，导致帧扫描器与
    后端集成没有任何自动化覆盖；本类补上：真实帧切分、最小扰动重压缩、
    含内容长度帧的回扫（本工具自己的输出）、torn 尾保留、幂等。
    """

    @unittest.skipUnless(_has_zstandard(), "需要 zstandard 以复刻 DSH 帧形态")
    def test_backend_decompresses_frame_without_content_size(self):
        """回归：DSH 帧头不含内容长度，流式解压必须成功。

        ``ZstdDecompressor().decompress()`` 对这种帧会报
        "could not determine content size in frame header"。
        """
        line = '{"type":"session","version":0,"id":"s1"}'
        frame = _dsh_style_frame(line)
        self.assertEqual(frame[4] >> 6, 0, "该帧不应带内容长度")
        decompress = dr.zstd_backend()[0]
        self.assertEqual(decompress(frame).decode("utf-8"), line + "\n")

    def test_scanner_splits_frames_with_content_size(self):
        """本工具重压缩产生的帧带内容长度（1/2/4 字节 FCS），扫描器须切分正确。"""
        decompress, compress, _ = dr.zstd_backend()
        payloads = [b"a" * n for n in (10, 300, 70000)]
        buffer = b"".join(compress(p) for p in payloads)
        frames = dr._scan_zstd_frames(buffer)
        self.assertEqual(len(frames), len(payloads))
        self.assertEqual(frames[-1][1], len(buffer), "最后一帧须覆盖到文件末尾")
        self.assertEqual(b"".join(buffer[s:e] for s, e in frames), buffer, "帧区间须无缝覆盖")
        self.assertEqual(dr._decompress_all(buffer, decompress), b"".join(payloads))

    @unittest.skipUnless(_has_zstandard(), "需要 zstandard 以复刻 DSH 帧形态")
    def test_scanner_splits_dsh_style_frames(self):
        """真实 DSH 帧形态（FCS=0、非单段、带校验和）的切分与回读。"""
        lines = [
            '{"type":"session","version":0,"id":"s1"}',
            '{"type":"turn/start","seq":1}',
            '{"type":"assistant/chunk","seq":2,"data":{}}',
        ]
        buffer = b"".join(_dsh_style_frame(line) for line in lines)
        frames = dr._scan_zstd_frames(buffer)
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[-1][1], len(buffer))
        decompress = dr.zstd_backend()[0]
        decoded = b"".join(decompress(buffer[s:e]) for s, e in frames).decode("utf-8")
        self.assertEqual(decoded, "".join(line + "\n" for line in lines))

    @unittest.skipUnless(_has_zstandard(), "需要 zstandard 以复刻 DSH 帧形态")
    def test_repair_real_frames_minimal_and_idempotent(self):
        """真实帧下：只重压缩命中帧，其余帧保持原字节；再跑一次幂等。"""
        decompress, _compress, _ = dr.zstd_backend()
        header = {"type": "session", "version": 0, "id": "s1"}
        plain = {"type": "turn/start", "seq": 1}
        chunk = {
            "type": "assistant/chunk",
            "seq": 2,
            "data": {"chunk": {"type": "finish",
                               "replayState": {"kind": "pi-ai", "version": 1}}},
        }
        tail = {"type": "step/end", "seq": 3}
        rows = [header, plain, chunk, tail]
        buffer = b"".join(
            _dsh_style_frame(json.dumps(row, ensure_ascii=False)) for row in rows
        )

        new_data, report = dr.repair_session_data(buffer)
        self.assertEqual(report["status"], "已修复")
        self.assertEqual(report["frames"], 4)
        self.assertEqual(len(report["hits"]), 1)

        old_frames = dr._scan_zstd_frames(buffer)
        new_frames = dr._scan_zstd_frames(new_data)
        self.assertEqual(len(new_frames), 4)
        for index in (0, 1, 3):
            self.assertEqual(
                new_data[new_frames[index][0]:new_frames[index][1]],
                buffer[old_frames[index][0]:old_frames[index][1]],
                "未命中帧 %d 必须保持原字节" % index,
            )
        self.assertNotEqual(
            new_data[new_frames[2][0]:new_frames[2][1]],
            buffer[old_frames[2][0]:old_frames[2][1]],
            "命中帧应被重压缩",
        )

        decoded = dr._decompress_all(new_data, decompress).decode("utf-8")
        lines = decoded.split("\n")
        self.assertEqual(len(lines), 5, "4 行 + 尾部换行")
        self.assertEqual(json.loads(lines[0]), header)
        self.assertEqual(json.loads(lines[1]), plain)
        self.assertEqual(json.loads(lines[3]), tail)
        replay = json.loads(lines[2])["data"]["chunk"]["replayState"]
        self.assertEqual(set(replay.keys()), {"response"})
        self.assertEqual(replay["response"]["kind"], "pi-ai")

        # 幂等：对已修复内容再跑一次，不再变更（也顺带证明能回扫带内容长度的帧）
        again, report2 = dr.repair_session_data(new_data)
        self.assertFalse(report2["changed"])
        self.assertEqual(report2["status"], "正常")
        self.assertEqual(report2["frames"], 4)
        self.assertEqual(again, new_data, "未变更时应按原帧字节重建出同一份数据")

    @unittest.skipUnless(_has_zstandard(), "需要 zstandard 以复刻 DSH 帧形态")
    def test_detection_finds_flat_replay_state_in_real_frames_with_unicode_separator(self):
        """真实 zstd 帧 + 正文含 U+2028：检测路径必须与修复路径口径一致。"""
        header = {"type": "session", "version": 0, "id": "s1"}
        chunk = {
            "type": "assistant/chunk",
            "seq": 1,
            "data": {"chunk": {"type": "finish", "text": "前\u2028后",
                               "replayState": {"kind": "pi-ai", "version": 1}}},
        }
        buffer = (
            _dsh_style_frame(json.dumps(header, ensure_ascii=False))
            + _dsh_style_frame(json.dumps(chunk, ensure_ascii=False))
        )
        tmp = tempfile.mkdtemp(prefix="dsh_realframes_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "session.jsonl.zstd")
        with open(path, "wb") as fh:
            fh.write(buffer)

        legacy, dup = dr._scan_file_content(path, dr.zstd_backend()[0])
        self.assertTrue(legacy, "U+2028 不应让检测漏报")
        self.assertFalse(dup)
        _, report = dr.repair_session_data(buffer)
        self.assertEqual(len(report["hits"]), 1)

    @unittest.skipUnless(_has_zstandard(), "需要 zstandard 以复刻 DSH 帧形态")
    def test_torn_tail_preserved_with_real_frames(self):
        """写断的残留字节必须原样保留、不参与重写。"""
        header = {"type": "session", "version": 0, "id": "s1"}
        chunk = {
            "type": "assistant/chunk",
            "seq": 1,
            "data": {"chunk": {"type": "finish",
                               "replayState": {"kind": "pi-ai", "version": 1}}},
        }
        torn = b"\x28\xb5\x2f\xfd\x24\x00TORN"
        buffer = (
            _dsh_style_frame(json.dumps(header, ensure_ascii=False))
            + _dsh_style_frame(json.dumps(chunk, ensure_ascii=False))
            + torn
        )
        new_data, report = dr.repair_session_data(buffer)
        self.assertTrue(report["torn_tail"])
        self.assertEqual(report["frames"], 2)
        self.assertEqual(report["status"], "已修复")
        self.assertTrue(new_data.endswith(torn), "torn 尾必须原样保留")
        # 尾部起点之后的字节不参与帧划分（tail 感知扫描器）
        frames, tail = dr._scan_zstd_frames_with_tail(new_data)
        self.assertEqual(len(frames), 2)
        self.assertEqual(tail, len(new_data) - len(torn))


class TestCombinedPlan(unittest.TestCase):
    """联合修复：workspace.json 索引归属 + 会话文件内容（GUI 一次调用的路径）。"""

    def setUp(self):
        _enable_fake_zstd(compress=lambda b: b)
        self.addCleanup(_disable_fake_zstd)
        self.fx = _FakeDshHome()
        self.addCleanup(self.fx.cleanup)
        header = json.dumps({"type": "session", "version": 0, "id": "session-y", "createdAt": 1,
                             "cwd": "D:\\project\\a", "delegationDepth": 0})
        flat = json.dumps({"type": "assistant/chunk", "seq": 1, "time": 2,
                           "data": {"chunk": {"type": "finish",
                                              "replayState": {"kind": "pi-ai", "version": 1}}}})
        content = (header + "\n" + flat + "\n").encode("utf-8")
        self.fx.session("--D-project-a--", "session-y", content)
        self.fx.write_index(_make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": [],
                     "createdAt": "t1", "updatedAt": "t1"},
        }))

    def test_plan_covers_index_and_session_data(self):
        plan = dr.plan_dsh_repair(self.fx.dsh)
        self.assertEqual(len(plan.index_plan.mutations), 1)
        self.assertEqual(len(plan.data_reports), 1)
        self.assertEqual(plan.data_reports[0]["status"], "需要修复")
        self.assertEqual(plan.data_reports[0]["actions"][0]["rule"], "扁平replayState包装")
        self.assertFalse(plan.empty)
        self.assertIn("扁平replayState包装", "\n".join(plan.describe()))

    def test_apply_repairs_both_and_becomes_healthy(self):
        plan = dr.plan_dsh_repair(self.fx.dsh)
        outcome = dr.apply_dsh_repair(self.fx.dsh, plan)
        self.assertTrue(outcome["index"].ok)
        self.assertEqual(outcome["index"].applied, 1)
        self.assertTrue(outcome["index"].backup_path)
        self.assertEqual(outcome["files"][0]["status"], "已修复")
        self.assertTrue(os.path.isfile(outcome["files"][0]["backup"]))
        self.assertTrue(dr.detect_ungrouped(self.fx.dsh).healthy)
        # 二次执行：无需再修
        self.assertTrue(dr.plan_dsh_repair(self.fx.dsh).empty)

    def test_zstd_missing_falls_back_to_plaintext_and_reports_note(self):
        """无 zstd 后端时：明文 JSONL 仍可检测/修复，并给出降级说明；索引修复不受影响。"""
        dr._set_zstd_backend(None, "", compress=None)
        plan = dr.plan_dsh_repair(self.fx.dsh)
        self.assertEqual(len(plan.index_plan.mutations), 1)
        # 明文内容不依赖 zstd 后端，仍能被处理
        self.assertEqual(len(plan.data_reports), 1)
        self.assertEqual(plan.data_reports[0]["plain"], True)
        self.assertTrue(plan.zstd_note)
        self.assertIn("缺少 zstd", plan.zstd_note)


class TestCliRepairData(unittest.TestCase):
    """CLI ``repair-data`` 子命令：dry-run / --apply / --json / 参数校验。"""

    def setUp(self):
        _enable_fake_zstd(compress=lambda b: b)
        self.addCleanup(_disable_fake_zstd)
        self.tmp = tempfile.mkdtemp(prefix="dsh_cli_data_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        header = json.dumps({"type": "session", "version": 0, "id": "session-r", "createdAt": 1,
                             "cwd": "D:\\project\\r", "delegationDepth": 0})
        flat = json.dumps({"type": "assistant/chunk", "seq": 1, "time": 2,
                           "data": {"chunk": {"type": "finish",
                                              "replayState": {"kind": "pi-ai", "version": 1}}}})
        self.content = (header + "\n" + flat + "\n").encode("utf-8")
        self.path = os.path.join(self.tmp, "session.jsonl.zstd")
        with open(self.path, "wb") as fh:
            fh.write(self.content)

    def _run(self, *args):
        from io import StringIO
        import contextlib
        buf = StringIO()
        err = StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = dr.main(list(args))
        return code, buf.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        code, out, _ = self._run("repair-data", self.path)
        self.assertEqual(code, 0)
        self.assertIn("需修复 1", out)
        self.assertIn("dry-run", out)
        self.assertIn("扁平replayState包装", out)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), self.content)

    def test_apply_backs_up_and_fixes(self):
        code, out, _ = self._run("repair-data", self.path, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("已修复", out)
        backups = [f for f in os.listdir(self.tmp) if ".bak." in f]
        self.assertEqual(len(backups), 1)
        with open(self.path, "rb") as fh:
            self.assertNotEqual(fh.read(), self.content)

    def test_json_output(self):
        code, out, _ = self._run("repair-data", self.tmp, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["file_total"], 1)
        self.assertEqual(data["need_repair"], 1)
        self.assertEqual(data["results"][0]["status"], "需要修复")
        self.assertEqual(data["results"][0]["hits"][0]["fields"], ["kind", "version"])

    def test_requires_target(self):
        code, _out, err = self._run("repair-data")
        self.assertEqual(code, 2)
        self.assertIn("用法", err)

    def test_fix_prints_combined_plan(self):
        """fix 也覆盖会话文件内容（与 GUI 的修复按钮一致）。"""
        fx = _FakeDshHome()
        self.addCleanup(fx.cleanup)
        header = json.dumps({"type": "session", "version": 0, "id": "session-y", "createdAt": 1,
                             "cwd": "D:\\project\\a", "delegationDepth": 0})
        flat = json.dumps({"type": "assistant/chunk", "seq": 1, "time": 2,
                           "data": {"chunk": {"type": "finish",
                                              "replayState": {"kind": "pi-ai", "version": 1}}}})
        fx.session("--D-project-a--", "session-y", (header + "\n" + flat + "\n").encode("utf-8"))
        fx.write_index(_make_ws_index({
            "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": [],
                     "createdAt": "t1", "updatedAt": "t1"},
        }))
        code, out, _ = self._run("fix", "--dsh-home", fx.dsh)
        self.assertEqual(code, 0)
        self.assertIn("加入工作区", out)
        self.assertIn("扁平replayState包装", out)
        self.assertIn("dry-run", out)


if __name__ == "__main__":
    unittest.main()
