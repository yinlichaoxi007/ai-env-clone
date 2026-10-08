"""导出前「不可脱敏原文」知情确认（core 文案位 + WorkBuddy 声明 + GUI gating）。

背景（W 文档 §3.4 的「必须」项）：会话转录是明文逐条对话，用户可能在对话里粘贴过
密钥、口令、内网地址或客户数据。这类内容**按字段名脱敏无效**（键名不固定、就藏在
普通正文里），所以只能在写盘前让用户知情并点头；用户不点头就不该生成备份包。

覆盖三层：
- :func:`core.pre_export_notices`：只认**本次勾选**的条目、去重、空白不算；
- WorkBuddy 适配器：转录类条目声明该说明，其余条目不受影响；
- GUI ``on_export``：取消 ⇒ 不弹保存框、不调用导出、不产生任何文件；确认 ⇒ 正常导出。

headless：``HEADLESS`` 下自定义弹窗退化为 ``messagebox``（真实弹窗会 wait_window 挂起）。
"""

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

from ai_env_clone import __main__ as gui  # noqa: E402
from ai_env_clone.__main__ import QoderBackupApp  # noqa: E402
from ai_env_clone.core import (  # noqa: E402
    BackupItem,
    TRANSCRIPT_PLAINTEXT_NOTE,
    pre_export_notices,
)

_ORIG_HEADLESS = None


def setUpModule() -> None:
    global _ORIG_HEADLESS
    _ORIG_HEADLESS = gui.HEADLESS
    gui.HEADLESS = True


def tearDownModule() -> None:
    if _ORIG_HEADLESS is not None:
        gui.HEADLESS = _ORIG_HEADLESS


def _item(key: str, note: str = "") -> BackupItem:
    return BackupItem(
        key=key, label=key, path=os.path.join(tempfile.gettempdir(), key),
        description="", pre_export_notice=note,
    )


# --------------------------------------------------------------------- #
# 1) core：纯函数，取文案与去重
# --------------------------------------------------------------------- #
class TestPreExportNotices(unittest.TestCase):
    def test_empty_when_nothing_declared(self):
        """没条目声明 ⇒ 空列表（大多数工具走这条，不应凭空弹提示）。"""
        self.assertEqual(pre_export_notices([_item("a"), _item("b")]), [])

    def test_only_selected_items_count(self):
        """说明挂在条目上 ⇒ 传进来的必须是「本次勾选」列表；这里验证确实按入参取。"""
        self.assertEqual(
            pre_export_notices([_item("projects", "转录含原文")]), ["转录含原文"])

    def test_dedup_keeps_first_occurrence_order(self):
        """多条转录条目共用同一句话时只提示一次，且保持先出现的顺序。"""
        items = [
            _item("workspace_sessions", TRANSCRIPT_PLAINTEXT_NOTE),
            _item("projects", TRANSCRIPT_PLAINTEXT_NOTE),
            _item("memory", "另一类内容"),
        ]
        self.assertEqual(pre_export_notices(items),
                         [TRANSCRIPT_PLAINTEXT_NOTE, "另一类内容"])

    def test_blank_note_is_not_a_notice(self):
        """空白 / 纯空格说明等于「没声明」，不该弹出只有标点正文的确认框。"""
        self.assertEqual(pre_export_notices([_item("a", "   ")]), [])


# --------------------------------------------------------------------- #
# 2) WorkBuddy：哪些条目声明、哪些不声明
# --------------------------------------------------------------------- #
class TestWorkBuddyDeclaresTranscriptNotice(unittest.TestCase):
    def setUp(self):
        from ai_env_clone.adapters.workbuddy import WorkBuddyAdapter
        self.by_key = {i.key: i for i in WorkBuddyAdapter().build_items(None, None)}

    def test_transcript_entries_declare_it(self):
        """事件流与工作区快照都是明文逐条对话 ⇒ 必须声明。"""
        for key in ("projects", "workspace_sessions"):
            self.assertEqual(self.by_key[key].pre_export_notice,
                             TRANSCRIPT_PLAINTEXT_NOTE, key)

    def test_other_entries_do_not(self):
        """未确认过的条目一律不声明：会话索引库是结构化数据、记忆与设置类走脱敏清单，
        给它们弹同一个提示会是失真的告警（用户很快就不看这类框了）。"""
        for key in ("session_db", "memory", "memory_md", "settings", "models",
                    "skills", "storage"):
            self.assertEqual(self.by_key[key].pre_export_notice, "", key)

    def test_notice_text_states_why_redaction_cannot_help(self):
        """文案要讲清「为什么脱敏帮不上」，否则用户会以为勾了敏感项就安全了。"""
        for frag in ("明文", "对话原文", "无法按字段名脱敏", "原样保留"):
            self.assertIn(frag, TRANSCRIPT_PLAINTEXT_NOTE)


# --------------------------------------------------------------------- #
# 3) GUI：on_export 的 gating
# --------------------------------------------------------------------- #
def _make_app(tool: str):
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=tool), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


