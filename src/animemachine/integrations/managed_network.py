"""Automatic source selection for the Ani-RSS owned by a Compose deployment."""
from __future__ import annotations

import concurrent.futures
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

from ..network import connectivity, registry, sources, transport
from ..torrents.metainfo import inspect_bytes

_DEFAULTS = {"mikanHost": "https://mikanani.me", "bgmApi": "https://api.bgm.tv"}
_SERVICES = {"mikanHost": "mikan", "bgmApi": "bangumi_api"}
_LOCK = threading.Lock()
_STATUS: dict = {"state": "idle"}
_RECHECK = threading.Event()


def _root() -> Path:
    return Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "integrations"


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == data:
        return
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
    temporary.replace(path)


def _read() -> dict:
    try:
        value = json.loads((_root() / "ani-rss-network.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        for key in ("managed", "selected", "recovery", "pending", "public"):
            if not isinstance(value.get(key, {}), dict):
                value[key] = {}
        check = value.get("nextCheck", 0)
        if not isinstance(check, (float, int)) or not math.isfinite(check):
            value["nextCheck"] = 0
        return value
    except (OSError, ValueError):
        return {}


def status() -> dict:
    return dict(_STATUS)


def _request_failed_recently(endpoint: registry.Endpoint) -> bool:
    try:
        row = sources._health().snapshot(endpoint.id, 'ani-rss-mikan-request',
                                         network_id=transport.network_environment_id())
        return bool(row and 0 <= time.time() - float(row.get('lastFailureAt') or 0) < 300)
    except (OSError, sqlite3.Error):
        return False


def request_failed(endpoint: str, key: str) -> None:
    """Actual outbound timeouts invalidate a successful probe, without editing user services."""
    if not owned(endpoint):
        return
    state = _read()
    identity = hashlib.sha256((endpoint + '\0' + key).encode()).hexdigest()
    if state.get('identity') != identity:
        return
    selected = state.get('selected', {}).get('mikanHost')
    source = next((item for item in registry.for_service('mikan') if item.base_url == selected), None)
    if source is None or 'mikanHost' in state.get('public', {}).get('manual', []):
        return
    sources._health().failure(source.id, 'ani-rss-mikan-request', 'upstream_timeout',
                              network_id=transport.network_environment_id())
    _RECHECK.set()


@contextlib.contextmanager
def _lease(lock):
    acquired = lock is None or lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired and lock is not None:
            lock.release()


def owned(endpoint: str) -> bool:
    return (os.getenv("ANM_MANAGED_ANI_RSS", "false").casefold() == "true"
            and endpoint.rstrip("/") == "http://ani-rss:7789")


def _metadata_url() -> str:
    path = _root() / "ani-rss-metadata-key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32))
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise ValueError("invalid managed metadata key")
    return f"http://animemachine:8787/api/managed-bangumi/{token}"


def metadata_request(token: str, path: str, query: str) -> dict | list:
    """Read public metadata only; never forward incoming headers or user tokens."""
    key_file = _root() / "ani-rss-metadata-key"
    if not os.getenv("ANM_MANAGED_ANI_RSS", "false").casefold() == "true" or not key_file.is_file():
        raise PermissionError("managed metadata unavailable")
    if not hmac.compare_digest(token, key_file.read_text(encoding="utf-8").strip()):
        raise PermissionError("managed metadata unavailable")
    search = bool(re.fullmatch(r"/search/subject/[^/?#]{1,500}", path))
    if not search and not re.fullmatch(r"/v0/(?:subjects/\d+(?:/(?:persons|characters))?|episodes(?:/\d+)?|persons/\d+|characters/\d+)", path):
        raise ValueError("unsupported public metadata path")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    if set(params) - {"subject_id", "type", "limit", "offset", "max_results", "start", "responseGroup"} or len(query) > 200:
        raise ValueError("unsupported public metadata query")
    if any(not re.fullmatch(r"small|medium|large" if key == "responseGroup" else r"\d{1,10}", value)
           for key, values in params.items() for value in values):
        raise ValueError("invalid public metadata query")
    candidates = registry.for_service("bangumi_api")
    selected = _read().get("selected", {}).get("bgmApi")
    candidates.sort(key=lambda item: (item.base_url != selected, item.trust != "official"))
    def validate(value):
        if not isinstance(value, (dict, list)):
            raise ValueError("invalid metadata payload")
        subject = re.fullmatch(r"/v0/subjects/(\d+)", path)
        if subject and (not isinstance(value, dict) or str(value.get("id")) != subject[1] or not value.get("name")):
            raise ValueError("metadata identity mismatch")
        return value
    result, _ = sources.fetch_json(
        [item.base_url + path + ("?" + query if query else "") for item in candidates],
        headers={"User-Agent": "AnimeMachine managed Ani-RSS metadata"}, timeout=5,
        validator=validate, service="bangumi_api", capability="managed-metadata", honor_cooldown=False)
    return result


def _proxy(config: dict, url: str) -> str:
    if not config.get("proxy"):
        return ""
    host = urllib.parse.urlsplit(url).hostname or ""
    domains = str(config.get("proxyList") or "").splitlines()
    if not any(host == value.strip() or host.endswith("." + value.strip()) for value in domains if value.strip()):
        return ""
    address = str(config.get("proxyHost") or "").strip()
    if "://" in address:
        address = urllib.parse.urlsplit(address).hostname or ""
    if not address or not config.get("proxyPort"):
        return ""
    if ":" in address:
        address = "[" + address + "]"
    auth = ""
    if config.get("proxyUsername"):
        auth = urllib.parse.quote(str(config["proxyUsername"]), safe="") + ":" + urllib.parse.quote(str(config.get("proxyPassword") or ""), safe="") + "@"
    return f"http://{auth}{address}:{int(config['proxyPort'])}"


def probe(endpoint: registry.Endpoint, config: dict, subject_id: int) -> bool:
    """Exercise the same proxy rules as Ani-RSS, validating actual public content."""
    start = time.monotonic()
    capability = "ani-rss-" + endpoint.service
    route = "environment_proxy" if _proxy(config, endpoint.base_url) else "direct"
    environment = transport.network_environment_id()
    store = sources._health()
    deadline = start + (40 if endpoint.service == 'mikan' else 16)
    def get(url: str, limit: int = 8 * 1024 * 1024):
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('source probe deadline exceeded')
            try:
                response = transport.request("GET", url, headers={"User-Agent": "ani-rss connectivity via AnimeMachine"},
                                             timeout=min(8, remaining), deadline=deadline,
                                             max_bytes=limit, proxy_url=_proxy(config, url))
                break
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt:
                    raise
        allowed = {urllib.parse.urlsplit(item.base_url).hostname for item in registry.for_service(endpoint.service)}
        if response.url.host not in allowed:
            raise ValueError("unexpected source redirect")
        return response
    try:
        with connectivity.recovery_probe():
            if endpoint.service == "bangumi_api":
                data = get(endpoint.base_url + f"/v0/subjects/{subject_id}", 2 * 1024 * 1024).json()
                if not isinstance(data, dict) or data.get("id") != subject_id or not data.get("name"):
                    raise ValueError("metadata identity mismatch")
            else:
                page = html.unescape(get(endpoint.base_url + "/Home").text)
                # Home starts with long-running titles whose full RSS can be
                # several MiB. Probe newer public titles instead of mistaking
                # the bounded download limit for a dead source.
                matches = sorted(set(re.findall(r"/Home/Bangumi/(\d+)", page, re.I)), key=int, reverse=True)[:3]
                if not matches or not re.search(r"(?:蜜柑|Mikan)", page, re.I):
                    raise ValueError("source did not return a Mikan page")
                enclosure = None
                for bangumi_id in matches:
                    rss = get(endpoint.base_url + "/RSS/Bangumi?bangumiId=" + bangumi_id, 2 * 1024 * 1024)
                    tree = ET.fromstring(rss.content)
                    if tree.tag != "rss":
                        raise ValueError("source did not return RSS")
                    enclosure = tree.find("./channel/item/enclosure")
                    if enclosure is not None:
                        break
                if enclosure is None:
                    raise ValueError("source did not return an episode feed")
                location = urllib.parse.urlsplit(enclosure.attrib.get("url", ""))
                trusted_hosts = {urllib.parse.urlsplit(item.base_url).hostname for item in registry.for_service("mikan")}
                if (location.hostname not in trusted_hosts or location.query or not location.path.startswith("/Download/")
                        or not location.path.endswith(".torrent")):
                    raise ValueError("untrusted episode metadata URL")
                raw = get(endpoint.base_url + location.path, 2 * 1024 * 1024).content
                inspect_bytes(raw, filename="source-health.torrent")
        store.success(endpoint.id, capability, time.monotonic() - start, route_mode=route, network_id=environment)
        return True
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, ET.ParseError):
        store.failure(endpoint.id, capability, "source_unavailable", route_mode=route, network_id=environment)
        return False


def _select(field: str, config: dict, subject_id: int, current: str, state: dict) -> str | None:
    endpoints = registry.for_service(_SERVICES[field])
    primary = next(item for item in endpoints if item.base_url == _DEFAULTS[field])
    failed = {item.id for item in endpoints if field == 'mikanHost' and _request_failed_recently(item)}
    # Require consecutive failures before leaving the official source, and two
    # healthy periodic checks before returning from an established fallback.
    first = primary.id not in failed and probe(primary, config, subject_id)
    if first:
        count = int(state.setdefault("recovery", {}).get(field, 0)) + 1
        state["recovery"][field] = count
        if current == primary.base_url or not current or count >= 2:
            return primary.base_url
        chosen = next((item for item in endpoints if item.base_url == current), None)
        return current if chosen and probe(chosen, config, subject_id) else primary.base_url
    state.setdefault("recovery", {})[field] = 0
    if primary.id not in failed and current == primary.base_url and probe(primary, config, subject_id):
        return primary.base_url
    alternatives = [item for item in endpoints if item.id != primary.id and item.id not in failed]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="anm-managed-route") as pool:
        results = list(zip(alternatives, pool.map(lambda item: probe(item, config, subject_id), alternatives)))
    healthy = [item for item, good in results if good]
    if not healthy:
        return None
    healthy.sort(key=lambda item: (item.trust != "official", item.base_url != current))
    return healthy[0].base_url


