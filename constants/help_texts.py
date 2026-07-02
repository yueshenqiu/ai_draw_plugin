# -*- coding: utf-8 -*-
"""帮助文本与命令说明"""

HELP_TEXT = (
    "🎨 AI Draw 图片生成插件命令列表：\n"
    "/ad help — 显示此帮助\n"
    "/ad on|off — 插件开关（当前会话）\n"
    "/ad <描述> — 自然语言生图\n"
    "/ad0 <tags> — 直接使用英文 tag 生图\n"
    "/ad r|h|rh|hr|t <描述> — 参考模式生图\n"
    "/ad w <模型ID> — 切换模型\n"
    "/ad m — 列出所有可用模型\n"
    "/ad s <尺寸> — 切换尺寸（竖/横/方）\n"
    "/ad art <序号> — 切换画师预设\n"
    "/ad c on|off — 自动撤回开关\n"
    "/ad nsfw <on|off> — NSFW 过滤开关\n"
    "/ad send <d|f> — 发送方式（d=直发快 / f=合并转发隐蔽）\n"
    "/ad pt <on|off> — 提示词显示开关\n"
    "/ad st|sp — 管理员模式开关\n"
    "/ad 撤回 — 手动撤回 AI绘图消息"
)

COMMAND_LIST = [
    ("/ad help", "帮助"),
    ("/ad on|off", "插件开关"),
    ("/ad <描述>", "自然语言生图"),
    ("/ad0 <tags>", "英文 tag 直接生图"),
    ("/ad r|h|rh|hr|t <描述>", "参考模式生图"),
    ("/ad w <模型ID>", "切换模型"),
    ("/ad m", "列出模型"),
    ("/ad s <尺寸>", "切换尺寸"),
    ("/ad art <序号>", "切换画师"),
    ("/ad c on|off", "自动撤回开关"),
    ("/ad nsfw <on|off>", "NSFW 过滤开关"),
    ("/ad send <d|f>", "发送方式（直发/合并转发）"),
    ("/ad pt <on|off>", "提示词显示开关"),
    ("/ad st|sp", "管理员模式开关"),
    ("/ad 撤回", "手动撤回"),
]
