#!/usr/bin/env python3
"""Classify torrent files against the live library without unsafe overwrites."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


HASH_CHUNK = 4 * 1024 * 1024
REVISION_FLAGS = {"rev", "revision", "fin", "final", "reseed", "v2", "v3"}


def migrate(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runtime_local_file_hash(
      final_path TEXT PRIMARY KEY,bytes INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
      sha256 TEXT NOT NULL,hashed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_local_hash_shape ON runtime_local_file_hash(bytes,mtime_ns);
    """)


def _norm(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum()
    )


def _parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.replace("\\", "/").split("/") if part not in {"", "."})


def _path(root: str, parts: tuple[str, ...]) -> Path:
    current = Path(root)
    for part in parts:
        if part == "..":
            raise ValueError("target path escapes its verified owner")
        current /= part
    return current


def owner_and_path(mapping_path: str, links: list[sqlite3.Row], fallback: sqlite3.Row) -> tuple[str, Path]:
    """Resolve a mapped relative path to its verified work/series owner."""
    parts = _parts(mapping_path)
    for link in links:
        directory = str(link["directory_name"])
        if parts and _norm(parts[0]) == _norm(directory):
            return str(link["target_unc"]), _path(str(link["target_unc"]), parts[1:])
    series = str(fallback["series_unc"] or "")
    if parts and parts[0].casefold() == "series extras" and series:
        return series, _path(series, parts)
    owner = str(fallback["target_unc"])
    return owner, _path(owner, parts)


def sha256_cached(db: sqlite3.Connection, path: Path, stamp: str) -> str:
    stat = path.stat()
    cached = db.execute(
        "SELECT sha256 FROM runtime_local_file_hash WHERE final_path=? AND bytes=? AND mtime_ns=?",
        (str(path), int(stat.st_size), int(stat.st_mtime_ns)),
    ).fetchone()
    if cached:
        return str(cached[0])
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK):
            digest.update(chunk)
    final_stat = path.stat()
    if (int(final_stat.st_size), int(final_stat.st_mtime_ns)) != (int(stat.st_size), int(stat.st_mtime_ns)):
        raise OSError("local file changed while hashing")
    value = digest.hexdigest()
    db.execute(
        """INSERT INTO runtime_local_file_hash VALUES(?,?,?,?,?)
           ON CONFLICT(final_path) DO UPDATE SET bytes=excluded.bytes,mtime_ns=excluded.mtime_ns,
           sha256=excluded.sha256,hashed_at=excluded.hashed_at""",
        (str(path), int(stat.st_size), int(stat.st_mtime_ns), value, stamp),
    )
    return value


def _revision_proven(torrent: sqlite3.Row) -> bool:
    try:
        flags = {str(value).casefold() for value in json.loads(torrent["release_flags_json"] or "[]")}
    except (TypeError, ValueError):
        flags = set()
    name = str(torrent["info_name"] or "").casefold()
    return bool(flags & REVISION_FLAGS) or bool(re.search(r"(?:^|[\W_])(rev\d*|fin(?:al)?|reseed|v[2-9])(?:[\W_]|$)", name))


def _staging_relative(config: Mapping[str, Any], plan_id: str, mapped: str) -> str:
    staging = str(config.get("differentialPlanning", {}).get("stagingDirectoryName", ".anm-staging"))
    if not staging or "/" in staging or "\\" in staging or staging in {".", ".."}:
        raise ValueError("invalid differentialPlanning.stagingDirectoryName")
    return str(PurePosixPath(staging) / plan_id / PurePosixPath(*_parts(mapped)))


