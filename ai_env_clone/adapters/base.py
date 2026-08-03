"""
适配器抽象基类。

每个被支持的 AI 工具对应一个适配器模块（如 ``adapters/qoder.py``），
只需实现 :class:`BaseAdapter` 定义的接口即可接入主流程，无需改动 CLI / GUI。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Sequence

from ..core import BackupItem, export_backup, import_backup, inspect_backup

__all__ = ["BaseAdapter", "get_adapter"]


class BaseAdapter(ABC):
    """AI 工具适配器必须实现的接口。"""

    #: 工具唯一标识，写进 manifest 的 ``tool`` 字段（小写、无空格），如 "qoder"
    name: str = ""
    #: 人类可读名称，用于界面展示，如 "Qoder"
    display_name: str = ""

    # ------------------------------------------------------------------ #
    @abstractmethod
    def detect_root(self) -> str | None:
        """探测该工具在本机的数据根目录；找不到返回 ``None``。"""

    @abstractmethod
    def build_items(self, root: str) -> list[BackupItem]:
        """根据根目录构造可备份条目清单。"""

    @abstractmethod
    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录。"""

    # ------------------------------------------------------------------ #
    # 通用实现（多数适配器无需重写）
    # ------------------------------------------------------------------ #
    def export(
        self, zip_path: str, root: str, progress=None, max_file_mb: float | None = 200.0
    ) -> dict:
        items = self.build_items(root)
        return export_backup(
            zip_path,
            items,
            root,
            tool_name=self.name,
            progress=progress,
            max_file_mb=max_file_mb,
        )

    def inspect(self, zip_path: str) -> dict:
        return inspect_backup(zip_path)

    def restore(self, zip_path: str, root: str, progress=None, **kw) -> dict:
        return import_backup(zip_path, root, progress=progress, **kw)

    @staticmethod
    def join(root: str, *parts: str) -> str:
        return os.path.join(root, *parts)


_ADAPTERS: dict[str, type["BaseAdapter"]] = {}


def register(cls: type["BaseAdapter"]) -> type["BaseAdapter"]:
    """类装饰器：注册适配器。"""
    if cls.name:
        _ADAPTERS[cls.name] = cls
    return cls


def get_adapter(name: str) -> BaseAdapter:
    """按标识获取已注册适配器实例；未知则抛出 ``KeyError``。"""
    if name not in _ADAPTERS:
        raise KeyError(
            "未找到适配器 %r，已支持：%s" % (name, ", ".join(sorted(_ADAPTERS)) or "（无）")
        )
    return _ADAPTERS[name]()


def list_adapters() -> list[str]:
    return sorted(_ADAPTERS)
