# -*- coding: utf-8 -*-
"""提示词引擎：LLM 生成 + 规则模板渲染 + 输出解析 + 后处理排序。

合并自：
- core/rules/prompt_rules.py（模板）
- core/utils/llm_helper.py（LLM API 调用）
- core/utils/prompt_output_parser.py（输出解析）
- core/utils/prompt_postprocessor.py（后处理排序）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .http_client import get_session

_logger = logging.getLogger("ai_draw_plugin")


def inject_logger(logger):
    global _logger
    _logger = logger


# ================================================================
# LLM API 调用（从 llm_helper.py 迁移）
# ================================================================

_EXPECTED_OUTPUT_PATTERN = re.compile(
    r"(\[(?:SCENE|PROMPT|NEG|CHARACTER|STYLE|SETTING)\][\s\S]*?(?:\[/(?:SCENE|PROMPT|NEG|CHARACTER|STYLE|SETTING)\])?)",
    re.IGNORECASE,
)


def _extract_final_answer_from_reasoning(reasoning: str) -> str:
    if not reasoning:
        return ""
    matches = list(_EXPECTED_OUTPUT_PATTERN.finditer(reasoning))
    if not matches:
        return ""
    first_match_start = matches[0].start()
    candidate = reasoning[first_match_start:].strip()
    if len(candidate) > 2000:
        candidate = candidate[:2000]
    if not re.search(r"\[(?:SCENE|PROMPT|NEG)\][\s\S]+?\[/(?:SCENE|PROMPT|NEG)\]", candidate, re.IGNORECASE):
        return ""
    _logger.info(f"[LLM] 从 reasoning_content 提取到有效输出，长度={len(candidate)}")
    return candidate


async def call_custom_llm_api(
    prompt: str,
    api_base: str,
    api_key: str,
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: int = 120,
) -> Tuple[bool, str, str, str]:
    """调用 OpenAI 兼容的 LLM API（异步非阻塞）。

    Returns:
        (成功, 生成内容, 推理内容, 实际使用的模型名)
    """
    if not api_base or not api_key or not model:
        _logger.error("[LLM] api_base / api_key / model 不能为空")
        return False, "API 配置不完整", "", ""

    base_url = api_base.rstrip("/")
    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept-Encoding": "gzip, deflate",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    prompt_bytes = len(prompt.encode("utf-8"))
    _logger.info(f"[LLM] 调用 {url} model={model} input={prompt_bytes}bytes")

    start_time = time.time()
    last_error = ""

    for attempt in range(1, 4):
        try:
            session = await get_session(timeout)
            async with session.post(url, headers=headers, json=payload) as resp:
                header_time = time.time() - start_time
                if resp.status == 200:
                    data = await resp.json()
                    total_elapsed = time.time() - start_time
                    choices = data.get("choices", [])
                    if not choices:
                        return False, "LLM 返回空的 choices 列表", "", model

                    message = choices[0].get("message", {})
                    content = message.get("content", "") or ""
                    reasoning = (
                        message.get("reasoning_content", "")
                        or data.get("reasoning_content", "")
                        or ""
                    )

                    if not content and reasoning:
                        extracted = _extract_final_answer_from_reasoning(reasoning)
                        if extracted:
                            content = extracted
                        else:
                            return False, "LLM 返回空内容且 reasoning 中无格式化输出", "", model

                    if not content:
                        return False, "LLM 返回空内容", "", model

                    actual_model = data.get("model", model)
                    body_time = total_elapsed - header_time
                    _logger.info(
                        f"[LLM] 成功 total={total_elapsed:.1f}s header={header_time:.1f}s "
                        f"body={body_time:.1f}s output_len={len(content)} model={actual_model}"
                    )
                    return True, content, reasoning, actual_model

                elif resp.status in (429, 502, 503, 504):
                    text = await resp.text()
                    last_error = f"HTTP {resp.status}: {text[:300]}"
                    _logger.warning(f"[LLM] 第{attempt}次失败 ({last_error})")
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                    continue
                else:
                    text = await resp.text()
                    _logger.error(f"[LLM] HTTP {resp.status}: {text[:500]}")
                    return False, f"API 请求失败 (HTTP {resp.status})", "", model

        except asyncio.TimeoutError:
            last_error = f"请求超时 ({timeout}s)"
            if attempt < 3:
                await asyncio.sleep(2)
                continue
        except aiohttp.ClientConnectorError as e:
            last_error = f"连接失败: {str(e)[:200]}"
            if attempt < 3:
                await asyncio.sleep(2)
                continue
        except Exception as e:
            _logger.error(f"[LLM] 未知错误: {e}", exc_info=True)
            return False, f"API 调用异常: {str(e)[:300]}", "", model

    return False, f"重试 3 次后仍失败: {last_error}", "", model


def has_custom_api_config(config: Dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        return False
    api_base = (config.get("api_base") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    mdl = (config.get("model_name") or config.get("model") or "").strip()
    return bool(api_base and api_key and mdl)


def get_custom_api_config(config: Dict[str, Any]) -> Tuple[str, str, str]:
    api_base = (config.get("api_base") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    mdl = (config.get("model_name") or config.get("model") or "").strip()
    return api_base, api_key, mdl


# ================================================================
# 提示词模板渲染（从 prompt_rules.py 引用 + 自建渲染逻辑）
# ================================================================

# 从旧位置导入模板（模板太大不适合内联）
def _load_templates():
    """懒加载提示词模板（保持向后兼容）。"""
    try:
        from ..core.rules.prompt_rules import (
            PROMPT_GENERATOR_TEMPLATE,
            PROMPT_GENERATOR_JSON_TEMPLATE,
            SFW_PROMPT_GENERATOR_TEMPLATE,
            SFW_PROMPT_GENERATOR_JSON_TEMPLATE,
        )
        return (
            PROMPT_GENERATOR_TEMPLATE,
            PROMPT_GENERATOR_JSON_TEMPLATE,
            SFW_PROMPT_GENERATOR_TEMPLATE,
            SFW_PROMPT_GENERATOR_JSON_TEMPLATE,
        )
    except ImportError:
        _logger.warning("[PromptEngine] 无法加载旧模板，使用默认空模板")
        return "", "", "", ""


# ================================================================
# 输出解析（从 prompt_output_parser.py 迁移）
# ================================================================


def _strip_code_fence(text: str) -> str:
    s = (text or "").strip()
    if not (s.startswith("```") and s.endswith("```")):
        return s
    inner = s[3:-3].strip()
    if "\n" not in inner:
        return inner.strip()
    first_line, rest = inner.split("\n", 1)
    if first_line.strip().isalpha() and len(first_line.strip()) < 15:
        return rest.strip()
    return inner.strip()


def _join_tags(tags) -> str:
    if not tags or not isinstance(tags, list):
        return ""
    return ", ".join([t.strip() for t in tags if isinstance(t, str) and t.strip()]).strip()


def parse_structured_prompt_payload(text: str) -> Optional[Dict[str, Any]]:
    cleaned = _strip_code_fence(text).strip()
    if not cleaned:
        return None

    candidates = [cleaned]
    if any(token in cleaned for token in ('"prompt"', '"global"', '"people"')):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(cleaned[start:end + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        version = obj.get("version")
        has_v2_fields = isinstance(obj.get("global"), list)
        has_v1_prompt = isinstance(obj.get("prompt"), str) and obj.get("prompt", "").strip()
        if version in (2, 3) or (isinstance(version, int) and version >= 2):
            if has_v2_fields or has_v1_prompt:
                return obj
            continue
        if has_v1_prompt:
            return obj
    return None


def _render_from_v2(obj: dict) -> Optional[str]:
    global_tags = obj.get("global")
    if not isinstance(global_tags, list):
        return None
    first_line = _join_tags(global_tags)
    if not first_line:
        return None

    people = obj.get("people", []) or []
    if not isinstance(people, list):
        people = []

    format_value = str(obj.get("format", "") or "").strip().lower()
    valid_people = []
    for person_tags in people:
        if not isinstance(person_tags, list):
            continue
        person_line = [t.strip() for t in person_tags if isinstance(t, str) and t.strip()]
        if person_line:
            valid_people.append(person_line)

    if format_value != "multi" or len(valid_people) <= 1:
        if valid_people:
            merged = _join_tags(global_tags + valid_people[0])
            return merged if merged else first_line
        return first_line

    lines = [first_line + ","]
    for i, person_tags in enumerate(valid_people, start=1):
        person_line = _join_tags(person_tags)
        if person_line:
            lines.append(f"char{i}:{person_line},")
    return "\n".join(lines).strip()


def parse_prompt_from_structured_output(text: str) -> Optional[str]:
    obj = parse_structured_prompt_payload(text)
    if not obj:
        return None

    version = obj.get("version")
    if version in (2, 3) or (isinstance(version, int) and version >= 2):
        rendered = _render_from_v2(obj)
        if rendered:
            return rendered

    prompt = obj.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        normalized = prompt.strip()
        if "\\n|" in normalized:
            normalized = normalized.replace("\\n", "\n")
        return normalized
    return None


# ================================================================
# 后处理排序（从 prompt_postprocessor.py 迁移）
# ================================================================

_COUNT_RE = re.compile(r"^(?:solo|\d+girls|\d+boys|\d+people|1girl|1boy)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^year\s+\d{4}$", re.IGNORECASE)
_CHARACTER_RE = re.compile(r"^[a-zA-Z][\w\s'-]+\([^)]+\)\s*$")

_CAMERA_TAGS = {
    "pov", "female pov", "looking at viewer",
    "from above", "from below", "wide angle",
    "close-up", "close up", "full body", "upper body", "lower body",
    "selfie", "mirror selfie", "group selfie", "holding phone",
}


def _split_prompt_segments(prompt: str) -> List[str]:
    text = (prompt or "").strip()
    if not text:
        return []
    if "\n" in text:
        return [seg.strip() for seg in text.split("\n") if seg.strip()]
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        segments = []
        for index, part in enumerate(parts):
            if not part:
                continue
            if index == 0:
                segments.append(part)
            else:
                segments.append(f"| {part}")
        return segments
    return [text]


def _join_prompt_segments(lines: List[str], original_prompt: str) -> str:
    if not lines:
        return ""
    if "\n" in (original_prompt or ""):
        return "\n".join(lines).strip()
    if "|" in (original_prompt or ""):
        normalized = []
        for index, line in enumerate(lines):
            raw = line.strip()
            if index == 0:
                normalized.append(raw.lstrip("|").strip())
            else:
                normalized.append(raw.lstrip("|").strip())
        return " | ".join([p for p in normalized if p]).strip()
    return "\n".join(lines).strip()


def user_mentions_appearance(raw_request: str) -> bool:
    if not raw_request:
        return False
    cn_keys = [
        "头发", "发色", "发型", "长发", "短发", "双马尾", "马尾", "刘海",
        "黑发", "金发", "白发", "粉发", "蓝发", "红发", "紫发", "银发", "棕发",
        "眼睛", "瞳", "瞳色", "蓝瞳", "红瞳", "金瞳", "绿瞳", "紫瞳", "黑长直",
    ]
    if any(k in raw_request for k in cn_keys):
        return True
    en_keys = ["hair", "haired", "eyes", "eyed", "twintails", "ponytail", "bangs"]
    return any(k in raw_request.lower() for k in en_keys)


def _strip_wrappers(tag: str) -> str:
    t = tag.strip()
    t = t.lstrip("{[(").rstrip("}])")
    t = t.strip()
    t = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", t).strip()
    t = re.sub(r"::\s*$", "", t).strip()
    return t


def remove_selfie_appearance_tags(prompt: str) -> str:
    if not prompt or not prompt.strip():
        return prompt
    if "::" in prompt:
        return prompt

    hair_colors = {
        "black", "blonde", "brown", "blue", "pink", "white", "silver",
        "red", "green", "purple", "orange", "gray", "grey", "aqua", "cyan",
    }
    eye_colors = {
        "black", "brown", "blue", "red", "green", "purple", "orange",
        "gray", "grey", "golden", "yellow", "pink", "aqua", "cyan",
    }
    hair_styles_exact = {
        "twintails", "twin tails", "ponytail", "side ponytail",
        "braid", "side braid", "pigtails", "hair bun", "bun",
        "bob cut", "hime cut", "bangs", "blunt bangs",
        "straight hair", "wavy hair", "curly hair", "messy hair",
    }

    def should_remove(tag: str) -> bool:
        core = _strip_wrappers(tag).lower()
        core = re.sub(r"\s+", " ", core).strip()
        if "hair" in core and any(x in core for x in ("ribbon", "ornament", "clip", "pin", "bow", "band", "flower")):
            return False
        m = re.match(r"^([a-z]+)\s+hair$", core)
        if m and m.group(1) in hair_colors:
            return True
        if re.match(r"^[a-z]+-haired$", core):
            return True
        if re.match(r"^(?:very )?(?:long|short|medium)\s+hair$", core):
            return True
        if core in hair_styles_exact:
            return True
        m2 = re.match(r"^([a-z]+)\s+eyes$", core)
        if m2 and m2.group(1) in eye_colors:
            return True
        return False

    lines = _split_prompt_segments(prompt)
    out_lines = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        prefix = ""
        if raw.startswith("|"):
            prefix = "|"
            raw = raw[1:].strip()
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        filtered = [t for t in tags if not should_remove(t)]
        joined = ", ".join(filtered)
        if prefix:
            out_lines.append(f"{prefix} {joined}".strip())
        else:
            out_lines.append(joined)
    return _join_prompt_segments(out_lines, prompt)


def _is_character_tag(tag: str) -> bool:
    t = tag.strip()
    t = t.lstrip("{[")
    t = t.rstrip("}]")
    t = t.strip()
    t = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", t).strip()
    t = re.sub(r"::\s*$", "", t).strip()
    return bool(_CHARACTER_RE.match(t))


def normalize_prompt_order(prompt: str) -> str:
    if not prompt or not prompt.strip():
        return prompt

    lines = _split_prompt_segments(prompt)
    out_lines = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        prefix = ""
        if raw.startswith("|"):
            prefix = "|"
            raw = raw[1:].strip()

        tags = [t.strip() for t in raw.split(",") if t.strip()]
        if not tags:
            continue

        nsfw_tags, counts, cameras, characters, years, rest = [], [], [], [], [], []
        for t in tags:
            core = _strip_wrappers(t)
            core_norm = re.sub(r"\s+", " ", core).strip().lower()
            if core_norm == "nsfw":
                nsfw_tags.append(t)
            elif _YEAR_RE.match(core_norm):
                years.append(t)
            elif _COUNT_RE.match(core_norm):
                counts.append(t)
            elif core_norm in _CAMERA_TAGS:
                cameras.append(t)
            elif _is_character_tag(t):
                characters.append(t)
            else:
                rest.append(t)

        new_tags = nsfw_tags + counts + cameras + characters + rest + years
        joined = ", ".join(new_tags).strip()
        if prefix:
            out_lines.append(f"{prefix} {joined}".strip())
        else:
            out_lines.append(joined)

    return _join_prompt_segments(out_lines, prompt)


# ================================================================
# LLM 提示词清理
# ================================================================

def cleanup_llm_prompt(prompt: str) -> str:
    """清理 LLM 返回的原始文本，提取有效提示词。"""
    if not prompt:
        return ""

    parsed = parse_prompt_from_structured_output(prompt)
    if parsed:
        return parsed

    cleaned = prompt.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()
        if "\n" in cleaned:
            first_line, rest = cleaned.split("\n", 1)
            if first_line.strip().isalpha() and len(first_line.strip()) < 15:
                cleaned = rest.strip()
    if cleaned.startswith("`") and cleaned.endswith("`") and cleaned.count("`") == 2:
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith(("'", '"')) and cleaned.endswith(("'", '"')) and len(cleaned) >= 2:
        cleaned = cleaned[1:-1].strip()

    for pat in [
        r"^(?:output|result|prompt|here(?:'s| is)(?: the)?(?: prompt)?)\s*[:：]\s*",
        r"^(?:the )?(?:generated )?prompt\s*(?:is|:)\s*",
    ]:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()

    if "\n" in cleaned:
        lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
        has_multi = any(l.startswith("|") for l in lines)
        valid = [l for l in lines if not re.match(r"^(note|explanation|this|i |the above|here)", l, re.IGNORECASE)]
        if valid:
            cleaned = "\n".join(valid) if has_multi else valid[0]
    return cleaned


# ================================================================
# 参考图角色特征分析（VLM）
# ================================================================

_CHARACTER_ANALYSIS_PROMPT = """Analyze this anime-style character image and output ONLY the character's visual appearance as Danbooru-style tags.

