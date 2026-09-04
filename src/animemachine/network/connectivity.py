"""Conservative whole-network offline state shared by network consumers."""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Iterator


OFFLINE_AFTER_SECONDS = 30 * 60
MAX_FAILED_PROBE_GAP_SECONDS = 3 * 60
OPPORTUNISTIC_RECOVERY_INTERVAL_SECONDS = 2 * 60
_LOCK = threading.RLock()
_LOCAL = threading.local()
_FAILED_SINCE: float | None = None
_OFFLINE = False
_FORCED_OFFLINE = False
_FORCED_FAILURE_SUPPRESSION = False
_LAST_PROBE_AT = 0.0
_LAST_FAILED_PROBE_AT = 0.0
_LAST_ONLINE_AT = 0.0
_NEXT_OPPORTUNISTIC_RECOVERY_AT = 0.0
_ENVIRONMENT_ID = ""


class OfflineModeError(ConnectionError):
    """Raised when a remote request is intentionally suppressed in local mode."""


def reset() -> None:
    global _FAILED_SINCE, _OFFLINE, _FORCED_OFFLINE, _FORCED_FAILURE_SUPPRESSION, _LAST_PROBE_AT, _LAST_FAILED_PROBE_AT, _LAST_ONLINE_AT, _NEXT_OPPORTUNISTIC_RECOVERY_AT, _ENVIRONMENT_ID
    with _LOCK:
        _FAILED_SINCE = None
        _OFFLINE = False
        _FORCED_OFFLINE = False
        _FORCED_FAILURE_SUPPRESSION = False
        _LAST_PROBE_AT = 0.0
        _LAST_FAILED_PROBE_AT = 0.0
        _LAST_ONLINE_AT = 0.0
        _NEXT_OPPORTUNISTIC_RECOVERY_AT = 0.0
        _ENVIRONMENT_ID = ""


def note_environment(identity: str, *, now: float | None = None) -> bool:
    """Reset inherited outage timing when the local network or proxy environment changes."""
    global _ENVIRONMENT_ID, _FAILED_SINCE, _OFFLINE, _LAST_FAILED_PROBE_AT, _NEXT_OPPORTUNISTIC_RECOVERY_AT
    value = str(identity or "").strip()
    if not value:
        return False
    stamp = time.monotonic() if now is None else float(now)
    with _LOCK:
        if not _ENVIRONMENT_ID:
            _ENVIRONMENT_ID = value
            return False
        if value == _ENVIRONMENT_ID:
            return False
        was_suspected = bool(_FAILED_SINCE is not None or _OFFLINE)
        _ENVIRONMENT_ID = value
        _FAILED_SINCE = stamp if was_suspected else None
        _OFFLINE = False
        _LAST_FAILED_PROBE_AT = stamp if was_suspected else 0.0
        _NEXT_OPPORTUNISTIC_RECOVERY_AT = 0.0
        return True


def note_probe(online: bool, *, now: float | None = None, started_at: float | None = None) -> dict[str, Any]:
    """Record one independent multi-endpoint probe; only 30 min continuous failure enters offline mode."""
    global _FAILED_SINCE, _OFFLINE, _LAST_PROBE_AT, _LAST_FAILED_PROBE_AT, _LAST_ONLINE_AT, _NEXT_OPPORTUNISTIC_RECOVERY_AT
    stamp = time.monotonic() if now is None else float(now)
    with _LOCK:
        previous = _OFFLINE
        if not online and started_at is not None and _LAST_ONLINE_AT >= float(started_at):
            online = True
        _LAST_PROBE_AT = stamp
        if online:
            _FAILED_SINCE = None
            _LAST_FAILED_PROBE_AT = 0.0
            _OFFLINE = False
            _LAST_ONLINE_AT = stamp
        else:
            # A long gap means continuous offline status was not actually observed.
            if (_FAILED_SINCE is None or not _LAST_FAILED_PROBE_AT
                    or stamp - _LAST_FAILED_PROBE_AT > MAX_FAILED_PROBE_GAP_SECONDS):
                _FAILED_SINCE = stamp
            _LAST_FAILED_PROBE_AT = stamp
            if stamp - _FAILED_SINCE >= OFFLINE_AFTER_SECONDS:
                _OFFLINE = True
                if not previous:
                    _NEXT_OPPORTUNISTIC_RECOVERY_AT = stamp
        return {
            **snapshot(now=stamp),
            "enteredOffline": bool(_OFFLINE and not previous),
            "recovered": bool(previous and not _OFFLINE),
        }


