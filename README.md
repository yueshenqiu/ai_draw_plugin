# AI Draw 图片生成插件

面向 MaiBot 的支持多服务商的图片生成插件。插件可以把中文自然语言转换为 NovelAI/Danbooru 标签，也支持英文标签直传、NovelAI V4/V4.5 人物分层、自拍、参考图、多模型、风格预设、任务队列、会话级内容过滤和精确撤回。

- 命令前缀：`/ad`、`/ad0`
- Tool 名称：`ai_draw`
- MaiBot SDK：`>=2.5.0,<3.0.0`
- Provider：BestNAI/NovelAI 兼容接口、YesNovelAI 原生接口
- 适配器：NapCat、SnowLuma


## 主要功能

- 自然语言生图：LLM 将中文描述翻译为英文标签。
- 精准与随机双模式：普通 `/ad` 克制补全，带“随机”前缀才自由扩写。
- NovelAI V4/V4.5 结构化提示词：全局正向、人物正向和人物负向分层传给 Provider。
- 自拍模式：支持固定自拍外貌、固定角色参考图和可选的日程增强。
- LLM Tool：MaiBot 可在对话中自动调用生图，并继承当前群的模型、尺寸、画师串和会话开关。
- 英文标签直传：`/ad0` 跳过提示词 LLM，适合精确控制。
- 参考图：支持角色参考、画风参考、角色+画风和 i2i。
- 多模型与画师串：按会话热切换模型、尺寸和风格预设。
- 发送与撤回：支持普通直发、合并转发、自动撤回和手动精确撤回。
- 可选任务队列：限制全局与单会话并发，支持状态查询和取消。

## 生成模式

### 精准模式

普通 `/ad <描述>` 使用精准模式。LLM 必须忠实翻译用户条件，只在画面缺少必要信息时做克制补全：

- 没有动作或姿态时，补一个与主体和服装相容的自然动作。
- 没有场景或背景时，补一个可辨识的具体地点，以及最多一个能体现该地点的环境物件或特征。
- `indoors`、`outdoors`、`simple background` 等泛化标签不能单独代替具体场景。
- 用户明确指定白底、纯色背景、空白背景或无背景时，不再添加实体场景。
- 用户已经指定动作、场景、表情、视线或构图时，不重复编造同类内容。
- 不主动添加画师、媒介、独立画风、年份或质量词；用户明确要求时仍会翻译。

示例：

```text
/ad 雷姆
/ad 雷姆，站在神社门口
/ad 自拍，汉服
/ad 一名少女，白色背景
```

### 随机模式

只有命令开头带随机前缀时才使用随机模式。用户明确给出的角色、服装、动作、物品和其他条件会被固定，LLM 可以自由补充场景、构图、光线和氛围等内容。

支持以下写法：

```text
/ad 随机
/ad 随机，雷姆
/ad 随机 雷姆
/ad 随机自拍，汉服
/ad 随机，自拍，汉服
/ad random selfie, hanfu
```

`/ad 随机` 会生成完整的随机画面；`/ad 随机自拍` 会生成随机自拍。场景扩写由主提示词 LLM 一次完成。


### 英文标签直传

`/ad0 <tags>` 跳过提示词 LLM，直接把英文标签交给 Provider：

```text
/ad0 solo, 1girl, keqing (genshin impact), standing, looking at viewer
```

直传模式仍会叠加当前画师串和全局负面提示词，但不会自动拆分人物层。提示词预设 `/ad y` 同样保持扁平模式。


### Provider 映射

| 内部内容 | BestNAI / `novelai` | YesNAI |
|---|---|---|
| 全局正向 | `prompt` | 外层 `input` |
| 人物正向 | `characters[i].prompt` | `characterPrompts[i].prompt`、`v4_prompt` 人物 Caption |
| 全局负向 | `negative_prompt` | `parameters.negative_prompt`、`v4_negative_prompt` 基础 Caption |
| 人物负向 | `characters[i].negative_prompt` | `characterPrompts[i].uc`、`v4_negative_prompt` 人物 Caption |

人物正向和人物负向始终使用相同索引。YesNAI 即使人物负向为空，也会保留对应的空 Caption。画师串只进入全局正向，不会进入人物正向或人物负向。

结构化人物字段只对 NovelAI V4/V4.5 模型启用；其他模型自动使用扁平提示词。`/ad0` 和 `/ad y` 始终保持扁平流程。解析器兼容旧 v2/v3 的 `global/people` 数组，但 `output_format = "json"` 时不会接受只有 `prompt` 字段的旧 JSON 或纯文本输出。

