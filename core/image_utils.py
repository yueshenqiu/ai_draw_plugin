# -*- coding: utf-8 -*-
"""图片处理工具：base64 保存/提取/清理、合并转发发送、展示文案。

合并自：
- core/utils/image_url_helper.py
- core/utils/display_message_helper.py
"""

import base64
import logging
import os
import re
import time
import uuid
from typing import Optional, List, Tuple

_logger = logging.getLogger("ai_draw_plugin")

# ---- 常量 ----
AI_DRAW_IMAGE_DISPLAY_PREFIX = "[AI绘图:"
AI_DRAW_IMAGE_DISPLAY_FALLBACK = "[AI绘图]"

_PLUGIN_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PROJECT_ROOT_DIR = os.path.abspath(os.path.join(_PLUGIN_ROOT_DIR, "..", ".."))
_IMAGE_OUTPUT_DIR = os.path.join(_PROJECT_ROOT_DIR, "data", "ai_draw_plugin", "generated_images")
os.makedirs(_IMAGE_OUTPUT_DIR, exist_ok=True)

_MAX_FILE_AGE_SECONDS = 30 * 60
_MAX_FILE_COUNT = 80
_CLEANUP_INTERVAL_SECONDS = 5 * 60
_last_cleanup_ts = 0.0


# ---- 图片格式检测 ----

def detect_image_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "webp"
    return "png"


# ---- 文件清理 ----

def _maybe_cleanup_generated_files():
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL_SECONDS:
        return
    _last_cleanup_ts = now
    _cleanup_generated_files(now)


def _cleanup_generated_files(now: float):
    try:
        entries: List[Tuple[str, float]] = []
        for entry in os.scandir(_IMAGE_OUTPUT_DIR):
            if entry.is_file():
                try:
                    stat = entry.stat()
                    entries.append((entry.path, stat.st_mtime))
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        return

    removed = 0
    remaining: List[Tuple[str, float]] = []
    for path, mtime in entries:
        if now - mtime > _MAX_FILE_AGE_SECONDS:
            try:
                os.remove(path)
                removed += 1
            except (FileNotFoundError, Exception):
                continue
        else:
            remaining.append((path, mtime))

    if len(remaining) > _MAX_FILE_COUNT:
        overflow = len(remaining) - _MAX_FILE_COUNT
        remaining.sort(key=lambda item: item[1])
        for path, _ in remaining[:overflow]:
            try:
                os.remove(path)
                removed += 1
            except (FileNotFoundError, Exception):
                continue

    if removed:
        _logger.debug(f"[ai_draw] 已清理 {removed} 个临时图片文件")


# ---- Base64 保存 ----

def save_base64_image_to_file(image_base64: str) -> Optional[str]:
    _maybe_cleanup_generated_files()
    try:
        data = image_base64.split(",", 1)[1] if image_base64.startswith("data:image") else image_base64
        image_bytes = base64.b64decode(data)
    except Exception as e:
        _logger.error(f"[ai_draw] 解码Base64图片失败: {e}")
        return None

    image_type = detect_image_type(image_bytes)
    extension = "jpg" if image_type == "jpeg" else image_type
    file_name = f"ai_draw_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{extension}"
    file_path = os.path.join(_IMAGE_OUTPUT_DIR, file_name)

    try:
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        _logger.debug(f"[ai_draw] 图片已保存: {file_path}")
        return file_path
    except Exception as e:
        _logger.error(f"[ai_draw] 保存图片失败: {e}")
        return None


# ---- 图片展示文案 ----

def build_action_image_display_message(description: Optional[str]) -> str:
    normalized = " ".join(str(description or "").split())
    if not normalized:
        return AI_DRAW_IMAGE_DISPLAY_FALLBACK
    return f"{AI_DRAW_IMAGE_DISPLAY_PREFIX}{normalized}]"


def is_ai_draw_image_display_message(text: Optional[str]) -> bool:
    if not isinstance(text, str):
        return False
    normalized = text.strip()
    return (
        normalized == AI_DRAW_IMAGE_DISPLAY_FALLBACK
        or normalized.startswith(AI_DRAW_IMAGE_DISPLAY_PREFIX)
    )


# ---- API 响应解析 ----

def process_api_response(result: str) -> Optional[str]:
    """处理 API 响应，提取图片数据（base64 或 URL）。"""
    if not result:
        return None
    match = re.search(r"!\[[^\]]*\]\(data:image/\w+;base64,([A-Za-z0-9+/=]+)\)", result)
    if match:
        return match.group(1)
    match = re.search(r"!\[[^\]]*\]\((https?://[^)]+)\)", result)
    if match:
        return match.group(1)
    if result.startswith(("http://", "https://")):
        return result
    if result.startswith(("iVBORw", "/9j/", "UklGR", "R0lGOD")):
        return result
    if "," in result and result.startswith("data:image"):
        return result.split(",", 1)[1]
    return result
