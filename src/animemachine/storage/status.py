"""Storage availability checks for local paths, host mounts and managed SMB binds."""
from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

NOT_CONFIGURED = "not_configured"
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
AUTHENTICATION_FAILED = "authentication_failed"
PERMISSION_DENIED = "permission_denied"
HOST_UNREACHABLE = "host_unreachable"
SHARE_NOT_FOUND = "share_not_found"
MOUNT_FAILED = "mount_failed"
STORAGE_STATES = {
    NOT_CONFIGURED,
    AVAILABLE,
    UNAVAILABLE,
    AUTHENTICATION_FAILED,
    PERMISSION_DENIED,
    HOST_UNREACHABLE,
    SHARE_NOT_FOUND,
    MOUNT_FAILED,
}
NETWORK_FILESYSTEMS = {"cifs", "smb3"}


class StorageUnavailableError(OSError):
    """Raised when an inventory walk cannot safely be considered complete."""


@dataclass(frozen=True)
class StorageProfile:
    key: str
    name: str
    path: Path
    access: str
    critical: bool
    storage_type: str


@dataclass(frozen=True)
class StorageStatus:
    state: str
    profile: StorageProfile
    filesystem: str | None = None
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.state == AVAILABLE

    def public(self) -> dict[str, object]:
        return {
            "state": self.state,
            "type": self.profile.storage_type,
            "access": self.profile.access,
            "critical": self.profile.critical,
        }


_PROFILE_ENV = {
    "torrent_pool": ("Torrent Pool", "ANM_TORRENT_POOL_DIR", "/Torrents", "ro", "ANM_TORRENT_POOL_STORAGE_TYPE"),
    "library": ("Library", "ANM_LIBRARY_DIR", "/Library", "rw", "ANM_LIBRARY_STORAGE_TYPE"),
    "external_library": ("External Library", "ANM_EXTERNAL_LIBRARY_DIR", "/External", "ro", "ANM_EXTERNAL_LIBRARY_STORAGE_TYPE"),
    "ani_rss_media": ("Ani-RSS Media", "ANM_ANI_RSS_MEDIA_DIR", "/Media", "ro", "ANM_ANI_RSS_MEDIA_STORAGE_TYPE"),
    "incomplete": ("Incomplete", "ANM_INCOMPLETE_DIR", "/downloads/incomplete", "rw", "ANM_INCOMPLETE_STORAGE_TYPE"),
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, str, bool], tuple[float, StorageStatus]] = {}
_SENSITIVE = re.compile(r"(?i)(\b(?:password|pass)\s*=\s*)([^,\s]+)")
_UNC_USERINFO = re.compile(r"(?i)(?<=//)[^/@%\s]+%[^/@\s]+@")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def profiles_from_env() -> dict[str, StorageProfile]:
    managed_qbt = _truthy(os.getenv("ANM_MANAGED_QBT"))
    managed_ani = _truthy(os.getenv("ANM_MANAGED_ANI_RSS"))
    result: dict[str, StorageProfile] = {}
    for key, (name, path_env, default_path, access, type_env) in _PROFILE_ENV.items():
        value = os.getenv(path_env, default_path).strip()
        storage_type = os.getenv(type_env, "local-path").strip().casefold() or "local-path"
        critical = ((key in {"library", "incomplete"} and managed_qbt)
                    or (key == "ani_rss_media" and managed_ani))
        result[key] = StorageProfile(
            key=key,
            name=name,
            path=Path(value or default_path),
            access=access,
            critical=critical,
            storage_type=storage_type,
        )
    return result


def redact(value: object) -> str:
    text = str(value)
    text = _SENSITIVE.sub(r"\1<redacted>", text)
    return _UNC_USERINFO.sub("<redacted>@", text)


def _mount_unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[tuple[Path, str]]:
    mounts: list[tuple[Path, str]] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mount_point = Path(_mount_unescape(left_fields[4]))
        mounts.append((mount_point, right_fields[0].casefold()))
    return mounts


def filesystem_for_path(path: Path, mountinfo: str | None = None) -> str | None:
    resolved = Path(os.path.abspath(str(path)))
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    candidates: list[tuple[int, str]] = []
    for mount_point, fs_type in parse_mountinfo(mountinfo):
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        candidates.append((len(mount_point.parts), fs_type))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _state_from_errno(number: int | None) -> str:
    if number in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return PERMISSION_DENIED
    if number in {errno.ENOENT, errno.ENOTDIR}:
        return SHARE_NOT_FOUND
    network_errors = {
        errno.EIO,
        getattr(errno, "ESTALE", -1),
        getattr(errno, "ENOTCONN", -1),
        getattr(errno, "EHOSTDOWN", -1),
        getattr(errno, "EHOSTUNREACH", -1),
        getattr(errno, "ENETDOWN", -1),
        getattr(errno, "ENETUNREACH", -1),
        getattr(errno, "ETIMEDOUT", -1),
    }
    if number in network_errors:
        return HOST_UNREACHABLE
    return UNAVAILABLE


