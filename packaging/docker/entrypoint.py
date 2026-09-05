#!/usr/bin/env python3
"""Stable Docker supervisor for app-layer updates and controlled restarts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request

_RESTART_CODE = 75
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_ACTIVE_CHILD: subprocess.Popen | None = None
_STOPPING = False


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    return tuple(map(int, match.groups())) if match else None


def _install_root() -> Path:
    return Path(os.getenv("ANM_INSTALL_ROOT", "/opt/animemachine"))


def _update_root() -> Path:
    return Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "application-update"


def _base_version() -> str:
    try:
        return (_install_root() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _valid_release(version: str) -> Path | None:
    root = _update_root() / "releases" / version
    if not _version_tuple(version) or not (root / "animemachine" / "__main__.py").is_file():
        return None
    metadata = list(root.glob("animemachine-*.dist-info/METADATA"))
    return root if len(metadata) == 1 else None


def _write_current(version: str) -> None:
    update_root = _update_root()
    update_root.mkdir(parents=True, exist_ok=True)
    path = update_root / "current"
    temporary = update_root / f".current-{os.getpid()}.tmp"
    temporary.write_text(version + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _clear_current() -> None:
    (_update_root() / "current").unlink(missing_ok=True)


def _selected_release() -> tuple[Path | None, str]:
    current_file = _update_root() / "current"
    try:
        version = current_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None, _base_version()
    current_tuple, base_tuple = _version_tuple(version), _version_tuple(_base_version())
    if current_tuple is None or (base_tuple is not None and current_tuple <= base_tuple):
        _clear_current()
        (_update_root() / "pending.json").unlink(missing_ok=True)
        return None, _base_version()
    root = _valid_release(version)
    if root is None:
        _clear_current()
        (_update_root() / "pending.json").unlink(missing_ok=True)
        return None, _base_version()
    return root, version


def _pending() -> dict[str, str] | None:
    path = _update_root() / "pending.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    version = str(value.get("version") or "").strip()
    previous = str(value.get("previous") or "").strip()
    if _version_tuple(version) is None or (previous and _version_tuple(previous) is None):
        return None
    return {"version": version, "previous": previous}


def _rollback(pending: dict[str, str]) -> None:
    previous = pending.get("previous", "")
    base_tuple = _version_tuple(_base_version())
    previous_tuple = _version_tuple(previous)
    if previous_tuple and (base_tuple is None or previous_tuple > base_tuple) and _valid_release(previous):
        _write_current(previous)
    else:
        _clear_current()
    (_update_root() / "pending.json").unlink(missing_ok=True)



def _numeric_id(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a numeric user/group id (got {raw!r}).") from exc
    if not 0 <= value <= 2_147_483_647:
        raise SystemExit(f"{name} must be between 0 and 2147483647.")
    return value


def _option(arguments: list[str], name: str) -> str:
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(name + "="):
            return value.partition("=")[2]
    return ""


def _writable_paths(arguments: list[str]) -> list[Path]:
    command = arguments[0] if arguments else "run"
    paths: list[Path] = []
    if command in {"run", "init", "serve", "healthcheck", "validate-config", "sync"}:
        paths.extend([
            Path(os.getenv("ANM_CONFIG_PATH", "/Config/config.json")).parent,
            Path(os.getenv("ANM_STATE_DIR", "/Data/state")),
            Path(os.getenv("ANM_LIBRARY_DIR", "/Library")),
        ])
        incomplete = os.getenv("ANM_INCOMPLETE_DIR", "").strip()
        if incomplete:
            paths.append(Path(incomplete))
    elif command in {"qbt-bootstrap", "ani-rss-bootstrap"}:
        config_dir = _option(arguments, "--config-dir")
        if config_dir:
            paths.append(Path(config_dir))
        if command == "qbt-bootstrap":
            for name in ("ANM_QBT_LIBRARY_DIR", "ANM_ANI_RSS_MEDIA_DIR", "ANM_INCOMPLETE_DIR"):
                value = os.getenv(name, "").strip()
                if value:
                    paths.append(Path(value))
    elif command == "torrent-collector":
        paths.extend([
            Path(os.getenv("TORRENT_COLLECTOR_STATE_DIR", "/CollectorState")),
            Path(os.getenv("ANM_TORRENT_POOL_DIR", "/Torrents")),
        ])
    return list(dict.fromkeys(paths))


def _drop_privileges(arguments: list[str]) -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    uid = _numeric_id("PUID", 1000)
    gid = _numeric_id("PGID", 1000)
    for path in _writable_paths(arguments):
        try:
            path.mkdir(parents=True, exist_ok=True)
            stat = path.stat()
            if stat.st_uid != uid or stat.st_gid != gid:
                os.chown(path, uid, gid)
        except OSError as exc:
            print(f"Warning: unable to prepare writable path {path}: {exc}", file=sys.stderr, flush=True)
    if uid == 0 and gid == 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.environ.get("HOME", "").strip() in {"", "/root"}:
        os.environ["HOME"] = "/tmp"


def _host(arguments: list[str]) -> str:
    for index, value in enumerate(arguments):
        if value == "--host" and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith("--host="):
            return value.partition("=")[2]
    return os.getenv("ANM_BIND_ADDRESS", "0.0.0.0")


def _health_probe_host(host: str) -> str:
    value = str(host or "").strip()
    if value in {"", "0.0.0.0", "*"}:
        return "127.0.0.1"
    value = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if value in {"::", "0:0:0:0:0:0:0:0"}:
        value = "::1"
    return f"[{value.replace('%', '%25')}]" if ":" in value else value


def _port(arguments: list[str]) -> int:
    for index, value in enumerate(arguments):
        candidate: str | None = None
        if value == "--port" and index + 1 < len(arguments):
            candidate = arguments[index + 1]
        elif value.startswith("--port="):
            candidate = value.partition("=")[2]
        if candidate is not None:
            try:
                return int(candidate)
            except ValueError:
                break
    try:
        return int(os.getenv("ANM_WEB_PORT", "8787"))
    except ValueError:
        return 8787


def _healthy(port: int, version: str, host: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{_health_probe_host(host)}:{port}/api/health/live", timeout=2) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return False
    return payload.get("ok") is True and payload.get("service") == "AnimeMachine" and payload.get("version") == version


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=3)


def _await_pending(child: subprocess.Popen, pending: dict[str, str], arguments: list[str]) -> int | None:
    deadline = time.monotonic() + 120
    version = pending["version"]
    port = _port(arguments)
    host = _host(arguments)
    while time.monotonic() < deadline:
        code = child.poll()
        if code is not None:
            if _STOPPING:
                return code
            _rollback(pending)
            return None
        if _healthy(port, version, host):
            (_update_root() / "pending.json").unlink(missing_ok=True)
            return child.wait()
        time.sleep(1)
    if _STOPPING:
        return child.wait()
    _terminate(child)
    _rollback(pending)
    return None


def _forward_signal(signum: int, _frame) -> None:
    global _STOPPING
    _STOPPING = True
    child = _ACTIVE_CHILD
    if child is not None and child.poll() is None:
        child.send_signal(signum)


def main(arguments: list[str]) -> int:
    global _ACTIVE_CHILD
    _drop_privileges(arguments)
    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)
    is_server = not arguments or arguments[0] == "run"
    while True:
        release_root, selected_version = _selected_release()
        env = os.environ.copy()
        if release_root is not None:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(value for value in (str(release_root), existing) if value)
        child = subprocess.Popen([sys.executable, "-m", "animemachine", *(arguments or ["run"])], env=env)
        _ACTIVE_CHILD = child
        pending = _pending() if is_server else None
        if pending and pending.get("version") == selected_version:
            code = _await_pending(child, pending, arguments)
            if code is None:
                if _STOPPING:
                    return 143
                continue
        else:
            code = child.wait()
        _ACTIVE_CHILD = None
        if is_server and code == _RESTART_CODE and not _STOPPING:
            continue
        return int(code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
