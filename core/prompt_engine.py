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

_SLOT_GLOBAL_LINE_RE = re.compile(
    r"^\s*(?:(?:[-*]|\d+[.)]|#{1,6})\s+)?(?:\*\*|__|`)?GLOBAL"
    r"\s*(?:\*\*|__|`)?\s*(?::|：|\|)"
    r"\s*(?:\*\*|__|`)?\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_SLOT_PERSON_LINE_RE = re.compile(
    r"^\s*(?:(?:[-*]|\d+[.)]|#{1,6})\s+)?(?:\*\*|__|`)?(?:"
    r"P(?P<short_index>\d+)\s*(?:"
    r"(?P<sign>[+-])|[_\s-]*(?P<short_kind>POS(?:ITIVE)?|NEG(?:ATIVE)?)"
    r")|"
    r"PERSON[_\s-]*(?P<long_index>\d+)[_\s-]*"
    r"(?P<kind>POS(?:ITIVE)?|NEG(?:ATIVE)?)"
    r")\s*(?:\*\*|__|`)?\s*(?::|：|\|)"
    r"\s*(?:\*\*|__|`)?\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)

_OPTIONAL_LLM_FIELDS = ("thinking", "reasoning_effort", "response_format")
_UNSUPPORTED_PARAMETER_RE = re.compile(
    r"(?:unsupported|unknown|unrecognized|unexpected|not\s+supported|"
    r"not\s+allowed|extra\s+(?:field|input|parameter)|invalid\s+parameter|"
    r"不支持|未知|未识别|不允许|多余|额外参数)",
    re.IGNORECASE,
)


def _optional_fields_to_remove(
    status: int, response_text: str, payload: Dict[str, Any],
) -> Tuple[str, ...]:
    if status not in (400, 422):
        return ()
    present = tuple(key for key in _OPTIONAL_LLM_FIELDS if key in payload)
    if not present:
        return ()
    error_text = str(response_text or "").lower()
    if not _UNSUPPORTED_PARAMETER_RE.search(error_text):
        return ()
    explicit = tuple(key for key in present if key.lower() in error_text)
    if explicit:
        return explicit
    if any(marker in error_text for marker in (
        "extra field", "extra input", "extra parameter", "额外参数", "多余",
    )):
        return present
    return ()

def _extract_final_answer_from_reasoning(reasoning: str) -> str:
    if not reasoning:
        return ""

    # 部分推理模型会在输出预算耗尽前把最终槽位写入 reasoning_content，
    # 却来不及生成独立 content。只回收可完整解析的结果，避免把推理草稿当提示词。
    reasoning_tail = reasoning[-65536:]
    for slot_block in reversed(_prompt_slot_blocks(reasoning_tail)):
        slot_payload = parse_prompt_slot_payload(slot_block)
        if slot_payload is None:
            continue
        candidate = render_prompt_slot_payload(slot_payload)
        _logger.info(
            "[LLM] 从 reasoning_content 提取到有效标签槽位，长度=%d",
            len(candidate),
        )
        return candidate

    # 兼容升级前由模型直接输出的 V2/V3/V4 JSON。
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
            "[LLM] 从 reasoning_content 提取到有效旧 JSON，长度=%d",
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


def _response_metrics(
    data: Dict[str, Any], choice: Optional[Dict[str, Any]] = None,
    *, content: str = "", reasoning: str = "",
) -> Dict[str, Any]:
    """Extract non-sensitive response metadata for diagnostics."""
    usage = data.get("usage") if isinstance(data, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("output_tokens_details")
    if not isinstance(details, dict):
        details = {}
    choice = choice if isinstance(choice, dict) else {}
    return {
        "finish_reason": choice.get("finish_reason") or "-",
        "content_len": len(content or ""),
        "reasoning_len": len(reasoning or ""),
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", "-")),
        "completion_tokens": usage.get(
            "completion_tokens", usage.get("output_tokens", "-"),
        ),
        "reasoning_tokens": details.get(
            "reasoning_tokens", usage.get("reasoning_tokens", "-"),
        ),
        "total_tokens": usage.get("total_tokens", "-"),
    }


def _format_response_metrics(metrics: Dict[str, Any]) -> str:
    return (
        f"finish_reason={metrics['finish_reason']} "
        f"content_len={metrics['content_len']} "
        f"reasoning_len={metrics['reasoning_len']} "
        f"tokens={metrics['prompt_tokens']}/{metrics['completion_tokens']}/"
        f"{metrics['reasoning_tokens']}/{metrics['total_tokens']}"
    )


def _output_budget_exhausted(metrics: Dict[str, Any]) -> bool:
    finish_reason = str(metrics.get("finish_reason") or "").strip().lower()
    return finish_reason in {"length", "max_tokens"}


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
    reasoning_effort: str = "",
    json_response_mode: str = "disabled",
    retry_without_thinking_on_empty: bool = False,
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
    thinking_mode = str(thinking_mode or "auto").strip().lower()
    reasoning_effort = str(reasoning_effort or "").strip().lower()
    json_response_mode = str(json_response_mode or "auto").strip().lower()
    if thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
    elif thinking_mode == "enabled":
        payload["thinking"] = {"type": "enabled"}
        if reasoning_effort in {"low", "medium", "high", "max"}:
            payload["reasoning_effort"] = reasoning_effort
    if structured_output and json_response_mode == "enabled":
        payload["response_format"] = {"type": "json_object"}

    prompt_bytes = len(prompt.encode("utf-8"))
    _logger.info(
        f"[LLM] 调用 {url} model={model} input={prompt_bytes}bytes "
        f"max_tokens={max_tokens} structured={structured_output}"
    )

    overall_start_time = time.time()
    last_error = ""
    removed_optional_fields = set()
    normal_empty_responses = 0
    empty_content_fallback_used = False
    empty_content_fallback_active = False
    max_attempts = 3
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
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
                        metrics = _response_metrics(data)
                        _logger.warning(
                            "[LLM] 响应没有 choices attempt=%d %s",
                            attempt, _format_response_metrics(metrics),
                        )
                        return False, "LLM 返回空的 choices 列表", "", model

                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    message = choice.get("message", {})
                    if not isinstance(message, dict):
                        message = {}
                    content = message.get("content", "") or ""
                    reasoning = (
                        message.get("reasoning_content", "")
                        or data.get("reasoning_content", "")
                        or ""
                    )
                    metrics = _response_metrics(
                        data, choice, content=content, reasoning=reasoning,
                    )

                    if not content and reasoning:
                        content = _extract_final_answer_from_reasoning(reasoning)

                    if not content:
                        _logger.warning(
                            "[LLM] 响应正文为空 attempt=%d %s",
                            attempt, _format_response_metrics(metrics),
                        )
                        if (
                            retry_without_thinking_on_empty
                            and not empty_content_fallback_used
                            and payload.get("thinking", {}).get("type") != "disabled"
                            and _output_budget_exhausted(metrics)
                        ):
                            normal_empty_responses += 1
                            if normal_empty_responses < 2:
                                _logger.warning(
                                    "[LLM] 第1次正常请求未生成正文，"
                                    "保持当前思考设置重试一次"
                                )
                                continue
                            empty_content_fallback_used = True
                            empty_content_fallback_active = True
                            payload["thinking"] = {"type": "disabled"}
                            payload.pop("reasoning_effort", None)
                            payload.pop("response_format", None)
                            # 严格只增加一次关闭思考请求，不继续放大付费调用次数。
                            max_attempts = attempt + 1
                            _logger.warning(
                                "[LLM] 前两次正常请求均未生成正文，"
                                "第三次关闭思考兜底"
                            )
                            continue
                        if _output_budget_exhausted(metrics):
                            return False, "模型推理耗尽输出预算，未生成正文", "", model
                        if str(metrics.get("finish_reason", "")).lower() == "content_filter":
                            return False, "LLM 响应被内容过滤且未生成正文", "", model
                        if reasoning:
                            return False, "LLM 返回空内容且 reasoning 中无完整标签槽位", "", model
                        return False, "LLM 返回空内容", "", model

                    actual_model = data.get("model", model)
                    _logger.info(
                        f"[LLM] 成功 attempt={attempt} total={total_elapsed:.1f}s "
                        f"attempt_header={header_time:.1f}s attempt_body={body_time:.1f}s "
                        f"output_len={len(content)} model={actual_model} "
                        f"{_format_response_metrics(metrics)}"
                    )
                    return True, content, reasoning, actual_model

                elif resp.status in (400, 422):
                    text = await resp.text()
                    removable = tuple(
                        key for key in _optional_fields_to_remove(
                            resp.status, text, payload,
                        )
                        if key not in removed_optional_fields
                    )
                    if empty_content_fallback_active and "thinking" in removable:
                        _logger.warning(
                            "[LLM] 当前接口不支持关闭思考参数，停止空正文兜底 (HTTP %s)",
                            resp.status,
                        )
                        return False, "当前接口不支持关闭思考，空正文兜底未执行", "", model
                    if removable:
                        for key in removable:
                            payload.pop(key, None)
                            removed_optional_fields.add(key)
                        # 参数协商不占用原有的三次瞬时网络重试预算；每个字段最多移除一次。
                        max_attempts += 1
                        _logger.warning(
                            "[LLM] 接口拒绝可选参数 %s，已移除后重试 (HTTP %s)",
                            ",".join(removable), resp.status,
                        )
                        continue
                    _logger.error(f"[LLM] HTTP {resp.status}: {text[:500]}")
                    return False, f"API 请求失败 (HTTP {resp.status})", "", model
                elif resp.status in (429, 502, 503, 504):
                    text = await resp.text()
                    last_error = f"HTTP {resp.status}: {text[:300]}"
                    _logger.warning(f"[LLM] 第{attempt}次失败 ({last_error})")
                    if attempt < max_attempts:
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
            if attempt < max_attempts:
                await asyncio.sleep(2)
                continue
        except aiohttp.ClientConnectorError as e:
            last_error = f"连接失败: {str(e)[:200]}"
            _logger.warning(f"[LLM] 第{attempt}次连接失败 ({last_error})")
            if attempt < max_attempts:
                await asyncio.sleep(2)
                continue
        except Exception as e:
            _logger.error(f"[LLM] 未知错误: {e}", exc_info=True)
            return False, f"API 调用异常: {str(e)[:300]}", "", model

    return False, f"请求重试 {attempt} 次后仍失败: {last_error}", "", model


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


def _split_slot_tags(value: str) -> Tuple[str, ...]:
    raw = str(value or "").strip()
    for closing in ("**", "__", "`"):
        if raw.endswith(closing):
            raw = raw[:-len(closing)].rstrip()
            break
    if raw.lower() in {"", "[]", "none", "null", "-"}:
        return ()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list) and all(
            isinstance(item, str) for item in parsed
        ):
            return tuple(item.strip() for item in parsed if item.strip())

    # 每项按协议都是单一 tag；模型混用竖线和逗号时也可以安全拆分。
    parts = re.split(r"\s*[|,]\s*", raw)
    return tuple(
        part.strip().strip("`\"'").strip()
        for part in parts
        if part.strip().strip("`\"'").strip()
    )


def _slot_count_token(tag: str) -> str:
    normalized = re.sub(r"[\s_]+", "", str(tag or "")).lower()
    wrapper_pairs = (
        ("{{", "}}"), ("[[", "]]"), ("(", ")"),
        ("{", "}"), ("[", "]"),
    )
    changed = True
    while changed and normalized:
        changed = False
        for opening, closing in wrapper_pairs:
            if normalized.startswith(opening) and normalized.endswith(closing):
                normalized = normalized[len(opening):-len(closing)].strip()
                changed = True
                break
    return normalized


def _has_slot_prefix(line: str) -> bool:
    cleaned = re.sub(
        r"^\s*(?:(?:[-*]|\d+[.)]|#{1,6})\s+)?(?:\*\*|__|`)?",
        "", str(line or ""),
    )
    return bool(re.match(
        r"^(?:GLOBAL\b|P\d+|PERSON[_\s-]*\d+)", cleaned,
        re.IGNORECASE,
    ))


def _slot_mapping_to_lines(text: str) -> str:
    """兼容模型把固定槽位误包成 JSON 键值对象的情况。"""
    cleaned = _strip_code_fence(text).strip()
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict) or any(
        str(key).strip().lower() == "people" for key in payload
    ):
        return ""

    def value_text(value: Any) -> Optional[str]:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            return " | ".join(item.strip() for item in value if item.strip())
        return None

    global_value: Optional[str] = None
    person_values: Dict[Tuple[int, bool], str] = {}
    for key, value in payload.items():
        key_text = str(key or "").strip()
        converted = value_text(value)
        if converted is None:
            continue
        if key_text.lower() == "global":
            global_value = converted
            continue
        match = _SLOT_PERSON_LINE_RE.match(f"{key_text}: value")
        if not match:
            continue
        index = int(match.group("short_index") or match.group("long_index"))
        sign = match.group("sign")
        kind = str(
            match.group("short_kind") or match.group("kind") or ""
        ).lower()
        is_positive = sign == "+" or kind.startswith("pos")
        slot_key = (index, is_positive)
        if slot_key in person_values:
            return ""
        person_values[slot_key] = converted

    if global_value is None:
        return ""
    lines = [f"GLOBAL: {global_value}"]
    for index, is_positive in sorted(
        person_values, key=lambda item: (item[0], not item[1]),
    ):
        sign = "+" if is_positive else "-"
        lines.append(f"P{index}{sign}: {person_values[(index, is_positive)]}")
    return "\n".join(lines)


