"""备份 / 还原的命令行入口：``tools`` / ``inspect`` / ``backup`` / ``restore``。

为什么单独成模块、且**不导入 tkinter**：与 ``--version`` / ``--docs`` / ``--check-update``
同一套理由——备份与还原的实质逻辑早已解耦在 :mod:`ai_env_clone.core` + 适配器层，
GUI 只是它的一个调用方。脚本、CI、以及没装 tkinter 的环境应当能驱动**同一套**逻辑
（同一个适配器、同一份导出脱敏、同一份还原路径改写与索引合并规则），而不是必须开
界面手工点。因此本模块只依赖 core 与适配器，输出为纯文本。

危险动作必须显式确认（``--yes``）：

- **备份**：本次勾选内容含「无法脱敏的对话原文」时（见 :func:`core.pre_export_notices`），
  与 GUI 的「导出前请确认」同一口径——命令行不能比界面少一道闸门。
- **还原**：写盘会改动本机真实工具数据，且「合并 / 全覆盖」两种方式的后果差别很大，
  所以先打印将要做什么，再由 ``--yes`` 表示已确认。

缺确认时**不执行**，以退出码 ``2`` 结束并说明该补哪个参数：脚本里「忘了加参数」
应当失败，而不是静默做完危险的事。

退出码：``0`` 成功 / ``1`` 失败 / ``2`` 用法错误或未确认而中止。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from typing import Callable

from . import backup_scan
from .adapters import get_adapter, list_adapters
from .core import (
    BackupError,
    BackupItem,
    ProgressInfo,
    companion_notes,
    human_size,
    inspect_backup,
    manifest_origin_info,
    pre_export_notices,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DECLINED = 2

#: 本子入口管理的子命令；``__main__`` 只在命中这些时才走 CLI，否则照旧启动界面。
SUBCOMMANDS = ("tools", "inspect", "backup", "restore")

RESTORE_MODE_MERGE = "merge"
RESTORE_MODE_REPLACE = "replace"

Emit = Callable[[str], None]


def agg_prefix(key: str) -> str:
    """聚合前缀：``session_db:wal`` → ``session_db``（与 GUI 的勾选行同口径）。"""
    return key.split(":", 1)[0]


def package_dir(tool_name: str, sub: str = "backup") -> str:
    """默认包目录：``<程序启动目录>/<sub>/<工具名>/``，与 GUI 的存放约定一致。

    放在程序同级而不是被备份工具的数据根里，是为了让「程序 + 它的备份包」能一起
    拷走；源码模式落在仓库根，打包模式落在 exe 同级。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, sub, tool_name)


def default_package_path(tool_name: str) -> str:
    """默认包文件名：``<工具>_backup_<时间戳>.zip``（与 GUI 导出的命名一致）。"""
    return os.path.join(
        package_dir(tool_name),
        "%s_backup_%s.zip" % (tool_name, datetime.now().strftime("%Y%m%d_%H%M%S")),
    )


