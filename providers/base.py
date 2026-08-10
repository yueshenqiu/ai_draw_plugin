# -*- coding: utf-8 -*-
"""图片生成 Provider 抽象基类。

参照 video_generator_plugin/providers/base.py 设计。
所有图片生成服务商需继承此类并实现 generate 方法。
"""

from abc import ABC, abstractmethod
import base64
import binascii
import io
import json
import math
from typing import Dict, Any, Tuple, Optional, Set
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError


class ResponseLimitError(ValueError):
    """远端响应超过 Provider 允许的安全上限。"""


class BaseImageProvider(ABC):
    """图片生成 Provider 抽象基类。

    每个 Provider 对应一种服务商（BestNAI、NovelAI、OpenAI DALL-E 等）。
    """

    # 子类覆写：config 中 endpoint 留空时，走该 Provider 的内置默认路径
    default_endpoint: str = ""

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
        base_url = model_config.get("base_url")
        model = model_config.get("model")
        if not isinstance(base_url, str) or not isinstance(model, str):
            return False
        base_url = base_url.strip()
        model = model.strip()
        if not base_url or not model or any(ord(char) < 32 for char in base_url):
            return False
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
        except ValueError:
            return False
        return bool(
            parsed.scheme.lower() in {"http", "https"}
            and hostname
            and not parsed.username
            and not parsed.password
        )

    def resolve_proxy_mode(self, model_config: Dict[str, Any]) -> str:
        """解析代理模式。

        Args:
            model_config: 模型配置字典

        Returns:
            str: 'auto' | 'inherit' | 'direct'
        """
        value = model_config.get("proxy_mode") or model_config.get("nai_proxy_mode") or "auto"
        mode = str(value).strip().lower() or "auto"
        if mode not in {"auto", "inherit", "direct"}:
            self._logger.warning(
                f"{self.log_prefix} 未知代理模式 {mode!r}，已回退 auto"
            )
            return "auto"
        return mode

    def bounded_float(
        self,
        value: Any,
        *,
        default: Optional[float],
        minimum: float,
        maximum: float,
        name: str,
    ) -> Optional[float]:
        """将配置值转换为有限浮点数，并夹在安全区间内。"""
        if value is None or value == "":
            return default
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self._logger.warning(
                f"{self.log_prefix} 参数 {name}={value!r} 非法，已使用默认值 {default!r}"
            )
            return default
        bounded = min(max(number, minimum), maximum)
        if bounded != number:
            self._logger.warning(
                f"{self.log_prefix} 参数 {name}={number!r} 超出范围 "
                f"[{minimum}, {maximum}]，已调整为 {bounded}"
            )
        return bounded

    def bounded_int(
        self,
        value: Any,
        *,
        default: int,
        minimum: int,
        maximum: int,
        name: str,
    ) -> int:
        """将配置值转换为有限整数，并夹在安全区间内。"""
        if value is None or value == "":
            return default
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if not math.isfinite(number) or not number.is_integer():
                raise ValueError
            integer = int(number)
        except (TypeError, ValueError, OverflowError):
            self._logger.warning(
                f"{self.log_prefix} 参数 {name}={value!r} 非法，已使用默认值 {default}"
            )
            return default
        bounded = min(max(integer, minimum), maximum)
        if bounded != integer:
            self._logger.warning(
                f"{self.log_prefix} 参数 {name}={integer!r} 超出范围 "
                f"[{minimum}, {maximum}]，已调整为 {bounded}"
            )
        return bounded

    @staticmethod
    def validate_dimensions(
        width: int,
        height: int,
        capabilities: Any,
        *,
        require_multiple: bool = True,
    ) -> None:
        """按能力声明校验请求尺寸，失败时抛出可展示给用户的 ValueError。"""
        if isinstance(width, bool) or isinstance(height, bool):
            raise ValueError("图片尺寸必须是整数")
        try:
            width = int(width)
            height = int(height)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("图片尺寸必须是整数") from exc
        if not (
            capabilities.min_dimension <= width <= capabilities.max_dimension
            and capabilities.min_dimension <= height <= capabilities.max_dimension
        ):
            raise ValueError(
                f"图片尺寸需在 {capabilities.min_dimension}~"
                f"{capabilities.max_dimension} 像素之间"
            )
        if width * height > capabilities.max_pixels:
            raise ValueError(
                f"图片总像素不能超过 {capabilities.max_pixels:,}"
            )
        dimension_multiple = int(getattr(capabilities, "dimension_multiple", 1) or 1)
        if require_multiple and dimension_multiple > 1 and (
            width % dimension_multiple or height % dimension_multiple
        ):
            raise ValueError(
                f"图片宽高必须是 {dimension_multiple} 像素的整数倍"
            )

    @staticmethod
    def validate_sampler(sampler: Any, capabilities: Any) -> str:
        """规范化采样器；仅在能力声明要求时严格限制列表。"""
        if not isinstance(sampler, str) or not sampler.strip():
            raise ValueError("采样器必须是非空字符串")
        normalized = sampler.strip()
        supported = {
            str(item).strip().lower(): str(item).strip()
            for item in (getattr(capabilities, "supported_samplers", None) or [])
            if str(item).strip()
        }
        if (
            getattr(capabilities, "enforce_supported_samplers", False)
            and supported
            and normalized.lower() not in supported
        ):
            raise ValueError(f"当前服务商不支持采样器: {normalized}")
        return supported.get(normalized.lower(), normalized)

    def filter_extra_params(
        self,
        extra_params: Any,
        reserved_fields: Set[str],
        provider_name: str,
    ) -> Dict[str, Any]:
        """过滤扩展参数，防止其覆盖核心字段或注入非法 JSON 数值。"""
        if not extra_params:
            return {}
        if not isinstance(extra_params, dict):
            self._logger.warning(
                f"{self.log_prefix} ({provider_name}) extra_params 必须是对象，已忽略"
            )
            return {}

        reserved = {str(key).lower() for key in reserved_fields}
        filtered: Dict[str, Any] = {}
        for key, value in extra_params.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                self._logger.warning(
                    f"{self.log_prefix} ({provider_name}) 忽略非法扩展参数名 {key!r}"
                )
                continue
            if key.lower() in reserved:
                self._logger.warning(
                    f"{self.log_prefix} ({provider_name}) extra_params 不允许覆盖保留字段 {key!r}"
                )
                continue
            if value is None or (isinstance(value, str) and value == ""):
                continue
            if not self._is_safe_json_value(value):
                self._logger.warning(
                    f"{self.log_prefix} ({provider_name}) 扩展参数 {key!r} 含非法或过大值，已忽略"
                )
                continue
            filtered[key] = value

        try:
            encoded = json.dumps(filtered, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeError):
            self._logger.warning(
                f"{self.log_prefix} ({provider_name}) extra_params 无法序列化，已忽略"
            )
            return {}
        if len(encoded) > 64 * 1024:
            self._logger.warning(
                f"{self.log_prefix} ({provider_name}) extra_params 超过 64 KiB，已忽略"
            )
            return {}
        return filtered

    @classmethod
    def _is_safe_json_value(cls, value: Any, depth: int = 0) -> bool:
        if depth > 6:
            return False
        if value is None or isinstance(value, (bool, int)):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, str):
            return len(value) <= 32 * 1024
        if isinstance(value, (list, tuple)):
            return len(value) <= 256 and all(
                cls._is_safe_json_value(item, depth + 1) for item in value
            )
        if isinstance(value, dict):
            return len(value) <= 256 and all(
                isinstance(key, str)
                and len(key) <= 128
                and cls._is_safe_json_value(item, depth + 1)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def validate_http_url(url: str, *, max_length: int = 4096) -> bool:
        """仅接受不含凭据的 HTTP(S) URL。"""
        if not isinstance(url, str) or not url or len(url) > max_length:
            return False
        if any(ord(char) < 32 for char in url):
            return False
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError:
            return False
        return bool(
            parsed.scheme.lower() in {"http", "https"}
            and hostname
            and not parsed.username
            and not parsed.password
        )

    def normalize_and_validate_base64_image(
        self,
        image_data: str,
        *,
        capabilities: Any,
        field_name: str = "图片",
    ) -> Tuple[bool, str, int]:
        """严格解码并验证图片，成功时返回规范化 base64 与字节数。"""
        if not isinstance(image_data, str) or not image_data:
            return False, f"{field_name}数据为空", 0

        payload = image_data.strip()
        if payload.startswith("base64://"):
            payload = payload[len("base64://"):]
        elif payload.startswith("data:"):
            header, separator, payload = payload.partition(",")
            lower_header = header.lower()
            if (
                not separator
                or not lower_header.startswith("data:image/")
                or ";base64" not in lower_header
            ):
                return False, f"{field_name} data URI 格式无效", 0

        payload = "".join(payload.split())
        max_encoded_length = ((capabilities.max_image_bytes + 2) // 3) * 4 + 4
        if not payload or len(payload) > max_encoded_length:
            return False, f"{field_name}超过 {capabilities.max_image_bytes // (1024 * 1024)} MiB 限制", 0

        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            return False, f"{field_name} base64 解码失败", 0

        valid, error = self.validate_image_bytes(
            raw,
            capabilities=capabilities,
            field_name=field_name,
        )
        if not valid:
            return False, error, 0
        return True, payload, len(raw)

    @staticmethod
    def validate_image_bytes(
        raw: bytes,
        *,
        capabilities: Any,
        field_name: str = "图片",
    ) -> Tuple[bool, str]:
        """验证图片格式、完整性、解码大小和像素数量。"""
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return False, f"{field_name}数据为空"
        if len(raw) > capabilities.max_image_bytes:
            return False, f"{field_name}超过 {capabilities.max_image_bytes // (1024 * 1024)} MiB 限制"

        image_bytes = bytes(raw)
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in {"PNG", "JPEG", "WEBP"}:
                    return False, f"{field_name}格式仅支持 PNG、JPEG 或 WebP"
                # 输入/返回图片不要求宽高是生成端的步进倍数；该约束只用于请求尺寸。
                BaseImageProvider.validate_dimensions(
                    width, height, capabilities, require_multiple=False,
                )
                image.verify()
            # verify() 不解码像素；重新打开并 load()，拒绝截断或损坏的文件。
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            return False, f"{field_name}不是完整有效的图片: {str(exc)[:80]}"
        return True, ""