def classify(
    db: sqlite3.Connection,
    *,
    torrent: sqlite3.Row,
    item: sqlite3.Row,
    mapping: sqlite3.Row | None,
    links: list[sqlite3.Row],
    fallback: sqlite3.Row,
    requested: bool,
    selected_before: bool,
    plan_id: str,
    stamp: str,
    config: Mapping[str, Any],
    owner_presence: Mapping[str, bool | None] | None = None,
) -> dict[str, Any]:
    mapped = str(mapping["target_relative_path"] if mapping else item["source_path"])
    owner, final_path = owner_and_path(mapped, links, fallback)
    base = {
        "index": int(item["file_index"]),
        "oldPath": str(item["source_path"]),
        "newPath": mapped,
        "finalPath": str(final_path),
        "ownerPath": owner,
        "length": int(item["length"]),
        "selectedBefore": bool(selected_before),
        "warning": None,
        "warningDetail": None,
        "sha256": None,
        "verification": None,
    }
    if selected_before:
        return {**base, "selected": True, "action": "previous_selection", "reason": "already_selected_in_managed_task"}
    # A verified single-work torrent owns its full manifest and does not need a
    # redundant file_map. Collections are rejected earlier unless they have an
    # exact full partition, so only an explicit unselected mapping excludes a
    # requested file here.
    if not requested or (mapping is not None and not bool(mapping["selected"])):
        return {**base, "selected": False, "action": "not_selected", "reason": "outside_selected_work"}
    # A missing verified owner means every descendant is missing. One owner
    # probe replaces hundreds of high-latency UNC file probes for new works.
    if owner_presence is not None and owner_presence.get(owner) is False:
        return {**base, "selected": True, "action": "add_missing", "reason": "target_owner_missing"}
    try:
        if final_path.is_symlink():
            return {**base, "selected": False, "action": "conflict_review", "reason": "target_is_symbolic_link", "warning": "targetIsSymbolicLink"}
        exists = final_path.exists()
        regular_file = final_path.is_file() if exists else False
    except OSError as exc:
        return {**base, "selected": False, "action": "conflict_review", "reason": "library_path_unreadable", "warning": "libraryPathUnreadable", "warningDetail": str(exc)}
    if not exists:
        return {**base, "selected": True, "action": "add_missing", "reason": "target_file_missing"}
    if not regular_file:
        return {**base, "selected": False, "action": "conflict_review", "reason": "target_is_not_regular_file", "warning": "targetNotRegularFile"}

    try:
        local_stat = final_path.stat()
        local_size = local_stat.st_size
        base["localBytes"] = int(local_stat.st_size)
        base["localMtimeNs"] = int(local_stat.st_mtime_ns)
    except OSError as exc:
        return {**base, "selected": False, "action": "conflict_review", "reason": "local_file_unreadable", "warning": "localFileUnreadable", "warningDetail": str(exc)}
    asset = db.execute("SELECT * FROM runtime_asset WHERE final_path=?", (str(final_path),)).fetchone()
    previous_torrent = db.execute("SELECT source_class,release_unit,effective_group FROM runtime_torrent WHERE info_hash=?", (asset["source_info_hash"],)).fetchone() if asset and asset["source_info_hash"] else None
    exact_source = bool(
        asset
        and str(asset["source_info_hash"] or "").casefold() == str(torrent["info_hash"]).casefold()
        and int(asset["source_file_index"] if asset["source_file_index"] is not None else -1) == int(item["file_index"])
    )
    if int(local_size) == int(item["length"]):
        policy = config.get("differentialPlanning", {})
        exact_verification = str(policy.get("samePathSizePolicy", "size_and_skip")) == "hash_and_skip"
        if not exact_verification:
            return {
                **base,
                "selected": False,
                "action": "skip_unchanged",
                "reason": "exact_managed_source" if exact_source else "same_target_path_and_exact_size",
                "verification": "provenance" if exact_source else "canonical_path_and_exact_bytes",
            }
        try:
            local_hash = sha256_cached(db, final_path, stamp)
        except OSError as exc:
            return {**base, "selected": False, "action": "conflict_review", "reason": "local_file_changed_during_hash", "warning": "localFileChangedDuringHash", "warningDetail": str(exc)}
        if asset and asset["sha256"] and str(asset["sha256"]).casefold() != local_hash:
            return {**base, "selected": False, "action": "conflict_review", "reason": "managed_file_changed_locally", "warning": "managedFileChanged", "sha256": local_hash}
        if not exact_source and not (asset and asset["sha256"]):
            return {
                **base,
                "selected": False,
                "action": "conflict_review",
                "reason": "exact_comparison_reference_unavailable",
                "warning": "exactComparisonUnavailable",
                "sha256": local_hash,
                "verification": "sha256_without_reference",
            }
        return {
            **base,
            "selected": False,
            "action": "skip_unchanged",
            "reason": "exact_managed_source_hash_verified" if asset and asset["sha256"] else "exact_managed_source_hash_baselined",
            "warning": None if asset and asset["sha256"] else "managedHashBaselineRecorded",
            "sha256": local_hash,
            "verification": "sha256_reference" if asset and asset["sha256"] else "provenance_sha256_baseline",
        }

    candidate_class = str(torrent["source_class"] or "").casefold()
    candidate_unit = str(torrent["release_unit"] or "unknown").casefold() if "release_unit" in torrent.keys() else "unknown"
    previous_class = str(previous_torrent["source_class"] or "").casefold() if previous_torrent else ""
    if candidate_class == "bdrip" and candidate_unit == "volume" and previous_class in {"webrip", "tvrip"}:
        group = re.sub(r"[^A-Za-z0-9._-]+", "_", str(torrent["effective_group"] or "unknown-group")).strip("_")
        coexist_relative = str(PurePosixPath("Editions") / f"BDRip-{group}" / PurePosixPath(*_parts(mapped)))
        coexist_path = _path(owner, _parts(coexist_relative))
        if not coexist_path.exists():
            return {**base, "selected": True, "action": "add_coexisting", "reason": "partial_bdrip_coexists_with_complete_web",
                    "newPath": coexist_relative, "finalPath": str(coexist_path), "warning": "coexistingPartialBd"}
    source_upgrade = candidate_class == "bdrip" and candidate_unit == "collection" and previous_class in {"webrip", "tvrip"}
    if _revision_proven(torrent) or source_upgrade:
        stage_relative = _staging_relative(config, plan_id, mapped)
        return {
            **base,
            "selected": True,
            "action": "stage_replace",
            "reason": "complete_bdrip_replaces_web" if source_upgrade else "revision_evidence_and_size_change",
            "newPath": stage_relative,
            "warning": "stagedSourceUpgrade" if source_upgrade else "stagedReplacement",
        }
    return {
        **base,
        "selected": False,
        "action": "conflict_review",
        "reason": "same_target_different_size_without_revision_proof",
        "warning": "sizeConflict",
    }
