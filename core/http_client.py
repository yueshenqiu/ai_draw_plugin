# -*- coding: utf-8 -*-
"""通用异步 HTTP 客户端（从 core/utils/llm_helper.py 提取）。"""

import asyncio
import ipaddress
import logging
import ssl
from typing import Any, Iterable, Optional

import aiohttp

try:
    import certifi
except ImportError:  # pragma: no cover - requirements normally provides it
    certifi = None

_logger = logging.getLogger("ai_draw_plugin")

# 模块级持久化 Session
_persistent_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


class _SessionHandle:
    """为共享 Session 注入调用方本次指定的默认超时。"""

    def __init__(self, session: aiohttp.ClientSession, timeout_seconds: int):
        self._session = session
        try:
            normalized_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            normalized_timeout = 120.0
        self._timeout = aiohttp.ClientTimeout(
            total=normalized_timeout if normalized_timeout > 0 else 120.0
        )

    def _kwargs(self, kwargs: dict) -> dict:
        if "timeout" not in kwargs:
            kwargs["timeout"] = self._timeout
        return kwargs

    def request(self, method: str, url: str, **kwargs: Any):
        return self._session.request(method, url, **self._kwargs(kwargs))

    def get(self, url: str, **kwargs: Any):
        return self._session.get(url, **self._kwargs(kwargs))

    def post(self, url: str, **kwargs: Any):
        return self._session.post(url, **self._kwargs(kwargs))

    def put(self, url: str, **kwargs: Any):
        return self._session.put(url, **self._kwargs(kwargs))

    def delete(self, url: str, **kwargs: Any):
        return self._session.delete(url, **self._kwargs(kwargs))

    def patch(self, url: str, **kwargs: Any):
        return self._session.patch(url, **self._kwargs(kwargs))

    def head(self, url: str, **kwargs: Any):
        return self._session.head(url, **self._kwargs(kwargs))

    def options(self, url: str, **kwargs: Any):
        return self._session.options(url, **self._kwargs(kwargs))

    async def close(self) -> None:
        """显式关闭底层共享 Session。后续 ``get_session`` 会自动重建。"""
        if not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "_SessionHandle":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def build_ssl_context() -> ssl.SSLContext:
    """构建启用证书验证的 SSL 上下文。

    使用 certifi 提供的 CA 包，解决嵌入式 Python（如 Windows 一键包）缺少系统
    CA 证书的问题——既保留对外部 API（DeepSeek/BestNAI/自定义 LLM）的证书校验，
    又不会因为找不到 CA 而握手失败。
    """
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except (AttributeError, OSError, ssl.SSLError, TypeError) as exc:
            _logger.warning("[HTTP] 无法载入 certifi CA，回退到系统证书库: %s", exc)
    return ssl.create_default_context()


def _build_ssl_context() -> ssl.SSLContext:
    """向后兼容旧的模块内调用；新代码应使用 ``build_ssl_context``。"""
    return build_ssl_context()


