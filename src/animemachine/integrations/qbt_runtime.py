#!/usr/bin/env python3
"""Read qBittorrent managed-task state into the product runtime overlay."""
from __future__ import annotations

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


def lifecycle(task: dict[str, Any]) -> str:
    state = str(task.get("state") or "").casefold()
    progress = float(task.get("progress") or 0)
    if progress >= 1 or state in {"uploading", "stalledup", "forcedup", "queuedup", "stoppedup", "pausedup"}:
        return "existing"
    if state in {"downloading", "metadl", "forceddl", "queueddl", "stalleddl", "checkingdl"} or int(task.get("downloaded") or 0) > 0:
        return "downloading"
    return "queued"


def fetch(endpoint: str, category: str) -> list[dict[str, Any]]:
    url = endpoint.rstrip("/") + "/api/v2/torrents/info?" + urllib.parse.urlencode({"category": category})
    headers = {"Referer": endpoint.rstrip("/")}
    key = _secret()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    with tls_support.urlopen(urllib.request.Request(url, headers=headers), timeout=30, max_bytes=8 * 1024 * 1024) as response:
        return json.loads(response.read())


def refresh(db_path: Path, endpoint: str, category: str) -> dict[str, Any]:
    tasks = {str(row.get("hash") or "").casefold(): row for row in fetch(endpoint, category)}
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    counts = {"queued": 0, "downloading": 0, "existing": 0, "missing": 0}
    completed_anime_ids: set[int] = set()
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        submissions = list(db.execute("SELECT info_hash FROM runtime_submission"))
        with db:
            for submission in submissions:
                info_hash = str(submission["info_hash"]).casefold()
                task = tasks.get(info_hash)
                if not task:
                    counts["missing"] += 1
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
                    if prior_states != {"existing"}:
                        completed_anime_ids.update(int(row[0]) for row in db.execute(
                            "SELECT DISTINCT anime_id FROM runtime_torrent_work WHERE info_hash=?", (info_hash,)))
                    runtime_catalog.ensure_completion_watch(db, info_hash)
            runtime_catalog.refresh_watch_matches(db)
    return {**counts, "completedAnimeIds": sorted(completed_anime_ids)}
