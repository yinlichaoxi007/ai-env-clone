"""多工具适配器注册中心。"""

from .base import BaseAdapter, get_adapter, list_adapters, register
from .qoder import QoderAdapter
from .codebuddy import CodeBuddyAdapter
from .reasonix import ReasonixAdapter
from .dsh import DSHAdapter
from .workbuddy import WorkBuddyAdapter
from .trae_cn import TraeCnAdapter
from .trae_solo_cn import TraeSoloCnAdapter
from .zcode import ZCodeAdapter

# 导入各适配器模块以触发 @register 注册
# 注意：注册顺序决定 GUI 默认工具与下拉顺序（list_adapters()[0] 为默认，
# 且首次启动无偏好缓存时采用注册序第一个）。Qoder 为已实测主力工具，
# 保持先注册以维持其默认地位；CodeBuddy、Reasonix、DSH 依次排在之后，
# 新增的 WorkBuddy / TraeCode CN / TraeWork CN / ZCode 追加在末尾，
# 不打乱既有工具的默认与下拉顺序（老用户的使用习惯不变）。
__all__ = [
    "BaseAdapter",
    "CodeBuddyAdapter",
    "DSHAdapter",
    "QoderAdapter",
    "ReasonixAdapter",
    "TraeCnAdapter",
    "TraeSoloCnAdapter",
    "WorkBuddyAdapter",
    "ZCodeAdapter",
    "get_adapter",
    "list_adapters",
    "register",
]
