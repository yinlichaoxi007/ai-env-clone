"""「转换旧版记忆」GUI 入口的测试（headless，全合成样本 + 临时目录）。

钉住四条性质：

1. **入口只在 Qoder 出现**，且打开对话框本身**不产生任何写入**（连读库都要用户点
   「生成计划」才开始）；
2. **dry-run → 确认 → 才写盘**，写盘**只新建**：第二次转换必须全部显示「已存在跳过」，
   且用户手改过的那份文件内容原样保留（合并不覆盖）；
3. **UID 为空直接拦住**：否则会落到产品读不到的 ``memories/unknown/``，白写一场；
4. **导出目标互斥**：选 CodeBuddy 时「落点布局 / UID / Qoder 数据根」整组收起，
   ``.mdc`` 平铺落到 ``.memories`` 目录。

headless 说明与 ``test_dsh_health_gui.py`` 一致：``winfo_ismapped()`` 在 withdraw 窗口下
不可靠，用 ``winfo_manager()`` 判断可见性；``HEADLESS`` 下自定义报告弹窗退化为被 mock 的
``messagebox``；对话框的后台线程由测试手动泵 ``_poll()``（不依赖主循环）。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import __main__ as gui  # noqa: E402
from ai_env_clone.__main__ import MemoryConvertDialog, QoderBackupApp  # noqa: E402

_ORIG_HEADLESS = None

UID = "019eb095-47ef-70d2-8a41-ea6278c176b1"


def setUpModule() -> None:
    global _ORIG_HEADLESS
    _ORIG_HEADLESS = gui.HEADLESS
    gui.HEADLESS = True


def tearDownModule() -> None:
    if _ORIG_HEADLESS is not None:
        gui.HEADLESS = _ORIG_HEADLESS


def _make_app(tool: str):
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _make_db(dir_path: str) -> str:
    """最小旧版 local.db：3 条 agent_memory + 1 条 lingma_memory（明文）。"""
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "local.db")
    con = sqlite3.connect(path)
    con.execute("""create table agent_memory (
        id text primary key, gmt_create text, gmt_modified text,
        scope text, scope_id text, keywords text, title text, content text,
        session_id text, is_merged int, freq int, source text, token_count int,
        type text, user_id text, category text, retention_score real,
        status text, usage_scenario text)""")
    con.execute("""create table lingma_memory (
        id text primary key, scope text, scope_id text, keywords text,
        title text, content text, category text, gmt_create text, status text)""")
    rows = [
        ("g1", "global", "", "全局记忆一", "全局内容", "kwA,kwB",
         "expert_experience", "[]"),
        ("w1", "workspace", r"D:\demo\proj", "项目记忆一", "项目内容",
         "kw1", "project_introduction", '["代码评审"]'),
        ("w2", "workspace", r"D:\other\proj", "项目记忆二", "另一个工程的内容",
         "", "project_tech_stack", "[]"),
    ]
    con.executemany(
        "insert into agent_memory values (%s)" % ",".join("?" * 19),
        [(r[0], "2026-05-06", "2026-05-06", r[1], r[2], r[5], r[3], r[4],
          "", 0, 1, "auto", 100, "memory", "1316632530577119", r[6], 0.5,
          "active", r[7]) for r in rows])
    con.execute("insert into lingma_memory values "
                "('l1','global',?,'老kw','更老一代记忆','更老内容',"
                "'project_specification','2025-06-01','active')", ("",))
    con.commit()
    con.close()
    return path


def _make_qoder_home(base: str) -> str:
    """搭一个假 Qoder 环境：``<base>/.qoder-cn/{shared_client/cache/db,memories/<UID>}``。"""
    home = os.path.join(base, ".qoder-cn")
    _make_db(os.path.join(home, "shared_client", "cache", "db"))
    os.makedirs(os.path.join(home, "memories", UID), exist_ok=True)
    return home


def _drain(dlg, timeout: float = 8.0) -> None:
    """手动泵对话框的消息队列，直到后台任务结束（无主循环时替代 win.after）。"""
    deadline = time.time() + timeout
    while dlg._running and time.time() < deadline:
        dlg._poll()
        time.sleep(0.02)
    assert not dlg._running, "后台任务超时未结束"


def _md_files(home: str) -> "list[str]":
    out = []
    for dirpath, _names, filenames in os.walk(os.path.join(home, "memories")):
        for fn in filenames:
            if fn.endswith(".md"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


class TestEntryPoint(unittest.TestCase):
    def test_button_exists_and_frame_follows_tool(self):
        """按钮挂在 Qoder 的「历史会话诊断」行里；该行只在选中 Qoder 时出现。"""
        root, app = _make_app("qoder")
        try:
            self.assertTrue(app.qoder_diag_frame.winfo_manager() == "pack")
            self.assertEqual(str(app.qoder_convert_btn.cget("text")), "转换旧版记忆…")
        finally:
            app._cancel_after()
            root.destroy()
        root2, app2 = _make_app("workbuddy")
        try:
            self.assertEqual(app2.qoder_diag_frame.winfo_manager(), "")
        finally:
            app2._cancel_after()
            root2.destroy()

    def test_memory_convert_opens_dialog(self):
        root, app = _make_app("qoder")
        try:
            with mock.patch.object(gui, "MemoryConvertDialog") as dlg_cls:
                app._memory_convert()
            self.assertEqual(dlg_cls.call_count, 1)
            self.assertIs(dlg_cls.call_args[0][0], app)
        finally:
            app._cancel_after()
            root.destroy()


class TestDialog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qmem_gui_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = _make_qoder_home(self.tmp)
        self.root, self.app = _make_app("qoder")
        self.addCleanup(self.root.destroy)
        # 对话框按 app.root_dir 推导数据根 ⇒ 指到临时目录，绝不碰真实 ~/.qoder-cn
        self.app.root_dir = self.tmp
        # 条目与「当前用户」下拉也重建到临时根：否则 UID 默认值来自真实机器，
        # 断言会随测试机装没装过 Qoder 而变（本文件必须与机器无关）。
        self.app.items = self.app.adapter.build_items(self.tmp)
        self.app._refresh_uid_combo()
        self.msgbox = mock.MagicMock()
        self.msgbox.askyesno.return_value = True
        self.patcher = mock.patch.object(gui, "messagebox", self.msgbox)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _dialog(self) -> MemoryConvertDialog:
        dlg = MemoryConvertDialog(self.app)
        self.addCleanup(lambda: dlg.win.destroy() if dlg._alive() else None)
        return dlg

    def test_opening_writes_nothing(self):
        """构造对话框不写盘、不自动读库；写盘按钮此刻必须不可用。"""
        dlg = self._dialog()
        self.assertEqual(_md_files(self.home), [])
        self.assertEqual(str(dlg.apply_btn.cget("state")), "disabled")
        self.assertIsNone(dlg._plan)
        self.assertIn("生成计划", dlg.out.get("1.0", tk.END))

    def test_defaults_from_detected_paths(self):
        dlg = self._dialog()
        self.assertEqual(dlg.db_var.get(),
                         os.path.join(self.home, "shared_client", "cache",
                                      "db", "local.db"))
        self.assertEqual(dlg.home_var.get(), self.home)
        self.assertEqual(dlg.uid_var.get(), UID)   # memories/ 下探测到的 UID
        self.assertTrue(dlg.lingma_var.get())

    def test_plan_is_dry_run_only(self):
        dlg = self._dialog()
        dlg._gen_plan()
        _drain(dlg)
        self.assertEqual(dlg._plan.to_create, 4)
        self.assertEqual(dlg._plan.existing, 0)
        self.assertEqual(_md_files(self.home), [])     # 计划阶段一个文件都没写
        self.assertEqual(str(dlg.apply_btn.cget("state")), "normal")
        text = dlg.out.get("1.0", tk.END)
        self.assertIn("未写盘", dlg.status_lbl.cget("text"))
        self.assertIn("全局记忆一", text)

    def test_apply_creates_then_never_overwrites(self):
        """写盘只新建；用户手改过的文件在第二次转换时必须原样保留。"""
        dlg = self._dialog()
        dlg._gen_plan()
        _drain(dlg)
        dlg._apply()
        _drain(dlg)          # _show_result 里会自动复盘一次计划
        files = _md_files(self.home)
        self.assertEqual(len(files), 4)
        self.assertTrue(any(os.sep + "全局记忆一.md" in f for f in files))
        with open(files[0], "r", encoding="utf-8") as f:
            self.assertIn("title:", f.read())
        edited = os.path.join(self.home, "memories", UID, "global",
                              "expert_experience", "全局记忆一.md")
        self.assertTrue(os.path.isfile(edited))
        with open(edited, "w", encoding="utf-8") as f:
            f.write("用户自己改过的内容\n")
        self.assertEqual(dlg._plan.existing, 4)        # 复盘后全部变「已存在」
        self.assertEqual(str(dlg.apply_btn.cget("state")), "disabled")
        dlg._apply()                                   # 无计划可写 ⇒ 直接返回
        _drain(dlg)
        with open(edited, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "用户自己改过的内容\n")

    def test_confirm_cancel_writes_nothing(self):
        self.msgbox.askyesno.return_value = False
        dlg = self._dialog()
        dlg._gen_plan()
        _drain(dlg)
        dlg._apply()
        self.assertEqual(_md_files(self.home), [])
        self.assertEqual(dlg.status_lbl.cget("text"), "已取消，未写盘")

    def test_empty_uid_blocks_writing_to_unknown(self):
        """UID 为空 ⇒ 拦住：落点会变成产品读不到的 memories/unknown/。"""
        dlg = self._dialog()
        dlg.uid_var.set("")
        dlg._gen_plan()
        self.assertIsNone(dlg._plan)
        self.assertEqual(str(dlg.apply_btn.cget("state")), "disabled")
        self.assertIn("memories/unknown/", dlg.out.get("1.0", tk.END))
        self.assertEqual(_md_files(self.home), [])

    def test_missing_db_blocks_with_actionable_hint(self):
        dlg = self._dialog()
        dlg.db_var.set(os.path.join(self.tmp, "nope", "local.db"))
        dlg._gen_plan()
        self.assertIsNone(dlg._plan)
        self.assertIn("找不到旧版会话库", dlg.out.get("1.0", tk.END))

    def test_scope_prefix_filters_workspace_memories(self):
        dlg = self._dialog()
        dlg.scope_var.set(r"D:\demo")
        dlg._gen_plan()
        _drain(dlg)
        titles = {e.memory.title for e in dlg._plan.entries}
        self.assertEqual(titles, {"全局记忆一", "项目记忆一", "更老一代记忆"})
        self.assertNotIn("项目记忆二", titles)

    def test_codebuddy_target_lands_flat_mdc(self):
        cb_dir = os.path.join(self.tmp, "cb", ".memories")
        os.makedirs(cb_dir)
        dlg = self._dialog()
        dlg.cb_mem_var.set(cb_dir)
        dlg.target_var.set(gui.MEMORY_TARGET_CODEBUDDY)
        dlg._on_target_change()
        # 互斥行收起：布局 / UID / Qoder 数据根都不该再占位
        for box in (dlg.box_layout, dlg.box_uid, dlg.home_row):
            self.assertEqual(box.winfo_manager(), "")
        self.assertEqual(dlg.cb_row.winfo_manager(), "pack")
        dlg._gen_plan()
        _drain(dlg)
        self.assertEqual(dlg._plan.to_create, 4)
        dlg._apply()
        _drain(dlg)
        mdc = sorted(os.listdir(cb_dir))
        self.assertEqual(len(mdc), 4)
        self.assertTrue(all(n.endswith(".mdc") for n in mdc))
        self.assertEqual(_md_files(self.home), [])   # 完全没碰 Qoder 的数据根

    def test_close_cancels_pending_poll(self):
        """关窗（含点 ✕）必须撤掉待触发的轮询 after，否则 Tk 会回调已销毁的窗口。"""
        dlg = MemoryConvertDialog(self.app)
        dlg._schedule_poll()
        self.assertIsNotNone(dlg._after_id)
        self.assertTrue(dlg.win.protocol("WM_DELETE_WINDOW"))   # ✕ 也走 _on_close
        dlg._on_close()
        self.assertIsNone(dlg._after_id)
        self.assertFalse(dlg._alive())

    def test_layout_option_switches_landing(self):
        dlg = self._dialog()
        dlg.layout_var.set("new")
        dlg._gen_plan()
        _drain(dlg)
        paths = [e.target_path for e in dlg._plan.entries]
        self.assertTrue(all("shared_client" not in p for p in paths))
        self.assertTrue(any(os.path.join("projects", "") in p for p in paths))


class TestPureText(unittest.TestCase):
    """文案层是纯函数：界面措辞（含「合并不覆盖」「回退方式」）可脱离 Tk 断言。"""

    def test_target_options_cover_both_targets(self):
        values = [v for v, _l, _d in gui.memory_convert_target_options()]
        self.assertEqual(values, [gui.MEMORY_TARGET_QODER,
                                  gui.MEMORY_TARGET_CODEBUDDY])

    def test_layout_options_default_is_evidenced_one(self):
        opts = gui.memory_convert_layout_options()
        self.assertEqual(opts[0][0], "legacy")
        self.assertIn("已证实", opts[0][1])

    def test_confirm_text_counts_and_reversibility(self):
        plan = mock.MagicMock(to_create=7, existing=3)
        title, text = gui.memory_convert_confirm_text(plan, "· 落点：X")
        self.assertIn("写盘", title)
        self.assertIn("新建 7 个文件", text)
        self.assertIn("已存在并跳过 3 个", text)
        self.assertIn("· 落点：X", text)
        self.assertIn("删掉它们即可完全回退", text)


if __name__ == "__main__":
    unittest.main()
