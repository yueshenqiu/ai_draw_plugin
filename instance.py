# -*- coding: utf-8 -*-
"""插件实例管理模块 — 用于在组件间共享插件实例。

完全参照 video_generator_plugin/instance.py 的线程安全单例模式。
"""

from typing import Optional, TYPE_CHECKING
from weakref import ref
import threading

if TYPE_CHECKING:
    from .plugin import AiDrawPlugin

_lock = threading.Lock()
_instance_ref = None


def get_plugin_instance() -> Optional["AiDrawPlugin"]:
    """获取插件实例。

    Returns:
        插件实例，如果未设置或已被回收则返回 None
    """
    global _instance_ref
    with _lock:
        if _instance_ref is None:
            return None
        instance = _instance_ref()
        if instance is None:
            _instance_ref = None
        return instance


def set_plugin_instance(plugin: "AiDrawPlugin") -> None:
    """设置插件实例（弱引用）。

    Args:
        plugin: 插件实例
    """
    global _instance_ref
    with _lock:
        _instance_ref = ref(plugin)


def clear_plugin_instance() -> None:
    """清理插件实例（用于热重载/测试/卸载）。"""
    global _instance_ref
    with _lock:
        _instance_ref = None


def is_plugin_ready() -> bool:
    """检查插件是否就绪。"""
    return get_plugin_instance() is not None
