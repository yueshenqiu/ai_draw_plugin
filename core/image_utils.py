# -*- coding: utf-8 -*-
"""图片处理工具：base64 保存/提取/清理、合并转发发送、展示文案。

合并自：
- core/utils/image_url_helper.py
- core/utils/display_message_helper.py
"""

import base64
import binascii
from io import BytesIO
import logging
import os
import re
import tempfile
import time
import uuid
import warnings
from typing import Optional, List, Tuple
from urllib.parse import urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError

_logger = logging.getLogger("ai_draw_plugin")

# ---- 常量 ----
AI_DRAW_IMAGE_DISPLAY_PREFIX = "[AI绘图:"
AI_DRAW_IMAGE_DISPLAY_FALLBACK = "[AI绘图]"

_PLUGIN_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PROJECT_ROOT_DIR = os.path.abspath(os.path.join(_PLUGIN_ROOT_DIR, "..", ".."))
_IMAGE_OUTPUT_DIR = os.path.join(_PROJECT_ROOT_DIR, "data", "ai_draw_plugin", "generated_images")
_IMAGE_OUTPUT_FALLBACK_DIRS = (
    os.path.join(_PLUGIN_ROOT_DIR, "temp_images", "generated_images"),
    os.path.join(tempfile.gettempdir(), "ai_draw_plugin", "generated_images"),
)
_QUEUE_SPOOL_DIRS = (
    os.path.join(tempfile.gettempdir(), "ai_draw_plugin", "queue_spool"),
    os.path.join(_PLUGIN_ROOT_DIR, "temp_images", "queue_spool"),
)
_QUEUE_SPOOL_PREFIX = "ai_draw_queue_"
_QUEUE_SPOOL_MAX_AGE_SECONDS = 2 * 60 * 60
_QUEUE_SPOOL_MAX_FILES = 100

_MAX_FILE_AGE_SECONDS = 30 * 60
_MAX_FILE_COUNT = 80
_CLEANUP_INTERVAL_SECONDS = 5 * 60
_last_cleanup_ts: dict[str, float] = {}

# 参考图/本地图片的硬限制。限制同时作用于压缩后的字节数与解码后的像素数，
# 防止超大文件和解压炸弹在进入 Provider 前消耗过量内存。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_777_216
_ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
_DATA_URI_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/webp": "WEBP",
}
_DATA_URI_PATTERN = re.compile(
    r"^data:([^;,]+)(?:;[^,]*)?;base64,(.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*(.+?)\s*\)",
    flags=re.IGNORECASE,
)


# ---- 图片格式检测 ----

def detect_image_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "webp"
    return ""


