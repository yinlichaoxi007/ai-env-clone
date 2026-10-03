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

__all__ = ["BaseAdapter", "MULTI_MACHINE_CYCLE_HINT", "get_adapter"]


#: 「多机串行使用」的正确姿势（与 README「覆盖 vs 融合」同源）。
#: 界面在**备份完成**与**还原前后**都会复述一次：这是本工具唯一安全的多机用法，
#: 且必须让用户在「刚拿到包」「正要还原」这两个决策点上看到，而不是只写在文档里。
MULTI_MACHINE_CYCLE_HINT = (
    "多台电脑请「串行」使用同一个工作区/会话：换机前先在旧电脑备份，"
    "到新电脑先还原再开始使用，之后如此循环；不要两台电脑交叉使用后互相还原"
    "（覆盖会丢掉其中一侧的增量）。"
)


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

    #: 以「整库覆盖」语义落盘的**核心库文件**（相对数据根的路径片段，如 ``workbuddy.db``）。
    #:
    #: 非空即表示本工具的会话/记录集中在**单个库文件**里（SQLite 等），「还原」= 把整库
    #: 换掉 ⇒ 目标机上**库内原有的记录会被备份内容取代**（磁盘上不在这几个库里的内容不受
    #: 影响）。界面据此对这类工具用更重的措辞提示「整库覆盖」，并交代正确的多机用法。
    #:
    #: 留空 = 数据按文件/目录落盘：还原只补入或更新备份包里的文件，目标机上其他数据不受
    #: 影响（例：DSH 的全局索引走并集合并、CodeBuddy 按 UUID 重映射路径）。
    #:
    #: ⚠ 这只是**声明**，不改变还原行为（本工具不做库内合并，见 README「覆盖 vs 融合」的
    #: 取舍说明）；声明与行为不符会让提示失真，改还原实现时务必同步改这里。
    RESTORE_LIBRARY_FILES: tuple[str, ...] = ()

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
        generated_files, generated_meta = self.export_generated(items, root)
        return export_backup(
            zip_path,
            items,
            root,
            tool_name=self.name,
            progress=progress,
            max_file_mb=max_file_mb,
            compresslevel=compresslevel,
            export_transform=self.export_transform(),
            export_transform_paths=self.export_transform_paths(),
            extra_files=generated_files,
            extra_meta=generated_meta,
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

    def export_transform_paths(self) -> "Sequence[str] | None":
        """
        返回导出时需「脱敏变换而非原样入库」的归档内相对路径**后缀**集合。

        典型场景：某配置文件可能含明文敏感凭证（如 CodeBuddy 的 ``models.json`` 里
        每个自定义模型可能带 ``apiKey``、``token`` 或其他私有凭证）。直接把明文凭证
        打包进备份 zip 存在泄露风险（zip 可能同步到外部/被他人获取），故在导出阶段把
        敏感字段替换为占位符，
        存档不含明文。匹配采用**后缀判定**（与 :meth:`restore_index_merge_paths` 一致）：
        归档内成员名相对公共根带根占位前缀（如 ``C__Users_x/.codebuddy/models.json``），
        只要成员名以 ``/`` + 声明片段结尾即命中，避免写死根前缀。

        默认返回 ``None``（不做变换，原样入库）。
        """
        return None

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """
        返回「导出脱敏」回调 ``callback(relpath, source_bytes) -> transformed_bytes``：

        - ``relpath``：归档内相对路径（已规范化正斜杠）；
        - ``source_bytes``：待入库文件的原始字节；
        - 返回：应写入备份包的改写后字节（如把敏感凭证字段替换为占位符）。

        仅当 ``relpath`` 出现在 :meth:`export_transform_paths` 中时由 core 调用。
        适配器应在此抹掉明文敏感字段而非简单跳过（跳过会导致恢复后配置缺失、模型不可用）。
        默认返回 ``None``（不做变换）。
        """
        return None

    def export_generated(self, items: "Sequence[BackupItem]", root: str):
        """返回 ``(额外生成文件, 额外元信息)``，默认 ``(None, None)``。

        - **额外生成文件**：``{绝对落点路径: 字节内容}``。这些文件**不在磁盘上**，
          由适配器在导出时按需构造。core 会按「相对 ``root`` 的路径」写入备份包，
          因此**还原时会自动落回原位**，不需要任何额外的还原逻辑。
          典型用途：把「会话 -> 它原本所属的工程工作区路径」落成一份 JSON 随包携带
          —— 这类映射往往只存在于**源机器**的运行态里（如 IDE 的「已打开文件夹」
          记录），跨机还原后若没有它，就只能给用户一个工具默认落点，连提示
          「这条会话原本属于哪个工程」都做不到。
        - **额外元信息**：合并进 manifest 的 ``extra`` 字段，供备份详情区展示
          （如「本包记录了 N 条会话的原始工作区路径，其中 M 条已确定」）。

        必须**尽力而为**：任何异常都不应让备份失败，探测不到就返回 ``(None, None)``
        或只返回能拿到的那部分。
        """
        return None, None

    # ------------------------------------------------------------------ #
    # 还原语义（界面**按适配器区分措辞**用，可重写）
    # ------------------------------------------------------------------ #
    @property
    def restore_replaces_library(self) -> bool:
        """本工具还原是否会**整体替换核心库文件**。

        ``True``：会话/记录集中在一个库文件里，还原 = 整库换掉，目标机库里原有的
        记录会被备份内容取代。``False``：按文件/目录落盘，只补入或更新文件。

        判定只依赖 :attr:`RESTORE_LIBRARY_FILES`，故适配器只需声明库文件名即可，
        不必两处维护。
        """
        return bool(self.RESTORE_LIBRARY_FILES)

    def restore_overwrite_notice(self) -> str:
        """还原**之前**的说明：本次还原会对目标机已有数据造成什么影响。

        供「确认还原」弹窗按适配器区分措辞 —— 对整库覆盖型工具必须说清「库里原有
        记录会被替换」，否则用户会以为和文件落盘一样「只增不删」而误还原。
        面向用户的文案不写 Markdown 标记（Tk 不渲染 ``**``），如需强调用「」。
        """
        if self.restore_replaces_library:
            names = "、".join(self.RESTORE_LIBRARY_FILES)
            return (
                "⚠ 本工具的会话与记录集中存放在库文件中（%s）。\n"
                "还原会把整个库替换成备份里的版本：目标机上库里原有的会话会被备份内容"
                "取代，备份包里没有的本机记录不会保留。\n"
                "若这台电脑上也有需要留下的数据，请先在这里导出一份备份再还原"
                "（还原时还会自动生成回滚快照，可回退到还原前）。" % names
            )
        return (
            "本工具的数据按文件/目录存放：还原只补入或更新备份包里的文件，"
            "目标机上备份包之外的会话与数据不受影响。"
        )

    def restore_result_note(self) -> str:
        """还原**之后**的说明：一句话交代目标机已有数据受到了什么影响。

        供「还原成功」提示按适配器区分措辞，替换原先对所有工具都写「会话为新增、
        不会覆盖已有会话」的统一说法 —— 那句话对整库覆盖型工具是不成立的。
        """
        if self.restore_replaces_library:
            names = "、".join(self.RESTORE_LIBRARY_FILES)
            return (
                "⚠ 本次为整库覆盖：目标机的库文件（%s）已整体换成备份里的版本，"
                "该库中原有的本机会话已被替换；如需找回，可用本次生成的回滚快照还原。"
                % names
            )
        return "按文件落盘：包内文件已补入或更新，目标机上包之外的其他数据未受影响。"

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