def _parse_ip_address(value: Any):
    """解析并规范化 IP 地址；IPv6 zone 和 IPv4-mapped IPv6 可安全比较。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_public_ip(value: Any) -> bool:
    """仅接受可直接路由的公网 IP，拒绝内网、回环及保留地址。"""
    address = _parse_ip_address(value)
    if address is None:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def get_response_peer_ip(response: Any) -> Optional[str]:
    """取得 aiohttp 响应实际连接的对端 IP；无法可靠取得时返回 ``None``。

    aiohttp 不同版本会把 transport 暴露在 connection、私有 connection 或
    protocol 上。这里仅做兼容读取，不从请求 URL/DNS 结果推断实际对端。
    """
    if response is None:
        return None

    transports = []
    try:
        for connection_name in ("connection", "_connection"):
            connection = getattr(response, connection_name, None)
            transport = (
                getattr(connection, "transport", None)
                if connection is not None
                else None
            )
            if transport is not None:
                transports.append(transport)

        for protocol in (
            getattr(response, "_protocol", None),
            getattr(getattr(response, "content", None), "_protocol", None),
        ):
            transport = (
                getattr(protocol, "transport", None)
                if protocol is not None
                else None
            )
            if transport is not None:
                transports.append(transport)
    except (AttributeError, RuntimeError, TypeError):
        return None

    seen = set()
    for transport in transports:
        marker = id(transport)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            peer = transport.get_extra_info("peername")
        except (AttributeError, OSError, RuntimeError, TypeError):
            continue
        if isinstance(peer, (tuple, list)):
            peer = peer[0] if peer else None
        address = _parse_ip_address(peer)
        if address is not None:
            return str(address)
    return None


def response_peer_is_public(response: Any) -> bool:
    """确认响应的实际对端为公网 IP；对端信息缺失时严格返回 ``False``。"""
    return is_public_ip(get_response_peer_ip(response))


def response_peer_matches(
    response: Any,
    allowed_ips: Optional[Iterable[Any]],
    require_public: bool = True,
) -> bool:
    """确认实际对端属于预解析地址集合，防止 DNS rebinding。

    ``allowed_ips`` 中的 IPv6 zone 会被移除，IPv4-mapped IPv6 会被规范化。
    响应对端缺失、集合为空、包含值无法解析或地址不匹配时均 fail-closed。
    """
    peer = _parse_ip_address(get_response_peer_ip(response))
    if peer is None or (require_public and not is_public_ip(peer)):
        return False

    if allowed_ips is None:
        return False
    if isinstance(allowed_ips, (str, bytes, ipaddress.IPv4Address, ipaddress.IPv6Address)):
        candidates = (allowed_ips,)
    else:
        try:
            candidates = tuple(allowed_ips)
        except TypeError:
            return False

    if not candidates:
        return False
    normalized = set()
    for candidate in candidates:
        address = _parse_ip_address(candidate)
        if address is None:
            return False
        normalized.add(address)
    if require_public and any(not is_public_ip(candidate) for candidate in normalized):
        return False
    return peer in normalized


async def get_session(timeout_seconds: int = 120) -> _SessionHandle:
    """获取或创建持久化的 aiohttp ClientSession，复用 TCP 连接池。

    底层连接池跨请求复用；返回的轻量句柄会把 ``timeout_seconds`` 注入本次调用
    发起的请求，因此并发调用不会被第一次创建 Session 的超时配置污染。
    默认对所有 HTTPS 请求启用证书验证（基于 certifi CA 包）。
    """
    global _persistent_session
    if _persistent_session is None or _persistent_session.closed:
        async with _session_lock:
            if _persistent_session is None or _persistent_session.closed:
                connector = aiohttp.TCPConnector(
                    limit=10, limit_per_host=5,
                    ttl_dns_cache=300, keepalive_timeout=60,
                    ssl=build_ssl_context(),  # 启用证书验证（优先 certifi CA 包）
                )
                _persistent_session = aiohttp.ClientSession(
                    # 仅作为未显式传 timeout 的安全兜底；本模块请求均按请求覆盖。
                    timeout=aiohttp.ClientTimeout(total=120), connector=connector,
                )
                _logger.debug("[HTTP] 已创建持久化 Session")
    return _SessionHandle(_persistent_session, timeout_seconds)


async def close_session() -> None:
    """关闭并清空模块级持久 Session，可在插件卸载时调用。"""
    global _persistent_session
    async with _session_lock:
        session = _persistent_session
        _persistent_session = None
    if session is not None and not session.closed:
        await session.close()
        _logger.debug("[HTTP] 已关闭持久化 Session")


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
            session = await get_session()
            request_timeout = aiohttp.ClientTimeout(total=timeout)
            async with session.post(
                url, headers=headers, json=payload, timeout=request_timeout,
            ) as resp:
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
