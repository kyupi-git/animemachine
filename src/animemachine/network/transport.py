"""Shared HTTP/2 transport with verified TLS, live proxy discovery and bounded reads."""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import httpx

from . import connectivity, tls


_LOCK = threading.RLock()
_CLIENTS: dict[str, httpx.Client] = {}
_PROXY_SIGNATURE: tuple[tuple[str, str], ...] | None = None
_PROXY_REVISION = 0
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_ROUTE_FALLBACK_STATUS = _RETRYABLE_STATUS | {407}
_HOST_LIMIT = 0
_HOST_LOCK = threading.Lock()
_HOST_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_BANDWIDTH_LOCAL = threading.local()
_BANDWIDTH_LOCK = threading.Lock()
_BANDWIDTH_NEXT_AT = 0.0
_NETWORK_BASE_CACHE: tuple[float, dict[str, str]] | None = None
_ROUTE_LOCK = threading.Lock()
_ROUTE_STATE: dict[tuple[int, str], dict[str, Any]] = {}
_ROUTE_PREFERENCE_SECONDS = 300.0


def _normalized_proxies(values: dict[str, str] | None) -> dict[str, str]:
    return {
        str(key).casefold(): str(value).strip()
        for key, value in (values or {}).items()
        if str(value).strip()
    }


def _environment_proxies() -> dict[str, str]:
    return _normalized_proxies(urllib.request.getproxies_environment())


def _system_proxies() -> dict[str, str]:
    if os.name == "nt":
        registry_getter = getattr(urllib.request, "getproxies_registry", None)
        return _normalized_proxies(registry_getter() if callable(registry_getter) else {})
    # Python exposes native system proxy discovery on macOS through getproxies().
    # Remove environment values so their source remains diagnosable.
    if sys_platform() == "darwin":
        discovered = _normalized_proxies(urllib.request.getproxies())
        environment = _environment_proxies()
        return {key: value for key, value in discovered.items() if environment.get(key) != value}
    return {}


def sys_platform() -> str:
    import sys
    return sys.platform


def _proxy_snapshot() -> tuple[dict[str, str], dict[str, str], int]:
    """Return environment/system proxy maps and a live configuration revision."""
    global _PROXY_SIGNATURE, _PROXY_REVISION
    environment = _environment_proxies()
    system = _system_proxies()
    signature = tuple(sorted(
        [(f"env:{key}", value) for key, value in environment.items()]
        + [(f"system:{key}", value) for key, value in system.items()]
    ))
    with _LOCK:
        if signature != _PROXY_SIGNATURE:
            _PROXY_SIGNATURE = signature
            _PROXY_REVISION += 1
        revision = _PROXY_REVISION
    return environment, system, revision


def proxy_revision() -> int:
    return _proxy_snapshot()[2]


def _proxy_settings() -> dict[str, str]:
    """Return the effective proxy map, including live Windows system-proxy changes."""
    environment, system, _revision = _proxy_snapshot()
    effective = dict(system)
    effective.update(environment)
    return effective


