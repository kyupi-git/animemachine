#!/usr/bin/env python3
"""Optional Ani-RSS provider adapter.

AnimeMachine never exposes the remote credential to the browser or stores it in
SQLite.  The adapter keeps remote subscriptions, search results and actions as
an orthogonal runtime overlay; it does not overwrite the local library state.
"""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
from ..network import tls as tls_support
from ..network import transport as network_transport
from ..network import validators as network_validators
from ..torrents import runtime as runtime_catalog
from .. import __version__

from ..config.loader import (archive_group_enabled, canonical_resolution, option_enabled,
                        region_policy_enabled, resource_group_enabled, serial_group_matches, serial_profile_language,
                        serial_rule_enabled, source_family, torrent_policy_eligible)


MODES = {"prefer": "prefer", "fallback": "fallback", "manual": "manual"}
COLLECTION = re.compile(r"(?i)(?:\b(?:batch|complete|collection)\b|合集|全集|全卷|全话|全話|一括)")
EPISODE = re.compile(r"(?i)(?:\b(?:ep(?:isode)?|e)[ ._-]*(\d{1,4})\b|第\s*(\d{1,4})\s*[话話集])")
RESOLUTION = re.compile(r"(?i)(2160|1080|720|576|540|480)[pi]")
SOURCE_CLASS = re.compile(r"(?i)\b(BD(?:Rip)?|DVD(?:Rip)?|WEB[ ._-]?(?:DL|Rip)|HDTV[ ._-]?Rip|TV[ ._-]?Rip)\b")

_BACKGROUND_RESOURCE_SCAN_LOCK = threading.Lock()
_COVER_FAILURE_LOCK = threading.Lock()
_COVER_FAILURES: dict[tuple[str, str], tuple[int | None, float]] = {}
_COVER_FAILURE_COOLDOWN_SECONDS = 30.0


@contextlib.contextmanager
def background_resource_scan_lease():
    """Allow only one automatic Ani-RSS resource-discovery pass at a time."""
    acquired = _BACKGROUND_RESOURCE_SCAN_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _BACKGROUND_RESOURCE_SCAN_LOCK.release()


SCHEMA = """
CREATE TABLE IF NOT EXISTS ani_rss_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),endpoint TEXT NOT NULL,version TEXT,
  connection_state TEXT NOT NULL,configured_mode TEXT NOT NULL,effective_mode TEXT NOT NULL,
  last_attempt_at TEXT,last_success_at TEXT,last_error TEXT,successful_generation INTEGER NOT NULL DEFAULT 0,
  credential_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS ani_rss_subscription(
  remote_id TEXT PRIMARY KEY,anime_id INTEGER,title TEXT NOT NULL,bgm_id INTEGER,
  enabled INTEGER NOT NULL,subscription_kind TEXT NOT NULL,current_episode INTEGER,total_episode INTEGER,
  remote_media_path TEXT,remote_state TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
  missed_successful_syncs INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ani_rss_subscription_anime ON ani_rss_subscription(anime_id,remote_state);
CREATE TABLE IF NOT EXISTS ani_rss_media(
  remote_id TEXT NOT NULL,anime_id INTEGER NOT NULL,filename TEXT NOT NULL,episode REAL,title TEXT NOT NULL,
  name TEXT NOT NULL,size INTEGER NOT NULL,extension TEXT NOT NULL,last_seen_at TEXT NOT NULL,
  PRIMARY KEY(remote_id,filename)
);
CREATE INDEX IF NOT EXISTS ix_ani_rss_media_anime ON ani_rss_media(anime_id,remote_id,episode);
CREATE TABLE IF NOT EXISTS ani_rss_resource(
  resource_id TEXT PRIMARY KEY,anime_id INTEGER NOT NULL,provider TEXT NOT NULL,resource_kind TEXT NOT NULL,
  title TEXT NOT NULL,resource_group TEXT,source_class TEXT,resolution INTEGER,subtitle TEXT,
  sequence_first INTEGER,sequence_last INTEGER,item_count INTEGER NOT NULL,total_bytes INTEGER,
  eligible INTEGER NOT NULL,rank_key TEXT NOT NULL,payload_json TEXT NOT NULL,
  discovered_at TEXT NOT NULL,expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ani_rss_resource_anime ON ani_rss_resource(anime_id,eligible,rank_key);
CREATE TABLE IF NOT EXISTS ani_rss_search_state(
  anime_id INTEGER PRIMARY KEY,last_attempt_at TEXT NOT NULL,last_success_at TEXT,result_count INTEGER NOT NULL DEFAULT 0,error_text TEXT
);
CREATE TABLE IF NOT EXISTS ani_rss_action(
  action_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,anime_id INTEGER NOT NULL,
  resource_id TEXT NOT NULL,remote_id TEXT,action_kind TEXT NOT NULL,state TEXT NOT NULL,
  requested_at TEXT NOT NULL,updated_at TEXT NOT NULL,error_text TEXT,evidence_json TEXT NOT NULL
);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(ani_rss_state)")}
    if "credential_fingerprint" not in columns:
        db.execute("ALTER TABLE ani_rss_state ADD COLUMN credential_fingerprint TEXT")


def _credential_fingerprint(key: str, endpoint: str) -> str:
    """Return a non-secret generation marker for the active Ani-RSS credential."""
    return hashlib.sha256(f"{endpoint}\0{key}".encode("utf-8")).hexdigest()


def state_available(value: dict[str, Any]) -> bool:
    """Use one health gate for every user-visible Ani-RSS overlay."""
    return value.get("connection_state") == "ready" and bool(value.get("credentialConfigured"))


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = dict(config.get("components", {}).get("aniRss", {}) or {})
    mode = str(raw.get("mode") or "prefer").casefold()
    raw["mode"] = mode if mode in MODES else "prefer"
    raw["endpoint"] = str(raw.get("endpoint") or "http://127.0.0.1:7789").rstrip("/")
    raw["mediaPath"] = str(raw.get("mediaPath") or "").strip()
    raw["syncMinutes"] = max(5, int(raw.get("syncMinutes", 30)))
    raw["deleteGraceSyncs"] = max(1, int(raw.get("deleteGraceSyncs", 2)))
    return raw


def _route_revision(settings: dict[str, Any]) -> int | None:
    """Return the live proxy-route revision when it can affect this Ani-RSS endpoint."""
    try:
        route = network_transport.proxy_route(str(settings.get("endpoint") or ""))
        if str(route.get("reason") or "") == "local":
            return None
        return int(route.get("revision") or 0)
    except (OSError, ValueError, TypeError):
        return None


def _record_route_revision(db: sqlite3.Connection, revision: int | None) -> None:
    """Persist the route generation that was actually used for the sync attempt."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
        return
    if revision is None:
        db.execute("DELETE FROM metadata WHERE key='ani_rss_route_revision'")
    else:
        db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('ani_rss_route_revision',?)",
            (str(revision),),
        )


def _month_index(value: str) -> int | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(value or ""))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return year * 12 + month - 1 if 1 <= month <= 12 else None


def automatic_search_eligible(start_month: str, *, today: dt.date | None = None) -> bool:
    """Background Ani-RSS discovery is limited to the rolling latest 24 aired months."""
    index = _month_index(start_month)
    current = today or dt.date.today()
    current_index = current.year * 12 + current.month - 1
    return index is not None and current_index - 23 <= index <= current_index


def _work_region_enabled(db: sqlite3.Connection, anime_id: int, config: dict[str, Any]) -> bool:
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_country'").fetchone()
    codes = [row[0] for row in db.execute("SELECT country_code FROM anime_country WHERE anime_id=?", (anime_id,))] if table else []
    return region_policy_enabled(config.get("torrentPolicy", {}), codes)


