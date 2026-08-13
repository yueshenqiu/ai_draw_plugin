# -*- coding: utf-8 -*-
"""所有 /ad 命令处理的实际逻辑。

从 plugin.py 提取，通过 get_plugin_instance() 获取插件实例。
plugin.py 中只保留 @Command/@Tool 装饰器的薄包装方法。
"""

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple

from ..core.prompt_types import GeneratedPrompt, PersonPrompt, StructuredPrompt

from ..instance import get_plugin_instance
from ..constants.help_texts import HELP_TEXT
from ..constants.constants import MODEL_MAPPINGS, SIZE_MAPPINGS
from ..providers.capabilities import ImageFeature
from ..providers import get_capabilities
from ..core.image_utils import (
    load_image_file_as_base64,
    load_queue_image_spool,
    remove_queue_image_spool,
    save_queue_image_spool,
)
from ..core.job_manager import (
    JobManagerClosedError,
    QueueFullError,
    SessionLimitError,
)


# 段守卫用：/ad 前允许的"同段正常前缀"——媒体占位符与 @某人（如私聊"引用图片 + /ad r"）
_LEADING_NOISE_RE = re.compile(
    r'^(?:\s*(?:\[image\]|\[图片\]|\[emoji\]|\[表情\]|\[voice\]|\[语音\]|\[file\]|\[文件\]|@[^\s]+)\s*)+',
    re.IGNORECASE,
)

_RANDOM_SELFIE_PREFIX_RE = re.compile(
    r"^(?:随机\s*自拍|random\s+selfie)(?=$|[\s,，:：])",
    re.IGNORECASE,
)
_RANDOM_PREFIX_RE = re.compile(
    r"^(?:随机|random|rand)(?=$|[\s,，:：])",
    re.IGNORECASE,
)
_SELFIE_PREFIX_RE = re.compile(
    r"^(?:自拍|selfie)(?=$|[\s,，:：])",
    re.IGNORECASE,
)
_MODE_SEPARATOR_RE = re.compile(r"^[\s,，:：]+")


@dataclass(frozen=True)
class GenerationRequest:
    policy: str
    request_text: str
    constraints: str
    is_selfie: bool


def _strip_mode_separator(value: str) -> str:
    return _MODE_SEPARATOR_RE.sub("", value or "").strip()


def _parse_generation_request(
    description: str,
    is_action: bool,
) -> GenerationRequest:
    """解析命令生成策略；Tool 保持旧的自由补全语义。"""
    raw = str(description or "").strip()
    from ..core.selfie_engine import detect_selfie_mode, detect_selfie_prefix

    if is_action:
        return GenerationRequest(
            policy="tool_legacy",
            request_text=raw,
            constraints=raw,
            is_selfie=detect_selfie_mode(raw),
        )

    selfie_match = _RANDOM_SELFIE_PREFIX_RE.match(raw)
    if selfie_match:
        constraints = _strip_mode_separator(raw[selfie_match.end():])
        return GenerationRequest(
            policy="random_content",
            request_text=constraints,
            constraints=constraints,
            is_selfie=True,
        )

    random_match = _RANDOM_PREFIX_RE.match(raw)
    if random_match:
        constraints = _strip_mode_separator(raw[random_match.end():])
        explicit_selfie = _SELFIE_PREFIX_RE.match(constraints)
        is_selfie = explicit_selfie is not None
        if explicit_selfie:
            constraints = _strip_mode_separator(
                constraints[explicit_selfie.end():]
            )
        return GenerationRequest(
            policy="random_content",
            request_text=constraints,
            constraints=constraints,
            is_selfie=is_selfie,
        )

    return GenerationRequest(
        policy="minimal",
        request_text=raw,
        constraints=raw,
        is_selfie=detect_selfie_prefix(raw),
    )


def _should_read_selfie_schedule(policy: str) -> bool:
    return policy in {"minimal", "random_content", "tool_legacy"}


_CONTINUATION_REQUEST_RE = re.compile(
    r"(?:继续|再来(?:一张|一个)?|还是这个|这身|这套|保持(?:这身|这套|设定)?|"
    r"换(?:个|一个)(?:姿势|动作|角度|场景|背景)|修改|改成|重画|重绘|"
    r"\b(?:again|continue|keep(?: this)?|same|redo|rerender|"
    r"change (?:the )?(?:pose|action|angle|scene|background|outfit)))\b",
    re.IGNORECASE,
)

_ACTION_HINT_RE = re.compile(
    r"躺|卧|坐|站|跪|趴|蹲|走|跑|跳|睡|抱|拿|握|举|抬|托|"
    r"微笑|笑|哭|看|望|挥手|摆|伸手|跷|盘腿|\b(?:lying|sitting|standing|"
    r"kneeling|crouching|sleeping|walking|running|holding|looking|smile|"
    r"waving)\b",
    re.IGNORECASE,
)
_SCENE_HINT_RE = re.compile(
    r"在[^，,。；;]{1,24}|床|卧室|浴室|教室|学校|街道|客厅|厨房|庭院|花园|"
    r"室内|室外|背景|白底|纯色|空白|\b(?:bed|bedroom|bathroom|classroom|school|"
    r"street|living room|kitchen|garden|indoors|outdoors|background)\b",
    re.IGNORECASE,
)


def _should_inherit_previous_context(request_text: str) -> bool:
    """仅在用户明确表达连续绘图时注入上一轮，避免新主题携带旧 Prompt。"""
    return bool(_CONTINUATION_REQUEST_RE.search(str(request_text or "")))


def _selfie_context_needed(description: str, policy: str) -> bool:
    """普通/随机自拍只在动作或场景仍缺失时读取日程；Tool 保持旧行为。"""
    if policy == "tool_legacy":
        return True
    text = str(description or "").strip()
    return not (_ACTION_HINT_RE.search(text) and _SCENE_HINT_RE.search(text))


def _build_selfie_schedule_context(policy: str, activity: str) -> str:
    activity = str(activity or "").strip()
    if not activity:
        return ""
    if policy == "minimal":
        guidance = (
            "精准自拍仅在用户未指定时，参考该活动补一个自然动作或姿态和一个简洁场景；"
            "不得增加第二个动作、第二处场景或其他自由发挥内容；"
            "日程只提供动作和场景参考，不是穿搭来源，不得改变、替换或弱化用户指定服装。"
        )
    else:
        guidance = (
            "随机自拍时优先围绕该活动补充动作、环境、表情和光线；"
            "日程只提供动作和场景参考，不是穿搭来源；"
            "用户固定的人物、服装及其他明确条件优先，不得被日程替换或弱化。"
        )
    return (
        '<selfie_scene_context source="current_schedule">\n'
        f"当前日程活动：{activity}\n"
        f"{guidance}\n"
        "</selfie_scene_context>"
    )


async def _build_selfie_scene_context(policy: str, description: str) -> str:
    plugin = get_plugin_instance()
    if not _should_read_selfie_schedule(policy):
        return ""

    from ..core.selfie_scene import (
        get_scene_for_selfie, build_scene_context, get_schedule_activity,
    )

    schedule_desc = await get_schedule_activity()
    if schedule_desc:
        plugin.ctx.logger.info("[自拍场景] 日程增强: %s", schedule_desc[:60])

    if policy in {"minimal", "random_content"}:
        return _build_selfie_schedule_context(policy, schedule_desc or "")

    scene = await get_scene_for_selfie(
        schedule_desc or description,
        api_base=plugin.config.prompt_generator.api_base or "",
        api_key=plugin.config.prompt_generator.api_key or "",
        model=plugin.config.prompt_generator.model_name or "",
    )
    if not scene:
        return ""
    plugin.ctx.logger.info(
        "[自拍场景] 增强: action=%s env=%s",
        scene.get("action", "")[:40], scene.get("environment", "")[:30],
    )
    return build_scene_context(scene)


