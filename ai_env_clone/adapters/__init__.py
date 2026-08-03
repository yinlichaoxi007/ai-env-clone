"""多工具适配器注册中心。"""

from .base import BaseAdapter, get_adapter, list_adapters, register
from .qoder import QoderAdapter

# 导入各适配器模块以触发 @register 注册
__all__ = [
    "BaseAdapter",
    "QoderAdapter",
    "get_adapter",
    "list_adapters",
    "register",
]
