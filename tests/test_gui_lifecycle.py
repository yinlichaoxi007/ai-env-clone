"""主窗口生命周期：**窗口销毁必须撤掉所有待触发的 after 回调**。

背景（实测）：Tk 的 ``after`` 定时器如果没被撤掉，窗口销毁后到期会被 Tk 报

    invalid command name "<id>_fit_layout"
    invalid command name "<id>_drain_queue"
    invalid command name "<id>_dsh_check"

整套 ``discover`` 里这种噪音刷了 **150+ 行**，把真正的失败信息淹掉；而现实里的
成因是「用户在任务刚结束时关窗」这类时序。两条保证缺一不可：

1. 关窗按钮走 ``_on_close`` → ``_cancel_after()``（既有行为）；
2. **任何**销毁路径（外部 ``destroy()`` / 测试拆卸 / 解释器退出）都通过根窗口的
   ``<Destroy>`` 绑定兜底走一次 ``_cancel_after()``（本轮新增）。
"""
import os
import sys
import time
import unittest
from unittest import mock

if "tkinter" not in sys.modules:
    import tkinter as tk  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone.__main__ import QoderBackupApp


def _make_app():
    """建一个最小可用（未映射）的窗口实例：不扫描真实数据目录、不写偏好缓存。"""
    import tkinter as tk

    fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
    with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
         mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None), \
         mock.patch.object(QoderBackupApp, "_detect_root", return_value=fake_root):
        root = tk.Tk()
        root.withdraw()
        app = QoderBackupApp(root)
    return root, app


class TestAfterCallbackCleanup(unittest.TestCase):
    def test_root_binds_destroy_for_cleanup(self):
        """根窗口必须绑了 <Destroy>——这是「任何销毁路径都能兜底清理」的唯一入口。"""
        root, app = _make_app()
        try:
            self.assertIn(
                "<Destroy>", root.bind(),
                "根窗口未绑定 <Destroy> ⇒ 外部 destroy 时残留 after 回调会报噪音")
        finally:
            app._cancel_after()
            root.destroy()

    def test_cancel_after_clears_every_job(self):
        """``_cancel_after`` 要清掉三类任务：队列轮询 / 节流重排 / DSH 延迟复检。"""
        root, app = _make_app()
        try:
            # 先撤掉 __init__ 排的那条轮询链，再逐类塞入新任务——直接给
            # ``_after_id`` 赋值会**覆盖**原句柄，让它变成撤不到的野链（本用例
            # 自己踩过：改完测试仍看到一行 invalid command 噪音）。
            app._cancel_after()
            app._after_id = root.after(10000, lambda: None)
            app._schedule_relayout(delay=10000)
            app._dsh_check_job = root.after(10000, lambda: None)

            app._cancel_after()

            self.assertIsNone(app._after_id, "队列轮询任务未被撤销")
            self.assertIsNone(app._relayout_job, "节流重排任务未被撤销")
            self.assertIsNone(getattr(app, "_dsh_check_job", None),
                              "DSH 延迟复检任务未被撤销")
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_destroy_via_binding_cancels_pending_jobs(self):
        """直接 ``root.destroy()``（不走关窗按钮）也必须清干净——<Destroy> 兜底生效。"""
        root, app = _make_app()
        app._schedule_relayout(delay=10000)
        app._dsh_check_job = root.after(10000, lambda: None)
        self.assertIsNotNone(app._relayout_job, "用例前提：确有待触发的重排任务")

        root.destroy()

        self.assertTrue(app._closing, "<Destroy> 兜底未把窗口标记为「正在关闭」")
        self.assertIsNone(app._after_id, "队列轮询任务未被撤销")
        self.assertIsNone(app._relayout_job, "节流重排任务未被撤销")
        self.assertIsNone(app._dsh_check_job, "DSH 延迟复检任务未被撤销")

    def test_manual_drain_keeps_single_poll_chain(self):
        """手动调用 ``_drain_queue``（测试泵消息用）不得产生**第二条轮询链**。

        轮询链是自调度的：每 tick 末尾排下一 tick。若手动调用时不先撤掉旧句柄，
        就会并行出两条链，而 ``_after_id`` 只记得住一个 ⇒ 另一条链成为「野链」，
        窗口销毁时撤销不到，Tk 报 `invalid command name ..._drain_queue`。
        判据：撤掉全部句柄后等过一个轮询间隔（80ms），若还有野链在跑，
        它会把 `_after_id` 重新排上。
        """
        root, app = _make_app()
        try:
            app._cancel_after()          # 起点：没有任何待触发的任务
            app._drain_queue()           # 手动泵一次
            self.assertIsNotNone(app._after_id, "手动调用后应排下一 tick")

            app._cancel_after()          # 撤掉「能看见的那个」句柄
            deadline = time.time() + 0.4
            while time.time() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertIsNone(
                app._after_id,
                "手动调用 _drain_queue 后仍有野轮询链在跑（句柄被覆盖丢失）")
        finally:
            app._cancel_after()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
