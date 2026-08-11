# -*- coding: utf-8 -*-
"""图片生成核心：调度 provider、构建请求、解析响应、发送结果。

从 plugin.py 的生图流程和撤回逻辑提取。
"""

import asyncio
import base64
from collections import OrderedDict
import ipaddress
import os
import random
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from ..instance import get_plugin_instance
from ..providers import get_provider_class
from .image_utils import (
    MAX_IMAGE_BYTES,
    decode_base64_image,
    load_image_file_as_base64,
    normalize_base64_image,
    process_api_response,
    validate_image_bytes,
)

_TEMP_IMAGES_DIR = Path(__file__).resolve().parent.parent / "temp_images"
_MAX_TEMP_FILES = 10

_TRACKED_MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_TRACKED_MESSAGES = 1000
_tracked_messages: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_tracked_messages_lock = threading.RLock()

_MAX_REFERENCE_REDIRECTS = 4
_ALLOWED_REMOTE_IMAGE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/pjpeg",
    "image/webp", "image/x-png",
})

# 缓存 bot 真实 QQ 号和昵称（用于合并转发，避免伪造身份触发风控）
_cached_bot_self_id: str = ""
_cached_bot_nickname: str = ""


def _cleanup_temp_images() -> None:
    """保留最新的 _MAX_TEMP_FILES 个文件，删除多余的旧文件。"""
    try:
        candidates = []
        for path in _TEMP_IMAGES_DIR.iterdir():
            try:
                if (
                    path.is_file()
                    and path.name.startswith("ai_fwd_")
                    and path.suffix.lower() in {".png", ".jpg", ".webp"}
                ):
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return

    candidates.sort(key=lambda item: item[0])
    for _, oldest in candidates[:-_MAX_TEMP_FILES]:
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            continue


def _get_temp_image_path(suffix: str = ".jpg", prefix: str = "ai_fwd_") -> Path:
    """在插件目录 temp_images/ 下生成临时文件路径，并确保最多保留 _MAX_TEMP_FILES 个文件。"""
    _TEMP_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_temp_images()
    # pid + 毫秒 + 随机段：并发生图任务可能在同一毫秒落点，仅靠时间戳会撞名
    unique = f"{os.getpid()}_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    filename = f"{prefix}{unique}{suffix}"
    return _TEMP_IMAGES_DIR / filename


def _prepare_image_file(image_base64: str) -> str:
    """解码图片、按原始格式写入临时文件，返回 file:/// URI。

    本地文件路径让同机适配器直接读盘、零传输开销（远快于 base64 内联）；
    不做任何压缩/转码，保持 NAI 原始 PNG 画质。
    """
    decoded = decode_base64_image(image_base64)
    if decoded is None:
        raise ValueError("invalid image data")
    img_bytes, image_format = decoded
    suffix = {
        "PNG": ".png",
        "JPEG": ".jpg",
        "WEBP": ".webp",
    }[image_format]
    tmp_path = _get_temp_image_path(suffix=suffix, prefix="ai_fwd_")
    tmp_path.write_bytes(img_bytes)
    return str(tmp_path).replace("\\", "/")



# 模型配置解析

def load_models_config(raw_config: dict) -> Dict[str, Dict[str, Any]]:
    """从 [models] section 加载所有模型配置。

    过滤掉 hint / default_model 等非模型条目。
    """
    models = {}
    if not isinstance(raw_config, dict):
        return models
    for key, value in raw_config.items():
        if key in ("default_model", "hint"):
            continue
        if isinstance(value, dict):
            models[key] = value
    return models


def get_model_config(models: Dict[str, dict], model_id: str) -> Optional[Dict[str, Any]]:
    """根据 model_id 获取模型配置。

    支持两种匹配方式：
    1. 直接匹配 model_id（如 "model1"）
    2. 遍历匹配 model 字段（如 "nai-diffusion-4-5-full"）
    """
    if not models or not model_id:
        return None
    # 直接匹配
    if model_id in models:
        return dict(models[model_id])
    # 遍历匹配 model 字段
    for cfg in models.values():
        if isinstance(cfg, dict) and cfg.get("model") == model_id:
            return dict(cfg)
    return None


# 图片生成编排

async def generate_and_send(
    prompt: str,
    model_config: dict,
    stream_id: str,
    prompt_text: str = "",
    size: str = "",
    kwargs: dict = None,
    ref_image: str = "",
    ref_mode: str = "",
) -> bool:
    """后台任务：生成图片 → 发送结果 → 触发自动撤回。"""
    plugin = get_plugin_instance()
    if not plugin:
        return False

    try:
        image_size = size or model_config.get("size_preset") or model_config.get("nai_size") or model_config.get("default_size", "1024x1280")
        success, result = await generate_image(prompt, model_config, image_size, stream_id, ref_image, ref_mode)

        if not success:
            await plugin.ctx.send.text(f"生成图片失败：{result}", stream_id)
            return False

        info = plugin._extract_session_info(kwargs or {})

        # 预热 bot 身份缓存，让会话内第一次合并转发不必现查身份
        if not _cached_bot_self_id:
            await _ensure_bot_identity(
                stream_id=stream_id,
                group_id=info["chat_id"] if info.get("chat_type") == "group" else "",
                user_id=info["user_id"] if info.get("chat_type") == "private" else "",
            )
        # 保持 2.3.7 的发送时序：生成完成后直接交给适配器发送，
        # 发送函数返回后再进入撤回调度。队列只负责任务编排，不介入发送回执。
        send_ok, sent_msg_id = await send_image_result(
            result, prompt_text or prompt, stream_id,
            group_id=info["chat_id"] if info.get("chat_type") == "group" else "",
            user_id=info.get("user_id", ""),
            kwargs=kwargs or {},
        )
        if send_ok:
            send_ts = int(time.time())
            schedule_auto_recall(
                kwargs=kwargs or {}, after_ts=send_ts, message_id=sent_msg_id,
            )
            return True
        return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        plugin.ctx.logger.error(f"[生图] 后台异常: {e}", exc_info=True)
        try:
            await plugin.ctx.send.text(f"图片生成遇到问题: {str(e)[:100]}", stream_id)
        except Exception:
            pass
        return False


async def generate_image(
    prompt: str,
    model_config: dict,
    size: str,
    stream_id: str = "",
    ref_image: str = "",
    ref_mode: str = "",
) -> Tuple[bool, str]:
    """调用 Provider 生成图片。"""
    plugin = get_plugin_instance()
    if not plugin:
        return False, "插件未就绪"

    format_name = model_config.get("format", "bestnai")
    provider_cls = get_provider_class(format_name)
    if provider_cls is None:
        return False, f"未知的服务商格式: {format_name}"

    provider = provider_cls(logger=plugin.ctx.logger, log_prefix="[ai_draw]")
    try:
        return await provider.generate(
            prompt=prompt, model_config=model_config,
            size=size, ref_image=ref_image, ref_mode=ref_mode,
        )
    except Exception as e:
        plugin.ctx.logger.error(f"[生图] Provider 调用失败: {e}", exc_info=True)
        return False, f"图片生成失败: {str(e)[:100]}"


