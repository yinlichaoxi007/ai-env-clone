"""
备份包目录的**递归扫描**与「所属工具」识别（不依赖 tkinter，便于单测）。

为什么单独成模块
----------------
备份浏览器（还原侧）与导入页签（导入侧）都要回答同一个问题：
「这个目录下有哪些备份包、分别属于哪个工具」。两处的**扫描规则必须完全一致**
（递归深度、上限、剪枝、取消），差别只在**过滤策略**与结论文案
⇒ 扫描与识别下沉到本模块，界面只消费结果。

三层识别，逐层降级（成本决定时机）
----------------------------------

- **L1 文件名**：``<工具名>_backup_*.zip`` / ``<工具名>_rollback_*.zip``，
  或所在父目录名就是工具名。**0 成本**（不打开 zip），列表阶段先铺一版；
- **L2 清单**：打开 zip 只读 ``ai_env_clone_manifest.json``，约 **10 ms/包**
  ⇒ 只对 L1 未命中的候选做，且限流（见 ``manifest_limit``），结果按
  ``(path, mtime, size)`` 缓存，重扫不重读；
- **L3 结构指纹**：逐个适配器试 ``match_structure(namelist)``，更贵
  ⇒ **只在用户选中某一行时**才算，不进列表阶段。

递归的代价（本机实测，见 ``docs/local/从备份包导入方案.md`` §4.3）
-----------------------------------------------------------------

- 一层扫描恒定极快（≤ 5 ms，与目录规模无关）⇒ 首屏永远秒出；
- ``~`` / ``%TEMP%`` / ``C:\\`` 全量递归 3 秒都扫不完 ⇒ 必须有硬上限；
- 备份包几乎总在 2~3 层以内，且**逐层扫描（BFS）远优于深度优先**：
  浅层的包会先出现，不会一头扎进深处。默认深度 3。
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ai_env_clone.core import (
    KIND_BACKUP,
    KIND_ROLLBACK,
    MANIFEST_NAME,
    list_backup_dir,
)

__all__ = [
    "BackupEntry",
    "ScanProgress",
    "ScanResult",
    "MAX_DEPTH",
    "MAX_DIRS",
    "MAX_HITS",
    "BUDGET_S",
    "MANIFEST_LIMIT",
    "PRUNE_DIRS",
    "tool_names",
    "display_name",
    "read_manifest",
    "tool_from_filename",
    "tool_from_parent_dir",
    "identify_tool",
    "is_identifiable",
    "is_importable_for",
    "scan",
]

#: 默认递归深度（层）。备份目录在第 2 层，第 4 层噪声骤增 ⇒ 3 层足够。
MAX_DEPTH = 3
#: 单次扫描最多访问的目录数（触顶即停并标记截断）。
MAX_DIRS = 20000
#: 单次扫描最多收集的 zip 数（触顶即停并标记截断）。
MAX_HITS = 500
#: 单次扫描的时间预算（秒）。家目录 / 盘根这类目录必须靠它兜底。
BUDGET_S = 3.0
#: L2 读清单的候选上限（每包约 10 ms，限流避免后台线程长时间占用磁盘）。
MANIFEST_LIMIT = 50

#: 扫描时跳过的目录名（小写；安全且收益大）。**不跳过** ``AppData`` 这类目录
#: ——用户可能真把备份放在那里，靠深度与预算兜底。
PRUNE_DIRS: frozenset = frozenset({
    "$recycle.bin",
    "system volume information",
    "node_modules",
    ".git",
    "__pycache__",
})

#: 截断原因 -> 给用户看的说明（界面在进度行后追加）。
REASON_LABELS: dict[str, str] = {
    "dirs": "已达目录数上限",
    "hits": "已达命中数上限",
    "budget": "已达时间上限",
    "cancelled": "已取消",
}


# --------------------------------------------------------------------------- #
# 适配器访问（惰性 + 缓存）
# --------------------------------------------------------------------------- #
_ADAPTER_CACHE: dict = {}


def _adapters() -> list:
    """按注册顺序返回全部适配器**实例**（惰性创建并缓存）。"""
    from ai_env_clone.adapters import get_adapter, list_adapters

    names = list_adapters()
    out = []
    for name in names:
        inst = _ADAPTER_CACHE.get(name)
        if inst is None:
            try:
                inst = get_adapter(name)
            except KeyError:  # pragma: no cover - 注册表与实例缓存不一致时才可能
                continue
            _ADAPTER_CACHE[name] = inst
        out.append(inst)
    return out


def tool_names() -> list:
    """全部已注册工具的标识（注册顺序）。"""
    from ai_env_clone.adapters import list_adapters

    return list_adapters()


def display_name(tool: str) -> str:
    """工具标识 -> 展示名；未知标识原样返回（不做猜测）。"""
    if not tool:
        return ""
    try:
        from ai_env_clone.adapters import get_adapter

        return get_adapter(tool).display_name
    except KeyError:
        return tool


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class BackupEntry:
    """列表里的一个候选备份包（``*.zip``）。"""

    path: str
    name: str
    size: int
    mtime: float
    kind: str = "unknown"      # backup / rollback / unknown（文件名判定）
    tool: str = ""             # 适配器 name；"" = 未识别出所属工具
    tool_source: str = ""      # filename / dirname / manifest / structure / ""
    manifest: Optional[dict] = None   # 仅 L2 读过的候选才有

    @property
    def identifiable(self) -> bool:
        """能否被认定为「某个工具的备份/快照」。

        判据：文件名/父目录已给出类型或所属工具，或 L2 读清单认出了工具。
        **不要求** ``zipfile.is_zipfile`` 通过——一个按命名约定命名的包即使已损坏，
        也应留在列表里（用户点「校验完整性」才能看到它坏了）；把它藏起来反而
        让人以为「备份丢了」。
        """
        return self.kind in (KIND_BACKUP, KIND_ROLLBACK) or bool(self.tool)


def is_identifiable(entry: BackupEntry) -> bool:
    """``BackupEntry.identifiable`` 的函数式写法（便于传入判定回调）。"""
    return entry.identifiable


@dataclass(frozen=True)
class ScanProgress:
    """扫描进度快照（``on_layer`` 回调携带，界面据此刷新进度行）。"""

    dirs_scanned: int = 0
    layers: int = 0
    hits: int = 0
    truncated: bool = False
    reason: str = ""

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


@dataclass
class ScanResult:
    """一次扫描的最终结果。"""

    rows: list = field(default_factory=list)   # list[BackupEntry]，按 mtime 倒序
    dirs_scanned: int = 0
    layers: int = 0
    truncated: bool = False
    reason: str = ""                           # "" / depth / dirs / hits / budget / cancelled

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)

    def progress(self) -> ScanProgress:
        return ScanProgress(
            dirs_scanned=self.dirs_scanned, layers=self.layers,
            hits=len(self.rows), truncated=self.truncated, reason=self.reason,
        )


# --------------------------------------------------------------------------- #
# L1 / L2：识别
# --------------------------------------------------------------------------- #
def tool_from_filename(name: str, tools: Optional[Sequence[str]] = None) -> str:
    """按文件名约定解析所属工具（``<tool>_backup_`` / ``<tool>_rollback_``）。

    旧版命名（``backup_<时间戳>.zip`` / ``aienv_rollback_<时间戳>.zip``）不含
    工具名，必然返回 ``""`` —— 那类包交给 L2 清单识别。
    """
    base = os.path.basename(name or "").lower()
    for tool in (tools if tools is not None else tool_names()):
        t = tool.lower()
        if base.startswith(t + "_backup_") or base.startswith(t + "_rollback_"):
            return tool
    return ""


def tool_from_parent_dir(path: str, tools: Optional[Sequence[str]] = None) -> str:
    """父目录名恰好是工具标识时返回它（如 ``backup/qoder/xxx.zip``）。

    注意这是**兜底**判据，优先级低于文件名：用户手工把一个异工具包丢进
    ``backup/qoder/`` 时，只要它的文件名合规就会先被文件名命中；两者都不合规
    才会误判，而真正写盘前的 ``restore_from`` 会以包内清单为准再次拦截。
    """
    parent = os.path.basename(os.path.dirname(os.path.abspath(path))).lower()
    if not parent:
        return ""
    for tool in (tools if tools is not None else tool_names()):
        if parent == tool.lower():
            return tool
    return ""


def read_manifest(zip_path: str) -> Optional[dict]:
    """只读包内 ``ai_env_clone_manifest.json``；任何异常一律返回 ``None``。

    坏包 / 非 zip / 无清单 / 清单不是合法 JSON 都在预期内，全部收敛为 ``None``，
    调用方据此认为「没读到声明」，不区分失败原因（识别只需二元结论）。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                raw = zf.read(MANIFEST_NAME)
            except KeyError:
                return None
        data = json.loads(raw.decode("utf-8"))
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def identify_tool(
    entries: Sequence[str],
    manifest: Optional[dict] = None,
    prefer: str = "",
) -> tuple:
    """识别归档条目属于哪个工具，返回 ``(tool, source)``。

    判定顺序：

    1. 清单的 ``tool`` 字段（最权威，且免费）—— 值必须落在已注册工具里，
       否则视为「未声明」（旧包 / 伪造的字段不做推断）；
    2. 逐个适配器试结构指纹 ``match_structure(entries)``。``prefer`` 指定的工具
       **排在最前**：这样「无声明但本来是当前工具的老包」不会被别的适配器的
       宽松指纹抢先命中（收窄还原时这条兼容性必须保住）。

    :return: ``(tool, source)``；识别不出返回 ``("", "")``。
        ``source`` ∈ ``{"manifest", "structure"}``。
    """
    names = tool_names()
    if isinstance(manifest, dict):
        declared = str(manifest.get("tool") or "").strip()
        if declared and declared in names:
            return declared, "manifest"

    order = list(names)
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    for name in order:
        inst = _ADAPTER_CACHE.get(name)
        if inst is None:
            try:
                from ai_env_clone.adapters import get_adapter

                inst = get_adapter(name)
            except KeyError:  # pragma: no cover
                continue
            _ADAPTER_CACHE[name] = inst
        try:
            matched, _missing = inst.match_structure(entries)
        except Exception:  # noqa: BLE001 - 指纹实现不应影响识别整体
            continue
        if matched:
            return name, "structure"
    return "", ""


