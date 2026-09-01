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
                retry_backoff: float = .4, max_bytes: int = 16 * 1024 * 1024) -> tuple[T, Endpoint, str]:
    store = health or Store()
    ranked = sorted(endpoints, key=lambda item: store.score(item.id, capability))
    errors: list[str] = []
    winner = threading.Event()

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
                response = transport.request("GET", item.base_url + path, headers=safe_headers, timeout=timeout,
                                             max_bytes=max_bytes,
                                             allow_credentials=bool(credentials and may_send_credentials(item)))
                result = validator(response.content, response.headers.get("content-type", ""))
                store.success(item.id, capability, time.monotonic() - started, len(response.content))
                winner.set()
                return result, item, str(response.url)
            except concurrent.futures.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                store.failure(item.id, capability, type(exc).__name__)
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
