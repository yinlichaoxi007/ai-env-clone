"""还原流程闭环测试：验证跨设备恢复时的提示（重映射 / 目标路径不一致）真实触发。

不依赖纯单元 mock 的 preview_path_rewrite，而是用真实 QoderBackupApp + 真实
restore_from + 真实 import_backup 跑通「构造备份包 → 还原 → 捕获弹窗」全链路，
断言提示文案包含关键措辞（重映射、项目路径补救说明）。
"""
import json
import os
import tempfile
import shutil
import unittest
import zipfile

import tkinter as tk

from ai_env_clone import __main__ as gui
from ai_env_clone.adapters import get_adapter
from ai_env_clone.adapters.codebuddy import detect_current_uid
from ai_env_clone.core import MANIFEST_NAME, inspect_backup, import_backup


SRC_UID = "00000000-0000-0000-0000-000000000000"  # 确定与当前登录用户不同


def _make_codebuddy_zip(zip_path: str, source_root: str) -> None:
    """构造一个 CodeBuddy 备份包：含跨用户 UUID 路径（触发重映射）+
    manifest.source_root 与还原目标不一致（触发目标路径不一致弹窗）。
    """
    manifest = {
        "version": 2,
        "kind": "backup",
        "tool": "codebuddy",
        "created_at": "2026-08-18T00:00:00",
        "source_root": source_root,
        "platform": os.name,
        "items": ["history"],
        "file_count": 1,
        "total_bytes": 10,
        "bytes_by_ext": {},
        "bytes_by_ext_compressed": {},
    }
    inner = "CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/history/x/index.json" % (SRC_UID, SRC_UID)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(inner, json.dumps({"title": "t", "messages": []}, ensure_ascii=False))


class TestRestorePrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zip_path = os.path.join(self.tmp, "cb_backup.zip")
        # 还原目标根：一个与 manifest.source_root 不同的临时目录
        self.restore_root = os.path.join(self.tmp, "restore_target")
        os.makedirs(self.restore_root, exist_ok=True)
        _make_codebuddy_zip(self.zip_path, source_root=os.path.join(self.tmp, "other_machine_root"))

        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.app = gui.QoderBackupApp(self.tk_root)
        # 切换到 codebuddy 适配器 + 设定还原目标根
        self.app.adapter = get_adapter("codebuddy")
        self.app.root_dir = self.restore_root

        # mock 弹窗：记录调用，askyesno 默认放行
        self.calls = []
        self._orig = dict(gui.messagebox.__dict__)
        gui.messagebox.showinfo = lambda title, msg, **k: self.calls.append(("showinfo", title, msg))
        gui.messagebox.showerror = lambda title, msg, **k: self.calls.append(("showerror", title, msg))
        gui.messagebox.askyesno = lambda title, msg, **k: (
            self.calls.append(("askyesno", title, msg)) or True
        )
        gui.messagebox.askquestion = lambda title, msg, **k: (
            self.calls.append(("askquestion", title, msg)) or True
        )
        gui.messagebox.askokcancel = lambda title, msg, **k: (
            self.calls.append(("askokcancel", title, msg)) or True
        )

    def tearDown(self):
        gui.messagebox.__dict__.update(self._orig)
        try:
            self.app._closing = True
            self.tk_root.update_idletasks()
            self.tk_root.destroy()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _titles(self, kind):
        return [c[1] for c in self.calls if c[0] == kind]

    def _msgs(self, kind):
        return [c[2] for c in self.calls if c[0] == kind]

    def test_rewrite_prompt_shown(self):
        """源 UUID ≠ 当前用户 → 前置弹出「已自动重映射登录用户」且文案含「重映射」。"""
        self.app.restore_from(self.zip_path, expected_kind="backup")
        self.assertIn("已自动重映射登录用户", self._titles("showinfo"))
        self.assertTrue(
            any("重映射" in m for m in self._msgs("showinfo")),
            "重映射提示文案应含『重映射』，实际：%r" % self._msgs("showinfo"),
        )

    def test_path_mismatch_prompt_has_project_hint(self):
        """目标路径不一致 → 弹窗文案含『项目路径』补救说明。"""
        self.app.restore_from(self.zip_path, expected_kind="backup")
        self.assertIn("目标路径不一致，请确认", self._titles("askyesno"))
        mismatch_msgs = [
            m for t, m in zip(self._titles("askyesno"), self._msgs("askyesno"))
            if t == "目标路径不一致，请确认"
        ]
        self.assertTrue(mismatch_msgs)
        self.assertTrue(
            any("项目路径" in m for m in mismatch_msgs),
            "目标路径不一致弹窗应含项目路径补救说明，实际：%r" % mismatch_msgs,
        )

    def test_same_machine_no_rewrite_prompt(self):
        """同源（源 UUID == 当前用户）且路径一致 → 不弹重映射提示。"""
        # 用当前用户真实 UUID 重写 zip 内的路径
        cur = detect_current_uid() or SRC_UID
        same_zip = os.path.join(self.tmp, "same.zip")
        manifest = {
            "version": 2, "kind": "backup", "tool": "codebuddy",
            "created_at": "2026-08-18T00:00:00", "source_root": self.restore_root,
            "platform": os.name, "items": ["history"], "file_count": 1,
            "total_bytes": 10, "bytes_by_ext": {}, "bytes_by_ext_compressed": {},
        }
        inner = "CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/history/x/index.json" % (cur, cur)
        with zipfile.ZipFile(same_zip, "w") as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr(inner, json.dumps({"title": "t", "messages": []}, ensure_ascii=False))
        self.app.restore_from(same_zip, expected_kind="backup")
        self.assertNotIn("已自动重映射登录用户", self._titles("showinfo"))


if __name__ == "__main__":
    unittest.main()