def _looks_like_prompt_slot_output(text: str) -> bool:
    cleaned = _strip_code_fence(text)
    return any(
        _SLOT_GLOBAL_LINE_RE.match(line)
        or _SLOT_PERSON_LINE_RE.match(line)
        for line in cleaned.splitlines()
    )


def _prompt_slot_blocks(text: str) -> Tuple[str, ...]:
    """按 GLOBAL 起始行切分模型输出中的独立槽位块。"""
    lines = _strip_code_fence(text).splitlines()
    starts = [
        index for index, line in enumerate(lines)
        if _SLOT_GLOBAL_LINE_RE.match(line)
    ]
    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        blocks.append("\n".join(lines[start:end]).strip())
    return tuple(blocks)


def _parse_content_slot_payload(text: str) -> Optional[Dict[str, Any]]:
    """解析正文槽位；多个草稿块存在时只接受最后一个完整块。"""
    direct = parse_prompt_slot_payload(text)
    if direct is not None:
        return direct
    mapping_lines = _slot_mapping_to_lines(text)
    if mapping_lines:
        mapped = parse_prompt_slot_payload(mapping_lines)
        if mapped is not None:
            _logger.info("[LLM] 已解析 JSON 包装的本地标签槽位")
            return mapped
    blocks = _prompt_slot_blocks(text)
    if len(blocks) <= 1:
        return None
    latest = parse_prompt_slot_payload(blocks[-1])
    if latest is not None:
        _logger.info(
            "[LLM] 正文包含 %d 个标签槽位块，已采用最后一个完整块",
            len(blocks),
        )
    return latest


