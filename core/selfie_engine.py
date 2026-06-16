# -*- coding: utf-8 -*-
"""自拍引擎：检测、提示、合并（从 core/rules/selfie_rules.py 迁移）。"""

import re
from typing import List


SELFIE_TRIGGER_KEYWORDS = [
    "自拍", "selfie", "self-shot", "自己拍", "给自己拍", "自拍照",
    "镜子", "mirror", "照镜子", "镜中", "镜面", "浴室镜", "全身镜", "穿衣镜",
    "手机拍", "前置", "前置摄像头", "front camera", "举手机",
    "合照", "合影", "一起拍", "group selfie",
]

SELFIE_HINT_FOR_LLM = """
【自拍模式判定与规则】

## 自拍意图判定（必须执行）
请根据用户请求判断是否存在自拍/自画像意图。以下情况应判定为自拍：
- 直接提到自拍相关词：自拍、selfie、镜子、前置摄像头等
- 向 bot 索要照片/展示：看你的、给我看、秀一下、拍给我看、发张照片、show me 等
- 明确用第二人称指向 bot 本人出镜：「你穿黑丝」「你的腿」「你今天穿了什么」等
- 提到拍照动作：拍一张、照一张、合照、合影等
- 展示/观看请求 + 服饰/身体部位：「看看黑丝」「来张JK」「白丝给我看看」等

**【关键】区分自拍与普通画图：用户是想让 bot 展示自己（自拍），还是想让 bot 画一幅与 bot 无关的画（普通画图）？**
- 明确画图请求（如"画一个穿黑丝的女孩"）→ 普通画图，不是自拍
- 展示/观看请求（如"看看黑丝"、"来张黑丝照"）→ 自拍
- 明确指向 bot（如"你穿黑丝"、"你的腿"）→ 自拍

**如果判定不是自拍意图，忽略下方所有自拍规则。判定为自拍意图则必须应用以下全部规则：**

## 自拍硬规则
- 除非用户明确要求外貌，否则禁止输出任何外貌描述类标签
- 除非用户明确要求某个角色/作品/cosplay，否则禁止输出任何角色名、作品名
- 自拍默认是 bot 本人出镜，不是现成作品角色
- 如果用户明确要求 cosplay，使用 `角色tag (cosplay)` 标记形式

## 自拍类型选择规则
1. 根据用户描述匹配最合适的类型
2. 根据上下文推断
3. 随机选择增加多样性

## 自拍类型及对应标签
### 1. 手机前置自拍 → 必须: selfie, pov, looking at viewer
### 2. 镜子自拍 → 必须: mirror selfie, holding phone, looking at viewer
### 3. 高角度俯拍 → 必须: selfie, from above, pov, looking up
### 4. 低角度仰拍 → 必须: selfie, from below, pov, looking down
### 5. 合照自拍 → 必须: group selfie, pov, looking at viewer

## 通用规则
- 必须: looking at viewer（直视镜头）
- 必须: pov（第一人称视角，镜子自拍除外）
- 前置自拍时手机在画面外，不要添加 holding phone
- 不要使用 selfie stick、holding selfie stick
- 禁止生成角色外貌描述和角色/作品标签
"""

_SELFIE_OUTPUT_TAGS = [
    "selfie", "mirror selfie", "group selfie",
    "self-shot", "self shot",
]


def detect_selfie_mode(description: str) -> bool:
    description_lower = description.lower()
    for keyword in SELFIE_TRIGGER_KEYWORDS:
        if keyword.lower() in description_lower:
            return True
    return False


def detect_selfie_prefix(description: str) -> bool:
    """检查描述是否以自拍关键词开头（如 /ad 自拍，遮住裙子）。"""
    desc = description.strip().lower()
    for keyword in SELFIE_TRIGGER_KEYWORDS:
        if desc.startswith(keyword.lower()):
            return True
    return False


def detect_selfie_from_output(prompt: str) -> bool:
    prompt_lower = prompt.lower()
    return any(tag in prompt_lower for tag in _SELFIE_OUTPUT_TAGS)


def get_selfie_hint() -> str:
    return SELFIE_HINT_FOR_LLM


def merge_selfie_prompt(generated_prompt: str, selfie_prompt_add: str) -> str:
    """智能合并自拍提示词，配置中的角色特征优先。"""
    if not selfie_prompt_add:
        return generated_prompt

    add_tags = [tag.strip() for tag in selfie_prompt_add.split(",") if tag.strip()]
    if not add_tags:
        return generated_prompt

    def normalize_tag(tag: str) -> str:
        tag = tag.strip()
        tag = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", tag).strip()
        tag = re.sub(r"::\s*$", "", tag).strip()
        tag = tag.strip("{}[]() ")
        return re.sub(r"\s+", " ", tag.lower()).strip()

    def is_hair_related(tag: str) -> bool:
        core = normalize_tag(tag)
        hair_keywords = [
            " hair", "haired", "twintails", "twin tails", "ponytail",
            "braid", "pigtails", "bun", "bob cut", "hime cut", "bangs",
            "hair ornament", "hair ribbon", "hair bow",
        ]
        return any(kw in core for kw in hair_keywords)

    def is_eye_related(tag: str) -> bool:
        core = normalize_tag(tag)
        eye_colors = {"black", "brown", "blue", "red", "green", "purple", "orange",
                      "gray", "grey", "golden", "yellow", "pink", "aqua", "cyan"}
        if core in {"eyelashes", "long eyelashes", "heterochromia"}:
            return True
        match = re.search(r"\b([a-z]+)\s+eyes\b", core)
        if match and match.group(1) in eye_colors:
            return True
        return bool(re.search(r"\b[a-z]+-eyed\b", core))

    has_hair_anchor = any(is_hair_related(tag) for tag in add_tags)
    has_eye_anchor = any(is_eye_related(tag) for tag in add_tags)

    generated_tags = [
        tag.strip() for tag in generated_prompt.replace("\n", ",").split(",") if tag.strip()
    ]

    filtered_tags = []
    for tag in generated_tags:
        if has_hair_anchor and is_hair_related(tag):
            continue
        if has_eye_anchor and is_eye_related(tag):
            continue
        filtered_tags.append(tag)

    if len(filtered_tags) >= 2:
        prefix = ", ".join(filtered_tags[:2])
        suffix = ", ".join(filtered_tags[2:]) if len(filtered_tags) > 2 else ""
        if suffix:
            merged = f"{prefix}, {', '.join(add_tags)}, {suffix}"
        else:
            merged = f"{prefix}, {', '.join(add_tags)}"
    else:
        merged = f"{', '.join(add_tags)}, {', '.join(filtered_tags)}"

    return merged.strip(", ")