def resolve_root(adapter, explicit: str | None) -> "tuple[str, str]":
    """定出要操作的数据根，返回 ``(root, 错误说明)``；``root`` 为空表示失败。

    显式 ``--root`` 优先；否则用适配器的探测结果。探测不到时必须**明确报错**，
    不能退回「猜一个路径」——猜错的根会把包写到别处、或把还原落进错误目录。
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit)), ""
    detected = adapter.detect_root() or adapter.build_default_root()
    if not detected:
        return "", ("未检测到「%s」的数据目录。请用 --root 显式指定，"
                    "或先在该工具里用过一次（产生数据目录）再重试。"
                    % adapter.display_name)
    return detected, ""


def parse_item_tokens(raw: str | None) -> "list[str]":
    """把 ``--items`` 的字符串切成标识列表（逗号或空格分隔，忽略空项）。

    ``--items`` 是一个**字符串**参数，必须在这里切好再交给 :func:`select_items`：
    直接把字符串当列表用，会按字符迭代而把每个字母都当成一个标识去匹配。
    """
    if not raw:
        return []
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def select_items(items: "list[BackupItem]",
                 wanted: "list[str] | None") -> "tuple[list[BackupItem], list[str]]":
    """挑出本次要备份的条目，返回 ``(条目, 无法识别的 --items 值)``。

    不给 ``--items`` 时取**完整备份口径**＝默认勾选、且排除「其他用户记忆」聚合项
    （与 GUI 的「默认勾选项即完整备份」一致：那些是没法从零重建的内容）。
    给的每项可以是聚合前缀（``session_db``，含主库与 ``-wal`` / ``-shm`` 全部行），
    也可以是完整 key（``session_db:wal``，只要其中一行）。
    """
    if not wanted:
        return [i for i in items
                if i.recommended and agg_prefix(i.key) != "memories_others"], []
    tokens = set(wanted)
    picked = [i for i in items if i.key in tokens or agg_prefix(i.key) in tokens]
    known = {i.key for i in picked} | {agg_prefix(i.key) for i in picked}
    unknown = [w for w in wanted if w not in known]
    return picked, unknown


def _tty():
    """stdout 能否用作「同行覆盖」的终端；不能时返回 ``None``。

    打包成窗口版 exe 时 ``sys.stdout`` 可能是 ``None``；重定向到文件 / 管道时写
    ``\\r`` 只会在日志里留下一堆碎片。两种情况都保持静默，只由 ``emit`` 输出整行。
    """
    out = sys.stdout
    return out if out and out.isatty() else None


def _progress():
    """进度回调：同一行百分比刷新，结束时换行（不刷屏，也不静默）。

    ``emit`` 只写整行，故这里直接操作 stdout：进度本身就是「同一行反复覆盖」的语义，
    交给逐行输出函数会退化成几十行滚动。
    """
    out = _tty()
    state = {"last": -1}

    def cb(info: ProgressInfo) -> None:
        if out is None:
            return
        pct = int(info.percent)
        if pct != state["last"]:
            state["last"] = pct
            out.write("\r[%3d%%] %s" % (pct, (info.message or "")[:40]))
            out.flush()

    return cb


def _finish_progress() -> None:
    out = _tty()
    if out is not None:
        out.write("\n")
        out.flush()


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def cmd_tools(emit: Emit) -> int:
    emit("已支持的工具（标识 = --tool 取值，按注册顺序）：")
    for name in list_adapters():
        adapter = get_adapter(name)
        root = adapter.detect_root() or ""
        mark = "已检测到" if root and os.path.exists(root) else "未检测到"
        emit("  %-14s %-16s %s：%s" % (name, adapter.display_name, mark, root or "—"))
    emit("\n未检测到的工具通常是本机没装或还没用过；可用 --root 显式指定数据目录。")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
def cmd_inspect(args: argparse.Namespace, emit: Emit) -> int:
    try:
        info = inspect_backup(args.package)
    except BackupError as exc:
        emit("无法读取：%s" % exc)
        return EXIT_ERROR
    manifest = info.get("manifest") or {}
    emit("包：%s" % args.package)
    emit("  工具：%s" % (manifest.get("tool") or "（包内未声明）"))
    emit("  类型：%s" % (manifest.get("kind") or "（包内未声明）"))
    emit("  创建时间：%s" % (manifest.get("created_at") or "—"))
    emit("  源数据根：%s" % (manifest.get("source_root") or "—"))
    emit("  文件数：%d" % info["file_count"])
    emit("  原始大小：%s" % human_size(info["total_bytes"]))
    items = manifest.get("items") or []
    if items:
        emit("  备份项：%s" % "、".join(str(i) for i in items))
    origin = manifest_origin_info(manifest)
    if origin:
        emit("  携带源机器信息的条目：%d 处（会话正文 / 工作区记录等）" % len(origin))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def backup_plan_lines(items: "list[BackupItem]", notices: "list[str]") -> "list[str]":
    """把「要备什么」讲清楚的纯文本行（与执行分离，方便单测措辞）。"""
    lines = ["本次备份 %d 项：" % len(items)]
    for it in items:
        lines.append("  · %s（%s%s）" % (
            it.label, agg_prefix(it.key), "" if it.exists else "，本机未找到"))
    if notices:
        lines.append("")
        lines.append("⚠ 以下内容无法自动脱敏，包内会原样保留：")
        lines.extend("  · %s" % n for n in notices)
        lines.append("  确认知悉请加 --yes。")
    return lines


def cmd_backup(args: argparse.Namespace, emit: Emit) -> int:
    try:
        adapter = get_adapter(args.tool)
    except KeyError as exc:
        emit("%s" % exc)
        return EXIT_ERROR

    root, err = resolve_root(adapter, args.root)
    if err:
        emit(err)
        return EXIT_ERROR

    items = adapter.build_items(root, current_uid=args.uid)
    selected, unknown = select_items(items, parse_item_tokens(args.items))
    if unknown:
        emit("无法识别的 --items：%s" % "、".join(unknown))
        emit("可取的标识：%s" % "、".join(sorted({agg_prefix(i.key) for i in items})))
        return EXIT_ERROR
    if not selected:
        emit("没有匹配到任何备份项（检查 --items）。")
        return EXIT_ERROR

    out = args.out or default_package_path(args.tool)
    notices = pre_export_notices(selected)
    for line in backup_plan_lines(selected, notices):
        emit(line)

    # 与 GUI 同一道闸门：含不可脱敏原文时，不确认就不写盘。
    if notices and not args.yes:
        emit("\n未执行备份：上面标注了不可脱敏的原文内容。")
        return EXIT_DECLINED

    emit("\n正在备份到 %s" % out)
    try:
        mf = adapter.export(
            out, root, items=selected, progress=_progress(),
            max_file_mb=(args.max_mb or None),
            compresslevel=args.compress_level,
        )
    except BackupError as exc:
        _finish_progress()
        emit("备份失败：%s" % exc)
        return EXIT_ERROR
    _finish_progress()

    emit("备份完成：%s" % mf["zip_path"])
    emit("  文件数：%d" % mf["file_count"])
    emit("  原始大小：%s → 包内 %s"
         % (human_size(mf["total_bytes"]), human_size(mf["zip_bytes"])))

    sensitive = [i.label for i in selected if getattr(i, "sensitive", False)]
    if sensitive:
        emit("  ⚠ 已对以下项的敏感凭证脱敏（%s）：包内不含明文，还原后需在目标机"
             "手动补填，否则对应模型/服务可见但不可用。" % "、".join(sensitive))
    origin = manifest_origin_info(mf)
    if origin:
        emit("  ⚠ 本包携带 %d 处源机器的路径等信息（会话正文 / 工作区记录自带，"
             "也是还原后把数据归回原工程的依据）；分享或上传前请留意会一并外传。"
             % len(origin))
    for note in companion_notes(selected):
        emit("  提示：%s" % note)
    emit("  还原：python -m ai_env_clone restore --tool %s --package \"%s\""
         % (args.tool, mf["zip_path"]))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def restore_plan_lines(adapter, info: dict, root: str, mode: str,
                       pkg_path: str) -> "list[str]":
    """还原前的「会做什么」文本（含适配器声明的后果说明）。"""
    manifest = info.get("manifest") or {}
    lines = [
        "包：%s" % pkg_path,
        "  来源数据根：%s" % (manifest.get("source_root") or "—"),
        "  文件数：%d，原始大小 %s" % (info["file_count"], human_size(info["total_bytes"])),
        "  还原目标：%s" % root,
        "  还原方式：%s" % ("合并（本机已有的不动，只补包里多出来的）"
                            if mode == RESTORE_MODE_MERGE else
                            "全覆盖（同名文件按包内版本写入）"),
        "",
        adapter.restore_overwrite_notice(mode),
    ]
    origin = manifest_origin_info(manifest)
    if origin:
        lines.append("")
        lines.append("本包含 %d 处源机器的路径等信息，还原时会写回本机对应位置。"
                     % len(origin))
    lines.append("")
    lines.append("确认无误请加 --yes；确认前不会写任何文件。")
    return lines


def cmd_restore(args: argparse.Namespace, emit: Emit) -> int:
    try:
        adapter = get_adapter(args.tool)
    except KeyError as exc:
        emit("%s" % exc)
        return EXIT_ERROR

    root, err = resolve_root(adapter, args.root)
    if err:
        emit(err)
        return EXIT_ERROR

    try:
        info = inspect_backup(args.package, match_structure=adapter.match_structure)
    except BackupError as exc:
        emit("无法读取：%s" % exc)
        return EXIT_ERROR

    # 第一道闸门：包必须属于本工具。跨工具的包会「看着写成功了、其实读不到」——
    # 路径改写 / 索引登记 / 结构校验全都按错误的工具规则执行（与 GUI 同一口径）。
    manifest = info.get("manifest") or {}
    pkg_tool, _src = backup_scan.identify_tool(
        info.get("entries") or [], manifest, prefer=adapter.name)
    if not pkg_tool:
        emit("不可还原：该包缺少工具声明，结构也与任何已支持工具都不匹配。")
        return EXIT_ERROR
    if pkg_tool != adapter.name:
        emit("不可还原：该包属于「%s」，而 --tool 指定的是「%s」。"
             % (backup_scan.display_name(pkg_tool), adapter.display_name))
        emit("跨工具取会话请用 GUI 的「导入会话…」→「从备份包导入」；"
             "整库还原请把 --tool 换成「%s」再执行。" % pkg_tool)
        return EXIT_ERROR

    mode = args.mode or (RESTORE_MODE_MERGE if adapter.restore_merge_supported
                         else RESTORE_MODE_REPLACE)
    if mode == RESTORE_MODE_MERGE and not adapter.restore_merge_supported:
        emit("「%s」不支持合并还原（其会话库为整库覆盖型），请用 --mode replace。"
             % adapter.display_name)
        return EXIT_ERROR

    for line in restore_plan_lines(adapter, info, root, mode, args.package):
        emit(line)
    if not args.yes:
        emit("\n未执行还原。")
        return EXIT_DECLINED

    rollback_dir = args.rollback_dir or package_dir(args.tool)
    os.makedirs(rollback_dir, exist_ok=True)
    emit("\n正在还原…")
    try:
        r = adapter.restore(
            args.package, root,
            progress=_progress(),
            make_rollback=not args.no_rollback,
            rollback_dir=rollback_dir,
            expected_kind="backup",
            strict=args.strict,
            match_structure=adapter.match_structure,
            mode=mode,
        )
    except BackupError as exc:
        _finish_progress()
        emit("还原失败：%s" % exc)
        return EXIT_ERROR
    _finish_progress()

    emit("已还原 %d 个文件到 %s" % (r["restored"], root))
    if r.get("skipped"):
        emit("  保留本机已有 %d 项（未改动）" % r["skipped"])
    if r.get("blocked"):
        emit("  被跳过 %d 项" % len(r["blocked"]))
    if r["rollback"]:
        emit("  回滚快照：%s" % r["rollback"])
    emit("  %s" % adapter.restore_result_note(mode))
    emit("  请重启「%s」以加载还原的会话与记忆；若会话列表为空，"
         "请确认本机工程路径与源机器一致。" % adapter.display_name)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数表 / 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ai_env_clone",
        description="AI 工具记忆与会话历史 备份 / 还原（命令行入口）",
    )
    sub = p.add_subparsers(dest="command", metavar="{tools,inspect,backup,restore}")

    sub.add_parser("tools", help="列出已支持的工具及其数据目录探测结果")

    pi = sub.add_parser("inspect", help="查看某个备份包的内容（只读）")
    pi.add_argument("--package", required=True, help="备份包 zip 路径")

    pb = sub.add_parser("backup", help="导出备份包")
    pb.add_argument("--tool", required=True, help="工具标识，见 `tools` 子命令")
    pb.add_argument("--root", help="数据目录；缺省为本工具探测结果")
    pb.add_argument("--uid", help="登录用户标识（记忆按用户分区时传入）")
    pb.add_argument("--items", help="逗号分隔的备份项标识（如 session_db,projects）；"
                                    "缺省为完整备份口径＝全部默认勾选项")
    pb.add_argument("--out", help="输出 zip 路径；缺省为 <程序目录>/backup/<工具>/")
    pb.add_argument("--max-mb", type=float, default=200.0,
                    help="单个文件体积上限（MB），超限跳过；0 = 不限制")
    pb.add_argument("--compress-level", type=int, default=6,
                    help="zip 压缩级别 0-9（默认 6）")
    pb.add_argument("--yes", action="store_true",
                    help="确认知悉「不可脱敏的原文内容」，允许继续备份")

    pr = sub.add_parser("restore", help="从备份包还原到本机的工具数据目录")
    pr.add_argument("--tool", required=True, help="工具标识，见 `tools` 子命令")
    pr.add_argument("--package", required=True, help="备份包 zip 路径")
    pr.add_argument("--root", help="还原目标；缺省为本工具探测结果")
    pr.add_argument("--mode", choices=[RESTORE_MODE_MERGE, RESTORE_MODE_REPLACE],
                    help="还原方式；缺省为「合并」（不支持合并的工具自动用全覆盖）")
    pr.add_argument("--rollback-dir", help="回滚快照存放目录（默认与备份包同目录）")
    pr.add_argument("--no-rollback", action="store_true",
                    help="不生成回滚快照（覆盖前不备份旧文件）")
    pr.add_argument("--strict", action="store_true",
                    help="即使包内有清单也强制做结构指纹校验")
    pr.add_argument("--yes", action="store_true",
                    help="确认上述影响，允许写盘")
    return p


def run(argv: "list[str]", emit: Emit | None = None) -> int:
    """执行子命令，返回退出码。``emit`` 便于单测捕获输出行。

    ``print`` 在**调用时**才取（不写成默认参数值）：默认参数在导入时就绑定了真实的
    ``print``，测试里 ``mock.patch("builtins.print")`` 会拦不住输出。
    """
    if emit is None:
        emit = print
    args = build_parser().parse_args(argv)
    if args.command == "tools":
        return cmd_tools(emit)
    if args.command == "inspect":
        return cmd_inspect(args, emit)
    if args.command == "backup":
        return cmd_backup(args, emit)
    if args.command == "restore":
        return cmd_restore(args, emit)
    emit("未知子命令：%s" % (args.command or "（缺省）"))
    return EXIT_ERROR


def handle_backup_restore_flags(argv: "list[str]") -> "int | None":
    """命中 CLI 子命令则执行并返回退出码，否则返回 ``None``（让界面照常启动）。

    在 ``import tkinter`` **之前**调用（见 ``__main__`` 里的顺序约定）：命令行分支
    不该依赖 GUI 库。``argparse`` 在报错时会自行打印用法并以 SystemExit(2) 结束，
    这里把它的退出码收进本模块的口径，避免直接抛出。

    打包版是 ``--windowed``（无控制台） exe，``sys.stdout`` 为 ``None``：这时所有输出
    与提示都无处可去，而备份 / 还原恰恰要人看清结论才知道下一步。**不做静默执行**——
    直接以失败退出，请改用源码模式跑命令行。
    """
    if not argv or argv[0] not in SUBCOMMANDS:
        return None
    if sys.stdout is None:
        return EXIT_ERROR
    try:
        return run(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ERROR
    except KeyboardInterrupt:
        print("已中断。")
        return EXIT_ERROR