async def send_image_result(
    result: str,
    prompt_text: str,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
    kwargs: dict = None,
) -> Tuple[bool, Optional[str]]:
    """处理 API 返回的图片数据并发送。

    发送方式由配置决定：
    - send_mode = direct（默认）：普通图片直发，快
    - send_mode = forward：合并转发，隐蔽但慢
    - force_forward_when_nsfw_off = true 且当前会话 NSFW 过滤关闭：强制合并转发

    Returns:
        (success, message_id) — message_id 用于精确撤回
    """
    plugin = get_plugin_instance()
    if not plugin:
        return False, None

    image_data = process_api_response(result)
    if not image_data:
        await plugin.ctx.send.text("图片生成API返回了无法处理的数据格式", stream_id)
        return False, None

    send_fn = _resolve_send_function(kwargs or {}, group_id, user_id)

    try:
        msg_id: Optional[str] = None
        if image_data.startswith(("iVBORw", "/9j/", "UklGR")):
            msg_id = await send_fn(image_data, stream_id, group_id, user_id)
        elif image_data.startswith(("http://", "https://")):
            downloaded = await _download_image_as_base64(image_data, plugin)
            if not downloaded:
                await plugin.ctx.send.text("生成图片链接无法安全下载", stream_id)
                return False, None
            msg_id = await send_fn(downloaded, stream_id, group_id, user_id)
        elif image_data.startswith("file://"):
            path = image_data[len("file://"):]
            if Path(path).exists():
                b64 = load_image_file_as_base64(path)
                if not b64:
                    await plugin.ctx.send.text("图片文件无效或超过大小/像素限制", stream_id)
                    return False, None
                msg_id = await send_fn(b64, stream_id, group_id, user_id)
            else:
                await plugin.ctx.send.text("图片文件不存在", stream_id)
                return False, None
        else:
            msg_id = await send_fn(image_data, stream_id, group_id, user_id)

        normalized_msg_id = _normalize_message_id(msg_id)
        if normalized_msg_id:
            track_sent_message_id(
                normalized_msg_id,
                kwargs=kwargs or {},
                stream_id=stream_id,
                group_id=group_id,
                user_id=user_id,
            )
        # 与 2.3.7 一致：发送调用未抛异常即视为已提交，message_id 只供撤回使用。
        return True, normalized_msg_id or None
    except Exception as e:
        plugin.ctx.logger.error(f"[发送] 图片发送失败: {e}")
        await plugin.ctx.send.text("图片已处理完成，但发送失败了", stream_id)
        return False, None


def _resolve_send_function(kwargs: dict, group_id: str, user_id: str):
    """根据会话发送方式和 NSFW 状态决定发送函数（合并转发 / 普通直发）。"""
    plugin = get_plugin_instance()
    if not plugin:
        return send_image_direct

    info = plugin._extract_session_info(kwargs)
    platform = info.get("platform", "")
    chat_id = info.get("chat_id", "")
    get_config = plugin._get_config_callable()

    force_forward = getattr(plugin.config.plugin, "force_forward_when_nsfw_off", True)

    # NSFW 过滤关闭时强制合并转发（更隐蔽）
    if force_forward and chat_id:
        sid = str(kwargs.get("stream_id", "") or "")
        nsfw_on = plugin._session_state.is_nsfw_filter_enabled(platform, chat_id, get_config,
                                                                stream_id=sid)
        if not nsfw_on:
            plugin.ctx.logger.info("[发送] NSFW 过滤关闭，强制使用合并转发")
            return send_image_forward

    # 会话级发送方式（指令热切换 > 配置默认）
    send_mode = plugin._session_state.get_send_mode(platform, chat_id, get_config) if chat_id else "direct"
    return send_image_forward if send_mode == "forward" else send_image_direct


async def send_image_forward(
    image_base64: str,
    stream_id: str,
    group_id: str = "",
    user_id: str = "",
) -> Optional[str]:
    """通过合并转发发送图片，返回 message_id 用于撤回。

    走 SDK passthrough 发送。合并转发更隐蔽但慢（QQ 服务端构建 multimsg 耗时）。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return None

    # 合并转发 node 必须带真实 bot QQ 号；uin=0 会被服务端拒绝 (retcode=1200)
    if not _cached_bot_self_id:
        await _ensure_bot_identity(stream_id=stream_id, group_id=group_id, user_id=user_id)
    if not _cached_bot_self_id:
        plugin.ctx.logger.warning("[发送] 未获取到 bot QQ 号，合并转发改为普通直发")
        return await send_image_direct(image_base64, stream_id, group_id, user_id)

    bot_uin = _cached_bot_self_id
    bot_name = _cached_bot_nickname or bot_uin

    # 写入本地临时文件，传 file:/// 路径供同机适配器直接读盘（零传输开销，远快于 base64 内联）
    file_uri = _prepare_image_file(image_base64)
    node_content = [{"type": "image", "data": {"file": f"file:///{file_uri}"}}]
    messages = [{"type": "node", "data": {"uin": bot_uin, "name": bot_name, "content": node_content}}]

    if group_id:
        action = "send_group_forward_msg"
        params = {"group_id": int(group_id), "messages": messages}
    elif user_id:
        action = "send_private_forward_msg"
        params = {"user_id": int(user_id), "messages": messages}
    else:
        plugin.ctx.logger.error("[发送] 无 group_id 或 user_id")
        return None

    resp_data = await _napcat_action(action, params)
    if resp_data and resp_data.get("message_id"):
        msg_id = resp_data["message_id"]
        plugin.ctx.logger.info(f"[发送] 合并转发成功, message_id={msg_id}")
        return msg_id
    retcode = resp_data.get("retcode", -1) if resp_data else -1
    plugin.ctx.logger.warning(f"[发送] 合并转发返回异常: retcode={retcode}")
    return None


async def send_image_direct(
    image_base64: str,
    stream_id: str,
    group_id: str = "",
    user_id: str = "",
) -> Optional[str]:
    """普通图片消息直发，返回 message_id 用于撤回。

    通过 SDK passthrough 调用适配器发送。普通图片消息比合并转发快很多
    （无需 QQ 服务端构建 multimsg），但隐蔽性略低。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return None

    # 写入本地临时文件，传 file:/// 路径供同机适配器直接读盘（零传输开销，远快于 base64 内联）
    file_uri = _prepare_image_file(image_base64)
    message = [{"type": "image", "data": {"file": f"file:///{file_uri}"}}]

    if group_id:
        action = "send_group_msg"
        params = {"group_id": int(group_id), "message": message}
    elif user_id:
        action = "send_private_msg"
        params = {"user_id": int(user_id), "message": message}
    else:
        plugin.ctx.logger.error("[发送] 无 group_id 或 user_id")
        return None

    resp_data = await _napcat_action(action, params)
    if resp_data and resp_data.get("message_id"):
        msg_id = resp_data["message_id"]
        plugin.ctx.logger.info(f"[发送] 普通直发成功, message_id={msg_id}")
        return msg_id
    retcode = resp_data.get("retcode", -1) if resp_data else -1
    plugin.ctx.logger.warning(f"[发送] 普通直发返回异常: retcode={retcode}")
    return None


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _normalize_action_response(payload: Optional[dict]) -> Optional[dict]:
    """把适配器/HTTP 的原始 OneBot 响应归一化为 {status,retcode,message_id,data}。"""
    if not isinstance(payload, dict):
        return None

    def _normalized_retcode(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value.strip())
        raise ValueError("invalid retcode")

    current = payload
    # MaiBot SDK 会把适配器结果包装为 success/result；有些版本还会嵌套多层。
    # 每一层都先检查失败信号，避免外层 success=True 掩盖内层 OneBot 失败。
    for _ in range(6):
        success = current.get("success")
        if success is False or (
            isinstance(success, str)
            and success.strip().lower() in {"false", "failed", "error", "0"}
        ) or (
            isinstance(success, (int, float))
            and not isinstance(success, bool)
            and success == 0
        ):
            return None

        status = str(current.get("status") or "").strip().lower()
        if status and status not in {"ok", "success"}:
            return None
        try:
            layer_retcode = _normalized_retcode(current.get("retcode"))
        except ValueError:
            return None
        if layer_retcode is not None and layer_retcode not in (0, 1):
            return None

        result = current.get("result")
        looks_wrapped = isinstance(result, dict) and (
            "success" in current
            or not any(
                key in current
                for key in ("status", "retcode", "data", "message_id", "msg_id")
            )
        )
        if looks_wrapped:
            current = result
            continue

        nested_data = current.get("data")
        looks_data_wrapped = (
            isinstance(nested_data, dict)
            and not (current.get("message_id") or current.get("msg_id"))
            and (
                "retcode" in nested_data
                or "success" in nested_data
                or "result" in nested_data
                or ("status" in nested_data and "data" in nested_data)
            )
        )
        if looks_data_wrapped:
            current = nested_data
            continue
        break
    else:
        return None

    status = str(current.get("status") or "ok").strip().lower() or "ok"
    try:
        retcode = _normalized_retcode(current.get("retcode"))
    except ValueError:
        return None
    if status not in {"ok", "success"} or (
        retcode is not None and retcode not in (0, 1)
    ):
        return None

    data = current.get("data")
    msg_id = str(current.get("message_id") or current.get("msg_id") or "")
    if not msg_id and isinstance(data, dict):
        msg_id = str(data.get("message_id") or data.get("msg_id") or "")
    return {
        "status": status,
        "retcode": retcode if retcode is not None else 0,
        "message_id": msg_id,
        "data": data,
    }