_PROBE_CODE = r'''
import errno, json, os, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
write = sys.argv[2] == "1"
result = {"ok": False, "errno": None, "detail": ""}
probe = None
try:
    stat = path.stat()
    if not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
    with os.scandir(path) as entries:
        next(entries, None)
    if write:
        probe = path / (".animemachine-storage-probe-%d-%d" % (os.getpid(), time.time_ns()))
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
        probe.unlink()
        probe = None
    result["ok"] = True
except OSError as exc:
    result["errno"] = exc.errno
    result["detail"] = type(exc).__name__
finally:
    if probe is not None:
        try: probe.unlink()
        except OSError: pass
print(json.dumps(result, separators=(",", ":")))
'''


def probe_path(path: Path, *, require_write: bool = False, timeout: float = 4.0) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE, str(path), "1" if require_write else "0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.5, timeout),
        )
    except subprocess.TimeoutExpired:
        return HOST_UNREACHABLE, "probe timeout"
    if completed.returncode != 0:
        return UNAVAILABLE, "probe process failed"
    try:
        payload = json.loads(completed.stdout.strip())
    except (ValueError, TypeError):
        return UNAVAILABLE, "invalid probe result"
    if payload.get("ok") is True:
        return AVAILABLE, ""
    number = payload.get("errno")
    return _state_from_errno(int(number) if isinstance(number, int) else None), str(payload.get("detail") or "")


def status_for_profile(profile: StorageProfile, *, timeout: float = 4.0, use_cache: bool = True) -> StorageStatus:
    if profile.storage_type in {"", "not-configured", "not_configured"}:
        return StorageStatus(NOT_CONFIGURED, profile)
    cache_key = (profile.key, str(profile.path), profile.storage_type, profile.access == "rw")
    now = time.monotonic()
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and now - cached[0] < 5.0:
                return cached[1]
    state, detail = probe_path(profile.path, require_write=profile.access == "rw", timeout=timeout)
    network_path = profile.storage_type in {"network", "managed-smb", "smb", "unc"} or str(profile.path).startswith("\\\\")
    if state == HOST_UNREACHABLE and detail == "probe timeout" and network_path:
        # A cold SMB session can exceed the short UI preflight even though the
        # share is healthy.  Let that first connection attempt finish, then
        # retry once before skipping an entire synchronization cycle.
        state, detail = probe_path(
            profile.path,
            require_write=profile.access == "rw",
            timeout=max(8.0, timeout * 2.0),
        )
    fs_type = filesystem_for_path(profile.path) if state == AVAILABLE else None
    if state == AVAILABLE and profile.storage_type == "managed-smb" and fs_type not in NETWORK_FILESYSTEMS:
        state, detail = MOUNT_FAILED, f"expected CIFS/SMB filesystem, got {fs_type or 'unknown'}"
    result = StorageStatus(state, profile, fs_type, detail)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, result)
    return result


def status_for_key(key: str, *, timeout: float = 4.0, use_cache: bool = True) -> StorageStatus:
    profiles = profiles_from_env()
    if key not in profiles:
        raise KeyError(key)
    return status_for_profile(profiles[key], timeout=timeout, use_cache=use_cache)


def profile_for_path(path: str | Path, profiles: Iterable[StorageProfile] | None = None) -> StorageProfile | None:
    target = Path(os.path.abspath(str(path)))
    candidates: list[tuple[int, StorageProfile]] = []
    for profile in profiles or profiles_from_env().values():
        profile_path = Path(os.path.abspath(str(profile.path)))
        try:
            target.relative_to(profile_path)
        except ValueError:
            continue
        candidates.append((len(profile.path.parts), profile))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def status_for_path(path: str | Path, *, require_write: bool = False, timeout: float = 4.0) -> StorageStatus:
    profile = profile_for_path(path)
    if profile is None:
        temporary = StorageProfile("path", "Path", Path(path), "rw" if require_write else "ro", False, "host-path")
        return status_for_profile(temporary, timeout=timeout, use_cache=False)
    if require_write and profile.access != "rw":
        profile = StorageProfile(profile.key, profile.name, profile.path, "rw", profile.critical, profile.storage_type)
    return status_for_profile(profile, timeout=timeout)


def snapshot(*, timeout: float = 4.0, use_cache: bool = True) -> dict[str, dict[str, object]]:
    return {
        key: status_for_profile(profile, timeout=timeout, use_cache=use_cache).public()
        for key, profile in profiles_from_env().items()
    }


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
