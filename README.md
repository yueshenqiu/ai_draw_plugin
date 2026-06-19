# AI Draw 图片生成插件

MaiBot 的 AI 绘图插件。通过 LLM 将自然语言描述转换为结构化提示词，再调用图像生成服务出图。支持多模型热切换、画师/风格预设、参考图模式、自拍模式、自动撤回等功能。

- **命令前缀**：`/ad`
- **SDK**：maibot-sdk 2.x
- **适配器**：兼容 NapCat 与 SnowLuma（均通过 SDK 标准接口）

## 功能特性

- **自然语言生图** — 用中文描述画面，LLM 自动翻译为英文 tag 提示词
- **直接 Tag 生图** — 跳过 LLM 翻译，直接用英文 tag 精确控制画面
- **参考图模式** — 提供参考图引导生成（角色参考 / 画风参考 / 角色+画风 / i2i 图生图）
- **自拍模式** — 基于预设角色外观生成自画像，支持日程 + LLM 场景增强
- **多模型热切换** — 运行时切换生成模型，无需重启
- **画师预设** — 多组画师风格 tag 组合，一键切换画风
- **风格预设** — 预设风格库（线描、油画、像素风等），引用图片二次创作
- **双发送模式** — 普通直发（快）/ 合并转发（隐蔽），可按会话热切换
- **自动撤回** — 生成的图片在可配置延时后自动精确撤回
- **NSFW 过滤** — 对直接 tag 与 LLM 产出的最终提示词双重扫描违规标签
- **管理员模式** — 管理员可控制插件开关、模型切换、参考模式等敏感命令

## 工作原理

```
用户命令 → 权限/开关校验 → LLM 生成提示词（或直传 tag）→ NSFW 过滤
        → Provider 调图像 API 出图
        → 经 SDK passthrough 发送（普通直发 / 合并转发）→ 延时自动精确撤回
```

发送、撤回、获取 bot 身份等动作均通过 MaiBot SDK 的 `ctx.api.call("adapter.napcat.*")`
passthrough 调用适配器完成，在 SDK 权限边界内执行，不直连 QQ 客户端的 HTTP 接口。
合并转发的精确撤回依赖适配器回传的真实 `message_id`。

> 参考图获取兼容 **NapCat** 与 **SnowLuma**：优先 SDK `message.get_by_id(include_binary_data=True)`
> 获取引用图片二进制，回退适配器 passthrough 的消息历史接口。

## 安装

### 依赖

