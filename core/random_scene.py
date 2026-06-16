# -*- coding: utf-8 -*-
"""随机场景生成与去重（从 core/utils/random_scene_description.py 迁移）。"""

import re
from typing import List


_DIRECT_REPLACEMENTS = {
    "POV": "第一人称视角", "pov": "第一人称视角", "第一视角": "第一人称视角",
    "天台": "屋顶", "教室里": "教室", "房间里": "室内", "床上": "在床上",
    "俯视": "俯视镜头",
}

_GENERIC_TOKENS = {
    "裸体", "全裸", "半裸", "裸足", "失神", "脸红", "高潮",
    "室内", "户外", "屋顶", "教室", "浴室", "在床上", "镜子",
    "第一人称视角", "俯视镜头",
}

_SUBSTRING_CANONICAL_RULES = [
    (("阴道扩张", "小穴扩张", "肉穴扩张", "宫口扩张", "扩阴"), "扩张"),
    (("扩张器",), "窥器"),
    (("插管", "导尿", "导管"), "插管"),
    (("手术室", "实验室", "手术台", "检查台", "医疗床"), "医疗场景"),
    (("拘束衣", "拘束", "束缚", "捆绑", "绑缚", "龟甲缚", "吊缚", "镣铐"), "拘束"),
    (("地牢", "牢房", "地下室", "刑房"), "地牢"),
    (("露出", "公开露出", "路边露出"), "露出"),
    (("镜子自拍", "对镜自拍"), "镜子自拍"),
    (("手机自拍", "自拍"), "自拍"),
    (("触手", "史莱姆", "异种", "兽奸", "外星"), "异种"),
    (("口交", "口内射精", "口射"), "口交"),
    (("颜射", "射脸"), "颜射"),
    (("站立后入", "后入"), "后入"),
]

_CLUSTER_RULES = [
    (("扩张", "窥器", "插管", "医疗场景", "拘束", "地牢"), "医疗拘束"),
    (("露出", "公开", "围观", "电车", "户外"), "公开露出"),
    (("镜子自拍", "自拍", "手机自拍"), "自拍"),
    (("异种", "触手"), "异种"),
    (("群交", "轮奸", "多人"), "多人"),
    (("修女", "教堂", "告解室", "神像"), "宗教"),
    (("办公室", "秘书", "上司", "职场"), "职场"),
]


def _normalize_count_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    pair = re.fullmatch(r"(\d+)男(\d+)女", token)
    if pair:
        return f"{pair.group(1)}个男性 {pair.group(2)}个女性"
    female_only = re.fullmatch(r"(\d+)女", token)
    if female_only:
        return f"{female_only.group(1)}个女性"
    male_only = re.fullmatch(r"(\d+)男", token)
    if male_only:
        return f"{male_only.group(1)}个男性"
    return token


def normalize_random_scene_description(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("\n", " ")
    cleaned = re.sub(r"[，,、|/]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    normalized_tokens = []
    for token in cleaned.split(" "):
        token = _normalize_count_token(token)
        token = _DIRECT_REPLACEMENTS.get(token, token)
        token = token.strip()
        if not token:
            continue
        normalized_tokens.extend(part for part in token.split() if part)

    return " ".join(normalized_tokens).strip()


def split_random_scene_tokens(text: str) -> list:
    cleaned = normalize_random_scene_description(text)
    if not cleaned:
        return []
    return [token for token in cleaned.split(" ") if token]


def _canonicalize_scene_token(token: str) -> str:
    normalized = _DIRECT_REPLACEMENTS.get(token, token).strip()
    if not normalized:
        return ""
    for keywords, replacement in _SUBSTRING_CANONICAL_RULES:
        if any(keyword in normalized for keyword in keywords):
            return replacement
    return normalized


def build_random_scene_signature(text: str) -> set:
    signature = set()
    for token in split_random_scene_tokens(text):
        canonical = _canonicalize_scene_token(token)
        if not canonical:
            continue
        if not re.fullmatch(r"\d+个(?:男性|女性)", canonical) and canonical not in _GENERIC_TOKENS:
            signature.add(canonical)
        for keywords, cluster_name in _CLUSTER_RULES:
            if any(keyword in canonical for keyword in keywords):
                signature.add(f"簇:{cluster_name}")
                break
    return signature


def calculate_random_scene_repeat_score(candidate: str, recent_scene: str) -> float:
    candidate_text = normalize_random_scene_description(candidate)
    recent_text = normalize_random_scene_description(recent_scene)
    if not candidate_text or not recent_text:
        return 0.0
    if candidate_text == recent_text:
        return 1.0

    c_sig = build_random_scene_signature(candidate_text)
    r_sig = build_random_scene_signature(recent_text)
    if not c_sig or not r_sig:
        return 0.0

    overlap = c_sig & r_sig
    if not overlap:
        return 0.0

    jaccard = len(overlap) / len(c_sig | r_sig)
    coverage = len(overlap) / min(len(c_sig), len(r_sig))
    cluster_overlap = any(item.startswith("簇:") for item in overlap)

    score = max(jaccard, coverage)
    if cluster_overlap:
        score = max(score, min(1.0, coverage + 0.15))
    return score


def is_random_scene_too_similar(candidate: str, recent_scenes: list, threshold: float = 0.6) -> bool:
    return get_random_scene_similarity_score(candidate, recent_scenes) >= threshold


def get_random_scene_similarity_score(candidate: str, recent_scenes: list) -> float:
    candidate_text = normalize_random_scene_description(candidate)
    if not candidate_text:
        return 0.0
    return max(
        (calculate_random_scene_repeat_score(candidate_text, s) for s in recent_scenes if s),
        default=0.0,
    )
