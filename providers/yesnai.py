# -*- coding: utf-8 -*-
"""YesNovelAI 原生 Provider。

通过 YesNovelAI business-api 的 /v1/nai/generate-image 端点调用 NovelAI 图片生成。
实现 BaseImageProvider 接口，对接自建 YesNovelAI 平台。
"""

import asyncio
import json
import math
import re
import ssl
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp
import certifi

from .base import BaseImageProvider, ResponseLimitError
from .capabilities import YESNAI_CAPABILITIES


class YesNAIProvider(BaseImageProvider):
    """YesNovelAI 图片生成 Provider（NAI 原生格式）"""

    default_endpoint = "/v1/nai/generate-image"

    _RESERVED_EXTRA_PARAMS = {
        "model", "action", "input", "prompt", "parameters", "size",
        "width", "height", "steps", "num_inference_steps", "n_samples",
        "negative_prompt", "sampler", "scale", "guidance_scale", "cfg",
        "cfg_rescale", "seed", "noise_schedule", "nocache", "image",
        "images", "img2img", "strength", "noise", "reference_image",
        "reference_images", "reference_image_multiple",
        "reference_strength_multiple", "reference_information_extracted_multiple",
        "director_reference_images", "director_reference_strength_values",
        "director_reference_secondary_strength_values", "ref_image", "ref_mode",
        "ref_strength", "ref_fidelity",
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

    # Public API

    async def generate(
        self,
        prompt: str,
        model_config: Dict[str, Any],
        size: Optional[str] = None,
        ref_image: str = "",
        ref_mode: str = "",
    ) -> Tuple[bool, str]:
        """调用 YesNovelAI /v1/nai/generate-image 接口生成图片（异步）。"""
        try:
            if not self.validate_config(model_config):
                return False, "模型配置不完整（缺少 base_url 或 model）"

            base_url = (model_config.get("base_url") or "").rstrip('/')
            if base_url.startswith("http://"):
                self._logger.warning(
                    f"{self.log_prefix} (YesNAI) base_url 为明文 HTTP，"
                    f"API Token 将以明文传输，建议改用 HTTPS"
                )

            endpoint = (model_config.get("endpoint") or model_config.get("nai_endpoint") or "").strip()
            if not endpoint:
                endpoint = self.default_endpoint
            if not endpoint.startswith('/'):
                endpoint = f"/{endpoint}"
            url = f"{base_url}{endpoint}"

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

            if ref_image and ref_mode:
                valid, normalized_ref, _ = self.normalize_and_validate_base64_image(
                    ref_image,
                    capabilities=YESNAI_CAPABILITIES,
                    field_name="参考图",
                )
                if not valid:
                    return False, normalized_ref
                ref_image = normalized_ref

            action, ref_extra = self._build_ref_params(
                ref_image=ref_image,
                ref_mode=ref_mode,
                ref_strength=ref_strength,
                ref_fidelity=ref_fidelity,
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

            seed = self.bounded_int(
                seed,
                default=-1,
                minimum=-1,
                maximum=4_294_967_295,
                name="seed",
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

            # ── 构建请求体 ──
            body: Dict[str, Any] = {
                "model": model_name,
                "action": action,
                "input": full_prompt,
                "parameters": parameters,
            }

            headers = {
                "Content-Type": "application/json",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"

            self._logger.info(
                f"{self.log_prefix} (YesNAI) 请求: model={model_name} "
                f"action={action} size={width}x{height} steps={steps}"
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
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
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
                        text = raw_response.decode("utf-8-sig", errors="replace")

                        if 300 <= response.status < 400:
                            location = response.headers.get("location", "")
                            self._logger.error(
                                f"{self.log_prefix} (YesNAI) 拒绝重定向: "
                                f"status={response.status}, location={location[:200]}"
                            )
                            return False, f"HTTP {response.status}: 接口发生重定向，请检查 base_url"

                        if response.status < 200 or response.status >= 300:
                            detail = text[:500]
                            low = detail.lower()
                            # NSFW 拦截
                            if response.status in (400, 422) and any(
                                k in low
                                for k in ("nsfw", "sensitive", "moderation", "blocked", "safety")
                            ):
                                return False, f"内容被安全拦截: {detail[:100]}"
                            return False, f"HTTP {response.status}: {detail[:200]}"

                        try:
                            data = json.loads(text)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            return False, "API 返回非 JSON 数据"
            except ResponseLimitError as exc:
                return False, str(exc)
            except (asyncio.TimeoutError, TimeoutError):
                return False, f"请求超时 ({timeout_seconds}s)"
            except aiohttp.ClientError as exc:
                return False, f"网络错误: {type(exc).__name__}: {exc}"
            except Exception as exc:
                self._logger.error(f"{self.log_prefix} (YesNAI) 请求异常: {exc!r}", exc_info=True)
                return False, f"请求失败: {str(exc)[:100]}"

            # ── 解析响应 ──
            return self._parse_response(data)

        except Exception as e:
            self._logger.error(f"{self.log_prefix} (YesNAI) 未知异常: {e!r}", exc_info=True)
            return False, f"YesNAI 接口请求失败: {str(e)[:100]}"

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

    # 参考图参数构建

    def _build_ref_params(
        self,
        ref_image: str,
        ref_mode: str,
        ref_strength: float,
        ref_fidelity: float,
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
            return "img2img", {
                "image": b64,
                "strength": 0.5,
                "noise": 0.0,
                "img2img": True,
            }

        if ref_mode == "style":
            return "generate", {
                "reference_image_multiple": [b64],
                "reference_strength_multiple": [ref_strength],
                "reference_information_extracted_multiple": [0.7],
            }

        if ref_mode in ("character", "character&style"):
            return "generate", {
                "director_reference_images": [b64],
                "director_reference_strength_values": [ref_strength],
                "director_reference_secondary_strength_values": [ref_fidelity],
            }

        # 未知模式，回退普通生图
        self._logger.warning(
            f"{self.log_prefix} (YesNAI) 未知参考模式 '{ref_mode}'，回退普通生图"
        )
        return "generate", {}

    # 响应解析

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

    # 工具方法

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
