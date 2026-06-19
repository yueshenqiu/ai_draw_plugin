# -*- coding: utf-8 -*-
"""BestNAI / NovelAI 兼容 Provider。

通过 OpenAI Chat Completions 兼容接口调用 NovelAI 图片生成服务。
从 core/clients/nai_web_client.py 迁移，适配 BaseImageProvider 接口。
"""

import asyncio
import base64
import io
import json
import re
import ssl
from typing import Dict, Any, Tuple, Optional, List

import requests
import certifi
from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError
from urllib3.util.ssl_ import create_urllib3_context

from .base import BaseImageProvider

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


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
        self.session = self._create_session(trust_env=True)
        self.direct_session = self._create_session(trust_env=False)
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
    ) -> Tuple[bool, str]:
        """调用 BestNAI Chat Completions 接口生成图片（异步）。"""
        try:
            if not self.validate_config(model_config):
                return False, "模型配置不完整（缺少 base_url 或 model）"

            base_url = (model_config.get("base_url") or "").rstrip('/')
            if base_url.startswith("http://"):
                self._logger.warning(f"{self.log_prefix} (BestNAI) base_url 为明文 HTTP，API Key 将以明文传输，建议改用 HTTPS")
            endpoint = model_config.get("endpoint") or model_config.get("nai_endpoint") or "/v1/chat/completions"
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
            steps = model_config.get("steps") or model_config.get("num_inference_steps")
            guidance_scale = model_config.get("scale") or model_config.get("guidance_scale")
            cfg_value = model_config.get("cfg") or model_config.get("nai_cfg")
            noise_schedule = model_config.get("noise_schedule") or model_config.get("nai_noise_schedule")
            nocache = model_config.get("nocache") or model_config.get("nai_nocache")
            size_override = model_config.get("size_preset") or model_config.get("nai_size")
            extra_params = model_config.get("extra_params") or model_config.get("nai_extra_params") or {}
            model_name = model_config.get("model") or model_config.get("default_model") or "nai-diffusion-4-5-full"

            final_size = size_override or size

            # 安全清理：NewAPI 不允许非英文内容
            full_prompt = self._sanitize_prompt(full_prompt)
            negative_prompt = self._sanitize_prompt(negative_prompt)
            if artist_prompt:
                artist_prompt = self._sanitize_prompt(artist_prompt)

            # 限制 steps 不超过 NewAPI 最大值 28
            if steps is not None:
                try:
                    steps = min(int(steps), 28)
                except (TypeError, ValueError):
                    steps = 23

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
            )

            # max_tokens 预算
            max_tokens = model_config.get("max_tokens") or 100000
            try:
                max_tokens = int(max_tokens)
            except (TypeError, ValueError):
                max_tokens = 100000
            if ref_image and ref_mode and max_tokens < 50000:
                max_tokens = 100000

            self._logger.info(
                f"{self.log_prefix} (BestNAI) max_tokens={max_tokens} ref_mode={ref_mode}"
            )

            payload = {
                "model": model_name,
                "max_tokens": max_tokens,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(generation_params, ensure_ascii=False),
                    }
                ],
            }
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            self._logger.info(f"{self.log_prefix} (BestNAI) 请求URL: {url}")

            # 异步执行 HTTP 请求
            proxy_mode = self.resolve_proxy_mode(model_config)
            response = await asyncio.to_thread(
                self._send_request, url, headers, payload, proxy_mode
            )

            # 处理响应
            if 300 <= response.status_code < 400:
                location = response.headers.get("location", "")
                self._logger.error(
                    f"{self.log_prefix} (BestNAI) 重定向: status={response.status_code}, location={location}"
                )
                return False, f"HTTP {response.status_code}: 接口发生重定向，请检查 base_url"

            if response.status_code != 200:
                error_message = self._extract_error_message(response)
                self._logger.error(
                    f"{self.log_prefix} (BestNAI) HTTP错误 {response.status_code}: {error_message[:200]}"
                )
                return False, f"HTTP {response.status_code}: {error_message[:100]}"

            # 解析返回内容
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type or response.text.strip().startswith("{"):
                try:
                    data = response.json()
                except Exception:
                    data = {}

                image_value = self._extract_first_image(data)
                if image_value:
                    self._logger.info(f"{self.log_prefix} (BestNAI) 图片生成成功")
                    return True, image_value

                message = self._extract_error_message_from_payload(data) or "未返回图片数据"
                self._logger.error(f"{self.log_prefix} (BestNAI) JSON响应无图片: {message}")
                return False, message

            image_base64 = base64.b64encode(response.content).decode('utf-8')
            self._logger.info(
                f"{self.log_prefix} (BestNAI) 图片生成成功，大小 {len(response.content)} bytes"
            )
            return True, image_base64

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

    def _send_request(self, url: str, headers: dict, payload: dict, proxy_mode: str = "auto"):
        """发送 HTTP 请求（同步，由 asyncio.to_thread 调用）。"""
        if proxy_mode == "direct":
            return self._request_with_session(False, url, headers, payload)

        if proxy_mode == "inherit":
            return self._request_with_session(True, url, headers, payload)

        if getattr(self, "_auto_proxy_direct_only", False):
            return self._request_with_session(False, url, headers, payload)

        try:
            return self._request_with_session(True, url, headers, payload)
        except requests.RequestException as exc:
            if not self._is_proxy_related_exception(exc):
                raise
            self._auto_proxy_direct_only = True
            self._logger.warning(f"{self.log_prefix} (BestNAI) 代理失败，回退直连: {exc}")
            return self._request_with_session(False, url, headers, payload)

    def _request_with_session(self, trust_env: bool, url: str, headers: dict, payload: dict):
        session = self._get_session(trust_env=trust_env)
        return session.post(
            url=url, headers=headers, json=payload,
            timeout=120, allow_redirects=False,
        )

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
    ) -> Dict[str, Any]:
        """构造 NewAPI 绘图参数。"""
        combined_prompt = prompt.strip()
        if artist_prompt:
            combined_prompt = f"{combined_prompt}, {artist_prompt.strip()}"

        params: Dict[str, Any] = {
            "model": model or "nai-diffusion-4-5-full",
            "prompt": combined_prompt,
            "n_samples": 1,
        }

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
        if isinstance(cfg_value, (int, float)) and 0 <= float(cfg_value) <= 1 and "cfg_rescale" not in (extra_params or {}):
            params["cfg_rescale"] = float(cfg_value)
        if nocache is not None and "nocache" not in (extra_params or {}):
            params["nocache"] = nocache
        if isinstance(extra_params, dict):
            for key, value in extra_params.items():
                if value not in (None, ""):
                    params[key] = value

        # 图生图 / 参考模式
        if ref_image and ref_mode:
            if ref_image.startswith(("data:", "http://", "https://")):
                image_uri = ref_image
            else:
                image_uri = self._to_data_uri(ref_image)
            if ref_mode == "i2i":
                if params.get("size"):
                    ref_image = self._resize_to_match(ref_image, params["size"])
                    image_uri = self._to_data_uri(ref_image)
                params["i2i"] = {"image": image_uri, "strength": 0.5, "noise": 0}
                self._logger.info(f"{self.log_prefix} (BestNAI) i2i strength=0.5 size={params.get('size')}")
            elif ref_mode == "style":
                params["controlnet"] = {
                    "strength": 1.0,
                    "images": [{"image": image_uri, "info_extracted": 0.7, "strength": 0.6}],
                }
            elif ref_mode == "character":
                params["character_references"] = [{
                    "image": image_uri, "type": "character", "fidelity": 1.0, "strength": 1.0,
                }]
            elif ref_mode == "character&style":
                params["character_references"] = [{
                    "image": image_uri, "type": "character&style", "fidelity": 1.0, "strength": 1.0,
                }]

        return params

    # ================================================================
    # 尺寸 & 图片工具
    # ================================================================

    @staticmethod
    def _normalize_size(size: Optional[str]) -> Optional[List[int]]:
        if not size:
            return None
        if isinstance(size, (list, tuple)) and len(size) == 2:
            try:
                return [int(size[0]), int(size[1])]
            except (TypeError, ValueError):
                return None

        size_text = str(size).strip().lower().replace("×", "x")
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
        if b64.startswith("R0lGOD"):
            return f"data:image/gif;base64,{b64}"
        return f"data:image/png;base64,{b64}"

    @staticmethod
    def _resize_to_match(b64_data: str, target_size: List[int]) -> str:
        if not _HAS_PIL or not target_size or len(target_size) != 2:
            return b64_data
        try:
            raw = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(raw))
            tw, th = target_size[0], target_size[1]
            iw, ih = img.size
            if iw == tw and ih == th:
                return b64_data
            target_ratio = tw / th
            current_ratio = iw / ih
            if current_ratio > target_ratio:
                new_h = th
                new_w = int(iw * th / ih)
            else:
                new_w = tw
                new_h = int(ih * tw / iw)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - tw) // 2
            top = (new_h - th) // 2
            img = img.crop((left, top, left + tw, top + th))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            return b64_data

    # ================================================================
    # 响应解析
    # ================================================================

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