def _get_random_fixed_constraints(request: GenerationRequest) -> str:
    constraints = request.constraints.strip()
    if not request.is_selfie:
        return constraints
    return f"自拍，{constraints}" if constraints else "自拍"


def _build_random_request_text(constraints: str) -> str:
    if constraints.strip():
        return (
            f"用户固定条件：{constraints.strip()}\n"
            "请按随机模式自由补充其余画面内容。"
        )
    return "请按随机模式生成一个完整且具体的随机画面。"


def _short_job_label(prefix: str, description: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(description or "")).strip()
    if len(text) > limit:
        text = f"{text[:limit - 1]}…"
    return f"{prefix}：{text}" if text else prefix


def _compact_job_kwargs(kwargs: dict) -> dict:
    """只保留后台任务所需的会话元数据，避免把整段图片二进制留在队列中。"""
    compact = {
        key: kwargs.get(key)
        for key in ("stream_id", "platform", "user_id", "group_id")
        if kwargs.get(key) not in (None, "")
    }
    message = kwargs.get("message")
    if not isinstance(message, dict):
        return compact

    message_info = message.get("message_info") or {}
    group_info = message_info.get("group_info") or {}
    user_info = message_info.get("user_info") or {}
    compact["message"] = {
        "platform": message.get("platform", ""),
        "message_info": {
            "group_info": {"group_id": group_info.get("group_id", "")},
            "user_info": {"user_id": user_info.get("user_id", "")},
        },
    }
    return compact


def _dispose_job_cleanup(
    cleanup: Optional[Callable[[], Any]], plugin: Any = None,
) -> None:
    if not callable(cleanup):
        return
    try:
        cleanup()
    except BaseException as exc:
        if plugin is not None:
            plugin.ctx.logger.warning("[生图任务] 清理任务资源失败: %s", exc)


def _is_queue_enabled(plugin: Any) -> bool:
    queue_config = getattr(getattr(plugin, "config", None), "queue", None)
    return bool(getattr(queue_config, "enabled", True))


async def _enqueue_draw_job(
    kwargs: dict,
    label: str,
    factory: Callable[[dict], Awaitable[Any]],
    cleanup: Optional[Callable[[], Any]] = None,
) -> tuple:
    """提交生图协程工厂，并把队列异常转换成安全的用户提示。"""
    plugin = get_plugin_instance()
    if plugin is None:
        _dispose_job_cleanup(cleanup, plugin)
        return None, "插件尚未就绪，请稍后再试"

    job_kwargs = _compact_job_kwargs(kwargs)

    async def _run_and_require_success() -> Any:
        result = await factory(job_kwargs)
        if result is False:
            raise RuntimeError("生图流程未成功完成")
        return result

    if not _is_queue_enabled(plugin):
        pending_cleanup = cleanup

        def _release_direct_resources(_task: Optional[asyncio.Task] = None) -> None:
            nonlocal pending_cleanup
            owned_cleanup = pending_cleanup
            pending_cleanup = None
            _dispose_job_cleanup(owned_cleanup, plugin)

        task: Optional[asyncio.Task] = None
        try:
            task = asyncio.create_task(
                _run_and_require_success(),
                name="ai-draw-direct",
            )
            task.add_done_callback(_release_direct_resources)
            plugin._track_task(task)
        except Exception as exc:
            if task is not None:
                task.cancel()
            else:
                _release_direct_resources()
            plugin.ctx.logger.error("[生图任务] 直接启动失败: %s", exc, exc_info=True)
            return None, "启动生图任务失败，请稍后再试"
        return {
            "job_id": None,
            "status": "running",
            "queue_position": None,
        }, "生图任务已直接启动（任务队列未启用）"

    manager = getattr(plugin, "_job_manager", None)
    if manager is None:
        _dispose_job_cleanup(cleanup, plugin)
        return None, "任务队列尚未就绪，请稍后再试"

    session_key = plugin._get_job_session_key(kwargs)
    submitted = False
    try:
        job_id = await manager.submit(
            _run_and_require_success,
            session_key,
            label=label,
            cleanup=cleanup,
        )
        submitted = True
    except SessionLimitError:
        return None, "当前会话的生图任务已达上限，请稍后再试，或用 /ad status、/ad cancel 管理任务"
    except QueueFullError:
        return None, "全局生图队列已满，请稍后再试"
    except JobManagerClosedError:
        return None, "插件正在重载，暂时无法提交生图任务"
    except Exception as exc:
        plugin.ctx.logger.error("[任务队列] 提交失败: %s", exc, exc_info=True)
        return None, "提交生图任务失败，请稍后再试"
    finally:
        if not submitted:
            _dispose_job_cleanup(cleanup, plugin)

    snapshots = await manager.status(session_key)
    snapshot = next((item for item in snapshots if item.job_id == job_id), None)
    status = snapshot.status if snapshot is not None else "queued"
    queue_position = snapshot.queue_position if snapshot is not None else None
    if status == "running":
        message = f"生图任务已开始，任务 ID：{job_id}"
    elif status == "queued":
        position_text = f"（全局队列第 {queue_position} 位）" if queue_position else ""
        message = f"生图任务已排队{position_text}，任务 ID：{job_id}"
    elif status == "completed":
        message = f"生图任务已完成，任务 ID：{job_id}"
    else:
        message = f"生图任务已提交，任务 ID：{job_id}"
    return {
        "job_id": job_id,
        "status": status,
        "queue_position": queue_position,
    }, message


async def _enqueue_ref_draw_job(
    kwargs: dict,
    label: str,
    ref_image: str,
    factory: Callable[[dict, str], Awaitable[Any]],
) -> tuple:
    """把参考图暂存到磁盘，避免后台任务长期持有大块 Base64。"""
    spool_path = save_queue_image_spool(ref_image)
    if not spool_path:
        return None, "参考图暂存失败，请重新引用图片后再试"

    async def _run_with_spooled_ref(job_kwargs: dict) -> Any:
        queued_ref_image = load_queue_image_spool(spool_path)
        if not queued_ref_image:
            plugin = get_plugin_instance()
            stream_id = str(job_kwargs.get("stream_id", "") or "")
            if plugin is not None and stream_id:
                await plugin.ctx.send.text("任务参考图已失效，请重新引用图片后再试", stream_id)
            return False
        return await factory(job_kwargs, queued_ref_image)

    return await _enqueue_draw_job(
        kwargs,
        label,
        _run_with_spooled_ref,
        cleanup=lambda: remove_queue_image_spool(spool_path),
    )


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


# ================================================================
# /ad help — 帮助
# ================================================================

async def handle_ad_help(stream_id: str) -> tuple:
    plugin = get_plugin_instance()
    await plugin.ctx.send.text(HELP_TEXT, stream_id)
    return True, "帮助已显示", 2


