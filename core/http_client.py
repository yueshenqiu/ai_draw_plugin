# -*- coding: utf-8 -*-
"""通用异步 HTTP 客户端（从 core/utils/llm_helper.py 提取）。"""

import asyncio
import logging
import ssl
from typing import Optional

import aiohttp
import certifi

_logger = logging.getLogger("ai_draw_plugin")

# 模块级持久化 Session
_persistent_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


def _build_ssl_context() -> ssl.SSLContext:
    """构建启用证书验证的 SSL 上下文。

    使用 certifi 提供的 CA 包，解决嵌入式 Python（如 Windows 一键包）缺少系统
    CA 证书的问题——既保留对外部 API（DeepSeek/BestNAI/自定义 LLM）的证书校验，
    又不会因为找不到 CA 而握手失败。
    """
    return ssl.create_default_context(cafile=certifi.where())


async def get_session(timeout_seconds: int = 120) -> aiohttp.ClientSession:
    """获取或创建持久化的 aiohttp ClientSession，复用 TCP 连接池。

    默认对所有 HTTPS 请求启用证书验证（基于 certifi CA 包）。
    """
    global _persistent_session
    if _persistent_session is None or _persistent_session.closed:
        async with _session_lock:
            if _persistent_session is None or _persistent_session.closed:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                connector = aiohttp.TCPConnector(
                    limit=10, limit_per_host=5,
                    ttl_dns_cache=300, keepalive_timeout=60,
                    ssl=_build_ssl_context(),  # 启用证书验证（certifi CA 包）
                )
                _persistent_session = aiohttp.ClientSession(
                    timeout=timeout, connector=connector,
                )
                _logger.debug("[HTTP] 已创建持久化 Session")
    return _persistent_session


async def http_post_json(
    url: str,
    headers: dict,
    payload: dict,
    timeout: int = 120,
    max_retries: int = 3,
) -> tuple[bool, int, str]:
    """发送异步 HTTP POST JSON 请求，带自动重试。

    Returns:
        (success, status_code, response_text)
    """
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            session = await get_session(timeout)
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status < 500:
                    return True, resp.status, text
                last_error = f"HTTP {resp.status}: {text[:300]}"
                _logger.warning(f"[HTTP] 第 {attempt} 次请求失败 ({last_error})")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                continue
        except asyncio.TimeoutError:
            last_error = f"请求超时 ({timeout}s)"
            _logger.warning(f"[HTTP] 第 {attempt} 次超时")
            if attempt < max_retries:
                await asyncio.sleep(2)
                continue
        except aiohttp.ClientConnectorError as e:
            last_error = f"连接失败: {str(e)[:200]}"
            _logger.warning(f"[HTTP] 第 {attempt} 次连接失败: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)
                continue
        except Exception as e:
            _logger.error(f"[HTTP] 未知错误: {e}", exc_info=True)
            return False, 0, f"请求异常: {str(e)[:300]}"

    return False, 0, f"重试 {max_retries} 次后仍失败: {last_error}"