async def _napcat_http_call(action: str, params: dict, timeout: int = 60) -> Optional[dict]:
    """直连本机 NapCat/SnowLuma HTTP API（仅 use_http_direct=true 时启用）。

    地址强制限定本机回环，防 SSRF；token 仅放入 Authorization 头、不写入日志。
    慢环境下 HTTP 直连超时预算更长（默认 60s），避免回执超时丢失 message_id。
    """
    import aiohttp

    plugin = get_plugin_instance()
    if not plugin:
        return None
    base_url = str(getattr(plugin.config.plugin, "napcat_http_url", "") or "").strip()
    token = str(getattr(plugin.config.plugin, "napcat_http_token", "") or "").strip()
    try:
        parsed_base_url = urlparse(base_url)
        host = (parsed_base_url.hostname or "").lower()
    except ValueError:
        plugin.ctx.logger.error("[HTTP直连] NapCat 地址格式无效")
        return None
    if parsed_base_url.scheme.lower() not in ("http", "https") or host not in _LOCAL_HOSTS:
        plugin.ctx.logger.error(f"[HTTP直连] 拒绝非本机地址（仅允许本机回环）: host={host or '?'}")
        return None

    url = f"{base_url.rstrip('/')}/{action}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    plugin.ctx.logger.warning(f"[HTTP直连] {action} HTTP {resp.status}")
                    return None
                return await resp.json()
    except Exception as e:
        plugin.ctx.logger.warning(f"[HTTP直连] {action} 失败: {e}")
        return None


# 两适配器命名空间差异：send_group_msg 在 napcat=group.* / SnowLuma=message.*；
# send_private_forward_msg 仅 SnowLuma 有，napcat 回退 send_forward_msg。其余沿用 message.*。
# 按优先级逐个尝试候选，命中"未找到 API"才换下一个。
_ACTION_API_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "get_login_info": ("adapter.napcat.system.get_login_info",),
    "send_group_msg": (
        "adapter.napcat.message.send_group_msg",  # SnowLuma
        "adapter.napcat.group.send_group_msg",    # napcat-adapter
    ),
    "send_private_forward_msg": (
        "adapter.napcat.message.send_private_forward_msg",  # SnowLuma
        "adapter.napcat.message.send_forward_msg",          # napcat-adapter 回退
    ),
}

# 缓存命中的完整 API 名，避免重复试错（适配器热切换时自愈）
_resolved_action_api: Dict[str, str] = {}

# 两适配器参数签名差异（SnowLuma 用 **kwargs 全兼容，napcat 各方法互斥）：
#   params=ctx.api.call(api, params=dict) / spread=call(api, **dict) / noargs=call(api)
# 未列出默认 params（发送类全走这条）。
_ACTION_CALL_STYLE: Dict[str, str] = {
    "get_login_info": "noargs",
    "delete_msg": "spread",
}


async def _call_action_api(plugin, api_name: str, action: str, params: dict):
    """按动作的参数约定调用 passthrough API（兼容两适配器互斥的方法签名）。"""
    style = _ACTION_CALL_STYLE.get(action, "params")
    if style == "noargs":
        return await plugin.ctx.api.call(api_name)
    if style == "spread":
        return await plugin.ctx.api.call(api_name, **params)
    return await plugin.ctx.api.call(api_name, params=params)


def _candidate_api_names(action: str) -> List[str]:
    """返回动作的候选完整 API 名（按优先级；上次命中的排最前）。"""
    candidates = list(_ACTION_API_CANDIDATES.get(action, (f"adapter.napcat.message.{action}",)))
    cached = _resolved_action_api.get(action)
    if cached:
        if cached in candidates:
            candidates.remove(cached)
        candidates.insert(0, cached)
    return candidates


def _is_api_missing_error(message: str) -> bool:
    """passthrough 错误是否为"该 API 名不存在"（可换命名空间重试）。"""
    return "未找到 API" in message


