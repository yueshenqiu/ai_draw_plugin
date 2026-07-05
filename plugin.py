# -*- coding: utf-8 -*-
"""AI Draw 图片生成插件 — MaiBot SDK 2.0（重构版）

命令前缀统一为 /ad（AI Draw）。
模型配置采用 provider-agnostic 多模型自包含结构 [models.modelX]。
文件结构参照 video_generator_plugin 分类整理。
"""

import asyncio
from typing import Any, ClassVar, Dict, List, Literal, Optional

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import ConfigDict, field_validator, model_validator

from .constants.constants import MODEL_MAPPINGS, SIZE_MAPPINGS, BESTNAI_MODEL_IDS
from .instance import set_plugin_instance, clear_plugin_instance
from .core.generator import load_models_config
from .core.session_state import session_state


# ================================================================
# Config Models（适配新 [models.modelX] 结构）
# ================================================================

def _ui(label: str, *, hint: str = "", order: int = 0, **extra: Any) -> Dict[str, Any]:
    """构造 WebUI 配置项元数据。

    插件配置页按 field.ui_type 渲染控件，ui_type 由 json_schema_extra 的
    "x-widget" 决定（无则按字段类型推断）。manifest 仅声明 zh-CN，故不写 i18n。
    """
    meta: Dict[str, Any] = {"label": label, "order": order}
    if hint:
        meta["hint"] = hint
    meta.update(extra)
    return meta


def _normalize_str_id_list(value: Any) -> List[str]:
    """把任意输入归一化为去重后的字符串 ID 列表（WebUI 列表编辑器用）。"""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item if item is not None else "").strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out


class PluginSectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件基本配置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True, description="是否启用插件",
        json_schema_extra=_ui("启用插件", hint="关闭后插件不响应任何 /ad 指令", order=0),
    )
    send_mode: Literal["direct", "forward"] = Field(
        default="direct", description="图片发送方式：direct（普通直发，快）/ forward（合并转发，隐蔽但慢）",
        json_schema_extra=_ui(
            "图片发送方式", order=1,
            hint="direct=普通直发（快）；forward=合并转发（隐蔽但慢，QQ 服务端构建耗时）",
        ),
    )
    force_forward_when_nsfw_off: bool = Field(
        default=True, description="NSFW 过滤关闭时强制用合并转发（更隐蔽）",
        json_schema_extra=_ui("NSFW 关闭时强制合并转发", order=2, hint="当前会话关闭 NSFW 过滤时，强制走合并转发以提升隐蔽性"),
    )
    use_http_direct: bool = Field(
        default=False, description="是否直连 NapCat/SnowLuma 本机 HTTP API（默认 false 走 SDK passthrough）。慢环境下回执易超时丢失 message_id，可开启此项用 HTTP 直连提速；仅本机地址，配置见 README",
        json_schema_extra=_ui(
            "HTTP 直连本机适配器", order=3,
            hint="默认关闭走 SDK passthrough。慢环境下回执易超时丢 message_id，可开启用本机 HTTP 直连提速；仅允许本机回环地址",
        ),
    )
    napcat_http_url: str = Field(
        default="http://127.0.0.1:5780", description="HTTP 直连地址（仅 use_http_direct=true 时生效，仅允许本机 127.0.0.1/localhost）",
        json_schema_extra=_ui(
            "HTTP 直连地址", order=4, placeholder="http://127.0.0.1:5780",
            hint="仅 HTTP 直连开启时生效，且只允许本机 127.0.0.1 / localhost",
        ),
    )
    napcat_http_token: str = Field(
        default="", description="HTTP 直连访问令牌（与 NapCat/SnowLuma 的 HTTP 服务器 token 一致）",
        json_schema_extra=_ui(
            "HTTP 直连令牌", order=5, placeholder="无则留空",
            hint="与 NapCat/SnowLuma 的 HTTP 服务器 token 一致，无则留空",
            **{"x-widget": "password"},
        ),
    )
    y_apply_artist_preset: bool = Field(
        default=False, description="使用 /ad y（提示词预设）时是否叠加当前风格预设（画师串）",
        json_schema_extra=_ui(
            "「/ad y 提示词预设」叠加风格预设（画师串）", order=6,
            hint="开启后 /ad y 会在提示词预设之上再叠加当前选中的风格预设（画师串）；默认关闭，只用提示词预设本身",
        ),
    )
    config_version: str = Field(
        default="2.0.0", description="配置版本号",
        json_schema_extra=_ui("配置版本", order=99, hidden=True, disabled=True),
    )

    @field_validator("send_mode", mode="before")
    @classmethod
    def _normalize_send_mode(cls, value: Any) -> str:
        s = str(value or "direct").strip().lower()
        return s if s in {"direct", "forward"} else "direct"