## 自拍与日程增强

输入以“自拍”开头，或 LLM 明确输出自拍意图时，会进入自拍处理：

- `selfie_prompt_add` 可追加固定人物外貌标签。
- `selfie_ref_image` 可指定 `selfie_refs/` 中的固定角色参考图。
- `selfie_appearance_policy` 控制是否移除 LLM 擅自补充的外貌标签。
- 自拍补充只进入第一个人物层，并会清理与人物负向词直接冲突的标签。

`[prompt_generator].scene_llm_enabled = true` 表示启用自拍日程与场景增强：

- 普通自拍：读取当前日程，并把它作为必要动作和场景补全的参考。
- 随机自拍：读取当前日程，并在保留用户条件的前提下自由扩写。
- Tool 自拍：保留独立场景增强流程，可额外调用场景 LLM；失败时使用确定性场景映射兜底。


## NSFW 过滤

`/ad nsfw on|off` 是按会话保存的过滤开关，语义如下：

- `on`：启用安全过滤。自然语言请求使用 SFW 内容策略，成人或露骨要求会转换为安全版本；`/ad0` 和 `/ad y` 使用标签黑名单拦截。
- `off`：关闭插件内容限制。自然语言、直传标签和提示词预设均可传递成人、血腥或其他内容；实际结果仍受所选 LLM 和图像服务规则影响。

如果 `[plugin].force_forward_when_nsfw_off = true`，关闭过滤后会强制使用合并转发，即使会话原本选择了普通直发。

自定义 `prompt_template` 也会强制组合当前内容策略和精准/随机/Tool 策略，不能绕过会话开关。

## 参考图模式

参考图优先从当前消息附件或明确回复的消息中读取。最近聊天图片回退默认关闭，避免误用群内其他人的图片；确有需要时可设置：

```toml
[plugin]
allow_recent_image_fallback = true
```

| 模式 | 命令 | 说明 | 权限 |
|---|---|---|---|
| 角色参考 | `/ad r <描述>` | 参考角色外貌，LLM 不主动编写固有外貌 | 管理员 |
| 画风参考 | `/ad h <描述>` | 参考画风，并停用当前画师串以避免串味 | 管理员 |
| 角色+画风 | `/ad rh <描述>` | 同时参考角色和画风 | 管理员，且 Provider 必须支持 |
| i2i | `/ad t <描述>` | 按参考图进行图生图 | 所有人或管理员模式规则 |
| 直传参考 | `/ad0 r\|h\|rh\|t <tags>` | 跳过提示词 LLM | 权限同上 |
| 提示词预设 | `/ad y <名称>` | 引用图片并使用 `[styles]` 中的完整提示词 | 所有人或管理员模式规则 |

支持 PNG、JPEG、WebP。单张图片最多 20 MB，解码后总像素最多 16,777,216。`allow_recent_image_fallback = false` 时，请直接附图或明确回复图片。

## 命令列表

“会话权限”表示管理员模式关闭时所有人可用，管理员模式开启时只有 `[admin].admin_users` 可用。

| 命令 | 说明 | 权限 |
|---|---|---|
| `/ad help` | 显示帮助 | 所有人 |
| `/ad <描述>` | 精准翻译和必要补全 | 会话权限 |
| `/ad 随机[自拍] <条件>` | 保留条件并自由补充画面 | 会话权限 |
| `/ad0 <tags>` | 英文标签直传 | 会话权限 |
| `/ad t <描述>`、`/ad0 t <tags>` | i2i 图生图 | 会话权限 |
| `/ad y <名称>` | 提示词预设图生图 | 会话权限 |
| `/ad status` | 查看当前会话任务 | 会话权限，需启用队列 |
| `/ad cancel [任务ID\|all]` | 取消最近、指定或全部任务 | 会话权限，需启用队列 |
| `/ad reset` | 清除两种内容模式的连续绘图上下文 | 会话权限 |
| `/ad 撤回` | 撤回当前会话中本插件记录的图片 | 所有人 |
| `/ad r\|h\|rh\|hr <描述>` | 角色/画风参考 | 管理员 |
| `/ad0 r\|h\|rh\|hr <tags>` | 角色/画风参考标签直传 | 管理员 |
| `/ad m`、`/ad w <模型ID>` | 列出或切换模型 | 会话权限 |
| `/ad s <竖\|横\|方>` | 切换会话尺寸 | 管理员 |
| `/ad art [序号]` | 查看或切换画师串 | 管理员 |
| `/ad c on\|off` | 自动撤回开关 | 管理员 |
| `/ad nsfw [on\|off]` | 查询或切换 NSFW 过滤 | 管理员 |
| `/ad send [d\|f]` | 查询或切换直发/合并转发 | 管理员 |
| `/ad pt on\|off` | 提示词显示开关 | 会话权限 |
| `/ad on\|off` | 当前会话插件开关 | 管理员 |
| `/ad st\|sp` | 开启/关闭管理员模式 | 管理员 |

