"""ai-env-clone：跨电脑备份与迁移国内 AI 编程工具的环境数据。

注意：为避免与 ``qoder_backup_core`` 形成循环导入，本包初始化时**不**自动导入
``adapters`` 子包。需要适配器时请显式：``from ai_env_clone.adapters import get_adapter``。
"""

from .core import (
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
    safe_target,
    scan_items,
    snapshot_sqlite,
)

__all__ = [
    "export_backup",
    "import_backup",
    "inspect_backup",
    "scan_items",
    "snapshot_sqlite",
    "safe_target",
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

__version__ = "0.1.0"
