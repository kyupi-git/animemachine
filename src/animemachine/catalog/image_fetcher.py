"""Process-isolated, prioritized and adaptive background cover downloads."""
from __future__ import annotations

import concurrent.futures
import contextlib
import heapq
import multiprocessing
import os
import queue
import signal
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _worker_main(db_path: str, requests: Any, results: Any, workers: int, host_limit: int) -> None:
    """Run remote image I/O outside the Web process and reserve foreground capacity."""
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    from . import service
    from ..network import transport

    transport.configure_host_limit(host_limit)
    service.IMAGE_LIMITER = service.RateLimiter(0.0)

    def fetch(task: tuple[int, dict[str, Any], bool]) -> tuple[int, str, float]:
        anime_id, network, refresh = task
        started = time.monotonic()
        try:
            image = service.get_anime_image(Path(db_path), anime_id, refresh=refresh, network=network,
                                            log_timing=False)
            if image and image[1] != "image/svg+xml":
                status = "available"
            else:
                with sqlite3.connect(db_path, timeout=5) as db:
                    row = db.execute("SELECT error FROM anime_image WHERE anime_id=?", (anime_id,)).fetchone()
                status = "unavailable" if row and row[0] == "no_cover" else "error:remote"
            return anime_id, status, time.monotonic() - started
        except BaseException as exc:  # keep a bad task from terminating the worker process
            return anime_id, f"error:{type(exc).__name__}", time.monotonic() - started

    futures: dict[concurrent.futures.Future[tuple[int, str, float]], int] = {}
    backlog: list[tuple[int, int, tuple[int, dict[str, Any], bool]]] = []
    queued_priority: dict[int, int] = {}
    active_ids: set[int] = set()
    sequence = 0
    stopping = False
    minimum = min(workers, max(2, int(os.getenv("ANM_IMAGE_MIN_CONCURRENCY", "6"))))
    target = min(workers, max(minimum, int(os.getenv("ANM_IMAGE_INITIAL_CONCURRENCY", "12"))))
    reserve = min(max(1, int(os.getenv("ANM_IMAGE_FOREGROUND_RESERVE", "2"))), max(1, workers - 1))
    fast_successes = 0

    def accept(message: Any) -> None:
        nonlocal sequence, stopping
        if message is None:
            stopping = True
            return
        priority, anime_id, network, refresh = message
        priority, anime_id = int(priority), int(anime_id)
        if anime_id in active_ids:
            return
        current = queued_priority.get(anime_id)
        if current is not None and current <= priority:
            return
        queued_priority[anime_id] = priority
        sequence += 1
        heapq.heappush(backlog, (priority, sequence, (anime_id, dict(network or {}), bool(refresh))))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="anm-cover") as pool:
        while not stopping or futures or backlog:
            try:
                accept(requests.get(timeout=.05 if (futures or backlog) else .15))
            except queue.Empty:
                pass
            for _ in range(255):
                try:
                    accept(requests.get_nowait())
                except queue.Empty:
                    break

            while backlog and len(futures) < target:
                priority, _order, task = backlog[0]
                anime_id = int(task[0])
                if queued_priority.get(anime_id) != priority:
                    heapq.heappop(backlog)
                    continue
                background_cap = max(1, target - reserve)
                if priority > 0 and len(futures) >= background_cap:
                    break
                heapq.heappop(backlog)
                queued_priority.pop(anime_id, None)
                active_ids.add(anime_id)
                future = pool.submit(fetch, task)
                futures[future] = anime_id

            for future in [item for item in futures if item.done()]:
                anime_id = futures.pop(future)
                active_ids.discard(anime_id)
                try:
                    completed_id, status, duration = future.result()
                    results.put((completed_id, status))
                    if status == "available" and duration <= 6.0:
                        fast_successes += 1
                        if fast_successes >= 4 and target < workers:
                            target += 1
                            fast_successes = 0
                    elif status.startswith("error:") or duration >= 15.0:
                        target = max(minimum, target - 2)
                        fast_successes = 0
                except BaseException as exc:
                    results.put((anime_id, f"error:{type(exc).__name__}"))
                    target = max(minimum, target - 2)
                    fast_successes = 0


