#!/usr/bin/env python3
"""Product runtime overlay: verified torrent/library state and immutable plans.

The Bangumi Archive database remains the work universe.  This module imports a
separate operational catalog without guessing identities, exposes read models
for the Web UI, and creates stopped-job plans without contacting qBittorrent.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..config.loader import (archive_group_enabled, canonical_resolution, option_enabled, resource_group_enabled,
                        serial_group_matches, serial_profile_language,
                        serial_rule_enabled, source_family)
from . import differential as differential_plan
from ..library import external as external_library
from ..storage import AVAILABLE, StorageUnavailableError, status_for_path


VIDEO = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm", ".flv", ".wmv"}
AUDIO = {".flac", ".wav", ".ape", ".aac", ".m4a", ".mp3", ".ac3", ".eac3", ".dts"}
SUBTITLE = {".ass", ".ssa", ".srt", ".sup", ".vtt"}
BONUS_VIDEO = re.compile(r"(?i)(?:^|[/\\\s._\-\[\](){}])(ncop|nced|cm|pv|menu|preview|trailer|promotional|映像特典|予告|特典)(?:\d{1,3})?(?:[/\\\s._\-\[\](){}]|$)")
MAL_URL = re.compile(r"myanimelist\.net/anime/(\d+)")
_STATS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATS_LOCK = threading.Lock()


OVERLAY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runtime_work(
  private_work_id INTEGER PRIMARY KEY,anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  target_unc TEXT NOT NULL UNIQUE,series_unc TEXT,directory_name TEXT NOT NULL,official_title TEXT NOT NULL,
  date_code TEXT NOT NULL,mal_id INTEGER,library_state TEXT NOT NULL,scope_state TEXT NOT NULL,
  relation_state TEXT NOT NULL,origin TEXT NOT NULL,mapping_method TEXT NOT NULL,evidence_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_torrent(
  info_hash TEXT PRIMARY KEY,torrent_path TEXT,asset_kind TEXT NOT NULL,magnet_uri TEXT,info_name TEXT,
  source_class TEXT,effective_group TEXT,language_hint TEXT,scan_state TEXT NOT NULL,scan_reason TEXT,
  file_count INTEGER,total_bytes INTEGER,torrent_created_at TEXT,created_by TEXT,release_flags_json TEXT NOT NULL,
  collection_hint INTEGER,video_height INTEGER,video_scan TEXT,bit_depth INTEGER,metadata_state TEXT NOT NULL,
  release_unit TEXT NOT NULL DEFAULT 'unknown',volume_sequence_json TEXT NOT NULL DEFAULT '[]',episode_sequence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS runtime_torrent_work(
  info_hash TEXT NOT NULL REFERENCES runtime_torrent(info_hash) ON DELETE CASCADE,
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,private_work_id INTEGER NOT NULL,
  role TEXT NOT NULL,priority_rank INTEGER,ranking_key_json TEXT,evidence_json TEXT NOT NULL,
  PRIMARY KEY(info_hash,private_work_id)
);
CREATE TABLE IF NOT EXISTS runtime_torrent_file(
  info_hash TEXT NOT NULL REFERENCES runtime_torrent(info_hash) ON DELETE CASCADE,
  file_index INTEGER NOT NULL,source_path TEXT NOT NULL,length INTEGER NOT NULL,file_kind TEXT NOT NULL,
  PRIMARY KEY(info_hash,file_index)
);
CREATE TABLE IF NOT EXISTS runtime_file_map(
  info_hash TEXT NOT NULL,file_index INTEGER NOT NULL,source_path TEXT NOT NULL,target_relative_path TEXT NOT NULL,
  length INTEGER NOT NULL,selected INTEGER NOT NULL,selection_reason TEXT NOT NULL,
  PRIMARY KEY(info_hash,file_index)
);
CREATE TABLE IF NOT EXISTS runtime_torrent_summary(
  info_hash TEXT PRIMARY KEY REFERENCES runtime_torrent(info_hash) ON DELETE CASCADE,
  manifest_text TEXT NOT NULL,manifest_count INTEGER NOT NULL,link_count INTEGER NOT NULL,
  map_count INTEGER NOT NULL,attachment_count INTEGER NOT NULL,attachment_kinds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_submission(
  info_hash TEXT PRIMARY KEY,qbt_save_path TEXT NOT NULL,category TEXT NOT NULL,tags_json TEXT NOT NULL,
  qbt_state TEXT NOT NULL,verified_at TEXT NOT NULL,plan_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_watch(
  watch_id INTEGER PRIMARY KEY AUTOINCREMENT,anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  source_info_hash TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',source_class TEXT,effective_group TEXT,
  language_hint TEXT,video_height INTEGER,video_scan TEXT,bit_depth INTEGER,release_unit TEXT NOT NULL,
  last_sequence INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  UNIQUE(anime_id,source_info_hash)
);
CREATE TABLE IF NOT EXISTS runtime_watch_match(
  watch_id INTEGER NOT NULL REFERENCES runtime_watch(watch_id) ON DELETE CASCADE,info_hash TEXT NOT NULL,
  sequence_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',detected_at TEXT NOT NULL,
  PRIMARY KEY(watch_id,info_hash)
);
CREATE TABLE IF NOT EXISTS runtime_asset(
  asset_id INTEGER PRIMARY KEY,final_path TEXT NOT NULL UNIQUE,owner_path TEXT NOT NULL,bytes INTEGER,sha256 TEXT,
  media_created_at TEXT,source_info_hash TEXT,source_file_index INTEGER,source_torrent_path TEXT,
  replacement_state TEXT NOT NULL,evidence_json TEXT NOT NULL,verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_completeness(
  anime_id INTEGER PRIMARY KEY REFERENCES anime_work(id) ON DELETE CASCADE,preferred_info_hash TEXT,
  similarity REAL NOT NULL,state TEXT NOT NULL,basis TEXT NOT NULL,observed_files INTEGER NOT NULL,
  expected_files INTEGER NOT NULL,evidence_json TEXT NOT NULL,assessed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_review(
  review_key TEXT PRIMARY KEY,kind TEXT NOT NULL,private_work_id INTEGER,info_hash TEXT,reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_plan(
  plan_id TEXT PRIMARY KEY,state TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0,request_json TEXT NOT NULL,
  plan_json TEXT NOT NULL,total_bytes INTEGER NOT NULL,task_count INTEGER NOT NULL,work_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error_text TEXT
);
CREATE INDEX IF NOT EXISTS ix_runtime_work_anime ON runtime_work(anime_id,library_state);
CREATE INDEX IF NOT EXISTS ix_runtime_torrent_work_anime ON runtime_torrent_work(anime_id,priority_rank);
CREATE INDEX IF NOT EXISTS ix_runtime_file_kind ON runtime_torrent_file(info_hash,file_kind);
CREATE INDEX IF NOT EXISTS ix_runtime_asset_owner ON runtime_asset(owner_path,source_info_hash,source_file_index);
CREATE INDEX IF NOT EXISTS ix_runtime_completeness_state ON runtime_completeness(state,similarity);
CREATE INDEX IF NOT EXISTS ix_plan_state ON download_plan(state,created_at);
CREATE INDEX IF NOT EXISTS ix_watch_state ON runtime_watch(state,anime_id);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _collision_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFKC", part).casefold().rstrip(" .")
        for part in value.replace("\\", "/").split("/")
    )


def normalize(value: str | None) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value or "").casefold() if ch.isalnum())


def file_kind(path: str) -> str:
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.casefold()
    lowered = path.casefold()
    if suffix in VIDEO:
        return "bonus_video" if BONUS_VIDEO.search(path) else "main_video"
    if suffix in AUDIO:
        return "cd_audio" if re.search(r"(?i)(?:^|[/\\])(?:cds?|ost|soundtrack)(?:[/\\]|\b)", path) else "audio"
    if suffix in SUBTITLE:
        return "subtitle"
    if re.search(r"(?i)(?:^|[/\\])scans?(?:[/\\]|\b)", path):
        return "scans"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return "images"
    if suffix in {".log", ".txt", ".md5", ".sha1", ".sha256", ".sfv", ".cue"}:
        return "metadata"
    return "other"


def migrate_overlay(db: sqlite3.Connection) -> None:
    db.executescript(OVERLAY_SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_torrent)")}
    for name, declaration in {"release_unit": "TEXT NOT NULL DEFAULT 'unknown'", "volume_sequence_json": "TEXT NOT NULL DEFAULT '[]'", "episode_sequence_json": "TEXT NOT NULL DEFAULT '[]'"}.items():
        if name not in columns:
            db.execute(f"ALTER TABLE runtime_torrent ADD COLUMN {name} {declaration}")
    differential_plan.migrate(db)
    external_library.migrate(db)


def _sequence_values(row: sqlite3.Row | dict[str, Any]) -> list[int]:
    key = "volume_sequence_json" if str(row["release_unit"]) == "volume" else "episode_sequence_json"
    try:
        return sorted({int(value) for value in json.loads(row[key] or "[]")})
    except (TypeError, ValueError, KeyError):
        return []


def ensure_completion_watch(db: sqlite3.Connection, info_hash: str) -> int:
    """Create exact-fingerprint watches after a managed episodic/volume task completes."""
    torrent = db.execute("SELECT * FROM runtime_torrent WHERE info_hash=?", (info_hash,)).fetchone()
    if not torrent or str(torrent["release_unit"]) not in {"episode", "volume"}:
        return 0
    sequence = _sequence_values(torrent)
    stamp = utcnow()
    anime_ids = [int(row[0]) for row in db.execute(
        "SELECT DISTINCT anime_id FROM runtime_torrent_work WHERE info_hash=?", (info_hash,))]
    for anime_id in anime_ids:
        db.execute("""INSERT INTO runtime_watch(anime_id,source_info_hash,state,source_class,effective_group,
          language_hint,video_height,video_scan,bit_depth,release_unit,last_sequence,created_at,updated_at)
          VALUES(?,?,'active',?,?,?,?,?,?,?,?,?,?) ON CONFLICT(anime_id,source_info_hash) DO UPDATE SET
          state='active',last_sequence=max(runtime_watch.last_sequence,excluded.last_sequence),updated_at=excluded.updated_at""",
          (anime_id, info_hash, torrent["source_class"], torrent["effective_group"], torrent["language_hint"],
           torrent["video_height"], torrent["video_scan"], torrent["bit_depth"], torrent["release_unit"],
           max(sequence, default=0), stamp, stamp))
    return len(anime_ids)


def refresh_watch_matches(db: sqlite3.Connection) -> int:
    """Match later pool items only when every release fingerprint field agrees."""
    stamp = utcnow(); inserted = 0
    watches = db.execute("SELECT * FROM runtime_watch WHERE state='active'").fetchall()
    for watch in watches:
        rows = db.execute("""SELECT DISTINCT t.* FROM runtime_torrent t JOIN runtime_torrent_work tw USING(info_hash)
          WHERE tw.anime_id=? AND t.info_hash<>? AND t.release_unit=?
          AND COALESCE(t.source_class,'')=COALESCE(?,'') AND COALESCE(t.effective_group,'')=COALESCE(?,'')
          AND COALESCE(t.language_hint,'')=COALESCE(?,'') AND COALESCE(t.video_height,-1)=COALESCE(?,-1)
          AND COALESCE(t.video_scan,'')=COALESCE(?,'') AND COALESCE(t.bit_depth,-1)=COALESCE(?,-1)""",
          (watch["anime_id"], watch["source_info_hash"], watch["release_unit"], watch["source_class"],
           watch["effective_group"], watch["language_hint"], watch["video_height"], watch["video_scan"], watch["bit_depth"])).fetchall()
        for torrent in rows:
            sequence = [value for value in _sequence_values(torrent) if value > int(watch["last_sequence"])]
            if not sequence:
                continue
            before = db.total_changes
            db.execute("INSERT OR IGNORE INTO runtime_watch_match(watch_id,info_hash,sequence_json,state,detected_at) VALUES(?,?,?,'pending',?)",
                       (watch["watch_id"], torrent["info_hash"], json.dumps(sequence), stamp))
            inserted += db.total_changes - before
    return inserted


def watches(db_path: Path) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.row_factory = sqlite3.Row; migrate_overlay(db)
        rows = db.execute("""SELECT w.*,a.title_ja,
          (SELECT count(*) FROM runtime_watch_match m WHERE m.watch_id=w.watch_id AND m.state='pending') pending_count
          FROM runtime_watch w JOIN anime_work a ON a.id=w.anime_id ORDER BY w.state,w.updated_at DESC""").fetchall()
        return [{"watchId": int(r["watch_id"]), "animeId": int(r["anime_id"]), "title": r["title_ja"],
                 "state": r["state"], "sourceClass": r["source_class"], "resourceGroup": r["effective_group"],
                 "subtitle": r["language_hint"], "resolution": r["video_height"], "releaseUnit": r["release_unit"],
                 "lastSequence": int(r["last_sequence"]), "pendingCount": int(r["pending_count"])} for r in rows]


def delete_watch(db_path: Path, watch_id: int) -> bool:
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        migrate_overlay(db)
        return bool(db.execute("DELETE FROM runtime_watch WHERE watch_id=?", (watch_id,)).rowcount)


def _offline_by_mal(path: Path | None) -> dict[int, list[dict[str, Any]]]:
    if not path or not path.is_file():
        return {}
    root = json.loads(path.read_text(encoding="utf-8-sig"))
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entry in root.get("data", []):
        for source in entry.get("sources", []):
            match = MAL_URL.search(str(source))
            if match:
                result[int(match.group(1))].append(entry)
                break
    return result


def _identity_indexes(db: sqlite3.Connection) -> tuple[dict[int, int], dict[tuple[str, str], set[int]], dict[str, set[int]], dict[int, str]]:
    bgm_to_anime = {int(row["bgm_id"]): int(row["id"]) for row in db.execute("SELECT id,bgm_id FROM anime_work")}
    by_title_year: dict[tuple[str, str], set[int]] = defaultdict(set)
    by_title: dict[str, set[int]] = defaultdict(set)
    start_by_bgm: dict[int, str] = {}
    sql = """SELECT aw.bgm_id,aw.start_month,aw.title_ja title FROM anime_work aw
             UNION ALL SELECT aw.bgm_id,aw.start_month,t.title FROM anime_work aw JOIN anime_title t ON t.anime_id=aw.id"""
    for row in db.execute(sql):
        key = normalize(row["title"])
        if not key:
            continue
        year = str(row["start_month"] or "")[:4]
        bid = int(row["bgm_id"])
        by_title_year[(key, year)].add(bid)
        by_title[key].add(bid)
        start_by_bgm[bid] = str(row["start_month"] or "")
    return bgm_to_anime, by_title_year, by_title, start_by_bgm


def _map_work(row: sqlite3.Row, bgm_to_anime: dict[int, int], by_title_year: dict[tuple[str, str], set[int]],
              by_title: dict[str, set[int]], start_by_bgm: dict[int, str], offline: dict[int, list[dict[str, Any]]]) -> tuple[int | None, str, dict[str, Any]]:
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except (TypeError, ValueError):
        evidence = {}
    direct = evidence.get("bangumiSubjectId")
    if direct is not None and int(direct) in bgm_to_anime:
        return bgm_to_anime[int(direct)], "verified_bangumi_subject_id", {"bangumiSubjectId": int(direct)}
    year = str(row["date_code"] or "")[:4]
    if not year.isdigit():
        year = ""
    title_key = normalize(row["official_title"])
    candidates = set(by_title_year.get((title_key, year), ()))
    if len(candidates) == 1:
        bid = next(iter(candidates))
        return bgm_to_anime[bid], "unique_exact_title_year", {"bangumiSubjectId": bid}
    mal_id = row["mal_id"]
    mal_candidates: set[int] = set()
    mal_any_year: set[int] = set()
    for entry in offline.get(int(mal_id), ()) if mal_id else ():
        for title in [entry.get("title"), *entry.get("synonyms", [])]:
            normalized = normalize(str(title or ""))
            mal_candidates.update(by_title_year.get((normalized, year), ()))
            mal_any_year.update(by_title.get(normalized, ()))
    if len(mal_candidates) == 1:
        bid = next(iter(mal_candidates))
        return bgm_to_anime[bid], "mal_crosslink_unique_title_year", {"bangumiSubjectId": bid, "malId": mal_id}
    if len(mal_any_year) == 1:
        bid = next(iter(mal_any_year))
        return bgm_to_anime[bid], "mal_crosslink_unique_title_archive", {
            "bangumiSubjectId": bid, "malId": mal_id, "privateDateCode": row["date_code"],
            "archiveStart": start_by_bgm.get(bid), "dateConflictRetained": True,
        }
    contained: set[int] = set()
    if len(title_key) >= 6 and year:
        for (candidate_title, candidate_year), bids in by_title_year.items():
            if candidate_year == year and len(candidate_title) >= 5 and (candidate_title in title_key or title_key in candidate_title):
                contained.update(bids)
    if len(contained) == 1:
        bid = next(iter(contained))
        return bgm_to_anime[bid], "unique_compound_title_year", {"bangumiSubjectId": bid}
    exact_any_year = set(by_title.get(title_key, ()))
    if len(exact_any_year) == 1:
        bid = next(iter(exact_any_year))
        return bgm_to_anime[bid], "unique_exact_title_archive", {"bangumiSubjectId": bid, "archiveStart": start_by_bgm.get(bid)}
    return None, "unresolved_archive_identity", {"title": row["official_title"], "dateCode": row["date_code"], "malId": mal_id}


def backfill_manifests(runtime_db: Path, manifest_json: Path) -> dict[str, int]:
    payload = json.loads(manifest_json.read_text(encoding="utf-8-sig"))
    with contextlib.closing(sqlite3.connect(runtime_db)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        # The JSON export is a compatibility/backfill source, not the source
        # of truth.  It may legitimately lag behind the incremental SQLite
        # pool index.  Replacing the table here used to erase manifests for
        # newly indexed torrents and made valid releases disappear from Web.
        inserted = 0
        for record in payload.get("records", []):
            info_hash = str(record.get("infoHash", "")).casefold()
            if not db.execute("SELECT 1 FROM torrent WHERE info_hash=?", (info_hash,)).fetchone():
                continue
            rows = [(info_hash, int(item["index"]), item["path"], int(item["length"])) for item in record.get("files", [])]
            db.executemany("INSERT OR REPLACE INTO torrent_manifest_file(info_hash,file_index,source_path,length) VALUES(?,?,?,?)", rows)
            inserted += len(rows)
        db.commit()
    return {"records": len(payload.get("records", [])), "files": inserted, "errors": len(payload.get("errors", []))}


def _work_state_signal(db: sqlite3.Connection) -> str:
    """Fingerprint mutable path state even when upgrading from an older writer.

    Current writers also touch ``verified_at``.  Including the small work-state
    projection makes a one-time upgrade from versions that did not do so
    invalidate the overlay instead of indefinitely reusing stale library data.
    """
    digest = hashlib.sha256()
    for work_id, library_state, scope_state in db.execute(
            "SELECT work_id,library_state,scope_state FROM anime_work ORDER BY work_id"):
        digest.update(f"{int(work_id)}\0{library_state or ''}\0{scope_state or ''}\n".encode("utf-8"))
    return digest.hexdigest()


def sync_overlay(metadata_db: Path, runtime_db: Path, *, offline_metadata: Path | None = None,
                 manifest_json: Path | None = None) -> dict[str, int]:
    if manifest_json and manifest_json.is_file():
        backfill_manifests(runtime_db, manifest_json)
    stamp = utcnow()
    runtime = sqlite3.connect(runtime_db)
    runtime.row_factory = sqlite3.Row
    product = sqlite3.connect(metadata_db)
    product.row_factory = sqlite3.Row
    product.execute("PRAGMA foreign_keys=ON")
    migrate_overlay(product)
    bgm_to_anime, by_title_year, by_title, start_by_bgm = _identity_indexes(product)
    offline = _offline_by_mal(offline_metadata)
    tables = {row[0] for row in runtime.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"torrent", "anime_work", "torrent_work", "torrent_resolution"}
    if not required.issubset(tables):
        raise RuntimeError("operational catalog is not migrated")
    def table_signal(table: str, stamp_column: str | None = None) -> tuple[int, str]:
        if table not in tables:
            return 0, ""
        count = int(runtime.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        latest = str(runtime.execute(f"SELECT COALESCE(MAX({stamp_column}),'') FROM {table}").fetchone()[0]) if stamp_column else ""
        return count, latest
    source_signature = json.dumps({
        "torrent": table_signal("torrent", "indexed_at"),
        "work": table_signal("anime_work", "verified_at"),
        "workStates": _work_state_signal(runtime),
        "links": table_signal("torrent_work"),
        "targets": table_signal("torrent_target_path", "updated_at"),
        "resolutions": table_signal("torrent_resolution", "updated_at"),
        "files": table_signal("torrent_manifest_file"),
        "maps": table_signal("file_map"),
        "submissions": table_signal("submission", "verified_at"),
        "assets": table_signal("asset_provenance", "verified_at"),
    }, sort_keys=True, separators=(",", ":"))
    previous_signature_row = product.execute("SELECT value FROM metadata WHERE key='runtime_source_signature'").fetchone()
    summary_ready = int(product.execute("SELECT COUNT(*) FROM runtime_torrent_summary").fetchone()[0]) == int(
        product.execute("SELECT COUNT(*) FROM runtime_torrent").fetchone()[0])
    if previous_signature_row and str(previous_signature_row[0]) == source_signature and summary_ready:
        result = {
            "metadataWorks": int(product.execute("SELECT COUNT(*) FROM anime_work").fetchone()[0]),
            "mappedRuntimeWorks": int(product.execute("SELECT COUNT(*) FROM runtime_work").fetchone()[0]),
            "unmappedRuntimeWorks": int(product.execute("SELECT COUNT(*) FROM runtime_review WHERE kind='work_identity'").fetchone()[0]),
            "torrents": int(product.execute("SELECT COUNT(*) FROM runtime_torrent").fetchone()[0]),
            "verifiedLinks": int(product.execute("SELECT COUNT(*) FROM runtime_torrent_work").fetchone()[0]),
            "manifestFiles": int(product.execute("SELECT COUNT(*) FROM runtime_torrent_file").fetchone()[0]),
            "submissions": int(product.execute("SELECT COUNT(*) FROM runtime_submission").fetchone()[0]),
            "watchMatches": 0, "snapshotReused": 1,
        }
        product.close(); runtime.close()
        return result
    work_map: dict[int, int] = {}
    work_rows = list(runtime.execute("SELECT * FROM anime_work ORDER BY work_id"))
    mapped_rows: list[tuple[Any, ...]] = []
    reviews: list[tuple[Any, ...]] = []
    submitted_work_ids = {int(row[0]) for row in runtime.execute("SELECT DISTINCT tw.work_id FROM torrent_work tw JOIN submission s ON s.info_hash=tw.info_hash")}
    for row in work_rows:
        anime_id, method, evidence = _map_work(row, bgm_to_anime, by_title_year, by_title, start_by_bgm, offline)
        if anime_id is None:
            reviews.append((f"work:{row['work_id']}", "work_identity", row["work_id"], None,
                            "operational work cannot be uniquely linked to Bangumi Archive", json.dumps(evidence, ensure_ascii=False), stamp))
            continue
        work_map[int(row["work_id"])] = anime_id
        origin = "managed_submission" if int(row["work_id"]) in submitted_work_ids else ("preexisting_local" if row["library_state"] == "existing" else "catalog")
        mapped_rows.append((row["work_id"], anime_id, row["target_unc"], row["series_unc"], row["directory_name"],
                            row["official_title"], row["date_code"], row["mal_id"], row["library_state"], row["scope_state"],
                            row["relation_state"], origin, method, json.dumps(evidence, ensure_ascii=False), stamp))

    manifest_table = "torrent_manifest_file" in tables
    file_map_table = "file_map" in tables
    submission_table = "submission" in tables
    asset_table = "asset_provenance" in tables
    torrent_rows = list(runtime.execute("SELECT * FROM torrent ORDER BY info_hash"))
    verified_link_counts = {str(row["info_hash"]): int(row["n"]) for row in runtime.execute(
        "SELECT info_hash,COUNT(*) n FROM torrent_work WHERE mapping_state='verified' GROUP BY info_hash")}
    directory_by_work = {int(row["work_id"]): str(row["directory_name"]) for row in work_rows}
    created_by_hash = {str(row["info_hash"]): str(row["torrent_created_at"] or "") for row in torrent_rows}
    asset_kind_by_hash = {str(row["info_hash"]): str(row["asset_kind"] if "asset_kind" in row.keys() else "torrent") for row in torrent_rows}
    archive_start_by_id = {int(row[0]): str(row[1] or "") for row in product.execute("SELECT id,start_month FROM anime_work")}
    with product:
        product.execute("UPDATE download_plan SET state='stale',updated_at=? WHERE state='preview'", (stamp,))
        for table in ("runtime_asset", "runtime_submission", "runtime_torrent_summary", "runtime_file_map", "runtime_torrent_file", "runtime_torrent_work", "runtime_work", "runtime_torrent", "runtime_review"):
            product.execute(f"DELETE FROM {table}")
        product.executemany("INSERT INTO runtime_work VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mapped_rows)
        product.executemany("INSERT INTO runtime_review VALUES(?,?,?,?,?,?,?)", reviews)
        torrent_values = []
        for row in torrent_rows:
            keys = set(row.keys())
            torrent_values.append((
                row["info_hash"], row["torrent_path"], row["asset_kind"] if "asset_kind" in keys else "torrent",
                row["magnet_uri"] if "magnet_uri" in keys else None, row["info_name"], row["source_class"], row["effective_group"],
                row["language_hint"], row["scan_state"], row["scan_reason"], row["file_count"], row["total_bytes"],
                row["torrent_created_at"], row["created_by"], row["release_flags_json"] or "[]", row["collection_hint"],
                row["video_height"], row["video_scan"], row["bit_depth"], row["metadata_state"] if "metadata_state" in keys else "available",
                row["release_unit"] if "release_unit" in keys else "unknown",
                row["volume_sequence_json"] if "volume_sequence_json" in keys else "[]",
                row["episode_sequence_json"] if "episode_sequence_json" in keys else "[]"))
        product.executemany("INSERT INTO runtime_torrent VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", torrent_values)
        link_values = []
        for row in runtime.execute("SELECT * FROM torrent_work WHERE mapping_state='verified' ORDER BY info_hash,work_id"):
            anime_id = work_map.get(int(row["work_id"]))
            if anime_id is None:
                continue
            if manifest_table and asset_kind_by_hash.get(str(row["info_hash"]), "torrent") == "torrent" and not runtime.execute(
                    "SELECT 1 FROM torrent_manifest_file WHERE info_hash=? LIMIT 1", (row["info_hash"],)).fetchone():
                continue
            archive_start = archive_start_by_id.get(anime_id, "")
            date_match = re.fullmatch(r"(\d{4})-(\d{2})", archive_start)
            created = created_by_hash.get(str(row["info_hash"]), "")[:10]
            if date_match and created:
                release_floor = f"{date_match.group(1)}-{date_match.group(2)}-01"
                if created < release_floor:
                    # A torrent cannot contain a work which had not been
                    # released when the torrent itself was constructed.
                    continue
            # A collection-level identity link is not proof that this torrent
            # contains files for every child (notably announced future works).
            # Publish availability only when an exact child partition has at
            # least one selected file. Single-work torrents use the manifest.
            if verified_link_counts.get(str(row["info_hash"]), 0) > 1 and file_map_table:
                directory = directory_by_work[int(row["work_id"])].casefold().replace("\\", "/")
                has_partition = runtime.execute("""SELECT 1 FROM file_map WHERE info_hash=? AND selected=1
                    AND (lower(replace(target_relative_path,'\\','/'))=? OR lower(replace(target_relative_path,'\\','/')) LIKE ?)
                    LIMIT 1""", (row["info_hash"], directory, directory + "/%")).fetchone() is not None
                if not has_partition:
                    continue
            link_values.append((row["info_hash"], anime_id, row["work_id"], row["role"], row["priority_rank"], row["ranking_key_json"], row["evidence_json"]))
        product.executemany("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", link_values)
        if manifest_table:
            product.executemany("INSERT INTO runtime_torrent_file VALUES(?,?,?,?,?)", (
                (row["info_hash"], row["file_index"], row["source_path"], row["length"], file_kind(row["source_path"]))
                for row in runtime.execute("SELECT * FROM torrent_manifest_file ORDER BY info_hash,file_index")))
        if file_map_table:
            product.executemany("INSERT INTO runtime_file_map VALUES(?,?,?,?,?,?,?)", (
                (row["info_hash"], row["file_index"], row["source_path"], row["target_relative_path"], row["length"], row["selected"], row["selection_reason"])
                for row in runtime.execute("SELECT * FROM file_map ORDER BY info_hash,file_index")))
        if submission_table:
            product.executemany("INSERT INTO runtime_submission VALUES(?,?,?,?,?,?,?)", (
                (row["info_hash"], row["qbt_save_path"], row["category"], row["tags_json"], row["qbt_state"], row["verified_at"], row["plan_revision"])
                for row in runtime.execute("SELECT * FROM submission ORDER BY info_hash")))
        if asset_table:
            product.executemany("INSERT INTO runtime_asset VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                (row["asset_id"], row["final_path"], row["owner_path"], row["bytes"], row["sha256"], row["media_created_at"],
                 row["source_info_hash"], row["source_file_index"], row["source_torrent_path"], row["replacement_state"],
                 row["evidence_json"], row["verified_at"],)
                for row in runtime.execute("SELECT * FROM asset_provenance ORDER BY asset_id")))
        product.execute("""INSERT INTO runtime_torrent_summary
            SELECT t.info_hash,COALESCE(f.manifest_text,''),COALESCE(f.manifest_count,0),
                   COALESCE(l.link_count,0),COALESCE(m.map_count,0),
                   COALESCE(f.attachment_count,0),COALESCE(f.attachment_kinds,0)
            FROM runtime_torrent t
            LEFT JOIN (
              SELECT info_hash,GROUP_CONCAT(source_path,' ') manifest_text,COUNT(*) manifest_count,
                     SUM(file_kind IN ('cd_audio','scans','bonus_video','images')) attachment_count,
                     COUNT(DISTINCT CASE WHEN file_kind IN ('cd_audio','scans','bonus_video','images') THEN file_kind END) attachment_kinds
              FROM runtime_torrent_file GROUP BY info_hash
            ) f USING(info_hash)
            LEFT JOIN (SELECT info_hash,COUNT(DISTINCT anime_id) link_count FROM runtime_torrent_work GROUP BY info_hash) l USING(info_hash)
            LEFT JOIN (SELECT info_hash,COUNT(*) map_count FROM runtime_file_map GROUP BY info_hash) m USING(info_hash)""")
        metadata = {
            "runtime_synced_at": stamp,
            "runtime_work_count": str(len(mapped_rows)),
            "runtime_unmapped_work_count": str(len(reviews)),
            "runtime_torrent_count": str(len(torrent_rows)),
            "runtime_verified_link_count": str(product.execute("SELECT COUNT(*) FROM runtime_torrent_work").fetchone()[0]),
            "runtime_manifest_file_count": str(product.execute("SELECT COUNT(*) FROM runtime_torrent_file").fetchone()[0]),
            "runtime_submission_count": str(product.execute("SELECT COUNT(*) FROM runtime_submission").fetchone()[0]),
            "runtime_source_signature": source_signature,
        }
        product.executemany("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", metadata.items())
    with product:
        watch_matches = refresh_watch_matches(product)
    result = {
        "metadataWorks": product.execute("SELECT COUNT(*) FROM anime_work").fetchone()[0],
        "mappedRuntimeWorks": len(mapped_rows), "unmappedRuntimeWorks": len(reviews),
        "torrents": len(torrent_rows), "verifiedLinks": product.execute("SELECT COUNT(*) FROM runtime_torrent_work").fetchone()[0],
        "manifestFiles": product.execute("SELECT COUNT(*) FROM runtime_torrent_file").fetchone()[0],
        "submissions": product.execute("SELECT COUNT(*) FROM runtime_submission").fetchone()[0],
        "watchMatches": watch_matches,
    }
    product.close(); runtime.close()
    return result


def _attachment_summary(db: sqlite3.Connection, target_unc: str) -> dict[str, int]:
    rows = db.execute("SELECT final_path FROM runtime_asset WHERE owner_path=?", (target_unc,))
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[file_kind(row["final_path"])] += 1
    return dict(sorted(counts.items()))


def completeness_for_anime(db: sqlite3.Connection, anime_id: int) -> dict[str, Any] | None:
    anime_id = physical_anime_id(db, anime_id)
    try:
        row = db.execute("SELECT * FROM runtime_completeness WHERE anime_id=?", (anime_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def physical_anime_id(db: sqlite3.Connection, anime_id: int) -> int:
    """Resolve logical supplements/split cours to one physical owner."""
    current = int(anime_id)
    seen: set[int] = set()
    try:
        while current not in seen:
            seen.add(current)
            row = db.execute("SELECT physical_owner_anime_id FROM anime_work WHERE id=?", (current,)).fetchone()
            if not row or row[0] is None:
                break
            current = int(row[0])
    except sqlite3.OperationalError:
        return int(anime_id)
    return current


def library_status(db: sqlite3.Connection, anime_id: int) -> dict[str, Any]:
    anime_id = physical_anime_id(db, anime_id)
    works = [dict(row) for row in db.execute("SELECT * FROM runtime_work WHERE anime_id=? ORDER BY target_unc", (anime_id,))]
    external = external_library.status(db, anime_id)
    if not works:
        return external or {"state": "not_in_library_catalog", "managed": False, "inspectionMode": "none", "targets": []}
    targets = []
    managed_any = False
    complete_managed_bdrip = False
    assessment = completeness_for_anime(db, anime_id)
    for work in works:
        managed = work["origin"] == "managed_submission"
        managed_any |= managed
        item = {"path": work["target_unc"], "seriesPath": work["series_unc"], "state": work["library_state"], "origin": work["origin"]}
        if managed:
            complete_managed_bdrip |= bool(db.execute("""SELECT 1 FROM runtime_torrent_work tw JOIN runtime_torrent t USING(info_hash)
                WHERE tw.private_work_id=? AND lower(COALESCE(t.source_class,''))='bdrip'
                  AND (t.release_unit='collection' OR t.collection_hint=1) LIMIT 1""", (work["private_work_id"],)).fetchone())
            expected_files: list[sqlite3.Row] = []
            for linked in db.execute("SELECT info_hash FROM runtime_torrent_work WHERE private_work_id=?", (work["private_work_id"],)):
                info_hash = linked["info_hash"]
                link_count = db.execute("SELECT COUNT(*) FROM runtime_torrent_work WHERE info_hash=?", (info_hash,)).fetchone()[0]
                directory = str(work["directory_name"]).casefold().replace("\\", "/")
                for mapped in db.execute("""SELECT m.*,f.file_kind FROM runtime_file_map m
                        JOIN runtime_torrent_file f ON f.info_hash=m.info_hash AND f.file_index=m.file_index
                        WHERE m.info_hash=? AND m.selected=1""", (info_hash,)):
                    relative = str(mapped["target_relative_path"]).casefold().replace("\\", "/")
                    if link_count == 1 or relative == directory or relative.startswith(directory + "/"):
                        expected_files.append(mapped)
            observed_rows = list(db.execute("SELECT * FROM runtime_asset WHERE owner_path=?", (work["target_unc"],)))
            observed_main = sum(file_kind(row["final_path"]) == "main_video" for row in observed_rows)
            expected_main = sum(row["file_kind"] == "main_video" for row in expected_files)
            item.update({
                "inspectionMode": "managed_provenance", "expectedFiles": len(expected_files),
                "expectedMainMedia": expected_main, "observedFiles": len(observed_rows),
                "observedMainMedia": int(observed_main or 0),
                "missingMainMedia": max(0, expected_main - int(observed_main or 0)),
                "attachments": _attachment_summary(db, work["target_unc"]),
            })
        else:
            if work["library_state"] == "existing" and assessment:
                item.update({"inspectionMode": str(assessment.get("basis") or "metadata_distribution"),
                             "expectedFiles": int(assessment.get("expected_files") or 0),
                             "observedFiles": int(assessment.get("observed_files") or 0),
                             "similarity": float(assessment.get("similarity") or 0),
                             "completenessState": str(assessment.get("state") or "incomplete")})
            else:
                item["inspectionMode"] = "not_inspected_preexisting" if work["library_state"] == "existing" else "catalog_state_only"
        targets.append(item)
    if external:
        targets.extend(external["targets"])
    state_order = {"existing": 9, "downloading": 8, "queued": 7, "external": 6, "placeholder": 5, "occupied_review": 4, "absent": 1}
    state = max((item["state"] for item in targets), key=lambda value: state_order.get(value, 0))
    preferred_origin = "native"
    expected_episode_row = db.execute("SELECT episode_count FROM anime_work WHERE id=?", (anime_id,)).fetchone()
    expected_episodes = int(expected_episode_row[0] or 0) if expected_episode_row else 0
    external_complete = bool(external and expected_episodes and len(external.get("observedEpisodes", [])) >= expected_episodes)
    if external_complete and managed_any and not complete_managed_bdrip:
        # A complete external WEB edition remains logically preferred while a
        # managed BDRip is still an episode/volume increment. Both stay visible.
        state = "external"; preferred_origin = "external"
    return {"state": state, "managed": managed_any, "preferredOrigin": preferred_origin,
            "coexistingExternal": bool(external and managed_any), "externalComplete": external_complete,
            "inspectionMode": "mixed" if len({x["inspectionMode"] for x in targets}) > 1 else targets[0]["inspectionMode"], "targets": targets}


def acquisition_fingerprint(item: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a release stream that may be safely continued/stitched."""
    subtitle = None if item.get("sourceFamily") == "archive" else item.get("subtitle")
    return (item.get("sourceFamily"), item.get("sourceClass"), item.get("resourceGroup"), subtitle,
            item.get("resolution"), item.get("scan"), item.get("bitDepth"), item.get("releaseUnit"))