def _slot_person_count(global_tags: Tuple[str, ...]) -> Optional[int]:
    gender_total = 0
    saw_gender_count = False
    saw_open_ended_count = False
    explicit_people: Optional[int] = None
    for tag in global_tags:
        normalized = _slot_count_token(tag)
        gender_match = re.fullmatch(
            r"(\d+)(\+?)(?:girls?|boys?|others?)", normalized,
        )
        if gender_match:
            gender_total += int(gender_match.group(1))
            saw_gender_count = True
            saw_open_ended_count = (
                saw_open_ended_count or gender_match.group(2) == "+"
            )
            continue
        people_match = re.fullmatch(r"(\d+)people", normalized)
        if people_match:
            explicit_people = int(people_match.group(1))
    if explicit_people is not None:
        return explicit_people
    if saw_open_ended_count:
        return None
    return gender_total if saw_gender_count else None


def _slot_gender_counts(global_tags: Tuple[str, ...]) -> Dict[str, int]:
    counts = {"1girl": 0, "1boy": 0, "1other": 0}
    suffix_to_tag = {
        "girl": "1girl", "girls": "1girl",
        "boy": "1boy", "boys": "1boy",
        "other": "1other", "others": "1other",
    }
    for tag in global_tags:
        normalized = _slot_count_token(tag)
        match = re.fullmatch(r"(\d+)\+?(girls?|boys?|others?)", normalized)
        if match:
            counts[suffix_to_tag[match.group(2)]] += int(match.group(1))
    return counts


