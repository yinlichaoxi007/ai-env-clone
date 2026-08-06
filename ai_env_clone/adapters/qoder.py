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
from qoder_backup_core import (  # noqa: F401
    QoderPaths,
    build_items,
    detect_qoder_root,
    detect_current_uid,
)


@register
class QoderAdapter(BaseAdapter):
    name = "qoder"
    display_name = "Qoder"

    def detect_root(self) -> str | None:
        paths = detect_qoder_root()
        return paths.root if paths.exists else None

    def build_default_root(self) -> str:
        return detect_qoder_root().root

    def build_items(self, root: str, current_uid: str | None = None) -> list[BackupItem]:
        paths = QoderPaths(root, os.path.join(root, "shared_client"))
        if not os.path.isdir(paths.shared):
            paths = QoderPaths(root, os.path.join(root, "sharedclient"))
        detected = detect_current_uid(paths.root, paths.shared)
        self.last_detected_uid = detected
        return build_items(paths, current_uid=current_uid)

    # Qoder 数据结构指纹：shared_client 目录 + 关键文件（local.db / *.zap）
    _REQUIRED_DIRS = ("shared_client", "sharedclient")
    _KEY_FILES = ("local.db", "*.zap")

    def match_structure(self, names: Sequence[str]) -> tuple[bool, list[str]]:
        """
        依据条目名判断是否为 Qoder 数据结构（缺 manifest 时回退识别 / 严格模式校验）。

        指纹：存在 ``shared_client/``（或 ``sharedclient/``）目录，且其中至少含
        ``local.db`` 或任意一个 ``*.zap`` 文件。
        """
        norm = [n.replace("\\", "/").lower() for n in names]
        missing: list[str] = []

        has_dir = any(
            any(f"/{d}/" in ("/" + x + "/") or x == d or x.startswith(d + "/")
                for d in self._REQUIRED_DIRS)
            for x in norm
        )
        if not has_dir:
            missing.append("缺少 shared_client/ 目录")

        has_key = False
        for x in norm:
            if "/local.db" in x and x.endswith("local.db"):
                has_key = True
                break
            if x.endswith(".zap") and (
                "/shared_client/" in x or "/sharedclient/" in x or x.startswith("shared_client/") or x.startswith("sharedclient/")
            ):
                has_key = True
                break
        if not has_key:
            missing.append("shared_client 内缺少 local.db 或 *.zap 关键文件")

        return (len(missing) == 0, missing)