尺寸别名：`竖/竖图/v = 832x1216`、`横/横图/h = 1216x832`、`方/方图/s = 1024x1024`。


## 任务队列

队列由 `[queue]` 控制：

```toml
[queue]
enabled = true
max_concurrent = 2
max_concurrent_per_session = 1
max_queued = 20
per_session_limit = 3
history_ttl = 300
```

- `enabled = false`：任务直接启动，不限制并发，`/ad status` 和 `/ad cancel` 不可用。
- `enabled = true`：限制全局和单会话并发，并保持同一会话的连续任务顺序。
- 参考图排队时写入受限临时文件；任务完成、失败、取消或插件启动清理时会删除。
- 取消是协作式取消。远端已经开始生成时，不保证停止远端计费。
- 队列参数在 JobManager 创建时读取，修改后应重新加载插件。

## 发送与撤回

插件默认通过 MaiBot SDK 的适配器 API 发送和撤回图片。

| 模式 | 配置/命令 | 特点 |
|---|---|---|
| 普通直发 | `send_mode = "direct"` / `/ad send d` | 快，直接发送图片消息 |
| 合并转发 | `send_mode = "forward"` / `/ad send f` | 更隐蔽，但 QQ 构建转发消息通常更慢 |

自动撤回由 `[auto_recall]` 控制：

```toml
[auto_recall]
enabled = true
delay_seconds = 50
allowed_groups = []
```

`allowed_groups` 留空表示不限制；填写时使用 `平台:会话ID`，例如 `qq:123456789`。`/ad c on|off` 会覆盖当前会话配置，Tool 生图也遵守该状态。即使图片已经安排撤回，只要延时期间执行 `/ad c off`，撤回任务也会再次检查开关并跳过。

插件只撤回当前会话中由自身记录的图片消息 ID；`/ad 撤回` 不会扫描和删除其他插件或其他会话的消息。

### 可选 HTTP 直连

默认使用 SDK passthrough。适配器回执较慢、经常拿不到 `message_id` 时，可让插件直连本机 OneBot HTTP 服务：

```toml
[plugin]
use_http_direct = true
napcat_http_url = "http://127.0.0.1:5780"
napcat_http_token = ""
```

HTTP 直连只允许 `127.0.0.1`、`localhost` 或本机 IPv6 回环地址，拒绝外部地址。Token 只放在请求头中。NapCat/SnowLuma 的 HTTP 服务端口和 Token 必须与这里一致。

## 安装

### 环境要求

- MaiBot `1.0.0` 至 `1.4.0`（以插件清单声明为准）
- maibot-sdk `2.5.x`
- Python 环境可安装 [`requirements.txt`](./requirements.txt) 中的依赖
- 已启用 NapCat 或 SnowLuma 适配器

核心依赖：

```text
maibot-sdk>=2.5.0,<3.0.0
requests>=2.32.0,<3.0.0
aiohttp>=3.9.0,<4.0.0
Pillow>=10.0.0,<14.0.0
certifi>=2024.2.2,<2030.0.0
```

### 安装步骤

1. 将插件目录放入 MaiBot 的 `plugins/` 目录。
2. 安装 `requirements.txt` 中的依赖。
3. 在插件管理器中重新扫描并加载插件。
4. 配置管理员、提示词 LLM 和至少一个图像模型。

也可以通过插件管理命令添加父目录：

```text
/pm plugin add_dir <父目录路径>
/pm plugin rescan
/pm plugin load ai_draw_plugin
```

## 配置

插件支持 WebUI 配置。也可以直接编辑 [`config.toml`](./config.toml)。首次使用至少需要设置：

1. `[admin].admin_users`
2. `[prompt_generator].api_base`、`api_key` 和 `model_name`
3. `[models].default_model`
4. 对应 `[[models.entries]]` 的 `base_url`、`api_key`、`model` 和 `format`

最小示例：

