"""
qoder_backup_core — Qoder 备份迁移核心（兼容层 + 真实实现）

本文件保持历史公共 API（供 ``qoder_backup_tool`` 与 ``test_qoder_backup`` 直接使用）：
    QoderPaths, detect_qoder_root, build_items,
    export_backup, import_backup, inspect_backup, scan_items,
    snapshot_sqlite, safe_target(别名为 _safe_target),
    is_excluded, is_critical, BackupItem, ScanResult, ProgressInfo,
    BackupError, MANIFEST_NAME, MANIFEST_VERSION, DEFAULT_EXCLUDES, ALWAYS_INCLUDE

通用逻辑在 ``ai_env_clone.core``；本文件只负责 Qoder 特有的「目录探测 + 条目定义」，
并通过 ``QoderAdapter``（``ai_env_clone/adapters/qoder.py``）接入多工具抽象层。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ai_env_clone.core import (
    ALWAYS_INCLUDE,
    BackupError,
    BackupItem,
    DEFAULT_EXCLUDES,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ProgressInfo,
    ScanResult,
    export_backup,
    import_backup,
    inspect_backup,
    is_critical,
    is_excluded,
    safe_target as _safe_target,  # 测试以 _safe_target 名导入
    scan_items,
    snapshot_sqlite,
)

__all__ = [
    "QoderPaths",
    "detect_qoder_root",
    "build_items",
    "export_backup",
    "import_backup",
    "inspect_backup",
    "scan_items",
    "snapshot_sqlite",
    "safe_target",
    "_safe_target",
    "is_excluded",
    "is_critical",
    "BackupItem",
    "ScanResult",
    "ProgressInfo",
    "BackupError",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "DEFAULT_EXCLUDES",
    "ALWAYS_INCLUDE",
]


@dataclass
class QoderPaths:
    """Qoder 数据目录布局。``shared`` 指向 shared_client（新版）或 sharedclient（旧版）。"""

    root: str
    shared: str

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.root) and os.path.isdir(self.shared)


def detect_qoder_root(explicit: str | None = None) -> "QoderPaths":
    """
    探测 Qoder 数据根目录。

    优先使用 ``explicit``；否则探测 ``~/.qoder-cn/shared_client``（新版）或
    ``~/.qoder-cn/sharedclient``（旧版）；都不存在则给出默认建议路径。
    """
    if explicit:
        root = os.path.expanduser(explicit)
        for name in ("shared_client", "sharedclient"):
            shared = os.path.join(root, name)
            if os.path.isdir(shared):
                return QoderPaths(root, shared)
        # 显式指定但子目录未匹配，仍以 .qoder-cn/shared_client 为约定
        return QoderPaths(root, os.path.join(root, "shared_client"))

    base = os.path.expanduser("~")
    root = os.path.join(base, ".qoder-cn")
    for name in ("shared_client", "sharedclient"):
        shared = os.path.join(root, name)
        if os.path.isdir(shared):
            return QoderPaths(root, shared)
    return QoderPaths(root, os.path.join(root, "shared_client"))


def build_items(paths: "QoderPaths") -> list[BackupItem]:
    """根据 Qoder 目录布局构造可备份条目清单。"""
    root, shared = paths.root, paths.shared
    items: list[BackupItem] = [
        BackupItem(
            key="settings",
            label="设置（全局/用户级）",
            path=os.path.join(root, "settings.json"),
            description="用户全局设置（如主题、自动化开关）。可选。",
            recommended=False,
        ),
        BackupItem(
            key="cache",
            label="缓存（代码索引/项目缓存）",
            path=os.path.join(root, "cache"),
            description="本地缓存，含项目数据缓存。建议必选。",
            recommended=True,
        ),
        BackupItem(
            key="memories",
            label="记忆区（长/短期记忆）",
            path=os.path.join(shared, "memories"),
            description="Qoder 记忆系统：项目长期记忆（memory.md）与短期记忆。建议必选。",
            recommended=True,
        ),
        BackupItem(
            key="rules",
            label="规则区（项目/用户规则）",
            path=os.path.join(shared, "rules"),
            description="项目级与用户级规则配置。建议必选。",
            recommended=True,
        ),
        BackupItem(
            key="cache/db",
            label="会话数据库（含历史索引，SQLite）",
            path=os.path.join(shared, "cache"),
            description="存放会话/历史的 SQLite 主库及其 -wal/-shm 配套文件（位于 cache/db 下）。建议必选。",
            recommended=True,
        ),
        BackupItem(
            key="code_index",
            label="代码索引数据库",
            path=os.path.join(shared, "index"),
            description="代码语义索引（.db/.bolt/.zap/.json）。建议必选。",
            recommended=True,
        ),
        BackupItem(
            key="session_env",
            label="会话环境",  # 默认通常不存在，仅作可选扩展位
            path=os.path.join(root, "session_env"),
            description="会话环境配置（如存在）。可选。",
            recommended=False,
        ),
    ]
    return items