def is_importable_for(entry: BackupEntry, target: str) -> bool:
    """该包能否作为 ``target`` 工具的**导入来源**（不是还原）。

    条件三者同时成立：认出了所属工具、不是本工具（本工具自己的包该走还原）、
    且「该工具 → target」在 ``import_matrix`` 里被登记为 ``SUPPORTED``。
    加密工具（qoder / trae-cn / trae-solo-cn）永远不可导入。
    """
    if not entry.tool or entry.tool == target:
        return False
    from ai_env_clone import import_matrix

    src = import_matrix.ALL_SOURCES.get(entry.tool)
    if src is None or src.status != import_matrix.SUPPORTED:
        return False
    cap = import_matrix.matrix_for(target)
    if cap is None:
        return False
    return any(s.tool == entry.tool and s.status == import_matrix.SUPPORTED
               for s in cap.sources)


# --------------------------------------------------------------------------- #
# 目录遍历
# --------------------------------------------------------------------------- #
def _subdirs(path: str) -> list:
    """列出直接子目录（**不跟进符号链接 / 目录联接**，跳过剪枝名单）。

    Windows 上 ``C:\\Users\\All Users`` 这类联接会造成绕圈，故必须用
    ``follow_symlinks=False`` 并额外用 ``is_junction()`` 排除（Python 3.12+）。
    """
    out: list = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.lower() in PRUNE_DIRS:
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                is_junction = getattr(e, "is_junction", None)
                if callable(is_junction):
                    try:
                        if is_junction():
                            continue
                    except OSError:
                        pass
                out.append(e.path)
    except OSError:
        return []
    return out


