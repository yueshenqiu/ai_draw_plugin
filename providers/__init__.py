# -*- coding: utf-8 -*-
"""图片生成 Provider 层 — 策略模式 + 懒加载注册表。

参照 video_generator_plugin/providers/__init__.py 的设计。
"""

from typing import Type, Optional
from .base import BaseImageProvider
from .capabilities import ProviderCapabilities, PROVIDER_CAPABILITIES

# Provider 注册表：format → (module_path, class_name)
PROVIDER_REGISTRY = {
    "bestnai": (".bestnai", "BestNAIProvider"),
    "novelai": (".bestnai", "BestNAIProvider"),  # 别名，同一实现
}

_provider_cache: dict = {}


def get_provider_class(format_name: str) -> Optional[Type[BaseImageProvider]]:
    """根据 format 名称获取 Provider 类（带懒加载缓存）。

    Args:
        format_name: 服务商标识，如 'bestnai'、'novelai'

    Returns:
        Provider 类，未知格式返回 None
    """
    if not format_name:
        return None

    fmt = str(format_name).strip().lower()
    if fmt in _provider_cache:
        return _provider_cache[fmt]

    entry = PROVIDER_REGISTRY.get(fmt)
    if entry is None:
        return None

    module_path, class_name = entry
    import importlib
    try:
        mod = importlib.import_module(module_path, package=__package__)
        cls = getattr(mod, class_name)
        _provider_cache[fmt] = cls
        return cls
    except Exception:
        return None


def get_capabilities(format_name: str) -> Optional[ProviderCapabilities]:
    """获取指定 Provider 的能力声明。

    Args:
        format_name: 服务商标识

    Returns:
        ProviderCapabilities 或 None
    """
    fmt = str(format_name).strip().lower()
    return PROVIDER_CAPABILITIES.get(fmt)


__all__ = [
    "BaseImageProvider",
    "ProviderCapabilities",
    "PROVIDER_CAPABILITIES",
    "PROVIDER_REGISTRY",
    "get_provider_class",
    "get_capabilities",
]
