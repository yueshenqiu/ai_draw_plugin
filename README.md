# AI Draw 图片生成插件

MaiBot 的 AI 绘图插件。通过 LLM 将自然语言描述转换为结构化提示词，再调用图像生成服务出图。支持有界任务队列、取消与状态查询、连续绘图、多模型热切换、参考图、自拍和精确撤回等功能。

- **命令前缀**：`/ad`
- **SDK**：maibot-sdk 2.x
- **适配器**：兼容 NapCat 与 SnowLuma（均通过 SDK 标准接口）

## 功能特性

- **自然语言生图** — 用中文描述画面，LLM 自动翻译为英文 tag 提示词
- **直接 Tag 生图** — 跳过 LLM 翻译，直接用英文 tag 精确控制画面
- **参考图模式** — 提供参考图引导生成（角色参考 / 画风参考 / 角色+画风 / i2i 图生图）；按模式自动隔离——角色参考不让 LLM 编角色固有外貌，画风参考不让 LLM 加画风标签，防止串味
- **自拍模式** — 基于预设角色外观生成自画像，支持日程 + LLM 场景增强
- **多模型热切换** — 运行时切换生成模型，无需重启
- **风格预设（画师串）** — 多组画师风格 tag 组合，一键切换画风（`/ad art`）
- **提示词预设** — 预设完整提示词库（线描、油画、像素风等），引用图片二次创作（`/ad y`）
- **任务队列与取消** — 全局并发、单会话串行和排队上限均可配置，支持 `/ad status` 与 `/ad cancel`
- **连续绘图** — 新请求可按语义继承上一张的设定；使用 `/ad reset` 随时清除上下文
- **双发送模式** — 普通直发（快）/ 合并转发（隐蔽），可按会话热切换
- **稳定发送与撤回** — 完善发送结果判断与图片定位流程，避免慢响应造成误报或漏撤
- **NSFW 过滤** — 自然语言路径从 LLM 模板约束，直传 tag / 提示词预设路径使用黑名单扫描
- **管理员模式** — 管理员可控制插件开关、模型切换、参考模式等敏感命令

## 工作原理

```
用户命令 → 权限/开关校验 → 有界任务队列 → LLM 生成提示词（或直传 tag）→ NSFW 过滤
        → Provider 调图像 API 出图
        → 经 SDK passthrough 发送（普通直发 / 合并转发）→ 延时自动精确撤回
```

发送、撤回、获取 bot 身份等动作默认通过 MaiBot SDK 的 `ctx.api.call("adapter.napcat.*")`
passthrough 调用适配器完成，在 SDK 权限边界内执行。合并转发的精确撤回依赖回传的真实
`message_id`。慢环境可选开启 HTTP 直连模式（仅本机、默认关闭，见下文「HTTP 直连模式」）。

> 参考图获取兼容 **NapCat** 与 **SnowLuma**：优先 SDK `message.get_by_id(include_binary_data=True)`
> 获取引用图片二进制，回退适配器 passthrough 的消息历史接口。
> 参考图仅接受 PNG、JPEG、WebP，单文件不超过 20 MB，解码后总像素不超过 16,777,216。

## 安装

### 依赖

```
maibot-sdk>=2.5.0,<3.0.0
requests>=2.32.0,<3.0.0
aiohttp>=3.9.0,<4.0.0
Pillow>=10.0.0,<14.0.0
certifi>=2024.2.2,<2030.0.0
```

### 可选的跨插件依赖

自拍场景增强（`[prompt_generator] scene_llm_enabled = true`）会尝试通过 `api.call`
读取日程插件 `xuqian13.autonomous-planning-plugin-v4` 的 `get_current_activity`，
用当前日程活动丰富自拍场景。**此依赖为可选**：未安装该插件时会静默降级，
不影响生图与其他功能。

### 安装方式

**方式一**：将 `ai_draw_plugin` 文件夹复制到 MaiBot 的 `plugins/` 目录下。

**方式二**：放在任意位置后，在插件管理中添加父目录：

```
/pm plugin add_dir <父目录路径>
/pm plugin rescan
/pm plugin load ai_draw_plugin
```

### 首次配置

打开 `config.toml`，填入以下关键信息：

1. **管理员 QQ 号** — `[admin]` 的 `admin_users`
2. **图像生成 API** — `[[models.entries]]` 中对应模型的 `base_url` 和 `api_key`
3. **LLM API** — `[prompt_generator]` 的 `api_base` 和 `api_key`（自然语言转提示词用）

> 发送与撤回走 SDK passthrough，依赖已安装并启用的 NapCat 或 SnowLuma 适配器，
> 插件本身无需任何 HTTP 端口或 token 配置。

