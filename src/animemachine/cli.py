#!/usr/bin/env python3
"""AnimeMachine (ANM) container entry point with resumable bootstrap."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import secrets
import string
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from collections.abc import Callable

from .catalog import service as catalog, metadata_repair
from .torrents import runtime as runtime_catalog, mapper as torrent_mapper
from .library import audit as library_audit, external as external_library
from .integrations import ani_rss
from .network import transport
from .config.policy import ConfigStore
from .config import credentials as credential_store
from .storage import AVAILABLE, StorageUnavailableError, status_for_path
from .storage import preflight as storage_preflight
from . import __version__


STATE = Path(os.getenv("ANM_STATE_DIR", "/Data/state"))
DB = Path(os.getenv("ANM_CATALOG_DB", str(STATE / "catalog" / "anime-catalog.sqlite3")))
ARCHIVE = Path(os.getenv("ANM_ARCHIVE_DIR", str(STATE / "metadata" / "archive")))
CACHE = Path(os.getenv("ANM_METADATA_CACHE_DIR", str(STATE / "metadata" / "cache")))
CONFIG = Path(os.getenv("ANM_CONFIG_PATH", "/Config/config.json"))
HEARTBEAT = STATE / "bootstrap.heartbeat"
RUNTIME_DB = Path(os.getenv("ANM_RUNTIME_CATALOG_DB", str(STATE / "catalog" / "runtime.sqlite3")))
OFFLINE_METADATA = Path(os.getenv("ANM_OFFLINE_METADATA", str(STATE / "metadata" / "anime-offline-database.json")))
MANIFEST_CACHE = Path(os.getenv("ANM_MANIFEST_CACHE", str(STATE / "torrent-assets" / "pool-manifests-current.json")))
PLAN_DIR = Path(os.getenv("ANM_PLAN_DIR", str(STATE / "plans")))
TORRENT_POOL = Path(os.getenv("ANM_TORRENT_POOL_DIR", "/Torrents"))
SYNC_STATUS = Path(os.getenv("ANM_SYNC_STATUS_FILE", str(STATE / "sync-status.json")))


def _secret_value(value_env: str, file_env: str, default_file: str = "") -> str:
    value = os.getenv(value_env, "").strip()
    if value:
        return value
    file_name = os.getenv(file_env, default_file).strip()
    if not file_name:
        return ""
    path = Path(file_name)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _persist_bootstrap_secret(path: Path, value: str) -> None:
    """Persist a generated/shared service secret without exposing it in logs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value + "\n")
    temporary.replace(path)
    _restrict_credential_permissions(path)


def _shared_bootstrap_secret(
    config_dir: Path, name: str, value_env: str, file_env: str, default_file: str,
    generator: Callable[[], str], validator: Callable[[str], bool], error_message: str,
) -> str:
    """Use deployment input when present, otherwise reuse or generate a stable secret."""
    supplied = _secret_value(value_env, file_env, default_file)
    shared_path = config_dir / ".animemachine" / name
    persisted = ""
    if shared_path.is_file():
        with contextlib.suppress(OSError):
            persisted = shared_path.read_text(encoding="utf-8").strip()
    value = supplied or persisted or str(generator())
    if not validator(value):
        raise ValueError(error_message)
    if value != persisted:
        _persist_bootstrap_secret(shared_path, value)
    return value


class InstanceLock:
    """Prevent two AnimeMachine processes from writing one persistent state."""
    def __init__(self, state: Path) -> None:
        self.path = state / "animemachine.instance.lock"
        self.handle: object | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError(f"AnimeMachine state is already in use: {STATE}") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        handle.seek(0)
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self.handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