async def _napcat_action(action: str, params: dict) -> Optional[dict]:
    """调用 NapCat 动作，返回归一化响应 {status,retcode,message_id,data}。

    默认走 SDK passthrough；use_http_direct=true 时走本机 HTTP 直连。
    两适配器个别动作命名空间/签名不同，按候选逐个尝试并缓存命中。失败返回 None。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return None

    # 分流：HTTP 直连（可选） vs SDK passthrough（默认）
    if getattr(plugin.config.plugin, "use_http_direct", False):
        return _normalize_action_response(await _napcat_http_call(action, params))

    candidates = _candidate_api_names(action)
    last_error = ""
    for index, api_name in enumerate(candidates):
        try:
            result = await _call_action_api(plugin, api_name, action, params)
        except Exception as e:
            last_error = str(e)
            # 完整 API 名不存在且还有候选 → 换命名空间重试（兼容另一适配器）
            if _is_api_missing_error(last_error) and index < len(candidates) - 1:
                continue
            plugin.ctx.logger.error(f"[passthrough] 调用 {api_name} 失败: {last_error}")
            return None
        if not isinstance(result, dict):
            plugin.ctx.logger.warning(f"[passthrough] {api_name} 返回非字典: {result!r}")
            return None
        # SDK 失败包装：handler 抛异常时返回 {"success": False, "error": ...}
        if result.get("success") is False:
            error_text = str(result.get("error") or "")
            if _is_api_missing_error(error_text) and index < len(candidates) - 1:
                last_error = error_text
                continue
            plugin.ctx.logger.warning(f"[passthrough] {api_name} 调用失败: {error_text}")
            return None
        normalized = _normalize_action_response(result)
        if normalized is None:
            plugin.ctx.logger.warning(f"[passthrough] {api_name} 返回失败回执")
            return None
        # 仅缓存已经通过响应校验的 API，避免把异常包装误记为可用候选。
        if _resolved_action_api.get(action) != api_name:
            _resolved_action_api[action] = api_name
            if index > 0:
                plugin.ctx.logger.info(f"[passthrough] {action} 解析到 {api_name}（已缓存）")
        return normalized

    plugin.ctx.logger.error(f"[passthrough] 调用 {action} 失败，所有候选 API 均不可用: {last_error}")
    return None


def _extract_message_id_from_response(resp) -> Optional[str]:
    """从 NapCat API 响应中提取 message_id。"""
    if not resp:
        return None
    if isinstance(resp, dict):
        mid = resp.get("message_id") or resp.get("msg_id")
        if mid:
            return str(mid)
        data = resp.get("data") or resp.get("result")
        if isinstance(data, dict):
            mid = data.get("message_id") or data.get("msg_id")
            if mid:
                return str(mid)
    return None


# 消息获取与识别

def extract_text_from_napcat_message(msg: dict) -> str:
    segments = msg.get("message", msg.get("raw_message", []))
    texts = []
    for seg in (segments or []):
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "text":
            continue
        data = seg.get("data", "")
        if isinstance(data, dict):
            text = data.get("text", "")
        elif isinstance(data, str):
            text = data
        else:
            continue
        if text:
            texts.append(text)
    return " ".join(texts)


def is_ai_draw_bot_message(msg: dict, display_text: str = "") -> bool:
    """判断消息是否为本插件 bot 发送的图片消息。

    匹配规则（满足任一即可）：
    1. bot 自己发送的合并转发/JSON 消息
    2. bot 自己发送的图片消息（send.image 直发）
    3. bot 自己发送的文件消息（PDF回退）
    4. 文本包含 [AI绘图] 标记
    """
    segments = msg.get("message", msg.get("raw_message", []))
    sender = msg.get("sender", {}) or {}
    sender_id = str(sender.get("user_id", ""))
    self_id = str(msg.get("self_id", ""))
    # SnowLuma 等适配器会从历史消息剥掉 self_id，改用已缓存的 bot QQ 号兜底判定，
    # 否则慢环境下丢失精确 message_id 后、回退匹配也认不出 bot 自己发的图。
    is_self = (
        msg.get("self") is True
        or (bool(self_id) and bool(sender_id) and self_id == sender_id)
        or (bool(_cached_bot_self_id) and bool(sender_id) and sender_id == _cached_bot_self_id)
    )

    # 检查消息段类型
    has_forward = False
    has_image = False
    has_file = False
    for seg in (segments or []):
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        if seg_type in ("forward", "json"):
            has_forward = True
        if seg_type == "image":
            has_image = True
        if seg_type == "file":
            file_name = str((seg.get("data") or {}).get("file", "") or (seg.get("data") or {}).get("name", ""))
            if "ai_draw" in file_name:
                has_file = True

    # 合并转发：bot 自己的就是本插件消息
    if has_forward:
        return is_self

    # 直接发图：bot 自己的图片消息同样需要撤回
    if has_image and is_self:
        return True

    # bot 自己发的文件消息（手动撤回时兜底）
    if has_file and is_self:
        return True

    # 文本内容含 [AI绘图] 标记
    content = extract_text_from_napcat_message(msg)
    if content:
        if "[AI绘图]" in content:
            return True
        if display_text and content == display_text:
            return True

    return False


def parse_napcat_message_list(result) -> list:
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []

    inner = result
    if "success" in result and "result" in result:
        r = result["result"]
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            inner = r
        else:
            return []

    data = inner.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        msgs = data.get("messages", [])
        if isinstance(msgs, list):
            return msgs

    msgs = inner.get("messages", [])
    if isinstance(msgs, list):
        return msgs

    for val in inner.values():
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            msgs = val.get("messages", [])
            if isinstance(msgs, list):
                return msgs
    return []


def _capture_bot_identity(messages: list) -> None:
    """从消息列表中提取并缓存 bot 的真实 QQ 号和昵称，用于合并转发。"""
    global _cached_bot_self_id, _cached_bot_nickname

    for msg in (messages or []):
        if not isinstance(msg, dict):
            continue
        # 获取 bot 的 QQ 号
        sid = str(msg.get("self_id", "") or "")
        if sid and not _cached_bot_self_id:
            _cached_bot_self_id = sid

        # 获取 bot 的昵称：找到 bot 自己发的消息，取 sender.nickname
        if sid and not _cached_bot_nickname:
            sender = msg.get("sender", {}) or {}
            sender_id = str(sender.get("user_id", "") or "")
            if sender_id == sid:
                nick = str(sender.get("nickname", "") or "")
                if nick:
                    _cached_bot_nickname = nick

        # 都拿到了就退出
        if _cached_bot_self_id and _cached_bot_nickname:
            return


async def _ensure_bot_identity(
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> bool:
    """确保已缓存 bot 的真实 QQ 号（合并转发 node 必需）。

    两条路覆盖不同适配器：
    - 主：get_login_info 直接拿 bot 身份，不依赖历史消息的 self_id 字段
      （SnowLuma 会在历史消息返回时剥掉 self_id，只能走这条）。
    - 回退：扫最近历史消息提取 self_id（NapCat 保留该字段，老路径仍可用），
      在 get_login_info 不可用时兜底。

    uin 退化为 "0" 会被服务端拒绝（retcode=1200）。返回 True 表示已有可用 QQ 号。
    """
    global _cached_bot_self_id, _cached_bot_nickname
    if _cached_bot_self_id:
        return True

    plugin = get_plugin_instance()

    # 主路径：get_login_info
    try:
        resp = await _napcat_action("get_login_info", {})
        if resp and resp.get("data"):
            data = resp.get("data") or {}
            uid = str(data.get("user_id", "") or "")
            nick = str(data.get("nickname", "") or "")
            if uid and uid != "0":
                _cached_bot_self_id = uid
                if nick and not _cached_bot_nickname:
                    _cached_bot_nickname = nick
                if plugin:
                    plugin.ctx.logger.info(
                        f"[身份缓存] get_login_info bot_uin={_cached_bot_self_id} "
                        f"nickname={_cached_bot_nickname or '(未获取到)'}"
                    )
                return True
    except Exception as e:
        if plugin:
            plugin.ctx.logger.warning(f"[身份缓存] get_login_info 失败，回退历史消息: {e}")

    # 回退路径：扫历史消息提取 self_id（NapCat 等保留该字段的适配器）
    if not _cached_bot_self_id and (group_id or user_id or stream_id):
        try:
            await fetch_recent_messages(
                stream_id=stream_id, limit=3,
                group_id=group_id, user_id=user_id,
            )
            if _cached_bot_self_id and plugin:
                plugin.ctx.logger.info(
                    f"[身份缓存] 历史消息 bot_uin={_cached_bot_self_id} "
                    f"nickname={_cached_bot_nickname or '(未获取到)'}"
                )
        except Exception as e:
            if plugin:
                plugin.ctx.logger.warning(f"[身份缓存] 历史消息回退失败: {e}")

    return bool(_cached_bot_self_id)


async def fetch_recent_messages(
    stream_id: str = "",
    limit: int = 10,
    group_id: str = "",
    user_id: str = "",
) -> list:
    """获取最近消息（优先适配器 passthrough，回退 MaiBot DB）。"""
    plugin = get_plugin_instance()
    if not plugin:
        return []

    # 群聊：passthrough 取历史
    if group_id:
        try:
            result = await plugin.ctx.api.call(
                "adapter.napcat.message.get_group_msg_history",
                params={"group_id": int(group_id), "count": limit},
            )
            msgs = parse_napcat_message_list(result)
            if msgs:
                _capture_bot_identity(msgs)
                plugin.ctx.logger.info(f"[撤回] NapCat 获取群消息: {len(msgs)} 条")
                return msgs
        except Exception as e:
            plugin.ctx.logger.warning(f"[撤回] get_group_msg_history 失败: {e}")

    # 私聊：passthrough 取历史
    if user_id:
        try:
            result = await plugin.ctx.api.call(
                "adapter.napcat.message.get_friend_msg_history",
                params={"user_id": int(user_id), "count": limit},
            )
            msgs = parse_napcat_message_list(result)
            if msgs:
                _capture_bot_identity(msgs)
                plugin.ctx.logger.info(f"[撤回] NapCat 获取私聊消息: {len(msgs)} 条")
                return msgs
        except Exception as e:
            plugin.ctx.logger.warning(f"[撤回] get_friend_msg_history 失败: {e}")

    # 回退：MaiBot 本地 DB
    if stream_id:
        try:
            messages = await plugin.ctx.message.get_recent(chat_id=stream_id, limit=limit)
            if messages and isinstance(messages, list) and len(messages) > 0:
                _capture_bot_identity(messages)
                plugin.ctx.logger.info(f"[撤回] get_recent 获取: {len(messages)} 条")
                return messages
        except Exception as e:
            plugin.ctx.logger.warning(f"[撤回] get_recent 失败: {e}")

    return []


async def fetch_ref_image(kwargs: dict, stream_id: str = "") -> Optional[str]:
    """自动获取参考图：当前消息附件 → 引用消息 → 可选的最近消息回退。"""
    plugin = get_plugin_instance()
    if not plugin:
        return None

    message = kwargs.get("message", {})
    if not isinstance(message, dict):
        message = {}

    raw_msg = message.get("raw_message", message.get("message", []))
    if not isinstance(raw_msg, list):
        raw_msg = []

    # DEBUG: 打印消息结构帮助排查
    seg_types = [s.get("type", "?") for s in raw_msg if isinstance(s, dict)]
    plugin.ctx.logger.debug(f"[参考图] message keys={list(message.keys())}, seg_types={seg_types}")

    # 1. 当前消息中直接附带的图片
    current_image = await _resolve_image_from_sdk_message(message, plugin)
    if current_image:
        plugin.ctx.logger.info("[参考图] 从当前消息获取")
        return current_image

    # 2. 引用消息中的图片（多种来源尝试）
    # 2a. message["reply"] 字段（部分 SDK 版本提供）
    reply = message.get("reply")
    if isinstance(reply, dict):
        img = await _resolve_image_from_sdk_message(reply, plugin)
        if img:
            plugin.ctx.logger.info("[参考图] 从 message.reply 获取")
            return img

    # 2b. raw_message 中的 reply segment → 追溯目标消息 ID → 获取图片
    for seg in raw_msg:
        if not isinstance(seg, dict) or seg.get("type") != "reply":
            continue
        reply_data = seg.get("data", {})
        if not isinstance(reply_data, dict):
            continue
        target_id = str(
            reply_data.get("target_message_id")
            or reply_data.get("id")
            or reply_data.get("message_id")
            or ""
        ).strip()
        if not target_id:
            continue

        plugin.ctx.logger.info(f"[参考图] 从 reply segment 追溯目标消息: {target_id}")

        # 2b-i. 优先 SDK message.get_by_id 查本地库（命中则瞬时返回、零服务器请求）
        try:
            sdk_result = await plugin.ctx.message.get_by_id(message_id=target_id, include_binary_data=True)
            if isinstance(sdk_result, dict):
                target_msg_data = _extract_napcat_msg(sdk_result)
                if isinstance(target_msg_data, dict):
                    img = await _resolve_image_from_sdk_message(
                        target_msg_data, plugin,
                    )
                    if img:
                        plugin.ctx.logger.info("[参考图] 从 SDK get_by_id 获取引用图片")
                        return img
        except Exception as e:
            plugin.ctx.logger.debug(f"[参考图] SDK get_by_id 失败: {e}")

        # 2b-ii. 本地库未命中，再用 get_msg 上服务器实时取回（URL 含有效 rkey，不依赖本地库）
        try:
            napcat_result = await plugin.ctx.api.call(
                "adapter.napcat.message.get_msg",
                message_id=int(target_id),
            )
            target_msg = _extract_napcat_msg(napcat_result)
            if target_msg:
                img = await _resolve_image_from_napcat_msg(target_msg, plugin)
                if img:
                    plugin.ctx.logger.info(f"[参考图] 从 NapCat get_msg 获取引用图片, type={img[:30]}...")
                    return img
        except Exception as e:
            plugin.ctx.logger.debug(f"[参考图] NapCat get_msg 失败: {e}")

    # 3. 最近消息回退默认关闭，避免未明确引用时拿到群里其他人的图片。
    allow_recent_fallback = bool(
        getattr(plugin.config.plugin, "allow_recent_image_fallback", False)
    )
    if not allow_recent_fallback:
        plugin.ctx.logger.debug("[参考图] 未启用最近消息图片回退")
        return None

    try:
        info = get_session_info_from_kwargs(kwargs)
        messages = await fetch_recent_messages(
            stream_id=stream_id, limit=30,
            group_id=info["chat_id"] if info["chat_type"] == "group" else "",
            user_id=info["user_id"] if info["chat_type"] == "private" else "",
        )
        for msg in reversed(messages or []):
            if not isinstance(msg, dict):
                continue
            img = await _resolve_image_from_sdk_message(msg, plugin)
            if img:
                plugin.ctx.logger.info("[参考图] 从历史消息获取图片")
                return img
    except Exception as e:
        plugin.ctx.logger.warning(f"[参考图] NapCat 历史获取失败: {e}")

    return None


def _extract_napcat_msg(result: Any) -> Optional[dict]:
    """从 NapCat get_msg 响应中提取消息体。"""
    if not isinstance(result, dict):
        return None
    inner = result.get("result", result)
    if not isinstance(inner, dict):
        return None
    data = inner.get("data", inner)
    if isinstance(data, dict):
        return data
    return inner


async def _resolve_image_from_napcat_msg(msg: dict, plugin) -> Optional[str]:
    """从 NapCat 消息中提取并安全规范化图片。"""
    return await _resolve_image_from_sdk_message(msg, plugin)


async def _resolve_image_candidate(
    value: Any,
    plugin,
    *,
    allow_napcat_file: bool = False,
) -> Optional[str]:
    """把任意图片候选统一解析为经过校验的 base64。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if validate_image_bytes(raw):
            return base64.b64encode(raw).decode("ascii")
        return None
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate or candidate.startswith("[图"):
        return None

    normalized = normalize_base64_image(candidate)
    if normalized:
        return normalized
    if candidate.lower().startswith(("http://", "https://")):
        return await _download_image_as_base64(candidate, plugin)
    if allow_napcat_file:
        return await _resolve_napcat_file_image(candidate, plugin)
    return None


