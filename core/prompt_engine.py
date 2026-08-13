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
from urllib.parse import urlparse

import aiohttp

from .http_client import get_session
from .prompt_types import GeneratedPrompt, PersonPrompt, StructuredPrompt

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

_OPTIONAL_LLM_FIELDS = ("thinking", "reasoning_effort", "response_format")
_UNSUPPORTED_PARAMETER_RE = re.compile(
    r"(?:unsupported|unknown|unrecognized|unexpected|not\s+supported|"
    r"not\s+allowed|extra\s+(?:field|input|parameter)|invalid\s+parameter|"
    r"不支持|未知|未识别|不允许|多余|额外参数)",
    re.IGNORECASE,
)


def _is_optional_parameter_rejection(
    status: int, response_text: str, payload: Dict[str, Any],
) -> bool:
    if status not in (400, 422):
        return False
    present = [key for key in _OPTIONAL_LLM_FIELDS if key in payload]
    if not present:
        return False
    error_text = str(response_text or "").lower()
    return bool(_UNSUPPORTED_PARAMETER_RE.search(error_text)) and (
        any(key in error_text for key in present)
        or "extra field" in error_text
        or "extra input" in error_text
        or "额外参数" in error_text
        or "多余" in error_text
    )


def _extract_final_answer_from_reasoning(reasoning: str) -> str:
    if not reasoning:
        return ""

    # 部分推理模型会在输出预算耗尽前把最终 JSON 写入 reasoning_content，
    # 却来不及生成独立 content。只回收可完整解析的标签槽位，避免把推理草稿当提示词。
    reasoning_tail = reasoning[-65536:]
    decoder = json.JSONDecoder()
    object_starts = [
        index for index, char in enumerate(reasoning_tail) if char == "{"
    ]
    for start in reversed(object_starts[-128:]):
        try:
            obj, _ = decoder.raw_decode(reasoning_tail[start:])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if parse_structured_prompt(obj) is None:
            continue
        candidate = json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"),
        )
        _logger.info(
            "[LLM] 从 reasoning_content 提取到有效标签槽位 JSON，长度=%d",
            len(candidate),
        )
        return candidate

    matches = list(_EXPECTED_OUTPUT_PATTERN.finditer(reasoning))
    if not matches:
        return ""
    first_match_start = matches[0].start()
    candidate = reasoning[first_match_start:].strip()
    if len(candidate) > 2000:
        candidate = candidate[:2000]
    if not re.search(r"\[(?:SCENE|PROMPT|NEG)\][\s\S]+?\[/(?:SCENE|PROMPT|NEG)\]", candidate, re.IGNORECASE):
        return ""
    _logger.info(f"[LLM] 从 reasoning_content 提取到有效旧格式输出，长度={len(candidate)}")
    return candidate