### 适配器要求

发送 / 撤回 / 身份获取通过适配器在 `adapter.napcat.*` 命名空间下暴露的公开 API 完成：

- **NapCat 适配器** — 原生提供全套 `adapter.napcat.message.*` 与 `adapter.napcat.system.get_login_info`。
- **SnowLuma 适配器** — 需 v1.9.12+ 或包含发送类公开 API 的版本（`send_group_msg`、
  `send_group_forward_msg`、`send_private_msg`、`delete_msg`、`get_login_info` 等）。

### 可选：HTTP 直连模式（慢环境提速）

默认走 SDK passthrough（合规、无需任何端口配置）。但 passthrough 经适配器转发，
适配器对单次动作有超时限制（通常 10 秒）；在网络较慢的环境（如云服务器到 QQ
多媒体服务器较远）下，发送大图的回执可能超过该超时被误判失败，导致拿不到
`message_id` 而无法精确撤回。

此类环境可开启 HTTP 直连模式：插件直接请求 NapCat/SnowLuma 在**本机**开放的
OneBot HTTP 服务，超时预算更长（60 秒），绕开适配器超时。发送调用完成但未取得
精确消息 ID 时，插件会继续完成正常的发送与自动撤回流程，避免将慢响应误判为失败。

**安全说明**：地址被强制限制为本机回环（`127.0.0.1`/`localhost`），拒绝任何非本机
地址以防 SSRF；访问令牌仅放入 `Authorization` 请求头、不写入日志。**默认关闭**，
需自行评估后开启。

在 `config.toml` 的 `[plugin]` 段配置：

```toml
use_http_direct = true                       # 开启 HTTP 直连（默认 false）
napcat_http_url = "http://127.0.0.1:5780"    # 本机 HTTP 服务地址（端口按下方配置）
napcat_http_token = "你的token"               # 与 HTTP 服务器的 token 一致，无则留空
```

并在对应客户端开启 HTTP 服务器：

**NapCat** — 在 NapCat 的网络配置中新增一个「HTTP 服务器」：

```json
{
  "name": "ai-draw-http",
  "enable": true,
  "host": "127.0.0.1",
  "port": 5780,
  "token": "你的token",
  "messagePostFormat": "array"
}
```

**SnowLuma** — 在 `config/onebot_<QQ号>.json` 的 `httpServers` 中新增一项：

```json
{
  "name": "ai-draw-http",
  "host": "127.0.0.1",
  "port": 5780,
  "accessToken": "你的token",
  "messageFormat": "array",
  "reportSelfMessage": false,
  "path": "/"
}
```

`config.toml` 的 `napcat_http_url`/`napcat_http_token` 需与此处的 `port`/`token` 对应。
配置完重启客户端使端口生效。

## 命令列表

所有命令以 `/ad` 为前缀。带 🔒 的命令仅管理员可用。

| 命令 | 说明 | 权限 |
|------|------|------|
| `/ad help` | 显示帮助 | 所有人 |
| `/ad <描述>` | 自然语言生图 | 所有人 |
| `/ad 随机` / `/ad 随机自拍` | 生成尽量不重复的随机场景 | 所有人 |
| `/ad0 <tags>` | 英文 tag 直接生图 | 所有人 |
| `/ad t <描述>` | i2i 图生图（引用图片） | 所有人 |
| `/ad0 t <tags>` | i2i 图生图（直传 tag） | 所有人 |
| `/ad y <名称>` | 提示词预设二次创作（引用图片） | 所有人 |
| `/ad status` | 查看当前会话排队、运行和最近任务（需启用队列） | 所有人 |
| `/ad cancel [任务ID]` | 取消最近或指定任务；`all` 取消全部（需启用队列） | 所有人 |
| `/ad reset` | 清除连续绘图上下文 | 所有人 |
| `/ad 撤回` | 精确撤回本插件记录的图片消息 | 所有人 |
| `/ad r\|h\|rh\|hr <描述>` | 参考模式（角色/画风） | 🔒 |
| `/ad0 r\|h\|rh\|hr <tags>` | 参考模式（直传 tag） | 🔒 |
| `/ad s <尺寸>` | 切换尺寸（竖/横/方） | 🔒 |
| `/ad art <序号>` | 切换风格预设（画师串） | 🔒 |
| `/ad w <模型ID>` / `/ad m` | 切换 / 列出模型 | 🔒¹ |
| `/ad c on\|off` | 自动撤回开关 | 🔒 |
| `/ad nsfw on\|off` | NSFW 过滤开关 | 🔒 |
| `/ad send d\|f` | 发送方式（d=直发 / f=合并转发） | 🔒 |
| `/ad pt on\|off` | 提示词显示开关 | 🔒¹ |
| `/ad on\|off` | 插件开关（当前会话） | 🔒 |
| `/ad st\|sp` | 管理员模式开关 | 🔒 |

