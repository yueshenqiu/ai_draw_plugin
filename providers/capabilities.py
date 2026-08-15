# -*- coding: utf-8 -*-
"""Provider 能力声明模块。

参照 video_generator_plugin/providers/capabilities.py 设计。
声明每个服务商支持的功能，便于运行时特性检测和参数校验。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Set


class ImageFeature(str, Enum):
    """图片生成功能特性"""
    TEXT2IMG = "text2img"
    IMG2IMG = "img2img"
    CHARACTER_REF = "character_ref"      # 角色参考
    STYLE_REF = "style_ref"              # 画风参考
    CHARACTER_STYLE_REF = "character_style_ref"  # 角色+画风参考
    ARTIST_PRESETS = "artist_presets"    # 风格预设（画师串）
    SELFIE_MODE = "selfie_mode"          # 自拍模式
    NSFW = "nsfw"                        # NSFW 内容


@dataclass
class ProviderCapabilities:
    """Provider 能力声明"""
    format: str                                    # 服务商标识
    display_name: str                              # 显示名称
    description: str                               # 描述
    features: Set[ImageFeature] = field(default_factory=set)  # 支持的功能
    max_steps: int = 28                            # 最大推理步数
    supported_samplers: List[str] = field(default_factory=list)  # 支持的采样器
    supported_sizes: List[str] = field(default_factory=list)     # 支持的尺寸预设
    # 以下安全边界追加在旧字段之后，保持原有位置参数构造 API 兼容。
    min_steps: int = 1                             # 最小推理步数
    min_dimension: int = 64                        # 单边最小像素
    max_dimension: int = 4096                      # 单边最大像素
    max_pixels: int = 16_777_216                   # 最大总像素（4096 x 4096）
    min_scale: float = 0.0                         # CFG scale 下限
    max_scale: float = 30.0                        # CFG scale 上限
    min_cfg_rescale: float = 0.0                   # CFG rescale 下限
    max_cfg_rescale: float = 1.0                   # CFG rescale 上限
    min_reference_strength: float = 0.0            # 参考强度下限
    max_reference_strength: float = 1.0            # 参考强度上限
    max_tokens: int = 1_000_000                    # 兼容接口最大输出预算
    max_image_bytes: int = 32 * 1024 * 1024        # 单张图片最大解码大小
    max_response_bytes: int = 48 * 1024 * 1024     # HTTP 响应体最大大小
    dimension_multiple: int = 1                    # 输出宽高必须是该值的整数倍
    enforce_supported_samplers: bool = False       # 是否严格拒绝声明列表外采样器


# ---- 各 Provider 能力声明 ----

BESTNAI_CAPABILITIES = ProviderCapabilities(
    format="bestnai",
    display_name="BestNAI / NovelAI 兼容",
    description="通过 OpenAI Chat Completions 兼容接口调用 NovelAI 图片生成",
    features={
        ImageFeature.TEXT2IMG,
        ImageFeature.IMG2IMG,
        ImageFeature.CHARACTER_REF,
        ImageFeature.STYLE_REF,
        ImageFeature.CHARACTER_STYLE_REF,
        ImageFeature.ARTIST_PRESETS,
        ImageFeature.SELFIE_MODE,
        ImageFeature.NSFW,
    },
    max_steps=28,
    supported_samplers=[
        "k_euler", "k_euler_ancestral",
        "k_dpmpp_2s_ancestral", "k_dpmpp_2m", "k_dpmpp_sde",
        "ddim", "plms",
    ],
    supported_sizes=["832x1216", "1216x832", "1024x1024"],
    dimension_multiple=64,
    enforce_supported_samplers=True,
)


YESNAI_CAPABILITIES = ProviderCapabilities(
    format="yesnai",
    display_name="YesNovelAI 原生",
    description="通过 YesNovelAI business-api 原生 NAI 端点调用 NovelAI 图片生成",
    features={
        ImageFeature.TEXT2IMG,
        ImageFeature.IMG2IMG,
        ImageFeature.CHARACTER_REF,
        ImageFeature.STYLE_REF,
        ImageFeature.ARTIST_PRESETS,
        ImageFeature.SELFIE_MODE,
        ImageFeature.NSFW,
    },
    max_steps=50,
    supported_samplers=[
        "k_euler", "k_euler_ancestral",
        "k_dpmpp_2s_ancestral", "k_dpmpp_2m", "k_dpmpp_sde",
        "ddim", "plms",
    ],
    supported_sizes=["832x1216", "1216x832", "1024x1024"],
    max_dimension=2048,
    max_pixels=3_145_728,
    dimension_multiple=64,
    enforce_supported_samplers=True,
)


PROVIDER_CAPABILITIES = {
    "bestnai": BESTNAI_CAPABILITIES,
    "novelai": BESTNAI_CAPABILITIES,  # 同 BestNAI
    "yesnai": YESNAI_CAPABILITIES,
}