async def handle_ad_status(kwargs: dict) -> tuple:
    """显示当前会话的活动任务与少量最近历史。"""
    plugin = get_plugin_instance()
    stream_id = str(kwargs.get("stream_id", "") or "")
    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1
    if not _is_queue_enabled(plugin):
        message = "任务队列未启用；生图任务会直接启动，不提供队列状态查询"
        await plugin.ctx.send.text(message, stream_id)
        return True, message, 2

    manager = getattr(plugin, "_job_manager", None)
    if manager is None:
        await plugin.ctx.send.text("任务队列尚未就绪", stream_id)
        return False, "任务队列尚未就绪", 1

    jobs = await manager.status(plugin._get_job_session_key(kwargs))
    active = [item for item in jobs if item.status in ("queued", "running")]
    recent = [item for item in jobs if item.status not in ("queued", "running")][-3:]
    if not active and not recent:
        message = "当前会话没有生图任务"
        await plugin.ctx.send.text(message, stream_id)
        return True, message, 2

    now = time.time()
    lines = ["🎨 当前会话生图任务"]
    if active:
        for item in active:
            label = item.label or "生图任务"
            if item.cancel_requested:
                state = "取消中"
                elapsed = now - (item.started_at or item.created_at)
            elif item.status == "queued":
                state = f"排队中 · 全局第 {item.queue_position or '?'} 位"
                elapsed = now - item.created_at
            else:
                state = "生成中"
                elapsed = now - (item.started_at or item.created_at)
            lines.append(
                f"• {item.job_id} · {state} · {_format_duration(elapsed)}\n  {label}"
            )
    else:
        lines.append("• 当前没有排队或运行中的任务")

    if recent:
        state_names = {
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }
        lines.append("最近记录：")
        for item in reversed(recent):
            lines.append(
                f"• {item.job_id} · {state_names.get(item.status, item.status)} · "
                f"{item.label or '生图任务'}"
            )

    message = "\n".join(lines)
    await plugin.ctx.send.text(message, stream_id)
    return True, "任务状态已显示", 2


async def handle_ad_cancel(job_id: str, kwargs: dict) -> tuple:
    """取消指定任务；未给 ID 时只取消当前会话最近提交的活动任务。"""
    plugin = get_plugin_instance()
    stream_id = str(kwargs.get("stream_id", "") or "")
    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1
    if not _is_queue_enabled(plugin):
        message = "任务队列未启用；直接启动的任务不支持 /ad cancel"
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1

    manager = getattr(plugin, "_job_manager", None)
    if manager is None:
        await plugin.ctx.send.text("任务队列尚未就绪", stream_id)
        return False, "任务队列尚未就绪", 1

    session_key = plugin._get_job_session_key(kwargs)
    requested = str(job_id or "").strip()
    cancel_all = requested.lower() == "all" or requested == "全部"
    if cancel_all:
        cancelled = await manager.cancel(session_key, None)
        if cancelled:
            message = f"已请求取消当前会话的 {len(cancelled)} 个生图任务"
        else:
            message = "当前会话没有可取消的生图任务"
    else:
        target_id = requested.lower()
        if not target_id:
            jobs = await manager.status(session_key)
            candidates = [
                item for item in jobs
                if item.status in ("queued", "running") and not item.cancel_requested
            ]
            if not candidates:
                message = "当前会话没有可取消的生图任务"
                await plugin.ctx.send.text(message, stream_id)
                return False, message, 1
            target_id = max(candidates, key=lambda item: item.created_at).job_id

        cancelled = await manager.cancel(session_key, target_id)
        if cancelled:
            message = f"已请求取消生图任务：{target_id}"
        else:
            message = f"未找到可取消的任务 {target_id}；它可能已完成或正在取消"

    await plugin.ctx.send.text(message, stream_id)
    return bool(cancelled), message, 2 if cancelled else 1


async def handle_ad_context_reset(kwargs: dict) -> tuple:
    """清除当前会话的连续绘图上下文。"""
    plugin = get_plugin_instance()
    stream_id = str(kwargs.get("stream_id", "") or "")
    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1
    removed = plugin._session_state.clear_draw_context(stream_id)
    message = "已清除连续绘图上下文" if removed else "当前没有可清除的连续绘图上下文"
    await plugin.ctx.send.text(message, stream_id)
    return True, message, 2


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
        if plugin.config.plugin.enabled:
            message = "插件已开启，可以正常使用生图命令"
        else:
            message = "当前会话已开启，但插件仍被全局配置关闭"
        await plugin.ctx.send.text(message, stream_id)
        return True, message, 2
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
        plugin._session_state.set_recall_enabled(
            info["platform"], info["chat_id"], True, stream_id=stream_id,
        )
        delay = plugin.config.auto_recall.delay_seconds
        await plugin.ctx.send.text(f"自动撤回已开启，将在 {delay}s 后撤回图片", stream_id)
        return True, "自动撤回已开启", 2
    elif action == "off":
        plugin._session_state.set_recall_enabled(
            info["platform"], info["chat_id"], False, stream_id=stream_id,
        )
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
            stream_id=stream_id,
        )
        state_text = "开启" if enabled else "关闭"
        await plugin.ctx.send.text(f"NSFW 过滤当前状态：{state_text}", stream_id)
        return True, "已查询状态", 1

    if action not in {"on", "off"}:
        return False, "用法: /ad nsfw <on|off>", 1

    enabled = action == "on"
    plugin._session_state.set_nsfw_filter_enabled(
        info["platform"], info["chat_id"], enabled, stream_id=stream_id,
    )

    state_text = "开启" if enabled else "关闭"
    await plugin.ctx.send.text(f"NSFW 过滤已{state_text}", stream_id)
    return True, f"NSFW过滤已{state_text}", 2


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
        current = plugin._session_state.get_selected_size(
            info["platform"], info["chat_id"], stream_id=stream_id,
        ) or "竖图"
        available = ", ".join(SIZE_MAPPINGS.keys())
        await plugin.ctx.send.text(f"当前尺寸: {current}\n可用: {available}", stream_id)
        return True, "已查询尺寸", 1

    size = SIZE_MAPPINGS.get(param)
    if not size:
        await plugin.ctx.send.text(f"未知尺寸: {param}\n可用: 竖/横/方", stream_id)
        return False, "未知尺寸", 1

    plugin._session_state.set_selected_size(
        info["platform"], info["chat_id"], size, stream_id=stream_id,
    )
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
    from ..core.generator import delete_tracked_message, get_tracked_message_ids
    plugin.ctx.logger.info("[手动撤回] 执行撤回")

    try:
        ids_to_recall = get_tracked_message_ids(kwargs, limit=20)
        if not ids_to_recall:
            await plugin.ctx.send.text("当前会话没有可撤回的 AI绘图消息", stream_id)
            return False, "无可撤回消息", 1

        recalled = 0
        for msg_id in ids_to_recall:
            if await delete_tracked_message(msg_id, kwargs):
                recalled += 1
            await asyncio.sleep(0.2)

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


def clear_styles_cache() -> None:
    """清除提示词预设缓存，供配置热重载调用（否则换了 config.toml 仍读旧缓存）。"""
    global _styles_cache
    _styles_cache = None


