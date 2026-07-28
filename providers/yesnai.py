# -*- coding: utf-8 -*-
"""YesNovelAI 原生 Provider。

通过 YesNovelAI business-api 的 /v1/nai/generate-image 端点调用 NovelAI 图片生成。
实现 BaseImageProvider 接口，对接自建 YesNovelAI 平台。
"""

import asyncio
import base64
import re
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

from .base import BaseImageProvider


class YesNAIProvider(BaseImageProvider):
    """YesNovelAI 图片生成 Provider（NAI 原生格式）"""

    default_endpoint = "/v1/nai/generate-image"

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
            sampler = model_config.get("sampler", "k_euler_ancestral")
            steps = model_config.get("steps") or model_config.get("num_inference_steps") or 28
            scale = model_config.get("scale") or model_config.get("guidance_scale") or 5.0
            cfg_value = model_config.get("cfg") or model_config.get("nai_cfg")
            noise_schedule = model_config.get("noise_schedule") or model_config.get("nai_noise_schedule") or "karras"
            nocache = model_config.get("nocache") or model_config.get("nai_nocache")
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
            width, height = self._resolve_size(final_size)

            # ── 安全清理 CJK 字符 ──
            full_prompt = self._sanitize_prompt(full_prompt)
            negative_prompt = self._sanitize_prompt(negative_prompt)

            # ── 限制 steps ──
            try:
                steps = min(int(steps), 50)
            except (TypeError, ValueError):
                steps = 28

            # ── 参考图参数 ──
            ref_fidelity = float(model_config.get("ref_fidelity", 0.5))
            ref_strength = float(model_config.get("ref_strength", 1.0))

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
                "scale": float(scale),
                "negative_prompt": negative_prompt or "",
            }

            if seed is not None and int(seed) >= 0:
                parameters["seed"] = int(seed)

            if noise_schedule:
                parameters["noise_schedule"] = str(noise_schedule)

            if isinstance(cfg_value, (int, float)) and 0 <= float(cfg_value) <= 1:
                parameters["cfg_rescale"] = float(cfg_value)

            if nocache is not None:
                parameters["nocache"] = bool(nocache)

            # 合并 extra_params 和 ref_extra
            if isinstance(extra_params, dict):
                for k, v in extra_params.items():
                    if v not in (None, ""):
                        parameters[k] = v
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
            timeout_seconds = float(model_config.get("timeout_seconds", 120))
            proxy = (model_config.get("proxy") or "").strip() or None

            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as session:
                    async with session.post(
                        url,
                        headers=headers,
                        json=body,
                        proxy=proxy,
                    ) as response:
                        text = await response.text()

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
                            data = await response.json(content_type=None)
                        except Exception:
                            import json
                            try:
                                data = json.loads(text)
                            except Exception:
                                return False, "API 返回非 JSON 数据"
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

    # ================================================================
    # 参考图参数构建
    # ================================================================

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

    # ================================================================
    # 响应解析
    # ================================================================

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

        # 剥离 data URI 前缀，拿到纯 base64
        b64 = self._strip_data_uri(first_image)

        # 验证图片签名（PNG 或 JPEG）
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return False, "图片 base64 解码失败"

        if not (raw.startswith(b"\x89PNG") or raw[:3] == b"\xff\xd8\xff"):
            return False, "图片不是有效 PNG/JPEG 格式"

        self._logger.info(
            f"{self.log_prefix} (YesNAI) 图片生成成功，大小 {len(raw)} bytes"
        )
        return True, b64

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
    def _resolve_size(size: Optional[str]) -> Tuple[int, int]:
        """解析尺寸字符串为 (width, height)。

        支持: "832x1216", "竖图", "v", "1024x1024" 等。
        """
        if not size:
            return 1024, 1024

        if isinstance(size, (list, tuple)) and len(size) == 2:
            try:
                return int(size[0]), int(size[1])
            except (TypeError, ValueError):
                return 1024, 1024

        size_text = str(size).strip().lower().replace("×", "x")

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

        # 回退默认
        return 1024, 1024

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