async def _resolve_image_payload(
    data: Any,
    plugin,
    *,
    allow_napcat_file: bool = True,
) -> Optional[str]:
    """解析消息段 data/content，保留 file 失败后的 URL 回退。"""
    if not isinstance(data, dict):
        return await _resolve_image_candidate(
            data, plugin, allow_napcat_file=allow_napcat_file,
        )

    for key in ("binary_data_base64", "base64"):
        resolved = await _resolve_image_candidate(data.get(key), plugin)
        if resolved:
            return resolved

    file_data = data.get("file")
    if file_data:
        resolved = await _resolve_image_candidate(
            file_data, plugin, allow_napcat_file=allow_napcat_file,
        )
        if resolved:
            return resolved

    for key in ("url", "content"):
        resolved = await _resolve_image_candidate(data.get(key), plugin)
        if resolved:
            return resolved
    return None


async def _resolve_image_segment(
    segment: dict,
    plugin,
    *,
    allow_napcat_file: bool = True,
) -> Optional[str]:
    if not isinstance(segment, dict):
        return None
    binary = segment.get("binary_data_base64")
    if binary:
        resolved = await _resolve_image_candidate(binary, plugin)
        if resolved:
            return resolved
    resolved = await _resolve_image_payload(
        segment.get("data", ""),
        plugin,
        allow_napcat_file=allow_napcat_file,
    )
    if resolved:
        return resolved
    return await _resolve_image_payload(
        segment.get("content", ""),
        plugin,
        allow_napcat_file=allow_napcat_file,
    )


