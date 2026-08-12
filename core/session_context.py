# -*- coding: utf-8 -*-
"""统一解析命令与 Tool 的会话上下文。"""

from typing import Any, Dict


def extract_session_info(kwargs: Dict[str, Any]) -> Dict[str, str]:
    message = kwargs.get("message", {})
    if isinstance(message, dict) and message:
        platform = str(message.get("platform", "") or "")
        info = message.get("message_info", {}) or {}
        group_info = info.get("group_info") or {}
        user_info = info.get("user_info") or {}
        user_id = str(user_info.get("user_id", "") or "")
        group_id = str(group_info.get("group_id") or "")
    else:
        platform = str(kwargs.get("platform", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")

    chat_id = group_id or user_id or str(kwargs.get("stream_id", "") or "")
    chat_type = "group" if group_id else "private"
    return {
        "platform": platform,
        "chat_id": chat_id,
        "user_id": user_id,
        "chat_type": chat_type,
    }