def _cancelled(cancel) -> bool:
    """``cancel`` 可以是可调用对象或 ``threading.Event``（都支持）。"""
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        try:
            return bool(is_set())
        except Exception:  # noqa: BLE001
            return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001
        return False


def _entries_in(directory: str) -> list:
    """把某目录下的 zip 变成候选行（纯元数据，不打开 zip）。"""
    rows: list = []
    tools = tool_names()
    for r in list_backup_dir(directory):
        tool = tool_from_filename(r["name"], tools) or tool_from_parent_dir(r["path"], tools)
        src = "filename" if tool and r["name"].lower().startswith(tool.lower() + "_") else (
            "dirname" if tool else ""
        )
        rows.append(BackupEntry(
            path=r["path"], name=r["name"], size=r["size"], mtime=r["mtime"],
            kind=r["kind"], tool=tool, tool_source=src,
        ))
    return rows


def scan(
    directory: str,
    cancel=None,
    on_layer: Optional[Callable[[list, ScanProgress], None]] = None,
    *,
    max_depth: int = MAX_DEPTH,
    max_dirs: int = MAX_DIRS,
    max_hits: int = MAX_HITS,
    budget_s: float = BUDGET_S,
    manifest_limit: int = MANIFEST_LIMIT,
    cache: Optional[dict] = None,
) -> ScanResult:
    """逐层（BFS）扫描 ``directory`` 下的备份包。

    :param cancel: 取消令牌，``threading.Event`` 或返回布尔的回调；置位后
        在「一个目录」的粒度上尽快退出。
    :param on_layer: 每层扫完回调一次 ``(rows, progress)``；``rows`` 是**累计**
        快照（调用方可直接整体替换列表），``progress`` 是当时的进度。
        **只在调用方线程执行**（GUI 里即后台线程），回调内不得触碰 Tk。
    :param manifest_limit: L2 读清单的候选上限（0 = 完全不读）。
    :param cache: L2 结果缓存（``{(path, mtime, size): (kind, tool, source)}``），
        跨次重扫复用，避免反复打开同一批包。
    :return: :class:`ScanResult`。**触顶一律返回而非抛错**，只置 ``truncated``
        与 ``reason``，界面据此如实告知用户「已停止扫描」。
    """
    result = ScanResult()
    cache = cache if cache is not None else {}
    started = time.monotonic()
    frontier = [directory]
    layer = 0
    rows_by_path: dict = {}
    manifests_used = 0

    def tripped() -> str:
        if _cancelled(cancel):
            return "cancelled"
        if result.dirs_scanned >= max_dirs:
            return "dirs"
        if len(rows_by_path) >= max_hits:
            return "hits"
        if time.monotonic() - started > budget_s:
            return "budget"
        return ""

    def emit() -> None:
        if on_layer is not None:
            result.rows.sort(key=lambda r: r.mtime, reverse=True)
            on_layer(list(result.rows), result.progress())

    while frontier:
        new_rows: list = []
        next_frontier: list = []
        for cur in frontier:
            # 第 0 层不设预算检查：单层扫描恒定 ≤5 ms（实测），且是首屏的一部分。
            if layer > 0:
                reason = tripped()
                if reason:
                    result.truncated = True
                    result.reason = reason
                    break
            result.dirs_scanned += 1
            for e in _entries_in(cur):
                if e.path in rows_by_path:
                    continue
                rows_by_path[e.path] = e
                new_rows.append(e)
            if layer < max_depth:
                next_frontier.extend(_subdirs(cur))
        result.layers = layer + 1
        result.rows.extend(new_rows)
        emit()
        if not result.truncated and manifests_used < manifest_limit:
            # L2：只对 L1 未命中的候选读清单（**全扫描合计**限流 + 缓存），读完回传一次
            manifests_used += _enrich_manifests(
                result, manifest_limit - manifests_used, cache
            )
            emit()
        if result.truncated:
            break
        if not next_frontier or layer >= max_depth:
            break
        frontier = next_frontier
        layer += 1

    result.rows.sort(key=lambda r: r.mtime, reverse=True)
    return result


