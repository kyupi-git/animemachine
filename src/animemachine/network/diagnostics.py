"""Read-only network diagnostics backed by the same adaptive health store as production requests."""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from . import connectivity, registry, sources, transport
from .health import Store



_CONNECTIVITY_CANARIES = (
    {"service": "internet_canary", "capability": "binary", "baseUrl": "http://www.msftconnecttest.com",
     "probeUrl": "http://www.msftconnecttest.com/connecttest.txt", "expectedBody": "Microsoft Connect Test"},
    {"service": "internet_canary", "capability": "binary", "baseUrl": "https://www.baidu.com",
     "probeUrl": "https://www.baidu.com/"},
)

_SERVICE_LABELS = {
    "archive_descriptor": "Bangumi Archive",
    "bangumi_subject_cache": "Bangumi subject cache",
    "bangumi_api": "Bangumi API",
    "bangumi_image": "Bangumi image",
}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip().rstrip("/") for value in values if str(value).strip()))


def _sample(db_path: Path) -> tuple[int | None, str]:
    bgm_id: int | None = None
    image_url = ""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            row = db.execute("SELECT bgm_id FROM anime_work WHERE bgm_id>0 ORDER BY start_month DESC,id DESC LIMIT 1").fetchone()
            if row:
                bgm_id = int(row[0])
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_image'").fetchone():
                row = db.execute(
                    "SELECT source_url FROM anime_image WHERE source_url IS NOT NULL AND source_url<>'' "
                    "AND image_blob IS NOT NULL ORDER BY fetched_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    image_url = str(row[0] or "")
    except (OSError, sqlite3.Error, ValueError):
        pass
    return bgm_id, image_url


def _configured_endpoints(config: dict[str, Any], db_path: Path) -> tuple[list[dict[str, str]], str]:
    network = config.get("metadata", {}).get("network", {})
    bgm_id, image_url = _sample(db_path)
    values: list[dict[str, str]] = []

    archive_urls = _unique([
        *(network.get("archiveManifestEndpoints") or []),
        *(item.base_url for item in registry.for_service("archive_descriptor")),
    ])
    for url in archive_urls:
        values.append({"service": "archive_descriptor", "capability": "json", "baseUrl": url, "probeUrl": url})

    if bgm_id is not None:
        bucket = str(bgm_id)[0]
        caches = _unique([
            *(network.get("bangumiSubjectCacheEndpoints") or []),
            *(item.base_url for item in registry.for_service("bangumi_subject_cache")),
        ])
        for base in caches:
            values.append({"service": "bangumi_subject_cache", "capability": "json", "baseUrl": base,
                           "probeUrl": f"{base}/{bucket}/{bgm_id}.json"})
        apis = _unique([
            *(network.get("bangumiApiEndpoints") or []),
            *(item.base_url for item in registry.for_service("bangumi_api")),
        ])
        for base in apis:
            values.append({"service": "bangumi_api", "capability": "json", "baseUrl": base,
                           "probeUrl": f"{base}/v0/subjects/{bgm_id}"})
    else:
        for base in _unique([*(network.get("bangumiApiEndpoints") or []),
                             *(item.base_url for item in registry.for_service("bangumi_api"))]):
            values.append({"service": "bangumi_api", "capability": "json", "baseUrl": base, "probeUrl": base})

    image_bases = _unique([
        *(network.get("bangumiImageEndpoints") or []),
        *(item.base_url for item in registry.for_service("bangumi_image")),
    ])
    image_path = urllib.parse.urlparse(image_url).path if image_url else ""
    for base in image_bases:
        values.append({"service": "bangumi_image", "capability": "binary", "baseUrl": base,
                       "probeUrl": f"{base}{image_path}" if image_path else base})
    return values, image_url


def _health_id(url: str, capability: str) -> str:
    return sources._endpoints([url], capability)[0].id


def _safe_failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return type(exc).__name__
    return type(exc).__name__


def _cover_url(payload: Any) -> str:
    queue: list[Any] = [payload]
    while queue:
        value = queue.pop(0)
        if not isinstance(value, dict):
            continue
        images = value.get("images")
        if isinstance(images, dict):
            for key in ("medium", "common", "large", "small"):
                candidate = str(images.get(key) or "").strip()
                if candidate and "no_icon" not in candidate:
                    return candidate
        for key in ("data", "subject", "result"):
            if isinstance(value.get(key), dict):
                queue.append(value[key])
    return ""


def _probe_json(item: dict[str, str], store: Store, timeout: float) -> tuple[dict[str, Any], str]:
    url = item["probeUrl"]
    endpoint_id = _health_id(url, "json")
    started = time.monotonic()
    cover = ""
    profile = transport.network_profile(url)
    route_mode = str(profile["routeMode"])
    network_id = str(profile["id"])
    try:
        response = transport.request("GET", url, headers={"Accept": "application/json"}, timeout=timeout,
                                     max_bytes=4 * 1024 * 1024)
        payload = json.loads(response.content.decode("utf-8-sig"))
        elapsed = max(.001, time.monotonic() - started)
        actual_route = dict(response.extensions.get("animemachine_proxy_route") or {})
        route_mode = str(actual_route.get("mode") or route_mode)
        active_profile = transport.network_profile(url, route_mode)
        store.success(endpoint_id, "json", elapsed, len(response.content), route_mode=route_mode,
                      network_id=str(active_profile.get("id") or network_id))
        cover = _cover_url(payload)
    except Exception as exc:
        failed_profile = transport.network_profile(url)
        store.failure(endpoint_id, "json", _safe_failure(exc), route_mode=str(failed_profile["routeMode"]),
                      network_id=str(failed_profile.get("id") or network_id))
    return _render_item(item, store), cover


def _probe_image(item: dict[str, str], store: Store, timeout: float) -> dict[str, Any]:
    url = item["probeUrl"]
    endpoint_id = _health_id(url, "binary")
    started = time.monotonic()
    profile = transport.network_profile(url)
    route_mode = str(profile["routeMode"])
    network_id = str(profile["id"])
    try:
        response = transport.request(
            "GET", url, headers={"Accept": "image/avif,image/webp,image/jpeg,image/png"}, timeout=timeout,
            max_bytes=12 * 1024 * 1024,
        )
        mime = str(response.headers.get("content-type") or "").split(";", 1)[0].casefold()
        if not response.content or not mime.startswith("image/"):
            raise ValueError("invalid_image_response")
        elapsed = max(.001, time.monotonic() - started)
        actual_route = dict(response.extensions.get("animemachine_proxy_route") or {})
        route_mode = str(actual_route.get("mode") or route_mode)
        active_profile = transport.network_profile(url, route_mode)
        store.success(endpoint_id, "binary", elapsed, len(response.content), route_mode=route_mode,
                      network_id=str(active_profile.get("id") or network_id))
    except Exception as exc:
        failed_profile = transport.network_profile(url)
        store.failure(endpoint_id, "binary", _safe_failure(exc), route_mode=str(failed_profile["routeMode"]),
                      network_id=str(failed_profile.get("id") or network_id))
    return _render_item(item, store)



def _light_probe(item: dict[str, str], store: Store | None, timeout: float) -> bool:
    """Touch a real service URL with a bounded partial GET to warm route/source state cheaply."""
    url = item["probeUrl"]
    capability = item["capability"]
    endpoint_id = _health_id(url, capability)
    started = time.monotonic()
    profile = transport.network_profile(url)
    route_mode = str(profile["routeMode"])
    network_id = str(profile["id"])
    try:
        headers = {"Accept": "application/json" if capability == "json" else "image/*,*/*;q=0.1"}
        with transport.stream("GET", url, headers=headers, timeout=timeout) as response:
            status = int(response.status_code)
            body_sample = b""
            for chunk in response.iter_bytes():
                body_sample = bytes(chunk[:1024])
                break
            byte_count = len(body_sample)
            actual_route = dict(response.extensions.get("animemachine_proxy_route") or {})
        route_mode = str(actual_route.get("mode") or route_mode)
        active_profile = transport.network_profile(url, route_mode)
        network_id = str(active_profile.get("id") or network_id)
        elapsed = max(.001, time.monotonic() - started)
        expected_body = str(item.get("expectedBody") or "")
        body_matches = not expected_body or body_sample.decode("utf-8", errors="replace").strip() == expected_body
        ok = status < 500 and status != 407 and body_matches
        if store is not None:
            if 200 <= status < 400 and body_matches:
                store.success(endpoint_id, capability, elapsed, byte_count, route_mode=route_mode, network_id=network_id)
            elif status >= 500:
                store.failure(endpoint_id, capability, f"HTTP {status}", route_mode=route_mode, network_id=network_id)
        return ok
    except Exception as exc:
        if store is not None and not connectivity.is_offline():
            failed_profile = transport.network_profile(url)
            store.failure(endpoint_id, capability, _safe_failure(exc), route_mode=str(failed_profile["routeMode"]),
                          network_id=str(failed_profile.get("id") or network_id))
        return False


def _recovery_light_probe(item: dict[str, str], timeout: float) -> bool:
    """Run one connectivity probe with recovery permission in the actual worker thread."""
    with connectivity.recovery_probe():
        return _light_probe(item, None, timeout)


def internet_canary_probe(*, timeout: float = 2.5) -> bool:
    """Prove that the wider Internet is still reachable when all AnimeMachine upstreams fail."""
    selected = [dict(item) for item in _CONNECTIVITY_CANARIES]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(selected), thread_name_prefix="anm-internet-canary") as pool:
        return any(pool.map(lambda item: _recovery_light_probe(item, timeout), selected))


