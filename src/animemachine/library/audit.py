"""Incremental, metadata-only library completeness assessment.

The audit never opens media payloads. It compares names, kinds and byte sizes
with the highest-ranked eligible torrent manifest and persists compact evidence.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Callable, Iterable

from ..torrents import runtime as runtime_catalog
from ..config.policy import ConfigStore
from ..storage import AVAILABLE, StorageUnavailableError, status_for_path


EPISODE = re.compile(r"(?i)(?:^|[^a-z0-9])(?:ep?|episode|第)?\s*(\d{1,4})(?:\s*[-_. ]?v\d+)?(?:[^a-z0-9]|$)")


def _signature(path: str, length: int, kind: str) -> dict[str, Any]:
    name = Path(path.replace("\\", "/")).stem.casefold()
    episode = EPISODE.search(name)
    tokens = set(re.findall(r"[a-z]{2,}|\d+|[\u3040-\u30ff\u3400-\u9fff]{2,}", name))
    return {"path": path, "bytes": max(0, int(length or 0)), "kind": kind,
            "episode": episode.group(1).lstrip("0") if episode else None, "tokens": tokens}


def _weight(kind: str) -> float:
    return {"main_video": 10, "subtitle": 2, "cd_audio": 3, "audio": 2,
            "bonus_video": 2, "scans": 1, "images": 1, "other": .5}.get(kind, 0)


def distribution_similarity(expected: Iterable[dict[str, Any]], observed: Iterable[dict[str, Any]]) -> float:
    wanted = [x for x in expected if _weight(x["kind"]) > 0]
    actual = list(observed)
    total = sum(_weight(x["kind"]) for x in wanted)
    if not total:
        return 0.0
    remaining = set(range(len(actual)))
    earned = 0.0
    for item in sorted(wanted, key=lambda x: -_weight(x["kind"])):
        candidates = []
        for index in remaining:
            other = actual[index]
            if other["kind"] != item["kind"]:
                continue
            size_ratio = abs(other["bytes"] - item["bytes"]) / max(item["bytes"], 1)
            episode_match = bool(item["episode"] and item["episode"] == other["episode"])
            token_overlap = len(item["tokens"] & other["tokens"]) / max(len(item["tokens"]), 1)
            score = (1.0 if size_ratio <= .05 else .7 if size_ratio <= .2 else .25) + (1.0 if episode_match else 0) + token_overlap
            candidates.append((score, index, size_ratio, episode_match, token_overlap))
        if not candidates:
            continue
        score, index, size_ratio, episode_match, overlap = max(candidates)
        remaining.remove(index)
        confidence = 1.0 if size_ratio <= .05 or episode_match or overlap >= .5 else .55
        earned += _weight(item["kind"]) * confidence
    return round(min(100.0, earned * 100.0 / total), 2)


def _state(score: float) -> str:
    if score >= 95: return "complete"
    if score >= 80: return "near_complete"
    if score >= 60: return "partial_high"
    if score >= 30: return "partial"
    return "incomplete"


def _expected(db: sqlite3.Connection, anime_id: int, info_hash: str) -> list[dict[str, Any]]:
    links = list(db.execute("""SELECT rw.directory_name FROM runtime_torrent_work tw JOIN runtime_work rw
        ON rw.private_work_id=tw.private_work_id WHERE tw.anime_id=? AND tw.info_hash=?""", (anime_id, info_hash)))
    all_links = db.execute("SELECT COUNT(DISTINCT anime_id) FROM runtime_torrent_work WHERE info_hash=?", (info_hash,)).fetchone()[0]
    directories = {str(row[0]).casefold().replace("\\", "/") for row in links}
    rows = []
    for row in db.execute("""SELECT f.source_path,f.length,f.file_kind,m.target_relative_path,m.selected
        FROM runtime_torrent_file f LEFT JOIN runtime_file_map m ON m.info_hash=f.info_hash AND m.file_index=f.file_index
        WHERE f.info_hash=?""", (info_hash,)):
        target = str(row[3] or row[0]).casefold().replace("\\", "/")
        if all_links > 1 and not any(target == d or target.startswith(d + "/") for d in directories):
            continue
        if row[4] is not None and not row[4]:
            continue
        rows.append(_signature(str(row[3] or row[0]), int(row[1]), str(row[2])))
    return rows


def _observed(paths: Iterable[str], cache: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """Return file signatures while scanning each physical root at most once.

    Several logical works can intentionally share one series or merged-season
    directory.  Reusing the immutable metadata snapshot avoids recursively
    walking the same remote tree once per catalog row.
    """
    rows = []
    for raw in paths:
        cache_key = str(raw).replace("/", "\\").casefold()
        if cache is not None and cache_key in cache:
            rows.extend(cache[cache_key])
            continue
        root = Path(raw)
        storage = status_for_path(root, timeout=4.0)
        if storage.state != AVAILABLE:
            raise StorageUnavailableError(f"storage unavailable: {root}")
        try:
            root_stat = root.stat()
        except FileNotFoundError:
            if cache is not None:
                cache[cache_key] = []
            continue
        except OSError as exc:
            raise StorageUnavailableError(exc.errno or 0, f"storage unavailable: {root}") from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            if cache is not None:
                cache[cache_key] = []
            continue
        found = []
        pending = [str(root)]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for item in entries:
                        lowered = item.name.casefold()
                        if item.is_dir(follow_symlinks=False):
                            if lowered not in {".anm-history", ".anm-staging"}:
                                pending.append(item.path)
                        elif item.is_file(follow_symlinks=False) and lowered not in {"empty.txt", "deprecated.txt"}:
                            found.append(_signature(item.path, item.stat(follow_symlinks=False).st_size,
                                                    runtime_catalog.file_kind(item.path)))
            except OSError as exc:
                raise StorageUnavailableError(exc.errno or 0, f"storage unavailable: {root}") from exc
        if cache is not None:
            cache[cache_key] = found
        rows.extend(found)
    return rows


def _verify_hash_baselines(db: sqlite3.Connection, owner_paths: list[str],
                           throttle: Callable[[], None] | None = None) -> dict[str, int]:
    if not owner_paths:
        return {"compared": 0, "matched": 0, "mismatched": 0, "unavailable": 0}
    marks = ",".join("?" for _ in owner_paths)
    rows = list(db.execute(
        f"SELECT final_path,bytes,sha256 FROM runtime_asset WHERE owner_path IN ({marks}) AND sha256 IS NOT NULL AND sha256<>''",
        owner_paths))
    result = {"compared": 0, "matched": 0, "mismatched": 0, "unavailable": 0}
    for final_path, expected_bytes, expected_sha in rows:
        if throttle:
            throttle()
        path = Path(str(final_path))
        try:
            before = path.stat()
            if expected_bytes is not None and before.st_size != int(expected_bytes):
                result["compared"] += 1; result["mismatched"] += 1; continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
                    if throttle:
                        throttle()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                result["unavailable"] += 1; continue
            result["compared"] += 1
            result["matched" if digest.hexdigest().casefold() == str(expected_sha).casefold() else "mismatched"] += 1
        except (OSError, PermissionError):
            result["unavailable"] += 1
    return result


def audit(db_path: Path, config: dict[str, Any], *, anime_ids: list[int] | None = None,
          progress: Callable[[dict[str, int]], None] | None = None,
          commit_every: int = 100,
          throttle: Callable[[], None] | None = None) -> dict[str, int]:
    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=60000")
        runtime_catalog.migrate_overlay(db)
        ids = anime_ids or [int(row[0]) for row in db.execute("SELECT DISTINCT anime_id FROM runtime_work WHERE library_state='existing'")]
        updated = skipped = unavailable = 0
        observed_cache: dict[str, list[dict[str, Any]]] = {}
        for anime_id in ids:
            if throttle:
                throttle()
            works = list(db.execute("SELECT target_unc,origin FROM runtime_work WHERE anime_id=? AND library_state='existing'", (anime_id,)))
            if not works:
                skipped += 1; continue
            try:
                observed = _observed((str(row[0]) for row in works), observed_cache)
            except StorageUnavailableError:
                skipped += 1
                unavailable += 1
                continue
            torrents = [x for x in runtime_catalog.torrents_for_anime(db, anime_id, config) if x["eligible"]]
            if not torrents:
                db.execute("""INSERT INTO runtime_completeness VALUES(?,NULL,0,'unassessed','no_comparison_manifest',?,0,?,datetime('now'))
                        ON CONFLICT(anime_id) DO UPDATE SET preferred_info_hash=NULL,similarity=0,state='unassessed',
                        basis='no_comparison_manifest',observed_files=excluded.observed_files,expected_files=0,
                        evidence_json=excluded.evidence_json,assessed_at=excluded.assessed_at""",
                           (anime_id, len(observed), json.dumps({"method": "local_file_inventory_only", "paths": len(works)})))
                updated += 1
                continue
            preferred = torrents[0]
            submission = db.execute("SELECT qbt_state FROM runtime_submission WHERE info_hash=?", (preferred["infoHash"],)).fetchone()
            completed = bool(submission and str(submission[0]).casefold() in {"completed", "seeding", "pausedup", "stoppedup", "uploading"})
            expected = _expected(db, anime_id, preferred["infoHash"])
            score = 100.0 if completed else distribution_similarity(expected, observed)
            basis = "managed_completed" if completed else "metadata_distribution"
            exact = {"compared": 0, "matched": 0, "mismatched": 0, "unavailable": 0}
            if config.get("differentialPlanning", {}).get("samePathSizePolicy") == "hash_and_skip":
                exact = _verify_hash_baselines(db, [str(row[0]) for row in works], throttle)
                if exact["mismatched"]:
                    score = 0.0
                    basis = "hash_mismatch"
            evidence = {"method": "kind_episode_name_size_v1", "paths": len(works),
                        "managed": any(str(row[1]) == "managed_submission" for row in works),
                        "hashVerification": exact}
            db.execute("""INSERT INTO runtime_completeness VALUES(?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(anime_id) DO UPDATE SET preferred_info_hash=excluded.preferred_info_hash,
                    similarity=excluded.similarity,state=excluded.state,basis=excluded.basis,
                    observed_files=excluded.observed_files,expected_files=excluded.expected_files,
                    evidence_json=excluded.evidence_json,assessed_at=excluded.assessed_at""",
                       (anime_id, preferred["infoHash"], score, _state(score), basis, len(observed), len(expected), json.dumps(evidence)))
            updated += 1
            processed = updated + skipped
            if processed % max(10, commit_every) == 0:
                db.commit()
                if progress:
                    progress({"updated": updated, "skipped": skipped, "unavailable": unavailable,
                              "processed": processed, "total": len(ids)})
        db.commit()
        if progress:
            progress({"updated": updated, "skipped": skipped, "unavailable": unavailable,
                      "processed": len(ids), "total": len(ids)})
        return {"updated": updated, "skipped": skipped, "unavailable": unavailable, "total": len(ids)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = ConfigStore(args.config, args.config).read()
    print(json.dumps(audit(args.db, config), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
