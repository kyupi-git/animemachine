"""Bounded per-title discovery using the built-in Collector's source adapters.

This worker caches validated Torrent metadata in application state. It neither
starts downloads nor writes to a read-only Torrent pool or media library.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

import httpx

from ..library.layout import portable_directory_component
from .collector_filter import CatalogMatcher, parse_release_title
from .metainfo import inspect_bytes


def keywords(db_path: Path, anime_id: int) -> tuple[int, list[str]]:
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        work = db.execute("SELECT bgm_id,title_ja,title_zh_hans,title_en FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if work is None:
            raise ValueError("anime not found")
        values = [work[1], work[2], work[3]]
        values.extend(row[0] for row in db.execute("SELECT title FROM anime_title WHERE anime_id=? AND language IN ('ja','en')", (anime_id,)))
    terms = list(dict.fromkeys(str(value).strip() for value in values if value and len(str(value).strip()) >= 2))
    return int(work[0]), terms[:3]


def discover(db_path: Path, anime_id: int, directory: Path, *,
             sources: dict[str, Callable[..., Any]] | None = None,
             download: Callable[..., tuple[bytes, str]] | None = None,
             progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    # Import in this dedicated process so per-request budgets do not mutate
    # the long-running Collector's configuration or global state.
    from . import collector
    bgm_id, terms = keywords(db_path, anime_id)
    fetchers = sources or {name: collector.HISTORY_FETCHERS[name] for name in ("nyaa-desc", "mikan", "acgrip")}
    download = download or collector.download_native
    # Background cover/overlay writes must not reload every title alias for
    # every release. The requested stable identity is checked again at exit.
    matcher = CatalogMatcher(db_path, snapshot=True)
    matcher.generation()
    stats: dict[str, Any] = {"sourcesDone": 0, "sourcesTotal": len(fetchers), "found": 0,
                             "sourcesFailed": 0, "saved": 0, "failed": 0, "bgmId": bgm_id, "errors": []}
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 120
    def publish() -> None:
        if progress:
            progress(dict(stats))
    publish()
    def fetch(source: str, adapter: Callable[..., Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        for term in terms[:2]:
            try:
                items, _ = adapter(term, 1)
                rows.extend(items[:30])
            except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
                errors.append(type(exc).__name__)
        return source, rows, errors
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="anm-title-source") as pool:
        futures = [pool.submit(fetch, source, adapter) for source, adapter in fetchers.items()]
        for future in concurrent.futures.as_completed(futures):
            source, items, errors = future.result()
            rows.extend((source, item) for item in items)
            stats["sourcesDone"] += 1
            if errors:
                stats["errors"].append({"source": source, "error": errors[-1]})
                if len(errors) == len(terms[:2]):
                    stats["sourcesFailed"] += 1
            publish()
    seen: set[str] = set()
    hashes: set[str] = set()
    attempted: dict[str, int] = {}
    for source, item in rows:
        title = str(item.get("title") or "")
        key = str(item.get("download_url") or item.get("details_url") or "")
        if not key or key in seen or attempted.get(source, 0) >= 6:
            continue
        seen.add(key)
        evidence = matcher.match(title, parse_release_title(title))
        if evidence.status != "exact" or evidence.anime_id != anime_id:
            continue
        stats["found"] += 1
        if len(hashes) >= 16 or time.monotonic() > deadline:
            break
        attempted[source] = attempted.get(source, 0) + 1
        try:
            raw, _ = download(item, source.split("-", 1)[0])
            metadata = inspect_bytes(raw, filename=title, include_files=True)
            info_hash = str(metadata["infoHash"]).lower()
            if info_hash in hashes:
                continue
            hashes.add(info_hash)
            filename = portable_directory_component(title[:140]) + f" [{info_hash}].torrent"
            target = directory / filename
            if not target.is_file() or target.read_bytes() != raw:
                temporary = target.with_suffix(".pending")
                temporary.write_bytes(raw)
                temporary.replace(target)
            stats["saved"] += 1
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            stats["failed"] += 1
            stats["errors"].append({"source": source, "error": type(exc).__name__})
        publish()
    # Recheck stable identity before the caller indexes cached data.
    if keywords(db_path, anime_id)[0] != bgm_id:
        raise RuntimeError("Catalog identity changed; search again")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--anime-id", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--status", required=True, type=Path)
    args = parser.parse_args()
    def progress(value: dict[str, Any]) -> None:
        args.status.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.status.with_suffix(".pending")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.status)
    result = discover(args.db, args.anime_id, args.out, progress=progress)
    progress(result)


if __name__ == "__main__":
    main()
