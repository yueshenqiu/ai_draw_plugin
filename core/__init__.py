# -*- coding: utf-8 -*-
"""AI Draw 图片生成插件 — 核心模块"""

from .session_state import SessionStateManager, session_state
from .generator import (
    load_models_config,
    get_model_config,
    generate_and_send,
    generate_image,
    send_image_result,
    send_image_forward,
    send_image_direct,
    fetch_recent_messages,
    fetch_ref_image,
    extract_image_from_message,
    get_session_info_from_kwargs,
    schedule_auto_recall,
)
from .prompt_engine import (
    call_custom_llm_api,
    has_custom_api_config,
    get_custom_api_config,
    cleanup_llm_prompt,
    parse_prompt_from_structured_output,
    parse_structured_prompt_payload,
    normalize_prompt_order,
    remove_selfie_appearance_tags,
    user_mentions_appearance,
    build_current_time_context,
)
from .selfie_engine import (
    detect_selfie_mode,
    detect_selfie_prefix,
    detect_selfie_from_output,
    get_selfie_hint,
    merge_selfie_prompt,
)
from .image_utils import (
    save_base64_image_to_file,
    process_api_response,
    build_action_image_display_message,
    is_nai_action_image_display_message,
)
from .random_scene import (
    normalize_random_scene_description,
    is_random_scene_too_similar,
    get_random_scene_similarity_score,
)

__all__ = [
    "session_state",
    "SessionStateManager",
    "load_models_config",
    "get_model_config",
    "generate_and_send",
    "generate_image",
    "send_image_result",
    "send_image_forward",
    "send_image_direct",
    "fetch_recent_messages",
    "fetch_ref_image",
    "extract_image_from_message",
    "get_session_info_from_kwargs",
    "schedule_auto_recall",
    "call_custom_llm_api",
    "has_custom_api_config",
    "get_custom_api_config",
    "cleanup_llm_prompt",
    "parse_prompt_from_structured_output",
    "parse_structured_prompt_payload",
    "normalize_prompt_order",
    "remove_selfie_appearance_tags",
    "user_mentions_appearance",
    "build_current_time_context",
    "detect_selfie_mode",
    "detect_selfie_prefix",
    "detect_selfie_from_output",
    "get_selfie_hint",
    "merge_selfie_prompt",
    "save_base64_image_to_file",
    "process_api_response",
    "build_action_image_display_message",
    "is_nai_action_image_display_message",
    "normalize_random_scene_description",
    "is_random_scene_too_similar",
    "get_random_scene_similarity_score",
]