def bootstrap_qbittorrent(config_dir: Path) -> None:
    """Seed only the API-key settings needed by the bundled qBittorrent.

    This runs as a one-shot Compose service before qBittorrent starts. Existing
    preferences are preserved and the secret is never printed.
    """
    alphabet = string.ascii_letters + string.digits
    qbt_key = _shared_bootstrap_secret(
        config_dir, "api-key", "ANM_QBT_API_KEY", "ANM_QBT_API_KEY_FILE",
        "/run/secrets/qbittorrent_api_key",
        lambda: "qbt_" + "".join(secrets.choice(alphabet) for _ in range(28)),
        lambda value: re.fullmatch(r"qbt_[A-Za-z0-9]{28}", value) is not None,
        "qBittorrent API key must use qbt_ followed by 28 alphanumeric characters",
    )
    username = os.getenv("ANM_QBT_WEB_USERNAME", "admin").strip() or "admin"
    web_secret = _shared_bootstrap_secret(
        config_dir, "web-password", "ANM_QBT_WEB_PASSWORD", "ANM_QBT_WEB_PASSWORD_FILE",
        "/run/secrets/qbittorrent_web_password",
        lambda: "qbt_" + secrets.token_urlsafe(24),
        lambda value: len(value) >= 12,
        "qBittorrent Web password must contain at least 12 characters",
    )
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha512", web_secret.encode(), salt, 100000, dklen=64)
    password_value = f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()})"'
    path = config_dir / "qBittorrent" / "qBittorrent.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else "[Preferences]\n"
    def set_value(section: str, key: str, value: str) -> None:
        nonlocal text
        if f"[{section}]" not in text:
            text = text.rstrip() + f"\n\n[{section}]\n"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(lambda _match, k=key, v=value: f"{k}={v}", text)
        else:
            text = text.replace(f"[{section}]\n", f"[{section}]\n{key}={value}\n", 1)
    for key, value in ((r"WebUI\APIKey", qbt_key), (r"WebUI\Username", username),
                       (r"WebUI\Password_PBKDF2", password_value), (r"WebUI\Address", "0.0.0.0"),
                       (r"WebUI\ServerDomains", "*")):
        set_value("Preferences", key, value)
    library_dir = os.getenv("ANM_QBT_LIBRARY_DIR", "/Library").strip() or "/Library"
    incomplete_dir = os.getenv("ANM_INCOMPLETE_DIR", "/downloads/incomplete").strip() or "/downloads/incomplete"
    for key, value in ((r"Session\DefaultSavePath", library_dir),
                       (r"Session\TempPath", incomplete_dir),
                       (r"Session\TempPathEnabled", "true")):
        set_value("BitTorrent", key, value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def bootstrap_ani_rss(config_dir: Path) -> None:
    """Create the minimal first-run Ani-RSS configuration for the bundled stack.

    Existing settings are retained; only the shared API key and download-client
    connection are declared by Compose. External Ani-RSS instances are never
    modified by this command.
    """
    ani_key = _shared_bootstrap_secret(
        config_dir, "api-key", "ANM_ANI_RSS_API_KEY", "ANM_ANI_RSS_API_KEY_FILE",
        "/run/secrets/ani_rss_api_key", lambda: "ani_" + secrets.token_urlsafe(24),
        lambda value: len(value) >= 24 and not any(character.isspace() for character in value),
        "Ani-RSS API key must contain at least 24 non-space characters",
    )
    qbt_key = _secret_value("ANM_QBT_API_KEY", "ANM_QBT_API_KEY_FILE",
                            "/run/secrets/qbittorrent_api_key")
    if len(qbt_key) < 24 or any(character.isspace() for character in qbt_key):
        raise ValueError("qBittorrent API key must contain at least 24 non-space characters")
    path = config_dir / "config.v2.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    data.update({
        "apiKey": ani_key,
        "downloadToolType": "qBittorrent",
        "downloadToolHost": os.getenv("ANM_MANAGED_QBITTORRENT_URL", "http://qbittorrent:8080").strip(),
        "downloadToolUsername": "",
        "downloadToolPassword": qbt_key,
    })
    for key, value in (
        ("qbUseDownloadPath", True),
        ("downloadPathTemplate", "/Media/番剧/${title}/Season ${season}"),
        ("ovaDownloadPathTemplate", "/Media/剧场版/${title}"),
        ("autoStart", True),
    ):
        data.setdefault(key, value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sync_status(phase: str, *, state: str = "running", details: dict[str, object] | None = None) -> None:
    """Publish a small atomic progress record without blocking catalog reads."""
    payload = {"schemaVersion": 1, "phase": phase, "state": state,
               "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "details": details or {}}
    SYNC_STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = SYNC_STATUS.with_suffix(SYNC_STATUS.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(SYNC_STATUS)


def ensure_runtime_config() -> None:
    """Create the persistent config once; environment values remain runtime-only overlays."""
    if CONFIG.exists():
        ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read_persistent()
        return
    data = json.loads(catalog.EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).write(data)


def _loopback_host(host: str) -> bool:
    value = str(host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _auth_user_count() -> int:
    path = Path(os.getenv("ANM_AUTH_DB", str(STATE / "auth" / "auth.sqlite3")))
    if not path.is_file():
        return 0
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=2)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_user'").fetchone():
                return 0
            return int(db.execute("SELECT COUNT(*) FROM auth_user").fetchone()[0])
    except sqlite3.Error:
        return 0


def _bootstrap_admin_active(username: str) -> bool:
    path = Path(os.getenv("ANM_AUTH_DB", str(STATE / "auth" / "auth.sqlite3")))
    if not path.is_file() or not username.strip():
        return False
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=2)) as db:
            row = db.execute(
                "SELECT enabled,role FROM auth_user WHERE username=? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        return bool(row and int(row[0]) == 1 and str(row[1]) == "admin")
    except sqlite3.Error:
        return False


def _bootstrap_credential_file() -> Path:
    configured = os.getenv("ANM_BOOTSTRAP_CREDENTIAL_FILE", "").strip()
    return Path(configured) if configured else STATE / "auth" / "initial-admin.txt"


def _read_bootstrap_credentials(path: Path) -> tuple[str, str] | None:
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        return None
    username, password = values.get("ANM_ADMIN_USERNAME", ""), values.get("ANM_ADMIN_PASSWORD", "")
    return (username, password) if username and password else None


def _restrict_credential_permissions(path: Path) -> None:
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return
    username = os.getenv("USERNAME", "").strip()
    domain = os.getenv("USERDOMAIN", "").strip()
    principal = f"{domain}\\{username}" if domain and username else username
    if not principal:
        print(f"[security] warning: unable to determine the Windows account for {path}", file=sys.stderr)
        return
    try:
        result = subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"{principal}:F", "*S-1-5-18:F", "/Q"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        print(f"[security] warning: unable to restrict credential file permissions: {path}", file=sys.stderr)


def _write_bootstrap_credentials(path: Path, username: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"ANM_ADMIN_USERNAME={username}\nANM_ADMIN_PASSWORD={password}\n")
    _restrict_credential_permissions(path)


def configure_web_security(host: str, port: int) -> dict[str, str] | None:
    """Apply one authentication rule to CLI, local launchers and Compose deployments."""
    remote = not _loopback_host(host)
    explicit = os.getenv("ANM_AUTH_ENABLED")
    enabled = (remote if explicit is None or not explicit.strip()
               else explicit.strip().casefold() in {"1", "true", "yes", "on"})
    if remote and not enabled and os.getenv("ANM_ALLOW_REMOTE_NO_AUTH", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise ValueError("remote binding requires authentication; use loopback or explicitly enable the advanced remote-no-auth override")
    os.environ["ANM_AUTH_ENABLED"] = "true" if enabled else "false"
    if not enabled:
        return None
    path = _bootstrap_credential_file()
    if _auth_user_count() > 0:
        saved = _read_bootstrap_credentials(path)
        if saved and _bootstrap_admin_active(saved[0]):
            return {"address": f"http://127.0.0.1:{port}" if remote else f"http://{host}:{port}",
                    "username": saved[0], "password": saved[1], "path": str(path)}
        return None
    username = os.getenv("ANM_ADMIN_USERNAME", "").strip() or "admin"
    password = os.getenv("ANM_ADMIN_PASSWORD", "")
    if not password:
        saved = _read_bootstrap_credentials(path)
        if saved:
            username, password = saved
        else:
            password = "anm_" + secrets.token_urlsafe(24)
            _write_bootstrap_credentials(path, username, password)
    os.environ["ANM_ADMIN_USERNAME"] = username
    os.environ["ANM_ADMIN_PASSWORD"] = password
    address = f"http://127.0.0.1:{port}" if remote else f"http://{host}:{port}"
    return {"address": address, "username": username, "password": password, "path": str(path)}


def _print_initial_credentials(values: dict[str, str] | None) -> None:
    if not values:
        return
    print("\n========== AnimeMachine login =================", flush=True)
    print(f"Username: {values['username']}", flush=True)
    print(f"Password: {values['password']}", flush=True)
    print(f"Saved:    {values['path']}", flush=True)
    print("================================================\n", flush=True)


def migrate_legacy_state() -> None:
    """One-time migration from the pre-/Data layout, before any writer starts."""
    legacy = CONFIG.parent / "state"
    if (STATE / "catalog" / "anime-catalog.sqlite3").exists() or not (
            legacy / "catalog" / "anime-catalog.sqlite3").is_file():
        return
    STATE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(legacy, STATE, dirs_exist_ok=True)


def heartbeat(stop: threading.Event) -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    while not stop.wait(15):
        HEARTBEAT.touch()


def init_catalog(force: bool = False, *, sync_after: bool = True) -> None:
    ensure_runtime_config()
    config = ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read()
    if not DB.exists() or force:
        DB.parent.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        thread = threading.Thread(target=heartbeat, args=(stop,), daemon=True)
        HEARTBEAT.touch(); thread.start()
        try:
            args = argparse.Namespace(
                manifest=None, ids=None, all_anime=True, db=DB, archive=None,
                archive_dir=ARCHIVE, cache=CACHE, cache_days=30, refresh=False,
                request_interval=float(os.getenv("ANM_METADATA_REQUEST_INTERVAL", "0.6")),
                network_config=config.get("metadata", {}).get("network", {}),
                progress_callback=lambda value: sync_status(
                    str(value.get("phase") or "catalog_bootstrap"), details=value),
            )
            catalog.build(args)
        finally:
            stop.set(); HEARTBEAT.unlink(missing_ok=True)
    if sync_after:
        sync_runtime(scan_pool=False)


def ensure_catalog_shell() -> bool:
    """Expose a shell immediately and report whether Archive bootstrap is required.

    A process may stop after the shell is committed but before Archive import
    completes.  Treat that explicit pending shell as resumable work instead of
    assuming every existing database is a completed catalog.
    """
    ensure_runtime_config()
    if DB.is_file():
        try:
            with contextlib.closing(sqlite3.connect(
                    f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)) as db:
                if not db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
                    return False
                values = dict(db.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('archive_name','record_count')"))
                return (str(values.get("archive_name") or "") == "bootstrap-pending"
                        and int(values.get("record_count") or 0) == 0)
        except (sqlite3.Error, ValueError):
            # An unrecognized or damaged database must not be silently replaced.
            return False
    DB.parent.mkdir(parents=True, exist_ok=True)
    bundled = catalog.DEFAULT_DB
    if bundled.is_file() and bundled.resolve() != DB.resolve():
        shutil.copy2(bundled, DB)
    else:
        catalog.write_database(DB, [], {"name": "bootstrap-pending"})
    return True


def background_bootstrap(shell_created: bool, catalog_ready: threading.Event | None = None) -> None:
    """Build the real catalog first, then start prefetch before slower runtime sync."""
    try:
        with catalog.DATABASE_MAINTENANCE_LOCK:
            if shell_created:
                init_catalog(force=True, sync_after=False)
                if catalog_ready is not None:
                    catalog_ready.set()
            sync_runtime(scan_pool=True, sync_ani_rss=False)
        with catalog.background_task_lease("metadata") as allowed:
            if allowed:
                with catalog.DATABASE_MAINTENANCE_LOCK:
                    metadata_repair.run_batch(DB, ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read())
    except Exception as exc:
        sync_status("deferred", state="error", details={"errorType": type(exc).__name__})
        print(f"[bootstrap] Deferred: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def sync_runtime(*, scan_pool: bool = True, sync_ani_rss: bool = True) -> dict[str, object]:
    """Run deterministic runtime synchronization.

    The Web product owns automatic Ani-RSS snapshot refresh in one dedicated
    monitor; explicit CLI syncs may still include it.
    """
    result: dict[str, object] = {}
    current_config = ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read()
    performance = current_config.get("performance", {})
    torrent_pool = Path(str(current_config.get("deployment", {}).get("torrentPoolRoot") or TORRENT_POOL))
    sync_status("starting")
    if scan_pool:
        library_root = Path(str(current_config.get("deployment", {}).get("libraryUncRoot") or "/Library"))
        library_storage = status_for_path(library_root, timeout=4.0)
        library_available = library_storage.state == AVAILABLE
        if not library_available:
            result["libraryStorage"] = {"state": library_storage.state, "scan": "skipped"}
        pool_storage = status_for_path(torrent_pool, timeout=4.0)
        if pool_storage.state != AVAILABLE:
            result["torrentPool"] = {"state": pool_storage.state, "scan": "skipped"}
        else:
            RUNTIME_DB.parent.mkdir(parents=True, exist_ok=True)
            queue = STATE / "review" / "unmapped-torrents.json"
            queue.parent.mkdir(parents=True, exist_ok=True)
            common = [sys.executable, "-m", "animemachine.torrents.scanner", str(torrent_pool), "--db", str(RUNTIME_DB),
                      "--config", str(CONFIG), "--queue-output", str(queue),
                      "--workers", str(int(performance.get("poolScanWorkers", 0))),
                      "--commit-every", str(int(performance.get("poolCommitEvery", 100))),
                      "--progress-file", str(SYNC_STATUS)]
            initial_batch = max(0, int(performance.get("initialTorrentBatch", 500)))
            pool_scan_available = True
            with contextlib.closing(sqlite3.connect(RUNTIME_DB)) as runtime:
                source_count = runtime.execute("SELECT COUNT(*) FROM torrent_source").fetchone()[0] if runtime.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='torrent_source'").fetchone()[0] else 0
            if source_count == 0 and initial_batch:
                partial = subprocess.run([*common, "--limit", str(initial_batch)], check=False, text=True, capture_output=True)
                if partial.returncode == 3:
                    result["torrentPool"] = {"state": "unavailable", "scan": "partial"}
                    pool_scan_available = False
                if partial.returncode not in {0, 2, 3}:
                    raise RuntimeError(partial.stderr[-1000:] or "initial torrent pool scan failed")
                if pool_scan_available and library_available:
                    try:
                        result["initialAutoMap"] = torrent_mapper.auto_map(
                            DB, RUNTIME_DB, current_config,
                            progress=lambda stats: sync_status("initial_torrent_mapping", details=stats),
                        )
                        result["initialOverlay"] = runtime_catalog.sync_overlay(
                            DB, RUNTIME_DB,
                            offline_metadata=OFFLINE_METADATA if OFFLINE_METADATA.is_file() else None,
                            manifest_json=MANIFEST_CACHE if MANIFEST_CACHE.is_file() else None,
                        )
                        sync_status("partial_ready", details={"initialTorrentBatch": initial_batch})
                    except StorageUnavailableError:
                        library_available = False
                        result["libraryStorage"] = {"state": "unavailable", "scan": "skipped"}
            if pool_scan_available:
                completed = subprocess.run(common, check=False, text=True, capture_output=True)
                result["poolScanExitCode"] = completed.returncode
                if completed.returncode == 3:
                    result["torrentPool"] = {"state": "unavailable", "scan": "partial"}
                    pool_scan_available = False
                elif completed.returncode not in {0, 2}:
                    raise RuntimeError(completed.stderr[-1000:] or "torrent pool scan failed")
            if pool_scan_available and library_available:
                try:
                    sync_status("torrent_mapping")
                    result["autoMap"] = torrent_mapper.auto_map(
                        DB, RUNTIME_DB, current_config,
                        progress=lambda stats: sync_status("torrent_mapping", details=stats),
                    )
                    result["pathReconciliation"] = torrent_mapper.reconcile_existing_paths(
                        DB, RUNTIME_DB, current_config,
                        progress=lambda stats: sync_status("path_reconciliation", details=stats),
                    )
                except StorageUnavailableError:
                    result["libraryStorage"] = {"state": "unavailable", "scan": "skipped"}
    if sync_ani_rss:
        sync_status("ani_rss")
        result["aniRss"] = ani_rss.sync(DB, current_config)
    sync_status("external_library")
    external_sources = list(current_config.get("externalLibraries", []))
    ani_source = ani_rss.media_source(current_config)
    if ani_source.get("path"):
        external_sources = [source for source in external_sources
                            if str(source.get("path")) != str(ani_source["path"])]
    if sync_ani_rss and ani_source.get("enabled") and ani_source.get("path"):
        external_sources.append(ani_source)
    result["externalLibraries"] = external_library.scan(
        DB, external_sources,
        progress=lambda stats: sync_status("external_library", details=stats),
    )
    if RUNTIME_DB.is_file():
        sync_status("runtime_overlay")
        result["overlay"] = runtime_catalog.sync_overlay(
            DB, RUNTIME_DB,
            offline_metadata=OFFLINE_METADATA if OFFLINE_METADATA.is_file() else None,
            manifest_json=MANIFEST_CACHE if MANIFEST_CACHE.is_file() else None,
        )
    else:
        with contextlib.closing(sqlite3.connect(DB)) as db, db:
            runtime_catalog.migrate_overlay(db)
        result["overlay"] = {"torrents": 0, "mappedRuntimeWorks": 0}
    sync_status("library_audit")
    result["libraryAudit"] = library_audit.audit(
        DB, current_config,
        progress=lambda stats: sync_status("library_audit", details=stats),
        commit_every=int(performance.get("libraryCommitEvery", 100)),
    )
    result["metadataRepairQueued"] = metadata_repair.enqueue(DB)
    sync_status("ready", state="complete", details={"runtime": result.get("overlay", {})})
    print(f"[sync] Complete: {json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)
    return result


def periodic_sync(stop: threading.Event, bootstrap_finished: threading.Event | None = None) -> None:
    if bootstrap_finished is not None:
        while not bootstrap_finished.wait(1):
            if stop.is_set():
                return
    last_run = time.monotonic()
    while not stop.wait(30):
        current = ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read()
        configured = current.get("components", {}).get("discovery", {}).get("pollMinutes", 30)
        minutes = max(5, int(os.getenv("ANM_SYNC_INTERVAL_MINUTES", str(configured))))
        if time.monotonic() - last_run < minutes * 60:
            continue
        try:
            with catalog.DATABASE_MAINTENANCE_LOCK:
                sync_runtime(scan_pool=True, sync_ani_rss=False)
            with catalog.background_task_lease("metadata", stop_event=stop) as allowed:
                if allowed:
                    with catalog.DATABASE_MAINTENANCE_LOCK:
                        metadata_repair.run_batch(DB, ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read())
            last_run = time.monotonic()
        except Exception as exc:
            sync_status("deferred", state="error", details={"errorType": type(exc).__name__})
            print(f"[sync] Background synchronization deferred: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            # Retry after a short cooldown instead of spinning every 30 s.
            last_run = time.monotonic() - max(0, minutes * 60 - 300)


def healthcheck() -> int:
    try:
        port = int(os.getenv("ANM_WEB_PORT", "8787"))
        target = os.getenv("ANM_HEALTHCHECK_URL", f"http://127.0.0.1:{port}/api/health/live")
        with transport.open_url(target, timeout=3, max_bytes=64 * 1024) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 90:
            return 0
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"AnimeMachine {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    run_parser = sub.choices["run"]
    run_parser.add_argument("--host", default=os.getenv("ANM_BIND_ADDRESS", "0.0.0.0"))
    run_parser.add_argument("--port", type=int, default=int(os.getenv("ANM_WEB_PORT", "8787")))
    init = sub.add_parser("init")
    init.add_argument("--force", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("healthcheck")
    sub.add_parser("validate-config")
    sub.add_parser("sync")
    storage_parser = sub.add_parser("storage-preflight")
    storage_parser.add_argument("--all", action="store_true", dest="require_all")
    storage_parser.add_argument("--timeout", type=float, default=4.0)
    qbt_bootstrap = sub.add_parser("qbt-bootstrap")
    qbt_bootstrap.add_argument("--config-dir", type=Path, default=Path("/QbtConfig"))
    ani_bootstrap = sub.add_parser("ani-rss-bootstrap")
    ani_bootstrap.add_argument("--config-dir", type=Path, default=Path("/AniRssConfig"))
    collector_parser = sub.add_parser("torrent-collector")
    collector_parser.add_argument("--self-test", action="store_true")
    collector_parser.add_argument("--audit-only", action="store_true")
    collector_parser.add_argument("--once", action="store_true")
    collector_parser.add_argument("--restore-quarantine", type=int, metavar="MOVE_ID")
    args = parser.parse_args()
    if args.command == "storage-preflight":
        return storage_preflight.run(require_all=args.require_all, timeout=args.timeout)
    if args.command == "qbt-bootstrap":
        bootstrap_qbittorrent(args.config_dir)
        return 0
    if args.command == "ani-rss-bootstrap":
        bootstrap_ani_rss(args.config_dir)
        return 0
    if args.command == "torrent-collector":
        from .torrents import collector
        return collector.main(self_test=args.self_test, audit_only=args.audit_only, once=args.once, restore_quarantine=args.restore_quarantine)
    migrate_legacy_state()
    ensure_runtime_config()
    credential_store.load_into_environment(STATE)
    if args.command == "healthcheck":
        return healthcheck()
    if args.command == "validate-config":
        ConfigStore(CONFIG, catalog.EXAMPLE_CONFIG).read()
        return 0
    if args.command == "init":
        init_catalog(args.force)
        return 0
    if args.command == "sync":
        init_catalog(sync_after=False); sync_runtime(scan_pool=True)
        return 0
    if args.command == "run":
        initial_credentials = configure_web_security(args.host, args.port)
        _print_initial_credentials(initial_credentials)
        instance_lock = InstanceLock(STATE)
        instance_lock.acquire()
        try:
            shell_created = ensure_catalog_shell()
            stop = threading.Event()
            bootstrap_finished = threading.Event()
            catalog_ready = threading.Event()
            if not shell_created:
                catalog_ready.set()
            def bootstrap_after_web_ready() -> None:
                # Let the first HTML/API requests complete before background writers
                # begin. WAL keeps later reads responsive while synchronization runs.
                try:
                    if not stop.wait(3):
                        background_bootstrap(shell_created, catalog_ready)
                finally:
                    bootstrap_finished.set()

            def start_background_workers() -> None:
                threading.Thread(target=bootstrap_after_web_ready, daemon=True,
                                 name="anm-bootstrap").start()
                threading.Thread(target=periodic_sync, args=(stop, bootstrap_finished), daemon=True,
                                 name="anm-periodic-sync").start()
            restart_requested = catalog.serve(
                DB, args.host, args.port, CONFIG,
                submission_enabled=os.getenv("ANM_SUBMISSION_ENABLED", "true").casefold() == "true",
                plan_dir=PLAN_DIR, ready_callback=start_background_workers,
                background_ready=bootstrap_finished, warmup_ready=catalog_ready,
                print_access_info=True)
        finally:
            instance_lock.release()
        if restart_requested and os.getenv("ANM_DOCKER_UPDATE_RUNTIME", "").strip() == "1":
            return 75
        return 0
    else:
        start_background_workers = None
        bootstrap_finished = None
    legacy_host = os.getenv("ANM_BIND_ADDRESS", "0.0.0.0")
    legacy_port = int(os.getenv("ANM_WEB_PORT", "8787"))
    _print_initial_credentials(configure_web_security(legacy_host, legacy_port))
    catalog.serve(DB, legacy_host, legacy_port, CONFIG,
                  submission_enabled=os.getenv("ANM_SUBMISSION_ENABLED", "true").casefold() == "true",
                  plan_dir=PLAN_DIR, ready_callback=start_background_workers,
                  background_ready=bootstrap_finished)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
