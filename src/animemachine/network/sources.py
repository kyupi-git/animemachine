"""Compatibility facade over the unified, validated network layer."""
from __future__ import annotations

import hashlib
import threading
import urllib.parse
from typing import Any, Callable, Iterable, TypeVar

from .hedging import first_valid
from .health import Store
from .registry import Endpoint
from .validators import json_bytes

T = TypeVar("T")
_HEALTH: Store | None = None
_HEALTH_LOCK = threading.Lock()


def _health() -> Store:
    global _HEALTH
    if _HEALTH is None:
        with _HEALTH_LOCK:
            if _HEALTH is None:
                _HEALTH = Store()
    return _HEALTH


def _endpoints(values: Iterable[str], service: str) -> list[Endpoint]:
    result: list[Endpoint] = []
    for value in dict.fromkeys(str(item).strip() for item in values if str(item).strip()):
        parsed = urllib.parse.urlparse(value)
        host = parsed.netloc.casefold().replace(":", "-") or "endpoint"
        origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
        fingerprint = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:10]
        result.append(Endpoint(f"{service}-{host}-{fingerprint}", service, value,
                               "user_defined", "never", (service,), True))
    return result


def ordered(endpoints: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in endpoints if str(item).strip()))


def fetch_json(endpoints: Iterable[str], *, timeout: float = 12, cooldown: int = 900,
               headers: dict[str, str] | None = None, attempts: int = 1,
               hedge_delays: tuple[float, ...] = (0, .8, 1.5)) -> tuple[Any, str]:
    del cooldown
    payload, _endpoint, final_url = first_valid(
        _endpoints(endpoints, "json"), capability="json", headers=headers,
        timeout=timeout, validator=lambda data, _mime: json_bytes(data, limit=4 * 1024 * 1024), health=_health(),
        attempts_per_endpoint=attempts, hedge_delays=hedge_delays, max_bytes=4 * 1024 * 1024)
    return payload, final_url


def asset_urls(url: str, proxy_templates: Iterable[str]) -> list[str]:
    values = [url]
    values.extend(str(template).replace("{url}", urllib.parse.quote(url, safe=":/?=&%"))
                  for template in proxy_templates if str(template).strip() and "{url}" in str(template))
    return list(dict.fromkeys(values))


def fetch_binary(endpoints: Iterable[str], *, timeout: float = 30, cooldown: int = 900,
                 limit: int = 8 * 1024 * 1024, headers: dict[str, str] | None = None,
                 validator: Callable[[bytes, str], T] | None = None,
                 attempts: int = 1,
                 hedge_delays: tuple[float, ...] = (0, .8, 1.5)) -> tuple[T | bytes, str, str]:
    del cooldown
    def validate(data: bytes, mime: str):
        if len(data) > limit: raise ValueError("response exceeds configured limit")
        normalized_mime = mime.split(";", 1)[0].strip()
        return validator(data, normalized_mime) if validator else (data, normalized_mime)
    result, _endpoint, final_url = first_valid(
        _endpoints(endpoints, "binary"), capability="binary", headers=headers,
        timeout=timeout, validator=validate, health=_health(), attempts_per_endpoint=attempts,
        hedge_delays=hedge_delays, max_bytes=limit)
    if validator:
        data, mime = result
    else:
        data, mime = result
    return data, mime, final_url
