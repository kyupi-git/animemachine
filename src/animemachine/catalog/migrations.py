#!/usr/bin/env python3
"""Idempotently migrate the private catalog to the AnimeMachine runtime contract."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_column(db: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def migrate(db: sqlite3.Connection) -> dict[str, int | str]:
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS torrent(
      info_hash TEXT PRIMARY KEY,torrent_path TEXT NOT NULL,torrent_bytes INTEGER,mtime_utc TEXT,
      manifest_sha256 TEXT NOT NULL,source_class TEXT,effective_group TEXT,language_hint TEXT,
      scan_state TEXT NOT NULL,scan_reason TEXT,info_name TEXT,primary_work_name TEXT,
      title_state TEXT DEFAULT 'unmapped',file_count INTEGER,total_bytes INTEGER,indexed_at TEXT,
      torrent_created_at TEXT,created_by TEXT,release_flags_json TEXT NOT NULL DEFAULT '[]',
      collection_hint INTEGER,video_height INTEGER,video_scan TEXT,bit_depth INTEGER,
      release_unit TEXT NOT NULL DEFAULT 'unknown',volume_sequence_json TEXT NOT NULL DEFAULT '[]',episode_sequence_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS torrent_source(
      source_path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
      info_hash TEXT REFERENCES torrent(info_hash),presence_state TEXT NOT NULL,last_seen_at TEXT NOT NULL,parse_error TEXT
    );
    CREATE TABLE IF NOT EXISTS anime_work(
      work_id INTEGER PRIMARY KEY AUTOINCREMENT,target_unc TEXT UNIQUE NOT NULL,directory_name TEXT NOT NULL,
      series_unc TEXT,official_title TEXT NOT NULL,date_code TEXT NOT NULL,mal_id INTEGER,
      relation_state TEXT NOT NULL,scope_state TEXT NOT NULL DEFAULT 'active',library_state TEXT NOT NULL,
      placeholder_content TEXT,evidence_json TEXT NOT NULL,verified_at TEXT NOT NULL,
      deprecation_content TEXT,replacement_work_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS anime_work_member(
      member_id INTEGER PRIMARY KEY AUTOINCREMENT,owner_work_id INTEGER NOT NULL REFERENCES anime_work(work_id) ON DELETE CASCADE,
      member_ordinal INTEGER NOT NULL,official_title TEXT NOT NULL,date_code TEXT NOT NULL,mal_id INTEGER,
      bangumi_subject_id INTEGER,relation_type TEXT NOT NULL DEFAULT 'collection_member',evidence_json TEXT NOT NULL DEFAULT '{}',
      UNIQUE(owner_work_id,member_ordinal)
    );
    CREATE TABLE IF NOT EXISTS torrent_work(
      info_hash TEXT NOT NULL REFERENCES torrent(info_hash),work_id INTEGER NOT NULL REFERENCES anime_work(work_id),
      role TEXT NOT NULL,mapping_state TEXT NOT NULL,evidence_json TEXT NOT NULL,priority_rank INTEGER,
      ranking_key_json TEXT,ranking_policy_version TEXT,PRIMARY KEY(info_hash,work_id)
    );
    CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS file_map(
      info_hash TEXT NOT NULL REFERENCES torrent(info_hash),file_index INTEGER NOT NULL,source_path TEXT NOT NULL,
      target_relative_path TEXT NOT NULL,length INTEGER NOT NULL,selected INTEGER NOT NULL,PRIMARY KEY(info_hash,file_index)
    );
    CREATE TABLE IF NOT EXISTS torrent_manifest_file(
      info_hash TEXT NOT NULL REFERENCES torrent(info_hash) ON DELETE CASCADE,
      file_index INTEGER NOT NULL,source_path TEXT NOT NULL,length INTEGER NOT NULL,
      PRIMARY KEY(info_hash,file_index)
    );
    CREATE TABLE IF NOT EXISTS submission(
      info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),qbt_save_path TEXT NOT NULL,category TEXT NOT NULL,
      tags_json TEXT NOT NULL,qbt_state TEXT NOT NULL,plan_path TEXT NOT NULL,audit_path TEXT NOT NULL,verified_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS placeholder(
      target_unc TEXT PRIMARY KEY,empty_sha256 TEXT NOT NULL,empty_content TEXT NOT NULL,lifecycle TEXT NOT NULL,info_hash TEXT
    );
    CREATE TABLE IF NOT EXISTS supplement(
      target_unc TEXT NOT NULL,info_hash TEXT NOT NULL REFERENCES torrent(info_hash),file_index INTEGER NOT NULL,
      reason TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(target_unc,info_hash,file_index)
    );
    CREATE TABLE IF NOT EXISTS torrent_resolution(
      info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),disposition TEXT NOT NULL,
      review_reason_text TEXT NOT NULL DEFAULT '',manual_action TEXT NOT NULL DEFAULT 'pending',
      user_note TEXT NOT NULL DEFAULT '',proposal_evidence_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS torrent_target_path(
      info_hash TEXT NOT NULL REFERENCES torrent(info_hash),target_ordinal INTEGER NOT NULL,target_unc TEXT NOT NULL,
      target_state TEXT NOT NULL,work_id INTEGER REFERENCES anime_work(work_id),confidence REAL NOT NULL,
      basis TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(info_hash,target_ordinal)
    );
    CREATE TABLE IF NOT EXISTS scope_exclusion(
      info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),scope_state TEXT NOT NULL,official_title TEXT NOT NULL,
      mal_id INTEGER,evidence_json TEXT NOT NULL,verified_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS title_review(
      info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),reason_codes_json TEXT NOT NULL,candidate_json TEXT NOT NULL,
      evidence_json TEXT NOT NULL,reviewed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS work_candidate(
      target_unc TEXT NOT NULL,info_hash TEXT NOT NULL REFERENCES torrent(info_hash),decision TEXT NOT NULL,
      priority_rank INTEGER,evidence_json TEXT NOT NULL,PRIMARY KEY(target_unc,info_hash)
    );
    CREATE TABLE IF NOT EXISTS asset_provenance(
      asset_id INTEGER PRIMARY KEY AUTOINCREMENT,final_path TEXT NOT NULL UNIQUE,owner_path TEXT NOT NULL,
      bytes INTEGER,sha256 TEXT,media_created_at TEXT,source_info_hash TEXT,source_file_index INTEGER,
      source_torrent_path TEXT,replacement_state TEXT NOT NULL DEFAULT 'current',evidence_json TEXT NOT NULL DEFAULT '{}',
      verified_at TEXT NOT NULL,observed_mtime_ns INTEGER,observed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS release_baseline(
      owner_path TEXT PRIMARY KEY,selected_strategy_json TEXT NOT NULL,comparison_fingerprint TEXT NOT NULL,
      policy_version TEXT NOT NULL,updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS upgrade_candidate(
      upgrade_id INTEGER PRIMARY KEY AUTOINCREMENT,owner_path TEXT NOT NULL,old_info_hash TEXT,new_info_hash TEXT NOT NULL,
      comparison_fingerprint TEXT NOT NULL UNIQUE,manifest_delta_json TEXT NOT NULL,proof_json TEXT NOT NULL,
      staging_unc TEXT,state TEXT NOT NULL,last_source_fingerprint TEXT,detected_at TEXT NOT NULL,updated_at TEXT NOT NULL
    );
    """)
    add_column(db, "torrent", "asset_kind", "TEXT NOT NULL DEFAULT 'torrent'")
    add_column(db, "torrent", "magnet_uri", "TEXT")
    add_column(db, "torrent", "metadata_state", "TEXT NOT NULL DEFAULT 'available'")
    add_column(db, "torrent", "source_uri", "TEXT")
    add_column(db, "torrent", "btmh_info_hash", "TEXT")
    add_column(db, "torrent", "release_unit", "TEXT NOT NULL DEFAULT 'unknown'")
    add_column(db, "torrent", "volume_sequence_json", "TEXT NOT NULL DEFAULT '[]'")
    add_column(db, "torrent", "episode_sequence_json", "TEXT NOT NULL DEFAULT '[]'")
    add_column(db, "torrent_source", "source_kind", "TEXT NOT NULL DEFAULT 'file'")
    add_column(db, "file_map", "selection_reason", "TEXT NOT NULL DEFAULT ''")
    add_column(db, "submission", "plan_revision", "INTEGER NOT NULL DEFAULT 1")
    add_column(db, "torrent_work", "ranking_key_json", "TEXT")
    add_column(db, "torrent_work", "ranking_policy_version", "TEXT")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS background_job(
      job_id TEXT PRIMARY KEY,job_type TEXT NOT NULL,state TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE,cursor_json TEXT NOT NULL DEFAULT '{}',
      progress_current INTEGER NOT NULL DEFAULT 0,progress_total INTEGER,
      config_fingerprint TEXT,error_text TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS submission_revision(
      revision_id INTEGER PRIMARY KEY AUTOINCREMENT,info_hash TEXT NOT NULL REFERENCES torrent(info_hash),
      revision INTEGER NOT NULL,operation TEXT NOT NULL CHECK(operation IN ('create','extend')),
      state TEXT NOT NULL,plan_json TEXT NOT NULL,created_at TEXT NOT NULL,verified_at TEXT,
      UNIQUE(info_hash,revision)
    );
    CREATE TABLE IF NOT EXISTS submission_file_revision(
      revision_id INTEGER NOT NULL REFERENCES submission_revision(revision_id) ON DELETE CASCADE,
      file_index INTEGER NOT NULL,selected_before INTEGER NOT NULL CHECK(selected_before IN (0,1)),
      selected_after INTEGER NOT NULL CHECK(selected_after IN (0,1)),target_relative_path TEXT NOT NULL,
      reason TEXT NOT NULL,PRIMARY KEY(revision_id,file_index)
    );
    CREATE TABLE IF NOT EXISTS metadata_snapshot(
      snapshot_id TEXT PRIMARY KEY,source TEXT NOT NULL,source_digest TEXT NOT NULL,
      source_created_at TEXT,imported_at TEXT NOT NULL,state TEXT NOT NULL,
      record_count INTEGER NOT NULL DEFAULT 0,license_notice TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS ix_torrent_asset_kind ON torrent(asset_kind,metadata_state,scan_state);
    CREATE INDEX IF NOT EXISTS ix_torrent_manifest_path ON torrent_manifest_file(info_hash,source_path);
    CREATE INDEX IF NOT EXISTS ix_submission_revision_hash ON submission_revision(info_hash,revision);
    CREATE INDEX IF NOT EXISTS ix_background_job_state ON background_job(state,updated_at);
    CREATE INDEX IF NOT EXISTS ix_torrent_resolution_state ON torrent_resolution(disposition,manual_action);
    CREATE INDEX IF NOT EXISTS ix_asset_owner ON asset_provenance(owner_path);
    CREATE INDEX IF NOT EXISTS ix_asset_source ON asset_provenance(source_info_hash,source_file_index);
    CREATE INDEX IF NOT EXISTS ix_upgrade_owner_state ON upgrade_candidate(owner_path,state);
    """)
    stamp = now()
    db.execute("UPDATE torrent SET asset_kind='torrent',metadata_state='available' WHERE asset_kind IS NULL OR asset_kind='' OR metadata_state IS NULL OR metadata_state='' ")
    db.execute("UPDATE torrent_source SET source_kind='file' WHERE source_kind IS NULL OR source_kind='' ")
    db.execute("INSERT INTO catalog_meta(key,value,updated_at) VALUES('schema_version',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(SCHEMA_VERSION), stamp))
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "torrents": db.execute("SELECT COUNT(*) FROM torrent").fetchone()[0],
        "magnetAssets": db.execute("SELECT COUNT(*) FROM torrent WHERE asset_kind='magnet'").fetchone()[0],
        "submissions": db.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
    }
    db.commit()
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("catalog integrity_check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("catalog foreign_key_check failed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with contextlib.closing(sqlite3.connect(args.db)) as db, db:
        report = migrate(db)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
