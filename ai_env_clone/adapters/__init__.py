"""多工具适配器注册中心。"""

from .base import BaseAdapter, get_adapter, list_adapters, register
from .qoder import QoderAdapter
from .codebuddy import CodeBuddyAdapter
from .reasonix import ReasonixAdapter
from .dsh import DSHAdapter

# 导入各适配器模块以触发 @register 注册
# 注意：注册顺序决定 GUI 默认工具与下拉顺序（list_adapters()[0] 为默认，
# 且首次启动无偏好缓存时采用注册序第一个）。Qoder 为已实测主力工具，
# 保持先注册以维持其默认地位；CodeBuddy、Reasonix、DSH 依次排在之后。
__all__ = [
    "BaseAdapter",
    "CodeBuddyAdapter",
    "DSHAdapter",
    "QoderAdapter",
    "ReasonixAdapter",
    "get_adapter",
    "list_adapters",
    "register",
]
