"""
AI 工具记忆与会话历史 备份 / 迁移工具（图形界面，统一入口）

运行：
    python -m ai_env_clone        # 源码/开发方式（仓库根目录执行）
    ai_env_clone                  # pip 安装后直接用命令启动（由 pyproject.toml 的 entry_points 生成）

通过 ``ai_env_clone`` 的适配器抽象层接入具体工具（下拉切换），核心逻辑位于
``ai_env_clone.core``，各工具适配器位于 ``ai_env_clone/adapters/``。本文件是
``python -m ai_env_clone`` 与 pip 命令 ``ai_env_clone`` 的唯一起始点，只负责界面与交互。
"""

from __future__ import annotations

import os
import sys
import json
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.compress_estimate import (
    COMPRESS_LEVELS,
    DEFAULT_COMPRESS_LEVEL,
    category_of,
    estimate_compressed_bytes,
    load_calibration,
    save_calibration,
)

from ai_env_clone.core import (
    BackupError,
    ProgressInfo,
    classify_zip_name,
    import_backup,
    inspect_backup,
    list_backup_dir,
    scan_items,
)

APP_TITLE_TPL = "%s 备份迁移工具"  # % (tool_display_name,)
BROWSER_TITLE_TPL = "还原备份/快照"

# 无头开关：单元测试置 True 时隐藏备份浏览器子窗口，避免测试闪窗（仅影响测试）。
HEADLESS = False


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f GB" % n


class QoderBackupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        # 宽度固定、高度动态：初始仅给个合理高度，构建完成后由 _fit_layout()
        # 按实际内容自适应（保证状态栏等完整区域始终可见，不写死过大/过小）。
        root.geometry("738x560")
        root.minsize(700, 460)

        # 工具切换下拉列出所有已注册适配器（新增适配器后自动出现）。
        # 默认工具固定为 qoder（已实测主力工具）；下拉仍列出全部，用户可手动切换。
        self._tool_names = list_adapters()
        default_tool = "qoder" if "qoder" in self._tool_names else (self._tool_names[0] if self._tool_names else "")
        self.adapter = get_adapter(default_tool)
        self.root.title(APP_TITLE_TPL % self.adapter.display_name)

        self.root_dir = self._detect_root()
        self.items = self.adapter.build_items(self.root_dir)
        self.vars: dict[str, tk.BooleanVar] = {}
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._closing = False
        self._worker: threading.Thread | None = None

        self._after_id = None
        self._build_ui()
        self._refresh_items()
        self._refresh_uid_combo()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(80, self._drain_queue)

    # ------------------------------------------------------------------ UI --
    def _on_switch_tool(self, event=None) -> None:
        """切换当前 AI 工具：重设适配器、自动探测新工具数据目录、重建备份内容、同步标题。"""
        disp = self.tool_var.get()
        name = self._tool_display.get(disp)
        if not name or name == self.adapter.name:
            return  # 未变化或映射缺失，忽略
        self.adapter = get_adapter(name)

        # 自动探测新工具的数据目录（B 项确认：覆盖而非保留旧路径）
        self.root_dir = self._detect_root()
        self.root_var.set(self.root_dir)
        # 重建备份内容（不同工具的项结构不同，必须重建）
        self.items = self.adapter.build_items(self.root_dir)
        self.vars.clear()
        self.others_by_uid.clear()
        self.others_master_var.set(False)
        # other_vars 仅在展开“其他用户”区块时创建，切换前可能不存在
        if getattr(self, "other_vars", None) is None:
            self.other_vars = {}
        self.other_vars.clear()

        # 同步所有涉及工具名的标题
        self.root.title(APP_TITLE_TPL % self.adapter.display_name)
        self.dir_frame.config(text="%s 数据目录" % self.adapter.display_name)

        self._refresh_items()
        self._refresh_uid_combo()
        self.status.configure(text="已切换到 %s" % self.adapter.display_name)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # 工具切换：维护者实现新适配器后，下拉即可切换当前 AI 工具
        tool_row = ttk.Frame(self.root)
        tool_row.pack(fill=tk.X, padx=12, pady=(6, 0))
        ttk.Label(tool_row, text="AI 工具：").pack(side=tk.LEFT)
        self.tool_var = tk.StringVar(value=self.adapter.display_name)
        self.tool_combo = ttk.Combobox(
            tool_row, textvariable=self.tool_var, width=18, state="readonly"
        )
        # 下拉显示各工具可读名，内部用 name 映射
        self._tool_display = {
            get_adapter(n).display_name: n for n in self._tool_names
        }
        self.tool_combo["values"] = list(self._tool_display.keys())
        self.tool_combo.bind("<<ComboboxSelected>>", self._on_switch_tool)
        self.tool_combo.pack(side=tk.LEFT, padx=(4, 0))

        # 数据目录
        self.dir_frame = ttk.LabelFrame(
            self.root, text="%s 数据目录" % self.adapter.display_name
        )
        self.dir_frame.pack(fill=tk.X, **pad)
        self.root_var = tk.StringVar(value=self.root_dir)
        row = ttk.Frame(self.dir_frame)
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
        # 当前登录用户 UID（记忆区默认备份对象，下拉切换）
        # 与数据目录识别状态同放一行：左=当前用户，右=识别状态
        uid_row = ttk.Frame(self.dir_frame)
        uid_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Label(uid_row, text="当前用户：").pack(side=tk.LEFT)
        self.uid_var = tk.StringVar()
        self.uid_combo = ttk.Combobox(
            uid_row, textvariable=self.uid_var, width=18, state="readonly"
        )
        self.uid_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.uid_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_uid_selected())
        ttk.Label(
            uid_row,
            text="（自动检测最近活动用户，可下拉切换）",
            foreground="#666",
        ).pack(side=tk.LEFT, padx=(6, 0))
        # 右侧识别状态：仅说明是否已识别，不再重复显示路径（路径见上方输入框）
        self.root_hint = ttk.Label(uid_row, text="", foreground="#666")
        self.root_hint.pack(side=tk.RIGHT)

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
        # 压缩方式档位：快速(1)/正常(6)，透传给 ZipFile.compresslevel。
        # 不再单列"高压缩比"——DEFLATE 在 lvl6 已近最优，lvl6→lvl9 体积几乎无差（CPU 多耗数倍）。
        # 显示名用公共模块的 COMPRESS_LEVELS（档位->名称）拼成下拉项。
        self._compress_levels = {
            "%s（%s）" % (name, "打包快" if lvl == 1 else "推荐"): lvl
            for lvl, name in COMPRESS_LEVELS.items()
        }
        self.compress_var = tk.StringVar(
            value=next(k for k, v in self._compress_levels.items() if v == DEFAULT_COMPRESS_LEVEL)
        )

        self.summary = ttk.Label(bar, text="", foreground="#0a6")
        self.summary.pack(side=tk.RIGHT)
        ttk.Button(bar, text="估算大小", command=self._estimate, width=10).pack(
            side=tk.RIGHT, padx=8
        )

        canvas = tk.Canvas(mid, highlightthickness=0)
        # 高度动态：内容少时收缩、内容多时给足并启用滚动条（见 _fit_layout）。
        sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        # 备份项列表：每行一个 Frame，内部左=勾选+标题(权重3) / 右=说明(权重7)，
        # 同行 grid 保证标题与说明行严格对齐（双独立列堆叠会导致累计错位）。
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.configure(yscrollcommand=sb.set)

        # 关键：让内嵌窗口宽度跟随 canvas 可视宽度，否则内容会被压成一条竖线；
        # 同步把说明列 Label 的 wraplength 设为说明列实际宽度，长文本自动换行。
        def _sync_width(evt=None):
            w = canvas.winfo_width()
            if w > 1:
                canvas.itemconfig("__lf__", width=w)
                # 说明列可用宽度 = canvas 宽 - 列0固定宽(170) - 左侧 padx(6) - 余量
                avail = max(60, w - 170 - 6 - 4)
                for lbl in getattr(self, "_wrap_labels", []):
                    try:
                        lbl.configure(wraplength=avail)
                    except Exception:
                        pass

        canvas.create_window((0, 0), window=self.list_frame, anchor="nw", tags="__lf__")
        canvas.bind("<Configure>", _sync_width)
        self._canvas = canvas
        self._sync_canvas_width = _sync_width
        self._wrap_labels: list = []  # 说明列 Label，随宽度自动换行

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=4)

        # 选项
        opt = ttk.LabelFrame(self.root, text="选项")
        opt.pack(fill=tk.X, **pad)
        # 恢复默认：把选项区域各参数复位到初始默认值，防用户改乱后无从恢复
        btn_row = ttk.Frame(opt)
        btn_row.pack(fill=tk.X, padx=8, pady=(8, 0))
        ttk.Button(
            btn_row, text="恢复默认", command=self._reset_options, width=10
        ).pack(side=tk.LEFT)
        orow = ttk.Frame(opt)
        orow.pack(fill=tk.X, padx=8, pady=8)
        self.skip_big = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            orow, text="跳过超大文件（大于", variable=self.skip_big
        ).pack(side=tk.LEFT)
        self.max_mb_var = tk.StringVar(value="200")
        self.max_mb_entry = ttk.Spinbox(
            orow, from_=0, to=100000, increment=10, width=7,
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
        # 严格校验模式：即使带清单也强制扫描内部结构指纹，防伪造声明文件
        self.strict_var = tk.BooleanVar(value=False)
        strict_cb = ttk.Checkbutton(
            orow, text="严格校验模式", variable=self.strict_var
        )
        strict_cb.pack(side=tk.LEFT, padx=8)
        _Tooltip(
            strict_cb,
            "勾选后，即使压缩包带有声明文件，也会强制扫描内部数据结构指纹并"
            "校验，用于防止伪造声明文件的恶意备份。\n未勾选时仅在缺少声明文件"
            "时才做结构指纹回退识别。",
        )
        # 压缩方式：置于选项区域，与导出行为相关
        ttk.Label(orow, text="压缩方式：").pack(side=tk.LEFT, padx=(8, 2))
        self.compress_combo = ttk.Combobox(
            orow, textvariable=self.compress_var, width=26, state="readonly"
        )
        self.compress_combo["values"] = list(self._compress_levels.keys())
        self.compress_combo.pack(side=tk.LEFT)

        # 操作
        act = ttk.Frame(self.root)
        act.pack(fill=tk.X, **pad)
        ttk.Button(act, text="导出备份", command=self.on_export).pack(
            side=tk.LEFT, ipadx=14, ipady=4
        )
        ttk.Button(act, text="还原备份/快照", command=self.on_import).pack(
            side=tk.LEFT, padx=10, ipadx=14, ipady=4
        )

        self.pbar = ttk.Progressbar(self.root, mode="determinate")
        self.pbar.pack(fill=tk.X, padx=12)
        self.status = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=4
        )
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        # 构建完成后按实际内容自适应窗口高度（保证状态栏等完整区域默认可见）
        self._fit_layout()

    # ------------------------------------------------------------- helpers --
    def _fit_layout(self) -> None:
        """按实际内容自适应窗口高度与备份列表区高度，保证状态栏等完整区域可见。

        - 备份列表区（canvas）：内容少则收缩、内容多则给足并启用滚动条，
          不写死固定高度（避免内容少时大片空白、内容多时被裁剪）。
        - 主窗口：构建完成后按各区块请求高度设定窗口高度（宽度不变），
          不超出屏幕可用高度，确保默认即可看到窗口内全部区域。
        HEADLESS 测试下同样安全（只计算几何、不弹出可见窗口）。
        """
        try:
            self.root.update_idletasks()
        except Exception:
            return
        # 列表内容实际高度（含 others 区块展开后的全部行）
        try:
            content_h = self.list_frame.winfo_reqheight()
        except Exception:
            content_h = 0
        # canvas 高度：内容少则贴合内容、内容多则封顶并滚动（下限避免过扁）
        LIST_MIN, LIST_MAX = 120, 380
        canvas_h = max(LIST_MIN, min(content_h, LIST_MAX))
        try:
            self._canvas.configure(height=canvas_h)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        # 主窗口高度：取内容请求高，封顶到屏幕可用高度的 90%，并满足 minsize
        try:
            want = self.root.winfo_reqheight()
        except Exception:
            want = 560
        try:
            screen = self.root.winfo_screenheight()
        except Exception:
            screen = 900
        win_max = max(520, int(screen * 0.9))
        cur_w = self.root.winfo_width() or 738
        want = max(want, 460)
        want = min(want, win_max)
        self.root.geometry("%dx%d" % (cur_w, want))

    def _reset_options(self) -> None:
        """把选项区域各参数复位到初始默认值，防用户改乱后无从选择。

        默认：跳过超大文件=开、阈值=200MB、生成回滚快照=开、严格校验=关、
        压缩方式=正常档。
        """
        self.skip_big.set(True)
        self.max_mb_var.set("200")
        self.max_mb_entry.configure(
            state="normal" if self.skip_big.get() else "disabled"
        )
        self.rollback_var.set(True)
        self.strict_var.set(False)
        self.compress_var.set(
            next(k for k, v in self._compress_levels.items() if v == DEFAULT_COMPRESS_LEVEL)
        )
        self.status.configure(text="已恢复选项默认设置")

    @staticmethod
    def _agg_prefix(key: str) -> str:
        """聚合前缀：同一逻辑备份项（如 session_db 主库/-wal/-shm，或
        memories_current 的 root/shared 两处）共享的前缀，用于 GUI 去重渲染。"""
        return key.split(":", 1)[0]

    @staticmethod
    def _toggle_var(var, cb) -> None:
        """点击名称文本时切换勾选（ttk.Checkbutton 的 Label 名称部分可点击）。
        直接 invoke 关联 checkbox：tk 会自行切换 variable 并执行 command（一次完成），
        切忌先手动 var.set 再 invoke（有 command 的 checkbox 会被 toggle 两次导致净不变）。
        """
        if str(cb.cget("state")) == "disabled":
            return
        cb.invoke()

    def _refresh_items(self) -> None:
        # 保留上一轮勾选状态（按聚合前缀），重建后恢复：路径识别错误只应让相关项
        # 显示"（未找到）"，不应让备份内容区的选项与用户勾选随识别结果忽有忽无。
        prev_sel = {p: v.get() for p, v in self.vars.items()}
        prev_others = getattr(self, "others_master_var", None)
        prev_others = prev_others.get() if prev_others is not None else None

        # 清空列表与底部"其他用户 UID"区块
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._wrap_labels = []
        self.others_block = ttk.Frame(self.list_frame)  # 底部 UID 区块，稍后 pack 到末尾
        self.vars.clear()
        self.others_by_uid: dict[str, list] = {}

        # 按聚合前缀分组（core 同名逻辑项已用唯一 key 区分路径，此处聚合成一行）
        groups: dict[str, list] = {}
        for item in self.items:
            groups.setdefault(self._agg_prefix(item.key), []).append(item)

        missing = 0
        for prefix, grp in groups.items():
            if prefix in ("memories_others", "user_sessions_others"):
                # 其他用户聚合项（记忆区或集中会话）：按 UID 聚合，稍后合并成一行
                for it in grp:
                    if it.uid:
                        self.others_by_uid.setdefault(it.uid, []).append(it)
                continue

            # 聚合项：任一子项存在即视为"找到"；勾选态优先沿用上一轮用户选择，
            # 仅当该项本次未找到（any_exists=False）时强制不勾选（避免勾选无效项）。
            any_exists = any(it.exists for it in grp)
            first = grp[0]
            if prefix in prev_sel and any_exists:
                default = prev_sel[prefix]
            else:
                default = first.recommended and any_exists
            var = tk.BooleanVar(value=default)
            self.vars[prefix] = var
            is_current = prefix == "memories_current"
            if is_current:
                self.current_var = var

            # 每行一个 Frame，内部 grid 左(勾选+名称,固定宽)/右(说明,占剩余并自动换行)，
            # 同行对齐，避免双独立列堆叠产生的累计错位。
            row = ttk.Frame(self.list_frame)
            row.pack(fill=tk.X, pady=1)
            # 列0固定最小宽度，保证所有行的说明列左边界统一对齐；列1占剩余并自动换行
            row.columnconfigure(0, minsize=170, weight=0)
            row.columnconfigure(1, weight=1)
            # 列0：ttk.Checkbutton(仅指示框) + ttk.Label(名称，可换行)。
            # 不用 tk.Checkbutton 自带的 text+wraplength —— 其 width 与 wraplength 冲突
            # 会在换行处裁掉文字；改为 Label 承载名称，ttk 风格与说明列统一。
            c0 = ttk.Frame(row)
            cb = ttk.Checkbutton(c0, variable=var)
            name_lbl = ttk.Label(
                c0, text=first.label, wraplength=140, justify="left", anchor="w"
            )
            cb.pack(side=tk.LEFT, padx=(0, 4))
            name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            c0.grid(row=0, column=0, sticky="w")
            if is_current:
                cb.configure(command=self._on_current_toggled)
            # 点击名称文本同样能切换勾选
            # 注意：cb 与 v 都必须用默认参数即时捕获，否则闭包会共享循环末尾的变量
            # （others 行复用同名 cb 变量，会导致所有项点击都误触 others 的勾选）。
            name_lbl.bind("<Button-1>", lambda e, v=var, c=cb: self._toggle_var(v, c))
            if not any_exists:
                missing += 1
                cb.configure(state="disabled")
                var.set(False)
                tag, color = "（未找到）", "#c00"
            else:
                tag, color = "", "#666"
            # 聚合说明：只显示一次主描述，避免同前缀多条 description 重复堆叠；
            # 多路径时补一句覆盖数量，信息不失真且不冗余。
            # 去掉 description 中可能带的位置后缀（如"（root 位置）"），避免与覆盖数量重复。
            detail = re.sub(r"\s*（[^）]*位置）", "", first.description)
            if len(grp) > 1:
                detail = "%s（涵盖 %d 处路径）" % (detail.rstrip("。"), len(grp))
            lbl = ttk.Label(
                row, text="%s %s" % (detail, tag),
                foreground=color, anchor="w", justify="left",
                wraplength=max(60, 720 - 170 - 6 - 4),
            )
            lbl.grid(row=0, column=1, sticky="w", padx=(6, 0))
            # 说明列自动换行跟随列宽，避免长文本溢出
            self._wrap_labels.append(lbl)

        # 其他用户记忆区（合并行，默认不勾；勾选后展开 UID 多选）
        # 沿用上一轮勾选（仅当本次确实探测到其他用户时）
        others_default = bool(prev_others) if (prev_others is not None and self.others_by_uid) else False
        self.others_master_var = tk.BooleanVar(value=others_default)
        row = ttk.Frame(self.list_frame)
        row.pack(fill=tk.X, pady=1)
        row.columnconfigure(0, minsize=170, weight=0)
        row.columnconfigure(1, weight=1)
        c0 = ttk.Frame(row)
        cb = ttk.Checkbutton(
            c0, variable=self.others_master_var, command=self._on_others_toggled
        )
        name_lbl = ttk.Label(
            c0, text="其他用户数据", wraplength=140, justify="left", anchor="w"
        )
        cb.pack(side=tk.LEFT, padx=(0, 4))
        name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        c0.grid(row=0, column=0, sticky="w")
        name_lbl.bind(
            "<Button-1>",
            lambda e, v=self.others_master_var, c=cb: self._toggle_var(v, c),
        )
        if not self.others_by_uid:
            cb.configure(state="disabled")
            self.others_master_var.set(False)
        if self.others_by_uid:
            hint = "勾选后展开下方 UID 列表，默认全选其余 %d 个用户" % len(self.others_by_uid)
        else:
            hint = "（未检测到其他用户）"
        lbl = ttk.Label(row, text=hint, foreground="#666", anchor="w", justify="left")
        lbl.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._wrap_labels.append(lbl)

        # 底部 UID 区块放到所有备份项之后
        self.others_block.pack(fill=tk.X, padx=4, pady=(2, 0))

        self._rebuild_others_block()

        # 兜底：重建后强制同步一次内嵌窗口宽度（不依赖 <Configure> 事件是否触发）
        if getattr(self, "_sync_canvas_width", None):
            self.root.after_idle(self._sync_canvas_width)
        # 列表内容变化后重新自适应高度（others 区块展开/收起会改变总高）
        self.root.after_idle(self._fit_layout)

        ok = os.path.isdir(self.root_dir)
        self.root_hint.config(
            text="数据目录已识别" if ok else "未找到数据目录，请手动指定",
            foreground="#0a6" if ok else "#c00",
        )
        if missing:
            self.summary.config(text="%d 项未找到" % missing, foreground="#c60")
        else:
            # 全部找到（含重新检测成功后）：清空上一次可能残留的"未找到"提示
            self.summary.config(text="", foreground="#0a6")

        self._rebuild_others_block()

        # 兜底：重建后强制同步一次内嵌窗口宽度（不依赖 <Configure> 事件是否触发）
        if getattr(self, "_sync_canvas_width", None):
            self.root.after_idle(self._sync_canvas_width)
        # 列表内容变化后重新自适应高度（others 区块展开/收起会改变总高）
        self.root.after_idle(self._fit_layout)

    def _rebuild_others_block(self) -> None:
        """在底部区块按当前勾选状态渲染"其他用户"UID 多选（仅勾选主项时显示）。"""
        for w in self.others_block.winfo_children():
            w.destroy()
        if not self.others_master_var.get() or not self.others_by_uid:
            return
        self.other_vars: dict[str, tk.BooleanVar] = {}
        inner = ttk.LabelFrame(
            self.others_block, text="其他用户 · 选择要备份的 UID"
        )
        inner.pack(fill=tk.X, padx=8, pady=2)
        from ai_env_clone.adapters.codebuddy import short_uid
        for uid in sorted(self.others_by_uid):
            v = tk.BooleanVar(value=True)  # 默认全选其余 UID
            self.other_vars[uid] = v
            row = ttk.Frame(inner)
            row.pack(fill=tk.X, padx=6, pady=1)
            ttk.Checkbutton(row, text=short_uid(uid), variable=v, width=12).pack(side=tk.LEFT)
            ttk.Label(
                row, text="该用户的数据（记忆/会话/规则等）", foreground="#666"
            ).pack(side=tk.LEFT, padx=6)

    def _on_current_toggled(self) -> None:
        # 当前用户记忆区勾选变化无需额外动作，导出时按 var 取值
        pass

    def _on_others_toggled(self) -> None:
        self._rebuild_others_block()

    def _selected_items(self):
        out = []
        for item in self.items:
            prefix = self._agg_prefix(item.key)
            if prefix == "memories_current":
                if getattr(self, "current_var", None) and self.current_var.get():
                    out.append(item)
            elif prefix == "memories_others":
                continue  # 由下方按 UID 过滤处理
            else:
                if self.vars.get(prefix) and self.vars[prefix].get():
                    out.append(item)
        # 其他用户：主项勾选且各 UID 子项勾选才纳入
        if getattr(self, "others_master_var", None) and self.others_master_var.get():
            for uid, items in self.others_by_uid.items():
                if self.other_vars.get(uid) and self.other_vars[uid].get():
                    out.extend(items)
        return out

    def _set_all(self, val: bool) -> None:
        for prefix in self.vars:
            if prefix == "memories_others":
                continue
            # 聚合项存在任一子项才允许勾选
            grp = [it for it in self.items if self._agg_prefix(it.key) == prefix]
            if any(it.exists for it in grp):
                if prefix == "memories_current":
                    self.current_var.set(val)
                else:
                    self.vars[prefix].set(val)
        if self.others_by_uid:
            self.others_master_var.set(val)
            self._rebuild_others_block()
            if val:
                for v in self.other_vars.values():
                    v.set(True)

    def _select_recommended(self) -> None:
        for prefix in self.vars:
            if prefix == "memories_others":
                continue
            grp = [it for it in self.items if self._agg_prefix(it.key) == prefix]
            any_exists = any(it.exists for it in grp)
            first = grp[0]
            if prefix == "memories_current":
                self.current_var.set(first.recommended and any_exists)
            else:
                self.vars[prefix].set(first.recommended and any_exists)
        # 其他用户默认不勾（recommended=False）
        if self.others_by_uid:
            self.others_master_var.set(False)
            self._rebuild_others_block()

    def _choose_root(self) -> None:
        d = filedialog.askdirectory(title="选择 %s 数据目录" % self.adapter.display_name)
        if d:
            self.root_var.set(d)
            self._redetect()

    def _detect_root(self, explicit: str | None = None) -> str:
        """经适配器探测数据根目录；显式指定或被探测到则用，否则回退默认建议路径。"""
        return explicit or self.adapter.detect_root() or self.adapter.build_default_root()

    def _refresh_uid_combo(self, force_clear: bool = False) -> None:
        """刷新当前用户下拉：列出全部 UID，默认选自动检测到的当前用户。

        force_clear=True 用于数据目录未识别（冻结）时：无论 self.items 是否还残留
        上一次有效目录的 UID，都强制清空下拉并禁用，避免目录已无效却仍显示旧用户名。
        """
        uids = (
            []
            if force_clear
            else sorted(
                {it.uid for it in self.items if it.uid}
                | set(self.others_by_uid.keys())
            )
        )
        if force_clear or not uids:
            # 数据目录未识别（或其中无用户）：清空下拉与当前选择，避免残留旧用户名
            self.uid_var.set("")
            self.uid_combo.configure(state="disabled")
            return
        if getattr(self, "current_var", None) and self.current_var.get():
            cur = next(
                (it.uid for it in self.items if self._agg_prefix(it.key) == "memories_current" and it.uid),
                None,
            )
        else:
            cur = None
        # UUID 较长时显示截短串（前若干位 + 省略号），同时维护 短串->真实uid 映射，
        # 供 _on_uid_selected 还原。短串理论上可能碰撞，但 8 位前缀碰撞概率极低，
        # 还原时优先精确匹配、其次前缀匹配、最后回退原串。
        from ai_env_clone.adapters.codebuddy import short_uid  # 延迟导入，避免循环依赖
        self._uid_short_map = {}
        short_values = []
        for u in uids:
            s = short_uid(u)
            self._uid_short_map[s] = u
            short_values.append(s)
        self.uid_combo["values"] = short_values
        detected = getattr(self.adapter, "last_detected_uid", None)
        if not uids:
            # 数据目录未识别（或其中无用户）：清空下拉与当前选择，避免残留旧用户名
            self.uid_var.set("")
            self.uid_combo.configure(state="disabled")
            return
        self.uid_combo.configure(state="readonly")
        if cur:
            self.uid_var.set(short_uid(cur))
        elif detected:
            self.uid_var.set(short_uid(detected))
        else:
            self.uid_var.set(short_values[0])

    def _redetect(self) -> None:
        self.root_dir = self._detect_root(self.root_var.get() or None)
        self.root_var.set(self.root_dir)
        if not os.path.isdir(self.root_dir):
            # 数据目录未识别：冻结备份内容区，保持已有选项与用户勾选不变，
            # 仅提示用户指定正确目录。避免识别错误时清单残缺（行忽有忽无）、
            # 勾选被重置、以及批量"xx项未找到"提示反复出现。
            if self.items:
                self._refresh_uid_combo(force_clear=True)  # 当前用户下拉强制清空
                self.root_hint.config(
                    text="未找到数据目录，请手动指定", foreground="#c00"
                )
                # 整体目录未识别，清空上一次可能残留的"xx项未找到"（不再显示局部未找到）
                self.summary.config(text="", foreground="#0a6")
                self._set_status("未找到数据目录，请手动指定")
                return
            # 从未成功识别过：仍重建一次，生成稳定的占位清单（各项未找到）
        self.items = self.adapter.build_items(self.root_dir)
        self._refresh_items()
        self._refresh_uid_combo()
        self._set_status("已重新检测数据目录")

    def _on_uid_selected(self) -> None:
        """下拉切换当前用户 UID 后，重建记忆区条目（保留其他用户的勾选状态）。"""
        chosen = self.uid_var.get().strip() or None
        # 下拉显示的是截短串，需还原成真实 UUID 再传给适配器
        if chosen and getattr(self, "_uid_short_map", None):
            chosen = self._uid_short_map.get(chosen, chosen)
        self.items = self.adapter.build_items(self.root_dir, current_uid=chosen)
        self._refresh_items()
        # 重建后把下拉同步到新检测到的当前用户（build_items 可能改回自动检测）
        detected = getattr(self.adapter, "last_detected_uid", None)
        new_cur = next(
            (it.uid for it in self.items if it.key == "memories_current" and it.uid),
            detected,
        )
        if new_cur and new_cur != chosen:
            from ai_env_clone.adapters.codebuddy import short_uid
            self.uid_var.set(short_uid(new_cur))
        self._set_status("已切换当前用户：%s" % (chosen or "自动检测"))

    def _tool_dir(self, sub: str) -> str:
        """返回备份/快照存放目录：<备份工具启动目录>/<sub>/<工具名>/。

        即与 run.bat（源码模式）或打包后的 exe 同一级目录下的 backup/<工具名>/，
        按工具名分目录，便于区分不同工具的备份数据。不放在被备份工具
        （如 Qoder）的数据根目录，也不在 ai_env_clone 包内部。
        源码模式下 __main__.py 位于 <仓库根>/ai_env_clone/，故取上级（仓库根）；
        单文件 exe 模式下取 exe 自身所在目录。
        """
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            # __main__.py 在 <仓库根>/ai_env_clone/，仓库根在其上级
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, sub, self.adapter.name)

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

    def _load_compress_calibration(self) -> dict | None:
        """读本工具最近一次真实备份按扩展名反算的实测压缩率。

        校准文件存于系统用户缓存目录（见 ``compress_estimate.cache_dir``），
        按工具分文件，不进仓库、不落入 ``.codebuddy``。无记录或损坏则返回 None。
        若适配器声明 ``supports_calibration=False``，则永远不读取校准，回退到内置经验系数。
        """
        if not self.adapter.supports_calibration:
            return None
        return load_calibration(self.adapter.name, self.adapter.COMPRESS_RATIO)

    def _save_compress_calibration(
        self, bytes_by_ext: dict, bytes_by_ext_compressed: dict
    ) -> None:
        """备份成功后，按扩展名反算实测压缩率并保存到本工具的校准文件。

        比类别级更精细：同类别下不同扩展名（如 .json 不可压 vs .txt 高度可压）
        才能各自贴合实测值。仅统计本次确实出现的扩展名；并按经验区间过滤
        异常值，避免写入的校准文件本身有错（如历史 ``.db``=0.06 那样的污染数据）。
        """
        save_calibration(
            self.adapter.name,
            bytes_by_ext,
            bytes_by_ext_compressed,
            self.adapter.COMPRESS_RATIO,
        )

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
        root_dir = self.root_dir
        compresslevel = self._compress_levels.get(self.compress_var.get(), DEFAULT_COMPRESS_LEVEL)
        compress_name = self.compress_var.get()
        # 读最近一次真实备份按类别反算的实测压缩率，用于按本次勾选组成校准
        calibration = self._load_compress_calibration()

        def work():
            r = scan_items(items, root_dir, max_file_mb=max_mb)
            # 按文件类型分组、用各类别经验压缩率加权，再用实测率整体校准
            comp_bytes = estimate_compressed_bytes(
                r.bytes_by_ext,
                compresslevel,
                per_extension=calibration,
                base_ratios=self.adapter.COMPRESS_RATIO,
            )
            # 仅当确实因"超大文件过滤"跳过大文件时，才在源大小后紧接着提示跳过情况；
            # 因排除规则（日志/临时/WAL 等）被忽略的文件属必要过滤，用户无感，不显示。
            skip_part = (
                "，已跳过 %d 个（%s）" % (r.oversize_count, human_size(r.oversize_bytes))
                if r.oversize_count > 0
                else ""
            )
            self.msg_queue.put(
                (
                    "status",
                    "待备份 %d 个文件，源约 %s%s；按「%s」压缩后约 %s（估算）"
                    % (
                        r.file_count,
                        human_size(r.total_bytes),
                        skip_part,
                        compress_name,
                        human_size(comp_bytes),
                    ),
                )
            )

        self._run_bg(work)

    def on_export(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showwarning("提示", "请至少勾选一项要备份的内容。")
            return

        default = "%s_backup_%s.zip" % (self.adapter.name, datetime.now().strftime("%Y%m%d_%H%M%S"))
        initial_dir = self._tool_dir("backup")
        os.makedirs(initial_dir, exist_ok=True)
        zip_path = filedialog.asksaveasfilename(
            title="保存备份文件",
            defaultextension=".zip",
            initialfile=default,
            initialdir=initial_dir,
            filetypes=[("Zip 备份", "*.zip")],
        )
        if not zip_path:
            return

        # 主线程先取值，后台线程不碰 Tk（_selected_items 读 Tk BooleanVar，必须在此算好）
        max_mb = self._max_mb()
        root_dir = self.root_dir
        compresslevel = self._compress_levels.get(self.compress_var.get(), DEFAULT_COMPRESS_LEVEL)
        sel_items = self._selected_items()

        def work():
            mf = self.adapter.export(
                zip_path,
                root_dir,
                items=sel_items,
                progress=self._progress_cb,
                max_file_mb=max_mb,
                compresslevel=compresslevel,
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
            # 记录本次备份按类别反算的实测压缩率，供后续估算按勾选组成校准
            if self.adapter.supports_calibration and mf.get("bytes_by_ext") and mf.get("bytes_by_ext_compressed"):
                self._save_compress_calibration(
                    mf["bytes_by_ext"], mf["bytes_by_ext_compressed"]
                )

        self._run_bg(work)

    def on_import(self) -> None:
        """打开备份浏览器，由用户浏览后选择还原备份或回滚快照。"""
        self.open_backup_browser()

    def open_backup_browser(self, initial: str | None = None) -> None:
        """打开独立的备份浏览器窗口（左侧列表 + 右侧详情，虚拟加载避免卡顿）。"""
        browser = getattr(self, "_browser", None)
        if browser is not None and browser.top.winfo_exists():
            browser.top.lift()
            browser.top.focus_force()
            if initial is not None:
                browser.select(initial)
            return
        self._browser = BackupBrowser(self, self._tool_dir("backup"), initial=initial)

    def restore_from(self, zip_path: str, expected_kind: str | None = None) -> None:
        """
        从指定备份包还原（供主窗口「还原备份包/回滚快照」及浏览器按钮复用）。

        :param expected_kind: 期望类型，传入后对包内 manifest.kind 做校验，
            防止仅改文件名就被误还原；不一致则弹窗报错并中止。
        """
        strict = self.strict_var.get()
        try:
            info = inspect_backup(
                zip_path, match_structure=self.adapter.match_structure
            )
        except BackupError as exc:
            messagebox.showerror("无法读取", str(exc))
            return

        if not messagebox.askyesno(
            "确认还原备份包",
            "即将把%s还原（覆盖写入）到：\n%s\n\n"
            "将自动解压并写入 %d 个文件（%s），无需手动解压。\n\n"
            "请务必先完全退出 %s，否则可能导致数据损坏。\n是否继续？"
            % (
                "回滚快照" if expected_kind == "rollback" else "备份包",
                self.root_dir,
                info["file_count"],
                human_size(info["total_bytes"]),
                self.adapter.display_name,
            ),
        ):
            return

        # 读取包内 manifest 记录的源路径，与当前目标目录不一致时二次确认，
        # 防止还原到错误目录覆盖掉别的工具/用户的数据。
        manifest = info.get("manifest") or {}
        recorded_root = manifest.get("source_root")
        if recorded_root and os.path.realpath(recorded_root) != os.path.realpath(self.root_dir):
            if not messagebox.askyesno(
                "目标路径不一致，请确认",
                "该备份包记录的数据来源路径为：\n%s\n\n"
                "而当前还原目标为：\n%s\n\n"
                "两者不一致，继续还原可能会覆盖目标目录中已有的数据。\n"
                "确认仍要还原到该目录吗？"
                % (recorded_root, self.root_dir),
            ):
                return

        # 统一识别「压缩包归属工具」，再与当前窗口工具比对：
        #   1) manifest 带 tool 字段 -> 以它为准；
        #   2) 否则（无声明文件，或声明文件未记录 tool）一律回退结构指纹判定，
        #      指纹匹配当前窗口工具才算可还原，不匹配则直接拦截。
        # 任何模式下，识别出的工具与当前窗口工具不一致都弹窗确认，防止误还原。
        pkg_tool = manifest.get("tool")
        if not pkg_tool:
            matched, missing = self.adapter.match_structure(info.get("entries") or [])
            if matched:
                pkg_tool = self.adapter.name
            else:
                messagebox.showerror(
                    "不可还原",
                    "该压缩包缺少有效的工具声明，且内部结构指纹与 %s 不匹配，\n"
                    "无法确认是可还原的备份。\n缺失项：%s"
                    % (
                        self.adapter.display_name,
                        "、".join(missing) or "（无）",
                    ),
                )
                return

        if pkg_tool != self.adapter.name:
            if not messagebox.askyesno(
                "工具类型不一致，请确认",
                "该压缩包识别出的工具为「%s」，\n而当前窗口工具为「%s」。\n\n"
                "两者不一致，继续还原可能写入错误的位置或覆盖其他数据。\n"
                "确认仍要按当前窗口工具还原吗？"
                % (pkg_tool, self.adapter.display_name),
            ):
                return

        root_dir = self.root_dir
        make_rollback = self.rollback_var.get()
        # 回滚快照与备份文件同目录（<备份工具目录>/backup/<工具名>/），方便按时间信息对比选择
        rollback_dir = self._tool_dir("backup")
        os.makedirs(rollback_dir, exist_ok=True)

        def work():
            r = import_backup(
                zip_path,
                root_dir,
                progress=self._progress_cb,
                make_rollback=make_rollback,
                rollback_dir=rollback_dir,
                expected_kind=expected_kind,
                strict=strict,
                match_structure=self.adapter.match_structure,
            )
            extra = "\n回滚快照：%s" % r["rollback"] if r["rollback"] else ""
            if r["blocked"]:
                extra += "\n\n被跳过 %d 项" % len(r["blocked"])
            self.msg_queue.put(
                (
                    "done",
                    (
                        "还原成功",
                        "已还原 %d 个文件（备份包已自动解压覆盖）。%s\n\n请重启 %s 验证记忆与会话。"
                        % (r["restored"], extra, self.adapter.display_name),
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


class BackupBrowser:
    """独立的备份浏览器窗口：左侧虚拟加载文件列表，右侧异步显示所选备份详情。

    - 列表只读取目录元数据（``list_backup_dir``，**不打开/不解压 zip**），秒开不卡顿。
    - 点击某文件后，后台线程读取包信息（默认不校验完整性），右侧面板填充；
      读取期间显示「读取中…」，避免大文件阻塞界面。
    - 用文件名前缀区分「备份」与「回滚快照」并提示二者区别。
    """

    KIND_LABEL = {
        "backup": ("备份", "#1a7f37"),     # 绿：主动导出的完整数据
        "rollback": ("回滚快照", "#b54708"),  # 橙：还原前自动生成的当前数据，可回退
        "unknown": ("其他", "#57606a"),
    }
    CATEGORY_LABEL = {
        "text": "文本/源码",
        "db": "数据库",
        "struct": "索引/结构化",
        "binary": "已压缩/二进制",
        "other": "其他",
    }

    def __init__(self, app: "QoderBackupApp", backup_dir: str, initial: str | None = None):
        self.app = app
        self.backup_dir = backup_dir
        self._reading = False
        self._pending_path: str | None = None
        self._result: dict | None = None

        top = tk.Toplevel(app.root)
        # 创建即隐藏：Toplevel 默认可见，双屏/慢渲染下首帧会闪；正常模式末尾再 deiconify。
        top.withdraw()
        top.title(BROWSER_TITLE_TPL)
        # 窗口宽度收敛：左侧列表按内容自适应(约503px)，右侧详情框请求宽约344px，
        # 二者加边距/sash 约 880；920 使右侧自然贴合内容、不空。
        top.geometry("920x640")
        top.minsize(800, 480)
        self.top = top

        # 顶部说明：区分备份/快照
        hint = ttk.Frame(top)
        hint.pack(fill=tk.X, padx=10, pady=(8, 2))
        ttk.Label(hint, text="备份", foreground="#1a7f37", font=("", 9, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(hint, text="= 主动导出的完整数据；", foreground="#555").pack(
            side=tk.LEFT
        )
        ttk.Label(hint, text="回滚快照", foreground="#b54708", font=("", 9, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(
            hint,
            text="= 还原前自动保存的当前数据，用于还原失败时回退。两者可对比时间选择。",
            foreground="#555",
        ).pack(side=tk.LEFT)

        # 工具条
        bar = ttk.Frame(top)
        bar.pack(fill=tk.X, padx=10, pady=4)
        ttk.Button(bar, text="刷新", command=self._load_list).pack(side=tk.LEFT, padx=(0, 6))
        self.verify_btn = ttk.Button(
            bar, text="校验完整性", command=self._on_verify
        )
        self.verify_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bar, text="全部校验", command=self._on_verify_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.restore_btn = ttk.Button(
            bar, text="还原此备份", command=self._on_restore, state="disabled"
        )
        self.restore_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._restore_tip = _Tooltip(self.restore_btn)
        ttk.Button(bar, text="打开所在目录", command=self._on_open_dir).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="切换目录", command=self._on_change_dir).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.dir_lbl = ttk.Label(bar, text=backup_dir, foreground="#888")
        self.dir_lbl.pack(side=tk.LEFT, padx=8)

        # 主体：左列表 + 右详情
        self.body = ttk.PanedWindow(top, orient=tk.HORIZONTAL)
        self.body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        left = ttk.Frame(self.body)
        right = ttk.Frame(self.body)
        self.body.add(left, weight=1)
        self.body.add(right, weight=1)
        # 左侧区域宽度由 _autosize_columns 按各列实际内容自适应设置，
        # 刚好容纳全部列（无横向滚动），并为右侧详情框留出空间。
        top.update_idletasks()

        # 左侧列表（Treeview，虚拟加载）
        cols = ("name", "kind", "size", "mtime", "verify")
        self.tree = ttk.Treeview(
            left, columns=cols, show="headings", selectmode="browse"
        )
        self.tree.heading("name", text="文件名")
        self.tree.heading("kind", text="类型")
        self.tree.heading("size", text="大小")
        self.tree.heading("mtime", text="修改时间")
        self.tree.heading("verify", text="完整性")
        self.tree.column("name", width=300)
        self.tree.column("kind", width=70, anchor="center")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("mtime", width=140)
        self.tree.column("verify", width=80, anchor="center")
        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        xsb = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=xsb.set)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        xsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # 注意：不使用双击自动还原，避免误操作；只允许点「还原此备份」按钮

        # 右侧详情
        # Text 不设 width 时默认请求 80 字符(~480px)，会把右侧 pane 撑得过宽；
        # 给适中字符宽使右侧自然宽度可控，左侧才能容纳全部列。
        self.detail = tk.Text(
            right, wrap=tk.WORD, state="disabled", padx=10, pady=10, width=46
        )
        self.detail.pack(fill=tk.BOTH, expand=True)

        self._load_list()
        if initial and os.path.isfile(initial):
            self.select(initial)

        # 按模式决定可见性：无头测试保持隐藏，正常模式显示窗口。
        if HEADLESS:
            self.top.withdraw()
        else:
            self.top.deiconify()

    def select(self, path: str) -> None:
        """选中并展示指定备份文件（供主窗口跳转打开指定文件）。"""
        if self.tree.exists(path):
            self.tree.selection_set(path)
            self.tree.see(path)
            self._on_select()

    # ----------------------------------------------------------- 列表加载 --
    def _load_list(self) -> None:
        # 刷新前先快照各文件的完整性校验结果，重建后回填，避免已校验信息被清掉
        prev_verify = {
            item: self.tree.set(item, "verify")
            for item in self.tree.get_children()
        }
        self.tree.delete(*self.tree.get_children())
        rows = list_backup_dir(self.backup_dir)
        if not rows:
            self.tree.insert(
                "", "end", values=("(该目录暂无备份文件)", "", "", "")
            )
            self._set_detail("该目录下还没有任何备份文件。\n\n"
                             "请在主窗口点「导出备份」生成备份，或把已有的备份包 "
                             "（%s_backup_*.zip）放到：\n%s"
                             % (self.app.adapter.name, self.backup_dir))
            return
        for r in rows:
            label, _ = self.KIND_LABEL.get(r["kind"], self.KIND_LABEL["unknown"])
            self.tree.insert(
                "",
                "end",
                iid=r["path"],
                values=(
                    r["name"],
                    label,
                    human_size(r["size"]),
                    datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
                    prev_verify.get(r["path"], ""),  # 已有结果回填，新文件留空待校验
                ),
            )
        self._autosize_columns()

    def _autosize_columns(self) -> None:
        """按内容自动调整各列宽度，并把左侧区域宽度设为刚好容纳全部列（无需横向滚动）。

        列宽策略：先算各列「理想宽度」（表头 + 内容测量，文件名列设上限防撑爆），
        再把分隔条设到理想总宽（受窗口上限约束、为右侧详情框预留最小空间）。
        若理想总宽仍超出左侧可用宽度，优先收缩文件名列，再按比例收缩其余列，
        保证「类型/大小/时间/完整性」列默认可见。
        """
        cols = ("name", "kind", "size", "mtime", "verify")
        # 各列最小必要宽度（表头也要放得下）
        minw = {"name": 120, "kind": 46, "size": 50, "mtime": 60, "verify": 62}
        # 文件名列上限，避免超长文件名把其他列挤掉；需 >= 最长文件名内容宽(216px)+余量
        # 才能完整显示 .zip 扩展名。
        name_max = 224
        cap = {"name": name_max}
        cap.update({c: 160 for c in cols if c != "name"})
        # 用 Treeview 实际渲染字体(TkDefaultFont)度量；否则按默认字体算宽会偏大、留空白。
        measure = tkfont.nametofont("TkDefaultFont").measure

        # 1) 各列理想宽度（暂不限制可用空间）
        ideal = {c: measure(self.tree.heading(c, "text")) for c in cols}
        for item in self.tree.get_children():
            for c in cols:
                val = self.tree.set(item, c)
                if not val:
                    continue
                ideal[c] = max(ideal[c], measure(val))
        for c in cols:
            # 列末余量 8px，避免列后留白过多
            ideal[c] = max(minw[c], min(ideal[c] + 8, cap[c]))
        total_ideal = sum(ideal.values())

        # 2) 左侧区域宽度：需容纳全部列 + 额外开销 pad（滚动条/缩进/边框），
        #    且不超过窗口上限，并为右侧详情框预留最小空间 right_min。
        try:
            top_w = int(self.top.winfo_width()) or 1040
        except Exception:  # noqa: BLE001
            top_w = 1040
        # pad 为列宽之和外的额外开销，确保 verify 列默认可见、不触发横向滚动。
        pad = 12
        right_min = 340  # 右侧详情框最小宽
        max_left = max(sum(minw.values()) + pad, top_w - right_min - 30)
        needed = total_ideal + pad
        if needed <= max_left:
            # 理想列宽全部容纳，无需收缩
            left_w = needed
            widths = dict(ideal)
        else:
            # 理想总宽超窗口上限：优先收缩文件名列，保住其余列默认可见、不触发横向滚动。
            target = max_left - pad
            widths = dict(ideal)
            total = sum(widths.values())
            over = total - target
            shrink = min(widths["name"] - minw["name"], over)
            widths["name"] -= shrink
            over -= shrink
            if over > 0:
                other = [c for c in cols if c != "name"]
                spare = sum(widths[c] - minw[c] for c in other)
                if spare > 0:
                    for c in other:
                        if over <= 0:
                            break
                        cut = min(widths[c] - minw[c], int(over * (widths[c] - minw[c]) / spare))
                        widths[c] -= cut
                        over -= cut
            left_w = target + pad
        try:
            self.body.sashpos(0, left_w)
            self.top.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        # mainloop 启动前直接设 sashpos 会被 PanedWindow 忽略，需在窗口布局完成后(after_idle)再设。
        self.top.after_idle(lambda: self._apply_sash(left_w))
        for c in cols:
            self.tree.column(c, width=widths[c], stretch=False)

    def _apply_sash(self, left_w: int) -> None:
        """窗口布局完成后真正应用 sash 位置（mainloop 前设置会被 PanedWindow 忽略）。"""
        try:
            self.body.sashpos(0, left_w)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------- 选中 -> 读详情 --
    def _on_select(self, _ev=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if not os.path.isfile(path):  # 占位行
            return
        kind = classify_zip_name(path)
        # 备份与回滚快照都可直接还原；无法识别类型（缺清单且文件名不像）则置灰
        if kind in ("backup", "rollback"):
            self.restore_btn.configure(state="normal")
            self._set_restore_tooltip(None)
        else:
            self.restore_btn.configure(state="disabled")
            self._set_restore_tooltip(
                "该文件无法识别为备份或回滚快照（缺少清单且文件名不匹配），"
                "为安全起见不可还原。"
            )
        # 立即显示"读取中"，后台读取，避免大文件卡顿
        self._set_detail("读取中…\n%s" % path)
        self._reading = True
        self._pending_path = path
        threading.Thread(target=self._read_worker, args=(path,), daemon=True).start()
        self.top.after(100, self._poll_result)

    def _read_worker(self, path: str) -> None:
        try:
            self._result = inspect_backup(
                path, verify=False, match_structure=self.app.adapter.match_structure
            )
        except BackupError as exc:
            self._result = {"__error__": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self._result = {"__error__": "读取失败：%s" % exc}

    def _poll_result(self) -> None:
        if self._result is not None:
            res = self._result
            self._result = None
            self._reading = False
            self._render_detail(res, self._pending_path)
            return
        if self._reading:
            self.top.after(100, self._poll_result)

    def _render_detail(self, info: dict, path: str) -> None:
        if "__error__" in info:
            self._set_detail("无法读取该文件：\n%s" % info["__error__"])
            return
        mf = info["manifest"] or {}
        # 以包内 manifest 记录的 kind 为准（文件名可能被改），无则退回文件名判断
        kind = mf.get("kind") or classify_zip_name(path)
        kind_label, kind_color = self.KIND_LABEL.get(kind, self.KIND_LABEL["unknown"])

        # 依据 manifest 实际类型 + 结构指纹校正还原按钮状态
        struct_match = info.get("structure_match")
        struct_missing = info.get("structure_missing") or []
        if kind in ("backup", "rollback"):
            # 有类型声明：缺 manifest kind 时若结构指纹不匹配则置灰
            if mf.get("kind") is None and struct_match is False:
                self.restore_btn.configure(state="disabled")
                self._set_restore_tooltip(
                    "该压缩包缺少清单文件，且内部结构不是 %s 的数据结构，"
                    "无法确认是可还原备份。\n缺失项：%s"
                    % (self.app.adapter.display_name, "、".join(struct_missing) or "（无）")
                )
            else:
                self.restore_btn.configure(state="normal")
                self._set_restore_tooltip(None)
        else:
            # 无类型声明：依赖结构指纹回退识别
            if struct_match:
                self.restore_btn.configure(state="normal")
                self._set_restore_tooltip(None)
            else:
                self.restore_btn.configure(state="disabled")
                miss_txt = "、".join(struct_missing) or "（无）"
                self._set_restore_tooltip(
                    "无法识别为 %s 的备份或回滚快照（缺少清单且内部结构不匹配），"
                    "为安全起见不可还原。\n缺失项：%s"
                    % (self.app.adapter.display_name, miss_txt)
                )

        has_manifest = info.get("has_manifest")
        kind_inferred = info.get("kind_inferred")
        lines = []
        lines.append("【%s】%s" % (kind_label, os.path.basename(path)))
        lines.append("【类型说明】%s"
                     % ("主动导出的完整数据，可直接还原。"
                        if kind == "backup"
                        else "还原前自动保存的当前数据，用于还原失败时回退，也可直接还原。"))
        lines.append("")
        lines.append("【文件数】%d" % info["file_count"])
        lines.append("【原始数据大小】%s （=将被备份的源文件大小，非磁盘占用）"
                     % human_size(info["total_bytes"]))
        lines.append("【创建时间】%s" % mf.get("created_at", "未知（清单未记录）"))
        lines.append("【来源目录】%s" % mf.get("source_root", "未知（清单未记录）"))
        items = mf.get("items", [])
        # 模块名均为英文字符：遵循英文惯例，用「逗号+空格」分隔（而非中文顿号），
        # 标点与下一个模块名之间保留一个空格，阅读更自然。
        lines.append("【包含模块】%s" % (", ".join(items) if items else "未知（清单未记录）"))
        # 类型识别：始终分两行展示「文件类型」与「所属 AI 工具类型」，并标注各自识别来源。
        # 不论有无声明文件，两行都出现，避免「一会儿有一会儿没」看的人发蒙。
        # 1) 文件类型(kind) 来源
        if has_manifest and not kind_inferred:
            kind_src = "声明文件记录"
        elif kind_inferred:
            kind_src = "声明文件缺类型字段，按文件名推断"
        elif not has_manifest and struct_match:
            kind_src = "无声明文件，结构指纹回退识别"
        else:
            kind_src = "无法识别"
        lines.append("【文件类型】%s（%s）" % (kind_label, kind_src))
        # 2) 所属 AI 工具类型 来源
        mf_tool = (mf.get("tool") or "").strip() if isinstance(mf, dict) else ""
        if mf_tool:
            tool_src = "声明文件记录"
            tool_name = self.app.adapter.display_name if mf_tool == self.app.adapter.name else mf_tool
        elif not has_manifest and struct_match:
            tool_src = "结构指纹回退识别"
            tool_name = self.app.adapter.display_name
        else:
            tool_src = "既无声明也无匹配的结构指纹"
            tool_name = "未知"
        lines.append("【所属工具】%s（%s）" % (tool_name, tool_src))

        # 按文件类型归类展示
        by_cat: dict[str, int] = {}
        for ext, size in info["bytes_by_ext"].items():
            by_cat[category_of(ext)] = by_cat.get(category_of(ext), 0) + size
        if by_cat:
            lines.append("")
            lines.append("文件类型分布（按源大小）：")
            for cat in ("text", "db", "struct", "binary", "other"):
                if cat in by_cat:
                    lines.append("  · %s：%s"
                                 % (self.CATEGORY_LABEL.get(cat, cat),
                                    human_size(by_cat[cat])))

        if info["unsafe"]:
            lines.append("")
            lines.append("⚠ 检测到 %d 个非法路径，恢复将被阻止" % len(info["unsafe"]))
        lines.append("")
        lines.append("提示：点「校验完整性」可逐文件校验（大文件较慢，结果在左侧「完整性」列）；"
                     "点「还原此备份」可恢复。")

        self._set_detail("\n".join(lines), title_color=kind_color)

    # ----------------------------------------------------------- 工具按钮 --
    def verify_one(self, path: str) -> None:
        """对单个备份文件异步执行完整性校验（独立线程，结果回写对应行）。"""
        if not os.path.isfile(path):
            return
        # 在左侧「完整性」列标记校验中，结果持久保留（切换选择仍可见）
        self.tree.set(path, "verify", "校验中…")
        if path == self.tree.selection()[0] if self.tree.selection() else None:
            self._set_detail("校验中（大文件可能较慢）…\n%s" % path)

        def worker() -> None:
            try:
                res = inspect_backup(path, verify=True)
                bad = res.get("corrupted")
                if bad is None:
                    mark, msg = "✓ 通过", "✓ 完整性校验通过，未发现损坏文件。"
                else:
                    mark, msg = "✗ 损坏", "✗ 备份包已损坏，首个损坏文件：%s" % bad
            except BackupError as exc:
                mark, msg = "✗ 错误", "无法读取：%s" % exc
            except Exception as exc:  # noqa: BLE001
                mark, msg = "✗ 错误", "校验失败：%s" % exc
            # 回主线程更新对应行，避免多线程直接操作 UI
            self.top.after(0, self._apply_verify_result, path, mark, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_verify(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧列表选择要校验的备份文件。")
            return
        self.verify_one(sel[0])

    def _on_verify_all(self) -> None:
        kids = self.tree.get_children()
        if not kids:
            messagebox.showinfo("提示", "当前目录下没有可校验的备份文件。")
            return
        for path in kids:
            self.verify_one(path)

    def _apply_verify_result(self, path: str, mark: str, msg: str) -> None:
        if self.tree.exists(path):
            self.tree.set(path, "verify", mark)
        # 仅当该文件仍是当前选中项时，把结论追加到右侧详情，避免写错文件
        if self.tree.selection() and self.tree.selection()[0] == path:
            self._set_detail(self.detail.get("1.0", "end").rstrip() + "\n\n" + msg)

    def _on_restore(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        if not os.path.isfile(path):
            return
        # 以包内 manifest 的 kind 为准（防止仅改文件名被误还原/误拒）
        expected_kind = None
        try:
            info = inspect_backup(path, verify=False)
            mf = info.get("manifest") or {}
            expected_kind = mf.get("kind") or classify_zip_name(path)
        except BackupError:
            expected_kind = classify_zip_name(path)
        if expected_kind not in ("backup", "rollback"):
            messagebox.showinfo(
                "不可还原",
                "该文件无法识别为备份或回滚快照（缺少清单且文件名不匹配），"
                "为安全起见不能还原。",
            )
            return
        self.top.withdraw()
        try:
            self.app.restore_from(path, expected_kind=expected_kind)
        finally:
            self.top.deiconify()

    def _on_open_dir(self) -> None:
        # 打开所选备份文件所在目录；未选择时退回备份根目录
        target = self._selected_dir()
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            messagebox.showinfo("打开目录", target)

    def _selected_dir(self) -> str:
        """返回应打开的目录：有选中文件则取该文件所在目录，否则用备份根目录。"""
        path = self._pending_path
        if path and os.path.isfile(path):
            return os.path.dirname(path)
        return self.backup_dir

    def _on_change_dir(self) -> None:
        """切换到其他存放备份/快照的目录（备份文件不一定在默认目录下）。"""
        new_dir = filedialog.askdirectory(
            title="选择备份/快照所在目录", initialdir=self.backup_dir
        )
        if not new_dir:
            return
        self.backup_dir = new_dir
        self.top.title(BROWSER_TITLE_TPL)
        self.dir_lbl.config(text=new_dir)
        self._set_detail("已切换到目录：\n%s\n\n正在加载该目录下的备份文件…" % new_dir)
        self._load_list()

    # ------------------------------------------------------------- 工具 --
    def _set_restore_tooltip(self, text: str | None) -> None:
        """更新「还原此备份」按钮的悬停提示；传 None 则清空（按钮可用时不显示）。"""
        self._restore_tip.set_text(text or "")

    def _set_detail(self, text: str, title_color: str | None = None) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", tk.END)
        if not text:
            self.detail.insert("1.0", "（未选择备份文件）\n")
        else:
            self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")


class _Tooltip:
    """极简悬停提示：进入控件显示，离开隐藏；可随时更新文本。"""

    def __init__(self, widget: tk.Widget, text: str = ""):
        self.widget = widget
        self.text = text
        self.win = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<Destroy>", self._hide)

    def set_text(self, text: str) -> None:
        self.text = text
        if self.win:
            self._hide()

    def _show(self, _e=None) -> None:
        if self.win or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        self.win.wm_geometry("+%d+%d" % (x, y))
        t = tk.Label(
            self.win, text=self.text, justify=tk.LEFT,
            background="#fffbe6", relief=tk.SOLID, borderwidth=1,
            wraplength=320, padx=6, pady=4, font=("", 9),
        )
        t.pack()

    def _hide(self, _e=None) -> None:
        if self.win:
            self.win.destroy()
            self.win = None


if __name__ == "__main__":
    main()
