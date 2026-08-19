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

    #: 该工具内置的压缩经验系数表（档位 -> 类别 -> 压缩后/源 占比）。
    #: 各适配器应**按自身数据结构单独定义并维护**，不要共用一个全局表。
    #: 这里给一份与通用兜底一致的默认表，未重写时也能跑（精度较差）。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.17,
            "db": 0.61,
            "struct": 0.48,
            "binary": 0.99,
            "other": 0.17,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.59,
            "struct": 0.46,
            "binary": 0.99,
            "other": 0.13,
        },
    }

    #: 是否支持"按真实备份反算的自动校准"。
    #: - True （默认，如 Qoder）：备份成功后把实测压缩率写缓存，后续估算优先用实测率，
    #:   没有校准记录时回退到 ``COMPRESS_RATIO`` 经验系数。
    #: - False：该适配器不参与自动校准，估算**永远只用内置经验系数** ``COMPRESS_RATIO``，
    #:   既不会读取也不会写入校准文件（便于尚无校准数据或不希望产生缓存的工具）。
    supports_calibration: bool = True

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
    # 数据根识别状态（GUI 识别状态区展示，可重写）
    # ------------------------------------------------------------------ #
    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """
        返回该工具在 ``root``（公共根，通常为用户主目录）下探测到的各数据根目录信息。

        用于 GUI「识别状态」区逐行展示「在数据目录下发现的具体根目录名称及状态」，
        不改变原有单路径数据目录模型，仅做展示增强。

        :param root: 公共根目录；``None`` 时由适配器自行决定（通常为 ``~``）。
        :return: 字典列表，每项含：
            - ``rel``    : 相对 ``root`` 的完整路径（含每层，如 ``AppData\\Local\\CodeBuddyExtension``）
            - ``exists`` : 该根目录是否存在（布尔）
            - ``note``   : 附加说明（如「含 N 个用户」），无则空串
        默认返回空列表（适配器应重写）。
        """

        return []

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
        items: Sequence[BackupItem] | None = None,
        progress=None,
        max_file_mb: float | None = 200.0,
        compresslevel: int = 6,
    ) -> dict:
        if items is None:
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
        # 自动注入适配器声明的还原修正逻辑（跨电脑迁移用），
        # 调用方未显式覆盖时才填入默认值，便于 CLI / 测试按需覆盖。
        if "path_rewrite" not in kw:
            kw["path_rewrite"] = self.restore_path_rewrite()
        if "restore_post_hook" not in kw:
            kw["restore_post_hook"] = self.restore_post_hook()
        if "restore_index_merge" not in kw:
            kw["restore_index_merge"] = self.restore_index_merge()
        if "restore_index_merge_paths" not in kw:
            kw["restore_index_merge_paths"] = self.restore_index_merge_paths()
        return import_backup(zip_path, root, progress=progress, **kw)

    # ------------------------------------------------------------------ #
    # 还原路径重写（跨电脑迁移用，可重写）
    # ------------------------------------------------------------------ #
    def restore_path_rewrite(self) -> "Callable[[str], str] | None":
        """
        返回一个「归档内相对路径 -> 还原目标相对路径」的重写函数；
        用于跨电脑还原时把源机器特有的标识（如用户 UUID）重映射到
        本机当前用户，避免数据落到「死目录」里而界面读不到。

        默认返回 ``None``（不做任何重写）。
        """
        return None

    def preview_path_rewrite(self, entries: "Sequence[str]") -> "dict | None":
        """
        预览跨电脑还原是否会发生路径重映射，供还原前向用户提示。

        默认实现返回 ``{"will_rewrite": False, "source_uids": [], "current_uid": None}``，
        即基类不做任何重映射、也不提示。有登录用户 UUID 概念的适配器（如 CodeBuddy）
        应重写本方法以检测源机器 UUID 与当前用户是否不同。
        """
        return {"will_rewrite": False, "source_uids": [], "current_uid": None}

    def restore_index_merge_paths(self) -> "Sequence[str] | None":
        """
        返回还原时需要「合并而非覆盖」的归档内相对路径**后缀**集合。

        典型场景：某工具的工作区名存储在一个**全局索引文件**（如 DSH 的
        ``storages/workspace.json``），直接覆盖写入会抹掉目标机器原本的
        其他工作区，使这些工作区的会话在界面里显示为 ``ungrouped``。

        声明的值为**后缀片段**（如 ``"storages/workspace.json"``）：归档内成员名相对
        公共根带根占位前缀（如 ``C__Users_x/.dsh/storages/workspace.json``），core 以
        ``成员名.endswith("/" + 片段)`` 判定命中，故**不要**写完整绝对路径。命中后 core
        先读取目标机器已有内容，再调用 :meth:`restore_index_merge` 合并后落盘。

        默认返回 ``None``（不启用合并，走普通覆盖）。
        """
        return None

    def restore_index_merge(self) -> "Callable[[str, bytes, bytes], bytes] | None":
        """
        返回「全局索引合并」回调 ``callback(relpath, source_bytes, original_bytes) -> merged_bytes``：

        - ``relpath``：归档内相对路径（经 ``path_rewrite`` 重写后的目标相对路径）；
        - ``source_bytes``：备份包里该文件的原始字节；
        - ``original_bytes``：还原前目标机器上该文件的已有字节（不存在则为 ``b""``）；
        - 返回：应写入目标的合并后字节。

        仅当 ``relpath`` 出现在 :meth:`restore_index_merge_paths` 中时由 core 调用。
        适配器应在此把源索引与本机已有索引**合并**（保留本机原有的全部工作区 /
        会话），而非简单覆盖。默认返回 ``None``（不做合并）。
        """
        return None

    def restore_post_hook(self) -> "Callable[[str, list[str]], None] | None":
        """
        还原落盘全部完成后的回调工厂：返回一个 ``callback(root_real, restored_targets)``。

        - ``root_real``：经 ``os.path.realpath`` 解析的目标根目录。
        - ``restored_targets``：本次实际写入的**目标文件绝对路径**列表。

        用于末端修补「被整体覆盖的全局索引文件」——典型场景：某工具的工作区名
        存储在一个全局索引（如 DSH 的 ``storages/workspace.json``），直接覆盖
        会抹掉目标机器原本的其他工作区，使它们显示为 ``ungrouped``。适配器
        可在此回调里把源机器索引与目标机器已有索引**合并**而非覆盖。

        默认返回 ``None``（不挂载任何后处理）；需要合并索引的适配器应重写本方法。
        """
        return None

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
    """按**注册顺序**返回全部已注册适配器标识（`@register` 触发顺序）。

    注册顺序即 ``adapters/__init__.py`` 的导入顺序，也是 GUI 默认工具与
    下拉列表的顺序（第一个为默认工具）。与排序无关，刻意保持注册序，
    让「默认工具」由适配器注册顺序决定、可预测。
    """
    return list(_ADAPTERS)
