#!/usr/bin/env python3
"""Conservative Archive-only mapper for fresh product installations.

It automatically accepts only unique, exact, single-work matches. Collections,
partial releases and ambiguous aliases remain in the operational review queue.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from ..library import layout as library_layout
from ..storage import StorageUnavailableError


MEDIA = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm"}
PARTIAL = re.compile(r"(?i)(?:^|\W)(?:vol(?:ume)?[ ._-]*\d+|ep(?:isode)?[ ._-]*\d+|\s-\s*\d{1,4})(?:\W|$)")
SEASON_SUFFIX = re.compile(r"(?i)\s*(?:第[二三四五六七八九十0-9]+期|\d+(?:st|nd|rd|th)?\s*season|season\s*\d+|s\d+)$")

from .title_identity import norm, queries

def join_root(root: str, child: str) -> str:
    if root.startswith("/"):
        return str(PurePosixPath(root) / child)
    return str(PureWindowsPath(root) / child)


def common_top(paths: list[str]) -> str | None:
    tops = {path.replace("\\", "/").split("/", 1)[0] for path in paths if "/" in path.replace("\\", "/")}
    return next(iter(tops)) if len(tops) == 1 and all("/" in path.replace("\\", "/") for path in paths) else None


def target_relative(path: str, top: str | None, child_dir: str | None) -> str:
    normalized = path.replace("\\", "/")
    if top and normalized.startswith(top + "/"):
        normalized = normalized[len(top) + 1:]
    return f"{child_dir}/{normalized}" if child_dir else normalized


def relation_component(meta: sqlite3.Connection, anime_id: int) -> list[sqlite3.Row]:
    pending = [anime_id]; seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        bgm = meta.execute("SELECT bgm_id FROM anime_work WHERE id=?", (current,)).fetchone()[0]
        for row in meta.execute("""SELECT aw.id FROM anime_relation r JOIN anime_work aw ON aw.bgm_id=r.related_bgm_id
                WHERE r.anime_id=? AND r.strict_group=1 UNION SELECT r.anime_id FROM anime_relation r
                WHERE r.related_bgm_id=? AND r.strict_group=1""", (current, bgm)):
            if int(row[0]) not in seen:
                pending.append(int(row[0]))
    return list(meta.execute("SELECT * FROM anime_work WHERE id IN (%s) ORDER BY start_month,id" % ",".join("?" * len(seen)), tuple(seen)))


def date_code(start_month: str) -> str:
    return start_month.replace("-", "_") if re.fullmatch(r"\d{4}-(?:\d{2}|XX)", start_month or "") else "20XX_XX"


def series_title(rows: list[sqlite3.Row]) -> str:
    return library_layout.franchise_title([dict(row) for row in rows])


def auto_map(metadata_db: Path, runtime_db: Path, config: dict[str, Any], *,
             progress: Callable[[dict[str, int]], None] | None = None,
             commit_every: int = 250,
             info_hashes: set[str] | None = None) -> dict[str, int]:
    meta = sqlite3.connect(metadata_db); meta.row_factory = sqlite3.Row
    runtime = sqlite3.connect(runtime_db); runtime.row_factory = sqlite3.Row
    hash_clause = ""
    hash_values: tuple[str, ...] = ()
    if info_hashes:
        hash_values = tuple(sorted(value.casefold() for value in info_hashes))
        hash_clause = " AND t.info_hash IN (%s)" % ",".join("?" for _ in hash_values)
    torrent_rows = list(runtime.execute("""SELECT t.* FROM torrent t WHERE t.scan_state!='reject'
        AND COALESCE(t.title_state,'unmapped') IN ('unmapped','review')
        AND NOT EXISTS(SELECT 1 FROM torrent_work tw WHERE tw.info_hash=t.info_hash AND tw.mapping_state='verified')
        """ + hash_clause + " ORDER BY t.info_hash", hash_values))
    if not torrent_rows:
        runtime.close(); meta.close()
        return {"examined": 0, "mapped": 0, "review": 0, "partial": 0}
    by_title: dict[str, set[int]] = {}
    for row in meta.execute("SELECT anime_id,title FROM anime_title"):
        by_title.setdefault(norm(row["title"]), set()).add(int(row["anime_id"]))
    root = str(config["deployment"]["libraryUncRoot"])
    aliases_by_id: dict[int, list[str]] = {}
    for anime_id, title in meta.execute("SELECT anime_id,title FROM anime_title"):
        aliases_by_id.setdefault(int(anime_id), []).append(str(title))
    path_index = library_layout.ExistingPathIndex(Path(root), config.get("library", {}).get("ignoredContainers", []))
    group_rank = {str(group["name"]).casefold(): int(group["tier"]) * 100 + int(group["order"])
                  for group in config["torrentPolicy"].get("resourceGroups", []) if group.get("enabled", True)}
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    stats = {"examined": 0, "mapped": 0, "review": 0, "partial": 0}
    component_cache: dict[int, list[sqlite3.Row]] = {}
    existing_target_cache: dict[int, dict[str, Any] | None] = {}
    for torrent in torrent_rows:
        stats["examined"] += 1
        name = str(torrent["info_name"] or Path(torrent["torrent_path"]).stem)
        release_unit = str(torrent["release_unit"] or "unknown")
        observed_names = [name, Path(str(torrent["torrent_path"])).stem]
        title_queries = list(dict.fromkeys(
            query for observed in observed_names for query in queries(observed, partial=release_unit in {"episode", "volume"})))
        manifest = list(runtime.execute("SELECT * FROM torrent_manifest_file WHERE info_hash=? ORDER BY file_index", (torrent["info_hash"],)))
        media = [row for row in manifest if PurePosixPath(str(row["source_path"]).replace("\\", "/")).suffix.casefold() in MEDIA]
        candidates: set[int] = set()
        for query in title_queries:
            matches = by_title.get(norm(query), set())
            if len(matches) == 1:
                candidates.update(matches)
        reasons: list[str] = []
        if PARTIAL.search(name) or release_unit in {"episode", "volume"}:
            stats["partial"] += 1
        if len(candidates) != 1:
            reasons.append("no_unique_exact_archive_title" if not candidates else "multiple_exact_archive_titles")
        anime = meta.execute("SELECT * FROM anime_work WHERE id=?", (next(iter(candidates)),)).fetchone() if len(candidates) == 1 else None
        if anime:
            if library_layout.physical_role(dict(anime)) == "supplement":
                reasons.append("supplement_uses_parent_physical_target")
            expected = int(anime["episode_count"] or 0)
            # Collection manifests commonly include many menus, NCOP/NCED and
            # video specials.  An upper bound based on the broadcast episode
            # count therefore rejects complete archival releases.  A short
            # manifest remains reviewable; surplus videos are handled later by
            # manifest classification and completeness ranking.
            if expected and release_unit not in {"episode", "volume"} and len(media) < expected:
                reasons.append("main_video_count_mismatch")
            start = str(anime["start_month"] or "")
            created = str(torrent["torrent_created_at"] or "")[:7]
            if re.fullmatch(r"\d{4}-\d{2}", start) and created and created < start:
                reasons.append("torrent_predates_work")
        if reasons:
            runtime.execute("UPDATE torrent SET title_state='review' WHERE info_hash=?", (torrent["info_hash"],))
            runtime.execute("""INSERT INTO title_review(info_hash,reason_codes_json,candidate_json,evidence_json,reviewed_at)
                VALUES(?,?,?,?,?) ON CONFLICT(info_hash) DO UPDATE SET reason_codes_json=excluded.reason_codes_json,
                candidate_json=excluded.candidate_json,evidence_json=excluded.evidence_json,reviewed_at=excluded.reviewed_at""",
                (torrent["info_hash"], json.dumps(reasons), json.dumps(sorted(candidates)), json.dumps({"queries": title_queries}, ensure_ascii=False), stamp))
            stats["review"] += 1
            continue
        assert anime is not None
        logical_anime_id = int(anime["id"])
        owner_id = int(anime["physical_owner_anime_id"] or logical_anime_id) if "physical_owner_anime_id" in anime.keys() else logical_anime_id
        while owner_id != logical_anime_id:
            owner = meta.execute("SELECT * FROM anime_work WHERE id=?", (owner_id,)).fetchone()
            if not owner or not owner["physical_owner_anime_id"]:
                if owner:
                    anime = owner
                break
            logical_anime_id = owner_id
            owner_id = int(owner["physical_owner_anime_id"])
        anime_id = int(anime["id"])
        component = component_cache.get(anime_id)
        if component is None:
            component = relation_component(meta, anime_id)
            for member in component:
                component_cache[int(member["id"])] = component
        aliases = aliases_by_id.get(anime_id, [])
        if anime_id not in existing_target_cache:
            existing_target_cache[anime_id] = path_index.resolve(
                date_code(anime["start_month"]), str(anime["title_ja"]), aliases)
        existing_target = existing_target_cache[anime_id]
        directory_title = library_layout.compound_title_from_evidence(
            str(anime["title_ja"]), aliases, [*observed_names, *title_queries], existing_target["title"] if existing_target else None)
        work_dir = library_layout.format_work_directory(
            config["naming"]["workTemplate"], date=date_code(anime["start_month"]), title=directory_title)
        series_path = None
        if existing_target:
            target = str(existing_target["path"])
            work_dir = Path(target).name
            series_path = str(existing_target["series"]) if existing_target["series"] else None
        elif len(component) > 1:
            existing_series = set()
            for row in component:
                member_id = int(row["id"])
                if member_id not in existing_target_cache:
                    existing_target_cache[member_id] = path_index.resolve(
                        date_code(row["start_month"]), str(row["title_ja"]), aliases_by_id.get(member_id, []))
                match = existing_target_cache[member_id]
                if match and match["series"] is not None:
                    existing_series.add(str(match["series"]))
            known = [date_code(row["start_month"]) for row in component if re.fullmatch(r"\d{4}-\d{2}", str(row["start_month"] or ""))]
            span = f"{min(known)}－{max(known)}" if known else "20XX_XX－20XX_XX"
            if len(existing_series) == 1:
                series_path = existing_series.pop()
            else:
                root_work = library_layout.choose_franchise_root([dict(row) for row in component])
                root_aliases = aliases_by_id.get(int(root_work["id"]), [])
                title = library_layout.compound_title_from_evidence(series_title(component), root_aliases, [*observed_names, *title_queries])
                series_dir = library_layout.format_series_directory(
                    config["naming"]["seriesTemplate"], start=span.split("－")[0],
                    end=span.split("－")[1], title=title)
                series_path = join_root(root, series_dir)
            target = join_root(series_path, work_dir)
        else:
            target = join_root(root, work_dir)
        evidence = json.dumps({"bangumiSubjectId": anime["bgm_id"], "method": "unique_exact_archive_title", "automated": True}, ensure_ascii=False)
        existing = runtime.execute("SELECT work_id FROM anime_work WHERE target_unc=?", (target,)).fetchone()
        if existing:
            work_id = int(existing[0])
        else:
            library_state = (
                "existing" if existing_target and existing_target["hasMedia"]
                else "placeholder" if existing_target
                else "absent"
            )
            cursor = runtime.execute("""INSERT INTO anime_work(target_unc,directory_name,series_unc,official_title,date_code,mal_id,
                relation_state,scope_state,library_state,placeholder_content,evidence_json,verified_at)
                VALUES(?,?,?,?,?,NULL,?,'active',?,NULL,?,?)""",
                (target, work_dir, series_path, anime["title_ja"], date_code(anime["start_month"]),
                 "series_member" if series_path else "standalone", library_state, evidence, stamp))
            work_id = int(cursor.lastrowid)
        rank = group_rank.get(str(torrent["effective_group"] or "").casefold(), 9999)
        ranking = json.dumps({"resourceGroup": rank, "resolution": -(int(torrent["video_height"] or 0)),
                              "bitDepth": -(int(torrent["bit_depth"] or 0)), "creationDate": torrent["torrent_created_at"]}, ensure_ascii=False)
        runtime.execute("INSERT OR REPLACE INTO torrent_work(info_hash,work_id,role,mapping_state,evidence_json,priority_rank,ranking_key_json,ranking_policy_version) VALUES(?,?,'primary','verified',?,?,?,'product-v2')",
                        (torrent["info_hash"], work_id, evidence, rank, ranking))
        runtime.execute("DELETE FROM file_map WHERE info_hash=?", (torrent["info_hash"],))
        top = common_top([str(row["source_path"]) for row in manifest])
        # A verified single-work torrent is saved directly into ``target``.
        # Its qBittorrent rename paths must therefore be relative to that
        # directory even when the work itself belongs to a series.  Child
        # prefixes are reserved for true multi-work collection partitions.
        child = None
        runtime.executemany("INSERT INTO file_map(info_hash,file_index,source_path,target_relative_path,length,selected,selection_reason) VALUES(?,?,?,?,?,1,'unique_exact_single_work')",
                            ((torrent["info_hash"], row["file_index"], row["source_path"], target_relative(row["source_path"], top, child), row["length"]) for row in manifest))
        runtime.execute("UPDATE torrent SET title_state='mapped',primary_work_name=? WHERE info_hash=?", (anime["title_ja"], torrent["info_hash"]))
        runtime.execute("DELETE FROM title_review WHERE info_hash=?", (torrent["info_hash"],))
        runtime.execute("INSERT OR REPLACE INTO torrent_target_path(info_hash,target_ordinal,target_unc,target_state,work_id,confidence,basis,updated_at) VALUES(?,1,?,'verified',?,1.0,'unique exact Archive title',?)",
                        (torrent["info_hash"], target, work_id, stamp))
        stats["mapped"] += 1
        if stats["examined"] % max(25, int(commit_every)) == 0:
            runtime.commit()
            if progress:
                progress({**stats, "total": len(torrent_rows)})
    runtime.commit()
    if progress:
        progress({**stats, "total": len(torrent_rows)})
    runtime.close(); meta.close()
    return stats


def remap_hashes(metadata_db: Path, runtime_db: Path, config: dict[str, Any],
                 info_hashes: set[str], *,
                 progress: Callable[[dict[str, int]], None] | None = None) -> dict[str, int]:
    """Rebuild selected automated identity/path mappings under current rules.

    This is intentionally explicit and hash-scoped.  It is used after a
    mapper/layout rule improves; it never rewrites user-reviewed mappings in
    bulk.  Orphaned automated work rows are removed only when no torrent or
    collection member still references them.
    """
    hashes = {str(value).casefold() for value in info_hashes if value}
    if not hashes:
        return {"examined": 0, "mapped": 0, "review": 0, "partial": 0}
    placeholders = ",".join("?" for _ in hashes)
    values = tuple(sorted(hashes))
    with contextlib.closing(sqlite3.connect(runtime_db)) as runtime, runtime:
        old_work_ids = [int(row[0]) for row in runtime.execute(
            f"SELECT DISTINCT work_id FROM torrent_work WHERE info_hash IN ({placeholders})", values)]
        runtime.execute(f"DELETE FROM torrent_work WHERE info_hash IN ({placeholders})", values)
        runtime.execute(f"DELETE FROM torrent_target_path WHERE info_hash IN ({placeholders})", values)
        runtime.execute(f"DELETE FROM file_map WHERE info_hash IN ({placeholders})", values)
        runtime.execute(f"DELETE FROM title_review WHERE info_hash IN ({placeholders})", values)
        runtime.execute(f"UPDATE torrent SET title_state='unmapped',primary_work_name=NULL WHERE info_hash IN ({placeholders})", values)
        for work_id in old_work_ids:
            runtime.execute("""DELETE FROM anime_work WHERE work_id=?
                AND json_extract(evidence_json,'$.automated')=1
                AND NOT EXISTS(SELECT 1 FROM torrent_work WHERE work_id=?)
                AND NOT EXISTS(SELECT 1 FROM anime_work_member WHERE owner_work_id=?)""",
                (work_id, work_id, work_id))
    return auto_map(metadata_db, runtime_db, config, progress=progress, info_hashes=hashes)


def reconcile_existing_paths(metadata_db: Path, runtime_db: Path, config: dict[str, Any], *,
                             progress: Callable[[dict[str, int]], None] | None = None,
                             commit_every: int = 250) -> dict[str, int]:
    """Consolidate absent automated targets onto unique existing library paths.

    Identity evidence is retained; only path ownership is reconciled.  A
    current target that already exists is never auto-merged because two
    occupied directories require the transactional library reconciler.
    """
    root = Path(str(config["deployment"]["libraryUncRoot"]))
    path_index = library_layout.ExistingPathIndex(
        root, config.get("library", {}).get("ignoredContainers", []))
    meta = sqlite3.connect(metadata_db); meta.row_factory = sqlite3.Row
    runtime = sqlite3.connect(runtime_db); runtime.row_factory = sqlite3.Row
    aliases_by_id: dict[int, list[str]] = {}
    for anime_id, title in meta.execute("SELECT anime_id,title FROM anime_title"):
        aliases_by_id.setdefault(int(anime_id), []).append(str(title))
    works = list(runtime.execute("""SELECT aw.* FROM anime_work aw
        WHERE json_extract(aw.evidence_json,'$.automated')=1
          AND EXISTS(SELECT 1 FROM torrent_work tw WHERE tw.work_id=aw.work_id AND tw.mapping_state='verified')
        ORDER BY aw.work_id"""))
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    stats = {"examined": 0, "reused": 0, "consolidated": 0, "occupied": 0, "unresolved": 0}
    for work in works:
        stats["examined"] += 1
        current = Path(str(work["target_unc"]))
        try:
            current_is_dir = stat.S_ISDIR(current.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            current_is_dir = False
        except OSError as exc:
            # A legacy automatically generated target may contain a character
            # rejected by the current host (notably ``*`` on Windows).  Treat
            # that one proposal as absent so the remaining library can still
            # be reconciled.  Fresh targets are portable at creation time.
            if getattr(exc, "winerror", None) == 123 or exc.errno == 22:
                current_is_dir = False
            else:
                raise StorageUnavailableError(exc.errno or 0, f"library storage unavailable: {root}") from exc
        if current_is_dir:
            indexed = path_index.exact(current)
            observed_state = "existing" if indexed and indexed["hasMedia"] else "placeholder"
            if str(work["library_state"]) != observed_state:
                runtime.execute("UPDATE anime_work SET library_state=?,verified_at=? WHERE work_id=?",
                                (observed_state, stamp, int(work["work_id"])))
            stats["occupied"] += 1
            continue
        try:
            evidence = json.loads(work["evidence_json"] or "{}")
            bgm_id = int(evidence.get("bangumiSubjectId") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            bgm_id = 0
        anime = meta.execute("SELECT * FROM anime_work WHERE bgm_id=?", (bgm_id,)).fetchone() if bgm_id else None
        if not anime:
            stats["unresolved"] += 1
            continue
        match = path_index.resolve(date_code(anime["start_month"]), str(anime["title_ja"]),
                                   aliases_by_id.get(int(anime["id"]), []))
        if not match or str(match["path"]) == str(current):
            stats["unresolved"] += 1
            continue
        canonical = runtime.execute("SELECT work_id FROM anime_work WHERE target_unc=?", (str(match["path"]),)).fetchone()
        old_id = int(work["work_id"])
        if canonical:
            new_id = int(canonical[0])
            links = list(runtime.execute("SELECT * FROM torrent_work WHERE work_id=?", (old_id,)))
            for link in links:
                runtime.execute("""INSERT OR REPLACE INTO torrent_work
                    (info_hash,work_id,role,mapping_state,evidence_json,priority_rank,ranking_key_json,ranking_policy_version)
                    VALUES(?,?,?,?,?,?,?,?)""", (
                    link["info_hash"], new_id, link["role"], link["mapping_state"], link["evidence_json"],
                    link["priority_rank"], link["ranking_key_json"], link["ranking_policy_version"]))
            runtime.execute("DELETE FROM torrent_work WHERE work_id=?", (old_id,))
            runtime.execute("UPDATE torrent_target_path SET target_unc=?,work_id=?,updated_at=? WHERE work_id=?",
                            (str(match["path"]), new_id, stamp, old_id))
            runtime.execute("DELETE FROM anime_work WHERE work_id=? AND NOT EXISTS(SELECT 1 FROM anime_work_member WHERE owner_work_id=?)",
                            (old_id, old_id))
            stats["consolidated"] += 1
        else:
            runtime.execute("""UPDATE anime_work SET target_unc=?,directory_name=?,series_unc=?,library_state=?,verified_at=?
                               WHERE work_id=?""",
                            (str(match["path"]), Path(match["path"]).name,
                             str(match["series"]) if match["series"] else None,
                             "existing" if match["hasMedia"] else "placeholder", stamp, old_id))
            runtime.execute("UPDATE torrent_target_path SET target_unc=?,updated_at=? WHERE work_id=?",
                            (str(match["path"]), stamp, old_id))
            stats["reused"] += 1
        if stats["examined"] % max(25, int(commit_every)) == 0:
            runtime.commit()
            if progress:
                progress(dict(stats))
    runtime.commit()
    if progress:
        progress(dict(stats))
    runtime.close(); meta.close()
    return stats


def remap_one_work(metadata_db: Path, runtime_db: Path, config: dict[str, Any], anime_id: int,
                   *, progress: Callable[[dict[str, int]], None] | None = None) -> dict[str, Any]:
    """Re-evaluate only pool rows whose parsed title equals one alias of a work.

    This is the on-demand path used by the Web UI.  It never turns a fuzzy
    substring into verified identity: the same unique exact-title rules used
    by the batch mapper still make the final decision.
    """
    with contextlib.closing(sqlite3.connect(metadata_db)) as meta:
        aliases = [str(row[0]) for row in meta.execute(
            "SELECT title FROM anime_title WHERE anime_id=?", (int(anime_id),))]
    wanted = {norm(value) for value in aliases if len(norm(value)) >= 3}
    hashes: set[str] = set()
    examined = 0
    with contextlib.closing(sqlite3.connect(runtime_db)) as runtime:
        runtime.row_factory = sqlite3.Row
        for row in runtime.execute("""SELECT info_hash,info_name,torrent_path FROM torrent
                WHERE scan_state!='reject' AND COALESCE(metadata_state,'available')='available'
                  AND COALESCE(title_state,'unmapped') IN ('unmapped','review')"""):
            examined += 1
            observed = [str(row["info_name"] or ""), Path(str(row["torrent_path"] or "")).stem]
            parsed = {norm(query) for value in observed for query in queries(value)}
            if wanted & parsed:
                hashes.add(str(row["info_hash"]))
    if progress:
        progress({"examined": examined, "candidates": len(hashes), "mapped": 0})
    result = auto_map(metadata_db, runtime_db, config, progress=progress, info_hashes=hashes) if hashes else {
        "examined": 0, "mapped": 0, "review": 0, "partial": 0}
    return {**result, "poolRowsExamined": examined, "candidateHashes": len(hashes)}
