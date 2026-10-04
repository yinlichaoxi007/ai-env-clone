"""「数据导入」区：随工具切换渲染能力矩阵、按钮可用性、非导入目标的提示。

headless 说明：模块期间打开 ``gui.HEADLESS``，使自定义弹窗退化为被 mock 的
``messagebox``，避免 ``grab_set()+wait_window()`` 在无头环境下永久阻塞。
"""

import os
import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import __main__ as gui  # noqa: E402
from ai_env_clone import backup_scan  # noqa: E402
from ai_env_clone import import_matrix  # noqa: E402
from ai_env_clone import workspace_plan  # noqa: E402
from ai_env_clone.__main__ import QoderBackupApp  # noqa: E402

_ORIG_HEADLESS = None


def setUpModule() -> None:
    global _ORIG_HEADLESS
    _ORIG_HEADLESS = gui.HEADLESS
    gui.HEADLESS = True


def tearDownModule() -> None:
    if _ORIG_HEADLESS is not None:
        gui.HEADLESS = _ORIG_HEADLESS


def _make_app(tool: str):
    """构造 app；_load_last_tool 决定初始适配器，且不写真实偏好缓存。"""
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


class TestImportArea(unittest.TestCase):
    def test_renders_for_every_supported_tool(self):
        """每个已支持工具都能渲染导入区，不抛异常（回归：新增适配器后布局仍自适应）。"""
        from ai_env_clone.adapters import list_adapters

        for tool in list_adapters():
            with self.subTest(tool=tool):
                root, app = _make_app(tool)
                try:
                    app._refresh_import_matrix()
                    text = app.imp_text.get("1.0", tk.END)
                    # 标题标出当前目标工具；摘要给出能力概况
                    self.assertIn(app.adapter.display_name, app.imp_frame.cget("text"))
                    self.assertTrue(app.imp_summary.cget("text").strip())
                    # 有来源行时正文不应为空
                    if import_matrix.describe(tool)["rows"]:
                        self.assertTrue(text.strip(), "%s 的导入区应有内容" % tool)
                finally:
                    root.destroy()

    def test_importable_targets_list_sources(self):
        """可导入的目标工具，正文逐条列出「来源软件（版本）— 状态」。"""
        for tool in import_matrix.session_import_targets():
            with self.subTest(tool=tool):
                root, app = _make_app(tool)
                try:
                    info = import_matrix.describe(tool)
                    text = app.imp_text.get("1.0", tk.END)
                    for row in info["rows"]:
                        self.assertIn(row["display"], text)
                        self.assertIn(row["versions"], text)
                        self.assertIn(row["status_label"], text)
                    self.assertEqual(str(app.imp_btn.cget("state")), "normal")
                finally:
                    root.destroy()

    def test_encrypted_targets_disable_button(self):
        """主库加密 / 待支持的工具：按钮置灰，且给出「仅备份还原」类说明。"""
        encrypted = [t for t in import_matrix.known_targets()
                     if not import_matrix.describe(t)["has_importable"]]
        self.assertTrue(encrypted, "应至少存在一个不支持导入的目标工具")
        for tool in encrypted:
            with self.subTest(tool=tool):
                root, app = _make_app(tool)
                try:
                    self.assertEqual(str(app.imp_btn.cget("state")), "disabled")
                finally:
                    root.destroy()

    def test_on_migrate_reports_unsupported(self):
        """对不支持导入的工具点「导入会话…」→ 弹提示而非打开对话框。"""
        tool = next(t for t in import_matrix.known_targets()
                    if not import_matrix.describe(t)["has_importable"])
        root, app = _make_app(tool)
        try:
            with mock.patch.object(gui, "MigrateDialog") as dlg, \
                 mock.patch.object(gui.messagebox, "showinfo") as info:
                app.on_migrate()
            dlg.assert_not_called()
            info.assert_called_once()
        finally:
            root.destroy()

    def test_on_migrate_opens_dialog_for_supported(self):
        """对支持导入的工具点「导入会话…」→ 打开 MigrateDialog。"""
        tool = import_matrix.session_import_targets()[0]
        root, app = _make_app(tool)
        try:
            with mock.patch.object(gui, "MigrateDialog") as dlg:
                app.on_migrate()
            dlg.assert_called_once()
        finally:
            root.destroy()

    def test_switch_tool_refreshes_area(self):
        """切换工具后导入区随新工具刷新（标题与摘要一致）。"""
        first = import_matrix.session_import_targets()[0]
        root, app = _make_app(first)
        try:
            target = "codebuddy"
            disp = next(d for d, n in app._tool_display.items() if n == target)
            with mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
                app.tool_var.set(disp)
                app._on_switch_tool()
            self.assertEqual(app.adapter.name, target)
            self.assertIn(app.adapter.display_name, app.imp_frame.cget("text"))
            info = import_matrix.describe(target)
            self.assertEqual(
                str(app.imp_btn.cget("state")),
                "normal" if info["has_importable"] else "disabled",
            )
        finally:
            root.destroy()

    def _make_dialog(self, tool: str, items=None):
        """构造 MigrateDialog，并把来源扫描打桩以保持测试快速、与真实数据无关。"""
        root, app = _make_app(tool)
        with mock.patch("ai_env_clone.session_migration.list_source_sessions",
                        return_value=list(items or [])):
            dlg = gui.MigrateDialog(app)
        return root, dlg

    def test_import_warning_summary_shown(self):
        """导入成功后，warn 汇总为**执行后**一次性弹窗（不逐条打断）。"""
        tool = import_matrix.session_import_targets()[0]
        root, dlg = self._make_dialog(tool)
        try:
            dlg.warns = ["落点不是当前登录用户数据根，会话可能读不到",
                         "未指定 workspaceId，已生成随机值"]
            dlg._result = ("ok", "session-abc")
            with mock.patch.object(gui.messagebox, "showinfo") as info, \
                 mock.patch.object(gui.messagebox, "showwarning") as warn, \
                 mock.patch.object(gui.messagebox, "showerror") as err:
                dlg._poll_import()
            info.assert_called_once()
            err.assert_not_called()
            warn.assert_called_once()
            body = warn.call_args[0][1]
            self.assertIn("落点不是当前登录用户数据根", body)
            self.assertIn("未指定 workspaceId", body)
        finally:
            root.destroy()

    def test_import_no_warning_no_extra_dialog(self):
        """无警告时只弹完成提示，不额外弹警告框。"""
        tool = import_matrix.session_import_targets()[0]
        root, dlg = self._make_dialog(tool)
        try:
            dlg.warns = []
            dlg._result = ("ok", "session-abc")
            with mock.patch.object(gui.messagebox, "showinfo") as info, \
                 mock.patch.object(gui.messagebox, "showwarning") as warn:
                dlg._poll_import()
            info.assert_called_once()
            warn.assert_not_called()
        finally:
            root.destroy()

    def test_import_failure_includes_warnings(self):
        """失败时把已收集的提示附在错误详情里，便于定位。"""
        tool = import_matrix.session_import_targets()[0]
        root, dlg = self._make_dialog(tool)
        try:
            dlg.warns = ["落点提示 X"]
            dlg._result = ("err", "RuntimeError: 未检测到可用的 zstd 后端")
            with mock.patch.object(gui.messagebox, "showerror") as err:
                dlg._poll_import()
            err.assert_called_once()
            self.assertIn("未检测到可用的 zstd 后端", err.call_args[0][1])
            self.assertIn("落点提示 X", err.call_args[0][1])
        finally:
            root.destroy()

    def test_partial_import_reports_both_sides(self):
        """批量导入部分失败：走「部分完成」提示，且不弹「全部失败」。"""
        tool = import_matrix.session_import_targets()[0]
        root, dlg = self._make_dialog(tool)
        try:
            dlg.warns = []
            dlg._result = ("partial", ([("会话 A", "sid-1")],
                                       ["会话 B：RuntimeError: 未检测到可用的 zstd 后端"]))
            with mock.patch.object(gui.messagebox, "showwarning") as warn, \
                 mock.patch.object(gui.messagebox, "showerror") as err:
                dlg._poll_import()
            warn.assert_called_once()
            err.assert_not_called()
            body = warn.call_args[0][1]
            self.assertIn("会话 A", body)
            self.assertIn("会话 B", body)
        finally:
            root.destroy()