def _enrich_manifests(result: ScanResult, limit: int, cache: dict) -> int:
    """对 L1 未命中的候选读清单（限流 + 缓存），就地补充 ``kind`` / ``tool``。

    :return: 本次**实际打开 zip** 的个数（缓存命中不计），调用方据此累计限流。
    """
    if limit <= 0:
        return 0
    used = 0
    for e in result.rows:
        if e.tool and e.kind in (KIND_BACKUP, KIND_ROLLBACK):
            continue
        if used >= limit:
            break
        key = (e.path, int(e.mtime), e.size)
        hit = cache.get(key)
        if hit is None:
            used += 1
            mf = read_manifest(e.path)
            if mf is None:
                hit = (e.kind, "", "")
            else:
                tool = str(mf.get("tool") or "").strip()
                tool = tool if tool in tool_names() else ""
                kind = str(mf.get("kind") or "").strip() or e.kind
                if kind not in (KIND_BACKUP, KIND_ROLLBACK):
                    kind = e.kind
                hit = (kind, tool, "manifest" if tool else "")
            cache[key] = hit
        kind, tool, src = hit
        if kind in (KIND_BACKUP, KIND_ROLLBACK):
            e.kind = kind
        if tool:
            # 清单声明最权威：覆盖「按文件名/父目录猜出来的」工具归属。
            e.tool = tool
            e.tool_source = src or "manifest"
    return used
