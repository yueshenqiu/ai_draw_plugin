# -*- coding: utf-8 -*-
"""图片生成 Provider 抽象基类。

参照 video_generator_plugin/providers/base.py 设计。
所有图片生成服务商需继承此类并实现 generate 方法。
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple, Optional


class BaseImageProvider(ABC):
    """图片生成 Provider 抽象基类。

    每个 Provider 对应一种服务商（BestNAI、NovelAI、OpenAI DALL-E 等）。
    """

    def __init__(self, logger, log_prefix: str = ""):
        self._logger = logger
        self.log_prefix = log_prefix

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: Optional[str] = None,
        ref_image: str = "",
        ref_mode: str = "",
    ) -> Tuple[bool, str]:
        """生成图片。

        Args:
            prompt: 正向提示词（英文 tag）
            model_config: 模型配置字典（包含 base_url、api_key、model 及所有生成参数）
            size: 图片尺寸（如 "832x1216" 或 "竖图"）
            ref_image: 参考图片 base64（用于图生图/角色参考/画风参考）
            ref_mode: 参考模式（i2i / character / style / character&style）

        Returns:
            Tuple[bool, str]: (是否成功, 图片数据或错误信息)
        """
        ...

    def validate_config(self, model_config: Dict[str, Any]) -> bool:
        """验证模型配置是否完整。

        Args:
            model_config: 模型配置字典

        Returns:
            bool: 配置是否有效
        """
        if not isinstance(model_config, dict):
            return False
        base_url = (model_config.get("base_url") or "").strip()
        model = (model_config.get("model") or "").strip()
        return bool(base_url and model)

    def resolve_proxy_mode(self, model_config: Dict[str, Any]) -> str:
        """解析代理模式。

        Args:
            model_config: 模型配置字典

        Returns:
            str: 'auto' | 'inherit' | 'direct'
        """
        value = model_config.get("proxy_mode") or model_config.get("nai_proxy_mode") or "auto"
        return str(value).strip().lower() or "auto"
