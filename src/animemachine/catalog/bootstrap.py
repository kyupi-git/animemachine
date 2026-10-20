"""Publish a usable recent Catalog before preparing its complete Archive metadata."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
from pathlib import Path
import sqlite3
import zipfile
from typing import Any, Callable

from .archive_update import merge_metadata


def metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        return dict(db.execute("SELECT key,value FROM metadata"))


def pending(path: Path) -> bool:
    """Older complete databases have no scope marker and remain complete."""
    values = metadata(path)
    return bool(values.get("catalog_scope") == "season" or (
        values.get("archive_name") == "bootstrap-pending" and values.get("record_count") == "0"))


def select_recent(catalog: Any, archive: Path, today: dt.date) -> dict[int, dict[str, Any]]:
    # Keep a full year of movie/BD candidates and continuing series, plus next
    # quarter for the existing early-season rollover. Normal UI filters still apply.
    first_month = ((today.month - 1) // 3) * 3 + 1
    first = dt.date(today.year, first_month, 1)
    lower = f"{first.year - 1:04}-{first.month:02}"
    upper_month = (first.year * 12 + first.month - 1) + 6
    upper = f"{upper_month // 12:04}-{upper_month % 12 + 1:02}"
    with zipfile.ZipFile(archive) as source:
        return {int(row["id"]): row for row in catalog.iter_jsonlines(source, "subject.jsonlines")
                if int(row.get("type") or 0) == 2 and lower <= str(row.get("date") or "")[:7] < upper}


def _checkpoint_ready(path: Path, descriptor: dict[str, Any]) -> bool:
    try:
        values = metadata(path)
        if (not descriptor.get("digest") or values.get("archive_digest") != descriptor["digest"]
                or values.get("build_state") != "ready" or values.get("catalog_scope") != "complete"
                or values.get("feature_schema_version") != "17" or int(values.get("record_count", "0")) <= 0):
            return False
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA quick_check").fetchone()[0] == "ok" and db.execute("PRAGMA foreign_key_check").fetchone() is None
    except (OSError, sqlite3.Error, ValueError):
        return False


def prepare(catalog: Any, path: Path, archive_dir: Path, network: dict[str, Any], *,
            ready: Callable[[], None], progress: Callable[[dict[str, Any]], None],
            today: dt.date | None = None) -> bool:
    """Resume committed stages; never rebuild a complete/legacy Catalog at startup."""
    if not pending(path):
        ready()
        return False
    values = metadata(path)
    if values.get("catalog_scope") == "season":
        ready()
    supplied = None
    if values.get("catalog_scope") == "season":
        name = values.get("archive_name", "")
        if name and Path(name).name == name:
            supplied = next((candidate for candidate in (
                archive_dir / name, Path(os.getenv("ANM_IMPORTS_DIR", "/Imports")) / name) if candidate.is_file()), None)
    archive, descriptor = catalog.ensure_archive(archive_dir, supplied=supplied, network=network, progress_callback=progress)
    if supplied is not None and descriptor.get("digest") != values.get("archive_digest"):
        raise ValueError("The saved bootstrap Archive no longer matches its verified checksum")
    stage = path.with_name("bootstrap-full.sqlite3")
    if values.get("catalog_scope") != "season":
        progress({"phase": "catalog_season_parse"})
        subjects = select_recent(catalog, archive, today or dt.datetime.now().astimezone().date())
        if subjects:
            rows = catalog.build_items_from_archive(archive, None, {}, basic_only=True, selected_subjects=subjects)
            catalog.write_database(stage, rows, {**descriptor, "scope": "season"})
            with catalog.DATABASE_MAINTENANCE_LOCK:
                merge_metadata(path, stage, catalog)
                catalog.ensure_instance_random_seed(path)
            del rows
            ready()
            progress({"phase": "catalog_season_ready"})
        del subjects
    if not _checkpoint_ready(stage, descriptor):
        progress({"phase": "catalog_full_parse"})
        rows = catalog.build_items_from_archive(archive, None, {})
        progress({"phase": "catalog_full_write", "records": len(rows)})
        catalog.write_database(stage, rows, descriptor)
        del rows
    progress({"phase": "catalog_full_merge"})
    with catalog.DATABASE_MAINTENANCE_LOCK:
        merge_metadata(path, stage, catalog)
        catalog.ensure_instance_random_seed(path)
    ready()
    # A crash before cleanup is harmless: the committed complete scope is authoritative.
    stage.unlink(missing_ok=True)
    progress({"phase": "catalog_ready"})
    return True
