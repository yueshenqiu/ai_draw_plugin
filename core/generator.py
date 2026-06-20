# -*- coding: utf-8 -*-
"""图片生成核心：调度 provider、构建请求、解析响应、发送结果。

从 plugin.py 的生图流程和撤回逻辑提取。
"""

import asyncio
import base64
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..instance import get_plugin_instance
from ..providers import get_provider_class
from .image_utils import process_api_response

_TEMP_IMAGES_DIR = Path(__file__).resolve().parent.parent / "temp_images"
_MAX_TEMP_FILES = 10

# 缓存 bot 真实 QQ 号和昵称（用于合并转发，避免伪造身份触发风控）
_cached_bot_self_id: str = ""
_cached_bot_nickname: str = ""


def _cleanup_temp_images() -> None:
    """保留最新的 _MAX_TEMP_FILES 个文件，删除多余的旧文件。"""
    try:
        files = sorted(_TEMP_IMAGES_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        while len(files) > _MAX_TEMP_FILES:
            oldest = files.pop(0)
            oldest.unlink(missing_ok=True)
    except Exception:
        pass


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
    img_bytes = base64.b64decode(image_base64)
    is_png = img_bytes[:8] == b'\x89PNG\r\n\x1a\n'
    suffix = ".png" if is_png else ".jpg"
    tmp_path = _get_temp_image_path(suffix=suffix, prefix="ai_fwd_")
    tmp_path.write_bytes(img_bytes)
    return str(tmp_path).replace("\\", "/")



# ================================================================
# 模型配置解析
# ================================================================

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


# ================================================================
# 图片生成编排
# ================================================================

async def generate_and_send(
    prompt: str,
    model_config: dict,
    stream_id: str,
    prompt_text: str = "",
    size: str = "",
    kwargs: dict = None,
    ref_image: str = "",
    ref_mode: str = "",
) -> None:
    """后台任务：生成图片 → 发送结果 → 触发自动撤回。"""
    plugin = get_plugin_instance()
    if not plugin:
        return

    try:
        image_size = size or model_config.get("size_preset") or model_config.get("nai_size") or model_config.get("default_size", "1024x1280")
        success, result = await generate_image(prompt, model_config, image_size, stream_id, ref_image, ref_mode)

        if not success:
            await plugin.ctx.send.text(f"生成图片失败：{result}", stream_id)
            return

        info = plugin._extract_session_info(kwargs or {})

        # 预热 bot 身份缓存，让会话内第一次合并转发不必现查身份
        if not _cached_bot_self_id:
            await _ensure_bot_identity(
                stream_id=stream_id,
                group_id=info["chat_id"] if info.get("chat_type") == "group" else "",
                user_id=info["user_id"] if info.get("chat_type") == "private" else "",
            )
        send_ok, sent_msg_id = await send_image_result(
            result, prompt_text or prompt, stream_id,
            group_id=info["chat_id"] if info.get("chat_type") == "group" else "",
            user_id=info.get("user_id", ""),
            kwargs=kwargs or {})
        if send_ok:
            send_ts = int(time.time())
            schedule_auto_recall(kwargs=kwargs or {}, after_ts=send_ts, message_id=sent_msg_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        plugin.ctx.logger.error(f"[生图] 后台异常: {e}", exc_info=True)
        try:
            await plugin.ctx.send.text(f"图片生成遇到问题: {str(e)[:100]}", stream_id)
        except Exception:
            pass


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
        msg_id = None
        if image_data.startswith(("iVBORw", "/9j/", "UklGR", "R0lGOD")):
            msg_id = await send_fn(image_data, stream_id, group_id, user_id)
        elif image_data.startswith(("http://", "https://")):
            await plugin.ctx.send.text(f"图片链接: {image_data}", stream_id)
        elif image_data.startswith("file://"):
            path = image_data[len("file://"):]
            if Path(path).exists():
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                msg_id = await send_fn(b64, stream_id, group_id, user_id)
            else:
                await plugin.ctx.send.text("图片文件不存在", stream_id)
        else:
            msg_id = await send_fn(image_data, stream_id, group_id, user_id)
        return True, msg_id
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
        nsfw_on = plugin._session_state.is_nsfw_filter_enabled(platform, chat_id, get_config)
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
    status = str(payload.get("status") or "").lower()
    retcode = payload.get("retcode")
    if (status and status != "ok") or (isinstance(retcode, int) and retcode not in (0, 1)):
        return None
    data = payload.get("data")
    msg_id = str(payload.get("message_id") or "")
    if not msg_id and isinstance(data, dict):
        msg_id = str(data.get("message_id") or data.get("msg_id") or "")
    return {
        "status": payload.get("status", "ok"),
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
    from urllib.parse import urlparse

    plugin = get_plugin_instance()
    if not plugin:
        return None
    base_url = str(getattr(plugin.config.plugin, "napcat_http_url", "") or "").strip()
    token = str(getattr(plugin.config.plugin, "napcat_http_token", "") or "").strip()
    host = (urlparse(base_url).hostname or "").lower()
    if host not in _LOCAL_HOSTS:
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
            ) as resp:
                if resp.status != 200:
                    plugin.ctx.logger.warning(f"[HTTP直连] {action} HTTP {resp.status}")
                    return None
                return await resp.json()
    except Exception as e:
        plugin.ctx.logger.warning(f"[HTTP直连] {action} 失败: {e}")
        return None


async def _napcat_action(action: str, params: dict) -> Optional[dict]:
    """调用 NapCat 动作，返回归一化响应 {status,retcode,message_id,data}。

    默认走 SDK passthrough（ctx.api.call，合规、在 SDK 边界内）；
    当 use_http_direct=true 时改走本机 HTTP 直连（慢环境提速、超时预算更长）。
    两个适配器（napcat-adapter / SnowLuma）返回结构一致。失败返回 None。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return None

    # 分流：HTTP 直连（可选） vs SDK passthrough（默认）
    if getattr(plugin.config.plugin, "use_http_direct", False):
        return _normalize_action_response(await _napcat_http_call(action, params))

    if action == "get_login_info":
        api_name = "adapter.napcat.system.get_login_info"
    else:
        api_name = f"adapter.napcat.message.{action}"
    try:
        result = await plugin.ctx.api.call(api_name, params=params)
    except Exception as e:
        plugin.ctx.logger.error(f"[passthrough] 调用 {api_name} 失败: {e}")
        return None
    if not isinstance(result, dict):
        plugin.ctx.logger.warning(f"[passthrough] {api_name} 返回非字典: {result!r}")
        return None
    # SDK 失败包装：handler 抛异常时返回 {"success": False, "error": ...}
    if result.get("success") is False:
        plugin.ctx.logger.warning(f"[passthrough] {api_name} 调用失败: {result.get('error')}")
        return None
    return _normalize_action_response(result)


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


# ================================================================
# 消息获取与识别
# ================================================================

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


def is_nai_bot_message(msg: dict, display_text: str = "") -> bool:
    """判断消息是否为本插件 bot 发送的图片消息。

    匹配规则（满足任一即可）：
    1. bot 自己发送的合并转发/JSON 消息
    2. bot 自己发送的图片消息（send.image 直发）
    3. bot 自己发送的文件消息（PDF回退）
    4. 文本包含 [NAI] 标记
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

    # 合并转发：bot 自己的就是 NAI 消息
    if has_forward:
        return is_self

    # 直接发图：bot 自己的图片消息同样需要撤回
    if has_image and is_self:
        return True

    # bot 自己发的文件消息（手动撤回时兜底）
    if has_file and is_self:
        return True

    # 文本内容含 [NAI] 标记
    content = extract_text_from_napcat_message(msg)
    if content:
        if "[NAI]" in content:
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
    """自动获取参考图：当前消息附件 → 引用消息 → NapCat 最近消息。"""
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
    for seg in raw_msg:
        if isinstance(seg, dict) and seg.get("type") == "image":
            data = seg.get("data", "")
            if isinstance(data, dict):
                data = data.get("file") or data.get("url") or data.get("base64") or ""
            if data:
                plugin.ctx.logger.info("[参考图] 从当前消息获取")
                return str(data)

    # 2. 引用消息中的图片（多种来源尝试）
    # 2a. message["reply"] 字段（部分 SDK 版本提供）
    reply = message.get("reply")
    if isinstance(reply, dict):
        img = extract_image_from_message(reply)
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
                inner = sdk_result.get("result", sdk_result)
                if isinstance(inner, dict):
                    target_msg_data = inner.get("message", inner)
                    if isinstance(target_msg_data, dict):
                        img = _extract_image_from_sdk_message(target_msg_data)
                        if img:
                            # 可能是 base64（binary_data_base64）或 URL；URL 需下载转 base64
                            if img.startswith(("http://", "https://")):
                                downloaded = await _download_image_as_base64(img, plugin)
                                if downloaded:
                                    plugin.ctx.logger.info("[参考图] 从 SDK get_by_id 获取引用图片(URL 下载)")
                                    return downloaded
                            else:
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

    # 3. NapCat 最近消息回退
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
            # NapCat 格式（raw_message/message 字段）
            segs = msg.get("message", msg.get("raw_message", []))
            for seg in (segs or []):
                if isinstance(seg, dict) and seg.get("type") == "image":
                    data = seg.get("data", "")
                    if isinstance(data, dict):
                        file_data = str(data.get("file") or "")
                        if file_data.startswith("base64://"):
                            plugin.ctx.logger.info("[参考图] NapCat 历史 base64")
                            return file_data[len("base64://"):]
                        if file_data:
                            resolved = await _resolve_napcat_file_image(file_data, plugin)
                            if resolved:
                                return resolved
                        url = str(data.get("url") or "")
                        if url:
                            downloaded = await _download_image_as_base64(url, plugin)
                            if downloaded:
                                return downloaded
                    if isinstance(data, str) and data:
                        continue  # SnowLuma 将图片替换为文字描述且不缓存二进制，跳过
                        return data
            # SDK 格式（SnowLuma 等非 NapCat 适配器），跳过文字描述
            img = _extract_image_from_sdk_message(msg)
            if img and not img.startswith("[图"):
                if img.startswith(("http://", "https://")):
                    downloaded = await _download_image_as_base64(img, plugin)
                    if downloaded:
                        plugin.ctx.logger.info("[参考图] SDK 历史 URL 下载")
                        return downloaded
                else:
                    plugin.ctx.logger.info("[参考图] SDK 历史图片")
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
    """从 NapCat 格式的消息字典中提取图片，优先转为 base64。"""
    segs = msg.get("message", msg.get("raw_message", []))
    if not isinstance(segs, list):
        return None
    for seg in segs:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        data = seg.get("data", "")
        if isinstance(data, dict):
            file_data = str(data.get("file") or "")
            if file_data.startswith("base64://"):
                return file_data[len("base64://"):]
            # 优先通过 NapCat get_image 获取本地文件并转 base64
            if file_data:
                resolved = await _resolve_napcat_file_image(file_data, plugin)
                if resolved:
                    return resolved
            # 回退：URL 下载转 base64（QQ 私链需鉴权，外部 API 无法直接访问）
            url = data.get("url") or ""
            if url:
                downloaded = await _download_image_as_base64(str(url), plugin)
                if downloaded:
                    return downloaded
                # 下载失败时不回传裸 URL——外部生图 API 无法读取鉴权私链
                return None
        elif isinstance(data, str) and data:
            return data
    return None


def _extract_image_from_sdk_message(msg: dict) -> Optional[str]:
    """从 SDK 格式的消息字典中提取图片。"""
    # message_segment 格式
    seg = msg.get("message_segment")
    if isinstance(seg, dict):
        if seg.get("type") in ("image", "imageurl"):
            content = seg.get("content") or (seg.get("data", {}) or {}).get("content", "")
            if content:
                return str(content)
        if seg.get("type") == "seglist":
            for child in seg.get("data", []) or []:
                if isinstance(child, dict) and child.get("type") in ("image", "imageurl"):
                    content = child.get("content") or (child.get("data", {}) or {}).get("content", "")
                    if content:
                        return str(content)

    # raw_message 格式
    raw = msg.get("raw_message", msg.get("message", []))
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict) and s.get("type") in ("image", "emoji"):
                # SnowLuma adapter 存储的原始二进制（include_binary_data=True 时返回）
                b64 = str(s.get("binary_data_base64") or "")
                if b64:
                    return b64
                data = s.get("data", "")
                if isinstance(data, dict):
                    url = data.get("url") or data.get("file") or data.get("base64") or ""
                    if url:
                        return str(url)
                elif isinstance(data, str) and data and not data.startswith("[图"):
                    return data
    return None


async def _download_image_as_base64(url: str, plugin) -> Optional[str]:
    """下载图片 URL 并转为 base64 字符串。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        import aiohttp
        from .http_client import get_session
        session = await get_session(30)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                plugin.ctx.logger.warning(f"[参考图] 下载图片失败 HTTP {resp.status} url={url[:200]}")
                return None
            data = await resp.read()
            if not data:
                return None
            return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        plugin.ctx.logger.warning(f"[参考图] 下载图片异常: {e}")
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
        napcat_resp = img_result.get("result", img_result)
        if not isinstance(napcat_resp, dict):
            napcat_resp = img_result
        inner = napcat_resp.get("data", {})
        if isinstance(inner, dict):
            file_or_url = str(inner.get("file") or inner.get("url") or "")
        elif isinstance(inner, str):
            file_or_url = inner
        else:
            return None
        if file_or_url.startswith("base64://"):
            plugin.ctx.logger.info("[参考图] get_image base64")
            return file_or_url[len("base64://"):]
        if file_or_url and not file_or_url.startswith(("http://", "https://")):
            img_path = Path(file_or_url)
            if img_path.exists():
                plugin.ctx.logger.info(f"[参考图] 本地文件: {file_or_url}")
                return base64.b64encode(img_path.read_bytes()).decode("utf-8")
        if file_or_url.startswith(("http://", "https://")):
            # get_image 返回的 URL 由本体刷新过 rkey，下载转 base64 供生图 API 使用；
            # 直接回传 URL 会让外部生图 API 拿到不可读图片（鉴权私链）。
            downloaded = await _download_image_as_base64(file_or_url, plugin)
            if downloaded:
                plugin.ctx.logger.info(f"[参考图] get_image URL 下载成功: {file_or_url[:60]}")
                return downloaded
            plugin.ctx.logger.warning(f"[参考图] get_image URL 下载失败: {file_or_url[:100]}")
            return None
    except Exception as e:
        plugin.ctx.logger.warning(f"[参考图] get_image 失败: {e}")
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


# ================================================================
# 会话信息提取
# ================================================================

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


# ================================================================
# 自动撤回
# ================================================================

def schedule_auto_recall(kwargs: dict = None, after_ts: int = 0, message_id: Optional[str] = None):
    """启动自动撤回后台任务。

    Args:
        kwargs: 命令 kwargs
        after_ts: 发送时间戳（Unix 秒），仅在 message_id 不可用时作为回退匹配。
        message_id: 发送 API 返回的精确 message_id（优先使用，无需匹配）。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return

    kwargs = kwargs or {}
    info = get_session_info_from_kwargs(kwargs)
    stream_id = str(kwargs.get("stream_id", "") or "")

    if not plugin._session_state.is_recall_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        return

    group_id = info["chat_id"] if info["chat_type"] == "group" else ""
    user_id = info["user_id"] if info["chat_type"] == "private" else ""

    task = asyncio.create_task(auto_recall_task(
        stream_id=stream_id, group_id=group_id, user_id=user_id,
        after_ts=after_ts, message_id=message_id,
    ))
    plugin._pending_tasks.append(task)


async def auto_recall_task(stream_id: str = "", group_id: str = "", user_id: str = "",
                         after_ts: int = 0, message_id: Optional[str] = None):
    """延时后撤回本图消息。

    优先使用 message_id 精确撤回（发送 API 返回的 ID）。
    仅在 message_id 不可用时回退到时间距离匹配。
    """
    plugin = get_plugin_instance()
    if not plugin:
        return

    try:
        delay = plugin.config.auto_recall.delay_seconds
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        await asyncio.sleep(delay + jitter)

        # 优先路径：直接用 message_id 撤回
        if message_id:
            try:
                resp = await _napcat_action("delete_msg", {"message_id": int(message_id)})
                if resp and resp.get("retcode") in (0, 1, None) and resp.get("status") != "failed":
                    plugin.ctx.logger.info(
                        f"[自动撤回] 精确撤回成功: message_id={message_id}"
                    )
                    return
                plugin.ctx.logger.warning(
                    f"[自动撤回] 精确撤回返回异常 (message_id={message_id}): {resp}"
                )
            except Exception as e:
                plugin.ctx.logger.warning(
                    f"[自动撤回] 精确撤回失败 (message_id={message_id}): {e}，回退时间匹配"
                )

        # 回退路径：时间距离匹配（message_id 不可用或撤回失败时）
        messages = await fetch_recent_messages(stream_id, limit=10, group_id=group_id, user_id=user_id)
        if not messages:
            plugin.ctx.logger.info(f"[自动撤回] 未获取到消息 stream={stream_id}")
            return

        candidates = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("message_id", "") or "")
            if not msg_id:
                continue
            msg_time = int(msg.get("time", 0) or 0)
            if after_ts and msg_time > 0 and msg_time < after_ts - 2:
                continue
            if is_nai_bot_message(msg):
                candidates.append((msg_time, msg_id))

        if not candidates:
            plugin.ctx.logger.info(f"[自动撤回] 未找到匹配消息 (after_ts={after_ts})")
            return

        has_time = any(c[0] > 0 for c in candidates)
        if has_time:
            candidates.sort(key=lambda x: (
                abs(x[0] - after_ts),
                0 if x[0] >= after_ts else 1,
            ))

        target_time, target_id = candidates[0]

        try:
            resp = await _napcat_action("delete_msg", {"message_id": int(target_id)})
            if resp and resp.get("retcode") in (0, 1, None) and resp.get("status") != "failed":
                plugin.ctx.logger.info(
                    f"[自动撤回] 回退撤回成功: {target_id} (after_ts={after_ts}, msg_time={target_time})"
                )
            else:
                plugin.ctx.logger.error(f"[自动撤回] 撤回 {target_id} 返回异常: {resp}")
        except Exception as e:
            plugin.ctx.logger.error(f"[自动撤回] 撤回 {target_id} 失败: {e}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        plugin.ctx.logger.error(f"[自动撤回] 异常: {e}")