def connectivity_probe(db_path: Path, config: dict[str, Any], *, timeout: float = 2.5) -> bool:
    """Check independent services, neutral canaries, then configured fallbacks without learning failures."""
    connectivity.note_environment(transport.network_environment_id())
    items, _sample_url = _configured_endpoints(config, db_path)
    selected: list[dict[str, str]] = []
    hosts: set[str] = set()
    services: set[str] = set()
    # First take one independent host per service. This makes a failed mirror unable
    # to masquerade as a whole-network outage while keeping the routine probe cheap.
    for item in items:
        host = urllib.parse.urlparse(item["probeUrl"]).netloc.casefold()
        service = item["service"]
        if not host or host in hosts or service in services:
            continue
        selected.append(item)
        hosts.add(host)
        services.add(service)
        if len(selected) >= 3:
            break
    # Sparse configurations may expose fewer than three services; fill with other hosts.
    if len(selected) < 3:
        for item in items:
            host = urllib.parse.urlparse(item["probeUrl"]).netloc.casefold()
            if not host or host in hosts:
                continue
            selected.append(item)
            hosts.add(host)
            if len(selected) >= 3:
                break
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(selected), thread_name_prefix="anm-connectivity"
        ) as pool:
            if any(pool.map(lambda item: _recovery_light_probe(item, timeout), selected)):
                return True
    if internet_canary_probe(timeout=timeout):
        return True

    # Some managed/filtered networks block generic connectivity-check hosts. Before
    # confirming a whole-network outage, try alternate configured mirrors that were
    # intentionally omitted from the cheap primary round. This only runs after both
    # the primary service set and neutral canaries failed.
    fallback: list[dict[str, str]] = []
    fallback_services: set[str] = set()
    for item in items:
        host = urllib.parse.urlparse(item["probeUrl"]).netloc.casefold()
        service = item["service"]
        if not host or host in hosts or service in fallback_services:
            continue
        fallback.append(item)
        hosts.add(host)
        fallback_services.add(service)
        if len(fallback) >= 4:
            break
    if not fallback:
        return False
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(fallback), thread_name_prefix="anm-connectivity-fallback"
    ) as pool:
        return any(pool.map(lambda item: _recovery_light_probe(item, timeout), fallback))