def background_search_due(db_path: Path, anime_id: int, config: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    """Return whether one work should be re-queried by the automatic rolling scanner."""
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        migrate(db)
        work = db.execute("SELECT start_month FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not work or not automatic_search_eligible(str(work["start_month"] or ""), today=current.date()):
            return False
        if not _work_region_enabled(db, anime_id, config):
            return False
        row = db.execute("SELECT last_attempt_at,error_text FROM ani_rss_search_state WHERE anime_id=?", (anime_id,)).fetchone()
    if not row or not row[0]:
        return True
    try:
        last = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return True
    if last > current + dt.timedelta(minutes=1):
        return True
    poll = max(5, int(config.get("components", {}).get("discovery", {}).get("pollMinutes", 30)))
    retry_minutes = min(poll, 5) if len(row) > 1 and row[1] else poll
    return current - last >= dt.timedelta(minutes=retry_minutes)


def automatic_search_ids(db_path: Path, *, today: dt.date | None = None) -> list[int]:
    """Return IDs in recent-6-month-first, then month-7..24 order."""
    current = today or dt.date.today()
    current_index = current.year * 12 + current.month - 1
    cutoff_index = current_index - 23
    current_month = f"{current_index // 12:04d}-{current_index % 12 + 1:02d}"
    recent_start_index = current_index - 5
    recent_start = f"{recent_start_index // 12:04d}-{recent_start_index % 12 + 1:02d}"
    cutoff = f"{cutoff_index // 12:04d}-{cutoff_index % 12 + 1:02d}"
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        recent = [int(row[0]) for row in db.execute(
            "SELECT id FROM anime_work WHERE start_month>=? AND start_month<=? ORDER BY id",
            (recent_start, current_month),
        )]
        older = [int(row[0]) for row in db.execute(
            "SELECT id FROM anime_work WHERE start_month>=? AND start_month<? ORDER BY start_month DESC,id",
            (cutoff, recent_start),
        )]
    return recent + older


def refresh_background_resources(db_path: Path, config: dict[str, Any], *,
                                 stop_event: threading.Event | None = None,
                                 abort_event: threading.Event | None = None) -> dict[str, Any]:
    """Refresh due rolling Ani-RSS resources once, without overlapping another pass.

    This pass is intentionally independent from image warm-up so a first-run
    connection that becomes ready a few seconds after startup is not missed.
    Per-work due timestamps remain authoritative; an overlapping pass is skipped.
    """
    current_state = state(db_path, config)
    if (not state_available(current_state)
            or str(current_state.get("effective_mode") or "manual") not in {"prefer", "fallback"}):
        return {"started": False, "reason": "unavailable", "checked": 0, "refreshed": 0, "failed": 0}
    with background_resource_scan_lease() as acquired:
        if not acquired:
            return {"started": False, "reason": "busy", "checked": 0, "refreshed": 0, "failed": 0}
        checked = refreshed = failed = 0
        for anime_id in automatic_search_ids(db_path):
            if ((stop_event is not None and stop_event.is_set())
                    or (abort_event is not None and abort_event.is_set())):
                break
            checked += 1
            # Re-check health between works. A dead optional endpoint stops this
            # pass immediately instead of cascading one timeout across the queue.
            live_state = state(db_path, config)
            if (not state_available(live_state)
                    or str(live_state.get("effective_mode") or "manual") not in {"prefer", "fallback"}):
                break
            if not background_search_due(db_path, anime_id, config):
                continue
            try:
                search_result = search(db_path, anime_id, config)
                if search_result.get("stale"):
                    break
                refreshed += 1
            except (OSError, RuntimeError, urllib.error.URLError, httpx.RequestError):
                failed += 1
                break
            except (ValueError, sqlite3.Error):
                failed += 1
                continue
        return {"started": True, "reason": "complete", "checked": checked,
                "refreshed": refreshed, "failed": failed}


def _secret() -> str:
    value = os.getenv("ANM_ANI_RSS_API_KEY", "").strip()
    secret_file = os.getenv("ANM_ANI_RSS_API_KEY_FILE", "").strip()
    if not value and secret_file and Path(secret_file).is_file():
        value = Path(secret_file).read_text(encoding="utf-8").strip()
    return value


def _strict_mapping_list(value: Any, label: str) -> list[dict[str, Any]]:
    """Accept an optional JSON array of objects, but never coerce malformed data to empty."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError(f"Ani-RSS {label} returned an unsupported payload")
    return value


class Client:
    """Small compatibility adapter for the authenticated Ani-RSS HTTP API."""

    def __init__(self, endpoint: str, key: str, timeout: int = 30) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Ani-RSS endpoint must be an absolute HTTP(S) URL")
        self.endpoint = endpoint.rstrip("/")
        self.key = key
        self.timeout = timeout

    def call(self, path: str, *, params: dict[str, Any] | None = None,
             body: Any | None = None, timeout: int | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.endpoint}/api/{path.lstrip('/')}" + (f"?{query}" if query else "")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data or b"", method="POST", headers={
            "User-Agent": f"AnimeMachine/{__version__} (Ani-RSS adapter)",
            "api-key": self.key,
            "Accept": "application/json",
            **({"Content-Type": "application/json; charset=utf-8"} if body is not None else {}),
        })
        try:
            with tls_support.urlopen(request, timeout=timeout or self.timeout, max_bytes=4 * 1024 * 1024) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Ani-RSS request failed with HTTP {exc.response.status_code}") from None
        except httpx.RequestError:
            raise RuntimeError("Ani-RSS request failed") from None
        if not isinstance(payload, dict):
            return payload
        if "code" in payload and int(payload.get("code", 500)) != 200:
            raise RuntimeError(str(payload.get("message") or "Ani-RSS request failed"))
        return payload.get("data") if "data" in payload else payload

    def probe(self) -> dict[str, Any]:
        about = self.call("about")
        about = {} if about is None else about
        if not isinstance(about, dict):
            raise RuntimeError("Ani-RSS probe returned an unsupported payload")
        subscriptions, advertised_total = self.subscription_snapshot()
        return {"version": str(about.get("version") or ""),
                "subscriptions": advertised_total if advertised_total is not None else len(subscriptions),
                "capabilities": {"list": True, "search": True, "subscribe": True,
                                 "collection": True, "delete": True}}

    def subscription_snapshot(self) -> tuple[list[dict[str, Any]], int | None]:
        """Return the de-duplicated listAni rows and its advertised total, when present."""
        data = self.call("listAni")
        if not isinstance(data, dict):
            raise RuntimeError("Ani-RSS listAni returned an unsupported payload")
        if "weekList" not in data or data.get("weekList") is None:
            raise RuntimeError("Ani-RSS listAni.weekList returned an unsupported payload")
        result: list[dict[str, Any]] = []
        for week in _strict_mapping_list(data.get("weekList"), "listAni.weekList"):
            if "items" not in week or week.get("items") is None:
                raise RuntimeError("Ani-RSS listAni.weekList[].items returned an unsupported payload")
            result.extend(_strict_mapping_list(week.get("items"), "listAni.weekList[].items"))
        unique: dict[str, dict[str, Any]] = {}
        for item in result:
            remote_id = str(item.get("id") or "").strip()
            if remote_id:
                unique[remote_id] = item
        advertised_total: int | None = None
        if data.get("total") is not None:
            raw_total = data.get("total")
            try:
                if isinstance(raw_total, bool) or (isinstance(raw_total, float) and not raw_total.is_integer()):
                    raise ValueError
                advertised_total = int(raw_total)
                if advertised_total < 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Ani-RSS listAni.total returned an unsupported payload") from exc
        return list(unique.values()), advertised_total

    def subscriptions(self) -> list[dict[str, Any]]:
        return self.subscription_snapshot()[0]


    def play_list(self, subscription_url: str, *, timeout: int = 20) -> list[dict[str, Any]]:
        value = str(subscription_url or "").strip()
        if not value:
            raise ValueError("Ani-RSS subscription URL is unavailable")
        data = self.call("playList", body={"url": value}, timeout=max(2, int(timeout)))
        if isinstance(data, list):
            return _strict_mapping_list(data, "playList")
        if isinstance(data, dict):
            for key in ("items", "list", "files", "data"):
                items = data.get(key)
                if items is not None:
                    return _strict_mapping_list(items, f"playList.{key}")
        raise RuntimeError("Ani-RSS playList returned an unsupported payload")

    def file_bytes(self, filename: str, *, limit: int = 12 * 1024 * 1024,
                   retries: int = 3) -> tuple[bytes, str]:
        """Read one Ani-RSS local file through its authenticated file API."""
        with self.stream_file(filename, retries=retries) as response:
            content_type = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise ValueError("Ani-RSS file exceeds configured limit")
                chunks.append(chunk)
            return b"".join(chunks), content_type

    @contextlib.contextmanager
    def stream_file(self, filename: str, range_header: str = "", *, retries: int = 3):
        candidates = _file_parameter_candidates(filename)
        retries = max(1, min(3, int(retries)))
        last_status = 502
        failed_statuses: list[int] = []
        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        for index, candidate in enumerate(candidates):
            query = urllib.parse.urlencode({"filename": candidate})
            url = f"{self.endpoint}/api/file?{query}"
            headers = {
                "User-Agent": f"AnimeMachine/{__version__} (Ani-RSS adapter)",
                "api-key": self.key,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
            if range_header:
                headers["Range"] = range_header
            for attempt in range(retries):
                manager = network_transport.stream(
                    "GET", url, headers=headers, timeout=self.timeout, allow_credentials=True, follow_redirects=False)
                try:
                    response = manager.__enter__()
                except (httpx.RequestError, OSError, TimeoutError) as exc:
                    if attempt + 1 < retries:
                        time.sleep((0.08, 0.2)[min(attempt, 1)])
                        continue
                    raise RemoteFileError(502) from exc
                last_status = int(response.status_code)
                if last_status in {200, 206}:
                    try:
                        yield response
                    except BaseException as exc:
                        manager.__exit__(type(exc), exc, exc.__traceback__)
                        raise
                    else:
                        manager.__exit__(None, None, None)
                    return
                failed_statuses.append(last_status)
                manager.__exit__(None, None, None)
                if last_status in retryable_statuses and attempt + 1 < retries:
                    time.sleep((0.08, 0.2)[min(attempt, 1)])
                    continue
                break
            if last_status in {400, 404, 422} and index + 1 < len(candidates):
                continue
            raise RemoteFileError(404 if 404 in failed_statuses else last_status)
        raise RemoteFileError(404 if 404 in failed_statuses else last_status)


@dataclass(frozen=True)
class RemotePlaybackItem:
    episode: float | None
    title: str
    name: str
    size: int
    filename: str
    extension: str


class RemoteFileError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Ani-RSS media request failed with HTTP {int(status)}")
        self.status = int(status)


def _path_name(value: str) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def _extension(value: str) -> str:
    name = _path_name(value)
    return name.rsplit(".", 1)[-1].casefold() if "." in name else ""


def _parse_remote_size(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    text = str(value or "").strip().replace(",", "")
    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)?", text)
    if not match:
        return 0
    unit = (match.group(2) or "B").upper()
    scale = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1000**2, "MIB": 1024**2,
             "GB": 1000**3, "GIB": 1024**3, "TB": 1000**4, "TIB": 1024**4}[unit]
    return max(0, int(float(match.group(1)) * scale))


def _remote_episode(item: dict[str, Any], name: str) -> float | None:
    value = item.get("episode")
    reported: float | None = None
    if value is not None:
        try:
            reported = float(value)
        except (TypeError, ValueError):
            pass
    match = re.search(
        r"(?i)(?:\bS\d{1,2}[ ._-]*E(\d{1,4}(?:\.5)?)\b|"
        r"\b(?:ep(?:isode)?|e)[ ._-]*(\d{1,4}(?:\.5)?)\b|"
        r"第\s*(\d{1,4}(?:\.5)?)\s*[话話集])",
        name,
    )
    if match:
        return float(next(group for group in match.groups() if group))
    return reported


def _normalize_play_list(items: list[dict[str, Any]]) -> list[RemotePlaybackItem]:
    result: list[RemotePlaybackItem] = []
    seen: set[str] = set()
    for item in items:
        filename = str(item.get("filename") or item.get("file") or item.get("path") or "").strip()
        if not filename or filename in seen:
            continue
        name = _path_name(str(item.get("name") or _path_name(filename)).strip()) or _path_name(filename)
        extension = str(item.get("extension") or item.get("extName") or item.get("ext") or _extension(name)).strip().lstrip(".").casefold()
        if f".{extension}" not in {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ogm"}:
            continue
        size = 0
        for key in ("size", "bytes", "fileSize", "length", "formatSize"):
            size = _parse_remote_size(item.get(key))
            if size:
                break
        episode = _remote_episode(item, name)
        title = str(item.get("title") or name).strip() or name
        if "/" in title or "\\" in title:
            title = _path_name(title) or name
        result.append(RemotePlaybackItem(episode, title, name, size, filename, extension))
        seen.add(filename)
    result.sort(key=lambda entry: (entry.episode is None, entry.episode or 0, _normalize(entry.name)))
    return result


def _looks_base64_path(value: str) -> bool:
    compact = value.strip().replace(" ", "+")
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return bool(_extension(decoded)) and ("/" in decoded or "\\" in decoded)


def _file_parameter_candidates(filename: str) -> list[str]:
    value = str(filename or "").strip()
    if not value:
        raise ValueError("Ani-RSS media filename is unavailable")
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    candidates = [value, encoded] if _looks_base64_path(value) else [encoded, value]
    return list(dict.fromkeys(candidates))


def playback_items(db_path: Path, anime_id: int, config: dict[str, Any], remote_id: str) -> list[RemotePlaybackItem]:
    current_state = state(db_path, config)
    if not state_available(current_state):
        raise ValueError("Ani-RSS playback source is unavailable")
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_subscription'").fetchone():
            raise ValueError("Ani-RSS playback source is unavailable")
        row = db.execute("""SELECT remote_id,title,evidence_json FROM ani_rss_subscription
            WHERE remote_id=? AND anime_id=? AND deleted_at IS NULL""", (remote_id, anime_id)).fetchone()
        has_media = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_media'").fetchone()
        cached = db.execute("""SELECT episode,title,name,size,filename,extension FROM ani_rss_media
            WHERE remote_id=? AND anime_id=? ORDER BY episode IS NULL,episode,name""", (remote_id, anime_id)).fetchall() if has_media else []
    if not row:
        raise ValueError("Ani-RSS playback source is unavailable")
    if cached:
        return [RemotePlaybackItem(item["episode"], item["title"], item["name"], int(item["size"] or 0),
                                   item["filename"], item["extension"]) for item in cached]
    evidence = json.loads(str(row["evidence_json"] or "{}"))
    subscription_url = str(evidence.get("url") or "").strip()
    client = _client(config)
    if not subscription_url:
        remote = next((item for item in client.subscriptions() if str(item.get("id") or "") == remote_id), None)
        subscription_url = str((remote or {}).get("url") or "").strip()
    try:
        return _normalize_play_list(client.play_list(subscription_url))
    except RuntimeError:
        raise RuntimeError("Ani-RSS playList request failed") from None


def stream_media(config: dict[str, Any], filename: str, range_header: str = ""):
    return _client(config).stream_file(filename, range_header)


def _client(config: dict[str, Any], api_key: str | None = None) -> Client:
    key = str(api_key or "").strip() or _secret()
    if not key:
        raise ValueError("Ani-RSS API key is not configured")
    return Client(_settings(config)["endpoint"], key)


def sync_due(db_path: Path, config: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    """Return whether the unified Ani-RSS snapshot is due for refresh.

    A complete snapshot follows ``syncMinutes``. Connection failures and partial
    media refreshes retry on a short bounded cadence so one transient failure
    cannot leave playable episodes stale for another full interval.
    """
    key = _secret()
    if not key:
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    settings = _settings(config)
    current_route_revision = _route_revision(settings)
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_state'").fetchone()
            row = db.execute("SELECT * FROM ani_rss_state WHERE singleton=1").fetchone() if table else None
            columns = [str(item[1]) for item in db.execute("PRAGMA table_info(ani_rss_state)")] if table else []
            metadata_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            route_row = db.execute(
                "SELECT value FROM metadata WHERE key='ani_rss_route_revision'"
            ).fetchone() if metadata_table else None
    except sqlite3.Error:
        return True
    if not row:
        return True
    if current_route_revision is not None:
        try:
            recorded_route_revision = int(route_row[0]) if route_row else None
        except (TypeError, ValueError):
            recorded_route_revision = None
        if recorded_route_revision != current_route_revision:
            return True
    values = dict(zip(columns, row))
    expected_fingerprint = _credential_fingerprint(key, settings["endpoint"])
    if (str(values.get("endpoint")) != settings["endpoint"]
            or str(values.get("configured_mode")) != settings["mode"]
            or str(values.get("credential_fingerprint") or "") != expected_fingerprint
            or not values.get("last_attempt_at")):
        return True

    def parsed(value: Any) -> dt.datetime | None:
        try:
            result = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=dt.timezone.utc)
            return result
        except ValueError:
            return None

    last_attempt = parsed(values.get("last_attempt_at"))
    last_success = parsed(values.get("last_success_at"))
    if last_attempt is None:
        return True
    # Wall-clock corrections must not suppress Ani-RSS indefinitely. A stored
    # timestamp materially in the future is treated as stale and refreshed now.
    future_tolerance = dt.timedelta(minutes=1)
    if last_attempt > current + future_tolerance or (last_success and last_success > current + future_tolerance):
        return True
    retry_minutes = min(settings["syncMinutes"], 5)
    if str(values.get("connection_state")) != "ready" or values.get("last_error") or last_success is None:
        return current - last_attempt >= dt.timedelta(minutes=retry_minutes)
    return current - last_success >= dt.timedelta(minutes=settings["syncMinutes"])


def _cover_endpoint_available(endpoint: str, credential_fingerprint: str) -> bool:
    # Only route changes that can actually affect this Ani-RSS endpoint may
    # invalidate its cooldown. Local/LAN endpoints stay direct, so unrelated
    # environment-proxy toggles must not make every queued image retry them.
    revision = _route_revision({"endpoint": endpoint})
    now = time.monotonic()
    key = (endpoint, credential_fingerprint)
    with _COVER_FAILURE_LOCK:
        failure = _COVER_FAILURES.get(key)
        if failure is None:
            return True
        failed_revision, retry_at = failure
        if failed_revision != revision or retry_at <= now:
            _COVER_FAILURES.pop(key, None)
            return True
        return False


def _cover_endpoint_result(endpoint: str, credential_fingerprint: str, *, healthy: bool) -> None:
    revision = _route_revision({"endpoint": endpoint})
    key = (endpoint, credential_fingerprint)
    with _COVER_FAILURE_LOCK:
        if healthy:
            # A successful authenticated read proves the endpoint healthy. Clear
            # stale failures from older credentials for the same endpoint too.
            for cached_key in [value for value in _COVER_FAILURES if value[0] == endpoint]:
                _COVER_FAILURES.pop(cached_key, None)
        else:
            _COVER_FAILURES[key] = (revision, time.monotonic() + _COVER_FAILURE_COOLDOWN_SECONDS)


def cached_cover(db_path: Path, anime_id: int, config: dict[str, Any] | None = None, *,
                 api_key: str | None = None) -> tuple[bytes, str, str] | None:
    """Copy a mapped Ani-RSS cached cover without replacing an existing AnimeMachine cache implicitly."""
    key = str(api_key or "").strip() or _secret()
    if not key:
        return None
    if config is not None and not state_available(state(db_path, config)):
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_state'").fetchone():
                return None
            state_row = db.execute("SELECT * FROM ani_rss_state WHERE singleton=1").fetchone()
            if not state_row or state_row["connection_state"] != "ready":
                return None
            if ("credential_fingerprint" not in state_row.keys()
                    or str(state_row["credential_fingerprint"] or "")
                    != _credential_fingerprint(key, str(state_row["endpoint"]))):
                return None
            if config is not None:
                current_settings = _settings(config)
                if (str(state_row["endpoint"]) != current_settings["endpoint"]
                        or str(state_row["configured_mode"]) != current_settings["mode"]):
                    return None
            rows = db.execute("""SELECT remote_id,evidence_json FROM ani_rss_subscription
                WHERE anime_id=? AND deleted_at IS NULL ORDER BY enabled DESC,last_seen_at DESC""", (anime_id,)).fetchall()
        endpoint = _settings(config or {})["endpoint"] if config is not None else str(state_row["endpoint"])
        credential_fingerprint = _credential_fingerprint(key, endpoint)
        # The file endpoint is an optional fast path. After a transport/server
        # failure, briefly bypass it for the remaining image queue. A route
        # change that can actually affect this endpoint invalidates the cooldown.
        if not _cover_endpoint_available(endpoint, credential_fingerprint):
            return None
        client = Client(endpoint, key, timeout=4)
        for row in rows:
            evidence = json.loads(str(row["evidence_json"] or "{}"))
            cover = str(evidence.get("cover") or "").strip()
            if not cover:
                continue
            try:
                # Cover reuse is only an optimization. One failed attempt is
                # enough to fall back immediately to AnimeMachine image sources.
                data, mime = client.file_bytes(cover, retries=1)
                data, mime = network_validators.cached_image_bytes(data, mime)
                _cover_endpoint_result(endpoint, credential_fingerprint, healthy=True)
                return data, mime, f"ani-rss://{row['remote_id']}/cover"
            except RemoteFileError as exc:
                # 404/400/422 are file-specific evidence and must not disable
                # other covers. Transport/retryable/server failures are endpoint
                # evidence and should make subsequent images fall back at once.
                if exc.status not in {400, 404, 422}:
                    _cover_endpoint_result(endpoint, credential_fingerprint, healthy=False)
                    return None
                continue
            except (OSError, RuntimeError, httpx.RequestError):
                _cover_endpoint_result(endpoint, credential_fingerprint, healthy=False)
                return None
            except ValueError:
                continue
    except (sqlite3.Error, OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _bgm_id(value: Any) -> int | None:
    match = re.search(r"(?:subject/|bgmId=)(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _normalize(value: str | None) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKC", value or "").casefold() if ch.isalnum())


def _resolve_anime(db: sqlite3.Connection, item: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    bgm_id = _bgm_id(item.get("bgmUrl") or item.get("url"))
    if bgm_id:
        row = db.execute("SELECT id FROM anime_work WHERE bgm_id=?", (bgm_id,)).fetchone()
        if row:
            return int(row[0]), {"mode": "bgm_id", "bgmId": bgm_id}
    candidates: set[int] = set()
    title_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(anime_title)")}
    for title in (item.get("jpTitle"), item.get("title"), item.get("mikanTitle")):
        key = _normalize(str(title or ""))
        if not key:
            continue
        if "normalized_title" in title_columns:
            rows = db.execute("SELECT anime_id FROM anime_title WHERE normalized_title=?", (key,))
        else:
            # The tiny first-paint catalog intentionally predates feature
            # migrations. Keep Ani-RSS sync operational while the full Archive
            # database is being built in the background.
            rows = ((anime_id,) for anime_id, stored in db.execute("SELECT anime_id,title FROM anime_title")
                    if _normalize(str(stored)) == key)
        candidates.update(int(row[0]) for row in rows)
    if len(candidates) == 1:
        return next(iter(candidates)), {"mode": "unique_title"}
    return None, {"mode": "unmatched" if not candidates else "ambiguous", "candidateIds": sorted(candidates)}


def probe(config: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    settings = _settings(config)
    key = str(api_key or "").strip() or _secret()
    if not key:
        return {"kind": "ani-rss", "reachable": False, "authenticated": False,
                "configuredMode": settings["mode"], "effectiveMode": "manual",
                "message": "credentials_required"}
    try:
        result = Client(settings["endpoint"], key).probe()
        return {"kind": "ani-rss", "reachable": True, "authenticated": True,
                "configuredMode": settings["mode"], "effectiveMode": settings["mode"],
                "message": "ok", **result}
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        return {"kind": "ani-rss", "reachable": False, "authenticated": False,
                "configuredMode": settings["mode"], "effectiveMode": "manual",
                "message": "connection_failed", "errorType": type(exc).__name__}


def sync(db_path: Path, config: dict[str, Any], *, abort_event: threading.Event | None = None) -> dict[str, Any]:
    settings = _settings(config); stamp = utcnow()
    key = _secret()
    fingerprint = _credential_fingerprint(key, settings["endpoint"]) if key else None
    route_revision = _route_revision(settings)
    resource_refresh_required = False
    try:
        if not key:
            raise ValueError("Ani-RSS API key is not configured")
        client = Client(settings["endpoint"], key, timeout=8)
        about = client.call("about")
        about = {} if about is None else about
        if not isinstance(about, dict):
            raise RuntimeError("Ani-RSS about returned an unsupported payload")
        subscriptions, advertised_total = client.subscription_snapshot()
        listing_complete = advertised_total is None or advertised_total == len(subscriptions)
        deletion_snapshot_complete = advertised_total is not None and advertised_total == len(subscriptions)
        capability = {"version": str(about.get("version") or "")}
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            migrate(db)
            previous_state = db.execute(
                "SELECT endpoint,credential_fingerprint FROM ani_rss_state WHERE singleton=1"
            ).fetchone()
            resource_refresh_required = bool(previous_state and (
                str(previous_state[0]) != settings["endpoint"]
                or str(previous_state[1] or "") != str(fingerprint or "")
            ))
            if resource_refresh_required:
                db.execute("DELETE FROM ani_rss_resource")
                db.execute("DELETE FROM ani_rss_search_state")
            _record_route_revision(db, route_revision)
            db.execute("""INSERT INTO ani_rss_state
                (singleton,endpoint,version,connection_state,configured_mode,effective_mode,last_attempt_at,
                 last_success_at,last_error,successful_generation,credential_fingerprint)
                VALUES(1,?,?,?,?,'manual',?,?,?,0,?)
                ON CONFLICT(singleton) DO UPDATE SET endpoint=excluded.endpoint,connection_state='error',
                configured_mode=excluded.configured_mode,effective_mode='manual',last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error,credential_fingerprint=excluded.credential_fingerprint""",
                (settings["endpoint"], None, "error", settings["mode"], stamp, None,
                 f"{type(exc).__name__}: {exc}", fingerprint))
        return {"state": "error", "effectiveMode": "manual", "errorType": type(exc).__name__,
                "resourceRefreshRequired": resource_refresh_required}

    mapped_subscriptions: list[tuple[str, int, str]] = []
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        db.row_factory = sqlite3.Row; db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=60000")
        migrate(db)
        previous_state = db.execute(
            "SELECT successful_generation,endpoint,credential_fingerprint FROM ani_rss_state WHERE singleton=1"
        ).fetchone()
        generation = int(previous_state[0] if previous_state else 0) + 1
        resource_refresh_required = bool(previous_state and (
            str(previous_state[1]) != settings["endpoint"]
            or str(previous_state[2] or "") != str(fingerprint or "")
        ))
        if resource_refresh_required:
            db.execute("DELETE FROM ani_rss_resource")
            db.execute("DELETE FROM ani_rss_search_state")
        _record_route_revision(db, route_revision)
        seen: set[str] = set(); mapped = 0
        cover_candidates: set[int] = set()
        for item in subscriptions:
            remote_id = str(item.get("id") or "").strip()
            if not remote_id:
                continue
            seen.add(remote_id)
            previous = db.execute("""SELECT anime_id,bgm_id,title,enabled,current_episode,total_episode,
                remote_media_path,evidence_json FROM ani_rss_subscription
                WHERE remote_id=? AND deleted_at IS NULL""", (remote_id,)).fetchone()
            previous_evidence: dict[str, Any] = {}
            if previous:
                with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
                    parsed = json.loads(str(previous["evidence_json"] or "{}"))
                    if isinstance(parsed, dict):
                        previous_evidence = parsed

            anime_id, identity = _resolve_anime(db, item)
            # listAni can transiently omit optional identity fields. A stable
            # remote id from the previous successful generation is stronger
            # evidence than dropping a known mapping for one partial payload.
            if anime_id is None and previous and previous["anime_id"] is not None:
                anime_id = int(previous["anime_id"])
                identity = {"mode": "stable_remote_id", "previous": previous_evidence.get("identity")}
            if anime_id is not None:
                mapped += 1

            def evidence_value(name: str, *aliases: str) -> Any:
                candidates = (name, *aliases)
                for candidate in candidates:
                    value = item.get(candidate)
                    if value is not None and not (isinstance(value, str) and not value.strip()):
                        return value
                for candidate in candidates:
                    value = previous_evidence.get(candidate)
                    if value is not None and not (isinstance(value, str) and not value.strip()):
                        return value
                return None

            subscription_url = str(evidence_value("url") or "").strip()
            bgm_id = _bgm_id(evidence_value("bgmUrl") or subscription_url)
            if bgm_id is None and previous and previous["bgm_id"] is not None:
                bgm_id = int(previous["bgm_id"])

            def integer_value(name: str, previous_value: Any) -> int:
                value = item.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    return int(previous_value or 0)
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return int(previous_value or 0)

            enabled = bool(item.get("enable")) if "enable" in item else bool(previous["enabled"] if previous else False)
            current_episode = integer_value("currentEpisodeNumber", previous["current_episode"] if previous else 0)
            total_episode = integer_value("totalEpisodeNumber", previous["total_episode"] if previous else 0)
            remote_media_path = str(evidence_value("customDownloadPathTemplate", "downloadPath") or (previous["remote_media_path"] if previous else "") or "").strip() or None
            evidence = {"identity": identity, "generation": generation,
                        "url": subscription_url or None, "bgmUrl": evidence_value("bgmUrl"),
                        "subgroup": evidence_value("subgroup"), "season": evidence_value("season"),
                        "cover": evidence_value("cover"), "image": evidence_value("image"),
                        "mikanTitle": evidence_value("mikanTitle"), "jpTitle": evidence_value("jpTitle"),
                        "type": evidence_value("type"), "customDownloadPathTemplate": remote_media_path, "downloadPath": remote_media_path,
                        "score": evidence_value("score"), "completed": evidence_value("completed")}
            if anime_id is not None and str(evidence.get("cover") or "").strip():
                cover_candidates.add(anime_id)
            title = str(item.get("title") or item.get("jpTitle") or (previous["title"] if previous else "") or remote_id)
            db.execute("""INSERT INTO ani_rss_subscription VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(remote_id) DO UPDATE SET anime_id=excluded.anime_id,title=excluded.title,
                bgm_id=excluded.bgm_id,enabled=excluded.enabled,subscription_kind=excluded.subscription_kind,
                current_episode=excluded.current_episode,total_episode=excluded.total_episode,
                remote_media_path=excluded.remote_media_path,remote_state=excluded.remote_state,last_seen_at=excluded.last_seen_at,
                missed_successful_syncs=0,deleted_at=NULL,evidence_json=excluded.evidence_json""",
                (remote_id, anime_id, title, bgm_id, enabled, "follow", current_episode,
                 total_episode, remote_media_path, "enabled" if enabled else "disabled", stamp, stamp, 0, None,
                 json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))))
            # playList reflects downloaded media, not subscription scheduling.
            # Keep disabled subscriptions refreshable because their existing files
            # remain valid external read-only media and may still change remotely.
            # A prior URL is retained when listAni temporarily omits it, so one
            # incomplete payload cannot freeze an otherwise healthy media snapshot.
            if anime_id is not None and subscription_url:
                mapped_subscriptions.append((remote_id, anime_id, subscription_url))
        # A prior Bangumi ``no_cover`` is only negative evidence for the
        # sources available at that time. Once a healthy Ani-RSS snapshot maps
        # an explicit cover to the work, release that negative cache so the next
        # normal image request can use Ani-RSS immediately instead of waiting for
        # the 24/72-hour cover-maintenance cadence (or forever outside its window).
        if cover_candidates and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_image'").fetchone():
            db.executemany(
                "UPDATE anime_image SET fetched_at=NULL,error=NULL "
                "WHERE anime_id=? AND image_blob IS NULL AND error='no_cover'",
                [(anime_id,) for anime_id in sorted(cover_candidates)],
            )
        missing = list(db.execute("SELECT remote_id,missed_successful_syncs FROM ani_rss_subscription WHERE deleted_at IS NULL"))
        deleted = 0
        # A truncated/paginated listAni response must never age unseen rows toward
        # deletion. Older Ani-RSS versions without ``total`` may still sync new or
        # updated rows, but absence alone is not strong enough evidence to delete a
        # previously verified subscription.
        if deletion_snapshot_complete:
            for row in missing:
                if str(row[0]) in seen:
                    continue
                misses = int(row[1]) + 1
                if misses >= settings["deleteGraceSyncs"]:
                    db.execute("UPDATE ani_rss_subscription SET remote_state='deleted',missed_successful_syncs=?,deleted_at=? WHERE remote_id=?",
                               (misses, stamp, row[0])); deleted += 1
                else:
                    db.execute("UPDATE ani_rss_subscription SET remote_state='missing_unconfirmed',missed_successful_syncs=? WHERE remote_id=?",
                               (misses, row[0]))
        # Subscription mapping is committed first, but ``last_success_at`` is
        # advanced only after the media phase also completes. This keeps the
        # configured cadence tied to one coherent subscription/media snapshot.
        db.execute("""INSERT INTO ani_rss_state
            (singleton,endpoint,version,connection_state,configured_mode,effective_mode,last_attempt_at,
             last_success_at,last_error,successful_generation,credential_fingerprint)
            VALUES(1,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET endpoint=excluded.endpoint,version=excluded.version,
            connection_state='ready',configured_mode=excluded.configured_mode,effective_mode=excluded.effective_mode,
            last_attempt_at=excluded.last_attempt_at,last_error=NULL,successful_generation=excluded.successful_generation,
            credential_fingerprint=excluded.credential_fingerprint""",
            (settings["endpoint"], capability.get("version"), "ready", settings["mode"], settings["mode"],
             stamp, None, None, generation, fingerprint))

    # Media availability is part of the same snapshot, but one bad subscription
    # must not invalidate listAni or erase the previous known-good playlist.
    media_results: dict[str, list[RemotePlaybackItem]] = {}
    media_failures = 0
    media_deferred = 0
    if mapped_subscriptions:
        # A bounded media phase must also be fair. If a few remote playlists hang,
        # rotate the starting point on each successful subscription generation so
        # later subscriptions cannot starve forever behind the same slow entries.
        workers = min(6, len(mapped_subscriptions))
        shift = ((generation - 1) * workers) % len(mapped_subscriptions)
        media_queue = mapped_subscriptions[shift:] + mapped_subscriptions[:shift]

        def fetch_media(entry: tuple[str, int, str]) -> tuple[str, list[RemotePlaybackItem] | None]:
            remote_id, _anime_id, url = entry
            try:
                return remote_id, _normalize_play_list(client.play_list(url, timeout=8))
            except (OSError, ValueError, RuntimeError, urllib.error.URLError, httpx.RequestError):
                return remote_id, None

        if abort_event is not None and abort_event.is_set():
            media_deferred = len(media_queue)
        else:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="anm-ani-media")
            futures = {pool.submit(fetch_media, entry): entry[0] for entry in media_queue}
            pending: set[concurrent.futures.Future[tuple[str, list[RemotePlaybackItem] | None]]] = set(futures)
            # Healthy local/LAN Ani-RSS instances commonly have many subscriptions.
            # Give the background phase enough time to cover all of them within
            # one configured interval, while still bounding a degraded endpoint.
            deadline = time.monotonic() + max(20.0, min(120.0, 10.0 + len(media_queue) * 2.0))
            aborted = False
            while pending:
                if abort_event is not None and abort_event.is_set():
                    aborted = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done_now, pending = concurrent.futures.wait(
                    pending, timeout=min(.2, remaining), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done_now:
                    try:
                        remote_id, items = future.result()
                    except Exception:
                        media_failures += 1
                        continue
                    if items is None:
                        media_failures += 1
                    else:
                        media_results[remote_id] = items
            if pending:
                if aborted:
                    media_deferred += len(pending)
                else:
                    media_failures += len(pending)
                for future in pending:
                    future.cancel()
            # Running requests keep their own short timeout, but queued work is
            # cancelled immediately. This lets a newly queued user operation take
            # the foreground lock without waiting for the full background budget.
            pool.shutdown(wait=False, cancel_futures=True)

    if media_results:
        anime_by_remote = {remote_id: anime_id for remote_id, anime_id, _url in mapped_subscriptions}
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            migrate(db)
            for remote_id, items in media_results.items():
                anime_id = anime_by_remote[remote_id]
                db.execute("DELETE FROM ani_rss_media WHERE remote_id=?", (remote_id,))
                db.executemany("""INSERT INTO ani_rss_media
                    (remote_id,anime_id,filename,episode,title,name,size,extension,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", [
                    (remote_id, anime_id, item.filename, item.episode, item.title, item.name,
                     item.size, item.extension, stamp) for item in items
                ])
                playable = [float(item.episode) for item in items if item.episode is not None]
                if playable:
                    db.execute("""UPDATE ani_rss_subscription
                        SET current_episode=MAX(COALESCE(current_episode,0),?) WHERE remote_id=?""",
                               (int(max(playable)), remote_id))
    # Remote deletion cleanup must not depend on any remaining subscription
    # returning a non-empty media result in this particular generation.
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        migrate(db)
        db.execute("""DELETE FROM ani_rss_media WHERE remote_id IN (
            SELECT remote_id FROM ani_rss_subscription WHERE deleted_at IS NOT NULL)""")

    media_complete = media_failures == 0 and media_deferred == 0
    snapshot_complete = media_complete and listing_complete
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        migrate(db)
        if snapshot_complete:
            db.execute("UPDATE ani_rss_state SET last_success_at=?,last_error=NULL WHERE singleton=1", (stamp,))
        else:
            reason = (
                f"SubscriptionSnapshotIncomplete: advertised={advertised_total},received={len(subscriptions)}"
                if not listing_complete else
                f"MediaSnapshotIncomplete: failures={media_failures},deferred={media_deferred}"
            )
            db.execute("UPDATE ani_rss_state SET last_error=? WHERE singleton=1", (reason,))
    return {"state": "ready", "version": capability.get("version"), "effectiveMode": settings["mode"],
            "subscriptions": len(seen), "mapped": mapped, "deleted": deleted, "generation": generation,
            "mediaSubscriptions": len(media_results), "mediaFailures": media_failures,
            "mediaDeferred": media_deferred, "listingComplete": listing_complete,
            "snapshotComplete": snapshot_complete,
            "mediaItems": sum(len(items) for items in media_results.values()),
            "resourceRefreshRequired": resource_refresh_required}


def state(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    key = _secret()
    credential_configured = bool(key)
    expected_fingerprint = _credential_fingerprint(key, settings["endpoint"]) if key else None
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_state'").fetchone()
        row = db.execute("SELECT * FROM ani_rss_state WHERE singleton=1").fetchone() if table else None
        metadata_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
        route_row = db.execute(
            "SELECT value FROM metadata WHERE key='ani_rss_route_revision'"
        ).fetchone() if metadata_table else None
    if not row:
        return {"endpoint": settings["endpoint"],
                "connection_state": "unconfigured" if not credential_configured else "unknown",
                "configured_mode": settings["mode"], "effective_mode": "manual",
                "credentialConfigured": credential_configured, "error": None}
    result = {k: row[k] for k in row.keys() if k not in {"last_error", "credential_fingerprint"}}
    result["credentialConfigured"] = credential_configured
    result["error"] = row["last_error"] and row["last_error"].split(":", 1)[0]
    # A previously healthy endpoint must not keep Ani-RSS logically enabled
    # after its credential is removed or the configured endpoint/mode changes.
    if not credential_configured:
        result.update(connection_state="unconfigured", configured_mode=settings["mode"],
                      effective_mode="manual", endpoint=settings["endpoint"])
    elif (str(row["endpoint"]) != settings["endpoint"]
          or str(row["configured_mode"]) != settings["mode"]
          or "credential_fingerprint" not in row.keys()
          or str(row["credential_fingerprint"] or "") != expected_fingerprint):
        result.update(connection_state="unknown", configured_mode=settings["mode"],
                      effective_mode="manual", endpoint=settings["endpoint"], error=None)
    else:
        current_route_revision = _route_revision(settings)
        if current_route_revision is not None:
            try:
                recorded_route_revision = int(route_row[0]) if route_row else None
            except (TypeError, ValueError):
                recorded_route_revision = None
            if recorded_route_revision != current_route_revision:
                result.update(connection_state="unknown", configured_mode=settings["mode"],
                              effective_mode="manual", endpoint=settings["endpoint"], error=None)
    return result


def _season(start_month: str) -> dict[str, Any]:
    year = int(start_month[:4]) if re.fullmatch(r"\d{4}-\d{2}", start_month or "") else dt.date.today().year
    month = int(start_month[5:7]) if re.fullmatch(r"\d{4}-\d{2}", start_month or "") else dt.date.today().month
    label = "冬" if month <= 3 else "春" if month <= 6 else "夏" if month <= 9 else "秋"
    return {"year": year, "season": label, "seasonLabel": f"{year}年{label}", "select": True}


def _group_rank(group: str, config: dict[str, Any]) -> int:
    text = _normalize(group)
    for index, item in enumerate(config.get("torrentPolicy", {}).get("resourceGroups", [])):
        for value in [item.get("name"), item.get("id"), *(item.get("aliases") or [])]:
            token = _normalize(str(value or ""))
            if token and (token in text or text in token):
                return index
    return 9999


def _policy_eligibility_and_rank(title: str, group: str, source_class: str,
                                 resolution: int | None, config: dict[str, Any]) -> tuple[bool, int]:
    policy = config.get("torrentPolicy", {})
    resolution_label = f"{resolution}p" if resolution else "unknown"
    eligible = torrent_policy_eligible(policy, source_class, group, resolution_label, "unknown",
                                       text=title, ui_language=serial_profile_language(config))
    matches = serial_group_matches(title, group, serial_profile_language(config))
    rank = min((int(item["order"]) for item in matches), default=_group_rank(group, config))
    return eligible, rank


def _policy_eligibility_reason(title: str, group: str, source_class: str,
                               resolution: int | None, config: dict[str, Any]) -> tuple[str, str]:
    """Return the same policy decision dimensions used by torrent_policy_eligible."""
    policy = config.get("torrentPolicy", {})
    allow = policy.get("allowUnlisted", {})
    resolution_label = f"{resolution}p" if resolution else "unknown"
    if not option_enabled(policy.get("contentClasses"), source_class, allow.get("sourceClass", True)):
        return "source_class_disabled", source_family(policy, source_class)
    if not option_enabled(policy.get("resolutions"), canonical_resolution(resolution_label),
                          allow.get("resolution", True)):
        return "resolution_disabled", source_family(policy, source_class)
    family = source_family(policy, source_class)
    if family == "archive":
        return ("eligible" if archive_group_enabled(policy, group) else "resource_group_disabled"), family
    if family == "serial":
        language = serial_profile_language(config)
        matches = serial_group_matches(title, group, language)
        enabled = (any(serial_rule_enabled(policy, language, match["id"]) for match in matches)
                   if matches else bool(allow.get("resourceGroup", False)))
        return ("eligible" if enabled else "resource_group_disabled"), family
    if not resource_group_enabled(policy.get("resourceGroups"), group, allow.get("resourceGroup", False)):
        return "resource_group_disabled", family
    if not option_enabled(policy.get("subtitles"), "unknown", allow.get("subtitle", True)):
        return "subtitle_disabled", family
    return "eligible", family


def _resource_fields(title: str) -> tuple[str, int | None, int | None]:
    source = SOURCE_CLASS.search(title)
    raw = (source.group(1) if source else "webrip").casefold().replace("-", "").replace("_", "").replace(" ", "")
    source_class = {"bd": "bdrip", "bdrip": "bdrip", "dvd": "dvdrip", "dvdrip": "dvdrip",
                    "webdl": "webrip", "webrip": "webrip", "hdtvrip": "tvrip", "tvrip": "tvrip"}.get(raw, "webrip")
    resolution = RESOLUTION.search(title)
    episode = EPISODE.search(title)
    number = int(next(value for value in episode.groups() if value)) if episode else None
    return source_class, int(resolution.group(1)) if resolution else None, number


def search(db_path: Path, anime_id: int, config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    key = _secret()
    if not key:
        raise ValueError("Ani-RSS API key is not configured")
    provider_endpoint = settings["endpoint"]
    provider_fingerprint = _credential_fingerprint(key, provider_endpoint)
    provider_route_revision = _route_revision(settings)
    client = _client(config, key); stamp = utcnow()
    poll_minutes = max(5, int(config.get("components", {}).get("discovery", {}).get("pollMinutes", 30)))
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=max(24, poll_minutes / 30))).replace(microsecond=0).isoformat()
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        db.row_factory = sqlite3.Row; migrate(db)
        work = db.execute("SELECT id,bgm_id,title_ja,title_zh_hans,title_en,start_month FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not work:
            raise ValueError("anime work not found")
        db.execute("""INSERT INTO ani_rss_search_state(anime_id,last_attempt_at,last_success_at,result_count,error_text) VALUES(?,?,NULL,0,NULL)
            ON CONFLICT(anime_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,error_text=NULL""", (anime_id, stamp))
        names = [str(work[key] or "").strip() for key in ("title_zh_hans", "title_ja", "title_en")]
        names = list(dict.fromkeys(name for name in names if name))
    def record_failure(exc: BaseException) -> None:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as failure_db, failure_db:
            migrate(failure_db)
            failure_db.execute(
                "UPDATE ani_rss_search_state SET error_text=? WHERE anime_id=?",
                (f"{type(exc).__name__}: {exc}", anime_id),
            )

    def remote_call(path: str, *, expected_type: type[Any] | None = None, **kwargs: Any) -> Any:
        try:
            result = client.call(path, **kwargs)
            if expected_type is not None and result is not None and not isinstance(result, expected_type):
                raise RuntimeError(f"Ani-RSS {path} returned an unsupported payload")
            return result
        except (OSError, ValueError, RuntimeError, urllib.error.URLError, httpx.RequestError) as exc:
            record_failure(exc)
            raise

    def mapping_list(value: Any, label: str) -> list[dict[str, Any]]:
        try:
            return _strict_mapping_list(value, label)
        except RuntimeError as exc:
            record_failure(exc)
            raise

    found: dict[str, dict[str, Any]] = {}
    for name in names:
        data = remote_call("mikan", expected_type=dict, params={"text": name},
                           body=_season(str(work["start_month"] or "")), timeout=60) or {}
        for week in mapping_list(data.get("weeks"), "mikan.weeks"):
            for item in mapping_list(week.get("items"), "mikan.weeks[].items"):
                url = str(item.get("url") or "")
                if url:
                    found[url] = item
        if found:
            break
    resources: list[dict[str, Any]] = []
    for item_url, item in found.items():
        groups = mapping_list(
            remote_call("mikanGroup", expected_type=list, params={"url": item_url}, timeout=60) or [],
            "mikanGroup",
        )
        for group in groups:
            label = str(group.get("label") or "").strip() or "Other"
            rss = str(group.get("rss") or "").strip()
            items = mapping_list(group.get("items"), "mikanGroup[].items")
            if not rss:
                continue
            episodes = [ep for entry in items for ep in [_resource_fields(str(entry.get("title") or ""))[2]] if ep is not None]
            combined_title = " ".join(str(entry.get("title") or "") for entry in items)
            source_class, resolution, _ = _resource_fields(combined_title)
            eligible, policy_rank = _policy_eligibility_and_rank(combined_title, label, source_class, resolution, config)
            payload = {"rss": rss, "type": "mikan", "bgmUrl": group.get("bgmUrl") or item.get("bgmUrl"),
                       "subgroup": label, "workTitle": item.get("title"), "items": items[:40]}
            rid = "ar-" + hashlib.sha256(("follow\0" + rss).encode("utf-8")).hexdigest()[:24]
            resources.append({"resource_id": rid, "kind": "follow", "title": f"{label} · {item.get('title') or names[0]}",
                              "group": label, "source": source_class, "resolution": resolution,
                              "first": min(episodes) if episodes else None, "last": max(episodes) if episodes else None,
                              "count": len(items), "bytes": sum(int(entry.get("size") or 0) for entry in items),
                              "eligible": eligible, "policyRank": policy_rank, "payload": payload})
            for entry in items:
                entry_title = str(entry.get("title") or "")
                if not COLLECTION.search(entry_title) or not entry.get("torrent"):
                    continue
                source_class, resolution, _ = _resource_fields(entry_title)
                torrent_url = str(entry["torrent"])
                collection_payload = {**payload, "torrentUrl": torrent_url, "torrentTitle": entry_title}
                collection_id = "ar-" + hashlib.sha256(("collection\0" + torrent_url).encode("utf-8")).hexdigest()[:24]
                collection_eligible, collection_rank = _policy_eligibility_and_rank(
                    entry_title, label, source_class, resolution, config)
                resources.append({"resource_id": collection_id, "kind": "collection", "title": entry_title,
                                  "group": label, "source": source_class, "resolution": resolution,
                                  "first": None, "last": None, "count": 1, "bytes": int(entry.get("size") or 0),
                                  "eligible": collection_eligible, "policyRank": collection_rank,
                                  "payload": collection_payload})
    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        return (0 if item["kind"] == "collection" else 1, int(item.get("policyRank", 9999)),
                -(item["resolution"] or 0), -item["count"], item["title"])
    resources.sort(key=rank)
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        migrate(db)
        current_state = db.execute(
            "SELECT endpoint,credential_fingerprint,connection_state FROM ani_rss_state WHERE singleton=1"
        ).fetchone()
        current_route_revision = _route_revision(settings)
        if (current_state and (str(current_state[0]) != provider_endpoint
                or str(current_state[1] or "") != provider_fingerprint)
                or current_route_revision != provider_route_revision):
            return {"animeId": anime_id, "found": 0, "eligible": 0, "stale": True}
        db.execute("DELETE FROM ani_rss_resource WHERE anime_id=?", (anime_id,))
        for index, item in enumerate(resources):
            db.execute("INSERT INTO ani_rss_resource VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (item["resource_id"], anime_id, "ani-rss", item["kind"], item["title"], item["group"],
                        item["source"], item["resolution"], None, item["first"], item["last"], item["count"],
                        item["bytes"], 1 if item["eligible"] else 0, f"{index:08d}",
                        json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":")), stamp, expires))
        db.execute("UPDATE ani_rss_search_state SET last_success_at=?,result_count=?,error_text=NULL WHERE anime_id=?",
                   (stamp, len(resources), anime_id))
    return {"animeId": anime_id, "found": len(resources), "eligible": sum(1 for item in resources if item["eligible"])}


