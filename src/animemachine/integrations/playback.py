#!/usr/bin/env python3
"""Safe, player-neutral season playlists for locally available anime media."""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from . import ani_rss
from ..storage import AVAILABLE, status_for_path
from ..storage import path_policy

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ogm"}
SUBTITLE_SUFFIXES = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".sub"}
BONUS = re.compile(
    r"(?i)(?:^|[/\\\s._\-\[\](){}])(?:ncop|nced|op(?:ening)?|ed(?:ing)?|cm|pv|menu|preview|trailer|"
    r"promotional|sp|mini(?:ature)?(?:[ ._-]+character)?[ ._-]+anime|picture[ ._-]+drama|"
    r"short[ ._-]+anime|映像特典|予告|特典映像|番宣|ミニアニメ|ピクチャードラマ)"
    r"(?:\d{1,3})?(?:[/\\\s._\-\[\](){}]|$)"
)
EPISODE_PATTERNS = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,4})(?:[ ._-]*E(?P<episode2>\d{1,4}))?\b"),
    re.compile(r"(?i)(?:^|[\[\s._-])(?:EP?|Episode)[ ._-]*(?P<episode>\d{1,4})(?:v\d+)?(?:[\]\s._-]|$)"),
    re.compile(r"(?:第\s*)?(?P<episode>\d{1,4})(?:\.\d+)?\s*(?:話|话|集)"),
    re.compile(r"(?:^|[\[\s._-])(?P<episode>\d{1,3})(?:v\d+)?(?:[\]\s._-]|$)"),
)


@dataclasses.dataclass(frozen=True)
class MediaLocator:
    source_type: str
    local_path: Path | None = None
    remote_filename: str | None = None
    name: str = ""
    extension: str = ""
    size: int = 0
    remote_id: str = ""

    @classmethod
    def local(cls, path: Path, size: int = 0) -> "MediaLocator":
        return cls("local", local_path=path, name=path.name, extension=path.suffix.lstrip(".").casefold(), size=size)

    @classmethod
    def remote(cls, filename: str, name: str, extension: str, size: int, remote_id: str) -> "MediaLocator":
        return cls("ani-rss", remote_filename=filename, name=name, extension=extension.casefold(), size=size, remote_id=remote_id)


@dataclasses.dataclass(frozen=True)
class PlaybackItem:
    locator: MediaLocator
    title: str
    episode: float | None
    bytes: int
    origin: str

    @property
    def path(self) -> Path:
        if self.locator.source_type != "local" or self.locator.local_path is None:
            raise ValueError("remote playback item has no local path")
        return self.locator.local_path

    @property
    def name(self) -> str:
        return self.locator.name


class PlaybackDiagnostics:
    """Small in-memory view of active/recent HTTP playback transfers."""

    def __init__(self, maximum: int = 24) -> None:
        self.maximum = max(4, int(maximum))
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def begin(self, token: str, locator: MediaLocator, requested_range: str) -> None:
        now = time.time()
        with self._lock:
            item = self._items.get(token)
            if item is None:
                item = {
                    "token": token[:8], "name": locator.name, "source": locator.source_type,
                    "range": requested_range or "full", "resumeCount": 0, "upstream": "local" if locator.source_type == "local" else "connecting",
                    "bytes": 0, "rateBps": 0.0, "startedAt": now, "updatedAt": now, "state": "active",
                }
                self._items[token] = item
                self._order.insert(0, token)
            else:
                item.update(range=requested_range or "full", updatedAt=now, state="active", startedAt=now, bytes=0, rateBps=0.0)
            if requested_range:
                item["resumeCount"] = int(item.get("resumeCount", 0)) + 1
            self._trim_locked()

    def resume(self, token: str) -> None:
        with self._lock:
            item = self._items.get(token)
            if item is not None:
                item["resumeCount"] = int(item.get("resumeCount", 0)) + 1
                item["updatedAt"] = time.time()

    def upstream(self, token: str, value: str) -> None:
        with self._lock:
            item = self._items.get(token)
            if item is not None:
                item["upstream"] = str(value)
                item["updatedAt"] = time.time()

    def transfer(self, token: str, byte_count: int) -> None:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return
            item["bytes"] = int(item.get("bytes", 0)) + max(0, int(byte_count))
            elapsed = max(.001, time.time() - float(item.get("startedAt") or time.time()))
            item["rateBps"] = float(item["bytes"]) / elapsed
            item["updatedAt"] = time.time()

    def finish(self, token: str, state: str = "complete") -> None:
        with self._lock:
            item = self._items.get(token)
            if item is not None:
                item["state"] = state
                item["updatedAt"] = time.time()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._items[token]) for token in self._order if token in self._items]

    def _trim_locked(self) -> None:
        while len(self._order) > self.maximum:
            token = self._order.pop()
            self._items.pop(token, None)


