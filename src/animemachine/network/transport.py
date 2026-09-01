"""Shared HTTP/2 transport with verified TLS, proxy discovery and bounded reads."""
from __future__ import annotations

import contextlib
import ipaddress
import threading
import urllib.parse
import urllib.request
from email.message import Message
from typing import Any

import httpx

from . import tls


_LOCK = threading.RLock()
_CLIENTS: dict[str, httpx.Client] = {}
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_HOST_LIMIT = 0
_HOST_LOCK = threading.Lock()
_HOST_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}


def _proxy_settings() -> dict[str, str]:
    return {str(key).casefold(): str(value).strip() for key, value in urllib.request.getproxies().items()
            if str(value).strip()}


def _proxy_for_url(url: str, proxies: dict[str, str]) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_private or address.is_link_local:
            return None
    if host.casefold() == "localhost" or (host and urllib.request.proxy_bypass(host)):
        return None
    return proxies.get(parsed.scheme.casefold()) or proxies.get("all")


def client(url: str | None = None) -> httpx.Client:
    """Return a pooled client selected from proxy settings read for this request."""
    proxies = _proxy_settings()
    proxy = _proxy_for_url(url, proxies) if url else None
    key = proxy or "__direct__"
    with _LOCK:
        current = _CLIENTS.get(key)
        if current is None:
            current = httpx.Client(http2=True, follow_redirects=True, trust_env=False, proxy=proxy,
                                   verify=tls.ssl_context(), limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
            _CLIENTS[key] = current
        return current


def reset() -> None:
    with _LOCK:
        for current in _CLIENTS.values():
            current.close()
        _CLIENTS.clear()
        tls.ssl_context.cache_clear()


def configure_host_limit(limit: int) -> None:
    """Set an optional per-host limit for the current process only."""
    global _HOST_LIMIT
    with _HOST_LOCK:
        _HOST_LIMIT = max(0, int(limit))
        _HOST_SEMAPHORES.clear()


@contextlib.contextmanager
def host_slot(url: str):
    host = urllib.parse.urlparse(url).netloc.casefold()
    with _HOST_LOCK:
        semaphore = None if not _HOST_LIMIT else _HOST_SEMAPHORES.setdefault(
            host, threading.BoundedSemaphore(_HOST_LIMIT))
    if semaphore is None:
        yield
        return
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


def request(method: str, url: str, *, headers: dict[str, str] | None = None, content: bytes | None = None,
            timeout: float | httpx.Timeout = 15, max_bytes: int = 16 * 1024 * 1024,
            allow_credentials: bool = False) -> httpx.Response:
    values = dict(headers or {})
    # Some transparent proxies and CDN relays advertise gzip while returning an
    # uncompressed body. httpx then fails before validators can inspect it.
    # These payloads are already size-bounded, so prefer a deterministic body.
    if not any(key.casefold() == "accept-encoding" for key in values):
        values["Accept-Encoding"] = "identity"
    if not allow_credentials:
        for key in list(values):
            if key.casefold() in {"authorization", "proxy-authorization", "x-api-key", "api-key", "apikey"}:
                values.pop(key)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    with host_slot(url):
        with client(url).stream(method, url, headers=values, content=content, timeout=timeout) as streamed:
            streamed.raise_for_status()
            declared = streamed.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ValueError("response exceeds configured limit")
            chunks: list[bytes] = []
            received = 0
            for chunk in streamed.iter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError("response exceeds configured limit")
                chunks.append(chunk)
            return httpx.Response(
                streamed.status_code, headers=streamed.headers, content=b"".join(chunks),
                request=streamed.request, extensions=dict(streamed.extensions),
            )


def is_retryable(exc: Exception) -> bool:
    """Classify transient transport failures consistently for every consumer."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.RequestError, TimeoutError, ConnectionError, OSError))


class Headers:
    def __init__(self, values: httpx.Headers): self._values = values
    def get(self, name: str, default: Any = None): return self._values.get(name, default)
    def get_content_type(self): return self._values.get("content-type", "application/octet-stream").split(";", 1)[0].strip()
    def items(self): return self._values.items()


class OpenResponse:
    def __init__(self, response: httpx.Response):
        self.response = response; self.headers = Headers(response.headers); self.status = response.status_code; self._offset = 0
    def read(self, size: int = -1) -> bytes:
        content = self.response.content
        if size < 0:
            value = content[self._offset:]; self._offset = len(content); return value
        value = content[self._offset:self._offset + size]; self._offset += len(value); return value
    def geturl(self) -> str: return str(self.response.url)
    def close(self): self.response.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def open_url(target: Any, *, timeout: float = 15, max_bytes: int = 64 * 1024 * 1024,
             allow_credentials: bool = True) -> OpenResponse:
    url = str(getattr(target, "full_url", target))
    method = str(getattr(target, "get_method", lambda: "GET")())
    headers = dict(getattr(target, "header_items", lambda: [])())
    content = getattr(target, "data", None)
    return OpenResponse(request(method, url, headers=headers, content=content, timeout=timeout,
                                max_bytes=max_bytes, allow_credentials=allow_credentials))
