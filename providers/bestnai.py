# -*- coding: utf-8 -*-
"""BestNAI / NovelAI 兼容 Provider。

通过 OpenAI Chat Completions 兼容接口调用 NovelAI 图片生成服务。
实现 BaseImageProvider 接口。
"""

import asyncio
import base64
from dataclasses import replace
import io
import json
import math
import re
import ssl
from typing import TYPE_CHECKING, Dict, Any, Tuple, Optional, List

import requests
import certifi
from PIL import Image
from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError
from urllib3.util.ssl_ import create_urllib3_context

from .base import BaseImageProvider, ResponseLimitError
from .capabilities import BESTNAI_CAPABILITIES

if TYPE_CHECKING:
    from ..core.prompt_types import StructuredPrompt


class SSLAdapter(HTTPAdapter):
    """自定义 SSL 适配器：保留证书验证，同时兼容部分老服务器握手。

    证书验证基于 certifi CA 包开启（解决嵌入式 Python 缺 CA 的问题），
    OP_LEGACY_SERVER_CONNECT 用于兼容不支持 RFC 5746 重协商的旧服务端。
    """
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.load_verify_locations(cafile=certifi.where())
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


class BestNAIProvider(BaseImageProvider):
    """BestNAI 图片生成 Provider（OpenAI Chat Completions 兼容）"""

    default_endpoint = "/v1/chat/completions"

    _RESERVED_EXTRA_PARAMS = {
        "model", "prompt", "size", "width", "height", "steps",
        "num_inference_steps", "n_samples", "negative_prompt", "sampler",
        "scale", "guidance_scale", "cfg", "cfg_rescale", "seed",
        "noise_schedule", "nocache", "i2i", "image", "images", "strength",
        "noise", "controlnet", "character_references", "reference_image",
        "reference_images", "reference_image_multiple",
        "reference_strength_multiple", "reference_information_extracted_multiple",
        "director_reference_images", "director_reference_strength_values",
        "director_reference_secondary_strength_values", "ref_image", "ref_mode",
        "ref_strength", "ref_fidelity", "max_tokens", "stream",
        "characters", "use_coords", "use_order",
    }

    # 匹配中/日/韩文 + 全角符号（NewAPI 仅允许英文）
    _CJK_RE = re.compile(
        r'[一-鿿㐀-䶿＀-＇＊-Ｚ＼＾-ｚ｜～-￯　-〿'
        r'぀-ゟ゠-ヿ가-힯'
        r' -⁯⺀-⻿⼀-⿟㆐-㆟'
        r'㇀-㇯㈀-㋿㌀-㏿'
        r'︰-﹏︐-︟㄀-ㄯ]'
    )

    def __init__(self, logger, log_prefix: str = ""):
        super().__init__(logger, log_prefix)
        # Provider 当前由生成器按请求创建。连接池也按请求创建并在读取完响应后关闭，
        # 避免临时 Provider 被回收前遗留两个 Session/连接池。
        self.session: Optional[requests.Session] = None
        self.direct_session: Optional[requests.Session] = None
        self._auto_proxy_direct_only = False

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
        """调用 BestNAI Chat Completions 接口生成图片（异步）。"""
        try:
            if not self.validate_config(model_config):
                return False, "模型配置不完整（缺少 base_url 或 model）"

            base_url = (model_config.get("base_url") or "").rstrip('/')
            if base_url.startswith("http://"):
                self._logger.warning(f"{self.log_prefix} (BestNAI) base_url 为明文 HTTP，API Key 将以明文传输，建议改用 HTTPS")
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

            # 拼接完整提示词
            custom_prompt_add = model_config.get("custom_prompt_add", "")
            full_prompt = f"{custom_prompt_add}, {prompt}" if custom_prompt_add else prompt

            # 画师提示词
            artist_prompt = model_config.get("nai_artist_prompt") or model_config.get("artist_prompt")

            # 读取生成参数
            negative_prompt = model_config.get("negative_prompt_add", "")
            sampler = model_config.get("sampler", "")
            steps = model_config.get("steps")
            if steps is None or steps == "":
                steps = model_config.get("num_inference_steps")
            guidance_scale = model_config.get("scale")
            if guidance_scale is None or guidance_scale == "":
                guidance_scale = model_config.get("guidance_scale")
            cfg_value = model_config.get("cfg")
            if cfg_value is None or cfg_value == "":
                cfg_value = model_config.get("nai_cfg")
            noise_schedule = model_config.get("noise_schedule") or model_config.get("nai_noise_schedule")
            nocache = model_config.get("nocache")
            if nocache is None:
                nocache = model_config.get("nai_nocache")
            size_override = model_config.get("size_preset") or model_config.get("nai_size")
            extra_params = model_config.get("extra_params") or model_config.get("nai_extra_params") or {}
            model_name = model_config.get("model") or model_config.get("default_model") or "nai-diffusion-4-5-full"
            seed = model_config.get("seed", -1)

            # 调用方显式传入的尺寸优先于模型配置中的默认/预设尺寸。
            final_size = size or size_override or model_config.get("default_size")

            # 安全清理：NewAPI 不允许非英文内容
            full_prompt = self._sanitize_prompt(full_prompt)
            negative_prompt = self._sanitize_prompt(negative_prompt)
            custom_prompt_add = self._sanitize_prompt(custom_prompt_add)
            if artist_prompt:
                artist_prompt = self._sanitize_prompt(artist_prompt)
            structured_prompt = self._sanitize_structured_prompt(
                structured_prompt, model_name,
            )

            normalized_size = self._normalize_size(final_size)
            if final_size and normalized_size is None:
                return False, f"无效的图片尺寸: {str(final_size)[:50]}"
            if normalized_size:
                self.validate_dimensions(
                    normalized_size[0], normalized_size[1], BESTNAI_CAPABILITIES
                )
            final_size = normalized_size

            if sampler not in (None, ""):
                try:
                    sampler = self.validate_sampler(sampler, BESTNAI_CAPABILITIES)
                except ValueError as exc:
                    return False, str(exc)
            else:
                sampler = ""

            steps = self.bounded_int(
                steps,
                default=23,
                minimum=BESTNAI_CAPABILITIES.min_steps,
                maximum=BESTNAI_CAPABILITIES.max_steps,
                name="steps",
            )
            guidance_scale = self.bounded_float(
                guidance_scale,
                default=None,
                minimum=BESTNAI_CAPABILITIES.min_scale,
                maximum=BESTNAI_CAPABILITIES.max_scale,
                name="scale",
            )
            cfg_value = self.bounded_float(
                cfg_value,
                default=None,
                minimum=BESTNAI_CAPABILITIES.min_cfg_rescale,
                maximum=BESTNAI_CAPABILITIES.max_cfg_rescale,
                name="cfg_rescale",
            )
            ref_fidelity = self.bounded_float(
                model_config.get("ref_fidelity", 1.0),
                default=1.0,
                minimum=BESTNAI_CAPABILITIES.min_reference_strength,
                maximum=BESTNAI_CAPABILITIES.max_reference_strength,
                name="ref_fidelity",
            )
            ref_strength = self.bounded_float(
                model_config.get("ref_strength", 1.0),
                default=1.0,
                minimum=BESTNAI_CAPABILITIES.min_reference_strength,
                maximum=BESTNAI_CAPABILITIES.max_reference_strength,
                name="ref_strength",
            )
            seed = self.bounded_int(
                seed,
                default=-1,
                minimum=-1,
                maximum=4_294_967_295,
                name="seed",
            )

            if ref_image and ref_mode:
                if ref_image.startswith(("http://", "https://")):
                    if not self.validate_http_url(ref_image):
                        return False, "参考图 URL 格式无效"
                else:
                    valid, normalized_ref, _ = self.normalize_and_validate_base64_image(
                        ref_image,
                        capabilities=BESTNAI_CAPABILITIES,
                        field_name="参考图",
                    )
                    if not valid:
                        return False, normalized_ref
                    ref_image = normalized_ref

            # 构建生成参数
            generation_params = self._build_generation_params(
                prompt=full_prompt,
                artist_prompt=artist_prompt,
                negative_prompt=negative_prompt,
                sampler=sampler,
                steps=steps,
                guidance_scale=guidance_scale,
                cfg_value=cfg_value,
                noise_schedule=noise_schedule,
                nocache=nocache,
                final_size=final_size,
                extra_params=extra_params,
                model=model_name,
                ref_image=ref_image,
                ref_mode=ref_mode,
                ref_fidelity=ref_fidelity,
                ref_strength=ref_strength,
                seed=seed,
                structured_prompt=structured_prompt,
                structured_base_prompt=(
                    f"{custom_prompt_add}, {', '.join(structured_prompt.global_tags)}"
                    if structured_prompt is not None and custom_prompt_add
                    else ", ".join(structured_prompt.global_tags)
                    if structured_prompt is not None
                    else ""
                ),
            )

            # max_tokens 预算
            max_tokens = self.bounded_int(
                model_config.get("max_tokens"),
                default=100000,
                minimum=1024,
                maximum=BESTNAI_CAPABILITIES.max_tokens,
                name="max_tokens",
            )
            if ref_image and ref_mode and max_tokens < 50000:
                self._logger.warning(
                    f"{self.log_prefix} (BestNAI) 参考图模式 max_tokens 过低，已调整为 50000"
                )
                max_tokens = 50000

            self._logger.info(
                f"{self.log_prefix} (BestNAI) max_tokens={max_tokens} ref_mode={ref_mode}"
            )

            timeout_seconds = self.bounded_float(
                model_config.get("timeout_seconds"),
                default=120.0,
                minimum=1.0,
                maximum=600.0,
                name="timeout_seconds",
            )

            payload = {
                "model": model_name,
                "max_tokens": max_tokens,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            generation_params,
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                    }
                ],
            }
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            # api_key 仅放入 Authorization 头，不写入日志/异常；下方仅记录 url（不含 key）
            self._logger.info(f"{self.log_prefix} (BestNAI) 请求URL: {url}")

            # 异步执行同步 requests 请求。取消外层 asyncio Task 无法强制终止已启动的
            # 工作线程，因此取消后仍等待线程在请求超时/完成时收尾，再释放并发槽位；
            # 这仍不保证远端任务停止或不计费。
            proxy_mode = self.resolve_proxy_mode(model_config)
            worker_task = asyncio.create_task(asyncio.to_thread(
                self._send_request,
                url,
                headers,
                payload,
                proxy_mode,
                timeout_seconds,
            ))
            try:
                response = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                response = None
                while not worker_task.done():
                    try:
                        response = await asyncio.shield(worker_task)
                    except asyncio.CancelledError:
                        # Repeated cancellation must not release the provider slot
                        # while the synchronous requests worker is still running.
                        continue
                    except BaseException:
                        # Retrieve the worker result below so its exception is consumed.
                        break

                if response is None:
                    try:
                        response = worker_task.result()
                    except BaseException:
                        # The caller already cancelled this operation; consume any
                        # worker failure and preserve cancellation as the outcome.
                        response = None
                if response is not None:
                    try:
                        response.close()
                    except Exception as exc:
                        self._logger.warning(
                            f"{self.log_prefix} (BestNAI) 取消后关闭响应失败: {exc}"
                        )
                raise
            try:
                return self._handle_response(response)
            finally:
                response.close()

        except ResponseLimitError as e:
            self._logger.error(f"{self.log_prefix} (BestNAI) 响应过大: {e}")
            return False, str(e)
        except requests.RequestException as e:
            self._logger.error(f"{self.log_prefix} (BestNAI) 网络异常: {e}")
            return False, f"网络请求失败: {str(e)}"
        except Exception as e:
            self._logger.error(f"{self.log_prefix} (BestNAI) 请求异常: {e!r}", exc_info=True)
            return False, f"BestNAI 接口请求失败: {str(e)[:100]}"

    # ================================================================
    # HTTP Session 管理
    # ================================================================

    @staticmethod
    def _create_session(trust_env: bool) -> requests.Session:
        session = requests.Session()
        session.trust_env = trust_env
        session.mount('https://', SSLAdapter())
        return session

    def _get_session(self, trust_env: bool) -> requests.Session:
        attr_name = "session" if trust_env else "direct_session"
        session = getattr(self, attr_name, None)
        if session is None:
            session = self._create_session(trust_env=trust_env)
            setattr(self, attr_name, session)
        return session

    def _send_request(
        self,
        url: str,
        headers: dict,
        payload: dict,
        proxy_mode: str = "auto",
        timeout_seconds: float = 120.0,
    ):
        """发送 HTTP 请求（同步，由 asyncio.to_thread 调用）。"""
        if proxy_mode == "direct":
            return self._request_with_session(
                False, url, headers, payload, timeout_seconds,
            )

        if proxy_mode == "inherit":
            return self._request_with_session(
                True, url, headers, payload, timeout_seconds,
            )

        if getattr(self, "_auto_proxy_direct_only", False):
            return self._request_with_session(
                False, url, headers, payload, timeout_seconds,
            )

        try:
            return self._request_with_session(
                True, url, headers, payload, timeout_seconds,
            )
        except requests.RequestException as exc:
            if not self._is_proxy_related_exception(exc):
                raise
            self._auto_proxy_direct_only = True
            self._logger.warning(f"{self.log_prefix} (BestNAI) 代理失败，回退直连: {exc}")
            return self._request_with_session(
                False, url, headers, payload, timeout_seconds,
            )

    def _request_with_session(
        self,
        trust_env: bool,
        url: str,
        headers: dict,
        payload: dict,
        timeout_seconds: float = 120.0,
    ):
        session = self._get_session(trust_env=trust_env)
        attr_name = "session" if trust_env else "direct_session"
        response: Optional[requests.Response] = None
        try:
            response = session.post(
                url=url,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            self._buffer_response(response, BESTNAI_CAPABILITIES.max_response_bytes)
            return response
        except Exception:
            if response is not None:
                response.close()
            raise
        finally:
            session.close()
            setattr(self, attr_name, None)

    @staticmethod
    def _buffer_response(response: requests.Response, max_bytes: int) -> None:
        """流式读取 requests 响应，避免服务端返回无限大的响应体。"""
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                raise ResponseLimitError(
                    f"API 响应超过 {max_bytes // (1024 * 1024)} MiB 限制"
                )

        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ResponseLimitError(
                    f"API 响应超过 {max_bytes // (1024 * 1024)} MiB 限制"
                )
        response._content = bytes(content)
        response._content_consumed = True

    @staticmethod
    def _is_proxy_related_exception(exc: requests.RequestException) -> bool:
        if isinstance(exc, ProxyError):
            return True
        current: Optional[BaseException] = exc
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            message = str(current).lower()
            if "proxy" in message or "407" in message:
                return True
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        return False

    # ================================================================
    # 提示词清理
    # ================================================================

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
            self._logger.warning(f"{self.log_prefix} (BestNAI) 提示词含非英文字符，已自动清理")
        return cleaned

    # ================================================================
    # 生成参数构建
    # ================================================================

    def _build_generation_params(
        self, prompt, artist_prompt, negative_prompt, sampler, steps,
        guidance_scale, cfg_value, noise_schedule, nocache, final_size,
        extra_params, model="", ref_image="", ref_mode="",
        ref_fidelity: float = 1.0, ref_strength: float = 1.0,
        seed: int = -1,
        structured_prompt: Optional["StructuredPrompt"] = None,
        structured_base_prompt: str = "",
    ) -> Dict[str, Any]:
        """构造 NewAPI 绘图参数。"""
        safe_extra_params = self.filter_extra_params(
            extra_params,
            self._RESERVED_EXTRA_PARAMS,
            "BestNAI",
        )
        combined_prompt = prompt.strip()
        if artist_prompt:
            combined_prompt = f"{artist_prompt.strip()}, {combined_prompt}"

        params: Dict[str, Any] = {
            "model": model or "nai-diffusion-4-5-full",
            "prompt": combined_prompt,
            "n_samples": 1,
        }

        if structured_prompt is not None and structured_prompt.people:
            base_prompt = structured_base_prompt.strip()
            if artist_prompt:
                base_prompt = f"{artist_prompt.strip()}, {base_prompt}"
            params["prompt"] = base_prompt
            layout = self.character_layout(len(structured_prompt.people))
            characters = []
            for index, person in enumerate(structured_prompt.people):
                character = {
                    "prompt": ", ".join(person.positive_tags),
                    "negative_prompt": ", ".join(person.negative_tags),
                }
                if layout:
                    character["position"] = layout[index][2]
                characters.append(character)
            params["characters"] = characters
            params["use_coords"] = bool(layout)
            params["use_order"] = True
            self._logger.info(
                f"{self.log_prefix} (BestNAI) V4人物结构: "
                f"count={len(characters)} use_coords={bool(layout)} "
                f"positions={','.join(item.get('position', '-') for item in characters)}"
            )

        normalized_size = self._normalize_size(final_size)
        if normalized_size:
            params["size"] = normalized_size
        if negative_prompt:
            params["negative_prompt"] = negative_prompt
        if sampler:
            params["sampler"] = sampler
        if steps is not None:
            params["steps"] = steps
        if guidance_scale is not None:
            params["scale"] = guidance_scale
        if noise_schedule:
            params["noise_schedule"] = noise_schedule
        if isinstance(cfg_value, (int, float)) and 0 <= float(cfg_value) <= 1:
            params["cfg_rescale"] = float(cfg_value)
        if nocache is not None:
            params["nocache"] = nocache
        if seed >= 0:
            params["seed"] = seed
        params.update(safe_extra_params)

        # 图生图 / 参考模式
        if ref_image and ref_mode:
            if ref_image.startswith(("data:", "http://", "https://")):
                image_uri = ref_image
            else:
                image_uri = self._to_data_uri(ref_image)
            if ref_mode == "i2i":
                if params.get("size") and not ref_image.startswith(("http://", "https://")):
                    ref_image = self._resize_to_match(ref_image, params["size"])
                    image_uri = self._to_data_uri(ref_image)
                params["i2i"] = {"image": image_uri, "strength": 0.5, "noise": 0}
                self._logger.info(f"{self.log_prefix} (BestNAI) i2i strength=0.5 size={params.get('size')}")
            elif ref_mode == "style":
                params["controlnet"] = {
                    "strength": ref_strength,
                    "images": [{"image": image_uri, "info_extracted": 0.7, "strength": ref_strength}],
                }
                self._logger.info(
                    f"{self.log_prefix} (BestNAI) style ref: info_extracted=0.7 strength={ref_strength}"
                )
            elif ref_mode == "character":
                params["character_references"] = [{
                    "image": image_uri, "type": "character",
                    "fidelity": ref_fidelity, "strength": ref_strength,
                }]
                self._logger.info(
                    f"{self.log_prefix} (BestNAI) character ref: fidelity={ref_fidelity} strength={ref_strength}"
                )
            elif ref_mode == "character&style":
                params["character_references"] = [{
                    "image": image_uri, "type": "character&style",
                    "fidelity": ref_fidelity, "strength": ref_strength,
                }]
                self._logger.info(
                    f"{self.log_prefix} (BestNAI) character&style ref: fidelity={ref_fidelity} strength={ref_strength}"
                )

        return params

    def _sanitize_structured_prompt(self, structured_prompt, model: str):
        if structured_prompt is None or not self._is_novelai_v4_model(model):
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

    # ================================================================
    # 尺寸 & 图片工具
    # ================================================================

    @staticmethod
    def _normalize_size(size: Optional[str]) -> Optional[List[int]]:
        if not size:
            return None
        if isinstance(size, (list, tuple)) and len(size) == 2:
            try:
                dimensions = [float(size[0]), float(size[1])]
                if not all(value.is_integer() and math.isfinite(value) for value in dimensions):
                    return None
                return [int(dimensions[0]), int(dimensions[1])]
            except (TypeError, ValueError, OverflowError):
                return None

        size_text = str(size).strip().lower().replace("×", "x")
        if len(size_text) > 32:
            return None
        size_aliases = {
            "竖": "832x1216", "竖图": "832x1216",
            "横": "1216x832", "横图": "1216x832",
            "方": "1024x1024", "方图": "1024x1024",
            "v": "832x1216", "h": "1216x832", "s": "1024x1024",
        }
        size_text = size_aliases.get(size_text, size_text)
        match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", size_text)
        if not match:
            return None
        return [int(match.group(1)), int(match.group(2))]

    @staticmethod
    def _to_data_uri(b64: str) -> str:
        if b64.startswith("/9j/"):
            return f"data:image/jpeg;base64,{b64}"
        if b64.startswith("iVBORw0KGgo"):
            return f"data:image/png;base64,{b64}"
        if b64.startswith("UklGR"):
            return f"data:image/webp;base64,{b64}"
        return f"data:image/png;base64,{b64}"

    @staticmethod
    def _resize_to_match(b64_data: str, target_size: List[int]) -> str:
        if not target_size or len(target_size) != 2:
            return b64_data
        try:
            raw = base64.b64decode(b64_data, validate=True)
            tw, th = int(target_size[0]), int(target_size[1])
            BaseImageProvider.validate_dimensions(tw, th, BESTNAI_CAPABILITIES)
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                iw, ih = source.size
                if iw == tw and ih == th:
                    return b64_data

                target_ratio = tw / th
                current_ratio = iw / ih
                if current_ratio > target_ratio:
                    crop_width = max(1, min(iw, round(ih * target_ratio)))
                    left = (iw - crop_width) // 2
                    crop_box = (left, 0, left + crop_width, ih)
                else:
                    crop_height = max(1, min(ih, round(iw / target_ratio)))
                    top = (ih - crop_height) // 2
                    crop_box = (0, top, iw, top + crop_height)

                with source.crop(crop_box) as cropped:
                    with cropped.resize(
                        (tw, th), Image.Resampling.LANCZOS,
                    ) as resized:
                        buf = io.BytesIO()
                        resized.save(buf, format="PNG")
            resized_bytes = buf.getvalue()
            valid, _ = BaseImageProvider.validate_image_bytes(
                resized_bytes,
                capabilities=BESTNAI_CAPABILITIES,
                field_name="缩放后的参考图",
            )
            if not valid:
                return b64_data
            return base64.b64encode(resized_bytes).decode("utf-8")
        except Exception:
            return b64_data

    # ================================================================
    # 响应解析
    # ================================================================

    def _handle_response(self, response: requests.Response) -> Tuple[bool, str]:
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            self._logger.error(
                f"{self.log_prefix} (BestNAI) 重定向: "
                f"status={response.status_code}, location={location[:200]}"
            )
            return False, f"HTTP {response.status_code}: 接口发生重定向，请检查 base_url"

        if response.status_code != 200:
            error_message = self._extract_error_message(response)
            self._logger.error(
                f"{self.log_prefix} (BestNAI) HTTP错误 "
                f"{response.status_code}: {error_message[:200]}"
            )
            return False, f"HTTP {response.status_code}: {error_message[:100]}"

        content_type = response.headers.get("content-type", "").lower()
        body = response.content or b""
        looks_like_json = body.lstrip().startswith((b"{", b"["))
        if "application/json" in content_type or "+json" in content_type or looks_like_json:
            try:
                data = response.json()
            except Exception:
                return False, "API 返回的 JSON 数据无效"

            image_value = self._extract_first_image(data)
            if image_value:
                if image_value.startswith(("http://", "https://")):
                    if not self.validate_http_url(image_value):
                        return False, "API 返回的图片 URL 无效"
                    self._logger.info(f"{self.log_prefix} (BestNAI) 图片链接生成成功")
                    return True, image_value

                valid, normalized_image, image_size = self.normalize_and_validate_base64_image(
                    image_value,
                    capabilities=BESTNAI_CAPABILITIES,
                    field_name="生成图片",
                )
                if not valid:
                    return False, normalized_image
                self._logger.info(
                    f"{self.log_prefix} (BestNAI) 图片生成成功，大小 {image_size} bytes"
                )
                return True, normalized_image

            message = self._extract_error_message_from_payload(data) or "未返回图片数据"
            self._logger.error(f"{self.log_prefix} (BestNAI) JSON响应无图片: {message[:200]}")
            return False, message[:200]

        valid, error = self.validate_image_bytes(
            body,
            capabilities=BESTNAI_CAPABILITIES,
            field_name="生成图片",
        )
        if not valid:
            return False, error
        image_base64 = base64.b64encode(body).decode("ascii")
        self._logger.info(
            f"{self.log_prefix} (BestNAI) 图片生成成功，大小 {len(body)} bytes"
        )
        return True, image_base64

    @classmethod
    def _extract_first_image(cls, data: Dict[str, Any]) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        content = cls._extract_message_content(data)
        if not content:
            return None

        data_uri_matches = re.findall(
            r"data:image/(\w+);base64,([A-Za-z0-9+/=]+)", content
        )
        if data_uri_matches:
            return data_uri_matches[0][1]

        direct_match = re.search(r"!\[[^\]]*\]\((https?://[^)]+)\)", content)
        if direct_match:
            return direct_match.group(1)

        if content.startswith(("data:image/", "http://", "https://")):
            return content
        return None

    @staticmethod
    def _extract_message_content(data: Dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0] or {}
        message = first_choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        return ""

    @classmethod
    def _extract_error_message_from_payload(cls, data: Dict[str, Any]) -> str:
        if not isinstance(data, dict):
            return ""
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or ""
            if isinstance(message, str):
                return message
        for key in ("message", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return ""

    @classmethod
    def _extract_error_message(cls, response: requests.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            message = cls._extract_error_message_from_payload(payload)
            if message:
                return message
        text = (response.text or "").strip()
        return text or "未知错误"
