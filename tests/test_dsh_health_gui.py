"""DSH 会话健康行：可见性随工具切换、检测按钮端到端（headless）。

headless 说明：``winfo_ismapped()`` 在 withdraw 窗口下不可靠，用
``winfo_manager()``（"pack"=已 pack，""=已 pack_forget）判断可见性。
检测按钮走「后台线程 → 消息队列 → 主线程渲染」全链路，messagebox 被 mock。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone.__main__ import QoderBackupApp  # noqa: E402


def _make_app(tool: str):
    """构造 app；_load_last_tool 决定初始适配器，且不写真实偏好缓存。"""
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _packed(w) -> bool:
    return w.winfo_manager() == "pack"


class TestDshHealthRow(unittest.TestCase):
    def test_hidden_for_non_dsh(self):
        """非 dsh 工具不显示「会话健康」行（不破坏其他工具布局）。"""
        root, app = _make_app("qoder")
        try:
            self.assertFalse(_packed(app.dsh_health_frame))
        finally:
            app._cancel_after()
            root.destroy()

    def test_shown_for_dsh_and_follows_switch(self):
        """选中 dsh 显示；切走隐藏；切回再显示。"""
        root, app = _make_app("dsh")
        try:
            self.assertTrue(_packed(app.dsh_health_frame))
            self.assertIsNotNone(app.dsh_check_btn)
            self.assertIsNotNone(app.dsh_fix_btn)
            app.tool_var.set("Qoder")
            app._on_switch_tool()
            self.assertFalse(_packed(app.dsh_health_frame))
            app.tool_var.set("DeepSeek Harness")
            app._on_switch_tool()
            self.assertTrue(_packed(app.dsh_health_frame))
        finally:
            app._cancel_after()
            root.destroy()

    def test_check_button_reports_ungrouped(self):
        """点击「检测会话健康」后：后台扫描 → 队列 → 标签更新 + 弹窗。"""
        root, app = _make_app("dsh")
        tmp = tempfile.mkdtemp(prefix="dsh_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        try:
            dsh_dir = os.path.join(tmp, ".dsh")
            session_path = os.path.join(
                dsh_dir, "sessions", "--D-project-a--", "session-x", "session.jsonl.zstd"
            )
            os.makedirs(os.path.dirname(session_path), exist_ok=True)
            open(session_path, "wb").close()
            storages = os.path.join(dsh_dir, "storages")
            os.makedirs(storages, exist_ok=True)
            idx = {
                "unit": {"name": "workspace", "version": 2},
                "global": {"initialized": True, "workspaceIds": [], "archivedSessionIds": []},
                "tables": {"workspaces": {}},
            }
            with open(os.path.join(storages, "workspace.json"), "w", encoding="utf-8") as fh:
                json.dump(idx, fh)

            with mock.patch.dict(os.environ, {"DSH_HOME": dsh_dir}, clear=False), \
                 mock.patch("ai_env_clone.__main__.messagebox.showinfo") as showinfo:
                app._dsh_check()
                # 后台线程完成前轮询消费队列（真实 GUI 由 after 循环驱动）
                deadline = time.time() + 5
                while time.time() < deadline:
                    app._drain_queue()
                    if not app.busy and "未分组" in app.dsh_health_label.cget("text"):
                        break
                    time.sleep(0.02)
                self.assertIn("未分组会话：1 个", app.dsh_health_label.cget("text"))
                self.assertTrue(showinfo.called)
        finally:
            app._cancel_after()
            root.destroy()


def _build_dsh_home(tmp: str) -> "tuple[str, str, str]":
    """构造含「扁平 replayState + 重复 call id」的假 DSH 目录（明文 JSONL，无需真实 zstd）。

    返回 ``(dsh_dir, session_log_path, workspace_json_path)``。
    """
    dsh_dir = os.path.join(tmp, ".dsh")
    session_path = os.path.join(
        dsh_dir, "sessions", "--D-project-a--", "session-x", "session.jsonl.zstd"
    )
    os.makedirs(os.path.dirname(session_path))
    call = lambda: {"type": "tool-call", "id": "call_a", "name": "read", "args": {}}
    lines = [
        {"type": "session", "version": 0, "id": "session-x", "createdAt": 1,
         "cwd": "D:\\project\\a", "delegationDepth": 0},
        {"type": "turn/start", "seq": 1, "data": {"turn": 1}},
        {"type": "assistant/message", "seq": 2, "data": {"message": {"role": "assistant", "content": [call()]}}},
        {"type": "tool/call", "seq": 3, "data": {"callId": "call_a"}},
        # 同一步内重复宣告 call_a（需勾选才修复）
        {"type": "assistant/message", "seq": 4, "data": {"message": {"role": "assistant", "content": [call()]}}},
        {"type": "tool/call", "seq": 5, "data": {"callId": "call_a"}},
        # 扁平 replayState（默认即修，无损）
        {"type": "assistant/chunk", "seq": 6, "data": {"chunk": {
            "type": "finish", "replayState": {"kind": "pi-ai", "version": 1}}}},
    ]
    with open(session_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")

    storages = os.path.join(dsh_dir, "storages")
    os.makedirs(storages, exist_ok=True)
    idx_path = os.path.join(storages, "workspace.json")
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump({
            "unit": {"name": "workspace", "version": 2},
            "global": {"initialized": True, "workspaceIds": ["ws-a"], "archivedSessionIds": []},
            "tables": {"workspaces": {
                "ws-a": {"path": "D:\\project\\a", "title": "a", "sessionIds": [],
                         "createdAt": "t1", "updatedAt": "t1"},
            }},
        }, fh)
    return dsh_dir, session_path, idx_path


class TestDshFixCheckbox(unittest.TestCase):
    """「同时修复重复调用 ID」选项：默认不勾选，勾选后才进入计划。"""

    def _make(self, checkbox: bool = False):
        root, app = _make_app("dsh")
        tmp = tempfile.mkdtemp(prefix="dsh_gui_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        dsh_dir, session_path, idx_path = _build_dsh_home(tmp)
        app.dsh_fix_dup_var.set(checkbox)
        return root, app, dsh_dir, session_path, idx_path

    def _run_fix(self, app, askyesno_mock):
        """点「修复」并轮询队列，直到确认弹窗被调用。"""
        app._dsh_fix()
        deadline = time.time() + 5
        while time.time() < deadline:
            app._drain_queue()
            if askyesno_mock.called:
                break
            time.sleep(0.02)
        self.assertTrue(askyesno_mock.called)

    def test_checkbox_default_off_and_gates_dup_repair(self):
        root, app, dsh_dir, session_path, idx_path = self._make(False)
        try:
            with mock.patch.dict(os.environ, {"DSH_HOME": dsh_dir}), \
                 mock.patch("ai_env_clone.__main__.messagebox.askyesno", return_value=True) as ask, \
                 mock.patch("ai_env_clone.__main__.messagebox.showinfo") as show:
                self.assertFalse(app.dsh_fix_dup_var.get())
                self.assertEqual(app.dsh_fix_dup_check.cget("text"), "同时修复重复调用 ID（会改写数据）")
                self._run_fix(app, ask)
                # 继续消费队列直到写盘完成
                deadline = time.time() + 5
                while time.time() < deadline:
                    app._drain_queue()
                    if show.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(show.called)

                confirm_text = ask.call_args[0][1]
                self.assertIn("replayState升级为信封", confirm_text)
                self.assertNotIn("重复tool-call", confirm_text)

                # 索引已登记
                with open(idx_path, "r", encoding="utf-8") as fh:
                    after = json.load(fh)
                self.assertIn("session-x", after["tables"]["workspaces"]["ws-a"]["sessionIds"])
                # replayState 已升级为信封；call id 未被改写
                with open(session_path, "r", encoding="utf-8") as fh:
                    rows = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
                replay = rows[-1]["data"]["chunk"]["replayState"]
                self.assertEqual(set(replay.keys()), {"response"})
                self.assertEqual(replay["response"]["kind"], "pi-ai")
                self.assertEqual([r["data"]["callId"] for r in rows if r["type"] == "tool/call"],
                                 ["call_a", "call_a"])
        finally:
            app._cancel_after()
            root.destroy()

    def test_checkbox_on_includes_dup_repair(self):
        root, app, dsh_dir, session_path, idx_path = self._make(True)
        try:
            with mock.patch.dict(os.environ, {"DSH_HOME": dsh_dir}), \
                 mock.patch("ai_env_clone.__main__.messagebox.askyesno", return_value=True) as ask, \
                 mock.patch("ai_env_clone.__main__.messagebox.showinfo") as show:
                self._run_fix(app, ask)
                deadline = time.time() + 5
                while time.time() < deadline:
                    app._drain_queue()
                    if show.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(show.called)

                confirm_text = ask.call_args[0][1]
                self.assertIn("replayState升级为信封", confirm_text)
                self.assertIn("重复tool-call id去重", confirm_text)
                # call id 已加后缀区分
                with open(session_path, "r", encoding="utf-8") as fh:
                    rows = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
                self.assertEqual([r["data"]["callId"] for r in rows if r["type"] == "tool/call"],
                                 ["call_a", "call_a#2"])
                msg_ids = [b["id"] for r in rows if r["type"] == "assistant/message"
                           for b in r["data"]["message"]["content"]]
                self.assertEqual(msg_ids, ["call_a", "call_a#2"])
                # 备份存在（会话文件 + 索引各一份）
                self.assertTrue(any(f.startswith("session.jsonl.zstd.bak.")
                                    for f in os.listdir(os.path.dirname(session_path))))
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
