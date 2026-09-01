#!/usr/bin/env python3
"""Recoverable history for AnimeMachine-owned library mutations.

Existing user content is never destroyed in place.  Rename/move operations
store compact before/after evidence; removals move payloads into /Config
history storage.  Product-created control files may opt out to keep the audit
focused on user content.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ..storage import AVAILABLE, StorageUnavailableError, status_for_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS library_history_event(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,transaction_id TEXT NOT NULL,
  operation TEXT NOT NULL,source_path TEXT,target_path TEXT,backup_path TEXT,
  object_kind TEXT NOT NULL,bytes INTEGER,product_created INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_library_history_created ON library_history_event(created_at DESC,event_id DESC);
CREATE INDEX IF NOT EXISTS ix_library_history_transaction ON library_history_event(transaction_id,event_id);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve(strict=False)), str(root.resolve(strict=False)))) == str(root.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def mutate(db_path: Path, library_root: Path, history_root: Path, *, operation: str,
           source: Path, target: Path | None = None, product_created: bool = False,
           transaction_id: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply one guarded move/rename/removal and persist recoverable evidence."""
    operation = operation.casefold()
    if operation not in {"move", "rename", "remove"}:
        raise ValueError("unsupported library mutation")
    storage = status_for_path(library_root, require_write=True, timeout=4.0)
    if storage.state != AVAILABLE:
        raise StorageUnavailableError(f"library storage unavailable: {library_root}")
    source = source.resolve(strict=False); library_root = library_root.resolve(strict=True)
    if not _inside(source, library_root) or source == library_root or not source.exists():
        raise ValueError("source must exist below the library root")
    if operation != "remove":
        if target is None:
            raise ValueError("move/rename requires a target")
        target = target.resolve(strict=False)
        if not _inside(target, library_root) or target == library_root or target.exists():
            raise ValueError("target must be a new path below the library root")
    transaction_id = transaction_id or uuid.uuid4().hex
    kind = "directory" if source.is_dir() else "file"
    size = _size(source)
    backup: Path | None = None
    if operation == "remove":
        relative = source.relative_to(library_root)
        backup = history_root.resolve(strict=False) / "files" / transaction_id / relative
        if backup.exists():
            raise ValueError("history backup collision")
    created_at = utcnow()
    event_id: int | None = None
    if not product_created:
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            migrate(db)
            cursor = db.execute("""INSERT INTO library_history_event(transaction_id,operation,source_path,target_path,backup_path,
                object_kind,bytes,product_created,state,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (transaction_id, operation, str(source), str(target) if target else None,
                 str(backup) if backup else None, kind, size, 0, "planned",
                 json.dumps(details or {}, ensure_ascii=False), created_at))
            event_id = int(cursor.lastrowid)
    try:
        if operation == "remove":
            assert backup is not None
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(backup))
        else:
            assert target is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
    except Exception:
        if event_id is not None:
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE library_history_event SET state='failed' WHERE event_id=?", (event_id,))
        raise
    event = {
        "transactionId": transaction_id, "operation": operation, "sourcePath": str(source),
        "targetPath": str(target) if target else None, "backupPath": str(backup) if backup else None,
        "objectKind": kind, "bytes": size, "productCreated": bool(product_created),
        "state": "applied", "details": details or {}, "createdAt": created_at,
    }
    if event_id is not None:
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE library_history_event SET state='applied' WHERE event_id=?", (event_id,))
        event["eventId"] = event_id
    return event


def restore_removed(db_path: Path, library_root: Path, history_root: Path, event_id: int) -> dict[str, Any]:
    """Restore one recoverably removed item when its original path is free."""
    storage = status_for_path(library_root, require_write=True, timeout=4.0)
    if storage.state != AVAILABLE:
        raise StorageUnavailableError(f"library storage unavailable: {library_root}")
    library_root = library_root.resolve(strict=True)
    history_root = history_root.resolve(strict=True)
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row; migrate(db)
        row = db.execute("SELECT * FROM library_history_event WHERE event_id=?", (event_id,)).fetchone()
        if not row or row["operation"] != "remove" or row["state"] != "applied" or not row["backup_path"]:
            raise ValueError("history event is not restorable")
        source = Path(str(row["backup_path"])).resolve(strict=False)
        target = Path(str(row["source_path"])).resolve(strict=False)
        if not _inside(source, history_root) or not _inside(target, library_root) or not source.exists():
            raise ValueError("history payload or original target is invalid")
        if target.exists():
            raise ValueError("original path is occupied")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        with db:
            db.execute("UPDATE library_history_event SET state='restored' WHERE event_id=?", (event_id,))
    return {"eventId": event_id, "state": "restored", "restoredPath": str(target)}


def list_events(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row; migrate(db)
        rows = db.execute("SELECT * FROM library_history_event ORDER BY event_id DESC LIMIT ?", (max(1, min(limit, 1000)),))
        return [{"eventId": row["event_id"], "transactionId": row["transaction_id"],
                 "operation": row["operation"], "sourcePath": row["source_path"],
                 "targetPath": row["target_path"], "backupPath": row["backup_path"],
                 "objectKind": row["object_kind"], "bytes": row["bytes"], "state": row["state"],
                 "details": json.loads(row["details_json"] or "{}"), "createdAt": row["created_at"]}
                for row in rows]
