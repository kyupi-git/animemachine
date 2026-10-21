#!/usr/bin/env python3
"""Read qBittorrent managed-task state into the product runtime overlay."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ..network import tls as tls_support
from ..torrents import runtime as runtime_catalog


def _secret() -> str:
    value = os.getenv("ANM_QBT_API_KEY", "").strip()
    path = Path(os.getenv("ANM_QBT_API_KEY_FILE", ""))
    if not value and str(path) not in {"", "."} and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    return value


def configured(config: dict[str, Any]) -> bool:
    """A template address alone does not enable an optional downloader."""
    settings = config.get("components", {}).get("downloadClient", {})
    return bool(str(settings.get("endpoint") or "").strip() and (
        _secret() or os.getenv("ANM_QBT_ALLOW_AUTH_BYPASS", "false").casefold() == "true"))


def lifecycle(task: dict[str, Any]) -> str:
    state = str(task.get("state") or "").casefold()
    progress = float(task.get("progress") or 0)
    if progress >= 1 or state in {"uploading", "stalledup", "forcedup", "queuedup", "stoppedup", "pausedup"}:
        return "existing"
    if state in {"downloading", "metadl", "forceddl", "queueddl", "stalleddl", "checkingdl"} or int(task.get("downloaded") or 0) > 0:
        return "downloading"
    return "queued"


def fetch(endpoint: str, category: str | None) -> list[dict[str, Any]]:
    url = endpoint.rstrip("/") + "/api/v2/torrents/info"
    if category is not None:
        url += "?" + urllib.parse.urlencode({"category": category})
    headers = {"Referer": endpoint.rstrip("/")}
    key = _secret()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    with tls_support.urlopen(urllib.request.Request(url, headers=headers), timeout=30, max_bytes=8 * 1024 * 1024) as response:
        return json.loads(response.read())


def refresh(db_path: Path, endpoint: str, category: str) -> dict[str, Any]:
    snapshot_started = datetime.now(timezone.utc)
    def snapshot(selected_category: str | None) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in fetch(endpoint, selected_category):
            info_hash = str(row.get("hash") or "").casefold()
            if not info_hash or info_hash in result:
                raise RuntimeError("qBittorrent returned missing or duplicate torrent identities")
            result[info_hash] = row
        return result

    tasks = snapshot(category)
    # A changed category must not look like a cancelled task. Validate a full
    # snapshot before releasing any previously submitted download state.
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        submitted = {str(row[0]).casefold() for row in db.execute("SELECT info_hash FROM runtime_submission")}
    if submitted.difference(tasks):
        tasks = snapshot(None)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    counts = {"queued": 0, "downloading": 0, "existing": 0, "missing": 0}
    completed_anime_ids: set[int] = set()
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        submissions = list(db.execute("SELECT info_hash,verified_at,qbt_state FROM runtime_submission"))
        with db:
            for submission in submissions:
                info_hash = str(submission["info_hash"]).casefold()
                task = tasks.get(info_hash)
                if not task:
                    verified_at = str(submission["verified_at"] or "")
                    try:
                        verified = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                        if verified.tzinfo is None:
                            verified = verified.replace(tzinfo=timezone.utc)
                    except ValueError:
                        verified = snapshot_started
                    if info_hash not in submitted or verified >= snapshot_started:
                        continue
                    counts["missing"] += 1
                    db.execute("UPDATE runtime_submission SET qbt_state='missing',verified_at=? WHERE info_hash=? AND qbt_state<>'missing'",
                               (stamp, info_hash))
                    for work in db.execute("SELECT private_work_id,target_unc FROM runtime_work WHERE private_work_id IN (SELECT private_work_id FROM runtime_torrent_work WHERE info_hash=?) AND library_state IN ('queued','downloading')",
                                           (info_hash,)).fetchall():
                        linked = {str(row[0]).casefold() for row in db.execute(
                            "SELECT s.info_hash FROM runtime_submission s JOIN runtime_torrent_work tw USING(info_hash) WHERE tw.private_work_id=?",
                            (work[0],))}
                        if linked.intersection(tasks):
                            continue
                        # Retain known media; subsequent planning still verifies
                        # actual files and uses the existing differential rules.
                        has_media = db.execute("SELECT 1 FROM runtime_asset WHERE owner_path=? LIMIT 1", (work[1],)).fetchone()
                        db.execute("UPDATE runtime_work SET library_state=?,updated_at=? WHERE private_work_id=?",
                                   ("existing" if has_media else "placeholder", stamp, work[0]))
                    continue
                state = lifecycle(task); counts[state] += 1
                prior_states = {str(row[0]) for row in db.execute(
                    "SELECT library_state FROM runtime_work WHERE private_work_id IN (SELECT private_work_id FROM runtime_torrent_work WHERE info_hash=?)",
                    (info_hash,))}
                db.execute("UPDATE runtime_submission SET qbt_state=?,verified_at=? WHERE info_hash=?",
                           (str(task.get("state") or "unknown"), stamp, info_hash))
                db.execute("""UPDATE runtime_work SET library_state=?,origin='managed_submission',updated_at=?
                    WHERE private_work_id IN (SELECT private_work_id FROM runtime_torrent_work WHERE info_hash=?)""",
                    (state, stamp, info_hash))
                if state == "existing":
                    if lifecycle({"state": submission["qbt_state"]}) != "existing" or not db.execute(
                            "SELECT 1 FROM runtime_asset WHERE source_info_hash=? LIMIT 1", (info_hash,)).fetchone():
                        runtime_catalog.record_completed_media(db, info_hash)
                    if prior_states != {"existing"}:
                        completed_anime_ids.update(int(row[0]) for row in db.execute(
                            "SELECT DISTINCT anime_id FROM runtime_torrent_work WHERE info_hash=?", (info_hash,)))
                    runtime_catalog.ensure_completion_watch(db, info_hash)
            runtime_catalog.refresh_watch_matches(db)
    return {**counts, "completedAnimeIds": sorted(completed_anime_ids)}
