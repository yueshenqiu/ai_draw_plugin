# -*- coding: utf-8 -*-
"""AI Draw 图片生成插件 — MaiBot SDK 2.0（重构版）

命令前缀统一为 /ad（AI Draw）。
模型配置采用 provider-agnostic 多模型自包含结构 [models.modelX]。
文件结构参照 video_generator_plugin 分类整理。
"""

import asyncio
from typing import Any, ClassVar, Dict, List, Optional

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .constants.constants import MODEL_MAPPINGS, SIZE_MAPPINGS, BESTNAI_MODEL_IDS
from .instance import set_plugin_instance, clear_plugin_instance
from .core.generator import load_models_config
from .core.session_state import session_state


# ================================================================
# Config Models（适配新 [models.modelX] 结构）
# ================================================================

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件基本配置"
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.0.0", description="配置版本号")
    napcat_http_url: str = Field(default="http://127.0.0.1:5780", description="NapCat HTTP API 地址（直连，绕过 IPC）")
    napcat_http_token: str = Field(default="", description="NapCat HTTP API Token")
    send_mode: str = Field(default="direct", description="图片发送方式：direct（普通直发，快）/ forward（合并转发，隐蔽但慢）")
    force_forward_when_nsfw_off: bool = Field(default=True, description="NSFW 过滤关闭时强制用合并转发（更隐蔽）")


class ModelsSectionConfig(PluginConfigBase):
    __ui_label__ = "多模型配置"
    default_model: str = Field(default="model1", description="默认使用的模型 ID")


class AutoRecallSection(PluginConfigBase):
    __ui_label__ = "自动撤回配置"
    enabled: bool = Field(default=True, description="是否默认启用自动撤回")
    delay_seconds: int = Field(default=30, description="撤回延迟时间（秒）")
    allowed_groups: list = Field(default_factory=list, description="允许自动撤回的会话白名单")


class AdminSection(PluginConfigBase):
    __ui_label__ = "管理员权限配置"
    admin_users: list = Field(default_factory=list, description="管理员用户ID列表")
    default_admin_mode: bool = Field(default=False, description="是否默认启用管理员模式")


class PromptShowSection(PluginConfigBase):
    __ui_label__ = "提示词显示配置"
    enabled: bool = Field(default=False, description="是否默认启用提示词显示")
    hide_selfie_prompt_add: bool = Field(default=False, description="显示时是否隐藏自拍补充提示词")
    selfie_prompt_add: str = Field(
        default="girl,long brown hair with a star-shaped hair accessory, warm brown eyes, blue and white princess dress with a cutout at the midriff, gold trim, purple gem accents, white thigh-high stockings, blue heels",
        description="自拍模式角色特征提示词（所有模型共享）",
    )
    negative_prompt_add: str = Field(
        default="1::artist collaboration,multiple views,thick outline::,lowres, bad anatomy, bad hands, bad composition, worst quality, jpeg artifacts, signature, watermark, username, blurry, deformed, disfigured, extra limbs, extra fingers, fewer limbs, fewer fingers, missing limbs, missing fingers, malformed limbs, malformed fingers, text, error, ugly, tiling, cropped, poorly drawn face, poorly drawn hands, abstract, chibi, doll, stuffed toy,male,",
        description="负面提示词（所有模型共享）",
    )
    selfie_ref_image: str = Field(
        default="",
        description="自拍参考图文件名（存放在 selfie_refs/ 目录下），留空则使用文字提示词模式",
    )


class NsfwFilterSection(PluginConfigBase):
    __ui_label__ = "NSFW 内容过滤配置"
    enabled: bool = Field(default=True, description="是否默认启用NSFW内容过滤")
    filter_tags: str = Field(default="{{{{{nsfw}}}}}", description="NSFW过滤标签")


class PromptGeneratorSection(PluginConfigBase):
    __ui_label__ = "提示词生成配置"
    model_name: str = Field(default="deepseek-v4-pro", description="LLM模型代号")
    api_base: str = Field(default="https://api.deepseek.com", description="自定义 LLM API 地址")
    api_key: str = Field(default="", description="自定义 LLM API 密钥")
    output_format: str = Field(default="json", description="输出格式：json/text")
    selfie_appearance_policy: str = Field(default="auto", description="自拍外貌标签策略：auto/never/keep")
    enforce_tag_order: bool = Field(default=False, description="是否对提示词做轻量排序")
    scene_llm_enabled: bool = Field(default=True, description="是否启用自拍场景LLM增强（使用本配置的模型）")
    temperature: float = Field(default=0.2, description="LLM温度")
    max_tokens: int = Field(default=4000, description="LLM最大输出token")
    prompt_template: str = Field(default="", description="自定义提示词模板")
    inherit_ttl: int = Field(default=3600, description="提示词继承有效时间（秒）")