def _run_until_idle(app, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.root.update()
        if not app.busy and app.msg_queue.empty():
            return True
        time.sleep(0.02)
    return False


class TestExportGate(unittest.TestCase):
    """导出前确认：取消必须一个文件都不产生，确认才继续。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="export_gate_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.exports = []
        self.asks = []
        self.save_dialogs = []
        self.zip_path = os.path.join(self.tmp, "out.zip")

    def _drive(self, tool: str, answer, *, with_notice: bool = True):
        """建 app ⇒ 打桩（弹窗 / 保存框 / 真正的打包动作）⇒ 点「导出」。

        :param with_notice: 直接控制「本批勾选内容要不要知情」。改的是被勾选项的字段，
            而不是依赖某工具的真实条目集合——这样用例断言的是 gating 行为本身。
            真实条目由 :meth:`test_workbuddy_real_items_trigger_the_gate` 单独覆盖。
        """
        root, app = _make_app(tool)
        self.addCleanup(self._close, root, app)
        app.root_dir = self.tmp
        app.items = app.adapter.build_items(self.tmp)
        app._refresh_items()

        # 选中「有对应勾选框、且非记忆分区」的第一项：记忆项走 current_var /
        # others_* 另一条取值路径，不是本用例要验证的东西。
        prefix = next(p for p in app.vars
                      if p not in ("memories_current", "memories_others")
                      and any(app._agg_prefix(i.key) == p for i in app.items))
        for key, var in app.vars.items():
            var.set(key == prefix)
        for it in app.items:
            if app._agg_prefix(it.key) == prefix:
                it.pre_export_notice = TRANSCRIPT_PLAINTEXT_NOTE if with_notice else ""

        def fake_export(zip_path, *a, **k):
            self.exports.append(zip_path)
            open(zip_path, "wb").close()
            return {"zip_path": zip_path, "file_count": 0, "total_bytes": 0,
                    "zip_bytes": 0, "bytes_by_ext": {}, "bytes_by_ext_compressed": {}}

        app.adapter.export = fake_export
        self._patches = [
            mock.patch.object(gui.messagebox, "askyesno",
                              side_effect=lambda t, x: (self.asks.append((t, x)),
                                                        answer)[1]),
            mock.patch.object(gui.messagebox, "showinfo"),
            mock.patch.object(gui.messagebox, "showerror"),
            mock.patch.object(gui.messagebox, "showwarning"),
            mock.patch.object(gui.filedialog, "asksaveasfilename",
                              side_effect=lambda **k: (self.save_dialogs.append(1),
                                                       self.zip_path)[1]),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        app.on_export()
        _run_until_idle(app)
        return app

    def _close(self, root, app):
        try:
            app._closing = True
            app._cancel_after()
        finally:
            root.destroy()

    def test_cancel_produces_no_file_and_no_dialogs_after(self):
        """点「取消」⇒ 不打开保存框、不调用导出、磁盘上没有半个包。

        放在选路径**之前**问，就是为了中止时连文件名都不用填、也不会留下空文件。
        """
        app = self._drive("workbuddy", answer=False)
        self.assertEqual(self.exports, [], "取消后不应调用导出")
        self.assertEqual(self.save_dialogs, [], "取消后不应再问保存路径")
        self.assertFalse(os.path.exists(self.zip_path), "取消后不该生成备份包文件")
        self.assertIn("未生成备份包", str(app.status.cget("text")))

    def test_confirm_asks_once_then_exports(self):
        """知情一次即可（不是每条目问一遍），确认后正常走导出。"""
        self._drive("workbuddy", answer=True)
        self.assertEqual(len(self.asks), 1, "确认框只应问一次")
        self.assertEqual(self.exports, [self.zip_path])

    def test_notice_text_names_the_content_and_the_outcome(self):
        """正文要说清「哪类内容、为什么、取消会怎样」，不能只是一句风险警告。"""
        self._drive("workbuddy", answer=True)
        title, text = self.asks[0]
        self.assertIn("导出前", title)
        for frag in (TRANSCRIPT_PLAINTEXT_NOTE, "取消", "不会生成"):
            self.assertIn(frag, text)

    def test_no_notice_means_no_extra_click(self):
        """没有条目声明时，导出流程一步直达——给所有工具白加一次确认是噪音。"""
        self._drive("qoder", answer=True, with_notice=False)
        self.assertEqual(self.asks, [], "无声明内容不该弹确认框")
        self.assertEqual(self.exports, [self.zip_path])

    def test_workbuddy_real_items_trigger_the_gate(self):
        """端到端：不覆盖字段，直接用 WorkBuddy 适配器真实条目——转录项被勾选时
        必须弹框；只勾记忆类时不该弹（防止声明写在代码里却和界面对不上）。"""
        root, app = _make_app("workbuddy")
        self.addCleanup(self._close, root, app)
        app.root_dir = self.tmp
        app.items = app.adapter.build_items(self.tmp)
        app._refresh_items()

        def select(keys):
            for prefix, var in app.vars.items():
                var.set(prefix in keys)

        with mock.patch.object(gui.messagebox, "askyesno",
                               side_effect=lambda t, x: self.asks.append(x) or False), \
             mock.patch.object(gui.messagebox, "showinfo"), \
             mock.patch.object(gui.filedialog, "asksaveasfilename",
                               return_value=self.zip_path), \
             mock.patch.object(app.adapter, "export",
                               side_effect=lambda *a, **k: self.exports.append(1)):
            select({"projects"})
            app.on_export()
            self.assertEqual(len(self.asks), 1)
            self.assertIn("对话原文", self.asks[0])

            self.asks.clear()
            select({"projects"})
            select({"memory"})
            app.on_export()
            self.assertEqual(self.asks, [], "只勾记忆不该弹转录确认")


if __name__ == "__main__":
    unittest.main()
