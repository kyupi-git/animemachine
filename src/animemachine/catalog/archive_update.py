"""Transactional Bangumi Archive refresh that preserves runtime and cached images."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import json
from pathlib import Path
from typing import Any


ARCHIVE_TIMEZONE = dt.timezone(dt.timedelta(hours=8))


def weekly_check(now: dt.datetime) -> dt.datetime:
    """Latest Thursday 02:12 in UTC+8, after Wednesday's 05:00 export.

    Cadence: https://github.com/bangumi/Archive#archive (verified 2026-10-02).
    A fixed upstream timezone keeps container/host timezones and DST irrelevant.
    """
    local = now.astimezone(ARCHIVE_TIMEZONE)
    check = (local - dt.timedelta(days=(local.weekday() - 3) % 7)).replace(
        hour=2, minute=12, second=0, microsecond=0)
    return check if check <= local else check - dt.timedelta(days=7)


class ArchiveUpdater:
    def __init__(self, db_path: Path, catalog_module: Any, config_store: Any | None = None,
                 *, archive_dir: Path | None = None,
                 operation_lock: threading.RLock | None = None) -> None:
        self.db_path = db_path
        self.catalog = catalog_module
        self.config_store = config_store
        self.archive_dir = archive_dir or (db_path.parent / "archive")
        self.operation_lock = operation_lock
        self.lock = threading.Lock()
        self.status_file = self.archive_dir / ".archive-update-state.json"
        self._status = self._load_status()
        if self._status.get("state") in {"checking", "building", "merging"}:
            previous = str(self._status.get("state"))
            self._status = {**self._status,
                "state": "interrupted",
                "previousState": previous,
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            self._persist_status(self._status)

    def _load_status(self) -> dict[str, Any]:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
            return dict(data) if isinstance(data, dict) and data.get("state") else {"state": "idle"}
        except (OSError, ValueError, TypeError):
            return {"state": "idle"}

    def _persist_status(self, status: dict[str, Any]) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(self.status_file)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return dict(self._status)

    def _set(self, state: str, **extra: Any) -> None:
        with self.lock:
            snapshot = {**self._status, "state": state,
                        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), **extra}
            self._persist_status(snapshot)
            self._status = snapshot

    def start(self, *, schedule: dict[str, Any] | None = None) -> bool:
        with self.lock:
            if self._status.get("state") in {"checking", "building", "merging"}:
                return False
            snapshot = {"state": "checking", "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
            if schedule is not None or self._status.get("schedule"):
                snapshot["schedule"] = schedule if schedule is not None else self._status["schedule"]
            self._persist_status(snapshot)
            self._status = snapshot
        threading.Thread(target=self._run, daemon=True, name="anm-archive-update").start()
        return True

    def recover_interrupted(self) -> bool:
        return self.start() if self.status().get("state") == "interrupted" else False

    def tick(self, now: dt.datetime | None = None) -> bool:
        """Start at most two checks per weekly cycle; catch up after downtime."""
        now = now or dt.datetime.now(dt.timezone.utc)
        cycle = weekly_check(now).isoformat()
        status = self.status()
        if status.get("state") in {"checking", "building", "merging", "interrupted"}:
            return False
        previous = status.get("schedule") or {}
        if previous.get("cycle", "") > cycle:
            return False  # Clock rollback must not replay a consumed cycle.
        attempt = 1
        if previous.get("cycle") == cycle:
            if previous.get("finished") or not previous.get("retryAt"):
                return False
            if now < dt.datetime.fromisoformat(previous["retryAt"]):
                return False
            attempt = 2
        return self.start(schedule={"cycle": cycle, "attempt": attempt, "finished": False})

    def _finish_schedule(self, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        with self.lock:
            schedule = dict(self._status.get("schedule") or {})
            if not schedule or schedule.get("finished"):
                return
            expected = dt.datetime.fromisoformat(schedule["cycle"]) - dt.timedelta(hours=21, minutes=12)
            try:
                published = dt.datetime.fromisoformat(str(self._status.get("archiveCreatedAt") or "").replace("Z", "+00:00"))
                fresh = (self._status.get("state") in {"complete", "unchanged"}
                         and published >= expected)
            except (ValueError, TypeError):
                fresh = False
            schedule["finished"] = bool(fresh or schedule["attempt"] >= 2)
            schedule["retryAt"] = None if schedule["finished"] else (now + dt.timedelta(days=1)).isoformat()
            snapshot = {**self._status, "schedule": schedule}
            self._persist_status(snapshot)
            self._status = snapshot

    def import_stream(self, stream: Any, length: int, filename: str) -> dict[str, Any]:
        """Install a browser-downloaded official Archive after strict verification."""
        if Path(filename).name != filename or not filename.startswith("dump-") or not filename.endswith(".zip"):
            raise ValueError("invalid Bangumi Archive filename")
        if length <= 0 or length > 2 * 1024 * 1024 * 1024:
            raise ValueError("invalid Bangumi Archive upload size")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        handle, raw = tempfile.mkstemp(prefix="anm-archive-upload-", suffix=".part", dir=self.archive_dir)
        os.close(handle)
        temporary = Path(raw)
        try:
            remaining = length
            with temporary.open("wb") as output:
                while remaining:
                    block = stream.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        raise ValueError("incomplete Bangumi Archive upload")
                    output.write(block)
                    remaining -= len(block)
            config = self.config_store.read() if self.config_store else {}
            network = config.get("metadata", {}).get("network", {})
            descriptor, endpoint = self.catalog.network_sources.fetch_json(
                network.get("archiveManifestEndpoints") or [self.catalog.LATEST_ARCHIVE_URL],
                timeout=float(network.get("probeTimeoutSeconds", 12)),
                cooldown=int(network.get("failureCooldownSeconds", 900)),
                headers={"User-Agent": self.catalog.USER_AGENT, "Accept": "application/json"})
            expected_hash = str(descriptor["digest"]).removeprefix("sha256:").lower()
            if filename != descriptor["name"]:
                raise ValueError(f"uploaded file is not the current Archive ({descriptor['name']})")
            if temporary.stat().st_size != int(descriptor["size"]):
                raise ValueError("uploaded Archive size does not match the official descriptor")
            if self.catalog.file_sha256(temporary) != expected_hash:
                raise ValueError("uploaded Archive SHA-256 does not match the official descriptor")
            target = self.archive_dir / filename
            os.replace(temporary, target)
            self.catalog.write_archive_receipt(target.with_suffix(target.suffix + ".verified.json"),
                                               expected_hash, int(descriptor["size"]), source=target)
            descriptor["resolved_manifest_endpoint"] = endpoint
            self._set("imported", archiveName=filename)
            return {"installed": True, "archiveName": filename, "sha256": expected_hash}
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self) -> None:
        if self.operation_lock is not None:
            with self.operation_lock:
                self._run_locked()
            return
        self._run_locked()

    def _run_locked(self) -> None:
        temporary: Path | None = None
        try:
            config = self.config_store.read() if self.config_store else {}
            archive, descriptor = self.catalog.ensure_archive(self.archive_dir, network=config.get("metadata", {}).get("network", {}))
            with contextlib.closing(sqlite3.connect(self.db_path, timeout=120)) as db:
                db.execute("PRAGMA busy_timeout=120000")
                current = db.execute("SELECT value FROM metadata WHERE key='archive_digest'").fetchone()
                release_event_source = db.execute(
                    "SELECT value FROM metadata WHERE key='release_event_source_version'"
                ).fetchone()
            digest = str(descriptor.get("digest") or "")
            if (current and current[0] == digest and release_event_source
                    and release_event_source[0] == self.catalog.RELEASE_EVENT_SOURCE_VERSION):
                self._set("unchanged", archiveName=descriptor.get("name"), archiveCreatedAt=descriptor.get("created_at"))
                return
            self._set("building", archiveName=descriptor.get("name"))
            manifest = self.catalog.all_anime_manifest(archive)
            rows = self.catalog.build_items_from_archive(archive, manifest, {})
            handle, raw = tempfile.mkstemp(prefix="anm-archive-update-", suffix=".sqlite3", dir=self.db_path.parent)
            os.close(handle)
            Path(raw).unlink(missing_ok=True)
            temporary = Path(raw)
            self.catalog.write_database(temporary, rows, descriptor)
            self._set("merging", archiveName=descriptor.get("name"), incomingWorks=len(rows))
            summary = merge_metadata(self.db_path, temporary, self.catalog)
            self._set("complete", archiveName=descriptor.get("name"), archiveCreatedAt=descriptor.get("created_at"), **summary)
        except Exception as exc:
            self._set("failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            self._finish_schedule()


def merge_metadata(target: Path, incoming: Path, catalog_module: Any) -> dict[str, int]:
    db = sqlite3.connect(target, timeout=120)
    try:
        db.execute("PRAGMA busy_timeout=120000")
        db.execute("PRAGMA foreign_keys=ON")
        catalog_module.migrate_catalog_features(db)
        before = db.execute("SELECT COUNT(*) FROM anime_work").fetchone()[0]
        db.execute("ATTACH DATABASE ? AS incoming", (str(incoming),))
        scalar = [row[1] for row in db.execute("PRAGMA table_info(anime_work)") if row[1] not in {"id", "bgm_id"}]
        with db:
            columns = ["bgm_id", *scalar]
            joined = ",".join(columns)
            db.execute(f"INSERT INTO anime_work({joined}) SELECT {joined} FROM incoming.anime_work s WHERE NOT EXISTS(SELECT 1 FROM anime_work t WHERE t.bgm_id=s.bgm_id)")
            for column in scalar:
                fallback = f"COALESCE(NULLIF((SELECT s.{column} FROM incoming.anime_work s WHERE s.bgm_id=anime_work.bgm_id),''),{column})" if column not in {"episode_count"} else f"COALESCE((SELECT s.{column} FROM incoming.anime_work s WHERE s.bgm_id=anime_work.bgm_id),{column})"
                db.execute(f"UPDATE anime_work SET {column}={fallback} WHERE EXISTS(SELECT 1 FROM incoming.anime_work s WHERE s.bgm_id=anime_work.bgm_id)")

            sourced = {
                "anime_title": ["language", "title", "title_type", "source"],
                "anime_staff": ["person_id", "name", "role", "role_type", "source"],
                "anime_cast": ["character_id", "character_name", "person_id", "person_name", "character_role", "language", "source"],
                "anime_relation": ["related_bgm_id", "related_title", "relation_type", "relation_code", "strict_group", "source", "related_subject_type", "related_subject_kind", "related_subject_meta_json"],
            }
            for table, fields in sourced.items():
                db.execute(f"DELETE FROM {table} WHERE source='bangumi-archive' AND anime_id IN (SELECT t.id FROM anime_work t JOIN incoming.anime_work s ON s.bgm_id=t.bgm_id)")
                field_sql = ",".join(fields)
                db.execute(f"INSERT OR IGNORE INTO {table}(anime_id,{field_sql}) SELECT t.id,{','.join('x.'+f for f in fields)} FROM incoming.{table} x JOIN incoming.anime_work s ON s.id=x.anime_id JOIN anime_work t ON t.bgm_id=s.bgm_id")
            simple = {
                "anime_tag": ["tag", "vote_count", "tag_rank"],
                "anime_theme": ["theme_code"],
                "anime_theme_evidence": ["theme_code", "confidence", "accepted", "evidence_json", "rule_version"],
                "anime_studio": ["studio"],
                "anime_country": ["country_code", "evidence"],
            }
            for table, fields in simple.items():
                db.execute(f"DELETE FROM {table} WHERE anime_id IN (SELECT t.id FROM anime_work t JOIN incoming.anime_work s ON s.bgm_id=t.bgm_id WHERE EXISTS(SELECT 1 FROM incoming.{table} x WHERE x.anime_id=s.id))")
                db.execute(f"INSERT OR IGNORE INTO {table}(anime_id,{','.join(fields)}) SELECT t.id,{','.join('x.'+f for f in fields)} FROM incoming.{table} x JOIN incoming.anime_work s ON s.id=x.anime_id JOIN anime_work t ON t.bgm_id=s.bgm_id")
            db.execute("""DELETE FROM anime_release_event
                WHERE source LIKE 'bangumi-archive:%' AND anime_id IN (
                    SELECT t.id FROM anime_work t JOIN incoming.anime_work s ON s.bgm_id=t.bgm_id
                )""")
            db.execute("""INSERT OR IGNORE INTO anime_release_event(anime_id,event_type,release_date,source)
                SELECT t.id,x.event_type,x.release_date,x.source
                FROM incoming.anime_release_event x
                JOIN incoming.anime_work s ON s.id=x.anime_id
                JOIN anime_work t ON t.bgm_id=s.bgm_id""")
            catalog_module.rebuild_studio_clusters(db)
            catalog_module.rebuild_theme_clusters(db)
            for key in ("archive_name", "archive_created_at", "archive_digest", "record_count", "built_at",
                        "release_event_source_version"):
                db.execute("INSERT INTO metadata(key,value) SELECT key,value FROM incoming.metadata WHERE key=? ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,))
            catalog_module.relation_graph.rebuild(db, force=True)
            catalog_module.rebuild_physical_layout(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("foreign-key verification failed after Archive merge")
        after = db.execute("SELECT COUNT(*) FROM anime_work").fetchone()[0]
        db.execute("DETACH DATABASE incoming")
        return {"previousWorks": int(before), "currentWorks": int(after), "addedWorks": int(after - before)}
    finally:
        db.close()