def validate_image_bytes(
    image_bytes: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Optional[str]:
    """验证图片字节并返回 ``PNG/JPEG/WEBP``；不合法或超限时返回 ``None``。"""
    if not isinstance(image_bytes, (bytes, bytearray)):
        return None
    size = len(image_bytes)
    if size <= 0 or size > max_bytes:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if image_format not in _ALLOWED_IMAGE_FORMATS:
                    return None
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    return None
                image.verify()

            # verify() 只检查文件结构，不会完整解码像素；重新打开并 load()，
            # 让截断数据、损坏的压缩流等问题在进入 Provider 前暴露。
            with Image.open(BytesIO(image_bytes)) as image:
                if str(image.format or "").upper() != image_format:
                    return None
                image.load()
    except (UnidentifiedImageError, OSError, EOFError, SyntaxError, ValueError,
            MemoryError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        return None

    return image_format


def decode_base64_image(
    image_base64: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Optional[Tuple[bytes, str]]:
    """受限解码 base64 图片，返回 ``(图片字节, 规范格式)``。

    支持纯 base64、``base64://`` 和图片 data URI。解码前先按理论长度
    计算输出大小，避免恶意输入诱发超大内存分配。
    """
    if not isinstance(image_base64, str) or max_bytes <= 0 or max_pixels <= 0:
        return None

    payload = image_base64.strip()
    declared_format: Optional[str] = None
    if payload.lower().startswith("base64://"):
        payload = payload[len("base64://"):]
    elif payload.lower().startswith("data:"):
        match = _DATA_URI_PATTERN.fullmatch(payload)
        if not match:
            return None
        declared_format = _DATA_URI_FORMATS.get(match.group(1).strip().lower())
        if not declared_format:
            return None
        payload = match.group(2)

    max_encoded_length = ((max_bytes + 2) // 3) * 4
    # 常见 MIME 换行只增加少量空白；先限制原始文本长度，再创建去空白副本。
    max_input_length = max_encoded_length + max(4096, max_encoded_length // 16)
    if not payload or len(payload) > max_input_length:
        return None

    payload = re.sub(r"\s+", "", payload)
    if not payload or len(payload) > max_encoded_length:
        return None

    remainder = len(payload) % 4
    if remainder == 1:
        return None
    padded_payload = payload + ("=" * ((4 - remainder) % 4))
    padding_size = len(padded_payload) - len(padded_payload.rstrip("="))
    if padding_size > 2:
        return None
    decoded_size = (len(padded_payload) // 4) * 3 - padding_size
    if decoded_size <= 0 or decoded_size > max_bytes:
        return None

    try:
        image_bytes = base64.b64decode(padded_payload, validate=True)
    except (ValueError, TypeError, binascii.Error):
        return None

    image_format = validate_image_bytes(
        image_bytes,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    if not image_format or (
        declared_format is not None and declared_format != image_format
    ):
        return None
    return image_bytes, image_format


def normalize_base64_image(
    image_base64: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Optional[str]:
    """校验并返回无前缀、无空白、带标准填充的纯 base64 图片。"""
    decoded = decode_base64_image(
        image_base64,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    if not decoded:
        return None
    image_bytes, _ = decoded
    return base64.b64encode(image_bytes).decode("ascii")


def load_image_file_as_base64(
    file_path: os.PathLike | str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Optional[str]:
    """受限读取并验证本地图片，成功时返回纯 base64 字符串。"""
    try:
        path = os.fspath(file_path)
        file_size = os.path.getsize(path)
        if file_size <= 0 or file_size > max_bytes:
            _logger.warning(f"[ai_draw] 拒绝读取大小异常的本地图片: size={file_size}")
            return None
        with open(path, "rb") as file_obj:
            image_bytes = file_obj.read(max_bytes + 1)
    except (OSError, ValueError, TypeError) as exc:
        _logger.warning(f"[ai_draw] 读取本地图片失败: {exc}")
        return None

    if len(image_bytes) > max_bytes or not validate_image_bytes(
        image_bytes, max_bytes=max_bytes, max_pixels=max_pixels,
    ):
        _logger.warning("[ai_draw] 拒绝读取无效或超限的本地图片")
        return None
    return base64.b64encode(image_bytes).decode("ascii")


def _trusted_queue_spool_path(file_path: os.PathLike | str) -> Optional[str]:
    """Return a normalized path only when it belongs to the queue spool."""
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(file_path))))
    except (OSError, TypeError, ValueError):
        return None

    basename = os.path.basename(candidate)
    if not basename.startswith(_QUEUE_SPOOL_PREFIX):
        return None
    if os.path.splitext(basename)[1].lower() not in {".png", ".jpg", ".webp"}:
        return None

    for raw_root in _QUEUE_SPOOL_DIRS:
        root = os.path.normcase(os.path.realpath(os.path.abspath(raw_root)))
        try:
            if os.path.commonpath((candidate, root)) == root:
                return candidate
        except ValueError:
            continue
    return None


def cleanup_queue_image_spool(*, remove_all: bool = False) -> int:
    """Remove stale queue-spool images, or all orphaned files at startup."""
    now = time.time()
    removed = 0
    for raw_root in _QUEUE_SPOOL_DIRS:
        root = os.path.realpath(os.path.abspath(raw_root))
        try:
            entries = []
            for entry in os.scandir(root):
                trusted_path = _trusted_queue_spool_path(entry.path)
                if trusted_path is None or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    entries.append((trusted_path, entry.stat(follow_symlinks=False).st_mtime))
                except FileNotFoundError:
                    continue
        except OSError:
            continue

        entries.sort(key=lambda item: item[1], reverse=True)
        for index, (path, modified_at) in enumerate(entries):
            expired = now - modified_at >= _QUEUE_SPOOL_MAX_AGE_SECONDS
            over_limit = index >= _QUEUE_SPOOL_MAX_FILES
            if not (remove_all or expired or over_limit):
                continue
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                _logger.warning(f"[ai_draw] 清理队列参考图失败: {exc}")
    return removed


def save_queue_image_spool(image_base64: str) -> Optional[str]:
    """Persist a validated reference image so queued jobs retain only a path."""
    decoded = decode_base64_image(image_base64)
    if not decoded:
        _logger.warning("[ai_draw] 拒绝暂存无效或超限的队列参考图")
        return None
    image_bytes, image_type = decoded
    extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[image_type]
    file_name = f"{_QUEUE_SPOOL_PREFIX}{uuid.uuid4().hex}.{extension}"

    last_error: Optional[Exception] = None
    for raw_root in _QUEUE_SPOOL_DIRS:
        root = os.path.realpath(os.path.abspath(raw_root))
        file_path = os.path.join(root, file_name)
        try:
            os.makedirs(root, exist_ok=True)
            with open(file_path, "xb") as file_obj:
                file_obj.write(image_bytes)
            return file_path
        except (OSError, ValueError) as exc:
            last_error = exc
            remove_queue_image_spool(file_path)
    _logger.error(f"[ai_draw] 队列参考图临时目录不可写: {last_error}")
    return None


def load_queue_image_spool(file_path: os.PathLike | str) -> Optional[str]:
    """Load one validated queue-spool image without accepting other paths."""
    try:
        raw_path = os.path.abspath(os.fspath(file_path))
    except (OSError, TypeError, ValueError):
        return None
    if os.path.islink(raw_path):
        return None
    trusted_path = _trusted_queue_spool_path(raw_path)
    if trusted_path is None or not os.path.isfile(trusted_path):
        return None
    return load_image_file_as_base64(trusted_path)


def remove_queue_image_spool(file_path: os.PathLike | str) -> bool:
    """Idempotently remove one queue-spool image without accepting other paths."""
    trusted_path = _trusted_queue_spool_path(file_path)
    if trusted_path is None:
        return False
    try:
        os.remove(trusted_path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        _logger.warning(f"[ai_draw] 删除队列参考图失败: {exc}")
        return False


# ---- 文件清理 ----

def _maybe_cleanup_generated_files(output_dir: str):
    now = time.time()
    if now - _last_cleanup_ts.get(output_dir, 0.0) < _CLEANUP_INTERVAL_SECONDS:
        return
    _last_cleanup_ts[output_dir] = now
    _cleanup_generated_files(now, output_dir)


def _cleanup_generated_files(now: float, output_dir: str):
    try:
        entries: List[Tuple[str, float]] = []
        for entry in os.scandir(output_dir):
            if entry.is_file():
                try:
                    stat = entry.stat()
                    entries.append((entry.path, stat.st_mtime))
                except FileNotFoundError:
                    continue
    except OSError:
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
    decoded = decode_base64_image(image_base64)
    if not decoded:
        _logger.error("[ai_draw] 拒绝保存无效或超限的Base64图片")
        return None
    image_bytes, image_type = decoded
    extension = {
        "PNG": "png",
        "JPEG": "jpg",
        "WEBP": "webp",
    }[image_type]
    file_name = f"ai_draw_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{extension}"
    candidates = (_IMAGE_OUTPUT_DIR, *_IMAGE_OUTPUT_FALLBACK_DIRS)
    attempted = set()
    last_error: Optional[Exception] = None
    for output_dir in candidates:
        normalized_dir = os.path.abspath(output_dir)
        if normalized_dir in attempted:
            continue
        attempted.add(normalized_dir)
        file_path = os.path.join(normalized_dir, file_name)
        try:
            os.makedirs(normalized_dir, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(image_bytes)
            _maybe_cleanup_generated_files(normalized_dir)
            _logger.debug(f"[ai_draw] 图片已保存: {file_path}")
            return file_path
        except (OSError, ValueError) as exc:
            last_error = exc
            continue

    _logger.error(f"[ai_draw] 所有可信输出目录均不可写: {last_error}")
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
    """提取并校验 API 返回的图片 base64 或 HTTP(S) URL。"""
    if not isinstance(result, str) or not result.strip():
        return None
    value = result.strip()

    for match in _MARKDOWN_IMAGE_PATTERN.finditer(value):
        target = match.group(1).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        normalized_url = _normalize_http_url(target)
        if normalized_url:
            return normalized_url
        normalized_base64 = normalize_base64_image(target)
        if normalized_base64:
            return normalized_base64

    normalized_url = _normalize_http_url(value)
    if normalized_url:
        return normalized_url
    return normalize_base64_image(value)


def _normalize_http_url(value: str) -> Optional[str]:
    """返回结构完整的 HTTP(S) URL；拒绝空白、凭据和畸形端口。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in parsed.netloc
        ):
            return None
        parsed.port  # 访问属性以触发畸形端口校验。
    except ValueError:
        return None
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