> ¹ 仅在管理员模式开启时限制。

**参考模式说明**：`r` 角色参考、`h` 画风参考、`rh`/`hr` 角色+画风、`t` i2i 图生图。四种都把参考图交给出图模型做 vibe transfer；其中**角色参考**会约束 LLM 不再自行编写角色的固有外貌（发色/发型/瞳色等交给参考图），**画风参考**会约束 LLM 不自行添加画风/画师标签（画风交给参考图）。服装、动作、场景与画面尺度仍以用户描述为准，尺度受 NSFW 开关分级约束。`r`/`h`/`rh`/`hr` 仅管理员可用；`t`（i2i）不限制。

此外插件注册了 `ai_draw` 工具，供 MaiBot 规划器在对话中自动调用生图。

### 任务队列与连续绘图

- `[queue].enabled = false` 时任务直接启动，不排队，也不应用队列的全局/单会话并发限制；`/ad status` 和 `/ad cancel` 不可用。
- `[queue].enabled = true` 时启用任务队列：默认全局最多同时生成 2 个任务，同一会话最多同时生成 1 个，保证连续绘图按提交顺序继承上下文。
- 参考图后台任务只保留受限临时文件路径，不在内存中长期持有大块 Base64；任务完成、失败或取消后自动删除。
- `/ad cancel` 默认只请求取消当前会话最近提交的活动任务；`/ad cancel <任务ID>` 精确取消；`/ad cancel all` 才会取消全部。
- 取消属于协作式取消：若远端服务已经开始生成，不保证能够停止远端计费；已经发送的图片请使用 `/ad 撤回`。
- BestNAI 的同步请求被取消后会保持“取消中”直到本地请求线程完成或超时，再释放并发槽，避免连续取消绕过队列并发上限。
- 队列参数位于 `[queue]`。热更新配置不会替换正在运行的队列，修改后请重载插件。

## 发送模式

| 模式 | 速度 | 特点 |
|------|------|------|
| `direct` 普通直发 | 快（数秒） | 直接发图片消息 |
| `forward` 合并转发 | 慢（QQ 服务端构建 multimsg） | 包在转发卡片里，更隐蔽 |

- 用 `/ad send d` 或 `/ad send f` 按会话热切换，状态独立保存。
- `force_forward_when_nsfw_off = true` 时，当会话 NSFW 过滤关闭会**强制合并转发**（优先级最高）。
- 自动撤回会根据发送结果选择合适的消息定位方式，并仅处理当前会话中的目标图片。

## NSFW 过滤

用 `/ad nsfw on|off` 按会话切换，对两类生图路径采用**不同**的约束方式：

- **LLM 自然语言路径**（`/ad`、`/ad r|h|rh|hr` 等）— 过滤开启时切换到 SFW 提示词模板，从**源头**约束 LLM 只产出穿着完整、仅靠姿态/光影营造氛围的内容；露下裆的构图会自动补安全裤。不做产出后二次黑名单拦截，避免误伤。
- **直传 tag / 提示词预设路径**（`/ad0`、`/ad0 t|r|h|rh`、`/ad y`）— 过滤开启时用**黑名单扫描**用户直传的英文 tag / 预设提示词，命中露骨或软色情标签即拦截、不出图并提示。

> 关闭过滤（`/ad nsfw off`）时两类路径都放行，可产出露骨内容；此时若 `force_forward_when_nsfw_off = true` 会强制走合并转发。

## 配置说明

| 配置段 | 用途 |
|--------|------|
| `[plugin]` | 插件开关、发送模式、HTTP 直连开关（`use_http_direct` / `napcat_http_url` / `napcat_http_token`） |
| `[admin]` | 管理员列表、默认管理员模式 |
| `[prompt_generator]` | 提示词生成 LLM 配置、自拍场景增强 |
| `[auto_recall]` | 自动撤回延时、白名单 |
| `[nsfw_filter]` | NSFW 过滤开关 |
| `[prompt_show]` | 提示词显示、自拍角色描述、负面提示词 |
| `[artist_presets]` | 风格预设（画师串）tag 组合 |
| `[styles]` | 提示词预设列表（`/ad y` 用） |
| `[random_scene]` | 随机场景生成参数 |
| `[queue]` | 是否启用队列、全局/单会话并发、排队上限、会话任务上限、历史状态保留时间 |
| `[[models.entries]]` | 图像生成模型（API、参数、风格预设/画师串） |

