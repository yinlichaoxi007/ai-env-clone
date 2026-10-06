"""
AI 工具记忆与会话历史 备份 / 迁移工具（图形界面，统一入口）

运行：
    python -m ai_env_clone            # 源码/开发方式（仓库根目录执行），启动图形界面
    python -m ai_env_clone --version  # 只打印版本号后退出（-V 亦可），不启动界面
    ai_env_clone                      # 安装为命令后直接启动

版本号定义在 ``ai_env_clone/version.py``（唯一来源），窗口标题与 ``--version``
输出都由它派生；改版本只改那一处。

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
import shutil
import subprocess
import threading
import webbrowser
from datetime import datetime

from ai_env_clone import version as _version
from ai_env_clone.version import app_title, handle_version_flag
from ai_env_clone.doctext import (
    _TABLE_MAX_WIDTH,
    handle_docs_flag,
    md_to_paragraphs,
    plain_text,
)
from ai_env_clone.resources import read_help_doc, resource_path
from ai_env_clone.updater import (
    check_update,
    describe_result,
    handle_check_update_flag,
    platform_asset_name,
)
from ai_env_clone import prefs as _prefs

# 版本查询必须先于 import tkinter 生效：版本号不该依赖 GUI 库——CI、最小化容器、
# 只做版本核对的脚本可能没装 tkinter（本项目的受管 Python 就没有）。命中即退出，
# 界面完全不启动。这段顺序由 tests/test_version.py 断言，别挪到 tkinter 之后。
if handle_version_flag(sys.argv[1:]):
    raise SystemExit(0)

# 「打印使用说明」同理不该依赖 GUI 库：CLI 只能输出纯文本，而使用说明菜单
# （帮助 → 使用说明）读的也是同一份渲染结果，口径一致性由「共用纯函数」保证。
if handle_docs_flag(sys.argv[1:]):
    raise SystemExit(0)

# 「检查更新」同理不依赖 GUI 库；退出码 0=有新版本 / 1=已是最新 / 2=检查失败。
_rc = handle_check_update_flag(sys.argv[1:])
if _rc is not None:
    raise SystemExit(_rc)

import tkinter as tk  # noqa: E402 - 必须在版本查询之后导入，见上
import tkinter.font as tkfont  # noqa: E402
from tkinter import filedialog, messagebox, scrolledtext, ttk  # noqa: E402

from ai_env_clone.adapters import get_adapter, list_adapters
from ai_env_clone.adapters.base import (
    MULTI_MACHINE_CYCLE_HINT,
    MULTI_MACHINE_CYCLE_HINT_MERGE,
)
from ai_env_clone import (
    archive_source,
    backup_scan,
    import_matrix,
    session_migration,
    workspace_plan,
)
from ai_env_clone.compress_estimate import (
    COMPRESS_LEVELS,
    DEFAULT_COMPRESS_LEVEL,
    cache_dir as _cache_dir,
    category_of,
    estimate_compressed_bytes,
    load_calibration,
    save_calibration,
)

from ai_env_clone.core import (
    BackupError,
    ProgressInfo,
    classify_zip_name,
    companion_notes,
    import_backup,
    inspect_backup,
    manifest_origin_info,
    origin_info_lines,
    scan_items,
    session_workspaces_lines,
)

# 主窗口标题模板（含版本号）定义在 ai_env_clone/version.py，见上方 import。
BROWSER_TITLE_TPL = "还原备份/快照"


def _clamp(v: float, lo: float, hi: float) -> float:
    """把 ``v`` 夹到 ``[lo, hi]``（弹性高度的上下限保护）。"""
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------- #
# 布局常量：主窗口高度自适应（见 QoderBackupApp._relayout）
# --------------------------------------------------------------------------- #
#: 备份项列表区的弹性高度范围（px）。自然值 = 默认观感高度；窗口富余时按比例
#: 向上放大（上限避免一屏只剩列表），窗口不足时按比例向下收缩（下限保证仍能
#: 看清约 4~5 行），再不足则由内容区竖向滚动条兜底。
_LIST_H_NATURAL = 285
_LIST_H_MIN = 150
_LIST_H_MAX = 560
#: 数据导入说明区（只读文本）的弹性行数范围。基准 4 行，随窗口高度在 2~8 行间伸缩。
_IMP_ROWS_NATURAL = 4
_IMP_ROWS_MIN = 2
_IMP_ROWS_MAX = 8
#: 主窗口**首次打开**时的默认高度 = 屏幕高的该比例。用「比例」而非固定像素，
#: 因此任意 DPI 缩放（100% / 125% / 150% …）下窗口占屏幕的视觉比例恒定；
#: 三工具（Qoder / CodeBuddy / Reasonix …）默认高度完全一致，不随内容多少变化
#: ——内容少的工具不会出现「窗口缩成一小条」的观感。之后窗口尺寸完全由用户
#: 与窗口管理器控制（见 `_autosize_window`）。
#:
#: ★ **只看屏幕、绝不看内容**（2026-10-03 用户定策）：一度改成「内容排不下就抬到
#: 上限」，但抬出来的窗口会顶到桌面边界——低分辨率 + 高 DPI 缩放的屏幕上控件按
#: 比例放大而屏幕物理尺寸不变，窗口反而可能**装不上屏幕**，连标题栏、状态栏
#: 都被挤出去，直接没法用。取舍很明确：**优先保证窗口完整可见，其次才是内容
#: 完整可见**；内容装不下由内容区自己的滚动条承载（用户已认可该行为）。
_WINDOW_H_DEFAULT_RATIO = 0.75
#: 上限兜底（非 Windows / 取不到工作区时用）：屏幕高的该比例。当前
#: `_WINDOW_H_DEFAULT_RATIO < _WINDOW_H_SCREEN_RATIO`，此项只在将来调大默认
#: 比例时起兜底作用。
_WINDOW_H_SCREEN_RATIO = 0.92
#: 默认高度的下限（px）：屏幕极矮时也要保证操作区可用。
_WINDOW_H_MIN = 460


def _work_area_height() -> int:
    """Windows 桌面工作区高度（已扣除任务栏）；非 Windows / 取不到时返回 0。

    ★ 用工作区而不是 ``winfo_screenheight()`` 当上限：屏幕高**包含任务栏**占用的
    那一条，按屏幕高开窗会让窗口底部（正好是状态栏）被任务栏压住。必须与
    `_enable_dpi_awareness()` 配合——进程声明 DPI 感知后该值才是物理像素，
    与 ``geometry`` 同量纲。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
            0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            h = int(rect.bottom - rect.top)
            return h if h > 1 else 0
    except Exception:
        pass
    return 0


def _window_height_ceiling(screen: int) -> int:
    """默认窗口高度的上限：优先**真实工作区**（已扣任务栏），取不到才按屏幕比例。

    这是「优先保证窗口完整可见」这条定策的落点：比例是**估计值**，工作区是
    **实测值**。屏高 864 / 任务栏 48 时两者给 795 与 816，都安全；但任务栏特别高
    或屏幕特别矮时，比例会算出超过可用高度的值、把窗口顶出桌面——取实测值即从
    结构上排除这种情况。
    """
    wa = _work_area_height()
    if wa >= _WINDOW_H_MIN:
        return wa
    return max(_WINDOW_H_MIN, int(screen * _WINDOW_H_SCREEN_RATIO))


# --------------------------------------------------------------------------- #
# 子窗口（Toplevel 弹窗）默认尺寸夹紧 + 竖向滚动兜底
# --------------------------------------------------------------------------- #
#: 弹窗默认高度下限（px）：再矮也要保证正文与底部按钮可用。
_DIALOG_H_MIN = 360
#: 弹窗高度上限相对**工作区**留的边距（px）：不贴着任务栏 / 屏幕边，避免底部按钮
#: 与窗口边框重叠或被遮挡。
_DIALOG_H_MARGIN = 48
#: 取不到工作区（非 Windows / 调用失败）时的兜底上限系数（相对屏幕高）。
_DIALOG_H_SCREEN_RATIO = 0.9

#: 自带滚动能力的控件：外层滚动容器遇到它们时**放行**，交给它们各自处理，
#: 避免「外层抢走滚轮、内层滚不动」（与主窗口 ``_on_content_wheel`` 同一判据）。
_SELF_SCROLLING = (tk.Text, tk.Listbox, tk.Canvas, ttk.Treeview)


def _clamp_dialog_size(win: tk.Toplevel, width: int, height: int) -> tuple[int, int]:
    """把弹窗默认尺寸夹进工作区，避免矮屏 / 高 DPI 下窗口超出屏幕、底部被裁。

    与主窗口同一取舍（见 ``_window_height_ceiling``）：**优先保证窗口完整可见**，
    内容装不下由内容区的滚动条承载（见 :class:`_ScrollBody`）。只夹**默认**尺寸——
    之后仍由用户与窗口管理器控制，程序不再改写。
    """
    try:
        screen_h = int(win.winfo_screenheight())
    except Exception:
        screen_h = 900
    try:
        screen_w = int(win.winfo_screenwidth())
    except Exception:
        screen_w = 1280
    wa = _work_area_height()
    cap_h = (wa - _DIALOG_H_MARGIN) if wa > _DIALOG_H_MIN \
        else int(screen_h * _DIALOG_H_SCREEN_RATIO)
    cap_h = max(_DIALOG_H_MIN, cap_h)
    h = int(_clamp(height, _DIALOG_H_MIN, cap_h))
    w = int(min(width, max(320, screen_w - 40)))
    try:
        win.geometry("%dx%d" % (w, h))
    except tk.TclError:
        pass
    return w, h