class ModelItem(PluginConfigBase):
    """单个生图模型配置（WebUI 列表卡片编辑）。id 为模型标识（如 model1），代码按 id 查找。"""
    # extra="allow"：兜底保留未显式声明的模型字段（如 extra_params 等），保存不丢
    model_config = ConfigDict(validate_assignment=True, extra="allow")
    id: str = Field(default="", description="模型 ID（唯一标识，如 model1）",
                    json_schema_extra=_ui("模型 ID", order=0, placeholder="model1"))
    name: str = Field(default="", description="模型显示名",
                      json_schema_extra=_ui("显示名", order=1, placeholder="BestNAI V4.5"))
    format: str = Field(default="bestnai", description="服务商格式",
                        json_schema_extra=_ui("服务商格式", order=2, placeholder="bestnai"))
    base_url: str = Field(default="", description="API 地址",
                          json_schema_extra=_ui("API 地址", order=3, placeholder="https://..."))
    api_key: str = Field(default="", description="API 密钥",
                         json_schema_extra=_ui("API 密钥", order=4, placeholder="sk-...", **{"x-widget": "password"}))
    model: str = Field(default="nai-diffusion-4-5-full", description="底层模型名",
                       json_schema_extra=_ui("底层模型名", order=5, placeholder="nai-diffusion-4-5-full"))
    endpoint: str = Field(default="/v1/chat/completions", description="接口路径",
                          json_schema_extra=_ui("接口路径", order=6))
    max_tokens: int = Field(default=100000, description="最大 token",
                            json_schema_extra=_ui("最大 token", order=7))
    sampler: str = Field(default="k_euler_ancestral", description="采样器",
                         json_schema_extra=_ui("采样器", order=8))
    steps: int = Field(default=28, description="步数",
                       json_schema_extra=_ui("步数", order=9))
    scale: float = Field(default=6.0, description="引导强度 scale",
                         json_schema_extra=_ui("引导强度", order=10, step=0.1))
    cfg: float = Field(default=0.0, description="cfg_rescale (0~1)",
                       json_schema_extra=_ui("cfg_rescale", order=11, step=0.1))
    noise_schedule: str = Field(default="karras", description="噪声调度",
                                json_schema_extra=_ui("噪声调度", order=12))
    default_size: str = Field(default="832x1216", description="默认尺寸",
                              json_schema_extra=_ui("默认尺寸", order=13))
    size_preset: str = Field(default="竖图", description="尺寸预设",
                             json_schema_extra=_ui("尺寸预设", order=14))
    artist_preset: str = Field(default="无", description="引用的风格预设（画师串）名",
                               json_schema_extra=_ui("风格预设名", order=15, placeholder="无"))


def _legacy_flat_models_to_entries(data: Any) -> Any:
    """把旧 [models.modelX] 扁平结构迁移成 {default_model, entries:[{id,...}]}。

    兼容：新结构（含 entries 键，原样）、旧扁平（default_model + 多个 modelX 子表）。
    """
    if not isinstance(data, dict):
        return data
    if "entries" in data:
        return data
    default_model = data.get("default_model", "model1")
    entries = []
    for key, value in data.items():
        if key in ("default_model", "hint"):
            continue
        if isinstance(value, dict):
            entry = dict(value)
            entry.setdefault("id", key)
            entries.append(entry)
    if entries:
        return {"default_model": default_model, "entries": entries}
    return data


