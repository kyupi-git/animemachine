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


IMAGE_PRIORITY_FOREGROUND = 0
IMAGE_PRIORITY_PREFETCH = 20
IMAGE_PRIORITY_RETRY = 900
IMAGE_PRIORITY_MAINTENANCE = 1000


def _worker_main(db_path: str, requests: Any, results: Any, workers: int, host_limit: int) -> None:
    """Run remote image I/O outside the Web process and reserve foreground capacity."""
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    from . import service
    from ..network import connectivity, transport

    transport.configure_host_limit(host_limit)
    service.IMAGE_LIMITER = service.RateLimiter(0.0)

    def fetch(task: tuple[int, int, dict[str, Any], bool, int]) -> tuple[int, str, float]:
        priority, anime_id, network, refresh, bandwidth = task
        started = time.monotonic()
        try:
            with transport.bandwidth_limit(bandwidth if priority > 0 else 0):
                image = service.get_anime_image(Path(db_path), anime_id, refresh=refresh, network=network,
                                                log_timing=False)
            if image and image[1] != "image/svg+xml":
                status = "available"
            else:
                with contextlib.closing(sqlite3.connect(db_path, timeout=5)) as db:
                    row = db.execute("SELECT error FROM anime_image WHERE anime_id=?", (anime_id,)).fetchone()
                status = "no_image" if row and row[0] == "no_cover" else "error:remote"
            return anime_id, status, time.monotonic() - started
        except BaseException as exc:  # keep a bad task from terminating the worker process
            return anime_id, f"error:{type(exc).__name__}", time.monotonic() - started

    futures: dict[concurrent.futures.Future[tuple[int, str, float]], tuple[int, int]] = {}
    backlog: list[tuple[int, int, tuple[int, int, dict[str, Any], bool]]] = []
    queued_priority: dict[int, int] = {}
    active_ids: set[int] = set()
    sequence = 0
    stopping = False
    minimum = min(workers, max(2, int(os.getenv("ANM_IMAGE_MIN_CONCURRENCY", "6"))))
    target = min(workers, max(minimum, int(os.getenv("ANM_IMAGE_INITIAL_CONCURRENCY", "12"))))
    reserve = 0 if workers <= 1 else min(max(1, int(os.getenv("ANM_IMAGE_FOREGROUND_RESERVE", "2"))), workers - 1)
    background_limit = workers
    background_bandwidth = 0
    background_paused = False
    external_reserve = 0
    foreground_pressure = False
    network_offline = False
    network_learning_suppressed = False
    fast_successes = 0
    foreground_latency_ewma: float | None = None
    background_duration_ewma: float | None = None
    background_failure_ewma = 0.0
    last_budget: tuple[int, str, float | None, int, int] | None = None
    cpu_wall_mark = time.monotonic()
    cpu_time_mark = time.process_time()
    process_cpu_load = 0.0

    def accept(message: Any) -> None:
        nonlocal sequence, stopping, background_limit, background_bandwidth, background_paused, external_reserve, foreground_pressure, network_offline, network_learning_suppressed
        if message is None:
            stopping = True
            return
        if isinstance(message, dict) and message.get("control") == "limits":
            background_limit = max(1, min(workers, int(message.get("concurrency") or workers)))
            background_bandwidth = max(0, int(message.get("bandwidthBytesPerSecond") or 0))
            background_paused = bool(message.get("paused"))
            external_reserve = max(0, min(workers, int(message.get("externalReserve") or 0)))
            foreground_pressure = bool(message.get("foregroundPressure"))
            network_offline = bool(message.get("networkOffline"))
            network_learning_suppressed = bool(message.get("networkLearningSuppressed"))
            connectivity.set_forced_offline(network_offline)
            connectivity.set_forced_failure_suppression(network_learning_suppressed)
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
        heapq.heappush(backlog, (priority, sequence, (priority, anime_id, dict(network or {}), bool(refresh))))

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

            active_foreground = sum(1 for _anime_id, active_priority in futures.values() if active_priority == 0)
            adaptive_cap = max(0, target - reserve)
            resource_ratio = 1.0
            budget_reasons: list[str] = []
            cpu_load: float | None = None
            cpu_scope = "system"
            now_cpu_wall = time.monotonic()
            if now_cpu_wall - cpu_wall_mark >= 1.0:
                elapsed_cpu_wall = max(.001, now_cpu_wall - cpu_wall_mark)
                current_cpu_time = time.process_time()
                process_cpu_load = max(0.0, (current_cpu_time - cpu_time_mark) / elapsed_cpu_wall / max(1, int(os.cpu_count() or 1)))
                cpu_wall_mark = now_cpu_wall
                cpu_time_mark = current_cpu_time
            if foreground_pressure or active_foreground:
                resource_ratio = min(resource_ratio, 0.35)
                budget_reasons.append("foreground")
            if foreground_latency_ewma is not None and foreground_latency_ewma >= 1.5:
                resource_ratio = min(resource_ratio, 0.35)
                budget_reasons.append("foreground_latency")
            elif foreground_latency_ewma is not None and foreground_latency_ewma >= 0.8:
                resource_ratio = min(resource_ratio, 0.65)
                budget_reasons.append("foreground_latency")
            if hasattr(os, "getloadavg"):
                with contextlib.suppress(OSError):
                    cpu_load = float(os.getloadavg()[0]) / max(1, int(os.cpu_count() or 1))
            if cpu_load is None:
                cpu_load = process_cpu_load
                cpu_scope = "process"
            if cpu_load is not None and cpu_load >= 0.9:
                resource_ratio = min(resource_ratio, 0.35)
                budget_reasons.append("cpu")
            elif cpu_load is not None and cpu_load >= 0.7:
                resource_ratio = min(resource_ratio, 0.65)
                budget_reasons.append("cpu")
            if background_duration_ewma is not None and background_duration_ewma >= 10.0:
                resource_ratio = min(resource_ratio, 0.50)
                budget_reasons.append("io")
            elif background_duration_ewma is not None and background_duration_ewma >= 5.0:
                resource_ratio = min(resource_ratio, 0.75)
                budget_reasons.append("io")
            if background_failure_ewma >= 0.25:
                resource_ratio = min(resource_ratio, 0.50)
                budget_reasons.append("network")
            manual_cap = min(background_limit, adaptive_cap)
            adaptive_budget = max(1, min(manual_cap, int(round(manual_cap * resource_ratio)))) if manual_cap else 0
            if network_offline:
                adaptive_budget = 0
                budget_reasons.append("offline")
            background_cap = 0 if background_paused or network_offline else max(0, adaptive_budget - external_reserve)
            budget = (background_cap, "+".join(dict.fromkeys(budget_reasons)) or "idle", round(cpu_load, 3) if cpu_load is not None else None, adaptive_budget, external_reserve)
            if budget != last_budget:
                results.put({"control": "budget", "effectiveConcurrency": background_cap, "adaptiveCapacity": adaptive_budget,
                             "externalReserve": external_reserve, "reason": budget[1], "cpuLoad": budget[2],
                             "cpuScope": cpu_scope, "foregroundLatencySeconds": foreground_latency_ewma,
                             "ioDurationSeconds": background_duration_ewma, "failureRate": background_failure_ewma})
                last_budget = budget

            while backlog and len(futures) < target:
                if network_offline:
                    break
                priority, _order, base_task = backlog[0]
                anime_id = int(base_task[1])
                if queued_priority.get(anime_id) != priority:
                    heapq.heappop(backlog)
                    continue
                if priority > 0:
                    active_background = sum(1 for _anime_id, active_priority in futures.values() if active_priority > 0)
                    if active_background >= background_cap:
                        break
                    if priority >= IMAGE_PRIORITY_MAINTENANCE:
                        active_maintenance = sum(
                            1 for _anime_id, active_priority in futures.values()
                            if active_priority >= IMAGE_PRIORITY_MAINTENANCE
                        )
                        if active_foreground or foreground_pressure or active_maintenance:
                            break
                heapq.heappop(backlog)
                queued_priority.pop(anime_id, None)
                active_ids.add(anime_id)
                task = (*base_task, background_bandwidth)
                future = pool.submit(fetch, task)
                futures[future] = (anime_id, priority)

            for future in [item for item in futures if item.done()]:
                anime_id, priority = futures.pop(future)
                active_ids.discard(anime_id)
                try:
                    completed_id, status, duration = future.result()
                    results.put((completed_id, status))
                    if priority == 0:
                        foreground_latency_ewma = duration if foreground_latency_ewma is None else .25 * duration + .75 * foreground_latency_ewma
                    else:
                        background_duration_ewma = duration if background_duration_ewma is None else .20 * duration + .80 * background_duration_ewma
                        failed_sample = 1.0 if status.startswith("error:") else 0.0
                        background_failure_ewma = .20 * failed_sample + .80 * background_failure_ewma
                    if priority > 0 and status == "available" and duration <= 6.0:
                        fast_successes += 1
                        if fast_successes >= 4 and target < workers:
                            target += 1
                            fast_successes = 0
                    elif priority > 0 and (status.startswith("error:") or duration >= 15.0):
                        target = max(minimum, target - 2)
                        fast_successes = 0
                except BaseException as exc:
                    results.put((anime_id, f"error:{type(exc).__name__}"))
                    if priority > 0:
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
        self.pending_priority: dict[int, int] = {}
        self.deferred_refresh: dict[int, dict[str, Any]] = {}
        self.last_results: dict[int, str] = {}
        self.process: multiprocessing.Process | None = None
        self.closed = threading.Event()
        self.background_paused = False
        self.background_concurrency = self.workers
        self.background_bandwidth_kib = 0
        self.foreground_pressure = False
        self.network_offline = False
        self.network_learning_suppressed = False
        self.external_reserve = 0
        self.budget_status: dict[str, Any] = {"effectiveConcurrency": self.background_concurrency,
                                              "adaptiveCapacity": self.background_concurrency, "externalReserve": 0,
                                              "reason": "idle"}
        self.listener = threading.Thread(target=self._listen, daemon=True, name="anm-image-results")
        self.listener.start()

    def _control_message(self) -> dict[str, Any]:
        return {
            "control": "limits",
            "paused": self.background_paused,
            "concurrency": self.background_concurrency,
            "bandwidthBytesPerSecond": self.background_bandwidth_kib * 1024,
            "foregroundPressure": self.foreground_pressure,
            "externalReserve": self.external_reserve,
            "networkOffline": self.network_offline,
            "networkLearningSuppressed": self.network_learning_suppressed,
        }

    def start(self) -> None:
        with self.lock:
            if self.closed.is_set():
                return
            if self.process is not None and self.process.is_alive():
                return
            self.pending_ids.clear()
            self.pending_priority.clear()
            self.deferred_refresh.clear()
            _clear_transient_negative_cache(self.db_path)
            self.process = self.context.Process(
                target=_worker_main,
                args=(str(self.db_path), self.requests, self.results, self.workers, self.host_limit),
                daemon=True,
                name="AnimeMachine-ImageFetcher",
            )
            self.process.start()
            control = self._control_message()
        with contextlib.suppress(OSError, ValueError, queue.Full):
            self.requests.put_nowait(control)

    def set_background_limits(self, *, paused: bool | None = None, concurrency: int | None = None,
                              bandwidth_kib: int | None = None) -> dict[str, Any]:
        self.start()
        with self.lock:
            if paused is not None:
                self.background_paused = bool(paused)
            if concurrency is not None:
                self.background_concurrency = max(1, min(self.workers, int(concurrency)))
            if bandwidth_kib is not None:
                self.background_bandwidth_kib = max(0, min(1024 * 1024, int(bandwidth_kib)))
            control = self._control_message()
        with contextlib.suppress(OSError, ValueError, queue.Full):
            self.requests.put_nowait(control)
        return self.snapshot()

    def set_foreground_pressure(self, active: bool) -> None:
        self.start()
        with self.lock:
            active = bool(active)
            if self.foreground_pressure == active:
                return
            self.foreground_pressure = active
            control = self._control_message()
        with contextlib.suppress(OSError, ValueError, queue.Full):
            self.requests.put_nowait(control)

    def set_network_state(self, *, offline: bool, suppress_learning: bool) -> None:
        """Mirror parent connectivity state without treating a suspected outage as confirmed local mode."""
        self.start()
        with self.lock:
            offline = bool(offline)
            suppress_learning = bool(suppress_learning)
            if self.network_offline == offline and self.network_learning_suppressed == suppress_learning:
                return
            self.network_offline = offline
            self.network_learning_suppressed = suppress_learning
            control = self._control_message()
        with contextlib.suppress(OSError, ValueError, queue.Full):
            self.requests.put_nowait(control)

    def set_network_offline(self, active: bool) -> None:
        self.set_network_state(offline=active, suppress_learning=active)

    def set_background_reserve(self, slots: int) -> None:
        """Reserve adaptive background slots for other low-priority modules."""
        self.start()
        with self.lock:
            reserve = max(0, min(self.workers, int(slots)))
            if self.external_reserve == reserve:
                return
            self.external_reserve = reserve
            control = self._control_message()
        with contextlib.suppress(OSError, ValueError, queue.Full):
            self.requests.put_nowait(control)

    def enqueue(self, anime_id: int, network: dict[str, Any], *, refresh: bool = False,
                priority: str | int = "foreground") -> bool:
        anime_id = int(anime_id)
        if isinstance(priority, int):
            rank = max(IMAGE_PRIORITY_FOREGROUND, int(priority))
        else:
            rank = {
                "foreground": IMAGE_PRIORITY_FOREGROUND,
                "prefetch": IMAGE_PRIORITY_PREFETCH,
                "retry": IMAGE_PRIORITY_RETRY,
                "maintenance": IMAGE_PRIORITY_MAINTENANCE,
            }.get(priority, IMAGE_PRIORITY_PREFETCH)
        self.start()
        with self.lock:
            if bool(getattr(self, "network_offline", False)):
                return False
        prior_rank: int | None = None
        already_pending = False
        with self.lock:
            if self.closed.is_set():
                return False
            already_pending = anime_id in self.pending_ids
            if already_pending:
                prior_rank = self.pending_priority.get(anime_id, 1)
                if refresh and rank == IMAGE_PRIORITY_FOREGROUND:
                    self.deferred_refresh[anime_id] = dict(network or {})
                if rank >= prior_rank:
                    return True
                self.pending_priority[anime_id] = rank
            else:
                self.pending_ids.add(anime_id)
                self.pending_priority[anime_id] = rank
            self.last_results.pop(anime_id, None)
        try:
            self.requests.put_nowait((rank, anime_id, dict(network or {}), bool(refresh and not already_pending)))
            return True
        except (OSError, ValueError, queue.Full):
            with self.lock:
                if already_pending and prior_rank is not None:
                    self.pending_priority[anime_id] = prior_rank
                elif not already_pending:
                    self.pending_ids.discard(anime_id)
                    self.pending_priority.pop(anime_id, None)
            return already_pending

    def pending(self, anime_id: int) -> bool:
        with self.lock:
            return int(anime_id) in self.pending_ids

    def result(self, anime_id: int) -> str | None:
        with self.lock:
            return self.last_results.get(int(anime_id))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "pending": len(self.pending_ids), "completed": len(self.last_results),
                "workers": self.workers, "hostLimit": self.host_limit,
                "paused": self.background_paused, "backgroundConcurrency": self.background_concurrency,
                "bandwidthKiBps": self.background_bandwidth_kib,
                "foregroundPressure": self.foreground_pressure,
                "externalReserve": self.external_reserve,
                "networkOffline": self.network_offline,
                "networkLearningSuppressed": self.network_learning_suppressed,
                "budget": dict(self.budget_status),
            }

    def _listen(self) -> None:
        while not self.closed.is_set():
            try:
                message = self.results.get(timeout=.25)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                if self.closed.is_set():
                    return
                continue
            if isinstance(message, dict) and message.get("control") == "budget":
                with self.lock:
                    self.budget_status = {key: value for key, value in message.items() if key != "control"}
                continue
            anime_id, status = message
            with self.lock:
                anime_id = int(anime_id)
                deferred = self.deferred_refresh.pop(anime_id, None)
                if deferred is not None and not self.closed.is_set():
                    try:
                        self.pending_priority[anime_id] = 0
                        self.requests.put_nowait((0, anime_id, deferred, True))
                        self.last_results.pop(anime_id, None)
                        continue
                    except (OSError, ValueError, queue.Full):
                        pass
                self.pending_ids.discard(anime_id)
                self.pending_priority.pop(anime_id, None)
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
            self.pending_priority.clear()
            self.deferred_refresh.clear()
