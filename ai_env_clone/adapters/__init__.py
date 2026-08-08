"""多工具适配器注册中心。"""

from .base import BaseAdapter, get_adapter, list_adapters, register
from .qoder import QoderAdapter
from .codebuddy import CodeBuddyAdapter

# 导入各适配器模块以触发 @register 注册
# 注意：注册顺序决定 GUI 默认工具（list_adapters()[0]）。Qoder 为已实测主力工具，
# 保持先注册以维持其默认地位；CodeBuddy 为新增工具，排在之后。
__all__ = [
    "BaseAdapter",
    "CodeBuddyAdapter",
    "QoderAdapter",
    "get_adapter",
    "list_adapters",
    "register",
]
