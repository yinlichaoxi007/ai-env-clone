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
    # 结构指纹（回退校验用，可重写）
    # ------------------------------------------------------------------ #
    def match_structure(self, names: Sequence[str]) -> tuple[bool, list[str]]:
        """
        判断 zip 内条目名列表是否匹配本工具的数据结构（用于**缺 manifest 时**
        回退识别类型 / 严格模式下二次校验）。

        :param names: 压缩包内所有条目名（含目录项，正斜杠分隔）。
        :return: ``(是否匹配, 缺失项说明列表)``。

        默认实现返回 ``(False, ["未实现结构指纹"])`` —— 即适配器未重写时，
        任何需要回退的包都被判为「不匹配」，等价于「只严格匹配 manifest，
        不允许无清单回退」。各适配器应按自身数据结构重写。
        """
        return False, ["未实现结构指纹"]

    # ------------------------------------------------------------------ #
    # 通用实现（多数适配器无需重写）
    # ------------------------------------------------------------------ #
    def export(
        self,
        zip_path: str,
        root: str,
        progress=None,
        max_file_mb: float | None = 200.0,
        compresslevel: int = 6,
    ) -> dict:
        items = self.build_items(root)
        return export_backup(
            zip_path,
            items,
            root,
            tool_name=self.name,
            progress=progress,
            max_file_mb=max_file_mb,
            compresslevel=compresslevel,
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