class RandomSceneSection(PluginConfigBase):
    __ui_label__ = "随机场景生成配置"
    temperature: float = Field(default=1.0, description="LLM温度")
    max_tokens: int = Field(default=200, description="LLM最大输出token")


class TaggerSection(PluginConfigBase):
    __ui_label__ = "图片打标配置"
    enabled: bool = Field(default=True, description="是否启用打标")
    api_base: str = Field(default="", description="打标专用 API 地址")
    api_key: str = Field(default="", description="打标专用 API 密钥")
    model_name: str = Field(default="", description="打标专用模型名称")
    temperature: float = Field(default=0.4, description="打标温度")
    max_tokens: int = Field(default=1200, description="打标最大输出token")


class CustomPromptSection(PluginConfigBase):
    __ui_label__ = "自定义系统提示词"
    system_prompt: str = Field(default="", description="自定义系统提示词")


class AiDrawPluginConfig(PluginConfigBase):
    """AI Draw 图片生成插件完整配置"""
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    models: ModelsSectionConfig = Field(default_factory=ModelsSectionConfig)
    admin: AdminSection = Field(default_factory=AdminSection)
    auto_recall: AutoRecallSection = Field(default_factory=AutoRecallSection)
    prompt_show: PromptShowSection = Field(default_factory=PromptShowSection)
    nsfw_filter: NsfwFilterSection = Field(default_factory=NsfwFilterSection)
    prompt_generator: PromptGeneratorSection = Field(default_factory=PromptGeneratorSection)
    random_scene: RandomSceneSection = Field(default_factory=RandomSceneSection)
    tagger: TaggerSection = Field(default_factory=TaggerSection)
    custom_prompt: CustomPromptSection = Field(default_factory=CustomPromptSection)


# ================================================================
# Plugin Class
# ================================================================