async def call_custom_llm_api(
    prompt: str,
    api_base: str,
    api_key: str,
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: int = 120,
    structured_output: bool = False,
    thinking_mode: str = "auto",
    reasoning_effort: str = "low",
    json_response_mode: str = "auto",
) -> Tuple[bool, str, str, str]:
    """调用 OpenAI 兼容的 LLM API（异步非阻塞）。

    Returns:
        (成功, 生成内容, 推理内容, 实际使用的模型名)
    """
    if not api_base or not api_key or not model:
        _logger.error("[LLM] api_base / api_key / model 不能为空")
        return False, "API 配置不完整", "", ""

    base_url = api_base.rstrip("/")
    if base_url.startswith("http://"):
        _logger.warning("[LLM] api_base 为明文 HTTP，API Key 将以明文传输，建议改用 HTTPS")
    url = f"{base_url}/v1/chat/completions"
    # api_key 仅放入 Authorization 头，不写入任何日志/异常；下方日志只记录 url（不含 key）
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
    hostname = (urlparse(api_base).hostname or "").lower()
    official_deepseek = hostname == "api.deepseek.com"
    deepseek_compatible = official_deepseek or "deepseek" in model.lower()
    thinking_mode = str(thinking_mode or "auto").strip().lower()
    reasoning_effort = str(reasoning_effort or "low").strip().lower()
    json_response_mode = str(json_response_mode or "auto").strip().lower()
    thinking_enabled = thinking_mode == "enabled" or (
        thinking_mode == "auto" and deepseek_compatible
    )
    if thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
    elif thinking_enabled:
        payload["thinking"] = {"type": "enabled"}
        if reasoning_effort in {"low", "high", "max"}:
            payload["reasoning_effort"] = reasoning_effort
    if structured_output and (
        json_response_mode == "enabled"
        or (json_response_mode == "auto" and deepseek_compatible)
    ):
        payload["response_format"] = {"type": "json_object"}

    prompt_bytes = len(prompt.encode("utf-8"))
    _logger.info(f"[LLM] 调用 {url} model={model} input={prompt_bytes}bytes")

    overall_start_time = time.time()
    last_error = ""
    compatibility_fallback_used = False

    for attempt in range(1, 4):
        attempt_start_time = time.time()
        try:
            session = await get_session(timeout)
            async with session.post(url, headers=headers, json=payload) as resp:
                header_time = time.time() - attempt_start_time
                if resp.status == 200:
                    data = await resp.json()
                    total_elapsed = time.time() - overall_start_time
                    body_time = max(0.0, total_elapsed - (attempt_start_time - overall_start_time) - header_time)
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
                            return False, "LLM 返回空内容且 reasoning 中无有效标签槽位 JSON", "", model

                    if not content:
                        return False, "LLM 返回空内容", "", model

                    actual_model = data.get("model", model)
                    _logger.info(
                        f"[LLM] 成功 attempt={attempt} total={total_elapsed:.1f}s "
                        f"attempt_header={header_time:.1f}s attempt_body={body_time:.1f}s "
                        f"output_len={len(content)} model={actual_model}"
                    )
                    return True, content, reasoning, actual_model

                elif resp.status in (400, 422) and not compatibility_fallback_used:
                    text = await resp.text()
                    if _is_optional_parameter_rejection(resp.status, text, payload):
                        for key in _OPTIONAL_LLM_FIELDS:
                            payload.pop(key, None)
                        compatibility_fallback_used = True
                        _logger.warning(
                            "[LLM] 当前接口不接受可选思考/JSON参数，已自动移除并重试 "
                            "(HTTP %s: %s)",
                            resp.status,
                            text[:200],
                        )
                        continue
                    _logger.error(f"[LLM] HTTP {resp.status}: {text[:500]}")
                    return False, f"API 请求失败 (HTTP {resp.status})", "", model
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
            _logger.warning(
                f"[LLM] 第{attempt}次请求超时，已耗时 {time.time() - attempt_start_time:.1f}s"
            )
            if attempt < 3:
                await asyncio.sleep(2)
                continue
        except aiohttp.ClientConnectorError as e:
            last_error = f"连接失败: {str(e)[:200]}"
            _logger.warning(f"[LLM] 第{attempt}次连接失败 ({last_error})")
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
    if not tags or not isinstance(tags, (list, tuple)):
        return ""
    return ", ".join([t.strip() for t in tags if isinstance(t, str) and t.strip()]).strip()


def _normalize_tag_sequence(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        tag.strip()
        for tag in value
        if isinstance(tag, str) and tag.strip()
    )


