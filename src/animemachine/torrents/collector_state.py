"""Persistent, migration-safe Torrent Collector state and file transactions."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
from typing import Any, Iterable

from .collector_filter import FILTER_RULESET_ID, SEARCH_RULESET_ID, FilterDecision
from .metainfo import CLASSIFIER_VERSION, inspect_bytes, read_torrent_file

SCHEMA_VERSION = 5
_TEMP_RE = re.compile(r"^\.anm-collector-.*\.part$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utf8_truncate(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def sanitize_title(title: str) -> str:
    title = re.sub(r'[\\/\x00<>:"|?*]', "_", title or "torrent")
    title = re.sub(r"\s+", " ", title).strip().strip(".")
    title = utf8_truncate(title, 180).rstrip(" .")
    return title or "torrent"


def namespaced_result_key(source: str, raw_key: str) -> str:
    source = (source or "unknown").strip().casefold()
    key = str(raw_key or "").strip()
    if key.casefold().startswith(source + ":"):
        return key
    return f"{source}:{key}"


class CollectorState:
    def __init__(
        self,
        db_path: Path,
        out_dir: Path,
        *,
        quarantine_dir: Path | None = None,
        max_retry_attempts: int = 6,
        output_uid: int | None = None,
        output_gid: int | None = None,
    ) -> None:
        self.db_path = db_path
        self.out_dir = out_dir
        self.quarantine_dir = quarantine_dir
        self.max_retry_attempts = max(1, int(max_retry_attempts))
        self.output_uid = output_uid
        self.output_gid = output_gid
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self.recover_pending_saves()
        self.recover_quarantine_moves()
        self.cleanup_stale_temp_files()

    def close(self) -> None:
        self.db.close()

    def _columns(self, table: str) -> set[str]:
        return {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._columns(table):
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS seen_results (
                result_key TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS torrent_files (
                sha256 TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS torrent_infohashes (
                infohash TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS native_jobs (
                jobset TEXT NOT NULL,
                source TEXT NOT NULL,
                term TEXT NOT NULL,
                next_page INTEGER NOT NULL DEFAULT 1,
                expected_more INTEGER NOT NULL DEFAULT 1,
                done INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (jobset, source, term)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS native_page_signatures (
                jobset TEXT NOT NULL,
                source TEXT NOT NULL,
                term TEXT NOT NULL,
                signature TEXT NOT NULL,
                page INTEGER NOT NULL,
                PRIMARY KEY (jobset, source, term, signature)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS retry_queue (
                result_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                details_url TEXT NOT NULL DEFAULT '',
                download_url TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            for definition in (
                "state TEXT NOT NULL DEFAULT 'retryable'",
                "next_retry_at TEXT",
                "error_class TEXT NOT NULL DEFAULT ''",
            ):
                self._add_column("retry_queue", definition)
            self.db.execute("""UPDATE retry_queue SET state='terminal',error_class='legacy_terminal'
                WHERE lower(last_error) LIKE '%http 404%' OR trim(lower(last_error))='404' OR lower(last_error) LIKE '%http 410%' OR trim(lower(last_error))='410'
                   OR lower(last_error) LIKE '%no http .torrent url%' OR lower(last_error) LIKE '%invalid bencode%'""")
            self.db.execute("UPDATE retry_queue SET state='dead_letter' WHERE state='retryable' AND attempts>=?",
                            (self.max_retry_attempts,))

            self.db.execute("""CREATE TABLE IF NOT EXISTS discoveries (
                result_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                original_title TEXT NOT NULL,
                details_url TEXT NOT NULL DEFAULT '',
                download_url TEXT NOT NULL DEFAULT '',
                discovery_json TEXT NOT NULL DEFAULT '{}',
                decision TEXT NOT NULL DEFAULT 'defer',
                decision_reason TEXT NOT NULL DEFAULT 'discovered',
                decision_json TEXT NOT NULL DEFAULT '{}',
                search_ruleset_id TEXT NOT NULL,
                filter_ruleset_id TEXT NOT NULL,
                metainfo_classifier_version TEXT NOT NULL,
                sha256 TEXT,
                infohash_v1 TEXT,
                infohash_v2 TEXT,
                saved_filename TEXT,
                collector_owned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS discoveries_filter_idx ON discoveries(filter_ruleset_id,decision)")
            self.db.execute("CREATE INDEX IF NOT EXISTS discoveries_saved_idx ON discoveries(saved_filename)")
            self._add_column("discoveries", "catalog_generation TEXT")
            self.db.execute("""CREATE TABLE IF NOT EXISTS torrent_identities (
                identity TEXT PRIMARY KEY,
                identity_kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                result_key TEXT,
                collector_owned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS pending_saves (
                result_key TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                infohash_v1 TEXT,
                infohash_v2 TEXT,
                created_at TEXT NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS collector_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS quarantine_moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                quarantine_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                infohash_v1 TEXT,
                infohash_v2 TEXT,
                reason TEXT NOT NULL,
                result_key TEXT,
                restored_at TEXT,
                created_at TEXT NOT NULL
            )""")
            self._add_column("quarantine_moves", "move_state TEXT NOT NULL DEFAULT 'moved'")
            self._add_column("quarantine_moves", "restored_path TEXT")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._set_meta("search_ruleset_id", SEARCH_RULESET_ID)
            self._set_meta("filter_ruleset_id", FILTER_RULESET_ID)
            self._set_meta("metainfo_classifier_version", CLASSIFIER_VERSION)
            # One migration discovery crawl is required when upgrading legacy state.
            if self._get_meta("discovery_schema_initialized") is None:
                legacy = self.db.execute("SELECT 1 FROM seen_results LIMIT 1").fetchone() is not None
                self._set_meta("needs_legacy_discovery_crawl", "1" if legacy else "0")
                self._set_meta("discovery_schema_initialized", "1")

    def _get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM collector_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        now = utc_now()
        self.db.execute(
            """INSERT INTO collector_meta(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, now),
        )

    def needs_legacy_discovery_crawl(self) -> bool:
        return self._get_meta("needs_legacy_discovery_crawl") == "1"

    def mark_legacy_discovery_crawl_complete(self) -> None:
        with self.db:
            self._set_meta("needs_legacy_discovery_crawl", "0")

    def native_job_get(self, jobset: str, source: str, term: str) -> tuple[int, bool, bool]:
        row = self.db.execute(
            "SELECT next_page,expected_more,done FROM native_jobs WHERE jobset=? AND source=? AND term=?",
            (jobset, source, term),
        ).fetchone()
        if row is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO native_jobs(jobset,source,term) VALUES(?,?,?)",
                    (jobset, source, term),
                )
            return 1, True, False
        return int(row["next_page"]), bool(row["expected_more"]), bool(row["done"])

    def native_job_update(
        self, jobset: str, source: str, term: str, next_page: int, expected_more: bool, *, done: bool
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO native_jobs(jobset,source,term,next_page,expected_more,done,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(jobset,source,term) DO UPDATE SET
                    next_page=excluded.next_page,expected_more=excluded.expected_more,
                    done=excluded.done,updated_at=excluded.updated_at""",
                (jobset, source, term, max(1, int(next_page)), int(bool(expected_more)), int(bool(done)), utc_now()),
            )

    def signature_seen(self, jobset: str, source: str, term: str, signature: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT page FROM native_page_signatures WHERE jobset=? AND source=? AND term=? AND signature=?",
            (jobset, source, term, signature),
        ).fetchone()

    def remember_signature(self, jobset: str, source: str, term: str, page: int, signature: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO native_page_signatures(jobset,source,term,signature,page) VALUES(?,?,?,?,?)",
                (jobset, source, term, signature, int(page)),
            )

    def record_discovery(self, source: str, item: dict[str, Any]) -> str:
        raw_key = str(item.get("id") or item.get("result_key") or item.get("details_url") or item.get("download_url") or "").strip()
        if not raw_key:
            identity = json.dumps(
                [source, str(item.get("title") or "")], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            raw_key = "title-" + hashlib.sha256(identity).hexdigest()
        key = namespaced_result_key(source, raw_key)
        title = str(item.get("title") or "")
        previous = self.db.execute("SELECT original_title FROM discoveries WHERE result_key=?", (key,)).fetchone()
        title_changed = previous is not None and str(previous["original_title"]) != title
        now = utc_now()
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        with self.db:
            self.db.execute(
                """INSERT INTO discoveries(
                    result_key,source,original_title,details_url,download_url,discovery_json,
                    search_ruleset_id,filter_ruleset_id,metainfo_classifier_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(result_key) DO UPDATE SET
                    original_title=excluded.original_title,details_url=excluded.details_url,
                    download_url=excluded.download_url,discovery_json=excluded.discovery_json,
                    search_ruleset_id=excluded.search_ruleset_id,updated_at=excluded.updated_at""",
                (key, source, title, str(item.get("details_url") or ""),
                 str(item.get("download_url") or ""), payload, SEARCH_RULESET_ID, FILTER_RULESET_ID,
                 CLASSIFIER_VERSION, now, now),
            )
            if title_changed:
                self.db.execute(
                    """UPDATE discoveries SET decision='defer',decision_reason='discovery_title_changed',
                       decision_json='{}',filter_ruleset_id='',catalog_generation=NULL,updated_at=? WHERE result_key=?""",
                    (now, key),
                )
                self.db.execute("DELETE FROM seen_results WHERE result_key=?", (key,))
                self.db.execute("DELETE FROM retry_queue WHERE result_key=?", (key,))
        return key

    def get_discovery(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM discoveries WHERE result_key=?", (key,)).fetchone()

    def needs_filter_evaluation(self, key: str) -> bool:
        row = self.get_discovery(key)
        if row is None:
            return True
        if row["filter_ruleset_id"] != FILTER_RULESET_ID or row["metainfo_classifier_version"] != CLASSIFIER_VERSION:
            return True
        filename = row["saved_filename"]
        if filename and not (self.out_dir / str(filename)).is_file():
            return True
        return row["decision"] in {"defer", "review"}

    def discoveries_needing_reevaluation(self, catalog_generation: str | None, limit: int = 500) -> list[sqlite3.Row]:
        generation = catalog_generation or ""
        return self.db.execute(
            """SELECT * FROM discoveries
               WHERE filter_ruleset_id<>? OR metainfo_classifier_version<>?
                  OR (decision IN ('defer','review') AND COALESCE(catalog_generation,'')<>?)
               ORDER BY updated_at ASC, result_key ASC LIMIT ?""",
            (FILTER_RULESET_ID, CLASSIFIER_VERSION, generation, max(1, int(limit))),
        ).fetchall()

    def reevaluation_pending_count(self, catalog_generation: str | None) -> int:
        generation = catalog_generation or ""
        row = self.db.execute(
            """SELECT COUNT(*) FROM discoveries
               WHERE filter_ruleset_id<>? OR metainfo_classifier_version<>?
                  OR (decision IN ('defer','review') AND COALESCE(catalog_generation,'')<>?)""",
            (FILTER_RULESET_ID, CLASSIFIER_VERSION, generation),
        ).fetchone()
        return int(row[0]) if row else 0

    def result_complete(self, key: str) -> bool:
        row = self.get_discovery(key)
        if row is not None:
            if row["filter_ruleset_id"] != FILTER_RULESET_ID or row["metainfo_classifier_version"] != CLASSIFIER_VERSION:
                return False
            if row["decision"] == "accept":
                filename = row["saved_filename"]
                return bool(filename and (self.out_dir / str(filename)).is_file())
            if row["decision"] == "reject":
                return True
            return False
        return self.db.execute("SELECT 1 FROM seen_results WHERE result_key=?", (key,)).fetchone() is not None

    def record_decision(
        self,
        key: str,
        decision: FilterDecision,
        *,
        metadata: dict[str, Any] | None = None,
        saved_filename: str | None = None,
        collector_owned: bool | None = None,
    ) -> None:
        metadata = metadata or {}
        catalog_data = decision.evidence.get("catalog") if isinstance(decision.evidence, dict) else None
        catalog_generation = catalog_data.get("generation") if isinstance(catalog_data, dict) else None
        now = utc_now()
        with self.db:
            self.db.execute(
                """UPDATE discoveries SET decision=?,decision_reason=?,decision_json=?,filter_ruleset_id=?,
                    metainfo_classifier_version=?,catalog_generation=?,sha256=COALESCE(?,sha256),infohash_v1=COALESCE(?,infohash_v1),
                    infohash_v2=COALESCE(?,infohash_v2),saved_filename=COALESCE(?,saved_filename),
                    collector_owned=COALESCE(?,collector_owned),updated_at=? WHERE result_key=?""",
                (decision.decision, decision.reason, json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True),
                 FILTER_RULESET_ID, CLASSIFIER_VERSION, catalog_generation, metadata.get("sha256"), metadata.get("infoHashV1"),
                 metadata.get("infoHashV2"), saved_filename, None if collector_owned is None else int(collector_owned), now, key),
            )
            if decision.decision in {"accept", "reject"}:
                self.db.execute("INSERT OR IGNORE INTO seen_results(result_key) VALUES(?)", (key,))
                self.db.execute("DELETE FROM retry_queue WHERE result_key=?", (key,))
            else:
                self.db.execute("DELETE FROM seen_results WHERE result_key=?", (key,))

    def cleanup_stale_temp_files(self) -> int:
        removed = 0
        for path in self.out_dir.iterdir():
            if path.is_file() and _TEMP_RE.match(path.name):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _identity_exists(self, sha256: str, v1: str | None, v2: str | None) -> sqlite3.Row | None:
        row = self.db.execute("SELECT sha256,filename FROM torrent_files WHERE sha256=?", (sha256,)).fetchone()
        if row and (self.out_dir / str(row["filename"])).is_file():
            return row
        for identity in (v1, v2):
            if not identity:
                continue
            row = self.db.execute("SELECT sha256,filename FROM torrent_identities WHERE identity=?", (identity,)).fetchone()
            if row and (self.out_dir / str(row["filename"])).is_file():
                return row
        return None

    @staticmethod
    def _available_collision_path(path: Path) -> Path:
        if not path.exists():
            return path
        index = 2
        while True:
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        try:
            fd = os.open(path, flags)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _clear_pending_save(self, result_key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM pending_saves WHERE result_key=?", (result_key,))

    def recover_pending_saves(self) -> int:
        recovered = 0
        rows = self.db.execute("SELECT * FROM pending_saves ORDER BY created_at,result_key").fetchall()
        for row in rows:
            filename = str(row["filename"])
            path = self.out_dir / filename
            try:
                if Path(filename).name != filename or not path.is_file():
                    continue
                raw = read_torrent_file(path)
                sha = hashlib.sha256(raw).hexdigest()
                if sha != str(row["sha256"]):
                    continue
                metadata = inspect_bytes(raw, filename=filename, include_files=False)
                expected_v1 = row["infohash_v1"]
                expected_v2 = row["infohash_v2"]
                if expected_v1 and metadata.get("infoHashV1") != expected_v1:
                    continue
                if expected_v2 and metadata.get("infoHashV2") != expected_v2:
                    continue
                self._record_saved_identity(
                    str(row["result_key"]), filename, sha,
                    metadata.get("infoHashV1"), metadata.get("infoHashV2"), collector_owned=True,
                )
                recovered += 1
            except (OSError, ValueError):
                pass
            finally:
                self._clear_pending_save(str(row["result_key"]))
        return recovered

    def atomic_save(self, raw: bytes, title: str, source: str, result_key: str, metadata: dict[str, Any]) -> tuple[bool, str]:
        sha = hashlib.sha256(raw).hexdigest()
        v1 = metadata.get("infoHashV1")
        v2 = metadata.get("infoHashV2")
        existing = self._identity_exists(sha, v1, v2)
        if existing:
            filename = str(existing["filename"])
            existing_sha = str(existing["sha256"])
            self._record_saved_identity(result_key, filename, existing_sha, v1, v2, collector_owned=None)
            self._clear_pending_save(result_key)
            return False, filename

        token = str(v1 or v2 or sha)[:12]
        base = sanitize_title(title)
        filename = f"{base} [{source}-{token}].torrent"
        final = self.out_dir / filename
        replace_invalid_owned = False
        if final.exists():
            try:
                current = read_torrent_file(final)
                current_meta = inspect_bytes(current, filename=final.name, include_files=False)
                current_sha = hashlib.sha256(current).hexdigest()
                if current_sha == sha or v1 and current_meta.get("infoHashV1") == v1 or v2 and current_meta.get("infoHashV2") == v2:
                    self._record_saved_identity(
                        result_key, final.name, current_sha, current_meta.get("infoHashV1"), current_meta.get("infoHashV2"),
                        collector_owned=None,
                    )
                    self._clear_pending_save(result_key)
                    return False, final.name
            except (OSError, ValueError):
                owned = self.db.execute(
                    "SELECT 1 FROM discoveries WHERE result_key=? AND saved_filename=? AND collector_owned=1",
                    (result_key, final.name),
                ).fetchone()
                replace_invalid_owned = owned is not None
            if not replace_invalid_owned:
                suffix = hashlib.sha256((filename + sha).encode()).hexdigest()[:8]
                final = self._available_collision_path(
                    self.out_dir / f"{utf8_truncate(base, 165)} [{source}-{token}-{suffix}].torrent"
                )
                filename = final.name

        temp = self.out_dir / f".anm-collector-{os.getpid()}-{random.getrandbits(48):012x}.part"
        try:
            with self.db:
                self.db.execute(
                    """INSERT INTO pending_saves(result_key,filename,sha256,infohash_v1,infohash_v2,created_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(result_key) DO UPDATE SET filename=excluded.filename,
                       sha256=excluded.sha256,infohash_v1=excluded.infohash_v1,infohash_v2=excluded.infohash_v2,
                       created_at=excluded.created_at""",
                    (result_key, filename, sha, v1, v2, utc_now()),
                )
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, final)
            self._fsync_directory(self.out_dir)
            if self.output_uid is not None and self.output_gid is not None:
                try:
                    os.chown(final, self.output_uid, self.output_gid)
                except (AttributeError, PermissionError, OSError):
                    pass
            self._record_saved_identity(result_key, filename, sha, v1, v2, collector_owned=True)
            self._clear_pending_save(result_key)
            return True, filename
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    def _record_saved_identity(self, key: str, filename: str, sha: str, v1: str | None, v2: str | None, *, collector_owned: bool | None) -> None:
        now = utc_now()
        if collector_owned is None:
            owner = self.db.execute("SELECT MAX(collector_owned) FROM torrent_identities WHERE sha256=?", (sha,)).fetchone()
            collector_owned = bool(owner and owner[0])
            if not collector_owned:
                owner = self.db.execute(
                    "SELECT MAX(collector_owned) FROM discoveries WHERE sha256=? OR saved_filename=?",
                    (sha, filename),
                ).fetchone()
                collector_owned = bool(owner and owner[0])
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO torrent_files(sha256,filename,saved_at) VALUES(?,?,?)", (sha, filename, now))
            for identity, kind in ((v1, "v1"), (v2, "v2")):
                if not identity:
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO torrent_identities(identity,identity_kind,sha256,filename,result_key,collector_owned,created_at) VALUES(?,?,?,?,?,?,?)",
                    (identity, kind, sha, filename, key, int(collector_owned), now),
                )
            if v1:
                self.db.execute("INSERT OR REPLACE INTO torrent_infohashes(infohash,sha256,filename,saved_at) VALUES(?,?,?,?)", (v1, sha, filename, now))
            self.db.execute(
                "UPDATE discoveries SET sha256=?,infohash_v1=?,infohash_v2=?,saved_filename=?,collector_owned=?,updated_at=? WHERE result_key=?",
                (sha, v1, v2, filename, int(collector_owned), now, key),
            )

    def reconcile(self) -> dict[str, int]:
        stats = {"indexed": 0, "missing": 0, "renamed": 0, "invalid": 0}
        actual: dict[str, tuple[str, dict[str, Any]]] = {}
        for path in self.out_dir.glob("*.torrent"):
            try:
                raw = read_torrent_file(path)
                metadata = inspect_bytes(raw, filename=path.name, include_files=False)
                sha = hashlib.sha256(raw).hexdigest()
                actual[sha] = (path.name, metadata)
                row = self.db.execute("SELECT filename FROM torrent_files WHERE sha256=?", (sha,)).fetchone()
                if row is None or row[0] != path.name:
                    stats["indexed"] += 1
                    if row is not None:
                        stats["renamed"] += 1
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO torrent_files(sha256,filename,saved_at) VALUES(?,?,?)", (sha, path.name, utc_now()))
                    for identity, kind in ((metadata.get("infoHashV1"), "v1"), (metadata.get("infoHashV2"), "v2")):
                        if identity:
                            self.db.execute("""INSERT INTO torrent_identities(identity,identity_kind,sha256,filename,result_key,collector_owned,created_at)
                                VALUES(?,?,?,?,NULL,0,?) ON CONFLICT(identity) DO UPDATE SET
                                identity_kind=excluded.identity_kind,sha256=excluded.sha256,filename=excluded.filename""",
                                (identity, kind, sha, path.name, utc_now()))
                    self.db.execute("UPDATE discoveries SET saved_filename=?,updated_at=? WHERE sha256=?", (path.name, utc_now(), sha))
            except (OSError, ValueError):
                stats["invalid"] += 1
                with self.db:
                    owned = self.db.execute(
                        "SELECT result_key FROM discoveries WHERE saved_filename=? AND collector_owned=1",
                        (path.name,),
                    ).fetchall()
                    for item in owned:
                        key = str(item[0])
                        self.db.execute(
                            "UPDATE discoveries SET decision='defer',decision_reason='saved_file_invalid',sha256=NULL,infohash_v1=NULL,infohash_v2=NULL,updated_at=? WHERE result_key=?",
                            (utc_now(), key),
                        )
                        self.db.execute("DELETE FROM seen_results WHERE result_key=?", (key,))
                    stale = self.db.execute("SELECT sha256 FROM torrent_files WHERE filename=?", (path.name,)).fetchall()
                    for item in stale:
                        stale_sha = str(item[0])
                        self.db.execute("DELETE FROM torrent_files WHERE sha256=?", (stale_sha,))
                        self.db.execute("DELETE FROM torrent_infohashes WHERE sha256=?", (stale_sha,))
                        self.db.execute("DELETE FROM torrent_identities WHERE sha256=?", (stale_sha,))

        rows = self.db.execute("SELECT sha256,filename FROM torrent_files").fetchall()
        for row in rows:
            sha, filename = str(row[0]), str(row[1])
            if sha in actual:
                continue
            stats["missing"] += 1
            with self.db:
                self.db.execute("DELETE FROM torrent_files WHERE sha256=?", (sha,))
                self.db.execute("DELETE FROM torrent_infohashes WHERE sha256=?", (sha,))
                self.db.execute("DELETE FROM torrent_identities WHERE sha256=?", (sha,))
                discoveries = self.db.execute("SELECT result_key FROM discoveries WHERE sha256=? AND collector_owned=1", (sha,)).fetchall()
                for item in discoveries:
                    key = str(item[0])
                    self.db.execute("UPDATE discoveries SET decision='defer',decision_reason='saved_file_missing',saved_filename=NULL,sha256=NULL,infohash_v1=NULL,infohash_v2=NULL,updated_at=? WHERE result_key=?", (utc_now(), key))
                    self.db.execute("DELETE FROM seen_results WHERE result_key=?", (key,))
        return stats

    def queue_retry(self, source: str, item: dict[str, Any], key: str, error: str, *, retryable: bool, error_class: str = "") -> str:
        row = self.db.execute("SELECT attempts FROM retry_queue WHERE result_key=?", (key,)).fetchone()
        attempts = int(row[0]) + 1 if row else 1
        if not retryable:
            state = "terminal"
            next_retry = None
        elif attempts >= self.max_retry_attempts:
            state = "dead_letter"
            next_retry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
        else:
            state = "retryable"
            delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
            delay += random.randint(0, max(1, delay // 5))
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                """INSERT INTO retry_queue(result_key,source,title,details_url,download_url,attempts,last_error,state,next_retry_at,error_class,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(result_key) DO UPDATE SET title=excluded.title,details_url=excluded.details_url,
                    download_url=excluded.download_url,attempts=excluded.attempts,last_error=excluded.last_error,state=excluded.state,
                    next_retry_at=excluded.next_retry_at,error_class=excluded.error_class,updated_at=excluded.updated_at""",
                (key, source, str(item.get("title") or "torrent"), str(item.get("details_url") or ""),
                 str(item.get("download_url") or ""), attempts, error[:1000], state, next_retry, error_class, utc_now()),
            )
        return state

    def due_retries(self, limit: int) -> list[sqlite3.Row]:
        now = utc_now()
        return self.db.execute(
            """SELECT * FROM retry_queue WHERE state='retryable' AND (next_retry_at IS NULL OR next_retry_at<=?)
               ORDER BY COALESCE(next_retry_at,updated_at) ASC LIMIT ?""",
            (now, int(limit)),
        ).fetchall()

    def clear_retry(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM retry_queue WHERE result_key=?", (key,))

    def audit_existing(self, classifier: Any, *, mode: str = "report") -> dict[str, int]:
        if mode not in {"report", "quarantine", "off"}:
            raise ValueError("audit mode must be report, quarantine, or off")
        stats = {"total": 0, "accept": 0, "reject": 0, "defer": 0, "invalid": 0, "quarantined": 0}
        if mode == "off":
            return stats
        for path in self.out_dir.glob("*.torrent"):
            stats["total"] += 1
            try:
                raw = read_torrent_file(path)
                metadata = inspect_bytes(raw, filename=path.name, include_files=True)
                sha = hashlib.sha256(raw).hexdigest()
                provenance = self.db.execute("SELECT original_title,collector_owned,result_key FROM discoveries WHERE sha256=? ORDER BY collector_owned DESC LIMIT 1", (sha,)).fetchone()
                audit_title = str(provenance["original_title"]) if provenance and provenance["original_title"] else str(metadata.get("name") or path.name)
                decision: FilterDecision = classifier(audit_title, metadata)
            except (OSError, ValueError):
                stats["invalid"] += 1
                continue
            stats[decision.decision] = stats.get(decision.decision, 0) + 1
            if mode != "quarantine" or decision.decision != "reject" or decision.confidence != "high":
                continue
            row = provenance
            if row is None or not bool(row["collector_owned"]):
                continue  # manual/legacy is report-only by default
            target_dir = self._validated_quarantine_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            if target.exists():
                target = self._available_collision_path(target_dir / f"{path.stem}-{sha[:8]}{path.suffix}")
            with self.db:
                cursor = self.db.execute(
                    """INSERT INTO quarantine_moves(original_path,quarantine_path,sha256,infohash_v1,infohash_v2,
                       reason,result_key,created_at,move_state) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (str(path), str(target), sha, metadata.get("infoHashV1"), metadata.get("infoHashV2"),
                     decision.reason, row["result_key"], utc_now(), "planned"),
                )
                move_id = int(cursor.lastrowid)
            try:
                os.replace(path, target)
                self._fsync_directory(path.parent)
                self._fsync_directory(target.parent)
            except OSError:
                with self.db:
                    self.db.execute("DELETE FROM quarantine_moves WHERE id=? AND move_state='planned'", (move_id,))
                raise
            with self.db:
                self.db.execute("UPDATE quarantine_moves SET move_state='moved' WHERE id=?", (move_id,))
            stats["quarantined"] += 1
        return stats

    def _validated_quarantine_dir(self) -> Path:
        if self.quarantine_dir is None:
            raise ValueError("quarantine directory is not configured")
        out = self.out_dir.resolve()
        target = self.quarantine_dir.resolve()
        try:
            target.relative_to(out)
        except ValueError:
            return target
        raise ValueError("quarantine directory must not be inside Torrent Pool")

    def _restore_quarantine_identity(self, row: sqlite3.Row, path: Path) -> bool:
        result_key = str(row["result_key"] or "")
        if not result_key or not path.is_file():
            return False
        try:
            raw = read_torrent_file(path)
            sha = hashlib.sha256(raw).hexdigest()
            if sha != str(row["sha256"]):
                return False
            metadata = inspect_bytes(raw, filename=path.name, include_files=False)
            expected_v1 = row["infohash_v1"]
            expected_v2 = row["infohash_v2"]
            if expected_v1 and metadata.get("infoHashV1") != expected_v1:
                return False
            if expected_v2 and metadata.get("infoHashV2") != expected_v2:
                return False
            self._record_saved_identity(
                result_key, path.name, sha, metadata.get("infoHashV1"), metadata.get("infoHashV2"), collector_owned=True,
            )
            return True
        except (OSError, ValueError):
            return False

    def restore_quarantine(self, move_id: int) -> bool:
        self.recover_quarantine_moves()
        row = self.db.execute(
            "SELECT * FROM quarantine_moves WHERE id=? AND restored_at IS NULL AND move_state='moved'",
            (int(move_id),),
        ).fetchone()
        if row is None:
            return False
        source = Path(str(row["quarantine_path"]))
        target = Path(str(row["original_path"]))
        if not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = self._available_collision_path(
                target.with_name(f"{target.stem}-{str(row['sha256'])[:8]}{target.suffix}")
            )
        with self.db:
            self.db.execute(
                "UPDATE quarantine_moves SET move_state='restore_planned',restored_path=? WHERE id=?",
                (str(target), int(move_id)),
            )
        try:
            os.replace(source, target)
            self._fsync_directory(source.parent)
            self._fsync_directory(target.parent)
        except OSError:
            with self.db:
                self.db.execute(
                    "UPDATE quarantine_moves SET move_state='moved',restored_path=NULL WHERE id=?",
                    (int(move_id),),
                )
            raise
        with self.db:
            self.db.execute(
                "UPDATE quarantine_moves SET restored_at=?,move_state='restored' WHERE id=?",
                (utc_now(), int(move_id)),
            )
        self._restore_quarantine_identity(row, target)
        return True

    def recover_quarantine_moves(self) -> dict[str, int]:
        stats = {"moved": 0, "restored": 0, "cancelled": 0, "conflict": 0}
        rows = self.db.execute(
            "SELECT * FROM quarantine_moves WHERE move_state IN ('planned','restore_planned') ORDER BY id"
        ).fetchall()
        for row in rows:
            source = Path(str(row["original_path"]))
            quarantine = Path(str(row["quarantine_path"]))
            state = str(row["move_state"])
            if state == "planned":
                if quarantine.is_file() and not source.exists():
                    with self.db:
                        self.db.execute("UPDATE quarantine_moves SET move_state='moved' WHERE id=?", (row["id"],))
                    stats["moved"] += 1
                elif source.is_file() and not quarantine.exists():
                    with self.db:
                        self.db.execute("DELETE FROM quarantine_moves WHERE id=?", (row["id"],))
                    stats["cancelled"] += 1
                else:
                    with self.db:
                        self.db.execute("UPDATE quarantine_moves SET move_state='conflict' WHERE id=?", (row["id"],))
                    stats["conflict"] += 1
                continue
            restored_raw = row["restored_path"]
            restored = Path(str(restored_raw)) if restored_raw else source
            if restored.is_file() and not quarantine.exists():
                with self.db:
                    self.db.execute(
                        "UPDATE quarantine_moves SET move_state='restored',restored_at=COALESCE(restored_at,?) WHERE id=?",
                        (utc_now(), row["id"]),
                    )
                self._restore_quarantine_identity(row, restored)
                stats["restored"] += 1
            elif quarantine.is_file() and not restored.exists():
                with self.db:
                    self.db.execute(
                        "UPDATE quarantine_moves SET move_state='moved',restored_path=NULL WHERE id=?",
                        (row["id"],),
                    )
                stats["cancelled"] += 1
            else:
                with self.db:
                    self.db.execute("UPDATE quarantine_moves SET move_state='conflict' WHERE id=?", (row["id"],))
                stats["conflict"] += 1
        return stats
