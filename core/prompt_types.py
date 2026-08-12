# -*- coding: utf-8 -*-
"""提示词生成结果使用的不可变任务局部类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class PersonPrompt:
    """单个人物绑定的正向与负向标签。"""

    positive_tags: Tuple[str, ...]
    negative_tags: Tuple[str, ...]


@dataclass(frozen=True)
class StructuredPrompt:
    """可直接映射到 NovelAI V4/V4.5 分层提示词的内部表示。"""

    global_tags: Tuple[str, ...]
    people: Tuple[PersonPrompt, ...]
    format: str
    intent: str
    continuity: str


@dataclass(frozen=True)
class GeneratedPrompt:
    """同时保留兼容字符串与可选分层数据的 LLM 解析结果。"""

    flat_prompt: str
    structured_prompt: Optional[StructuredPrompt] = None