class _ScrollBody:
    """弹窗用的竖向滚动容器：内容放进 :attr:`frame`，超出可视高才出现滚动条。

    与主窗口内容区同一套做法（canvas + 内嵌 frame + 必要时才显示竖向滚动条），
    只是省掉了「弹性高度」——弹窗内容高度由子控件自然决定，装不下就滚动。
    滚轮由调用方绑到弹窗 Toplevel 上（子控件的事件会冒泡到 Toplevel）。
    """

    def __init__(self, parent: tk.Misc, row: int = 0, column: int = 0) -> None:
        self.outer = ttk.Frame(parent)
        self.outer.grid(row=row, column=column, sticky="nsew")
        self.outer.rowconfigure(0, weight=1)
        self.outer.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.outer, highlightthickness=0, bd=0, takefocus=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.sb = ttk.Scrollbar(self.outer, orient="vertical",
                                command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.frame = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self._visible = False
        self.frame.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    # -- 布局联动 --
    def sync(self) -> None:
        """内容高度变化后手动同步一次（构建完成后调用，避免首帧判断滞后）。"""
        self._on_inner()

    def _on_inner(self, _event=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            return
        self._sync_bar()

    def _on_canvas(self, event) -> None:
        # 内嵌 frame 跟随可视宽：否则长内容会把自身撑宽、右侧被裁。
        try:
            self.canvas.itemconfigure(self._win, width=event.width)
        except tk.TclError:
            return
        self._sync_bar()

    def _sync_bar(self) -> None:
        need = self._needs_bar()
        if need == self._visible:
            return
        self._visible = need
        if need:
            self.sb.grid(row=0, column=1, sticky="ns")
        else:
            self.sb.grid_remove()

    def _needs_bar(self) -> bool:
        try:
            return self.frame.winfo_reqheight() > self.canvas.winfo_height()
        except tk.TclError:
            return False

    @property
    def scrollable(self) -> bool:
        """当前是否处于「内容超出可视高、滚动条已启用」状态。"""
        return self._visible

    def wheel(self, event):
        """滚轮处理（绑到弹窗 Toplevel）。内层自带滚动条时放行、不抢。"""
        if not self._visible:
            return None
        if isinstance(event.widget, _SELF_SCROLLING):
            return None
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            delta = -1
        else:
            delta = 1
        try:
            self.canvas.yview_scroll(delta, "units")
        except tk.TclError:
            return None
        return "break"


# 无头开关：单元测试置 True 时隐藏备份浏览器子窗口，避免测试闪窗（仅影响测试）。
HEADLESS = False


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f GB" % n


# --------------------------------------------------------------------------- #
# 还原方式（「合并 / 全覆盖」二选一）—— 文案与取值抽成纯函数，弹窗只负责渲染
# --------------------------------------------------------------------------- #
# 方案 §6.1 的实现注意：确认框改成自建对话框后，若把标题与正文写在对话框类里，
# 「文案对不对」就再没人测了。故此处把**标题 / 正文 / 单选项**都抽成模块级纯函数，
# 对话框与测试共用同一份输出（见 tests/test_restore_prompts.py）。
RESTORE_MODE_MERGE = "merge"
RESTORE_MODE_REPLACE = "replace"


def restore_mode_options(
    *, merge_supported: bool, merge_notice: str, replace_notice: str
) -> list[tuple[str, str, str]]:
    """返回还原方式单选项 ``[(value, label, description), ...]``（纯函数）。

    - 支持增量合并：两项，「合并（推荐）」在前（默认选中，方案 §9 第 7 条）；
    - 不支持：只返回「全覆盖」一项 —— 界面据此**不显示单选区**（方案 §6.1）。
    """
    replace = (RESTORE_MODE_REPLACE, "全覆盖", replace_notice)
    if not merge_supported:
        return [replace]
    return [(RESTORE_MODE_MERGE, "合并（推荐）", merge_notice), replace]


def restore_confirm_text(
    *,
    kind_label: str,
    root_dir: str,
    file_count: int,
    total_size: str,
    origin_line: str,
    display_name: str,
    notice: str = "",
) -> tuple[str, str]:
    """返回「确认还原备份包」弹窗的 ``(标题, 正文)``（纯函数，便于单测断言文案）。

    :param notice: 正文里交代的还原语义说明。**不支持**合并的工具把「全覆盖」说明
        直接放这里（只有一种方式，无需单选）；**支持**合并的工具传空串 —— 语义随
        单选项一起渲染（见 :func:`restore_mode_options`），避免同一段话说两遍。
    """
    body = (
        "即将把%s还原到：\n%s\n\n"
        "将自动解压并写入 %d 个文件（%s），无需手动解压。\n"
        "%s"
        "%s"
        "\n请务必先完全退出 %s，否则可能导致数据损坏。\n是否继续？"
        % (
            kind_label,
            root_dir,
            file_count,
            total_size,
            origin_line,
            (notice + "\n") if notice else "",
            display_name,
        )
    )
    return "确认还原备份包", body


class RestoreModeDialog:
    """还原方式二选一（合并 / 全覆盖）弹窗；返回 ``"merge"`` / ``"replace"``，取消为 ``None``。

    与其它子窗口同样的取舍：默认尺寸夹进工作区，内容超出可视高时竖向滚动
    （见 :class:`_ScrollBody`）。只有支持增量合并的工具才渲染单选区。
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        body: str,
        options: list[tuple[str, str, str]],
        default: str,
    ) -> None:
        self.result: str | None = None
        self._var = tk.StringVar(value=default)
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        _clamp_dialog_size(self.win, 640, 540)
        self.win.transient(parent)
        self.win.rowconfigure(0, weight=1, minsize=0)
        self.win.columnconfigure(0, weight=1)
        self._scroll = _ScrollBody(self.win, row=0, column=0)
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.win.bind(_seq, self._scroll.wheel)
        frame = self._scroll.frame
        ttk.Label(
            frame, text=body, justify="left", anchor="w", wraplength=580
        ).pack(fill=tk.X, padx=12, pady=(12, 8))
        for value, label, desc in options:
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, padx=12, pady=(2, 4))
            ttk.Radiobutton(
                row, text=label, value=value, variable=self._var
            ).pack(anchor="w")
            ttk.Label(
                row, text=desc, justify="left", anchor="w", wraplength=556,
                foreground="#555555",
            ).pack(anchor="w", padx=(22, 0))
        btns = ttk.Frame(self.win)
        btns.grid(row=1, column=0, sticky="e", padx=12, pady=(4, 12))
        ttk.Button(btns, text="确定", command=self._ok, width=10).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="取消", command=self._cancel, width=10).pack(side=tk.LEFT)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self._scroll.sync()
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        self.win.wait_window()

    def _ok(self) -> None:
        self.result = self._var.get()
        self.win.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.win.destroy()


def ask_restore_mode(
    parent: tk.Misc,
    *,
    title: str,
    body: str,
    options: list[tuple[str, str, str]],
    default: str = RESTORE_MODE_MERGE,
) -> str | None:
    """弹出还原方式选择；返回 ``"merge"`` / ``"replace"``，取消返回 ``None``。

    无头测试下退化为 ``messagebox.askyesno``（「是」= 默认方式）——自建弹窗会
    ``grab_set()`` + ``wait_window()`` 阻塞等点击，在无头测试里会永久挂起。
    """
    if HEADLESS:
        head = body + "\n\n" + "\n\n".join(
            "%s：%s" % (label, desc) for _value, label, desc in options
        )
        return default if messagebox.askyesno(title, head, parent=parent) else None
    return RestoreModeDialog(
        parent, title=title, body=body, options=options, default=default
    ).result


# --------------------------------------------------------------------------- #
# 用户偏好缓存（记住上次选择的工具，下次启动保持）
# --------------------------------------------------------------------------- #
# 实现已抽到 ``ai_env_clone/prefs.py``：``--docs`` / ``--check-update`` 这些
# CLI 分支生效在 ``import tkinter`` 之前，也要读「更新设置」（代理、通道），
# 所以偏好读写不能留在本文件（它 import 了 tkinter）。
# ★ 那里改成了**读-改-写**（深度合并）：旧实现 ``json.dump({"last_tool": …})``
# 是整文件覆盖，直接复用会把同文件的 ``update`` 段抹掉。
_PREFS_FILENAME = _prefs.PREFS_FILENAME


def _prefs_path() -> str:
    """偏好缓存文件完整路径（转发到 ``prefs.prefs_path``）。"""
    return _prefs.prefs_path()


def _load_last_tool() -> str | None:
    """读用户上次选择的工具标识；无缓存 / 损坏 / 该工具已注销则返回 None（回退默认）。"""
    return _prefs.load_last_tool()


def _save_last_tool(name: str) -> None:
    """记录用户当前选择的工具，供下次启动保持。写失败静默，不影响主流程。"""
    _prefs.save_last_tool(name)


class QoderBackupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        # 宽度固定、高度动态：初始仅给个合理高度，构建完成后由 _fit_layout()
        # 按实际内容自适应（保证状态栏等完整区域始终可见，不写死过大/过小）。
        # 宽度按屏幕宽收窄：常规屏 960，窄屏（如 1024/800）不让窗口超出屏幕，
        # 下限 720 —— 再窄时内容区会出现横向滚动条兜底，被挤出的部分仍可查看。
        try:
            _sw = int(root.winfo_screenwidth())
        except Exception:
            _sw = 1280
        _init_w = max(720, min(960, _sw - 80))
        root.geometry("%dx560" % _init_w)
        # 最小宽度只保证主要按钮行排得开，**不再按内容请求宽卡死 940**：
        # 需要更窄时，内容区自动出现横向滚动条（各区域内容完整可查看），
        # 否则小屏用户既拖不窄、又被裁掉右侧，反而没法用。
        root.minsize(min(680, _init_w), 460)

        # 工具切换下拉列出所有已注册适配器（新增适配器后自动出现）。
        # 默认工具：优先沿用用户上次选择（缓存），无缓存/失效时按注册顺序取第一个。
        self._tool_names = list_adapters()
        last_tool = _load_last_tool()
        default_tool = last_tool or (self._tool_names[0] if self._tool_names else "")
        self.adapter = get_adapter(default_tool)
        self.root.title(app_title(self.adapter.display_name))

        self.root_dir = self._detect_root()
        self.items = self.adapter.build_items(self.root_dir)
        self.vars: dict[str, tk.BooleanVar] = {}
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._closing = False
        self._worker: threading.Thread | None = None

        self._after_id = None
        # DSH / WorkBuddy 修复完成后自动复检的那一次 after：**必须单独记住句柄**，否则窗口
        # 在它触发前被关闭 ⇒ Tk 报 "invalid command name ..._dsh_check"（实测残留
        # 噪音就是这么来的）。_after_id 被 _drain_queue 自己占用，不能复用。
        self._dsh_check_job = None
        self._wb_check_job = None
        self._build_ui()
        self._refresh_items()
        self._refresh_uid_combo()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 兜底：无论窗口被谁销毁（关窗 / 外部 destroy / 测试拆卸），都清掉待触发的
        # after 回调，避免 Tk 报 "invalid command name ..." 噪音（见 _on_root_destroy）。
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
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
        self.root.title(app_title(self.adapter.display_name))
        self.dir_frame.config(text="%s 数据目录" % self.adapter.display_name)

        self._refresh_items()
        self._refresh_uid_combo()
        # 记住用户本次选择，下次启动默认沿用
        _save_last_tool(self.adapter.name)
        self.status.configure(text="已切换到 %s" % self.adapter.display_name)
        # 导入能力区随所选工具刷新（提示可导入的软件 / 版本 / 数据范围）
        self._refresh_import_matrix()
        # 仅特定工具显示的行（DSH 会话健康行 / Qoder 历史诊断行）随工具刷新
        self._update_tool_rows_visibility()

    # ------------------------------------------------------- 内容区滚动容器 --
    def _build_scroll_body(self) -> None:
        """构建内容区滚动容器（canvas + 按需竖向滚动条）与内嵌内容 frame。

        除进度条与状态栏外的全部区域都挂在 ``self.content`` 下：

        - 窗口高度不足时，canvas 只呈现可视部分，内容超出即出现竖向滚动条，
          因此再矮的窗口也不会「底部被裁掉且无法看到」；
        - 窗口高度富余时，``_relayout()`` 把多出的高度按比例分给有弹性余量的
          区域（备份列表区、数据导入说明区），界面铺满、不留大片空白。

        内嵌窗口的宽度始终跟随 canvas 可视宽度（否则内容会被压成一条竖线）。
        """
        body_outer = ttk.Frame(self.root)
        body_outer.grid(row=0, column=0, sticky="nsew")
        body_outer.rowconfigure(0, weight=1)
        body_outer.columnconfigure(0, weight=1)

        self._content_canvas = tk.Canvas(body_outer, highlightthickness=0)
        self._content_sb = ttk.Scrollbar(
            body_outer, orient="vertical", command=self._content_canvas.yview
        )
        # 横向兜底滚动条：内容请求宽度大于可视宽度时出现（例如某行的说明文字 /
        # 按钮组在最窄窗口下排不开）。窗宽通常远大于内容请求宽，故极少出现；
        # 一旦出现，保证被挤出的部分仍可横向看到，而不是「显示不全且无从查看」。
        self._content_hsb = ttk.Scrollbar(
            body_outer, orient="horizontal", command=self._content_canvas.xview
        )
        self._content_canvas.configure(
            yscrollcommand=self._content_sb.set,
            xscrollcommand=self._content_hsb.set,
        )
        self._content_canvas.grid(row=0, column=0, sticky="nsew")
        # 滚动条先占位再 grid_remove：内容完整可见时不显示、也不留白条；
        # 需要时再 grid() 放回（见 _relayout）。
        self._content_sb.grid(row=0, column=1, sticky="ns")
        self._content_sb.grid_remove()
        self._content_hsb.grid(row=1, column=0, sticky="ew")
        self._content_hsb.grid_remove()
        self._content_scrollable = False
        self._content_hscrollable = False
        # 弹性布局的两个缓存（见 _measure_fixed / _invalidate_fixed）：
        # _fixed_cache = 「非弹性部分」总高；_fixed_cache_w = 量它时的可视宽。
        # 初值 -1 保证首次重排必定走一次完整测量。
        self._fixed_cache = None
        self._fixed_cache_w = -1
        # 跟随内容区宽度自动折行的「长说明」label（按钮下方的用途说明等），
        # 构建时 append，宽度变化时由 _sync_hint_wrap 统一更新 wraplength。
        self._hint_labels: list = []

        self.content = ttk.Frame(self._content_canvas)
        self._content_canvas.create_window(
            (0, 0), window=self.content, anchor="nw", tags="__ct__"
        )
        self.content.bind(
            "<Configure>",
            lambda e: self._content_canvas.configure(
                scrollregion=self._content_canvas.bbox("all")
            ),
        )

        def _sync_content_width(evt=None):
            """内嵌内容窗口宽度 = max(可视宽, 内容请求宽)，并据此显隐横向滚动条。

            宽度**不做上限裁剪**：把内嵌窗口压得比内容请求宽更窄，只会让各区域
            按 pack 顺序把排在最后的控件挤出可视区（表现为「右侧显示不全」）。
            这里反过来——需要多宽就给多宽，超出可视宽的部分交给横向滚动条。

            返回**「折行宽是否因此失效」**：折行宽是按内容区**实际宽**算的，只有当
            实际宽将随本次设置而改变时才需要重刷。用「实际宽 vs 目标宽」比较，
            而不是用「内嵌窗宽度有没有被改写」——竖向滚动条一显隐，内嵌窗宽度
            会从旧可视宽改到新可视宽（内容区实际宽其实没变），按前者判会白多刷
            一次整屏（实测单次 700~1100ms）。
            """
            w = self._content_canvas.winfo_width()
            if w <= 1:
                return False
            try:
                req = self.content.winfo_reqwidth()
            except Exception:
                req = 0
            try:
                actual = int(self.content.winfo_width())
            except Exception:
                actual = 0
            target = max(w, req)
            self._content_canvas.itemconfig("__ct__", width=target)
            self._set_hscroll(req > w + 1)
            return actual > 1 and abs(actual - target) > 0.5

        self._content_canvas.bind("<Configure>", _sync_content_width)
        self._sync_content_width = _sync_content_width

        # 滚轮：仅在内容区确实超出可视高时才接管。鼠标落在自带滚动能力的控件上
        # （备份项列表 canvas / 识别结果 canvas / 导入说明 Text）时放行，交给它们
        # 各自处理，避免「外层抢走滚轮、内层滚不动」。
        def _on_content_wheel(event):
            if not self._content_scrollable:
                return
            if isinstance(
                event.widget, (tk.Text, tk.Listbox, tk.Canvas, ttk.Treeview)
            ):
                return
            if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
                delta = -1
            else:
                delta = 1
            self._content_canvas.yview_scroll(delta, "units")
            return "break"

        self._on_content_wheel = _on_content_wheel
        self.root.bind("<MouseWheel>", _on_content_wheel)
        self.root.bind("<Button-4>", _on_content_wheel)
        self.root.bind("<Button-5>", _on_content_wheel)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # 根布局改用 grid 分三行：内容区（row 0，吸收全部剩余高度）/ 进度条 / 状态栏。
        # 之前全用 pack：pack 在空间不足时是「按顺序砍掉排在后面的控件」——进度条与
        # 状态栏正好排在最后，于是窗口一调矮就先被裁掉看不见。grid 的 row 0 带
        # weight=1 且 minsize=0，剩余高度先满足 row 1/2 的请求高度，再分给 row 0，
        # 因此底部两行在任何窗口高度下都完整可见；内容区自身超出部分由滚动条承载。
        self.root.rowconfigure(0, weight=1, minsize=0)
        self.root.rowconfigure(1, weight=0)
        self.root.rowconfigure(2, weight=0)
        self.root.columnconfigure(0, weight=1)

        self._build_scroll_body()

        # 工具切换：维护者实现新适配器后，下拉即可切换当前 AI 工具
        tool_row = ttk.Frame(self.content)
        tool_row.pack(fill=tk.X, padx=12, pady=(6, 0))
        # 留一个句柄：菜单与下拉框同处这一行，测试要能量「菜单有没有把行高撑高」。
        self.tool_row = tool_row
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

        # 菜单放在「AI 工具」这一行的**右侧**（该行原本右侧全空）⇒ **零新增窗口高度**：
        # 本行高度不变 ⇒ 不触发 _fit_layout() 的高度基准重算。
        # ⚠️ 不用 root.config(menu=…)：那会独占一行、需要重新校准高度基准，
        #    且 macOS 还会把它收进系统应用菜单（三平台表现就不一致了）。
        self._build_row_menus(tool_row)

        # 数据目录
        self.dir_frame = ttk.LabelFrame(
            self.content, text="%s 数据目录" % self.adapter.display_name
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
        # 识别状态区：在数据目录输入框与当前用户行之间，列出探测到的各数据根目录
        self.detect_frame = ttk.Frame(self.dir_frame)
        self.detect_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
        self.detect_summary = ttk.Label(self.detect_frame, text="", foreground="#0a6")
        # summary 默认 pack（保证 ok=False 时的「未找到数据目录」提示可见），
        # 单项成功时由 _refresh_detect_status pack_forget，避免 1 个数据根时看着偏高+留白。
        # 用 grid 严格按 row 垂直堆叠（pack side=TOP 在 ttk.Frame 内多次实测位置反转/重叠，
        # 实测 summary y=42、detect_body y=0；grid 按 row=0/1 强制垂直序，更稳）。
        self.detect_frame.columnconfigure(0, weight=1)
        self.detect_summary.grid(row=0, column=0, sticky="ew")
        self._detect_summary_packed = True
        # canvas + scrollbar 包到子 frame (detect_body)，确保与 summary 垂直堆叠——
        # 若都直接 pack 到 detect_frame，side 混用（TOP/LEFT）会出现 summary 与 canvas
        # 在同一行重叠覆盖（实测 y=0 都从 0 开始）。
        self.detect_body = ttk.Frame(self.detect_frame)
        # 识别到的数据根目录逐行区：用 canvas 包一层，限制最大高度（约两行），
        # 超出则显示竖向滚动条，避免数据根过多时把主窗口整体高度撑高。
        # canvas + scrollbar 改为包到 detect_body 子 frame，确保与上方 summary 垂直堆叠
        # （detect_frame 内直接 pack 不同 side 会导致 summary 与 canvas 同 y=0 重叠）。
        self.detect_rows_canvas = tk.Canvas(
            self.detect_body, highlightthickness=0
        )
        self.detect_rows_sb = ttk.Scrollbar(
            self.detect_body, orient="vertical",
            command=self.detect_rows_canvas.yview,
        )
        self.detect_rows_frame = ttk.Frame(self.detect_rows_canvas)
        self.detect_rows_frame.bind(
            "<Configure>",
            lambda e: self.detect_rows_canvas.configure(
                scrollregion=self.detect_rows_canvas.bbox("all")
            ),
        )
        self.detect_rows_canvas.create_window(
            (0, 0), window=self.detect_rows_frame, anchor="nw", tags="__drf__"
        )
        self.detect_rows_canvas.configure(
            yscrollcommand=self.detect_rows_sb.set
        )

        # 内嵌窗口宽度跟随 canvas 可视宽，否则内容被压成竖线；
        # 同时把各行的折行宽度同步过去——数据根的相对路径可能很长，不折行
        # 就会被 canvas 裁掉右半（canvas 只有竖向滚动，横向不会自动换行）。
        def _sync_detect_width(evt=None):
            w = self.detect_rows_canvas.winfo_width()
            if w > 1:
                self.detect_rows_canvas.itemconfig("__drf__", width=w)
                wrap = max(160, w - 6)
                for child in self.detect_rows_frame.winfo_children():
                    try:
                        child.configure(wraplength=wrap)
                    except Exception:
                        pass

        self.detect_rows_canvas.bind("<Configure>", _sync_detect_width)
        self._sync_detect_width = _sync_detect_width

        # 滚轮滚动：鼠标在识别结果区域时直接驱动内容纵向滚动，方便查看多条数据根。
        # 是否拦截滚轮由「刷新时算好的稳定标志」_detect_overflow 决定（内容超过最大
        # 两行高才拦截），不再实时量 winfo_reqheight()——避免刷新时序里 canvas 高度
        # 变更触发重排导致量值抖动、与滚动条显隐判据打架，从而在「识别结果区域」滚轮
        # 明明该滚却因误判「未超」而 return 不拦截（表现为滚轮无响应）。
        def _on_detect_mousewheel(event):
            # 内容未超过最大高度（不需要滚动）时放行滚轮给父容器，不拦截。
            if not getattr(self, "_detect_overflow", False):
                return
            # Windows: delta 为 ±120 的整数倍；其他平台用 Button-4/5
            if event.num == 4:
                delta = -1
            elif event.num == 5:
                delta = 1
            else:
                delta = -1 if event.delta > 0 else 1
            # 以「行」为单位平滑滚动（yscrollincrement 已设为单行高），小幅超限也能
            # 逐行滚动手感自然，且明确区别于「只能在滚动条上」的原生像素滚动。
            # 最大高度（两行）保持不变。
            self.detect_rows_canvas.yview_scroll(delta, "units")
            return "break"  # 阻止事件继续冒泡到主窗口

        # 存为实例方法：rows 生成处（逐行 label）也要复用同一 handler 绑定，
        # 否则鼠标停在文字 label 上时 <MouseWheel> 不会冒泡到父 frame，导致
        # 「文字区域滚轮无反应、只能在滚动条上滚」（Tk 该事件不自动向上冒泡）。
        self._on_detect_mousewheel = _on_detect_mousewheel

        # 滚轮绑定覆盖：内嵌 frame（逐行）、canvas、以及外层 detect_frame
        # （含 canvas 与滚动条之间缝隙），确保鼠标停在该行区任意位置都能触发。
        for _w in (self.detect_rows_canvas, self.detect_rows_frame, self.detect_frame, self.detect_body):
            _w.bind("<MouseWheel>", _on_detect_mousewheel)
            _w.bind("<Button-4>", _on_detect_mousewheel)
            _w.bind("<Button-5>", _on_detect_mousewheel)
        # 最大高度：恰好容纳两行识别结果（含行间间距），用真实渲染字体度量。
        # 公式取两行内容高（2*line_h + 行间 pady）再 +2 余量，使「正好两行」时
        # 不触发滚动条、第三行起才截断并显示滚动条（最多显示两行）。
        line_h = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        self._detect_max_h = line_h * 2 + 8
        # 滚轮以「行」为单位平滑滚动（值=单行高），小幅超限也能逐行滚动手感自然
        self.detect_rows_canvas.configure(yscrollincrement=line_h)
        # canvas 高度自适应：刷新时设为 min(内容高, 最大两行高)，不固定为两行。
        self.detect_rows_canvas.configure(height=self._detect_max_h)  # 初始默认两行高，刷新时再按内容收
        # 布局用 grid（而非 pack(side=LEFT)）：pack 下 canvas 只按**请求宽**占位，
        # 而 tk.Canvas 的默认请求宽只有 378px —— 结果是识别区只用了面板一半宽度，
        # 长路径行右侧被裁（实测：可见宽 378 / 可用 895）。grid + column weight=1
        # 让 canvas 横向撑满，宽度由 detect_body 决定。高度仍由 configure(height=…)
        # 控制，故不会因额外垂直空间被拉高（原来不用 expand 的顾虑在此不存在）。
        self.detect_body.columnconfigure(0, weight=1)
        self.detect_rows_canvas.grid(row=0, column=0, sticky="ew")
        self.detect_rows_sb.grid(row=0, column=1, sticky="ns")
        self.detect_rows_sb.grid_remove()  # 默认收起，内容溢出时才显示
        # detect_body 紧跟 summary 之后垂直堆叠（grid row=1，与 summary row=0 严格按序）
        self.detect_body.grid(row=1, column=0, sticky="ew")
        # 当前登录用户 UID（记忆区默认备份对象，下拉切换）
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

        # DSH 会话健康（仅「DeepSeek Harness」适配器显示，其他工具 pack_forget 隐藏，
        # 不占用空间、不影响各区域自适应逻辑；dsh 选中时该行按内容自适应抬高窗口）。
        self.dsh_health_frame = ttk.Frame(self.dir_frame)
        self.dsh_health_label = ttk.Label(
            self.dsh_health_frame, text="", foreground="#333",
            anchor="w", justify="left", wraplength=760,
        )
        self.dsh_health_label.pack(fill=tk.X)
        # zstd 缺失时显示「安装并重新检测」按钮（默认隐藏，按检测结果切换）
        self.dsh_zstd_install_btn = ttk.Button(
            self.dsh_health_frame, text="安装 zstd 并重新检测",
            command=self._dsh_install_zstd, width=20,
        )
        self.dsh_zstd_install_btn.pack_forget()
        health_row = ttk.Frame(self.dsh_health_frame)
        health_row.pack(fill=tk.X, pady=(2, 0))
        self.dsh_check_btn = ttk.Button(
            health_row, text="检测会话健康", command=self._dsh_check, width=14
        )
        self.dsh_check_btn.pack(side=tk.LEFT)
        self.dsh_fix_btn = ttk.Button(
            health_row, text="修复会话数据", command=self._dsh_fix, width=16
        )
        self.dsh_fix_btn.pack(side=tk.LEFT, padx=(6, 0))
        # 「修复重复调用 ID」与检测/修复按钮平级，但默认不勾选：它是有语义改动的
        # 修复（tool-call id 会被加 #n 后缀），只有勾选时才随「修复」一并处理。
        self.dsh_fix_dup_var = tk.BooleanVar(value=False)
        self.dsh_fix_dup_check = ttk.Checkbutton(
            health_row, text="同时修复重复调用 ID（会改写数据）",
            variable=self.dsh_fix_dup_var,
        )
        self.dsh_fix_dup_check.pack(side=tk.LEFT, padx=(6, 0))
        # 子代理关系修复的来源：这里指的是**这些外部导入会话原本来自哪个工具**（用户视角），
        # 不是「必须选 ZCode」——留空即自动查找，找不到时再按需要修复的那批会话的来源
        # 去选对应工具的**数据目录或备份包**。措辞不点名具体工具，免得用户误以为只能选 ZCode。
        relink_row = ttk.Frame(self.dsh_health_frame)
        relink_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(relink_row, text="外部导入会话的来源：").pack(side=tk.LEFT)
        self.dsh_relink_source_var = tk.StringVar(value="")
        ttk.Entry(relink_row, textvariable=self.dsh_relink_source_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(relink_row, text="选数据目录…", width=11,
                   command=lambda: self._pick_relink_source(False)).pack(side=tk.LEFT)
        ttk.Button(relink_row, text="选备份包…", width=10,
                   command=lambda: self._pick_relink_source(True)).pack(side=tk.LEFT, padx=(4, 0))
        # 留空＝自动查找：把这层含义常驻写在界面上（用户不必猜「不填会怎样」），
        # 并在检测/修复的报告里回显**实际用了哪个来源**。
        self.dsh_relink_hint_var = tk.StringVar(value="")
        relink_hint = ttk.Label(self.dsh_health_frame, textvariable=self.dsh_relink_hint_var,
                                foreground="#666", anchor="w", justify="left", wraplength=760)
        relink_hint.pack(fill=tk.X)
        self._hint_labels.append(relink_hint)
        self.dsh_relink_source_var.trace_add("write", lambda *_: self._refresh_relink_hint())
        self._refresh_relink_hint()
        # 说明文字另起一行：与三个按钮挤在同一行时整行请求宽达 1122px（可用仅 899），
        # 末位说明被挤出可视区。独立成行后独占宽度、按宽度自动折行。
        dsh_hint = ttk.Label(
            self.dsh_health_frame,
            text="检测 DSH 会话是否「未分组」（磁盘存在但索引未登记）、属旧格式无法加载"
            "（扁平 replayState / 同一步重复调用 ID），或因投影缓存过期而在侧边栏看不到 / 标题错；"
            "「修复会话数据」还会按上面的来源修复「旧版导入留下的顶层子代理会话」",
            foreground="#666", anchor="w", justify="left", wraplength=760,
        )
        dsh_hint.pack(fill=tk.X, pady=(4, 0))
        self._hint_labels.append(dsh_hint)
        # 默认隐藏；选中 dsh 工具时由 _update_dsh_health_visibility 显示
        # 是否已检测且存在可修复项（未分组会话 / 陈旧投影缓存）：None=未检测、
        # True/False=已检测结论，供修复按钮置灰判断（无任何可修项时禁用）。
        self._dsh_has_fixable = None
        self.dsh_health_frame.pack_forget()

        # WorkBuddy 会话健康（仅 WorkBuddy 适配器显示；与 DSH 那套同构：检测 → dry-run
        # 计划 → 确认 → 备份写盘 → 复检）。存在的意义是「导入后工作区看得见、会话列表全空」
        # 这一类失配：会话正文（projects/*.jsonl）与索引行（workbuddy.db 的 sessions）
        # 必须同时到位，且索引行的 user_id 归属得对当前登录账号可见（见 workbuddy_repair）。
        self.wb_health_frame = ttk.Frame(self.dir_frame)
        self.wb_health_label = ttk.Label(
            self.wb_health_frame, text="", foreground="#333",
            anchor="w", justify="left", wraplength=760,
        )
        self.wb_health_label.pack(fill=tk.X)
        wb_health_row = ttk.Frame(self.wb_health_frame)
        wb_health_row.pack(fill=tk.X, pady=(2, 0))
        self.wb_check_btn = ttk.Button(
            wb_health_row, text="检测会话健康", command=self._wb_check, width=14
        )
        self.wb_check_btn.pack(side=tk.LEFT)
        self.wb_fix_btn = ttk.Button(
            wb_health_row, text="修复会话数据", command=self._wb_fix, width=16
        )
        self.wb_fix_btn.pack(side=tk.LEFT, padx=(6, 0))
        wb_hint = ttk.Label(
            self.wb_health_frame,
            text="检测 WorkBuddy 的会话索引库与磁盘事件流是否配套：导入行的归属写错"
            "（会话列表空但工作区照常显示）、正文在但库里没登记、工作区缺行、侧栏秒开快照"
            "陈旧；「修复会话数据」写盘前先备份 workbuddy.db，请先完全退出 WorkBuddy",
            foreground="#666", anchor="w", justify="left", wraplength=760,
        )
        wb_hint.pack(fill=tk.X, pady=(4, 0))
        self._hint_labels.append(wb_hint)
        self.wb_health_frame.pack_forget()

        # Qoder 历史诊断行：仅选中 qoder 时显示。新版 Qoder CN 已改为独立桌面端
        # （Electron，数据根 %APPDATA%/com.qodercn.app.stable，会话主库 main.sqlite），
        # 旧数据在 ~/.qoder-cn；应用内「导入旧版数据」只搬它认识的那部分，
        # 常见「导入成功但历史仍不可见」。此处给出结构化诊断（见 adapters.qoder.diagnose_history）。
        self.qoder_diag_frame = ttk.LabelFrame(self.content, text="历史会话诊断")
        diag_row = ttk.Frame(self.qoder_diag_frame)
        diag_row.pack(fill=tk.X, pady=(2, 0))
        self.qoder_diag_btn = ttk.Button(
            diag_row, text="检测历史会话", command=self._qoder_diag, width=14
        )
        self.qoder_diag_btn.pack(side=tk.LEFT)
        # 说明文字另起一行：原先与按钮挤在同一行时整行请求宽达 1016px（可视仅 939），
        # 于是内容区常年出现横向滚动条、右侧被挤出可视区；且每次切换工具宽度都要在
        # 「够/不够」之间反复，白多刷一遍整屏。独立成行后按宽度自动折行。
        qoder_hint = ttk.Label(
            self.qoder_diag_frame,
            text="诊断「新版导入数据后仍看不到历史会话」：对比新旧两处数据根"
            "（新版 main.sqlite / 旧版 local.db）与导入账本，给出成因结论",
            foreground="#666", anchor="w", justify="left", wraplength=760,
        )
        qoder_hint.pack(fill=tk.X, pady=(4, 0))
        self._hint_labels.append(qoder_hint)
        self.qoder_diag_frame.pack_forget()

        # 备份内容
        # mid 不 expand：本区域的高度弹性由内部 inner 的**显式高度**承担
        # （_relayout 按窗口可用高度在 MIN_LIST_H~MAX_LIST_H 之间设定），
        # 不依赖 pack 的 expand 分配——那样在窗口变矮时只会被裁而不收缩。
        mid = ttk.LabelFrame(self.content, text="备份内容（勾选即生效）")
        mid.pack(fill=tk.X, expand=False, **pad)
        self._mid = mid

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

        right_bar = ttk.Frame(bar)
        right_bar.pack(side=tk.RIGHT)
        self.summary = ttk.Label(right_bar, text="", foreground="#0a6")
        self.summary.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(right_bar, text="估算大小", command=self._estimate, width=10).pack(
            side=tk.LEFT, padx=8
        )

        # 锁高容器 inner：ttk.Frame 尊重 height 配置，pack_propagate(False) 后高度
        # 严格等于显式设定值（既不吸收 mid 的多余空间，也不会被内容请求高度撑大）。
        # 这个高度就是本界面最主要的**弹性高度**：_relayout 按窗口可用高度在
        # _LIST_H_MIN~_LIST_H_MAX 之间设定它——窗口调大则列表区同步变大（一屏能看
        # 到更多备份项），调小则同步收缩，为下方区域让出空间。
        inner = ttk.Frame(mid)
        inner.pack(fill=tk.X, expand=False, padx=8, pady=4)
        inner.pack_propagate(False)
        inner.configure(height=_LIST_H_NATURAL)
        self._list_inner = inner

        canvas = tk.Canvas(inner, highlightthickness=0)
        # 显式给初始高度：避免 pack(fill=BOTH, expand) 在 _relayout 设高前把 canvas
        # 撑成 list_frame 的请求高度（CodeBuddy 项多可达 500+），导致 mid/主窗随内容
        # 变高、三工具观感不一致。
        canvas.configure(height=_LIST_H_NATURAL)
        sb = ttk.Scrollbar(inner, orient="vertical", command=canvas.yview)
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

        # canvas 在 inner 内 fill=BOTH：横向撑满 inner 宽（说明列不靠左），
        # 纵向占满 inner 锁定/弹性设定的高度。
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 0), pady=0)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 0), pady=0)

        # 列表区滚轮：内容超出一屏时，鼠标停在列表任意位置都能滚动列表自身；
        # 未超出则放行给外层内容区滚动（否则鼠标一进列表区，外层就滚不动了）。
        # Tk 的 <MouseWheel> 不会从子 widget 自动传播，故 canvas 与逐行控件都要绑。
        def _on_list_wheel(event):
            try:
                box = canvas.bbox("all")
            except Exception:
                box = None
            if not box or (box[3] - box[1]) <= canvas.winfo_height():
                return
            if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
                delta = -1
            else:
                delta = 1
            canvas.yview_scroll(delta, "units")
            return "break"

        self._on_list_wheel = _on_list_wheel
        for _w in (canvas, self.list_frame):
            _w.bind("<MouseWheel>", _on_list_wheel)
            _w.bind("<Button-4>", _on_list_wheel)
            _w.bind("<Button-5>", _on_list_wheel)

        # 数据导入：在**当前所选工具**的备份窗口内，提示「可导入的软件 + 版本 +
        # 数据范围 + 状态」，并提供导入入口（把其它软件的会话以本工具原生格式写出）。
        # 内容随工具切换刷新（见 _refresh_import_matrix）。
        self.imp_frame = ttk.LabelFrame(
            self.content,
            text="数据导入（把其它软件的会话导入到「%s」）" % self.adapter.display_name,
        )
        self.imp_frame.pack(fill=tk.X, **pad)

        imp_head = ttk.Frame(self.imp_frame)
        imp_head.pack(fill=tk.X, padx=8, pady=(8, 2))
        self.imp_summary = ttk.Label(imp_head, text="", foreground="#0a6", anchor="w")
        self.imp_summary.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.imp_btn = ttk.Button(
            imp_head, text="导入会话…", command=self.on_migrate, width=14
        )
        self.imp_btn.pack(side=tk.RIGHT)
        ttk.Button(
            imp_head, text="导入说明", command=self._show_import_help, width=10
        ).pack(side=tk.RIGHT, padx=(0, 6))

        imp_body = ttk.Frame(self.imp_frame)
        imp_body.pack(fill=tk.X, padx=8, pady=(0, 8))
        # 只读 Text：行数随窗口高度弹性伸缩（_relayout 在 _IMP_ROWS_MIN~MAX 之间
        # 设定），默认 4 行；自带滚动条，不因来源条目多而把主窗口撑高。
        self.imp_text = tk.Text(
            imp_body, height=_IMP_ROWS_NATURAL, wrap="word", relief=tk.FLAT,
            highlightthickness=0,
            background=self.root.cget("background"), cursor="arrow",
        )
        imp_sb = ttk.Scrollbar(imp_body, orient="vertical", command=self.imp_text.yview)
        self.imp_text.configure(yscrollcommand=imp_sb.set)
        self.imp_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        imp_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.imp_text.tag_configure("head", font=("TkDefaultFont", 9, "bold"))
        self.imp_text.tag_configure("scope", foreground="#333")
        self.imp_text.tag_configure("note", foreground="#777")
        for st, color in import_matrix.STATUS_COLORS.items():
            self.imp_text.tag_configure("st_" + st, foreground=color)
        self.imp_text.configure(state="disabled")

        # 选项
        opt = ttk.LabelFrame(self.content, text="选项")
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
        # 第二行：整齐排开剩余选项，避免全部挤在一行后被挤出可视区（实测一行
        # 8 个控件的请求宽 943 > 可用 899，「压缩方式」下拉被压窄、右侧显示不全）。
        orow2 = ttk.Frame(opt)
        orow2.pack(fill=tk.X, padx=8, pady=(0, 8))
        # 严格校验模式：即使带清单也强制扫描内部结构指纹，防伪造声明文件
        self.strict_var = tk.BooleanVar(value=False)
        strict_cb = ttk.Checkbutton(
            orow2, text="严格校验模式", variable=self.strict_var
        )
        strict_cb.pack(side=tk.LEFT)
        _Tooltip(
            strict_cb,
            "勾选后，即使压缩包带有声明文件，也会强制扫描内部数据结构指纹并"
            "校验，用于防止伪造声明文件的恶意备份。\n未勾选时仅在缺少声明文件"
            "时才做结构指纹回退识别。",
        )
        # 备份后定位敏感文件：勾选且本次含脱敏项时，自动打开文件夹并打开相关文件，
        # 并尝试定位到敏感字段（如 apiKey / 令牌等）行，方便用户手动记录凭证。默认勾选。
        self.locate_sensitive_var = tk.BooleanVar(value=True)
        locate_cb = ttk.Checkbutton(
            orow2, text="备份后定位敏感文件", variable=self.locate_sensitive_var
        )
        locate_cb.pack(side=tk.LEFT, padx=16)
        _Tooltip(
            locate_cb,
            "勾选后（默认），若本次备份包含「含敏感凭证的项」（如 CodeBuddy 的"
            "自定义模型配置，其中的 apiKey/令牌等已被脱敏为占位符），备份完成时本工具会"
            "自动打开该文件所在文件夹、用可跳行的编辑器（自动探测 VS Code / Notepad++ /"
            "Notepad--，优先使用）打开文件并定位光标到敏感字段（如 apiKey/令牌）所在行，"
            "方便你手动记下这些凭证；若都没装则退化为系统默认程序（如记事本）仅打开文件、"
            "不跳行。\n不勾选则只弹出文字提醒，不自动打开文件。\n注意：备份包本身不含明文"
            "凭证，请务必在源机器记下、并在目标机器手动补填。",
        )
        # 压缩方式：置于选项区域，与导出行为相关。宽度按最长候选项实测取值
        # （中文按 2 个字符宽估算），避免固定 width=26 时被挤窄导致文字显示不全。
        ttk.Label(orow2, text="压缩方式：").pack(side=tk.LEFT, padx=(0, 2))
        _cvals = list(self._compress_levels.keys())
        _cwidth = max(10, max((len(v) * 2 for v in _cvals), default=10))
        self.compress_combo = ttk.Combobox(
            orow2, textvariable=self.compress_var, width=_cwidth, state="readonly"
        )
        self.compress_combo["values"] = _cvals
        self.compress_combo.pack(side=tk.LEFT)

        # 操作
        act = ttk.Frame(self.content)
        act.pack(fill=tk.X, **pad)
        ttk.Button(act, text="导出备份", command=self.on_export).pack(
            side=tk.LEFT, ipadx=14, ipady=4
        )
        ttk.Button(act, text="还原备份/快照", command=self.on_import).pack(
            side=tk.LEFT, padx=10, ipadx=14, ipady=4
        )

        self.pbar = ttk.Progressbar(self.root, mode="determinate")
        self.pbar.grid(row=1, column=0, sticky="ew", padx=12)
        self.status = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=4
        )
        self.status.grid(row=2, column=0, sticky="ew")

        # 构建完成后按实际内容自适应窗口高度（保证状态栏等完整区域默认可见）
        self._refresh_import_matrix()
        # 行显隐先定，再自适应高度——否则按含隐藏行的高度算出来的窗口高会偏大
        self._update_tool_rows_visibility()
        self._fit_layout(autosize=True)
        # 首次重排发生在窗口映射之前（那时量不到可用高度），故窗口尺寸定下来后
        # 再排一次：让首屏**一次到位**落到最终布局，而不是先摆一版、几十毫秒后
        # 再跳一次（那一跳肉眼可见，也容易被当成「界面还没稳定」）。
        self._fit_layout()
        # 窗口尺寸变化（用户拖动 / 最大化 / 不同分辨率屏幕）时重排内容区：
        # 富余则按比例放大各弹性区域，不足则按比例压缩，压缩到底再出现竖向滚动条。
        self.root.bind("<Configure>", self._on_root_configure)

    # ------------------------------------------------------------- helpers --
    def _avail_body_height(self) -> int:
        """内容区（滚动容器）当前可视高度；量不到时返回 0（无头 / 尚未映射）。

        单独抽成方法便于测试注入：headless（窗口 withdraw）下 ``winfo_height``
        恒为 1，无法反映真实窗口高度。
        """
        try:
            h = int(self._content_canvas.winfo_height())
        except Exception:
            return 0
        return h if h > 1 else 0

    def _content_height(self) -> int:
        """内嵌内容 frame 的请求高度（= 各区域按当前弹性尺寸排布后的总高）。

        ⚠️ 父容器的请求高度是**惰性**的：改完子控件尺寸后不刷一次几何就读，
        拿到的还是上一轮的值（实测叶子控件即时、父容器要等 idle）。故只在
        ``_measure_fixed`` / ``_refresh_geometry`` 之后取值才有意义。
        """
        try:
            return int(self.content.winfo_reqheight())
        except Exception:
            return 0

    def _content_width(self) -> int:
        """内容区当前可视宽度（量不到时 0）。

        用滚动容器的宽度而不是内嵌 frame 的宽度**作为「宽度是否变化」的判据**：
        竖向滚动条一出现，可视宽先变、内嵌宽后变，取前者更早发现变化、也避免
        用「滚动条占位后再量、再判定」这类会自我循环的口径。
        """
        try:
            w = int(self._content_canvas.winfo_width())
        except Exception:
            return 0
        return w if w > 1 else 0

    def _current_flex(self) -> tuple:
        """从控件读回**当前生效**的弹性尺寸 (列表区像素高, 导入说明行数)。

        不另存一份 Python 状态：控件值才是唯一事实来源，二者一旦不同步，
        就会出现「以为没变、其实要重排」或反之的误判。
        """
        try:
            list_h = int(self._list_inner.cget("height"))
        except Exception:
            list_h = _LIST_H_NATURAL
        try:
            rows = int(self.imp_text.cget("height"))
        except Exception:
            rows = _IMP_ROWS_NATURAL
        return list_h, rows

    def _invalidate_fixed(self) -> None:
        """让「非弹性部分高度」缓存失效（内容结构变化后调用，下次重排重测）。

        只在这两类时机需要：① 区域增减（换工具 / 识别行重填 / UID 区展开收起）；
        ② 宽度变化（文案折行数变了）。**窗口高度变化时一律不需要**——这正是
        拖动窗口能零重绘的关键。
        """
        self._fixed_cache = None
        self._fixed_cache_w = -1

    def _measure_fixed(self, fresh: bool = False) -> float:
        """量出「非弹性部分」的总高度并缓存（默认内含一次几何刷新）。

        非弹性高度 = 内容总高 − 当前两个弹性区的显式高度。因此**不必**先把
        弹性区复位到自然值再量：复位那一下会让用户看见「先弹回自然尺寸、再压
        回去」的中间态，正是拖动窗口时界面闪烁的来源。

        ``fresh=True`` 表示调用方刚刚刷过几何且其后没有再改动任何影响高度的
        东西，可直接读值——少刷一次就是少一次整屏重绘（实测单次 180~650ms）。
        """
        if not fresh:
            self._refresh_geometry()
        list_h, rows = self._current_flex()
        h = self._content_height()
        fixed = max(0.0, h - list_h - self._imp_block_px(rows))
        self._fixed_cache = fixed
        self._fixed_cache_w = self._content_width()
        return fixed

    def _imp_metrics(self) -> tuple:
        """标定数据导入说明区的高度模型 ``(每行像素, 常量像素, 最小像素)``。

        该区高度对行数**不是**纯线性：说明框里与文字并排放着一条竖向滚动条，
        它的最小请求高度（实测 51px）在行数很少时会反过来决定整块高度——
        实测 2 行与 3 行的高度**完全相同**，到第 4 行才开始随行数增长。只按
        「行数 × 行高」线性建模，会在压缩到 2 行时低估该区约 20px，从而把竖向
        滚动条误判出来（内容明明装得下却出现滚动条）。

        故按 ``max(行数 × 每行 + 常量, 最小高)`` 三点标定：2 行取被最小高主导
        的点、8 行取纯线性点，即可解出常量项。行高与 Text 自身内距直接量叶子
        控件（即时值，不必刷几何）；整块的两个点各需一次刷新（父容器的请求高
        是惰性值），故本方法只在首次调用时刷新两次，之后全程走缓存。
        """
        cached = getattr(self, "_imp_metrics_cache", None)
        if cached:
            return cached
        row_px, pad, floor = 13.0, 70.0, 117.0
        orig = None
        try:
            orig = int(self.imp_text.cget("height")) or _IMP_ROWS_NATURAL
            self.imp_text.configure(height=2)
            t2 = float(self.imp_text.winfo_reqheight())
            self.imp_text.configure(height=8)
            t8 = float(self.imp_text.winfo_reqheight())
            row_px = max(1.0, (t8 - t2) / 6.0)
            pad_text = t2 - 2.0 * row_px
            self.imp_text.configure(height=2)
            self._refresh_geometry()
            h2 = float(self.imp_frame.winfo_reqheight())
            self.imp_text.configure(height=8)
            self._refresh_geometry()
            h8 = float(self.imp_frame.winfo_reqheight())
            if h8 > h2:
                pad = (h8 - 8.0 * row_px - pad_text) + pad_text
                floor = h2
        except Exception:
            pass
        finally:
            try:
                if orig is not None:
                    # 复原行数即回到调用前的状态；无需再刷几何（调用方紧接着
                    # 就会按目标值重设尺寸）。
                    self.imp_text.configure(height=orig)
            except Exception:
                pass
        self._imp_metrics_cache = (row_px, pad, floor)
        return self._imp_metrics_cache

    def _imp_block_px(self, rows: float) -> float:
        """数据导入说明区在给定行数下占用的像素高（含其滚动条的最小高度）。"""
        row_px, pad, floor = self._imp_metrics()
        return max(rows * row_px + pad, floor)

    def _apply_flex(self, list_h: int, imp_rows: int) -> None:
        """把两个弹性区域的显式尺寸设为给定值（列表区像素高 / 导入说明行数）。

        值没变时直接返回：拖动窗口时绝大多数帧算出来的目标尺寸与当前一致
        （尤其被上下限夹紧的区间），跳过可以省掉一次全内容区重排+重绘。
        """
        list_h = int(list_h)
        imp_rows = int(imp_rows)
        if (list_h, imp_rows) == self._current_flex():
            return
        try:
            self._list_inner.configure(height=list_h)
            self._canvas.configure(height=list_h)
        except Exception:
            pass
        try:
            self.imp_text.configure(height=imp_rows)
        except Exception:
            pass

    def _bind_list_wheel(self, widget=None) -> None:
        """递归给备份列表区内的控件绑定滚轮处理。

        Tk 的 ``<MouseWheel>`` 不会从子 widget 冒泡到父容器，所以「鼠标停在
        某一行的文字上滚不动」是必然的——必须逐层绑定。行是动态重建的，故每次
        重建后统一重绑一次（控件量级很小，成本可忽略）。
        """
        handler = getattr(self, "_on_list_wheel", None)
        if handler is None:
            return
        node = widget if widget is not None else getattr(self, "list_frame", None)
        if node is None:
            return
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                node.bind(seq, handler)
            except Exception:
                pass
        try:
            children = node.winfo_children()
        except Exception:
            return
        for child in children:
            self._bind_list_wheel(child)

    def _target_flex(self, k: float) -> tuple:
        """把缩放系数 k 变成**最终会应用到控件上**的整数尺寸 (列表区高, 说明行数)。

        应用的是取整并夹紧后的值，求内容高时也必须用同一组值——否则「算出来的
        高度」与「实际排出来的高度」会差几个像素，临界尺寸下就会误判滚动条。
        """
        list_h = _clamp(round(_LIST_H_NATURAL * k), _LIST_H_MIN, _LIST_H_MAX)
        imp_rows = _clamp(round(_IMP_ROWS_NATURAL * k), _IMP_ROWS_MIN, _IMP_ROWS_MAX)
        return int(list_h), int(imp_rows)

    def _content_h_at(self, k: float, fixed: float) -> int:
        """给定缩放系数时内容区的总高（解析式，不触发任何真实重排）。"""
        list_h, imp_rows = self._target_flex(k)
        return int(round(fixed + list_h + self._imp_block_px(imp_rows)))

    def _solve_scale(self, avail: int, fixed: float) -> float:
        """求缩放系数 k，使内容总高最接近 avail（纯算术，不触发任何重排）。

        内容高度对 k 是分段单调函数::

            h(k) = fixed + 列表区高(k) + 说明区高(k)

        其中 ``fixed`` 是「非弹性部分」的高度（见 ``_measure_fixed``）。二分
        30 轮即可收敛到 1px 以内，全程不重排、不重绘一次——拖动窗口时正是靠
        这一点做到「只改一次尺寸、只重排一次」。
        """

        def h_at(kk: float) -> float:
            return self._content_h_at(kk, fixed)

        lo, hi = 0.05, 6.0
        if h_at(hi) <= avail:
            return hi
        for _ in range(30):
            mid = (lo + hi) / 2.0
            if h_at(mid) < avail:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    def _set_scroll(self, need: bool) -> None:
        """按需显隐内容区竖向滚动条（状态未变时不动作，避免无谓重排）。"""
        if need == getattr(self, "_content_scrollable", None):
            return
        self._content_scrollable = need
        try:
            if need:
                self._content_sb.grid()
            else:
                self._content_sb.grid_remove()
                self._content_canvas.yview_moveto(0)
        except Exception:
            pass

    def _set_hscroll(self, need: bool) -> None:
        """按需显隐内容区横向滚动条（状态未变时不动作，避免无谓重排）。"""
        if need == getattr(self, "_content_hscrollable", None):
            return
        self._content_hscrollable = need
        try:
            if need:
                self._content_hsb.grid()
            else:
                self._content_hsb.grid_remove()
                self._content_canvas.xview_moveto(0)
        except Exception:
            pass

    def _set_content_window_height(self, h: int) -> None:
        """设定内嵌内容窗口的高度：至少撑满可视区，内容更高时用内容自身高度。"""
        if h <= 0:
            return
        try:
            self._content_canvas.itemconfig("__ct__", height=h)
        except Exception:
            pass

    def _sync_hint_wrap(self) -> None:
        """让「长说明」label 的折行宽度跟随内容区宽度。

        这类 label（按钮下方的用途说明）文本较长：固定 wraplength 会在窄窗口下
        溢出被裁、在宽窗口下过早折行。跟随宽度后始终铺满可用宽、不多不少。
        """
        try:
            avail = int(self.content.winfo_width())
        except Exception:
            return
        if avail <= 1:
            return
        for lbl in getattr(self, "_hint_labels", []):
            try:
                lbl.configure(wraplength=max(200, avail - 40))
            except Exception:
                pass

    def _sync_fold_widths(self) -> None:
        """只同步「按宽度折行」的三处（不含内容区的内嵌宽/横向滚动条判定）。

        折行宽取的都是**实际几何宽度**（已随窗口更新，不是惰性请求值），所以
        调完立刻刷几何就能得到正确的内容高度。内嵌宽那一项单独放在后面：它要读
        内容区的**请求宽**（惰性值），必须等刷完几何才准——顺序反了会先按上一轮
        布局的请求宽把内嵌窗设错，再刷一次才发现要改，白多一轮整屏重绘。
        """
        for fn in (
            getattr(self, "_sync_canvas_width", None),
            getattr(self, "_sync_detect_width", None),
            getattr(self, "_sync_hint_wrap", None),
        ):
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                pass

    def _sync_aux_widths(self) -> None:
        """同步全部与宽度相关的几何（宽度变化 / 内容结构变化后调用一次）。

        包含四处内嵌滚动容器的宽度与「长文案」的折行宽：内容区、备份项列表区、
        数据根识别区。折行宽变了内容高度才会准，所以必须在测量基准之前跑完。
        """
        self._sync_fold_widths()
        fn = getattr(self, "_sync_content_width", None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass

    def _autosize_window(self) -> None:
        """首次构建时把窗口高度设定为屏幕高的固定比例（默认观感高度）。

        取值 = ``屏幕高 × _WINDOW_H_DEFAULT_RATIO``（下限 ``_WINDOW_H_MIN``、
        上限 `_window_height_ceiling`）。三个要点：

        1. **按比例而非按内容**：三工具默认高度完全一致，内容少的工具（如
           Qoder）窗口不会缩成一小条；内容多时由内容区自身的滚动条承载。
           ★ **绝不为了塞下内容而抬高窗口**——那会让低分辨率 + 高 DPI 缩放的
           用户把窗口开出屏幕（标题栏/状态栏看不见，反而没法用）。
        2. **按屏幕比例而非固定像素**：进程已声明 DPI 感知（见
           `_enable_dpi_awareness`），``winfo_screenheight`` 与 ``geometry``
           同为物理像素，故该比例在任意 DPI 缩放下都成立——窗口占屏幕的视觉
           大小恒定，小尺寸/高分屏上都不会超出屏幕被遮挡。
        3. **上限取实测工作区**：比固定比例更严格地保证「窗口整体可见」，
           详见 `_window_height_ceiling`。

        只在 ``_fit_layout(autosize=True)`` 时调用；之后窗口尺寸完全由用户与
        窗口管理器控制，程序不再改写——否则用户手调过的窗口会在切工具/展开
        列表时被莫名其妙地弹回。
        """
        try:
            screen = int(self.root.winfo_screenheight())
        except Exception:
            screen = 900
        default_h = int(screen * _WINDOW_H_DEFAULT_RATIO)
        cap = _window_height_ceiling(screen)
        win_h = max(_WINDOW_H_MIN, min(default_h, cap))
        # 宽度沿用窗口当前宽度（只改高度）。量不到时回退到 geometry 串里的宽度，
        # 绝不用猜测值覆盖——曾用 ``or 738`` 兜底，一旦窗口尚未映射就会把宽度
        # 悄悄改窄，导致界面右侧显示不全。
        cur_w = 0
        try:
            cur_w = int(self.root.winfo_width())
        except Exception:
            cur_w = 0
        if cur_w <= 1:
            try:
                cur_w = int(self.root.winfo_geometry().split("x")[0])
            except Exception:
                cur_w = 0
        if cur_w <= 1:
            cur_w = 960
        try:
            self.root.geometry("%dx%d" % (cur_w, win_h))
            self.root.update_idletasks()
        except Exception:
            pass

    def _on_root_configure(self, event) -> None:
        """主窗口尺寸变化（用户拖动 / 最大化 / 换分辨率屏幕）→ 节流后重排内容。"""
        if event.widget is not self.root:
            return
        self._schedule_relayout()

    def _on_root_destroy(self, event) -> None:
        """根窗口被销毁时的兜底清理：撤掉尚未触发的 after 回调。

        关窗按钮走 `_on_close` → `_cancel_after`，但窗口也可能被**别的方式**销毁
        （外部 `destroy()`、测试拆卸、解释器退出）。此时残留的节流重排 / 轮询回调
        会在定时器到期时被 Tk 报 `invalid command name "..._fit_layout"`——实测整套
        测试刷了 150+ 行这种噪音，把真正的失败信息淹掉。
        """
        if event.widget is not self.root:
            return  # 子控件销毁也会触发 <Destroy>，只认根窗口
        self._closing = True
        self._cancel_after()

    def _schedule_relayout(self, delay: int = 60) -> None:
        """把连续的尺寸变化合并成一次重排，避免拖动窗口时反复重算几何。"""
        job = getattr(self, "_relayout_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._relayout_job = None
        try:
            self._relayout_job = self.root.after(delay, self._fit_layout)
        except Exception:
            self._relayout_job = None

    def _fit_layout(self, autosize: bool = False) -> None:
        """按当前窗口可用高度重排内容区：等比缩放各弹性区域 + 必要时竖向滚动。

        设 ``avail`` = 内容区可视高度，内容总高 = 非弹性部分 + 两个弹性区：

        - 内容比 ``avail`` 矮：按比例**放大**各弹性区域——窗口越高，备份列表区
          与数据导入说明区越大，界面铺满、底部不留大片空白；
        - 内容比 ``avail`` 高：按比例**压缩**这两个区域，为下方区域腾出空间；
          压缩到各自下限仍不够时出现竖向滚动条，因此无论窗口多矮，全部内容都
          可达、不会被裁掉看不见。

        缩放系数 k 同时作用于两个弹性区域（各自按其自然值等比伸缩），且各自
        被自身的上下限夹紧。非弹性区域（数据目录、选项、按钮行等）高度由行
        内容决定、不参与缩放——强行拉伸只会产生空无一物的留白。

        **一次到位**：目标尺寸先纯算术解出（``_solve_scale``），再一次性应用到
        控件上，中途不把弹性区复位到自然值——否则用户会看到「先弹回自然、再压
        回去」的中间态，拖窗口时整片界面持续闪动（这正是本轮修复的两个现象）。
        重排过程只在宽度/结构真的变了时才刷几何，故拖窗口高度时零重绘。

        进度条与状态栏已由根 grid 布局固定在最下方，任何窗口高度下都完整可见。

        ``autosize=True`` 额外按屏幕比例把窗口高度一次性设定到合适值（仅首次
        构建使用）。HEADLESS 测试下同样安全（量不到高度时退化为自然尺寸）。
        """
        if getattr(self, "_in_relayout", False):
            return
        self._in_relayout = True
        try:
            self._do_fit_layout(autosize)
        finally:
            self._in_relayout = False

    def _refresh_geometry(self) -> None:
        """强制同步一次几何计算，使后续 winfo_reqheight 读到的是新尺寸。

        改完弹性区域的显式高度后**必须**调用，否则 Tk 的请求高度还是上一轮的
        值——基准高度一旦偏大/偏小，缩放系数就会算错（实测表现为「窗口调矮、
        列表区反而变大」）。
        """
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _do_fit_layout(self, autosize: bool) -> None:
        # ① 宽度变化 → 文案折行数、内嵌宽、识别区高度都要重算，「非弹性部分」的
        #    高度也随之改变，故先同步宽度相关几何、再作废基准缓存。
        #    **宽度没变时整段跳过**：拖高度时既不重绘也不重排（消除闪烁的关键）。
        refreshed = False
        if self._content_width() != self._fixed_cache_w:
            self._sync_fold_widths()
            self._refresh_geometry()
            refreshed = True
            if self._sync_content_width():
                # 内嵌宽真的变了（窗口比内容窄、走横向滚动的场景）→ 折行要按新
                # 宽度重算并再刷一次，否则基准高度是按旧折行量出来的。
                self._sync_fold_widths()
                self._refresh_geometry()
            self._fixed_cache = None

        avail = self._avail_body_height()
        if avail <= 0:
            # 量不到可用高度（无头 / 窗口尚未映射）：保持自然尺寸，不做缩放，
            # 也不显示滚动条——避免用 0 高度算出「全部压缩到底」的假结果。
            self._apply_flex(_LIST_H_NATURAL, _IMP_ROWS_NATURAL)
            self._refresh_geometry()
            self._set_scroll(False)
            self._fixed_cache = None  # 窗口一旦可量就必须重测
            if autosize:
                self._autosize_window()
            return

        fixed = self._fixed_cache
        if fixed is None:
            fixed = self._measure_fixed(fresh=refreshed)

        k = self._solve_scale(avail, fixed)
        list_h, imp_rows = self._target_flex(k)
        # 全程只可能改这一次尺寸 → 不会出现「先复位、再压缩」的两段式重排。
        self._apply_flex(list_h, imp_rows)
        # 内容总高直接按解析式算出（非弹性 + 列表区 + 说明区），不必再刷一次
        # 几何去量——少一次全屏重绘，拖动窗口时就不再闪。
        content_h = int(round(fixed + list_h + self._imp_block_px(imp_rows)))
        # 压缩到底仍装不下 → 出现竖向滚动条，并让内嵌窗口保持内容高度
        self._set_scroll(content_h > avail)
        self._set_content_window_height(max(content_h, avail))

        if autosize:
            self._autosize_window()
        # 收尾再同步一次宽度相关几何：竖向滚动条刚显隐过，可视宽要等下一次几何
        # 计算才更新，而内嵌窗宽 / 折行宽都依赖它——这里同步一次可少一轮「先按
        # 旧宽排一遍、下一帧再纠正」的重排。（只配置控件、不刷几何，代价极小。）
        self._sync_aux_widths()

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

            # 聚合项：任一子项存在即视为"找到"；勾选态优先沿用上一轮用户选择。
            # 未找到项也保留用户原有勾选状态（仅标红提示，不改动勾选）。
            any_exists = any(it.exists for it in grp)
            first = grp[0]
            if prefix in prev_sel:
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
                # 仅标红提示未找到，保留用户原有勾选状态（不在识别错误时改动勾选）
                tag, color = "（未找到）", "#c00"
            else:
                tag, color = "", "#666"
            # 聚合说明：只显示一次主描述，避免同前缀多条 description 重复堆叠；
            # 多路径时补一句覆盖数量，信息不失真且不冗余。
            # 去掉 description 中可能带的位置后缀（如"（root 位置）"），避免与覆盖数量重复。
            detail = re.sub(r"\s*（[^）]*位置）", "", first.description)
            if len(grp) > 1:
                detail = "%s（涵盖 %d 处路径）" % (detail.rstrip("。"), len(grp))
            # 追加该项的具体相对路径，方便用户了解备份位置（相对数据目录）
            try:
                rel_path = os.path.relpath(first.path, self.root_dir)
            except ValueError:
                rel_path = first.path
            detail = "%s\n路径: %s" % (detail, rel_path)
            # 携带源设备信息的条目：**在勾选处**就说清（而不是等备份完才说），
            # 因为「要不要把这个包发给别人」是勾选那一刻就在做的决定。
            if first.carries_origin:
                detail += "\n携带源设备信息：%s" % first.carries_origin
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

        # 滚轮绑定：Tk 的 <MouseWheel> 不从子 widget 冒泡，重建行之后必须重绑
        self._bind_list_wheel()
        # 列表内容变化后重新自适应高度（others 区块展开/收起会改变总高）。
        # 走 _schedule_relayout 而非裸 after_idle：任务句柄被记录，窗口销毁时
        # 能一并取消，避免留下「invalid command name」噪音。
        self._schedule_relayout(0)

        ok = os.path.isdir(self.root_dir)
        self._refresh_detect_status(ok)
        if missing:
            self.summary.config(text="%d 项未找到" % missing, foreground="#c60")
        else:
            # 全部找到（含重新检测成功后）：清空上一次可能残留的"未找到"提示
            self.summary.config(text="", foreground="#0a6")
        # 行数/文案都换过一轮 → 作废布局基准缓存，让紧随其后的那一次重排重新
        # 量高度（量的过程中会先按当前宽度重刷各行的折行宽，故不必再单独排一次
        # after_idle 同步宽度；那样还会在窗口销毁后留下 "invalid command name" 噪音）。
        # 也**不再**重复 _rebuild_others_block / _schedule_relayout：重复调用只会
        # 白做一遍重建，正是「换工具后要等好几秒才稳定」的成因之一。
        self._invalidate_fixed()

    def _rebuild_others_block(self) -> None:
        """在底部区块按当前勾选状态渲染"其他用户"UID 多选（仅勾选主项时显示）。"""
        for w in self.others_block.winfo_children():
            w.destroy()
        # UID 行增减会改变内容总高（展开/收起都在这里发生）：作废布局基准并
        # 排一次重排，否则展开后滚动条显隐与弹性区尺寸会停在展开前的那一刻。
        self._invalidate_fixed()
        self._schedule_relayout(0)
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
        # 展开出来的 UID 行同样要能在其任意位置用滚轮滚动列表
        self._bind_list_wheel()

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

    # ----------------------------------------------------------- 数据导入 --
    def _refresh_import_matrix(self) -> None:
        """把「当前所选工具」的导入能力矩阵渲染进导入区（工具切换时刷新）。"""
        name = self.adapter.name
        info = import_matrix.describe(name)
        try:
            self.imp_frame.configure(
                text="数据导入（把其它软件的会话导入到「%s」）" % self.adapter.display_name
            )
            self.imp_summary.configure(text=info["summary"])
            self.imp_text.configure(state="normal")
            self.imp_text.delete("1.0", tk.END)
            for row in info["rows"]:
                self.imp_text.insert(
                    tk.END,
                    "· %s（%s）" % (row["display"], row["versions"]),
                    ("head",),
                )
                self.imp_text.insert(
                    tk.END, "  —  %s\n" % row["status_label"], ("st_" + row["status"],)
                )
                for item in row["scope"]:
                    self.imp_text.insert(tk.END, "      可导入范围：%s\n" % item, ("scope",))
                if row["note"]:
                    self.imp_text.insert(tk.END, "      说明：%s\n" % row["note"], ("note",))
            self.imp_text.configure(state="disabled")
            self.imp_btn.configure(state="normal" if info["has_importable"] else "disabled")
        except tk.TclError:  # 极端情况下控件已销毁，忽略
            pass

    def _show_import_help(self) -> None:
        """弹窗展示「导入 / 导出」的完整说明与各工具支持情况。"""
        lines = [
            "「数据导入」= 把其它软件的历史会话，以当前所选工具的原生格式写出，",
            "使其能被当前工具像原生会话一样打开（会话内容、推理与工具调用尽量保留）。",
            "",
            "· 仅支持明文可读会话的工具；会话主库加密的工具（如下方标注）只能整体",
            "  备份 / 还原，无法跨软件互换。",
            "· 导入不会覆盖目标工具已有会话：新会话使用全新生成的 id。",
            "· 部分工具对「工作区 / 项目路径」敏感（CodeBuddy / DSH），建议保持目标机",
            "  项目路径与源机器一致，否则会话可能不被索引显示。",
            "",
        ]
        for tool in import_matrix.known_targets():
            lines.extend(import_matrix.format_lines(tool))
            lines.append("")
        self._show_text_dialog("数据导入说明", "\n".join(lines))

    def _show_text_dialog(self, title: str, text: str) -> None:
        """通用只读文本弹窗（可滚动、可复制）。

        无头测试下退化为 ``messagebox.showinfo``，避免弹出真实窗口。
        """
        if HEADLESS:
            messagebox.showinfo(title, text)
            return
        win = tk.Toplevel(self.root)
        win.title(title)
        _clamp_dialog_size(win, 760, 560)
        win.transient(self.root)
        frame = ttk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        txt = tk.Text(frame, wrap="word", relief=tk.FLAT)
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy, width=12).pack(pady=(0, 10))
        try:
            win.grab_set()
        except tk.TclError:
            pass
        return win

    # ------------------------------------------------------------------ #
    # 菜单（设置 / 帮助）—— 与「AI 工具」同行、右侧对齐，零新增窗口高度
    # ------------------------------------------------------------------ #
    def _build_row_menus(self, row: ttk.Frame) -> None:
        """在「AI 工具」那一行的**右侧**放置「设置 / 帮助」两个菜单按钮。

        ★ 为什么放这一行（2026-10-03 用户定案）：该行原本只有一个左对齐 Label
        与 Combobox，**右侧全空**；菜单放进来的**行高不变** ⇒ ``_fit_layout()``
        的高度基准不用重算（``root.config(menu=…)`` 会独占一行、必须重新校准）。
        宽度增量由内容区既有的「横向撑满 / 按需横向滚动」机制吸收。

        ⚠️ ``pack(side=RIGHT)`` 是「**先** pack 的贴最右」⇒ 必须先 pack「帮助」、
        再 pack「设置」，视觉上才是「设置 ｜ 帮助」。
        ⚠️ 本行保持 ``pack``，不要改成 ``grid``（跨机制改会让宽度基准不一致）。
        ★ ``padding=(4, 3)`` 是**实测选出来的**：ttk Menubutton 默认请求高 25px，
        比同行 ttk Combobox 的 23px 高 2px，会把这一行的请求高度从 23 顶到 25
        ⇒ 「零新增高度」就不成立了。``(4, 3)`` 恰好也是 23px（实测 4 档取值），
        与下拉框齐平、按钮又不显局促。
        """
        help_mb = ttk.Menubutton(row, text="帮助", padding=(4, 3))
        help_menu = tk.Menu(help_mb, tearoff=False)
        help_menu.add_command(label="使用说明", accelerator="F1",
                              command=self._show_help)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self._show_about)
        help_mb["menu"] = help_menu
        help_mb.pack(side=tk.RIGHT)

        set_mb = ttk.Menubutton(row, text="设置", padding=(4, 3))
        set_menu = tk.Menu(set_mb, tearoff=False)
        set_menu.add_command(label="更新设置…", command=self._show_update_settings)
        set_mb["menu"] = set_menu
        set_mb.pack(side=tk.RIGHT, padx=(0, 6))

        self._menu_buttons = (help_mb, set_mb)
        # 热键只挂**无副作用**的项（使用说明）；更新动作不挂，避免误触替换程序。
        self.root.bind("<F1>", lambda _e: self._show_help())

    def _show_help(self) -> None:
        """「帮助 → 使用说明」：纯文本查看器（与 CLI ``--docs`` **同源**）。

        「同源」是指两边都用 :func:`doctext.md_to_paragraphs` 的同一份输出，
        不是靠人工同步两份文案 ⇒ 这里不许自己写第二套 markdown 解析。

        ★ **表格宽度按窗口实测**：渲染前量一次正文区能放多少显示列，传给
        ``md_to_paragraphs``；窗口缩放后重新量、重新排版（见
        :meth:`_render_doc`）。曾经的实现把可用宽度写死在 ``doctext`` 里，
        于是「把窗口拉到最大，表格仍是分条形态」——渲染结果与窗口宽度无关。
        ★ 表格与分条之间只有**一个**切换阈值（``doctext._table_min_total``）：
        缩小到某宽度变分条、放大后再变回表格，用的是同一个宽度，不会错开。
        """
        try:
            text = read_help_doc()
        except OSError:
            # 打包漏带资源属构建缺陷，但不该抛异常；给可落地的下一步。
            messagebox.showwarning(
                "使用说明不可用",
                "未找到使用说明文档。\n\n"
                "源码模式下应存在 docs/使用说明.md；打包版本可能是构建时漏带了资源。\n"
                "可访问项目主页查看在线文档：\n"
                "https://github.com/%s/%s"
                % (_version.PROJECT_OWNER, _version.PROJECT_REPO),
            )
            return
        if HEADLESS:
            messagebox.showinfo("使用说明", text)
            return
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        _clamp_dialog_size(win, 880, 660)
        win.transient(self.root)
        frame = ttk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        # ★ ``wrap="char"`` 而不是默认的 ``"word"``：本查看器读的是**中文为主的文档**，
        # 而 Tk 的 word 折行只认空格当断点。中文句子动辄上百列没有一个空格 ⇒
        # 它只能回溯到段首那个空格（如列表项的 ``- ``）就断，结果是「一行只有一个
        # ``-``」＋右侧大片留白。按字符折行才是中文的正确断点粒度。
        # 英文词被劈开的代价可接受：真文档里的长英文串（路径等）几乎都在**表格**里，
        # 而表格行是我们自己按宽度折好的，不会走到控件折行这一步。
        txt = scrolledtext.ScrolledText(
            frame, wrap="char", relief=tk.FLAT, padx=8, pady=6
        )
        txt.pack(fill=tk.BOTH, expand=True)
        self._configure_doc_tags(txt)
        # 挂个句柄：``ScrolledText`` 内部自带 Frame + 滚动条，从窗口遍历控件找不到
        # 正文控件；测试要断言「图片行确实没进来」只能靠这个引用。
        win.doc_text = txt
        win.doc_source = text        # 原文；重排时反复渲染的是它，不是已渲染结果
        win.doc_paras = []           # 上次渲染出的段落序列（内容没变就不动控件）
        win.doc_cols = None
        win.doc_job = None
        self._render_doc(win, first=True)
        txt.bind("<Configure>", lambda _e: self._schedule_doc(win))
        ttk.Button(win, text="关闭", command=win.destroy, width=12).pack(pady=(0, 10))
        try:
            win.grab_set()
        except tk.TclError:
            pass
        return win

    def _render_doc(self, win, first: bool = False) -> None:
        """按**当前窗口宽度**渲染使用说明；内容与上次一致时什么都不做。

        「先渲染再比较」而不是「比较宽度再渲染」是刻意的：一块宽度可能让 A 表换形态、
        B 表不变，只有真渲染出段落序列才能判断这次重排有没有意义。比较相等就早退，
        于是 ``<Configure>`` 不会引发「重排 → 滚动条变化 → 又触发 Configure」的振荡，
        用户光标处的滚动位置也就不会被无谓地重置。
        """
        txt = win.doc_text
        cols = self._doc_available_columns(txt)
        paras = md_to_paragraphs(win.doc_source, cols)
        if not first and paras == win.doc_paras:
            win.doc_cols = cols
            return
        anchor = None
        if not first:
            try:
                anchor = txt.index("@0,0")   # 记下顶部的字符位置，重排后滚回去
            except tk.TclError:
                anchor = None
        try:
            txt.configure(state="normal")
            txt.delete("1.0", tk.END)
            for para, tag in paras:
                txt.insert(tk.END, para + "\n", tag)
            txt.configure(state="disabled")
        except tk.TclError:          # 窗口在重排途中被关掉
            return
        win.doc_paras = paras
        win.doc_cols = cols
        if anchor is not None:
            try:
                txt.see(anchor)
            except tk.TclError:
                pass

    def _schedule_doc(self, win) -> None:
        """窗口缩放 → 防抖后重排（拖拽期间每个像素都触发 Configure，不能当场重排）。"""
        if win.doc_job is not None:
            try:
                win.after_cancel(win.doc_job)
            except tk.TclError:
                return
        try:
            win.doc_job = win.after(180, lambda: self._rerender_doc(win))
        except tk.TclError:
            win.doc_job = None

    def _rerender_doc(self, win) -> None:
        win.doc_job = None
        try:
            self._render_doc(win)
        except tk.TclError:
            pass

    def _doc_available_columns(self, txt) -> int:
        """实测使用说明正文区能容纳多少**显示列**（东亚字符算 2 列）。

        两条实测事实决定了这里必须「量」而不是「算」：

        - 表格用等宽字体（标签 ``table``），所以**一列宽的像素值**可以在同一字体上
          量出来：量 32 个 ``0`` 取平均，比 ``measure("M")`` 稳（后者在 YaHei 下
          12 px 而实际平均 7 px，差 70%）。
        - 正文区宽度 = 控件宽 − 内边距/边框 − 右侧滚动条宽。滚动条是
          ``ScrolledText`` 内部的子控件，不扣掉就会多算约 17 px。
        """
        try:
            txt.update_idletasks()
            fnt = tkfont.Font(font=txt.tag_cget("table", "font"))
            char_px = max(1.0, fnt.measure("0" * 32) / 32.0)
        except tk.TclError:
            return _TABLE_MAX_WIDTH
        width = txt.winfo_width()
        if width <= 1:               # 窗口还没映射出真实尺寸，先按默认宽度渲染
            return _TABLE_MAX_WIDTH
        chrome = 0
        for opt in ("borderwidth", "padx", "highlightthickness"):
            try:
                chrome += 2 * int(txt.cget(opt))
            except (tk.TclError, ValueError):
                pass
        vbar = getattr(txt, "vbar", None)
        if vbar is not None:
            try:
                chrome += vbar.winfo_width()
            except tk.TclError:
                pass
        # 下限 20 列：再窄也没法用，``doctext`` 那边会自己退成「分条」。
        return max(20, int((width - chrome) // char_px))

    @staticmethod
    def _configure_doc_tags(txt: "tk.Text") -> None:
        """给使用说明查看器配置各段落标签的字体与缩进（纯排版，不含业务逻辑）。

        ★ 刻意**不设 ``background``**：系统暗色主题下硬编码浅底会让文字看不见，
        而 Tk 默认前景/背景已随主题 ⇒ 只调字体、缩进与颜色以外的间距。
        """
        base = tkfont.nametofont("TkDefaultFont")
        mono = tkfont.nametofont("TkFixedFont")
        family = base.actual("family")
        size = base.actual("size")
        for tag, boost in (("h1", 4), ("h2", 3), ("h3", 2), ("h4", 1)):
            txt.tag_configure(
                tag, font=(family, size + boost, "bold"), spacing1=10, spacing3=4
            )
        txt.tag_configure("p", spacing3=4)
        txt.tag_configure("li", lmargin1=18, lmargin2=32, spacing3=2)
        txt.tag_configure("quote", lmargin1=18, lmargin2=18, foreground="#666")
        txt.tag_configure("code", font=mono, lmargin1=18, lmargin2=18)
        txt.tag_configure("table", font=mono, spacing1=4, spacing3=6)
        # 极窄窗口（网格折不动了）才会出现的「分条」形态：内容是「标题 + 缩进字段」，
        # 不需要对齐 ⇒ 不设等宽字体，只把折行后的续行按缩进对齐（lmargin2），
        # 否则长句折行会顶到最左边、「 列名：值」的层级感全丢。
        txt.tag_configure("records", lmargin2=14, spacing1=1, spacing3=2)
        txt.tag_configure("hr", spacing1=4, spacing3=4)

    def _show_about(self) -> None:
        """「帮助 → 关于」：版本 / 检查更新 / 版权 / 项目链接 / 打赏二维码。"""
        if HEADLESS:
            messagebox.showinfo("关于", "\n".join(_version.about_lines(
                self.adapter.display_name)))
            return
        AboutDialog(self)

    def _show_update_settings(self) -> None:
        """「设置 → 更新设置」：自动检查开关 / 频率 / 通道 / 代理。"""
        if HEADLESS:
            messagebox.showinfo("更新设置", "（单元测试下不弹窗）")
            return
        UpdateSettingsDialog(self)

    def on_migrate(self) -> None:
        """打开「导入会话」对话框（仅当前工具支持导入时可用）。"""
        if self.busy:
            messagebox.showinfo("请稍候", "当前有任务正在进行，请等待完成后再试。")
            return
        info = import_matrix.describe(self.adapter.name)
        if not info["has_importable"]:
            messagebox.showinfo(
                "暂不支持导入",
                "「%s」目前不支持从其它软件导入会话。\n\n%s"
                % (self.adapter.display_name, info["summary"]),
            )
            return
        MigrateDialog(self)

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

    def _refresh_detect_status(self, ok: bool) -> None:
        """刷新「识别状态」区：列出数据目录下探测到的各数据根目录及状态。

        位于数据目录输入框与当前用户行之间，仅做展示增强，不改变单路径数据目录模型。
        严格成对处理：``ok`` 为 False 时清空逐行并提示未找到；恢复 True 时重新填充，
        绝不残留旧文案。
        """
        # 清空上一次逐行内容（成对处理：先清后填，避免旧行残留）
        for w in self.detect_rows_frame.winfo_children():
            w.destroy()
        # 识别行数变化同样改变内容总高 → 作废布局基准（含下方的提前 return 分支）
        self._invalidate_fixed()
        # 每次刷新先收起滚动条，填充后按需再显示（成对处理，避免残留）
        try:
            self.detect_rows_sb.grid_remove()
        except Exception:
            pass
        if not ok:
            self.detect_summary.config(
                text="未找到数据目录，请手动指定", foreground="#c00"
            )
            return
        roots = self.adapter.detect_data_roots(self.root_dir)
        found = sum(1 for r in roots if r.get("exists"))
        if not roots:
            self.detect_summary.config(
                text="已识别数据目录（未细分具体根）", foreground="#0a6"
            )
            return
        self.detect_summary.config(
            text="已识别 %d 个数据根目录：" % found, foreground="#0a6"
        )
        # 单项（只探测到 1 个数据根）时不显示独立 summary 描述行（避免 1 个
        # 数据根时看着偏高+留白），「已识别」信息并入 canvas 首行前缀；多项
        # 时保持独立 summary 行。判据用「根的总数==1」而非「found==1」——qoder
        # 永远只返回 1 个根（含 found=0 即目录不存在的情况），无论该根是否
        # 存在都应单行紧凑、隐藏 summary，避免「偏高+空行」。
        # 成对处理：根数变化（1 ↔ 多）时 summary 的 pack 状态对应切换，绝不残留旧 pack。
        if len(roots) == 1:
            if self._detect_summary_packed:
                # grid 控件用 grid_remove 隐藏（保留 grid 配置便于重新显示）
                self.detect_summary.grid_remove()
                self._detect_summary_packed = False
            r0 = roots[0]
            leading = "已识别 %d 个数据根目录：" % found if r0.get("exists") else "未找到数据根目录："
        else:
            if not self._detect_summary_packed:
                # 恢复 summary 显示（grid row=0, column=0 与 __init__ 一致）
                self.detect_summary.grid(row=0, column=0, sticky="ew")
                self._detect_summary_packed = True
            leading = None
        for r in roots:
            rel = r.get("rel", "")
            exists = r.get("exists", False)
            note = r.get("note", "")
            status = "✓ 已识别" if exists else "✗ 未找到"
            color = "#0a6" if exists else "#c00"
            line = "• %s   %s%s" % (rel, status, ("  " + note) if note else "")
            if leading:
                line = leading + "  " + line
                leading = None  # 仅首行加前缀
            lbl = ttk.Label(
                self.detect_rows_frame, text=line, foreground=color, anchor="w"
            )
            # 文字 label 自身也绑定滚轮：Tk 的 <MouseWheel> 不会从子 widget 自动
            # 冒泡到父 frame，鼠标停在文字上时必须由 label 自己拦截，否则「文字
            # 区域滚轮无反应、只能在滚动条上滚」。handler 复用实例方法。
            lbl.bind("<MouseWheel>", self._on_detect_mousewheel)
            lbl.bind("<Button-4>", self._on_detect_mousewheel)
            lbl.bind("<Button-5>", self._on_detect_mousewheel)
            lbl.pack(fill=tk.X, anchor="w", pady=0)
        # 动态显隐竖向滚动条：内容超过最大高度（两行）才显示，否则收起，
        # 避免数据根多时也把主窗口整体高度撑高（主窗高度固定为屏 3/4）。
        # canvas 高度自适应：内容少则贴合内容、最多两行高（不固定两行）。
        # 稳定记录「是否溢出」供滚轮 handler 复用，避免实时量高导致判据抖动。
        try:
            if getattr(self, "_sync_detect_width", None):
                self._sync_detect_width()
            # 内容高 = **逐行请求高之和**，直接算，不调 update_idletasks 去量父容器：
            # 父容器的请求高是惰性值，为量它必须先刷几何，而这一刷会把刚重建好的
            # 整窗（含全部备份行）重排+重绘一遍——实测单次 180~650ms，换工具时
            # 光这一步就白等 0.3~1.5s。子控件自身的请求高是即时的，累加即可。
            content_h = 0
            for child in self.detect_rows_frame.winfo_children():
                try:
                    content_h += int(child.winfo_reqheight())
                except Exception:
                    pass
            # 高度 = min(内容实际高, 最大两行高)：1 行就 1 行高，2 行以上截断
            canvas_h = min(content_h, self._detect_max_h)
            self.detect_rows_canvas.configure(height=canvas_h)
            self._detect_overflow = content_h > self._detect_max_h
            if self._detect_overflow:
                self.detect_rows_sb.grid()
            else:
                self.detect_rows_sb.grid_remove()
        except Exception:
            self._detect_overflow = False

    def _redetect(self) -> None:
        self.root_dir = self._detect_root(self.root_var.get() or None)
        self.root_var.set(self.root_dir)
        if not os.path.isdir(self.root_dir):
            # 数据目录未识别：把现有清单各项的 path 重映射到「当前（失效的）root_dir」
            # 下对应相对位置，使其 exists 自然为 False，从而触发「未找到」标红与右上角
            # 计数（成对处理，恢复有效后 build_items 会重建、自然复位）。用户明确要求：
            # 目录失效时备份内容各项也要标红提示未找到，且勾选状态保持不变。
            if self.items and getattr(self, "_last_good_root", None):
                old_root = self._last_good_root
                for it in self.items:
                    try:
                        rel = os.path.relpath(it.path, old_root)
                    except ValueError:
                        rel = os.path.basename(it.path)
                    it.path = os.path.join(self.root_dir, rel)
                self._refresh_uid_combo(force_clear=True)  # 当前用户下拉强制清空
                self._refresh_detect_status(False)
                # 保留勾选（_refresh_items 内 prev_sel 沿用当前勾选），仅标红未找到项
                self._refresh_items()
                self._set_status("未找到数据目录，请手动指定")
                self._update_tool_rows_visibility()
                return
            # 从未成功识别过：仍重建一次，生成稳定的占位清单（各项未找到）
        self.items = self.adapter.build_items(self.root_dir)
        self._last_good_root = self.root_dir  # 记录最后有效数据根，供失效时重映射
        self._refresh_items()
        self._refresh_uid_combo()
        self._set_status("已重新检测数据目录")
        self._update_tool_rows_visibility()

    # -------------------------------------------------- DSH 会话健康检测/修复 --
    def _dsh_home_for_check(self) -> str:
        """当前 DSH 数据根（与备份条目一致：适配器探测，含 $DSH_HOME 覆盖）。"""
        from ai_env_clone.adapters import dsh as dsh_adapter

        return dsh_adapter._dsh_home()

    def _update_dsh_health_visibility(self) -> None:
        """dsh 适配器选中时显示「会话健康」行，其他工具隐藏（不破坏布局）。

        用幂等 ``pack`` / ``pack_forget`` 而非 ``winfo_ismapped`` 判断：
        headless（withdraw）下 winfo_ismapped 恒为 False，会导致切换工具时
        该行无法隐藏（见 tests/test_detect_rows.py 的同类说明）。
        """
        frame = getattr(self, "dsh_health_frame", None)
        if frame is None:
            return
        if self.adapter.name == "dsh":
            frame.pack(fill=tk.X, padx=10, pady=(0, 8))
            self._set_dsh_buttons_state()
        else:
            frame.pack_forget()

    def _update_tool_rows_visibility(self) -> None:
        """统一刷新「仅特定工具显示」的行（DSH 会话健康行、WorkBuddy 会话健康行、
        Qoder 历史诊断行）。

        工具切换 / 重新探测数据目录后调用，保证行可见性与当前适配器一致。
        行的进出会改变内容总高，故作废布局基准（重排任务已由调用方的
        ``_refresh_items`` 排好，这里不重复排）。
        """
        self._update_dsh_health_visibility()
        self._update_wb_health_visibility()
        self._update_qoder_diag_visibility()
        self._invalidate_fixed()

    def _update_qoder_diag_visibility(self) -> None:
        """qoder 适配器选中时显示「历史会话诊断」行，其他工具隐藏（不破坏布局）。

        与 ``_update_dsh_health_visibility`` 同理，用幂等 ``pack`` / ``pack_forget``
        而非 ``winfo_ismapped``：headless（withdraw）下后者恒为 False。
        """
        frame = getattr(self, "qoder_diag_frame", None)
        if frame is None:
            return
        if self.adapter.name == "qoder":
            frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        else:
            frame.pack_forget()

    def _update_wb_health_visibility(self) -> None:
        """workbuddy 适配器选中时显示「会话健康」行，其他工具隐藏（不破坏布局）。

        与 ``_update_dsh_health_visibility`` 同理，用幂等 ``pack`` / ``pack_forget``。
        """
        frame = getattr(self, "wb_health_frame", None)
        if frame is None:
            return
        if self.adapter.name == "workbuddy":
            frame.pack(fill=tk.X, padx=10, pady=(0, 8))
            self._set_wb_buttons_state()
        else:
            frame.pack_forget()

    def _wb_home_for_check(self) -> str:
        """当前 WorkBuddy 数据根（与备份条目同口径：适配器探测 ``~/.workbuddy``）。"""
        from ai_env_clone.adapters import workbuddy as wb_adapter

        return wb_adapter._workbuddy_home()

    def _set_wb_buttons_state(self) -> None:
        """数据根存在才允许点检测/修复（没装过 WorkBuddy 时点了也只是空跑）。"""
        ok = os.path.isdir(self._wb_home_for_check())
        for name in ("wb_check_btn", "wb_fix_btn"):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            try:
                widget.configure(state="normal" if ok else "disabled")
            except tk.TclError:
                pass

    def _wb_check(self) -> None:
        """检测 WorkBuddy 会话索引与磁盘事件流的失配（归属 / 未登记 / 工作区缺行 / 快照）。"""
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        from ai_env_clone.workbuddy_repair import detect_wb_sessions

        wb_home = self._wb_home_for_check()
        self._set_status("正在检测 WorkBuddy 会话健康…")

        def work():
            try:
                result = detect_wb_sessions(wb_home)
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))
                return
            self.msg_queue.put(("wb_report", result))

        self._run_bg(work)

    def _render_wb_report(self, result) -> None:
        """主线程渲染检测结果：更新健康标签 + 弹窗摘要。"""
        text = "\n".join(result.summary_lines())
        self.wb_health_label.configure(
            text=text,
            foreground="#0a6" if result.healthy else "#c60",
        )
        self._fit_layout()
        self._set_status("WorkBuddy 会话健康检测完成")
        title = ("WorkBuddy 会话健康：正常" if result.healthy
                 else "WorkBuddy 会话健康：发现 %d 项需注意" % result.attention_total)
        self._show_plain_report_dialog(title, text)

    def _wb_fix(self) -> None:
        """修复会话数据：生成 dry-run 计划（后台）→ 主线程确认 → 备份并写盘 → 复检。"""
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        from ai_env_clone.workbuddy_repair import plan_wb_repair

        wb_home = self._wb_home_for_check()
        self._set_status("正在生成 WorkBuddy 修复计划（dry-run）…")

        def work():
            try:
                plan = plan_wb_repair(wb_home)
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))
                return
            self.msg_queue.put(("wb_fix_plan", plan))

        self._run_bg(work)

    def _confirm_wb_fix(self, plan) -> None:
        """主线程确认修复计划，确认后后台备份 + 写库 + 移走陈旧快照。"""
        from ai_env_clone.workbuddy_repair import apply_wb_repair

        if plan.empty:
            lines = ["会话索引库与磁盘事件流已配套，无需修复。"]
            lines.extend("- %s" % note for note in plan.notes)
            self._set_status("无需修复")
            self._show_plain_report_dialog(
                "修复 WorkBuddy 会话数据", "\n".join(lines))
            return

        text = ("将执行以下修复（写盘前先备份 workbuddy.db 及 -wal/-shm）：\n\n"
                + "\n\n".join(plan.describe())
                + "\n\n共 %d 项。是否继续？\n\n"
                "注意：请先完全退出 WorkBuddy（含系统托盘图标），否则改动可能被其"
                "内存中的旧列表覆盖。" % plan.total())
        if not self._show_plain_report_dialog("确认修复 WorkBuddy 会话数据", text, ask=True):
            self._set_status("已取消修复")
            return

        wb_home = plan.home or self._wb_home_for_check()
        self._set_status("正在修复 WorkBuddy 会话数据…")

        def work():
            try:
                outcome = apply_wb_repair(wb_home, plan, backup=True)
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(("wb_fix_done", ("修复失败",
                                                    "%s: %s" % (type(exc).__name__, exc), False)))
                return
            if not outcome["ok"]:
                self.msg_queue.put(("wb_fix_done", ("修复失败", outcome["error"], False)))
                return
            lines = []
            if outcome["backups"]:
                lines.append("已备份：%s" % "、".join(
                    os.path.basename(b) for b in outcome["backups"]))
            if outcome["ownership"]:
                lines.append("会话归属改正（user_id 置空串）：%d 条。" % outcome["ownership"])
            if outcome["registered"]:
                lines.append("补登记会话：%d 个（%s）。" % (
                    len(outcome["registered"]),
                    "、".join(s[:8] for s in outcome["registered"][:6])))
            if outcome["workspaces"]:
                lines.append("补登记工作区：%d 个。" % len(outcome["workspaces"]))
            if outcome["snapshots_moved"]:
                lines.append("移走陈旧侧栏快照：%d 个（已备份为 .bak.<时间戳>）。"
                             % len(outcome["snapshots_moved"]))
            for err in outcome.get("snapshot_errors") or []:
                lines.append("· %s" % err)
            if not lines:
                lines.append("无改动。")
            lines.append("现在重新打开 WorkBuddy 查看会话列表；仍看不到请再点一次「检测会话健康」。")
            self.msg_queue.put(("wb_fix_done", ("修复完成", "\n".join(lines), True)))

        self._run_bg(work)

    def _qoder_diag(self) -> None:
        """检测 Qoder 新旧两处数据根，诊断「导入后仍看不到历史会话」的成因。"""
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        from ai_env_clone.adapters.qoder import diagnose_history

        home = os.path.expanduser("~")
        self._set_status("正在检测 Qoder 历史会话…")

        def work():
            try:
                report = diagnose_history(home)
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))
                return
            self.msg_queue.put(("qoder_report", report))

        self._run_bg(work)

    def _render_qoder_report(self, report: dict) -> None:
        """主线程渲染 Qoder 诊断结果（结构化事实 + 结论列表）。"""
        def _n(v):
            return "读取失败" if v is None else str(v)

        lines = [
            "【数据根】",
            "· 新版桌面端（Electron）：%s" % report["electron_root"],
            "  存在：%s" % ("是" if report["electron_exists"] else "否"),
            "· 旧版（插件 / 旧桌面端）：%s" % report["legacy_db"],
            "",
            "【会话与导入账本】",
            "· 新版会话库 main.sqlite：会话 %s 个 / 消息 %s 条"
            % (_n(report["new_sessions"]), _n(report["new_messages"])),
            "· 应用内导入账本 sessionMigration.sqlite：import_job %s / import_history %s"
            % (_n(report["import_jobs"]), _n(report["import_history"])),
            "· 旧版会话库 local.db：会话 %s 个 / 消息 %s 条"
            % (_n(report["legacy_sessions"]), _n(report["legacy_messages"])),
            "· 旧版交接标记 .cn_migration 已落盘：%s"
            % ("是" if report["cn_migration_done"] else "否"),
            "",
            "【结论】",
        ]
        lines += ["· " + f for f in report["findings"]]
        lines += [
            "",
            "说明：旧版 local.db 的容器是标准 SQLite（未整库加密），但会话正文列"
            "（chat_message.content / chat_record.question）为列级密文，正文取不出 —— "
            "故本工具只能整库搬运，无法转成其它软件的原生格式，也无法代 Qoder 完成导入。"
            "跨电脑迁移请用本工具的整库备份 / 还原（Qoder 适配器已覆盖三处数据面："
            "~/.qoder-cn 旧代数据 + CLI/Agent 新族、桌面端 main.sqlite、本地工作区）。",
        ]
        text = "\n".join(lines)
        empty_new = report["new_sessions"] == 0
        self._show_plain_report_dialog(
            "Qoder 历史会话诊断：%s" % ("新版会话库为空" if empty_new else "已读取到新版会话"),
            text,
        )
        self._set_status("Qoder 历史会话诊断完成")

    def _set_dsh_buttons_state(self) -> None:
        """数据目录有效时启用检测按钮；修复按钮另需「存在可修复项」才启用。

        修复按钮置灰规则：目录无效 → 禁用；已检测且明确无任何可修项
        （``_dsh_has_fixable is False``，即既无未分组会话也无陈旧投影缓存）
        → 禁用；其余（未检测 / 有可修项）→ 可用，避免用户点击后被告知
        「无需修复」造成困惑。
        """
        ok = os.path.isdir(self.root_dir)
        check_w = getattr(self, "dsh_check_btn", None)
        if check_w is not None:
            try:
                check_w.configure(state="normal" if ok else "disabled")
            except tk.TclError:
                pass
        fix_w = getattr(self, "dsh_fix_btn", None)
        if fix_w is not None:
            # 数据目录可用即允许点「修复」：健康检测看不到「子代理关系修复」的待办
            # （那要靠来源数据现算，来源还是自动查找的），用检测结论去置灰会让用户
            # 明明有问题却点不动。真没可修项时，修复流程自己会如实说「无需修复」。
            fix_ok = ok
            try:
                fix_w.configure(state="normal" if fix_ok else "disabled")
            except tk.TclError:
                pass

    def _pick_relink_source(self, as_package: bool) -> None:
        """选「外部导入会话的来源」——**只在自动查找不到时才需要**。

        按「需要修复的那批外部导入会话原本来自哪个工具」来选：那个工具的**数据目录**，
        或它的**备份包**（zip）。
        """
        if as_package:
            path = filedialog.askopenfilename(
                title="选择外部导入会话的备份包",
                filetypes=[("备份包", "*.zip"), ("全部文件", "*.*")])
        else:
            path = filedialog.askdirectory(title="选择外部导入会话的数据目录")
        if path:
            self.dsh_relink_source_var.set(path)
            self._set_dsh_buttons_state()

    def _refresh_relink_hint(self) -> None:
        """回显「来源留空＝自动查找」这层含义（用户不必猜不填会怎样）。"""
        var = getattr(self, "dsh_relink_hint_var", None)
        if var is None:
            return
        if (self.dsh_relink_source_var.get() or "").strip():
            var.set("将只用你指定的这个来源判定父子关系（外部导入会话原本所属工具的数据目录"
                    "或备份包），不再自动查找其它数据与备份包")
        else:
            var.set("未指定来源：会自动查找这些外部导入会话原本来自哪个工具（本机数据、"
                    "本工具的备份包与上次用过的备份包目录都会找一遍）。若自动找不到，请按"
                    "「需要修复的那批会话原本来自哪个工具」选它的数据目录或备份包；"
                    "「检测会话健康」会列出本次实际使用的来源")

    def _dsh_check(self) -> None:
        """检测 DSH 会话健康：未分组 / 索引问题 / 旧格式 replayState / 重复调用 ID /
        陈旧投影缓存 / 旧版导入的子代理会话（含判定所用的来源）。"""
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        from ai_env_clone.dsh_repair import detect_ungrouped, zstd_backend

        dsh_home = self._dsh_home_for_check()
        decompress, _compress, zstd_name = zstd_backend()
        relink_source = (self.dsh_relink_source_var.get() or "").strip()
        self._set_status("正在检测 DSH 会话健康…")

        def work():
            result = detect_ungrouped(dsh_home, decompress=decompress,
                                      relink_source=relink_source)
            self.msg_queue.put(("dsh_report", (result, zstd_name)))

        self._run_bg(work)

    def _show_plain_report_dialog(self, title: str, text: str,
                                  ask: bool = False) -> bool:
        """显示白底黑字的自定义报告弹窗（可滚动、按钮常驻可见）。

        工具无关：DSH「会话健康」、修复计划确认、Qoder「历史会话诊断」等报告共用。

        为什么不用 ``messagebox``：它的尺寸由系统决定，正文一长（修复计划会逐条列几十行）
        就把底部按钮顶出屏幕，用户连「继续 / 取消」都点不到（2026-10-05 实测反馈）。
        这里：默认尺寸用 ``_clamp_dialog_size`` 夹进工作区，正文放**只读 Text + 滚动条**，
        按钮单独占一行固定在底部——内容再多也点得到。

        :param ask: ``True`` 时给「继续 / 取消」两个按钮并返回用户选择；``False`` 只给「确定」。
        ``HEADLESS``（单元测试）下退化为 ``messagebox``：自定义弹窗会 ``grab_set()`` +
        ``wait_window()`` 阻塞等用户点击，在无头测试里会**永久挂起**，故必须避开；
        测试侧已 mock ``messagebox``，行为等价且不阻塞。
        """
        if HEADLESS:
            if ask:
                return bool(messagebox.askyesno(title, text))
            messagebox.showinfo(title, text)
            return True

        answer = {"ok": False}
        top = tk.Toplevel(self.root)
        top.title(title)
        top.transient(self.root)
        top.configure(bg="white")
        # 正文可滚动 ⇒ 窗口高度只受工作区限制，不再随内容无限增高
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        try:
            icon = self.root.wm_iconbitmap()
            if icon:
                top.iconbitmap(icon)
        except Exception:
            pass
        wrap = tk.Frame(top, bg="white")
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        body = tk.Text(wrap, wrap="word", height=16, width=64, bg="white", fg="black",
                       relief="flat", padx=12, pady=12, font=("", 10), takefocus=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=body.yview)
        body.configure(yscrollcommand=sb.set)
        body.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        body.insert("1.0", text)
        body.configure(state="disabled")     # 只读：可选中复制，不可编辑
        body.yview_moveto(0.0)

        def _wheel(event):
            step = -1 if (getattr(event, "num", None) == 4
                          or getattr(event, "delta", 0) > 0) else 1
            body.yview_scroll(step, "units")
            return "break"

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(seq, _wheel)

        row = ttk.Frame(top)
        row.grid(row=1, column=0, sticky="ew", pady=(8, 10))

        def _close(ok: bool) -> None:
            answer["ok"] = ok
            top.destroy()

        if ask:
            ttk.Button(row, text="继续", command=lambda: _close(True),
                       width=10).pack(side=tk.RIGHT, padx=(6, 12))
            ttk.Button(row, text="取消", command=lambda: _close(False),
                       width=10).pack(side=tk.RIGHT)
        else:
            ttk.Button(row, text="确定", command=lambda: _close(True),
                       width=10).pack(side=tk.RIGHT, padx=(6, 12))

        w, h = _clamp_dialog_size(top, 780, 560)
        top.minsize(420, 260)
        top.protocol("WM_DELETE_WINDOW", lambda: _close(False))
        top.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - top.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - top.winfo_height()) // 2
        # 位置与尺寸**一次**给出：分成「先尺寸、后位置」两次调用时，几何管理器可能按
        # 控件的请求尺寸重排，窗口会缩成没法用的大小（实测退化成 1x1）。
        top.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))

        top.grab_set()
        top.wait_window(top)
        return bool(answer["ok"])

    def _dsh_fix(self) -> None:
        """修复会话数据：workspace 索引归属 + 会话文件内容 + 移走陈旧投影缓存。

        流程：后台生成联合修复计划（dry-run）→ 主线程确认 → 备份并写盘 → 复检。
        """
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        from ai_env_clone.dsh_repair import plan_dsh_repair

        dsh_home = self._dsh_home_for_check()
        fix_dup_ids = bool(self.dsh_fix_dup_var.get())
        relink_source = (self.dsh_relink_source_var.get() or "").strip()
        self._set_status("正在生成修复计划（dry-run）…")

        def work():
            plan = plan_dsh_repair(dsh_home, fix_dup_ids=fix_dup_ids,
                                   relink_source=relink_source)
            self.msg_queue.put(("dsh_fix_plan", plan))

        self._run_bg(work)

    def _render_dsh_report(self, result, zstd_name: str) -> None:
        """主线程渲染检测结果：更新健康标签 + 弹窗摘要。"""
        lines = result.summary_lines()
        text = "\n".join(lines)
        self.dsh_health_label.configure(
            text=text,
            foreground="#0a6" if result.healthy else "#c60",
        )
        # zstd 缺失时显示「安装并重新检测」按钮，否则隐藏（成对处理，不留残留）
        if getattr(result, "zstd_missing", False):
            self.dsh_zstd_install_btn.pack(fill=tk.X, pady=(4, 0))
        else:
            self.dsh_zstd_install_btn.pack_forget()
        # 记录「是否还有可修项」（未分组会话 / 陈旧投影缓存 / 待改造的旧导入子代理会话）
        self._dsh_has_fixable = bool(
            getattr(result, "ungrouped", [])
            or getattr(result, "projcache_stale", [])
            or getattr(result, "relink_targets", [])
        )
        self._set_dsh_buttons_state()
        self._fit_layout()
        self._set_status("DSH 会话健康检测完成")
        if result.healthy:
            title = "DSH 会话健康：正常"
        else:
            title = "DSH 会话健康：发现 %d 项需注意" % result.attention_total
        self._show_plain_report_dialog(title, text)

    def _dsh_install_zstd(self) -> None:
        """点击「安装 zstd 并重新检测」：后台安装 zstd 后端后重新探测并复检。

        仅当环境可就地安装时（源码运行）才尝试 pip；打包后的 exe 冻结环境
        无法把 zstd 装进自身，直接标记为不可安装，交由主线程提示改用源码运行。
        """
        if self.busy:
            messagebox.showwarning("请稍候", "当前有任务正在执行。")
            return
        self._set_status("正在安装 zstd 解压支持…")

        def work():
            from ai_env_clone.dsh_repair import zstd_backend

            # 冻结（PyInstaller 单文件 exe）环境：sys.path 指向临时 _MEI 目录，
            # pip 装不进运行期，避免无谓网络等待。
            frozen = bool(getattr(sys, "frozen", False))
            ok = False
            name = ""
            if not frozen:
                for pkg in ("zstandard", "pyzstd"):
                    code = subprocess.run(
                        [sys.executable, "-m", "pip", "install", pkg],
                        capture_output=True, text=True,
                    ).returncode
                    if code == 0:
                        break
                decompress, _compress, name = zstd_backend()  # 重新探测（会重新 import）
                ok = decompress is not None
            self.msg_queue.put(("dsh_zstd_installed", (ok, name, frozen)))

        self._run_bg(work)

    def _on_zstd_installed(self, payload) -> None:
        """主线程处理安装结果：成功则隐藏按钮并复检；失败则区分环境给出指引。"""
        ok, name, frozen = payload
        if ok:
            self._set_status("已安装 zstd（%s），正在重新检测…" % name)
            # 隐藏按钮；复检后若 zstd 已可用，_render_dsh_report 不会再显示它
            self.dsh_zstd_install_btn.pack_forget()
            self._fit_layout()
            self._dsh_check()
            return
        self._set_status("zstd 安装失败")
        if frozen:
            messagebox.showwarning(
                "无法在打包程序中安装",
                "当前运行的是打包后的 AiEnvClone.exe，无法就地安装 zstd。\n\n"
                "方式一：改源码运行（推荐）\n"
                "  1) 删除 dist\\AiEnvClone.exe\n"
                "  2) 重新运行 run.bat（会自动回退到 Python 源码运行）\n"
                "  3) 在源码模式下点击「安装 zstd 并重新检测」完成安装\n\n"
                "方式二：手动命令行安装\n"
                "  pip install zstandard\n"
                "  python -m ai_env_clone",
            )
        else:
            messagebox.showerror(
                "zstd 安装失败",
                "自动安装 zstandard / pyzstd 失败（可能是网络问题）。\n"
                "请手动执行：pip install zstandard，然后重新检测。",
            )

    def _confirm_dsh_fix(self, plan) -> None:
        """主线程确认联合修复计划，确认后后台备份 + 写盘。"""
        from ai_env_clone.dsh_repair import apply_dsh_repair

        fix_dup_ids = bool(self.dsh_fix_dup_var.get())
        relink = getattr(plan, "relink", None)
        if plan.empty:
            lines = []
            if not plan.index_plan.mutations:
                lines.append("未发现可自动归属的未分组会话。")
            lines.extend("- 跳过 %s：%s" % (sid, reason) for sid, reason in plan.index_plan.skipped)
            if not plan.data_reports:
                lines.append("会话文件内容无需修复。")
            lines.append("投影缓存无陈旧记录。")
            if relink is not None:
                lines.append("未发现需要修复的子代理会话关系。")
                if relink.source_note:
                    lines.append("（子代理关系判定所用的来源：%s）" % relink.source_note)
            if plan.zstd_note:
                lines.append(plan.zstd_note)
            self._set_status("无需修复")
            self._show_plain_report_dialog("修复 DSH 会话数据", "\n".join(lines) or "无需修复")
            return

        parts = []
        if plan.index_plan.mutations:
            parts.append(
                "① 索引归属修复与工作区目录补建（仅增量：登记会话、补齐字段、补建缺失"
                "目录，绝不删除任何条目）：\n"
                + "\n".join("   - " + line for line in plan.index_plan.describe())
            )
        if plan.data_reports:
            rows = []
            for report in plan.data_reports:
                rules = "、".join("%s×%d" % (a["rule"], a["count"]) for a in report["actions"])
                rows.append("   - %s：%s" % (os.path.basename(report["file"]), rules))
            parts.append(
                "② 会话文件内容修复（写盘前逐个备份 <文件>.bak.<UTC>）：\n"
                + "\n".join(rows)
            )
        if plan.projcache_stale:
            parts.append(
                "③ 移走陈旧投影缓存（不重写缓存值，由 DSH 冷读时按最新日志重算，"
                "原记录备份为 <sid>.json.bak-<时间戳>）：\n"
                + "\n".join(
                    "   - %s：%s" % (stale.session_id, stale.reason)
                    for stale in plan.projcache_stale
                )
            )
        relink = getattr(plan, "relink", None)
        if relink is not None and getattr(relink, "targets", None):
            parts.append(
                "④ 子代理关系修复（把旧版导入留下的顶层子代理会话改成 DSH 原生子代理会话，"
                "会话正文一字不改；旧会话目录整体移到 sessions/.removed/、其 id 从工作区列表"
                "摘除、原投影缓存移走，均保留备份，可用 .removed 里的目录回退）：\n"
                + "\n".join("   - " + line for line in relink.describe()
                            if not line.startswith("跳过"))
            )
        relink_skipped = [line for line in
                          (getattr(relink, "describe", lambda: [])() if relink else [])
                          if line.startswith("跳过")]
        note = ""
        if not fix_dup_ids:
            note = "\n\n注：未勾选「同时修复重复调用 ID」，其内容将保持原样（需要时可勾选后重跑）。"
        elif plan.zstd_note:
            note = "\n\n注：%s" % plan.zstd_note
        if relink_skipped:
            note += "\n\n子代理关系修复中未能处理的条目：\n" + "\n".join(relink_skipped[:8])
        total = (
            len(plan.index_plan.mutations)
            + len(plan.data_reports)
            + len(plan.projcache_stale)
            + len(getattr(relink, "targets", []) if relink else [])
        )
        if total == 0:
            # 只有「跳过」没有可执行项：别让用户对着「共 0 项」点确认。
            self._set_status("无需修复")
            self._show_plain_report_dialog(
                "修复 DSH 会话数据",
                "\n".join([line for line in (relink.describe() if relink else [])]
                          or ["无需修复"]) + note)
            return
        text = "将执行以下修复：\n\n" + "\n\n".join(parts) + "\n\n共 %d 项。是否继续？" % total + note
        if not self._show_plain_report_dialog("确认修复 DSH 会话数据", text, ask=True):
            self._set_status("已取消修复")
            return

        dsh_home = self._dsh_home_for_check()
        self._set_status("正在修复 DSH 会话数据…")

        def work():
            outcome = apply_dsh_repair(dsh_home, plan, fix_dup_ids=fix_dup_ids, backup=True)
            index_res = outcome["index"]
            file_reports = outcome["files"]
            projcache = outcome.get("projcache") or []
            backups = [r["backup"] for r in file_reports if r["backup"]]
            ok = (
                index_res.ok
                and all(r["status"] != "拒绝" for r in file_reports)
                and all(p["ok"] for p in projcache)
                and all(c["ok"] for c in (getattr(index_res, "created", None) or []))
            )
            lines = []
            if index_res.applied:
                lines.append(
                    "索引归属修复 %d 处%s。"
                    % (index_res.applied, "；备份：%s" % index_res.backup_path if index_res.backup_path else "")
                )
            created = getattr(index_res, "created", None) or []
            if created:
                good = [c for c in created if c["ok"]]
                failed = [c for c in created if not c["ok"]]
                lines.append("补建缺失的工作区目录 %d 个。" % len(good))
                for c in failed:
                    lines.append("  · 未能补建 %s：%s" % (c["path"], c["error"]))
            if projcache:
                moved = [p for p in projcache if p["ok"]]
                lines.append(
                    "移走陈旧投影缓存 %d 个，备份：%s"
                    % (len(moved),
                       "、".join(os.path.basename(p["backup"]) for p in moved) or "（未生成）")
                )
            if file_reports:
                fixed = [r for r in file_reports if r["status"] == "已修复"]
                lines.append(
                    "会话文件修复 %d 个，备份：%s"
                    % (len(fixed), "、".join(os.path.basename(b) for b in backups) or "（未生成）")
                )
                for report in file_reports:
                    for action in report["actions"]:
                        lines.append("  · %s（%d 处）" % (action["rule"], action["count"]))
            relink_res = (outcome.get("relink") or {})
            relink_targets = relink_res.get("targets") or []
            if relink_targets:
                good = [r for r in relink_targets if r["ok"]]
                lines.append(
                    "子代理关系修复 %d 条（旧目录已移到 sessions/.removed/，可回退）"
                    % len(good))
                for r in relink_targets:
                    if not r["ok"]:
                        lines.append("  · %s 失败：%s" % (r["child_id"], r["error"]))
                moved_parents = relink_res.get("parents") or []
                if moved_parents:
                    lines.append(
                        "父会话已发布的 v4 移走 %d 个（DSH 下次加载会按子会话证据重新迁移并"
                        "补上 subagent/catalog）：%s"
                        % (len(moved_parents),
                           "、".join(p["parent_id"] for p in moved_parents)))
            if not ok:
                detail = relink_res.get("error") or index_res.error or "、".join(
                    r["problems"][0]["detail"] for r in file_reports
                    if r["status"] == "拒绝" and r["problems"]
                )
                self.msg_queue.put(("dsh_fix_done", ("修复失败", detail, False)))
                return
            self.msg_queue.put(("dsh_fix_done", ("修复完成", "\n".join(lines) or "修复完成", True)))

        self._run_bg(work)

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

        即与启动方式同级目录下的 backup/<工具名>/，按工具名分目录，便于区分
        不同工具的备份数据。不放在被备份工具（如 Qoder）的数据根目录，
        也不在 ai_env_clone 包内部。
        - 源码模式（python -m ai_env_clone）：__main__.py 在 <仓库根>/ai_env_clone/，
          取上级即仓库根，备份落在 <仓库根>/backup/<工具名>/。
        - 打包模式（单文件 exe / app / 二进制）：可执行程序是独立分发物，备份目录
          放在 exe 同级（如 dist/backup/<工具名>/），让程序与它的备份数据在一起，
          便于随程序一起拷贝/迁移，而不必跳回源码仓库根。
        """
        if getattr(sys, "frozen", False):
            # 打包后的 exe：备份目录放在 exe 同级（如 dist/backup/<工具名>/），
            # 让可执行程序与它的备份数据在一起，便于随程序分发/迁移。
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
                    # 队列约定恒为 (kind, payload)；形状不符的消息直接丢弃，
                    # 避免一次解包异常中断 after 重排导致界面卡死。
                    item = self.msg_queue.get_nowait()
                    if not isinstance(item, tuple) or len(item) != 2:
                        continue
                    kind, payload = item
                    if kind == "progress":
                        self.pbar["value"] = payload.percent
                        msg = payload.message
                        self._set_status(
                            "%s (%d/%d)" % (msg[:60], payload.current, payload.total)
                        )
                    elif kind == "done":
                        self.busy = False
                        self.pbar["value"] = 0
                        title, text = payload[0], payload[1]
                        reveal = payload[2] if len(payload) > 2 else []
                        self._set_status(title)
                        messagebox.showinfo(title, text)
                        # 备份完成后若勾选了定位敏感文件，自动打开文件夹并定位脱敏字段
                        if reveal:
                            self._reveal_sensitive_files(reveal)
                    elif kind == "error":
                        self.busy = False
                        self.pbar["value"] = 0
                        self._set_status("操作失败")
                        messagebox.showerror("失败", str(payload))
                    elif kind == "status":
                        self._set_status(payload)
                    elif kind == "dsh_report":
                        # DSH 会话健康检测结果（后台线程产出，主线程渲染）
                        self.pbar["value"] = 0
                        result, zstd_name = payload
                        self._render_dsh_report(result, zstd_name)
                    elif kind == "qoder_report":
                        # Qoder 历史会话诊断结果（后台线程产出，主线程渲染）
                        self.pbar["value"] = 0
                        self._render_qoder_report(payload)
                    elif kind == "dsh_zstd_installed":
                        # zstd 自动安装完成（后台线程产出，主线程处理）
                        self.pbar["value"] = 0
                        self._on_zstd_installed(payload)
                    elif kind == "dsh_fix_plan":
                        # 联合修复计划已生成：主线程确认后执行
                        self.pbar["value"] = 0
                        self._confirm_dsh_fix(payload)
                    elif kind == "dsh_fix_done":
                        # 修复写盘完成：提示并自动复检
                        self.pbar["value"] = 0
                        title, text, ok = payload
                        self._set_status(title)
                        if ok:
                            messagebox.showinfo(title, text)
                        else:
                            messagebox.showerror("修复失败", text)
                        self._dsh_check_job = self.root.after(50, self._dsh_check)
                    elif kind == "wb_report":
                        # WorkBuddy 会话健康检测结果（后台线程产出，主线程渲染）
                        self.pbar["value"] = 0
                        self._render_wb_report(payload)
                    elif kind == "wb_fix_plan":
                        # WorkBuddy 修复计划已生成：主线程确认后执行
                        self.pbar["value"] = 0
                        self._confirm_wb_fix(payload)
                    elif kind == "wb_fix_done":
                        # WorkBuddy 修复写盘完成：提示并自动复检
                        self.pbar["value"] = 0
                        title, text, ok = payload
                        self._set_status(title)
                        if ok:
                            messagebox.showinfo(title, text)
                        else:
                            messagebox.showerror("修复失败", text)
                        self._wb_check_job = self.root.after(50, self._wb_check)
            except queue.Empty:
                pass
            if not self._closing:
                # 排下一 tick 前，先把上一次存的句柄撤掉：`_drain_queue` 也可能被
                # **手动调用**（测试用它泵消息），不撤就会多出一条并行轮询链，而
                # `_after_id` 只记得住一个 ⇒ 另一条链的句柄丢失、窗口销毁时无从
                # 撤销，Tk 便报 "invalid command name ..._drain_queue"（整套测试
                # 实测残留 14 处噪音全是这么来的）。
                # 已经触发过的句柄再 cancel 是空操作，无需区分「已触发 / 未触发」。
                if self._after_id is not None:
                    try:
                        self.root.after_cancel(self._after_id)
                    except tk.TclError:
                        pass
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
        # 布局重排的节流任务同样要撤掉（窗口销毁后再触发会报 invalid command）
        job = getattr(self, "_relayout_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
            self._relayout_job = None
        # DSH / WorkBuddy 修复后的「延迟自动复检」（同样是窗口销毁后会报 invalid command 的回调）
        for attr in ("_dsh_check_job", "_wb_check_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

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
        # 「完整备份」口径核对（主线程取值）：默认勾选项即本工具的**完整备份内容**
        # （会话、记忆、规则等无法从零重建的部分）。用户若取消了其中若干项，必须
        # 在他**刚拿到包**这一刻说清「这个包不完整」——否则等到换机还原才发现缺
        # 数据，而源机器可能已经不用了。只统计磁盘上确实存在的项：不存在项本就
        # 没东西可备，不该被算成「被跳过」。
        sel_ids = {id(it) for it in sel_items}
        skipped_defaults = sorted(
            {
                it.label
                for it in self.items
                if it.recommended
                and it.exists
                and self._agg_prefix(it.key) != "memories_others"
                and id(it) not in sel_ids
            }
        )
        # 敏感项相关：勾选了「定位敏感文件」且存在含敏感凭证的项时，备份后自动打开定位
        locate_sensitive = self.locate_sensitive_var.get()
        sensitive_paths = [
            i.path for i in sel_items if getattr(i, "sensitive", False) and getattr(i, "path", None)
        ]

        def work():
            mf = self.adapter.export(
                zip_path,
                root_dir,
                items=sel_items,
                progress=self._progress_cb,
                max_file_mb=max_mb,
                compresslevel=compresslevel,
            )
            sensitive_labels = [i.label for i in sel_items if getattr(i, "sensitive", False)]
            base_msg = (
                "备份完成！\n\n文件：%s\n包含：%d 个文件\n原始大小：%s\n压缩后：%s"
                % (
                    mf["zip_path"],
                    mf["file_count"],
                    human_size(mf["total_bytes"]),
                    human_size(mf["zip_bytes"]),
                )
            )
            if sensitive_labels:
                base_msg += (
                    "\n\n⚠ 安全提醒：本次勾选了含敏感凭证的项（%s）。"
                    "其中的敏感凭证（apiKey、令牌等）已在备份中脱敏（替换为占位符），"
                    "备份包不含明文凭证。请在源机器单独记下这些凭证，"
                    "还原到目标机器后需手动补填，否则对应功能虽可见但无法使用。"
                    % "、".join(sensitive_labels)
                )
                if locate_sensitive:
                    base_msg += "\n\n已自动打开相关文件并定位到敏感字段，请手动记录后关闭。"
            # 配套条目提醒：勾了某条目、但它「成对存在」的配套条目没勾时明示一次。
            # 典型是 DSH：实时配置 cordis.patch.yml 只存密钥**引用**（apiKeyEnv），
            # 真密钥在 .credentials.yaml（默认不勾）⇒ 不提醒的话，用户还原后模型
            # 直接用不了，且完全看不出原因。提示落在「刚拿到包」这一刻，用户才好
            # 决定是否单独备份。
            companion = companion_notes(sel_items)
            if companion:
                base_msg += "\n\n提示：\n" + "\n\n".join(companion)
            # 携带源设备信息（路径等）：备份完成后明示一次——用户此刻手里刚拿到包，
            # 最可能发生的动作就是把它拷走/发出去，提醒要落在这个时点。
            origin_rows = manifest_origin_info(mf)
            if origin_rows:
                merged: list = []
                for row in origin_rows:
                    sig = (row["label"] or row["key"], row["note"])
                    if sig not in merged:
                        merged.append(sig)
                base_msg += (
                    "\n\n⚠ 隐私提醒：本备份包会携带源机器上的路径等信息：\n%s\n"
                    "这些信息是会话/记录本身自带的（也是还原后把数据归回原工程工作区的依据），"
                    "但把备份包分享、上传或发给他人时，它们会一并外传，请留意。"
                    % "\n".join("　· %s：%s" % (lbl, note) for lbl, note in merged)
                )
            # 完整性口径 + 多机用法：都落在「用户刚把包拿到手」这一刻。前者防止
            # 「以为备全了、换机才发现缺数据」；后者是本工具唯一安全的多机姿势，
            # 只在文档里写等于没写。
            if skipped_defaults:
                base_msg += (
                    "\n\n⚠ 本次不是完整备份：你已跳过 %d 项默认勾选的内容（%s）。\n"
                    "默认勾选项即本工具的完整备份口径（会话、记忆、规则等无法从零重建的"
                    "内容都包含在内）；如需完整备份，请重新勾选后再导一次。"
                    % (len(skipped_defaults), "、".join(skipped_defaults))
                )
            else:
                base_msg += (
                    "\n\n本次为完整备份：默认勾选项（会话、记忆、规则等无法从零重建的"
                    "内容）均已包含；未勾选项为可重建的插件/技能/索引，或出于安全不随包"
                    "携带的凭证（需在新机重新填写）。"
                )
            base_msg += "\n\n用法提醒：%s" % MULTI_MACHINE_CYCLE_HINT
            # done payload 扩展为三元组：(title, text, reveal_paths)；reveal_paths 为空列表时主线程不定位
            reveal = sensitive_paths if (locate_sensitive and sensitive_paths) else []
            self.msg_queue.put(
                (
                    "done",
                    ("导出成功", base_msg, reveal),
                )
            )
            # 记录本次备份按类别反算的实测压缩率，供后续估算按勾选组成校准
            if self.adapter.supports_calibration and mf.get("bytes_by_ext") and mf.get("bytes_by_ext_compressed"):
                self._save_compress_calibration(
                    mf["bytes_by_ext"], mf["bytes_by_ext_compressed"]
                )

        self._run_bg(work)

    # ------------------------------------------------------------------ #
    # 定位敏感文件：自动探测可用编辑器（VS Code / Notepad++ / Notepad--），
    # 优先用支持「命令行跳到指定行」的编辑器精确跳转；都不支持时退化为
    # 用系统默认关联程序（如记事本）仅打开文件、不跳行。
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_editor() -> "tuple[str | None, str]":
        """
        探测系统可用的、能命令行跳到指定行的编辑器。

        返回 ``(editor_path, kind)``：``kind`` 为 ``"vscode"`` / ``"npp"``（含 Notepad--）
        / ``"none"``（无可用跳行编辑器，退化为默认程序打开）。

        探测顺序：``code`` → ``notepad++.exe`` → ``notepad--.exe`` / ``np--.exe``，先查
        ``PATH`` 再查常见安装目录（很多编辑器装了但不在 PATH 里）。
        """
        candidates = [
            ("code", "vscode"),
            ("notepad++.exe", "npp"),
            ("notepad--.exe", "npp"),
            ("np--.exe", "npp"),
        ]
        extra_dirs = []
        if sys.platform.startswith("win"):
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            extra_dirs = [
                os.path.join(pf, "Notepad++"),
                os.path.join(pf86, "Notepad++"),
                os.path.join(pf, "Notepad--"),
                os.path.join(pf86, "Notepad--"),
                os.path.join(pf, "np--"),
                os.path.join(pf86, "np--"),
            ]
        for name, kind in candidates:
            path = shutil.which(name)
            if path:
                return path, kind
            for d in extra_dirs:
                cand = os.path.join(d, name)
                if os.path.isfile(cand):
                    return cand, kind
        return None, "none"

    @staticmethod
    def _open_editor_at_line(editor: "str | None", kind: str, path: str,
                             line: "int | None") -> None:
        """
        用指定编辑器打开 ``path`` 并尽量跳到 ``line`` 行。

        - ``vscode``：``code -g "<path>:<line>"``
        - ``npp``（Notepad++ / Notepad--）：``<exe> -n<line> "<path>"``
        - ``none`` / 无行号：``os.startfile(path)``（系统默认程序，记事本不支持跳行）
        """
        if editor and kind == "vscode" and line:
            subprocess.run([editor, "-g", "%s:%d" % (path, line)], check=False)
        elif editor and kind == "npp" and line:
            subprocess.run([editor, "-n%d" % line, path], check=False)
        else:
            os.startfile(path)  # type: ignore[attr-defined]

    def _reveal_sensitive_files(self, paths: "list[str]") -> None:
        """
        备份完成后自动打开含敏感凭证的原始文件，并尽量定位到敏感字段行。

        - Windows：用 ``explorer /select,"<path>"`` 在资源管理器打开所在文件夹并**高亮该文件**；
          再用能跳行的编辑器（VS Code / Notepad++ / Notepad--，按可用性自动探测）以
          ``-g`` / ``-n`` 参数打开并定位到该文件内首个敏感字段（如 apiKey/令牌）所在行；
          若未装这些编辑器，则退化为 ``os.startfile`` 用系统默认程序（如记事本）仅打开文件、
          不跳行（记事本不支持命令行跳行）。
        - 其他平台：``os.startfile`` 打开文件并打开所在文件夹。
        任何一步失败（找不到字段 / 无编辑器 / 异常）不抛错，只弹一次提示，不影响主流程。
        """
        editor, kind = self._find_editor()
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            folder = os.path.dirname(p)
            # 1) 打开所在文件夹并高亮该文件（Windows 用 /select；其他平台退化为打开文件夹）
            #    注意：explorer 的 /select 必须整体一个参数、路径不再额外加引号，
            #    且不要 shell=True（否则参数被二次拆分导致只打开文档库而非目标文件夹）。
            if sys.platform.startswith("win"):
                try:
                    subprocess.run(
                        ["explorer", "/select,", p], check=False, shell=False
                    )
                except Exception:  # noqa: BLE001
                    try:
                        os.startfile(folder)  # type: ignore[attr-defined]
                    except Exception:
                        pass
            else:
                try:
                    os.startfile(folder)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            # 2) 打开文件并定位到敏感字段行
            line = self._first_sensitive_line(p)
            try:
                self._open_editor_at_line(editor, kind, p, line)
            except Exception:  # noqa: BLE001
                try:
                    os.startfile(p)  # type: ignore[attr-defined]
                except Exception:
                    messagebox.showinfo("打开文件", p)

    # 字段名出现这些片段即视为「敏感凭证」行（与适配器脱敏判定保持一致）
    _SENSITIVE_HINTS = (
        "apikey", "api_key", "token", "secret", "password", "passwd",
        "accesskey", "access_key", "privatekey", "private_key",
        "credential", "auth",
    )

    @staticmethod
    def _first_sensitive_line(path: str) -> "int | None":
        """返回文件首个敏感字段（apiKey/令牌等明文凭证）所在行号（1-based），找不到返回 None。"""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    low = line.lower()
                    if "***redacted***" in low:
                        continue  # 已脱敏占位符，不视为需定位的明文凭证
                    if any(hint in low for hint in QoderBackupApp._SENSITIVE_HINTS):
                        return n
        except OSError:
            return None
        return None

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

        # -------------------------------------------------------------- #
        # 第一道闸门：先认「包属于哪个工具」，再决定能不能在本窗口还原。
        #
        # 收窄后的语义（见 docs/local/从备份包导入方案.md）：**还原只在本工具
        # 自己的备份上进行**。跨工具的包一律拒绝并指路到「导入会话 → 从备份包
        # 导入」——不再提供「按当前窗口工具强行还原」的选项。原因是那条路会
        # 「看着成功」：归档内路径相对**源**适配器的 detect_root()，文件会落对
        # 地方，但同一次调用里的 path_rewrite / restore_index_merge /
        # restore_post_hook / 结构校验全都按**错误的工具**规则执行，结果是
        # 「文件在、列表里看不见」。极端例子是 ZCode（detect_root() 是盘根
        # C:\，包内多一层 Users/<用户名>/），路径段与指纹都对不上。
        #
        # 识别顺序：manifest 的 tool 字段优先；无声明时**跨全部适配器**做结构
        # 指纹识别（当前窗口工具排最前，保住「老包无清单但本来就是本工具」的
        # 兼容）——这样「是别的工具但没清单」也能被正确拒绝并点名。
        # -------------------------------------------------------------- #
        manifest = info.get("manifest") or {}
        pkg_tool, _tool_src = backup_scan.identify_tool(
            info.get("entries") or [], manifest, prefer=self.adapter.name
        )
        if not pkg_tool:
            messagebox.showerror(
                "不可还原",
                "该压缩包缺少有效的工具声明，且内部结构指纹与任何已支持工具都不匹配，\n"
                "无法确认是可还原的备份。\n\n"
                "如果它是别的工具的备份包，请用「导入会话…」→「从备份包导入」"
                "取其中的会话。",
            )
            return
        if pkg_tool != self.adapter.name:
            pkg_display = backup_scan.display_name(pkg_tool)
            messagebox.showerror(
                "不可在此还原",
                "该备份包属于「%s」，而当前窗口是「%s」。\n\n"
                "还原只在本工具自己的备份上进行：跨工具时，两个工具的\n"
                "「路径改写 / 索引登记 / 结构校验」规则完全不同，强行还原会把数据\n"
                "写得「看着在、其实读不到」。\n\n"
                "要取这个包里的会话内容，请走导入通道：\n"
                "主窗口「导入会话…」→「从备份包导入」页签，在那里选中这个包。\n\n"
                "若要整库还原它，请先把主窗口的工具切到「%s」，再重新打开备份浏览器。"
                % (pkg_display, self.adapter.display_name, pkg_display),
            )
            return

        # 携带源设备信息（相对路径 / 标记）的条目：还原会写回本机对应位置，
        # 属预期行为且对「归回原工作区」有用，提前说一句即可，不做阻拦。
        origin_rows = manifest_origin_info(info.get("manifest") or {})
        origin_line = ""
        if origin_rows:
            origin_line = ("\n本包携带 %d 处源机器的路径等信息（IDE 工作区记录 / 会话正文等），"
                           "还原时会一并写回本机对应位置。\n" % len(origin_rows))

        # 还原方式（方案 §6.1）：支持增量合并的工具弹自建对话框二选一（默认「合并」），
        # 不支持的工具保持原样——只有「全覆盖」，用 messagebox 确认即可。
        merge_supported = self.adapter.restore_merge_supported
        title, body = restore_confirm_text(
            kind_label="回滚快照" if expected_kind == "rollback" else "备份包",
            root_dir=self.root_dir,
            file_count=info["file_count"],
            total_size=human_size(info["total_bytes"]),
            origin_line=origin_line,
            display_name=self.adapter.display_name,
            notice="" if merge_supported else self.adapter.restore_overwrite_notice("replace"),
        )
        if merge_supported:
            mode = ask_restore_mode(
                self.root,
                title=title,
                body=body,
                options=restore_mode_options(
                    merge_supported=True,
                    merge_notice=self.adapter.restore_overwrite_notice("merge"),
                    replace_notice=self.adapter.restore_overwrite_notice("replace"),
                ),
                default=RESTORE_MODE_MERGE,
            )
            if mode is None:
                return
        else:
            if not messagebox.askyesno(title, body):
                return
            mode = RESTORE_MODE_REPLACE

        # 读取包内 manifest 记录的源路径，与当前目标目录不一致时二次确认，
        # 防止还原到错误目录覆盖掉别的工具/用户的数据。
        recorded_root = manifest.get("source_root")
        if recorded_root and os.path.realpath(recorded_root) != os.path.realpath(self.root_dir):
            mismatch_tail = (
                "本次为「合并」还原，不会改动本机已有的会话与记忆。"
                if mode == RESTORE_MODE_MERGE
                else "继续还原可能会覆盖目标目录中已有的数据。"
            )
            if not messagebox.askyesno(
                "目标路径不一致，请确认",
                "该备份包记录的数据来源路径为：\n%s\n\n"
                "而当前还原目标为：\n%s\n\n"
                "两者不一致，最常见的原因是两台电脑的【系统用户名不同】"
                "（数据路径包含用户名），工具会在还原时自动把会话路径"
                "重映射到当前用户，通常无需干预。\n\n"
                "⚠️ 但还需你确认：目标机器上该工程的项目路径 / 工程名"
                "是否与源机器完全一致？CodeBuddy 的会话按「项目路径派生的"
                "工作区 ID」索引，若不一致，已还原的会话可能【不被索引显示】。\n"
                "补救方法（还原前后均可）：把目标机器上的项目放到与源机器"
                "相同的路径，或先在目标机器用 CodeBuddy 打开该工程，再重启 IDE。\n\n"
                "%s确认仍要还原到该目录吗？" % (recorded_root, self.root_dir, mismatch_tail),
            ):
                return

        # 跨电脑还原前向用户提示：若源机器登录用户 UUID 与当前不同，
        # 工具会自动重映射会话路径，但需让用户知晓并检查会话是否可读。
        preview = self.adapter.preview_path_rewrite(info.get("entries") or [])
        if preview and preview.get("will_rewrite"):
            messagebox.showinfo(
                "已自动重映射登录用户",
                "检测到该备份来自另一台电脑（登录用户与当前不同），"
                "工具已自动将会话 / 记忆路径重映射为当前用户，还原后请打开 %s "
                "确认会话可正常读取。\n\n"
                "若发现会话列表为空或点开无内容，请确认目标机器的项目路径"
                "与源机器一致（CodeBuddy 按项目路径派生工作区 ID 索引会话）。"
                % self.adapter.display_name,
            )

        root_dir = self.root_dir
        make_rollback = self.rollback_var.get()
        # 回滚快照与备份文件同目录（<备份工具目录>/backup/<工具名>/），方便按时间信息对比选择
        rollback_dir = self._tool_dir("backup")
        os.makedirs(rollback_dir, exist_ok=True)
        # 还原结果说明按适配器 + 本次方式区分（合并 / 整库覆盖 / 文件落盘）——不能在
        # 后台线程里读 Tk，故在主线程先取好文案。
        result_note = self.adapter.restore_result_note(mode)
        # 用法提醒同样按方式三分：合并没有「覆盖掉本机数据」这个前提（方案 §6.3）。
        cycle_hint = (MULTI_MACHINE_CYCLE_HINT_MERGE if mode == RESTORE_MODE_MERGE
                      else MULTI_MACHINE_CYCLE_HINT)

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
                path_rewrite=self.adapter.restore_path_rewrite(),
                restore_post_hook=self.adapter.restore_post_hook(),
                restore_index_merge=self.adapter.restore_index_merge(),
                restore_index_merge_paths=self.adapter.restore_index_merge_paths(),
                mode=mode,
                restore_policy_for=self.adapter.restore_policy_for,
                restore_merge_target=self.adapter.restore_merge_target(),
            )
            extra = "\n回滚快照：%s" % r["rollback"] if r["rollback"] else ""
            if r.get("skipped"):
                extra += "\n保留本机已有 %d 项（未改动）" % r["skipped"]
            if r["blocked"]:
                extra += "\n\n被跳过 %d 项" % len(r["blocked"])
            self.msg_queue.put(
                (
                    "done",
                    (
                        "还原成功",
                        "已还原 %d 个文件。\n\n%s%s\n\n"
                        "请重启 %s 以加载还原的会话与记忆；若会话列表为空，"
                        "请确认目标机器的项目路径与源机器一致。\n\n用法提醒：%s"
                        % (
                            r["restored"],
                            result_note,
                            extra,
                            self.adapter.display_name,
                            cycle_hint,
                        ),
                    ),
                )
            )

        self._run_bg(work)


def _enable_dpi_awareness() -> None:
    """让进程在 Windows 上感知 DPI，确保 tk 的几何/字体缩放与系统一致。

    默认 Python 进程是 DPI-UNAWARE：Windows 会把整个窗口位图虚化放大到
    当前缩放比，导致 ``winfo_screenheight`` 与 ``geometry`` 量纲错配、
    高分屏（缩放 > 100%）下主窗口被放得过大而显示不全。声明 Per-Monitor
    Aware v2 后，tk 用 ``tk scaling`` 精确渲染，窗口相对屏幕大小在任意
    DPI 下恒定，不再被虚化放大。非 Windows 平台直接跳过（无 DPI 概念）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE_V2（Win8.1+，最稳）
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:
        # 回退：Win7/Vista 的进程级 DPI 感知
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


def main() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    QoderBackupApp(root)
    root.mainloop()


class BackupBrowser:
    """独立的备份浏览器窗口：左侧虚拟加载文件列表，右侧异步显示所选备份详情。

    - 列表走共用模块 ``backup_scan``：**首屏同步只扫一层**（≤5 ms）秒开，
      随后后台逐层（BFS）递归到 3 层，每层回传一批边扫边填。
    - 点击某文件后，后台线程读取包信息（默认不校验完整性），右侧面板填充；
      读取期间显示「读取中…」，避免大文件阻塞界面。
    - **收窄的还原语义**：只有本工具自己的 backup/rollback 才可点「还原此备份」；
      其他工具的包整行灰显、按钮置灰，详情里点名是谁的包并指路到
      「导入会话…」→「从备份包导入」。
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
        # 扫描状态：_rows 是「未过滤的候选全集」，_refresh_view 按过滤开关渲染。
        self._rows: list = []
        self._scan_cache: dict = {}
        self._scan_cancel: threading.Event | None = None
        self._scan_queue: "queue.Queue" = queue.Queue()
        self._scan_done = True
        self._scan_progress: backup_scan.ScanProgress | None = None

        top = tk.Toplevel(app.root)
        # 创建即隐藏：Toplevel 默认可见，双屏/慢渲染下首帧会闪；正常模式末尾再 deiconify。
        top.withdraw()
        top.title(BROWSER_TITLE_TPL)
        # 窗口宽度收敛：左侧列表按内容自适应(约503px)，右侧详情框请求宽约344px，
        # 二者加边距/sash 约 880；920 使右侧自然贴合内容、不空。
        # 默认尺寸夹进工作区：矮屏 / 高 DPI 下不让窗口开出屏幕（底部被裁）。
        _w, _h = _clamp_dialog_size(top, 920, 640)
        top.minsize(min(800, _w), min(480, _h))
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

        # 过滤开关 + 扫描进度行。
        # 这里的「可识别」必须比导入侧的「可导入」**宽**：本工具的包恰恰不可导入
        # （导入侧会把它排除），但它正是本窗口唯一能还原的东西 ⇒ 两个开关若共用
        # 同一判定，必然有一边是错的。
        sub = ttk.Frame(top)
        sub.pack(fill=tk.X, padx=10, pady=(0, 2))
        self.filter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sub,
            text="只显示可识别的备份（隐藏无关 zip）",
            variable=self.filter_var,
            command=self._refresh_view,
        ).pack(side=tk.LEFT)
        self.scan_lbl = ttk.Label(sub, text="", foreground="#888")
        self.scan_lbl.pack(side=tk.LEFT, padx=10)

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
        # 非本工具的包 / 认不出是备份的 zip：整行灰显（仍可见，但不误导可点）
        self.tree.tag_configure("dim", foreground="#9a9a9a")
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
        """刷新列表：**首屏同步只扫一层**（实测 ≤5 ms）→ 立刻渲染，绝不白屏；
        随后后台逐层（BFS）扫描，每层扫完回传一批，边扫边填。

        逐层而非深度优先：备份包几乎总在 2~3 层以内，深挖会先钻到 ``~/.workbuddy/
        plugins/...`` 深处，浅层的包反而**最后**才出现（实测见方案 §4.3）。
        """
        self._cancel_scan()
        self._rows = []
        self.tree.delete(*self.tree.get_children())
        self._scan_done = True
        self._scan_progress = None
        # 首屏：同步只扫一层。不读清单（L2 约 10 ms/包，必须在后台）。
        top = backup_scan.scan(
            self.backup_dir, max_depth=0, manifest_limit=0, cache=self._scan_cache
        )
        self._rows = top.rows
        self._refresh_view()
        self._set_scan_status(top.progress())
        # 后台：逐层扫到默认深度（3 层），每层回传一批
        self._start_scan()

    def _cancel_scan(self) -> None:
        """置取消令牌，让上一轮后台扫描在「一个目录」的粒度上尽快退出。"""
        ev = self._scan_cancel
        if ev is not None:
            ev.set()
        self._scan_cancel = None

    def _start_scan(self) -> None:
        """后台逐层扫描（不触碰 Tk：结果全部经线程安全队列回主线程消费）。"""
        ev = threading.Event()
        self._scan_cancel = ev
        self._scan_done = False
        directory = self.backup_dir
        cache = self._scan_cache

        def worker() -> None:
            def on_layer(rows, progress) -> None:
                self._scan_queue.put(("layer", rows, progress))

            res = backup_scan.scan(directory, cancel=ev, on_layer=on_layer, cache=cache)
            self._scan_queue.put(("done", res, res.progress()))

        threading.Thread(target=worker, daemon=True).start()
        self.top.after(120, self._poll_scan)

    def _poll_scan(self) -> None:
        """主线程消费后台扫描回传的批次（每层一批）。"""
        if not self._alive():
            return
        while True:
            try:
                kind, payload, progress = self._scan_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "layer":
                self._rows = payload
                self._refresh_view()
                self._set_scan_status(progress)
            else:  # done
                self._rows = payload.rows
                self._scan_done = True
                self._refresh_view()
                self._set_scan_status(progress)
        if not self._scan_done:
            self.top.after(150, self._poll_scan)

    def _alive(self) -> bool:
        """窗口是否还在（后台扫描回传时窗口可能已被关掉）。"""
        try:
            return bool(self.top.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _set_scan_status(self, progress) -> None:
        """刷新进度行：已扫描目录 / 层数 / 命中数 / 隐藏数。"""
        hidden = len(self._rows) - len(self._filtered_rows())
        text = "已扫描 %s（第 %d 层 / 共 %d 个目录）· 命中 %d 个包" % (
            self.backup_dir, progress.layers, progress.dirs_scanned, len(self._rows)
        )
        if hidden > 0:
            text += " · 另有 %d 个无关 zip 已隐藏" % hidden
        if progress.truncated:
            text += " · 已停止（%s）" % (progress.reason_label or "达到上限")
        try:
            self.scan_lbl.config(text=text)
        except Exception:  # noqa: BLE001 - 窗口已销毁
            pass

    def _filtered_rows(self) -> list:
        """按过滤开关筛出应显示的行。

        关闭开关时**全列**（认不出的行灰显）；开启时只留「可识别」的行。
        """
        if not self.filter_var.get():
            return list(self._rows)
        return [r for r in self._rows if r.identifiable]

    def _refresh_view(self) -> None:
        """按当前过滤开关重画列表（保留已有校验结果与当前选中行）。"""
        prev_verify = {
            item: self.tree.set(item, "verify")
            for item in self.tree.get_children()
        }
        prev_sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        rows = self._filtered_rows()
        if not rows:
            if self._rows:
                self.tree.insert("", "end", values=("(已按过滤条件隐藏全部文件)", "", "", ""))
                self._set_detail(
                    "该目录下扫到 %d 个 zip，但没有一个能被识别为备份/快照。\n\n"
                    "取消勾选「只显示可识别的备份」可以看到全部文件。" % len(self._rows)
                )
            else:
                self.tree.insert("", "end", values=("(该目录暂无备份文件)", "", "", ""))
                self._set_detail(
                    "该目录下还没有任何备份文件。\n\n"
                    "请在主窗口点「导出备份」生成备份，或把已有的备份包 "
                    "（%s_backup_*.zip）放到：\n%s"
                    % (self.app.adapter.name, self.backup_dir)
                )
            self._autosize_columns()
            return
        other = self.app.adapter.name
        for r in rows:
            label, _ = self.KIND_LABEL.get(r.kind, self.KIND_LABEL["unknown"])
            # 灰显：不是本工具的包（可识别但要走导入），或压根认不出是备份
            dim = (not r.identifiable) or (bool(r.tool) and r.tool != other)
            self.tree.insert(
                "",
                "end",
                iid=r.path,
                tags=("dim",) if dim else (),
                values=(
                    r.name,
                    label,
                    human_size(r.size),
                    datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M"),
                    prev_verify.get(r.path, ""),  # 已有结果回填，新文件留空待校验
                ),
            )
        # 恢复选中行（后台每扫完一层都会重画，否则会把用户的选择清掉）
        keep = [p for p in prev_sel if self.tree.exists(p)]
        if keep:
            self.tree.selection_set(keep[0])
            self.tree.see(keep[0])
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
        # 先置灰：按钮是否可用由 **包内清单 + 所属工具** 决定（见 _render_detail），
        # 在读完详情之前不放行，避免「跨工具的包被误点还原」。
        self.restore_btn.configure(state="disabled")
        self._set_restore_tooltip(None)
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

        struct_match = info.get("structure_match")
        struct_missing = info.get("structure_missing") or []
        has_manifest = info.get("has_manifest")

        # ---------------- 所属工具：清单声明优先，无声明则跨适配器指纹 ----------------
        # 收窄后「还原」只对本工具的包开放，所以这里必须把「是谁的包」认准，
        # 而不是像以前那样只在当前工具上试指纹（那样别的工具的包会显得「无法识别」）。
        mf_tool = (mf.get("tool") or "").strip() if isinstance(mf, dict) else ""
        if mf_tool:
            tool_src = "声明文件记录"
            if mf_tool not in backup_scan.tool_names():
                tool_src = "声明文件记录了未知工具，按原文显示"
        else:
            mf_tool, fp_src = backup_scan.identify_tool(
                info.get("entries") or [], mf, prefer=self.app.adapter.name
            )
            tool_src = ("结构指纹回退识别" if fp_src == "structure"
                        else "既无声明也无匹配的结构指纹")
        is_self = bool(mf_tool) and mf_tool == self.app.adapter.name
        tool_name = backup_scan.display_name(mf_tool) if mf_tool else "未知"
        if is_self:
            tool_name = "%s（本工具）" % tool_name

        # ---------------- 还原可行性三态（按钮 + 结论行 + 悬停提示） ----------------
        # 跨工具的包**无论类型如何**都不可在本窗口还原：整库还原要用**它自己**的
        # 路径改写 / 索引登记 / 结构校验规则，用当前工具的规则去写会「看着在、其实
        # 读不到」。它只能走「导入会话 → 从备份包导入」。
        self_btn_state = "disabled"
        tip = None
        if mf_tool and not is_self:
            breason = "other"
            tip = ("这是 %s 的备份。本窗口只能还原本工具的备份；要取其中的会话，"
                   "请用「导入会话…」→「从备份包导入」。" % tool_name)
        elif is_self:
            # 清单声明（或指纹回退识别）确认是本工具：可还原。
            breason = "ok"
        elif struct_match:
            breason = "ok_fp"
        elif kind in ("backup", "rollback"):
            # 名字/清单说是备份包，但既没声明所属工具、指纹也不匹配本工具：
            # 不能凭「它自称是备份」就写盘（可能是别的工具的包）。
            breason = "no_struct"
            why = ("该压缩包缺少清单文件" if not has_manifest
                   else "清单未记录所属工具")
            tip = ("%s，且内部结构不是 %s 的数据结构，无法确认是可还原本工具的备份。"
                   "\n缺失项：%s"
                   % (why, self.app.adapter.display_name,
                      "、".join(struct_missing) or "（无）"))
        else:
            breason = "unknown"
            tip = ("无法识别为 %s 的备份或回滚快照（缺少清单且内部结构不匹配），"
                   "为安全起见不可还原。\n缺失项：%s"
                   % (self.app.adapter.display_name,
                      "、".join(struct_missing) or "（无）"))
        if breason in ("ok", "ok_fp"):
            self.restore_btn.configure(state="normal")
            self._set_restore_tooltip(None)
        else:
            self.restore_btn.configure(state=self_btn_state)
            self._set_restore_tooltip(tip)

        # 类型说明：跨工具 / 无法归属的包不要写「可直接还原」，否则自相矛盾
        if breason == "other":
            type_note = "这是 %s 的备份包，本窗口不还原它（见下方【还原】）。" % tool_name
        elif breason in ("no_struct", "unknown"):
            type_note = "疑似备份包，但无法确认是本工具的数据（见下方【还原】）。"
        elif kind == "backup":
            type_note = "主动导出的完整数据，可直接还原。"
        else:
            type_note = "还原前自动保存的当前数据，用于还原失败时回退，也可直接还原。"

        # 每行是 ``(文本, 颜色标签)``；None = 正文色。真上色（此前 title_color 是死参数）
        seg: list = []
        seg.append(("【%s】%s" % (kind_label, os.path.basename(path)), "title:" + kind_color))
        seg.append(("【类型说明】%s" % type_note, "warn" if breason == "other" else None))
        seg.append(("", None))
        seg.append(("【文件数】%d" % info["file_count"], None))
        seg.append(("【原始数据大小】%s （=将被备份的源文件大小，非磁盘占用）"
                    % human_size(info["total_bytes"]), None))
        seg.append(("【创建时间】%s" % mf.get("created_at", "未知（清单未记录）"), None))
        seg.append(("【来源目录】%s" % mf.get("source_root", "未知（清单未记录）"), None))
        items = mf.get("items", [])
        # 模块名均为英文字符：遵循英文惯例，用「逗号+空格」分隔（而非中文顿号），
        # 标点与下一个模块名之间保留一个空格，阅读更自然。
        seg.append(("【包含模块】%s" % (", ".join(items) if items else "未知（清单未记录）"), None))
        # 类型识别：始终分两行展示「文件类型」与「所属 AI 工具类型」，并标注各自识别来源。
        # 不论有无声明文件，两行都出现，避免「一会儿有一会儿没」看的人发蒙。
        # 1) 文件类型(kind) 来源
        kind_inferred = info.get("kind_inferred")
        if has_manifest and not kind_inferred:
            kind_src = "声明文件记录"
        elif kind_inferred:
            kind_src = "声明文件缺类型字段，按文件名推断"
        elif not has_manifest and struct_match:
            kind_src = "无声明文件，结构指纹回退识别"
        else:
            kind_src = "无法识别"
        seg.append(("【文件类型】%s（%s）" % (kind_label, kind_src), None))
        # 2) 所属 AI 工具类型 + 三态高亮（与本工具不同时可导入 / 不可导入）
        if not mf_tool:
            seg.append(("【所属工具】未知（%s）" % tool_src, "muted"))
        elif is_self:
            seg.append(("【所属工具】%s（%s）" % (tool_name, tool_src), None))
        elif import_matrix.ALL_SOURCES.get(mf_tool) is not None and self._can_import_here(mf_tool):
            seg.append(("【所属工具】%s ← 与本工具（%s）不同，但可导入"
                        % (tool_name, self.app.adapter.display_name), "warn"))
            seg.append(("【跨工具导入】可导入为 %s 的原生会话（只有会话过去，不含索引/缓存）。"
                        "入口：主窗口「导入会话…」→「从备份包导入」。"
                        % self.app.adapter.display_name, "warn"))
        else:
            cap = import_matrix.ALL_SOURCES.get(mf_tool)
            why = "会话库加密，只能整库还原" if (cap and cap.status == import_matrix.BACKUP_ONLY) \
                else "该来源暂不可导入"
            seg.append(("【所属工具】%s ← 与本工具（%s）不同，且无法导入（%s）"
                        % (tool_name, self.app.adapter.display_name, why), "alert"))
        seg.append(("【还原】%s" % self._restore_conclusion(breason, tool_name), None if breason in ("ok", "ok_fp")
                    else ("warn" if breason == "other" else "alert")))
        if breason == "other":
            seg.append(("　　要取其中的会话，请用「导入会话…」→「从备份包导入」选中这个包；"
                        "若要整库还原它，请先把主窗口的工具切到「%s」，再重新打开本窗口。"
                        % tool_name, "warn"))

        # 按文件类型归类展示
        by_cat: dict[str, int] = {}
        for ext, size in info["bytes_by_ext"].items():
            by_cat[category_of(ext)] = by_cat.get(category_of(ext), 0) + size
        if by_cat:
            seg.append(("", None))
            seg.append(("文件类型分布（按源大小）：", None))
            for cat in ("text", "db", "struct", "binary", "other"):
                if cat in by_cat:
                    seg.append(("  · %s：%s"
                                % (self.CATEGORY_LABEL.get(cat, cat),
                                   human_size(by_cat[cat])), None))

        # 携带的源设备信息：让用户在**打开/分享这个包之前**就知道包里有源机器的路径。
        # 旧备份包没有该字段（origin_info_lines 返回空），此处自然不显示，不报错、不推断。
        for line in origin_info_lines(mf):
            seg.append((line, None))
        # 包内附带的「会话 -> 原始工作区路径」映射（本工具生成、随包携带）。
        for line in session_workspaces_lines(mf):
            seg.append((line, None))

        if info["unsafe"]:
            seg.append(("", None))
            seg.append(("⚠ 检测到 %d 个非法路径，恢复将被阻止" % len(info["unsafe"]), "alert"))
        seg.append(("", None))
        if breason in ("ok", "ok_fp"):
            seg.append(("提示：点「校验完整性」可逐文件校验（大文件较慢，结果在左侧「完整性」列）；"
                        "点「还原此备份」可恢复。", None))
        else:
            seg.append(("提示：点「校验完整性」可逐文件校验（大文件较慢，结果在左侧「完整性」列）；"
                        "此包不能在本窗口还原，见上方【还原】。", None))

        self._write_detail(seg)

    def _can_import_here(self, src_tool: str) -> bool:
        """该来源工具的包能否导入为**当前窗口工具**的原生会话。"""
        if src_tool == self.app.adapter.name:
            return False
        cap = import_matrix.matrix_for(self.app.adapter.name)
        if cap is None:
            return False
        return any(s.tool == src_tool and s.status == import_matrix.SUPPORTED
                   for s in cap.sources)

    def _restore_conclusion(self, breason: str, tool_name: str) -> str:
        """详情里【还原】那一行的结论文案（与按钮三态一一对应）。"""
        if breason in ("ok", "ok_fp"):
            return "可在本窗口直接还原。"
        if breason == "other":
            return "不可在此还原 ← 这是 %s 的备份" % tool_name
        if breason == "no_struct":
            return "不可还原 ← 未声明所属工具，且内部结构不匹配本工具"
        return "不可还原 ← 无法识别为备份或回滚快照"


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
        # 仅当该文件仍是当前选中项时，把结论追加到右侧详情，避免写错文件。
        # 直接追加而非「取全文重设」：重设会丢掉详情里的着色标签。
        if self.tree.selection() and self.tree.selection()[0] == path:
            self.detail.configure(state="normal")
            self.detail.insert(tk.END, "\n\n" + msg, "body")
            self.detail.configure(state="disabled")

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

    #: 详情 Text 的着色标签 -> 前景色（正文 / 可还原 / 跨工具 / 警示 / 次要）
    DETAIL_TAGS: dict = {
        "body": "#333333",
        "ok": "#1a7f37",
        "warn": "#b54708",
        "alert": "#a05a00",
        "muted": "#57606a",
    }

    def _set_detail(self, text: str, title_color: str | None = None) -> None:
        """把纯文本写入详情；``title_color`` 只给**首行**着色（正文用 body 色）。

        ``title_color`` 此前是个收了却从未使用的死参数，本次真正生效。
        """
        seg = []
        if text:
            for i, line in enumerate(text.split("\n")):
                seg.append((line, "title:" + title_color if (i == 0 and title_color) else None))
        self._write_detail(seg)

    def _write_detail(self, segments) -> None:
        """写入 ``[(文本, 颜色标签), ...]``；标签见 :attr:`DETAIL_TAGS`。

        以 ``title:#rrggbb`` 形式可**临时**指定颜色（每条详情首行按包类型着色，
        不新增全局标签）。
        """
        detail = self.detail
        detail.configure(state="normal")
        detail.delete("1.0", tk.END)
        for name, color in self.DETAIL_TAGS.items():
            detail.tag_configure(name, foreground=color)
        if not segments:
            detail.insert("1.0", "（未选择备份文件）\n", "body")
            detail.configure(state="disabled")
            return
        first = True
        for text, tag in segments:
            if not first:
                detail.insert(tk.END, "\n", "body")
            first = False
            if not text:
                continue
            if tag and tag.startswith("title:"):
                color = tag.split(":", 1)[1]
                detail.tag_configure("title", foreground=color)
                detail.insert(tk.END, text, "title")
            else:
                detail.insert(tk.END, text, tag or "body")
        detail.configure(state="disabled")


def missing_workspaces_text(plans: list, limit: int = 8) -> str:
    """「这些目标工作区不存在」对话框里的明细文本（每行带**落点来源**）。

    为什么必须带来源：跨机还原后本机往往没有那个工程目录，用户要判断「要不要创建」，
    就得知道这个路径是**源会话原来的工程**（创建它 = 让会话回到原工作区），还是
    只是某工具的**默认落点**（创建它 = 收容无工作区的会话）。两者都合法，但含义不同。
    """
    rows = []
    for p in plans[:limit] if limit else plans:
        if p.note.startswith("由源会话"):
            src = "源会话还原"
        else:
            src = workspace_plan.MODE_LABELS.get(p.mode, p.mode)
        rows.append("· %s（%s）" % (p.path, src))
    if limit and len(plans) > limit:
        rows.append("…（共 %d 处）" % len(plans))
    return "\n".join(rows)


class MigrateDialog:
    """「导入会话」对话框：从其它软件选会话，导入为当前工具的原生会话。

    上方选择来源软件与来源目录，扫描出可导入会话列表（支持 Ctrl / Shift 多选，
    一次导入多条）；下方是**自动判定**的落点设置。

    目标工作区**默认不需要用户填**（判定规则见 :mod:`ai_env_clone.workspace_plan`）：

    1. 源会话自带工作区 -> 直接沿用（尽量与源机器路径同源）；
    2. 源会话没有工作区 -> 用当前工具「无工作区会话」的默认落点；
    3. 勾选「手动指定工作区」-> 本次导入的**全部**会话都落到同一个工作区，
       **包括自带工作区的会话**；界面常驻警示，导入前还会再确认一次。

    「目标工作区」一栏**只展示实际会落到的那个工作区**：单选显示具体落点；多选且
    落点不一致时只提示「存在 N 个不同的工作区」（不挑一条显示，否则会被读成统一
    落点）；落点一致时显示该落点，**并注明它是不是各会话自带的**。未勾选手动指定时
    **手动输入行整体收起**——置灰保留上一次填的路径会让人误以为它就是本次实际落点。

    ⚠️ 多选时**绝不能只甩一个路径**：见 :meth:`MigrateDialog._describe_multi`——
    「手动指定」勾选时的预填值就是自动判定值，二者字符串完全相同，只显示路径会被读成
    「这不就是我手动填的那个」；若该值其实是工具的默认落点，还会被读成「这些会话原本
    就属于这个工作区」（WorkBuddy 的默认落点更是本次现造的时间戳新目录）。

    工作区在目标机不存在时**不静默创建**：先问用户（是=创建后再导入 /
    否=不创建但仍按原落点导入 / 取消=中止导入），避免「写了但看不到」。

    导入在后台线程执行，避免大会话（如 DSH 的 MB 级 jsonl.zstd）卡住界面。
    """

    def __init__(self, app: "QoderBackupApp"):
        self.app = app
        self.target_tool = app.adapter.name
        self.target_display = app.adapter.display_name

        cap = import_matrix.matrix_for(self.target_tool)
        self.sources = list(cap.importable) if cap else []
        self._by_display = {s.display: s for s in self.sources}
        self.cur_source = self.sources[0] if self.sources else None
        self.items: list = []
        #: 本次扫描到的**全部**会话（含子代理）；``items`` 是其中按开关过滤后的可见子集，
        #: 两者必须逐项对齐，`_selected_items` 才能按列表下标取到正确条目。
        self._all_items: list = []
        self._result = None
        #: 导入过程中的 warn 回调累计（落点同源提示等），导入**执行后**一次性汇总展示。
        self.warns: list = []
        #: 本批导入「无工作区会话」的默认落点。WorkBuddy 的 playground 目录名带时间戳，
        #: 这里固定一次，保证同一批导入落到同一处（而不是每导入一条就新建一个目录）。
        self._default_ws = workspace_plan.default_workspace(
            self.target_tool, datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
        #: 目标工具能否接收导入（qoder / trae-* 是 BACKUP_ONLY ⇒ 页签 B 整体置灰）。
        self._can_import = bool(self.sources)

        # ---- 页签 B（从备份包导入）的状态 ----
        #: 当前「包解包成来源根」的临时句柄（换包 / 关窗时清理）。
        self._archive = None
        self._arc_path = ""
        #: 正在后台解包的包路径（尚未完成）。**在途去重**的关键：`_arc_path` 只在
        #: 解包完成后才写入，若只靠它判重，扫描每回一层就重建包列表、恢复选中并再
        #: 触发一次选择事件，于是同一个包被反复重新解包——既刷出大量后台线程，又让
        #: 每次新请求都 `_arc_token += 1` 把上一个结果判为过期丢弃，最终会话列表
        #: 永远填不上、界面被线程拖死（2026-10-04 用户实测反馈）。
        self._arc_pending = ""
        self._arc_token = 0
        #: 包列表：未过滤全集 + 后台扫描状态（与备份浏览器同构）。
        self._pkg_rows: list = []
        self._pkg_cache: dict = {}
        self._pkg_cancel: "threading.Event | None" = None
        self._pkg_queue: "queue.Queue" = queue.Queue()
        self._pkg_done = True
        self._pkg_progress = None
        #: 变化预告里 warn 级条数（有则需要最终确认时默认按钮改为「否」）。
        self._warn_count = 0

        self.win = tk.Toplevel(app.root)
        self.win.title("导入会话 → %s" % self.target_display)
        # 默认尺寸夹进工作区：矮屏 / 高 DPI 下不让窗口开出屏幕（底部按钮被裁）。
        _clamp_dialog_size(self.win, 840, 780)
        self.win.transient(app.root)
        # 关窗必须清理临时解包目录（方案 §5.5：不复活临时目录）。
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        # 内容超出窗口高度时竖向滚动；底部操作行固定在 row 1，永远可见、不参与滚动。
        self.win.rowconfigure(0, weight=1, minsize=0)
        self.win.columnconfigure(0, weight=1)
        self._scroll = _ScrollBody(self.win, row=0, column=0)
        self.body = self._scroll.frame
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.win.bind(_seq, self._scroll.wheel)
        self._build()
        self._scroll.sync()
        if self.cur_source is not None:
            self._on_source_change()

    # ---------------------------------------------------------------- UI --
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # 页签只换「来源」：下半部分（会话列表 / 落点 / 变化预告 / 操作）两个页签**共用**，
        # 一行分支都不加（方案 §3.1）。页签 B 只把「选中的包」变成 (来源工具, 临时来源根)。
        self.notebook = ttk.Notebook(self.body)
        self.notebook.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.tab_dir = ttk.Frame(self.notebook)
        self.tab_pkg = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_dir, text="从数据目录导入")
        self.notebook.add(self.tab_pkg, text="从备份包导入")
        self._build_tab_dir(self.tab_dir)
        self._build_tab_pkg(self.tab_pkg)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)
        if not self._can_import:
            # 目标工具会话库为产品侧加密：页签 B 整体置灰并给一行原因。
            self.notebook.tab(1, state="disabled")

        # 会话列表（支持多选：Ctrl / Shift）
        lst = ttk.LabelFrame(self.body, text="可导入会话（Ctrl / Shift 可多选，一次导入多条）")
        lst.pack(fill=tk.BOTH, expand=True, **pad)
        # 子代理会话开关：默认隐藏。子代理记录的「用户消息」是父代理写的任务提示词，
        # 正文只有 AI 干活过程；列出来会被当成用户自己的会话（2026-10-05 实测反馈）。
        opt_row = ttk.Frame(lst)
        opt_row.pack(fill=tk.X, padx=8, pady=(6, 0))
        self.subagent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_row, text="包含子代理会话（AI 内部过程，默认不导入）",
            variable=self.subagent_var, command=self._on_subagent_toggle,
        ).pack(side=tk.LEFT)
        self.subagent_hint_var = tk.StringVar(value="")
        ttk.Label(opt_row, textvariable=self.subagent_hint_var,
                  foreground="#a05a00").pack(side=tk.LEFT, padx=(8, 0))
        listwrap = ttk.Frame(lst)
        listwrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.listbox = tk.Listbox(listwrap, activestyle="dotbox", selectmode="extended")
        lsb = ttk.Scrollbar(listwrap, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lsb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<<ListboxSelect>>", lambda *_: self._on_pick())

        # 落点设置
        dst = ttk.LabelFrame(self.body, text="导入落点（%s 原生位置）" % self.target_display)
        dst.pack(fill=tk.X, **pad)

        self.tgt_root_var = tk.StringVar(value=session_migration.default_target_root(self.target_tool))
        r = ttk.Frame(dst)
        r.pack(fill=tk.X, padx=8, pady=(8, 2))
        ttk.Label(r, text="目标根目录：").pack(side=tk.LEFT)
        ttk.Entry(r, textvariable=self.tgt_root_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )

        # 目标工作区：默认自动判定，只读展示；勾选后可手动指定
        ws_row = ttk.Frame(dst)
        ws_row.pack(fill=tk.X, padx=8, pady=(2, 2))
        ttk.Label(ws_row, text="目标工作区：").pack(side=tk.LEFT, anchor="n")
        self.ws_preview_var = tk.StringVar()
        ttk.Label(ws_row, textvariable=self.ws_preview_var, foreground="#0a6",
                  wraplength=620, justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.X, expand=True,
                                                        padx=(4, 0))

        self.manual_row = ttk.Frame(dst)
        self.manual_row.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.manual_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.manual_row, text="手动指定工作区", variable=self.manual_var,
            command=self._on_manual_toggle,
        ).pack(side=tk.LEFT)
        self.manual_lbl = ttk.Label(self.manual_row, text="")
        self.manual_lbl.pack(side=tk.LEFT, padx=(8, 0))

        self.manual_entry_row = ttk.Frame(dst)
        self.manual_entry_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        self.manual_var_value = tk.StringVar(value="")
        #: CodeBuddy 的工作区标识是路径派生出的 id，不方便手打：给一份目标机已有 id 供选。
        self.manual_combo = ttk.Combobox(
            self.manual_entry_row, textvariable=self.manual_var_value, width=52,
            values=session_migration.list_target_workspaces(
                self.target_tool, self.tgt_root_var.get()
            ),
        )
        self.manual_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.manual_combo.bind("<KeyRelease>", lambda *_: self._refresh_preview())
        self.manual_combo.bind("<<ComboboxSelected>>", lambda *_: self._refresh_preview())
        self.manual_browse_btn = ttk.Button(
            self.manual_entry_row, text="浏览…", command=self._browse_manual, width=8
        )
        self.manual_browse_btn.pack(side=tk.LEFT, padx=(6, 0))

        # 手动指定 = 覆盖全部会话（含自带工作区的）——常驻警示，避免误操作
        # 注：Tk 标签不渲染 Markdown，正文里不要写 ** 之类的标记（会原样显示）。
        self.manual_warn = ttk.Label(
            dst, foreground="#c00", wraplength=720, justify=tk.LEFT,
            text="⚠ 手动指定会覆盖本次导入的全部会话的工作区，包括本身就自带工作区的会话。",
        )

        self.hint = ttk.Label(dst, text="", foreground="#a05a00", wraplength=720, justify=tk.LEFT)
        self.hint.pack(fill=tk.X, padx=8, pady=(4, 4))

        # 变化预告（常驻，随选中实时刷新；两个页签共用）。把「导入后会变成什么样」变成
        # 用户可见的数据，而不是导入完才发现「工具调用没了」。文案见 import_matrix。
        notice = ttk.LabelFrame(self.body, text="本次导入的变化预告")
        notice.pack(fill=tk.X, **pad)
        self.notice_text = tk.Text(notice, height=7, wrap=tk.WORD, state="disabled",
                                   padx=8, pady=6)
        self.notice_text.pack(fill=tk.X, padx=8, pady=8)
        # 提示行标题用加粗，正文 info 用 #555、warn 用 #b54708（与备份浏览器配色一致）。
        self.notice_text.tag_configure("head", foreground="#333333",
                                       font=("", 9, "bold"))
        self.notice_text.tag_configure("info", foreground="#555555")
        self.notice_text.tag_configure("warn", foreground="#b54708")

        # 操作（固定在窗口 row 1：不随内容滚动，任何窗口高度下都可见）
        act = ttk.Frame(self.win)
        act.grid(row=1, column=0, sticky="ew", **pad)
        self.status_lbl = ttk.Label(act, text="", foreground="#0a6")
        self.status_lbl.pack(side=tk.LEFT)
        ttk.Button(act, text="关闭", command=self._on_close, width=10).pack(side=tk.RIGHT)
        self.import_btn = ttk.Button(
            act, text="导入选中会话", command=self._do_import, width=14
        )
        self.import_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._sync_rows()
        self._refresh_notices()

    # ---------------------------------------------------- 页签 A：数据目录 --
    def _build_tab_dir(self, parent) -> None:
        src = ttk.LabelFrame(parent, text="来源软件（明文可读，可导入到「%s」）" % self.target_display)
        src.pack(fill=tk.X, padx=8, pady=8)

        row = ttk.Frame(src)
        row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(row, text="来源软件：").pack(side=tk.LEFT)
        self.src_var = tk.StringVar(value=self.cur_source.display if self.cur_source else "")
        self.src_combo = ttk.Combobox(
            row, textvariable=self.src_var, width=22, state="readonly",
            values=[s.display for s in self.sources],
        )
        self.src_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.src_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_source_change())
        self.src_note = ttk.Label(row, text="", foreground="#666")
        self.src_note.pack(side=tk.LEFT, padx=(8, 0))

        row2 = ttk.Frame(src)
        row2.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Label(row2, text="来源目录：").pack(side=tk.LEFT)
        self.src_root_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.src_root_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )
        ttk.Button(row2, text="浏览…", command=self._browse_source, width=8).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(row2, text="扫描会话", command=self._scan, width=10).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # ---------------------------------------------------- 页签 B：备份包 --
    def _build_tab_pkg(self, parent) -> None:
        box = ttk.LabelFrame(
            parent, text="备份包（递归子目录；备份与回滚快照都可作为导入来源）"
        )
        box.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        if not self._can_import:
            ttk.Label(
                box, foreground="#a05a00", wraplength=740, justify=tk.LEFT,
                text="本工具（%s）的会话库为产品侧加密，只支持整库备份 / 还原，"
                     "不支持接收外部会话导入。请把包带回其来源工具，或先在主窗口"
                     "把工具切到该来源工具后再导入。" % self.target_display,
            ).pack(anchor="w", padx=8, pady=10)
            return

        row = ttk.Frame(box)
        row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(row, text="备份包目录：").pack(side=tk.LEFT)
        self.pkg_dir_var = tk.StringVar(value=self._default_pkg_dir())
        ttk.Entry(row, textvariable=self.pkg_dir_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )
        ttk.Button(row, text="切换目录", command=self._browse_pkg_dir, width=10).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(row, text="刷新", command=self._load_packages, width=8).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        cols = ("name", "kind", "size", "mtime")
        treewrap = ttk.Frame(box)
        treewrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 2))
        self.pkg_tree = ttk.Treeview(
            treewrap, columns=cols, show="headings", selectmode="browse", height=6
        )
        self.pkg_tree.heading("name", text="文件名")
        self.pkg_tree.heading("kind", text="类型")
        self.pkg_tree.heading("size", text="大小")
        self.pkg_tree.heading("mtime", text="修改时间")
        self.pkg_tree.column("name", width=380)
        self.pkg_tree.column("kind", width=70, anchor="center")
        self.pkg_tree.column("size", width=90, anchor="e")
        self.pkg_tree.column("mtime", width=140)
        psb = ttk.Scrollbar(treewrap, orient="vertical", command=self.pkg_tree.yview)
        self.pkg_tree.configure(yscrollcommand=psb.set)
        self.pkg_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        psb.pack(side=tk.RIGHT, fill=tk.Y)
        self.pkg_tree.bind("<<TreeviewSelect>>", lambda *_: self._on_pkg_pick())
        # 不可导入的包（本工具自己的包 / 加密工具 / 认不出的 zip）整行灰显。
        self.pkg_tree.tag_configure("dim", foreground="#9a9a9a")

        sub = ttk.Frame(box)
        sub.pack(fill=tk.X, padx=8, pady=(0, 2))
        self.pkg_filter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sub, text="只显示可导入的包", variable=self.pkg_filter_var,
            command=self._refresh_pkg_view,
        ).pack(side=tk.LEFT)
        self.pkg_lbl = ttk.Label(sub, text="", foreground="#888")
        self.pkg_lbl.pack(side=tk.LEFT, padx=10)

        # 来源软件**从包里认出来**（不是下拉）：声明 > 文件名 > 结构指纹。
        self.pkg_src_lbl = ttk.Label(box, text="来源软件：（尚未选择备份包）",
                                     foreground="#555", wraplength=740, justify=tk.LEFT)
        self.pkg_src_lbl.pack(fill=tk.X, padx=8, pady=(0, 2))
        self.pkg_detail_lbl = ttk.Label(box, text="", foreground="#555",
                                        wraplength=740, justify=tk.LEFT)
        self.pkg_detail_lbl.pack(fill=tk.X, padx=8, pady=(0, 6))

    def _sync_rows(self) -> None:
        """按目标工具类型设置「工作区」栏的命名与提示，并重置手动输入行。"""
        tool = self.target_tool
        self.manual_lbl.configure(text="（%s）" % workspace_plan.workspace_kind_label(tool))

        hints = {
            "reasonix": "Reasonix 按「项目名」隔离会话；自动判定取源会话的项目名，"
                        "取不到时用 global-workspace。",
            "codebuddy": "CodeBuddy 按项目路径派生的 workspaceId 索引会话"
                         "（实测 = md5(路径小写、反斜杠)）；自动判定取源 workspaceId，"
                         "或由源会话的工作区路径派生。",
            "workbuddy": "写出的会话会登记进 workbuddy.db 的 sessions 表；"
                         "自动判定沿用源会话的工作区路径（来源是 CodeBuddy 时，先把它那串"
                         "工作区 id 反查回项目路径），确实取不到时才落到 "
                         "~/WorkBuddy/<时间戳>（与产品 playground 一致）。",
            "dsh": "DSH 会话按工作区索引，并在 storages/workspace.json 登记；"
                   "自动判定沿用源会话的工作区路径（CodeBuddy 来源先反查 id 对应的路径），"
                   "确实取不到时落到用户主目录。写出前需可用 zstd 后端（缺失时会明确报错）。",
        }
        self.hint.configure(text=hints.get(tool, ""))
        self._on_manual_toggle()

    # ------------------------------------------------------- 工作区判定 --
    def _manual_value(self) -> str:
        """用户手动指定的工作区标识（未勾选时返回空串 = 走自动）。"""
        if not self.manual_var.get():
            return ""
        return self.manual_var_value.get().strip()

    def _plan_for(self, item, manual: str = "") -> "workspace_plan.WorkspacePlan":
        return workspace_plan.plan_for(
            self.target_tool, item, manual,
            root=self.tgt_root_var.get(), default_value=self._default_ws,
            origin=(item or {}).get("title", "") or "",
        )

    def _selected_plans(self) -> list:
        """当前选择下，**每条会话各一份**落点计划（顺序与 :meth:`_selected_items` 一致）。

        手动指定时每条都是同一个值（即「覆盖全部会话」），但仍是 N 份，
        以保证与 ``_build_jobs`` 的 ``zip`` 一一对应——否则多选时只有第一条会被导入。
        """
        manual = self._manual_value()
        items = self._selected_items()
        if not items:
            return [self._plan_for(None, manual)]
        return [self._plan_for(None if manual else it, manual) for it in items]

    def _refresh_preview(self) -> None:
        """刷新「目标工作区」预览：**只展示实际会落到的那个工作区**。

        - 未勾选手动：单选（或未选）时显示自动判定的具体落点；多选且落点不一致时
          只说明「存在多个路径」，**不挑一条显示**——挑一条会被误读成统一落点；
        - 勾选手动：显示手动值，并标注它会覆盖全部会话。

        ⚠️ **多选时必须交代落点来源**（沿用会话自带 / 工具默认）。只甩一个路径会有两种
        误读：① 被当成「我手动填过的那一个」——因为「手动指定」勾选时的**预填值**就是
        自动判定值（``_on_manual_toggle``），两者字符串一模一样；② 被当成「这些会话原本
        就属于这个工作区」——而它其实只是该工具「无工作区会话」的默认落点（WorkBuddy
        还是本次现造的时间戳新目录）。故凡涉及工具默认落点，一律写明「非手动指定」。
        """
        manual = self._manual_value()
        plans = self._selected_plans()
        picked = len(self._selected_items())
        if manual:
            p = plans[0]
            text = "%s    【%s】" % (
                workspace_plan.describe_value(self.target_tool, p.value) or "（未确定）",
                workspace_plan.MODE_LABELS[workspace_plan.MODE_MANUAL])
            text += " · 目标机已存在" if p.exists else " · 目标机不存在（导入时会询问是否创建）"
            if picked > 1:
                text += " · 将覆盖全部 %d 条会话" % picked
        elif picked <= 1:
            p = plans[0]
            text = workspace_plan.describe(p)
            if p.path and not p.exists and p.mode == workspace_plan.MODE_SESSION:
                text += " · 导入时会询问是否创建"
        else:
            text = self._describe_multi(plans)
        if self.manual_var.get() and not manual:
            text += "　⚠ 已勾选「手动指定」但未填值，将按自动判定"
        self.ws_preview_var.set(text)

    def _describe_multi(self, plans: list) -> str:
        """多选（≥2 条）时的落点预览文案：先报结构，再报来源，最后报存在性。"""
        MODE_DEFAULT = workspace_plan.MODE_DEFAULT
        MODE_SESSION = workspace_plan.MODE_SESSION
        n = len(plans)
        values = {p.value for p in plans}
        n_default = sum(1 for p in plans if p.mode == MODE_DEFAULT)
        shown = workspace_plan.describe_value(self.target_tool, plans[0].value)

        if len(values) == 1:
            if plans[0].mode == MODE_SESSION:
                # 各会话自带的工作区恰好是同一个：这就是它们真实的归属，直接显示
                text = ("%s    【自动 · 已选 %d 条会话，会话自带的工作区相同，均落到这一处】"
                        % (shown, n))
            elif plans[0].mode == MODE_DEFAULT:
                # 措辞按成因区分：真的「没记录工作区」 vs 「只记了不可逆的工作区 id」
                # （CodeBuddy），后者不能让用户以为「源会话本来就没有工作区」。
                reason = ("都没有可还原为路径的工作区" if plans[0].note
                          else "都没有记录工作区")
                text = ("已选 %d 条会话，%s → 统一落到 %s 的默认落点：%s"
                        "    【自动判定，非手动指定】"
                        % (n, reason, self.target_display, shown))
                if plans[0].note:
                    text += "　⚠ " + plans[0].note
            else:  # 取值恰好相同但来源混合（某条 cwd 正好等于默认落点）
                text = ("%s    【自动 · 已选 %d 条会话，落点恰好相同（来源混合：%d 条沿用"
                        "自带、%d 条用工具默认）】"
                        % (shown, n, n - n_default, n_default))
        else:
            text = ("已选 %d 条会话，存在 %d 个不同的工作区（各按自身自动判定写入，"
                    "此处不合并显示）" % (n, len(values)))
            if n_default:
                # 同样是「没工作区」，成因可能不同：真没记录 vs 只有不可逆的 id（CodeBuddy）
                why = ("未记录可还原为路径的工作区"
                       if any(p.note for p in plans if p.mode == MODE_DEFAULT)
                       else "未记录工作区")
                text += ("；其中 %d 条%s，将落到 %s 的默认落点"
                         % (n_default, why, self.target_display))
        missing = workspace_plan.describe_missing(plans)
        if missing:
            # 用「另有」而非「其中」：上面可能已经有「其中 N 条未记录工作区」一句
            text += "；另有 %d 处目标机不存在（导入时会询问是否创建）" % len(missing)
        return text

    def _on_manual_toggle(self) -> None:
        """手动指定开关：联动输入行、浏览按钮与警示文案。

        未勾选时**整行收起**（而不是置灰保留）——留着上一次填的手动路径，
        会让人误以为它就是本次导入实际要用的落点。
        """
        on = bool(self.manual_var.get())
        state = "normal" if on else "disabled"
        self.manual_combo.configure(state=state)
        self.manual_browse_btn.configure(state=state)
        if on:
            if not self.manual_var_value.get().strip():
                # 预填当前自动判定值，便于在它基础上微调
                auto = self._plan_for(self._selected_items()[0] if self._selected_items() else None)
                self.manual_var_value.set(auto.value)
            self._offer_hint_candidates()
            # 先 pack 警示行，再把它前面的输入行插进去（Tk 的 before= 要求参照物已被管理）
            self.manual_warn.pack(fill=tk.X, padx=8, pady=(2, 0), before=self.hint)
            self.manual_entry_row.pack(fill=tk.X, padx=8, pady=(2, 0), before=self.manual_warn)
        else:
            self.manual_entry_row.pack_forget()
            self.manual_warn.pack_forget()
        self._refresh_preview()

    def _offer_hint_candidates(self) -> None:
        """把「源会话正文里出现过的候选路径」列进手动输入下拉，供一键采用。

        场景：源会话是 CodeBuddy 的、只留了不可逆的 ``workspaceId``（md5），而本机
        又没有它的「已打开工程」记录（典型：另一台机器备份过来、本机从没打开过）——
        这时自动判定退到工具默认落点，但正文里其实出现过工程路径。**不自动采用**
        （实测启发式会推错），而是摆进下拉让人挑。
        """
        hints: list = []
        # 用**自动判定**的计划取候选：此刻手动值已生效，走 _selected_plans() 会拿到
        # 手动计划（没有 hints），候选就丢了。
        for it in self._selected_items():
            for h in self._plan_for(it).hints:
                if h not in hints:
                    hints.append(h)
        if not hints:
            return
        try:
            existing = list(self.manual_combo.cget("values") or ())
        except tk.TclError:  # pragma: no cover - 极端情况下控件已销毁
            existing = []
        self.manual_combo.configure(values=hints + [v for v in existing if v not in hints])

    def _browse_manual(self) -> None:
        """选目录 -> 填入工作区标识（WorkBuddy/DSH 为路径；CodeBuddy 派生 id；Reasonix 取目录名）。"""
        d = filedialog.askdirectory(title="选择目标工作区目录")
        if not d:
            return
        kind = workspace_plan.workspace_kind(self.target_tool)
        if kind == workspace_plan.KIND_ID:
            d = workspace_plan.codebuddy_workspace_id(d)
        elif kind == workspace_plan.KIND_SCOPE:
            d = workspace_plan.scope_from_path(d) or d
        self.manual_var_value.set(d)
        self._refresh_preview()

    # ------------------------------------------------------------ 事件 --
    def _on_source_change(self) -> None:
        src = self._by_display.get(self.src_var.get())
        if src is None:
            return
        self.cur_source = src
        self.src_note.configure(text=src.note)
        self.src_root_var.set(session_migration.default_source_root(src.tool))
        self._scan()
        self._refresh_notices()

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(title="选择来源数据目录")
        if d:
            self.src_root_var.set(d)
            self._scan()

    def _scan(self) -> None:
        if self.cur_source is None:
            return
        self.listbox.delete(0, tk.END)
        # 一次扫全（含子代理），由开关决定**显示**哪些：勾选/取消勾选无需重新扫描。
        self._all_items = session_migration.list_source_sessions(
            self.cur_source.tool, self.src_root_var.get(), include_subagents=True
        )
        self._render_items()
        if self.items:
            self.listbox.selection_set(0)
            self._on_pick()
        else:
            self._refresh_preview()

    def _on_subagent_toggle(self) -> None:
        """勾选/取消「包含子代理会话」：只重排当前已扫描到的条目，不重新扫描。"""
        self._render_items()
        if self.items:
            self.listbox.selection_set(0)
        self._on_pick()

    def _render_items(self) -> None:
        """按「包含子代理会话」开关把 ``_all_items`` 渲染进列表，并维护 ``items``。

        ``items`` 必须与列表**逐项对齐**（``_selected_items`` 用列表下标取条目）。
        """
        show_sub = bool(self.subagent_var.get())
        all_items = list(self._all_items)
        self.items = [it for it in all_items
                      if show_sub or not it.get("subagent")]
        hidden = len(all_items) - len(self.items)
        self.listbox.delete(0, tk.END)
        for it in self.items:
            title = it["title"] or "(无标题)"
            mark = "〔子代理〕" if it.get("subagent") else ""
            self.listbox.insert(tk.END, "%s%s    —  %s" % (mark, title[:60], it["detail"]))
        if hidden:
            self.subagent_hint_var.set("另有 %d 条子代理会话（勾选后显示）" % hidden)
        elif any(it.get("subagent") for it in all_items):
            self.subagent_hint_var.set("已显示子代理会话：导入时会挂到父会话下，不占会话列表")
        else:
            self.subagent_hint_var.set("")
        self.status_lbl.configure(
            text="共扫描到 %d 个会话%s" % (
                len(self.items), ("（已隐藏 %d 条子代理会话）" % hidden) if hidden else ""),
            foreground="#0a6" if self.items else "#a05a00",
        )

    def _on_pick(self) -> None:
        """选中会话变化：只需刷新「目标工作区」预览（工作区由规则自动判定）。"""
        self._refresh_preview()

    # --------------------------------------------- 页签 B：备份包 -> 来源根 --
    def _on_tab_change(self, _ev=None) -> None:
        """切页签只换「来源」：回到哪个页签就重算该页签的来源（会话列表共用同一份）。

        页签 B **绝不改动页签 A 的下拉**（方案 §6.2：不静默改判、不自动切下拉）。
        """
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        if idx == 0:
            src = self._by_display.get(self.src_var.get())
            if src is not None:
                self.cur_source = src
                self._scan()
                self._refresh_notices()
            return
        if not self._can_import:
            return
        if not self._pkg_rows:
            self._load_packages()
        if self.pkg_tree.selection():
            self._on_pkg_pick()

    # ---- 目录记忆（上次使用的备份包目录） ----
    def _pkg_memory_path(self) -> str:
        return os.path.join(_cache_dir(), "import_sources.json")

    def _default_pkg_dir(self) -> str:
        """页签 B 的初始目录：上次用过的（仍在）> 本工具默认备份目录。"""
        try:
            with open(self._pkg_memory_path(), "r", encoding="utf-8") as fh:
                saved = (json.load(fh) or {}).get("backup_dir") or ""
        except (OSError, ValueError):
            saved = ""
        if saved and os.path.isdir(saved):
            return saved
        try:
            return self.app._tool_dir("backup")
        except Exception:  # noqa: BLE001 - 取不到就用主目录
            return os.path.expanduser("~")

    def _remember_pkg_dir(self, directory: str) -> None:
        try:
            path = self._pkg_memory_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"backup_dir": directory}, fh, ensure_ascii=False)
        except OSError:
            pass

    def _browse_pkg_dir(self) -> None:
        cur = self.pkg_dir_var.get()
        d = filedialog.askdirectory(
            title="选择备份包所在目录", initialdir=cur if os.path.isdir(cur) else None
        )
        if not d:
            return
        self.pkg_dir_var.set(d)
        self._remember_pkg_dir(d)
        self._load_packages()

    # ---- 列表：与备份浏览器共用 backup_scan（递归、上限、剪枝、取消全一致） ----
    def _load_packages(self) -> None:
        """首屏同步只扫一层 → 立刻渲染；随后后台逐层扫到默认深度。"""
        if not self._can_import:
            return
        self._cancel_pkg_scan()
        self._pkg_rows = []
        self.pkg_tree.delete(*self.pkg_tree.get_children())
        self._pkg_done = True
        self._pkg_progress = None
        directory = self.pkg_dir_var.get()
        top = backup_scan.scan(directory, max_depth=0, manifest_limit=0,
                              cache=self._pkg_cache)
        self._pkg_rows = top.rows
        self._refresh_pkg_view()
        self._set_pkg_status(top.progress())
        ev = threading.Event()
        self._pkg_cancel = ev
        self._pkg_done = False

        def worker() -> None:
            res = backup_scan.scan(
                directory, cancel=ev, cache=self._pkg_cache,
                on_layer=lambda rows, prog: self._pkg_queue.put(("layer", rows, prog)),
            )
            self._pkg_queue.put(("done", res, res.progress()))

        threading.Thread(target=worker, daemon=True).start()
        self.win.after(120, self._poll_pkg_scan)

    def _cancel_pkg_scan(self) -> None:
        ev = self._pkg_cancel
        if ev is not None:
            ev.set()
        self._pkg_cancel = None

    def _poll_pkg_scan(self) -> None:
        if not self._alive():
            return
        while True:
            try:
                kind, payload, progress = self._pkg_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "layer":
                self._pkg_rows = payload
                self._refresh_pkg_view()
                self._set_pkg_status(progress)
            else:
                self._pkg_rows = payload.rows
                self._pkg_done = True
                self._refresh_pkg_view()
                self._set_pkg_status(progress)
        if not self._pkg_done:
            self.win.after(150, self._poll_pkg_scan)

    def _alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _pkg_filtered_rows(self) -> list:
        """按「只显示可导入的包」过滤。

        与备份浏览器的开关**判定不同**（方案 §4.2）：这里排除本工具自己的包
        （该走还原）与加密工具的包（永远不可导入）。
        """
        if not self.pkg_filter_var.get():
            return list(self._pkg_rows)
        return [r for r in self._pkg_rows
                if backup_scan.is_importable_for(r, self.target_tool)]

    def _refresh_pkg_view(self) -> None:
        if not self._can_import:
            return
        prev_sel = self.pkg_tree.selection()
        self.pkg_tree.delete(*self.pkg_tree.get_children())
        rows = self._pkg_filtered_rows()
        for r in rows:
            label, _color = BackupBrowser.KIND_LABEL.get(
                r.kind, BackupBrowser.KIND_LABEL["unknown"])
            dim = not backup_scan.is_importable_for(r, self.target_tool)
            self.pkg_tree.insert(
                "", "end", iid=r.path, tags=("dim",) if dim else (),
                values=(r.name, label, human_size(r.size),
                        datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")),
            )
        keep = [p for p in prev_sel if self.pkg_tree.exists(p)]
        if keep:
            self.pkg_tree.selection_set(keep[0])

    def _set_pkg_status(self, progress) -> None:
        if not self._can_import:
            return
        hidden = len(self._pkg_rows) - len(self._pkg_filtered_rows())
        text = "已扫描 %s（第 %d 层 / 共 %d 个目录）· 命中 %d 个包" % (
            self.pkg_dir_var.get(), progress.layers, progress.dirs_scanned,
            len(self._pkg_rows),
        )
        if hidden > 0:
            text += " · 另有 %d 个不可导入的包已隐藏" % hidden
        if progress.truncated:
            text += " · 已停止（%s）" % (progress.reason_label or "达到上限")
        try:
            self.pkg_lbl.config(text=text)
        except Exception:  # noqa: BLE001 - 窗口已销毁
            pass

    # ---- 选中包 -> 认工具 -> 解包 -> 扫描会话 ----
    def _on_pkg_pick(self) -> None:
        """选中备份包：认出来源工具，后台按需解包并交给既有扫描函数。"""
        if not self._can_import:
            return
        sel = self.pkg_tree.selection()
        if not sel:
            return
        path = sel[0]
        # 去重：同一个包正在解包（`_arc_pending`）或已解好（`_arc_path`）就直接返回。
        # 否则每次列表重建 / 切页签都会重开一个后台解包线程，并把上一个结果判为过期。
        if path == self._arc_pending or path == self._arc_path:
            return
        self._arc_pending = path
        self._arc_token += 1
        token = self._arc_token
        self.import_btn.configure(state="disabled")
        self.items = []
        self.listbox.delete(0, tk.END)
        self.pkg_src_lbl.configure(text="来源软件：正在识别…", foreground="#555")
        self.pkg_detail_lbl.configure(text="")
        self.status_lbl.configure(text="正在识别并解包该备份包…", foreground="#a05a00")

        def worker() -> None:
            try:
                info = inspect_backup(path)
                mf = info.get("manifest") or {}
                tool, src = backup_scan.identify_tool(
                    info.get("entries") or [], mf, prefer=self.target_tool
                )
                if not tool:
                    self.win.after(0, self._apply_archive_meta, token, path, info,
                                   "", "", None, "无法识别该包属于哪个工具。")
                    return
                row = next((r for r in self._pkg_rows if r.path == path), None)
                importable = (row is not None
                              and backup_scan.is_importable_for(row, self.target_tool))
                if not importable:
                    # 行内 L1/L2 可能没认出（L3 指纹才认出），这里以识别结果为准再判一次
                    importable = (tool != self.target_tool
                                  and import_matrix.ALL_SOURCES.get(tool) is not None
                                  and any(s.tool == tool
                                          for s in import_matrix.importable_sources_for(
                                              self.target_tool)))
                if not importable:
                    self.win.after(0, self._apply_archive_meta, token, path, info,
                                   tool, src, None, "")
                    return
                arc = archive_source.extract_session_root(path, tool, light=True)
                try:
                    items = session_migration.list_source_sessions(
                        tool, arc.root, include_subagents=True)
                except Exception:
                    arc.cleanup()
                    raise
                self.win.after(0, self._apply_archive, token, path, info, tool, src,
                               arc, items)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) if isinstance(
                    exc, archive_source.ArchiveSourceError) else "%s: %s" % (
                        type(exc).__name__, exc)
                self.win.after(0, self._apply_archive_meta, token, path, None,
                               "", "", None, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_archive_meta(self, token: int, path: str, info, tool: str,
                            src: str, arc, error: str) -> None:
        """主线程：只更新「来源软件 + 包详情 + 提示」，不产生会话列表。"""
        if token != self._arc_token or not self._alive():
            if arc is not None:
                arc.cleanup()
            return
        self._arc_pending = ""
        self._arc_path = path
        self._show_pkg_meta(info, tool, src)
        if error:
            self.pkg_src_lbl.configure(
                text="来源软件：无法确定（%s）" % error, foreground="#a05a00")
            self.status_lbl.configure(text=error, foreground="#a05a00")
            self._refresh_preview()
            self._refresh_notices()
            return
        if self.items == []:
            self.status_lbl.configure(
                text="该包不能导入为「%s」的会话（本工具的包请走「还原备份/快照」）。"
                     % self.target_display,
                foreground="#a05a00")
        self._refresh_preview()
        self._refresh_notices()

    def _apply_archive(self, token: int, path: str, info, tool: str, src: str,
                       arc, items: list) -> None:
        """主线程：接住解包结果（过期结果直接丢弃并清理）。"""
        if token != self._arc_token or not self._alive():
            arc.cleanup()
            return
        old = self._archive
        self._archive = arc
        self._arc_pending = ""
        self._arc_path = path
        if old is not None:
            old.cleanup()
        self.cur_source = import_matrix.ALL_SOURCES.get(tool) or self.cur_source
        self._show_pkg_meta(info, tool, src)
        self._all_items = items
        self._render_items()
        if self.items:
            self.listbox.selection_set(0)
        self.import_btn.configure(state="normal")
        note = ("；会话正文将在导入时解出" if arc.deferred else "")
        self.status_lbl.configure(
            text="已从备份包解出 %d 个可导入会话（按需解出 %d/%d 个条目%s）"
                 % (len(self.items), arc.extracted, arc.total, note),
            foreground="#0a6" if self.items else "#a05a00",
        )
        self._refresh_preview()
        self._refresh_notices()

    def _show_pkg_meta(self, info, tool: str, src: str) -> None:
        """只读展示「来源软件（依据）+ 包详情」。"""
        if info is None:
            return
        src_label = {"manifest": "包内声明", "filename": "文件名",
                     "dirname": "父目录名", "structure": "结构指纹"}.get(src, "包内声明")
        if tool:
            self.pkg_src_lbl.configure(
                text="来源软件：%s（依据：%s）" % (backup_scan.display_name(tool), src_label),
                foreground="#555")
        mf = info.get("manifest") or {}
        parts = []
        if mf.get("kind"):
            parts.append("类型：%s" % BackupBrowser.KIND_LABEL.get(
                mf["kind"], (mf["kind"], ""))[0])
        parts.append("文件数：%d" % info.get("file_count", 0))
        parts.append("原始大小：%s" % human_size(info.get("total_bytes", 0)))
        if mf.get("created_at"):
            parts.append("导出时间：%s" % mf["created_at"])
        if mf.get("source_root"):
            parts.append("来源机器：%s" % mf["source_root"])
        if mf.get("items"):
            parts.append("包含模块：%s" % ", ".join(mf["items"]))
        self.pkg_detail_lbl.configure(text="　·　".join(parts))

    # ---- 变化预告 ----
    def _refresh_notices(self) -> None:
        """按「当前来源 + 目标工具」重算变化预告（常驻区域，随选中实时刷新）。"""
        src_tool = self.cur_source.tool if self.cur_source else ""
        data = import_matrix.notices_for(src_tool, self.target_tool)
        self._warn_count = len(data["warn"])
        src_name = (backup_scan.display_name(src_tool) if src_tool
                    else "（未选择来源）")
        lines: list = []
        if not src_tool:
            lines.append(("请先选择来源（页签一选软件、页签二选备份包）。",
                          "info"))
        else:
            lines.append(("本次导入的变化预告（%s → %s）"
                          % (src_name, self.target_display), "head"))
            lines.append(("── 必然变化 ──", "head"))
            lines.extend(("· " + t, "info") for t in data["must"])
            lines.append(("── 需要注意 ──", "head"))
            if data["warn"]:
                lines.extend(("⚠ " + t, "warn") for t in data["warn"])
            else:
                lines.append(("（无）", "info"))
            lines.append(("── 不影响的 ──", "head"))
            lines.extend(("· " + t, "info") for t in data["keep"])
        self._render_notices(lines)

    def _render_notices(self, lines: list) -> None:
        widget = self.notice_text
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        first = True
        for text, tag in lines:
            if not first:
                widget.insert(tk.END, "\n", tag)
            first = False
            widget.insert(tk.END, text, tag)
        widget.configure(state="disabled")

    def _on_close(self) -> None:
        """关对话框：取消扫描 + 清理临时解包目录（方案 §5.5：不复活临时目录）。"""
        self._cancel_pkg_scan()
        self._arc_token += 1
        if self._archive is not None:
            self._archive.cleanup()
            self._archive = None
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def _selected_items(self) -> list:
        """当前选中的全部会话条目（支持多选）。"""
        return [self.items[i] for i in self.listbox.curselection() if 0 <= i < len(self.items)]

    def _selected_item(self):
        items = self._selected_items()
        return items[0] if items else None

    # ------------------------------------------------------------ 导入 --
    def _warn_summary(self) -> str:
        """最终确认框里的一行变化预告摘要（复用同一个弹窗，不新增点击）。"""
        if not self._warn_count:
            return ""
        return ("\n\n本次导入有 %d 条需要注意的变化，已在界面的「变化预告」里列出。"
                % self._warn_count)

    def _confirm_default(self) -> str:
        """有 warn 级变化时，确认框默认按钮改为「否」（方案 §6.5.3）。"""
        return messagebox.NO if self._warn_count else messagebox.YES

    def _confirm_manual_override(self, items: list, plan) -> bool:
        """手动指定工作区时的确认：明确指出会覆盖「自带工作区」的会话。"""
        owned = [it for it in items
                 if workspace_plan.session_workspace(self.target_tool, it)]
        if not owned or HEADLESS:
            return True
        return bool(messagebox.askyesno(
            "确认覆盖工作区",
            "已勾选「手动指定工作区」。\n\n"
            "本次导入的 %d 条会话将全部落到：\n%s\n\n"
            "其中 %d 条本身就自带工作区，它们原本的工作区归属会被覆盖。\n\n是否继续？"
            % (len(items), plan.value, len(owned)) + self._warn_summary(),
            parent=self.win, default=self._confirm_default(),
        ))

    def _confirm_unresolved_workspaces(self, items: list, plans: list) -> bool:
        """落点无法确定的会话：导入前**显式**确认，不再静默退默认落点。

        这类会话原本**有**工作区，但只留下一个不可逆的 id（CodeBuddy 的 ``workspaceId``
        = ``md5(项目路径)``），备份包与本机都没能还原成路径。若不提醒，用户只会在导入
        完成后才发现会话跑到了工具的默认目录（WorkBuddy 还会现造一个带时间戳的新目录）。

        :return: ``True`` 继续导入；``False`` 不继续（已中止，或已切到「手动指定」）。
        """
        if HEADLESS:
            return True
        pairs = [(it, p) for it, p in zip(items, plans)
                 if p.mode == workspace_plan.MODE_DEFAULT
                 and (it.get("workspace_id") or "").strip()]
        if not pairs:
            return True

        cands: list = []
        for it, _p in pairs:
            for c in (it.get("workspace_candidates") or []):
                if c and c not in cands:
                    cands.append(c)

        head = "\n".join(
            "· %s（工作区 %s…）" % (it.get("title") or "(无标题)",
                                    (it.get("workspace_id") or "")[:8])
            for it, _p in pairs[:6])
        if len(pairs) > 6:
            head += "\n…（共 %d 条）" % len(pairs)
        msg = (
            "有 %d 条会话无法确定原本的工作区：\n\n%s\n\n"
            "它们原本有工作区，但只留下一个不可逆的 id（由项目路径哈希而来），"
            "备份包与本机都没能把它还原成路径。\n\n"
            "本次会把这些会话落到目标工具的默认落点：\n%s\n"
            % (len(pairs), head, pairs[0][1].path or pairs[0][1].value)
        )
        if cands:
            msg += ("\n它们的正文里出现过下列候选路径（仅供参考、不保证正确）：\n"
                    + "\n".join("· " + c for c in cands[:6]))
            if len(cands) > 6:
                msg += "\n…（共 %d 条）" % len(cands)
        msg += ("\n\n是：接受默认落点，继续导入\n"
                "否：返回，改用「手动指定工作区」自己填\n"
                "取消：中止本次导入")

        ans = messagebox.askyesnocancel("无法确定原始工作区", msg, parent=self.win)
        if ans is None:
            self.status_lbl.configure(text="已取消导入（工作区未确定）",
                                      foreground="#a05a00")
            return False
        if ans:
            return True
        # 「否」：切到手动指定并把候选预填进下拉，让用户挑一个或自己填
        self.manual_var.set(True)
        if cands and not self.manual_var_value.get().strip():
            self.manual_var_value.set(cands[0])
        self._on_manual_toggle()
        self.status_lbl.configure(
            text="已切到「手动指定工作区」——确认落点后再次点「导入」",
            foreground="#a05a00")
        return False

    def _ask_create_workspaces(self, plans: list):
        """工作区在目标机不存在时询问是否创建。

        :return: ``True`` 创建 / ``False`` 不创建（仍按原落点导入）/ ``None`` 中止导入。
        """
        if HEADLESS:
            return True
        return messagebox.askyesnocancel(
            "目标工作区不存在",
            "以下目标工作区在目标机上不存在：\n\n%s\n\n是否自动创建？\n"
            "　是：先创建目录再导入\n"
            "　否：不创建，仍按原落点导入（目标工具写出时会自建自己需要的目录）\n"
            "　取消：中止本次导入" % missing_workspaces_text(plans),
            parent=self.win,
        )

    def _do_import(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("请选择会话",
                                "请先在列表中选择要导入的会话（可 Ctrl / Shift 多选）。",
                                parent=self.win)
            return
        if self.cur_source is None:
            return

        tool = self.target_tool
        if tool not in ("reasonix", "codebuddy", "workbuddy", "dsh"):
            messagebox.showinfo("不支持", "目标工具暂不支持导入。", parent=self.win)
            return

        # ---- 子代理会话：必须连同父会话一起导入（DSH 侧父子关系是双向写的） ----
        resolved = self._resolve_subagent_items(items)
        if resolved is None:
            self.status_lbl.configure(text="已取消导入（子代理会话未处理）", foreground="#a05a00")
            return
        items = resolved

        # ---- 工作区：自动判定（源会话自带 > 工具默认），手动指定则覆盖全部 ----
        manual = self._manual_value()
        plans = [self._plan_for(None if manual else it, manual) for it in items]
        if manual and not self._confirm_manual_override(items, plans[0]):
            return
        # 落点「推不出来」的会话（只有不可逆的 workspaceId）：导入前显式确认一次，
        # 免得用户事后才发现它们跑到了工具默认目录。
        if not manual and not self._confirm_unresolved_workspaces(items, plans):
            return

        self.warns = []
        missing = workspace_plan.describe_missing(plans)
        if missing:
            choice = self._ask_create_workspaces(missing)
            if choice is None:
                self.status_lbl.configure(text="已取消导入（工作区未创建）", foreground="#a05a00")
                return
            if choice:
                created = [p for p in (workspace_plan.ensure(m) for m in missing) if p[0]]
                if created:
                    self.warns.append("已按要求创建目标工作区目录：\n"
                                      + "\n".join("· " + c[1] for c in created))
            else:
                self.warns.append(
                    "目标工作区目录不存在且未创建，仍按原落点导入；"
                    "若该路径在目标机确实不存在，目标工具可能把会话显示为「未知工作区」。")

        # ---- 逐条组装导入参数（每条会话用自己的落点） ----
        jobs = self._build_jobs(items, plans)
        # 子代理条目可能因父会话线索缺失被跳过：如实告知，不静默吞掉。
        done_items = {id(it) for it, _kw in jobs}
        skipped = [it for it in items
                   if it.get("subagent") and id(it) not in done_items]
        if skipped:
            self.warns.append(
                "以下子代理会话的来源里没有记住父会话 id，已跳过（未写出）：\n"
                + "\n".join("· %s" % (it.get("title") or "(无标题)") for it in skipped))
        if not jobs:
            if skipped:
                messagebox.showinfo(
                    "没有可导入的会话",
                    "选中的条目都是无法挂接父会话的子代理会话，本次没有导入任何会话。",
                    parent=self.win)
            return

        self._result = None
        self.import_btn.configure(state="disabled")
        self.status_lbl.configure(
            text="正在导入 %d 条会话…（解析来源 → 写出为目标原生格式）" % len(jobs),
            foreground="#0a6")

        threading.Thread(target=self._run_jobs, args=(jobs,), daemon=True).start()
        self.win.after(150, self._poll_import)

    def _build_jobs(self, items: list, plans: list) -> list:
        """把「会话 + 落点计划」翻译成 ``migrate_session`` 的调用参数（纯函数，便于测试）。

        导入 DSH 时额外走 :func:`session_migration.plan_dsh_import`：它先给每条会话
        分配好目标 id / 创建时间，并建立子代理 → 父会话的挂接（父会话日志要写
        ``subagent/catalog``，其中含子会话 id，故必须先分配再写）。父会话排在其子代理
        之前写出。
        """
        tool = self.target_tool
        order = list(items)
        info: dict = {}
        if tool == "dsh":
            sub_plan = session_migration.plan_dsh_import(items)
            order = list(sub_plan["ordered"])
            info = {id(job["item"]): job for job in sub_plan["jobs"]}
        plan_of = {id(it): p for it, p in zip(items, plans)}
        jobs: list = []
        for it in order:
            plan = plan_of.get(id(it))
            if plan is None:
                continue
            kwargs = {
                "source_tool": self.cur_source.tool,
                "source_path": it["path"],
                "target_tool": tool,
                "target_root": self.tgt_root_var.get(),
            }
            if tool == "reasonix":
                # Reasonix 的工作区 = 项目名（scope）
                kwargs["scope"] = plan.value or workspace_plan.default_workspace("reasonix")
            else:
                # CodeBuddy 的 workspaceId / WorkBuddy 与 DSH 的工作区路径（cwd）
                kwargs["workspace_id"] = plan.value
            if self.cur_source.tool == "zcode":
                kwargs["source_session_id"] = it["id"]
            extra = info.get(id(it))
            if extra is not None:
                kwargs["session_id"] = extra["session_id"]
                kwargs["created_ms"] = extra["created_ms"]
                if extra["parent_session_id"]:
                    kwargs["parent_session_id"] = extra["parent_session_id"]
                if extra["subagent_catalog"]:
                    kwargs["subagent_catalog"] = extra["subagent_catalog"]
            jobs.append((it, kwargs))
        return jobs

    def _resolve_subagent_items(self, items: list):
        """把子代理会话的父会话补进本次导入，返回补全后的条目列表。

        为什么必须补：DSH 的父子关系写两份——子会话 header 的 ``parentSession``
        与**父会话日志**里的 ``subagent/catalog``。父会话不在本批时，写出的子代理会话
        既不进侧边栏（子代理不登记工作区）又挂不到任何父会话，等于凭空多出一份没人能
        打开的记录。故：父会话可选时**显式询问后一并导入**；来源没记住父会话 id 的
        条目则跳过并如实告知，绝不降级成顶层会话。

        :return: 补全后的条目列表；用户取消时返回 ``None``（中止导入）。
        """
        if self.target_tool != "dsh":
            # 其它目标工具没有子代理结构：条目录入前已由开关决定是否显示，
            # 这里原样透传（子代理正文按普通会话写出）。
            return list(items)
        plan = session_migration.plan_dsh_import(items, available=self._all_items)
        if plan["missing_parents"] and not HEADLESS:
            names = "\n".join("· %s" % (it.get("title") or "(无标题)")[:60]
                              for it in plan["missing_parents"][:6])
            if not messagebox.askyesno(
                "有子代理会话无法挂接",
                "以下子代理会话的来源里没有记住父会话 id：\n\n%s\n\n"
                "DSH 不接受没有父会话的子代理会话，本次会**跳过**这些条目，"
                "其余会话正常导入。是否继续？" % names,
                parent=self.win,
            ):
                return None
        if plan["extra_parents"] and not HEADLESS:
            names = "\n".join("· %s" % (it.get("title") or "(无标题)")[:60]
                              for it in plan["extra_parents"][:6])
            if not messagebox.askyesno(
                "需要连同父会话一起导入",
                "选中的子代理会话在 DSH 里要挂到父会话下，而父会话日志需要先写出"
                "（含子会话 id 的 subagent/catalog）。\n\n"
                "以下父会话不在本次选择里，将**一并导入**（它们会像普通会话一样"
                "出现在会话列表里）：\n\n%s\n\n是否继续？" % names,
                parent=self.win,
            ):
                return None
        return list(plan["ordered"])

    def _run_jobs(self, jobs: list) -> None:
        """逐条导入（在后台线程执行），结果写入 ``self._result``。"""
        done: list = []
        errs: list = []
        arc = self._archive
        for it, kwargs in jobs:
            title = it.get("title") or "(无标题)"
            try:
                # 延迟解包模式：列表阶段只解了索引，先按会话补解正文再迁移。
                # 不是包来源（走页签一的数据目录）时 materialize 是空操作。
                if arc is not None:
                    arc.materialize(kwargs["source_path"])
                sid = session_migration.migrate_session(
                    warn=lambda m: self.warns.append(m), **kwargs
                )
                done.append((title, sid))
            except Exception as exc:  # noqa: BLE001
                errs.append("%s：%s: %s" % (title, type(exc).__name__, exc))
        if errs and not done:
            self._result = ("err", "\n".join(errs))
        elif errs:
            self._result = ("partial", (done, errs))
        else:
            self._result = ("ok", done)

    def _poll_import(self) -> None:
        if self._result is None:
            self.win.after(150, self._poll_import)
            return
        status, payload = self._result
        self.import_btn.configure(state="normal")

        if status in ("ok", "partial"):
            errs: list = []
            if status == "partial":
                done, errs = payload
            elif isinstance(payload, list):
                done = payload
            else:  # 兼容单条结果（("ok", "<sid>")）
                done = [("", payload)]
            body = "\n".join("· %s → %s" % (t or "(无标题)", sid) for t, sid in done[:10])
            if len(done) > 10:
                body += "\n…（共 %d 条）" % len(done)
            if errs:
                self.status_lbl.configure(
                    text="部分完成：%d 条成功 / %d 条失败" % (len(done), len(errs)),
                    foreground="#a05a00")
                messagebox.showwarning(
                    "部分导入完成",
                    "已导入 %d 条会话为 %s 的原生会话：\n\n%s\n\n失败 %d 条：\n%s"
                    % (len(done), self.target_display, body, len(errs),
                       "\n".join("· " + e for e in errs)),
                    parent=self.win)
            else:
                self.status_lbl.configure(
                    text="导入完成：%d 条会话" % len(done), foreground="#0a6")
                messagebox.showinfo(
                    "导入完成",
                    "已导入 %d 条会话为 %s 的原生会话。\n\n%s\n\n"
                    "如界面未立即显示，请重启 %s 后再查看。"
                    % (len(done), self.target_display, body, self.target_display),
                    parent=self.win)
            # 警告汇总：**执行后**一次性展示，不逐条打断（非阻断，会话已写入）。
            if self.warns:
                messagebox.showwarning(
                    "导入已完成，但有注意事项",
                    "会话已成功写入，但有以下事项需要确认：\n\n"
                    + "\n\n".join("· " + w for w in self.warns),
                    parent=self.win,
                )
        else:
            self.status_lbl.configure(text="导入失败：%s" % payload, foreground="#c00")
            detail = payload
            if self.warns:
                detail += "\n\n附带提示：\n" + "\n".join("· " + w for w in self.warns)
            messagebox.showerror("导入失败", detail, parent=self.win)


#: 链接色：浅底用深蓝、深底用浅蓝。Tk 没有「链接色」这个概念，而**硬编码一种蓝**
#: 会在系统暗色主题下变成看不清的深蓝（本文件 ``_configure_doc_tags`` 对背景色
#: 有过同样的取舍）。
_LINK_FG_ON_LIGHT = "#1155cc"
_LINK_FG_ON_DARK = "#6caeff"

#: 「关于」里要变成可点击链接的地址形态。
_URL_RE = re.compile(r"https?://\S+")


def _link_foreground(widget: "tk.Misc") -> str:
    """按控件底色的亮度挑一个可读的链接色（取不到颜色就按浅底处理）。"""
    try:
        rgb = widget.winfo_rgb(widget.cget("background"))   # 每通道 0..65535
    except tk.TclError:
        return _LINK_FG_ON_LIGHT
    lum = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000
    return _LINK_FG_ON_LIGHT if lum > 32768 else _LINK_FG_ON_DARK


class AboutDialog:
    """「关于」对话框：版本信息 + **只读**的更新检查 + 打赏二维码。

    四件事必须守住（都由测试钉住）：

    1. **版本号来自 ``version.about_lines()``**，与 ``--version``、exe 版本资源同源；
       本类只负责把这几行画出来，不许自己拼版本字符串。
    2. **检查更新是只读的**：只调 ``updater.check_update()``（不下载、不替换），
       且跑在后台线程里；网络慢或需代理时界面不冻结。
    3. **二维码缺图时整块隐藏**（不留空白、不报错）：仓库当前并没有该图片，
       「源码模式 / 未随包分发」都会命中这条**正常路径**。
       ★ ``PhotoImage`` **必须持引用**（``self._qr_image``），否则被 GC 回收后
       二维码显示为空白——Tk 的经典坑。
    4. **项目地址可点、可复制**（用户反馈）：信息块用**只读 ``Text``** 而不是
       ``Label``（``Label`` 里的字**选不中**，地址只能手抄）；URL 挂 ``link`` 标签，
       单击用系统浏览器打开，拖选 ``Ctrl+C`` 或右键菜单复制。
    """

    def __init__(self, app: "QoderBackupApp"):
        self.app = app
        self._qr_image = None       # ★ 必须留在实例上，见类 docstring 第 3 条
        self._checking = False

        top = tk.Toplevel(app.root)
        self.top = top
        top.title("关于 %s" % _version.APP_NAME)
        top.transient(app.root)
        top.resizable(False, False)
        try:
            icon = app.root.wm_iconbitmap()
            if icon:
                top.iconbitmap(icon)
        except Exception:
            pass

        body = ttk.Frame(top)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(16, 4))
        self._build_qr(body)

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))

        # 信息块是**只读 Text**（不是 Label）：这样文字可选、可复制，见类 docstring。
        self.info = self._build_info(right)
        self.info.pack(fill=tk.X)

        ttk.Separator(right, orient="horizontal").pack(fill=tk.X, pady=8)

        row = ttk.Frame(right)
        row.pack(fill=tk.X)
        self.latest_var = tk.StringVar(value="最新版本：未检查")
        ttk.Label(row, textvariable=self.latest_var).pack(side=tk.LEFT)
        self.check_btn = ttk.Button(row, text="检查更新", width=10,
                                    command=self.check)
        self.check_btn.pack(side=tk.RIGHT)

        # 更新说明摘要：只在真有更新且带说明时才 pack（平时不占高度）。
        self.notes = tk.Text(right, height=6, width=46, wrap="word",
                             relief=tk.FLAT, state="disabled")
        self._notes_packed = False

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, pady=(4, 12))
        ttk.Button(btns, text="关闭", width=12, command=top.destroy).pack(
            side=tk.RIGHT, padx=16
        )

        top.update_idletasks()
        x = app.root.winfo_x() + (app.root.winfo_width() - top.winfo_width()) // 2
        y = app.root.winfo_y() + (app.root.winfo_height() - top.winfo_height()) // 3
        top.geometry("+%d+%d" % (max(0, x), max(0, y)))
        top.grab_set()

    def _build_info(self, parent) -> "tk.Text":
        """版本信息块：只读 ``Text``，项目地址**可点（开浏览器）也可复制**。

        为什么不用 ``tk.Label``：``Label`` 里的文字**选不中**，用户想把项目地址复制
        出去只能手抄。只读的 ``Text`` 照样不允许编辑，但**仍可拖选与 ``Ctrl+C``**
        （Tk 的常规行为），再给 URL 范围挂一个 ``link`` 标签就有了「点一下打开」。

        宽度必须**实测反推**，不能按 ``font.measure()`` 直接换算：``Text`` 的
        ``width`` 单位是「平均字符宽」而非像素，两者不相等且随字体/DPI 漂移
        ⇒ 先量 1 字符与 2 字符的 ``reqwidth`` 求差（= 真实字符宽），再按最长行
        换算列数。窄了会**裁掉**右边的字（``wrap="none"`` 不会折行），故宁可宽一点。
        """
        lines = _version.about_lines(self.app.adapter.display_name)
        font = tkfont.nametofont("TkDefaultFont")
        bg = self.top.cget("background")
        txt = tk.Text(
            parent, height=len(lines), width=1, wrap="none", font=font,
            borderwidth=0, highlightthickness=0, padx=0, pady=0,
            takefocus=0, cursor="arrow", background=bg,
        )
        txt.insert("1.0", "\n".join(lines))
        txt.tag_configure("link", foreground=_link_foreground(self.top),
                          underline=True)
        self._tag_links(txt)
        txt.tag_bind("link", "<Button-1>", self._on_link_click)
        txt.tag_bind("link", "<Enter>",
                     lambda _e: txt.configure(cursor="hand2"))
        txt.tag_bind("link", "<Leave>",
                     lambda _e: txt.configure(cursor="arrow"))
        txt.bind("<Button-3>", self._on_context)
        txt.configure(state="disabled")     # 只读：能选能复制，不能改

        txt.update_idletasks()
        one = txt.winfo_reqwidth()
        txt.configure(width=2)
        txt.update_idletasks()
        char_px = max(1, txt.winfo_reqwidth() - one)
        need_px = max(font.measure(line) for line in lines)
        txt.configure(width=max(1, -(-(need_px - (one - char_px)) // char_px)))
        return txt

    def _tag_links(self, txt: "tk.Text") -> None:
        """把文本里的 URL 都标上 ``link`` 标签（标签本身不携带地址）。"""
        text = txt.get("1.0", "end-1c")
        for m in _URL_RE.finditer(text):
            txt.tag_add("link", "1.0 +%dc" % m.start(), "1.0 +%dc" % m.end())

    def _url_at(self, index: str) -> str:
        """取某位置的**整行**里的 URL（没有则空串）。

        ★ 按「行」找而不是记「标签范围 → 地址」的映射：``tag_bind`` 是**按标签**
        （整个控件一份）生效的，单击时拿不到「点的哪个范围」，只能靠位置反查；
        而地址本来就是行内唯一的那一串，按行取足矣。
        """
        line = self.info.get(index + " linestart", index + " lineend")
        m = _URL_RE.search(line)
        return m.group(0) if m else ""

    def _on_link_click(self, event) -> None:
        self._open_url_at(self.info.index("@%d,%d" % (event.x, event.y)))

    def _open_url_at(self, index: str) -> None:
        """用系统默认浏览器打开该位置的地址（没有地址就什么也不做）。"""
        url = self._url_at(index)
        if url:
            webbrowser.open(url)

    def _on_context(self, event) -> None:
        """右键菜单：复制链接地址 / 复制选中 / 全选（``Label`` 时代这些都做不到）。"""
        menu = self._build_context_menu(
            self.info.index("@%d,%d" % (event.x, event.y))
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_context_menu(self, index: str) -> "tk.Menu":
        """按**点击所在行**拼菜单（独立成方法是为了能脱离鼠标事件测）。"""
        menu = tk.Menu(self.top, tearoff=False)
        url = self._url_at(index)
        if url:
            menu.add_command(label="复制链接地址",
                             command=lambda: self._copy_text(url))
        if self.info.tag_ranges("sel"):
            menu.add_command(label="复制", command=self._copy_selection)
        menu.add_command(label="全选", command=self._select_all)
        return menu

    def _copy_text(self, text: str) -> None:
        """写系统剪贴板。★ 写完必须 ``update_idletasks()``：Tk 的剪贴板由本进程
        提供服务，不刷一次就退出，Windows 上内容会丢。"""
        self.top.clipboard_clear()
        self.top.clipboard_append(text)
        self.top.update_idletasks()

    def _copy_selection(self) -> None:
        try:
            self._copy_text(self.info.get("sel.first", "sel.last"))
        except tk.TclError:      # 没有选区
            pass

    def _select_all(self) -> None:
        self.info.tag_add("sel", "1.0", "end-1c")

    def _build_qr(self, parent) -> None:
        """打赏二维码：**找不到图片就整块不建**（正常路径，不是异常）。"""
        path = resource_path("docs/images/reward_qr.png")
        if not path:
            return
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            return
        # ★ subsample() **只支持整数倍**，故按「可见宽约 240」取整数倍降低。
        k = max(1, -(-img.width() // 240))
        if k > 1:
            img = img.subsample(k, k)
        self._qr_image = img        # ★ 持引用，否则 GC 后显示空白
        col = ttk.Frame(parent)
        col.pack(side=tk.LEFT, anchor="n")
        ttk.Label(col, image=img).pack()
        ttk.Label(col, text="打赏支持", foreground="#666").pack(pady=(4, 0))

    # -- 检查更新（只读） -------------------------------------------------- #
    def check(self) -> None:
        """点「检查更新」：置为检查中并禁用按钮，实际请求走后台线程。"""
        if self._checking:
            return
        self._checking = True
        self.check_btn.configure(state="disabled")
        self.latest_var.set("最新版本：检查中…")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        """后台线程：真正发请求；结果一律经 ``after`` 回到主线程再碰控件。"""
        settings = _prefs.update_settings()
        failed = False
        try:
            result = check_update(
                current=_version.__version__,
                include_prerelease=bool(settings.get("include_prerelease")),
                proxy=settings.get("proxy") or "",
                skipped_version=settings.get("skipped_version") or "",
            )
        except Exception as exc:      # noqa: BLE001 - 任何异常都要落到界面，不许静默
            failed, text, info, notes = True, "检查更新失败：%r" % (exc,), None, ""
        else:
            text = describe_result(result, _version.__version__, platform_asset_name())
            info = result.info
            notes = (info.notes if info is not None else "") or ""
        try:
            self.top.after(0, lambda: self._apply_check(text, info, notes, failed))
        except tk.TclError:           # 对话框已被关闭
            pass

    def _apply_check(self, text: str, info, notes: str, failed: bool) -> None:
        """主线程里刷新结果：文案 + 说明摘要；成功检查后记录检查时间。"""
        self._checking = False
        try:
            self.check_btn.configure(state="normal")
        except tk.TclError:
            return
        if info is not None:
            self.latest_var.set("最新版本：v%s" % info.version)
            # 更新说明 = 结论（含「未做哈希校验 / 这是预发布」这类提醒）+ 发布说明
            notes = text + (("\n\n" + notes) if notes else "")
        else:
            self.latest_var.set(text)
        if notes:
            self.notes.configure(state="normal")
            self.notes.delete("1.0", tk.END)
            self.notes.insert("1.0", notes)
            self.notes.configure(state="disabled")
            if not self._notes_packed:
                self.notes.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
                self._notes_packed = True
        if not failed:
            _prefs.record_check()


class UpdateSettingsDialog:
    """「设置 → 更新设置」：自动检查开关 / 频率 / 更新通道 / 代理。

    持久化走 ``prefs.save_update_settings``（**深度合并**写入 ``prefs.json``）。
    ★ 不要在这里自己写 JSON：整文件覆盖会把同文件的 ``last_tool`` 抹掉，
    这正是 ``prefs.py`` 改成读-改-写的原因。
    """

    def __init__(self, app: "QoderBackupApp"):
        self.app = app
        cfg = _prefs.update_settings()

        top = tk.Toplevel(app.root)
        self.top = top
        top.title("更新设置")
        top.transient(app.root)
        top.resizable(False, False)

        body = ttk.Frame(top)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)

        self.auto_var = tk.BooleanVar(value=bool(cfg["auto_check"]))
        ttk.Checkbutton(body, text="启动时自动检查更新", variable=self.auto_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        ttk.Label(body, text="检查频率：").grid(row=1, column=0, sticky="w", pady=4)
        self.interval_var = tk.StringVar(
            value=_prefs.INTERVAL_LABELS.get(cfg["interval"], _prefs.INTERVAL_LABELS["daily"])
        )
        self.interval_combo = ttk.Combobox(
            body, textvariable=self.interval_var, state="readonly", width=16,
            values=[_prefs.INTERVAL_LABELS[k] for k in _prefs.CHECK_INTERVALS],
        )
        self.interval_combo.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=4)

        self.pre_var = tk.BooleanVar(value=bool(cfg["include_prerelease"]))
        ttk.Checkbutton(body, text="包含预发布版本（rc / beta 等）",
                        variable=self.pre_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )

        ttk.Label(body, text="代理：").grid(row=3, column=0, sticky="w", pady=4)
        self.proxy_var = tk.StringVar(value=cfg["proxy"])
        ttk.Entry(body, textvariable=self.proxy_var, width=32).grid(
            row=3, column=1, sticky="w", padx=(6, 0), pady=4
        )

        ttk.Label(
            body,
            text="自动检查默认关闭：本机可能需要代理才能访问 GitHub，\n"
                 "静默检查会让你每次启动都白等几秒。\n"
                 "代理留空时按「环境变量 → 直连」的顺序尝试；\n"
                 "只对 GitHub / Gitee 请求生效，不影响其它功能。",
            justify=tk.LEFT, foreground="#666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, pady=(4, 12))
        ttk.Button(btns, text="保存", width=10, command=self.save).pack(
            side=tk.RIGHT, padx=(6, 16)
        )
        ttk.Button(btns, text="取消", width=10, command=top.destroy).pack(
            side=tk.RIGHT
        )

        top.update_idletasks()
        x = app.root.winfo_x() + (app.root.winfo_width() - top.winfo_width()) // 2
        y = app.root.winfo_y() + (app.root.winfo_height() - top.winfo_height()) // 3
        top.geometry("+%d+%d" % (max(0, x), max(0, y)))
        top.grab_set()

    def _interval_key(self) -> str:
        """把下拉里的中文标签翻回持久化用的键（翻不出来就回退 ``daily``）。"""
        label = self.interval_var.get()
        for key, text in _prefs.INTERVAL_LABELS.items():
            if text == label:
                return key
        return "daily"

    def save(self) -> None:
        """保存并关闭。写失败只提示、不抛（偏好存不下不该挡住主流程）。"""
        ok = _prefs.save_update_settings(
            auto_check=bool(self.auto_var.get()),
            interval=self._interval_key(),
            include_prerelease=bool(self.pre_var.get()),
            proxy=self.proxy_var.get().strip(),
        )
        if not ok:
            messagebox.showwarning("保存失败", "更新设置未能写入偏好文件。")
        self.top.destroy()


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
