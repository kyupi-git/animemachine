"""Daily local-library quiet hours, checked before filesystem access."""
from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config.loader import DEFAULT_CONFIG, PACKAGE_ROOT
from ..config.policy import ConfigStore


class StorageUnavailableError(OSError):
    """An inventory operation cannot safely be considered complete."""


class StorageSleepingError(StorageUnavailableError):
    """The configured local library is in its daily quiet period."""


@lru_cache(maxsize=4)
def _read_config(path: str, mtime: int, size: int) -> dict[str, Any]:
    return ConfigStore(Path(path), PACKAGE_ROOT / "resources/config.example.json").read_persistent()


def current_config() -> dict[str, Any]:
    path = Path(os.getenv("ANM_CONFIG_PATH", str(DEFAULT_CONFIG)))
    if not path.exists():
        return {}
    info = path.stat()
    from ..config.runtime import apply_runtime_overrides
    return apply_runtime_overrides(_read_config(str(path.absolute()), info.st_mtime_ns, info.st_size))


def sleeping(config: dict[str, Any] | None = None, *, now: dt.datetime | None = None) -> bool:
    config = current_config() if config is None else config
    schedule = config.get("library", {}).get("sleep", {})
    if not schedule.get("enabled", False):
        return False
    start, end = schedule.get("start", "00:00"), schedule.get("end", "08:00")
    moment = (now or dt.datetime.now().astimezone()).strftime("%H:%M")
    return start <= moment < end if start < end else moment >= start or moment < end


def clock_info() -> str:
    current = dt.datetime.now().astimezone()
    zone = os.getenv('TZ') or current.tzname() or ''
    return f"{current:%Y-%m-%d %H:%M} · {zone} (UTC{current:%z})"


def _within(path: str | Path, root: str | Path) -> bool:
    """Lexical containment must not resolve/stat the sleeping disk."""
    target = os.path.normcase(os.path.abspath(str(path)))
    boundary = os.path.normcase(os.path.abspath(str(root)))
    try:
        return os.path.commonpath((target, boundary)) == boundary
    except ValueError:
        return False


def sleeping_path(path: str | Path, config: dict[str, Any] | None = None, *, now: dt.datetime | None = None) -> bool:
    config = current_config() if config is None else config
    root = str(config.get("deployment", {}).get("libraryUncRoot") or "").strip()
    return bool(root and sleeping(config, now=now) and _within(path, root))


def require_awake(path: str | Path, config: dict[str, Any] | None = None) -> None:
    if sleeping_path(path, config) or (config is not None and sleeping_path(path)):
        raise StorageSleepingError("local_library_sleeping")