def torrents_for_anime(db: sqlite3.Connection, anime_id: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    anime_id = physical_anime_id(db, anime_id)
    classes = {str(key).casefold(): bool(value) for key, value in config["torrentPolicy"]["contentClasses"].items()}
    groups = config["torrentPolicy"].get("resourceGroups", [])
    resolutions = config["torrentPolicy"].get("resolutions", {})
    subtitles = config["torrentPolicy"].get("subtitles", {})
    serial_language = serial_profile_language(config)
    allow_unlisted = config["torrentPolicy"].get("allowUnlisted", {})
    rows = db.execute("""SELECT t.*,tw.priority_rank,tw.role,tw.private_work_id,
             rw.date_code,rw.scope_state,rw.relation_state,aw.start_month archive_start,aw.episode_count archive_episode_count,
             EXISTS(SELECT 1 FROM runtime_submission s WHERE s.info_hash=t.info_hash) submitted,
             COALESCE(s.manifest_text,(SELECT GROUP_CONCAT(f.source_path,' ') FROM runtime_torrent_file f WHERE f.info_hash=t.info_hash)) manifest_text,
             COALESCE(s.manifest_count,(SELECT COUNT(*) FROM runtime_torrent_file f WHERE f.info_hash=t.info_hash)) manifest_count,
             COALESCE(s.link_count,(SELECT COUNT(DISTINCT x.anime_id) FROM runtime_torrent_work x WHERE x.info_hash=t.info_hash)) link_count,
             COALESCE(s.map_count,(SELECT COUNT(*) FROM runtime_file_map m WHERE m.info_hash=t.info_hash)) map_count,
             COALESCE(s.attachment_count,(SELECT COUNT(*) FROM runtime_torrent_file f WHERE f.info_hash=t.info_hash AND f.file_kind IN ('cd_audio','scans','bonus_video','images'))) attachment_count,
             COALESCE(s.attachment_kinds,(SELECT COUNT(DISTINCT f.file_kind) FROM runtime_torrent_file f WHERE f.info_hash=t.info_hash AND f.file_kind IN ('cd_audio','scans','bonus_video','images'))) attachment_kinds
        FROM runtime_torrent_work tw JOIN runtime_torrent t ON t.info_hash=tw.info_hash
        LEFT JOIN runtime_torrent_summary s ON s.info_hash=t.info_hash
        JOIN runtime_work rw ON rw.private_work_id=tw.private_work_id
        JOIN anime_work aw ON aw.id=tw.anime_id
        WHERE tw.anime_id=? ORDER BY COALESCE(tw.priority_rank,999999),t.info_hash""", (anime_id,))
    result = []
    for row in rows:
        enabled = option_enabled(classes, row["source_class"], allow_unlisted.get("sourceClass", True))
        family = source_family(config["torrentPolicy"], row["source_class"])
        manifest_text = row["manifest_text"] or ""
        serial_matches = serial_group_matches(
            f"{row['torrent_path']} {row['info_name']} {manifest_text}",
            row["effective_group"], serial_language) if family == "serial" else []
        if family == "serial":
            group_enabled = any(serial_rule_enabled(config["torrentPolicy"], serial_language, match["id"]) for match in serial_matches)
            if not serial_matches:
                group_enabled = bool(allow_unlisted.get("resourceGroup", False))
        else:
            group_enabled = archive_group_enabled(config["torrentPolicy"], row["effective_group"])
        resolution = f"{row['video_height']}{row['video_scan'] or 'p'}" if row["video_height"] else "unknown"
        resolution_enabled = option_enabled(resolutions, canonical_resolution(resolution), allow_unlisted.get("resolution", True))
        # Archive releases intentionally ignore subtitle presence. Serial
        # releases use the language-aware group+subtitle profiles above.
        subtitle_enabled = True if family in {"archive", "serial"} else option_enabled(subtitles, row["language_hint"], allow_unlisted.get("subtitle", True))
        manifest_count = int(row["manifest_count"] or 0)
        link_count = int(row["link_count"] or 0)
        exact_partition = link_count == 1 or int(row["map_count"] or 0) == manifest_count
        long_running_blocked = bool(config.get("scope", {}).get("excludeLongRunningContinuous", False)) and row["scope_state"] == "excluded_long_running"
        scope_ok = row["scope_state"] != "excluded" and not long_running_blocked
        eligible = bool(enabled and group_enabled and resolution_enabled and subtitle_enabled and scope_ok and row["scan_state"] != "reject" and row["metadata_state"] == "available" and manifest_count and exact_partition)
        if not enabled:
            reason = "source_class_disabled"
        elif not group_enabled:
            reason = "resource_group_disabled"
        elif not resolution_enabled:
            reason = "resolution_disabled"
        elif not subtitle_enabled:
            reason = "subtitle_disabled"
        elif not scope_ok:
            reason = "scope_excluded"
        elif not manifest_count or not exact_partition:
            reason = "manifest_or_partition_unavailable"
        else:
            reason = "eligible"
        result.append({
            "infoHash": row["info_hash"], "name": row["info_name"], "sourceClass": row["source_class"],
            "storageDirectory": str(Path(str(row["torrent_path"])).parent) if row["torrent_path"] else "",
            "resourceGroup": row["effective_group"], "subtitle": row["language_hint"], "priorityRank": row["priority_rank"],
            "sourceFamily": family, "serialLanguage": serial_language,
            "serialProfileIds": [match["id"] for match in serial_matches],
            "serialGroupRank": min((match["order"] for match in serial_matches), default=9999),
            "totalBytes": row["total_bytes"], "fileCount": row["file_count"], "creationDate": row["torrent_created_at"],
            "flags": json.loads(row["release_flags_json"] or "[]"),
            "collection": bool(row["collection_hint"] or str(row["release_unit"] or "").casefold() == "collection"),
            "releaseUnit": str(row["release_unit"] or "unknown"),
            "volumeSequence": json.loads(row["volume_sequence_json"] or "[]"),
            "episodeSequence": json.loads(row["episode_sequence_json"] or "[]"),
            "resolution": row["video_height"], "scan": row["video_scan"], "bitDepth": row["bit_depth"],
            "submitted": bool(row["submitted"]), "mappedWorkCount": int(link_count), "manifestAvailable": manifest_count > 0,
            "partitionVerified": bool(exact_partition),
            "attachmentCount": int(row["attachment_count"] or 0), "attachmentKinds": int(row["attachment_kinds"] or 0),
            "archiveStart": row["archive_start"], "expectedEpisodes": int(row["archive_episode_count"] or 0),
            "eligible": eligible, "eligibilityReason": reason,
        })

    def contiguous(values: set[int]) -> int:
        current = 0
        while current + 1 in values:
            current += 1
        return current

    def release_phase(start_month: str | None, episode_count: int) -> tuple[str, str | None]:
        match = re.fullmatch(r"(\d{4})-(\d{2})", str(start_month or ""))
        if not match or episode_count <= 0:
            return "unknown", None
        year, month = int(match.group(1)), int(match.group(2))
        following = dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        # Month-only metadata intentionally uses month end, then allows two
        # extra weeks beyond weekly broadcasting to avoid premature penalties.
        estimated_end = following - dt.timedelta(days=1) + dt.timedelta(days=max(0, episode_count - 1) * 7 + 14)
        today = dt.datetime.now(dt.timezone.utc).date()
        if today <= estimated_end:
            return "airing", estimated_end.isoformat()
        if today <= estimated_end + dt.timedelta(days=56):
            return "finishing", estimated_end.isoformat()
        if today <= estimated_end + dt.timedelta(days=420):
            return "serial_archive_ready", estimated_end.isoformat()
        return "disc_archive_ready", estimated_end.isoformat()

    eligible_items = [item for item in result if item["eligible"]]
    global_episode_latest = max((max(item.get("episodeSequence") or [0]) for item in eligible_items), default=0)
    global_volume_latest = max((max(item.get("volumeSequence") or [0]) for item in eligible_items), default=0)
    strategies: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in eligible_items:
        strategies[acquisition_fingerprint(item)].append(item)
    for members in strategies.values():
        episodes = {int(value) for item in members for value in item.get("episodeSequence", []) if int(value) > 0}
        volumes = {int(value) for item in members for value in item.get("volumeSequence", []) if int(value) > 0}
        episode_contiguous = contiguous(episodes)
        volume_contiguous = contiguous(volumes)
        expected = max(int(item.get("expectedEpisodes") or 0) for item in members)
        phase, estimated_end = release_phase(members[0].get("archiveStart"), expected)
        latest = max(episodes, default=0) if episodes else max(volumes, default=0)
        global_latest = global_episode_latest if episodes else global_volume_latest if volumes else 0
        lag = max(0, global_latest - latest)
        flags = {str(flag).casefold() for item in members for flag in item.get("flags", [])}
        collection = any(item.get("collection") for item in members)
        family_name = str(members[0].get("sourceFamily") or "")
        episode_complete = bool(expected and episode_contiguous >= expected)
        declared_final = bool(flags.intersection({"fin", "final", "complete", "bdbox", "bd-box"}))
        if family_name == "serial" and global_episode_latest and lag >= 2:
            status, rank = "stale", 2
        elif episode_complete or (collection and (not episodes or expected == 0 or episode_contiguous >= expected)):
            status, rank = "complete", 0
        elif declared_final and (volume_contiguous or episode_contiguous):
            status, rank = "complete", 0
        elif phase == "airing" and lag <= 1 and latest:
            status, rank = "current", 1
        elif volumes and volume_contiguous == global_volume_latest and lag <= 1:
            # Total disc count is often unavailable. This is explicitly a
            # best-known pool frontier, not a claim that every disc exists.
            status, rank = "best_known", 1
        elif latest:
            status, rank = "incomplete", 2
        else:
            status, rank = "unknown", 3
        ratio = (min(1.0, episode_contiguous / expected) if expected and episodes
                 else min(1.0, latest / global_latest) if global_latest else None)
        coverage = {
            "status": status, "rank": rank, "ratio": round(ratio, 4) if ratio is not None else None,
            "phase": phase, "estimatedBroadcastEnd": estimated_end, "expectedEpisodes": expected or None,
            "episodeFrontier": episode_contiguous or None, "volumeFrontier": volume_contiguous or None,
            "poolLatest": global_latest or None, "lag": lag,
        }
        for item in members:
            item["resourceCompleteness"] = coverage
    for item in result:
        item.setdefault("resourceCompleteness", {"status": "ineligible", "rank": 4, "ratio": None,
                                                  "phase": "unknown", "lag": 0})
    policy = config.get("torrentPolicy", {})
    def order_index(values: list[Any], value: Any) -> int:
        normalized = str(value or "unknown").casefold().replace("-", "").replace(" ", "")
        choices = [str(x).casefold().replace("-", "").replace(" ", "") for x in values]
        return choices.index(normalized) if normalized in choices else len(choices)
    archive_ids = {str(value).casefold() for value in policy.get("archiveGroupIds", [])}
    group_order = [x.get("name") for x in sorted((g for g in policy.get("resourceGroups", []) if g.get("enabled", True) and str(g.get("id", "")).casefold() in archive_ids), key=lambda g: (int(g.get("tier", 999)), int(g.get("order", 999))))]
    dimensions = policy.get("strategyOrder", [])
    content_order = policy.get("contentClassPriority", list(policy.get("contentClasses", {})))
    def release_strategy_name(item: dict[str, Any]) -> str:
        source = str(item.get("sourceClass") or "unknown").casefold()
        unit = str(item.get("releaseUnit") or "unknown").casefold()
        return {("bdrip", "collection"): "bdrip_collection", ("bdrip", "volume"): "bdrip_volume",
                ("webrip", "collection"): "webrip_collection", ("webrip", "episode"): "webrip_episode",
                ("tvrip", "collection"): "tvrip_collection", ("tvrip", "episode"): "tvrip_episode"}.get((source, unit), "other")
    def release_strategy_rank(item: dict[str, Any]) -> tuple[int, int, int]:
        name = release_strategy_name(item)
        source = str(item.get("sourceClass") or "unknown").casefold()
        unit = str(item.get("releaseUnit") or "unknown").casefold()
        return (order_index(policy.get("releaseStrategyPriority", []), name),
                order_index(content_order, source) if name == "other" else 0,
                {"collection": 0, "volume": 1, "episode": 2}.get(unit, 3) if name == "other" else 0)
    def collection_revision_name(item: dict[str, Any], flags: set[str]) -> str:
        revised = bool(flags.intersection({"fin", "rev", "reseed", "bdbox", "bd-box"}))
        return "collection_revision" if item.get("collection") and revised else "collection" if item.get("collection") else "revision" if revised else "ordinary"
    def directional_rank(first: str, preferred: str, number: int) -> int:
        if number <= 0:
            return 10**30
        return -number if first == preferred else number
    def ranking(item: dict[str, Any]) -> tuple[Any, ...]:
        flags = {str(x).casefold() for x in item.get("flags", [])}
        created = re.sub(r"\D", "", str(item.get("creationDate") or ""))
        created_number = int((created + "0" * 14)[:14] or 0)
        values = {
            "resourceCompleteness": (int(item.get("resourceCompleteness", {}).get("rank", 4)),
                                     int(item.get("resourceCompleteness", {}).get("lag", 0)),
                                     -int((item.get("resourceCompleteness", {}).get("ratio") or 0) * 10000)),
            "releaseStrategy": release_strategy_rank(item),
            "seriesCompleteness": -int(item.get("mappedWorkCount") or 0),
            "resourceGroup": (item.get("serialGroupRank", 9999) if item.get("sourceFamily") == "serial"
                              else order_index(group_order, item.get("resourceGroup"))),
            "collectionOrRevision": order_index(policy.get("collectionRevisionPriority", []), collection_revision_name(item, flags)),
            "attachmentCompleteness": (order_index(policy.get("attachmentPriority", []), "with_attachments" if item.get("attachmentCount", 0) else "without_attachments"), -int(item.get("attachmentKinds") or 0), -int(item.get("attachmentCount") or 0)),
            "sourceClass": order_index(content_order, item.get("sourceClass")),
            "resolution": order_index(policy.get("resolutionPriority", []), canonical_resolution(f"{item.get('resolution')}{item.get('scan') or 'p'}")),
            "subtitle": 0 if item.get("sourceFamily") == "archive" else order_index(policy.get("subtitlePriority", []), item.get("subtitle")),
            "bitDepth": order_index(policy.get("bitDepthPriority", []), f"{item.get('bitDepth')}bit" if item.get("bitDepth") else "unknown"),
            "torrentCreationDate": directional_rank((policy.get("creationDatePriority") or ["newest"])[0], "newest", created_number),
            "size": directional_rank((policy.get("sizePriority") or ["larger"])[0], "larger", int(item.get("totalBytes") or 0)),
        }
        return tuple(values.get(name, 999999) for name in dimensions) + (str(item.get("infoHash")),)
    result.sort(key=ranking)
    for index, item in enumerate(result, 1):
        item["effectiveRank"] = index
    return result


def runtime_stats(db_path: Path) -> dict[str, Any]:
    key = str(db_path.resolve(strict=False))
    now = time.monotonic()
    with _STATS_LOCK:
        cached = _STATS_CACHE.get(key)
        if cached and now - cached[0] < 5:
            return dict(cached[1])
    result = {
        "torrents": 0, "verifiedLinks": 0, "worksWithTorrent": 0, "libraryWorks": 0,
        "submissions": 0, "reviewItems": 0, "previewPlans": 0,
    }
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            queries = {
                "torrents": ("runtime_torrent", "SELECT COUNT(*) FROM runtime_torrent"),
                "verifiedLinks": ("runtime_torrent_work", "SELECT COUNT(*) FROM runtime_torrent_work"),
                "worksWithTorrent": ("runtime_torrent_work", "SELECT COUNT(DISTINCT anime_id) FROM runtime_torrent_work"),
                "libraryWorks": ("runtime_work", "SELECT COUNT(*) FROM runtime_work"),
                "submissions": ("runtime_submission", "SELECT COUNT(*) FROM runtime_submission"),
                "reviewItems": ("runtime_review", "SELECT COUNT(*) FROM runtime_review"),
                "previewPlans": ("download_plan", "SELECT COUNT(*) FROM download_plan WHERE state='preview'"),
            }
            for name, (table, query) in queries.items():
                if table in tables:
                    result[name] = int(db.execute(query).fetchone()[0])
    except sqlite3.OperationalError as exc:
        if not any(marker in str(exc).casefold() for marker in ("locked", "busy")):
            raise
    with _STATS_LOCK:
        _STATS_CACHE[key] = (now, result)
    return dict(result)


def verify_runtime(db_path: Path) -> dict[str, Any]:
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        migrate_overlay(db)
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(db.execute("PRAGMA foreign_key_check").fetchall())
        missing_manifest_hashes = [row[0] for row in db.execute("""SELECT DISTINCT tw.info_hash FROM runtime_torrent_work tw
            JOIN runtime_torrent t ON t.info_hash=tw.info_hash WHERE t.asset_kind='torrent'
            AND NOT EXISTS(SELECT 1 FROM runtime_torrent_file f WHERE f.info_hash=tw.info_hash)""")]
        missing_manifests = len(missing_manifest_hashes)
        impossible_dates = db.execute("""SELECT COUNT(*) FROM runtime_torrent_work tw
            JOIN runtime_torrent t ON t.info_hash=tw.info_hash JOIN anime_work aw ON aw.id=tw.anime_id
            WHERE aw.start_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
              AND t.torrent_created_at IS NOT NULL AND substr(t.torrent_created_at,1,7)<aw.start_month""").fetchone()[0]
        result = {"integrity": integrity, "foreignKeyErrors": foreign_keys,
                  "verifiedLinksWithoutManifest": missing_manifests, "missingManifestHashes": missing_manifest_hashes,
                  "torrentPredatesWorkLinks": impossible_dates,
                  **runtime_stats(db_path)}
        result["ok"] = integrity == "ok" and foreign_keys == 0 and missing_manifests == 0 and impossible_dates == 0
        return result


def _qbt_path(target_unc: str, config: dict[str, Any]) -> str:
    configured_root = str(config["deployment"]["libraryUncRoot"])
    if configured_root.startswith(("/", "\\")) and not configured_root.startswith("\\\\"):
        relative = PurePosixPath(target_unc).relative_to(PurePosixPath(configured_root))
    else:
        relative = PureWindowsPath(target_unc).relative_to(PureWindowsPath(configured_root))
    return config["deployment"]["qbtLibraryRoot"].rstrip("/") + "/" + "/".join(relative.parts)


def _create_plan_connected(db: sqlite3.Connection, config: dict[str, Any], request: dict[str, Any],
                           plan_dir: Path | None = None, *, plan_id: str | None = None,
                           replace_existing: bool = False) -> dict[str, Any]:
    library_root_value = str(config.get("deployment", {}).get("libraryUncRoot") or "").strip()
    if library_root_value:
        library_root = Path(library_root_value)
        storage = status_for_path(library_root, require_write=True, timeout=4.0)
        if storage.state != AVAILABLE:
            raise StorageUnavailableError(f"library storage unavailable: {library_root}")
    requested_ids = sorted({int(value) for value in request.get("animeIds", [])})
    anime_ids = sorted({physical_anime_id(db, value) for value in requested_ids})
    if not anime_ids:
        raise ValueError("animeIds must contain at least one work")
    plan_id = plan_id or uuid.uuid4().hex
    stamp = utcnow()
    explicit: dict[int, str] = {}
    for key, value in (request.get("torrentSelections") or {}).items():
        owner_id = physical_anime_id(db, int(key))
        normalized = str(value).casefold()
        if owner_id in explicit and explicit[owner_id] != normalized:
            raise ValueError("logical members sharing one physical season selected different torrents")
        explicit[owner_id] = normalized
    choices: list[tuple[int, dict[str, Any], sqlite3.Row]] = []
    for anime_id in anime_ids:
        status = library_status(db, anime_id)
        if status["state"] in {"queued", "downloading", "occupied_review", "deprecated", "upgrade_staged", "upgrade_blocked"}:
            raise ValueError(f"anime {anime_id} cannot be safely planned in state: {status['state']}")
        torrents = [item for item in torrents_for_anime(db, anime_id, config) if item["eligible"]]
        selected_hash = explicit.get(anime_id)
        if selected_hash:
            selected = next((item for item in torrents if item["infoHash"] == selected_hash), None)
            if not selected:
                raise ValueError(f"anime {anime_id} selected torrent is not eligible")
            torrents = [selected, *[item for item in torrents if item["infoHash"] != selected_hash]]
        if not torrents:
            raise ValueError(f"anime {anime_id} has no eligible verified torrent")
        chosen_items = [torrents[0]]
        # Episode/volume selections represent an exact release stream. Expand
        # that stream with every non-overlapping unit sharing its fingerprint;
        # the UI presents this atomic set as one multi-episode/multi-volume row.
        if torrents[0].get("releaseUnit") in {"episode", "volume"}:
            top = torrents[0]
            sequence_key = "episodeSequence" if top["releaseUnit"] == "episode" else "volumeSequence"
            seen = set(top.get(sequence_key) or [])
            for candidate in torrents[1:]:
                sequence = set(candidate.get(sequence_key) or [])
                if acquisition_fingerprint(candidate) == acquisition_fingerprint(top) and sequence and sequence.isdisjoint(seen):
                    chosen_items.append(candidate); seen.update(sequence)
        for chosen in chosen_items:
            work = db.execute("""SELECT rw.* FROM runtime_torrent_work tw JOIN runtime_work rw ON rw.private_work_id=tw.private_work_id
                                 WHERE tw.anime_id=? AND tw.info_hash=? ORDER BY rw.target_unc LIMIT 1""", (anime_id, chosen["infoHash"])).fetchone()
            if not work:
                raise ValueError(f"anime {anime_id} has no verified target path")
            choices.append((anime_id, chosen, work))
    grouped: dict[str, list[tuple[int, dict[str, Any], sqlite3.Row]]] = defaultdict(list)
    for choice in choices:
        grouped[choice[1]["infoHash"]].append(choice)
    jobs = []
    assessments = []
    for info_hash, selected_works in grouped.items():
        torrent = db.execute("SELECT * FROM runtime_torrent WHERE info_hash=?", (info_hash,)).fetchone()
        manifest = list(db.execute("SELECT * FROM runtime_torrent_file WHERE info_hash=? ORDER BY file_index", (info_hash,)))
        all_links = list(db.execute("SELECT tw.*,rw.target_unc,rw.series_unc,rw.directory_name,rw.library_state FROM runtime_torrent_work tw JOIN runtime_work rw ON rw.private_work_id=tw.private_work_id WHERE tw.info_hash=?", (info_hash,)))
        chosen_private = {int(work["private_work_id"]) for _, _, work in selected_works}
        existing_submission = db.execute("SELECT * FROM runtime_submission WHERE info_hash=?", (info_hash,)).fetchone()
        map_rows = {int(row["file_index"]): row for row in db.execute("SELECT * FROM runtime_file_map WHERE info_hash=?", (info_hash,))}
        if len(all_links) > 1 and len(map_rows) != len(manifest):
            raise ValueError(f"collection {info_hash} lacks an exact full partition")
        targets = [work["target_unc"] for _, _, work in selected_works]
        series = {work["series_unc"] for _, _, work in selected_works if work["series_unc"]}
        # A single-work torrent downloads directly into its child directory.
        # Only a verified multi-work collection uses the common series root;
        # its exact file_map then partitions files into child-relative paths.
        save_target = next(iter(series)) if len(series) == 1 and len(all_links) > 1 else targets[0]
        owner_presence: dict[str, bool | None] = {}
        for owner in ({str(link["target_unc"]) for link in all_links}
                      | {str(link["series_unc"]) for link in all_links if link["series_unc"]}):
            try:
                Path(owner).stat()
                owner_presence[owner] = True
            except (FileNotFoundError, NotADirectoryError):
                owner_presence[owner] = False
            except OSError as exc:
                raise StorageUnavailableError(exc.errno or 0, f"library storage unavailable: {library_root}") from exc
        files = []
        selected_bytes = 0
        for item in manifest:
            mapping = map_rows.get(int(item["file_index"]))
            # Compatibility for mappings created before single-work torrents
            # became child-rooted save jobs.  Those rows redundantly started
            # with the work directory, which would download into
            # ``work/work/file``.  Strip only the exact verified owner prefix;
            # multi-work collection partitions must retain it.
            if len(all_links) == 1 and mapping:
                mapping_path = str(mapping["target_relative_path"]).replace("\\", "/")
                owner_dir = str(all_links[0]["directory_name"]).replace("\\", "/").strip("/")
                path_parts = mapping_path.split("/", 1)
                if len(path_parts) == 2 and differential_plan._norm(path_parts[0]) == differential_plan._norm(owner_dir):
                    mapping = dict(mapping)
                    mapping["target_relative_path"] = path_parts[1]
            requested = len(all_links) == 1 and int(all_links[0]["private_work_id"]) in chosen_private
            if len(all_links) > 1 and mapping:
                lowered = str(mapping["target_relative_path"]).casefold().replace("\\", "/")
                if lowered == "series extras" or lowered.startswith("series extras/"):
                    requested = bool(chosen_private)
                for link in all_links:
                    directory = str(link["directory_name"]).casefold()
                    if int(link["private_work_id"]) in chosen_private and (lowered == directory or lowered.startswith(directory + "/")):
                        requested = True
                        break
            selected_before = bool(existing_submission and mapping and mapping["selected"])
            planned = differential_plan.classify(
                db, torrent=torrent, item=item, mapping=mapping, links=all_links,
                fallback=selected_works[0][2], requested=requested,
                selected_before=selected_before, plan_id=plan_id, stamp=stamp, config=config,
                owner_presence=owner_presence,
            )
            if planned["action"] in {"add_missing", "add_coexisting", "stage_replace"}:
                selected_bytes += int(item["length"])
            files.append(planned)
        group = torrent["effective_group"] or "unknown-group"
        summary = dict(sorted((action, sum(row["action"] == action for row in files)) for action in {
            "add_missing", "add_coexisting", "skip_unchanged", "stage_replace", "conflict_review", "previous_selection", "not_selected"
        }))
        assessment = {
            "infoHash": info_hash, "resourceGroup": group, "targets": targets,
            "animeIds": sorted({int(anime_id) for anime_id, _, _ in selected_works}),
            "files": files, "summary": summary, "downloadBytes": selected_bytes,
            "hasWarnings": any(row.get("warning") for row in files),
        }
        assessments.append(assessment)
        if any(row["action"] in {"add_missing", "add_coexisting", "stage_replace"} for row in files):
            jobs.append({
                "operation": "extend" if existing_submission else "create", "torrentPath": torrent["torrent_path"],
                "infoHash": info_hash, "resourceGroup": group, "savePath": existing_submission["qbt_save_path"] if existing_submission else _qbt_path(save_target, config),
                "contentLayout": "NoSubfolder", "tags": sorted({group, *config["components"]["downloadClient"]["tags"], str(torrent["source_class"] or "Unknown"), *( ["collection"] if torrent["collection_hint"] or torrent["release_unit"] == "collection" else [])}),
                "targets": targets, "files": files, "selectedBytes": selected_bytes,
            })
    # Manual and automatic choices may be mixed in one cart. Different hashes
    # must never write the same final file, even when two logical Archive works
    # share one physical directory or a collection carries common attachments.
    destinations: dict[str, tuple[str, str, int, frozenset[int]]] = {}
    for assessment in assessments:
        info_hash = str(assessment["infoHash"])
        for item in assessment["files"]:
            if item.get("action") not in {"add_missing", "add_coexisting", "stage_replace"}:
                continue
            final_path = str(item.get("finalPath") or "")
            if not final_path:
                continue
            key = _collision_key(final_path)
            previous = destinations.get(key)
            if previous and previous[0] != info_hash:
                same_owner = previous[3] == frozenset(int(value) for value in assessment["animeIds"])
                same_size = previous[2] == int(item.get("bytes") or item.get("length") or 0)
                if same_owner and same_size:
                    item["action"] = "not_selected"
                    item["reason"] = "duplicate_target_same_size"
                    continue
                raise ValueError(
                    f"mixed selection collision at {final_path}: {previous[0]} and {info_hash}"
                )
            destinations[key] = (info_hash, final_path, int(item.get("bytes") or item.get("length") or 0),
                                 frozenset(int(value) for value in assessment["animeIds"]))
    selected_actions = {"add_missing", "add_coexisting", "stage_replace"}
    for assessment in assessments:
        assessment["downloadBytes"] = sum(int(item.get("bytes") or item.get("length") or 0)
                                          for item in assessment["files"] if item.get("action") in selected_actions)
        assessment["summary"] = dict(sorted((action, sum(row["action"] == action for row in assessment["files"]))
                                             for action in {row["action"] for row in assessment["files"]}))
    for job in jobs:
        job["selectedBytes"] = sum(int(item.get("bytes") or item.get("length") or 0)
                                   for item in job["files"] if item.get("action") in selected_actions)
    jobs = [job for job in jobs if job["selectedBytes"] > 0]
    payload = {"schemaVersion": "1.1", "approved": False, "planId": plan_id,
               "qbtEndpoint": config["components"]["downloadClient"]["endpoint"],
               "qbtLibraryRoot": config["deployment"]["qbtLibraryRoot"],
               "category": config["components"]["downloadClient"]["category"], "jobs": jobs,
               "assessments": assessments}
    total = sum(int(job["selectedBytes"]) for job in jobs)
    with db:
        if replace_existing:
            changed = db.execute("""UPDATE download_plan SET state='preview',approved=0,request_json=?,plan_json=?,
                total_bytes=?,task_count=?,work_count=?,updated_at=?,error_text=NULL WHERE plan_id=? AND state='building'""",
                (json.dumps(request, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), total,
                 len(jobs), len(anime_ids), stamp, plan_id)).rowcount
            if changed != 1:
                raise ValueError("queued plan is no longer buildable")
        else:
            db.execute("INSERT INTO download_plan VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                       (plan_id, "preview", 0, json.dumps(request, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), total, len(jobs), len(anime_ids), stamp, stamp))
    if plan_dir:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / f"{plan_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"planId": plan_id, "state": "preview", "approved": False, "taskCount": len(jobs),
            "workCount": len(anime_ids), "totalBytes": total, "jobs": jobs,
            "assessments": assessments}


def create_plan(db_path: Path, config: dict[str, Any], request: dict[str, Any], plan_dir: Path | None = None) -> dict[str, Any]:
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=60000")
        migrate_overlay(db)
        return _create_plan_connected(db, config, request, plan_dir)


def queue_plan(db_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Persist a large plan request immediately so HTTP never waits for it."""
    anime_ids = sorted({int(value) for value in request.get("animeIds", [])})
    if not anime_ids:
        raise ValueError("animeIds must contain at least one work")
    plan_id = uuid.uuid4().hex; stamp = utcnow()
    payload = {"schemaVersion": "1.1", "planId": plan_id, "jobs": [], "assessments": []}
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
        migrate_overlay(db)
        db.execute("INSERT INTO download_plan VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                   (plan_id, "building", 0, json.dumps(request, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False), 0, 0, len(anime_ids), stamp, stamp))
    return {"planId": plan_id, "state": "building", "approved": False,
            "taskCount": 0, "workCount": len(anime_ids), "totalBytes": 0,
            "jobs": [], "assessments": []}


def build_queued_plan(db_path: Path, config: dict[str, Any], plan_id: str,
                      request: dict[str, Any], plan_dir: Path | None = None) -> None:
    """Build one queued plan in the background while catalog reads continue."""
    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=60000")
            migrate_overlay(db)
            _create_plan_connected(db, config, request, plan_dir, plan_id=plan_id, replace_existing=True)
    except Exception as exc:
        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db, db:
            db.execute("UPDATE download_plan SET state='error',updated_at=?,error_text=? WHERE plan_id=?",
                       (utcnow(), f"{type(exc).__name__}: {exc}", plan_id))


def get_plan(db_path: Path, plan_id: str) -> dict[str, Any] | None:
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row; migrate_overlay(db)
        row = db.execute("SELECT * FROM download_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["plan_json"])
        return {"planId": row["plan_id"], "state": row["state"], "approved": bool(row["approved"]),
                "taskCount": row["task_count"], "workCount": row["work_count"], "totalBytes": row["total_bytes"],
                "createdAt": row["created_at"], "error": row["error_text"], "jobs": payload.get("jobs", []),
                "aniRssJobs": payload.get("aniRssJobs", []),
                "assessments": payload.get("assessments", [])}


def stage_plan_submission(db_path: Path, plan_id: str, output: Path) -> dict[str, Any]:
    """Atomically lock a preview and materialize the approved worker input."""
    db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
    try:
        migrate_overlay(db)
        row = db.execute("SELECT * FROM download_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if not row or row["state"] != "preview" or row["approved"]:
            raise ValueError("plan is missing, stale, or already submitted")
        payload = json.loads(row["plan_json"]); payload["approved"] = True
        if not payload.get("jobs") and not payload.get("aniRssJobs"):
            raise ValueError("plan contains no missing or verified replacement files to submit")
        stamp = utcnow()
        with db:
            changed = db.execute("UPDATE download_plan SET state='submitting',approved=1,plan_json=?,updated_at=? WHERE plan_id=? AND state='preview' AND approved=0",
                                 (json.dumps(payload, ensure_ascii=False), stamp, plan_id)).rowcount
            if changed != 1:
                raise ValueError("plan was concurrently changed")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload
    finally:
        db.close()


def recoverable_plan_submissions(db_path: Path) -> list[dict[str, Any]]:
    """Return approved submissions left incomplete by an interrupted process."""
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        migrate_overlay(db)
        rows = db.execute(
            "SELECT plan_id,state,plan_json FROM download_plan "
            "WHERE approved=1 AND state IN ('submitting','retry') ORDER BY updated_at,plan_id"
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["plan_json"])
            except (TypeError, json.JSONDecodeError):
                with db:
                    db.execute(
                        "UPDATE download_plan SET state='failed',updated_at=?,error_text=? WHERE plan_id=?",
                        (utcnow(), "stored approved plan is invalid", row["plan_id"]),
                    )
                continue
            recovered.append({"planId": row["plan_id"], "state": row["state"], "payload": payload})
        return recovered


def finish_plan_submission(db_path: Path, plan_id: str, *, success: bool, error: str | None = None,
                           retryable: bool = False) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        stamp = utcnow()
        row = db.execute("SELECT plan_json FROM download_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return
        payload = json.loads(row[0])
        state = "submitted" if success else ("retry" if retryable else "failed")
        with db:
            db.execute("UPDATE download_plan SET state=?,updated_at=?,error_text=? WHERE plan_id=?",
                       (state, stamp, error, plan_id))
            if success:
                for job in payload.get("jobs", []):
                    db.execute("""INSERT INTO runtime_submission(info_hash,qbt_save_path,category,tags_json,qbt_state,verified_at,plan_revision)
                        VALUES(?,?,?,?,?,?,1) ON CONFLICT(info_hash) DO UPDATE SET qbt_save_path=excluded.qbt_save_path,
                        category=excluded.category,tags_json=excluded.tags_json,qbt_state=excluded.qbt_state,
                        verified_at=excluded.verified_at,plan_revision=runtime_submission.plan_revision+1""",
                        (job["infoHash"], job["savePath"], payload["category"], json.dumps(job["tags"], ensure_ascii=False), "stoppedDL", stamp))
                    targets = [str(value) for value in job.get("targets", [])]
                    if targets:
                        marks = ",".join("?" for _ in targets)
                        db.execute(f"UPDATE runtime_work SET library_state='queued',origin='managed_submission',updated_at=? WHERE target_unc IN ({marks})",
                                   (stamp, *targets))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="synchronize a verified operational catalog into the product database")
    sync.add_argument("--metadata-db", type=Path, required=True)
    sync.add_argument("--runtime-db", type=Path, required=True)
    sync.add_argument("--offline-metadata", type=Path)
    sync.add_argument("--manifest-json", type=Path)
    verify = sub.add_parser("verify", help="verify product runtime invariants without external side effects")
    verify.add_argument("--metadata-db", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "sync":
        result = sync_overlay(args.metadata_db, args.runtime_db, offline_metadata=args.offline_metadata,
                              manifest_json=args.manifest_json)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif args.command == "verify":
        result = verify_runtime(args.metadata_db)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
