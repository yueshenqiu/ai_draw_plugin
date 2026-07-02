# -*- coding: utf-8 -*-
"""
插件内部常量。

包括：
- 消息标记常量
- 模型格式枚举
- 模型别名映射
- 尺寸映射
- BestNAI 模型 ID 列表
"""

# ---- 消息标记 ----

# 用于标记"本插件发送的图片消息"的 display_message
AI_DRAW_IMAGE_DISPLAY_MARKER = "[ai_draw_plugin:image]"

# Action 图片展示文案前缀
AI_DRAW_IMAGE_DISPLAY_PREFIX = "[AI绘图:"
AI_DRAW_IMAGE_DISPLAY_FALLBACK = "[AI绘图]"


# ---- 模型格式（Provider 类型） ----

MODEL_FORMATS = {
    "bestnai": "BestNAI / NovelAI 兼容接口",
    "novelai": "NovelAI 官方接口",
    "openai": "OpenAI DALL-E（预留）",
}


# ---- 旧版模型缩写映射（用于 /ad w 快速切换） ----

MODEL_MAPPINGS = {
    "3": "nai-diffusion-3",
    "f3": "nai-diffusion-3-furry",
    "4": "nai-diffusion-4-full",
    "4.5": "nai-diffusion-4-5-full",
}


# ---- 尺寸别名映射 ----

SIZE_MAPPINGS = {
    "竖": "832x1216", "竖图": "832x1216",
    "横": "1216x832", "横图": "1216x832",
    "方": "1024x1024", "方图": "1024x1024",
    "h": "1216x832", "v": "832x1216", "s": "1024x1024",
}


# ---- BestNAI 模型 ID 列表 ----

BESTNAI_MODEL_IDS = [
    "nai-diffusion-3",
    "nai-diffusion-3-furry",
    "nai-diffusion-4-curated",
    "nai-diffusion-4-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-4-5-full",
]