class AiDrawPlugin(MaiBotPlugin):
    """AI Draw 图片生成插件，命令前缀 /ad（AI Draw）"""

    config_model = AiDrawPluginConfig

    # 订阅全局模型配置热重载
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ("model",)

    # ---- 常量 ----
    MODEL_MAPPINGS: ClassVar[dict] = MODEL_MAPPINGS
    SIZE_MAPPINGS: ClassVar[dict] = SIZE_MAPPINGS
    BESTNAI_MODEL_IDS: ClassVar[list] = BESTNAI_MODEL_IDS

    # ---- 随机场景跟踪 ----
    _recent_random_scenes: ClassVar[list] = []
    _MAX_RECENT_SCENES: ClassVar[int] = 5

    # ================================================================
    # Lifecycle
    # ================================================================

    async def on_load(self) -> None:
        from .core.session_state import session_state as ss_mod

        # 注入 logger
        for mod in (ss_mod,):
            if hasattr(mod, "inject_logger"):
                mod.inject_logger(self.ctx.logger)

        self._session_state = session_state
        self._last_structured_prompt_payload: Optional[Dict[str, Any]] = None
        self._pending_tasks: list = []

        # 加载模型配置
        raw_models = self._load_raw_models_config()
        self._loaded_models = load_models_config(raw_models)
        self._session_state.set_loaded_models(self._loaded_models)

        # 设置全局单例
        set_plugin_instance(self)

        self.ctx.logger.info(
            f"ai_draw_plugin v2 已加载，共 {len(self._loaded_models)} 个模型配置"
        )

    async def on_unload(self) -> None:
        for task in list(self._pending_tasks):
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()
        clear_plugin_instance()
        self.ctx.logger.info("ai_draw_plugin 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            raw_models = self._load_raw_models_config()
            self._loaded_models = load_models_config(raw_models)
            self._session_state.set_loaded_models(self._loaded_models)
            self.ctx.logger.info(
                f"ai_draw_plugin 配置已更新: version={version}, models={len(self._loaded_models)}"
            )
        elif scope == "model":
            self.ctx.logger.info(f"全局模型配置已更新: version={version}")

    # ================================================================
    # Config helpers
    # ================================================================

    def _load_raw_models_config(self) -> dict:
        """直接从 TOML 文件读取 [models] 原始配置（绕过 PluginConfigBase 的字段过滤）。

        PluginConfigBase 只保留已定义字段，动态的 [models.modelX] 会被丢弃，
        因此必须直接解析 TOML 文件获取完整的多模型配置。
        """
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                self.ctx.logger.error("[ai_draw] 无法导入 tomllib/tomli，模型配置加载失败")
                return {}

        from pathlib import Path
        config_path = Path(__file__).parent / "config.toml"
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            self.ctx.logger.error(f"[ai_draw] 读取 config.toml 失败: {e}")
            return {}

        raw_models = data.get("models", {})
        return raw_models if isinstance(raw_models, dict) else {}

    def _get_config_callable(self):
        def get_config(path: str, default=None):
            parts = path.split(".")
            obj = self.config
            for part in parts:
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                else:
                    return default
            return obj
        return get_config

    # ================================================================
    # Session info
    # ================================================================

    def _extract_session_info(self, kwargs: dict) -> dict:
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
    # Permission
    # ================================================================

    def _check_user_permission_from_kwargs(self, kwargs: dict) -> bool:
        info = self._extract_session_info(kwargs)
        return self._session_state.check_user_permission(
            info["platform"], info["chat_id"], info["user_id"], self._get_config_callable(),
        )

    # ================================================================
    # Model config resolution
    # ================================================================

    def _get_model_config_from_kwargs(self, kwargs: dict) -> dict:
        """构建合并后的模型配置字典（从 [models.modelX] 中获取完整配置）。"""
        info = self._extract_session_info(kwargs)

        # 确定当前使用的 model_id
        default_model_id = self.config.models.default_model if hasattr(self.config, 'models') else "model1"
        if info["chat_id"]:
            selected = self._session_state.get_selected_model(info["platform"], info["chat_id"])
            if selected:
                # 可能是旧的 model name（如 "nai-diffusion-4-5-full"）
                if selected not in self._loaded_models:
                    default_model_id = self._resolve_model_id_by_name(selected) or default_model_id
                else:
                    default_model_id = selected
            else:
                default_model_id = default_model_id

        base = dict(self._loaded_models.get(default_model_id, {}) or {})

        # 从 [prompt_show] 注入共享的负面提示词和自拍提示词（作为 baseline）
        ps = self.config.prompt_show
        base.setdefault("negative_prompt_add", ps.negative_prompt_add)
        base.setdefault("selfie_prompt_add", ps.selfie_prompt_add)

        # Session 级别的模型覆盖
        if info["chat_id"]:
            selected_name = self._session_state.get_selected_model(info["platform"], info["chat_id"])
            if selected_name and selected_name in BESTNAI_MODEL_IDS:
                base["model"] = selected_name

        # Artist preset selection（从当前 model 的 artist_presets 中选）
        if info["chat_id"] and default_model_id:
            selected_preset = self._session_state.get_selected_artist_preset_config(
                info["platform"], info["chat_id"], default_model_id,
            )
            if selected_preset:
                artist_prompt = str(selected_preset.get("prompt", "") or "")
                if artist_prompt:
                    base["nai_artist_prompt"] = artist_prompt
                neg = str(selected_preset.get("negative_prompt_add", "") or "").strip()
                if neg:
                    existing_neg = base.get("negative_prompt_add", "")
                    base["negative_prompt_add"] = f"{neg}, {existing_neg}" if existing_neg else neg

        # Size selection
        if info["chat_id"]:
            selected_size = self._session_state.get_selected_size(info["platform"], info["chat_id"])
            if selected_size:
                base["size_preset"] = selected_size
                base["nai_size"] = selected_size

        # NSFW filter
        if info["chat_id"]:
            if self._session_state.is_nsfw_filter_enabled(
                info["platform"], info["chat_id"], self._get_config_callable(),
            ):
                nsfw_tags = self.config.nsfw_filter.filter_tags
                current_neg = base.get("negative_prompt_add", "")
                base["negative_prompt_add"] = f"{nsfw_tags}, {current_neg}" if current_neg else nsfw_tags

        # 统一 key 别名（兼容旧代码同时读取新/旧 key）
        base.setdefault("nai_endpoint", base.get("endpoint", "/v1/chat/completions"))
        base.setdefault("nai_proxy_mode", base.get("proxy_mode", "auto"))

        return base

    def _resolve_model_id_by_name(self, model_name: str) -> Optional[str]:
        """根据模型名（如 nai-diffusion-4-5-full）反向查找 model_id（如 model1）。"""
        for mid, cfg in self._loaded_models.items():
            if isinstance(cfg, dict) and cfg.get("model") == model_name:
                return mid
        return None

    # ================================================================
    # Task tracking
    # ================================================================

    def _track_task(self, task: asyncio.Task) -> None:
        self._pending_tasks.append(task)
        task.add_done_callback(
            lambda t: None if t not in self._pending_tasks else self._pending_tasks.remove(t)
        )

    # @Tool — LLM 触发生图
    @Tool(
        "nai_web_draw",
        brief_description="生成图片、自拍、照片。用于画图、自拍、拍照、发照片等一切需要生成图像的场景。",
        detailed_description=(
            "使用 BestNAI / NovelAI 根据描述生成二次元插画。\n"
            "参数：description（必填，关键词列表，空格分隔）、size（可选，图片尺寸）"
        ),
        parameters=[
            ToolParameterInfo(name="description", param_type=ToolParamType.STRING,
                              description="画面关键词列表，空格分隔。禁止完整句子。", required=True),
            ToolParameterInfo(name="size", param_type=ToolParamType.STRING,
                              description="图片尺寸（默认从配置获取）", required=False),
        ],
    )
    async def handle_nai_web_draw(self, description: str = "", size: str = "", **kwargs) -> dict:
        from .components.command import handle_ad_web_draw
        return await handle_ad_web_draw(description, size, kwargs)

    # /ad on|off — 插件总开关
    @Command("ad_plugin_toggle", pattern=r"^/ad\s+(?P<action>on|off)$")
    async def handle_ad_plugin_toggle(self, **kwargs) -> tuple:
        from .components.command import handle_ad_plugin_toggle
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_plugin_toggle(matched.get("action", "").strip().lower(), kwargs)

    # /ad c on|off — 自动撤回开关
    @Command("ad_recall_control", pattern=r"^/ad\s+c\s+(?P<action>on|off)$")
    async def handle_ad_recall_control(self, **kwargs) -> tuple:
        from .components.command import handle_ad_recall_control
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_recall_control(matched.get("action", "").strip().lower(), kwargs)

    # /ad nsfw <on|off> — NSFW 过滤开关
    @Command("ad_nsfw_control", pattern=r"^/ad\s+nsfw(?:\s+(?P<action>on|off))?$")
    async def handle_ad_nsfw_control(self, **kwargs) -> tuple:
        from .components.command import handle_ad_nsfw_control
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_nsfw_control((matched.get("action") or "").strip().lower(), kwargs)

    # /ad send <d|f> — 发送方式（d=直发 f=合并转发）
    @Command("ad_send_mode", pattern=r"^/ad\s+send(?:\s+(?P<action>d|f|direct|forward))?$")
    async def handle_ad_send_mode(self, **kwargs) -> tuple:
        from .components.command import handle_ad_send_mode
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_send_mode((matched.get("action") or "").strip().lower(), kwargs)

    # /ad pt <on|off> — 提示词显示开关
    @Command("ad_prompt_show", pattern=r"^/ad\s+pt\s+(?P<action>on|off)$")
    async def handle_ad_prompt_show(self, **kwargs) -> tuple:
        from .components.command import handle_ad_prompt_show
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_prompt_show(matched.get("action", "").strip().lower(), kwargs)

    # /ad st|sp 管理员模式 · /ad w|m 模型 · /ad s 尺寸 · /ad art 画师 · /ad help 帮助
    @Command("ad_admin_st_sp", pattern=r"^/ad\s+(?P<action>st|sp)$")
    async def handle_ad_admin_toggle(self, **kwargs) -> tuple:
        from .components.command import handle_ad_admin_toggle
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_admin_toggle(matched.get("action", "").strip().lower(), kwargs)

    @Command("ad_help", pattern=r"^/ad\s+help$")
    async def handle_ad_help(self, **kwargs) -> tuple:
        from .components.command import handle_ad_help
        return await handle_ad_help(kwargs.get("stream_id", ""))

    @Command("ad_switch_model", pattern=r"^/ad\s+(?P<action>w|m)(?:\s+(?P<param>.+))?$")
    async def handle_ad_switch_model(self, **kwargs) -> tuple:
        from .components.command import handle_ad_switch_model
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_switch_model((matched.get("param") or "").strip(), kwargs)

    @Command("ad_switch_size", pattern=r"^/ad\s+s(?:\s+(?P<param>.+))?$")
    async def handle_ad_switch_size(self, **kwargs) -> tuple:
        from .components.command import handle_ad_switch_size
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_switch_size((matched.get("param") or "").strip(), kwargs)

    @Command("ad_switch_artist", pattern=r"^/ad\s+art(?:\s+(?P<param>.+))?$")
    async def handle_ad_switch_artist(self, **kwargs) -> tuple:
        from .components.command import handle_ad_switch_artist
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_switch_artist((matched.get("param") or "").strip(), kwargs)

    # /ad y <风格名> — 风格预设图生图
    @Command("ad_style", pattern=r"(?:[\s\S]*，说：\s*)?/ad\s+y\s+(?P<style>.+)$")
    async def handle_ad_style(self, **kwargs) -> tuple:
        from .components.command import handle_ad_style
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_style((matched.get("style") or "").strip(), kwargs)

    # /ad 撤回 — 手动撤回
    @Command("ad_manual_recall", pattern=r"^/ad\s+撤回")
    async def handle_ad_manual_recall(self, **kwargs) -> tuple:
        from .components.command import handle_ad_manual_recall
        return await handle_ad_manual_recall(kwargs)

    # /ad0 rh|hr|r|h|t <tags> — 直传 tag + 参考图，跳过 LLM/VLM
    @Command("dr0_ref_draw", pattern=r"(?:[\s\S]*，说：\s*)?/ad0\s+(?P<mode>rh|hr|r|h|t)\s+(?P<tags>[\s\S]+)$")
    async def handle_dr0_ref_draw(self, **kwargs) -> tuple:
        from .components.command import handle_dr0_ref_draw
        matched = kwargs.get("matched_groups", {})
        return await handle_dr0_ref_draw(
            (matched.get("mode") or "").strip().lower(),
            (matched.get("tags") or "").strip(),
            kwargs,
        )

    # /ad0 <tags> — 直接 tag 生图
    @Command("dr0_draw", pattern=r"^/ad0\s+(?P<tags>[\s\S]+)$")
    async def handle_dr0_draw(self, **kwargs) -> tuple:
        from .components.command import handle_dr0_draw
        matched = kwargs.get("matched_groups", {})
        return await handle_dr0_draw(matched.get("tags", "").strip(), kwargs)

    # /ad rh|hr|r|h|t <描述> — 参考模式（r/h/rh/hr 仅管理员，t=i2i 不限）
    @Command("ad_ref_draw", pattern=r"(?:[\s\S]*，说：\s*)?/ad\s+(?P<mode>rh|hr|r|h|t)\s+(?P<description>[\s\S]+)$")
    async def handle_ad_ref_draw(self, **kwargs) -> tuple:
        from .components.command import handle_ad_ref_draw
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_ref_draw(
            (matched.get("mode") or "").strip().lower(),
            (matched.get("description") or "").strip(),
            kwargs,
        )

    # /ad <描述> — LLM 提示词生图（最低优先级，排除上面所有子命令）
    @Command(
        "ad_draw",
        pattern=(
            r"(?:[\s\S]*，说：\s*)?/ad\s+"
            r"(?!on$|off$|st$|sp$|pt\s|nsfw\b|send\b|help$|c\s|w\b|m\b|s\b|art\b|y\s|撤回"
            r"|rh\s|hr\s|r\s|h\s|t\s)"
            r"(?P<description>[\s\S]+)$"
        ),
    )
    async def handle_ad_draw(self, **kwargs) -> tuple:
        from .components.command import handle_ad_draw
        matched = kwargs.get("matched_groups", {})
        return await handle_ad_draw((matched.get("description") or "").strip(), kwargs)


# ================================================================
# Factory
# ================================================================

def create_plugin():
    return AiDrawPlugin()
