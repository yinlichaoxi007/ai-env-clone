"""「导入 lingma 会话」GUI 入口与对话框的测试（headless，全合成样本 + 临时目录）。

钉住六条性质（数据安全的承诺只能靠这些断言兑现，不是靠文案）：

1. **入口位置与门控**：入口在「历史会话诊断」行（换代回填属同一工具内的回填，不占用
   跨软件的「数据导入」能力位 ⇒ Qoder 的「导入会话…」照常置灰并说明只能整库备份），
   且**条件不满足即不提供**：按钮置灰、同行写明还缺哪一条、点入口只弹原因
   **不打开对话框**（用户要求「数据路径均存在且当前所选用户就是新阿里云账号才可
   提供此功能」）；
2. **打开对话框只读**：自动生成的计划不写盘 —— 新旧两库计数一字不变、没有快照文件；
3. **确认才写盘**：``askyesno`` 返回 False 时库原样，返回 True 时才写入；
4. **写入后读回校验通过**，快照落在 ``main.sqlite`` 同目录；
5. **回滚只摘本工具写的行**：产品自写的会话与消息原封不动，计数回到写入前；
6. **关窗撤掉轮询 ``after``**：否则 Tk 会回调已销毁的窗口（与
   :class:`MemoryConvertDialog` 同一性质）。

夹具直接复用核心测试 :mod:`tests.test_qoder_session_import` 的 ``_Fixture``
（会话族 DDL + 新旧两库 + 明文文字稿），避免两份 fixture 各自漂移。
"""

import os
import shutil
import sqlite3
import sys
import time
import tkinter as tk
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone import __main__ as gui                       # noqa: E402
from ai_env_clone import qoder_session_import as qsi            # noqa: E402
from ai_env_clone.__main__ import LingmaImportDialog, QoderBackupApp  # noqa: E402
from tests.test_qoder_session_import import (                   # noqa: E402
    NEW_UID, OLD_UID, PRODUCT_PAYLOAD, _Fixture)

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


def _drain(dlg, timeout: float = 10.0) -> None:
    """手动泵对话框的消息队列，直到后台任务结束（无主循环时替代 win.after）。"""
    deadline = time.time() + timeout
    while dlg._running and time.time() < deadline:
        dlg._poll()
        time.sleep(0.02)
    assert not dlg._running, "后台任务超时未结束"


