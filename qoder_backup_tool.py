"""
Qoder 记忆与会话历史 备份 / 迁移工具（图形界面）

运行：
    python qoder_backup_tool.py

核心逻辑位于 ``qoder_backup_core.py``，本文件只负责界面与交互。
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from qoder_backup_core import (
    BackupError,
    ProgressInfo,
    export_backup,
    import_backup,
    inspect_backup,
    scan_items,
)
from qoder_backup_core import QoderPaths  # 目录布局对象
from ai_env_clone.adapters import get_adapter

APP_TITLE = "Qoder 备份迁移工具"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f GB" % n


class QoderBackupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("720x640")
        root.minsize(660, 600)

        self.adapter = get_adapter("qoder")
        self.paths = self._make_paths()
        self.items = self.adapter.build_items(self.paths.root)
        self.vars: dict[str, tk.BooleanVar] = {}
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._closing = False
        self._worker: threading.Thread | None = None

        self._after_id = None
        self._build_ui()
        self._refresh_items()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(80, self._drain_queue)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # 数据目录
        top = ttk.LabelFrame(self.root, text="Qoder 数据目录")
        top.pack(fill=tk.X, **pad)
        self.root_var = tk.StringVar(value=self.paths.root)
        row = ttk.Frame(top)
        row.pack(fill=tk.X, padx=8, pady=8)
        ttk.Entry(row, textvariable=self.root_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(row, text="更改…", command=self._choose_root, width=10).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(row, text="重新检测", command=self._redetect, width=10).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.root_hint = ttk.Label(top, text="", foreground="#666")
        self.root_hint.pack(anchor="w", padx=10, pady=(0, 8))

        # 备份内容
        mid = ttk.LabelFrame(self.root, text="备份内容（勾选即生效）")
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        bar = ttk.Frame(mid)
        bar.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Button(bar, text="全选", command=lambda: self._set_all(True), width=8).pack(
            side=tk.LEFT
        )
        ttk.Button(bar, text="全不选", command=lambda: self._set_all(False), width=8).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(bar, text="推荐项", command=self._select_recommended, width=8).pack(
            side=tk.LEFT
        )
        self.summary = ttk.Label(bar, text="", foreground="#0a6")
        self.summary.pack(side=tk.RIGHT)
        ttk.Button(bar, text="估算大小", command=self._estimate, width=10).pack(
            side=tk.RIGHT, padx=8
        )

        canvas = tk.Canvas(mid, highlightthickness=0, height=240)
        sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=4)

        # 选项
        opt = ttk.LabelFrame(self.root, text="选项")
        opt.pack(fill=tk.X, **pad)
        orow = ttk.Frame(opt)
        orow.pack(fill=tk.X, padx=8, pady=8)
        self.skip_big = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            orow, text="跳过超大文件（大于", variable=self.skip_big
        ).pack(side=tk.LEFT)
        self.max_mb_var = tk.StringVar(value="200")
        self.max_mb_entry = ttk.Spinbox(
            orow, from_=0, to=100000, increment=50, width=7,
            textvariable=self.max_mb_var, state="normal",
        )
        self.max_mb_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(orow, text="MB）").pack(side=tk.LEFT)
        self.skip_big.trace_add(
            "write", lambda *_: self.max_mb_entry.configure(
                state="normal" if self.skip_big.get() else "disabled"
            )
        )
        self.rollback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            orow, text="恢复前生成回滚快照", variable=self.rollback_var
        ).pack(side=tk.LEFT, padx=16)

        # 操作
        act = ttk.Frame(self.root)
        act.pack(fill=tk.X, **pad)
        ttk.Button(act, text="导出备份", command=self.on_export).pack(
            side=tk.LEFT, ipadx=14, ipady=4
        )
        ttk.Button(act, text="还原备份包", command=self.on_import).pack(
            side=tk.LEFT, padx=10, ipadx=14, ipady=4
        )
        ttk.Button(act, text="查看备份包", command=self.on_inspect).pack(
            side=tk.LEFT, ipadx=14, ipady=4
        )

        self.pbar = ttk.Progressbar(self.root, mode="determinate")
        self.pbar.pack(fill=tk.X, padx=12)
        self.status = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=4
        )
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    # ------------------------------------------------------------- helpers --
    def _refresh_items(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.vars.clear()

        missing = 0
        for item in self.items:
            var = tk.BooleanVar(value=item.recommended and item.exists)
            self.vars[item.key] = var
            row = ttk.Frame(self.list_frame)
            row.pack(fill=tk.X, anchor="w", pady=1)
            cb = ttk.Checkbutton(row, text=item.label, variable=var, width=18)
            cb.pack(side=tk.LEFT)
            if not item.exists:
                missing += 1
                cb.state(["disabled"])
                var.set(False)
                tag, color = "（未找到）", "#c00"
            else:
                tag, color = "", "#666"
            ttk.Label(
                row, text="%s %s" % (item.description, tag), foreground=color
            ).pack(side=tk.LEFT, padx=6)

        ok = self.paths.exists
        self.root_hint.config(
            text=(
                "已识别：%s ｜ 共享目录：%s"
                % (self.paths.root, os.path.basename(self.paths.shared))
                if ok
                else "未找到 Qoder 数据目录，请手动指定"
            ),
            foreground="#0a6" if ok else "#c00",
        )
        if missing:
            self.summary.config(text="%d 项未找到" % missing, foreground="#c60")

    def _selected_items(self):
        return [i for i in self.items if self.vars.get(i.key) and self.vars[i.key].get()]

    def _set_all(self, val: bool) -> None:
        for item in self.items:
            if item.exists:
                self.vars[item.key].set(val)

    def _select_recommended(self) -> None:
        for item in self.items:
            self.vars[item.key].set(item.recommended and item.exists)

    def _choose_root(self) -> None:
        d = filedialog.askdirectory(title="选择 Qoder 数据目录")
        if d:
            self.root_var.set(d)
            self._redetect()

    def _make_paths(self, explicit: str | None = None) -> QoderPaths:
        root = explicit or self.adapter.detect_root() or self.adapter.build_default_root()
        return QoderPaths(
            root,
            os.path.join(root, "shared_client")
            if os.path.isdir(os.path.join(root, "shared_client"))
            else os.path.join(root, "sharedclient"),
        )

    def _redetect(self) -> None:
        self.paths = self._make_paths(self.root_var.get() or None)
        self.root_var.set(self.paths.root)
        self.items = self.adapter.build_items(self.paths.root)
        self._refresh_items()
        self._set_status("已重新检测数据目录")

    def _max_mb(self):
        """读取「跳过超大文件」阈值（MB）。**只能在主线程调用**（访问 Tk 变量）。

        返回 ``None`` 表示不限制阈值；否则返回用户设定（>=0）的 MB 数值。
        输入非法时回退到默认 200MB，避免崩溃。
        """
        if not self.skip_big.get():
            return None
        try:
            val = float(self.max_mb_var.get())
        except (ValueError, TypeError):
            val = 200.0
        if val <= 0:
            return None
        return val

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    # ---------------------------------------------------------- 后台任务 --
    def _drain_queue(self) -> None:
        """在主线程消费后台线程的消息，保证 Tk 线程安全。"""
        if self._closing:
            return
        try:
            try:
                while True:
                    kind, payload = self.msg_queue.get_nowait()
                    if kind == "progress":
                        self.pbar["value"] = payload.percent
                        msg = payload.message
                        self._set_status(
                            "%s (%d/%d)" % (msg[:60], payload.current, payload.total)
                        )
                    elif kind == "done":
                        self.busy = False
                        self.pbar["value"] = 0
                        title, text = payload
                        self._set_status(title)
                        messagebox.showinfo(title, text)
                    elif kind == "error":
                        self.busy = False
                        self.pbar["value"] = 0
                        self._set_status("操作失败")
                        messagebox.showerror("失败", str(payload))
                    elif kind == "status":
                        self._set_status(payload)
            except queue.Empty:
                pass
            if not self._closing:
                self._after_id = self.root.after(80, self._drain_queue)
            else:
                self._after_id = None
        except tk.TclError:
            # 窗口已销毁后仍有残留 after 回调触发，忽略即可
            pass

    def _run_bg(self, fn) -> None:
        """
        在后台线程执行 ``fn``。

        ``fn`` 内部**禁止**访问任何 Tk 控件或 Tk 变量（包括 ``self._max_mb()``）；
        所有 Tk 相关取值必须在主线程先取好，通过闭包参数传入。
        结果一律经 ``msg_queue`` 回传主线程处理。
        """
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        self.busy = True
        self.pbar["value"] = 0

        def runner():
            try:
                fn()
            except Exception as exc:  # 兜底，防止后台线程静默崩溃
                self.msg_queue.put(("error", exc))
            finally:
                self.busy = False

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()

    def _progress_cb(self, info: ProgressInfo) -> None:
        # 仅投递到队列，绝不直接碰 Tk
        if not self._closing:
            self.msg_queue.put(("progress", info))

    def _cancel_after(self) -> None:
        """取消尚未触发的 after 回调，避免窗口销毁后报错噪音。"""
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _on_close(self) -> None:
        """关闭窗口：若有任务在跑先确认，避免线程访问已销毁的 Tk。"""
        if self.busy and not messagebox.askyesno(
            "任务进行中", "仍有任务正在执行，强制退出可能产生不完整的文件。\n确定退出？"
        ):
            return
        self._closing = True
        self._cancel_after()
        self.root.destroy()

    # -------------------------------------------------------------- 动作 --
    def _estimate(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showwarning("提示", "请至少勾选一项。")
            return
        self._set_status("正在估算…")
        # 在主线程取好 Tk 相关的值，后台线程只用普通 Python 对象
        max_mb = self._max_mb()
        root_dir = self.paths.root

        def work():
            r = scan_items(items, root_dir, max_file_mb=max_mb)
            self.msg_queue.put(
                (
                    "status",
                    "待备份 %d 个文件，约 %s；已跳过 %d 个（%s）"
                    % (
                        r.file_count,
                        human_size(r.total_bytes),
                        r.skipped_count,
                        human_size(r.skipped_bytes),
                    ),
                )
            )

        self._run_bg(work)

    def on_export(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showwarning("提示", "请至少勾选一项要备份的内容。")
            return

        default = "qoder_backup_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = filedialog.asksaveasfilename(
            title="保存备份文件",
            defaultextension=".zip",
            initialfile=default,
            filetypes=[("Zip 备份", "*.zip")],
        )
        if not zip_path:
            return

        # 主线程先取值，后台线程不碰 Tk
        max_mb = self._max_mb()
        root_dir = self.paths.root

        def work():
            mf = export_backup(
                zip_path,
                items,
                root_dir,
                max_file_mb=max_mb,
                progress=self._progress_cb,
            )
            self.msg_queue.put(
                (
                    "done",
                    (
                        "导出成功",
                        "备份完成！\n\n文件：%s\n包含：%d 个文件\n原始大小：%s\n压缩后：%s"
                        % (
                            mf["zip_path"],
                            mf["file_count"],
                            human_size(mf["total_bytes"]),
                            human_size(mf["zip_bytes"]),
                        ),
                    ),
                )
            )

        self._run_bg(work)

    def on_inspect(self) -> None:
        zip_path = filedialog.askopenfilename(
            title="选择备份文件", filetypes=[("Zip 备份", "*.zip")]
        )
        if not zip_path:
            return
        try:
            info = inspect_backup(zip_path)
        except BackupError as exc:
            messagebox.showerror("无法读取", str(exc))
            return

        mf = info["manifest"] or {}
        text = (
            "文件数：%d\n总大小：%s\n创建时间：%s\n来源目录：%s\n包含模块：%s"
            % (
                info["file_count"],
                human_size(info["total_bytes"]),
                mf.get("created_at", "未知"),
                mf.get("source_root", "未知"),
                "、".join(mf.get("items", [])) or "未知",
            )
        )
        if info["unsafe"]:
            text += "\n\n⚠ 检测到 %d 个非法路径，恢复将被阻止" % len(info["unsafe"])
        messagebox.showinfo("备份包信息", text)

    def on_import(self) -> None:
        zip_path = filedialog.askopenfilename(
            title="选择要恢复的备份文件", filetypes=[("Zip 备份", "*.zip")]
        )
        if not zip_path:
            return
        try:
            info = inspect_backup(zip_path)
        except BackupError as exc:
            messagebox.showerror("无法读取", str(exc))
            return

        if not messagebox.askyesno(
            "确认还原备份包",
            "即将把备份包还原（覆盖写入）到：\n%s\n\n"
            "将自动解压并写入 %d 个文件（%s），无需手动解压。\n\n"
            "请务必先完全退出 Qoder，否则可能导致数据损坏。\n是否继续？"
            % (self.paths.root, info["file_count"], human_size(info["total_bytes"])),
        ):
            return

        # 主线程先取值，后台线程不碰 Tk
        root_dir = self.paths.root
        make_rollback = self.rollback_var.get()

        def work():
            r = import_backup(
                zip_path,
                root_dir,
                progress=self._progress_cb,
                make_rollback=make_rollback,
            )
            extra = "\n回滚快照：%s" % r["rollback"] if r["rollback"] else ""
            if r["blocked"]:
                extra += "\n\n被跳过 %d 项" % len(r["blocked"])
            self.msg_queue.put(
                (
                    "done",
                    (
                        "还原成功",
                        "已还原 %d 个文件（备份包已自动解压覆盖）。%s\n\n请重启 Qoder 验证记忆与会话。"
                        % (r["restored"], extra),
                    ),
                )
            )

        self._run_bg(work)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    QoderBackupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
