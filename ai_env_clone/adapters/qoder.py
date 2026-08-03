"""
Qoder 适配器。

委托 ``qoder_backup_core``（Qoder 特有逻辑的唯一事实来源），
自身只暴露统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口。
"""

from __future__ import annotations

import os

from ..core import BackupItem
from .base import BaseAdapter, register

# 复用 qoder_backup_core 的探测与条目构造，避免逻辑重复
from qoder_backup_core import QoderPaths, build_items, detect_qoder_root  # noqa: F401


@register
class QoderAdapter(BaseAdapter):
    name = "qoder"
    display_name = "Qoder"

    def detect_root(self) -> str | None:
        paths = detect_qoder_root()
        return paths.root if paths.exists else None

    def build_default_root(self) -> str:
        return detect_qoder_root().root

    def build_items(self, root: str) -> list[BackupItem]:
        paths = QoderPaths(root, os.path.join(root, "shared_client"))
        if not os.path.isdir(paths.shared):
            paths = QoderPaths(root, os.path.join(root, "sharedclient"))
        return build_items(paths)
