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
from ..config.collector import settings as collector_settings
from .collector_filter import CatalogMatcher, parse_release_title
from .metainfo import inspect_bytes


def keywords(db_path: Path, anime_id: int) -> tuple[int, list[str]]:
    from .content_policy import require_allowed
    require_allowed(db_path, anime_id)
    with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
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
             progress: Callable[[dict[str, Any]], None] | None = None,
             config: dict[str, Any] | None = None) -> dict[str, Any]:
    # Import in this dedicated process so per-request budgets do not mutate
    # the long-running Collector's configuration or global state.
    from . import collector
    bgm_id, terms = keywords(db_path, anime_id)
    options = collector_settings(config or {})['search']
    source_names = {'nyaa': 'nyaa-desc', 'mikan': 'mikan', 'acgrip': 'acgrip'}
    fetchers = sources if sources is not None else {source_names[name]: collector.HISTORY_FETCHERS[source_names[name]] for name in options['sources']}
    download = download or collector.download_native
    # Background cover/overlay writes must not reload every title alias for
    # every release. The requested stable identity is checked again at exit.
    matcher = CatalogMatcher(db_path, snapshot=True)
    matcher.generation()
    stats: dict[str, Any] = {"sourcesDone": 0, "sourcesTotal": len(fetchers), "found": 0,
                             "sourcesFailed": 0, "saved": 0, "failed": 0, "bgmId": bgm_id, "errors": []}
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + options['timeoutSeconds']
    def publish() -> None:
        if progress:
            progress(dict(stats))
    publish()
    def fetch(source: str, adapter: Callable[..., Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
        rows, errors = [], []
        for term in terms[:2]:
            if time.monotonic() >= deadline:
                break
            try:
                items, _ = adapter(term, 1)
                rows.extend(items[:options['maxResults']])
            except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
                errors.append(type(exc).__name__)
        return source, rows, errors
    rows = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(3, len(fetchers))), thread_name_prefix="anm-title-source")
    futures = {pool.submit(fetch, source, adapter): source for source, adapter in fetchers.items()}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=min(20, options['timeoutSeconds'] / 3)):
            source, items, errors = future.result()
            rows.extend((source, item) for item in items)
            stats["sourcesDone"] += 1
            if errors:
                stats["errors"].append({"source": source, "error": errors[-1]})
                if len(errors) == len(terms[:2]):
                    stats["sourcesFailed"] += 1
            publish()
    except concurrent.futures.TimeoutError:
        for future, source in futures.items():
            if not future.done():
                stats['sourcesFailed'] += 1
                stats['errors'].append({'source': source, 'error': 'timeout'})
        publish()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Round-robin sources so a large feed cannot consume the entire budget.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for source, item in rows:
        by_source.setdefault(source, []).append(item)
    rows = [(source, items[index]) for index in range(max(map(len, by_source.values()), default=0))
            for source, items in by_source.items() if index < len(items)]
    seen: set[str] = set()
    hashes: set[str] = set()
    attempted: dict[str, int] = {}
    for source, item in rows:
        title = str(item.get("title") or "")
        from .content_policy import excluded_text, marked
        if marked(item.get('nsfw')) or excluded_text(title):
            continue
        key = str(item.get("download_url") or item.get("details_url") or "")
        if not key or key in seen or attempted.get(source, 0) >= options['maxResults']:
            continue
        seen.add(key)
        evidence = matcher.match(title, parse_release_title(title))
        if evidence.status != "exact" or evidence.anime_id != anime_id:
            continue
        stats["found"] += 1
        if len(hashes) >= options['maxResults'] or time.monotonic() > deadline:
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
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    def progress(value: dict[str, Any]) -> None:
        args.status.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.status.with_suffix(".pending")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.status)
    from ..config.policy import ConfigStore
    from ..config.loader import PACKAGE_ROOT
    config = ConfigStore(args.config, PACKAGE_ROOT / 'resources/config.example.json').read() if args.config else None
    result = discover(args.db, args.anime_id, args.out, progress=progress, config=config)
    progress(result)


if __name__ == "__main__":
    main()
