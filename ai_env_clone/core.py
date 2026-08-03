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
    skipped_count: int = 0
    skipped_bytes: int = 0
    missing_keys: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)


def _arcname(abs_path: str, root: str) -> str:
    """归档内路径 = 相对工具根目录的路径（正斜杠）。"""
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


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
                for dp, _, fns in os.walk(item.path)
                for fn in fns
            )
        else:
            walker = iter([item.path])

        for full in walker:
            try:
                rel = _arcname(full, root)
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
                if is_excluded(rel, excludes) or (
                    limit is not None and size > limit
                ):
                    result.skipped_count += 1
                    result.skipped_bytes += size
                    continue

            seen.add(key)
            result.files.append((full, rel))
            result.total_bytes += size

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
        with open(path, "rb") as f:
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
) -> dict:
    """
    导出备份到 zip。

    :param tool_name: 来源工具标识（写入 manifest，便于跨工具识别）。
    :param extra_meta: 适配器可附加的自定义元信息（如版本、子模块说明）。
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
        "tool": tool_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": root,
        "platform": os.name,
        "items": selected_keys,
        "file_count": scan.file_count,
        "total_bytes": scan.total_bytes,
        "skipped_count": scan.skipped_count,
        "skipped_bytes": scan.skipped_bytes,
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
                    zf.write(write_from, arcname=rel)
                except (OSError, PermissionError) as exc:
                    manifest.setdefault("failed", []).append(
                        {"path": rel, "error": str(exc)}
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


def inspect_backup(zip_path: str) -> dict:
    """
    读取备份包信息并做完整性校验。

    :return: {'manifest': dict|None, 'file_count': int, 'total_bytes': int,
              'unsafe': [str], 'has_manifest': bool}
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
    }
    probe_root = os.path.realpath(tempfile.gettempdir())

    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise BackupError("备份包已损坏，首个损坏文件：%s" % bad)

        for m in zf.infolist():
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
            if safe_target(probe_root, m.filename) is None:
                info["unsafe"].append(m.filename)

    return info


def import_backup(
    zip_path: str,
    root: str,
    progress: ProgressCb = _noop,
    make_rollback: bool = True,
    overwrite: bool = True,
) -> dict:
    """
    恢复备份到根目录。

    :param make_rollback: 覆盖前把同名旧文件打包成回滚快照
    :param overwrite: 为 ``False`` 时跳过已存在的文件
    :return: {'restored': int, 'skipped': int, 'blocked': [str], 'rollback': str|None}
    """
    info = inspect_backup(zip_path)
    if info["unsafe"]:
        raise BackupError(
            "备份包中存在非法路径，已终止恢复：\n%s"
            % "\n".join(info["unsafe"][:5])
        )

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
                t = safe_target(root_real, m.filename)
                if t and os.path.isfile(t):
                    victims.append((t, m.filename))
            if victims:
                rollback_path = os.path.join(
                    root_real,
                    "aienv_rollback_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S"),
                )
                try:
                    with zipfile.ZipFile(
                        rollback_path, "w", zipfile.ZIP_DEFLATED
                    ) as rb:
                        for idx, (t, arc) in enumerate(victims, 1):
                            progress(
                                ProgressInfo(idx, len(victims), "生成回滚快照 %s" % arc)
                            )
                            rb.write(t, arcname=arc)
                except OSError:
                    rollback_path = None

        for idx, m in enumerate(members, 1):
            target = safe_target(root_real, m.filename)
            if target is None:
                blocked.append(m.filename)
                continue

            if os.path.exists(target) and not overwrite:
                skipped += 1
                continue

            progress(ProgressInfo(idx, total, "恢复 %s" % m.filename))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with zf.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 256)
                restored += 1
                if target.lower().endswith((".db", ".sqlite")):
                    restored_dbs.append(target)
            except OSError as exc:
                blocked.append("%s (%s)" % (m.filename, exc))

    for db in restored_dbs:
        for suffix in ("-wal", "-shm"):
            stray = db + suffix
            if os.path.exists(stray):
                try:
                    os.remove(stray)
                except OSError:
                    pass

    progress(ProgressInfo(total, total, "完成"))
    return {
        "restored": restored,
        "skipped": skipped,
        "blocked": blocked,
        "rollback": rollback_path,
    }
