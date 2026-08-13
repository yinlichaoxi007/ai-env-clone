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
from typing import Callable, Iterable, Sequence

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
)

#: 无论体积多大都必须备份的关键文件（否则会话历史会丢失）
#: 包含 SQLite 主库及其配套文件（.db 主库、.db-wal 预写日志、.db-shm 共享内存），
#: 三者必须作为一个整体一起备份，缺一不可。
ALWAYS_INCLUDE: tuple[str, ...] = (
    "*/cache/db/*.db",
    "*/cache/db/*.sqlite",
    "*/cache/db/*.db-wal",
    "*/cache/db/*.db-shm",
)


class BackupError(Exception):
    """备份 / 恢复过程中的可预期错误。"""


@dataclass
class BackupItem:
    """一个可勾选的备份单元。适配器负责构造这些条目。"""

    key: str
    label: str
    path: str            # 绝对路径
    description: str
    recommended: bool = True
    uid: str | None = None  # 记忆区按 UID 拆分时的用户标识，非记忆项为 None

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    @property
    def is_dir(self) -> bool:
        return os.path.isdir(self.path)


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
    """判断是否为不受体积上限约束的关键数据文件。"""
    p = _norm_for_match(rel_path)
    name = p.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(p, pat.lower()) or fnmatch.fnmatch(name, pat.lower())
        for pat in ALWAYS_INCLUDE
    )


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
) -> dict:
    """
    导出备份到 zip。

    :param tool_name: 来源工具标识（写入 manifest，便于跨工具识别）。
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

    manifest = {
        "version": MANIFEST_VERSION,
        "kind": kind,
        "tool": tool_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": root,
        "platform": os.name,
        "items": selected_keys,
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
                try:
                    zf.write(_longpath(write_from), arcname=rel)
                except (OSError, PermissionError) as exc:
                    manifest.setdefault("failed", []).append(
                        {"path": rel, "error": str(exc)}
                    )
                else:
                    # 成功写入才统计；按归档内扩展名归类（SQLite 快照仍记为 .db）
                    ext = os.path.splitext(rel)[1].lower()
                    info = zf.getinfo(rel)
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

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [
            m for m in zf.infolist() if not m.is_dir() and m.filename != MANIFEST_NAME
        ]
        total = len(members)

        if make_rollback:
            victims = []
            for m in members:
                arcname = path_rewrite(m.filename) if path_rewrite else m.filename
                t = safe_target(root_real, arcname)
                if t and os.path.isfile(_longpath(t)):
                    victims.append((t, arcname))
            if victims:
                rb_dir = os.path.realpath(rollback_dir) if rollback_dir else root_real
                os.makedirs(rb_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tool_tag = (tool or "aienv").replace(" ", "")
                rollback_path = os.path.join(rb_dir, "%s_rollback_%s.zip" % (tool_tag, ts))
                # 回滚快照自身也写入 manifest，便于后续被识别/还原
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
                    with zipfile.ZipFile(
                        rollback_path, "w", zipfile.ZIP_DEFLATED
                    ) as rb:
                        for idx, (t, arc) in enumerate(victims, 1):
                            progress(
                                ProgressInfo(idx, len(victims), "生成回滚快照 %s" % arc)
                            )
                            rb.write(_longpath(t), arcname=arc)
                        rb.writestr(
                            MANIFEST_NAME,
                            json.dumps(rb_manifest, ensure_ascii=False, indent=2),
                        )
                except OSError:
                    rollback_path = None

        for idx, m in enumerate(members, 1):
            arcname = path_rewrite(m.filename) if path_rewrite else m.filename
            target = safe_target(root_real, arcname)
            if target is None:
                blocked.append(arcname)
                continue

            if os.path.exists(target) and not overwrite:
                skipped += 1
                continue

            progress(ProgressInfo(idx, total, "恢复 %s" % m.filename))
            parent = os.path.dirname(target)
            os.makedirs(_longpath(parent), exist_ok=True)
            try:
                with zf.open(m) as src, open(_longpath(target), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 256)
                restored += 1
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