```toml
[plugin]
enabled = true
send_mode = "direct"
force_forward_when_nsfw_off = true

[admin]
admin_users = ["你的QQ号"]
default_admin_mode = false

[prompt_generator]
model_name = "your-llm-model"
api_base = "https://your-llm.example.com"
api_key = "your-llm-key"
output_format = "json"
scene_llm_enabled = true
temperature = 0.2
max_tokens = 4000
prompt_template = ""
inherit_ttl = 3600

[prompt_show]
enabled = false
selfie_prompt_add = ""
negative_prompt_add = ""
selfie_ref_image = ""

[models]
default_model = "model1"

[[models.entries]]
id = "model1"
name = "BestNAI V4.5"
format = "bestnai"
base_url = "https://your-image-api.example.com"
api_key = "your-image-key"
model = "nai-diffusion-4-5-full"
endpoint = ""
sampler = "k_euler_ancestral"
steps = 28
scale = 5.0
cfg = 0.0
noise_schedule = "karras"
default_size = "832x1216"
size_preset = "竖图"
artist_preset = "无"
```

### Provider 格式

| `format` | 默认端点 | 说明 |
|---|---|---|
| `bestnai` | `/v1/chat/completions` | OpenAI Chat Completions 兼容的 BestNAI/NovelAI 接口 |
| `yesnai` | `/v1/nai/generate-image` | YesNovelAI business-api 原生 NAI 请求格式 |

端点留空时使用 Provider 默认值。YesNAI 未声明“角色+画风”组合参考能力，使用 `rh/hr` 前应确认当前 Provider 能力。

### 画师串与提示词预设

```toml
[artist_presets]

[[artist_presets.presets]]
name = "无"
prompt = ""

[[artist_presets.presets]]
name = "示例画师串"
prompt = "artist:example, masterpiece, best quality"

[styles]

[[styles.presets]]
name = "线描"
prompt = "line art, clean lines, white background"
```

模型通过 `artist_preset = "预设名称"` 选择默认画师串，运行中可用 `/ad art <序号>` 切换。模型内联的旧 `artist_presets` 仍兼容，并优先于全局 `[artist_presets]`。

`/ad y` 默认不叠加当前画师串；需要叠加时设置：

```toml
[plugin]
y_apply_artist_preset = true
```

### 自定义提示词模板

建议保持 `prompt_template = ""` 使用内置模板。如果自定义模板，应继续要求输出与上文一致的 `version=4` JSON，并至少保留 `<<USER_REQUEST>>`。

常用占位符：

- `<<USER_REQUEST>>`：当前用户请求
- `<<CUSTOM_SYSTEM_PROMPT>>`：`[custom_prompt].system_prompt`
- `<<CONTENT_POLICY>>`：当前 SFW/不限制内容策略
- `<<GENERATION_POLICY>>`：精准、随机或 Tool 策略
- `<<PREVIOUS_PROMPT>>`：连续绘图上下文
- `<<SELFIE_SCENE_CONTEXT>>`：自拍日程/场景上下文
- `<<SELFIE_HINT>>`：自拍规则
- `<<CHARACTER_REF_CONTEXT>>`：参考图模式约束
- `<<CURRENT_TIME_CONTEXT>>`：仅 Tool 模式使用的时间上下文

自定义模板未写 `<<CONTENT_POLICY>>` 或 `<<GENERATION_POLICY>>` 时，插件会自动在末尾追加对应策略。


## 常见问题

### `HTTP 429` 或“请求过于频繁”

这是图像服务限流，不是提示词流程错误。等待后重试，或降低队列并发。不要通过连续取消再提交来绕过并发限制。

### “提示词生成失败”

检查提示词 LLM 的 `api_base`、`api_key`、`model_name` 和 `max_tokens`。`output_format = "json"` 时，LLM 必须返回有效的 `global/people` 结构化 JSON；内置模板要求 `version=4`，解析器仍兼容旧 v2/v3 人物数组。正文为空且 reasoning 中也没有可解析结构、只有旧 `prompt` 字段、纯文本或损坏 JSON 时都会终止任务，不会继续调用图像 Provider。

### 自拍没有日程增强

确认 `scene_llm_enabled = true`，并确认日程插件已经加载且当前存在活动。没有日程插件时会正常降级，由主提示词 LLM完成必要补全。

### 参考图未找到

优先直接附图或回复目标图片。最近消息回退默认关闭；开启前应评估群聊中误取他人图片的风险。

### 画师串没有生效

先用 `/ad art` 查看当前模型可用预设和当前序号。画风参考 `h/rh/hr` 会主动停用画师串，以免画师串与参考画风互相干扰；`/ad y` 是否叠加画师串由 `y_apply_artist_preset` 决定。


## 隐私与安全