def _local_target(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_private or address.is_link_local:
            return True
    if host in {"localhost", "qbittorrent", "ani-rss", "host.docker.internal", "gateway.docker.internal"}:
        return True
    return host.endswith((".localhost", ".local", ".lan", ".home.arpa"))


def _offline_request_guard(request: httpx.Request) -> None:
    """Apply local-mode policy to every HTTPX hop, including automatic redirects."""
    url = str(request.url)
    if connectivity.is_offline() and not connectivity.recovery_allowed() and not _local_target(url):
        raise connectivity.OfflineModeError(
            "remote network requests are paused while AnimeMachine is in local mode"
        )


def _local_or_bypassed(url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if _local_target(url):
        return True, "local"
    if host and urllib.request.proxy_bypass(host):
        return True, "bypass"
    return False, ""


def _proxy_value(url: str, proxies: dict[str, str]) -> str | None:
    parsed = urllib.parse.urlparse(url)
    return proxies.get(parsed.scheme.casefold()) or proxies.get("all")


def _raw_route_candidates(url: str) -> list[dict[str, Any]]:
    environment, system, revision = _proxy_snapshot()
    bypassed, reason = _local_or_bypassed(url)
    if bypassed:
        return [{"mode": "direct", "proxy": None, "revision": revision, "reason": reason}]
    values: list[dict[str, Any]] = []
    environment_proxy = _proxy_value(url, environment)
    system_proxy = _proxy_value(url, system)
    if environment_proxy:
        values.append({"mode": "environment_proxy", "proxy": environment_proxy,
                       "revision": revision, "reason": "environment"})
    if system_proxy and system_proxy != environment_proxy:
        mode = "windows_system_proxy" if os.name == "nt" else "system_proxy"
        values.append({"mode": mode, "proxy": system_proxy, "revision": revision, "reason": "system"})
    values.append({"mode": "direct", "proxy": None, "revision": revision, "reason": "fallback" if values else "none"})
    return values


def _route_key(url: str, revision: int) -> tuple[int, str]:
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), "", "", ""))
    return revision, origin


def _route_candidates(url: str) -> list[dict[str, Any]]:
    values = _raw_route_candidates(url)
    if len(values) <= 1:
        return values
    key = _route_key(url, int(values[0]["revision"]))
    now = time.monotonic()
    with _ROUTE_LOCK:
        state = dict(_ROUTE_STATE.get(key) or {})
    preferred = str(state.get("preferred") or "")
    preferred_until = float(state.get("preferredUntil") or 0.0)
    cooldowns = dict(state.get("cooldowns") or {})
    if preferred and preferred_until > now:
        values.sort(key=lambda item: (str(item["mode"]) != preferred, float(cooldowns.get(str(item["mode"]), 0.0)) > now))
    else:
        values.sort(key=lambda item: float(cooldowns.get(str(item["mode"]), 0.0)) > now)
    return values


def route_candidates(url: str) -> list[dict[str, Any]]:
    """Return all usable routes in current preference order, with proxy credentials redacted."""
    return [{**item, "proxy": _safe_proxy(item.get("proxy"))} for item in _route_candidates(url)]


def _remember_route(url: str, route: dict[str, Any], *, ok: bool) -> None:
    if not ok and not connectivity.failure_learning_allowed():
        return
    key = _route_key(url, int(route["revision"]))
    mode = str(route["mode"])
    now = time.monotonic()
    with _ROUTE_LOCK:
        state = _ROUTE_STATE.setdefault(key, {"failures": {}, "cooldowns": {}})
        failures = state.setdefault("failures", {})
        cooldowns = state.setdefault("cooldowns", {})
        if ok:
            failures[mode] = 0
            cooldowns.pop(mode, None)
            state["preferred"] = mode
            state["preferredUntil"] = now + _ROUTE_PREFERENCE_SECONDS
        else:
            count = int(failures.get(mode, 0)) + 1
            failures[mode] = count
            cooldowns[mode] = now + min(120.0, 5.0 * (2 ** min(count - 1, 4)))
            if state.get("preferred") == mode:
                state["preferredUntil"] = 0.0


def _proxy_decision(url: str) -> tuple[str, str | None, int, str]:
    route = _route_candidates(url)[0]
    return str(route["mode"]), route.get("proxy"), int(route["revision"]), str(route["reason"])


def _safe_proxy(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port_value = parsed.port
    except ValueError:
        return "<invalid-proxy>"
    if not parsed.scheme or not host:
        return "<invalid-proxy>"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{port_value}" if port_value else ""
    return urllib.parse.urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, parsed.query, parsed.fragment))


def proxy_route(url: str) -> dict[str, Any]:
    """Describe the route AnimeMachine will actually use for this URL."""
    mode, proxy, revision, reason = _proxy_decision(url)
    return {"mode": mode, "proxy": _safe_proxy(proxy), "revision": revision, "reason": reason}