def _slot_open_ended_gender_tags(
    global_tags: Tuple[str, ...],
) -> Tuple[str, ...]:
    suffix_to_tag = {
        "girl": "1girl", "girls": "1girl",
        "boy": "1boy", "boys": "1boy",
        "other": "1other", "others": "1other",
    }
    result = []
    for tag in global_tags:
        normalized = _slot_count_token(tag)
        match = re.fullmatch(r"\d+\+(girls?|boys?|others?)", normalized)
        if match:
            result.append(suffix_to_tag[match.group(1)])
    return tuple(dict.fromkeys(result))


def _slot_person_gender_hints(tags: Tuple[str, ...]) -> Tuple[str, ...]:
    aliases = {
        "1girl": "1girl", "girl": "1girl", "woman": "1girl", "female": "1girl",
        "1boy": "1boy", "boy": "1boy", "man": "1boy", "male": "1boy",
        "1other": "1other", "other": "1other",
    }
    return tuple(sorted({
        aliases[normalized]
        for tag in tags
        if (normalized := _slot_count_token(tag)) in aliases
    }))


def _is_person_slot_count_tag(tag: str) -> bool:
    normalized = _slot_count_token(tag)
    return normalized == "solo" or bool(re.fullmatch(
        r"(?:\d+\+?(?:girls?|boys?|others?)|\d+people)", normalized,
    ))


