# -*- coding: utf-8 -*-
"""自拍场景增强：将用户的中文活动描述 → 英文 SD 标签（action / environment / expression / lighting）。

参照 mais_art_journal 的 scene_action_llm.py，适配本插件配置体系。
用法：
1. generate_scene_tags() — LLM 生成（主路径）
2. get_scene_fallback()   — 确定性映射（兜底）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from .prompt_engine import call_custom_llm_api, has_custom_api_config

_logger = logging.getLogger("ai_draw_plugin")


# ================================================================
# 确定性映射（LLM 失败时兜底）
# ================================================================

# 活动关键词 → 动作 / 环境 / 表情 / 光线
_ACTIVITY_SCENE_MAP: Dict[str, Dict[str, str]] = {
    "sleeping": {
        "action": "lying down, hugging pillow, cozy, sleeping pose",
        "environment": "bedroom, dim lighting, cozy atmosphere, bed, blankets",
        "expression": "peaceful expression, closed eyes, sleeping face",
        "lighting": "dim warm light, night lamp, soft shadows",
    },
    "waking_up": {
        "action": "stretching, yawning, messy hair, sitting up in bed",
        "environment": "bedroom, morning light, curtains, warm sunlight through window",
        "expression": "drowsy expression, half-open eyes, gentle sleepy smile",
        "lighting": "soft morning light, golden hour, warm sunlight",
    },
    "eating": {
        "action": "holding chopsticks, eating, enjoying meal",
        "environment": "dining room, table setting, warm interior",
        "expression": "happy expression, enjoying food, content smile",
        "lighting": "warm indoor lighting, cozy atmosphere",
    },
    "working": {
        "action": "typing on laptop, focused, sitting at desk",
        "environment": "office desk, computer screen, modern workspace",
        "expression": "focused expression, serious, concentrated",
        "lighting": "office lighting, even illumination, screen glow",
    },
    "studying": {
        "action": "holding book, reading, writing notes",
        "environment": "library, bookshelves, desk lamp, study room",
        "expression": "focused, thoughtful expression, concentrated",
        "lighting": "desk lamp, focused light, warm study atmosphere",
    },
    "exercising": {
        "action": "stretching, athletic pose, holding water bottle",
        "environment": "gym, fitness equipment, bright space",
        "expression": "energetic expression, determined, active",
        "lighting": "bright natural light, gym lighting",
    },
    "relaxing": {
        "action": "lying on couch, relaxed, listening to music",
        "environment": "living room, sofa, afternoon sun, cozy home",
        "expression": "relaxed smile, content, peaceful",
        "lighting": "soft afternoon light, warm ambient light",
    },
    "socializing": {
        "action": "making peace sign, happy, laughing, casual pose",
        "environment": "outdoor cafe, bright atmosphere, city background",
        "expression": "bright smile, happy, cheerful laugh",
        "lighting": "bright cheerful lighting, natural sunlight",
    },
    "commuting": {
        "action": "walking, holding bag, casual stroll",
        "environment": "city street, urban landscape, sidewalk",
        "expression": "calm expression, relaxed, everyday mood",
        "lighting": "morning sunlight, natural outdoor light",
    },
    "hobby": {
        "action": "holding camera, creative pose, drawing",
        "environment": "art studio, creative space, colorful",
        "expression": "excited, passionate, creative focus",
        "lighting": "creative studio lighting, soft natural light",
    },
    "self_care": {
        "action": "applying makeup, mirror, gentle pose",
        "environment": "bathroom, mirror, vanity, clean",
        "expression": "gentle smile, self-care, satisfied",
        "lighting": "bathroom lighting, mirror reflection, soft light",
    },
    "shopping": {
        "action": "holding shopping bag, browsing, casual stance",
        "environment": "shopping mall, store interior, bright commercial space",
        "expression": "happy, excited, browsing with interest",
        "lighting": "bright commercial lighting, indoor illumination",
    },
    "outdoor": {
        "action": "walking, enjoying nature, casual stroll",
        "environment": "park, trees, pathway, flowers, outdoor scenery",
        "expression": "peaceful smile, relaxed, enjoying fresh air",
        "lighting": "natural sunlight, dappled light through trees",
    },
    "other": {
        "action": "standing, casual pose, natural posture",
        "environment": "indoor, natural lighting, comfortable room",
        "expression": "natural smile, relaxed",
        "lighting": "natural lighting, ambient indoor light",
    },
}

# 中文活动关键词 → 活动类型（优先级从高到低匹配）
_TYPE_KEYWORD_MAP: list = [
    ("睡觉", "sleeping"), ("入眠", "sleeping"), ("午休", "sleeping"),
    ("睡醒", "waking_up"), ("起床", "waking_up"), ("刚醒", "waking_up"),
    ("吃", "eating"), ("餐", "eating"), ("烹饪", "eating"), ("做饭", "eating"),
    ("喝咖啡", "eating"), ("喝茶", "eating"), ("喝奶茶", "eating"),
    ("工作", "working"), ("办公", "working"), ("加班", "working"),
    ("学习", "studying"), ("看书", "studying"), ("读书", "studying"),
    ("写作业", "studying"), ("复习", "studying"), ("备考", "studying"),
    ("运动", "exercising"), ("健身", "exercising"), ("跑步", "exercising"),
    ("散步", "outdoor"), ("逛街", "shopping"), ("购物", "shopping"),
    ("通勤", "commuting"), ("赶路", "commuting"), ("坐车", "commuting"),
    ("化妆", "self_care"), ("护肤", "self_care"), ("打扮", "self_care"),
    ("休息", "relaxing"), ("放松", "relaxing"), ("发呆", "relaxing"),
    ("刷手机", "relaxing"), ("听音乐", "relaxing"), ("看剧", "relaxing"),
    ("聊天", "socializing"), ("聚会", "socializing"), ("约会", "socializing"),
    ("画画", "hobby"), ("拍照", "hobby"), ("弹琴", "hobby"),
    ("在外面", "outdoor"), ("公园", "outdoor"), ("散步", "outdoor"),
]


def _classify_activity(description: str) -> str:
    """根据用户描述识别活动类型，返回活动 key（用于查 _ACTIVITY_SCENE_MAP）。"""
    for keyword, activity_type in _TYPE_KEYWORD_MAP:
        if keyword in description:
            return activity_type
    return "other"


def get_scene_fallback(description: str) -> Dict[str, str]:
    """确定性映射兜底：根据用户描述关键词返回场景标签。"""
    activity_type = _classify_activity(description)
    scene = dict(_ACTIVITY_SCENE_MAP.get(activity_type, _ACTIVITY_SCENE_MAP["other"]))
    _logger.info(f"[SelfieScene] 兜底映射: 描述='{description[:30]}' → 类型={activity_type} action={scene['action'][:30]}")
    return scene


# ================================================================
# LLM 场景生成
# ================================================================

_SCENE_LLM_PROMPT = """You are a selfie scene tag generator for anime image generation.
Given a description of what someone is currently doing, output a JSON object with 4 keys:
- action: physical pose/gesture (3-8 English tags)
- environment: background and surroundings (3-8 English tags)
- expression: facial expression (2-5 English tags)
- lighting: light conditions (2-4 English tags)

