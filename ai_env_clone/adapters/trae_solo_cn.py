"""
TraeWork CN 适配器（安装目录名 ``TRAE SOLO CN``）。

> **命名口径**：安装目录叫 ``TRAE SOLO CN``，但产品自身向用户展示的名字是
> ``TraeWork CN``（``product.json`` 的 ``win32NameVersion``，注册表
> 「应用和功能」里也是 ``TraeWork CN (User)``）。本工具统一用**用户看得到的产品名**
> 展示，``name`` 仍保留 ``trae-solo-cn`` 以免破坏既有备份与偏好缓存。
> 同族的另一支：安装目录 ``Trae CN`` -> 产品名 ``TraeCode CN``。

TraeWork CN 与 TraeCode CN 属同一产品家族的两种形态，
目录布局完全同构：userData 在 ``%APPDATA%``、extensions 在 ``~/.<产品目录>``。

因此本适配器**复用** ``trae_cn.py`` 中的家族通用实现
（:func:`~ai_env_clone.adapters.trae_cn.build_family_items` /
:func:`~ai_env_clone.adapters.trae_cn.family_data_roots`），只替换：

- userData 目录名：``Trae CN`` → ``TRAE SOLO CN``
- extensions 目录名：``~/.trae-cn`` → ``~/.trae-solo-cn``
  （本机实测未安装独立扩展目录时，退回并标注「与 Trae CN 共用扩展目录」）
- 条目 key 前缀：``trae_solo_cn:``（与 Trae CN 的聚合前缀区分，互不干扰）
- 额外说明：SOLO 形态带沙箱虚拟机 ``ModularData/ai-agent/vm/``（本机实测 ≈2.1GB、
  7 万文件），属**程序运行态**，按统一策略不列入备份选项。

数据布局与「不列入备份」清单详见 ``trae_cn.py`` 的模块 docstring
（两个形态完全一致，不在此重复）。

> ⚠️ 与 Trae CN 相同：智能体会话内容是**产品侧加密**的（``database.db`` 非标准
> SQLite），本工具只能做整库不透明备份/还原，无法转换为其它软件的原生格式。
"""

from __future__ import annotations

import os

from ..core import BackupItem
from .base import BaseAdapter, register
from .trae_cn import (
    KEY_PREFIX_TRAE_SOLO_CN,
    build_family_items,
    extensions_dir,
    family_data_roots,
    user_data_dir,
)

#: 产品标识（userData 目录名）
TRAE_SOLO_PRODUCT = "TRAE SOLO CN"
#: 扩展目录名（``~/.trae-solo-cn``）
TRAE_SOLO_PRODUCT_DIR = "trae-solo-cn"
#: 家族共用扩展目录（本机实测 Trae SOLO CN 未创建独立扩展目录时的回退）
FAMILY_SHARED_PRODUCT_DIR = "trae-cn"


def _solo_ext_dir() -> str:
    """解析 Trae SOLO CN 实际使用的 extensions 目录。

    优先 ``~/.trae-solo-cn``；不存在但 ``~/.trae-cn``（家族共用）存在时退回后者
    ——避免 SOLO 侧完全不显示扩展目录，同时在识别状态区明确标注为「共用」。
    """
    own = extensions_dir(TRAE_SOLO_PRODUCT_DIR)
    if os.path.isdir(own):
        return own
    shared = extensions_dir(FAMILY_SHARED_PRODUCT_DIR)
    if os.path.isdir(shared):
        return shared
    return own  # 都不存在：返回本产品目录（条目会显示「未找到」，结构保持稳定）


@register
class TraeSoloCnAdapter(BaseAdapter):
    name = "trae-solo-cn"
    #: 用户可见的产品名（安装目录仍为 ``TRAE SOLO CN``）
    display_name = "TraeWork CN"

    #: 与 Trae CN 同族：AI Agent 会话记录集中在 ``ModularData/ai-agent/database.db``
    #: ⇒ 还原 = 整库覆盖，目标机库中原有的会话会被备份内容取代。
    RESTORE_LIBRARY_FILES: tuple[str, ...] = ("database.db",)

    #: 与 Trae CN 同族，压缩特性一致；SOLO 侧 ``snapshot/`` 与工作区元数据占比更高，
    #: other 档略微上调（详见 trae_cn.TraeCnAdapter.COMPRESS_RATIO 的说明）。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.18,
            "db": 0.87,
            "struct": 0.5,
            "binary": 0.99,
            "other": 0.6,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.85,
            "struct": 0.45,
            "binary": 0.99,
            "other": 0.55,
        },
    }

    def detect_root(self) -> str | None:
        """探测 Trae SOLO CN 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return os.path.expanduser("~")

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return os.path.expanduser("~")

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 Trae SOLO CN 各数据根目录信息（含沙箱虚拟机备注）。"""
        roots = family_data_roots(root, TRAE_SOLO_PRODUCT, TRAE_SOLO_PRODUCT_DIR, has_vm=True)
        # 标注扩展目录来源：独立还是与 Trae CN 共用
        own = extensions_dir(TRAE_SOLO_PRODUCT_DIR)
        for r in roots:
            if r["rel"].replace("\\", "/").endswith(".trae-cn") and not os.path.isdir(own):
                r["note"] = (r["note"] or "") + "（本机无 .trae-solo-cn，与 Trae CN 共用扩展目录）"
        return roots

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 Trae SOLO CN 备份条目（``root_dir`` 为公共根 ``~``）。

        ``current_uid`` 为兼容性参数（Trae 无 UID 拆分），忽略。
        """
        home = root_dir or os.path.expanduser("~")
        return build_family_items(
            home,
            user_data_dir(TRAE_SOLO_PRODUCT),
            _solo_ext_dir(),
            key_prefix=KEY_PREFIX_TRAE_SOLO_CN,
            has_vm=True,
        )

    def match_structure(self, names: "Sequence[str]") -> "tuple[bool, list[str]]":
        """结构指纹：归档内出现 ``TRAE SOLO CN/ModularData/ai-agent/database.db`` 即认作本工具。"""
        for n in names or []:
            j = n.replace("\\", "/")
            if j.endswith("TRAE SOLO CN/ModularData/ai-agent/database.db"):
                return True, []
        return False, ["未找到 TRAE SOLO CN/ModularData/ai-agent/database.db"]