def note_online_activity(*, now: float | None = None) -> None:
    """Clear an offline candidate when an ordinary remote request proves connectivity."""
    global _FAILED_SINCE, _OFFLINE, _LAST_FAILED_PROBE_AT, _LAST_ONLINE_AT, _NEXT_OPPORTUNISTIC_RECOVERY_AT
    stamp = time.monotonic() if now is None else float(now)
    with _LOCK:
        _FAILED_SINCE = None
        _LAST_FAILED_PROBE_AT = 0.0
        _OFFLINE = False
        _LAST_ONLINE_AT = stamp
        _NEXT_OPPORTUNISTIC_RECOVERY_AT = 0.0


def set_forced_offline(active: bool) -> None:
    """Mirror the parent process offline state into isolated workers."""
    global _FORCED_OFFLINE
    with _LOCK:
        _FORCED_OFFLINE = bool(active)


def set_forced_failure_suppression(active: bool) -> None:
    """Mirror a suspected whole-network outage into workers without entering local mode."""
    global _FORCED_FAILURE_SUPPRESSION
    with _LOCK:
        _FORCED_FAILURE_SUPPRESSION = bool(active)


def outage_suspected() -> bool:
    """Return whether network-failure learning should be suspended pending a connectivity verdict."""
    with _LOCK:
        return bool(_FAILED_SINCE is not None or _OFFLINE or _FORCED_OFFLINE or _FORCED_FAILURE_SUPPRESSION)


def failure_learning_allowed() -> bool:
    return not outage_suspected()


def is_offline() -> bool:
    with _LOCK:
        return bool(_OFFLINE or _FORCED_OFFLINE)


def recovery_allowed() -> bool:
    return bool(getattr(_LOCAL, "recovery_probe", False))


@contextlib.contextmanager
def recovery_probe() -> Iterator[None]:
    previous = recovery_allowed()
    _LOCAL.recovery_probe = True
    try:
        yield
    finally:
        _LOCAL.recovery_probe = previous


@contextlib.contextmanager
def opportunistic_recovery(*, now: float | None = None) -> Iterator[bool]:
    """Permit one bounded ordinary remote attempt so confirmed local mode cannot become permanent."""
    global _NEXT_OPPORTUNISTIC_RECOVERY_AT
    stamp = time.monotonic() if now is None else float(now)
    with _LOCK:
        allowed = bool(
            _OFFLINE
            and not _FORCED_OFFLINE
            and stamp >= _NEXT_OPPORTUNISTIC_RECOVERY_AT
        )
        if allowed:
            _NEXT_OPPORTUNISTIC_RECOVERY_AT = stamp + OPPORTUNISTIC_RECOVERY_INTERVAL_SECONDS
    if not allowed:
        yield False
        return
    previous = recovery_allowed()
    _LOCAL.recovery_probe = True
    try:
        yield True
    finally:
        _LOCAL.recovery_probe = previous


def snapshot(*, now: float | None = None) -> dict[str, Any]:
    stamp = time.monotonic() if now is None else float(now)
    with _LOCK:
        failed_for = 0.0 if _FAILED_SINCE is None else max(0.0, stamp - _FAILED_SINCE)
        return {
            "offline": bool(_OFFLINE or _FORCED_OFFLINE),
            "confirmedOffline": bool(_OFFLINE),
            "forcedOffline": bool(_FORCED_OFFLINE),
            "outageSuspected": bool(_FAILED_SINCE is not None or _OFFLINE or _FORCED_OFFLINE or _FORCED_FAILURE_SUPPRESSION),
            "failureLearningSuppressed": not failure_learning_allowed(),
            "failedForSeconds": round(failed_for, 1),
            "offlineAfterSeconds": OFFLINE_AFTER_SECONDS,
            "maxFailedProbeGapSeconds": MAX_FAILED_PROBE_GAP_SECONDS,
            "opportunisticRecoveryIntervalSeconds": OPPORTUNISTIC_RECOVERY_INTERVAL_SECONDS,
            "lastProbeAtMonotonic": _LAST_PROBE_AT,
            "lastFailedProbeAtMonotonic": _LAST_FAILED_PROBE_AT,
            "lastOnlineAtMonotonic": _LAST_ONLINE_AT,
        }