- 用户自然语言会发送给配置的提示词 LLM。
- 最终提示词以及启用参考模式时的参考图会发送给图像 Provider。
- 运行日志可能包含用户描述和最终提示词，分享前请先脱敏。
- 不要提交真实 API Key、适配器 Token、管理员账号、群白名单或个人自拍参考图。
- 外部 API 默认验证 TLS 证书；使用明文 HTTP 时插件会记录安全警告。
- HTTP 直连被限制为本机回环地址，避免把 Token 发往外部地址。
- QQ 消息撤回只删除平台消息，不能撤销已经发送给第三方 API 的数据。

## 项目结构

```text
ai_draw_plugin/
├── plugin.py                  # 配置模型、命令和 Tool 注册、插件生命周期
├── instance.py                # 插件实例管理
├── config.toml                # 默认配置
├── _manifest.json             # 插件元数据
├── components/
│   └── command.py             # 命令、生成策略与工作流
├── constants/
│   ├── constants.py           # 模型与尺寸别名
│   └── help_texts.py          # 内置帮助文本
├── core/
│   ├── generator.py           # Provider 调用、发送、参考图和撤回
│   ├── prompt_engine.py       # LLM 调用、V4 JSON 解析和标签处理
│   ├── prompt_types.py        # 结构化提示词不可变类型
│   ├── selfie_engine.py       # 自拍检测与外貌处理
│   ├── selfie_scene.py        # 日程和 Tool 自拍场景增强
│   ├── session_context.py     # 命令与 Tool 会话信息解析
│   ├── session_state.py       # 会话级运行状态
│   ├── job_manager.py         # 有界任务队列
│   ├── image_utils.py         # 图片验证和临时文件
│   └── rules/
│       └── prompt_rules.py    # 内容策略、精准/随机/Tool 模板
└── providers/
    ├── base.py                # Provider 基类和安全边界
    ├── bestnai.py             # BestNAI/NovelAI 兼容格式
    ├── yesnai.py              # YesNovelAI 原生格式
    └── capabilities.py        # Provider 能力声明
```

## 扩展 Provider

1. 在 `providers/` 中新建模块并继承 `BaseImageProvider`。
2. 实现 `generate()`，需要结构化提示词时在签名末尾接受 `structured_prompt=None`。
3. 在 `providers/__init__.py` 注册 `format`。
4. 在 `providers/capabilities.py` 声明尺寸、采样器和参考图能力。
5. 在 `config.toml` 中添加对应的 `[[models.entries]]`。

Provider 若不接受 `structured_prompt`，生成器会保持旧调用兼容，不会为了兼容而重复调用图像 API。

## 更新日志

### 2.4.5

- 优化结构化提示词生成的稳定性，在保留模型推理能力的同时降低空响应概率，并兼容不支持扩展参数的中转接口。
- 完善精准与随机模式的条件保留、必要场景补全和自拍日程使用边界。
- 角色参考与角色+画风参考模式改由参考图负责人物身份和固定外貌，提示词仅保留服装、动作、表情、场景等可变内容。

### 2.4.4

- 统一启用 NovelAI V4/V4.5 全局正向、人物正向与人物负向的结构化提示词，并适配 BestNAI 与 YesNAI。
- `/ad` 新增精准翻译与带条件的随机生成模式，在保留用户要求的同时按模式完成必要补全或自由扩展。
- 统一普通绘图、自拍与 LLM Tool 的结构化提示词流程，支持人物级负面约束与画师预设。

### 2.4.3

- 修复 `/ad c off` 对 LLM Tool 自动撤回不生效的问题，并在延时撤回前再次检查会话开关。
- 修复 LLM Tool 丢失会话上下文导致 `/ad s` 尺寸设置不生效的问题。
- 为 LLM Tool 增加 `portrait`、`landscape`、`square` 尺寸选择与合法值校验。

### 2.4.2

- 重写图片发送逻辑，统一发送时序与 HTTP 直连行为。

### 2.4.1

- 重写图片发送与自动撤回逻辑，修复慢响应场景下的发送误报与撤回失效。

### 2.4.0

- 新增可配置开关的有界任务队列、`/ad status`、`/ad cancel` 与卸载时安全清理。
- 新增连续绘图上下文与 `/ad reset`，同一会话默认串行执行以避免上下文竞争。
- 自动/手动撤回改用本插件消息 ID 账本，避免历史扫描误撤其他消息。
- 加固参考图下载、图片解码、Provider 参数边界、响应大小和 HTTP Session 生命周期。
- 补充配置发布与第三方 API 数据传输的隐私说明，避免误提交本机凭据和个人文件。
- 修复全局/会话开关未覆盖所有入口、随机自拍判定、参考模式串味及多处配置优先级问题。
