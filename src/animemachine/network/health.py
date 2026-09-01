"""Persistent per-capability endpoint health with EWMA scoring and cooldowns."""
from __future__ import annotations

import hashlib
import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path


_LOCK = threading.RLock()


def proxy_fingerprint() -> str:
    values = [os.getenv(key, "") for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "ANM_CA_BUNDLE")]
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


class Store:
    def __init__(self, path: Path | None = None):
        state = Path(os.getenv("ANM_STATE_DIR", str(Path.cwd() / ".local" / "state")))
        self.path = path or state / "network-health.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA busy_timeout=30000;
            CREATE TABLE IF NOT EXISTS endpoint_health(
              endpoint_id TEXT NOT NULL,capability TEXT NOT NULL,proxy_fingerprint TEXT NOT NULL,
              successes INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,latency_ewma REAL,throughput_ewma REAL,
              cooldown_until REAL NOT NULL DEFAULT 0,last_status TEXT,last_updated REAL NOT NULL,
              PRIMARY KEY(endpoint_id,capability,proxy_fingerprint));
            """)
            db.commit()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def _row(self, endpoint_id: str, capability: str):
        with _LOCK, contextlib.closing(self._connect()) as db:
            return db.execute("SELECT successes,failures,consecutive_failures,latency_ewma,throughput_ewma,cooldown_until FROM endpoint_health WHERE endpoint_id=? AND capability=? AND proxy_fingerprint=?",
                              (endpoint_id, capability, proxy_fingerprint())).fetchone()

    def score(self, endpoint_id: str, capability: str) -> tuple:
        row = self._row(endpoint_id, capability)
        if not row:
            return (0, 0.5, 1.0, 0.0)
        success, failure, consecutive, latency, throughput, cooldown = row
        rate = (success + 1) / (success + failure + 2)
        return (1 if cooldown > time.time() else 0, -rate, latency or 1.0, -(throughput or 0.0))

    def success(self, endpoint_id: str, capability: str, latency: float, byte_count: int = 0) -> None:
        throughput = byte_count / max(latency, .001)
        self._update(endpoint_id, capability, True, latency, throughput, "ok")

    def failure(self, endpoint_id: str, capability: str, status: str) -> None:
        self._update(endpoint_id, capability, False, None, None, status)

    def _update(self, endpoint_id, capability, ok, latency, throughput, status):
        key = (endpoint_id, capability, proxy_fingerprint())
        with _LOCK, contextlib.closing(self._connect()) as db:
            prior = db.execute("SELECT successes,failures,consecutive_failures,latency_ewma,throughput_ewma FROM endpoint_health WHERE endpoint_id=? AND capability=? AND proxy_fingerprint=?", key).fetchone() or (0,0,0,None,None)
            successes, failures, consecutive, old_latency, old_throughput = prior
            successes += int(ok); failures += int(not ok); consecutive = 0 if ok else consecutive + 1
            latency_value = latency if old_latency is None else (.3 * latency + .7 * old_latency) if latency is not None else old_latency
            throughput_value = throughput if old_throughput is None else (.3 * throughput + .7 * old_throughput) if throughput is not None else old_throughput
            cooldown = 0.0 if ok or consecutive < 2 else time.time() + min(900, 60 * (2 ** min(4, consecutive - 2)))
            db.execute("INSERT OR REPLACE INTO endpoint_health VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (*key, successes, failures, consecutive, latency_value, throughput_value, cooldown, status, time.time()))
            db.commit()