def _normalize_slot_person_counts(
    global_tags: Tuple[str, ...],
    positive_slots: Dict[int, Tuple[str, ...]],
) -> Optional[Dict[int, Tuple[str, ...]]]:
    """为可确定性别的多人 V4 人物槽补齐单人计数标签。"""
    if len(positive_slots) <= 1:
        return positive_slots

    expected = _slot_gender_counts(global_tags)
    open_ended = _slot_open_ended_gender_tags(global_tags)
    expected_total = _slot_person_count(global_tags)
    resolved: Dict[int, str] = {}
    unresolved = []
    for index, tags in positive_slots.items():
        hints = _slot_person_gender_hints(tags)
        if len(hints) > 1:
            return None
        if hints:
            resolved[index] = hints[0]
        else:
            unresolved.append(index)

    if not unresolved:
        resolved_counts = {"1girl": 0, "1boy": 0, "1other": 0}
        for hint in resolved.values():
            resolved_counts[hint] += 1
        expected_kinds = {
            tag for tag, count in expected.items() if count > 0
        }
        resolved_kinds = {
            tag for tag, count in resolved_counts.items() if count > 0
        }
        if expected_kinds and expected_kinds != resolved_kinds:
            return None
        expected = resolved_counts
    else:
        if open_ended:
            extra_people = len(positive_slots) - sum(expected.values())
            if extra_people < 0:
                return None
            if extra_people:
                if len(open_ended) != 1:
                    return None
                expected[open_ended[0]] += extra_people
        generic_people_count = (
            sum(expected.values()) == 0
            and expected_total == len(positive_slots)
        )
        if sum(expected.values()) != len(positive_slots) and not generic_people_count:
            return None
        if generic_people_count:
            return None

    remaining = dict(expected)
    for hint in resolved.values():
        remaining[hint] -= 1
    if any(count < 0 for count in remaining.values()):
        return None

    possible = [tag for tag, count in remaining.items() if count > 0]
    if unresolved and len(possible) == 1 and remaining[possible[0]] == len(unresolved):
        for index in unresolved:
            resolved[index] = possible[0]
            remaining[possible[0]] -= 1
    if unresolved and any(index not in resolved for index in unresolved):
        return None
    if any(remaining.values()):
        return None

    normalized_slots: Dict[int, Tuple[str, ...]] = {}
    for index, tags in positive_slots.items():
        count_tag = resolved.get(index)
        if not count_tag:
            normalized_slots[index] = tags
            continue
        redundant_identity = {
            "1girl": "girl", "1boy": "boy", "1other": "other",
        }[count_tag]
        without_duplicate = tuple(
            tag for tag in tags
            if not _is_person_slot_count_tag(tag)
            and _slot_count_token(tag) != redundant_identity
        )
        normalized_slots[index] = (count_tag,) + without_duplicate
    return normalized_slots