Output format (JSON only, no explanation):
{
  "hair": ["hair color tag", "hair style tag", ...],
  "eyes": ["eye color tag", ...],
  "clothing": ["clothing tags", ...],
  "features": ["other distinctive features", ...]
}

Rules:
- hair: include hair color (e.g. "black hair", "blonde hair") and hair style (e.g. "long hair", "twintails", "ponytail")
- eyes: include eye color (e.g. "blue eyes", "red eyes")
- clothing: describe what the character is wearing
- features: any other distinctive visual features (glasses, accessories, etc.)
- Use ONLY standard Danbooru tags
- If you can't determine something, leave the array empty
- Output ONLY the JSON, nothing else"""


async def analyze_ref_image_character(
    image_base64: str,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
) -> Optional[Dict[str, Any]]:
    """使用 VLM 分析参考图中的角色外貌特征。

    Args:
        image_base64: 参考图的 base64 编码
        api_base: VLM API 地址
        api_key: VLM API 密钥
        model: VLM 模型名

    Returns:
        角色特征字典，失败时返回 None
    """
    if not api_base or not api_key or not model:
        _logger.info("[RefAnalysis] VLM 未配置，跳过参考图分析")
        return None

    base_url = api_base.rstrip("/")
    url = f"{base_url}/v1/chat/completions"

    image_format = "png"
    if image_base64.startswith("/9j/"):
        image_format = "jpeg"
    elif image_base64.startswith("iVBORw"):
        image_format = "png"
    elif image_base64.startswith("UklGR"):
        image_format = "webp"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CHARACTER_ANALYSIS_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{image_format};base64,{image_base64}"
                        },
                    },
                ],
            }
        ],
        "temperature": 0.2,
        "max_tokens": 600,
    }

    try:
        session = await get_session(60)
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                _logger.warning(f"[RefAnalysis] VLM 请求失败 HTTP {resp.status}: {text[:200]}")
                return None

            data = await resp.json()
            choices = data.get("choices", [])
            if not choices:
                return None

            content = choices[0].get("message", {}).get("content", "")
            if not content:
                return None

            # 解析 JSON 响应
            content_clean = content.strip()
            if content_clean.startswith("```"):
                content_clean = content_clean.strip("`")
                if "\n" in content_clean:
                    content_clean = content_clean.split("\n", 1)[1]
                content_clean = content_clean.strip()

            try:
                result = json.loads(content_clean)
            except json.JSONDecodeError:
                # 尝试提取 JSON 块
                m = re.search(r"\{[\s\S]*\}", content_clean)
                if m:
                    try:
                        result = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        _logger.warning(f"[RefAnalysis] 无法解析 VLM 响应: {content[:200]}")
                        return None
                else:
                    _logger.warning(f"[RefAnalysis] 无法解析 VLM 响应: {content[:200]}")
                    return None

            _logger.info(f"[RefAnalysis] 角色特征提取成功: {json.dumps(result, ensure_ascii=False)[:200]}")
            return result

    except asyncio.TimeoutError:
        _logger.warning("[RefAnalysis] VLM 请求超时")
        return None
    except Exception as e:
        _logger.warning(f"[RefAnalysis] VLM 请求异常: {e}")
        return None


def build_character_ref_context(ref_mode: str, char_features: Optional[Dict[str, Any]] = None) -> str:
    """构建角色参考模式的 LLM 上下文。

    Args:
        ref_mode: 参考模式 (character / character&style)
        char_features: VLM 分析出的角色特征（可选）

    Returns:
        格式化的上下文文本，嵌入 LLM prompt
    """
    parts = []

    parts.append("<character_ref_mode>")
    parts.append("【角色参考模式 - 最高优先级规则】")

    if char_features:
        # 有 VLM 分析结果：提供准确的外貌信息
        hair_tags = char_features.get("hair", [])
        eye_tags = char_features.get("eyes", [])
        clothing_tags = char_features.get("clothing", [])
        feature_tags = char_features.get("features", [])

        parts.append("以下是从参考图中分析出的角色外貌特征：")

        if hair_tags:
            parts.append(f"- 发色/发型: {', '.join(hair_tags)}")
        if eye_tags:
            parts.append(f"- 瞳色: {', '.join(eye_tags)}")
        if clothing_tags:
            parts.append(f"- 服装: {', '.join(clothing_tags)}")
        if feature_tags:
            parts.append(f"- 其他特征: {', '.join(feature_tags)}")

        parts.append("")
        parts.append("规则：")
        parts.append("1. 以上外貌特征已由参考图定义，你必须原样使用这些发色/发型/瞳色标签")
        parts.append("2. 如果用户未指定服装变更，使用以上服装标签")
        parts.append("3. 如果用户明确指定了不同的服装/穿搭，替换为用户的描述")
        parts.append("4. 不要编造任何与以上特征冲突的外貌标签")
    else:
        # 无 VLM 分析：至少告诉 LLM 不要编造外貌
        parts.append("参考图已定义了角色的完整外貌。你必须严格遵守：")
        parts.append("")
        parts.append("1. 【禁止编造外貌】绝对不要编造任何发色、发型、瞳色、肤色标签")
        parts.append("   - 禁止输出: black hair, brown hair, blonde hair, long hair, short hair, twintails 等")
        parts.append("   - 禁止输出: blue eyes, red eyes, green eyes 等瞳色标签")
        parts.append("2. 【服装谨慎处理】除非用户明确指定服装，否则不要编造服装标签")
        parts.append("3. 【专注可描述内容】你只能描述以下内容：")
        parts.append("   - 动作姿势 (pose/action)")
        parts.append("   - 表情神态 (expression)")
        parts.append("   - 场景环境 (setting/background)")
        parts.append("   - 光影氛围 (lighting/atmosphere)")
        parts.append("   - 镜头构图 (camera/composition)")
        parts.append("4. 角色的外貌完全由参考图决定，prompt 中不得包含外貌描述")

    parts.append("</character_ref_mode>")
    return "\n".join(parts)


# ================================================================
# 时间上下文
# ================================================================

def build_current_time_context() -> str:
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 8:
        period, lighting = "清晨", "dawn, early morning, sunrise, soft morning light"
    elif 8 <= hour < 11:
        period, lighting = "上午", "morning, daylight, bright natural light"
    elif 11 <= hour < 14:
        period, lighting = "中午", "noon, midday, bright sunlight"
    elif 14 <= hour < 17:
        period, lighting = "下午", "afternoon, warm daylight, sunlit"
    elif 17 <= hour < 19:
        period, lighting = "傍晚", "dusk, sunset, golden hour, evening glow"
    elif 19 <= hour < 23:
        period, lighting = "夜晚", "night, moonlight, night sky, city lights, warm indoor light"
    else:
        period, lighting = "深夜", "late night, midnight, moonlight, dim light, warm indoor light"
    return (
        f"<current_time_context>\n当前本地时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{period}）。\n"
        f"仅在用户未明确指定时，用于补全时间、光线和背景氛围。优先考虑 {lighting}。\n</current_time_context>"
    )