> 所有外部 API 调用默认启用 TLS 证书验证（基于 certifi CA 包）。建议 `api_base` / `base_url`
> 使用 HTTPS；若配置为明文 HTTP，插件会告警提示 API Key 存在明文传输风险。

## 隐私与配置安全

- 自然语言描述会发送给配置的提示词 LLM；最终提示词及启用参考模式时的参考图会发送给配置的生图服务。请只使用你信任的 API 服务商。
- MaiBot 运行日志可能包含用户描述、最终提示词和上游错误摘要。分享日志前请先检查并脱敏；日志应按敏感数据管理。
- 公开仓库中的 `config.toml` 必须保持 API Key、NapCat Token、管理员账号和群白名单为空或使用明显占位值。真实运行配置不要直接提交。
- `config_back/`、`selfie_refs/` 中的个人图片及运行时临时图片已由 `.gitignore` 排除；不要删除这些忽略规则后提交本地文件。
- QQ 消息撤回只会删除平台消息，不会撤销已经发送给第三方 API 的提示词或参考图。

## 项目结构

```
ai_draw_plugin/
├── plugin.py                # 插件主类，命令路由与生命周期
├── instance.py              # 单例实例管理
├── config.toml              # 配置文件
├── _manifest.json           # 插件元数据
├── requirements.txt         # 依赖声明
│
├── components/
│   └── command.py           # 命令处理器实现、NSFW 过滤
│
├── constants/
│   ├── constants.py         # 模型映射、尺寸别名
│   └── help_texts.py        # 帮助文本
│
├── core/
│   ├── generator.py         # 生图流程编排、passthrough 发送/撤回
│   ├── prompt_engine.py     # LLM 提示词生成与解析
│   ├── selfie_engine.py     # 自拍模式引擎
│   ├── selfie_scene.py      # 自拍场景增强（日程 + LLM）
│   ├── random_scene.py      # 随机场景生成
│   ├── job_manager.py       # 有界任务队列、状态与取消
│   ├── http_client.py       # HTTP 会话管理（TLS 验证）
│   ├── image_utils.py       # 图片处理工具
│   ├── session_state.py     # 会话状态管理
│   └── rules/
│       └── prompt_rules.py  # 提示词模板规则
│
└── providers/
    ├── base.py              # Provider 抽象基类
    ├── bestnai.py           # BestNAI/NovelAI 实现
    ├── yesnai.py            # YesNAI 实现
    └── capabilities.py      # Provider 能力声明
```

## 扩展 Provider

插件用策略模式管理图像生成服务商。接入新服务商：

1. 在 `providers/` 下新建模块，继承 `BaseImageProvider`
2. 实现 `generate()` 方法和 `capabilities` 属性
3. 在 `providers/__init__.py` 的注册表中添加映射
4. 在 `config.toml` 新增模型段，`format` 填对应 provider 标识

## 2.4.3 变更摘要

- 修复 `/ad c off` 对 LLM Tool 自动撤回不生效的问题，并在延时撤回前再次检查会话开关。
- 修复 LLM Tool 丢失会话上下文导致 `/ad s` 尺寸设置不生效的问题。
- 为 LLM Tool 增加 portrait、landscape、square 尺寸选择与合法值校验。

## 2.4.2 变更摘要

- 重写图片发送逻辑，统一发送时序与 HTTP 直连行为。

## 2.4.1 变更摘要

- 重写图片发送与自动撤回逻辑，修复慢响应场景下的发送误报与撤回失效。

## 2.4.0 变更摘要

- 新增可配置开关的有界任务队列、`/ad status`、`/ad cancel` 与卸载时安全清理。
- 新增连续绘图上下文与 `/ad reset`，同一会话默认串行执行以避免上下文竞争。
- 自动/手动撤回改用本插件消息 ID 账本，避免历史扫描误撤其他消息。
- 加固参考图下载、图片解码、Provider 参数边界、响应大小和 HTTP Session 生命周期。
- 补充配置发布与第三方 API 数据传输的隐私说明，避免误提交本机凭据和个人文件。
- 修复全局/会话开关未覆盖所有入口、随机自拍判定、参考模式串味及多处配置优先级问题。


- **AI Draw Contributors**想说的话：
- “https://github.com/saberlights/nai_draw_plugin ” 他的插件爆改而来，融百家之长的自用插件，有需要的用前请备份！！！后面还要加n多东西，早用早享受，晚用享bug

## 许可证

GPL-3.0-or-later
