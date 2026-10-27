#!/usr/bin/env python3
"""Read-only external media-library inventory and conservative catalog mapping."""
from __future__ import annotations

import datetime as dt
import contextlib
import difflib
import json
import hashlib
import os
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

from ..storage import AVAILABLE, StorageUnavailableError, status_for_path
from ..storage.sleep import require_awake, StorageSleepingError
from ..storage.path_policy import is_junction
from .layout import WORK_DIRECTORY_RE
from . import media as library_media

VIDEO = library_media.VIDEO_SUFFIXES
YEAR = re.compile(r"\s*[（(](?P<year>19\d{2}|20\d{2})[）)]\s*$")
EPISODE_PATTERNS = (
    re.compile(r"(?i)(?:^|[^A-Z0-9])S(?P<season>\d{1,3})E(?P<episode>\d{1,4})(?:[^A-Z0-9]|$)"),
    re.compile(r"(?i)(?:^|[\[\s._-])(?:EP?|Episode)[ ._-]*(?P<episode>\d{1,4})(?:v\d+)?(?:[\]\s._-]|$)"),
    re.compile(r"(?:第\s*)?(?P<episode>\d{1,4})\s*(?:話|话|集)"),
)
ANI_RSS_SECTIONS = {"番剧": "tv", "劇場版": "movie", "剧场版": "movie"}
TECHNICAL_FOLDER = re.compile(r"(?i)^(?:season|series|s|part|cour|vol(?:ume)?)[ ._-]*\d+(?:[ ._-]*(?:part|cour)[ ._-]*\d+)?$")
TECHNICAL_TEXT = re.compile(r"(?ix)(?:\[[^\]]{1,120}\]|\([^)]*(?:1080|720|2160|web|bd|hevc|avc|x26)[^)]*\)|"
                            r"\b(?:1080[pi]|720p|2160p|web[ ._-]?(?:dl|rip)|bd[ ._-]?rip|hdtv|hevc|avc|x26[45]|"
                            r"10bit|8bit|multi[ ._-]?subs?|chs|cht)\b)")