def _normalize_choice(value: Any, allowed: Tuple[str, ...], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


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

        has_v2_fields = isinstance(obj.get("global"), list)
        has_v1_prompt = isinstance(obj.get("prompt"), str) and obj.get("prompt", "").strip()
        if has_v2_fields:
            return obj
        if has_v1_prompt:
            return obj
    return None


def render_structured_prompt_flat(
    global_tags: Tuple[str, ...], people: Tuple[PersonPrompt, ...],
) -> str:
    """将分层提示词渲染为完整的正向兼容字符串。"""
    first_line = _join_tags(global_tags)
    if not first_line:
        return ""

    if len(people) <= 1:
        if people:
            merged = _join_tags(global_tags + people[0].positive_tags)
            return merged if merged else first_line
        return first_line

    lines = [first_line + ","]
    for i, person in enumerate(people, start=1):
        person_line = _join_tags(person.positive_tags)
        if person_line:
            lines.append(f"char{i}:{person_line},")
    return "\n".join(lines).strip()


def _render_positive_fallback(obj: Dict[str, Any]) -> str:
    """从不可用的结构对象中尽量保留正向标签，且绝不混入人物负向。"""
    global_tags = _normalize_tag_sequence(obj.get("global"))
    raw_people = obj.get("people")
    people = []
    if isinstance(raw_people, list):
        for item in raw_people:
            value = item.get("prompt") if isinstance(item, dict) else item
            positive_tags = _normalize_tag_sequence(value)
            if not positive_tags and isinstance(value, str):
                positive_tags = tuple(
                    tag.strip()
                    for tag in re.split(r"[,\n]", value)
                    if tag.strip()
                )
            if positive_tags:
                people.append(PersonPrompt(positive_tags=positive_tags, negative_tags=()))

    if global_tags:
        return render_structured_prompt_flat(global_tags, tuple(people))
    return _join_tags(tuple(
        tag for person in people for tag in person.positive_tags
    ))


def parse_structured_prompt(
    obj: Dict[str, Any],
    *,
    intent: Optional[str] = None,
    continuity: Optional[str] = None,
) -> Optional[StructuredPrompt]:
    """把 v2+ global/people JSON 归一成不可变的内部结构。"""
    global_tags = _normalize_tag_sequence(obj.get("global"))
    if not global_tags:
        return None

    raw_people = obj.get("people", []) or []
    people_list = []
    if not isinstance(raw_people, list):
        return None
    for item in raw_people:
        if isinstance(item, dict):
            positive_tags = _normalize_tag_sequence(item.get("prompt"))
            if not positive_tags:
                return None
            negative_tags = _normalize_tag_sequence(item.get("negative_prompt"))
        else:
            positive_tags = _normalize_tag_sequence(item)
            if not positive_tags:
                return None
            negative_tags = ()
        people_list.append(PersonPrompt(
            positive_tags=positive_tags,
            negative_tags=negative_tags,
        ))
    people = tuple(people_list)

    format_value = "multi" if len(people) > 1 else "single"
    flat_prompt = render_structured_prompt_flat(global_tags, people)
    if not flat_prompt:
        return None

    return StructuredPrompt(
        global_tags=global_tags,
        people=people,
        format=format_value,
        intent=_normalize_choice(
            intent if intent is not None else obj.get("intent"),
            ("normal", "selfie"),
            "normal",
        ),
        continuity=_normalize_choice(
            continuity if continuity is not None else obj.get("continuity"),
            ("new", "keep", "adjust", "switch"),
            "new",
        ),
    )


def _render_from_v2(obj: dict) -> Optional[str]:
    structured = parse_structured_prompt(obj)
    if not structured:
        return _render_positive_fallback(obj) or None
    return render_structured_prompt_flat(
        structured.global_tags, structured.people,
    )


def parse_prompt_from_structured_output(text: str) -> Optional[str]:
    obj = parse_structured_prompt_payload(text)
    if not obj:
        return None

    if isinstance(obj.get("global"), list):
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


def parse_generated_prompt(
    text: str,
    *,
    intent: Optional[str] = None,
    continuity: Optional[str] = None,
) -> GeneratedPrompt:
    """解析 LLM 输出，并在可用时同时返回 NovelAI 分层提示词。"""
    obj = parse_structured_prompt_payload(text)
    if obj:
        if isinstance(obj.get("global"), list):
            structured = parse_structured_prompt(
                obj, intent=intent, continuity=continuity,
            )
            if structured:
                return GeneratedPrompt(
                    flat_prompt=render_structured_prompt_flat(
                        structured.global_tags, structured.people,
                    ),
                    structured_prompt=structured,
                )
            fallback = _render_positive_fallback(obj)
            if fallback:
                return GeneratedPrompt(flat_prompt=fallback)

        prompt = obj.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            normalized = prompt.strip()
            if "\\n|" in normalized:
                normalized = normalized.replace("\\n", "\n")
            return GeneratedPrompt(flat_prompt=normalized)

    return GeneratedPrompt(flat_prompt=_cleanup_plain_llm_prompt(text))


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

def _cleanup_plain_llm_prompt(prompt: str) -> str:
    if not prompt:
        return ""

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


def cleanup_llm_prompt(prompt: str) -> str:
    """兼容旧调用：始终返回可显示、可发送的完整正向提示词字符串。"""
    return parse_generated_prompt(prompt).flat_prompt


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
