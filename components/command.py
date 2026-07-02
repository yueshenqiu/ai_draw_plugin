# -*- coding: utf-8 -*-
"""所有 /ad 命令处理的实际逻辑。

从 plugin.py 提取，通过 get_plugin_instance() 获取插件实例。
plugin.py 中只保留 @Command/@Tool 装饰器的薄包装方法。
"""

import asyncio
import base64
import re
import traceback
from pathlib import Path
from typing import Optional

from ..instance import get_plugin_instance
from ..constants.help_texts import HELP_TEXT
from ..constants.constants import MODEL_MAPPINGS, SIZE_MAPPINGS
from ..providers.capabilities import ImageFeature
from ..providers import get_capabilities


# ================================================================
# /ad help — 帮助
# ================================================================

async def handle_ad_help(stream_id: str) -> tuple:
    plugin = get_plugin_instance()
    await plugin.ctx.send.text(HELP_TEXT, stream_id)
    return True, "帮助已显示", 2


# ================================================================
# /ad on|off — 插件总开关
# ================================================================

async def handle_ad_plugin_toggle(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not info["chat_id"]:
        await plugin.ctx.send.text("无法获取会话信息", stream_id)
        return False, "无会话信息", 1

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    if action == "on":
        plugin._session_state.set_plugin_enabled(info["platform"], info["chat_id"], True)
        await plugin.ctx.send.text("插件已开启，可以正常使用生图命令", stream_id)
        return True, "插件已开启", 2
    elif action == "off":
        plugin._session_state.set_plugin_enabled(info["platform"], info["chat_id"], False)
        await plugin.ctx.send.text("插件已关闭，所有生图命令将不可用", stream_id)
        return True, "插件已关闭", 2
    return False, "未知操作", 1


# ================================================================
# /ad c on|off — 自动撤回开关
# ================================================================

async def handle_ad_recall_control(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not info["chat_id"]:
        await plugin.ctx.send.text("无法获取会话信息", stream_id)
        return False, "无会话信息", 1

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_chat_permission(info["platform"], info["chat_id"])
    if not ok:
        await plugin.ctx.send.text(err or "无权限", stream_id)
        return False, err, 1

    if action == "on":
        plugin._session_state.set_recall_enabled(info["platform"], info["chat_id"], True)
        delay = plugin.config.auto_recall.delay_seconds
        await plugin.ctx.send.text(f"自动撤回已开启，将在 {delay}s 后撤回图片", stream_id)
        return True, "自动撤回已开启", 2
    elif action == "off":
        plugin._session_state.set_recall_enabled(info["platform"], info["chat_id"], False)
        await plugin.ctx.send.text("自动撤回已关闭", stream_id)
        return True, "自动撤回已关闭", 2
    return False, "未知操作", 1


# ================================================================
# /ad nsfw <on|off> — NSFW 过滤开关
# ================================================================

async def handle_ad_nsfw_control(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not info["chat_id"]:
        await plugin.ctx.send.text("无法获取会话信息", stream_id)
        return False, "无会话信息", 1

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    if not action:
        enabled = plugin._session_state.is_nsfw_filter_enabled(
            info["platform"], info["chat_id"], plugin._get_config_callable(),
        )
        state_text = "开启" if enabled else "关闭"
        await plugin.ctx.send.text(f"NSFW 过滤当前状态：{state_text}", stream_id)
        return True, "已查询状态", 1

    if action == "on":
        plugin._session_state.set_nsfw_filter_enabled(info["platform"], info["chat_id"], True)
        await plugin.ctx.send.text("NSFW 过滤已开启", stream_id)
        return True, "NSFW过滤已开启", 2
    elif action == "off":
        plugin._session_state.set_nsfw_filter_enabled(info["platform"], info["chat_id"], False)
        await plugin.ctx.send.text("NSFW 过滤已关闭", stream_id)
        return True, "NSFW过滤已关闭", 2
    return False, "用法: /ad nsfw <on|off>", 1


# ================================================================
# /ad send <d|f> — 发送方式开关（d=直发 f=合并转发）
# ================================================================

async def handle_ad_send_mode(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not info["chat_id"]:
        await plugin.ctx.send.text("无法获取会话信息", stream_id)
        return False, "无会话信息", 1

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    # 无参数：查询当前状态
    if not action:
        mode = plugin._session_state.get_send_mode(
            info["platform"], info["chat_id"], plugin._get_config_callable(),
        )
        mode_text = "合并转发" if mode == "forward" else "普通直发"
        await plugin.ctx.send.text(
            f"当前发送方式：{mode_text}\n用法: /ad send d（直发）| /ad send f（合并转发）", stream_id,
        )
        return True, "已查询状态", 1

    if action in ("d", "direct"):
        plugin._session_state.set_send_mode(info["platform"], info["chat_id"], "direct")
        await plugin.ctx.send.text("发送方式已设为：普通直发（快）", stream_id)
        return True, "发送方式=直发", 2
    elif action in ("f", "forward"):
        plugin._session_state.set_send_mode(info["platform"], info["chat_id"], "forward")
        await plugin.ctx.send.text("发送方式已设为：合并转发（隐蔽但慢）", stream_id)
        return True, "发送方式=合并转发", 2
    return False, "用法: /ad send <d|f>", 1


# ================================================================
# /ad pt <on|off> — 提示词显示开关
# ================================================================

async def handle_ad_prompt_show(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not info["chat_id"]:
        await plugin.ctx.send.text("无法获取会话信息", stream_id)
        return False, "无会话信息", 1

    if plugin._session_state.is_admin_mode_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
            await plugin.ctx.send.text("没有权限", stream_id)
            return False, "没有权限", 1

    if action == "on":
        plugin._session_state.set_prompt_show_enabled(info["platform"], info["chat_id"], True)
        await plugin.ctx.send.text("提示词显示已开启", stream_id)
        return True, "提示词显示已开启", 2
    elif action == "off":
        plugin._session_state.set_prompt_show_enabled(info["platform"], info["chat_id"], False)
        await plugin.ctx.send.text("提示词显示已关闭", stream_id)
        return True, "提示词显示已关闭", 2
    return False, "用法: /ad pt <on|off>", 1


# ================================================================
# /ad st|sp — 管理员模式开关
# ================================================================

async def handle_ad_admin_toggle(action: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    if action == "st":
        plugin._session_state.set_admin_mode(info["platform"], info["chat_id"], True)
        await plugin.ctx.send.text("管理员模式已开启，仅管理员可使用生图命令", stream_id)
        return True, "管理员模式已开启", 2
    elif action == "sp":
        plugin._session_state.set_admin_mode(info["platform"], info["chat_id"], False)
        await plugin.ctx.send.text("管理员模式已关闭，所有人可使用生图命令", stream_id)
        return True, "管理员模式已关闭", 2
    return False, "用法: /ad st|sp", 1


# ================================================================
# /ad w <模型ID> — 切换模型  /ad m — 列出模型
# ================================================================

async def handle_ad_switch_model(param: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    # 权限检查
    if plugin._session_state.is_admin_mode_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
            await plugin.ctx.send.text("没有权限", stream_id)
            return False, "没有权限", 1

    # 列出所有可用模型
    if not param:
        all_models = plugin._loaded_models or {}
        default_model_id = plugin.config.models.default_model if hasattr(plugin.config, 'models') else "model1"
        current = plugin._session_state.get_selected_model(info["platform"], info["chat_id"]) or default_model_id

        lines = [f"当前模型: {current}", "---", "可用模型:"]
        for mid, cfg in all_models.items():
            if isinstance(cfg, dict):
                name = cfg.get("name", mid)
                fmt = cfg.get("format", "?")
                model_name = cfg.get("model", "?")
                marker = " ← 当前" if mid == current else ""
                lines.append(f"  {mid}: {name} [{fmt}] {model_name}{marker}")

        # 也列出旧版缩写
        lines.append("---")
        lines.append("快捷切换: " + ", ".join(f"{k}={v}" for k, v in MODEL_MAPPINGS.items()))
        await plugin.ctx.send.text("\n".join(lines), stream_id)
        return True, "已列出模型", 1

    # 切换模型
    full = MODEL_MAPPINGS.get(param)
    if full:
        plugin._session_state.set_selected_model(info["platform"], info["chat_id"], full)
        await plugin.ctx.send.text(f"已切换模型: {full}", stream_id)
        return True, f"已切换模型: {full}", 2

    # 直接 model_id 切换
    all_models = plugin._loaded_models or {}
    if param in all_models:
        plugin._session_state.set_selected_model(info["platform"], info["chat_id"], param)
        name = all_models[param].get("name", param) if isinstance(all_models[param], dict) else param
        await plugin.ctx.send.text(f"已切换模型: {param} ({name})", stream_id)
        return True, f"已切换: {param}", 2

    available = ", ".join(list(MODEL_MAPPINGS.keys()) + list(all_models.keys()))
    await plugin.ctx.send.text(f"未知模型: {param}\n可用: {available}", stream_id)
    return False, "未知模型", 1


# ================================================================
# /ad s <尺寸> — 切换尺寸
# ================================================================

async def handle_ad_switch_size(param: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    if not param:
        current = plugin._session_state.get_selected_size(info["platform"], info["chat_id"]) or "竖图"
        available = ", ".join(SIZE_MAPPINGS.keys())
        await plugin.ctx.send.text(f"当前尺寸: {current}\n可用: {available}", stream_id)
        return True, "已查询尺寸", 1

    size = SIZE_MAPPINGS.get(param)
    if not size:
        await plugin.ctx.send.text(f"未知尺寸: {param}\n可用: 竖/横/方", stream_id)
        return False, "未知尺寸", 1

    plugin._session_state.set_selected_size(info["platform"], info["chat_id"], size)
    await plugin.ctx.send.text(f"已切换尺寸: {param} ({size})", stream_id)
    return True, f"已切换尺寸: {size}", 2


# ================================================================
# /ad art <序号> — 切换风格预设（画师串）
# ================================================================

async def handle_ad_switch_artist(param: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    model_id = plugin._session_state.get_selected_model(info["platform"], info["chat_id"])
    if not model_id:
        model_id = plugin.config.models.default_model if hasattr(plugin.config, 'models') else "model1"

    all_models = plugin._loaded_models or {}
    model_cfg = all_models.get(model_id, {}) or {}
    # 使用统一的解析逻辑：模型内联 artist_presets > 全局 [artist_presets]
    presets_raw = plugin._session_state._resolve_model_artist_presets(model_id)
    presets = plugin._session_state._parse_artist_presets(presets_raw or [])

    if not presets:
        await plugin.ctx.send.text("当前模型没有配置风格预设（画师串）", stream_id)
        return False, "无风格预设", 1

    if not param:
        current_idx = plugin._session_state.get_effective_artist_index(
            info["platform"], info["chat_id"], model_id,
        )
        current_name = presets[current_idx - 1].get("name", f"#{current_idx}") if 1 <= current_idx <= len(presets) else "无"
        lines = [f"当前风格预设（画师串）: #{current_idx} {current_name}", "可用风格预设（画师串）:"]
        for i, p in enumerate(presets, 1):
            lines.append(f"  #{i} {p.get('name', '')}")
        await plugin.ctx.send.text("\n".join(lines), stream_id)
        return True, "已查询风格预设", 1

    try:
        idx = int(param)
    except ValueError:
        await plugin.ctx.send.text("请提供有效的风格预设序号（数字）", stream_id)
        return False, "无效序号", 1

    if not (1 <= idx <= len(presets)):
        await plugin.ctx.send.text(f"序号超出范围 (1-{len(presets)})", stream_id)
        return False, "序号超出范围", 1

    plugin._session_state.set_selected_artist_index(info["platform"], info["chat_id"], idx)
    name = presets[idx - 1].get("name", f"#{idx}")
    await plugin.ctx.send.text(f"已切换风格预设（画师串）: #{idx} {name}", stream_id)
    return True, f"已切换风格预设: #{idx}", 2


# ================================================================
# /ad 撤回 — 手动撤回
# ================================================================

async def handle_ad_manual_recall(kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    info = plugin._extract_session_info(kwargs)

    from ..core.generator import fetch_recent_messages, is_ai_draw_bot_message
    plugin.ctx.logger.info("[手动撤回] 执行撤回")

    try:
        messages = await fetch_recent_messages(
            stream_id=stream_id, limit=20,
            group_id=info["chat_id"] if info["chat_type"] == "group" else "",
            user_id=info["user_id"] if info["chat_type"] == "private" else "",
        )
        if not messages:
            await plugin.ctx.send.text("未找到最近消息", stream_id)
            return False, "无消息", 1

        bot_sender_ids = set()
        ids_to_recall = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("message_id", "") or "")
            if not msg_id:
                continue
            if is_ai_draw_bot_message(msg):
                ids_to_recall.append(msg_id)
                sender = msg.get("sender", {}) or {}
                sid = str(sender.get("user_id", ""))
                if sid:
                    bot_sender_ids.add(sid)

        if bot_sender_ids:
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("message_id", "") or "")
                if not msg_id or msg_id in ids_to_recall:
                    continue
                sender = msg.get("sender", {}) or {}
                sid = str(sender.get("user_id", ""))
                if sid not in bot_sender_ids:
                    continue
                segments = msg.get("message", msg.get("raw_message", []))
                if any(isinstance(s, dict) and s.get("type") == "image" for s in (segments or [])):
                    ids_to_recall.append(msg_id)

        recalled = 0
        for msg_id in ids_to_recall:
            try:
                await plugin.ctx.api.call("adapter.napcat.message.delete_msg", message_id=msg_id)
                recalled += 1
            except Exception:
                pass
            await asyncio.sleep(0.4)

        if recalled:
            await plugin.ctx.send.text(f"已撤回 {recalled} 条 AI绘图消息", stream_id)
        else:
            await plugin.ctx.send.text("未找到可撤回的 AI绘图消息", stream_id)
    except Exception as e:
        plugin.ctx.logger.error(f"[手动撤回] 失败: {e}")
        await plugin.ctx.send.text(f"撤回失败: {str(e)[:100]}", stream_id)
    return True, "撤回完成", 1


# ================================================================
# /ad y <名称> — 引用图片 + 提示词预设 → 图生图
# ================================================================

_styles_cache: Optional[dict] = None


def _load_styles() -> dict:
    """从 config.toml [styles] 加载提示词预设（缓存）。"""
    global _styles_cache
    if _styles_cache is not None:
        return _styles_cache
    try:
        import tomllib as _toml
        from pathlib import Path as _Path
        with open(_Path(__file__).parent.parent / "config.toml", "rb") as f:
            _styles_cache = _toml.load(f).get("styles", {})
        return _styles_cache
    except Exception:
        _styles_cache = {}
        return {}


def _resolve_style(name: str) -> Optional[str]:
    """根据名称（模糊匹配）查找提示词预设 prompt。"""
    styles = _load_styles()
    if not styles:
        return None
    if name in styles:
        return styles[name]
    # 模糊匹配
    name_lower = name.strip().lower()
    for k, v in styles.items():
        if name_lower == k.lower() or name_lower in k.lower() or k.lower() in name_lower:
            return v
    return None


async def handle_ad_style(style_name: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not style_name.strip():
        await plugin.ctx.send.text("请指定提示词预设名，例如：/ad y 线描", stream_id)
        return False, "未指定提示词预设", 1

    # 查找提示词预设 prompt
    style_prompt = _resolve_style(style_name.strip())
    if not style_prompt:
        styles = _load_styles()
        names = ", ".join(list(styles.keys())[:10])
        await plugin.ctx.send.text(f"未找到提示词预设 '{style_name}'。可用：{names}...", stream_id)
        return False, f"未找到提示词预设: {style_name}", 1

    # 获取参考图
    from ..core.generator import fetch_ref_image
    ref_image = await fetch_ref_image(kwargs, stream_id)
    if not ref_image:
        await plugin.ctx.send.text("请引用一张图片后使用 /ad y 命令", stream_id)
        return False, "未找到参考图", 1

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        await plugin.ctx.send.text(err, stream_id)
        return False, err, 1

    model_config = plugin._get_model_config_from_kwargs(
        kwargs,
        apply_artist_preset=getattr(plugin.config.plugin, "y_apply_artist_preset", False),
    )
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("模型配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1

    # NSFW 过滤
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        found = _filter_nsfw_tags_from_prompt(style_prompt)
        if found:
            plugin.ctx.logger.info("[提示词预设生图] NSFW过滤拦截: %s", ", ".join(found))
            await plugin.ctx.send.text(
                f"NSFW 过滤已开启，提示词预设 '{style_name}' 被拦截。请使用 /ad nsfw off 关闭过滤。",
                stream_id,
            )
            return False, f"NSFW过滤拦截: {found}", 1

    plugin.ctx.logger.info("[提示词预设生图] 预设=%s", style_name[:30])
    from ..core.generator import generate_and_send
    plugin._track_task(asyncio.create_task(
        generate_and_send(style_prompt, model_config, stream_id,
                          prompt_text=f"[{style_name}] {style_prompt[:80]}",
                          kwargs=kwargs, ref_image=ref_image, ref_mode="i2i")
    ))
    return True, f"正在图生图（{style_name}）...", 2


# ================================================================
# /ad0 <tags> — 直接 tag 生图
# ================================================================

async def handle_dr0_ref_draw(mode: str, tags: str, kwargs: dict) -> tuple:
    """直接参考生图：/ad0 rh|r|h|t <英文标签> — 跳过 LLM/VLM，直传参考图+标签"""
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not tags:
        await plugin.ctx.send.text(
            f"请输入英文标签，例如：/ad0 {mode} 1girl, lying on sofa, smile", stream_id
        )
        return False, "未提供标签", 1

    # 参考模式（角色/画风）仅管理员可用；i2i 图生图（t）不限制
    if mode in ("r", "h", "rh", "hr"):
        info = plugin._extract_session_info(kwargs)
        if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
            await plugin.ctx.send.text("没有权限", stream_id)
            return False, "没有权限", 1

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        await plugin.ctx.send.text(err, stream_id)
        return False, err, 1

    # 获取参考图
    from ..core.generator import fetch_ref_image
    ref_image = await fetch_ref_image(kwargs, stream_id)
    if not ref_image:
        await plugin.ctx.send.text(
            "未找到参考图片。请：\n1. 直接发送图片后使用命令\n2. 或引用（回复）一张图片",
            stream_id,
        )
        return False, "未找到参考图", 1

    mode_map = {"r": "character", "h": "style", "rh": "character&style", "hr": "character&style", "t": "i2i"}
    mode_names = {"r": "角色参考", "h": "画风参考", "rh": "角色+画风", "hr": "角色+画风", "t": "图生图"}
    ref_mode = mode_map[mode]

    plugin.ctx.logger.info("[直接参考生图] 模式=%s 标签=%s", mode_names[mode], tags[:80])

    model_config = plugin._get_model_config_from_kwargs(kwargs)
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("BestNAI 配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1

    # NSFW 过滤：扫描直接标签中是否包含违规 tag
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        found = _filter_nsfw_tags_from_prompt(tags)
        if found:
            plugin.ctx.logger.info("[直接参考生图] NSFW过滤拦截: %s", ", ".join(found))
            await plugin.ctx.send.text(
                f"NSFW 过滤已开启，以下标签被拦截：{', '.join(found)}\n"
                f"请使用 /ad nsfw off 关闭过滤后再试，或用 /ad {mode} <中文描述> 走 LLM 生图",
                stream_id,
            )
            return False, f"NSFW过滤拦截: {found}", 1

    from ..core.generator import generate_and_send
    plugin._track_task(asyncio.create_task(
        generate_and_send(tags, model_config, stream_id,
                          prompt_text=tags, kwargs=kwargs,
                          ref_image=ref_image, ref_mode=ref_mode)
    ))
    return True, f"正在生成图片（{mode_names[mode]}·直传）...", 2


async def handle_dr0_draw(description: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not description:
        await plugin.ctx.send.text("请输入英文标签，例如：/ad0 hatsune miku, smile", stream_id)
        return False, "未提供标签", 1

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        await plugin.ctx.send.text(err, stream_id)
        return False, err, 1

    plugin.ctx.logger.info("[直接生图] 标签: %s", description)
    model_config = plugin._get_model_config_from_kwargs(kwargs)
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("BestNAI 配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1

    # NSFW 过滤：扫描直接标签中是否包含违规 tag
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    ):
        found = _filter_nsfw_tags_from_prompt(description)
        if found:
            plugin.ctx.logger.info("[直接生图] NSFW过滤拦截: %s", ", ".join(found))
            await plugin.ctx.send.text(
                f"NSFW 过滤已开启，以下标签被拦截：{', '.join(found)}\n"
                f"请使用 /ad nsfw off 关闭过滤后再试，或用 /ad <中文描述> 走 LLM 生图",
                stream_id,
            )
            return False, f"NSFW过滤拦截: {found}", 1

    from ..core.generator import generate_and_send
    plugin._track_task(asyncio.create_task(
        generate_and_send(description, model_config, stream_id, prompt_text=description, kwargs=kwargs)
    ))
    return True, "正在生成图片...", 2


# ================================================================
# /ad r|h|rh|hr|t <描述> — 参考模式生图
# ================================================================

async def handle_ad_ref_draw(mode: str, description: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not description:
        await plugin.ctx.send.text(f"请输入描述，例如：/ad {mode} 拉姆穿浴衣", stream_id)
        return False, "未提供描述", 1

    # 参考模式（角色/画风）仅管理员可用；i2i 图生图（t）不限制
    if mode in ("r", "h", "rh", "hr"):
        info = plugin._extract_session_info(kwargs)
        if not plugin._session_state.is_admin_user(info["user_id"], plugin._get_config_callable()):
            await plugin.ctx.send.text("没有权限", stream_id)
            return False, "没有权限", 1

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        await plugin.ctx.send.text(err, stream_id)
        return False, err, 1

    from ..core.generator import fetch_ref_image
    ref_image = await fetch_ref_image(kwargs, stream_id)
    if not ref_image:
        await plugin.ctx.send.text(
            "未找到参考图片。请：\n1. 直接发送图片后使用命令\n2. 或引用（回复）一张图片",
            stream_id,
        )
        return False, "未找到参考图", 1

    mode_map = {"r": "character", "h": "style", "rh": "character&style", "hr": "character&style", "t": "i2i"}
    mode_names = {"r": "角色参考", "h": "画风参考", "rh": "角色+画风", "hr": "角色+画风", "t": "图生图"}
    ref_mode = mode_map[mode]

    plugin.ctx.logger.info("[参考生图] 模式=%s 描述=%s", mode_names[mode], description[:80])
    plugin._track_task(asyncio.create_task(
        ad_workflow(description, kwargs, is_action=False, ref_image=ref_image, ref_mode=ref_mode)
    ))
    return True, f"正在生成图片（{mode_names[mode]}）...", 2


# ================================================================
# /ad <描述> — LLM 提示词 → 生图
# ================================================================

async def handle_ad_draw(description: str, kwargs: dict) -> tuple:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not description:
        await plugin.ctx.send.text("请输入你想画的内容，例如：/ad 画一张初音未来", stream_id)
        return False, "未提供描述", 1

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        await plugin.ctx.send.text(err, stream_id)
        return False, err, 1

    plugin.ctx.logger.info("[LLM生图] 收到请求: %s", description[:80])
    plugin._track_task(asyncio.create_task(ad_workflow(description, kwargs, is_action=False)))
    return True, "正在生成图片...", 2


# ================================================================
# Tool: LLM 触发生图
# ================================================================

async def handle_ad_web_draw(description: str, size: str, kwargs: dict) -> dict:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not plugin._check_user_permission_from_kwargs(kwargs):
        return {"success": False, "message": "没有权限"}

    raw_description = description.strip()
    if not raw_description:
        return {"success": False, "message": "图片描述为空"}

    plugin._track_task(asyncio.create_task(
        ad_workflow(raw_description, kwargs, is_action=True, size=size)
    ))
    return {"success": True, "message": "图片生成请求已提交，请稍候"}


# ================================================================
# 核心工作流：描述 → LLM 提示词 → 图片 → 发送
# ================================================================

async def ad_workflow(
    description: str,
    kwargs: dict,
    is_action: bool = False,
    size: str = "",
    ref_image: str = "",
    ref_mode: str = "",
) -> None:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    # 自拍判定（在随机模式覆盖 description 之前，以原始用户输入为准）
    # /ad 命令：严格前缀匹配（/ad 自拍，xxx → selfie；/ad 画自拍 → 非selfie）
    # Tool 调用：子串匹配（LLM 描述"帮我生成一张自拍" → selfie）
    from ..core.selfie_engine import detect_selfie_prefix, detect_selfie_mode
    if is_action:
        is_selfie = detect_selfie_mode(description)
    else:
        is_selfie = detect_selfie_prefix(description)

    # 随机模式
    is_random_selfie = description in ("随机自拍", "random selfie")
    if description in ("随机", "random", "rand") or is_random_selfie:
        rand_desc = await _generate_random_description(selfie=is_random_selfie)
        if not rand_desc:
            await plugin.ctx.send.text("随机场景生成失败，请稍后再试~", stream_id)
            return
        description = rand_desc
        plugin.ctx.logger.info("[随机场景] %s", description)

    # LLM 提示词生成
    info = plugin._extract_session_info(kwargs)
    nsfw_enabled = plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    )

    # 角色/画风参考的隔离规则已内置进各自专属提示词模板（见 prompt_rules.get_generator_template），
    # 按 ref_mode 选模板即可，无需再在运行时向 base 注入“禁止外貌”块。

    # 自拍场景增强：从日程 + LLM 获取 action/environment/expression/lighting
    selfie_scene_context = ""
    if is_selfie and plugin.config.prompt_generator.scene_llm_enabled:
        from ..core.selfie_scene import (
            get_scene_for_selfie, build_scene_context, get_schedule_activity,
        )
        schedule_desc = await get_schedule_activity()
        if schedule_desc:
            plugin.ctx.logger.info("[自拍场景] 日程增强: %s", schedule_desc[:60])
        scene_input = schedule_desc or description
        scene = await get_scene_for_selfie(
            scene_input,
            api_base=plugin.config.prompt_generator.api_base or "",
            api_key=plugin.config.prompt_generator.api_key or "",
            model=plugin.config.prompt_generator.model_name or "",
        )
        if scene:
            plugin.ctx.logger.info(
                "[自拍场景] 增强: action=%s env=%s",
                scene.get("action", "")[:40], scene.get("environment", "")[:30],
            )
            selfie_scene_context = build_scene_context(scene)

    generated_prompt = await _generate_prompt_with_llm(
        description, stream_id, is_action, nsfw_enabled,
        ref_mode=ref_mode,
        selfie_scene_context=selfie_scene_context,
        is_selfie=is_selfie,
    )
    if not generated_prompt:
        await plugin.ctx.send.text("提示词生成失败，请稍后再试~", stream_id)
        return

    plugin.ctx.logger.debug("[LLM生图] 原始提示词: %s", generated_prompt)

    # 自拍处理（is_selfie 已在随机模式前以原始用户输入判定）
    from ..core.prompt_engine import normalize_prompt_order

    selfie_base_prompt = generated_prompt
    if is_selfie:
        model_cfg = plugin._get_model_config_from_kwargs(kwargs)

        # 尝试使用自拍参考图（仅在没有手动上传参考图时生效）
        selfie_ref_filename = (plugin.config.prompt_show.selfie_ref_image or "").strip()
        selfie_ref_used = False
        if selfie_ref_filename and not ref_image:
            ref_dir = Path(__file__).parent.parent / "selfie_refs"
            ref_path = ref_dir / selfie_ref_filename
            if ref_path.exists():
                provider_fmt = model_cfg.get("format", "bestnai")
                caps = get_capabilities(provider_fmt)
                if caps and ImageFeature.CHARACTER_REF in caps.features:
                    try:
                        ref_image = base64.b64encode(ref_path.read_bytes()).decode("utf-8")
                        ref_mode = "character"
                        selfie_ref_used = True
                        plugin.ctx.logger.info(
                            "[自拍参考图] 使用固定角色参考图: %s", selfie_ref_filename
                        )
                    except Exception as e:
                        plugin.ctx.logger.warning("[自拍参考图] 读取图片失败: %s", e)
                else:
                    plugin.ctx.logger.info(
                        "[自拍参考图] 当前 provider(%s) 不支持角色参考，回退文字提示词", provider_fmt
                    )
            else:
                plugin.ctx.logger.warning(
                    "[自拍参考图] 参考图文件不存在: %s", ref_path
                )

        # 使用参考图时跳过文字 selfie_prompt_add 合并（图片已定义角色外貌）
        include_selfie_add = not selfie_ref_used
        generated_prompt = _process_selfie_prompt(generated_prompt, description, include_selfie_add, model_cfg)

    if plugin.config.prompt_generator.enforce_tag_order:
        generated_prompt = normalize_prompt_order(generated_prompt)

    plugin.ctx.logger.info("[LLM生图] 最终提示词: %s", generated_prompt)

    # NSFW 开启时由 SFW 提示词模板（SFW_PROMPT_GENERATOR_*）从源头约束 LLM 产出，
    # 此处不再做产出后的黑名单二次拦截：避免 LLM 已规避、却因个别软色情词被拦下不发图。

    # 提示词显示
    if _is_prompt_show_enabled_from_kwargs(kwargs):
        show_prompt = generated_prompt
        header = "\U0001f4dd 提示词:"
        if is_selfie and plugin.config.prompt_show.hide_selfie_prompt_add:
            show_prompt = _process_selfie_prompt(
                selfie_base_prompt, description, False,
                plugin._get_model_config_from_kwargs(kwargs),
            )
            header = "\U0001f4dd 提示词(已隐藏自拍补充):"
        await plugin.ctx.send.text(f"{header}\n{show_prompt}", stream_id)

    # 生成并发送图片
    model_config = plugin._get_model_config_from_kwargs(kwargs)
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("BestNAI 配置错误，请检查配置文件", stream_id)
        return

    from ..core.generator import generate_and_send
    await generate_and_send(generated_prompt, model_config, stream_id,
                            prompt_text=generated_prompt, size=size, kwargs=kwargs,
                            ref_image=ref_image, ref_mode=ref_mode)


# ================================================================
# 内部辅助函数
# ================================================================

def _check_chat_permission(platform: str, chat_id: str) -> tuple:
    plugin = get_plugin_instance()
    allowed = plugin.config.auto_recall.allowed_groups
    if not allowed:
        return True, None
    key = f"{platform}:{chat_id}"
    if key in allowed:
        return True, None
    return False, "当前会话不在允许列表中"


# NSFW 标签黑名单（用于 /ad0 直接标签模式与 LLM 生图最终提示词，NSFW 过滤开启时生效）
_NSFW_BLACKLIST = [
    # 显式露骨
    "nsfw", "nude", "naked", "sex", "penis", "pussy", "vagina",
    "nipples", "anus", "penetration", "cum", "ejaculation",
    "fellatio", "cunnilingus", "paizuri", "footjob", "handjob",
    "masturbation", "orgasm", "topless", "bottomless", "no panties",
    "exposed", "spread pussy", "spread legs", "pussy juice",
    "fingering", "dildo", "vibrator", "bondage", "tentacle",
    " rape", "rape ", "guro", "gore", "loli", "shota",
    # 性暗示 / 软色情
    "suggestive", "seductive", "erotic", "lewd", "ecchi",
    "partially dressed", "partially undressed", "undressed",
    "clothes half-removed", "half-dressed", "half undressed",
    "bra visible", "bra strap", "panties", "underwear", "lingerie",
    "cleavage", "downblouse", "upskirt", "visible midriff",
    "skirt lifted", "skirt pull", "shirt lift", "clothes lift",
    "thighhighs", "garter belt", "see-through", "wet clothes",
    "presenting", "spread", "legs spread", "knees up", "m legs",
    "after sex", "ahegao", "drooling", "saliva", "covered nipples",
]


def _filter_nsfw_tags_from_prompt(prompt: str) -> tuple:
    """检查 prompt 中是否包含 NSFW 标签。返回 (过滤后prompt, 被过滤的标签列表)。"""
    prompt_lower = prompt.lower()
    found = []
    for tag in _NSFW_BLACKLIST:
        # 使用词边界匹配，避免误杀（如 "ass" 不杀 "grass"）
        if re.search(r'\b' + re.escape(tag) + r'\b', prompt_lower):
            found.append(tag)
    return found


def _check_plugin_enabled(kwargs: dict) -> tuple:
    """检查当前会话插件是否开启。返回 (ok, error_message)。"""
    plugin = get_plugin_instance()
    info = plugin._extract_session_info(kwargs)
    if not plugin._session_state.is_plugin_enabled(info["platform"], info["chat_id"]):
        return False, "插件已关闭，请使用 /ad on 开启"
    return True, None


def _is_prompt_show_enabled_from_kwargs(kwargs: dict) -> bool:
    plugin = get_plugin_instance()
    info = plugin._extract_session_info(kwargs)
    if not info["chat_id"]:
        return False
    return plugin._session_state.is_prompt_show_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
    )


def _process_selfie_prompt(description: str, raw_request: str,
                           include_selfie_add: bool, model_config: dict) -> str:
    plugin = get_plugin_instance()
    from ..core.selfie_engine import merge_selfie_prompt
    from ..core.prompt_engine import remove_selfie_appearance_tags, user_mentions_appearance

    selfie_add = (plugin.config.prompt_show.selfie_prompt_add or "") if plugin else ""
    policy = (plugin.config.prompt_generator.selfie_appearance_policy or "auto").strip().lower()
    user_specified = user_mentions_appearance(raw_request)

    if policy == "auto" and not user_specified:
        description = remove_selfie_appearance_tags(description)
    if include_selfie_add and selfie_add:
        description = merge_selfie_prompt(description, selfie_add)
    if policy == "never" and not user_specified:
        description = remove_selfie_appearance_tags(description)
    return description


async def _generate_prompt_with_llm(
    request_text: str, stream_id: str = "",
    is_action: bool = False, nsfw_enabled: bool = False,
    ref_mode: str = "",
    selfie_scene_context: str = "",
    is_selfie: bool = False,
) -> Optional[str]:
    plugin = get_plugin_instance()
    gen_cfg = plugin.config.prompt_generator

    if not request_text.strip():
        return None

    # 按参考模式 + NSFW 过滤开关 + 输出格式，取该指令专属的提示词模板
    from ..core.rules.prompt_rules import get_generator_template

    output_format = (gen_cfg.output_format or "json").strip().lower()
    default_tpl = get_generator_template(ref_mode, nsfw_enabled, output_format)

    template = gen_cfg.prompt_template or default_tpl
    prompt = _render_generator_prompt(template, request_text, is_action=is_action,
                                      selfie_scene_context=selfie_scene_context,
                                      is_selfie=is_selfie)

    # LLM 调用
    from ..core.prompt_engine import call_custom_llm_api, has_custom_api_config, cleanup_llm_prompt

    if has_custom_api_config({
        "api_base": gen_cfg.api_base, "api_key": gen_cfg.api_key, "model_name": gen_cfg.model_name,
    }):
        success, response, _, _ = await call_custom_llm_api(
            prompt=prompt, api_base=gen_cfg.api_base, api_key=gen_cfg.api_key,
            model=gen_cfg.model_name, temperature=gen_cfg.temperature, max_tokens=gen_cfg.max_tokens,
        )
    else:
        try:
            result = await plugin.ctx.llm.generate(
                prompt=prompt, temperature=gen_cfg.temperature, max_tokens=gen_cfg.max_tokens,
            )
            response = result.get("content", "") if isinstance(result, dict) else str(result)
            success = bool(response)
        except Exception as e:
            plugin.ctx.logger.error("[LLM] 生成提示词失败: %s", e)
            return None

    if not success:
        plugin.ctx.logger.error(f"[LLM] 提示词生成失败: {response}")
        return None
    if not response:
        plugin.ctx.logger.error("[LLM] 提示词生成失败: LLM 返回空内容")
        return None

    return cleanup_llm_prompt(response)


def _render_generator_prompt(template: str, request: str, is_action: bool = False,
                             selfie_scene_context: str = "",
                             is_selfie: bool = False) -> str:
    plugin = get_plugin_instance()
    from ..core.selfie_engine import get_selfie_hint
    from ..core.prompt_engine import build_current_time_context

    custom_sys = plugin.config.custom_prompt.system_prompt or ""
    if custom_sys:
        custom_sys = custom_sys.strip() + "\n\n"

    # 自拍规则块（约 40 行）仅在判定为自拍时注入；普通生图不带，节省 token
    selfie_hint = get_selfie_hint() if is_selfie else ""
    current_time = build_current_time_context()

    prompt = template.replace("<<CUSTOM_SYSTEM_PROMPT>>", custom_sys)
    if not is_action:
        prompt = prompt.replace("<<PREVIOUS_PROMPT>>", "")
    prompt = prompt.replace("<<SELFIE_SCENE_CONTEXT>>", selfie_scene_context or "")
    # 角色/画风隔离已内置进模板，占位符置空即可
    prompt = prompt.replace("<<CHARACTER_REF_CONTEXT>>", "")
    prompt = prompt.replace("<<USER_REQUEST>>", request.strip() or "N/A")
    prompt = prompt.replace("<<CURRENT_TIME_CONTEXT>>", current_time)
    prompt = prompt.replace("<<SELFIE_HINT>>", selfie_hint)
    return prompt.strip()


async def _generate_random_description(selfie: bool = False) -> Optional[str]:
    plugin = get_plugin_instance()
    rand_cfg = plugin.config.random_scene
    from ..core.random_scene import normalize_random_scene_description

    selfie_extra = ""
    if selfie:
        selfie_extra = "\n\n额外要求（自拍模式）：\n- 必须明确是自拍\n- 自拍内容同样要明确偏成人向"

    history = ""
    if plugin._recent_random_scenes:
        history = "\n".join(plugin._recent_random_scenes)
        history = f"\n\n以下是最近已生成过的内容，禁止与它们重复或相似：\n{history}"

    prompt = (
        "随机生成一个二次元 NSFW 场景，并用空格分隔的中文短标签描述它。\n"
        "要求：\n- 题材不限，强度不限\n"
        "- 结果必须具体、可视化、适合转成 Danbooru 风格标签\n"
        "- 只输出 1 行，包含 6-10 个中文短标签\n"
        f"- 标签尽量简短，使用明确视觉概念{selfie_extra}"
        f"{history}"
    )

    try:
        from ..core.prompt_engine import call_custom_llm_api, has_custom_api_config
        api_cfg = {
            "api_base": plugin.config.prompt_generator.api_base or "",
            "api_key": plugin.config.prompt_generator.api_key or "",
            "model_name": plugin.config.prompt_generator.model_name or "",
        }
        if has_custom_api_config(api_cfg):
            success, response, _, _ = await call_custom_llm_api(
                prompt=prompt,
                api_base=api_cfg["api_base"],
                api_key=api_cfg["api_key"],
                model=api_cfg["model_name"],
                temperature=rand_cfg.temperature,
                max_tokens=rand_cfg.max_tokens,
            )
            if not success:
                plugin.ctx.logger.error("[随机场景] 自定义 API 失败: %s", response)
                return None
        else:
            result = await plugin.ctx.llm.generate(
                prompt=prompt, temperature=rand_cfg.temperature, max_tokens=rand_cfg.max_tokens,
            )
            response = result.get("content", "") if isinstance(result, dict) else str(result)
    except Exception as e:
        plugin.ctx.logger.error("[随机场景] LLM调用失败: %s", e)
        return None

    if not response:
        return None
    lines = [l.strip() for l in response.strip().split("\n") if l.strip()]
    return normalize_random_scene_description(lines[0]) if lines else None