def resources(db_path: Path, anime_id: int, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if config is not None and not state_available(state(db_path, config)):
        return []
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_resource'").fetchone():
            return []
        rows = db.execute("SELECT * FROM ani_rss_resource WHERE anime_id=? AND expires_at>=? ORDER BY rank_key,resource_id",
                          (anime_id, utcnow())).fetchall()
        region_enabled = _work_region_enabled(db, anime_id, config) if config is not None else True
        result = []
        for index, row in enumerate(rows, 1):
            eligible = bool(row["eligible"]) and region_enabled
            reason, family = ("eligible", "other")
            if config is not None:
                reason, family = _policy_eligibility_reason(
                    str(row["title"] or ""), str(row["resource_group"] or ""),
                    str(row["source_class"] or "unknown"), row["resolution"], config)
            if not region_enabled:
                reason = "region_disabled"
            elif eligible:
                reason = "eligible"
            elif reason == "eligible":
                reason = "policy_excluded"
            result.append({"resourceId": row["resource_id"], "provider": "ani-rss", "kind": row["resource_kind"],
                           "title": row["title"], "resourceGroup": row["resource_group"],
                           "sourceClass": row["source_class"], "sourceFamily": family,
                           "resolution": row["resolution"], "sequenceFirst": row["sequence_first"],
                           "sequenceLast": row["sequence_last"], "itemCount": row["item_count"],
                           "totalBytes": row["total_bytes"], "eligible": eligible,
                           "eligibilityReason": reason, "effectiveRank": index})
        return result


def subscriptions_for_anime(db_path: Path, anime_id: int) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_subscription'").fetchone():
            return []
        rows = db.execute("""SELECT remote_id,title,enabled,subscription_kind,current_episode,total_episode,
            remote_state,last_seen_at FROM ani_rss_subscription WHERE anime_id=? AND deleted_at IS NULL
            ORDER BY enabled DESC,title""", (anime_id,)).fetchall()
        media_counts: dict[str, int] = {}
        media_episodes: dict[str, list[float]] = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_media'").fetchone():
            media_counts = {str(remote_id): int(count) for remote_id, count in db.execute(
                "SELECT remote_id,COUNT(*) FROM ani_rss_media WHERE anime_id=? GROUP BY remote_id", (anime_id,))}
            for remote_id, episode in db.execute(
                    "SELECT remote_id,episode FROM ani_rss_media WHERE anime_id=? AND episode IS NOT NULL ORDER BY remote_id,episode,filename",
                    (anime_id,)):
                media_episodes.setdefault(str(remote_id), []).append(float(episode))
        return [{"remoteId": row["remote_id"], "title": row["title"], "enabled": bool(row["enabled"]),
                 "kind": row["subscription_kind"], "currentEpisode": row["current_episode"],
                 "totalEpisode": row["total_episode"], "state": row["remote_state"],
                 "playableCount": media_counts.get(str(row["remote_id"]), 0),
                 "playableEpisodes": media_episodes.get(str(row["remote_id"]), []),
                 "lastSeenAt": row["last_seen_at"]} for row in rows]


def _load_resource(db_path: Path, resource_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
    try:
        migrate(db); row = db.execute("SELECT * FROM ani_rss_resource WHERE resource_id=? AND expires_at>=?", (resource_id, utcnow())).fetchone()
        if not row or not row["eligible"]:
            raise ValueError("Ani-RSS resource is missing, expired, or ineligible")
        return row, json.loads(row["payload_json"])
    finally:
        db.close()


def subscribe(db_path: Path, resource_id: str, config: dict[str, Any]) -> dict[str, Any]:
    row, payload = _load_resource(db_path, resource_id)
    anime_id = int(row["anime_id"]); kind = str(row["resource_kind"])
    idempotency = hashlib.sha256(f"{anime_id}\0{kind}\0{resource_id}".encode()).hexdigest()
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
        db.row_factory = sqlite3.Row; migrate(db)
        prior = db.execute("SELECT * FROM ani_rss_action WHERE idempotency_key=?", (idempotency,)).fetchone()
        if prior and prior["state"] == "submitted":
            remote_id = str(prior["remote_id"] or "")
            existing_ids = {str(item.get("id") or "") for item in _client(config).subscriptions()}
            if remote_id and remote_id in existing_ids:
                return {"actionId": prior["action_id"], "state": "submitted", "remoteId": remote_id, "idempotent": True}
            db.execute("UPDATE ani_rss_action SET state='remote_missing',updated_at=? WHERE idempotency_key=?",
                       (utcnow(), idempotency)); db.commit()
    client = _client(config); action_id = uuid.uuid4().hex; stamp = utcnow()
    try:
        ani = client.call("rssToAni", body={"url": payload["rss"], "type": payload.get("type", "mikan"),
                                             "bgmUrl": payload.get("bgmUrl"), "subgroup": payload.get("subgroup"),
                                             "enable": True}, timeout=90)
        if not isinstance(ani, dict):
            raise RuntimeError("Ani-RSS did not return a subscription object")
        remote_id = str(ani.get("id") or "")
        existing = {str(item.get("id")): item for item in client.subscriptions()}
        same = next((item for item in existing.values() if str(item.get("url") or "") == str(ani.get("url") or "")), None)
        if same:
            remote_id = str(same.get("id")); state_value = "submitted"
        elif kind == "follow":
            client.call("addAni", body=ani, timeout=90); state_value = "submitted"
        else:
            torrent_url = str(payload.get("torrentUrl") or "")
            parsed = urllib.parse.urlparse(torrent_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("invalid Ani-RSS collection torrent URL")
            request = urllib.request.Request(torrent_url, headers={"User-Agent": f"AnimeMachine/{__version__}"})
            with tls_support.urlopen(request, timeout=90, max_bytes=16 * 1024 * 1024) as response:
                torrent = response.read(16 * 1024 * 1024 + 1)
            if len(torrent) > 16 * 1024 * 1024:
                raise ValueError("Ani-RSS collection torrent exceeds the safety limit")
            client.call("startCollection", body={"torrent": base64.b64encode(torrent).decode("ascii"), "ani": ani}, timeout=180)
            state_value = "submitted"
        evidence = {"provider": "ani-rss", "resourceKind": kind, "credentialStored": False}
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            migrate(db); db.execute("INSERT OR REPLACE INTO ani_rss_action VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, idempotency, anime_id, resource_id, remote_id or None, kind, state_value,
                 stamp, utcnow(), None, json.dumps(evidence, separators=(",", ":"))))
        if kind == "follow":
            sync(db_path, config)
        return {"actionId": action_id, "state": state_value, "remoteId": remote_id or None, "idempotent": False}
    except Exception as exc:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            migrate(db); db.execute("INSERT OR REPLACE INTO ani_rss_action VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, idempotency, anime_id, resource_id, None, kind, "failed", stamp, utcnow(),
                 f"{type(exc).__name__}: {exc}", json.dumps({"provider": "ani-rss"})))
        raise


def delete_subscription(db_path: Path, remote_id: str, config: dict[str, Any], *, delete_files: bool = False) -> dict[str, Any]:
    """Delete one exact remote subscription; never broad-match or delete another item."""
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        migrate(db); row = db.execute("SELECT remote_id,title FROM ani_rss_subscription WHERE remote_id=? AND deleted_at IS NULL", (remote_id,)).fetchone()
        if not row:
            raise ValueError("managed Ani-RSS subscription not found")
    client = _client(config)
    client.call("deleteAni", params={"deleteFiles": str(bool(delete_files)).lower()}, body=[remote_id], timeout=90)
    remaining = {str(item.get("id") or "") for item in client.subscriptions()}
    if remote_id in remaining:
        raise RuntimeError("Ani-RSS did not confirm subscription deletion")
    result = sync(db_path, config)
    stamp = utcnow()
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        migrate(db)
        db.execute("UPDATE ani_rss_subscription SET remote_state='deleted',deleted_at=?,missed_successful_syncs=0 WHERE remote_id=?",
                   (stamp, remote_id))
    return {**result, "deleted": True, "remoteId": remote_id, "deleteFiles": bool(delete_files)}


def media_source(config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    path = settings["mediaPath"]
    return {"id": "ani-rss-media", "kind": "ani-rss", "enabled": bool(path),
            "path": path, "readOnly": True, "scanMinutes": settings["syncMinutes"]}


def partition_plan(db_path: Path, request: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Route each selected work to qBittorrent or Ani-RSS without duplicates."""
    requested = sorted({int(value) for value in request.get("animeIds", [])})
    explicit_local = {int(key): str(value).casefold() for key, value in (request.get("torrentSelections") or {}).items()}
    explicit_resource = {int(key): str(value) for key, value in (request.get("resourceSelections") or {}).items()}
    routing_mode = str(request.get("routingMode") or "default").strip().casefold()
    if routing_mode not in {"default", "ani-rss", "torrent"}:
        raise ValueError("routingMode must be default, ani-rss, or torrent")
    current_state = state(db_path, config)
    remote_available = state_available(current_state)
    mode = str(current_state.get("effective_mode") or current_state.get("effectiveMode") or "manual")
    local_ids: list[int] = []; remote_jobs: list[dict[str, Any]] = []; skipped_works: list[dict[str, Any]] = []
    for requested_id in requested:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
            db.row_factory = sqlite3.Row; migrate(db)
            owner = runtime_catalog.physical_anime_id(db, requested_id)
            region_enabled = _work_region_enabled(db, owner, config)
            remote_token = explicit_resource.get(requested_id) or explicit_resource.get(owner)
            local_token = explicit_local.get(requested_id) or explicit_local.get(owner)
            if remote_token and routing_mode != "torrent" and region_enabled and remote_available:
                remote = db.execute("SELECT * FROM ani_rss_resource WHERE resource_id=? AND anime_id IN (?,?) AND eligible=1 AND expires_at>=?",
                                    (remote_token, requested_id, owner, utcnow())).fetchone()
                if not remote and routing_mode == "default":
                    raise ValueError(f"Ani-RSS selection for anime {requested_id} is unavailable")
                if remote:
                    remote_jobs.append(_remote_plan_job(remote)); continue
            local: list[dict[str, Any]] = []
            if routing_mode != "ani-rss":
                local = [item for item in runtime_catalog.torrents_for_anime(db, owner, config) if item["eligible"]]
            if routing_mode == "torrent":
                if local:
                    local_ids.append(requested_id)
                else:
                    row = db.execute("SELECT id,title_ja,title_zh_hans,title_en FROM anime_work WHERE id=?", (requested_id,)).fetchone()
                    skipped_works.append(dict(row) if row else {"id": requested_id})
                continue
            if local_token and routing_mode == "default":
                local_ids.append(requested_id); continue
            has_local_collection = any(item.get("collection") for item in local)
            choose_remote = remote_available and region_enabled and (routing_mode == "ani-rss" or ((mode == "prefer" and not has_local_collection)
                                                           or (mode == "fallback" and not local)))
            remote: sqlite3.Row | None = None
            if choose_remote:
                remote = db.execute("SELECT * FROM ani_rss_resource WHERE anime_id IN (?,?) AND eligible=1 AND expires_at>=? ORDER BY rank_key,resource_id LIMIT 1",
                                    (requested_id, owner, utcnow())).fetchone()
        if choose_remote and not remote:
            # Automatic queries are allowed only after a healthy connection
            # has made the configured mode effective. If Ani-RSS disappears
            # between the last healthy snapshot and this user action, keep the
            # request on AnimeMachine's own resource path instead of surfacing
            # an optional-integration transport failure.
            try:
                search(db_path, owner, config)
            except (OSError, RuntimeError, urllib.error.URLError, httpx.RequestError):
                remote_available = False
                choose_remote = False
            if choose_remote:
                with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
                    db.row_factory = sqlite3.Row
                    remote = db.execute("SELECT * FROM ani_rss_resource WHERE anime_id IN (?,?) AND eligible=1 AND expires_at>=? ORDER BY rank_key,resource_id LIMIT 1",
                                        (requested_id, owner, utcnow())).fetchone()
        if remote:
            remote_jobs.append(_remote_plan_job(remote))
        elif routing_mode == "ani-rss":
            with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
                db.row_factory = sqlite3.Row
                local = [item for item in runtime_catalog.torrents_for_anime(db, owner, config) if item["eligible"]]
                if local:
                    local_ids.append(requested_id)
                else:
                    row = db.execute("SELECT id,title_ja,title_zh_hans,title_en FROM anime_work WHERE id=?", (requested_id,)).fetchone()
                    skipped_works.append(dict(row) if row else {"id": requested_id})
        elif local:
            local_ids.append(requested_id)
        else:
            raise ValueError(f"anime {requested_id} has no eligible local or Ani-RSS resource")
    local_request = {**request, "animeIds": local_ids}
    local_request["_skippedWorks"] = skipped_works
    return local_request, remote_jobs


def _remote_plan_job(row: sqlite3.Row) -> dict[str, Any]:
    return {"provider": "ani-rss", "resourceId": row["resource_id"], "animeId": int(row["anime_id"]),
            "resourceKind": row["resource_kind"], "resourceGroup": row["resource_group"],
            "title": row["title"], "selectedBytes": int(row["total_bytes"] or 0),
            "state": "preview"}


def attach_plan(db_path: Path, plan: dict[str, Any] | None, request: dict[str, Any],
                remote_jobs: list[dict[str, Any]], plan_dir: Path | None = None) -> dict[str, Any]:
    """Persist Ani-RSS jobs in the existing immutable preview-plan contract."""
    if not remote_jobs:
        if plan is None:
            return {"planId": "", "state": "preview", "approved": False, "jobs": [],
                    "aniRssJobs": [], "assessments": [], "totalBytes": 0, "taskCount": 0,
                    "workCount": len({int(value) for value in request.get("animeIds", [])})}
        plan.setdefault("aniRssJobs", [])
        return plan
    stamp = utcnow(); remote_bytes = sum(int(job.get("selectedBytes") or 0) for job in remote_jobs)
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        db.row_factory = sqlite3.Row; migrate(db)
        if plan is None:
            plan_id = uuid.uuid4().hex
            payload = {"schemaVersion": "1.2", "approved": False, "planId": plan_id,
                       "jobs": [], "aniRssJobs": remote_jobs, "assessments": []}
            db.execute("INSERT INTO download_plan VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                       (plan_id, "preview", 0, json.dumps(request, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False), remote_bytes, len(remote_jobs),
                        len({int(value) for value in request.get("animeIds", [])}), stamp, stamp))
            plan = {"planId": plan_id, "state": "preview", "approved": False, "jobs": [],
                    "assessments": [], "totalBytes": remote_bytes, "taskCount": len(remote_jobs),
                    "workCount": len({int(value) for value in request.get("animeIds", [])})}
        else:
            row = db.execute("SELECT plan_json,total_bytes,task_count FROM download_plan WHERE plan_id=? AND state='preview'",
                             (plan["planId"],)).fetchone()
            if not row:
                raise ValueError("local preview plan disappeared before Ani-RSS merge")
            payload = json.loads(row["plan_json"]); payload["schemaVersion"] = "1.2"; payload["aniRssJobs"] = remote_jobs
            db.execute("UPDATE download_plan SET plan_json=?,total_bytes=?,task_count=?,updated_at=? WHERE plan_id=?",
                       (json.dumps(payload, ensure_ascii=False), int(row["total_bytes"]) + remote_bytes,
                        int(row["task_count"]) + len(remote_jobs), stamp, plan["planId"]))
            plan["totalBytes"] = int(plan.get("totalBytes") or 0) + remote_bytes
            plan["taskCount"] = int(plan.get("taskCount") or 0) + len(remote_jobs)
        plan["aniRssJobs"] = remote_jobs
        if plan_dir:
            plan_dir.mkdir(parents=True, exist_ok=True)
            current = db.execute("SELECT plan_json FROM download_plan WHERE plan_id=?", (plan["planId"],)).fetchone()
            (plan_dir / f"{plan['planId']}.json").write_text(json.dumps(json.loads(current[0]), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return plan