class _GuiFixture(_Fixture):
    """临时 home（新旧两库 + 文字稿）+ 指向它的 Qoder 主窗口 + mock 弹窗。"""

    def setUp(self) -> None:
        super().setUp()
        self.root, self.app = _make_app("qoder")
        self.addCleanup(self.root.destroy)
        # 对话框与门控都按 app.root_dir 取数据根 ⇒ 指到临时目录，绝不碰真实 ~/.qoder-cn
        self.app.root_dir = self.home
        # 「当前用户」下拉钉死成本样本的账号：主窗口构造时从**真实机器**探测，不覆盖
        # 的话门控结果随测试机装没装过 Qoder 而变（本文件必须与机器无关）。
        self.select_uid(NEW_UID)
        self.msgbox = mock.MagicMock()
        self.msgbox.askyesno.return_value = True
        patcher = mock.patch.object(gui, "messagebox", self.msgbox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def select_uid(self, uid: str) -> None:
        """换界面「当前用户」的选中项（下拉里显示的是短串，需要同时建映射）。"""
        short = uid[:8]
        self.app.uid_var.set(short)
        self.app._uid_short_map = {short: uid}

    def refresh(self) -> None:
        """按当前界面状态重测门控（作废缓存，走真实的可见性刷新路径）。"""
        self.app._lingma_gate_cache = None
        self.app._update_qoder_diag_visibility()

    def gate(self):
        return qsi.check_eligibility(self.home, NEW_UID, self.new_db, [self.old_db])

    def dialog(self) -> LingmaImportDialog:
        dlg = LingmaImportDialog(self.app, self.gate())
        self.addCleanup(lambda: dlg.win.destroy() if dlg._alive() else None)
        _drain(dlg)
        return dlg


class TestEntryPoint(_GuiFixture):
    def test_import_area_keeps_backup_only_semantics(self) -> None:
        """「数据导入」区对 Qoder 仍是「跨软件不支持」：按钮文案与置灰都不被特例污染。

        换代回填不占用这个能力位（它写回的是 Qoder 自己的库），所以这里必须
        与其它加密主库工具口径一致，只在说明里把用户引向诊断行。
        """
        self.make()
        self.app._refresh_import_matrix()
        self.assertEqual(str(self.app.imp_btn.cget("text")), "导入会话…")
        self.assertEqual(str(self.app.imp_btn.cget("state")), "disabled")
        text = self.app.imp_text.get("1.0", tk.END)
        self.assertIn("只能整体备份 / 还原", text)
        self.assertIn("历史会话诊断", text)

    def test_gate_closed_when_new_db_missing(self) -> None:
        """只有旧库、没有新版 main.sqlite ⇒ 入口不提供，且说清缺什么。"""
        self.make()
        shutil.rmtree(os.path.join(self.home, "AppData"), ignore_errors=True)
        self.new_db = ""
        self.refresh()
        self.assertEqual(str(self.app.qoder_import_btn.cget("state")), "disabled")
        text = self.app.qoder_gate_lbl.cget("text")
        self.assertIn("运行期条件：未满足", text)
        self.assertIn("main.sqlite", text)
        with mock.patch.object(gui, "LingmaImportDialog") as dlg_cls:
            self.app._lingma_import()
        dlg_cls.assert_not_called()
        self.assertTrue(self.msgbox.showinfo.called)

    def test_gate_closed_for_legacy_account(self) -> None:
        """当前所选用户仍是旧 lingma 账号形态 ⇒ 同样不提供（按钮置灰）。"""
        self.make()
        self.select_uid(OLD_UID)
        self.refresh()
        self.assertEqual(str(self.app.qoder_import_btn.cget("state")), "disabled")
        self.assertIn("不是新版阿里云账号", self.app.qoder_gate_lbl.cget("text"))

    def test_gate_open_and_opens_dialog(self) -> None:
        self.make()
        self.refresh()
        self.assertEqual(str(self.app.qoder_import_btn.cget("text")), "导入 lingma 会话…")
        self.assertEqual(str(self.app.qoder_import_btn.cget("state")), "normal")
        self.assertIn("运行期条件：已满足", self.app.qoder_gate_lbl.cget("text"))
        with mock.patch.object(gui, "LingmaImportDialog") as dlg_cls:
            self.app._lingma_import()
        self.assertEqual(dlg_cls.call_count, 1)
        self.assertIs(dlg_cls.call_args[0][0], self.app)
        self.assertTrue(dlg_cls.call_args[0][1].ready)

    def test_gate_rechecked_after_switching_account(self) -> None:
        """门控缓存键必须带所选账号：切账号后不能沿用上一账号的判定。"""
        self.make()
        self.refresh()
        self.assertEqual(str(self.app.qoder_import_btn.cget("state")), "normal")
        self.select_uid(OLD_UID)
        self.refresh()
        self.assertEqual(str(self.app.qoder_import_btn.cget("state")), "disabled")


class TestDialog(_GuiFixture):
    def test_opening_reads_but_writes_nothing(self) -> None:
        """构造对话框会自动出计划（只读）：两库计数一字不变、没有快照与台账。"""
        self.make()
        before = self.counts()
        old_before = self._old_counts()
        dlg = self.dialog()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self._old_counts(), old_before)
        self.assertFalse(os.path.exists(self.new_db + qsi.SNAPSHOT_SUFFIX))
        self.assertFalse(os.path.exists(self.new_db + qsi.MANIFEST_SUFFIX))
        self.assertEqual(len(dlg._plan.sessions), 2)
        self.assertEqual(str(dlg.apply_btn.cget("state")), "normal")
        out = dlg.out.get("1.0", tk.END)
        self.assertIn("未写盘", dlg.status_lbl.cget("text"))
        self.assertIn("有损", out)
        self.assertIn("刻意不写", out)
        # 取不到文字稿的那条必须列出原因，不能静默消失
        self.assertIn("没有明文文字稿", out)

    def test_cancel_writes_nothing(self) -> None:
        self.make()
        before = self.counts()
        dlg = self.dialog()
        self.msgbox.askyesno.return_value = False
        dlg._apply()
        self.assertEqual(str(dlg.status_lbl.cget("text")), "已取消，未写盘")
        self.assertEqual(self.counts(), before)
        self.assertFalse(os.path.exists(self.new_db + qsi.SNAPSHOT_SUFFIX))

    def test_apply_writes_then_rollback_restores(self) -> None:
        self.make()
        before = self.counts()
        dlg = self.dialog()
        dlg._apply()
        _drain(dlg)
        mid = self.counts()
        self.assertGreater(mid["chat_sessions"], before["chat_sessions"])
        self.assertIn("读回校验通过", dlg.out.get("1.0", tk.END))
        snap = self.new_db + qsi.SNAPSHOT_SUFFIX
        self.assertTrue(os.path.isfile(snap))          # 写前先有兜底
        con = sqlite3.connect(self.new_db)
        try:
            self.assertEqual(con.execute(
                "select payload_json from chat_session_messages where message_id='pm1'"
            ).fetchone()[0], PRODUCT_PAYLOAD)          # 产品自写行没被碰
        finally:
            con.close()

        dlg._rollback()
        _drain(dlg)
        self.assertEqual(self.counts(), before)
        self.assertIn("已摘除", dlg.out.get("1.0", tk.END))

    def test_selection_limits_what_is_written(self) -> None:
        """只导选中的那一条：写盘子计划里只有它，其余留在计划里不动。"""
        self.make()
        dlg = self.dialog()
        dlg.listbox.selection_clear(0, tk.END)
        dlg.listbox.selection_set(0)
        chosen = dlg._selected_plans()
        self.assertEqual(len(chosen), 1)
        dlg._apply()
        _drain(dlg)
        written = self._new_session_count()
        self.assertEqual(written, 1)

    def test_close_cancels_pending_poll(self) -> None:
        self.make()
        dlg = LingmaImportDialog(self.app, self.gate())
        dlg._schedule_poll()
        self.assertIsNotNone(dlg._after_id)
        self.assertTrue(dlg.win.protocol("WM_DELETE_WINDOW"))
        dlg._on_close()
        self.assertIsNone(dlg._after_id)
        self.assertFalse(dlg._alive())

    # ------------------------------------------------------------ 辅助 --
    def _old_counts(self) -> dict:
        con = sqlite3.connect(self.old_db)
        try:
            return {"chat_session": con.execute(
                "select count(*) from chat_session").fetchone()[0]}
        finally:
            con.close()

    def _new_session_count(self) -> int:
        con = sqlite3.connect(self.new_db)
        try:
            return con.execute("select count(*) from chat_sessions"
                               " where session_id <> 'product-session'").fetchone()[0]
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
