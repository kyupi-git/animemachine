"""Persist Web-entered service credentials outside public configuration."""
from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CREDENTIAL_FILES = {
    "ANM_QBT_API_KEY": "qbittorrent-api-key",
    "ANM_ANI_RSS_API_KEY": "ani-rss-api-key",
    "ASSRT_API_TOKEN": "assrt-api-token",
    "OPEN_SUBTITLES_API_KEY": "opensubtitles-api-key",
}
CREDENTIAL_FILE_ENV = {
    "ANM_QBT_API_KEY": "ANM_QBT_API_KEY_FILE",
    "ANM_ANI_RSS_API_KEY": "ANM_ANI_RSS_API_KEY_FILE",
}
_STATE_LOADED: set[str] = set()


def _state_dir(state_dir: Path | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    configured = os.getenv("ANM_STATE_DIR", "").strip()
    return Path(configured) if configured else PROJECT_ROOT / ".local" / "state"


def credential_path(environment: str, state_dir: Path | None = None) -> Path:
    try:
        name = CREDENTIAL_FILES[environment]
    except KeyError as exc:
        raise ValueError(f"unsupported credential environment: {environment}") from exc
    return _state_dir(state_dir) / "credentials" / name


def _restrict_permissions(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700 if directory else 0o600)
        return
    username = os.getenv("USERNAME", "").strip()
    domain = os.getenv("USERDOMAIN", "").strip()
    principal = f"{domain}\\{username}" if domain and username else username
    if not principal:
        return
    principal_access = f"{principal}:(OI)(CI)F" if directory else f"{principal}:F"
    system_access = "*S-1-5-18:(OI)(CI)F" if directory else "*S-1-5-18:F"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", principal_access, system_access, "/Q"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def store(environment: str, value: str, state_dir: Path | None = None) -> Path:
    secret = str(value or "").strip()
    if not secret:
        raise ValueError("credential must not be empty")
    path = credential_path(environment, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(path.parent, directory=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(secret + "\n")
        temporary.replace(path)
        _restrict_permissions(path)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    deployment_file = _deployment_file(environment)
    deployment_managed = (
        bool(os.getenv(environment, "").strip()) and environment not in _STATE_LOADED
    ) or deployment_file is not None
    if not deployment_managed:
        os.environ[environment] = secret
        _STATE_LOADED.add(environment)
    return path


def store_many(values: dict[str, str], state_dir: Path | None = None) -> dict[str, Path]:
    """Persist multiple credentials as one rollback-safe local transaction."""
    prepared: dict[str, str] = {}
    snapshots: dict[str, tuple[Path, bytes | None, bool, str | None, bool]] = {}
    for environment, value in values.items():
        if environment not in CREDENTIAL_FILES:
            raise ValueError(f"unsupported credential environment: {environment}")
        secret = str(value or "").strip()
        if not secret:
            continue
        path = credential_path(environment, state_dir)
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = None
        snapshots[environment] = (
            path, previous, environment in os.environ, os.environ.get(environment), environment in _STATE_LOADED
        )
        prepared[environment] = secret
    stored: dict[str, Path] = {}
    try:
        for environment, secret in prepared.items():
            stored[environment] = store(environment, secret, state_dir)
        return stored
    except Exception:
        for environment, (path, previous, had_environment, environment_value, was_state_loaded) in snapshots.items():
            with contextlib.suppress(OSError):
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=path.parent)
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(previous)
                        temporary.replace(path)
                        _restrict_permissions(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            if had_environment and environment_value is not None:
                os.environ[environment] = environment_value
            else:
                os.environ.pop(environment, None)
            if was_state_loaded:
                _STATE_LOADED.add(environment)
            else:
                _STATE_LOADED.discard(environment)
        raise


def _deployment_file(environment: str) -> Path | None:
    file_environment = CREDENTIAL_FILE_ENV.get(environment)
    if not file_environment:
        return None
    value = os.getenv(file_environment, "").strip()
    return Path(value) if value else None


def load_into_environment(state_dir: Path | None = None) -> set[str]:
    """Load durable credentials only when deployment environment/secrets are absent."""
    loaded: set[str] = set()
    for environment in CREDENTIAL_FILES:
        if os.getenv(environment, "").strip() and environment not in _STATE_LOADED:
            continue
        deployment_file = _deployment_file(environment)
        if deployment_file is not None:
            source = deployment_file
            source_is_state = False
        else:
            source = credential_path(environment, state_dir)
            source_is_state = True
        if not source.is_file():
            continue
        try:
            secret = source.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if secret:
            os.environ[environment] = secret
            if source_is_state:
                _STATE_LOADED.add(environment)
            else:
                _STATE_LOADED.discard(environment)
            loaded.add(environment)
    return loaded
