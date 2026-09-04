"""Validated delayed-hedge requests over independent endpoints."""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Callable, TypeVar

from .health import Store
from .registry import Endpoint, may_send_credentials
from . import transport

T = TypeVar("T")


def first_valid(endpoints: list[Endpoint], path: str = "", *, capability: str = "json",
                validator: Callable[[bytes, str], T], headers: dict[str, str] | None = None,
                credentials: bool = False, timeout: float = 8, hedge_delays: tuple[float, ...] = (0, .8, 1.5),
                health: Store | None = None, attempts_per_endpoint: int = 1,
                retry_backoff: float = .4, max_bytes: int = 16 * 1024 * 1024,
                honor_cooldown: bool = False) -> tuple[T, Endpoint, str]:
    store = health or Store()
    candidate_routes = []
    for item in endpoints:
        target = item.base_url + path
        profile = transport.network_profile(target)
        candidate_routes.append((item.id, str(profile["routeMode"]), str(profile["id"])))
    ranked_ids, _selection = store.rank(candidate_routes, capability)
    by_id = {item.id: item for item in endpoints}
    ranked = [by_id[endpoint_id] for endpoint_id in ranked_ids if endpoint_id in by_id]
    if honor_cooldown:
        available = []
        for item in ranked:
            target = item.base_url + path
            profile = transport.network_profile(target)
            evaluation = store.evaluation(item.id, capability, str(profile["routeMode"]), str(profile["id"]))
            if not evaluation.get("coolingDown"):
                available.append(item)
        if not available:
            raise RuntimeError("all endpoints are cooling down")
        ranked = available
    errors: list[str] = []
    winner = threading.Event()

    def record_route_failures(item: Endpoint, target_url: str, capability_name: str, attempts: object) -> None:
        for attempt in attempts if isinstance(attempts, list) else []:
            if not isinstance(attempt, dict) or not attempt.get("error"):
                continue
            route_mode = str(attempt.get("mode") or "direct")
            profile = transport.network_profile(target_url, route_mode)
            store.failure(item.id, capability_name, str(attempt.get("error") or "route_failure"),
                          route_mode=route_mode, network_id=str(profile["id"]))

    def one(item: Endpoint, delay: float):
        if delay and winner.wait(delay):
            raise concurrent.futures.CancelledError()
        if winner.is_set():
            raise concurrent.futures.CancelledError()
        safe_headers = headers if (not credentials or may_send_credentials(item)) else {}
        attempts = max(1, min(8, int(attempts_per_endpoint)))
        last_error: Exception | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                if winner.is_set():
                    raise concurrent.futures.CancelledError()
                target_url = item.base_url + path
                profile = transport.network_profile(target_url)
                route_mode = profile["routeMode"]
                network_id = profile["id"]
                response = transport.request("GET", target_url, headers=safe_headers, timeout=timeout,
                                             max_bytes=max_bytes,
                                             allow_credentials=bool(credentials and may_send_credentials(item)))
                result = validator(response.content, response.headers.get("content-type", ""))
                extensions = getattr(response, "extensions", {}) or {}
                record_route_failures(item, target_url, capability, extensions.get("animemachine_route_attempts"))
                actual_route = dict(extensions.get("animemachine_proxy_route") or {})
                route_mode = str(actual_route.get("mode") or route_mode)
                actual_profile = transport.network_profile(target_url, route_mode)
                store.success(item.id, capability, time.monotonic() - started, len(response.content),
                              route_mode=route_mode, network_id=str(actual_profile.get("id") or network_id))
                winner.set()
                return result, item, str(response.url)
            except concurrent.futures.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                target_url = item.base_url + path
                response = getattr(exc, "response", None)
                extensions = getattr(response, "extensions", {}) or {}
                record_route_failures(item, target_url, capability, extensions.get("animemachine_route_attempts"))
                failed_profile = transport.network_profile(target_url)
                store.failure(item.id, capability, type(exc).__name__,
                              route_mode=str(failed_profile["routeMode"]), network_id=str(failed_profile["id"]))
                if attempt + 1 >= attempts or not transport.is_retryable(exc):
                    break
                time.sleep(max(0.0, retry_backoff) * (2 ** attempt))
        assert last_error is not None
        raise last_error

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(ranked) or 1))
    try:
        futures = {pool.submit(one, item, hedge_delays[min(index, len(hedge_delays)-1)]): item for index, item in enumerate(ranked)}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                for pending in futures: pending.cancel()
                return result
            except concurrent.futures.CancelledError:
                continue
            except Exception as exc:
                errors.append(f"{futures[future].id}:{type(exc).__name__}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError("all endpoints failed: " + "; ".join(errors))
