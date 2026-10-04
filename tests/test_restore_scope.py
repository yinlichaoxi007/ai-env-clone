"""还原语义收窄（``restore_from``）+ 备份浏览器三态（``_render_detail``）测试。

收窄后的规则（``docs/local/从备份包导入方案.md`` §2.2 / §2.3）：

===== ============ ==========================================
声明工具  结构指纹  结论
===== ============ ==========================================
== 本工具  –          允许
!= 本工具  –          **拒绝**（``showerror`` + 指路到「从备份包导入」）
缺失     匹配本工具  允许（跨机老包兼容）
缺失     匹配别的工具 **拒绝** + 点名是谁
缺失     都不匹配    拒绝（无法识别）
===== ============ ==========================================

并且：「拒绝」必须发生在**任何还原确认弹窗之前**（不能先问「确认还原」再说不许）。
"""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

import tkinter as tk

from ai_env_clone import __main__ as gui
from ai_env_clone.adapters import get_adapter
from ai_env_clone.core import MANIFEST_NAME, inspect_backup


def _make_zip(zip_path, tool=None, kind="backup", entries=("data/a.txt",), manifest=True,
              source_root="C:/src"):
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        if manifest:
            mf = {
                "version": 2, "kind": kind, "created_at": "2026-10-01T00:00:00",
                "source_root": source_root, "platform": os.name, "items": [],
                "file_count": len(entries), "total_bytes": 1,
                "bytes_by_ext": {}, "bytes_by_ext_compressed": {},
            }
            if tool:
                mf["tool"] = tool
            zf.writestr(MANIFEST_NAME, json.dumps(mf, ensure_ascii=False))
        for name in entries:
            zf.writestr(name, "x")


class _PromptCapture:
    """把 ``gui.messagebox`` 换成记录器（不弹真窗）。"""

    def __init__(self):
        self.calls = []

    def install(self):
        self._orig = dict(gui.messagebox.__dict__)
        gui.messagebox.showinfo = lambda t, m, **k: self.calls.append(("showinfo", t, m))
        gui.messagebox.showwarning = lambda t, m, **k: self.calls.append(("showwarning", t, m))
        gui.messagebox.showerror = lambda t, m, **k: self.calls.append(("showerror", t, m))
        gui.messagebox.askyesno = lambda t, m, **k: (
            self.calls.append(("askyesno", t, m)) or True
        )

    def restore(self):
        gui.messagebox.__dict__.update(self._orig)

    def titles(self, kind=None):
        return [c[1] for c in self.calls if kind is None or c[0] == kind]

    def msgs(self, kind=None):
        return [c[2] for c in self.calls if kind is None or c[0] == kind]

    def has(self, kind, title):
        return any(c[0] == kind and c[1] == title for c in self.calls)


class TestRestoreScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.restore_root = os.path.join(self.tmp, "target")
        os.makedirs(self.restore_root, exist_ok=True)

        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.app = gui.QoderBackupApp(self.tk_root)
        self.app.adapter = get_adapter("qoder")   # 本工具 = qoder
        self.app.root_dir = self.restore_root

        self.prompt = _PromptCapture()
        self.prompt.install()

    def tearDown(self):
        self.prompt.restore()
        try:
            self.app._closing = True
            self.tk_root.update_idletasks()
            self.tk_root.destroy()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 拒绝 --
    def test_cross_tool_manifest_rejected_with_pointer(self):
        """别的工具的包（清单声明）→ 拒绝，且**在还原确认之前**就拒绝。"""
        z = os.path.join(self.tmp, "wb.zip")
        _make_zip(z, tool="workbuddy")
        self.app.restore_from(z, expected_kind="backup")
        self.assertTrue(self.prompt.has("showerror", "不可在此还原"))
        msg = [m for t, m in zip(self.prompt.titles(), self.prompt.msgs())
               if t == "不可在此还原"][0]
        self.assertIn("从备份包导入", msg)
        self.assertIn("WorkBuddy", msg)          # 展示名，不是裸 key
        self.assertNotIn("workbuddy", msg)
        self.assertFalse(self.prompt.has("askyesno", "确认还原备份包"),
                         "拒绝必须发生在还原确认之前")

    def test_no_manifest_but_other_tool_fingerprint_rejected(self):
        """无声明、但结构指纹是**别的**工具 → 拒绝并点名。"""
        z = os.path.join(self.tmp, "wb2.zip")
        _make_zip(z, manifest=False, entries=(".workbuddy/workbuddy.db",))
        self.app.restore_from(z, expected_kind=None)
        self.assertTrue(self.prompt.has("showerror", "不可在此还原"))
        msg = [m for t, m in zip(self.prompt.titles(), self.prompt.msgs())
               if t == "不可在此还原"][0]
        self.assertIn("WorkBuddy", msg)
        self.assertFalse(self.prompt.has("askyesno", "确认还原备份包"))

    def test_unidentifiable_package_rejected(self):
        """清单无 tool 且任何指纹都不匹配 → 现状文案拒绝。"""
        z = os.path.join(self.tmp, "plain.zip")
        _make_zip(z, tool=None, entries=("data/a.txt",))
        self.app.restore_from(z, expected_kind="backup")
        self.assertTrue(self.prompt.has("showerror", "不可还原"))
        self.assertFalse(self.prompt.has("askyesno", "确认还原备份包"))

    # ------------------------------------------------------------ 放行 --
    def test_same_tool_package_allowed(self):
        z = os.path.join(self.tmp, "qd.zip")
        _make_zip(z, tool="qoder", source_root=self.restore_root)
        self.app.restore_from(z, expected_kind="backup")
        self.assertFalse(self.prompt.has("showerror", "不可在此还原"))
        self.assertFalse(self.prompt.has("showerror", "不可还原"))
        self.assertTrue(self.prompt.has("askyesno", "确认还原备份包"))

    def test_no_manifest_but_self_fingerprint_allowed(self):
        """无声明、指纹匹配**本工具**的老包：仍要放行（跨机兼容）。"""
        z = os.path.join(self.tmp, "qd_old.zip")
        _make_zip(z, manifest=False, entries=(".qoder-cn/User/x.json",),
                  source_root=self.restore_root)
        self.app.restore_from(z, expected_kind=None)
        self.assertFalse(self.prompt.has("showerror", "不可在此还原"))
        self.assertFalse(self.prompt.has("showerror", "不可还原"))
        self.assertTrue(self.prompt.has("askyesno", "确认还原备份包"))


class TestBrowserDetail(unittest.TestCase):
    """浏览器详情：跨工具置灰 + 指名 + tooltip 指路；本工具仍可点。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.app = gui.QoderBackupApp(self.tk_root)
        self.app.adapter = get_adapter("qoder")
        self.browsers = []

    def tearDown(self):
        for b in self.browsers:
            try:
                b._cancel_scan()
                b.top.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.app._closing = True
            self.tk_root.update_idletasks()
            self.tk_root.destroy()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self, zip_path):
        info = inspect_backup(zip_path, match_structure=self.app.adapter.match_structure)
        browser = gui.BackupBrowser(self.app, self.tmp)
        self.browsers.append(browser)
        browser._render_detail(info, zip_path)
        return browser

    def test_other_tool_disabled_and_pointed(self):
        z = os.path.join(self.tmp, "other.zip")
        _make_zip(z, tool="workbuddy")
        b = self._render(z)
        self.assertEqual(str(b.restore_btn.cget("state")), "disabled")
        self.assertIn("从备份包导入", b._restore_tip.text)
        self.assertIn("WorkBuddy", b._restore_tip.text)
        text = b.detail.get("1.0", "end")
        self.assertIn("WorkBuddy", text)
        self.assertIn("【还原】不可在此还原", text)
        self.assertIn("从备份包导入", text)
        self.assertNotIn("workbuddy", text)          # 不再印内部英文 key

    def test_own_tool_enabled(self):
        z = os.path.join(self.tmp, "own.zip")
        _make_zip(z, tool="qoder")
        b = self._render(z)
        self.assertEqual(str(b.restore_btn.cget("state")), "normal")
        self.assertEqual(b._restore_tip.text, "")
        text = b.detail.get("1.0", "end")
        self.assertIn("（本工具）", text)
        self.assertIn("【还原】可在本窗口直接还原。", text)

    def test_unknown_tool_disabled(self):
        z = os.path.join(self.tmp, "plain.zip")
        _make_zip(z, tool=None, entries=("data/a.txt",))
        b = self._render(z)
        self.assertEqual(str(b.restore_btn.cget("state")), "disabled")
        self.assertIn("【还原】不可还原", b.detail.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
