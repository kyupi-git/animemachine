#!/usr/bin/env python3
"""Deferred, batched metadata repair; local catalog readiness never depends on it."""
from __future__ import annotations
import contextlib, datetime as dt, json, sqlite3, time
from pathlib import Path
from typing import Any
from ..network import sources as network_sources

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata_repair_queue(
  anime_id INTEGER PRIMARY KEY REFERENCES anime_work(id) ON DELETE CASCADE,reason TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata_evidence(
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,provider TEXT NOT NULL,
  payload_json TEXT NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(anime_id,provider));
"""

def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def enqueue(db_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.executescript(SCHEMA); stamp = utcnow(); before = db.total_changes
        db.execute("""INSERT INTO metadata_repair_queue(anime_id,reason,state,updated_at)
          SELECT id,CASE WHEN start_month IS NULL OR start_month='' THEN 'unknown_date' ELSE 'missing' END,'pending',?
          FROM anime_work WHERE start_month IS NULL OR start_month='' OR summary IS NULL OR summary=''
          ON CONFLICT(anime_id) DO UPDATE SET reason=excluded.reason,state='pending',updated_at=excluded.updated_at
          WHERE metadata_repair_queue.state='complete'""", (stamp,))
        return db.total_changes - before

def run_batch(db_path: Path, config: dict[str, Any]) -> dict[str, int]:
    policy = config.get("metadata", {}).get("onlineRepair", {})
    if not policy.get("enabled", False): return {"processed": 0, "repaired": 0, "failed": 0}
    network = config.get("metadata", {}).get("network", {})
    endpoints = network.get("bangumiApiEndpoints") or ["https://api.bgm.tv"]
    limit = max(1, min(200, int(policy.get("batchSize", 50))))
    result = {"processed": 0, "repaired": 0, "failed": 0}
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.row_factory = sqlite3.Row; db.executescript(SCHEMA)
        rows = db.execute("""SELECT q.*,w.bgm_id FROM metadata_repair_queue q JOIN anime_work w ON w.id=q.anime_id
          WHERE q.state IN ('pending','retry') AND q.attempts<8 ORDER BY q.attempts,q.updated_at LIMIT ?""", (limit,)).fetchall()
        for row in rows:
            result["processed"] += 1; stamp = utcnow()
            try:
                payload, _ = network_sources.fetch_json(
                    [f"{str(base).rstrip('/')}/v0/subjects/{int(row['bgm_id'])}" for base in endpoints],
                    timeout=float(network.get("probeTimeoutSeconds", 12)),
                    cooldown=int(network.get("failureCooldownSeconds", 900)))
                date = str(payload.get("date") or "")[:7] or None
                with db:
                    db.execute("INSERT OR REPLACE INTO metadata_evidence VALUES(?,?,?,?)",
                               (row["anime_id"], "bangumi", json.dumps(payload, ensure_ascii=False), stamp))
                    db.execute("""UPDATE anime_work SET summary=CASE WHEN summary IS NULL OR summary='' THEN ? ELSE summary END,
                      start_month=CASE WHEN start_month IS NULL OR start_month='' THEN ? ELSE start_month END,
                      episode_count=COALESCE(episode_count,?) WHERE id=?""",
                      (payload.get("summary"), date, payload.get("eps"), row["anime_id"]))
                    db.execute("UPDATE metadata_repair_queue SET state='complete',attempts=attempts+1,last_error=NULL,updated_at=? WHERE anime_id=?",
                               (stamp, row["anime_id"]))
                result["repaired"] += 1
            except Exception as exc:
                with db:
                    db.execute("UPDATE metadata_repair_queue SET state='retry',attempts=attempts+1,last_error=?,updated_at=? WHERE anime_id=?",
                               (f"{type(exc).__name__}: {exc}"[:500], stamp, row["anime_id"]))
                result["failed"] += 1
            time.sleep(max(0.2, float(config.get("runtime", {}).get("metadataRequestDelaySeconds", 1.2))))
    return result
