"""Safe release discovery and self-update for portable and Docker AnimeMachine installs."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .network import downloads, registry as network_registry, sources as network_sources, transport
from .network.health import Store


_REPOSITORY = "kyupi-git/animemachine"
_LATEST_RELEASE_API = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"
_RELEASE_PAGE = f"https://github.com/{_REPOSITORY}/releases/latest"
_CACHE_SECONDS = 30 * 60
_MAX_RELEASE_JSON = 2 * 1024 * 1024
_MAX_RELEASE_BYTES = 1024 * 1024 * 1024
_VERSION_RE = re.compile(r"^(?:v)?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)
_CACHE_LOCK = threading.RLock()
_CACHE: tuple[float, dict[str, Any]] | None = None
_UPDATE_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_LAST_RELEASE_SOURCE = ""
_UPDATE_API_SERVICE = "application_update_api"
_UPDATE_API_CAPABILITY = "update_json"
_UPDATE_ROUTE_CAPABILITY = "update_route"


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    return tuple(map(int, match.groups())) if match else None


def _install_mode() -> tuple[str, Path | None]:
    mode = os.getenv("ANM_INSTALL_MODE", "").strip().casefold()
    root_value = os.getenv("ANM_INSTALL_ROOT", "").strip()
    root = Path(root_value).resolve() if root_value else None
    if mode in {"portable", "source", "docker"}:
        return mode, root
    if Path("/.dockerenv").exists():
        return "docker", root
    return "source", root


def _portable_asset_name(version: str) -> str | None:
    if os.name == "nt":
        return f"AnimeMachine-{version}-release-windows.zip"
    system = platform.system().casefold()
    release_platform = {"linux": "linux", "darwin": "macos"}.get(system)
    if not release_platform:
        return None
    architecture = platform.machine().strip()
    if not architecture:
        return None
    return f"AnimeMachine-{version}-release-{release_platform}-{architecture}.tar.gz"


def _docker_asset_name(version: str) -> str:
    return f"animemachine-{version}-py3-none-any.whl"


def _health_probe_host(host: str) -> str:
    value = str(host or "").strip()
    if value in {"", "0.0.0.0", "*"}:
        return "127.0.0.1"
    value = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if value in {"::", "0:0:0:0:0:0:0:0"}:
        value = "::1"
    return f"[{value.replace('%', '%25')}]" if ":" in value else value


def _asset_name(version: str, mode: str | None = None) -> str | None:
    return _docker_asset_name(version) if mode == "docker" else _portable_asset_name(version)


def _state_root() -> Path:
    configured = os.getenv("ANM_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("/Data/state") if Path("/.dockerenv").exists() else Path.cwd() / ".local" / "state"


def _docker_update_root() -> Path:
    return _state_root() / "application-update"


def _state_file() -> Path:
    return _docker_update_root() / "status.json"


def _read_state() -> dict[str, Any]:
    with _STATE_LOCK:
        try:
            value = json.loads(_state_file().read_text(encoding="utf-8"))
            return dict(value) if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def _record_state(**updates: Any) -> dict[str, Any]:
    """Persist small, non-secret update state without making update checks depend on storage."""
    with _STATE_LOCK:
        current = _read_state()
        current.update(updates)
        path = _state_file()
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(current, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return current


def update_state() -> dict[str, Any]:
    return _read_state()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_probe(parent: Path) -> bool:
    probe = parent / f".animemachine-write-test-{uuid.uuid4().hex}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe.mkdir()
        probe.rmdir()
    except OSError:
        return False
    return True


def _availability(mode: str, root: Path | None, asset_name: str | None) -> tuple[bool, str]:
    if mode == "docker":
        if os.getenv("ANM_DOCKER_UPDATE_RUNTIME", "").strip() != "1":
            return False, "docker_update_runtime_unavailable"
        if not asset_name:
            return False, "platform_not_supported"
        if not _write_probe(_docker_update_root()):
            return False, "docker_state_not_writable"
        return True, ""
    if mode != "portable":
        return False, "portable_release_required"
    if root is None or not root.is_dir():
        return False, "install_root_unavailable"
    if not asset_name:
        return False, "platform_not_supported"
    if not (root / "VERSION").is_file():
        return False, "portable_release_invalid"
    if not _write_probe(root.parent):
        return False, "install_parent_not_writable"
    return True, ""


def _proxy_templates() -> list[str]:
    try:
        _endpoints, payload = network_registry.load()
    except (OSError, ValueError):
        return []
    values = payload.get("applicationUpdateProxies", payload.get("archiveAssetProxies", []))
    return [str(item).strip() for item in values if str(item).strip() and "{url}" in str(item)]


def _candidate_urls(url: str) -> list[str]:
    return network_sources.asset_urls(url, _proxy_templates())


def _release_payload() -> dict[str, Any]:
    global _LAST_RELEASE_SOURCE
    payload, final_url = network_sources.fetch_json(
        _candidate_urls(_LATEST_RELEASE_API),
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=8, attempts=1, hedge_delays=(0, .45, .9),
        service=_UPDATE_API_SERVICE, capability=_UPDATE_API_CAPABILITY, honor_cooldown=True,
    )
    if not isinstance(payload, dict):
        raise ValueError("invalid release response")
    _LAST_RELEASE_SOURCE = str(final_url or "")
    return payload


def _checksum_from_digest(asset: dict[str, Any]) -> str:
    digest = str(asset.get("digest") or "").strip().casefold()
    if digest.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", digest[7:]):
        return digest[7:]
    return ""


def _checksum_from_asset(asset: dict[str, Any]) -> str:
    url = str(asset.get("browser_download_url") or "")
    if not url:
        return ""
    data, _mime, final_url = network_sources.fetch_binary(
        _candidate_urls(url), timeout=8, limit=4096, attempts=1, hedge_delays=(0, .45, .9),
        service="application_update_checksum", capability="update_checksum", honor_cooldown=True)
    if final_url:
        _record_state(downloadSource=str(final_url))
    text = bytes(data).decode("utf-8", errors="replace").strip()
    match = re.search(r"(?i)(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])", text)
    return match.group(1).casefold() if match else ""


def _release_asset_url(tag: str, name: str) -> str:
    if not _version_tuple(tag) or not name or "/" in name or "\\" in name:
        raise ValueError("invalid release asset identity")
    normalized_tag = tag if tag.casefold().startswith("v") else f"v{tag}"
    return f"https://github.com/{_REPOSITORY}/releases/download/{normalized_tag}/{name}"


def _build_status(payload: dict[str, Any]) -> dict[str, Any]:
    current_tuple = _version_tuple(__version__)
    tag = str(payload.get("tag_name") or "").strip()
    latest_tuple = _version_tuple(tag)
    latest = ".".join(map(str, latest_tuple)) if latest_tuple else ""
    update_available = bool(current_tuple and latest_tuple and latest_tuple > current_tuple)
    mode, root = _install_mode()
    expected_name = _asset_name(latest, mode) if latest else None
    assets = [item for item in payload.get("assets", []) if isinstance(item, dict)]
    asset = next((item for item in assets if item.get("name") == expected_name), None)
    checksum_asset = next((item for item in assets if item.get("name") == f"{expected_name}.sha256"), None)
    can_update, reason = _availability(mode, root, expected_name)
    if update_available and asset is None:
        can_update, reason = False, "release_asset_unavailable"
    checksum = _checksum_from_digest(asset or {})
    if update_available and asset is not None and not checksum and checksum_asset is None:
        can_update, reason = False, "release_checksum_unavailable"
    asset_url = _release_asset_url(tag, expected_name) if asset and expected_name and latest_tuple else ""
    checksum_url = (_release_asset_url(tag, str(checksum_asset.get("name")))
                    if checksum_asset and latest_tuple else "")
    normalized_tag = f"v{latest}" if latest else ""
    return {
        "currentVersion": __version__,
        "latestVersion": latest or tag,
        "updateAvailable": update_available,
        "releaseUrl": f"https://github.com/{_REPOSITORY}/releases/tag/{normalized_tag}" if normalized_tag else _RELEASE_PAGE,
        "installMode": mode,
        "canUpdate": bool(update_available and can_update),
        "reason": reason if update_available and not can_update else "",
        "asset": {
            "name": str(asset.get("name") or "") if asset else "",
            "url": asset_url,
            "size": int(asset.get("size") or 0) if asset else 0,
            "sha256": checksum,
            "checksumUrl": checksum_url,
        },
    }


def _endpoint_id(url: str, service: str) -> str:
    return network_sources._endpoints([url], service)[0].id


def _safe_failure(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return f"HTTP {status_code}" if status_code else type(exc).__name__


def _source_label(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or url


def _source_entries(release: dict[str, Any] | None = None) -> list[dict[str, str]]:
    entries = [{"service": "application_update_github", "kind": "github", "url": _RELEASE_PAGE,
                "capability": _UPDATE_ROUTE_CAPABILITY}]
    entries.extend({"service": _UPDATE_API_SERVICE, "kind": "api", "url": url,
                    "capability": _UPDATE_API_CAPABILITY} for url in _candidate_urls(_LATEST_RELEASE_API))
    asset_url = str((release or {}).get("asset", {}).get("url") or "")
    if asset_url:
        entries.extend({"service": "application_update_release", "kind": "release", "url": url,
                        "capability": _UPDATE_ROUTE_CAPABILITY} for url in _candidate_urls(asset_url))
    seen: set[tuple[str, str]] = set()
    result = []
    for item in entries:
        key = (item["service"], item["url"])
        if key not in seen:
            seen.add(key); result.append(item)
    return result


def _probe_entry(item: dict[str, str], store: Store, *, timeout: float, honor_cooldown: bool = True) -> None:
    url, service, capability = item["url"], item["service"], item["capability"]
    endpoint_id = _endpoint_id(url, service)
    profile = transport.network_profile(url)
    route_mode, network_id = str(profile["routeMode"]), str(profile["id"])
    if honor_cooldown and store.evaluation(endpoint_id, capability, route_mode, network_id).get("coolingDown"):
        return
    started = time.monotonic()
    try:
        if item["kind"] == "api":
            response = transport.request(
                "GET", url,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                timeout=timeout, max_bytes=_MAX_RELEASE_JSON,
            )
            payload = json.loads(response.content.decode("utf-8-sig"))
            if not isinstance(payload, dict) or not _version_tuple(str(payload.get("tag_name") or "")):
                raise ValueError("invalid release response")
        else:
            response = transport.request("HEAD", url, timeout=timeout, max_bytes=_MAX_RELEASE_BYTES)
        actual_route = dict(response.extensions.get("animemachine_proxy_route") or {})
        route_mode = str(actual_route.get("mode") or route_mode)
        active_profile = transport.network_profile(url, route_mode)
        store.success(endpoint_id, capability, max(.001, time.monotonic() - started), len(response.content),
                      route_mode=route_mode, network_id=str(active_profile.get("id") or network_id))
    except Exception as exc:
        failed_profile = transport.network_profile(url)
        store.failure(endpoint_id, capability, _safe_failure(exc), route_mode=str(failed_profile["routeMode"]),
                      network_id=str(failed_profile.get("id") or network_id))


def _render_source_diagnostics(entries: list[dict[str, str]], store: Store) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, str]]] = {}
    for item in entries:
        groups.setdefault(item["service"], []).append(item)
    selections: dict[str, tuple[list[str], dict[str, Any]]] = {}
    for service, items in groups.items():
        candidates = []
        for item in items:
            profile = transport.network_profile(item["url"])
            candidates.append((_endpoint_id(item["url"], service), str(profile["routeMode"]), str(profile["id"])))
        selections[service] = store.rank(candidates, items[0]["capability"], persist=False)
    labels = {"github": "GitHub", "api": "GitHub API", "release": "GitHub Release"}
    for item in entries:
        url, service, capability = item["url"], item["service"], item["capability"]
        endpoint_id = _endpoint_id(url, service)
        route = transport.proxy_route(url); profile = transport.network_profile(url)
        snap = store.snapshot(endpoint_id, capability, str(route.get("mode") or "direct"), str(profile["id"]))
        ordered, explanation = selections[service]
        rank = {value: index + 1 for index, value in enumerate(ordered)}
        parsed = urllib.parse.urlparse(url)
        direct_host = {"github": "github.com", "api": "api.github.com", "release": "github.com"}[item["kind"]]
        rendered.append({
            "service": service, "kind": item["kind"], "label": labels[item["kind"]],
            "baseUrl": url, "host": parsed.netloc, "sourceType": "direct" if parsed.netloc.casefold() == direct_host else "proxy",
            "route": route, "networkProfile": profile, **snap,
            "selection": {"selected": endpoint_id == explanation.get("selectedEndpointId"),
                          "rank": int(rank.get(endpoint_id, 0)), "reason": str(explanation.get("reason") or ""),
                          "quality": next((c.get("quality") for c in explanation.get("candidates", []) if c.get("endpointId") == endpoint_id), None)},
        })
    return rendered


def _probe_update_sources(*, recheck: bool, release: dict[str, Any] | None = None) -> dict[str, Any]:
    entries = _source_entries(release)
    store = Store()
    if recheck and entries:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(entries)), thread_name_prefix="anm-update-diag") as pool:
            futures = [pool.submit(_probe_entry, item, store, timeout=4.0, honor_cooldown=True) for item in entries]
            for future in futures:
                try:
                    future.result(timeout=5.0)
                except (concurrent.futures.TimeoutError, RuntimeError, OSError, ValueError):
                    pass
    items = _render_source_diagnostics(entries, store)
    selected_release = next((item for item in items if item["kind"] == "release" and item.get("selection", {}).get("selected")), None)
    selected_api = next((item for item in items if item["kind"] == "api" and item.get("selection", {}).get("selected")), None)
    return {
        "items": items,
        "selectedDownloadSource": str((selected_release or {}).get("baseUrl") or ""),
        "selectedApiSource": str((selected_api or {}).get("baseUrl") or _LAST_RELEASE_SOURCE),
        "checkedAt": _utc_now(),
    }


def _decorate_status(result: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    state = _read_state()
    latest = str(result.get("latestVersion") or "")
    verification = dict(state.get("verification") or {})
    if verification.get("version") == latest and verification.get("status") in {"verified", "failed"}:
        sha_status = str(verification["status"])
    elif result.get("updateAvailable") and (result.get("asset") or {}).get("sha256"):
        sha_status = "pending"
    elif result.get("updateAvailable"):
        sha_status = "unavailable"
    else:
        sha_status = "not_required"
    preferred = str(diagnostics.get("selectedDownloadSource") or "")
    state_source = str(state.get("downloadSource") or "")
    result.update({
        "downloadSource": preferred or state_source or _LAST_RELEASE_SOURCE,
        "downloadSourceLabel": _source_label(preferred or state_source or _LAST_RELEASE_SOURCE),
        "sha256Status": sha_status,
        "verification": verification,
        "upgradeResult": dict(state.get("lastUpgrade") or {}),
        "automaticResult": dict(state.get("lastAutomatic") or {}),
        "sourceDiagnostics": diagnostics,
    })
    return result


def status(*, force: bool = False) -> dict[str, Any]:
    """Return current/latest release state with persistent source health and update details."""
    global _CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE is not None and now - _CACHE[0] < _CACHE_SECONDS:
            return json.loads(json.dumps(_CACHE[1]))
    checked_at = _utc_now()
    try:
        result = _build_status(_release_payload())
        result["checkedAt"] = checked_at
        diagnostics = _probe_update_sources(recheck=force, release=result)
        result = _decorate_status(result, diagnostics)
        _record_state(lastCheck={"at": checked_at, "status": "ok", "latestVersion": str(result.get("latestVersion") or ""),
                                "source": str(_LAST_RELEASE_SOURCE or diagnostics.get("selectedApiSource") or "")})
    except Exception as exc:
        _record_state(lastCheck={"at": checked_at, "status": "failed", "error": type(exc).__name__})
        raise
    with _CACHE_LOCK:
        _CACHE = (now, json.loads(json.dumps(result)))
    return result


def network_diagnostics(*, force: bool = False) -> dict[str, Any]:
    try:
        return dict(status(force=force).get("sourceDiagnostics") or {})
    except Exception:
        return _probe_update_sources(recheck=False, release=None)


def _automatic_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = dict(config.get("applicationUpdate", {}).get("automaticCheck", {}) or {})
    return {"enabled": raw.get("enabled") is True,
            "mode": str(raw.get("mode") or "notify"), "time": str(raw.get("time") or "04:35")}


def automatic_check_due(config: dict[str, Any], now: dt.datetime | None = None) -> bool:
    settings = _automatic_settings(config)
    if not settings["enabled"]:
        return False
    current = now or dt.datetime.now().astimezone()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", settings["time"])
    if not match:
        return False
    target = current.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    if current < target:
        return False
    last = dict(_read_state().get("lastAutomatic") or {})
    return str(last.get("date") or "") != current.date().isoformat()


def record_automatic_result(*, date: str, mode: str, status_value: str, latest_version: str = "", message: str = "") -> None:
    _record_state(lastAutomatic={"date": date, "at": _utc_now(), "mode": mode, "status": status_value,
                                 "latestVersion": latest_version, "message": message[:240]})


def reconcile_upgrade_state() -> dict[str, Any]:
    """Finalize a previously staged upgrade after this process has restarted."""
    state = _read_state()
    last = dict(state.get("lastUpgrade") or {})
    if last.get("status") != "restarting":
        return last
    target = str(last.get("version") or "").strip()
    if target and _version_tuple(target) == _version_tuple(__version__):
        last.update({"status": "installed", "completedAt": _utc_now()})
        _record_state(lastUpgrade=last)
    return last


def _preferred_download_urls(release: dict[str, Any]) -> list[str]:
    """Order release routes by cached health and skip routes in active backoff."""
    asset_url = str((release.get("asset") or {}).get("url") or "")
    values = _candidate_urls(asset_url) if asset_url else []
    diagnostics = dict(release.get("sourceDiagnostics") or {})
    items = [item for item in diagnostics.get("items", []) if isinstance(item, dict) and item.get("kind") == "release"]
    if not items:
        return values
    by_url = {str(item.get("baseUrl") or ""): item for item in items}
    usable = [url for url in values if not bool((by_url.get(url) or {}).get("coolingDown"))]
    if not usable:
        usable = values
    def key(url: str) -> tuple[int, int, float, int]:
        item = by_url.get(url) or {}
        selected = bool((item.get("selection") or {}).get("selected"))
        failed_without_success = bool(item.get("lastFailure")) and not item.get("recentSuccessRate")
        latency = float(item.get("latencyMs")) if item.get("latencyMs") is not None else float("inf")
        return (0 if selected else 1, 1 if failed_without_success else 0, latency, values.index(url))
    return sorted(usable, key=key)


def _safe_member_path(name: str, expected_root: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    value = PurePosixPath(normalized)
    if value.is_absolute() or ".." in value.parts or not value.parts or value.parts[0] != expected_root:
        raise ValueError("release archive contains an unsafe path")
    return value


def _extract_release(archive: Path, destination: Path, expected_version: str) -> Path:
    expected_root = f"AnimeMachine-{expected_version}"
    destination.mkdir(parents=False, exist_ok=False)
    if archive.name.casefold().endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if not infos:
                raise ValueError("release archive is empty")
            for info in infos:
                _safe_member_path(info.filename, expected_root)
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise ValueError("release archive contains a symbolic link")
            source.extractall(destination)
    elif archive.name.casefold().endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if not members:
                raise ValueError("release archive is empty")
            for member in members:
                _safe_member_path(member.name, expected_root)
                if not (member.isfile() or member.isdir()):
                    raise ValueError("release archive contains an unsupported entry type")
            source.extractall(destination, members=members)
    else:
        raise ValueError("unsupported release archive")
    release_root = destination / expected_root
    version_file = release_root / "VERSION"
    if not version_file.is_file() or version_file.read_text(encoding="utf-8").strip() != expected_version:
        raise ValueError("release archive version mismatch")
    launcher = release_root / ("AnimeMachine.ps1" if os.name == "nt" else "AnimeMachine.sh")
    if not launcher.is_file():
        raise ValueError("release archive launcher is missing")
    return release_root


def _write_windows_worker(path: Path, *, root: Path, new_root: Path, old_pid: int, host: str, port: int) -> None:
    content = r'''param([string]$Root,[string]$NewRoot,[int]$OldPid,[string]$BindAddress,[string]$HealthHost,[int]$Port)
$ErrorActionPreference = 'Stop'
$Token = Split-Path -Leaf (Split-Path -Parent $NewRoot)
$Backup = "$Root.anm-backup-$Token"
$Failed = "$Root.anm-failed-$Token"
function Wait-AnmProcess([int]$PidValue) {
  while (Get-Process -Id $PidValue -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 250 }
}
function Move-Persistent([string]$From,[string]$To) {
  foreach ($Name in @('.env.local','config.json','data','imports')) {
    $Source = Join-Path $From $Name
    if (Test-Path -LiteralPath $Source) {
      $Target = Join-Path $To $Name
      if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
      Move-Item -LiteralPath $Source -Destination $Target -Force
    }
  }
}
function Start-Anm([string]$TargetRoot) {
  $Launcher = Join-Path $TargetRoot 'AnimeMachine.ps1'
  $Args = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $Launcher + '"'),'-BindAddress',('"' + $BindAddress + '"'),'-Port',[string]$Port)
  return Start-Process -FilePath 'powershell.exe' -ArgumentList $Args -WorkingDirectory $TargetRoot -PassThru
}
function Wait-Healthy([string]$ExpectedVersion) {
  $Deadline = [DateTime]::UtcNow.AddSeconds(120)
  while ([DateTime]::UtcNow -lt $Deadline) {
    try {
      $Health = Invoke-RestMethod -Uri "http://${HealthHost}:$Port/api/health/live" -TimeoutSec 2
      if ($Health.ok -eq $true -and $Health.service -eq 'AnimeMachine' -and $Health.version -eq $ExpectedVersion) { return $true }
    } catch {}
    Start-Sleep -Seconds 1
  }
  return $false
}
Wait-AnmProcess $OldPid
for ($i=0; $i -lt 120; $i++) {
  try {
    if (Test-Path -LiteralPath $Backup) { throw 'update backup path already exists' }
    Move-Item -LiteralPath $Root -Destination $Backup
    break
  } catch {
    if ($i -eq 119) { throw }
    Start-Sleep -Milliseconds 250
  }
}
try {
  Move-Item -LiteralPath $NewRoot -Destination $Root
  Move-Persistent $Backup $Root
  $ExpectedVersion = (Get-Content -LiteralPath (Join-Path $Root 'VERSION') -Raw).Trim()
  $Started = Start-Anm $Root
  if (-not (Wait-Healthy $ExpectedVersion)) { throw 'updated AnimeMachine did not become healthy' }
  Remove-Item -LiteralPath $Backup -Recurse -Force
} catch {
  if ($Started) { & taskkill.exe /PID $Started.Id /T /F 2>$null | Out-Null; Start-Sleep -Milliseconds 750 }
  if (Test-Path -LiteralPath $Failed) { throw 'update rollback path already exists' }
  if (Test-Path -LiteralPath $Root) { Move-Item -LiteralPath $Root -Destination $Failed }
  if (Test-Path -LiteralPath $Backup) { Move-Item -LiteralPath $Backup -Destination $Root }
  if (Test-Path -LiteralPath $Failed) { Move-Persistent $Failed $Root; Remove-Item -LiteralPath $Failed -Recurse -Force }
  Start-Anm $Root | Out-Null
  throw
}
$Stage = Split-Path -Parent $NewRoot
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
$Cleanup = $PSScriptRoot
Start-Process -FilePath 'cmd.exe' -WindowStyle Hidden -ArgumentList @('/c', "ping 127.0.0.1 -n 2 >nul & rmdir /s /q `\"$Cleanup`\"") | Out-Null
'''
    path.write_text(content, encoding="utf-8-sig")


def _write_unix_worker(path: Path, *, root: Path, new_root: Path, old_pid: int, host: str, port: int) -> None:
    # Arguments are passed separately, so the script itself contains no interpolated shell data.
    content = r'''#!/bin/sh
set -eu
root=$1
new_root=$2
old_pid=$3
bind_address=$4
port=$5
health_host=$6
token=$(basename "$(dirname "$new_root")")
backup="${root}.anm-backup-${token}"
failed="${root}.anm-failed-${token}"
while kill -0 "$old_pid" 2>/dev/null; do sleep 1; done
move_persistent() {
  from=$1; to=$2
  for name in .env.local config.json data imports; do
    if [ -e "$from/$name" ]; then
      rm -rf "$to/$name" || return 1
      mv "$from/$name" "$to/$name" || return 1
    fi
  done
}
rollback() {
  set +e
  [ -n "${new_pid:-}" ] && kill "$new_pid" 2>/dev/null
  [ -e "$root" ] && mv "$root" "$failed"
  [ -e "$backup" ] && mv "$backup" "$root"
  if [ -e "$failed" ] && [ -e "$root" ]; then move_persistent "$failed" "$root"; rm -rf "$failed"; fi
  if [ -e "$root/AnimeMachine.sh" ]; then
    ANM_BIND_ADDRESS="$bind_address" ANM_WEB_PORT="$port" nohup "$root/AnimeMachine.sh" >/dev/null 2>&1 &
  fi
  rm -rf "$(dirname "$new_root")"
  exit 1
}
[ ! -e "$backup" ] && [ ! -e "$failed" ] || exit 1
mv "$root" "$backup" || exit 1
mv "$new_root" "$root" || { mv "$backup" "$root" 2>/dev/null || true; exit 1; }
move_persistent "$backup" "$root" || rollback
expected=$(cat "$root/VERSION") || rollback
ANM_BIND_ADDRESS="$bind_address" ANM_WEB_PORT="$port" nohup "$root/AnimeMachine.sh" >/dev/null 2>&1 &
new_pid=$!
healthy=0
python_cmd=${PYTHON:-python3}
i=0
while [ "$i" -lt 120 ]; do
  if "$python_cmd" - "$port" "$expected" "$health_host" <<'PYHEALTH' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(f"http://{sys.argv[3]}:{sys.argv[1]}/api/health/live", timeout=2) as response:
    value=json.load(response)
raise SystemExit(0 if value.get("ok") is True and value.get("service")=="AnimeMachine" and value.get("version")==sys.argv[2] else 1)
PYHEALTH
  then healthy=1; break; fi
  sleep 1; i=$((i + 1))
done
[ "$healthy" -eq 1 ] || rollback
rm -rf "$backup" "$(dirname "$new_root")"
worker_dir=$(dirname "$0")
rm -rf "$worker_dir"
exit 0
'''
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)



def _safe_wheel_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError("Docker update wheel contains an unsafe path")
    return value


def _extract_docker_wheel(wheel: Path, destination: Path, expected_version: str) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(wheel) as source:
        infos = source.infolist()
        if not infos or len(infos) > 10000:
            raise ValueError("Docker update wheel is empty or unexpectedly large")
        expanded = 0
        for info in infos:
            _safe_wheel_member(info.filename)
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                raise ValueError("Docker update wheel contains a symbolic link")
            expanded += max(0, int(info.file_size))
            if expanded > 512 * 1024 * 1024:
                raise ValueError("Docker update wheel expands beyond the configured limit")
        source.extractall(destination)
    package = destination / "animemachine"
    if not (package / "__main__.py").is_file() or not (package / "web" / "static" / "index.html").is_file():
        raise ValueError("Docker update wheel is missing AnimeMachine runtime files")
    metadata_files = sorted(destination.glob("animemachine-*.dist-info/METADATA"))
    if len(metadata_files) != 1:
        raise ValueError("Docker update wheel metadata is missing or ambiguous")
    metadata_text = metadata_files[0].read_text(encoding="utf-8", errors="strict")
    name_match = re.search(r"(?mi)^Name:\s*(\S+)\s*$", metadata_text)
    version_match = re.search(r"(?mi)^Version:\s*(\S+)\s*$", metadata_text)
    if not name_match or name_match.group(1).casefold() != "animemachine":
        raise ValueError("Docker update wheel package name mismatch")
    if not version_match or version_match.group(1).strip() != expected_version:
        raise ValueError("Docker update wheel version mismatch")
    return destination


def _validate_docker_dependencies(release_root: Path) -> None:
    """Only activate an app-layer update when the base image already has its exact runtime dependencies."""
    from importlib import metadata

    metadata_files = sorted(release_root.glob("animemachine-*.dist-info/METADATA"))
    if len(metadata_files) != 1:
        raise ValueError("Docker update wheel metadata is missing")
    requirements = []
    for line in metadata_files[0].read_text(encoding="utf-8").splitlines():
        if line.casefold().startswith("requires-dist:"):
            requirements.append(line.split(":", 1)[1].strip())
    for requirement in requirements:
        base_requirement, _separator, marker = requirement.partition(";")
        if marker and "extra" in marker.casefold():
            continue
        # AnimeMachine intentionally pins runtime dependencies.  If a future release
        # stops doing so, a full image update is safer than mutating the base runtime.
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^;\s]+)", base_requirement.strip())
        if not match:
            raise ValueError("Docker base image update required for changed runtime dependencies")
        package_name, expected = match.groups()
        try:
            installed = metadata.version(package_name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(f"Docker base image is missing runtime dependency: {package_name}") from exc
        if installed != expected:
            raise ValueError(f"Docker base image update required for {package_name} {expected}")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _activate_docker_release(wheel: Path, version: str) -> None:
    update_root = _docker_update_root()
    releases = update_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    stage = update_root / f".stage-{uuid.uuid4().hex}"
    try:
        _extract_docker_wheel(wheel, stage, version)
        _validate_docker_dependencies(stage)
        target = releases / version
        if target.exists():
            shutil.rmtree(target)
        os.replace(stage, target)
        current_file = update_root / "current"
        previous = current_file.read_text(encoding="utf-8").strip() if current_file.is_file() else ""
        if previous and _version_tuple(previous) is None:
            previous = ""
        pending_file = update_root / "pending.json"
        pending = '{"version":"' + version + '","previous":"' + previous + '"}\n'
        # Publish rollback metadata before switching the active pointer.  If the
        # process is interrupted between the two writes, the previous layer stays
        # active; the reverse order could start an unverified layer without a
        # pending health check on the next container start.
        _atomic_text(pending_file, pending)
        try:
            _atomic_text(current_file, version + "\n")
        except Exception:
            pending_file.unlink(missing_ok=True)
            raise
        # Keep the current and previous application layers; older verified layers are disposable.
        keep = {version, previous}
        for candidate in releases.iterdir():
            if candidate.is_dir() and candidate.name not in keep and _version_tuple(candidate.name):
                shutil.rmtree(candidate, ignore_errors=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def apply(*, host: str, port: int) -> dict[str, Any]:
    """Stage a verified update and request a controlled restart."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("update already in progress")
    stage: Path | None = None
    download_dir: Path | None = None
    try:
        release = status(force=False)
        if not release.get("updateAvailable"):
            raise ValueError("no newer release is available")
        if not release.get("canUpdate"):
            raise ValueError(str(release.get("reason") or "online update is unavailable"))
        mode, root = _install_mode()
        asset = dict(release.get("asset") or {})
        expected_size = int(asset.get("size") or 0)
        if expected_size <= 0 or expected_size > _MAX_RELEASE_BYTES:
            raise ValueError("release asset size is invalid")
        sha256 = str(asset.get("sha256") or "")
        if not sha256:
            checksum_url = str(asset.get("checksumUrl") or "")
            if not checksum_url:
                raise ValueError("release checksum is unavailable")
            sha256 = _checksum_from_asset({"browser_download_url": checksum_url})
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("release checksum is invalid")
        latest_version = str(release["latestVersion"])
        _record_state(
            verification={"status": "pending", "version": latest_version, "sha256": sha256},
            lastUpgrade={"status": "downloading", "version": latest_version, "mode": mode, "at": _utc_now()},
        )
        download_dir = Path(tempfile.mkdtemp(prefix="animemachine-download-"))
        archive = download_dir / str(asset["name"])
        urls = _preferred_download_urls(release)
        downloaded = downloads.download_verified(urls, archive, expected_size=expected_size, expected_sha256=sha256,
                                                 segments=2, attempts_per_source=3, retry_backoff=.5)
        used_source = str((downloaded.get("urls") or [""])[0] or "")
        _record_state(
            downloadSource=used_source,
            verification={"status": "verified", "version": latest_version, "sha256": str(downloaded.get("sha256") or sha256),
                          "source": used_source, "at": _utc_now()},
        )
        if mode == "docker":
            _activate_docker_release(archive, latest_version)
            shutil.rmtree(download_dir, ignore_errors=True)
            download_dir = None
            _record_state(lastUpgrade={"status": "restarting", "version": latest_version, "mode": "docker", "at": _utc_now()})
            return {"accepted": True, "restarting": True, "version": latest_version, "mode": "docker"}
        if mode != "portable" or root is None:
            raise ValueError("portable release required")
        stage = root.parent / f".animemachine-update-{uuid.uuid4().hex}"
        new_root = _extract_release(archive, stage, latest_version)
        worker_dir = Path(tempfile.mkdtemp(prefix="animemachine-updater-"))
        if os.name == "nt":
            worker = worker_dir / "update.ps1"
            _write_windows_worker(worker, root=root, new_root=new_root, old_pid=os.getpid(), host=host, port=port)
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(worker),
                 "-Root", str(root), "-NewRoot", str(new_root), "-OldPid", str(os.getpid()),
                 "-BindAddress", host, "-HealthHost", _health_probe_host(host), "-Port", str(port)],
                cwd=worker_dir, close_fds=True, creationflags=creationflags,
            )
        else:
            worker = worker_dir / "update.sh"
            _write_unix_worker(worker, root=root, new_root=new_root, old_pid=os.getpid(), host=host, port=port)
            subprocess.Popen(
                ["/bin/sh", str(worker), str(root), str(new_root), str(os.getpid()), host, str(port), _health_probe_host(host)],
                cwd=worker_dir, close_fds=True, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        shutil.rmtree(download_dir, ignore_errors=True)
        download_dir = None
        _record_state(lastUpgrade={"status": "restarting", "version": latest_version, "mode": "portable", "at": _utc_now()})
        return {"accepted": True, "restarting": True, "version": latest_version, "mode": "portable"}
    except Exception as exc:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        state = _read_state()
        verification = dict(state.get("verification") or {})
        if "digest" in str(exc).casefold() or "sha256" in str(exc).casefold():
            verification.update({"status": "failed", "at": _utc_now()})
        _record_state(verification=verification,
                      lastUpgrade={"status": "failed", "version": str((locals().get("release") or {}).get("latestVersion") or ""),
                                   "mode": str((locals().get("mode") or "")), "at": _utc_now(), "message": type(exc).__name__})
        raise
    finally:
        if download_dir is not None:
            shutil.rmtree(download_dir, ignore_errors=True)
        _UPDATE_LOCK.release()
