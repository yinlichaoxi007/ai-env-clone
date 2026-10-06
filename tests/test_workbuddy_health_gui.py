"""WorkBuddy 会话健康行（检测 / 修复按钮）的 headless 端到端测试。

与 ``tests/test_dsh_health_gui.py`` 同构：选中 WorkBuddy 才显示这一行；点「检测
会话健康」走「后台线程 → 消息队列 → 主线程渲染」全链路；点「修复会话数据」先出
dry-run 计划、确认后备份写库并自动复检。messagebox 全部 mock，HEADLESS 下自定义
弹窗退化为 messagebox（否则会 wait_window 挂起）。
"""

import json
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
from ai_env_clone.__main__ import QoderBackupApp  # noqa: E402

UID = "9ae9129b-c0e9-4158-b4cd-983fac049c6d"
CWD = "D:\\project\\Demo"

_ORIG_HEADLESS = None


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


def _packed(w) -> bool:
    return w.winfo_manager() == "pack"


def _build_home(tmp: str, sessions: int = 2) -> str:
    """构造「导入后侧栏看不到会话」的现场：库里 user_id='imported'、正文都在、快照为空。"""
    home = os.path.join(tmp, ".workbuddy")
    os.makedirs(home, exist_ok=True)
    db = os.path.join(home, "workbuddy.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT,
            status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            last_activity_at INTEGER, deleted_at INTEGER,
            is_playground INTEGER NOT NULL DEFAULT 0, source_mode TEXT, mode TEXT,
            transport TEXT NOT NULL DEFAULT 'local');
        CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER NOT NULL);
        """)
    con.execute("insert into workspaces(path,last_opened_at) values(?,1)", (CWD,))
    for i in range(sessions):
        con.execute(
            "insert into sessions(id,cwd,user_id,title,status,created_at,updated_at,"
            " source_mode) values(?,?,?,?,?,?,?,'import')",
            ("s%d" % i, CWD, "imported", "会话%d" % i, "completed", 1000, 2000))
    con.commit()
    con.close()

    sdir = os.path.join(home, "projects", "demo")
    os.makedirs(sdir, exist_ok=True)
    for i in range(sessions):
        with open(os.path.join(sdir, "s%d.jsonl" % i), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "会话%d" % i,
                                 "timestamp": 1730000000000}, ensure_ascii=False) + "\n")
            fh.write(json.dumps({"type": "message", "role": "user", "cwd": CWD,
                                 "content": [{"type": "text", "text": "问题"}],
                                 "timestamp": 1730000001000}, ensure_ascii=False) + "\n")
    snap_dir = os.path.join(home, UID)
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, "sidebar-list-snapshot.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"version": 1, "items": []}, fh)
    return home


def _db_owner(home: str) -> dict:
    con = sqlite3.connect(os.path.join(home, "workbuddy.db"))
    try:
        return {r[0]: r[1] for r in con.execute("select id, user_id from sessions")}
    finally:
        con.close()


def _drain_until(app, predicate, timeout: float = 5.0) -> bool:
    """真实 GUI 由 after 循环驱动队列，测试里手动轮询消费。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app._drain_queue()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestWbHealthRowVisibility(unittest.TestCase):
    def test_hidden_for_other_tools(self):
        """非 WorkBuddy 工具不显示这一行（也别把 DSH 行带出来）。"""
        root, app = _make_app("dsh")
        try:
            self.assertFalse(_packed(app.wb_health_frame))
            self.assertTrue(_packed(app.dsh_health_frame))
        finally:
            app._cancel_after()
            root.destroy()

    def test_shown_for_workbuddy_and_follows_switch(self):
        root, app = _make_app("workbuddy")
        try:
            self.assertTrue(_packed(app.wb_health_frame))
            self.assertFalse(_packed(app.dsh_health_frame))
            self.assertEqual(app.wb_check_btn.cget("text"), "检测会话健康")
            self.assertEqual(app.wb_fix_btn.cget("text"), "修复会话数据")
            app.tool_var.set("DeepSeek Harness")
            app._on_switch_tool()
            self.assertFalse(_packed(app.wb_health_frame))
            app.tool_var.set("WorkBuddy")
            app._on_switch_tool()
            self.assertTrue(_packed(app.wb_health_frame))
        finally:
            app._cancel_after()
            root.destroy()

    def test_buttons_need_existing_home(self):
        """没装过 WorkBuddy（数据根不存在）时按钮禁用，装了才可点。"""
        root, app = _make_app("workbuddy")
        tmp = tempfile.mkdtemp(prefix="wb_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        missing = os.path.join(tmp, ".workbuddy")
        try:
            with mock.patch("ai_env_clone.adapters.workbuddy._workbuddy_home",
                            return_value=missing):
                app._update_wb_health_visibility()
                self.assertEqual(str(app.wb_check_btn.cget("state")), "disabled")
                home = _build_home(tmp)
                app._update_wb_health_visibility()
                self.assertEqual(str(app.wb_check_btn.cget("state")), "normal")
                self.assertEqual(app._wb_home_for_check(), home)
        finally:
            app._cancel_after()
            root.destroy()


class TestWbCheckButton(unittest.TestCase):
    def test_check_reports_wrong_owner_and_stale_snapshot(self):
        root, app = _make_app("workbuddy")
        tmp = tempfile.mkdtemp(prefix="wb_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = _build_home(tmp, sessions=2)
        try:
            with mock.patch("ai_env_clone.adapters.workbuddy._workbuddy_home",
                            return_value=home), \
                 mock.patch("ai_env_clone.__main__.messagebox.showinfo") as showinfo:
                app._wb_check()
                ok = _drain_until(
                    app,
                    lambda: "归属写错的导入会话（侧栏看不到）：2 条"
                    in app.wb_health_label.cget("text"))
                text = app.wb_health_label.cget("text")
                self.assertTrue(ok, text)
                self.assertIn("陈旧的侧栏秒开快照：1 个", text)
                self.assertIn("可自动修复：3 项", text)
                self.assertEqual(str(app.wb_health_label.cget("foreground")), "#c60")
                self.assertTrue(showinfo.called)
                # 只读：检测不写盘
                self.assertEqual(_db_owner(home), {"s0": "imported", "s1": "imported"})
        finally:
            app._cancel_after()
            root.destroy()


class TestWbFixButton(unittest.TestCase):
    def _run_fix(self, app, home, askyesno):
        """点「修复会话数据」→ 消费队列直到确认弹窗出现（再按需等到写盘完成）。"""
        with mock.patch("ai_env_clone.adapters.workbuddy._workbuddy_home",
                        return_value=home), \
             mock.patch("ai_env_clone.__main__.messagebox.askyesno",
                        return_value=askyesno) as ask, \
             mock.patch("ai_env_clone.__main__.messagebox.showinfo") as show:
            app._wb_fix()
            self.assertTrue(_drain_until(app, lambda: ask.called), "确认弹窗未出现")
            if askyesno:
                # 后台写盘完成后：结果弹窗 + 复检回调已排程
                self.assertTrue(_drain_until(
                    app, lambda: show.called and app._wb_check_job is not None))
            return ask, show

    def test_fix_writes_and_moves_snapshot_then_rechecks(self):
        root, app = _make_app("workbuddy")
        tmp = tempfile.mkdtemp(prefix="wb_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = _build_home(tmp, sessions=2)
        snap = os.path.join(home, UID, "sidebar-list-snapshot.json")
        try:
            ask, _show = self._run_fix(app, home, askyesno=True)
            confirm_text = ask.call_args[0][1]
            self.assertIn("把 2 条导入会话的 user_id 改为空串", confirm_text)
            self.assertIn("移走 1 个陈旧的侧栏快照", confirm_text)
            self.assertIn("完全退出 WorkBuddy", confirm_text)
            self.assertEqual(_db_owner(home), {"s0": "", "s1": ""})
            self.assertFalse(os.path.exists(snap))
            # 复检已排程（窗口关闭时由 _cancel_after 撤销，不留 invalid command 噪音）
            self.assertIsNotNone(app._wb_check_job)
        finally:
            app._cancel_after()
            root.destroy()

    def test_cancel_leaves_data_untouched(self):
        root, app = _make_app("workbuddy")
        tmp = tempfile.mkdtemp(prefix="wb_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = _build_home(tmp, sessions=1)
        try:
            self._run_fix(app, home, askyesno=False)
            self.assertEqual(_db_owner(home), {"s0": "imported"})
            self.assertTrue(os.path.exists(os.path.join(home, UID, "sidebar-list-snapshot.json")))
            self.assertEqual(app.status.cget("text"), "已取消修复")
        finally:
            app._cancel_after()
            root.destroy()

    def test_healthy_home_offers_no_changes(self):
        """库里归属正确、正文齐全 ⇒ 计划为空，只提示无需修复。"""
        root, app = _make_app("workbuddy")
        tmp = tempfile.mkdtemp(prefix="wb_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = _build_home(tmp, sessions=1)
        con = sqlite3.connect(os.path.join(home, "workbuddy.db"))
        con.execute("update sessions set user_id=''")
        con.commit()
        con.close()
        with open(os.path.join(home, UID, "sidebar-list-snapshot.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "items": [{"id": "s0"}]}, fh)
        try:
            with mock.patch("ai_env_clone.adapters.workbuddy._workbuddy_home",
                            return_value=home), \
                 mock.patch("ai_env_clone.__main__.messagebox.askyesno") as ask, \
                 mock.patch("ai_env_clone.__main__.messagebox.showinfo") as show:
                app._wb_fix()
                self.assertTrue(_drain_until(app, lambda: show.called))
                self.assertFalse(ask.called, "无需修复时不该问「是否继续」")
                self.assertIn("无需修复", show.call_args[0][1])
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