def _active_network_base() -> dict[str, str]:
    """Return a privacy-bounded identity for the active local network."""
    global _NETWORK_BASE_CACHE
    now = time.monotonic()
    local_address = ""
    with contextlib.suppress(OSError):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("1.1.1.1", 53))
            local_address = str(probe.getsockname()[0] or "")
        finally:
            probe.close()
    with _LOCK:
        if (
            _NETWORK_BASE_CACHE is not None
            and now - _NETWORK_BASE_CACHE[0] < 30.0
            and _NETWORK_BASE_CACHE[1].get("localAddress", "") == local_address
        ):
            return dict(_NETWORK_BASE_CACHE[1])

    interface = ""
    network_name = ""
    kind = "network"
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=1.5,
                errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = completed.stdout or ""
            match = re.search(r"(?mi)^\s*SSID\s*:\s*(.+?)\s*$", text)
            if match and match.group(1).strip():
                network_name = match.group(1).strip()
                kind = "wifi"
        except (OSError, subprocess.SubprocessError):
            pass
        if not network_name:
            try:
                completed = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$p=Get-NetConnectionProfile | Where-Object {$_.IPv4Connectivity -ne 'Disconnected' -or $_.IPv6Connectivity -ne 'Disconnected'} | Select-Object -First 1; if($p){$p.Name}",
                    ],
                    capture_output=True, text=True, timeout=1.5, errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    network_name = completed.stdout.strip().splitlines()[0].strip()
                    kind = "lan"
            except (OSError, subprocess.SubprocessError):
                pass
    elif _PROC_ROUTE.exists():
        try:
            for line in _PROC_ROUTE.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    interface = fields[0]
                    kind = "wifi" if interface.casefold().startswith(("wl", "wlan")) else "lan"
                    break
        except (OSError, ValueError):
            pass

    if kind == "network" and local_address:
        kind = "lan"

    subnet = local_address
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(local_address)
        if isinstance(address, ipaddress.IPv4Address):
            subnet = str(ipaddress.ip_network(f"{address}/24", strict=False).network_address) + "/24"
        elif isinstance(address, ipaddress.IPv6Address):
            subnet = str(ipaddress.ip_network(f"{address}/64", strict=False).network_address) + "/64"
    label = network_name or interface or subnet or "unknown"
    base_key = "|".join((kind, network_name, interface, subnet))
    result = {"kind": kind, "label": label, "baseKey": base_key, "localAddress": local_address}
    with _LOCK:
        _NETWORK_BASE_CACHE = (now, dict(result))
    return result


# Kept as a module constant so tests can replace it without filesystem side effects.
_PROC_ROUTE = Path("/proc/net/route")


def network_environment_id() -> str:
    """Return a private fingerprint of the current LAN/Wi-Fi and proxy configuration."""
    base = _active_network_base()
    environment, system, _revision = _proxy_snapshot()
    proxy_material = tuple(sorted(
        [(f"env:{key}", value) for key, value in environment.items()]
        + [(f"system:{key}", value) for key, value in system.items()]
    ))
    material = repr((base["baseKey"], proxy_material))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def network_profile(url: str, route_mode: str | None = None) -> dict[str, str]:
    """Identify the LAN/Wi-Fi plus a proxy route for health learning."""
    base = _active_network_base()
    if route_mode is None:
        mode, proxy, _revision, _reason = _proxy_decision(url)
    else:
        environment, system, _revision = _proxy_snapshot()
        mode = str(route_mode)
        if mode == "environment_proxy":
            proxy = _proxy_value(url, environment)
        elif mode in {"windows_system_proxy", "system_proxy"}:
            proxy = _proxy_value(url, system)
        else:
            mode, proxy = "direct", None
    safe_proxy = _safe_proxy(proxy)
    material = "|".join((base["baseKey"], mode, safe_proxy))
    profile_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return {
        "id": profile_id,
        "kind": base["kind"],
        "label": base["label"],
        "routeMode": mode,
        "proxy": safe_proxy,
    }


