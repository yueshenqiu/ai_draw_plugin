# -*- coding: utf-8 -*-
"""
统一会话状态管理器（从 core/services/session_state.py 迁移）。

集中管理所有会话级别的运行时状态：
- 管理员模式 / 模型选择 / 画师串选择 / 尺寸选择
- 自动撤回 / NSFW过滤 / 提示词显示
- 上一轮提示词上下文（Action 专用）

适配新的 [models.modelX] 配置结构。
"""

import logging
import time
from typing import Optional, Dict, List, Tuple, Callable, Any

_logger = logging.getLogger("ai_draw_plugin")


def inject_logger(logger):
    global _logger
    _logger = logger


class SessionStateManager:
    """单例模式的会话状态管理器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self._plugin_enabled: Dict[str, bool] = {}       # /ad on|off 插件总开关
        self._admin_mode: Dict[str, bool] = {}
        self._selected_models: Dict[str, str] = {}
        self._selected_artists: Dict[str, int] = {}
        self._selected_sizes: Dict[str, str] = {}
        self._recall_enabled: Dict[str, bool] = {}
        self._nsfw_filter: Dict[str, bool] = {}
        self._prompt_show: Dict[str, bool] = {}
        self._send_mode: Dict[str, str] = {}  # /ad send direct|forward 会话级发送方式
        self._last_draw_context: Dict[str, Tuple[str, str, float]] = {}
        self._last_selfie_context: Dict[str, Tuple[str, str, str, Dict[str, List[str]], float]] = {}
        self._loaded_models: Dict[str, dict] = {}  # 由 plugin.on_load 注入

    def set_loaded_models(self, models: Dict[str, dict]) -> None:
        """注入已加载的模型配置（由 plugin 在 on_load / on_config_update 时调用）。"""
        self._loaded_models = dict(models or {})

    @staticmethod
    def _make_key(platform: str, chat_id: str) -> str:
        return f"{platform}:{chat_id}"

    # ==================== 管理员模式 ====================

    def is_admin_mode_enabled(self, platform: str, chat_id: str, get_config: Callable) -> bool:
        key = self._make_key(platform, chat_id)
        if key in self._admin_mode:
            return self._admin_mode[key]
        return get_config("admin.default_admin_mode", False)

    def set_admin_mode(self, platform: str, chat_id: str, enabled: bool):
        key = self._make_key(platform, chat_id)
        self._admin_mode[key] = enabled
        _logger.info(f"[ai_draw] 会话 {key} 管理员模式已{'开启' if enabled else '关闭'}")

    def check_user_permission(self, platform: str, chat_id: str, user_id: str, get_config: Callable) -> bool:
        if not self.is_admin_mode_enabled(platform, chat_id, get_config):
            return True
        admin_users = get_config("admin.admin_users", [])
        # 空=禁止（fail-closed）：管理员模式开启但未配置管理员时，无人可用生图命令。
        if not admin_users:
            return False
        return str(user_id) in admin_users

    def is_admin_user(self, user_id: str, get_config: Callable) -> bool:
        admin_users = get_config("admin.admin_users", [])
        # 空=禁止（fail-closed）：未配置管理员时无人有权限，需改 config.toml 恢复。
        if not admin_users:
            return False
        return str(user_id) in admin_users

    # ==================== 插件总开关（/ad on|off）====================

    def is_plugin_enabled(self, platform: str, chat_id: str) -> bool:
        """检查当前会话是否启用了插件。默认开启。"""
        key = self._make_key(platform, chat_id)
        return self._plugin_enabled.get(key, True)

    def set_plugin_enabled(self, platform: str, chat_id: str, enabled: bool):
        key = self._make_key(platform, chat_id)
        self._plugin_enabled[key] = enabled
        _logger.info(f"[ai_draw] 会话 {key} 插件已{'开启' if enabled else '关闭'}")

    # ==================== 模型选择 ====================

    def get_selected_model(self, platform: str, chat_id: str) -> Optional[str]:
        key = self._make_key(platform, chat_id)
        return self._selected_models.get(key)

    def set_selected_model(self, platform: str, chat_id: str, model: str):
        key = self._make_key(platform, chat_id)
        self._selected_models[key] = model
        _logger.info(f"[ai_draw] 会话 {key} 已切换模型: {model}")

    # ==================== 画师串选择（适配新 [models.modelX] 结构）====================

    def get_selected_artist_index(self, platform: str, chat_id: str) -> int:
        key = self._make_key(platform, chat_id)
        return self._selected_artists.get(key, 1)

    def _resolve_model_artist_presets(self, model_id: str) -> Optional[List[Dict]]:
        """从已加载的模型配置中查找指定 model_id 的画师预设。

        优先级：模型内联 artist_presets > 全局 [artist_presets]。
        """
        all_models = self._loaded_models
        if not all_models:
            return None

        # 1. 模型内联 artist_presets（向后兼容）
        model_cfg = all_models.get(model_id)
        if isinstance(model_cfg, dict):
            inline = model_cfg.get("artist_presets", [])
            if inline:
                return _normalize_artist_presets(inline)

        # 2. 遍历按 model 字段匹配的内联 artist_presets
        for key, cfg in all_models.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("model") == model_id:
                inline = cfg.get("artist_presets", [])
                if inline:
                    return _normalize_artist_presets(inline)

        # 3. 全局 [artist_presets] 节
        global_presets = _load_global_artist_presets()
        if global_presets:
            return global_presets

        return None

    def get_effective_artist_index(
        self, platform: str, chat_id: str, model_id: str,
    ) -> int:
        """获取指定会话当前实际生效的画师串索引。"""
        presets_raw = self._resolve_model_artist_presets(model_id)
        artist_presets = self._parse_artist_presets(presets_raw or [])
        if not artist_presets:
            return 1

        key = self._make_key(platform, chat_id)
        if key in self._selected_artists:
            selected_index = self._selected_artists[key]
            return selected_index if 1 <= selected_index <= len(artist_presets) else 1

        # 回退到该模型的 artist_preset（新字段）或 default_artist_preset（旧字段）
        model_cfg = self._loaded_models.get(model_id, {}) or {}
        default_value = model_cfg.get("artist_preset") or model_cfg.get("default_artist_preset", "")
        return self._resolve_index_from_default(default_value, artist_presets)

    def set_selected_artist_index(self, platform: str, chat_id: str, index: int):
        key = self._make_key(platform, chat_id)
        self._selected_artists[key] = index
        _logger.info(f"[ai_draw] 会话 {key} 已切换画师串: #{index}")

    def get_selected_artist_preset_config(
        self, platform: str, chat_id: str, model_id: str,
    ) -> Optional[Dict[str, Any]]:
        """获取指定会话当前选中的画师预设完整配置。"""
        presets_raw = self._resolve_model_artist_presets(model_id)
        artist_presets = self._parse_artist_presets(presets_raw or [])
        if not artist_presets:
            return None

        key = self._make_key(platform, chat_id)
        if key in self._selected_artists:
            selected_index = self._selected_artists[key]
        else:
            model_cfg = self._loaded_models.get(model_id, {}) or {}
            default_value = model_cfg.get("artist_preset") or model_cfg.get("default_artist_preset", "")
            selected_index = self._resolve_index_from_default(default_value, artist_presets)

        if 1 <= selected_index <= len(artist_presets):
            return artist_presets[selected_index - 1]
        return artist_presets[0] if artist_presets else None

    @staticmethod
    def _resolve_index_from_default(default_value, artist_presets: List[Dict]) -> int:
        if default_value is None:
            return 1
        if isinstance(default_value, int):
            return default_value if 1 <= default_value <= len(artist_presets) else 1
        default_text = str(default_value).strip()
        if not default_text:
            return 1
        if default_text.isdigit():
            index = int(default_text)
            return index if 1 <= index <= len(artist_presets) else 1
        for index, preset in enumerate(artist_presets, 1):
            if preset.get("name", "").strip() == default_text:
                return index
        return 1

    @staticmethod
    def _parse_artist_presets(presets_raw: List) -> List[Dict[str, Any]]:
        if not presets_raw:
            return []
        result = []
        for i, preset in enumerate(presets_raw, 1):
            if isinstance(preset, dict):
                name = preset.get("name", f"画师串 {i}")
                prompt = preset.get("prompt", "")
                normalized: Dict[str, Any] = {"name": name, "prompt": prompt}
                neg = str(preset.get("negative_prompt_add", "") or "").strip()
                if neg:
                    normalized["negative_prompt_add"] = neg
                result.append(normalized)
            elif isinstance(preset, str):
                preview = preset[:30] + "..." if len(preset) > 30 else preset
                result.append({"name": f"#{i} {preview}", "prompt": preset})
        return result

    # ==================== 尺寸选择 ====================

    def get_selected_size(self, platform: str, chat_id: str) -> Optional[str]:
        key = self._make_key(platform, chat_id)
        return self._selected_sizes.get(key)

    def set_selected_size(self, platform: str, chat_id: str, size: str):
        key = self._make_key(platform, chat_id)
        self._selected_sizes[key] = size
        _logger.info(f"[ai_draw] 会话 {key} 已切换尺寸: {size}")

    # ==================== 自动撤回 ====================

    def is_recall_enabled(self, platform: str, chat_id: str, get_config: Callable) -> bool:
        key = self._make_key(platform, chat_id)
        if key in self._recall_enabled:
            return self._recall_enabled[key]
        return get_config("auto_recall.enabled", False)

    def set_recall_enabled(self, platform: str, chat_id: str, enabled: bool):
        key = self._make_key(platform, chat_id)
        self._recall_enabled[key] = enabled
        _logger.info(f"[ai_draw] 会话 {key} 自动撤回已{'开启' if enabled else '关闭'}")

    # ==================== NSFW 过滤 ====================

    def is_nsfw_filter_enabled(self, platform: str, chat_id: str, get_config: Callable,
                                stream_id: str = "") -> bool:
        key = self._make_key(platform, chat_id)
        if key in self._nsfw_filter:
            return self._nsfw_filter[key]
        if stream_id and stream_id in self._nsfw_filter:
            return self._nsfw_filter[stream_id]
        return get_config("nsfw_filter.enabled", False)

    def set_nsfw_filter_enabled(self, platform: str, chat_id: str, enabled: bool,
                                 stream_id: str = ""):
        key = self._make_key(platform, chat_id)
        self._nsfw_filter[key] = enabled
        if stream_id:
            self._nsfw_filter[stream_id] = enabled
        _logger.info(f"[ai_draw] 会话 {key} NSFW过滤已{'开启' if enabled else '关闭'}")

    # ==================== 发送方式 ====================

    def get_send_mode(self, platform: str, chat_id: str, get_config: Callable) -> str:
        """获取会话级发送方式：direct（普通直发）/ forward（合并转发）。"""
        key = self._make_key(platform, chat_id)
        if key in self._send_mode:
            return self._send_mode[key]
        return get_config("plugin.send_mode", "direct") or "direct"

    def set_send_mode(self, platform: str, chat_id: str, mode: str):
        key = self._make_key(platform, chat_id)
        self._send_mode[key] = mode
        _logger.info(f"[ai_draw] 会话 {key} 发送方式已设为 {mode}")

    # ==================== 提示词显示 ====================

    def is_prompt_show_enabled(self, platform: str, chat_id: str, get_config: Callable) -> bool:
        key = self._make_key(platform, chat_id)
        if key in self._prompt_show:
            return self._prompt_show[key]
        default_enabled = get_config("prompt_show.enabled", None)
        if default_enabled is not None:
            return bool(default_enabled)
        return bool(get_config("prompt_generator.show_prompt", False))

    def set_prompt_show_enabled(self, platform: str, chat_id: str, enabled: bool):
        key = self._make_key(platform, chat_id)
        self._prompt_show[key] = enabled
        _logger.info(f"[ai_draw] 会话 {key} 提示词显示已{'开启' if enabled else '关闭'}")

    # ==================== 调试/管理 ====================

    def get_session_state_summary(self, platform: str, chat_id: str) -> Dict[str, Any]:
        key = self._make_key(platform, chat_id)
        return {
            "key": key,
            "admin_mode": self._admin_mode.get(key),
            "model": self._selected_models.get(key),
            "artist_index": self._selected_artists.get(key),
            "size": self._selected_sizes.get(key),
            "recall": self._recall_enabled.get(key),
            "nsfw_filter": self._nsfw_filter.get(key),
            "prompt_show": self._prompt_show.get(key),
        }

    def clear_session_state(self, platform: str, chat_id: str):
        key = self._make_key(platform, chat_id)
        for d in (self._admin_mode, self._selected_models, self._selected_artists,
                  self._selected_sizes, self._recall_enabled, self._nsfw_filter, self._prompt_show):
            d.pop(key, None)
        _logger.info(f"[ai_draw] 会话 {key} 状态已清除")

    # ==================== 上一轮提示词上下文（Action 专用）====================

    def get_last_draw_context(self, chat_stream_id: str, ttl: float = 0) -> Tuple[Optional[str], Optional[str]]:
        if not chat_stream_id:
            return None, None
        entry = self._last_draw_context.get(chat_stream_id)
        if entry is None:
            return None, None
        prompt, request, ts = entry
        if ttl > 0 and (time.time() - ts) > ttl:
            self._last_draw_context.pop(chat_stream_id, None)
            return None, None
        return prompt, request or None

    def set_last_draw_context(self, chat_stream_id: str, prompt: str, request: str = ""):
        if not chat_stream_id or not isinstance(prompt, str) or not prompt.strip():
            return
        self._last_draw_context[chat_stream_id] = (prompt.strip(), (request or "").strip(), time.time())

    def get_last_draw_prompt(self, chat_stream_id: str) -> Optional[str]:
        prompt, _ = self.get_last_draw_context(chat_stream_id)
        return prompt

    def set_last_draw_prompt(self, chat_stream_id: str, prompt: str):
        self.set_last_draw_context(chat_stream_id, prompt)

    # ==================== 上一轮自拍场景 ====================

    def get_last_selfie_context(
        self, chat_stream_id: str, ttl: float = 0
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Dict[str, List[str]]]:
        if not chat_stream_id:
            return None, None, None, {}
        entry = self._last_selfie_context.get(chat_stream_id)
        if entry is None:
            return None, None, None, {}
        prompt, request, scene_summary, anchor_data, ts = entry
        if ttl > 0 and (time.time() - ts) > ttl:
            self._last_selfie_context.pop(chat_stream_id, None)
            return None, None, None, {}
        return prompt or None, request or None, scene_summary or None, dict(anchor_data or {})

    def set_last_selfie_context(
        self, chat_stream_id: str, prompt: str, request: str = "",
        scene_summary: str = "", anchor_data: Optional[Dict[str, List[str]]] = None,
    ):
        if not chat_stream_id:
            return
        prompt_text = (prompt or "").strip()
        scene_text = (scene_summary or "").strip()
        normalized_anchor = dict(anchor_data or {})
        if not prompt_text and not scene_text and not normalized_anchor:
            return
        self._last_selfie_context[chat_stream_id] = (
            prompt_text, (request or "").strip(), scene_text,
            normalized_anchor, time.time(),
        )


def _normalize_artist_presets(presets) -> List[Dict]:
    """归一化画师预设：支持 dict 格式 {name: prompt} 和 list 格式 [{name, prompt}]。"""
    if isinstance(presets, dict):
        return [{"name": k, "prompt": v} for k, v in presets.items() if isinstance(v, str)]
    if isinstance(presets, list):
        return presets
    return []


_global_presets_cache: Optional[List[Dict]] = None


def clear_global_presets_cache() -> None:
    """清除全局画师预设缓存，供配置热重载调用（否则换了 config.toml 仍读旧缓存）。"""
    global _global_presets_cache
    _global_presets_cache = None


def _load_global_artist_presets() -> Optional[List[Dict]]:
    """从 config.toml 的 [artist_presets] 节加载全局画师预设（缓存）。"""
    global _global_presets_cache
    if _global_presets_cache is not None:
        return _global_presets_cache if _global_presets_cache else None

    try:
        import tomllib as _toml
        from pathlib import Path as _Path
        config_path = _Path(__file__).parent.parent / "config.toml"
        if not config_path.exists():
            _global_presets_cache = []
            return None
        with open(config_path, "rb") as f:
            data = _toml.load(f)
        raw = data.get("artist_presets", {})
        # 新结构 {"presets": [{name, prompt}]} 取内层列表；旧扁平字典 {名称: 提示词} 直接用
        if isinstance(raw, dict) and "presets" in raw:
            raw = raw.get("presets") or []
        if raw:
            normalized = _normalize_artist_presets(raw)
            _global_presets_cache = normalized
            return normalized if normalized else None
        _global_presets_cache = []
        return None
    except Exception:
        _global_presets_cache = []
        return None


# 全局单例
session_state = SessionStateManager()
