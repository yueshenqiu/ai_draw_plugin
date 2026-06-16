# -*- coding: utf-8 -*-
"""插件组件层 — 命令处理逻辑。"""

from ..instance import get_plugin_instance, is_plugin_ready
from .command import (
    handle_ad_help,
    handle_ad_plugin_toggle,
    handle_ad_recall_control,
    handle_ad_nsfw_control,
    handle_ad_send_mode,
    handle_ad_prompt_show,
    handle_ad_admin_toggle,
    handle_ad_switch_model,
    handle_ad_switch_size,
    handle_ad_switch_artist,
    handle_ad_manual_recall,
    handle_ad_style,
    handle_dr0_ref_draw,
    handle_dr0_draw,
    handle_ad_ref_draw,
    handle_ad_draw,
    handle_ad_web_draw,
    ad_workflow,
)

__all__ = [
    "get_plugin_instance",
    "is_plugin_ready",
    "handle_ad_help",
    "handle_ad_plugin_toggle",
    "handle_ad_recall_control",
    "handle_ad_nsfw_control",
    "handle_ad_send_mode",
    "handle_ad_prompt_show",
    "handle_ad_admin_toggle",
    "handle_ad_switch_model",
    "handle_ad_switch_size",
    "handle_ad_switch_artist",
    "handle_ad_manual_recall",
    "handle_ad_style",
    "handle_dr0_ref_draw",
    "handle_dr0_draw",
    "handle_ad_ref_draw",
    "handle_ad_draw",
    "handle_ad_web_draw",
    "ad_workflow",
]