def _public_feed(url: str, target: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    hosts = {urllib.parse.urlsplit(item.base_url).hostname for item in registry.for_service("mikan")}
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in hosts or parsed.username or parsed.password
            or port not in {None, 80, 443} or parsed.path != "/RSS/Bangumi" or parsed.fragment
            or not values.get("bangumiId") or set(values) - {"bangumiId", "subgroupid"}
            or any(len(items) != 1 or not items[0].isdigit() for items in values.values())):
        return None
    return target + parsed.path + "?" + parsed.query


def _migrate_feeds(client, target: str, *, operation_lock=None, cancelled=lambda: False) -> tuple[int, bool]:
    changed = 0
    for item in client.subscriptions():
        if not isinstance(item, dict) or not item.get("id"):
            continue
        old_urls = [str(item.get("url") or "")]
        standby = item.get("standbyRssList")
        old_urls += [str(row.get("url") or "") for row in standby if isinstance(row, dict)] if isinstance(standby, list) else []
        if not any((replacement := _public_feed(url, target)) and replacement != url for url in old_urls):
            continue
        if cancelled() or changed >= 10:
            return changed, False
        # Re-read under the action lock, so a subscription changed or removed
        # during the initial snapshot cannot be resurrected or overwritten.
        with _lease(operation_lock) as acquired:
            if not acquired or cancelled():
                return changed, False
            fresh = next((row for row in client.subscriptions() if row.get("id") == item.get("id")), None)
            if fresh is None:
                continue
            update = copy.deepcopy(fresh)
            url = _public_feed(str(fresh.get("url") or ""), target)
            if url:
                update["url"] = url
            standby = update.get("standbyRssList")
            for row in standby if isinstance(standby, list) else []:
                if isinstance(row, dict):
                    replacement = _public_feed(str(row.get("url") or ""), target)
                    if replacement:
                        row["url"] = replacement
            if update != fresh:
                client.call("setAni", body=update, timeout=8)
                changed += 1
    return changed, True