def _proxy_for_url(url: str, proxies: dict[str, str]) -> str | None:
    bypassed, _reason = _local_or_bypassed(url)
    return None if bypassed else _proxy_value(url, proxies)


def _client_for(proxy: str | None, _revision: int) -> httpx.Client:
    key = proxy or "__direct__"
    with _LOCK:
        current = _CLIENTS.get(key)
        if current is None:
            current = httpx.Client(
                http2=True,
                follow_redirects=True,
                trust_env=False,
                proxy=proxy,
                verify=tls.ssl_context(),
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
                event_hooks={"request": [_offline_request_guard]},
            )
            _CLIENTS[key] = current
        return current


def client(url: str | None = None, *, route_mode: str | None = None) -> httpx.Client:
    """Return a pooled client selected from live proxy settings for this request."""
    if url:
        if route_mode is None:
            _mode, proxy, revision, _reason = _proxy_decision(url)
        else:
            routes = _raw_route_candidates(url)
            route = next((item for item in routes if str(item["mode"]) == str(route_mode)), None)
            if route is None:
                route = next(item for item in routes if item["mode"] == "direct")
            proxy, revision = route.get("proxy"), int(route["revision"])
    else:
        _environment, _system, revision = _proxy_snapshot()
        proxy = None
    return _client_for(proxy, revision)


def reset() -> None:
    global _PROXY_SIGNATURE, _PROXY_REVISION, _NETWORK_BASE_CACHE
    with _LOCK:
        for current in _CLIENTS.values():
            current.close()
        _CLIENTS.clear()
        _PROXY_SIGNATURE = None
        _PROXY_REVISION = 0
        _NETWORK_BASE_CACHE = None
        tls.ssl_context.cache_clear()
    with _ROUTE_LOCK:
        _ROUTE_STATE.clear()


def configure_host_limit(limit: int) -> None:
    """Set an optional per-host limit for the current process only."""
    global _HOST_LIMIT
    with _HOST_LOCK:
        _HOST_LIMIT = max(0, int(limit))
        _HOST_SEMAPHORES.clear()


@contextlib.contextmanager
def bandwidth_limit(bytes_per_second: int):
    """Limit aggregate transfer rate for requests made by the current thread."""
    previous = getattr(_BANDWIDTH_LOCAL, "bytes_per_second", 0)
    _BANDWIDTH_LOCAL.bytes_per_second = max(0, int(bytes_per_second))
    try:
        yield
    finally:
        _BANDWIDTH_LOCAL.bytes_per_second = previous


def _throttle(byte_count: int) -> None:
    global _BANDWIDTH_NEXT_AT
    limit = int(getattr(_BANDWIDTH_LOCAL, "bytes_per_second", 0) or 0)
    if limit <= 0 or byte_count <= 0:
        return
    with _BANDWIDTH_LOCK:
        now = time.monotonic()
        start_at = max(now, _BANDWIDTH_NEXT_AT)
        _BANDWIDTH_NEXT_AT = start_at + (byte_count / limit)
        delay = start_at - now
    if delay > 0:
        time.sleep(delay)


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


def _request_headers(headers: dict[str, str] | None, *, allow_credentials: bool) -> dict[str, str]:
    values = dict(headers or {})
    if not any(key.casefold() == "accept-encoding" for key in values):
        values["Accept-Encoding"] = "identity"
    if not allow_credentials:
        for key in list(values):
            if key.casefold() in {"authorization", "proxy-authorization", "x-api-key", "api-key", "apikey"}:
                values.pop(key)
    return values