```
maibot-sdk>=2.0.0
requests>=2.32.0
aiohttp>=3.9.0
Pillow>=10.0.0
certifi>=2024.2.2
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
2. **图像生成 API** — `[models.modelX]` 的 `base_url` 和 `api_key`
3. **LLM API** — `[prompt_generator]` 的 `api_base` 和 `api_key`（自然语言转提示词用）
4. **VLM API**（可选） — `[tagger]` 的 `api_base` 和 `api_key`（角色参考模式分析参考图用）

> 发送与撤回走 SDK passthrough，依赖已安装并启用的 NapCat 或 SnowLuma 适配器，
> 插件本身无需任何 HTTP 端口或 token 配置。

### 适配器要求

发送 / 撤回 / 身份获取通过适配器在 `adapter.napcat.*` 命名空间下暴露的公开 API 完成：

- **NapCat 适配器** — 原生提供全套 `adapter.napcat.message.*` 与 `adapter.napcat.system.get_login_info`。
- **SnowLuma 适配器** — 需 v1.9.12+ 或包含发送类公开 API 的版本（`send_group_msg`、
  `send_group_forward_msg`、`send_private_msg`、`delete_msg`、`get_login_info` 等）。

## 命令列表

所有命令以 `/ad` 为前缀。带 🔒 的命令仅管理员可用。

| 命令 | 说明 | 权限 |
|------|------|------|
| `/ad help` | 显示帮助 | 所有人 |
| `/ad <描述>` | 自然语言生图 | 所有人 |
| `/ad0 <tags>` | 英文 tag 直接生图 | 所有人 |
| `/ad t <描述>` | i2i 图生图（引用图片） | 所有人 |
| `/ad0 t <tags>` | i2i 图生图（直传 tag） | 所有人 |
| `/ad y <风格>` | 风格预设二次创作（引用图片） | 所有人 |
| `/ad 撤回` | 手动撤回最近图片 | 所有人 |
| `/ad r\|h\|rh\|hr <描述>` | 参考模式（角色/画风） | 🔒 |
| `/ad0 r\|h\|rh\|hr <tags>` | 参考模式（直传 tag） | 🔒 |
| `/ad s <尺寸>` | 切换尺寸（竖/横/方） | 🔒 |
| `/ad art <序号>` | 切换画师预设 | 🔒 |
| `/ad w <模型ID>` / `/ad m` | 切换 / 列出模型 | 🔒¹ |
| `/ad c on\|off` | 自动撤回开关 | 🔒 |
| `/ad nsfw on\|off` | NSFW 过滤开关 | 🔒 |
| `/ad send d\|f` | 发送方式（d=直发 / f=合并转发） | 🔒 |
| `/ad pt on\|off` | 提示词显示开关 | 🔒¹ |
| `/ad on\|off` | 插件开关（当前会话） | 🔒 |
| `/ad st\|sp` | 管理员模式开关 | 🔒 |

> ¹ 仅在管理员模式开启时限制。

**参考模式说明**：`r` 角色参考、`h` 画风参考、`rh`/`hr` 角色+画风、`t` i2i 图生图。前四种走 LLM/VLM 分析参考图，仅管理员可用；`t`（i2i）不限制。

此外插件注册了 `nai_web_draw` 工具，供 MaiBot 规划器在对话中自动调用生图。

## 发送模式

| 模式 | 速度 | 特点 |
|------|------|------|
| `direct` 普通直发 | 快（数秒） | 直接发图片消息 |
| `forward` 合并转发 | 慢（QQ 服务端构建 multimsg） | 包在转发卡片里，更隐蔽 |

- 用 `/ad send d` 或 `/ad send f` 按会话热切换，状态独立保存。
- `force_forward_when_nsfw_off = true` 时，当会话 NSFW 过滤关闭会**强制合并转发**（优先级最高）。
- 所有发出的图片都会在 `[auto_recall]` 配置的延时后自动精确撤回。

## NSFW 过滤

开启 `/ad nsfw on` 后，对两条生图路径都会扫描违规标签并拦截：

- **直接 tag 路径**（`/ad0`）— 扫描用户直传的英文 tag。
- **LLM 路径**（`/ad`）— 在 LLM 产出最终英文提示词后、送生图前扫描。

黑名单覆盖显式露骨词与软色情/性暗示标签。命中即拦截并提示，不出图。

## 配置说明

| 配置段 | 用途 |
|--------|------|
| `[plugin]` | 插件开关、发送模式 |
| `[admin]` | 管理员列表、默认管理员模式 |
| `[prompt_generator]` | 提示词生成 LLM 配置、自拍场景增强 |
| `[auto_recall]` | 自动撤回延时、白名单 |
| `[nsfw_filter]` | NSFW 过滤开关 |
| `[prompt_show]` | 提示词显示、自拍角色描述、负面提示词 |
| `[artist_presets]` | 画师预设 tag 组合 |
| `[styles]` | 风格预设列表（`/ad y` 用） |
| `[random_scene]` | 随机场景生成参数 |
| `[tagger]` | 角色参考模式的 VLM 分析配置 |
| `[models.modelX]` | 图像生成模型（API、参数、画师预设） |

> 所有外部 API 调用默认启用 TLS 证书验证（基于 certifi CA 包）。建议 `api_base` / `base_url`
> 使用 HTTPS；若配置为明文 HTTP，插件会告警提示 API Key 存在明文传输风险。

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
│   ├── http_client.py       # HTTP 会话管理（TLS 验证）
│   ├── image_utils.py       # 图片处理工具
│   ├── session_state.py     # 会话状态管理
│   └── rules/
│       └── prompt_rules.py  # 提示词模板规则
│
└── providers/
    ├── base.py              # Provider 抽象基类
    ├── bestnai.py           # BestNAI/NovelAI 实现
    └── capabilities.py      # Provider 能力声明
```

## 扩展 Provider

插件用策略模式管理图像生成服务商。接入新服务商：

1. 在 `providers/` 下新建模块，继承 `BaseImageProvider`
2. 实现 `generate()` 方法和 `capabilities` 属性
3. 在 `providers/__init__.py` 的注册表中添加映射
4. 在 `config.toml` 新增模型段，`format` 填对应 provider 标识


- **三花**想说的话：
- “https://github.com/Rabbit-Jia-Er ” 他的插件爆改而来，融百家之长的自用插件，有需要的用前请备份！！！后面还要加n多东西，早用早享受，晚用享bug

## 许可证

GPL-3.0-or-later