def _rebuild_global_person_counts(
    global_tags: Tuple[str, ...],
    positive_slots: Dict[int, Tuple[str, ...]],
) -> Tuple[str, ...]:
    if len(positive_slots) <= 1:
        return global_tags

    counts = {"1girl": 0, "1boy": 0, "1other": 0}
    for tags in positive_slots.values():
        hints = _slot_person_gender_hints(tags)
        if len(hints) != 1:
            return global_tags
        counts[hints[0]] += 1

    def count_tag(tag: str, count: int) -> str:
        if tag == "1girl" and count >= 6:
            return "6+girls"
        if tag == "1boy" and count >= 6:
            return "6+boys"
        suffix = {"1girl": "girl", "1boy": "boy", "1other": "other"}[tag]
        return f"{count}{suffix}{'s' if count != 1 else ''}"

    person_tags = tuple(
        count_tag(tag, count) for tag, count in counts.items() if count > 0
    )
    count_indices = [
        index for index, tag in enumerate(global_tags)
        if _is_person_slot_count_tag(tag)
    ]
    insert_at = count_indices[0] if count_indices else 0
    without_counts = tuple(
        tag for tag in global_tags if not _is_person_slot_count_tag(tag)
    )
    rebuilt = (
        without_counts[:insert_at]
        + person_tags
        + without_counts[insert_at:]
    )
    if rebuilt != global_tags:
        _logger.info(
            "[LLM] 已按 %d 个人物槽修正 GLOBAL 人数: %s -> %s",
            len(positive_slots),
            " | ".join(global_tags),
            " | ".join(rebuilt),
        )
    return rebuilt


def parse_prompt_slot_payload(text: str) -> Optional[Dict[str, Any]]:
    """解析无 JSON 的固定标签槽位，并归一成内部字典。"""
    cleaned = _strip_code_fence(text).strip()
    if not cleaned:
        return None

    global_tags: Optional[Tuple[str, ...]] = None
    positive_slots: Dict[int, Tuple[str, ...]] = {}
    negative_slots: Dict[int, Tuple[str, ...]] = {}
    saw_slot = False

    for line in cleaned.splitlines():
        global_match = _SLOT_GLOBAL_LINE_RE.match(line)
        if global_match:
            saw_slot = True
            if global_tags is not None:
                return None
            global_tags = _split_slot_tags(global_match.group("value"))
            continue

        person_match = _SLOT_PERSON_LINE_RE.match(line)
        if not person_match:
            if _has_slot_prefix(line):
                return None
            continue
        saw_slot = True
        index_text = (
            person_match.group("short_index")
            or person_match.group("long_index")
        )
        index = int(index_text)
        if index < 1:
            return None
        sign = person_match.group("sign")
        kind = str(
            person_match.group("short_kind")
            or person_match.group("kind")
            or ""
        ).lower()
        is_positive = sign == "+" or kind.startswith("pos")
        target = positive_slots if is_positive else negative_slots
        if index in target:
            return None
        target[index] = _split_slot_tags(person_match.group("value"))

    if not saw_slot or not global_tags:
        return None

    person_indices = sorted(set(positive_slots) | set(negative_slots))
    if person_indices:
        if person_indices != list(range(1, person_indices[-1] + 1)):
            return None
        if any(not positive_slots.get(index) for index in person_indices):
            return None
    elif any(_is_person_slot_count_tag(tag) for tag in global_tags):
        return None

    if len(person_indices) > 1 and any(
        _slot_count_token(tag) == "solo" for tag in global_tags
    ):
        return None

    normalized_slots = _normalize_slot_person_counts(global_tags, positive_slots)
    if normalized_slots is None:
        return None
    positive_slots = normalized_slots
    global_tags = _rebuild_global_person_counts(global_tags, positive_slots)

    people = [
        {
            "prompt": list(positive_slots[index]),
            "negative_prompt": list(negative_slots.get(index, ())),
        }
        for index in person_indices
    ]
    return {"global": list(global_tags), "people": people}