def tick(client, *, subject_id: int = 300649, operation_lock=None, cancelled=lambda: False) -> dict:
    """A restart-safe bounded pass. Call only from an optional background worker."""
    if not owned(client.endpoint):
        return {"state": "unmanaged"}
    if not _LOCK.acquire(blocking=False):
        return {**status(), "changed": False}
    try:
        state = _read()
        identity = hashlib.sha256((client.endpoint + "\0" + client.key).encode()).hexdigest()
        if state.get("identity") != identity:
            state = {"identity": identity, "managed": {}, "selected": {}, "recovery": {}}
        now = time.time()
        selected_source = next((item for item in registry.for_service('mikan')
                                if item.base_url == state.get('selected', {}).get('mikanHost')), None)
        recheck = _RECHECK.is_set() or bool(selected_source and _request_failed_recently(selected_source))
        if not recheck and 0 <= state.get("nextCheck", 0) - now <= 1800:
            _STATUS.clear()
            _STATUS.update(state.get("public", {"state": "idle"}), changed=False)
            return status()
        _RECHECK.clear()
        _STATUS.update(state="checking")
        config = client.call("config", timeout=8)
        if not isinstance(config, dict):
            raise ValueError("unsupported Ani-RSS configuration")
        patch = {}; selected = dict(state.get("selected", {})); managed = dict(state.get("managed", {}))
        manual = []
        healthy = set()
        for field in _DEFAULTS:
            current = str(config.get(field) or _DEFAULTS[field]).rstrip("/")
            prior = managed.get(field, _DEFAULTS[field])
            pending = state.get("pending", {}).get(field)
            if current == pending:
                prior = current
                managed[field] = current
            if current != prior:
                manual.append(field)
                selected.pop(field, None)
                continue
            candidate = (_DEFAULTS[field] if field == "bgmApi" and config.get("bgmToken") else
                         _select(field, config, subject_id, selected.get(field, _DEFAULTS[field]), state))
            if candidate:
                healthy.add(field)
                selected[field] = candidate
                # Community APIs receive no Ani-RSS/Bangumi credentials. The
                # owned read-only adapter strips headers before fetching data.
                desired = (_metadata_url() if field == "bgmApi" and candidate != _DEFAULTS[field] else candidate)
                if field == "bgmApi" and config.get("bgmToken"):
                    desired = _DEFAULTS[field]
                if current != desired:
                    patch[field] = desired
        with _lease(operation_lock) as acquired:
            if not acquired or cancelled():
                return {"state": "retrying", "changed": False}
            fresh = client.call("config", timeout=8)
            sensitive = (*_DEFAULTS, "bgmToken", "proxy", "proxyList", "proxyHost", "proxyPort", "proxyUsername", "proxyPassword")
            if any(fresh.get(field) != config.get(field) for field in sensitive):
                state["nextCheck"] = now + 30
                _save(_root() / "ani-rss-network.json", state)
                _STATUS.update(state="retrying")
                return status()
            if patch:
                state.update(pending=patch, managed=managed, selected=selected)
                _save(_root() / "ani-rss-network.json", state)
                client.call("setConfig", body=patch, timeout=8)
            managed.update({field: patch.get(field, str(config.get(field) or _DEFAULTS[field])) for field in _DEFAULTS if field not in manual})
            state.update(managed=managed, selected=selected, pending={})
            _save(_root() / "ani-rss-network.json", state)
        migrations, complete = (_migrate_feeds(client, selected["mikanHost"], operation_lock=operation_lock, cancelled=cancelled)
                                if "mikanHost" in selected and "mikanHost" not in manual else (0, True))
        ready = len(healthy | set(manual)) == 2 and complete
        state.update(nextCheck=now + (900 if ready and complete and not _RECHECK.is_set() else 30),
                     updatedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        public = {"state": "ready" if ready else "retrying", "manual": manual,
                  "sources": {field: value for field, value in selected.items() if field not in manual}}
        state["public"] = public
        _save(_root() / "ani-rss-network.json", state)
        _STATUS.clear()
        _STATUS.update(public, changed=bool(patch or migrations), migratedFeeds=migrations)
        return status()
    except (OSError, ValueError, RuntimeError, httpx.HTTPError):
        _STATUS.update(state="retrying", changed=False)
        return status()
    finally:
        _LOCK.release()