class ModelsSectionConfig(PluginConfigBase):
    # extra="allow"：兜底保留未声明字段
    model_config = ConfigDict(validate_assignment=True, extra="allow")
    __ui_label__: ClassVar[str] = "多模型配置"
    __ui_order__: ClassVar[int] = 10
    default_model: str = Field(
        default="model1", description="默认使用的模型 ID",
        json_schema_extra=_ui(
            "默认模型 ID", order=0, placeholder="model1",
            hint="对应下方某个模型的 ID（如 model1）",
        ),
    )
    entries: List[ModelItem] = Field(
        default_factory=list, description="生图模型列表",
        json_schema_extra=_ui("生图模型列表", order=1, hint="每个模型含 API、参数、引用的风格预设名；代码按 id 查找"),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy(cls, data: Any) -> Any:
        return _legacy_flat_models_to_entries(data)


class AutoRecallSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "自动撤回配置"
    __ui_order__: ClassVar[int] = 3
    enabled: bool = Field(
        default=True, description="是否默认启用自动撤回",
        json_schema_extra=_ui("启用自动撤回", order=0, hint="发图后延时自动撤回，可用 /ad 指令按会话热切换"),
    )
    delay_seconds: int = Field(
        default=30, description="撤回延迟时间（秒）",
        json_schema_extra=_ui("撤回延迟（秒）", order=1, hint="发送成功后等待多少秒再撤回"),
    )
    allowed_groups: List[str] = Field(
        default_factory=list, description="允许自动撤回的会话白名单",
        json_schema_extra=_ui(
            "撤回白名单", order=2, placeholder="platform:chat_id",
            hint="格式 platform:chat_id；留空=全部会话允许自动撤回",
        ),
    )

    @field_validator("allowed_groups", mode="before")
    @classmethod
    def _normalize_allowed_groups(cls, value: Any) -> List[str]:
        return _normalize_str_id_list(value)


class AdminSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "管理员权限配置"
    __ui_order__: ClassVar[int] = 1
    admin_users: List[str] = Field(
        default_factory=list, description="管理员用户ID列表",
        json_schema_extra=_ui(
            "管理员 QQ 号", order=0, placeholder="请输入 QQ 号",
            hint="管理员 QQ 号列表；管理员模式开启时仅这些用户可用生图指令",
        ),
    )
    default_admin_mode: bool = Field(
        default=False, description="是否默认启用管理员模式",
        json_schema_extra=_ui("默认启用管理员模式", order=1, hint="开启后仅管理员可使用生图指令"),
    )

    @field_validator("admin_users", mode="before")
    @classmethod
    def _normalize_admin_users(cls, value: Any) -> List[str]:
        return _normalize_str_id_list(value)


class PromptShowSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "提示词显示配置"
    __ui_order__: ClassVar[int] = 5
    enabled: bool = Field(
        default=False, description="是否默认启用提示词显示",
        json_schema_extra=_ui("启用提示词显示", order=0, hint="开启后发图时一并展示最终提示词，可用 /ad 指令热切换"),
    )
    hide_selfie_prompt_add: bool = Field(
        default=False, description="显示时是否隐藏自拍补充提示词",
        json_schema_extra=_ui("隐藏自拍补充提示词", order=1, hint="展示提示词时，隐藏下方的自拍角色特征部分"),
    )
    selfie_prompt_add: str = Field(
        default="girl,long brown hair with a star-shaped hair accessory, warm brown eyes, blue and white princess dress with a cutout at the midriff, gold trim, purple gem accents, white thigh-high stockings, blue heels",
        description="自拍模式角色特征提示词（所有模型共享）",
        json_schema_extra=_ui(
            "自拍角色特征提示词", order=2, rows=4,
            hint="自拍模式下追加的角色外貌标签，所有模型共享",
            **{"x-widget": "textarea"},
        ),
    )
    negative_prompt_add: str = Field(
        default="1::artist collaboration,multiple views,thick outline::,lowres, bad anatomy, bad hands, bad composition, worst quality, jpeg artifacts, signature, watermark, username, blurry, deformed, disfigured, extra limbs, extra fingers, fewer limbs, fewer fingers, missing limbs, missing fingers, malformed limbs, malformed fingers, text, error, ugly, tiling, cropped, poorly drawn face, poorly drawn hands, abstract, chibi, doll, stuffed toy,male,",
        description="负面提示词（所有模型共享）",
        json_schema_extra=_ui(
            "全局负面提示词", order=3, rows=5,
            hint="所有模型共享的负面提示词",
            **{"x-widget": "textarea"},
        ),
    )
    selfie_ref_image: str = Field(
        default="", description="自拍参考图文件名（存放在 selfie_refs/ 目录下），留空则使用文字提示词模式",
        json_schema_extra=_ui(
            "自拍参考图文件名", order=4, placeholder="留空则用文字提示词",
            hint="存放在 selfie_refs/ 目录下的文件名；留空则使用上方文字提示词模式",
        ),
    )


class NsfwFilterSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "NSFW 内容过滤配置"
    __ui_order__: ClassVar[int] = 4
    enabled: bool = Field(
        default=True, description="是否默认启用NSFW内容过滤",
        json_schema_extra=_ui("启用 NSFW 过滤", order=0, hint="开启后向提示词注入过滤标签，可用 /ad 指令按会话热切换"),
    )
    filter_tags: str = Field(
        default="{{{{{nsfw}}}}}", description="NSFW过滤标签",
        json_schema_extra=_ui("NSFW 过滤标签", order=1, hint="注入到负面提示词的 NSFW 过滤标签"),
    )



class PromptGeneratorSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "提示词生成配置"
    __ui_order__: ClassVar[int] = 2
    model_name: str = Field(
        default="deepseek-v4-pro", description="LLM模型代号",
        json_schema_extra=_ui("LLM 模型代号", order=0, placeholder="deepseek-v4-pro"),
    )
    api_base: str = Field(
        default="https://api.deepseek.com", description="自定义 LLM API 地址",
        json_schema_extra=_ui("LLM API 地址", order=1, placeholder="https://api.deepseek.com"),
    )
    api_key: str = Field(
        default="", description="自定义 LLM API 密钥",
        json_schema_extra=_ui("LLM API 密钥", order=2, placeholder="sk-...", **{"x-widget": "password"}),
    )
    output_format: Literal["json", "text"] = Field(
        default="json", description="输出格式：json/text",
        json_schema_extra=_ui("输出格式", order=3, hint="LLM 提示词输出格式：json（结构化）/ text（纯文本）"),
    )
    selfie_appearance_policy: Literal["auto", "never", "keep"] = Field(
        default="auto", description="自拍外貌标签策略：auto/never/keep",
        json_schema_extra=_ui(
            "自拍外貌标签策略", order=4,
            hint="auto=自动判断；never=从不加外貌标签；keep=保留",
        ),
    )
    enforce_tag_order: bool = Field(
        default=False, description="是否对提示词做轻量排序",
        json_schema_extra=_ui("轻量排序提示词", order=5, hint="对生成的提示词标签做轻量重排"),
    )
    scene_llm_enabled: bool = Field(
        default=True, description="是否启用自拍场景LLM增强（使用本配置的模型）",
        json_schema_extra=_ui("自拍场景 LLM 增强", order=6, hint="启用后用本配置的模型增强自拍场景描述"),
    )
    temperature: float = Field(
        default=0.2, description="LLM温度",
        json_schema_extra=_ui("LLM 温度", order=7, step=0.1, hint="越低越稳定，越高越发散"),
    )
    max_tokens: int = Field(
        default=4000, description="LLM最大输出token",
        json_schema_extra=_ui("最大输出 token", order=8),
    )
    prompt_template: str = Field(
        default="", description="自定义提示词模板",
        json_schema_extra=_ui(
            "自定义提示词模板", order=9, rows=4, placeholder="留空使用内置模板",
            hint="覆盖内置的提示词生成模板，留空则用默认",
            **{"x-widget": "textarea"},
        ),
    )
    inherit_ttl: int = Field(
        default=3600, description="提示词继承有效时间（秒）",
        json_schema_extra=_ui("提示词继承有效期（秒）", order=10, hint="上一轮提示词可被继承的有效时间"),
    )

    @field_validator("output_format", mode="before")
    @classmethod
    def _normalize_output_format(cls, value: Any) -> str:
        s = str(value or "json").strip().lower()
        return s if s in {"json", "text"} else "json"

    @field_validator("selfie_appearance_policy", mode="before")
    @classmethod
    def _normalize_appearance_policy(cls, value: Any) -> str:
        s = str(value or "auto").strip().lower()
        return s if s in {"auto", "never", "keep"} else "auto"


class RandomSceneSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "随机场景生成配置"
    __ui_order__: ClassVar[int] = 8
    temperature: float = Field(
        default=1.0, description="LLM温度",
        json_schema_extra=_ui("LLM 温度", order=0, step=0.1, hint="随机场景生成温度，越高越多样"),
    )
    max_tokens: int = Field(
        default=200, description="LLM最大输出token",
        json_schema_extra=_ui("最大输出 token", order=1),
    )


class CustomPromptSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "自定义系统提示词"
    __ui_order__: ClassVar[int] = 9
    system_prompt: str = Field(
        default="", description="自定义系统提示词",
        json_schema_extra=_ui(
            "自定义系统提示词", order=0, rows=5, placeholder="留空使用内置系统提示词",
            hint="覆盖提示词生成的系统提示词，留空则用内置",
            **{"x-widget": "textarea"},
        ),
    )



class PresetItem(PluginConfigBase):
    """单条预设：名称 + 提示词。用于风格预设（画师串）与提示词预设的 WebUI 列表编辑。"""
    name: str = Field(
        default="", description="预设名称",
        json_schema_extra=_ui("名称", order=0, placeholder="如 梦幻柔美2.0"),
    )
    prompt: str = Field(
        default="", description="预设内容（提示词 / 画师串）",
        json_schema_extra=_ui(
            "内容", order=1, rows=4, placeholder="英文提示词 / 画师串（支持 NAI 权重语法）",
            **{"x-widget": "textarea"},
        ),
    )


def _legacy_flat_to_presets(data: Any) -> Any:
    """把旧扁平字典 {名称: 提示词} 归一成新结构 {"presets": [{name, prompt}]}。

    兼容三种输入：新结构（含 presets 键，原样返回）、旧扁平字典、直接列表。
    这样旧 config.toml 无需改动即可通过校验；WebUI 首次保存后自然写成新结构。
    """
    if not isinstance(data, dict):
        if isinstance(data, list):
            return {"presets": data}
        return data
    if "presets" in data:
        return data
    items = [{"name": str(k), "prompt": v} for k, v in data.items() if isinstance(v, str)]
    return {"presets": items} if items else data


class ArtistPresetsSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "风格预设（画师串）"
    __ui_order__: ClassVar[int] = 6
    presets: List[PresetItem] = Field(
        default_factory=list, description="风格预设（画师串）列表；模型里用 artist_preset = 名称 引用",
        json_schema_extra=_ui("风格预设（画师串）", order=0, hint="每条含名称+画师串内容；模型用 artist_preset 引用名称"),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy(cls, data: Any) -> Any:
        return _legacy_flat_to_presets(data)


class StylesSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "提示词预设"
    __ui_order__: ClassVar[int] = 7
    presets: List[PresetItem] = Field(
        default_factory=list, description="提示词预设列表（/ad y 命令使用）",
        json_schema_extra=_ui("提示词预设", order=0, hint="每条含名称+完整提示词；/ad y <名称> 引用"),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy(cls, data: Any) -> Any:
        return _legacy_flat_to_presets(data)


class AiDrawPluginConfig(PluginConfigBase):
    """AI Draw 图片生成插件完整配置"""
    # extra="allow"：保留未显式声明的顶层段（如动态 [models.modelX]），
    # 否则 PluginConfigBase 默认 extra="ignore" 会在 WebUI 保存时把这些段整段写丢。
    model_config = ConfigDict(validate_assignment=True, extra="allow")

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    models: ModelsSectionConfig = Field(default_factory=ModelsSectionConfig)
    admin: AdminSection = Field(default_factory=AdminSection)
    auto_recall: AutoRecallSection = Field(default_factory=AutoRecallSection)
    prompt_show: PromptShowSection = Field(default_factory=PromptShowSection)
    nsfw_filter: NsfwFilterSection = Field(default_factory=NsfwFilterSection)
    prompt_generator: PromptGeneratorSection = Field(default_factory=PromptGeneratorSection)
    artist_presets: ArtistPresetsSection = Field(default_factory=ArtistPresetsSection)
    styles: StylesSection = Field(default_factory=StylesSection)
    random_scene: RandomSceneSection = Field(default_factory=RandomSceneSection)
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

        # 清预设缓存，确保加载读的是当前 config.toml（防跨重载残留空缓存）
        self._clear_preset_caches()

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
            # 清画师预设/提示词预设缓存，否则换了 config.toml 仍读旧缓存（导致"读不到预设"）
            self._clear_preset_caches()
            raw_models = self._load_raw_models_config()
            self._loaded_models = load_models_config(raw_models)
            self._session_state.set_loaded_models(self._loaded_models)
            self.ctx.logger.info(
                f"ai_draw_plugin 配置已更新: version={version}, models={len(self._loaded_models)}"
            )
        elif scope == "model":
            self.ctx.logger.info(f"全局模型配置已更新: version={version}")

    @staticmethod
    def _clear_preset_caches() -> None:
        """清空模块级预设缓存（画师串 + 提示词预设），供热重载/加载时调用。"""
        try:
            from .core.session_state import clear_global_presets_cache
            clear_global_presets_cache()
        except Exception:
            pass
        try:
            from .components.command import clear_styles_cache
            clear_styles_cache()
        except Exception:
            pass

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
        if not isinstance(raw_models, dict):
            return {}
        # 新结构 {default_model, entries:[{id,...}]} → 归一回旧扁平 {modelX: config}，
        # 使 load_models_config 及下游生图链零改动。旧扁平结构原样返回。
        if "entries" in raw_models:
            flat: dict = {"default_model": raw_models.get("default_model", "model1")}
            for entry in (raw_models.get("entries") or []):
                if not isinstance(entry, dict):
                    continue
                mid = str(entry.get("id", "") or "").strip()
                if not mid:
                    continue
                cfg = {k: v for k, v in entry.items() if k != "id"}
                flat[mid] = cfg
            return flat
        return raw_models

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

    def _get_model_config_from_kwargs(self, kwargs: dict, apply_artist_preset: bool = True) -> dict:
        """构建合并后的模型配置字典（从 [models.modelX] 中获取完整配置）。

        apply_artist_preset=False 时跳过风格预设（画师串）的叠加，供 /ad y 提示词预设按配置控制。
        """
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

        # Artist preset selection（风格预设/画师串，从当前 model 的 artist_presets 中选）
        if apply_artist_preset and info["chat_id"] and default_model_id:
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
        "ai_draw",
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
    async def handle_ai_draw(self, description: str = "", size: str = "", **kwargs) -> dict:
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

    # /ad st|sp 管理员模式 · /ad w|m 模型 · /ad s 尺寸 · /ad art 风格预设(画师串) · /ad help 帮助
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

    # /ad y <名称> — 提示词预设图生图
    # 正则用宽松前缀负责"抠出 /ad 后内容"；防误触发（/ad 藏在长文本内）由 _is_real_ad_command 段守卫负责
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

    # /ad0 rh|hr|r|h|t <tags> — 直传 tag + 参考图，跳过 LLM
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
