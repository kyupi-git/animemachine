#!/usr/bin/env python3
"""Optional Ani-RSS provider adapter.

AnimeMachine never exposes the remote credential to the browser or stores it in
SQLite.  The adapter keeps remote subscriptions, search results and actions as
an orthogonal runtime overlay; it does not overwrite the local library state.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
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
from ..torrents import runtime as runtime_catalog
from .. import __version__

from ..config.loader import (archive_group_enabled, canonical_resolution, option_enabled,
                        resource_group_enabled, serial_group_matches, serial_profile_language,
                        serial_rule_enabled, source_family, torrent_policy_eligible)


MODES = {"prefer": "prefer", "fallback": "fallback", "manual": "manual"}
COLLECTION = re.compile(r"(?i)(?:\b(?:batch|complete|collection)\b|合集|全集|全卷|全话|全話|一括)")
EPISODE = re.compile(r"(?i)(?:\b(?:ep(?:isode)?|e)[ ._-]*(\d{1,4})\b|第\s*(\d{1,4})\s*[话話集])")
RESOLUTION = re.compile(r"(?i)(2160|1080|720|576|540|480)[pi]")
SOURCE_CLASS = re.compile(r"(?i)\b(BD(?:Rip)?|DVD(?:Rip)?|WEB[ ._-]?(?:DL|Rip)|HDTV[ ._-]?Rip|TV[ ._-]?Rip)\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ani_rss_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),endpoint TEXT NOT NULL,version TEXT,
  connection_state TEXT NOT NULL,configured_mode TEXT NOT NULL,effective_mode TEXT NOT NULL,
  last_attempt_at TEXT,last_success_at TEXT,last_error TEXT,successful_generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ani_rss_subscription(
  remote_id TEXT PRIMARY KEY,anime_id INTEGER,title TEXT NOT NULL,bgm_id INTEGER,
  enabled INTEGER NOT NULL,subscription_kind TEXT NOT NULL,current_episode INTEGER,total_episode INTEGER,
  remote_media_path TEXT,remote_state TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
  missed_successful_syncs INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ani_rss_subscription_anime ON ani_rss_subscription(anime_id,remote_state);
CREATE TABLE IF NOT EXISTS ani_rss_resource(
  resource_id TEXT PRIMARY KEY,anime_id INTEGER NOT NULL,provider TEXT NOT NULL,resource_kind TEXT NOT NULL,
  title TEXT NOT NULL,resource_group TEXT,source_class TEXT,resolution INTEGER,subtitle TEXT,
  sequence_first INTEGER,sequence_last INTEGER,item_count INTEGER NOT NULL,total_bytes INTEGER,
  eligible INTEGER NOT NULL,rank_key TEXT NOT NULL,payload_json TEXT NOT NULL,
  discovered_at TEXT NOT NULL,expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ani_rss_resource_anime ON ani_rss_resource(anime_id,eligible,rank_key);
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


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = dict(config.get("components", {}).get("aniRss", {}) or {})
    mode = str(raw.get("mode") or "prefer").casefold()
    raw["mode"] = mode if mode in MODES else "prefer"
    raw["endpoint"] = str(raw.get("endpoint") or "http://127.0.0.1:7789").rstrip("/")
    raw["mediaPath"] = str(raw.get("mediaPath") or "").strip()
    raw["syncMinutes"] = max(5, int(raw.get("syncMinutes", 30)))
    raw["deleteGraceSyncs"] = max(1, int(raw.get("deleteGraceSyncs", 2)))
    return raw


def _secret() -> str:
    value = os.getenv("ANM_ANI_RSS_API_KEY", "").strip()
    secret_file = os.getenv("ANM_ANI_RSS_API_KEY_FILE", "").strip()
    if not value and secret_file and Path(secret_file).is_file():
        value = Path(secret_file).read_text(encoding="utf-8").strip()
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
        about = self.call("about") or {}
        listing = self.call("listAni") or {}
        return {"version": str(about.get("version") or ""), "subscriptions": int(listing.get("total") or 0),
                "capabilities": {"list": True, "search": True, "subscribe": True,
                                 "collection": True, "delete": True}}

    def subscriptions(self) -> list[dict[str, Any]]:
        data = self.call("listAni") or {}
        result: list[dict[str, Any]] = []
        for week in data.get("weekList") or []:
            result.extend(item for item in (week.get("items") or []) if isinstance(item, dict))
        unique: dict[str, dict[str, Any]] = {}
        for item in result:
            remote_id = str(item.get("id") or "").strip()
            if remote_id:
                unique[remote_id] = item
        return list(unique.values())


    def play_list(self, subscription_url: str) -> list[dict[str, Any]]:
        value = str(subscription_url or "").strip()
        if not value:
            raise ValueError("Ani-RSS subscription URL is unavailable")
        data = self.call("playList", body={"url": value}, timeout=60) or []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "list", "files", "data"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        raise RuntimeError("Ani-RSS playList returned an unsupported payload")

    @contextlib.contextmanager
    def stream_file(self, filename: str, range_header: str = ""):
        candidates = _file_parameter_candidates(filename)
        last_status = 502
        failed_statuses: list[int] = []
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
            manager = network_transport.client(url).stream("GET", url, headers=headers, timeout=self.timeout, follow_redirects=False)
            try:
                response = manager.__enter__()
            except Exception as exc:
                raise RemoteFileError(502) from exc
            last_status = int(response.status_code)
            if last_status not in {200, 206}:
                failed_statuses.append(last_status)
                manager.__exit__(None, None, None)
                if last_status in {400, 404, 422} and index + 1 < len(candidates):
                    continue
                raise RemoteFileError(404 if 404 in failed_statuses else last_status)
            try:
                yield response
            finally:
                manager.__exit__(None, None, None)
            return
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
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_subscription'").fetchone():
            raise ValueError("Ani-RSS playback source is unavailable")
        row = db.execute("""SELECT remote_id,title,evidence_json FROM ani_rss_subscription
            WHERE remote_id=? AND anime_id=? AND deleted_at IS NULL""", (remote_id, anime_id)).fetchone()
    if not row:
        raise ValueError("Ani-RSS playback source is unavailable")
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


def _client(config: dict[str, Any]) -> Client:
    key = _secret()
    if not key:
        raise ValueError("Ani-RSS API key is not configured")
    return Client(_settings(config)["endpoint"], key)


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


def probe(config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    if not _secret():
        return {"kind": "ani-rss", "reachable": False, "authenticated": False,
                "configuredMode": settings["mode"], "effectiveMode": "manual",
                "message": "credentials_required"}
    try:
        result = _client(config).probe()
        return {"kind": "ani-rss", "reachable": True, "authenticated": True,
                "configuredMode": settings["mode"], "effectiveMode": settings["mode"],
                "message": "ok", **result}
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        return {"kind": "ani-rss", "reachable": False, "authenticated": False,
                "configuredMode": settings["mode"], "effectiveMode": "manual",
                "message": "connection_failed", "errorType": type(exc).__name__}


def sync(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config); stamp = utcnow()
    try:
        client = _client(config)
        capability = client.probe()
        subscriptions = client.subscriptions()
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            migrate(db)
            db.execute("""INSERT INTO ani_rss_state VALUES(1,?,?,?,?,'manual',?,?,?,0)
                ON CONFLICT(singleton) DO UPDATE SET endpoint=excluded.endpoint,connection_state='error',
                configured_mode=excluded.configured_mode,effective_mode='manual',last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error""",
                (settings["endpoint"], None, "error", settings["mode"], stamp, None,
                 f"{type(exc).__name__}: {exc}"))
        return {"state": "error", "effectiveMode": "manual", "errorType": type(exc).__name__}

    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        db.row_factory = sqlite3.Row; db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=60000")
        migrate(db)
        state = db.execute("SELECT successful_generation FROM ani_rss_state WHERE singleton=1").fetchone()
        generation = int(state[0] if state else 0) + 1
        seen: set[str] = set(); mapped = 0
        for item in subscriptions:
            remote_id = str(item.get("id") or "").strip()
            if not remote_id:
                continue
            seen.add(remote_id)
            anime_id, identity = _resolve_anime(db, item)
            if anime_id is not None:
                mapped += 1
            bgm_id = _bgm_id(item.get("bgmUrl") or item.get("url"))
            evidence = {"identity": identity, "generation": generation,
                        "url": item.get("url"), "bgmUrl": item.get("bgmUrl"),
                        "subgroup": item.get("subgroup"), "season": item.get("season")}
            db.execute("""INSERT INTO ani_rss_subscription VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(remote_id) DO UPDATE SET anime_id=excluded.anime_id,title=excluded.title,
                bgm_id=excluded.bgm_id,enabled=excluded.enabled,subscription_kind=excluded.subscription_kind,
                current_episode=excluded.current_episode,total_episode=excluded.total_episode,
                remote_state=excluded.remote_state,last_seen_at=excluded.last_seen_at,
                missed_successful_syncs=0,deleted_at=NULL,evidence_json=excluded.evidence_json""",
                (remote_id, anime_id, str(item.get("title") or item.get("jpTitle") or remote_id), bgm_id,
                 1 if item.get("enable") else 0, "follow", int(item.get("currentEpisodeNumber") or 0),
                 int(item.get("totalEpisodeNumber") or 0), None,
                 "enabled" if item.get("enable") else "disabled", stamp, stamp, 0, None,
                 json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))))
        missing = list(db.execute("SELECT remote_id,missed_successful_syncs FROM ani_rss_subscription WHERE deleted_at IS NULL"))
        deleted = 0
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
        db.execute("""INSERT INTO ani_rss_state VALUES(1,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET endpoint=excluded.endpoint,version=excluded.version,
            connection_state='ready',configured_mode=excluded.configured_mode,effective_mode=excluded.effective_mode,
            last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,last_error=NULL,
            successful_generation=excluded.successful_generation""",
            (settings["endpoint"], capability.get("version"), "ready", settings["mode"], settings["mode"],
             stamp, stamp, None, generation))
        return {"state": "ready", "version": capability.get("version"), "effectiveMode": settings["mode"],
                "subscriptions": len(seen), "mapped": mapped, "deleted": deleted, "generation": generation}


def state(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_state'").fetchone()
        row = db.execute("SELECT * FROM ani_rss_state WHERE singleton=1").fetchone() if table else None
        if not row:
            return {"connectionState": "unconfigured" if not _secret() else "unknown",
                    "configuredMode": settings["mode"], "effectiveMode": "manual",
                    "credentialConfigured": bool(_secret())}
        result = {k: row[k] for k in row.keys() if k != "last_error"}
        return {**result, "credentialConfigured": bool(_secret()),
                "error": row["last_error"] and row["last_error"].split(":", 1)[0]}


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
    client = _client(config); stamp = utcnow()
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).replace(microsecond=0).isoformat()
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
        db.row_factory = sqlite3.Row; migrate(db)
        work = db.execute("SELECT id,bgm_id,title_ja,title_zh_hans,title_en,start_month FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not work:
            raise ValueError("anime work not found")
        names = [str(work[key] or "").strip() for key in ("title_zh_hans", "title_ja", "title_en")]
        names = list(dict.fromkeys(name for name in names if name))
    found: dict[str, dict[str, Any]] = {}
    for name in names[:2]:
        data = client.call("mikan", params={"text": name}, body=_season(str(work["start_month"] or "")), timeout=60) or {}
        for week in data.get("weeks") or []:
            for item in week.get("items") or []:
                url = str(item.get("url") or "")
                if url:
                    found[url] = item
        if found:
            break
    resources: list[dict[str, Any]] = []
    for item_url, item in found.items():
        groups = client.call("mikanGroup", params={"url": item_url}, timeout=60) or []
        for group in groups:
            label = str(group.get("label") or "").strip() or "Other"
            rss = str(group.get("rss") or "").strip()
            items = [entry for entry in (group.get("items") or []) if isinstance(entry, dict)]
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
        migrate(db); db.execute("DELETE FROM ani_rss_resource WHERE anime_id=?", (anime_id,))
        for index, item in enumerate(resources):
            db.execute("INSERT INTO ani_rss_resource VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (item["resource_id"], anime_id, "ani-rss", item["kind"], item["title"], item["group"],
                        item["source"], item["resolution"], None, item["first"], item["last"], item["count"],
                        item["bytes"], 1 if item["eligible"] else 0, f"{index:08d}",
                        json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":")), stamp, expires))
    return {"animeId": anime_id, "found": len(resources), "eligible": sum(1 for item in resources if item["eligible"])}


def resources(db_path: Path, anime_id: int, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_resource'").fetchone():
            return []
        rows = db.execute("SELECT * FROM ani_rss_resource WHERE anime_id=? AND expires_at>=? ORDER BY rank_key,resource_id",
                          (anime_id, utcnow())).fetchall()
        result = []
        for index, row in enumerate(rows, 1):
            eligible = bool(row["eligible"])
            reason, family = ("eligible", "other")
            if config is not None:
                reason, family = _policy_eligibility_reason(
                    str(row["title"] or ""), str(row["resource_group"] or ""),
                    str(row["source_class"] or "unknown"), row["resolution"], config)
            if eligible:
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
        return [{"remoteId": row["remote_id"], "title": row["title"], "enabled": bool(row["enabled"]),
                 "kind": row["subscription_kind"], "currentEpisode": row["current_episode"],
                 "totalEpisode": row["total_episode"], "state": row["remote_state"],
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
    mode = str(current_state.get("effective_mode") or current_state.get("effectiveMode") or "manual")
    local_ids: list[int] = []; remote_jobs: list[dict[str, Any]] = []; skipped_works: list[dict[str, Any]] = []
    for requested_id in requested:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
            db.row_factory = sqlite3.Row; migrate(db)
            owner = runtime_catalog.physical_anime_id(db, requested_id)
            remote_token = explicit_resource.get(requested_id) or explicit_resource.get(owner)
            local_token = explicit_local.get(requested_id) or explicit_local.get(owner)
            if remote_token and routing_mode != "torrent":
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
            choose_remote = routing_mode == "ani-rss" or ((mode == "prefer" and not has_local_collection)
                                                           or (mode == "fallback" and not local))
            remote: sqlite3.Row | None = None
            if choose_remote:
                remote = db.execute("SELECT * FROM ani_rss_resource WHERE anime_id IN (?,?) AND eligible=1 AND expires_at>=? ORDER BY rank_key,resource_id LIMIT 1",
                                    (requested_id, owner, utcnow())).fetchone()
        if choose_remote and not remote:
            # Automatic queries are allowed only after a healthy connection
            # has made the configured mode effective.
            search(db_path, owner, config)
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