@contextlib.contextmanager
def stream(method: str, url: str, *, headers: dict[str, str] | None = None, content: bytes | None = None,
           timeout: float | httpx.Timeout = 15, allow_credentials: bool = False,
           follow_redirects: bool | None = None):
    """Open a streaming response, falling back across live proxy/system/direct routes before yielding."""
    values = _request_headers(headers, allow_credentials=allow_credentials)
    local_target = _local_target(url)
    if not local_target and connectivity.outage_suspected():
        connectivity.note_environment(network_environment_id())
    needs_recovery_permit = connectivity.is_offline() and not connectivity.recovery_allowed() and not local_target
    recovery_context = connectivity.opportunistic_recovery() if needs_recovery_permit else contextlib.nullcontext(False)
    with recovery_context as opportunistic:
        if connectivity.is_offline() and not connectivity.recovery_allowed() and not local_target:
            raise connectivity.OfflineModeError("remote network requests are paused while AnimeMachine is in local mode")
        routes = _route_candidates(url)
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        with host_slot(url):
            for index, route in enumerate(routes):
                kwargs: dict[str, Any] = {"headers": values, "content": content, "timeout": timeout}
                if follow_redirects is not None:
                    kwargs["follow_redirects"] = follow_redirects
                try:
                    manager = client(url, route_mode=str(route["mode"])).stream(method, url, **kwargs)
                    response = manager.__enter__()
                except (httpx.RequestError, httpx.InvalidURL, OSError, TimeoutError, ValueError, ImportError) as exc:
                    last_error = exc
                    attempts.append({"mode": str(route["mode"]), "status": "", "error": type(exc).__name__})
                    _remember_route(url, route, ok=False)
                    continue
                status = int(response.status_code)
                if not local_target and status < 500 and status != 407 and (
                    not connectivity.recovery_allowed() or opportunistic
                ):
                    connectivity.note_online_activity()
                if status in _ROUTE_FALLBACK_STATUS and index + 1 < len(routes):
                    attempts.append({"mode": str(route["mode"]), "status": status, "error": f"HTTP {status}"})
                    _remember_route(url, route, ok=False)
                    manager.__exit__(None, None, None)
                    continue
                route_info = {**route, "proxy": _safe_proxy(route.get("proxy"))}
                attempts.append({"mode": str(route["mode"]), "status": status,
                                 "error": f"HTTP {status}" if status in _ROUTE_FALLBACK_STATUS else ""})
                extensions = getattr(response, "extensions", None)
                if isinstance(extensions, dict):
                    extensions["animemachine_proxy_route"] = route_info
                    extensions["animemachine_route_attempts"] = list(attempts)
                if status in _ROUTE_FALLBACK_STATUS:
                    _remember_route(url, route, ok=False)
                try:
                    yield response
                except (httpx.RequestError, OSError, TimeoutError):
                    _remember_route(url, route, ok=False)
                    raise
                else:
                    if status not in _ROUTE_FALLBACK_STATUS:
                        _remember_route(url, route, ok=True)
                finally:
                    manager.__exit__(None, None, None)
                return
        if last_error is not None:
            raise last_error
        raise httpx.ConnectError("no usable network route", request=httpx.Request(method, url))


def request(method: str, url: str, *, headers: dict[str, str] | None = None, content: bytes | None = None,
            timeout: float | httpx.Timeout = 15, max_bytes: int = 16 * 1024 * 1024,
            allow_credentials: bool = False) -> httpx.Response:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    with stream(method, url, headers=headers, content=content, timeout=timeout,
                allow_credentials=allow_credentials) as streamed:
        streamed.raise_for_status()
        declared = streamed.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise ValueError("response exceeds configured limit")
        chunks: list[bytes] = []
        received = 0
        for chunk in streamed.iter_bytes():
            _throttle(len(chunk))
            received += len(chunk)
            if received > max_bytes:
                raise ValueError("response exceeds configured limit")
            chunks.append(chunk)
        return httpx.Response(
            streamed.status_code, headers=streamed.headers, content=b"".join(chunks), request=streamed.request,
            extensions=dict(streamed.extensions),
        )


def is_retryable(exc: Exception) -> bool:
    """Classify transient transport failures consistently for every consumer."""
    if isinstance(exc, connectivity.OfflineModeError):
        return False
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
