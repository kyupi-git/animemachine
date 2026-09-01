"""Container-side storage preflight used by the CLI and Compose."""
from __future__ import annotations

import concurrent.futures
import os
import sys
from dataclasses import replace

from .status import AVAILABLE, NOT_CONFIGURED, profiles_from_env, redact, status_for_path, status_for_profile


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _preflight_profile(profile):
    if profile.key == "ani_rss_media" and _truthy(os.getenv("ANM_MANAGED_ANI_RSS")):
        return replace(profile, access="rw")
    return profile


def run(*, require_all: bool = False, timeout: float = 4.0) -> int:
    failed = False
    for base_profile in profiles_from_env().values():
        profile = _preflight_profile(base_profile)
        status = status_for_profile(profile, timeout=timeout, use_cache=False)
        if status.state == NOT_CONFIGURED:
            print(f"[storage] {profile.name}: not configured", flush=True)
            continue
        description = f"{profile.storage_type}, {profile.access}, {profile.path}"
        if status.state == AVAILABLE:
            print(f"[storage] {profile.name}: available ({description})", flush=True)
            continue
        print(f"[storage] {profile.name}: {status.state} ({redact(status.detail or description)})", file=sys.stderr, flush=True)
        if require_all or profile.critical:
            failed = True
    return 1 if failed else 0


def check_config(config: dict, *, timeout: float = 4.0) -> dict[str, dict[str, object]]:
    """Check user-facing storage paths concurrently without exposing low-level errors."""
    deployment = config.get("deployment", {}) if isinstance(config, dict) else {}
    ani = config.get("components", {}).get("aniRss", {}) if isinstance(config, dict) else {}
    external = config.get("externalLibraries", []) if isinstance(config, dict) else []
    checks: list[tuple[str, str, str, bool]] = [
        ("library", "Library", str(deployment.get("libraryUncRoot") or ""), True),
        ("torrentPool", "Torrent Pool", str(deployment.get("torrentPoolRoot") or ""), False),
        ("aniRssMedia", "Ani-RSS Media", str(ani.get("mediaPath") or ""), False),
    ]
    for index, item in enumerate(external if isinstance(external, list) else []):
        if isinstance(item, dict) and item.get("enabled") and item.get("path"):
            checks.append((f"external{index + 1}", "External Library", str(item["path"]), False))

    def one(entry: tuple[str, str, str, bool]) -> tuple[str, dict[str, object]]:
        key, name, path, writable = entry
        if not path:
            return key, {"state": NOT_CONFIGURED, "name": name, "path": "", "access": "rw" if writable else "ro",
                         "message": f"{name} is not configured"}
        status = status_for_path(path, require_write=writable, timeout=timeout)
        if status.state == AVAILABLE:
            message = f"{name} is {'read/write' if writable else 'readable'}"
        elif status.state == "permission_denied":
            message = f"{name} permission is insufficient"
        elif status.state in {"host_unreachable", "mount_failed"}:
            message = f"{name} is temporarily unreachable"
        elif status.state == "authentication_failed":
            message = f"{name} credentials are invalid"
        else:
            message = f"{name} is unavailable"
        return key, {"state": status.state, "name": name, "path": path,
                     "access": "rw" if writable else "ro", "message": message}

    if not checks:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(checks)), thread_name_prefix="anm-storage-preflight") as pool:
        return dict(pool.map(one, checks))


def print_config_summary(config: dict, *, timeout: float = 4.0) -> dict[str, dict[str, object]]:
    result = check_config(config, timeout=timeout)
    for item in result.values():
        print(f"[storage] {item['message']}: {item['path']}", flush=True)
    return result