Rules:
1. Output ONLY valid JSON, no markdown, no explanations
2. All values must be English tags suitable for Stable Diffusion / NovelAI
3. Do NOT include character appearance (hair, eyes, clothing)
4. Tags should feel natural for the scenario
5. Keep tags concise and descriptive
6. For selfie scenarios, prefer ONE hand visible (free hand), the other hand holds the phone off-screen

Examples:

Activity: 在书房看轻小说
{"action": "holding book, reading, relaxed pose, one hand visible", "environment": "study room, bookshelf, warm interior, cozy", "expression": "content smile, absorbed in reading", "lighting": "desk lamp, warm indoor light, soft shadows"}

Activity: 在厨房做早饭
{"action": "holding spatula, cooking, one hand on pan, busy", "environment": "kitchen, stove, morning atmosphere, window", "expression": "happy smile, focused on cooking", "lighting": "morning light through window, bright kitchen"}

Activity: 躺在床上刷手机
{"action": "lying on bed, holding phone, relaxed, cozy pose", "environment": "bedroom, bed, pillows, warm blankets", "expression": "relaxed smile, entertained, casual", "lighting": "dim warm light, night lamp, cozy atmosphere"}

Now generate for the following activity:"""


async def generate_scene_tags(
    description: str,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
) -> Optional[Dict[str, str]]:
    """使用 LLM 将中文活动描述生成英文场景标签。

    Args:
        description: 用户的中文活动描述（如"在看书"、"躺在床上"）
        api_base: LLM API 地址（留空则使用 plugin.ctx.llm）
        api_key: LLM API 密钥
        model: LLM 模型名

    Returns:
        {"action": "...", "environment": "...", "expression": "...", "lighting": "..."}
        失败返回 None
    """
    if not description or not description.strip():
        return None

    prompt = f"{_SCENE_LLM_PROMPT}\n\nActivity: {description.strip()}"

    # 优先使用自定义 API，否则使用 plugin.ctx.llm
    if has_custom_api_config({"api_base": api_base, "api_key": api_key, "model_name": model}):
        success, response, _, _ = await call_custom_llm_api(
            prompt=prompt, api_base=api_base, api_key=api_key,
            model=model, temperature=0.7, max_tokens=600, timeout=30,
        )
        if not success:
            _logger.warning(f"[SelfieScene] 自定义 API 调用失败: {response}")
            return None
    else:
        try:
            from ..instance import get_plugin_instance
            plugin = get_plugin_instance()
            if not plugin:
                return None
            result = await plugin.ctx.llm.generate(
                prompt=prompt, temperature=0.7, max_tokens=600,
            )
            response = result.get("content", "") if isinstance(result, dict) else str(result)
            success = bool(response)
        except Exception as e:
            _logger.warning(f"[SelfieScene] plugin.ctx.llm 调用失败: {e}")
            return None

        if not success or not response:
            _logger.warning("[SelfieScene] LLM 返回空响应")
            return None

    # 解析 JSON
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        scene = json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试提取 JSON 对象
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                scene = json.loads(m.group(0))
            except json.JSONDecodeError:
                _logger.warning(f"[SelfieScene] JSON 解析失败: {cleaned[:100]}")
                return None
        else:
            _logger.warning(f"[SelfieScene] 响应中无 JSON: {cleaned[:100]}")
            return None

    required = {"action", "environment", "expression", "lighting"}
    missing = required - set(scene.keys())
    if missing:
        _logger.warning(f"[SelfieScene] 缺少字段: {missing}")
        return None

    for key in required:
        if not isinstance(scene[key], str) or not scene[key].strip():
            _logger.warning(f"[SelfieScene] 字段 {key} 无效")
            return None

    _logger.info(
        f"[SelfieScene] LLM 生成成功: action={scene['action'][:40]} "
        f"env={scene['environment'][:30]}"
    )
    return {
        "action": scene["action"],
        "environment": scene["environment"],
        "expression": scene["expression"],
        "lighting": scene["lighting"],
    }


async def get_scene_for_selfie(
    description: str,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
) -> Optional[Dict[str, str]]:
    """自拍场景增强主入口：LLM 优先，失败则确定性映射兜底。

    Returns:
        场景标签字典，完全失败返回 None
    """
    # 1. 尝试 LLM 生成
    if description and description.strip():
        result = await generate_scene_tags(description, api_base, api_key, model)
        if result:
            return result

    # 2. 确定性映射兜底
    if description and description.strip():
        _logger.info(f"[SelfieScene] LLM 失败，使用确定性映射")
        return get_scene_fallback(description)

    return None


def build_scene_context(scene: Dict[str, str]) -> str:
    """将场景标签字典格式化为 prompt 片段，注入到 LLM 提示词中。

    Returns:
        格式化的场景上下文文本
    """
    parts = ["<selfie_scene_context>"]
    parts.append("【当前场景信息 - 由场景分析器自动生成】")

    if scene.get("action"):
        parts.append(f"- 动作姿态: {scene['action']}")
    if scene.get("expression"):
        parts.append(f"- 表情神态: {scene['expression']}")
    if scene.get("environment"):
        parts.append(f"- 背景环境: {scene['environment']}")
    if scene.get("lighting"):
        parts.append(f"- 光线氛围: {scene['lighting']}")

    parts.append("")
    parts.append("使用以上场景信息来丰富你的 prompt，但不要逐字照抄。")
    parts.append("优先保持用户原描述的风格和内容，场景标签仅作为补充参考。")
    parts.append("</selfie_scene_context>")
    return "\n".join(parts)


# ================================================================
# 日程接入：从 autonomous_planning_plugin 获取当前活动
# ================================================================

_ACTIVITY_API = "xuqian13.autonomous-planning-plugin-v4.get_current_activity"


async def get_schedule_activity() -> Optional[str]:
    """从 autonomous_planning_plugin 获取 bot 当前活动的中文描述。

    调用 autonomous_planning v4 API，提取当前活动的 name/description。
    失败（插件未加载/无活动）时返回 None。

    Returns:
        活动描述字符串（如"在书房看轻小说"），或 None
    """
    try:
        from ..instance import get_plugin_instance
        plugin = get_plugin_instance()
        if not plugin:
            return None

        result = await plugin.ctx.api.call(_ACTIVITY_API, chat_id="global")
        if not isinstance(result, dict) or result.get("error"):
            return None
        if not result.get("has_activity"):
            return None

        activity = result.get("activity")
        if not isinstance(activity, dict):
            return None

        # 优先用 description，其次 name
        desc = str(activity.get("description") or "").strip()
        if not desc:
            desc = str(activity.get("name") or "").strip()
        return desc or None

    except Exception:
        return None