SEASON_SUFFIX = re.compile(
    r"(?ix)\s*(?:第\s*(?:\d+|[一二三四五六七八九十]+)\s*(?:期|季)|season\s*\d+|s\d+)\s*$"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_media_configuration(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),sources_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_library_source(
  source_id TEXT PRIMARY KEY,kind TEXT NOT NULL,root_path TEXT NOT NULL,read_only INTEGER NOT NULL,
  scan_state TEXT NOT NULL,last_scan_at TEXT NOT NULL,evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_media_file(
  source_id TEXT NOT NULL REFERENCES external_library_source(source_id) ON DELETE CASCADE,
  absolute_path TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,anime_id INTEGER,
  match_state TEXT NOT NULL,title_hint TEXT NOT NULL,year_hint INTEGER,media_hint TEXT,
  season_number INTEGER,episode_number INTEGER,evidence_json TEXT NOT NULL,last_seen_at TEXT NOT NULL,
  PRIMARY KEY(source_id,absolute_path)
);
CREATE INDEX IF NOT EXISTS ix_external_media_anime ON external_media_file(anime_id,match_state);
"""
MATCH_RULE_VERSION = 4
_SCAN_LOCKS_GUARD = threading.Lock()
_SCAN_LOCKS: dict[str, threading.Lock] = {}


def _source_scan_lock(source_id: str) -> threading.Lock:
    with _SCAN_LOCKS_GUARD:
        return _SCAN_LOCKS.setdefault(source_id, threading.Lock())


def local_source(config: dict[str, Any]) -> dict[str, Any]:
    root = str(config.get('deployment', {}).get('libraryUncRoot') or '').strip()
    return {'id': 'local:' + hashlib.sha256(root.encode()).hexdigest()[:20],
            'kind': 'local', 'path': root, 'enabled': bool(root), 'readOnly': True, 'scanMinutes': 60}


def configured_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Media discovery is independent of download services and their credentials."""
    from ..integrations.ani_rss import media_source
    candidates = [local_source(config), *config.get('externalLibraries', []), media_source(config)]
    result, seen = [], set()
    for source in candidates:
        path = str(source.get('path') or '').strip()
        if not source.get('enabled') or not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            result.append(dict(source))
    return result


def _source_identity(kind: str, path: str) -> dict[str, str]:
    return {'kind': kind.casefold(), 'path': os.path.normcase(os.path.abspath(path))}


def reconcile_sources(db_path: Path, config: dict[str, Any] | Callable[[], dict[str, Any]]) -> bool:
    """Forget only derived inventory for intentionally removed/changed roots."""
    def desired() -> dict[str, dict[str, str]]:
        current = config() if callable(config) else config
        return {str(source['id']): _source_identity(str(source.get('kind') or 'generic'), str(source['path']))
                for source in configured_sources(current)}
    selected = desired()
    with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='external_media_configuration'").fetchone()
        previous = db.execute("SELECT sources_json FROM external_media_configuration WHERE singleton=1").fetchone() if exists else None
        if previous and json.loads(previous[0]) == selected:
            return False
        migrate(db)
        with db:
            db.execute('BEGIN IMMEDIATE')
            # Settings may have changed while this writer waited for another task.
            selected = desired()
            for source_id, kind, root in db.execute('SELECT source_id,kind,root_path FROM external_library_source').fetchall():
                if selected.get(str(source_id)) != _source_identity(str(kind), str(root)):
                    db.execute('DELETE FROM external_media_file WHERE source_id=?', (source_id,))
                    db.execute('DELETE FROM external_library_source WHERE source_id=?', (source_id,))
            db.execute("INSERT OR REPLACE INTO external_media_configuration VALUES(1,?)", (json.dumps(selected, sort_keys=True),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                db.execute("DELETE FROM metadata WHERE key='media_source_registry'")
    return True


def _source_current(db: sqlite3.Connection, source_id: str, kind: str, root: Path) -> bool:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='external_media_configuration'").fetchone():
        return True
    row = db.execute("SELECT sources_json FROM external_media_configuration WHERE singleton=1").fetchone()
    return row is None or json.loads(row[0]).get(source_id) == _source_identity(kind, str(root))


class _SupersededScan(RuntimeError):
    pass


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _catalog_generation(db: sqlite3.Connection) -> dict[str, str]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata' AND type='table'").fetchone():
        return {}
    return dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('built_at','record_count','archive_digest')"))


def normalize(value: str | None) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value or "").casefold() if ch.isalnum())


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    from . import corrections
    corrections.migrate(db)


def _title_hint(root: Path, path: Path, kind: str) -> tuple[str, int | None, str | None]:
    relative = path.relative_to(root)
    section = relative.parts[0] if relative.parts else ""
    if kind == "ani-rss" and section in ANI_RSS_SECTIONS and len(relative.parts) >= 2:
        # Support both section/letter/title and section/title/Season N layouts.
        folders = list(relative.parts[1:-1])
        raw = next((part for part in folders
                    if len(part.strip()) > 1 and not TECHNICAL_FOLDER.fullmatch(part.strip())),
                   path.parent.name)
        media = ANI_RSS_SECTIONS[section]
    else:
        raw = path.parent.name
        media = None
    catalog_name = _catalog_directory_hint(raw)
    if catalog_name is not None:
        return catalog_name[0], catalog_name[1], media
    match = YEAR.search(raw)
    year = int(match.group("year")) if match else None
    title = YEAR.sub("", raw).strip()
    return title, year, media


def _episode_hint(name: str) -> tuple[int | None, int | None]:
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(name)
        if match:
            season = match.groupdict().get("season")
            return int(season) if season else None, int(match.group("episode"))
    return None, None


def _catalog_directory_hint(value: str) -> tuple[str, int | None] | None:
    match = WORK_DIRECTORY_RE.fullmatch(value)
    if match is None:
        return None
    year = re.match(r"((?:19|20)\d{2})(?:_|-|$)", match.group("date"))
    return match.group("title"), int(year[1]) if year else None


def _clean_title(value: str) -> tuple[str, int | None]:
    catalog_name = _catalog_directory_hint(value)
    if catalog_name is not None:
        return catalog_name
    match = YEAR.search(value)
    year = int(match.group("year")) if match else None
    value = YEAR.sub("", value)
    value = TECHNICAL_TEXT.sub(" ", value)
    value = re.sub(r"(?i)(?:^|[\s._-])S\d{1,3}E\d{1,4}.*$", " ", value)
    value = re.sub(r"(?i)(?:^|[\s._-])(?:EP?|Episode)[ ._-]*\d{1,4}.*$", " ", value)
    value = re.sub(r"(?:第\s*)?\d{1,4}\s*(?:話|话|集).*$", " ", value)
    return re.sub(r"[\s._-]+$", "", value).strip(" ._-[]【】"), year


def _media_hint(relative: Path) -> str | None:
    text = "/".join(relative.parts).casefold()
    if re.search(r"(?:^|[/\\\s._-])(?:movie|movies|film|劇場版|剧场版|映画)(?:$|[/\\\s._-])", text):
        return "movie"
    if re.search(r"(?:^|[/\\\s._-])(?:ova|oad)(?:$|[/\\\s._-])", text):
        return "ova"
    if re.search(r"(?:^|[/\\\s._-])(?:web|ona)(?:$|[/\\\s._-])", text):
        return "web"
    if re.search(r"(?:^|[/\\\s._-])(?:tv|番剧|番劇)(?:$|[/\\\s._-])", text):
        return "tv"
    return None


def _title_hints(root: Path, path: Path, kind: str) -> list[tuple[str, int | None, str | None, str]]:
    relative = path.relative_to(root)
    preferred, preferred_year, preferred_media = _title_hint(root, path, kind)
    candidates: list[tuple[str, int | None, str | None, str]] = []
    media = preferred_media or _media_hint(relative)
    if preferred and not TECHNICAL_FOLDER.fullmatch(preferred):
        candidates.append((preferred, preferred_year, media, "layout"))
    inherited_year = preferred_year
    for part in reversed(relative.parts[:-1]):
        cleaned, year = _clean_title(part)
        inherited_year = inherited_year or year
        if (not cleaned or len(cleaned) == 1 or TECHNICAL_FOLDER.fullmatch(cleaned)
                or cleaned.casefold() in {"anime", "media", "video", "videos", "complete", "incomplete", "番剧", "番劇", "剧场版", "劇場版"}):
            continue
        candidates.append((cleaned, year or inherited_year, media, "ancestor"))
    filename_title, filename_year = _clean_title(path.stem)
    if filename_title:
        candidates.append((filename_title, filename_year or inherited_year, media, "filename"))
    result: list[tuple[str, int | None, str | None, str]] = []
    seen: set[tuple[str, int | None, str | None]] = set()
    for title, year, medium, source in candidates:
        key = (normalize(title), year, medium)
        if len(key[0]) >= 2 and key not in seen:
            seen.add(key); result.append((title, year, medium, source))
    return result


def _media_files(root: Path) -> Iterable[Path]:
    pending = [root]
    while pending:
        current = pending.pop()
        require_awake(current)
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    require_awake(current)
                    if is_junction(entry.path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in {"incomplete", '.anm-history', '.anm-staging',
                                '$recycle.bin', 'system volume information', '@eadir', '@recycle',
                                '#recycle', '.snapshot', 'lost+found'}:
                            pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        path = Path(entry.path)
                        if library_media.is_main_video(path.relative_to(root)):
                            yield path
        except OSError as exc:
            raise StorageUnavailableError(exc.errno or 0, f"storage unavailable: {root}") from exc


def _title_index(db: sqlite3.Connection) -> dict[str, int | set[int]]:
    # Most normalized titles identify one work. Allocate a set only for collisions.
    result: dict[str, int | set[int]] = {}
    def insert(key: str, anime_id: int) -> None:
        previous = result.get(key)
        if previous is None:
            result[key] = anime_id
        elif isinstance(previous, set):
            previous.add(anime_id)
        elif previous != anime_id:
            result[key] = {previous, anime_id}

    def add(anime_id: int, title: str | None) -> None:
        key = normalize(title)
        if key:
            insert(key, anime_id)
        base = normalize(SEASON_SUFFIX.sub("", title or ""))
        if base and base != key:
            insert(base, anime_id)

    for anime_id, title in db.execute("SELECT anime_id,title FROM anime_title"):
        add(int(anime_id), str(title))
    for anime_id, ja, zh, en in db.execute("SELECT id,title_ja,title_zh_hans,title_en FROM anime_work"):
        for title in (ja, zh, en):
            add(int(anime_id), title)
    try:
        rows = db.execute("""SELECT anime_id,title FROM ani_rss_subscription
            WHERE deleted_at IS NULL AND anime_id IS NOT NULL""")
        for anime_id, title in rows:
            add(int(anime_id), _clean_title(str(title))[0])
    except sqlite3.OperationalError:
        pass
    return result


def _resolve(db: sqlite3.Connection, index: dict[str, int | set[int]], title: str, year: int | None,
             media: str | None) -> tuple[int | None, str, dict[str, Any]]:
    query = normalize(title)
    try:
        subscription_ids = {
            int(anime_id) for anime_id, stored in db.execute(
                "SELECT anime_id,title FROM ani_rss_subscription WHERE deleted_at IS NULL AND anime_id IS NOT NULL"
            ) if normalize(_clean_title(str(stored))[0]) == query
        }
    except sqlite3.OperationalError:
        subscription_ids = set()
    if len(subscription_ids) == 1:
        resolved = next(iter(subscription_ids))
        return resolved, "verified", {"mode": "ani_rss_subscription_title", "exactTitleCandidates": [resolved]}
    matched = index.get(query, set())
    candidates = [matched] if isinstance(matched, int) else sorted(matched)
    evidence: dict[str, Any] = {"exactTitleCandidates": candidates}
    if len(candidates) > 1 and media:
        marks = ",".join("?" for _ in candidates)
        media_candidates = [int(row[0]) for row in db.execute(
            f"SELECT id FROM anime_work WHERE id IN ({marks}) AND media_code=?", (*candidates, media))]
        if media_candidates:
            candidates = media_candidates
            evidence["mediaFilteredCandidates"] = candidates
    if len(candidates) > 1 and year:
        marks = ",".join("?" for _ in candidates)
        year_candidates = [int(row[0]) for row in db.execute(
            f"SELECT id FROM anime_work WHERE id IN ({marks}) AND substr(start_month,1,4)=?", (*candidates, str(year)))]
        if year_candidates:
            candidates = year_candidates
            evidence["yearFilteredCandidates"] = candidates
    if len(candidates) == 1:
        return candidates[0], "verified", evidence
    if candidates:
        return None, "ambiguous", evidence

    # ani-rss folder titles are often a regional translation that differs by one
    # particle, word order, or a dropped subtitle. Only use a fuzzy fallback inside
    # an exact year/media bucket, require a strong unique winner, and persist the
    # score/title as evidence. This avoids global best-guess title matching.
    where: list[str] = []
    values: list[Any] = []
    if year:
        where.append("substr(w.start_month,1,4)=?")
        values.append(str(year))
    if media:
        where.append("w.media_code=?")
        values.append(media)
    if not where or len(query) < 8:
        return None, "unmatched", evidence
    rows = db.execute(f"""SELECT w.id,t.title FROM anime_work w JOIN anime_title t ON t.anime_id=w.id
        WHERE {' AND '.join(where)} UNION SELECT w.id,w.title_ja FROM anime_work w WHERE {' AND '.join(where)}
        UNION SELECT w.id,w.title_zh_hans FROM anime_work w WHERE {' AND '.join(where)}
        UNION SELECT w.id,w.title_en FROM anime_work w WHERE {' AND '.join(where)}""", values * 4)
    scored: dict[int, tuple[float, str, str]] = {}
    for anime_id, candidate_title in rows:
        candidate = normalize(candidate_title)
        if len(candidate) < 8:
            continue
        if candidate.startswith(query) or query.startswith(candidate):
            score, mode = min(len(query), len(candidate)) / max(len(query), len(candidate)), "unique_prefix"
        else:
            score, mode = difflib.SequenceMatcher(None, query, candidate, autojunk=False).ratio(), "unique_similarity"
        previous = scored.get(int(anime_id))
        if previous is None or score > previous[0]:
            scored[int(anime_id)] = (score, str(candidate_title), mode)
    ranked = sorted(((score, anime_id, matched, mode) for anime_id, (score, matched, mode) in scored.items()), reverse=True)
    if ranked:
        best_score, best_id, matched_title, mode = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        prefix_ok = mode == "unique_prefix" and best_score >= 0.62
        similarity_ok = mode == "unique_similarity" and best_score >= 0.92
        if (prefix_ok or similarity_ok) and best_score - runner_up >= 0.05:
            evidence.update({"fallbackMode": mode, "fallbackScore": round(best_score, 4),
                             "fallbackRunnerUp": round(runner_up, 4), "fallbackTitle": matched_title})
            return best_id, "verified", evidence
    # Some ani-rss season folders retain the franchise's first-air year. A very
    # strong, unique title match may override only that hint; media still agrees.
    if media:
        global_rows = db.execute("""SELECT w.id,t.title FROM anime_work w JOIN anime_title t ON t.anime_id=w.id WHERE w.media_code=?
            UNION SELECT w.id,w.title_ja FROM anime_work w WHERE w.media_code=?
            UNION SELECT w.id,w.title_zh_hans FROM anime_work w WHERE w.media_code=?
            UNION SELECT w.id,w.title_en FROM anime_work w WHERE w.media_code=?""", (media,) * 4)
        global_scores: dict[int, tuple[float, str]] = {}
        for anime_id, candidate_title in global_rows:
            candidate = normalize(candidate_title)
            if len(candidate) < 8:
                continue
            score = difflib.SequenceMatcher(None, query, candidate, autojunk=False).ratio()
            previous = global_scores.get(int(anime_id))
            if previous is None or score > previous[0]:
                global_scores[int(anime_id)] = (score, str(candidate_title))
        global_ranked = sorted(((score, anime_id, matched) for anime_id, (score, matched) in global_scores.items()), reverse=True)
        if global_ranked:
            best_score, best_id, matched_title = global_ranked[0]
            runner_up = global_ranked[1][0] if len(global_ranked) > 1 else 0.0
            if best_score >= 0.96 and best_score - runner_up >= 0.04:
                evidence.update({"fallbackMode": "unique_global_similarity", "fallbackScore": round(best_score, 4),
                                 "fallbackRunnerUp": round(runner_up, 4), "fallbackTitle": matched_title,
                                 "ignoredYearHint": year})
                return best_id, "verified", evidence
    return None, "unmatched" if not candidates else "ambiguous", evidence


def scan(db_path: Path, sources: list[dict[str, Any]],
         progress: Callable[[dict[str, int]], None] | None = None, *,
         force: bool = False, config: dict[str, Any] | None = None) -> dict[str, int]:
    """Scan read-only media sources without overlapping the same source.

    A due tick for a source whose previous pass is still running is skipped rather
    than queued. Independent sources keep scanning normally.
    """
    totals = {"sources": 0, "files": 0, "unchanged": 0, "verified": 0, "ambiguous": 0,
              "unmatched": 0, "unavailable": 0, "deferred": 0, "errors": 0}
    for source in sources:
        if not source.get("enabled"):
            continue
        source_id = str(source.get("id") or "").strip()
        lock_key = source_id or f"path:{str(source.get('path') or '').strip()}"
        source_lock = _source_scan_lock(lock_key)
        if not source_lock.acquire(blocking=force):
            totals["deferred"] += 1
            if progress:
                progress(dict(totals))
            continue
        base = dict(totals)
        def source_progress(partial: dict[str, int]) -> None:
            if progress:
                progress({key: base[key] + int(partial.get(key, 0)) for key in totals})
        try:
            partial = _scan_unlocked(db_path, [source], source_progress if progress else None,
                                    force=force, config=config)
        finally:
            source_lock.release()
        for key in totals:
            totals[key] += int(partial.get(key, 0))
        if progress:
            progress(dict(totals))
    return totals


def _scan_unlocked(db_path: Path, sources: list[dict[str, Any]],
                   progress: Callable[[dict[str, int]], None] | None = None, *,
                   force: bool = False, config: dict[str, Any] | None = None) -> dict[str, int]:
    stats = {"sources": 0, "files": 0, "unchanged": 0, "verified": 0, "ambiguous": 0,
             "unmatched": 0, "unavailable": 0, "deferred": 0, "errors": 0}
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA synchronous=NORMAL"); db.execute("PRAGMA busy_timeout=60000")
        migrate(db)
        db.execute("PRAGMA temp_store=FILE")
        db.execute("CREATE TEMP TABLE scan_file AS SELECT * FROM external_media_file WHERE 0")
        index = None
        index_generation = None
        resolution_cache: dict[tuple[str, int | None, str | None], tuple[int | None, str, dict[str, Any]]] = {}
        stamp = utcnow()
        for source in sources:
            if not source.get("enabled"):
                continue
            source_id = str(source.get("id") or "").strip()
            kind = str(source.get("kind") or "generic").strip().casefold()
            raw_root = str(source.get("path") or "").strip()
            if not source_id or not raw_root:
                stats["unavailable"] += 1
                continue
            root = Path(raw_root)
            if not _source_current(db, source_id, kind, root):
                stats['deferred'] += 1
                continue
            if source.get("readOnly") is not True:
                raise ValueError(f"external library {source_id} must be explicitly read-only")
            previous = db.execute(
                "SELECT last_scan_at,evidence_json,scan_state,root_path,kind FROM external_library_source WHERE source_id=?",
                (source_id,),
            ).fetchone()
            previous_evidence = json.loads(str(previous[1])) if previous and previous[1] else {}
            generation = _catalog_generation(db)
            if generation != index_generation:
                index = None
                resolution_cache.clear()
                index_generation = generation
            source_changed = bool(previous and (str(previous[3]) != str(root) or str(previous[4]).casefold() != kind))
            force_remap = (source_changed or int(previous_evidence.get("matchRuleVersion", 0)) != MATCH_RULE_VERSION
                           or previous_evidence.get('catalogGeneration') != generation)
            interval = max(5, int(source.get("scanMinutes", 60)))
            if previous and str(previous[2]) == "ready" and not force_remap and not force:
                with contextlib.suppress(ValueError):
                    last = dt.datetime.fromisoformat(str(previous[0]).replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=dt.timezone.utc)
                    now = dt.datetime.now(dt.timezone.utc)
                    last = last.astimezone(dt.timezone.utc)
                    if last <= now + dt.timedelta(minutes=1) and now - last < dt.timedelta(minutes=interval):
                        stats["deferred"] += 1
                        continue
            if previous and str(previous[2]) != 'ready' and not force_remap and not force:
                with contextlib.suppress(ValueError):
                    attempted = dt.datetime.fromisoformat(str(previous_evidence.get('attemptedAt', '')).replace('Z', '+00:00'))
                    if dt.timedelta(0) <= dt.datetime.now(dt.timezone.utc) - attempted < dt.timedelta(seconds=60):
                        stats['deferred'] += 1
                        continue
            try:
                require_awake(root, config)
                storage = status_for_path(root, timeout=4.0)
                storage_state = storage.state
            except StorageSleepingError:
                storage_state = 'sleeping'
            if storage_state != AVAILABLE:
                last_scan = str(previous[0]) if previous else "1970-01-01T00:00:00+00:00"
                evidence = {"followLinks": False, "matchRuleVersion": MATCH_RULE_VERSION,
                            "storageState": storage_state, "attemptedAt": stamp,
                            "catalogGeneration": generation}
                db.execute('BEGIN IMMEDIATE')
                if not _source_current(db, source_id, kind, root):
                    db.rollback()
                    stats['deferred'] += 1
                    continue
                db.execute("""INSERT INTO external_library_source VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind,root_path=excluded.root_path,
                    read_only=1,scan_state=excluded.scan_state,evidence_json=excluded.evidence_json""",
                    (source_id, kind, str(root), 1, storage_state, last_scan,
                     json.dumps(evidence, ensure_ascii=False)))
                db.commit()
                stats["unavailable"] += 1
                if progress: progress(dict(stats))
                continue
            stats["sources"] += 1
            source_counters = {key: stats[key] for key in ("files", "unchanged", "verified", "ambiguous", "unmatched")}
            db.execute('DELETE FROM scan_file')
            db.commit()
            try:
                for path in _media_files(root):
                    absolute = str(path.absolute())
                    try:
                        info = path.stat()
                    except OSError as exc:
                        raise StorageUnavailableError(exc.errno or 0, f"storage unavailable: {root}") from exc
                    prior_file = db.execute(
                        "SELECT size,mtime_ns,match_state FROM external_media_file WHERE source_id=? AND absolute_path=?",
                        (source_id, absolute),
                    ).fetchone()
                    if (not force_remap and prior_file and int(prior_file[0]) == int(info.st_size)
                            and int(prior_file[1]) == int(info.st_mtime_ns)):
                        db.execute('INSERT INTO scan_file SELECT * FROM external_media_file WHERE source_id=? AND absolute_path=?',
                                   (source_id, absolute))
                        override = db.execute('SELECT original_json,size,mtime_ns FROM external_media_override WHERE source_id=? AND absolute_path=?',
                                              (source_id,absolute)).fetchone()
                        if override and int(override[1]) == info.st_size and int(override[2]) == info.st_mtime_ns:
                            automatic = json.loads(override[0])
                            db.execute('UPDATE scan_file SET anime_id=?,match_state=?,episode_number=?,evidence_json=? WHERE source_id=? AND absolute_path=?',
                                       (automatic['anime_id'],automatic['match_state'],automatic['episode_number'],automatic['evidence_json'],source_id,absolute))
                        db.execute("UPDATE scan_file SET last_seen_at=? WHERE source_id=? AND absolute_path=?",
                                   (stamp, source_id, absolute))
                        stats["files"] += 1; stats["unchanged"] += 1; stats[str(prior_file[2])] += 1
                        if stats["files"] % 250 == 0 and progress:
                            progress(dict(stats))
                        continue
                    hints = _title_hints(root, path, kind)
                    if hints and index is None:
                        index = _title_index(db)
                    title, year, media, _ = hints[0] if hints else (path.parent.name, None, None, "fallback")
                    resolved: list[tuple[int, dict[str, Any], str]] = []
                    attempted: list[dict[str, Any]] = []
                    for hint_title, hint_year, hint_media, hint_source in hints:
                        cache_key = (normalize(hint_title), hint_year, hint_media)
                        if cache_key not in resolution_cache:
                            if len(resolution_cache) >= 4096:
                                resolution_cache.clear()
                            resolution_cache[cache_key] = _resolve(db, index or {}, hint_title, hint_year, hint_media)
                        candidate_id, candidate_state, candidate_evidence = resolution_cache[cache_key]
                        attempted.append({"title": hint_title, "year": hint_year, "media": hint_media,
                                          "source": hint_source, "state": candidate_state})
                        if candidate_id is not None and candidate_state == "verified":
                            resolved.append((candidate_id, candidate_evidence, hint_title))
                            if hint_source == "layout":
                                break
                    identities = {item[0] for item in resolved}
                    if len(identities) == 1:
                        anime_id, match_state = identities.pop(), "verified"
                        chosen = next(item for item in resolved if item[0] == anime_id)
                        evidence = {**chosen[1], "hintCandidates": attempted, "chosenTitle": chosen[2]}
                    elif len(identities) > 1:
                        anime_id, match_state, evidence = None, "ambiguous", {"hintCandidates": attempted, "candidateIds": sorted(identities)}
                    else:
                        anime_id, match_state, evidence = None, "unmatched", {"hintCandidates": attempted}
                    season_number, episode_number = _episode_hint(path.name)
                    stats["files"] += 1
                    stats[match_state] += 1
                    db.execute("INSERT INTO scan_file VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (source_id, absolute, int(info.st_size), int(info.st_mtime_ns), anime_id, match_state, title, year, media,
                         season_number, episode_number, json.dumps(evidence, ensure_ascii=False), stamp))
                    if stats["files"] % 250 == 0 and progress:
                        progress(dict(stats))
                # Filesystem reads only update the temporary snapshot. Hold the
                # Catalog write lock solely for the completed publication.
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                current = _catalog_generation(db)
                if current != generation or not _source_current(db, source_id, kind, root):
                    raise _SupersededScan('media source or catalog changed during scan')
                db.execute("""INSERT OR REPLACE INTO external_library_source VALUES(?,?,?,?,?,?,?)""",
                           (source_id, kind, str(root), 1, 'ready', stamp,
                            json.dumps({'followLinks': False, 'matchRuleVersion': MATCH_RULE_VERSION,
                                        'catalogGeneration': generation, 'storageState': AVAILABLE})))
                db.execute('DELETE FROM external_media_file WHERE source_id=?', (source_id,))
                db.execute('INSERT INTO external_media_file SELECT * FROM scan_file')
                from . import corrections
                corrections.apply_overrides(db, source_id=source_id, refresh_original=True)
                db.commit()
            except _SupersededScan:
                db.rollback()
                for counter, value in source_counters.items():
                    stats[counter] = value
                stats['deferred'] += 1
            except (StorageUnavailableError, OSError):
                db.rollback()
                for counter, value in source_counters.items():
                    stats[counter] = value
                last_scan = str(previous[0]) if previous else "1970-01-01T00:00:00+00:00"
                db.execute('BEGIN IMMEDIATE')
                if not _source_current(db, source_id, kind, root):
                    db.rollback()
                    stats['deferred'] += 1
                    continue
                db.execute("""INSERT INTO external_library_source VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind,root_path=excluded.root_path,
                    read_only=1,scan_state='unavailable',evidence_json=excluded.evidence_json""",
                    (source_id, kind, str(root), 1, "unavailable", last_scan,
                     json.dumps({"followLinks": False, "matchRuleVersion": MATCH_RULE_VERSION,
                                 "storageState": "unavailable", "attemptedAt": stamp,
                                 "catalogGeneration": generation})))
                db.commit()
                stats["unavailable"] += 1
            if progress: progress(dict(stats))
        db.commit()
    return stats


def status(db: sqlite3.Connection, anime_id: int) -> dict[str, Any] | None:
    try:
        cursor = db.execute("""SELECT e.*,s.kind,s.scan_state AS source_scan_state FROM external_media_file e
            JOIN external_library_source s USING(source_id)
            WHERE e.anime_id=? AND e.match_state='verified' ORDER BY e.source_id,e.absolute_path""", (anime_id,))
        columns = [str(item[0]) for item in cursor.description or ()]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    episodes = sorted({int(row["episode_number"]) for row in rows if row["episode_number"] is not None})
    directories: dict[str, dict[str, Any]] = {}
    for row in rows:
        directory = str(Path(str(row["absolute_path"])).parent)
        key = str(row["source_id"])
        local = row['kind'] == 'local'
        target = directories.setdefault(key, {
            "path": directory, "state": "existing" if local else "external", "origin": 'preexisting_local' if local else f"external:{row['source_id']}",
            "sourceKind": str(row["kind"]), "inspectionMode": "local_inventory" if local else "external_readonly",
            "bytes": 0, "fileCount": 0,
        })
        # Subdirectories/discs are subdivisions of this work in this source.
        # Playback still selects only files matched to anime_id; a shared parent
        # is a display/filter boundary, never evidence for another work's files.
        target['path'] = os.path.commonpath((target['path'], directory))
        target["bytes"] += int(row["size"])
        target["fileCount"] += 1
    return {
        "state": "existing" if any(row['kind'] == 'local' for row in rows) else "external", "managed": False,
        "inspectionMode": "local_inventory" if any(row['kind'] == 'local' for row in rows) else "external_readonly",
        "storageState": "available" if all(str(row["source_scan_state"]) == "ready" for row in rows) else "unavailable",
        "targets": sorted(directories.values(), key=lambda item: (item["origin"], item["path"].casefold())),
        "observedFiles": len(rows), "observedEpisodes": episodes,
    }