def _prewarm_candidates(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep warmup cheap: primary plus one fallback per common service, at most eight hosts."""
    selected: list[dict[str, str]] = []
    per_service: dict[str, int] = {}
    hosts: set[str] = set()
    for item in items:
        service = item["service"]
        host = urllib.parse.urlparse(item["probeUrl"]).netloc.casefold()
        if not host or host in hosts or per_service.get(service, 0) >= 2:
            continue
        selected.append(item)
        hosts.add(host)
        per_service[service] = per_service.get(service, 0) + 1
        if len(selected) >= 8:
            break
    return selected


def prewarm(db_path: Path, config: dict[str, Any], *, timeout: float = 2.5) -> dict[str, Any]:
    """Low-cost route/source warmup for common services and mirrors."""
    if connectivity.is_offline():
        return {"skipped": True, "reason": "offline", "attempted": 0, "succeeded": 0}
    items, _sample_url = _configured_endpoints(config, db_path)
    items = _prewarm_candidates(items)
    if not items:
        return {"skipped": True, "reason": "no_endpoints", "attempted": 0, "succeeded": 0}
    store = Store()
    workers = min(4, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="anm-net-prewarm") as pool:
        results = list(pool.map(lambda item: _light_probe(item, store, timeout), items))
    return {"skipped": False, "attempted": len(items), "succeeded": sum(bool(value) for value in results)}

def _render_item(item: dict[str, str], store: Store) -> dict[str, Any]:
    capability = item["capability"]
    endpoint_id = _health_id(item["probeUrl"], capability)
    route = transport.proxy_route(item["probeUrl"])
    profile = transport.network_profile(item["probeUrl"])
    network_id = str(profile["id"])
    modes = ("direct", "environment_proxy", "windows_system_proxy", "system_proxy")
    route_profiles = {mode: transport.network_profile(item["probeUrl"], mode) for mode in modes}
    payload = {
        "service": item["service"],
        "label": _SERVICE_LABELS.get(item["service"], item["service"]),
        "baseUrl": item["baseUrl"],
        "route": route,
        "networkProfile": profile,
        **store.snapshot(endpoint_id, capability, str(route.get("mode") or "direct"), network_id),
        "routeHealth": {
            mode: store.snapshot(endpoint_id, capability, mode, str(route_profiles[mode]["id"])) for mode in modes
        },
    }
    if item["service"] == "bangumi_image":
        payload["trends"] = {
            mode: store.trend(endpoint_id, capability, network_id=str(route_profiles[mode]["id"])).get(mode, [])
            for mode in modes
        }
    return payload


def _annotate_selection(items: list[dict[str, str]], rendered: list[dict[str, Any]], store: Store) -> None:
    for service in dict.fromkeys(item["service"] for item in items):
        group = [item for item in items if item["service"] == service]
        if len(group) < 2:
            continue
        capability = group[0]["capability"]
        candidates = []
        for item in group:
            profile = transport.network_profile(item["probeUrl"])
            candidates.append((_health_id(item["probeUrl"], capability), str(profile["routeMode"]), str(profile["id"])))
        ordered, explanation = store.rank(candidates, capability, persist=False)
        rank_by_id = {endpoint_id: index + 1 for index, endpoint_id in enumerate(ordered)}
        evaluations = {str(value.get("endpointId") or ""): value for value in explanation.get("candidates", [])}
        for raw, payload in zip(items, rendered):
            if raw["service"] != service:
                continue
            endpoint_id = _health_id(raw["probeUrl"], capability)
            evaluation = dict(evaluations.get(endpoint_id) or {})
            payload["selection"] = {
                "selected": endpoint_id == explanation.get("selectedEndpointId"),
                "rank": int(rank_by_id.get(endpoint_id, 0)),
                "reason": str(explanation.get("reason") or ""),
                "quality": evaluation.get("quality"),
                "confidence": evaluation.get("confidence"),
                "effectiveSamples": evaluation.get("effectiveSamples"),
                "advantage": explanation.get("advantage"),
                "switchThreshold": explanation.get("switchThreshold"),
                "holdRemainingSeconds": explanation.get("holdRemainingSeconds"),
            }


def snapshot(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    items, _sample_url = _configured_endpoints(config, db_path)
    store = Store()
    rendered = [_render_item(item, store) for item in items]
    _annotate_selection(items, rendered, store)
    route_target = next((item["probeUrl"] for item in items if item["service"] == "bangumi_api"),
                        "https://api.bgm.tv")
    return {
        "route": transport.proxy_route(route_target),
        "routeCandidates": transport.route_candidates(route_target),
        "networkProfile": transport.network_profile(route_target),
        "proxyRevision": transport.proxy_revision(),
        "connectivity": connectivity.snapshot(),
        "items": rendered,
        "checkedAt": max((float(item.get("lastUpdated") or 0) for item in rendered), default=0),
    }


def recheck(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    items, cached_image_url = _configured_endpoints(config, db_path)
    store = Store()
    network = config.get("metadata", {}).get("network", {})
    timeout = max(1.0, min(12.0, float(network.get("probeTimeoutSeconds", 8))))
    json_items = [item for item in items if item["capability"] == "json"]
    image_items = [item for item in items if item["capability"] == "binary"]
    covers: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(json_items) or 1),
                                                thread_name_prefix="anm-net-diag") as pool:
        for rendered, cover in pool.map(lambda value: _probe_json(value, store, timeout), json_items):
            del rendered
            if cover:
                covers.append(cover)
    sample_image = covers[0] if covers else cached_image_url
    if sample_image:
        parsed = urllib.parse.urlparse(sample_image)
        for item in image_items:
            base = urllib.parse.urlparse(item["baseUrl"])
            item["probeUrl"] = urllib.parse.urlunparse((base.scheme, base.netloc, parsed.path,
                                                        parsed.params, parsed.query, parsed.fragment))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(image_items) or 1),
                                                thread_name_prefix="anm-image-diag") as pool:
        list(pool.map(lambda value: _probe_image(value, store, timeout), image_items))
    return snapshot(db_path, config)