async def _resolve_image_from_sdk_message(msg: dict, plugin) -> Optional[str]:
    """从 SDK/NapCat 的各种消息结构中提取并规范化图片。"""
    if not isinstance(msg, dict):
        return None

    if msg.get("type") in ("image", "imageurl", "emoji"):
        resolved = await _resolve_image_segment(msg, plugin)
        if resolved:
            return resolved

    segment = msg.get("message_segment")
    if isinstance(segment, dict):
        if segment.get("type") in ("image", "imageurl", "emoji"):
            resolved = await _resolve_image_segment(segment, plugin)
            if resolved:
                return resolved
        if segment.get("type") == "seglist":
            for child in segment.get("data", []) or []:
                if isinstance(child, dict) and child.get("type") in (
                    "image", "imageurl", "emoji",
                ):
                    resolved = await _resolve_image_segment(child, plugin)
                    if resolved:
                        return resolved

    raw = msg.get("raw_message", msg.get("message", []))
    if isinstance(raw, dict):
        return await _resolve_image_from_sdk_message(raw, plugin)
    if isinstance(raw, list):
        for child in raw:
            if isinstance(child, dict) and child.get("type") in (
                "image", "imageurl", "emoji",
            ):
                resolved = await _resolve_image_segment(child, plugin)
                if resolved:
                    return resolved
    return None