def _item(title="会话 A", path=r"D:\src\a.jsonl", cwd="", **extra):
    it = {"id": path, "title": title, "path": path, "detail": "", "cwd": cwd,
          "workspace_id": "", "scope": ""}
    it.update(extra)
    return it


def _write_session_message(session_dir: str, project: str) -> None:
    """造一个「会话正文里出现过工程路径」的 CodeBuddy 会话目录。

    形态照抄真实数据：``<会话目录>/messages/<id>.json``，路径以 JSON 转义
    （反斜杠成对）出现——挖掘侧靠 :func:`unescape_path_literal` 还原。
    """
    import json as _json
    mdir = os.path.join(session_dir, "messages")
    os.makedirs(mdir, exist_ok=True)
    refs = [project + r"\src\a.py", project + r"\src\b.py", project + r"\README.md"]
    payload = {"id": "m1", "role": "tool",
               "message": _json.dumps({"files": refs}, ensure_ascii=False)}
    with open(os.path.join(mdir, "m1.json"), "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False)


class TestWorkspaceAutoResolution(unittest.TestCase):
    """目标工作区默认**自动判定**：会话自带 > 工具默认；手动指定才覆盖全部。"""

    def _dialog(self, tool, items):
        root, app = _make_app(tool)
        with mock.patch("ai_env_clone.session_migration.list_source_sessions",
                        return_value=list(items)), \
             mock.patch("ai_env_clone.session_migration.list_target_workspaces",
                        return_value=[]):
            dlg = gui.MigrateDialog(app)
        return root, dlg

    def _jobs(self, dlg):
        return dlg._build_jobs(dlg._selected_items(), dlg._selected_plans())

    def _absent_workspace(self) -> str:
        """返回一个**确实不存在**的工作区路径。

        ⚠️ 不能用 ``D:\\__no_such_ws__`` 之类的硬编码路径：本机运行环境注入了
        WorkBuddy CLI 的 brokered-fs shim（``sitecustomize.py``），对部分根目录下的
        任意路径 ``os.path.isdir`` 会返回 True。临时目录下的判断是准确的。
        """
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "X")
        assert not os.path.isdir(path)
        return path

    def test_uses_source_workspace_by_default(self):
        """源会话有工作区 -> 自动沿用（无需用户填任何东西）。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            self.assertFalse(dlg.manual_var.get())
            jobs = self._jobs(dlg)
            self.assertEqual(jobs[0][1]["workspace_id"], r"D:\project\X")
            self.assertIn("X", dlg.ws_preview_var.get())
        finally:
            root.destroy()

    def test_falls_back_to_tool_default(self):
        """源会话无工作区 -> 用该工具的「无工作区」默认落点。"""
        root, dlg = self._dialog("dsh", [_item(cwd="")])
        try:
            jobs = self._jobs(dlg)
            self.assertEqual(jobs[0][1]["workspace_id"], os.path.expanduser("~"))
        finally:
            root.destroy()

    def test_each_session_keeps_its_own_workspace(self):
        """多选批量导入：每条会话按各自自带的工作区落点，互不影响。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl", cwd=r"D:\project\Y")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()   # 程序化改选不会触发 <<ListboxSelect>>，手动刷新一次
            self.assertEqual(len(dlg._selected_items()), 2)
            jobs = self._jobs(dlg)
            self.assertEqual([j[1]["workspace_id"] for j in jobs],
                             [r"D:\project\X", r"D:\project\Y"])
            self.assertIn("2 条会话", dlg.ws_preview_var.get())
        finally:
            root.destroy()

    def test_manual_overrides_every_session_and_warns(self):
        """手动指定 -> **全部**会话都落到同一工作区（含自带工作区的），并常驻警示。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl", cwd=r"D:\project\Y")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            self.assertEqual(dlg.manual_warn.winfo_manager(), "")
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            dlg.manual_var.set(True)
            dlg.manual_var_value.set(r"E:\imported")
            dlg._on_manual_toggle()
            # 警示常驻显示（这是「覆盖全部会话工作区」的提醒）
            self.assertEqual(dlg.manual_warn.winfo_manager(), "pack")
            self.assertIn("全部", dlg.manual_warn.cget("text"))
            jobs = self._jobs(dlg)
            self.assertEqual([j[1]["workspace_id"] for j in jobs],
                             [r"E:\imported", r"E:\imported"])
            # 关掉手动 -> 警示收起，回到自动
            dlg.manual_var.set(False)
            dlg._on_manual_toggle()
            self.assertEqual(dlg.manual_warn.winfo_manager(), "")
            self.assertEqual([j[1]["workspace_id"] for j in self._jobs(dlg)],
                             [r"D:\project\X", r"D:\project\Y"])
        finally:
            root.destroy()

    def test_manual_entry_row_hidden_until_checked(self):
        """未勾选「手动指定」时手动输入行必须**收起**（置灰保留旧路径会误导人）。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            self.assertEqual(dlg.manual_entry_row.winfo_manager(), "")
            dlg.manual_var.set(True)
            dlg.manual_var_value.set(r"E:\imported")
            dlg._on_manual_toggle()
            self.assertEqual(dlg.manual_entry_row.winfo_manager(), "pack")
            self.assertIn(r"E:\imported", dlg.ws_preview_var.get())
            # 取消勾选：输入行收起，预览不再残留手动路径
            dlg.manual_var.set(False)
            dlg._on_manual_toggle()
            self.assertEqual(dlg.manual_entry_row.winfo_manager(), "")
            self.assertNotIn("E:\\imported", dlg.ws_preview_var.get())
            self.assertNotIn("手动指定", dlg.ws_preview_var.get())
            self.assertIn(r"D:\project\X", dlg.ws_preview_var.get())
        finally:
            root.destroy()

    def test_multi_select_distinct_hides_single_path(self):
        """多选且落点不同 -> 只说明「存在多个」，不挑一条显示（否则会被当成统一落点）。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl", cwd=r"D:\project\Y")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn("2 条会话", text)
            self.assertIn("2 个不同的工作区", text)
            self.assertNotIn(r"D:\project\X", text)
            self.assertNotIn(r"D:\project\Y", text)
        finally:
            root.destroy()

    def test_multi_select_same_workspace_shows_it(self):
        """多选但落点一致 -> 显示那一个实际落点。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl", cwd=r"D:\project\X")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn(r"D:\project\X", text)
            self.assertIn("工作区相同", text)
        finally:
            root.destroy()

    # ---- 多选文案必须交代「落点来源」（否则会被读成「手动填的那个」）----
    #
    # 根因：勾选「手动指定」时输入框的**预填值**就是自动判定值
    # （``_on_manual_toggle`` -> ``plan_for(None, "")``），两者字符串完全相同；
    # 若自动判定走的是 MODE_DEFAULT（工具默认落点），多选预览只甩一个路径，
    # 用户就会认成「我手动填的那个路径没被清掉」。

    def test_manual_prefill_equals_default_landing(self):
        """不变量：无工作区会话的「手动预填值」== 工具默认落点（误读根源，锁死它）。"""
        from ai_env_clone import workspace_plan as wp

        root, dlg = self._dialog("workbuddy", [_item(cwd="")])
        try:
            auto = dlg._plan_for(None)              # 未勾选手动时的自动判定
            self.assertEqual(auto.mode, wp.MODE_DEFAULT)
            self.assertEqual(auto.value, dlg._default_ws)
            self.assertTrue(dlg._default_ws.startswith(
                os.path.join(os.path.expanduser("~"), "WorkBuddy")))
        finally:
            root.destroy()

    def test_multi_select_default_landing_says_not_manual(self):
        """多选且都没记录工作区 -> 必须写明「统一落到工具默认落点」且标注非手动指定。"""
        items = [_item("A", r"D:\src\a.jsonl"), _item("B", r"D:\src\b.jsonl")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn("2 条会话", text)
            self.assertIn("都没有记录工作区", text)
            self.assertIn(dlg._default_ws, text)
            self.assertIn("非手动指定", text)
            # 关键：不能出现手动态标记，否则等于没改
            self.assertNotIn("【手动指定", text)
        finally:
            root.destroy()

    def test_multi_select_same_session_workspace_names_origin(self):
        """多选且自带工作区相同 -> 显示该路径，并说明它来自会话自带（不是默认落点）。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl", cwd=r"D:\project\X")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn(r"D:\project\X", text)
            self.assertIn("会话自带的工作区相同", text)
            self.assertNotIn("非手动指定", text)   # 有人提交来的落点，无需反证
        finally:
            root.destroy()

    def test_multi_select_mixed_reports_default_count(self):
        """多选混合（部分自带、部分无）-> 说明有几条会落到默认落点。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X"),
                 _item("B", r"D:\src\b.jsonl")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn("2 个不同的工作区", text)
            self.assertIn("1 条未记录工作区", text)
            self.assertIn("默认落点", text)
        finally:
            root.destroy()

    def test_multi_select_unresolved_codebuddy_id_explains_why(self):
        """源只有 CodeBuddy 的 workspaceId（哈希、不可逆）-> 必须说明「不可还原为路径」。

        回归：这类会话在列表里明明显示「工作区 059d5d31ffef」，落点却是工具默认目录，
        若文案还写「都没有记录工作区」，就等于把「有工作区但还原不出路径」说成
        「本来就没有工作区」，用户只会更困惑。

        注意 wid 必须挑一个**本机注定还原不出**的：真实样本 ``059d5d31…`` 在本机
        有 IDE「已打开工程」记录，会被正常还原成路径（那是另一条用例的场景），
        拿它来断言「还原不出」会变成随机器而异的脆弱测试。
        """
        wid = "0123456789abcdef0123456789abcdef"
        items = [_item("A", r"D:\src\a", workspace_id=wid),
                 _item("B", r"D:\src\b", workspace_id=wid)]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            text = dlg.ws_preview_var.get()
            self.assertIn("可还原为路径", text)
            self.assertIn("不可逆", text)
            self.assertIn("01234567", text)
            self.assertIn(dlg._default_ws, text)
            self.assertNotIn("【手动指定", text)
        finally:
            root.destroy()

    def test_resolved_codebuddy_id_lands_on_project_path(self):
        """能还原的 CodeBuddy id -> 直接落到还原出的工程路径，并写明「由源会话还原」。

        这条覆盖「另一台机器备份 → 本机还原」：IDE 记录可以没有，路径由会话正文
        挖出后用 md5 校验确认，因此落点仍是原本的工程，而不是工具默认目录。
        """
        tmp = tempfile.mkdtemp(prefix="cb_session_")
        self.addCleanup(shutil.rmtree, tmp, True)
        proj = r"D:\project\Demo" if os.name == "nt" else "/tmp/project/Demo"
        wid = workspace_plan.codebuddy_workspace_id(proj)
        _write_session_message(tmp, proj)
        items = [_item("A", tmp, workspace_id=wid)]
        with mock.patch("ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths",
                        return_value=[]), \
             mock.patch("ai_env_clone.workspace_plan._MINE_CACHE", {}):
            root, dlg = self._dialog("workbuddy", items)
            try:
                dlg._on_pick()
                plan = dlg._selected_plans()[0]
                self.assertEqual(plan.mode, workspace_plan.MODE_SESSION)
                self.assertEqual(plan.value, proj)
                self.assertTrue(plan.note.startswith("由源会话"))
            finally:
                root.destroy()

    def test_manual_dropdown_offers_content_candidates(self):
        """还原不出时，手动输入下拉里要列出「源会话正文里出现过的候选」，供一键采用。

        含义：不自动猜（实测启发式会推错），但也**不能只说一句「还原不出」** ——
        得把可用的路径线索交到用户手里。
        """
        tmp = tempfile.mkdtemp(prefix="cb_hint_")
        self.addCleanup(shutil.rmtree, tmp, True)
        proj = r"D:\work\SomeProj" if os.name == "nt" else "/tmp/work/SomeProj"
        _write_session_message(tmp, proj)
        # 故意用一个本机还原不出的 wid：正文里有路径，但哈希对不上
        items = [_item("A", tmp, workspace_id="fedcba9876543210fedcba9876543210")]
        with mock.patch("ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths",
                        return_value=[]), \
             mock.patch("ai_env_clone.workspace_plan._MINE_CACHE", {}):
            root, dlg = self._dialog("workbuddy", items)
            try:
                dlg._on_pick()
                plan = dlg._selected_plans()[0]
                self.assertIn(proj, plan.hints)
                dlg.manual_var.set(True)
                dlg._on_manual_toggle()
                self.assertIn(proj, dlg.manual_combo.cget("values"))
            finally:
                root.destroy()

    def test_missing_workspace_dialog_names_origin(self):
        """「是否创建」对话框必须点名每个落点的来源（源会话还原 / 工具默认）。"""
        proj = r"D:\project\Demo"
        plans = [
            workspace_plan.plan_for("workbuddy", {"cwd": proj, "workspace_id": ""},
                                    root=r"C:\t"),
            workspace_plan.plan_for("workbuddy", None, root=r"C:\t",
                                    default_value=r"C:\t\WorkBuddy\2026"),
        ]
        text = gui.missing_workspaces_text(plans)
        self.assertIn(proj, text)
        self.assertIn("自动 · 沿用会话自带工作区", text)
        self.assertIn("工具默认（无工作区会话）", text)

    def test_no_manual_leak_after_toggling_off_multi(self):
        """多选下「勾选手动 -> 取消」后，预览不得残留手动态（含预填值造成的假象）。"""
        items = [_item("A", r"D:\src\a.jsonl"), _item("B", r"D:\src\b.jsonl")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            dlg.listbox.selection_set(0, 1)
            dlg._on_pick()
            dlg.manual_var.set(True)
            dlg._on_manual_toggle()
            self.assertIn("【手动指定", dlg.ws_preview_var.get())
            dlg.manual_var.set(False)
            dlg._on_manual_toggle()
            text = dlg.ws_preview_var.get()
            self.assertNotIn("【手动指定", text)
            self.assertNotIn("将覆盖全部", text)
            self.assertIn("非手动指定", text)      # 反证标注：自动判定，不是你填的
        finally:
            root.destroy()

    def test_single_select_shows_actual_landing(self):
        """单选 -> 预览直接给出**实际会用到**的工作区（自动判定结果）。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            dlg.listbox.selection_set(0)
            dlg._on_pick()
            self.assertIn(r"D:\project\X", dlg.ws_preview_var.get())
            self.assertIn("沿用会话自带工作区", dlg.ws_preview_var.get())
        finally:
            root.destroy()

    def test_manual_checked_but_empty_falls_back_with_note(self):
        """勾了「手动指定」但清空内容 -> 仍按自动判定，并明确提示，不静默二义。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            dlg.manual_var.set(True)
            dlg._on_manual_toggle()          # 会预填自动值
            dlg.manual_var_value.set("")     # 用户清空
            dlg._refresh_preview()
            self.assertIn("未填值", dlg.ws_preview_var.get())
            self.assertEqual([j[1]["workspace_id"] for j in self._jobs(dlg)],
                             [r"D:\project\X"])
        finally:
            root.destroy()

    def test_manual_override_confirmation_can_abort(self):
        """手动指定且源会话自带工作区时，导入前再确认一次；用户拒绝则不导入。"""
        items = [_item("A", r"D:\src\a.jsonl", cwd=r"D:\project\X")]
        root, dlg = self._dialog("workbuddy", items)
        try:
            items_sel = dlg._selected_items()
            plan = dlg._plan_for(None, r"E:\imported")
            # HEADLESS 下确认框会被跳过，这里临时关掉以验证「真的会问」
            with mock.patch.object(gui, "HEADLESS", False), \
                 mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask, \
                 mock.patch.object(gui.session_migration, "migrate_session") as mig:
                self.assertFalse(dlg._confirm_manual_override(items_sel, plan))
                self.assertIn("覆盖", ask.call_args[0][1])
                ask.reset_mock()
                # 用户拒绝 -> _do_import 直接返回
                dlg.manual_var.set(True)
                dlg.manual_var_value.set(r"E:\imported")
                dlg._do_import()
            ask.assert_called_once()
            mig.assert_not_called()
        finally:
            root.destroy()

    def test_missing_workspace_prompt_create(self):
        """工作区不存在 -> 先问是否创建；选「是」则建目录并继续导入。"""
        ws = self._absent_workspace()
        root, dlg = self._dialog("workbuddy", [_item("A", r"D:\src\a.jsonl", cwd=ws)])
        try:
            with mock.patch.object(gui, "HEADLESS", False), \
                 mock.patch.object(gui.messagebox, "askyesnocancel", return_value=True) as ask, \
                 mock.patch.object(gui.workspace_plan, "ensure",
                                   return_value=(True, ws)) as ens, \
                 mock.patch.object(dlg, "_run_jobs") as run, \
                 mock.patch.object(dlg.win, "after", return_value=None):
                dlg._do_import()
            ask.assert_called_once()
            ens.assert_called_once()
            run.assert_called_once()
            self.assertIn(ws, ask.call_args[0][1])
            self.assertTrue(any("已按要求创建" in w for w in dlg.warns))
        finally:
            root.destroy()

    def test_missing_workspace_prompt_decline_keeps_landing(self):
        """选「否」-> 不创建，但落点不变（不偷偷改工作区），并给出提示。"""
        ws = self._absent_workspace()
        root, dlg = self._dialog("workbuddy", [_item("A", r"D:\src\a.jsonl", cwd=ws)])
        try:
            with mock.patch.object(gui, "HEADLESS", False), \
                 mock.patch.object(gui.messagebox, "askyesnocancel", return_value=False), \
                 mock.patch.object(gui.workspace_plan, "ensure") as ens, \
                 mock.patch.object(dlg, "_run_jobs") as run, \
                 mock.patch.object(dlg.win, "after", return_value=None):
                dlg._do_import()
            ens.assert_not_called()
            run.assert_called_once()
            jobs = run.call_args[0][0]
            self.assertEqual(jobs[0][1]["workspace_id"], ws)
            self.assertTrue(any("未创建" in w for w in dlg.warns))
        finally:
            root.destroy()

    def test_missing_workspace_prompt_cancel_aborts(self):
        """选「取消」-> 中止导入，不写任何会话。"""
        ws = self._absent_workspace()
        root, dlg = self._dialog("workbuddy", [_item("A", r"D:\src\a.jsonl", cwd=ws)])
        try:
            with mock.patch.object(gui, "HEADLESS", False), \
                 mock.patch.object(gui.messagebox, "askyesnocancel", return_value=None), \
                 mock.patch.object(dlg, "_run_jobs") as run:
                dlg._do_import()
            run.assert_not_called()
            self.assertIn("取消", dlg.status_lbl.cget("text"))
        finally:
            root.destroy()

    def test_reasonix_scope_and_codebuddy_id_resolution(self):
        """Reasonix 的目标是项目名（scope）；CodeBuddy 的目标是路径派生的 workspaceId。"""
        from ai_env_clone import workspace_plan as wp

        root, dlg = self._dialog("reasonix", [_item(cwd=r"D:\project\ai-env-clone")])
        try:
            jobs = dlg._build_jobs(dlg._selected_items(), dlg._selected_plans())
            self.assertEqual(jobs[0][1]["scope"], "ai-env-clone")
            self.assertNotIn("workspace_id", jobs[0][1])
        finally:
            root.destroy()

        root, dlg = self._dialog("codebuddy", [_item(cwd=r"D:\project\ai-env-clone")])
        try:
            jobs = dlg._build_jobs(dlg._selected_items(), dlg._selected_plans())
            self.assertEqual(jobs[0][1]["workspace_id"],
                             wp.codebuddy_workspace_id(r"D:\project\ai-env-clone"))
        finally:
            root.destroy()

    def test_codebuddy_reuses_source_workspace_id(self):
        """源本身是 CodeBuddy 时直接沿用它的 workspaceId（路径同源最稳）。"""
        src = _item("A", r"D:\src\a", workspace_id="deadbeef" * 4)
        root, dlg = self._dialog("codebuddy", [src])
        try:
            jobs = dlg._build_jobs(dlg._selected_items(), dlg._selected_plans())
            self.assertEqual(jobs[0][1]["workspace_id"], "deadbeef" * 4)
        finally:
            root.destroy()

    def test_workbuddy_does_not_pass_scope(self):
        """WorkBuddy 的 projects 目录名必须由 cwd 全路径派生，不能再传 scope 覆盖。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            jobs = dlg._build_jobs(dlg._selected_items(), dlg._selected_plans())
            self.assertNotIn("scope", jobs[0][1])
            self.assertEqual(jobs[0][1]["workspace_id"], r"D:\project\X")
        finally:
            root.destroy()


class TestUnresolvedWorkspaceConfirm(unittest.TestCase):
    """落点「推不出来」的会话：导入前必须显式确认，不能静默退默认落点。

    这类会话原本**有**工作区，但只有 ``md5(项目路径)`` 这个不可逆的 id，
    备份包与本机都没能还原成路径 —— 用户不提醒的话，只会在导入完成后才发现
    会话跑到了工具的默认目录。
    """

    def _dialog(self, tool, items):
        return TestWorkspaceAutoResolution._dialog(self, tool, items)

    def _pick(self, dlg):
        dlg.listbox.selection_set(0)
        dlg._on_pick()
        return dlg._selected_items(), dlg._selected_plans()

    def test_all_resolved_skips_dialog(self):
        """每条会话都有可用的工作区 -> 不该打扰用户。"""
        root, dlg = self._dialog("workbuddy", [_item(cwd=r"D:\project\X")])
        try:
            items, plans = self._pick(dlg)
            with mock.patch.object(gui, "HEADLESS", False),                  mock.patch.object(gui.messagebox, "askyesnocancel") as m:
                self.assertTrue(dlg._confirm_unresolved_workspaces(items, plans))
                m.assert_not_called()
        finally:
            root.destroy()

    def test_accept_default_continues(self):
        wid = "0" * 32
        it = _item("A", path=r"D:\src\a.jsonl", workspace_id=wid)
        root, dlg = self._dialog("workbuddy", [it])
        try:
            items, plans = self._pick(dlg)
            self.assertEqual(plans[0].mode, workspace_plan.MODE_DEFAULT)
            with mock.patch.object(gui, "HEADLESS", False),                  mock.patch.object(gui.messagebox, "askyesnocancel",
                                   return_value=True) as m:
                self.assertTrue(dlg._confirm_unresolved_workspaces(items, plans))
            self.assertIn("0" * 8, m.call_args[0][1])      # 报文里点出不可逆的 id
            self.assertFalse(dlg.manual_var.get())          # 不改用手动指定
        finally:
            root.destroy()

    def test_switch_to_manual_prefills_candidate(self):
        """选「否」-> 切到手动指定，并把候选路径预填进输入框（用户可一键改）。"""
        wid = "0" * 32
        it = _item("A", path=r"D:\src\a.jsonl", workspace_id=wid,
                   workspace_candidates=[r"D:\proj\One", r"D:\proj\Two"])
        root, dlg = self._dialog("workbuddy", [it])
        try:
            items, plans = self._pick(dlg)
            with mock.patch.object(gui, "HEADLESS", False),                  mock.patch.object(gui.messagebox, "askyesnocancel",
                                   return_value=False) as m:
                self.assertFalse(dlg._confirm_unresolved_workspaces(items, plans))
            self.assertIn(r"D:\proj\One", m.call_args[0][1])   # 候选在报文里列出
            self.assertTrue(dlg.manual_var.get())
            self.assertEqual(dlg.manual_var_value.get(), r"D:\proj\One")
        finally:
            root.destroy()

    def test_cancel_stops_import(self):
        wid = "0" * 32
        it = _item("A", path=r"D:\src\a.jsonl", workspace_id=wid)
        root, dlg = self._dialog("workbuddy", [it])
        try:
            items, plans = self._pick(dlg)
            with mock.patch.object(gui, "HEADLESS", False),                  mock.patch.object(gui.messagebox, "askyesnocancel",
                                   return_value=None):
                self.assertFalse(dlg._confirm_unresolved_workspaces(items, plans))
            self.assertFalse(dlg.manual_var.get())
        finally:
            root.destroy()


class TestPkgTabInFlightDedup(unittest.TestCase):
    """页签 B：同一个包**在途只解包一次**（否则线程风暴 + 结果互相判过期）。

    回归 2026-10-04 用户实测：选中包后会话列表一直不显示内容、点几下界面就卡死。
    成因是 ``_refresh_pkg_view`` 每回一层就重建列表并恢复选中，从而**再次**触发
    ``<<TreeviewSelect>>`` → ``_on_pkg_pick``；而当时只用「已解包完成的路径」
    （``_arc_path``）判重，于是同一个包被反复重新解包：既刷出大量后台线程，又让
    每次新请求把上一个结果判为过期丢弃 ⇒ 列表永远填不上。
    """

    class _NoThread:
        """替身线程：只记录 target，不真正启动（让断言与调度无关、可确定）。"""

        started: list = []

        def __init__(self, *args, **kwargs):
            type(self).started.append(kwargs.get("target"))
            self._target = kwargs.get("target")

        def start(self):
            pass

    def _make_dialog(self, tool: str):
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
            root = tk.Tk()
            root.withdraw()
            app = QoderBackupApp(root)
        with mock.patch("ai_env_clone.session_migration.list_source_sessions",
                        return_value=[]):
            dlg = gui.MigrateDialog(app)
        return root, dlg

    def test_repeated_selection_extracts_once(self):
        target = import_matrix.session_import_targets()[0]
        # 来源工具必须与目标不同，包才「可导入」（本工具的包该走还原）。
        src_tool = next(s.tool for s in import_matrix.importable_sources_for(target))
        root, dlg = self._make_dialog(target)
        try:
            self.assertTrue(dlg._can_import, "用例前提：目标工具可接收导入")
            path = os.path.abspath(os.path.join(
                "x", "%s_backup_20260101.zip" % src_tool))
            dlg._pkg_rows = [
                backup_scan.BackupEntry(path, os.path.basename(path), 10, 0,
                                        "backup", src_tool)
            ]
            dlg.pkg_tree.insert("", "end", iid=path, values=(
                os.path.basename(path), "备份", "10B", "2026-01-01 00:00"))
            dlg.pkg_tree.selection_set(path)

            self._NoThread.started = []
            with mock.patch.object(gui.threading, "Thread", self._NoThread):
                dlg._on_pkg_pick()        # 第一次：起一个后台解包
                dlg._on_pkg_pick()        # 再次触发：应被在途去重拦下
                dlg._refresh_pkg_view()   # 模拟「扫描回一层」重建列表并恢复选中
                dlg._on_pkg_pick()        # 恢复选中后可能再触发一次：仍应拦下
            self.assertEqual(
                len(self._NoThread.started), 1,
                "同一个包不应重复起解包线程（实测起了 %d 个）"
                % len(self._NoThread.started))
            self.assertEqual(dlg._arc_pending, path)
        finally:
            root.destroy()

    def test_switching_package_starts_new_extraction(self):
        target = import_matrix.session_import_targets()[0]
        src_tool = next(s.tool for s in import_matrix.importable_sources_for(target))
        root, dlg = self._make_dialog(target)
        try:
            paths = [os.path.abspath(os.path.join("x", "%s_backup_%d.zip" % (src_tool, i)))
                     for i in (1, 2)]
            dlg._pkg_rows = [
                backup_scan.BackupEntry(p, os.path.basename(p), 10, 0, "backup", src_tool)
                for p in paths
            ]
            for p in paths:
                dlg.pkg_tree.insert("", "end", iid=p, values=(
                    os.path.basename(p), "备份", "10B", "2026-01-01 00:00"))

            self._NoThread.started = []
            with mock.patch.object(gui.threading, "Thread", self._NoThread):
                dlg.pkg_tree.selection_set(paths[0])
                dlg._on_pkg_pick()
                dlg.pkg_tree.selection_set(paths[1])
                dlg._on_pkg_pick()
            self.assertEqual(len(self._NoThread.started), 2,
                             "换一个包应重新解包")
            self.assertEqual(dlg._arc_pending, paths[1])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