def _load_styles() -> dict:
    """从 config.toml [styles] 加载提示词预设（缓存），归一成 {名称: 提示词} 字典。

    兼容两种结构：旧扁平字典 {名称: 提示词}，新结构 {"presets": [{name, prompt}]}。
    """
    global _styles_cache
    if _styles_cache is not None:
        return _styles_cache
    try:
        import tomllib as _toml
        from pathlib import Path as _Path
        with open(_Path(__file__).parent.parent / "config.toml", "rb") as f:
            raw = _toml.load(f).get("styles", {})
        if isinstance(raw, dict) and "presets" in raw:
            # 新结构：列表转 {名称: 提示词}
            result = {}
            for item in (raw.get("presets") or []):
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "").strip()
                    prompt = item.get("prompt", "")
                    if name and isinstance(prompt, str):
                        result[name] = prompt
            _styles_cache = result
        elif isinstance(raw, dict):
            # 旧扁平字典：只保留 str 值
            _styles_cache = {k: v for k, v in raw.items() if isinstance(v, str)}
        else:
            _styles_cache = {}
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

    if not plugin._check_user_permission_from_kwargs(kwargs):
        await plugin.ctx.send.text("没有权限", stream_id)
        return False, "没有权限", 1

    ok, err = _check_plugin_enabled(kwargs)
    if not ok:
        if err:
            await plugin.ctx.send.text(err, stream_id)
        return False, err or "已忽略", 1

    # 通过权限与开关校验后再读取参考图，避免无权限请求触发历史/网络读取。
    from ..core.generator import fetch_ref_image
    ref_image = await fetch_ref_image(kwargs, stream_id)
    if not ref_image:
        await plugin.ctx.send.text("请引用一张图片后使用 /ad y 命令", stream_id)
        return False, "未找到参考图", 1

    model_config = plugin._get_model_config_from_kwargs(
        kwargs,
        apply_artist_preset=getattr(plugin.config.plugin, "y_apply_artist_preset", False),
    )
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("当前生图模型配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1
    supported, reason = _check_ref_mode_capability(model_config, "i2i")
    if not supported:
        await plugin.ctx.send.text(reason or "当前模型不支持图生图", stream_id)
        return False, reason or "不支持图生图", 1

    # NSFW 过滤
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
        stream_id=stream_id,
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
    job, message = await _enqueue_ref_draw_job(
        kwargs,
        _short_job_label("提示词预设图生图", style_name),
        ref_image,
        lambda job_kwargs, queued_ref_image: generate_and_send(
            style_prompt, model_config, stream_id,
            prompt_text=f"[{style_name}] {style_prompt[:80]}",
            kwargs=job_kwargs, ref_image=queued_ref_image, ref_mode="i2i",
        ),
    )
    if not job:
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1
    return True, message, 2


# ================================================================
# /ad0 <tags> — 直接 tag 生图
# ================================================================

async def handle_dr0_ref_draw(mode: str, tags: str, kwargs: dict) -> tuple:
    """直接参考生图：/ad0 rh|r|h|t <英文标签> — 跳过 LLM，直传参考图+标签"""
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
        if err:
            await plugin.ctx.send.text(err, stream_id)
        return False, err or "已忽略", 1

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
        await plugin.ctx.send.text("当前生图模型配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1
    supported, reason = _check_ref_mode_capability(model_config, ref_mode)
    if not supported:
        await plugin.ctx.send.text(reason or "当前模型不支持该参考模式", stream_id)
        return False, reason or "不支持参考模式", 1

    # NSFW 过滤：扫描直接标签中是否包含违规 tag
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
        stream_id=stream_id,
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
    job, message = await _enqueue_ref_draw_job(
        kwargs,
        _short_job_label(f"{mode_names[mode]}·直传", tags),
        ref_image,
        lambda job_kwargs, queued_ref_image: generate_and_send(
            tags, model_config, stream_id,
            prompt_text=tags, kwargs=job_kwargs,
            ref_image=queued_ref_image, ref_mode=ref_mode,
        ),
    )
    if not job:
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1
    return True, message, 2


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
        if err:
            await plugin.ctx.send.text(err, stream_id)
        return False, err or "已忽略", 1

    plugin.ctx.logger.info("[直接生图] 标签: %s", description)
    model_config = plugin._get_model_config_from_kwargs(kwargs)
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("当前生图模型配置错误，请检查配置文件", stream_id)
        return False, "配置错误", 1

    # NSFW 过滤：扫描直接标签中是否包含违规 tag
    info = plugin._extract_session_info(kwargs)
    if plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
        stream_id=stream_id,
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
    job, message = await _enqueue_draw_job(
        kwargs,
        _short_job_label("直接标签生图", description),
        lambda job_kwargs: generate_and_send(
            description, model_config, stream_id,
            prompt_text=description, kwargs=job_kwargs,
        ),
    )
    if not job:
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1
    return True, message, 2


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
        if err:
            await plugin.ctx.send.text(err, stream_id)
        return False, err or "已忽略", 1

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
    job, message = await _enqueue_ref_draw_job(
        kwargs,
        _short_job_label(mode_names[mode], description),
        ref_image,
        lambda job_kwargs, queued_ref_image: ad_workflow(
            description, job_kwargs, is_action=False,
            ref_image=queued_ref_image, ref_mode=ref_mode,
        ),
    )
    if not job:
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1
    return True, message, 2


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
        if err:
            await plugin.ctx.send.text(err, stream_id)
        return False, err or "已忽略", 1

    plugin.ctx.logger.info("[LLM生图] 收到请求: %s", description[:80])
    job, message = await _enqueue_draw_job(
        kwargs,
        _short_job_label("自然语言生图", description),
        lambda job_kwargs: ad_workflow(description, job_kwargs, is_action=False),
    )
    if not job:
        await plugin.ctx.send.text(message, stream_id)
        return False, message, 1
    return True, message, 2


# ================================================================
# Tool: LLM 触发生图
# ================================================================

_TOOL_SIZE_ALIASES = {
    "portrait": "832x1216",
    "vertical": "832x1216",
    "landscape": "1216x832",
    "horizontal": "1216x832",
    "square": "1024x1024",
    "832x1216": "832x1216",
    "1216x832": "1216x832",
    "1024x1024": "1024x1024",
}


def _normalize_tool_size(size: str) -> tuple[str, Optional[str]]:
    value = str(size or "").strip().lower().replace("×", "x")
    if not value or value in {"auto", "default", "自动", "默认"}:
        return "", None
    normalized = SIZE_MAPPINGS.get(value) or _TOOL_SIZE_ALIASES.get(value)
    if normalized:
        return normalized, None
    return "", (
        f"不支持的图片尺寸: {str(size)[:50]}。"
        "可用值: portrait/832x1216、landscape/1216x832、square/1024x1024"
    )

async def handle_ad_web_draw(description: str, size: str, kwargs: dict) -> dict:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")

    if not plugin._check_user_permission_from_kwargs(kwargs):
        return {"success": False, "message": "没有权限"}

    info = plugin._extract_session_info(kwargs)
    if not plugin.config.plugin.enabled:
        return {"success": False, "message": "插件已被全局关闭"}
    if not plugin._session_state.is_plugin_enabled(info["platform"], info["chat_id"]):
        return {"success": False, "message": "当前会话已关闭生图功能"}

    raw_description = description.strip()
    if not raw_description:
        return {"success": False, "message": "图片描述为空"}

    normalized_size, size_error = _normalize_tool_size(size)
    if size_error:
        return {"success": False, "message": size_error}

    job, message = await _enqueue_draw_job(
        kwargs,
        _short_job_label("Tool 生图", raw_description),
        lambda job_kwargs: ad_workflow(
            raw_description, job_kwargs, is_action=True, size=normalized_size,
        ),
    )
    if not job:
        return {"success": False, "message": message}
    return {
        "success": True,
        "message": message,
        "job_id": job["job_id"],
        "status": job["status"],
        "queue_position": job["queue_position"],
    }


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
) -> bool:
    plugin = get_plugin_instance()
    stream_id = kwargs.get("stream_id", "")
    generation_request = _parse_generation_request(description, is_action)
    policy = generation_request.policy
    is_selfie = generation_request.is_selfie
    random_fixed_constraints = (
        _get_random_fixed_constraints(generation_request)
        if policy == "random_content" else ""
    )
    request_for_history = (
        random_fixed_constraints or generation_request.constraints or description
    )
    description = generation_request.request_text

    info = plugin._extract_session_info(kwargs)
    nsfw_enabled = plugin._session_state.is_nsfw_filter_enabled(
        info["platform"], info["chat_id"], plugin._get_config_callable(),
        stream_id=stream_id,
    )

    if policy == "random_content":
        description = _build_random_request_text(random_fixed_constraints)

    # Provider 能力预检查，避免在不支持参考模式的模型上浪费一次 LLM/API 调用。
    if ref_mode:
        supported, reason = _check_ref_mode_capability(
            plugin._get_model_config_from_kwargs(kwargs), ref_mode,
        )
        if not supported:
            await plugin.ctx.send.text(reason or "当前模型不支持该参考模式", stream_id)
            return False

    selfie_scene_context = ""
    if (
        is_selfie
        and plugin.config.prompt_generator.scene_llm_enabled
        and _should_read_selfie_schedule(policy)
        and _selfie_context_needed(description, policy)
    ):
        selfie_scene_context = await _build_selfie_scene_context(
            policy, description,
        )

    generated = await _generate_prompt_with_llm(
        description, stream_id, is_action, nsfw_enabled,
        selfie_scene_context=selfie_scene_context,
        ref_mode=ref_mode,
        policy=policy,
        is_selfie=is_selfie,
    )
    if not generated or not generated.flat_prompt:
        await plugin.ctx.send.text("提示词生成失败，请稍后再试~", stream_id)
        return False

    generated_prompt = generated.flat_prompt
    structured_prompt = generated.structured_prompt

    plugin.ctx.logger.debug("[LLM生图] 原始提示词: %s", generated_prompt)

    # 自拍处理（is_selfie 已在随机模式前以原始用户输入判定）
    from ..core.prompt_engine import normalize_prompt_order
    from ..core.selfie_engine import detect_selfie_from_output

    # LLM 已明确输出自拍意图或自拍标签时，纠正仅靠关键词前缀可能漏掉的场景。
    if not is_selfie and (
        (structured_prompt is not None and structured_prompt.intent == "selfie")
        or detect_selfie_from_output(generated_prompt)
    ):
        is_selfie = True

    selfie_base_prompt = generated_prompt
    selfie_base_structured = structured_prompt
    if is_selfie:
        model_cfg = plugin._get_model_config_from_kwargs(
            kwargs,
            apply_artist_preset=ref_mode not in ("style", "character&style"),
        )

        # 尝试使用自拍参考图（仅在没有手动上传参考图时生效）
        selfie_ref_filename = (plugin.config.prompt_show.selfie_ref_image or "").strip()
        selfie_ref_used = False
        if selfie_ref_filename and not ref_image:
            ref_path = _resolve_safe_selfie_ref(selfie_ref_filename)
            if ref_path is not None:
                provider_fmt = model_cfg.get("format", "bestnai")
                caps = get_capabilities(provider_fmt)
                if caps and ImageFeature.CHARACTER_REF in caps.features:
                    loaded_ref = load_image_file_as_base64(ref_path)
                    if loaded_ref:
                        ref_image = loaded_ref
                        ref_mode = "character"
                        selfie_ref_used = True
                        plugin.ctx.logger.info(
                            "[自拍参考图] 使用固定角色参考图: %s", selfie_ref_filename
                        )
                    else:
                        plugin.ctx.logger.warning(
                            "[自拍参考图] 图片无效或超过统一大小/像素限制: %s",
                            selfie_ref_filename,
                        )
                else:
                    plugin.ctx.logger.info(
                        "[自拍参考图] 当前 provider(%s) 不支持角色参考，回退文字提示词", provider_fmt
                    )
            else:
                plugin.ctx.logger.warning(
                    "[自拍参考图] 参考图路径无效、不存在或扩展名不受支持: %s",
                    selfie_ref_filename,
                )

        include_selfie_add = selfie_ref_used or not bool(ref_image)
        generated_prompt, structured_prompt = _process_selfie_prompt_result(
            generated_prompt,
            structured_prompt,
            request_for_history,
            include_selfie_add,
        )

    if ref_mode in {"character", "character&style"}:
        generated_prompt, structured_prompt = _process_character_reference_prompt_result(
            generated_prompt, structured_prompt,
        )

    if plugin.config.prompt_generator.enforce_tag_order:
        if structured_prompt is not None:
            structured_prompt = _normalize_structured_prompt_order(structured_prompt)
            generated_prompt = _render_structured_prompt_flat(structured_prompt)
        else:
            generated_prompt = normalize_prompt_order(generated_prompt)

    plugin.ctx.logger.info("[LLM生图] 最终提示词: %s", generated_prompt)

    # NSFW 开启时由 SFW 提示词模板（SFW_PROMPT_GENERATOR_*）从源头约束 LLM 产出，
    # 此处不再做产出后的黑名单二次拦截：避免 LLM 已规避、却因个别软色情词被拦下不发图。

    # 提示词显示
    if _is_prompt_show_enabled_from_kwargs(kwargs):
        show_prompt = generated_prompt
        header = "\U0001f4dd 提示词:"
        if is_selfie and plugin.config.prompt_show.hide_selfie_prompt_add:
            show_prompt, _ = _process_selfie_prompt_result(
                selfie_base_prompt,
                selfie_base_structured,
                request_for_history,
                False,
            )
            header = "\U0001f4dd 提示词(已隐藏自拍补充):"
        await plugin.ctx.send.text(f"{header}\n{show_prompt}", stream_id)

    # 生成并发送图片
    apply_artist_preset = ref_mode not in ("style", "character&style")
    model_config = plugin._get_model_config_from_kwargs(
        kwargs, apply_artist_preset=apply_artist_preset,
    )
    if not model_config or not model_config.get("base_url"):
        await plugin.ctx.send.text("当前生图模型配置错误，请检查配置文件", stream_id)
        return False

    from ..core.generator import generate_and_send
    sent = await generate_and_send(generated_prompt, model_config, stream_id,
                                   prompt_text=generated_prompt, size=size, kwargs=kwargs,
                                   ref_image=ref_image, ref_mode=ref_mode,
                                   structured_prompt=structured_prompt)
    if sent:
        plugin._session_state.set_last_draw_context(
            stream_id, generated_prompt, request_for_history,
            nsfw_enabled=nsfw_enabled,
        )
        if is_selfie:
            plugin._session_state.set_last_selfie_context(
                stream_id, generated_prompt, request_for_history,
                scene_summary=selfie_scene_context,
                nsfw_enabled=nsfw_enabled,
            )
    return bool(sent)


# ================================================================
# 内部辅助函数
# ================================================================

def _check_ref_mode_capability(model_config: dict, ref_mode: str) -> tuple:
    """在调用 LLM/生图 API 前确认当前 Provider 声明支持对应参考能力。"""
    if not ref_mode:
        return True, None
    fmt = str((model_config or {}).get("format", "bestnai") or "bestnai").strip().lower()
    caps = get_capabilities(fmt)
    if caps is None:
        return False, f"当前服务商 {fmt} 未声明参考图能力"
    required = {
        "i2i": ImageFeature.IMG2IMG,
        "character": ImageFeature.CHARACTER_REF,
        "style": ImageFeature.STYLE_REF,
        "character&style": ImageFeature.CHARACTER_STYLE_REF,
    }.get(ref_mode)
    if required is None:
        return False, f"未知参考模式: {ref_mode}"
    if required not in caps.features:
        return False, f"当前服务商 {caps.display_name} 不支持该参考模式"
    return True, None


def _resolve_safe_selfie_ref(filename: str) -> Optional[Path]:
    """只解析 selfie_refs 目录内的候选图片；内容校验由共享读取器负责。"""
    if not filename or Path(filename).name != filename:
        return None
    root = (Path(__file__).resolve().parent.parent / "selfie_refs").resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return None
    if not candidate.is_file():
        return None
    return candidate


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
    """检查 prompt 中是否包含 NSFW 标签，返回命中的标签列表。"""
    prompt_lower = prompt.lower()
    found = []
    for tag in _NSFW_BLACKLIST:
        # 使用词边界匹配，避免误杀（如 "ass" 不杀 "grass"）
        if re.search(r'\b' + re.escape(tag) + r'\b', prompt_lower):
            found.append(tag)
    return found


def _is_real_ad_command(kwargs: dict) -> bool:
    """判断 /ad(0) 是否为用户真实指令，而非藏在长文本/引用内容里的误匹配。

    可靠依据：适配器把消息拆成结构化段（reply/at/text 分开）。真实指令的
    "/ad ..." 是某个 text 段的开头（其前允许有媒体占位符 [image]/[图片] 或 @某人，
    这些是同段渲染的正常前缀，如私聊"引用图片 + /ad r"）；而引用回放/历史消息里嵌的
    /ad 前面是实义文字（如"某人说：/ad ..."），才是误触发，应拦截。

    命令正则用 [\\s\\S]* 宽松前缀从任意位置抠 /ad，会把后者误判成指令，
    故用段结构做权威校验。拿不到结构化段时（老适配器）回退为放行，不误伤。
    """
    message = kwargs.get("message", {})
    if not isinstance(message, dict) or not message:
        return True  # 无 message 上下文，无法判定，放行（保持旧行为）
    raw = message.get("raw_message", message.get("message"))

    def _startswith_cmd(text: str) -> bool:
        # 去掉开头的媒体占位符 / @某人（同段正常前缀），再判断是否以 /ad 开头
        t = _LEADING_NOISE_RE.sub("", str(text).lstrip())
        t = t.lstrip()
        return t.startswith(("/ad ", "/ad0 ", "/ad\t", "/ad0\t")) or t.rstrip() in ("/ad", "/ad0")

    if isinstance(raw, str):
        return _startswith_cmd(raw)
    if not isinstance(raw, list) or not raw:
        return True  # 无结构化段，放行
    for seg in raw:
        text = ""
        if isinstance(seg, dict):
            if seg.get("type") not in ("text", None):
                continue
            data = seg.get("data", "")
            text = data if isinstance(data, str) else (data.get("text", "") if isinstance(data, dict) else "")
        elif isinstance(seg, str):
            text = seg
        else:
            continue
        if _startswith_cmd(text):
            return True
    return False


def _is_bot_self_message(kwargs: dict) -> bool:
    """判断触发命令的消息是否为 bot 自己发的。

    bot 自己发的提示文案里可能含 /ad（如"请使用 /ad nsfw off 关闭过滤"），
    这些不应被当成用户指令。与 _is_real_ad_command 一起构成双重防误触发。
    """
    message = kwargs.get("message", {})
    if not isinstance(message, dict) or not message:
        return False
    if message.get("self") is True:
        return True
    info = message.get("message_info", {}) or {}
    user_info = info.get("user_info") or {}
    sender_id = str(user_info.get("user_id", "") or "")
    self_id = str(message.get("self_id", "") or "")
    if sender_id and self_id and sender_id == self_id:
        return True
    # 兜底：与已缓存的 bot QQ 号比对（SnowLuma 等会从历史消息剥掉 self_id）
    try:
        from ..core.generator import _cached_bot_self_id
        if _cached_bot_self_id and sender_id and sender_id == str(_cached_bot_self_id):
            return True
    except Exception:
        pass
    return False


def _check_plugin_enabled(kwargs: dict) -> tuple:
    """检查当前会话插件是否开启。返回 (ok, error_message)。"""
    plugin = get_plugin_instance()
    # 防误触发（err=None → 调用方静默跳过，不向群里发提示）：
    # ① /ad 藏在长文本/引用内容里而非段首 → 非真实指令
    # ② bot 自己发的含 /ad 文案回流
    if not _is_real_ad_command(kwargs) or _is_bot_self_message(kwargs):
        return False, None
    if not plugin.config.plugin.enabled:
        return False, "插件已被全局关闭，请在配置中启用"
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
    processed, _ = _process_selfie_prompt_result(
        description, None, raw_request, include_selfie_add,
    )
    return processed


def _process_selfie_prompt_result(
    flat_prompt: str,
    structured_prompt: Optional[StructuredPrompt],
    raw_request: str,
    include_selfie_add: bool,
) -> Tuple[str, Optional[StructuredPrompt]]:
    plugin = get_plugin_instance()
    from ..core.selfie_engine import merge_selfie_prompt
    from ..core.prompt_engine import (
        remove_selfie_appearance_tags,
        user_mentions_appearance,
    )

    selfie_add = (plugin.config.prompt_show.selfie_prompt_add or "") if plugin else ""
    policy = (plugin.config.prompt_generator.selfie_appearance_policy or "auto").strip().lower()
    user_specified = user_mentions_appearance(raw_request)

    if structured_prompt is None:
        description = flat_prompt
        if policy == "auto" and not user_specified:
            description = remove_selfie_appearance_tags(description)
        if include_selfie_add and selfie_add:
            description = merge_selfie_prompt(description, selfie_add)
        if policy == "never" and not user_specified:
            description = remove_selfie_appearance_tags(description)
        return description, None

    people = structured_prompt.people
    if not people:
        description = flat_prompt
        if policy == "auto" and not user_specified:
            description = remove_selfie_appearance_tags(description)
        if include_selfie_add and selfie_add:
            description = merge_selfie_prompt(description, selfie_add)
        if policy == "never" and not user_specified:
            description = remove_selfie_appearance_tags(description)
        return description, None

    global_tags = structured_prompt.global_tags
    should_remove_appearance = (
        policy == "never"
        or (policy == "auto" and not user_specified)
    )
    if should_remove_appearance:
        global_tags = tuple(
            tag for tag in global_tags if not _is_appearance_prompt_tag(tag)
        )

    processed_people = []
    for index, person in enumerate(people):
        positive_source = person.positive_tags
        if should_remove_appearance:
            positive_source = tuple(
                tag for tag in positive_source if not _is_appearance_prompt_tag(tag)
            )
        person_text = ", ".join(positive_source)
        if index == 0 and include_selfie_add and selfie_add:
            person_text = merge_selfie_prompt(person_text, selfie_add)
        positive_tags = _split_prompt_tags(person_text)
        negative_tags = tuple(
            tag for tag in person.negative_tags
            if not should_remove_appearance or not _is_appearance_prompt_tag(tag)
        )
        positive_keys = {_normalize_prompt_tag(tag) for tag in positive_tags}
        negative_tags = tuple(
            tag for tag in negative_tags
            if _normalize_prompt_tag(tag) not in positive_keys
        )
        processed_people.append(PersonPrompt(
            positive_tags=positive_tags,
            negative_tags=negative_tags,
        ))
    people = tuple(processed_people)

    processed = StructuredPrompt(
        global_tags=global_tags,
        people=people,
        format=structured_prompt.format,
        intent=structured_prompt.intent,
        continuity=structured_prompt.continuity,
    )
    return _render_structured_prompt_flat(processed), processed


def _split_prompt_tags(prompt: str) -> Tuple[str, ...]:
    return tuple(tag.strip() for tag in (prompt or "").split(",") if tag.strip())


def _normalize_prompt_tag(tag: str) -> str:
    value = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", str(tag or "").strip())
    value = re.sub(r"::\s*$", "", value).strip("{}[]() ")
    return re.sub(r"\s+", " ", value.lower()).strip()


def _is_appearance_prompt_tag(tag: str) -> bool:
    from ..core.prompt_engine import remove_selfie_appearance_tags

    value = _normalize_prompt_tag(tag)
    return bool(value) and not remove_selfie_appearance_tags(value).strip()


_REFERENCE_HAIR_TRAIT_RE = re.compile(
    r"(?:\b(?:black|blonde|brown|blue|pink|white|silver|red|green|purple|"
    r"orange|gray|grey|aqua|cyan|multicolored|two[- ]tone|gradient) hair\b|"
    r"\b(?:very )?(?:long|short|medium) hair\b|\b(?:straight|wavy|curly|"
    r"messy|spiked|fluffy|layered|silky|glossy|detailed) hair\b|"
    r"\b(?:twintails?|twin tails|ponytail|side ponytail|pigtails?|braids?|"
    r"side braid|hair bun|double bun|bob cut|hime cut|ahoge|bangs|blunt bangs|"
    r"hair over shoulders|hair over one eye)\b)",
    re.IGNORECASE,
)
_REFERENCE_EYE_TRAIT_RE = re.compile(
    r"(?:\b(?:black|brown|blue|red|green|purple|orange|gray|grey|golden|"
    r"yellow|pink|aqua|cyan|amber|multicolored) eyes?\b|\b[a-z]+-eyed\b|"
    r"\b(?:heterochromia|detailed eyes|shiny eyes|large eyes|narrow eyes|"
    r"long eyelashes|eyelashes)\b)",
    re.IGNORECASE,
)
_REFERENCE_FACE_TRAIT_RE = re.compile(
    r"(?:\b(?:delicate|detailed|sharp|soft|round|oval|long|small) facial features\b|"
    r"\b(?:round|oval|long|heart-shaped|square) face\b|\b(?:small|large|button|"
    r"pointed) nose\b|\b(?:thin|thick|bushy) eyebrows\b|\b(?:freckles|mole|"
    r"beauty mark)\b)",
    re.IGNORECASE,
)
_REFERENCE_SKIN_TRAIT_RE = re.compile(
    r"\b(?:pale|fair|light|dark|brown|tan|tanned|olive|white|black|blue|green|"
    r"red|purple|gray|grey) skin\b",
    re.IGNORECASE,
)
_REFERENCE_FIXED_MARK_RE = re.compile(
    r"\b(?:facial tattoo|face tattoo|tattoo|birthmark|facial scar|face scar|"
    r"body scar|piercing|navel piercing|lip piercing|nose piercing)\b",
    re.IGNORECASE,
)
_REFERENCE_BODY_TRAITS = {
    "petite", "slim", "slender", "skinny", "curvy", "chubby", "plump",
    "muscular", "athletic", "tall", "short", "shortstack",
    "flat chest", "small breasts", "medium breasts", "large breasts",
    "huge breasts", "gigantic breasts", "compact breasts", "rounded breasts",
    "natural breasts", "perky breasts", "sagging breasts", "high bust",
}
_REFERENCE_SPECIES_TRAITS = {
    "elf", "dark elf", "pointy ears", "animal ears", "cat ears", "dog ears",
    "fox ears", "bunny ears", "wolf ears", "cat girl", "dog girl", "fox girl",
    "demon girl", "angel", "oni", "kemonomimi", "tail", "wings", "horns",
}


def _normalize_reference_trait_tag(tag: str) -> str:
    value = str(tag or "").strip()
    value = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", value).strip()
    value = re.sub(r"::\s*$", "", value).strip()
    while len(value) >= 2 and (
        (value[0] == "{" and value[-1] == "}")
        or (value[0] == "[" and value[-1] == "]")
    ):
        value = value[1:-1].strip()
    return re.sub(r"\s+", " ", value.lower()).strip()


def _is_character_reference_trait(tag: str) -> bool:
    value = _normalize_reference_trait_tag(tag)
    if not value:
        return False
    if re.search(r"\([^)]+\)(?:\s*\(cosplay\))?$", value) or "cosplay" in value:
        return True
    if _REFERENCE_HAIR_TRAIT_RE.search(value) or _REFERENCE_EYE_TRAIT_RE.search(value):
        return True
    if _REFERENCE_FACE_TRAIT_RE.search(value) or _REFERENCE_SKIN_TRAIT_RE.search(value):
        return True
    if _REFERENCE_FIXED_MARK_RE.search(value):
        return True
    if value in _REFERENCE_BODY_TRAITS or value in _REFERENCE_SPECIES_TRAITS:
        return True
    if re.fullmatch(r"[a-hj-z](?:\.|-)?\s*cup", value):
        return True
    return False


def _reference_person_fallback(global_tags: Tuple[str, ...]) -> str:
    normalized = {_normalize_reference_trait_tag(tag) for tag in global_tags}
    has_girl = any("girl" in tag for tag in normalized)
    has_boy = any("boy" in tag for tag in normalized)
    if has_girl and not has_boy:
        return "girl"
    if has_boy and not has_girl:
        return "boy"
    return "person"


def _filter_character_reference_flat_prompt(prompt: str) -> str:
    lines = []
    for line in str(prompt or "").splitlines():
        stripped = line.strip()
        prefix = ""
        match = re.match(r"^(char\d+:)\s*", stripped, re.IGNORECASE)
        if match:
            prefix = match.group(1)
            stripped = stripped[match.end():]
        trailing_comma = stripped.endswith(",")
        tags = [tag.strip() for tag in stripped.strip(",").split(",") if tag.strip()]
        filtered = [tag for tag in tags if not _is_character_reference_trait(tag)]
        if not filtered:
            if prefix:
                filtered = ["person"]
            else:
                continue
        rendered = ", ".join(filtered)
        if prefix:
            rendered = f"{prefix}{rendered}"
        if trailing_comma:
            rendered += ","
        lines.append(rendered)
    return "\n".join(lines).strip() or "person"


def _process_character_reference_prompt_result(
    flat_prompt: str,
    structured_prompt: Optional[StructuredPrompt],
) -> Tuple[str, Optional[StructuredPrompt]]:
    if structured_prompt is None:
        return _filter_character_reference_flat_prompt(flat_prompt), None

    global_tags = tuple(
        tag for tag in structured_prompt.global_tags
        if not _is_character_reference_trait(tag)
    )
    fallback = _reference_person_fallback(global_tags)
    people = []
    for person in structured_prompt.people:
        positive_tags = tuple(
            tag for tag in person.positive_tags
            if not _is_character_reference_trait(tag)
        ) or (fallback,)
        negative_tags = tuple(
            tag for tag in person.negative_tags
            if not _is_character_reference_trait(tag)
        )
        people.append(PersonPrompt(
            positive_tags=positive_tags,
            negative_tags=negative_tags,
        ))
    processed = StructuredPrompt(
        global_tags=global_tags,
        people=tuple(people),
        format=structured_prompt.format,
        intent=structured_prompt.intent,
        continuity=structured_prompt.continuity,
    )
    return _render_structured_prompt_flat(processed), processed


def _render_structured_prompt_flat(structured_prompt: StructuredPrompt) -> str:
    from ..core.prompt_engine import render_structured_prompt_flat

    return render_structured_prompt_flat(
        structured_prompt.global_tags, structured_prompt.people,
    )


def _normalize_structured_prompt_order(
    structured_prompt: StructuredPrompt,
) -> StructuredPrompt:
    from ..core.prompt_engine import normalize_prompt_order

    global_tags = _split_prompt_tags(normalize_prompt_order(
        ", ".join(structured_prompt.global_tags),
    ))
    people = tuple(
        PersonPrompt(
            positive_tags=_split_prompt_tags(normalize_prompt_order(
                ", ".join(person.positive_tags),
            )),
            negative_tags=person.negative_tags,
        )
        for person in structured_prompt.people
    )
    return StructuredPrompt(
        global_tags=global_tags,
        people=people,
        format=structured_prompt.format,
        intent=structured_prompt.intent,
        continuity=structured_prompt.continuity,
    )


def _resolve_prompt_max_tokens(
    configured: int, output_format: str, model_name: str,
) -> int:
    """Use the configured output budget without model-specific inflation."""
    return max(1, int(configured))


async def _generate_prompt_with_llm(
    request_text: str, stream_id: str = "",
    is_action: bool = False, nsfw_enabled: bool = False,
    selfie_scene_context: str = "",
    ref_mode: str = "",
    policy: str = "minimal",
    is_selfie: bool = False,
) -> Optional[GeneratedPrompt]:
    plugin = get_plugin_instance()
    gen_cfg = plugin.config.prompt_generator

    if not request_text.strip():
        return None

    # 加载模板
    from ..core.rules.prompt_rules import (
        CONTENT_POLICY_TEXTS,
        GENERATION_POLICY_TEXTS,
        build_prompt_generator_template,
    )

    output_format = (gen_cfg.output_format or "json").strip().lower()
    template = gen_cfg.prompt_template or build_prompt_generator_template(
        sfw_enabled=nsfw_enabled,
        output_format=output_format,
    )
    content_policy_text = (
        CONTENT_POLICY_TEXTS["sfw" if nsfw_enabled else "allow_nsfw"]
        if gen_cfg.prompt_template else ""
    )
    policy_text = GENERATION_POLICY_TEXTS.get(
        policy, GENERATION_POLICY_TEXTS["minimal"],
    )
    previous_prompt = ""
    previous_request = ""
    if stream_id and _should_inherit_previous_context(request_text):
        previous_prompt, previous_request = plugin._session_state.get_last_draw_context(
            stream_id, ttl=gen_cfg.inherit_ttl, nsfw_enabled=nsfw_enabled,
        )

    prompt = _render_generator_prompt(
        template, request_text, is_action=is_action,
        selfie_scene_context=selfie_scene_context,
        previous_prompt=previous_prompt or "",
        previous_request=previous_request or "",
        ref_mode=ref_mode,
        policy=policy,
        policy_text=policy_text,
        is_selfie=is_selfie,
        content_policy_text=content_policy_text,
    )

    # LLM 调用
    from ..core.prompt_engine import (
        call_custom_llm_api,
        has_custom_api_config,
        parse_generated_prompt,
    )

    llm_config = {
        "api_base": gen_cfg.api_base, "api_key": gen_cfg.api_key, "model_name": gen_cfg.model_name,
    }
    generation_temperature = (
        max(float(gen_cfg.temperature), 1.0)
        if policy == "random_content" else gen_cfg.temperature
    )
    effective_max_tokens = _resolve_prompt_max_tokens(
        gen_cfg.max_tokens, output_format, gen_cfg.model_name,
    )
    if has_custom_api_config(llm_config):
        success, response, _, _ = await call_custom_llm_api(
            prompt=prompt, api_base=gen_cfg.api_base, api_key=gen_cfg.api_key,
            model=gen_cfg.model_name, temperature=generation_temperature,
            max_tokens=effective_max_tokens,
            structured_output=output_format == "json",
        )
    else:
        try:
            result = await plugin.ctx.llm.generate(
                prompt=prompt, temperature=generation_temperature,
                max_tokens=effective_max_tokens,
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

    generated = parse_generated_prompt(response)
    if generated.structured_prompt is not None:
        parsed = generated.structured_prompt
        resolved_format = "multi" if len(parsed.people) > 1 else "single"
        resolved_intent = "selfie" if is_selfie else "normal"
        resolved_continuity = "adjust" if previous_prompt else "new"
        if (
            parsed.format != resolved_format
            or parsed.intent != resolved_intent
            or parsed.continuity != resolved_continuity
        ):
            generated = GeneratedPrompt(
                flat_prompt=generated.flat_prompt,
                structured_prompt=StructuredPrompt(
                    global_tags=parsed.global_tags,
                    people=parsed.people,
                    format=resolved_format,
                    intent=resolved_intent,
                    continuity=resolved_continuity,
                ),
            )
    if output_format == "json" and generated.structured_prompt is None:
        plugin.ctx.logger.error("[LLM] JSON 输出不符合 V4 结构，已终止本次生图")
        return None
    return generated


def _render_generator_prompt(
    template: str,
    request: str,
    is_action: bool = False,
    selfie_scene_context: str = "",
    previous_prompt: str = "",
    previous_request: str = "",
    ref_mode: str = "",
    policy: str = "minimal",
    policy_text: str = "",
    is_selfie: bool = False,
    content_policy_text: str = "",
) -> str:
    plugin = get_plugin_instance()
    from ..core.selfie_engine import get_selfie_hint
    from ..core.prompt_engine import build_current_time_context

    custom_sys = plugin.config.custom_prompt.system_prompt or ""
    if custom_sys:
        custom_sys = custom_sys.strip() + "\n\n"

    selfie_hint = get_selfie_hint() if is_selfie else ""
    current_time = (
        build_current_time_context() if policy == "tool_legacy" else ""
    )

    prompt = template.replace("<<CUSTOM_SYSTEM_PROMPT>>", custom_sys)
    if "<<CONTENT_POLICY>>" in prompt:
        prompt = prompt.replace("<<CONTENT_POLICY>>", content_policy_text)
    elif content_policy_text:
        prompt = f"{prompt.rstrip()}\n\n{content_policy_text}"
    if "<<GENERATION_POLICY>>" in prompt:
        prompt = prompt.replace("<<GENERATION_POLICY>>", policy_text)
    elif policy_text:
        prompt = f"{prompt.rstrip()}\n\n{policy_text}"
    previous_context = ""
    if previous_prompt:
        previous_context = (
            "<previous_draw_context>\n"
            f"上一轮用户请求：{previous_request or '(未记录)'}\n"
            f"上一轮最终提示词：{previous_prompt}\n"
            "仅当本轮是继续、修改、重画或保持上一张设定时继承；若是全新主题则忽略。\n"
            "</previous_draw_context>"
        )
    prompt = prompt.replace("<<PREVIOUS_PROMPT>>", previous_context)
    prompt = prompt.replace("<<SELFIE_SCENE_CONTEXT>>", selfie_scene_context or "")
    prompt = prompt.replace("<<CHARACTER_REF_CONTEXT>>", _build_ref_context(ref_mode))
    prompt = prompt.replace("<<USER_REQUEST>>", request.strip() or "N/A")
    prompt = prompt.replace("<<CURRENT_TIME_CONTEXT>>", current_time)
    prompt = prompt.replace("<<SELFIE_HINT>>", selfie_hint)
    prompt = prompt.replace("<<TAG_CANDIDATES>>", "")
    if policy in {"minimal", "random_content"}:
        prompt = (
            f"{prompt.rstrip()}\n\n"
            "<final_source_check>\n"
            f"原始用户条件：{request.strip()}\n"
            "输出前逐项核对原始条件中的人物、服装、动作、物品和场景；除 SFW 转换外，"
            "任何明确条件都不得遗漏或用相似概念替代。\n"
            "</final_source_check>"
        )
    return prompt.strip()


def _build_ref_context(ref_mode: str) -> str:
    mode = (ref_mode or "").strip().lower()
    if mode == "character":
        return (
            "<reference_image_rules priority=\"highest\">提供了角色参考图，参考图是人物身份和固定外貌的唯一来源。"
            "忽略公共模板中要求填写已知角色名或原创人物外貌的规则；禁止输出角色名、作品名、cosplay 身份，"
            "也禁止输出发色、发型、瞳色、脸型、肤色、种族特征、固定饰物、体型和胸部尺寸等人物固有特征。"
            "只描述人数、服装及穿着状态、临时身体状态或伤势、动作、姿势、表情、视线、道具、构图和场景。"
            "</reference_image_rules>"
        )
    if mode == "style":
        return (
            "<reference_image_rules>提供了画风参考图。不要添加画师名、作品风格或额外画风标签；"
            "画风完全交给参考图，内容、人物、动作和场景按用户要求生成。"
            "</reference_image_rules>"
        )
    if mode == "character&style":
        return (
            "<reference_image_rules priority=\"highest\">参考图同时是人物身份、固定外貌和画风的唯一来源。"
            "忽略公共模板中要求填写已知角色名或原创人物外貌的规则；禁止输出角色名、作品名、cosplay 身份，"
            "以及发色、发型、瞳色、脸型、肤色、种族特征、固定饰物、体型、胸部尺寸、画师名或额外风格标签。"
            "只描述人数、服装及穿着状态、临时身体状态或伤势、动作、姿势、表情、视线、道具、构图、场景和光线。"
            "</reference_image_rules>"
        )
    if mode == "i2i":
        return (
            "<reference_image_rules>提供了图生图参考。保留用户未要求修改的主体与构图，"
            "仅按本轮描述调整指定内容。"
            "</reference_image_rules>"
        )
    return ""
