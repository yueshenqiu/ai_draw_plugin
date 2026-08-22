# -*- coding: utf-8 -*-
"""通过 YesNovelAI Native 与高层 Images 接口调用 NovelAI 图片生成。"""

import asyncio
import base64
from dataclasses import replace
import io
import json
import math
import re
import ssl
import zipfile
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import certifi
import msgpack
from PIL import Image

from .base import (
    BaseImageProvider,
    ResponseLimitError,
    normalize_prompt_structure,
)
from .capabilities import YESNAI_CAPABILITIES

if TYPE_CHECKING:
    from ..core.prompt_types import StructuredPrompt


class YesNAIProvider(BaseImageProvider):
    """YesNovelAI 图片生成 Provider（Native/V1 双协议）"""

    default_endpoint = "/native/ai/generate-image"
    v1_images_endpoint = "/v1/images/generations"
    v1_nai_endpoint = "/v1/nai/generate-image"
    _NATIVE_ZIP_MAX_ENTRIES = 32
    _NATIVE_STREAM_MAX_BYTES = 256 * 1024 * 1024
    _NATIVE_STREAM_TOTAL_TIMEOUT = 600.0
    _STREAM_FALLBACK_HTTP_STATUSES = frozenset({404, 405, 406, 415, 501})
    # 最新接口的 2048/3,145,728 限制针对生成尺寸；参考图继续沿用原安全上限，
    # 由 YesNAI/NovelAI 的参考图流程执行其自身缩放。
    _REFERENCE_IMAGE_CAPABILITIES = replace(
        YESNAI_CAPABILITIES,
        max_dimension=4096,
        max_pixels=16_777_216,
    )

    _RESERVED_EXTRA_PARAMS = {
        "model", "action", "input", "prompt", "parameters", "size",
        "width", "height", "steps", "num_inference_steps", "n_samples",
        "negative_prompt", "sampler", "scale", "guidance_scale", "cfg",
        "cfg_rescale", "seed", "noise_schedule", "nocache", "image",
        "images", "img2img", "strength", "noise", "color_correct",
        "reference_image",
        "reference_images", "reference_image_multiple",
        "reference_strength_multiple", "reference_information_extracted_multiple",
        "director_reference_images", "director_reference_strength_values",
        "director_reference_secondary_strength_values",
        "director_reference_information_extracted",
        "director_reference_descriptions", "ref_image", "ref_mode",
        "ref_strength", "ref_fidelity",
        "characterPrompts", "v4_prompt", "v4_negative_prompt", "stream",
        "params_version", "use_coords", "legacy_v3_extend", "legacy_uc",
        "dynamic_thresholding", "controlnet_strength",
        "normalize_reference_strength_multiple",
        "deliberate_euler_ancestral_bug", "prefer_brownian",
    }

    # 匹配中/日/韩文 + 全角符号（NewAPI 仅允许英文，YesNovelAI 原生 NAI 同样要求英文 tag）
    _CJK_RE = re.compile(
        r'[一-鿿㐀-䶿＀-＇＊-Ｚ＼＾-ｚ｜～-￯　-〿'
        r'぀-ゟ゠-ヿ가-힯'
        r' -⁯⺀-⻿⼀-⿟㆐-㆟'
        r'㇀-㇯㈀-㋿㌀-㏿'
        r'︰-﹏︐-︟㄀-ㄯ]'
    )

    def __init__(self, logger, log_prefix: str = ""):
        super().__init__(logger, log_prefix)

    def _build_request_body(
        self,
        model_name: str,
        action: str,
        full_prompt: str,
        parameters: Dict[str, Any],
        negative_prompt: str,
        artist_prompt: str = "",
        custom_prompt_add: str = "",
        structured_prompt: Optional["StructuredPrompt"] = None,
        prompt_structure: str = "auto",
    ) -> Dict[str, Any]:
        request_parameters = dict(parameters)
        request_prompt = full_prompt
        uses_v4_prompt = self._uses_v4_prompt_envelope(
            model_name,
            action,
            prompt_structure,
        )
        if normalize_prompt_structure(prompt_structure) == "flat":
            # Defensive guard for direct/internal callers: flat mode must never
            # leak V4 character fields even if a stale StructuredPrompt is passed.
            structured_prompt = None
        if structured_prompt is not None and structured_prompt.people:
            layout = self.character_layout(len(structured_prompt.people))
            character_prompts = []
            positive_captions = []
            negative_captions = []
            for index, person in enumerate(structured_prompt.people):
                if layout:
                    x, y, _ = layout[index]
                    center = {"x": x, "y": y}
                else:
                    center = {"x": 0.5, "y": 0.5}
                positive = ", ".join(person.positive_tags)
                negative = ", ".join(person.negative_tags)
                character_prompts.append({
                    "prompt": positive,
                    "uc": negative,
                    "center": dict(center),
                    "enabled": True,
                })
                positive_captions.append({
                    "char_caption": positive,
                    "centers": [dict(center)],
                })
                negative_captions.append({
                    "char_caption": negative,
                    "centers": [dict(center)],
                })
            request_parameters["characterPrompts"] = character_prompts
            request_parameters["v4_prompt"] = {
                "caption": {
                    "base_caption": "",
                    "char_captions": positive_captions,
                },
                "use_coords": bool(layout),
                "use_order": True,
            }
            request_parameters["v4_negative_prompt"] = {
                "caption": {
                    "base_caption": negative_prompt or "",
                    "char_captions": negative_captions,
                },
                "legacy_uc": False,
            }
            request_prompt = ", ".join(part for part in (
                artist_prompt.strip(),
                custom_prompt_add.strip(),
                ", ".join(structured_prompt.global_tags).strip(),
            ) if part)

            request_parameters["v4_prompt"]["caption"]["base_caption"] = request_prompt

        # V5 文生图使用 flat 高层协议，但 Launcher 的 V4.5/V5 I2I 都使用
        # v4_prompt envelope。其他低层请求才使用 parameters.prompt。
        if not uses_v4_prompt:
            request_parameters["prompt"] = request_prompt

        if uses_v4_prompt:
            request_parameters.setdefault("v4_prompt", {
                "caption": {
                    "base_caption": request_prompt,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order": True,
            })
            request_parameters.setdefault("v4_negative_prompt", {
                "caption": {
                    "base_caption": negative_prompt or "",
                    "char_captions": [],
                },
                "legacy_uc": False,
            })

        return {
            "model": model_name,
            "action": action,
            "input": request_prompt,
            "parameters": request_parameters,
            # Launcher 1.7.2 对 generate/img2img/infill 都发送该字段。
            "use_new_shared_trial": True,
        }

    @classmethod
    def _resolve_request_url(
        cls,
        base_url: str,
        configured_endpoint: str = "",
    ) -> str:
        """解析 YesNAI Native 地址，兼容站点根地址与 Launcher Base URL。"""
        base_parts = urlsplit(str(base_url or "").strip())
        if base_parts.fragment:
            raise ValueError("YesNAI base_url 不能包含 URL fragment")
        base_path = base_parts.path.rstrip("/")
        base_has_native_prefix = base_path.lower().endswith("/native")

        endpoint_text = str(configured_endpoint or "").strip()
        endpoint_parts = urlsplit(endpoint_text)
        if endpoint_parts.scheme or endpoint_parts.netloc:
            raise ValueError("YesNAI endpoint 必须是相对路径")
        if endpoint_parts.fragment:
            raise ValueError("YesNAI endpoint 不能包含 URL fragment")
        endpoint_path = (
            f"/{endpoint_parts.path.strip('/')}"
            if endpoint_parts.path
            else ""
        )
        endpoint_lower = endpoint_path.lower()
        if endpoint_lower in {"", "/native"}:
            endpoint_path = (
                "/ai/generate-image"
                if base_has_native_prefix
                else cls.default_endpoint
            )
        elif endpoint_lower in {
            cls.v1_nai_endpoint,
            cls.v1_images_endpoint,
        }:
            # /v1/nai/generate-image 与 /v1/images/generations 都是站点根路径；
            # base_url 若沿用了 Launcher 的 /native 前缀，需要先剥掉它。
            if base_has_native_prefix:
                base_path = base_path[:-len("/native")]
        elif base_has_native_prefix and endpoint_lower.startswith("/native/"):
            base_path = base_path[:-len("/native")]
        if endpoint_path.lower().endswith("/ai/generate-image-stream"):
            endpoint_path = endpoint_path[:-len("-stream")]

        query = endpoint_parts.query or base_parts.query
        return urlunsplit((
            base_parts.scheme,
            base_parts.netloc,
            f"{base_path}{endpoint_path}",
            query,
            "",
        ))

    @classmethod
    def _resolve_v1_images_url(
        cls,
        base_url: str,
        configured_endpoint: str = "",
    ) -> str:
        """解析高层 /v1/images/generations 地址。"""
        endpoint_text = str(configured_endpoint or "").strip()
        if endpoint_text:
            return cls._resolve_request_url(base_url, endpoint_text)

        base_parts = urlsplit(str(base_url or "").strip())
        if base_parts.fragment:
            raise ValueError("YesNAI base_url 不能包含 URL fragment")
        base_path = base_parts.path.rstrip("/")
        if base_path.lower().endswith("/native"):
            base_path = base_path[:-len("/native")]
        return urlunsplit((
            base_parts.scheme,
            base_parts.netloc,
            f"{base_path}{cls.v1_images_endpoint}",
            base_parts.query,
            "",
        ))

    @classmethod
    def _resolve_transport(
        cls,
        base_url: str,
        configured_endpoint: str,
        prompt_structure: str,
        ref_mode: str = "",
    ) -> str:
        """根据显式端点、提示词协议和参考模式选择 wire protocol。

        Flat 文生图默认使用高层 Images API；Flat 图生图必须改走低层
        Native，因为高层端点不承载 ``parameters.image``。
        """
        endpoint_text = str(configured_endpoint or "").strip()
        if endpoint_text:
            endpoint_parts = urlsplit(endpoint_text)
            endpoint_path = (
                f"/{endpoint_parts.path.strip('/')}"
                if endpoint_parts.path else ""
            ).lower()
            if endpoint_path == cls.v1_images_endpoint:
                return "v1-images"
            if endpoint_path == cls.v1_nai_endpoint:
                return "native-json"
            if endpoint_path.endswith("/ai/generate-image-stream"):
                return "native-stream"
            if endpoint_path.endswith("/ai/generate-image"):
                return "native-zip"
            if endpoint_path in {"", "/native"}:
                return "native-stream"
            # 保持旧版自定义 Native endpoint 的兼容行为。
            return "native-zip"
        if (
            normalize_prompt_structure(prompt_structure) == "flat"
            and str(ref_mode or "").strip().lower() != "i2i"
        ):
            return "v1-images"
        # Native 默认与 Launcher 一致使用 MessagePack 流。显式配置 ZIP
        # 或 JSON 端点时仍严格遵循配置。
        return "native-stream"

    @classmethod
    def _resolve_stream_request_url(
        cls,
        base_url: str,
        configured_endpoint: str = "",
    ) -> str:
        """将兼容配置中的生成端点解析为 Native 流式端点。"""
        resolved = cls._resolve_request_url(base_url, configured_endpoint)
        parts = urlsplit(resolved)
        path = parts.path.rstrip("/")
        path_lower = path.lower()
        if path_lower.endswith("/ai/generate-image-stream"):
            return resolved
        if path_lower.endswith("/ai/generate-image"):
            return urlunsplit((
                parts.scheme,
                parts.netloc,
                f"{path}-stream",
                parts.query,
                parts.fragment,
            ))
        return resolved

    # ================================================================
    # Public API
    # ================================================================

    async def generate(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: Optional[str] = None,
        ref_image: str = "",
        ref_mode: str = "",
        structured_prompt: Optional["StructuredPrompt"] = None,
    ) -> Tuple[bool, str]:
        """调用 YesNovelAI Native 或高层 Images 接口生成图片（异步）。"""
        try:
            if not self.validate_config(model_config):
                return False, "模型配置不完整（缺少 base_url 或 model）"

            # 命令层通常已经传入小写模式，但 Provider 也可能被 Tool 或
            # 内部调用直接使用；统一规范化，避免 ``I2I`` 被当作文生图。
            ref_mode = str(ref_mode or "").strip().lower()

            base_url = (model_config.get("base_url") or "").rstrip('/')
            if base_url.startswith("http://"):
                self._logger.warning(
                    f"{self.log_prefix} (YesNAI) base_url 为明文 HTTP，"
                    f"API Token 将以明文传输，建议改用 HTTPS"
                )

            configured_endpoint = (
                model_config.get("endpoint")
                or model_config.get("nai_endpoint")
                or ""
            )

            api_key = model_config.get("api_key", "")
            token = api_key
            if isinstance(api_key, str) and api_key.lower().startswith("bearer "):
                token = api_key.split(" ", 1)[1]

            # ── 拼接完整提示词 ──
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            full_prompt = f"{custom_prompt_add}, {prompt}" if custom_prompt_add else prompt

            # 画师提示词（拼在 prompt 前面，符合 NAI 质量词优先）
            artist_prompt = (
                model_config.get("nai_artist_prompt")
                or model_config.get("artist_prompt")
            )
            if artist_prompt:
                full_prompt = f"{artist_prompt.strip()}, {full_prompt}"

            # 负面提示词
            negative_prompt = model_config.get("negative_prompt_add", "")

            # ── 读取生成参数 ──
            sampler = model_config.get("sampler")
            if sampler is None or sampler == "":
                sampler = "k_euler_ancestral"
            steps = model_config.get("steps")
            if steps is None or steps == "":
                steps = model_config.get("num_inference_steps")
            scale = model_config.get("scale")
            if scale is None or scale == "":
                scale = model_config.get("guidance_scale")
            cfg_value = model_config.get("cfg")
            if cfg_value is None or cfg_value == "":
                cfg_value = model_config.get("nai_cfg")
            noise_schedule = model_config.get("noise_schedule") or model_config.get("nai_noise_schedule") or "karras"
            nocache = model_config.get("nocache")
            if nocache is None:
                nocache = model_config.get("nai_nocache")
            extra_params = (
                model_config.get("extra_params")
                or model_config.get("nai_extra_params")
                or {}
            )
            model_name = (
                model_config.get("model")
                or model_config.get("default_model")
                or "nai-diffusion-4-5-full"
            )
            raw_prompt_structure = model_config.get("prompt_structure")
            prompt_structure = normalize_prompt_structure(
                raw_prompt_structure
            )
            if (
                raw_prompt_structure is not None
                and str(raw_prompt_structure).strip().lower()
                not in {"auto", "nai_v4", "flat"}
            ):
                self._logger.warning(
                    f"{self.log_prefix} (YesNAI) 未知 prompt_structure="
                    f"{raw_prompt_structure!r}，已回退 auto"
                )
            transport = self._resolve_transport(
                base_url, configured_endpoint, prompt_structure, ref_mode,
            )
            if transport == "v1-images" and prompt_structure != "flat":
                return False, (
                    "YesNAI /v1/images/generations 只支持 flat 提示词结构，"
                    "请将当前模型的 prompt_structure 改为 flat"
                )
            if (
                prompt_structure == "flat"
                and ref_mode
                and ref_mode != "i2i"
            ):
                return False, (
                    "当前模型已选择 flat 提示词结构，YesNAI flat 不支持角色参考或画风参考，"
                    "请切换 nai_v4 模式或移除该参考图"
                )
            if transport == "v1-images":
                request_url = self._resolve_v1_images_url(
                    base_url, configured_endpoint,
                )
            else:
                request_url = self._resolve_request_url(
                    base_url, configured_endpoint,
                )
            seed = model_config.get("seed", -1)

            # ── 尺寸解析 ──
            size_override = (
                model_config.get("size_preset")
                or model_config.get("nai_size")
            )
            final_size = size or size_override or model_config.get("default_size", "832x1216")
            resolved_size = self._parse_size(final_size)
            if resolved_size is None:
                return False, f"无效的图片尺寸: {str(final_size)[:50]}"
            width, height = resolved_size
            self.validate_dimensions(width, height, YESNAI_CAPABILITIES)
            try:
                sampler = self.validate_sampler(sampler, YESNAI_CAPABILITIES)
            except ValueError as exc:
                return False, str(exc)

            # ── 安全清理 CJK 字符 ──
            full_prompt = self._sanitize_prompt(full_prompt)
            negative_prompt = self._sanitize_prompt(negative_prompt)
            custom_prompt_add = self._sanitize_prompt(custom_prompt_add)
            artist_prompt = self._sanitize_prompt(artist_prompt or "")
            if prompt_structure == "flat":
                structured_prompt = None
            else:
                structured_prompt = self._sanitize_structured_prompt(
                    structured_prompt,
                    model_name,
                    prompt_structure=prompt_structure,
                )

            if prompt_structure == "nai_v4" and not self._is_novelai_v4_model(model_name):
                return False, (
                    f"模型 {model_name} 不支持 nai_v4 提示词结构，"
                    "请改用 flat 或选择 V4/V4.5 模型"
                )
            if (
                ref_mode in {"character", "style", "character&style"}
                and "nai-diffusion-4-5" not in str(model_name).strip().lower()
            ):
                return False, (
                    f"YesNAI 精准参考仅支持 V4.5 基础或 Inpainting 模型，当前为 {model_name}"
                )

            steps = self.bounded_int(
                steps,
                default=28,
                minimum=YESNAI_CAPABILITIES.min_steps,
                maximum=YESNAI_CAPABILITIES.max_steps,
                name="steps",
            )
            scale = self.bounded_float(
                scale,
                default=5.0,
                minimum=YESNAI_CAPABILITIES.min_scale,
                maximum=YESNAI_CAPABILITIES.max_scale,
                name="scale",
            )
            cfg_value = self.bounded_float(
                cfg_value,
                default=None,
                minimum=YESNAI_CAPABILITIES.min_cfg_rescale,
                maximum=YESNAI_CAPABILITIES.max_cfg_rescale,
                name="cfg_rescale",
            )

            # ── 参考图参数 ──
            ref_fidelity = self.bounded_float(
                model_config.get("ref_fidelity", 0.5),
                default=0.5,
                minimum=YESNAI_CAPABILITIES.min_reference_strength,
                maximum=YESNAI_CAPABILITIES.max_reference_strength,
                name="ref_fidelity",
            )
            ref_strength = self.bounded_float(
                model_config.get("ref_strength", 1.0),
                default=1.0,
                minimum=YESNAI_CAPABILITIES.min_reference_strength,
                maximum=YESNAI_CAPABILITIES.max_reference_strength,
                name="ref_strength",
            )
            i2i_strength = self.bounded_float(
                model_config.get("i2i_strength", 0.65),
                default=0.65,
                minimum=0.0,
                maximum=1.0,
                name="i2i_strength",
            )
            i2i_noise = self.bounded_float(
                model_config.get("i2i_noise", 0.1),
                default=0.1,
                minimum=0.0,
                maximum=1.0,
                name="i2i_noise",
            )
            if ref_image and ref_mode:
                valid, normalized_ref, _ = (
                    self.normalize_and_validate_base64_image(
                        ref_image,
                        capabilities=self._REFERENCE_IMAGE_CAPABILITIES,
                        field_name="参考图",
                    )
                )
                if not valid:
                    return False, normalized_ref
                ref_image = normalized_ref
                if ref_mode == "i2i":
                    ok, resized_ref, _, resize_error = (
                        self.normalize_i2i_base64_image(
                            ref_image,
                            width,
                            height,
                            self._REFERENCE_IMAGE_CAPABILITIES,
                            field_name="图生图参考图",
                        )
                    )
                    if not ok:
                        return False, resize_error
                    ref_image = resized_ref

            action, ref_extra = self._build_ref_params(
                ref_image=ref_image,
                ref_mode=ref_mode,
                ref_strength=ref_strength,
                ref_fidelity=ref_fidelity,
                i2i_strength=i2i_strength,
                i2i_noise=i2i_noise,
            )

            # ── 构建 parameters ──
            parameters: Dict[str, Any] = {
                "width": width,
                "height": height,
                "steps": steps,
                "n_samples": 1,
                "sampler": sampler,
                "scale": scale,
                "negative_prompt": negative_prompt or "",
            }
            uses_v4_prompt = self._uses_v4_prompt_envelope(
                model_name,
                action,
                prompt_structure,
            )
            if action == "img2img" and not uses_v4_prompt:
                # YesNAI 的低层 I2I 示例要求非 V4/flat prompt 同时位于
                # parameters.prompt；顶层 input 仍保留作为统一 envelope。
                parameters["prompt"] = full_prompt
            if uses_v4_prompt:
                # V4/V4.5 and V5 I2I share Launcher's V4+ low-level contract.
                # V5 flat text-to-image exits through /v1/images before this
                # body is sent, so its existing high-level request is unchanged.
                parameters.update({
                    "params_version": 3,
                    "use_coords": False,
                    "legacy_v3_extend": False,
                    "legacy_uc": False,
                    "dynamic_thresholding": False,
                    "controlnet_strength": 1.0,
                    "normalize_reference_strength_multiple": True,
                    "deliberate_euler_ancestral_bug": False,
                    "prefer_brownian": True,
                })

            seed = self.bounded_int(
                seed,
                default=-1,
                minimum=-1,
                maximum=4_294_967_295,
                name="seed",
            )

            if transport == "v1-images":
                if ref_image or ref_mode:
                    return False, (
                        "当前 flat 模式使用 YesNAI /v1/images/generations，"
                        "该端点不支持参考图或图生图"
                    )
                return await self._generate_v1_images_request(
                    url=request_url,
                    token=token,
                    model_config=model_config,
                    model_name=model_name,
                    prompt=full_prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    steps=steps,
                    sampler=sampler,
                    scale=scale,
                    seed=seed,
                    noise_schedule=noise_schedule,
                    cfg_value=cfg_value,
                    nocache=nocache,
                    extra_params=extra_params,
                )

            if seed >= 0:
                parameters["seed"] = seed

            if noise_schedule:
                parameters["noise_schedule"] = str(noise_schedule)

            if cfg_value is not None:
                parameters["cfg_rescale"] = cfg_value

            if nocache is not None:
                parameters["nocache"] = bool(nocache)

            # 扩展参数不能覆盖已验证的核心字段或参考图字段。
            parameters.update(self.filter_extra_params(
                extra_params,
                self._RESERVED_EXTRA_PARAMS,
                "YesNAI",
            ))
            if ref_extra:
                parameters.update(ref_extra)

            body = self._build_request_body(
                model_name=model_name,
                action=action,
                full_prompt=full_prompt,
                parameters=parameters,
                negative_prompt=negative_prompt,
                artist_prompt=artist_prompt,
                custom_prompt_add=custom_prompt_add,
                structured_prompt=structured_prompt,
                prompt_structure=prompt_structure,
            )

            # Native 默认保留旧版已验证的流式优先链路；显式 ZIP、JSON
            # 端点则严格按用户配置发送，不把端点静默改写成另一种协议。
            stream_body = dict(body)
            stream_parameters = dict(body["parameters"])
            stream_parameters["stream"] = "msgpack"
            stream_body["parameters"] = stream_parameters

            request_headers = {
                "Content-Type": "application/json",
                "Accept": (
                    "application/json"
                    if transport == "native-json"
                    else "application/x-msgpack"
                ),
            }
            if token:
                request_headers["Authorization"] = f"Bearer {token}"

            stream_url = self._resolve_stream_request_url(
                base_url, configured_endpoint,
            )

            self._logger.info(
                f"{self.log_prefix} (YesNAI) 请求: model={model_name} "
                f"action={action} size={width}x{height} steps={steps} "
                f"prompt_structure={prompt_structure} "
                f"wire_prompt={'v4' if uses_v4_prompt else 'flat'} "
                f"transport={transport}"
            )

            # ── 发送请求 ──
            timeout_seconds = self.bounded_float(
                model_config.get("timeout_seconds"),
                default=120.0,
                minimum=1.0,
                maximum=600.0,
                name="timeout_seconds",
            )
            proxy_value = model_config.get("proxy")
            if proxy_value is not None and not isinstance(proxy_value, str):
                return False, "代理地址必须是字符串"
            proxy = (proxy_value or "").strip() or None
            if proxy:
                try:
                    parsed_proxy = urlsplit(proxy)
                    proxy_hostname = parsed_proxy.hostname
                except ValueError:
                    return False, "代理地址必须是有效的 HTTP(S) URL"
                if parsed_proxy.scheme.lower() not in {"http", "https"} or not proxy_hostname:
                    return False, "代理地址必须是有效的 HTTP(S) URL"

            try:
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                connector = aiohttp.TCPConnector(ssl=ssl_context)
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=self._NATIVE_STREAM_TOTAL_TIMEOUT,
                        connect=min(timeout_seconds, 30.0),
                        sock_read=timeout_seconds,
                    ),
                    trust_env=False,
                    connector=connector,
                ) as session:
                    if transport == "native-json":
                        async with session.post(
                            request_url,
                            headers=request_headers,
                            json=body,
                            proxy=proxy,
                            allow_redirects=False,
                        ) as response:
                            raw_response = await self._read_limited_response(
                                response,
                                YESNAI_CAPABILITIES.max_response_bytes,
                            )
                            if 300 <= response.status < 400:
                                return False, (
                                    f"HTTP {response.status}: 接口发生重定向，请检查 base_url"
                                )
                            if response.status < 200 or response.status >= 300:
                                text = raw_response.decode("utf-8-sig", errors="replace")
                                return False, self._format_http_error(
                                    response.status, text,
                                )
                            return self._parse_success_response(raw_response)

                    if transport == "native-zip":
                        # 显式 ZIP 端点必须直连，不能先探测流式端点。
                        fallback_headers = {
                            "Content-Type": "application/json",
                            "Accept": "application/zip, application/json",
                        }
                        if token:
                            fallback_headers["Authorization"] = f"Bearer {token}"
                        async with session.post(
                            request_url,
                            headers=fallback_headers,
                            json=body,
                            proxy=proxy,
                            allow_redirects=False,
                        ) as response:
                            raw_response = await self._read_limited_response(
                                response,
                                YESNAI_CAPABILITIES.max_response_bytes,
                            )
                            if 300 <= response.status < 400:
                                return False, (
                                    f"HTTP {response.status}: 接口发生重定向，请检查 base_url"
                                )
                            if response.status < 200 or response.status >= 300:
                                text = raw_response.decode("utf-8-sig", errors="replace")
                                return False, self._format_http_error(
                                    response.status, text,
                                )
                            return self._parse_success_response(raw_response)

                    # Native 流式端点不可用时，按旧版规则回退到 ZIP；
                    # 只有明确表示不支持流式的状态才重试，避免把真实的
                    # 上游 500 当成可安全重复的请求。
                    stream_headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/x-msgpack",
                    }
                    if token:
                        stream_headers["Authorization"] = f"Bearer {token}"
                    async with session.post(
                        stream_url,
                        headers=stream_headers,
                        json=stream_body,
                        proxy=proxy,
                        allow_redirects=False,
                    ) as response:
                        if 300 <= response.status < 400:
                            location = response.headers.get("location", "")
                            self._logger.error(
                                f"{self.log_prefix} (YesNAI) 拒绝重定向: "
                                f"status={response.status}, location={location[:200]}"
                            )
                            return False, f"HTTP {response.status}: 接口发生重定向，请检查 base_url"

                        if response.status < 200 or response.status >= 300:
                            raw_response = await self._read_limited_response(
                                response,
                                YESNAI_CAPABILITIES.max_response_bytes,
                            )
                            text = raw_response.decode("utf-8-sig", errors="replace")
                            if not self._streaming_is_unsupported(
                                response.status, text,
                            ):
                                return False, self._format_http_error(
                                    response.status, text,
                                )
                            self._logger.warning(
                                f"{self.log_prefix} (YesNAI) Native 流式端点不可用，"
                                f"回退 ZIP: status={response.status}"
                            )
                        else:
                            return await self._read_native_stream_response(response)

                    fallback_headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/zip, application/json",
                    }
                    if token:
                        fallback_headers["Authorization"] = f"Bearer {token}"
                    async with session.post(
                        request_url,
                        headers=fallback_headers,
                        json=body,
                        proxy=proxy,
                        allow_redirects=False,
                    ) as response:
                        raw_response = await self._read_limited_response(
                            response,
                            YESNAI_CAPABILITIES.max_response_bytes,
                        )
                        if 300 <= response.status < 400:
                            return False, (
                                f"HTTP {response.status}: 接口发生重定向，请检查 base_url"
                            )
                        if response.status < 200 or response.status >= 300:
                            text = raw_response.decode("utf-8-sig", errors="replace")
                            return False, self._format_http_error(
                                response.status, text,
                            )
                        return self._parse_success_response(raw_response)
            except ResponseLimitError as exc:
                return False, str(exc)
            except (asyncio.TimeoutError, TimeoutError):
                return False, (
                    f"收图超时（连续 {timeout_seconds}s 未收到数据，"
                    f"或单次请求时长超过 {self._NATIVE_STREAM_TOTAL_TIMEOUT}s）"
                )
            except aiohttp.ClientError as exc:
                return False, f"网络错误: {type(exc).__name__}: {exc}"
            except Exception as exc:
                self._logger.error(f"{self.log_prefix} (YesNAI) 请求异常: {exc!r}", exc_info=True)
                return False, f"请求失败: {str(exc)[:100]}"

        except Exception as e:
            self._logger.error(f"{self.log_prefix} (YesNAI) 未知异常: {e!r}", exc_info=True)
            return False, f"YesNAI 接口请求失败: {str(e)[:100]}"

    async def _generate_v1_images_request(
        self,
        *,
        url: str,
        token: str,
        model_config: Dict[str, Any],
        model_name: str,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        sampler: str,
        scale: Optional[float],
        seed: int,
        noise_schedule: str,
        cfg_value: Optional[float],
        nocache: Any,
        extra_params: Any,
    ) -> Tuple[bool, str]:
        """调用 YesNAI 高层 Images API（flat/V5 路径）。"""
        nai: Dict[str, Any] = {
            "steps": steps,
            "sampler": sampler,
            "negative_prompt": negative_prompt or "",
            "noise_schedule": str(noise_schedule or "karras"),
        }
        if scale is not None:
            nai["scale"] = scale
        if seed >= 0:
            nai["seed"] = seed
        ignored_fields = []
        if cfg_value not in (None, 0, 0.0):
            ignored_fields.append("cfg_rescale")
        if nocache is not None:
            ignored_fields.append("nocache")
        if isinstance(extra_params, dict):
            ignored_fields.extend(
                str(key) for key in extra_params
                if str(key) not in {"steps", "sampler", "scale", "seed", "negative_prompt", "noise_schedule"}
            )
        if ignored_fields:
            self._logger.warning(
                f"{self.log_prefix} (YesNAI) /v1/images/generations "
                "未透传 Native 专用参数: "
                + ", ".join(dict.fromkeys(ignored_fields))
            )
        body = {
            "model": model_name,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "n": 1,
            "response_format": "b64_json",
            "nai": nai,
        }

        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            request_headers["Authorization"] = f"Bearer {token}"

        proxy_value = model_config.get("proxy")
        if proxy_value is not None and not isinstance(proxy_value, str):
            return False, "代理地址必须是字符串"
        proxy = (proxy_value or "").strip() or None
        if proxy:
            try:
                parsed_proxy = urlsplit(proxy)
                proxy_hostname = parsed_proxy.hostname
            except ValueError:
                return False, "代理地址必须是有效的 HTTP(S) URL"
            if parsed_proxy.scheme.lower() not in {"http", "https"} or not proxy_hostname:
                return False, "代理地址必须是有效的 HTTP(S) URL"

        timeout_seconds = self.bounded_float(
            model_config.get("timeout_seconds"),
            default=120.0,
            minimum=1.0,
            maximum=600.0,
            name="timeout_seconds",
        )
        self._logger.info(
            f"{self.log_prefix} (YesNAI) 请求: model={model_name} "
            f"size={width}x{height} steps={steps} "
            f"prompt_structure=flat transport=v1-images"
        )
        return await self._post_image_request(
            url=url,
            headers=request_headers,
            body=body,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
            transport="v1-images",
        )

    async def _post_image_request(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
        proxy: Optional[str],
        timeout_seconds: float,
        transport: str,
    ) -> Tuple[bool, str]:
        """发送非流式图片请求并统一解析 ZIP/JSON 响应。"""
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self._NATIVE_STREAM_TOTAL_TIMEOUT,
                    connect=min(timeout_seconds, 30.0),
                    sock_read=timeout_seconds,
                ),
                trust_env=False,
                connector=connector,
            ) as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=body,
                    proxy=proxy,
                    allow_redirects=False,
                ) as response:
                    raw_response = await self._read_limited_response(
                        response,
                        YESNAI_CAPABILITIES.max_response_bytes,
                    )
                    if 300 <= response.status < 400:
                        return False, (
                            f"HTTP {response.status}: 接口发生重定向，请检查 base_url"
                        )
                    if response.status < 200 or response.status >= 300:
                        text = raw_response.decode("utf-8-sig", errors="replace")
                        return False, self._format_http_error(
                            response.status, text,
                        )
                    return self._parse_success_response(raw_response)
        except ResponseLimitError as exc:
            return False, str(exc)
        except (asyncio.TimeoutError, TimeoutError):
            return False, (
                f"收图超时（连续 {timeout_seconds}s 未收到数据，"
                f"或单次请求时长超过 {self._NATIVE_STREAM_TOTAL_TIMEOUT}s）"
            )
        except aiohttp.ClientError as exc:
            return False, f"网络错误: {type(exc).__name__}: {exc}"
        except Exception as exc:
            self._logger.error(
                f"{self.log_prefix} (YesNAI) {transport} 请求异常: {exc!r}",
                exc_info=True,
            )
            return False, f"请求失败: {str(exc)[:100]}"

    @staticmethod
    async def _read_limited_response(
        response: aiohttp.ClientResponse,
        max_bytes: int,
    ) -> bytes:
        """流式读取 aiohttp 响应，并限制解压后的实际响应大小。"""
        if response.content_length is not None and response.content_length > max_bytes:
            raise ResponseLimitError(
                f"API 响应超过 {max_bytes // (1024 * 1024)} MiB 限制"
            )

        content = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ResponseLimitError(
                    f"API 响应超过 {max_bytes // (1024 * 1024)} MiB 限制"
                )
        return bytes(content)

    async def _read_native_stream_response(
        self,
        response: aiohttp.ClientResponse,
    ) -> Tuple[bool, str]:
        """读取 Native 流，收到 final 帧后立即返回最终图片。"""
        content_type = str(response.headers.get("Content-Type", "")).lower()
        buffer = bytearray()
        mode = ""
        total_received = 0
        decoded_frames = 0

        async for chunk in response.content.iter_chunked(64 * 1024):
            if not chunk:
                continue
            total_received += len(chunk)
            if total_received > self._NATIVE_STREAM_MAX_BYTES:
                raise ResponseLimitError("Native 流累计数据超过 256 MiB 限制")
            buffer.extend(chunk)

            if not mode:
                mode = self._detect_native_stream_mode(buffer, content_type)
                if not mode:
                    if len(buffer) > YESNAI_CAPABILITIES.max_response_bytes:
                        raise ResponseLimitError("Native 流单帧超过 48 MiB 限制")
                    continue

            buffer_limit = YESNAI_CAPABILITIES.max_response_bytes
            if mode == "msgpack":
                buffer_limit += 4
            if len(buffer) > buffer_limit:
                raise ResponseLimitError("Native 流单帧超过 48 MiB 限制")

            if mode == "raw":
                continue

            if mode == "sse":
                while True:
                    block = self._pop_sse_block(buffer)
                    if block is None:
                        break
                    event = self._decode_sse_event(block)
                    if event is None:
                        continue
                    decoded_frames += 1
                    result = self._parse_native_stream_event(event)
                    if result is not None:
                        return result
                continue

            while len(buffer) >= 4:
                frame_length = int.from_bytes(buffer[:4], "big", signed=False)
                if (
                    frame_length <= 0
                    or frame_length > YESNAI_CAPABILITIES.max_response_bytes
                ):
                    return False, "Native MessagePack 帧长度无效"
                if len(buffer) < 4 + frame_length:
                    break

                frame = bytes(buffer[4:4 + frame_length])
                del buffer[:4 + frame_length]
                try:
                    event = msgpack.unpackb(
                        frame,
                        raw=False,
                        strict_map_key=False,
                        max_str_len=YESNAI_CAPABILITIES.max_response_bytes,
                        max_bin_len=YESNAI_CAPABILITIES.max_response_bytes,
                        max_array_len=100_000,
                        max_map_len=100_000,
                        max_ext_len=YESNAI_CAPABILITIES.max_response_bytes,
                    )
                except Exception as exc:
                    self._logger.warning(
                        f"{self.log_prefix} (YesNAI) MessagePack 帧解析失败: "
                        f"{type(exc).__name__}"
                    )
                    continue

                decoded_frames += 1
                result = self._parse_native_stream_event(event)
                if result is not None:
                    return result

        if mode == "sse" and buffer.strip():
            event = self._decode_sse_event(bytes(buffer))
            if event is not None:
                decoded_frames += 1
                result = self._parse_native_stream_event(event)
                if result is not None:
                    return result

        if mode == "raw" or (not mode and buffer):
            return self._parse_success_response(bytes(buffer))
        if mode == "msgpack" and buffer:
            return False, "Native MessagePack 流在半帧处结束"
        return False, (
            f"Native 流结束但未收到最终图片（已解析 {decoded_frames} 帧）"
        )

    @staticmethod
    def _detect_native_stream_mode(
        buffer: bytearray,
        content_type: str,
    ) -> str:
        sample = bytes(buffer[:64]).lstrip(b"\xef\xbb\xbf \t\r\n")
        if sample.startswith((b"PK\x03\x04", b"{", b"[")):
            return "raw"
        if sample.startswith((b"data:", b"event:", b"retry:", b":")):
            return "sse"
        if len(buffer) < 4:
            return ""
        if len(buffer) >= 4:
            frame_length = int.from_bytes(buffer[:4], "big", signed=False)
            if 0 < frame_length <= YESNAI_CAPABILITIES.max_response_bytes:
                return "msgpack"
        if "application/x-msgpack" in content_type:
            return "msgpack"
        if "text/event-stream" in content_type:
            return "sse"
        if "zip" in content_type or "json" in content_type:
            return "raw"
        return ""

    @staticmethod
    def _pop_sse_block(buffer: bytearray) -> Optional[bytes]:
        separators = ((b"\r\n\r\n", 4), (b"\n\n", 2))
        matches = [
            (index, size)
            for separator, size in separators
            if (index := buffer.find(separator)) >= 0
        ]
        if not matches:
            return None
        index, separator_size = min(matches, key=lambda item: item[0])
        block = bytes(buffer[:index])
        del buffer[:index + separator_size]
        return block

    @staticmethod
    def _decode_sse_event(block: bytes) -> Optional[Dict[str, Any]]:
        event_type = ""
        data_lines = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                event_type = line[6:].strip().decode("utf-8", errors="replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        data = b"\n".join(data_lines)
        if data.strip() == b"[DONE]":
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if event_type and not payload.get("event_type"):
            payload["event_type"] = event_type
        return payload

    def _parse_native_stream_event(
        self,
        event: Any,
    ) -> Optional[Tuple[bool, str]]:
        if not isinstance(event, dict):
            return None
        normalized: Dict[str, Any] = {}
        for key, value in event.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            normalized[str(key)] = value

        event_type = str(normalized.get("event_type") or "").strip().lower()
        if event_type == "error" or normalized.get("error"):
            message = (
                normalized.get("message")
                or normalized.get("error")
                or "Native 流式生成失败"
            )
            return False, f"Native 流错误: {str(message)[:300]}"
        if event_type != "final":
            return None

        image_bytes = self._coerce_native_stream_image(
            normalized.get("image", normalized.get("data")),
        )
        if not image_bytes:
            return False, "Native final 帧没有图片数据"
        valid, error = self.validate_image_bytes(
            image_bytes,
            capabilities=YESNAI_CAPABILITIES,
            field_name="生成图片",
        )
        if not valid:
            return False, error
        sample_index = normalized.get("samp_ix", 0)
        self._logger.info(
            f"{self.log_prefix} (YesNAI) 流式图片生成成功，"
            f"sample={sample_index} size={len(image_bytes)} bytes"
        )
        return True, base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _coerce_native_stream_image(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, list):
            if len(value) > YESNAI_CAPABILITIES.max_image_bytes:
                return b""
            try:
                return bytes(value)
            except (TypeError, ValueError):
                return b""
        if isinstance(value, str):
            encoded = value.strip()
            if "," in encoded and encoded.lower().startswith("data:image/"):
                encoded = encoded.split(",", 1)[1]
            max_encoded_length = (
                (YESNAI_CAPABILITIES.max_image_bytes + 2) // 3
            ) * 4 + 4
            if not encoded or len(encoded) > max_encoded_length:
                return b""
            try:
                return base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return b""
        return b""

    @classmethod
    def _streaming_is_unsupported(cls, status: int, text: str) -> bool:
        if status in cls._STREAM_FALLBACK_HTTP_STATUSES:
            return True
        if status not in {400, 422}:
            return False
        detail = str(text or "").lower()
        return any(marker in detail for marker in (
            "streaming is not allowed",
            "streaming_unsupported",
            "stream not allowed",
        ))

    # ================================================================
    # 参考图参数构建
    # ================================================================

    def _build_ref_params(
        self,
        ref_image: str,
        ref_mode: str,
        ref_strength: float,
        ref_fidelity: float,
        i2i_strength: float = 0.65,
        i2i_noise: float = 0.1,
    ) -> Tuple[str, Dict[str, Any]]:
        """将插件参考模式翻译为 YesNovelAI 原生 NAI 参数。

        Returns:
            (action, extra_params_dict)
        """
        if not ref_image or not ref_mode:
            return "generate", {}

        # 剥离 data URI 前缀，拿到纯 base64
        b64 = self._strip_data_uri(ref_image)

        if ref_mode == "i2i":
            strength = float(i2i_strength)
            return "img2img", {
                "image": b64,
                "strength": strength,
                "noise": float(i2i_noise),
            }

        if ref_mode in ("style", "character", "character&style"):
            director_image = self._prepare_director_reference_image(b64)
            secondary_strength = round(1.0 - ref_fidelity, 2)
            return "generate", {
                "director_reference_images": [director_image],
                "director_reference_strength_values": [ref_strength],
                "director_reference_secondary_strength_values": [secondary_strength],
                "director_reference_information_extracted": [1.0],
                "director_reference_descriptions": [{
                    "caption": {
                        "base_caption": ref_mode,
                        "char_captions": [],
                    },
                    "legacy_uc": False,
                }],
            }

        # 未知模式，回退普通生图
        self._logger.warning(
            f"{self.log_prefix} (YesNAI) 未知参考模式 '{ref_mode}'，回退普通生图"
        )
        return "generate", {}

    @staticmethod
    def _prepare_director_reference_image(image_b64: str) -> str:
        """按 NovelAI SDK 规则归一化 Director Reference 图片。"""
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
            with Image.open(io.BytesIO(image_bytes)) as source:
                source.load()
                image = source.convert("RGB")
        except (
            Image.DecompressionBombError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise ValueError(f"角色参考图预处理失败: {str(exc)[:80]}") from exc

        try:
            target_width, target_height = 1024, 1536
            source_width, source_height = image.size
            source_ratio = source_width / source_height
            target_ratio = target_width / target_height
            if source_ratio > target_ratio:
                resized_width = target_width
                resized_height = max(1, int(target_width / source_ratio))
            else:
                resized_height = target_height
                resized_width = max(1, int(target_height * source_ratio))

            with image.resize(
                (resized_width, resized_height),
                Image.Resampling.LANCZOS,
            ) as resized:
                with Image.new(
                    "RGB", (target_width, target_height), (0, 0, 0),
                ) as normalized:
                    normalized.paste(
                        resized,
                        (
                            (target_width - resized_width) // 2,
                            (target_height - resized_height) // 2,
                        ),
                    )
                    buffer = io.BytesIO()
                    normalized.save(buffer, format="PNG")
        finally:
            image.close()

        return base64.b64encode(buffer.getvalue()).decode("ascii")

    # ================================================================
    # 响应解析
    # ================================================================

    def _parse_success_response(
        self,
        raw_response: bytes,
    ) -> Tuple[bool, str]:
        """解析 Native ZIP；非 ZIP 响应继续兼容 YesNAI JSON。"""
        payload = bytes(raw_response or b"")
        if payload and zipfile.is_zipfile(io.BytesIO(payload)):
            return self._parse_native_zip_response(payload)

        try:
            data = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            return False, "API 返回非 ZIP 或 JSON 数据"
        return self._parse_response(data)

    def _parse_native_zip_response(self, raw_response: bytes) -> Tuple[bool, str]:
        """从 NovelAI Native ZIP 中安全读取首张有效图片。"""
        try:
            with zipfile.ZipFile(io.BytesIO(raw_response)) as archive:
                entries = archive.infolist()
                if len(entries) > self._NATIVE_ZIP_MAX_ENTRIES:
                    return False, "Native ZIP 文件数量超过安全限制"

                total_size = sum(max(0, entry.file_size) for entry in entries)
                if total_size > YESNAI_CAPABILITIES.max_response_bytes:
                    return False, "Native ZIP 解压后超过安全限制"

                image_entries = [
                    entry for entry in entries
                    if not entry.is_dir()
                    and entry.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                ]
                if not image_entries:
                    return False, "Native ZIP 中没有 PNG、JPEG 或 WebP 图片"

                last_error = ""
                for entry in image_entries:
                    if entry.file_size > YESNAI_CAPABILITIES.max_image_bytes:
                        last_error = "生成图片超过安全限制"
                        continue
                    with archive.open(entry, "r") as image_file:
                        image_bytes = image_file.read(
                            YESNAI_CAPABILITIES.max_image_bytes + 1
                        )
                    if len(image_bytes) > YESNAI_CAPABILITIES.max_image_bytes:
                        last_error = "生成图片超过安全限制"
                        continue
                    valid, error = self.validate_image_bytes(
                        image_bytes,
                        capabilities=YESNAI_CAPABILITIES,
                        field_name="生成图片",
                    )
                    if not valid:
                        last_error = error
                        continue

                    self._logger.info(
                        f"{self.log_prefix} (YesNAI) 图片生成成功，"
                        f"大小 {len(image_bytes)} bytes"
                    )
                    return True, base64.b64encode(image_bytes).decode("ascii")

                return False, last_error or "Native ZIP 中没有完整有效的图片"
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            NotImplementedError,
            RuntimeError,
            OSError,
            ValueError,
        ):
            return False, "Native 接口返回的 ZIP 数据无效"

    @staticmethod
    def _format_http_error(status: int, text: str) -> str:
        detail = str(text or "")[:500]
        code = ""
        message = ""
        try:
            payload = json.loads(detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
            code = code or str(payload.get("code") or "")
            message = message or str(payload.get("message") or payload.get("detail") or "")

        combined = f"{code} {message} {detail}".lower()
        if status in (400, 422) and any(
            marker in combined
            for marker in ("nsfw", "sensitive", "moderation", "blocked", "safety")
        ):
            return f"内容被安全拦截: {(message or detail)[:100]}"
        if "pricing_feature_requires_paid_base" in combined:
            return (
                "YesNAI 定价规则拒绝了参考图功能："
                "当前基础请求未被服务端计为付费，请检查 PricingRule"
            )

        status_messages = {
            401: "YesNAI API Token 无效、过期或已禁用",
            402: "YesNAI Gems 余额不足",
            403: "YesNAI API Token 没有当前模型权限",
            413: "YesNAI Native 请求体或图片超过限制",
            429: "YesNAI 免费限速或 Token 额度已达到限制",
        }
        if status in status_messages:
            return status_messages[status]
        code_label = f" [{code[:80]}]" if code else ""
        return f"HTTP {status}{code_label}: {(message or detail)[:200]}"

    def _parse_response(self, data: Any) -> Tuple[bool, str]:
        """解析 YesNovelAI 响应，返回 (成功, 图片base64或错误信息)。"""
        if not isinstance(data, dict):
            return False, "API 返回格式不正确"

        # 兼容 OpenAI images 格式: {"data": [{"b64_json": "..."}]}
        if "data" in data and isinstance(data.get("data"), list) and not data.get("images"):
            images = []
            for item in data["data"]:
                if isinstance(item, dict):
                    b64_val = item.get("b64_json") or item.get("image")
                    if b64_val:
                        images.append(b64_val)
            data = {**data, "images": images}

        # 检查 job 状态
        job = data.get("job")
        if job is None:
            job = {}
        if isinstance(job, dict):
            status = str(job.get("status") or "").lower()
            if status in ("failed", "error"):
                msg = job.get("error") or job.get("message") or "任务失败"
                return False, str(msg)[:200]

        # 提取图片
        images = data.get("images")
        if not isinstance(images, list) or not images:
            # 尝试 error/message 字段
            for key in ("error", "message", "detail"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return False, val[:200]
            return False, "API 返回中没有图片"

        first_image = images[0]
        if not isinstance(first_image, str):
            return False, "图片字段类型错误"

        valid, normalized_image, image_size = self.normalize_and_validate_base64_image(
            first_image,
            capabilities=YESNAI_CAPABILITIES,
            field_name="生成图片",
        )
        if not valid:
            return False, normalized_image

        self._logger.info(
            f"{self.log_prefix} (YesNAI) 图片生成成功，大小 {image_size} bytes"
        )
        return True, normalized_image

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _strip_data_uri(image: str) -> str:
        """剥离 data:image/...;base64, 前缀，返回纯 base64 字符串。"""
        if not image:
            return image
        if image.startswith("data:"):
            # 格式: data:image/png;base64,xxxx
            comma_idx = image.find(",")
            if comma_idx > 0:
                return image[comma_idx + 1:]
        return image

    @staticmethod
    def _parse_size(size: Optional[str]) -> Optional[Tuple[int, int]]:
        """解析尺寸字符串为 (width, height)。

        支持: "832x1216", "竖图", "v", "1024x1024" 等。
        """
        if not size:
            return 1024, 1024

        if isinstance(size, (list, tuple)) and len(size) == 2:
            try:
                dimensions = (float(size[0]), float(size[1]))
                if not all(value.is_integer() and math.isfinite(value) for value in dimensions):
                    return None
                return int(dimensions[0]), int(dimensions[1])
            except (TypeError, ValueError, OverflowError):
                return None

        size_text = str(size).strip().lower().replace("×", "x")
        if len(size_text) > 32:
            return None

        # 别名映射
        aliases = {
            "竖": "832x1216", "竖图": "832x1216",
            "横": "1216x832", "横图": "1216x832",
            "方": "1024x1024", "方图": "1024x1024",
            "v": "832x1216", "h": "1216x832", "s": "1024x1024",
        }
        size_text = aliases.get(size_text, size_text)

        match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", size_text)
        if match:
            return int(match.group(1)), int(match.group(2))

        return None

    @staticmethod
    def _resolve_size(size: Optional[str]) -> Tuple[int, int]:
        """兼容旧调用：无效尺寸仍回退到 1024x1024。"""
        return YesNAIProvider._parse_size(size) or (1024, 1024)

    def _sanitize_prompt(self, text: str) -> str:
        """清理提示词中的中/日/韩文和全角符号。"""
        if not text:
            return text
        temp = text
        for full, half in [
            ("：", ":"), ("，", ","), ("　", " "),
            ("（", "("), ("）", ")"), ("［", "["), ("］", "]"),
            ("｛", "{"), ("｝", "}"),
        ]:
            temp = temp.replace(full, half)
        cleaned = self._CJK_RE.sub("", temp)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned != text:
            self._logger.warning(
                f"{self.log_prefix} (YesNAI) 提示词含非英文字符，已自动清理"
            )
        return cleaned

    def _sanitize_structured_prompt(
        self,
        structured_prompt,
        model: str,
        *,
        prompt_structure: str = "auto",
    ):
        if (
            structured_prompt is None
            or normalize_prompt_structure(prompt_structure) == "flat"
            or not self._is_novelai_v4_model(model)
        ):
            return None
        global_tags = tuple(
            cleaned for tag in structured_prompt.global_tags
            if (cleaned := self._sanitize_prompt(tag))
        )
        people = []
        for person in structured_prompt.people:
            positive_tags = tuple(
                cleaned for tag in person.positive_tags
                if (cleaned := self._sanitize_prompt(tag))
            )
            if not positive_tags:
                return None
            negative_tags = tuple(
                cleaned for tag in person.negative_tags
                if (cleaned := self._sanitize_prompt(tag))
            )
            people.append(replace(
                person,
                positive_tags=positive_tags,
                negative_tags=negative_tags,
            ))
        if not global_tags:
            return None
        return replace(
            structured_prompt,
            global_tags=global_tags,
            people=tuple(people),
        )

    @staticmethod
    def _is_novelai_v4_model(model: str) -> bool:
        value = str(model or "").strip().lower()
        return "nai-diffusion-4" in value or "novelai-v4" in value

    @staticmethod
    def _is_novelai_v5_model(model: str) -> bool:
        value = str(model or "").strip().lower()
        return "nai-diffusion-5" in value or "novelai-v5" in value

    @classmethod
    def _uses_v4_prompt_envelope(
        cls,
        model: str,
        action: str,
        prompt_structure: str,
    ) -> bool:
        # Launcher 的 V4.5 与 V5 Img2ImgRequest 都使用 v4_prompt，哪怕
        # V5 文生图在插件中配置为 flat。
        if str(action or "").strip().lower() == "img2img":
            return (
                cls._is_novelai_v4_model(model)
                or cls._is_novelai_v5_model(model)
            )
        return (
            normalize_prompt_structure(prompt_structure) != "flat"
            and cls._is_novelai_v4_model(model)
        )
