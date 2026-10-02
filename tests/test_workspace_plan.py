"""导入落点（目标工作区）自动判定：派生规则、模式选择、创建与展示。

派生规则的用例全部取自**本机真实数据**（回归锚点）：
- CodeBuddy ``workspaceId``：``history/`` 下的真实目录名；
- WorkBuddy ``projects/<slug>``：``workbuddy.db`` 的 sessions.cwd 与真实目录名（11/11）；
- DSH ``sessions/<工作区编码>``：``workspace.json`` 的 path 与真实目录名（6/6）。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import session_migration as sm  # noqa: E402
from ai_env_clone import workspace_plan as wp  # noqa: E402


class TestDerivationRules(unittest.TestCase):
    """派生规则必须与各产品自身的推导一致，否则导入的会话会「写了但看不到」。"""

    def test_codebuddy_workspace_id_matches_real_dir(self):
        """本机 ``history/059d5d31ffef54c1aab84e8c6edc485b`` 对应本仓库所在项目。"""
        self.assertEqual(
            wp.codebuddy_workspace_id(r"D:\project\ai-env-clone"),
            "059d5d31ffef54c1aab84e8c6edc485b",
        )

    def test_codebuddy_workspace_id_normalizes(self):
        """大小写与分隔符不敏感（统一转小写 + 反斜杠）。"""
        a = wp.codebuddy_workspace_id(r"D:\Project\AI-Env-Clone")
        b = wp.codebuddy_workspace_id("d:/project/ai-env-clone")
        self.assertEqual(a, b)
        self.assertEqual(wp.codebuddy_workspace_id(""), "")

    def test_workbuddy_project_slug_samples(self):
        """对齐 ``~/.workbuddy/projects/`` 的目录名规则（含中文 / 空格 / 全角符号）。"""
        cases = {
            r"D:\project\ai-env-clone": "d-project-ai-env-clone",
            r"D:\project\SampleApp": "d-project-SampleApp",
            r"C:\Users\user\WorkBuddy\2026-08-09-16-36-58":
                "c-Users-user-WorkBuddy-2026-08-09-16-36-58",
            r"D:\Desktop\示例资料": "d-Desktop-示例资料",
            r"D:\Desktop\学习资料\【阶段一｜单元60分-8.10日任务】\▶︎ 1. 【板块一｜单词任务】":
                "d-Desktop-学习资料-【阶段一｜单元60分-8.10日任务】-▶︎ 1. 【板块一｜单词任务】",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(sm.workbuddy_project_slug(path), expected)

    def test_workbuddy_slug_not_over_sanitized(self):
        """回归：旧实现把非 ``[\\w-]`` 一律压成 ``-``，中文工作区会变成一串 ``-``。"""
        slug = sm.workbuddy_project_slug(r"D:\Desktop\示例资料")
        self.assertIn("示例资料", slug)
        self.assertNotIn("-----", slug)

    def test_dsh_workspace_dirname_samples(self):
        """对齐 ``~/.dsh/sessions/`` 的目录名规则（非 ASCII 编码为 ``~码点~``）。"""
        cases = {
            r"D:\project\ai-env-clone": "--D-project-ai-env-clone--",
            r"D:\project\SampleApp_v2": "--D-project-SampleApp_v2--",
            (r"D:\project\demo\子项目-分组\src-模块\demo_common"):
                "--D-project-demo-~5B50~9879~76EE-~5206~7EC4-src-~6A21~5757-demo_common--",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(sm._dsh_workspace_dirname(path), expected)

    def test_scope_from_path(self):
        self.assertEqual(wp.scope_from_path(r"D:\project\ai-env-clone"), "ai-env-clone")
        # 中文保留（与 Reasonix 自身 write_reasonix 的 \w 口径、WorkBuddy 的 slug 口径一致）
        self.assertEqual(wp.scope_from_path(r"D:\Desktop\示例资料"), "示例资料")
        self.assertEqual(wp.scope_from_path(""), "")


class TestDefaults(unittest.TestCase):
    """「无工作区会话」的默认落点：每种目标工具各有其原生位置。"""

    def test_workbuddy_default_is_playground_stamp(self):
        p = wp.default_workspace("workbuddy", "2026-09-27-14-00-00")
        self.assertTrue(p.endswith(os.path.join("WorkBuddy", "2026-09-27-14-00-00")), p)

    def test_dsh_default_is_home(self):
        self.assertEqual(wp.default_workspace("dsh"), os.path.expanduser("~"))

    def test_reasonix_default_is_global_scope(self):
        self.assertEqual(wp.default_workspace("reasonix"), "global-workspace")

    def test_codebuddy_default_is_stable_hex(self):
        v = wp.default_workspace("codebuddy")
        self.assertEqual(len(v), 32)
        # 稳定：同一份「无工作区」始终落到同一处，而不是每次随机
        self.assertEqual(v, wp.codebuddy_workspace_id(wp.CODEBUDDY_FALLBACK_NAME))


class TestPlanFor(unittest.TestCase):
    """三种模式的判定：会话自带 > 工具默认 > 手动覆盖。"""

    def test_session_mode_uses_source_workspace(self):
        for tool, item, expect in (
            ("workbuddy", {"cwd": r"D:\project\X"}, r"D:\project\X"),
            ("dsh", {"cwd": r"D:\project\X"}, r"D:\project\X"),
            ("reasonix", {"scope": "myproj"}, "myproj"),
            ("codebuddy", {"workspace_id": "abc123"}, "abc123"),
        ):
            with self.subTest(tool=tool):
                p = wp.plan_for(tool, item)
                self.assertEqual(p.mode, wp.MODE_SESSION)
                self.assertEqual(p.value, expect)

    def test_reasonix_falls_back_to_scope_from_cwd(self):
        p = wp.plan_for("reasonix", {"cwd": r"D:\project\X"})
        self.assertEqual(p.value, "X")

    def test_codebuddy_falls_back_to_id_from_cwd(self):
        item = {"cwd": r"D:\project\X"}
        p = wp.plan_for("codebuddy", item)
        self.assertEqual(p.value, wp.codebuddy_workspace_id(r"D:\project\X"))

    def test_default_mode_when_no_workspace(self):
        p = wp.plan_for("dsh", {"cwd": ""})
        self.assertEqual(p.mode, wp.MODE_DEFAULT)
        self.assertEqual(p.value, os.path.expanduser("~"))

    def test_manual_mode_overrides_session_workspace(self):
        """手动指定必须**覆盖**源会话自带的工作区（这是要警示用户的行为）。"""
        item = {"cwd": r"D:\project\X"}
        p = wp.plan_for("workbuddy", item, manual=r"E:\imported")
        self.assertEqual(p.mode, wp.MODE_MANUAL)
        self.assertEqual(p.value, r"E:\imported")
        self.assertNotEqual(p.value, item["cwd"])

    def test_blank_manual_means_auto(self):
        p = wp.plan_for("workbuddy", {"cwd": r"D:\project\X"}, manual="   ")
        self.assertEqual(p.mode, wp.MODE_SESSION)

    def test_exists_detection_for_id_kinds(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "wid-1"))
            p = wp.plan_for("codebuddy", {"workspace_id": "wid-1"}, root=d)
            self.assertTrue(p.exists)
            self.assertEqual(p.path, os.path.join(d, "wid-1"))
            p2 = wp.plan_for("codebuddy", {"workspace_id": "wid-2"}, root=d)
            self.assertFalse(p2.exists)


class TestEnsureAndDescribe(unittest.TestCase):
    def test_ensure_creates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "new", "ws")
            plan = wp.plan_for("workbuddy", None, manual=target)
            created, path = wp.ensure(plan)
            self.assertTrue(created)
            self.assertEqual(path, target)
            self.assertTrue(os.path.isdir(target))
            # 第二次：已存在，不再新建
            created2, _ = wp.ensure(plan)
            self.assertFalse(created2)

    def test_ensure_reasonix_creates_sessions_subdir(self):
        with tempfile.TemporaryDirectory() as d:
            plan = wp.plan_for("reasonix", None, manual="proj", root=d)
            created, path = wp.ensure(plan)
            self.assertTrue(created)
            self.assertTrue(os.path.isdir(os.path.join(path, "sessions")))

    def test_ensure_survives_uncreatable_path(self):
        """建不出来也不能抛异常（会话文件本身仍会照常写出）。"""
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "blocker")   # 先建一个**文件**占位
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            bad = os.path.join(blocker, "child")   # 以文件为父目录 -> 必然建不出来
            plan = wp.WorkspacePlan(tool="dsh", mode=wp.MODE_MANUAL,
                                    value=bad, path=bad, exists=False, origin="")
            created, path = wp.ensure(plan)
            self.assertFalse(created)
            self.assertEqual(path, bad)

    def test_describe_missing_dedupes_and_skips_existing(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "here"))
            plans = [
                wp.plan_for("codebuddy", {"workspace_id": "here"}, root=d),
                wp.plan_for("codebuddy", {"workspace_id": "gone"}, root=d),
                wp.plan_for("codebuddy", {"workspace_id": "gone"}, root=d),
            ]
            missing = wp.describe_missing(plans)
            self.assertEqual([p.value for p in missing], ["gone"])

    def test_describe_labels_modes(self):
        p = wp.plan_for("reasonix", {"scope": "proj"}, root="")
        self.assertIn("自动", wp.describe(p))
        self.assertIn("proj", wp.describe(p))

    def test_kind_labels(self):
        self.assertEqual(wp.workspace_kind("workbuddy"), wp.KIND_PATH)
        self.assertEqual(wp.workspace_kind("dsh"), wp.KIND_PATH)
        self.assertEqual(wp.workspace_kind("codebuddy"), wp.KIND_ID)
        self.assertEqual(wp.workspace_kind("reasonix"), wp.KIND_SCOPE)


class TestCodebuddyWorkspacePathIndex(unittest.TestCase):
    """``workspaceId -> 项目路径`` 反查（哈希不可逆，只能从 IDE 记录反查）。

    这是「CodeBuddy 会话导入 WorkBuddy 却落到默认 playground」的正解：只有把 id
    还原成路径，``plan_for`` 才会走 MODE_SESSION，落到会话原本的工程工作区。
    """

    def test_index_maps_real_workspace_id(self):
        """本机真实锚点：``history/059d5d31ffef…`` 对应本仓库所在项目。"""
        idx = wp.codebuddy_workspace_path_index([r"D:\project\ai-env-clone"])
        self.assertEqual(idx.get("059d5d31ffef54c1aab84e8c6edc485b"),
                         r"D:\project\ai-env-clone")

    def test_index_tolerates_empty_and_junk(self):
        """候选为空 / 探测失败时必须返回空表，不能抛异常（导入流程不能被拖垮）。"""
        self.assertEqual(wp.codebuddy_workspace_path_index([]), {})
        self.assertEqual(wp.codebuddy_workspace_path_index(["", None]), {})

    def test_index_keeps_first_on_collision(self):
        """同一工作区大小写不同路径 -> 同一个 id，保留首个（大小写不敏感）。"""
        idx = wp.codebuddy_workspace_path_index(
            [r"D:\Project\X", r"d:\project\x"])
        self.assertEqual(len(idx), 1)

    def test_resolved_id_becomes_session_workspace_for_workbuddy(self):
        """端到端语义：反查成功 -> 目标是 WorkBuddy 时走「沿用会话自带工作区」。"""
        idx = wp.codebuddy_workspace_path_index([r"D:\project\ai-env-clone"])
        wid = "059d5d31ffef54c1aab84e8c6edc485b"
        item = {"cwd": idx.get(wid, ""), "workspace_id": wid}
        p = wp.plan_for("workbuddy", item)
        self.assertEqual(p.mode, wp.MODE_SESSION)
        self.assertEqual(p.value, r"D:\project\ai-env-clone")
        self.assertNotIn("WorkBuddy-", p.value)     # 不能是 playground 时间戳目录

    def test_unresolved_id_notes_reason_and_shows_it(self):
        """反查不到 -> 落默认落点，但必须交代成因（不能只甩一个路径）。"""
        item = {"cwd": "", "workspace_id": "deadbeef" * 4}
        p = wp.plan_for("workbuddy", item)
        self.assertEqual(p.mode, wp.MODE_DEFAULT)
        self.assertIn("不可逆", p.note)
        self.assertIn("deadbeef", p.note)
        self.assertIn(p.note, wp.describe(p))       # 预览文案必须带上成因

    def test_no_note_for_genuinely_workspace_less_session(self):
        """源会话真没工作区（无 workspace_id）-> 不加成因说明，措辞保持原样。"""
        p = wp.plan_for("workbuddy", {"cwd": "", "workspace_id": ""})
        self.assertEqual(p.mode, wp.MODE_DEFAULT)
        self.assertEqual(p.note, "")
        self.assertNotIn("不可逆", wp.describe(p))


class TestCodebuddyContentRecovery(unittest.TestCase):
    """跨机场景：**本机没有该工程的 IDE 记录**时，改从会话正文里还原路径。

    这正是「另一台机器备份 → 本机还原 → 导入」的路径：IDE 的「已打开文件夹」记录
    不在备份范围内（它只记本机打开过的工程），但会话正文/检查点里出现过的绝对路径
    **随包过来了**。由于 ``workspaceId`` 是 ``md5(路径)``，候选可以**当场校验**：
    哈希对得上才是结论。
    """

    PROJ = r"D:\project\Demo" if os.name == "nt" else "/tmp/project/Demo"

    def setUp(self):
        wp._MINE_CACHE.clear()
        self.addCleanup(wp._MINE_CACHE.clear)

    def _session(self) -> str:
        """造一个正文里含工程路径的会话目录。"""
        d = tempfile.mkdtemp(prefix="cb_rec_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        mdir = os.path.join(d, "messages")
        os.makedirs(mdir)
        with open(os.path.join(mdir, "m1.json"), "w", encoding="utf-8") as f:
            f.write('{"files": ["%s", "%s", "%s"]}' % (
                (self.PROJ + r"\src\a.py").replace("\\", "\\\\"),
                (self.PROJ + r"\src\b.py").replace("\\", "\\\\"),
                (self.PROJ + r"\README.md").replace("\\", "\\\\")))
        return d

    def test_recovers_path_without_ide_records(self):
        """没有 IDE 记录也能还原：正文候选 + md5 校验。"""
        sdir = self._session()
        wid = wp.codebuddy_workspace_id(self.PROJ)
        with mock.patch("ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths",
                        return_value=[]):
            path, how = wp.codebuddy_recover_workspace_path(wid, sdir)
        self.assertEqual(path, self.PROJ)
        self.assertIn("哈希校验", how)

    def test_recovers_end_to_end_to_project_workspace(self):
        """端到端：还原成功后落点是**工程工作区**，不是 playground 时间戳目录。"""
        sdir = self._session()
        item = {"cwd": "", "workspace_id": wp.codebuddy_workspace_id(self.PROJ),
                "path": sdir}
        with mock.patch("ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths",
                        return_value=[]):
            p = wp.plan_for("workbuddy", item)
        self.assertEqual(p.mode, wp.MODE_SESSION)
        self.assertEqual(p.value, self.PROJ)
        self.assertIn("由源会话", p.note)
        self.assertIn("· 由源会话", wp.describe(p))     # 正常结果用 ·，不用 ⚠

    def test_hash_mismatch_never_adopts_guess(self):
        """哈希对不上 -> 绝不采用候选值：仍落默认落点，但把候选交给人。"""
        sdir = self._session()
        item = {"cwd": "", "workspace_id": "0123456789abcdef0123456789abcdef",
                "path": sdir}
        p = wp.plan_for("workbuddy", item)
        self.assertEqual(p.mode, wp.MODE_DEFAULT)      # 不是 SESSION
        self.assertNotEqual(p.value, self.PROJ)
        self.assertIn(self.PROJ, p.hints)              # 候选照样给到手
        self.assertIn(self.PROJ, p.note)
        self.assertIn("不可逆", p.note)

    def test_codebuddy_target_keeps_workspace_id(self):
        """目标是 CodeBuddy 时工作区**就是 id**，不走「还原成路径」这条路。"""
        sdir = self._session()
        wid = wp.codebuddy_workspace_id(self.PROJ)
        item = {"cwd": "", "workspace_id": wid, "path": sdir}
        with mock.patch("ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths",
                        return_value=[]):
            p = wp.plan_for("codebuddy", item)
        self.assertEqual(p.mode, wp.MODE_SESSION)
        self.assertEqual(p.value, wid)

    def test_mining_is_cached(self):
        """挖掘要读文件，同一会话只挖一次（预览会在每次选择变化时重算）。"""
        sdir = self._session()
        wp.codebuddy_session_path_hints(sdir)
        self.assertTrue(wp._MINE_CACHE)
        before = len(wp._MINE_CACHE)
        wp.codebuddy_session_path_hints(sdir)
        self.assertEqual(len(wp._MINE_CACHE), before)


class TestWorkBuddyLanding(unittest.TestCase):
    """端到端：导入到 WorkBuddy 后，会话目录名必须与产品自身规则一致。"""

    def test_write_workbuddy_uses_product_slug(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = r"D:\Desktop\示例资料"
            sid = sm.SessionWriter.write_workbuddy(
                sm.Session(source_tool="dsh", title="t", messages=[
                    sm.SessionMessage(role="user", content="hi"),
                ]), d, cwd=cwd)
            slug_dir = os.path.join(d, "projects", "d-Desktop-示例资料")
            self.assertTrue(os.path.isdir(slug_dir), os.listdir(os.path.join(d, "projects")))
            self.assertTrue(os.path.isfile(os.path.join(slug_dir, sid + ".jsonl")))

    def test_write_dsh_uses_product_workspace_dir(self):
        try:
            import zstandard  # type: ignore
        except ImportError:  # pragma: no cover - 后端缺失时跳过
            self.skipTest("未安装 zstandard，跳过 DSH 用例")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "storages"))
            with open(os.path.join(d, "storages", "workspace.json"), "w",
                      encoding="utf-8") as f:
                f.write('{"unit": {"name": "workspace", "version": 3}, "tables": {}}')
            cwd = r"D:\project\demo\子项目\demo_common"
            sid = sm.SessionWriter.write_dsh(
                sm.Session(source_tool="workbuddy", title="t", messages=[
                    sm.SessionMessage(role="user", content="hi"),
                ]), d, cwd=cwd, warn=lambda _m: None)
            wsdir = sm._dsh_workspace_dirname(cwd)
            self.assertTrue(os.path.isdir(os.path.join(d, "sessions", wsdir, sid)))


if __name__ == "__main__":
    unittest.main()