class MediaTokenRegistry:
    """Opaque sliding-idle sessions; one active episode renews the whole queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}

    def issue_many(self, locators: list[MediaLocator], idle_seconds: int, maximum_seconds: int) -> list[str]:
        self.prune()
        now = time.time()
        session_id = secrets.token_urlsafe(18)
        tokens = [secrets.token_urlsafe(24) for _ in locators]
        with self._lock:
            self._sessions[session_id] = {
                "idle": idle_seconds,
                "expires": now + idle_seconds,
                "hardExpires": now + maximum_seconds,
                "locators": dict(zip(tokens, locators)),
            }
            self._tokens.update((token, session_id) for token in tokens)
        return tokens

    def resolve(self, token: str) -> MediaLocator | None:
        with self._lock:
            session_id = self._tokens.get(token)
            session = self._sessions.get(session_id or "")
            now = time.time()
            if not session or session["expires"] < now or session["hardExpires"] < now:
                self._remove_locked(session_id)
                return None
            session["expires"] = min(now + session["idle"], session["hardExpires"])
            return session["locators"].get(token)

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            expired = [key for key, session in self._sessions.items()
                       if session["expires"] < now or session["hardExpires"] < now]
            for key in expired:
                self._remove_locked(key)

    def _remove_locked(self, session_id: str | None) -> None:
        session = self._sessions.pop(session_id or "", None)
        if session:
            for token in session["locators"]:
                self._tokens.pop(token, None)


class PlaylistTokenRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, dict[str, Any]] = {}

    def issue(self, body: bytes, anime_id: int, idle_seconds: int, maximum_seconds: int) -> str:
        self.prune()
        now = time.time()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = {"body": body, "animeId": anime_id, "idle": idle_seconds,
                                   "expires": now + idle_seconds, "hardExpires": now + maximum_seconds}
        return token

    def resolve(self, token: str) -> tuple[bytes, int] | None:
        with self._lock:
            item = self._tokens.get(token)
            now = time.time()
            if not item or item["expires"] < now or item["hardExpires"] < now:
                self._tokens.pop(token, None)
                return None
            item["expires"] = min(now + item["idle"], item["hardExpires"])
            return item["body"], int(item["animeId"])

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            expired = [token for token, item in self._tokens.items()
                       if item["expires"] < now or item["hardExpires"] < now]
            for token in expired:
                self._tokens.pop(token, None)


def _episode(path: Path, fallback: float | None = None, expected_count: int | None = None) -> float | None:
    if fallback is not None:
        return fallback
    name = path.stem
    for index, pattern in enumerate(EPISODE_PATTERNS):
        match = pattern.search(name)
        if match:
            value = float(match.group("episode"))
            # A bare number surrounded by separators is weak evidence: it is
            # frequently part of an episode title (for example
            # "44 - CLOUDY BEACH").  Explicit Ep/SxxE/話 patterns remain
            # authoritative; a bare value far outside the work's known run is
            # left unlabeled instead of inventing an episode number.
            if index == len(EPISODE_PATTERNS) - 1 and expected_count and value > expected_count + 3:
                continue
            return value
    return fallback


def _episode_label(value: float) -> str:
    if value.is_integer():
        return f"Ep{int(value):02d}"
    return f"Ep{value:g}"


def _natural(value: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))


def _is_main_video(path: Path, file_kind: str | None = None) -> bool:
    if path.suffix.casefold() not in VIDEO_SUFFIXES or BONUS.search(path.as_posix()):
        return False
    return not file_kind or file_kind == "main_video"


def _default_main_queue(items: list[PlaybackItem]) -> list[PlaybackItem]:
    """Choose one default file per episode without conflating equal-sized media."""
    origin_rank = {"managed": 3, "external": 2, "ani-rss": 2, "preexisting": 1}
    numbered: dict[float, list[PlaybackItem]] = {}
    unnumbered: list[PlaybackItem] = []
    for item in items:
        (numbered.setdefault(item.episode, []) if item.episode is not None else unnumbered).append(item)

    selected = [max(candidates, key=lambda item: (
        origin_rank.get(item.origin, 0), item.bytes
    )) for candidates in numbered.values()]
    selected.extend(unnumbered)
    return selected


def configured_media_roots(config: dict[str, Any]) -> list[Path]:
    roots = [Path(str(config.get("deployment", {}).get("libraryUncRoot", "/Library")))]
    roots.extend(
        Path(str(source["path"])) for source in config.get("externalLibraries", [])
        if source.get("enabled") and source.get("readOnly") is True and source.get("path")
    )
    ani_rss_media = str(config.get("components", {}).get("aniRss", {}).get("mediaPath") or "").strip()
    if ani_rss_media:
        roots.append(Path(ani_rss_media))
    return roots


def ani_rss_media_path_state(config: dict[str, Any]) -> str:
    """Report whether AnimeMachine can directly read the configured Ani-RSS media root."""
    raw = str(config.get("components", {}).get("aniRss", {}).get("mediaPath") or "").strip()
    if not raw:
        return "unconfigured"
    try:
        status = status_for_path(Path(raw), timeout=4.0)
        return "available" if status.state == AVAILABLE else "unavailable"
    except OSError:
        return "unavailable"


def _under(path: Path, root: Path) -> bool:
    return path_policy.is_within(path, root)


def authorize_media_path(path: Path | str, config: dict[str, Any]) -> Path:
    return path_policy.authorize_existing(path, configured_media_roots(config))


def open_authorized_media(path: Path | str, config: dict[str, Any]):
    return path_policy.open_authorized(path, configured_media_roots(config))


def _allowed(path: Path, config: dict[str, Any]) -> bool:
    try:
        authorize_media_path(path, config)
        return True
    except path_policy.PathAuthorizationError:
        return False


def _path_key(path: Path) -> str:
    try:
        return path_policy.identity(path)
    except path_policy.PathAuthorizationError:
        return os.path.normcase(os.path.abspath(str(path)))


def _existing_rows(db: sqlite3.Connection, anime_id: int, expected_count: int | None) -> Iterable[tuple[Path, int, float | None, str, str | None]]:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_asset'").fetchone():
        for row in db.execute("""SELECT a.final_path,a.bytes,a.source_file_index,a.source_info_hash,
                                  f.file_kind
            FROM runtime_asset a JOIN runtime_work w ON w.target_unc=a.owner_path
            LEFT JOIN runtime_torrent_file f ON f.info_hash=a.source_info_hash AND f.file_index=a.source_file_index
            WHERE w.anime_id=? ORDER BY a.final_path""", (anime_id,)):
            path = Path(str(row[0]))
            yield path, int(row[1] or 0), _episode(path, expected_count=expected_count), "managed", str(row[4] or "") or None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_media_file'").fetchone():
        for row in db.execute("""SELECT absolute_path,size,episode_number FROM external_media_file
                                  WHERE anime_id=? AND match_state='verified' ORDER BY absolute_path""", (anime_id,)):
            path = Path(str(row[0]))
            yield path, int(row[1] or 0), _episode(path, float(row[2]) if row[2] is not None else None, expected_count), "external", "main_video"


def _scan_preexisting(db: sqlite3.Connection, anime_id: int, config: dict[str, Any], known: set[str], expected_count: int | None) -> Iterable[tuple[Path, int, float | None, str, str | None]]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_work'").fetchone():
        return
    for target, state in db.execute("SELECT target_unc,library_state FROM runtime_work WHERE anime_id=?", (anime_id,)):
        root = Path(str(target))
        if state not in {"existing", "downloading", "queued"} or not _allowed(root, config):
            continue
        storage = status_for_path(root, timeout=4.0)
        if storage.state != AVAILABLE or storage.filesystem in {"cifs", "smb3", "nfs", "nfs4"}:
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            key = _path_key(path)
            if key in known or not path.is_file() or not _is_main_video(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            yield path, size, _episode(path, expected_count=expected_count), "preexisting", "main_video"


def collect_items(db_path: Path, anime_id: int, config: dict[str, Any], *, source: str = "") -> list[PlaybackItem]:
    """Return a deterministic main-feature queue without opening media payloads."""
    if source.startswith("ani-rss:"):
        remote_id = source.split(":", 1)[1].strip()
        if not remote_id:
            raise ValueError("Ani-RSS playback source is invalid")
        remote_items = ani_rss.playback_items(db_path, anime_id, config, remote_id)
        items = [PlaybackItem(
            MediaLocator.remote(item.filename, item.name, item.extension, item.size, remote_id),
            (f"{_episode_label(item.episode)} · " if item.episode is not None else "") + item.title,
            item.episode, item.size, "ani-rss",
        ) for item in remote_items]
        items.sort(key=lambda item: (item.episode is None, item.episode or 0, _natural(item.name)))
        return items

    source_root = authorize_media_path(Path(source), config) if source else None
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        work = db.execute("SELECT title_ja,episode_count FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not work:
            raise ValueError("anime not found")
        expected_count = int(work["episode_count"]) if work["episode_count"] is not None else None
        rows = list(_existing_rows(db, anime_id, expected_count))
        known = {_path_key(row[0]) for row in rows}
        rows.extend(_scan_preexisting(db, anime_id, config, known, expected_count))
    items: list[PlaybackItem] = []
    seen: set[tuple[str, int]] = set()
    for path, recorded_size, episode, origin, file_kind in rows:
        try:
            path = authorize_media_path(path, config)
        except path_policy.PathAuthorizationError:
            continue
        if source_root and not _under(path, source_root):
            continue
        if not path.is_file() or not _is_main_video(path, file_kind):
            continue
        try:
            actual_size = path.stat().st_size
        except OSError:
            continue
        if recorded_size and actual_size != recorded_size:
            continue
        identity = (_path_key(path), actual_size)
        if identity in seen:
            continue
        seen.add(identity)
        prefix = f"{_episode_label(episode)} · " if episode is not None else ""
        items.append(PlaybackItem(MediaLocator.local(path, actual_size), prefix + path.stem, episode, actual_size, origin))
    items = _default_main_queue(items)
    items.sort(key=lambda item: (item.episode is None, item.episode or 0, _natural(item.name)))
    return items


def _mapped_path(path: Path, mappings: list[dict[str, Any]]) -> str | None:
    for mapping in mappings:
        server = Path(str(mapping.get("serverPathPrefix") or ""))
        client = str(mapping.get("clientPathPrefix") or "").rstrip("/\\")
        if not client or not _under(path, server):
            continue
        relative = os.path.relpath(str(path), str(server))
        if client.startswith("\\\\"):
            return client + "\\" + str(PureWindowsPath(relative))
        return client + "/" + urllib.parse.quote(PurePosixPath(relative.replace("\\", "/")).as_posix(), safe="/:@[]!$&'()*+,;=")
    return None


def _subtitle_for(item: PlaybackItem, anime_id: int,
                  directory_cache: dict[str, list[Path]] | None = None) -> Path | None:
    if item.locator.source_type != "local":
        return None
    path = item.path
    cache = directory_cache if directory_cache is not None else {}
    nearby = [path.with_suffix(suffix) for suffix in sorted(SUBTITLE_SUFFIXES)]

    def sidecars(directory: Path) -> list[Path]:
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key not in cache:
            try:
                cache[key] = sorted(
                    (candidate for candidate in directory.iterdir()
                     if candidate.is_file() and candidate.suffix.casefold() in SUBTITLE_SUFFIXES),
                    key=lambda candidate: candidate.name.casefold(),
                )
            except OSError:
                cache[key] = []
        prefix = path.stem.casefold() + "."
        return [candidate for candidate in cache[key]
                if candidate.stem.casefold() == path.stem.casefold()
                or candidate.name.casefold().startswith(prefix)]

    nearby.extend(sidecars(path.parent))
    state_root = Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "subtitles" / "external" / str(anime_id)
    if state_root.is_dir():
        nearby.extend(sidecars(state_root))
    return next((candidate for candidate in nearby if candidate.is_file()), None)


def token_policy(config: dict[str, Any]) -> tuple[int, int]:
    policy = config.get("playback", {})
    idle = max(900, min(int(policy.get("playlistIdleSeconds", policy.get("playlistTtlSeconds", 43200))), 172800))
    maximum = max(idle, min(int(policy.get("playlistMaximumSeconds", 604800)), 2592000))
    return idle, maximum


def media_url(public_base_url: str, token: str, locator: MediaLocator) -> str:
    filename = urllib.parse.quote(locator.name or "media", safe="")
    return f"{public_base_url.rstrip('/')}/api/playback/media/{token}/{filename}"


def player_protocol_url(player: str, target: str) -> str:
    """Return the player-native handoff URI for one tokenized HTTP target."""
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("playback target must be an absolute HTTP(S) URL")
    kind = player.casefold()
    if kind in {"vlc", "potplayer"}:
        return f"{kind}://{urllib.parse.quote(target, safe=':/?&=%')}"
    if kind == "iina":
        return f"iina://weblink?url={urllib.parse.quote(target, safe='')}"
    raise ValueError("unsupported player")


def _m3u_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def playlist_payload(db_path: Path, anime_id: int, config: dict[str, Any], registry: MediaTokenRegistry,
                     public_base_url: str, *, start: int = 1, source: str = "", force_http: bool = False,
                     items: list[PlaybackItem] | None = None) -> tuple[bytes, list[PlaybackItem]]:
    queue = list(items) if items is not None else collect_items(db_path, anime_id, config, source=source)
    if not queue:
        raise ValueError("no playable media")
    if start < 1 or start > len(queue):
        raise ValueError("start is outside the playable media range")
    if start > 1:
        queue = queue[start - 1:] + queue[:start - 1]
    idle, maximum = token_policy(config)
    policy = config.get("playback", {})
    mappings = policy.get("directPathMappings", []) if policy.get("preferDirectPaths", True) and not force_http else []
    lines = ["#EXTM3U", "#PLAYLIST:AnimeMachine"]
    subtitle_directories: dict[str, list[Path]] = {}
    subtitles = [_subtitle_for(item, anime_id, subtitle_directories) for item in queue]
    mapped = [(_mapped_path(item.path, mappings) if item.locator.source_type == "local" else None) for item in queue]
    mapped_subtitles = [_mapped_path(path, mappings) if path else None for path in subtitles]
    token_locators: list[MediaLocator] = []
    media_token_indexes: list[int] = []
    subtitle_token_indexes: list[int] = []
    for index, item in enumerate(queue):
        if not mapped[index]:
            token_locators.append(item.locator); media_token_indexes.append(index)
    for index, path in enumerate(subtitles):
        if path and not mapped_subtitles[index]:
            token_locators.append(MediaLocator.local(path, path.stat().st_size)); subtitle_token_indexes.append(index)
    issued = iter(registry.issue_many(token_locators, idle, maximum)) if token_locators else iter(())
    media_tokens = {index: next(issued) for index in media_token_indexes}
    subtitle_tokens = {index: next(issued) for index in subtitle_token_indexes}
    for index, item in enumerate(queue):
        location = mapped[index] or media_url(public_base_url, media_tokens[index], item.locator)
        subtitle = subtitles[index]
        subtitle_location = mapped_subtitles[index]
        if subtitle and not subtitle_location:
            subtitle_locator = MediaLocator.local(subtitle, subtitle.stat().st_size)
            subtitle_location = media_url(public_base_url, subtitle_tokens[index], subtitle_locator)
        if subtitle_location:
            lines.append(f"#EXTVLCOPT:input-slave={subtitle_location}")
        lines.extend((f"#EXTINF:-1,{_m3u_text(item.title)}", location))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8"), queue


def episode_media(db_path: Path, anime_id: int, config: dict[str, Any], registry: MediaTokenRegistry,
                  public_base_url: str, *, source: str, episode: float | None = None, index: int | None = None) -> tuple[str, PlaybackItem]:
    items = collect_items(db_path, anime_id, config, source=source)
    selected: PlaybackItem | None = None
    if episode is not None:
        selected = next((item for item in items if item.episode is not None and abs(item.episode - episode) < 1e-9), None)
    elif index is not None and 1 <= index <= len(items):
        selected = items[index - 1]
    if selected is None:
        raise ValueError("requested episode is not playable")
    idle, maximum = token_policy(config)
    token = registry.issue_many([selected.locator], idle, maximum)[0]
    return media_url(public_base_url, token, selected.locator), selected


def media_mime(locator: MediaLocator | Path | str) -> str:
    if isinstance(locator, MediaLocator):
        name = locator.name
    elif isinstance(locator, Path):
        name = locator.name
    else:
        name = str(locator)
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def etag(path: Path, size: int, mtime_ns: int) -> str:
    raw = f"{path}:{size}:{mtime_ns}".encode("utf-8", "surrogatepass")
    return '"' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()[:12]).decode("ascii").rstrip("=") + '"'