def _clear_transient_negative_cache(db_path: Path) -> None:
    """Allow transient cover failures to retry immediately after a service restart."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_image'").fetchone():
            db.execute("UPDATE anime_image SET fetched_at=NULL,error=NULL WHERE image_blob IS NULL AND error IS NOT NULL AND error<>'no_cover'")


class ImageFetcher:
    """Non-blocking producer facade for the dedicated image process."""
    def __init__(self, db_path: Path, *, workers: int | None = None,
                 host_limit: int | None = None) -> None:
        self.db_path = db_path.resolve(strict=False)
        self.workers = max(1, min(32, int(workers or os.getenv("ANM_IMAGE_FETCH_WORKERS", "16"))))
        self.host_limit = max(1, min(12, int(host_limit or os.getenv("ANM_IMAGE_HOST_CONCURRENCY", "6"))))
        self.context = multiprocessing.get_context("spawn")
        self.requests = self.context.Queue()
        self.results = self.context.Queue()
        self.lock = threading.RLock()
        self.pending_ids: set[int] = set()
        self.deferred_refresh: dict[int, dict[str, Any]] = {}
        self.last_results: dict[int, str] = {}
        self.process: multiprocessing.Process | None = None
        self.closed = threading.Event()
        self.listener = threading.Thread(target=self._listen, daemon=True, name="anm-image-results")
        self.listener.start()

    def start(self) -> None:
        with self.lock:
            if self.closed.is_set():
                return
            if self.process is not None and self.process.is_alive():
                return
            self.pending_ids.clear()
            self.deferred_refresh.clear()
            _clear_transient_negative_cache(self.db_path)
            self.process = self.context.Process(
                target=_worker_main,
                args=(str(self.db_path), self.requests, self.results, self.workers, self.host_limit),
                daemon=True,
                name="AnimeMachine-ImageFetcher",
            )
            self.process.start()

    def enqueue(self, anime_id: int, network: dict[str, Any], *, refresh: bool = False,
                priority: str = "foreground") -> bool:
        anime_id = int(anime_id)
        self.start()
        with self.lock:
            if self.closed.is_set():
                return False
            already_pending = anime_id in self.pending_ids
            if already_pending:
                if refresh:
                    self.deferred_refresh[anime_id] = dict(network or {})
                return True
            self.pending_ids.add(anime_id)
            self.last_results.pop(anime_id, None)
        rank = 0 if priority == "foreground" else 1
        try:
            self.requests.put_nowait((rank, anime_id, dict(network or {}), bool(refresh)))
            return True
        except (OSError, ValueError, queue.Full):
            if not already_pending:
                with self.lock:
                    self.pending_ids.discard(anime_id)
            return already_pending

    def pending(self, anime_id: int) -> bool:
        with self.lock:
            return int(anime_id) in self.pending_ids

    def result(self, anime_id: int) -> str | None:
        with self.lock:
            return self.last_results.get(int(anime_id))

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {"pending": len(self.pending_ids), "completed": len(self.last_results),
                    "workers": self.workers, "hostLimit": self.host_limit}

    def _listen(self) -> None:
        while not self.closed.is_set():
            try:
                anime_id, status = self.results.get(timeout=.25)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                if self.closed.is_set():
                    return
                continue
            with self.lock:
                anime_id = int(anime_id)
                deferred = self.deferred_refresh.pop(anime_id, None)
                if deferred is not None and not self.closed.is_set():
                    try:
                        self.requests.put_nowait((0, anime_id, deferred, True))
                        self.last_results.pop(anime_id, None)
                        continue
                    except (OSError, ValueError, queue.Full):
                        pass
                self.pending_ids.discard(anime_id)
                self.last_results[anime_id] = str(status)

    def close(self) -> None:
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            process = self.process
            self.process = None
        if process is not None and process.is_alive():
            with contextlib.suppress(OSError, ValueError, queue.Full):
                self.requests.put_nowait(None)
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
        if self.listener is not threading.current_thread():
            self.listener.join(2)
        for channel in (self.requests, self.results):
            with contextlib.suppress(Exception):
                channel.close()
            with contextlib.suppress(Exception):
                channel.join_thread()
        with self.lock:
            self.pending_ids.clear()
            self.deferred_refresh.clear()