def _safe_url_for_log(url: str) -> str:
    """移除查询串、凭据和片段，避免把签名参数写入日志。"""
    try:
        parsed = urlparse(str(url or ""))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse((parsed.scheme, f"{host}{port}", "", "", "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _is_public_ip(value: str) -> bool:
    from .http_client import is_public_ip

    return is_public_ip(value)


def _canonical_ip(value: Any) -> Optional[str]:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.compressed
    except ValueError:
        return None


class _PinnedResolver:
    """只把指定主机解析到预检得到的 IP，避免连接阶段再次查询 DNS。"""

    def __init__(self, hostname: str, addresses: Tuple[str, ...]):
        self._hostname = hostname.rstrip(".").lower()
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> List[dict]:
        if host.rstrip(".").lower() != self._hostname:
            raise OSError("resolver hostname mismatch")
        resolved = []
        for value in self._addresses:
            address = ipaddress.ip_address(value)
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            resolved.append({
                "hostname": host,
                "host": address.compressed,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            })
        if not resolved:
            raise OSError("no pinned address for requested family")
        return resolved

    async def close(self) -> None:
        return None


async def _resolve_public_image_url(
    url: str,
    plugin,
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """校验 URL 并返回固定的公网 DNS 结果。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return None

    hostname = parsed.hostname
    literal = _canonical_ip(hostname)
    if literal:
        addresses = {literal}
    else:
        try:
            loop = asyncio.get_running_loop()
            resolved = await asyncio.wait_for(
                loop.getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                ),
                timeout=5,
            )
            addresses = {
                canonical
                for item in resolved
                if item and len(item) > 4 and item[4]
                for canonical in [_canonical_ip(item[4][0])]
                if canonical
            }
        except (asyncio.TimeoutError, OSError, socket.gaierror) as exc:
            plugin.ctx.logger.warning(
                f"[参考图] URL DNS 解析失败: {_safe_url_for_log(url)} "
                f"({type(exc).__name__})"
            )
            return None

    if not addresses or any(not _is_public_ip(address) for address in addresses):
        plugin.ctx.logger.warning(
            f"[参考图] 拒绝非公网图片地址: {_safe_url_for_log(url)}"
        )
        return None
    return hostname, tuple(sorted(addresses))


async def _validate_public_image_url(url: str, plugin) -> bool:
    """验证 URL 语法和 DNS 解析结果，拒绝本机、内网及保留地址。"""
    return await _resolve_public_image_url(url, plugin) is not None


def _response_peer_is_public(response) -> bool:
    """复验实际连接的对端 IP；无法取得时严格拒绝。"""
    from .http_client import response_peer_is_public

    return response_peer_is_public(response)


async def _download_image_as_base64(url: str, plugin) -> Optional[str]:
    """安全下载公网图片并转为 base64，逐跳复验重定向和响应内容。"""
    if not url or not isinstance(url, str):
        return None

    try:
        import aiohttp
        from .http_client import build_ssl_context, response_peer_matches

        request_timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
        current_url = url.strip()

        for redirect_count in range(_MAX_REFERENCE_REDIRECTS + 1):
            resolved_target = await _resolve_public_image_url(current_url, plugin)
            if resolved_target is None:
                return None
            hostname, allowed_addresses = resolved_target
            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(hostname, allowed_addresses),
                use_dns_cache=False,
                force_close=True,
                ssl=build_ssl_context(),
            )
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=request_timeout,
                trust_env=False,
            ) as session:
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"Accept": "image/png,image/jpeg,image/webp"},
                ) as resp:
                    if not response_peer_matches(resp, allowed_addresses):
                        plugin.ctx.logger.warning(
                            f"[参考图] 实际连接地址与 DNS 预检不一致: "
                            f"{_safe_url_for_log(current_url)}"
                        )
                        return None

                    if resp.status in (301, 302, 303, 307, 308):
                        if redirect_count >= _MAX_REFERENCE_REDIRECTS:
                            plugin.ctx.logger.warning("[参考图] 图片重定向次数过多")
                            return None
                        location = str(resp.headers.get("Location") or "").strip()
                        if not location:
                            return None
                        current_url = urljoin(current_url, location)
                        continue

                    if resp.status != 200:
                        plugin.ctx.logger.warning(
                            f"[参考图] 下载图片失败 HTTP {resp.status} "
                            f"url={_safe_url_for_log(current_url)}"
                        )
                        return None

                    content_type = str(resp.headers.get("Content-Type") or "")
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if media_type not in _ALLOWED_REMOTE_IMAGE_CONTENT_TYPES:
                        plugin.ctx.logger.warning(
                            f"[参考图] 拒绝非图片 Content-Type: "
                            f"{media_type or '(missing)'}"
                        )
                        return None

                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_IMAGE_BYTES:
                                plugin.ctx.logger.warning("[参考图] 图片响应超过大小限制")
                                return None
                        except (TypeError, ValueError):
                            plugin.ctx.logger.warning("[参考图] 图片 Content-Length 无效")
                            return None

                    chunks = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        chunks.extend(chunk)
                        if len(chunks) > MAX_IMAGE_BYTES:
                            plugin.ctx.logger.warning("[参考图] 图片下载过程中超过大小限制")
                            return None

                    image_bytes = bytes(chunks)
                    if not validate_image_bytes(image_bytes):
                        plugin.ctx.logger.warning("[参考图] 下载内容不是有效图片或像素数超限")
                        return None
                    return base64.b64encode(image_bytes).decode("ascii")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        plugin.ctx.logger.warning(f"[参考图] 下载图片异常: {type(e).__name__}")
        return None

    return None


def _trusted_local_image_path(value: str) -> Optional[Path]:
    """只接受插件临时目录或系统临时目录中的普通本地文件。"""
    raw_path = str(value or "").strip()
    if not raw_path or "\x00" in raw_path:
        return None

    if raw_path.lower().startswith("file://"):
        try:
            parsed = urlparse(raw_path)
        except ValueError:
            return None
        if parsed.scheme.lower() != "file" or parsed.netloc.lower() not in ("", "localhost"):
            return None
        raw_path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]

    windows_form = raw_path.replace("/", "\\")
    if windows_form.startswith("\\\\"):
        return None

    path = Path(raw_path)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None

    trusted_roots = (_TEMP_IMAGES_DIR, Path(tempfile.gettempdir()))
    for root in trusted_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except (OSError, RuntimeError, ValueError):
            continue
    return None


async def _resolve_napcat_file_image(file_data: str, plugin) -> Optional[str]:
    """通过 NapCat get_image API 解析文件引用为可用图片。"""
    try:
        img_result = await plugin.ctx.api.call(
            "adapter.napcat.file.get_image",
            params={"file": file_data},
        )
        if not isinstance(img_result, dict):
            return None
        if _normalize_action_response(img_result) is None:
            return None

        napcat_resp = img_result
        for _ in range(6):
            result = napcat_resp.get("result") if isinstance(napcat_resp, dict) else None
            if not isinstance(result, dict):
                break
            napcat_resp = result
        inner = napcat_resp.get("data", napcat_resp) if isinstance(napcat_resp, dict) else napcat_resp
        if isinstance(inner, dict):
            candidates = [
                inner.get("binary_data_base64"),
                inner.get("base64"),
                inner.get("file"),
                inner.get("url"),
            ]
        elif isinstance(inner, str):
            candidates = [inner]
        else:
            return None

        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            candidate = candidate.strip()
            normalized = normalize_base64_image(candidate)
            if normalized:
                plugin.ctx.logger.info("[参考图] get_image base64")
                return normalized
            if candidate.lower().startswith(("http://", "https://")):
                downloaded = await _download_image_as_base64(candidate, plugin)
                if downloaded:
                    plugin.ctx.logger.info(
                        f"[参考图] get_image URL 下载成功: {_safe_url_for_log(candidate)}"
                    )
                    return downloaded
                continue

            image_path = _trusted_local_image_path(candidate)
            if image_path is None:
                plugin.ctx.logger.warning("[参考图] 拒绝不受信任的 get_image 本地路径")
                continue
            image_base64 = load_image_file_as_base64(image_path)
            if image_base64:
                plugin.ctx.logger.info("[参考图] 已读取受信任的 get_image 本地文件")
                return image_base64
            plugin.ctx.logger.warning("[参考图] 本地图片无效或超过大小/像素限制")
    except Exception as e:
        plugin.ctx.logger.warning(f"[参考图] get_image 失败: {type(e).__name__}")
    return None


def extract_image_from_message(msg: dict) -> Optional[str]:
    if not isinstance(msg, dict):
        return None
    seg = msg.get("message_segment")
    if isinstance(seg, dict):
        if seg.get("type") in ("image", "imageurl"):
            content = seg.get("content") or seg.get("data", {}).get("content", "")
            if content:
                return str(content)
        if seg.get("type") == "seglist":
            for child in seg.get("data", []) or []:
                if isinstance(child, dict) and child.get("type") in ("image", "imageurl"):
                    content = child.get("content") or child.get("data", {}).get("content", "")
                    if content:
                        return str(content)
    return None


# 会话信息提取

def get_session_info_from_kwargs(kwargs: dict) -> dict:
    """从 kwargs 提取 session 信息。"""
    message = kwargs.get("message", {})
    if isinstance(message, dict) and message:
        platform = str(message.get("platform", "") or "")
        info = message.get("message_info", {}) or {}
        group_info = info.get("group_info") or {}
        user_info = info.get("user_info") or {}
        user_id = str(user_info.get("user_id", "") or "")
        group_id = str(group_info.get("group_id") or "")
        chat_id = group_id or user_id
        chat_type = "group" if group_id else "private"
        return {"platform": platform, "chat_id": chat_id, "user_id": user_id, "chat_type": chat_type}

    user_id = str(kwargs.get("user_id", "") or "")
    group_id = str(kwargs.get("group_id", "") or "")
    chat_id = group_id or user_id or str(kwargs.get("stream_id", "") or "")
    chat_type = "group" if group_id else "private"
    return {"platform": "", "chat_id": chat_id, "user_id": user_id, "chat_type": chat_type}


# 本插件发送消息 ID 追踪（自动/手动撤回的唯一可信来源）

def _normalize_message_id(message_id: Any) -> str:
    normalized = str(message_id or "").strip()
    return "" if normalized in ("", "0", "None") else normalized


def _tracking_context(
    kwargs: Optional[dict] = None,
    *,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> Dict[str, str]:
    raw_kwargs = kwargs or {}
    info = get_session_info_from_kwargs(raw_kwargs)
    platform = str(info.get("platform", "") or "")
    resolved_stream_id = str(stream_id or raw_kwargs.get("stream_id", "") or "")
    resolved_group_id = str(group_id or "")
    resolved_user_id = str(user_id or "")
    if not resolved_group_id and info.get("chat_type") == "group":
        resolved_group_id = str(info.get("chat_id", "") or "")
    if not resolved_user_id:
        resolved_user_id = str(info.get("user_id", "") or "")

    if resolved_group_id:
        chat_type = "group"
        chat_id = resolved_group_id
    elif resolved_user_id:
        chat_type = "private"
        chat_id = resolved_user_id
    else:
        chat_type = str(info.get("chat_type", "") or "")
        chat_id = str(info.get("chat_id", "") or resolved_stream_id)

    if chat_id:
        session_key = f"{platform}:{chat_type}:{chat_id}"
    elif resolved_stream_id:
        session_key = f"stream:{resolved_stream_id}"
    else:
        session_key = ""
    return {
        "platform": platform,
        "chat_type": chat_type,
        "chat_id": chat_id,
        "group_id": resolved_group_id,
        "user_id": resolved_user_id,
        "stream_id": resolved_stream_id,
        "session_key": session_key,
    }


def _cleanup_tracked_messages(now: Optional[float] = None) -> None:
    current = time.time() if now is None else now
    expired = [
        message_id
        for message_id, record in _tracked_messages.items()
        if current - float(record.get("tracked_at", 0) or 0) > _TRACKED_MESSAGE_TTL_SECONDS
    ]
    for message_id in expired:
        _tracked_messages.pop(message_id, None)
    while len(_tracked_messages) > _MAX_TRACKED_MESSAGES:
        _tracked_messages.popitem(last=False)


def track_sent_message_id(
    message_id: Any,
    kwargs: Optional[dict] = None,
    *,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> bool:
    """记录由本插件发送且已取得成功回执的 message_id。"""
    normalized = _normalize_message_id(message_id)
    context = _tracking_context(
        kwargs, stream_id=stream_id, group_id=group_id, user_id=user_id,
    )
    if not normalized or not context["session_key"]:
        return False
    with _tracked_messages_lock:
        _cleanup_tracked_messages()
        _tracked_messages.pop(normalized, None)
        _tracked_messages[normalized] = {**context, "tracked_at": time.time()}
        _cleanup_tracked_messages()
    return True


def _tracked_record_matches(record: Dict[str, Any], context: Dict[str, str]) -> bool:
    if record.get("session_key") == context.get("session_key"):
        return True
    if context.get("platform") and record.get("platform") and (
        context["platform"] != record["platform"]
    ):
        return False
    if context.get("group_id"):
        return context["group_id"] == record.get("group_id")
    if context.get("user_id"):
        return context["user_id"] == record.get("user_id")
    return bool(
        context.get("stream_id")
        and context["stream_id"] == record.get("stream_id")
    )


def get_tracked_message_ids(
    kwargs: Optional[dict] = None,
    *,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
    limit: int = 20,
) -> List[str]:
    """返回当前会话内本插件实际发送的消息 ID（新到旧），供手动撤回使用。"""
    context = _tracking_context(
        kwargs, stream_id=stream_id, group_id=group_id, user_id=user_id,
    )
    if not context["session_key"]:
        return []
    try:
        safe_limit = max(0, int(limit))
    except (TypeError, ValueError):
        safe_limit = 20
    with _tracked_messages_lock:
        _cleanup_tracked_messages()
        matched = [
            message_id
            for message_id, record in reversed(_tracked_messages.items())
            if _tracked_record_matches(record, context)
        ]
    return matched[:safe_limit] if safe_limit else []


def is_tracked_message_id(message_id: Any, *, tracking_key: str = "") -> bool:
    """检查 ID 是否仍在本插件追踪表中；可选限制为指定会话 key。"""
    normalized = _normalize_message_id(message_id)
    if not normalized:
        return False
    with _tracked_messages_lock:
        _cleanup_tracked_messages()
        record = _tracked_messages.get(normalized)
        return bool(
            record and (not tracking_key or record.get("session_key") == tracking_key)
        )


def untrack_message_id(message_id: Any) -> bool:
    """在撤回成功后移除消息 ID；撤回失败时不应调用，以便稍后重试。"""
    normalized = _normalize_message_id(message_id)
    if not normalized:
        return False
    with _tracked_messages_lock:
        return _tracked_messages.pop(normalized, None) is not None


async def _delete_tracked_message_for_key(message_id: Any, tracking_key: str) -> bool:
    normalized = _normalize_message_id(message_id)
    if not normalized or not tracking_key or not is_tracked_message_id(
        normalized, tracking_key=tracking_key,
    ):
        return False
    try:
        try:
            api_message_id: Any = int(normalized)
        except ValueError:
            api_message_id = normalized
        response = await _napcat_action("delete_msg", {"message_id": api_message_id})
    except Exception:
        return False
    if not response or response.get("retcode") not in (0, 1, None) or (
        response.get("status") == "failed"
    ):
        return False
    untrack_message_id(normalized)
    return True


async def delete_tracked_message(message_id: Any, kwargs: Optional[dict] = None) -> bool:
    """精确撤回当前会话账本中的一条图片消息，成功后自动移除追踪记录。"""
    context = _tracking_context(kwargs or {})
    if not context["session_key"]:
        return False
    return await _delete_tracked_message_for_key(message_id, context["session_key"])


# 自动撤回

def schedule_auto_recall(kwargs: dict = None, after_ts: int = 0, message_id: Optional[str] = None):
    """启动自动撤回后台任务。

    Args:
        kwargs: 命令 kwargs
        after_ts: 发送时间戳（Unix 秒），在 message_id 不可用时作为历史匹配基准。
        message_id: 发送 API 返回的精确 message_id（优先使用）。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return

    kwargs = kwargs or {}
    info = get_session_info_from_kwargs(kwargs)
    stream_id = str(kwargs.get("stream_id", "") or "")

    normalized_msg_id = _normalize_message_id(message_id)

    allowed_groups = getattr(plugin.config.auto_recall, "allowed_groups", []) or []
    allowed_keys = {str(item).strip() for item in allowed_groups if str(item).strip()}
    session_allow_key = f"{info['platform']}:{info['chat_id']}"
    if allowed_keys and session_allow_key not in allowed_keys:
        plugin.ctx.logger.info(
            f"[自动撤回] 当前会话不在 allowed_groups，跳过: {session_allow_key}"
        )
        return

    if not plugin._session_state.is_recall_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
        stream_id=stream_id,
    ):
        return

    group_id = info["chat_id"] if info["chat_type"] == "group" else ""
    user_id = info["user_id"] if info["chat_type"] == "private" else ""
    tracking_context = _tracking_context(
        kwargs, stream_id=stream_id, group_id=group_id, user_id=user_id,
    )
    tracking_key = tracking_context["session_key"]
    if normalized_msg_id and not is_tracked_message_id(
        normalized_msg_id, tracking_key=tracking_key,
    ):
        plugin.ctx.logger.warning(
            f"[自动撤回] message_id 未由本插件在当前会话记录，跳过: {normalized_msg_id}"
        )
        return
    task = asyncio.create_task(auto_recall_task(
        stream_id=stream_id, group_id=group_id, user_id=user_id,
        platform=info["platform"], chat_id=info["chat_id"],
        after_ts=after_ts, message_id=normalized_msg_id,
        tracking_key=tracking_key,
    ))
    plugin._track_task(task)


async def _recall_from_recent_history(
    stream_id: str,
    group_id: str,
    user_id: str,
    after_ts: int,
    tracking_key: str,
) -> bool:
    """从最近历史中定位本 bot 的目标图片消息并撤回。"""
    plugin = get_plugin_instance()
    if not plugin:
        return False

    messages = await fetch_recent_messages(
        stream_id, limit=10, group_id=group_id, user_id=user_id,
    )
    if not messages:
        plugin.ctx.logger.info(f"[自动撤回] 未获取到消息 stream={stream_id}")
        return False

    candidates = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        msg_id = _normalize_message_id(msg.get("message_id"))
        if not msg_id:
            continue
        msg_time = int(msg.get("time", 0) or 0)
        if after_ts and msg_time > 0 and msg_time < after_ts - 2:
            continue
        if is_ai_draw_bot_message(msg):
            candidates.append((msg_time, msg_id))

    if not candidates:
        plugin.ctx.logger.info(f"[自动撤回] 未找到匹配消息 (after_ts={after_ts})")
        return False

    if any(item[0] > 0 for item in candidates):
        candidates.sort(key=lambda item: (
            abs(item[0] - after_ts),
            0 if item[0] >= after_ts else 1,
        ))
    target_time, target_id = candidates[0]

    try:
        try:
            api_message_id: Any = int(target_id)
        except ValueError:
            api_message_id = target_id
        response = await _napcat_action("delete_msg", {"message_id": api_message_id})
    except Exception as exc:
        plugin.ctx.logger.error(f"[自动撤回] 撤回 {target_id} 失败: {exc}")
        return False
    if not response or response.get("retcode") not in (0, 1, None) or (
        response.get("status") == "failed"
    ):
        plugin.ctx.logger.error(f"[自动撤回] 撤回 {target_id} 返回异常: {response}")
        return False

    untrack_message_id(target_id)
    plugin.ctx.logger.info(
        f"[自动撤回] 历史定位撤回成功: {target_id} "
        f"(after_ts={after_ts}, msg_time={target_time})"
    )
    return True


async def auto_recall_task(stream_id: str = "", group_id: str = "", user_id: str = "",
                         platform: str = "", chat_id: str = "",
                         after_ts: int = 0, message_id: Optional[str] = None,
                         tracking_key: str = ""):
    """延时后撤回本图消息。

    优先使用精确 message_id；必要时从最近历史中定位目标消息。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return

    try:
        delay = max(0, float(plugin.config.auto_recall.delay_seconds))
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        await asyncio.sleep(max(0, delay + jitter))

        # 开关可能在调度后、延时期间被 /ad c off 关闭；此时必须放弃撤回。
        if not plugin._session_state.is_recall_enabled(
            platform, chat_id, plugin._get_config_callable(), stream_id=stream_id,
        ):
            plugin.ctx.logger.info("[自动撤回] 会话开关已关闭，跳过已调度消息")
            return

        normalized_msg_id = _normalize_message_id(message_id)
        if normalized_msg_id and is_tracked_message_id(
            normalized_msg_id, tracking_key=tracking_key,
        ):
            deleted = await _delete_tracked_message_for_key(
                normalized_msg_id, tracking_key,
            )
            if deleted:
                plugin.ctx.logger.info(f"[自动撤回] 精确撤回成功: {normalized_msg_id}")
                return
            plugin.ctx.logger.warning(
                f"[自动撤回] 精确撤回失败: {normalized_msg_id}，改用历史定位"
            )

        await _recall_from_recent_history(
            stream_id, group_id, user_id, after_ts, tracking_key,
        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        plugin.ctx.logger.error(f"[自动撤回] 异常: {e}")
