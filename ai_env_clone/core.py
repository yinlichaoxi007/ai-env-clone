"""
ai-env-clone 核心逻辑层（与具体 AI 工具无关）

提供通用的备份/恢复能力：目录扫描、过滤、zip 打包、一致性快照、
完整性校验与还原。所有"某工具特有"的知识（目录布局、条目清单）
都剥离到 ``adapters`` 包，核心层只认通用的 ``BackupItem``。

设计要点
--------
1. 归档内路径统一以工具根目录为基准，保证「备份什么路径，就恢复到什么路径」。
2. 内置噪音过滤 + 关键文件豁免（SQLite 主库及其 -wal/-shm 不受体积上限约束）。
3. 恢复前自动生成回滚快照，支持一键还原。
4. SQLite 数据库使用在线备份 API 一致性快照，即使工具运行中也可安全备份。
5. 内置 Zip Slip 路径穿越防护。
6. 全流程回调式进度上报，便于 GUI 展示且不阻塞主线程。
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Mapping, Sequence

from . import merge_plan

#: manifest 中记录类型的字段值（也用于 ``export_backup`` 的 kind 默认值）
KIND_BACKUP = "backup"
KIND_ROLLBACK = "rollback"

__all__ = [
    "BackupItem",
    "ScanResult",
    "ProgressInfo",
    "BackupError",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "DEFAULT_EXCLUDES",
    "ALWAYS_INCLUDE",
    "is_excluded",
    "is_critical",
    "scan_items",
    "snapshot_sqlite",
    "export_backup",
    "inspect_backup",
    "import_backup",
    "safe_target",
    "list_backup_dir",
    "classify_zip_name",
    "BACKUP_PREFIX",
    "ROLLBACK_PREFIX",
    "BACKUP_MARK",
    "ROLLBACK_MARK",
    "KIND_BACKUP",
    "KIND_ROLLBACK",
]

MANIFEST_NAME = "ai_env_clone_manifest.json"
MANIFEST_VERSION = 2

#: 默认排除规则（大小写不敏感，按「路径片段」或「glob」匹配）
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "*- 副本*",          # 资源管理器复制产生的冗余副本
    "*- Copy*",
    "*.7z",              # 已归档压缩包，无需二次压缩
    "*.zip",
    "diagnosis*.bin",    # 诊断转储，可再生
    "*.log",
    "*.tmp",
    "*/tmp/*",
    "*/logs/*",
    "*.db-wal",
    "*.db-shm",
    # 记忆类文件的「旧快照」。云端托管的那份（如 ``memory/<用户 id>_memory.md``）
    # 由服务端持有权威副本、并在本地留一份 ``.bak``；**该快照可能仍含服务端后续
    # 已脱掉的明文口令**（本地快照不会随服务端更新而回溯清洗），凭证不入包 ⇒ 排除。
    # 只针对记忆/规则这类「服务端或本机另有权威副本」的 markdown 快照，不动其它 ``.bak``。
    "*/memory/*.md.bak",
    "MEMORY.md.bak",
)

#: 无论体积多大都必须备份的关键文件（否则会话历史会丢失）
#: 包含 SQLite 主库及其配套文件（.db 主库、.db-wal 预写日志、.db-shm 共享内存），
#: 三者必须作为一个整体一起备份，缺一不可。
ALWAYS_INCLUDE: tuple[str, ...] = (
    "*/cache/db/*.db",
    "*/cache/db/*.sqlite",
    "*/cache/db/*.db-wal",
    "*/cache/db/*.db-shm",
    # WorkBuddy 的主索引库不在 */cache/db/ 下 —— 它是数据根直属的
    # ``~/.workbuddy/workbuddy.db``，而 ``-wal`` / ``-shm`` 会被 DEFAULT_EXCLUDES 的
    # ``*.db-wal`` / ``*.db-shm`` 命中。若不在此豁免，「db + -wal + -shm 三件套
    # 同批入包」这条不变量对它就不成立：主库受单文件体积上限约束（超限即静默跳过、
    # 会话列表全丢）、配套文件被静默过滤。
    # ⚠️ 只写**精确的三条路径**，不要写成 ``*/.workbuddy/*.db-wal`` 之类的通配——
    #    那会连带命中 ``.workbuddy/workspace/sessions/*/modify_backup/`` 下 WorkBuddy
    #    自己保存的同名运行态副本（体积大且无迁移价值），实测会平白多打进 90+ 个文件。
    ".workbuddy/workbuddy.db",
    ".workbuddy/workbuddy.db-wal",
    ".workbuddy/workbuddy.db-shm",
    "*/.workbuddy/workbuddy.db",
    "*/.workbuddy/workbuddy.db-wal",
    "*/.workbuddy/workbuddy.db-shm",
)


class BackupError(Exception):
    """备份 / 恢复过程中的可预期错误。"""


#: 会话类数据「携带源设备信息」的通用说明（各适配器共用同一措辞，避免各写各的）。
#:
#: 背景（2026-09-27 本机实测）：会话/对话正文里**大量**出现当时的文件绝对路径——
#: 工具调用参数、命令输出、文件快照都会把它记下来。CodeBuddy 侧实测
#: ``history/**`` 24519 个 JSON 里 20564 个命中、``check-point/**`` 186/218 命中；
#: Qoder ``project_sessions`` 51/60、WorkBuddy ``workspace_sessions`` 13/60 亦命中。
#: 这些路径**跨机还原后正是用来把数据归回原本工程工作区的线索**（有用），
#: 但同时意味着备份包会把源机器的目录结构一并带走（需告知用户）。
ORIGIN_NOTE_CONVERSATION = "会话正文与命令输出会记录源机器上的文件绝对路径"

#: 会话数据库（SQLite）内的同类说明。
ORIGIN_NOTE_SESSION_DB = "会话数据库内会记录源机器上的文件绝对路径"


@dataclass
class BackupItem:
    """一个可勾选的备份单元。适配器负责构造这些条目。"""

    key: str
    label: str
    path: str            # 绝对路径
    description: str
    recommended: bool = True
    uid: str | None = None  # 记忆区按 UID 拆分时的用户标识，非记忆项为 None
    sensitive: bool = False  # 含 apiKey 等敏感凭证，备份时需脱敏、恢复需用户手动补填
    #: 非空 = 该条目会**携带源机器上的路径 / 标识等信息**，值是给用户看的说明。
    #: 导出时登记进清单的 ``origin_info``，界面在勾选处、备份完成提示与备份包详情里
    #: 都会明示——目的是让用户在**分享备份包之前**就知道包里有源设备信息，而非事后才知。
    #: 注意：这里的说明只陈述「包里有」，不含评价；是否分享由用户决定。
    carries_origin: str = ""
    #: 非空 = ``(配套条目 key, 说明)``：**勾选了本条目、但未勾选该配套条目**时，
    #: 备份完成提示里追加这条说明（见 :func:`companion_notes`）。
    #: 用途是「单独成文件、缺了它数据就不完整」的成对内容。典型（DSH 实测）：
    #: 实时配置 ``profiles/<profile>/cordis.patch.yml`` 只保存 provider 的密钥**引用**
    #: （``apiKeyEnv``），真密钥在 ``.credentials.yaml``；后者含明文、默认不勾
    #: ⇒ 必须让用户知道要单独备份它。
    companion: "tuple[str, str] | None" = None

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    @property
    def is_dir(self) -> bool:
        return os.path.isdir(self.path)


def origin_info_entries(items: Sequence[BackupItem]) -> list[dict]:
    """从条目清单里挑出「携带源设备信息」的条目，供写入清单 / 界面展示。

    只认**确实存在**的条目：不存在的内容不会进包，报它会误导（清单里 ``items``
    字段同样只记存在的条目），故调用方应传入已筛过的列表。

    :return: ``[{"key","label","note"}, ...]``，按 key 排序、按 (key, note) 去重。
        同一逻辑项在 GUI 里是多行（如 ``ide_ws_records:<hash>`` 一条工作区一个文件），
        这里按 key 保留各自条目，展示侧再按 (label, note) 合并成一行。
    """
    rows: list[dict] = []
    seen: set = set()
    for it in items:
        note = (getattr(it, "carries_origin", "") or "").strip()
        if not note:
            continue
        sig = (it.key, note)
        if sig in seen:
            continue
        seen.add(sig)
        rows.append({"key": it.key, "label": it.label, "note": note})
    rows.sort(key=lambda r: r["key"])
    return rows


def companion_notes(items: Sequence[BackupItem]) -> list[str]:
    """挑出「已勾选、但它的配套条目没勾选」的提醒文案（去重、保序）。

    用于「拆成两个文件、缺一个数据就不完整」的成对内容。典型是 DSH：勾了实时配置
    ``profiles/<profile>/cordis.patch.yml``（只存 provider 的密钥**引用**）却没勾
    ``.credentials.yaml``（存真密钥）⇒ 备份包里取不到密钥，还原后模型不可用，
    必须当场告诉用户。

    :param items: **本次实际勾选**的条目（不是全量清单）。配套条目是否被勾选，
        就以这份列表里有没有它的 ``key`` 判断。
    :return: 需要追加到备份完成提示里的说明列表；无则空列表。
    """
    selected = {it.key for it in items}
    out: list[str] = []
    for it in items:
        comp = getattr(it, "companion", None)
        if not comp:
            continue
        comp_key, note = comp
        if note and comp_key not in selected and note not in out:
            out.append(note)
    return out


def manifest_origin_info(manifest: dict | None) -> list[dict]:
    """读取清单里的 ``origin_info``；旧备份包没有该字段时返回空列表。

    只做形状收敛（过滤掉空说明 / 非字典项），**不补默认值**——「没有登记」与
    「登记为空」在这里含义相同：本包没有已知的源设备信息。
    """
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get("origin_info")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        note = str(row.get("note") or "").strip()
        if not note:
            continue
        out.append({
            "key": str(row.get("key") or ""),
            "label": str(row.get("label") or ""),
            "note": note,
        })
    return out


def origin_info_lines(manifest: dict | None, indent: str = "  ") -> list[str]:
    """把「携带的源设备信息」渲染成多行文案（供备份包详情区展示）。

    同一 (label, note) 合并成一行（GUI 里一条逻辑项可能是多个文件条目），
    末尾附一句「分享前留意」，因为这类信息的风险点不在本机、而在**把包发出去**。
    """
    rows = manifest_origin_info(manifest)
    if not rows:
        return []
    merged: list[tuple[str, str]] = []
    for row in rows:
        sig = (row["label"] or row["key"], row["note"])
        if sig not in merged:
            merged.append(sig)
    lines = ["", "【携带的源设备信息】备份包会记下源机器上的路径 / 标识（这是数据能还原回"
                 "原位、会话能被归回原工作区的原因），分享、上传或发送该包前请留意："]
    for label, note in merged:
        lines.append("%s· %s：%s" % (indent, label, note))
    lines.append("%s备份包内的目录结构还会带上源机器的用户名与登录用户标识（如 %s）；"
                 "跨机还原时工具会按当前机器重映射。" % (indent, r"…\Users\<用户名>"))
    return lines


def session_workspaces_lines(manifest: dict | None, indent: str = "  ") -> list[str]:
    """把「会话 -> 原始工作区路径」的备份记录渲染成多行文案（详情区展示用）。

    这是**本工具生成**的附加文件（由适配器的 ``export_generated`` 产出，随包携带、
    还原后落回会话根），记录每条会话原本所属的工程工作区路径。旧备份包没有这段，
    返回空列表 —— 不显示、也不推断「它是不是坏了」。
    """
    if not isinstance(manifest, dict):
        return []
    extra = manifest.get("extra")
    if not isinstance(extra, dict):
        return []
    sw = extra.get("session_workspaces")
    if not isinstance(sw, dict):
        return []
    try:
        total = int(sw.get("total") or 0)
        resolved = int(sw.get("resolved") or 0)
        unresolved = int(sw.get("unresolved") or 0)
    except (TypeError, ValueError):
        return []
    if total <= 0:
        return []
    return [
        "",
        "【会话原始工作区】包内另存有一份「会话 -> 原始工作区路径」映射（%s）："
        % (sw.get("file") or "session-workspaces.json"),
        "%s· 共 %d 个工作区：%d 个可还原为具体路径，%d 个只记录了不可逆 id"
        % (indent, total, resolved, unresolved),
        "%s· 还原后导入会话时据此把会话放回原工程工作区；推不出路径的会在导入前"
        "明确提示，不会静默换落点。" % indent,
        "%s· ⚠ 该文件记录了源机器的工程路径，分享或上传备份包前请留意。" % indent,
    ]


# --------------------------------------------------------------------------- #
# 过滤 / 扫描
# --------------------------------------------------------------------------- #
def _norm_for_match(rel: str) -> str:
    return rel.replace(os.sep, "/").lower()


def is_excluded(rel_path: str, patterns: Iterable[str]) -> bool:
    """判断归档内相对路径是否命中排除规则。"""
    p = _norm_for_match(rel_path)
    name = p.rsplit("/", 1)[-1]
    for pat in patterns:
        pl = pat.replace(os.sep, "/").lower()
        if fnmatch.fnmatch(p, pl) or fnmatch.fnmatch(name, pl):
            return True
        if fnmatch.fnmatch("/" + p, pl):
            return True
    return False


@dataclass
class ScanResult:
    """扫描统计结果。"""

    files: list[tuple[str, str]] = field(default_factory=list)  # (绝对路径, 归档相对路径)
    total_bytes: int = 0
    # 因排除规则（日志/临时/WAL 等）被忽略的文件——属备份项本身的必要过滤，用户无感，
    # 不计入"跳过"提示。
    skipped_count: int = 0
    skipped_bytes: int = 0
    # 因"超大文件过滤"被跳过的文件（仅当用户开启了跳过超大文件且确有非关键文件超阈值）。
    # 这才是状态栏要提示的"已跳过"。
    oversize_count: int = 0
    oversize_bytes: int = 0
    missing_keys: list[str] = field(default_factory=list)
    # 按扩展名（小写，含点，如 ".md"；无扩展名为 ""）分组的字节数，用于按文件类型
    # 估算压缩后体积，比单一固定系数更贴近实际。新增字段，向后兼容。
    bytes_by_ext: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)


def _arcname(abs_path: str, root: str) -> str:
    """归档内路径 = 相对工具根目录的路径（正斜杠）。"""
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


def _longpath(path: str) -> str:
    """Windows 长路径前缀（``\\\\?\\``），绕过 260 字符 MAX_PATH 限制；
    其他平台原样返回。CodeBuddy 等会话消息文件层级深、绝对路径常超 260，
    不加前缀会导致 getsize/open 报 WinError 3。"""
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        if os.path.isabs(path):
            return "\\\\?\\" + os.path.abspath(path)
    return path


def _strip_longpath(path: str) -> str:
    """去掉 ``\\\\?\\`` 长路径前缀，便于 ``os.path.relpath`` 计算归档相对路径。"""
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_critical(rel_path: str) -> bool:
    """判断是否为不受体积上限约束、也不受排除规则过滤的关键数据文件。

    匹配口径与 :func:`is_excluded` 保持一致（含试拼前导 ``/``）：``ALWAYS_INCLUDE``
    的模式都写作 ``*/段/名`` 形式，而归档内相对路径**首段前没有斜杠**（如
    ``.workbuddy/workbuddy.db``），若不试拼前导斜杠，形如 ``*/.workbuddy/*.db``
    的规则会永远匹配不上（只对位于中间层的 ``*/cache/db/*.db`` 才恰好生效）。
    """
    p = _norm_for_match(rel_path)
    name = p.rsplit("/", 1)[-1]
    for pat in ALWAYS_INCLUDE:
        pl = pat.replace(os.sep, "/").lower()
        if fnmatch.fnmatch(p, pl) or fnmatch.fnmatch(name, pl):
            return True
        if fnmatch.fnmatch("/" + p, pl):
            return True
    return False


def scan_items(
    items: Sequence[BackupItem],
    root: str,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    max_file_mb: float | None = 200.0,
) -> ScanResult:
    """
    扫描待备份文件清单。

    :param max_file_mb: 单文件体积上限（MB），超出则跳过；``None`` 表示不限制。
        命中 :data:`ALWAYS_INCLUDE` 的关键库文件不受此限制。
    """
    result = ScanResult()
    excludes = tuple(excludes)
    limit = None if max_file_mb is None else int(max_file_mb * 1024 * 1024)
    seen: set[str] = set()

    for item in items:
        if not item.exists:
            result.missing_keys.append(item.key)
            continue

        if item.is_dir:
            walker = (
                os.path.join(dp, fn)
                for dp, _, fns in os.walk(_longpath(item.path))
                for fn in fns
            )
        else:
            walker = iter([item.path])

        for full in walker:
            # walk 入口加了 \\?\ 前缀后，返回的子路径也带前缀；归档名需先去前缀
            plain = _strip_longpath(full)
            try:
                rel = _arcname(plain, root)
            except ValueError:
                continue
            if rel.startswith(".."):
                continue
            key = rel.lower()
            if key in seen:
                continue

            try:
                size = os.path.getsize(full)
            except OSError:
                continue

            critical = is_critical(rel)
            if not critical:
                excluded = is_excluded(rel, excludes)
                oversize = limit is not None and size > limit
                if excluded or oversize:
                    # 排除规则过滤属必要、用户无感；仅超大文件过滤计入"已跳过"提示
                    if oversize:
                        result.oversize_count += 1
                        result.oversize_bytes += size
                    result.skipped_count += 1
                    result.skipped_bytes += size
                    continue

            seen.add(key)
            result.files.append((full, rel))
            result.total_bytes += size
            ext = os.path.splitext(full)[1].lower()
            result.bytes_by_ext[ext] = result.bytes_by_ext.get(ext, 0) + size

    result.files.sort(key=lambda x: x[1])
    return result


# --------------------------------------------------------------------------- #
# 进度
# --------------------------------------------------------------------------- #
@dataclass
class ProgressInfo:
    current: int
    total: int
    message: str

    @property
    def percent(self) -> float:
        return 100.0 * self.current / self.total if self.total else 0.0


ProgressCb = Callable[[ProgressInfo], None]


def _noop(_: ProgressInfo) -> None:
    pass


# --------------------------------------------------------------------------- #
# SQLite 安全快照
# --------------------------------------------------------------------------- #
def snapshot_sqlite(src: str, dst: str) -> bool:
    """使用 SQLite 在线备份 API 生成一致性快照。成功返回 True，失败返回 False。"""
    try:
        src_conn = sqlite3.connect("file:%s?mode=ro" % src.replace("?", "%3f"), uri=True)
    except sqlite3.Error:
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        dst_conn = sqlite3.connect(dst)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        src_conn.close()


def _is_sqlite(path: str) -> bool:
    try:
        with open(_longpath(path), "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export_backup(
    zip_path: str,
    items: Sequence[BackupItem],
    root: str,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    max_file_mb: float | None = 200.0,
    progress: ProgressCb = _noop,
    compresslevel: int = 6,
    tool_name: str = "unknown",
    extra_meta: dict | None = None,
    kind: str = KIND_BACKUP,
    export_transform: "Callable[[str, bytes], bytes] | None" = None,
    export_transform_paths: "Sequence[str] | None" = None,
    extra_files: "Mapping[str, bytes] | None" = None,
) -> dict:
    """
    导出备份到 zip。

    :param tool_name: 来源工具标识（写入 manifest，便于跨工具识别）。
    :param extra_files: 适配器**生成**的附加文件（``绝对落点路径 -> 字节内容``）。
        这些文件**不在磁盘上**，由适配器在导出时构造（典型：把「会话 -> 原始工作区
        路径」映射落成一份 JSON，随包携带，使跨机还原后仍能提醒用户会话原本属于
        哪个工程）。路径须位于 ``root`` 之下，写入时按 :func:`_arcname` 转成归档内
        相对路径，因此**还原时会自动落回原位**，无需任何特殊处理。
    :param extra_meta: 适配器可附加的自定义元信息（如版本、子模块说明）。
    :param kind: 包类型，写入 manifest 的 ``kind`` 字段，供还原时校验，
        防止仅改文件名就被误还原。默认 ``"backup"``，回滚快照传 ``"rollback"``。
    :return: manifest 字典
    :raises BackupError: 无可备份内容或写入失败
    """
    scan = scan_items(items, root, excludes, max_file_mb)
    if not scan.files:
        raise BackupError(
            "没有扫描到任何可备份的文件。\n"
            "请确认数据目录是否正确，或勾选了实际存在的模块。"
        )

    os.makedirs(os.path.dirname(os.path.abspath(zip_path)) or ".", exist_ok=True)
    tmp_zip = zip_path + ".part"
    selected_keys = [i.key for i in items if i.exists]
    # 携带源设备信息的条目：与 items 同口径（只记确实存在的），供还原/分享前提示。
    # 旧版本读本字段会直接忽略（多一个键不影响向下兼容）。
    origin_info = origin_info_entries([i for i in items if i.exists])

    manifest = {
        "version": MANIFEST_VERSION,
        "kind": kind,
        "tool": tool_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": root,
        "platform": os.name,
        "items": selected_keys,
        "origin_info": origin_info,
        "file_count": scan.file_count,
        "total_bytes": scan.total_bytes,
        "skipped_count": scan.skipped_count,
        "skipped_bytes": scan.skipped_bytes,
        # 按类型（扩展名）累计的源体积与压缩后体积，供「按当前勾选组成」校准压缩估算
        "bytes_by_ext": {},
        "bytes_by_ext_compressed": {},
    }
    if extra_meta:
        manifest["extra"] = extra_meta

    total = scan.file_count
    transform_paths_norm = (
        {("/" + p.replace("\\", "/").lstrip("/")) for p in export_transform_paths}
        if export_transform_paths is not None
        else None
    )
    tmp_dir = tempfile.mkdtemp(prefix="aienv_snap_")
    try:
        with zipfile.ZipFile(
            tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel
        ) as zf:
            for idx, (full, rel) in enumerate(scan.files, 1):
                progress(ProgressInfo(idx, total, "打包 %s" % rel))

                write_from = full
                snap = None
                if _is_sqlite(full):
                    snap = os.path.join(tmp_dir, "%d.db" % idx)
                    if snapshot_sqlite(full, snap):
                        write_from = snap
                    else:
                        snap = None
                # 命中「需脱敏变换的文件」（按归档内相对路径后缀匹配）：
                # 读源字节 → 适配器改写 → 以改写后字节写入，避免明文敏感信息入库。
                rel_norm = rel.replace("\\", "/")
                hit_transform = (
                    export_transform is not None
                    and transform_paths_norm is not None
                    and any(rel_norm.endswith(suffix) for suffix in transform_paths_norm)
                )
                try:
                    if hit_transform:
                        with open(_longpath(write_from), "rb") as fh:
                            data = export_transform(rel_norm, fh.read())
                        zf.writestr(rel, data)
                    else:
                        zf.write(_longpath(write_from), arcname=rel)
                except (OSError, PermissionError) as exc:
                    manifest.setdefault("failed", []).append(
                        {"path": rel, "error": str(exc)}
                    )
                else:
                    # 成功写入才统计；按归档内扩展名归类（SQLite 快照仍记为 .db）
                    info = zf.getinfo(rel)
                    ext = os.path.splitext(rel)[1].lower()
                    manifest["bytes_by_ext"][ext] = (
                        manifest["bytes_by_ext"].get(ext, 0) + info.file_size
                    )
                    manifest["bytes_by_ext_compressed"][ext] = (
                        manifest["bytes_by_ext_compressed"].get(ext, 0)
                        + info.compress_size
                    )
                finally:
                    if snap and os.path.exists(snap):
                        try:
                            os.remove(snap)
                        except OSError:
                            pass

            # 适配器生成、不在磁盘上的附加文件（如「会话 -> 原始工作区路径」映射）。
            # 按 _arcname 转归档名后写入，还原时自然回到原位；单独记进 manifest 的
            # generated 字段，便于详情区展示与「分享前留意」提示。
            generated: list[str] = []
            for abs_path, data in (extra_files or {}).items():
                try:
                    grel = _arcname(abs_path, root).replace(os.sep, "/")
                except ValueError:
                    continue
                if grel.startswith("..") or not grel:
                    continue
                zf.writestr(grel, data)
                generated.append(grel)
                manifest["file_count"] += 1
                manifest["total_bytes"] += len(data)
                ext = os.path.splitext(grel)[1].lower()
                manifest["bytes_by_ext"][ext] = (
                    manifest["bytes_by_ext"].get(ext, 0) + len(data)
                )
            if generated:
                manifest["generated"] = sorted(generated)

            zf.writestr(
                MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2)
            )
    except Exception as exc:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        raise BackupError("打包失败：%s" % exc) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if os.path.exists(zip_path):
        os.remove(zip_path)
    os.replace(tmp_zip, zip_path)

    manifest["zip_path"] = zip_path
    manifest["zip_bytes"] = os.path.getsize(zip_path)
    progress(ProgressInfo(total, total, "完成"))
    return manifest


# --------------------------------------------------------------------------- #
# 校验 / 导入
# --------------------------------------------------------------------------- #
def safe_target(root_real: str, member_name: str) -> str | None:
    """
    计算成员的落地绝对路径，并阻断 Zip Slip 路径穿越。

    非法（绝对路径、``..`` 穿越、驱动器跳转）时返回 ``None``。
    """
    name = member_name.replace("\\", "/")
    if not name:
        return None
    if name.startswith("/") or os.path.isabs(name):
        return None
    if len(name) > 1 and name[1] == ":":
        return None

    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if not parts:
        return None

    target = os.path.normpath(os.path.join(root_real, *parts))
    try:
        if os.path.commonpath([os.path.abspath(target), root_real]) != root_real:
            return None
    except ValueError:
        return None
    return target


# --------------------------------------------------------------------------- #
# 还原辅助：原子写文件 / 回滚快照 / 增量合并
# --------------------------------------------------------------------------- #
def _write_bytes_atomic(target: str, data: bytes) -> None:
    """先写 ``<target>.aienv_tmp`` 再 ``os.replace`` 原子替换，避免半截文件。"""
    tmp = target + ".aienv_tmp"
    with open(_longpath(tmp), "wb") as fh:
        fh.write(data)
    os.replace(_longpath(tmp), _longpath(target))


def _create_rollback_snapshot(
    victims: "Sequence[tuple[str, str]]",
    rollback_dir: str | None,
    root_real: str,
    tool: str | None,
    zip_path: str,
    progress: ProgressCb,
) -> str | None:
    """把 ``victims``（``(目标绝对路径, 归档内相对路径)``）打包成回滚快照；失败返回 ``None``。

    回滚快照与备份文件同目录，文件名带工具名与时间戳，并写入 manifest，
    便于后续被识别 / 再次还原。合并与全覆盖两条路径共用本函数。
    """
    rb_dir = os.path.realpath(rollback_dir) if rollback_dir else root_real
    os.makedirs(rb_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tool_tag = (tool or "aienv").replace(" ", "")
    rollback_path = os.path.join(rb_dir, "%s_rollback_%s.zip" % (tool_tag, ts))
    rb_manifest = {
        "version": MANIFEST_VERSION,
        "kind": KIND_ROLLBACK,
        "tool": tool or "unknown",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": root_real,
        "platform": os.name,
        "desc": "还原前自动生成的回滚快照",
        "from_backup": os.path.basename(zip_path),
    }
    try:
        with zipfile.ZipFile(rollback_path, "w", zipfile.ZIP_DEFLATED) as rb:
            for idx, (t, arc) in enumerate(victims, 1):
                progress(ProgressInfo(idx, len(victims), "生成回滚快照 %s" % arc))
                rb.write(_longpath(t), arcname=arc)
            rb.writestr(
                MANIFEST_NAME,
                json.dumps(rb_manifest, ensure_ascii=False, indent=2),
            )
    except OSError:
        return None
    return rollback_path


def _restore_from_snapshot(
    rollback_path: str | None, created_new: "Sequence[str]", root_real: str
) -> list[str]:
    """把本次新写入 / 覆盖的目标恢复到合并前状态，返回未能恢复的路径列表。

    - ``created_new``：合并前**不存在**、本次新建的目标 ⇒ 删除；
    - 其余被覆盖的目标 ⇒ 从回滚快照写回原字节。
    """
    failed: list[str] = []
    for target in created_new:
        try:
            if os.path.isfile(_longpath(target)):
                os.remove(_longpath(target))
        except OSError:
            failed.append(target)
    if not rollback_path or not os.path.isfile(_longpath(rollback_path)):
        return failed
    try:
        with zipfile.ZipFile(rollback_path, "r") as rb:
            for m in rb.infolist():
                if m.is_dir() or m.filename == MANIFEST_NAME:
                    continue
                target = safe_target(root_real, m.filename)
                if target is None:
                    failed.append(m.filename)
                    continue
                try:
                    os.makedirs(_longpath(os.path.dirname(target)), exist_ok=True)
                    _write_bytes_atomic(target, rb.read(m))
                except OSError:
                    failed.append(target)
    except (OSError, zipfile.BadZipFile):
        failed.append(rollback_path)
    return failed


def _run_merge_restore(
    zf: zipfile.ZipFile,
    members: "Sequence[zipfile.ZipInfo]",
    root_real: str,
    path_rewrite: "Callable[[str], str] | None",
    restore_policy_for: "Callable[[str], str] | None",
    restore_merge_target: "Callable[[str, str, bytes], None] | None",
    make_rollback: bool,
    rollback_dir: str | None,
    tool: str | None,
    zip_path: str,
    progress: ProgressCb,
) -> dict:
    """增量合并还原（方案 §3.4 的两阶段 + 失败整体回滚）。

    阶段 0/1【计算】：分类每个成员并读出合并源字节，**不写任何目标**；
    阶段 2【快照】：对所有将被写入/合并且目标已存在的文件生成回滚快照；
    阶段 3【提交】：逐个目标落地（新文件走 tmp+replace，合并走适配器就地钩子）；
    阶段 5【回滚】：任一目标失败 ⇒ 删除新建文件 + 从快照写回被覆盖文件，再抛错。
    """
    restored = skipped = 0
    blocked: list[str] = []
    plan: list[tuple[str, str, str, zipfile.ZipInfo]] = []

    for m in members:
        arcname = path_rewrite(m.filename) if path_rewrite else m.filename
        target = safe_target(root_real, arcname)
        if target is None:
            blocked.append(arcname)
            continue
        arc_norm = arcname.replace("\\", "/")
        policy = (
            restore_policy_for(arc_norm)
            if restore_policy_for is not None
            else merge_plan.REPLACE
        )
        if policy == merge_plan.KEEP_LOCAL:
            if os.path.isfile(_longpath(target)):
                skipped += 1
                continue
            plan.append(("write", arc_norm, target, m))
        elif policy == merge_plan.MERGE and restore_merge_target is not None:
            plan.append(("merge", arc_norm, target, m))
        else:
            plan.append(("write", arc_norm, target, m))

    # 阶段 1 续：读出合并源字节（只读包，不写盘）。读失败 ⇒ 尚未动过本机，直接中止。
    merge_bytes: dict[str, bytes] = {}
    for kind, arc_norm, _target, m in plan:
        if kind != "merge":
            continue
        try:
            merge_bytes[arc_norm] = zf.read(m)
        except Exception as exc:  # noqa: BLE001 - 读包失败一律中止且不改本机
            raise BackupError(
                "读取备份包内容失败，未改动本机数据：%s（%s）" % (m.filename, exc)
            ) from exc

    # 阶段 2：回滚快照（覆盖 / 合并的既有目标）。
    victims = [
        (target, arc_norm)
        for _kind, arc_norm, target, _m in plan
        if os.path.isfile(_longpath(target))
    ]
    rollback_path = None
    if make_rollback and victims:
        rollback_path = _create_rollback_snapshot(
            victims, rollback_dir, root_real, tool, zip_path, progress
        )

    created_new = [
        target
        for _kind, _arc, target, _m in plan
        if not os.path.isfile(_longpath(target))
    ]
    written: list[str] = []
    total = len(plan)
    try:
        for idx, (kind, arc_norm, target, m) in enumerate(plan, 1):
            progress(ProgressInfo(idx, total, "合并 %s" % m.filename))
            os.makedirs(_longpath(os.path.dirname(target)), exist_ok=True)
            if kind == "merge":
                restore_merge_target(arc_norm, target, merge_bytes.get(arc_norm, b""))
            else:
                _write_bytes_atomic(target, zf.read(m))
            written.append(target)
            restored += 1
    except Exception as exc:  # noqa: BLE001 - 需要整体回滚后再上抛
        failed = _restore_from_snapshot(rollback_path, created_new, root_real)
        tail = ""
        if failed:
            tail = "（注意：以下目标回滚失败，请用回滚快照手工恢复：%s）" % "、".join(failed[:5])
        raise BackupError("合并还原失败，已回滚，本机数据未变%s：%s" % (tail, exc)) from exc

    return {
        "restored": restored,
        "skipped": skipped,
        "blocked": blocked,
        "rollback": rollback_path,
        "restored_targets": written,
    }


def inspect_backup(
    zip_path: str,
    verify: bool = False,
    match_structure: "Callable[[Sequence[str]], tuple[bool, list[str]]] | None" = None,
) -> dict:
    """
    读取备份包信息（默认不做完整性校验，避免对大文件逐个解压算 CRC 导致卡顿）。

    :param verify: 为 ``True`` 时额外调用 ``ZipFile.testzip()`` 校验完整性
        （会对每个文件解压算 CRC，**大文件会很慢**，建议仅在用户主动点击
        「校验完整性」时开启）。
    :param match_structure: 可选的结构指纹回调函数，签名为
        ``(names: Sequence[str]) -> (matched: bool, missing: list[str])``。
        当包内缺失 manifest 或 manifest 无 ``kind`` 时，用它对 zip 内条目名
        做结构指纹判定（识别数据类型 / 是否为该工具数据）。不传则跳过。
    :return: {'manifest': dict|None, 'file_count': int, 'total_bytes': int,
              'unsafe': [str], 'has_manifest': bool, 'bytes_by_ext': dict,
              'corrupted': str|None, 'structure_match': bool|None,
              'structure_missing': list[str], 'entries': list[str]}
        - ``bytes_by_ext``：扩展名（小写含点）-> 源字节数，供 GUI 按类型归类展示。
        - ``corrupted``：校验失败时返回首个损坏文件名，否则为 ``None``。
        - ``structure_match``：结构指纹是否匹配（仅当传入 match_structure 且
          manifest 缺 ``kind`` 时计算，否则为 ``None``）。
        - ``structure_missing``：结构指纹缺失项说明。
        - ``entries``：zip 内全部条目名（含目录），供上层做结构判定/展示。
    """
    if not os.path.exists(zip_path):
        raise BackupError("备份文件不存在：%s" % zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise BackupError("不是有效的 zip 备份文件：%s" % zip_path)

    info: dict = {
        "manifest": None,
        "file_count": 0,
        "total_bytes": 0,
        "unsafe": [],
        "has_manifest": False,
        "bytes_by_ext": {},
        "corrupted": None,
        "structure_match": None,
        "structure_missing": [],
        "entries": [],
    }
    probe_root = os.path.realpath(tempfile.gettempdir())

    with zipfile.ZipFile(zip_path, "r") as zf:
        if verify:
            bad = zf.testzip()
            if bad is not None:
                info["corrupted"] = bad

        names: list[str] = []
        for m in zf.infolist():
            names.append(m.filename)
            if m.filename == MANIFEST_NAME:
                info["has_manifest"] = True
                try:
                    info["manifest"] = json.loads(zf.read(m).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    info["manifest"] = None
                continue
            if m.is_dir():
                continue
            info["file_count"] += 1
            info["total_bytes"] += m.file_size
            ext = os.path.splitext(m.filename)[1].lower()
            info["bytes_by_ext"][ext] = info["bytes_by_ext"].get(ext, 0) + m.file_size
            if safe_target(probe_root, m.filename) is None:
                info["unsafe"].append(m.filename)
        info["entries"] = names

    # 解析类型：优先用 manifest 的 kind；有 manifest 但缺 kind（老版本包）时
    # 退而用文件名约定推断，仍视为「有声明」，不触发结构指纹回退识别。
    # 仅当完全无 manifest 时，才用结构指纹回退识别类型。
    manifest = info.get("manifest") or {}
    if manifest.get("kind") in (None, ""):
        inferred = classify_zip_name(zip_path) if manifest else None
        if inferred in ("backup", "rollback"):
            manifest = dict(manifest)
            manifest["kind"] = inferred
            info["manifest"] = manifest
            info["kind_inferred"] = True
        elif not manifest:
            # 完全无 manifest：结构指纹回退
            if match_structure is not None:
                matched, missing = match_structure(names)
                info["structure_match"] = matched
                info["structure_missing"] = missing
    else:
        info["kind_inferred"] = False

    return info


#: 备份/快照文件名标记 -> 类型标识
#: 现行约定为 ``<tool>_backup_`` / ``<tool>_rollback_``（含工具名，便于多工具混放时分辨）；
#: 旧版曾用无工具名前缀（``backup_`` / ``aienv_rollback_``），此处依然兼容识别。
BACKUP_PREFIX = "backup_"
ROLLBACK_PREFIX = "aienv_rollback_"
BACKUP_MARK = "_backup_"
ROLLBACK_MARK = "_rollback_"


def classify_zip_name(filename: str) -> str:
    """
    按文件名约定判定 zip 类型：``backup``（主动导出备份）/
    ``rollback``（还原前自动生成的回滚快照）/ ``unknown``。

    匹配优先级：含 ``_rollback_`` 标记或旧版 ``aienv_rollback_`` 前缀 → rollback；
    含 ``_backup_`` 标记或旧版 ``backup_`` 前缀 → backup；其余 → unknown。

    注意：分类仅依据文件名约定；真正的类型以包内 manifest 的 ``kind`` 字段为准
    （还原时二次校验，防止仅改名就被误还原）。
    """
    base = os.path.basename(filename)
    lowered = base.lower()
    if ROLLBACK_MARK in lowered or base.startswith(ROLLBACK_PREFIX):
        return KIND_ROLLBACK
    if BACKUP_MARK in lowered or base.startswith(BACKUP_PREFIX):
        return KIND_BACKUP
    return "unknown"


def list_backup_dir(dir_path: str) -> list:
    """
    列出目录下所有 ``*.zip`` 文件（**只读目录元数据，不打开/不解压 zip**，秒开），
    供备份浏览器做虚拟加载。

    :return: 按修改时间倒序的列表，每项
        ``{'path', 'name', 'size', 'mtime', 'kind'}``
        （kind 为 ``classify_zip_name`` 的结果）。
    """
    rows: list = []
    if not os.path.isdir(dir_path):
        return rows
    for name in os.listdir(dir_path):
        if not name.lower().endswith(".zip"):
            continue
        full = os.path.join(dir_path, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        rows.append(
            {
                "path": full,
                "name": name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "kind": classify_zip_name(name),
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def import_backup(
    zip_path: str,
    root: str,
    progress: ProgressCb = _noop,
    make_rollback: bool = True,
    overwrite: bool = True,
    rollback_dir: str | None = None,
    expected_kind: str | None = None,
    strict: bool = False,
    match_structure: "Callable[[Sequence[str]], tuple[bool, list[str]]] | None" = None,
    path_rewrite: "Callable[[str], str] | None" = None,
    restore_post_hook: "Callable[[str, list[str]], None] | None" = None,
    restore_index_merge: "Callable[[str, bytes, bytes], bytes] | None" = None,
    restore_index_merge_paths: "Sequence[str] | None" = None,
    mode: str = "replace",
    restore_policy_for: "Callable[[str], str] | None" = None,
    restore_merge_target: "Callable[[str, str, bytes], None] | None" = None,
) -> dict:
    """
    恢复备份到根目录。

    回滚快照默认保存到 ``rollback_dir``（调用方按工具名分目录传入，例如
    ``<工具运行目录>/backup/<工具名>/``，与备份文件同目录，方便按时间信息对比选择）；
    若未传则退回到 ``root_real``。

    :param make_rollback: 覆盖前把同名旧文件打包成回滚快照
    :param overwrite: 为 ``False`` 时跳过已存在的文件
    :param rollback_dir: 回滚快照存放目录（可按工具名分目录），不存在则创建
    :param expected_kind: 期望的包类型（``"backup"`` / ``"rollback"``）。
        若包内 manifest 的 ``kind`` 与此不符，抛出 ``BackupError`` 阻止还原，
        防止仅改文件名就被误还原。传 ``None`` 则不做类型限制。
    :param strict: 严格校验模式。为 ``True`` 时即使包内带 manifest，也强制
        对 zip 内条目做结构指纹扫描（需配合 ``match_structure``），不一致则
        抛出 ``BackupError``，用于防止伪造声明文件的恶意备份。
    :param match_structure: 结构指纹回调函数（同 :func:`inspect_backup`）。
        缺 manifest 或 ``strict`` 模式下用于判定数据类型与结构是否匹配。
    :param path_rewrite: 可选「归档内相对路径 -> 还原目标相对路径」重写函数，
        用于跨电脑还原时把源机器特有标识（如用户 UUID）重映射为本机当前用户，
        避免数据落到目标机器读不到的「死目录」（典型症状：界面能看到历史会话
        列表残留，但点开看不到具体对话内容）。返回 ``None`` 表示不改写。
        注意：重写仅改变落盘位置，回滚快照的「被覆盖文件」判定也基于重写后的
        目标路径，确保回滚能正确对照新位置。
    :param restore_post_hook: 还原落盘全部完成后调用的回调
        ``callback(root_real, restored_targets)``。``root_real`` 为经
        ``os.path.realpath`` 解析的目标根目录；``restored_targets`` 为本次
        实际落盘写入的**目标文件绝对路径**列表（不含被跳过项）。
        用于跨电脑还原时修正「被整体覆盖的全局索引文件」——典型场景：
        某工具的工作区名存在一个全局索引（如 DSH 的 ``storages/workspace.json``），
        直接覆盖写入会抹掉目标机器原本的其他工作区，使它们变成 ``ungrouped``；
        适配器可在本回调里把源索引与目标机器已有索引**合并**而非覆盖。
        返回 ``None`` 表示不挂载任何后处理。
    :param restore_index_merge: 针对「会被整体覆盖的全局索引文件」的合并回调
        ``callback(relpath, source_bytes, original_bytes) -> merged_bytes``：
        - ``relpath``：归档内相对路径（经 ``path_rewrite`` 重写后的目标相对路径）；
        - ``source_bytes``：备份包里该文件的原始字节；
        - ``original_bytes``：还原前目标机器上该文件的已有字节（不存在则为 ``b""``）；
        - 返回：应写入目标的合并后字节。
        仅当 ``relpath`` 出现在 ``restore_index_merge_paths`` 中时被调用，
        用于跨电脑还原时把源索引与本机已有索引**合并**而非覆盖（避免抹掉本机
        其他工作区）。与 ``restore_post_hook`` 互斥取舍：合并需在覆盖**前**拿到
        本机旧内容，故用本参数；若不需旧内容、只做末端修补则用 ``restore_post_hook``。
    :param restore_index_merge_paths: 需要走 ``restore_index_merge`` 的相对路径**后缀**集合
        （如 ``["storages/workspace.json"]``）。匹配采用**后缀判定**：归档内成员名相对公共根
        带根占位前缀（如 ``C__Users_x/.dsh/storages/workspace.json``），故只要成员名以
        ``/<声明片段>`` 结尾即视为命中，避免写死根前缀而失效。``None`` 表示不启用合并。
    :param mode: 还原方式。``"replace"``（默认）= 全覆盖，逐文件覆盖写入，**与历史行为完全
        一致**；``"merge"`` = 增量合并，按 :mod:`ai_env_clone.merge_plan` 的策略把包内数据
        并入目标（本机已有数据不动），并**先算后写 + 失败整体回滚**（见 ``_run_merge_restore``）。
        合并模式下 ``restore_index_merge`` / ``restore_post_hook`` **不生效**——索引并集改由
        策略 ``"merge"`` 经 ``restore_merge_target`` 完成。
    :param restore_policy_for: 仅合并模式使用：``relpath -> "replace"|"merge"|"keep_local"``。
        通常直接传适配器的 :meth:`BaseAdapter.restore_policy_for`。
    :param restore_merge_target: 仅合并模式使用：``callback(relpath, target_abs, source_bytes)``，
        就地完成记录级合并（如把包内 SQLite 的会话行并入本机库）。声明了 ``"merge"`` 策略的
        适配器必须提供，否则那些成员会退化为覆盖写入。
    :return: {'restored': int, 'skipped': int, 'blocked': [str], 'rollback': str|None,
              'kind': str|None, 'tool': str|None, 'source_root': str|None,
              'structure_match': bool|None, 'structure_missing': list[str]}
    """
    info = inspect_backup(zip_path, match_structure=match_structure)
    if info["unsafe"]:
        raise BackupError(
            "备份包中存在非法路径，已终止恢复：\n%s"
            % "\n".join(info["unsafe"][:5])
        )

    manifest = info.get("manifest") or {}
    kind = manifest.get("kind")
    tool = manifest.get("tool")
    source_root = manifest.get("source_root")

    if expected_kind is not None and kind is not None and kind != expected_kind:
        raise BackupError(
            "类型不匹配，已阻止还原以防误覆盖数据。\n\n"
            "压缩包文件名虽然像「%s」，但包内 manifest 记录的类型是「%s」。\n"
            "本工具仅还原类型为「%s」的压缩包，请确认是否选错了文件。"
            % (
                {"backup": "备份", "rollback": "回滚快照"}.get(expected_kind, expected_kind),
                {"backup": "备份", "rollback": "回滚快照"}.get(kind, kind),
                {"backup": "备份", "rollback": "回滚快照"}.get(expected_kind, expected_kind),
            )
        )

    # 结构指纹校验：缺 manifest / 无 kind 时回退判断；strict 模式强制校验
    if match_structure is not None:
        need_structure = (kind is None) or strict
        if need_structure:
            matched, missing = match_structure(info.get("entries") or [])
            info["structure_match"] = matched
            info["structure_missing"] = missing
            if not matched:
                if kind is None:
                    reason = (
                        "该压缩包缺少清单文件，且内部数据结构与 %s 不匹配，"
                        "无法确认是可还原的备份。\n缺失项：%s"
                        % (
                            {"backup": "备份", "rollback": "回滚快照"}.get(
                                expected_kind or "backup", expected_kind or "备份"
                            ),
                            "、".join(missing) or "（无）",
                        )
                    )
                else:
                    reason = (
                        "严格校验模式下，压缩包内部结构指纹与 %s 不一致"
                        "（疑似伪造声明文件），已阻止还原以防数据损坏。\n缺失项：%s"
                        % (tool or "该工具", "、".join(missing) or "（无）")
                    )
                raise BackupError(reason)

    os.makedirs(root, exist_ok=True)
    root_real = os.path.realpath(root)

    rollback_path = None
    restored = skipped = 0
    blocked: list[str] = []
    restored_dbs: list[str] = []
    restored_targets: list[str] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [
            m for m in zf.infolist() if not m.is_dir() and m.filename != MANIFEST_NAME
        ]
        total = len(members)

        if mode == "merge":
            # 增量合并：走独立的两阶段实现（先算后写 + 失败整体回滚），
            # 不使用 restore_index_merge / restore_post_hook（索引并集改由 "merge" 策略钩子完成）。
            merged = _run_merge_restore(
                zf,
                members,
                root_real,
                path_rewrite,
                restore_policy_for,
                restore_merge_target,
                make_rollback,
                rollback_dir,
                tool,
                zip_path,
                progress,
            )
            progress(ProgressInfo(total, total, "完成"))
            merged.update(
                {
                    "kind": kind,
                    "tool": tool,
                    "source_root": source_root,
                    "structure_match": info.get("structure_match"),
                    "structure_missing": info.get("structure_missing"),
                }
            )
            return merged

        if make_rollback:
            victims = []
            for m in members:
                arcname = path_rewrite(m.filename) if path_rewrite else m.filename
                t = safe_target(root_real, arcname)
                if t and os.path.isfile(_longpath(t)):
                    victims.append((t, arcname))
            if victims:
                rollback_path = _create_rollback_snapshot(
                    victims, rollback_dir, root_real, tool, zip_path, progress
                )

        for idx, m in enumerate(members, 1):
            arcname = path_rewrite(m.filename) if path_rewrite else m.filename
            target = safe_target(root_real, arcname)
            if target is None:
                blocked.append(arcname)
                continue

            if os.path.exists(target) and not overwrite:
                skipped += 1
                continue

            # 该文件是否属于「需合并而非覆盖的全局索引」
            # 注意：归档内成员名相对公共根，带「根占位前缀」（如 ``C__Users_x/.dsh/...``，
            # 或 macOS/Linux 下的 ``Users/x/.dsh/...``），故 ``restore_index_merge_paths``
            # 里声明的相对片段用**后缀匹配**（不以完整路径相等判定），避免写死根前缀而失效。
            # 成员名已统一规范为正斜杠，Windows 反斜杠亦兜底替换。
            arcname_norm = arcname.replace("\\", "/")
            merge_paths_norm = (
                {("/" + p.replace("\\", "/").lstrip("/")) for p in restore_index_merge_paths}
                if restore_index_merge_paths is not None
                else None
            )
            needs_merge = (
                restore_index_merge is not None
                and merge_paths_norm is not None
                and any(arcname_norm.endswith(suffix) for suffix in merge_paths_norm)
            )

            progress(ProgressInfo(idx, total, "恢复 %s" % m.filename))
            parent = os.path.dirname(target)
            os.makedirs(_longpath(parent), exist_ok=True)
            try:
                if needs_merge:
                    # 合并写入：读源字节 + 本机原有字节，交由适配器合并后再落盘，
                    # 避免直接覆盖抹掉目标机器原本的其他工作区（如 DSH 的 ungrouped 问题）。
                    source_bytes = zf.read(m)
                    original_bytes = b""
                    if os.path.isfile(_longpath(target)):
                        with open(_longpath(target), "rb") as oh:
                            original_bytes = oh.read()
                    merged = restore_index_merge(arcname_norm, source_bytes, original_bytes)
                    with open(_longpath(target), "wb") as dst:
                        dst.write(merged)
                else:
                    with zf.open(m) as src, open(_longpath(target), "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 256)
                restored += 1
                restored_targets.append(target)
                if target.lower().endswith((".db", ".sqlite")):
                    restored_dbs.append(target)
            except OSError as exc:
                blocked.append("%s (%s)" % (m.filename, exc))

    for db in restored_dbs:
        for suffix in ("-wal", "-shm"):
            stray = db + suffix
            if os.path.exists(_longpath(stray)):
                try:
                    os.remove(_longpath(stray))
                except OSError:
                    pass

    progress(ProgressInfo(total, total, "完成"))

    # 还原后处理：交由适配器修正「被整体覆盖的全局索引文件」（如 DSH 的
    # workspace.json 工作区索引），避免覆盖目标机器原有的工作区关联。
    # 即便 hook 内部抛错也不应中断已完成的还原，记录但不向上抛，
    # 让 GUI 仍能报告还原成功（索引合并失败可手动补救）。
    if restore_post_hook is not None and restored_targets:
        try:
            restore_post_hook(root_real, restored_targets)
        except Exception:  # pragma: no cover - 防御性兜底，避免破坏已完成的还原
            import traceback
            traceback.print_exc()

    return {
        "restored": restored,
        "skipped": skipped,
        "blocked": blocked,
        "rollback": rollback_path,
        "kind": kind,
        "tool": tool,
        "source_root": source_root,
        "structure_match": info.get("structure_match"),
        "structure_missing": info.get("structure_missing"),
    }
