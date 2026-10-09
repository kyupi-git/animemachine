"""Idempotent subscribed-work directories, using the shared library layout."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable

from ..catalog.migrations import migrate as migrate_operational
from ..storage import AVAILABLE, StorageUnavailableError, status_for_path
from ..storage.path_policy import is_junction
from ..torrents import mapper, runtime, scanner
from . import history, layout


def _guard(root: Path, target: Path) -> None:
    """Reject escaped targets, links and non-directory ancestors before writes."""
    relative = target.absolute().relative_to(root.absolute())
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("target must be below the connected library root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or is_junction(current):
            raise ValueError("library target contains a linked directory")
        if current.exists() and not current.is_dir():
            raise ValueError("library target conflicts with an existing file")
    if not history._inside(target, root):
        raise ValueError("library target is outside the connected root")


def create(metadata_db: Path, runtime_db: Path, config: dict[str, Any], *,
           progress: Callable[[dict[str, Any]], None] | None = None,
           throttle: Callable[[], None] | None = None) -> dict[str, Any]:
    configured = str(config.get("deployment", {}).get("libraryUncRoot") or "").strip()
    if not configured:
        raise ValueError("connect a writable local library first")
    root = Path(configured).absolute()
    if status_for_path(root, require_write=True, timeout=4.0).state != AVAILABLE or not root.is_dir():
        raise StorageUnavailableError("the local library is unavailable or read-only")
    summary: dict[str, Any] = {"created": 0, "existing": 0, "skipped": 0, "failed": 0,
                               "done": 0, "total": 0, "errors": []}
    with contextlib.closing(sqlite3.connect(metadata_db, timeout=60)) as meta, contextlib.closing(scanner.connect(runtime_db)) as operational:
        meta.row_factory = sqlite3.Row
        runtime.migrate_overlay(meta)
        history.migrate(meta)
        scanner.schema(operational)
        migrate_operational(operational)
        tables = {row[0] for row in meta.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        subscribed = {int(row[0]) for row in meta.execute("SELECT anime_id FROM runtime_watch WHERE state='active'")}
        if "ani_rss_subscription" in tables:
            subscribed.update(int(row[0]) for row in meta.execute(
                "SELECT anime_id FROM ani_rss_subscription WHERE anime_id IS NOT NULL AND deleted_at IS NULL"))
        owners = sorted({runtime.physical_anime_id(meta, anime_id) for anime_id in subscribed})
        summary["total"] = len(owners)
        aliases: dict[int, list[str]] = {}
        for anime_id, title in meta.execute("SELECT anime_id,title FROM anime_title"):
            aliases.setdefault(int(anime_id), []).append(str(title))
        index = layout.ExistingPathIndex(root, config.get("library", {}).get("ignoredContainers", []))
        transaction_id = uuid.uuid4().hex
        for anime_id in owners:
            if throttle:
                throttle()
            anime = meta.execute("SELECT * FROM anime_work WHERE id=?", (anime_id,)).fetchone()
            created_paths: list[tuple[Path, tuple[int, int]]] = []
            event_id = None
            try:
                if anime is None:
                    raise ValueError("subscribed work has no Catalog identity")
                # Preserve previously registered targets across Archive title/date changes.
                registered = [row for row in operational.execute("SELECT * FROM anime_work WHERE scope_state='active'")
                              if str(json.loads(row["evidence_json"]).get("bangumiSubjectId")) == str(anime["bgm_id"])]
                if len(registered) > 1:
                    raise ValueError("subscribed work has multiple registered library targets")
                if registered:
                    row = registered[0]
                    planned = {"target": row["target_unc"], "directory": row["directory_name"], "series": row["series_unc"]}
                else:
                    planned = mapper.work_target(anime, mapper.relation_component(meta, anime_id), aliases, index, config)
                target = Path(planned["target"]).absolute()
                _guard(root, target)
                collision = operational.execute("SELECT * FROM anime_work WHERE target_unc=?", (str(target),)).fetchone()
                if collision and str(json.loads(collision["evidence_json"]).get("bangumiSubjectId")) != str(anime["bgm_id"]):
                    raise ValueError("directory is registered to a different work")
                already_exists = target.is_dir()
                evidence = {"bangumiSubjectId": anime["bgm_id"], "method": "subscribed_placeholder", "automated": True}
                if not already_exists:
                    event = meta.execute("""INSERT INTO library_history_event(transaction_id,operation,target_path,
                        object_kind,bytes,product_created,state,details_json,created_at)
                        VALUES(?,'create_directory',?,'directory',0,1,'planned',?,?)""",
                        (transaction_id, str(target), json.dumps(evidence), history.utcnow()))
                    event_id = int(event.lastrowid)
                    meta.commit()
                    if status_for_path(root, require_write=True, timeout=4.0).state != AVAILABLE or not root.is_dir():
                        raise StorageUnavailableError("local library disconnected during directory creation")
                    pending = []
                    current = target
                    while current != root and not current.exists():
                        pending.append(current)
                        current = current.parent
                    for directory in reversed(pending):
                        _guard(root, directory)
                        try:
                            directory.mkdir()
                        except FileExistsError:
                            _guard(root, directory)
                        else:
                            identity = directory.stat()
                            created_paths.append((directory, (identity.st_dev, identity.st_ino)))
                _guard(root, target)
                observed = index.exact(target)
                library_state = "existing" if observed and observed["hasMedia"] else "placeholder"
                operational.execute("""INSERT INTO anime_work(target_unc,directory_name,series_unc,official_title,date_code,
                    relation_state,scope_state,library_state,evidence_json,verified_at)
                    VALUES(?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(target_unc) DO UPDATE SET
                    library_state=CASE WHEN anime_work.library_state='existing' THEN 'existing' ELSE excluded.library_state END,
                    verified_at=excluded.verified_at""",
                    (str(target), planned["directory"], planned["series"], anime["title_ja"], mapper.date_code(anime["start_month"]),
                     "series_member" if planned["series"] else "standalone", library_state, json.dumps(evidence), history.utcnow()))
                operational.commit()
                meta.execute("UPDATE library_history_event SET state='applied' WHERE operation='create_directory' AND target_path=? AND state='planned'",
                             (str(target),))
                meta.commit()
                summary["existing" if already_exists else "created"] += 1
                if not observed:
                    index._append(target, Path(planned["series"]) if planned["series"] else None,
                                  mapper.date_code(anime["start_month"]), str(anime["title_ja"]))
            except (OSError, ValueError, sqlite3.Error) as exc:
                operational.rollback()
                # Roll back only our unchanged, still-empty directories, deepest first.
                for directory, identity in reversed(created_paths):
                    try:
                        _guard(root, directory)
                        current_stat = directory.stat()
                        if (current_stat.st_dev, current_stat.st_ino) == identity:
                            directory.rmdir()
                    except (OSError, ValueError):
                        pass  # A concurrent addition or replacement must survive rollback.
                if event_id:
                    meta.execute("UPDATE library_history_event SET state='failed' WHERE event_id=?", (event_id,))
                    meta.commit()
                summary["skipped" if isinstance(exc, ValueError) else "failed"] += 1
                summary["errors"].append({"animeId": anime_id, "error": str(exc)})
            summary["done"] += 1
            summary["currentTitle"] = str(anime["title_zh_hans"] or anime["title_ja"]) if anime else ""
            if progress:
                progress(dict(summary))
        runtime.sync_overlay(metadata_db, runtime_db)
    return summary