def render_prompt_slot_payload(obj: Dict[str, Any]) -> str:
    """把已验证的槽位字典渲染成规范文本，供 reasoning 安全回收。"""
    global_tags = _normalize_tag_sequence(obj.get("global"))
    if not global_tags:
        return ""
    lines = [f"GLOBAL: {' | '.join(global_tags)}"]
    raw_people = obj.get("people")
    if isinstance(raw_people, list):
        for index, person in enumerate(raw_people, start=1):
            if not isinstance(person, dict):
                return ""
            positive = _normalize_tag_sequence(person.get("prompt"))
            negative = _normalize_tag_sequence(person.get("negative_prompt"))
            if not positive:
                return ""
            lines.append(f"P{index}+: {' | '.join(positive)}")
            lines.append(f"P{index}-: {' | '.join(negative)}")
    return "\n".join(lines)


def _casefold_mapping_value(
    mapping: Dict[str, Any], key: str,
) -> Tuple[bool, Any]:
    expected = key.casefold()
    for raw_key, value in mapping.items():
        if str(raw_key).casefold() == expected:
            return True, value
    return False, None


def _normalize_structured_json_keys(obj: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(obj)
    for key in ("global", "people", "prompt", "intent", "continuity"):
        found, value = _casefold_mapping_value(obj, key)
        if found:
            normalized[key] = value

    raw_people = normalized.get("people")
    if isinstance(raw_people, list):
        people = []
        for item in raw_people:
            if not isinstance(item, dict):
                people.append(item)
                continue
            person = dict(item)
            for key in ("prompt", "negative_prompt"):
                found, value = _casefold_mapping_value(item, key)
                if found:
                    person[key] = value
            people.append(person)
        normalized["people"] = people
    return normalized


def parse_structured_prompt_payload(text: str) -> Optional[Dict[str, Any]]:
    """兼容解析升级前由 LLM 直接输出的 JSON。"""
    cleaned = _strip_code_fence(text).strip()
    if not cleaned:
        return None

    candidates = [cleaned]
    lowered = cleaned.lower()
    if any(token in lowered for token in ('"prompt"', '"global"', '"people"')):
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
        obj = _normalize_structured_json_keys(obj)

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

    if len(people_list) > 1 and any(
        _slot_count_token(tag) == "solo" for tag in global_tags
    ):
        return None
    if len(people_list) > 1:
        positive_slots = {
            index: person.positive_tags
            for index, person in enumerate(people_list, start=1)
        }
        normalized_slots = _normalize_slot_person_counts(
            global_tags, positive_slots,
        )
        if normalized_slots is None:
            return None
        people_list = [
            PersonPrompt(
                positive_tags=normalized_slots[index],
                negative_tags=person.negative_tags,
            )
            for index, person in enumerate(people_list, start=1)
        ]
        global_tags = _rebuild_global_person_counts(
            global_tags, normalized_slots,
        )
    people = tuple(people_list)

    expected_people = _slot_person_count(global_tags)
    if expected_people is not None and expected_people != len(people):
        return None

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
    obj = _parse_content_slot_payload(text)
    if obj:
        rendered = _render_from_v2(obj)
        if rendered:
            return rendered

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
    slot_obj = _parse_content_slot_payload(text)
    if slot_obj:
        structured = parse_structured_prompt(
            slot_obj, intent=intent, continuity=continuity,
        )
        if structured:
            return GeneratedPrompt(
                flat_prompt=render_structured_prompt_flat(
                    structured.global_tags, structured.people,
                ),
                structured_prompt=structured,
            )

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

    cleaned = _strip_code_fence(text).strip()
    if _looks_like_prompt_slot_output(cleaned):
        return GeneratedPrompt(flat_prompt="")
    if cleaned.startswith("{"):
        return GeneratedPrompt(flat_prompt="")
    return GeneratedPrompt(flat_prompt=_cleanup_plain_llm_prompt(text))


# ================================================================
# 后处理排序（从 prompt_postprocessor.py 迁移）
# ================================================================

_COUNT_RE = re.compile(
    r"^(?:solo|\d+\+?girls|\d+\+?boys|\d+people|1girl|1boy)$",
    re.IGNORECASE,
)
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
