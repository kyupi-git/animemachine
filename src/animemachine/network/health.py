"""Persistent endpoint health learned independently for each network environment and route."""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import connectivity


_LOCK = threading.RLock()
_ROUTE_MODES = ("direct", "environment_proxy", "windows_system_proxy", "system_proxy")
_TREND_BUCKET_SECONDS = 300
_TREND_RETENTION_SECONDS = 48 * 3600
_DECAY_HALF_LIFE_SECONDS = 6 * 3600
_CONFIDENCE_SAMPLE_SCALE = 6.0
_HYSTERESIS_BASE = 0.05
_HYSTERESIS_LOW_CONFIDENCE = 0.08
_HYSTERESIS_HOLD_SECONDS = 90.0
_HYSTERESIS_BREAK_ADVANTAGE = 0.18
_DEFAULT_NETWORK_ID = "default"


def _route_mode(value: str | None) -> str:
    mode = str(value or "direct")
    return mode if mode in _ROUTE_MODES else "direct"


def _network_id(value: str | None) -> str:
    return str(value or _DEFAULT_NETWORK_ID).strip() or _DEFAULT_NETWORK_ID


def _quality(success_rate: float, latency_seconds: float | None, throughput_bps: float | None) -> float:
    latency_quality = 1.0 / (1.0 + float(latency_seconds if latency_seconds is not None else 1.0) / 1.2)
    throughput = max(0.0, float(throughput_bps or 0.0))
    throughput_quality = throughput / (throughput + 512 * 1024) if throughput > 0 else 0.0
    return max(0.0, min(1.0, 0.68 * success_rate + 0.20 * latency_quality + 0.12 * throughput_quality))


def _decay(age_seconds: float) -> float:
    return math.exp(-math.log(2.0) * max(0.0, age_seconds) / _DECAY_HALF_LIFE_SECONDS)


class Store:
    def __init__(self, path: Path | None = None):
        state = Path(os.getenv("ANM_STATE_DIR", str(Path.cwd() / ".local" / "state")))
        self.path = path or state / "network-health.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next_prune = 0.0
        with contextlib.closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            self._migrate_health_table(db)
            self._migrate_trend_table(db)
            db.executescript("""
            CREATE INDEX IF NOT EXISTS endpoint_trend_lookup
              ON endpoint_trend(endpoint_id,capability,network_id,route_mode,bucket);
            CREATE TABLE IF NOT EXISTS endpoint_selection(
              selection_key TEXT PRIMARY KEY,capability TEXT NOT NULL,selected_endpoint_id TEXT NOT NULL,
              selected_route_mode TEXT NOT NULL,selected_quality REAL NOT NULL DEFAULT 0,
              switched_at REAL NOT NULL,updated_at REAL NOT NULL);
            """)
            db.commit()

    @staticmethod
    def _table_exists(db: sqlite3.Connection, name: str) -> bool:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def _migrate_health_table(self, db: sqlite3.Connection) -> None:
        exists = self._table_exists(db, "endpoint_route_health")
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(endpoint_route_health)")} if exists else set()
        if exists and "network_id" not in columns:
            db.execute("ALTER TABLE endpoint_route_health RENAME TO endpoint_route_health_legacy")
            legacy_columns = columns
        else:
            legacy_columns = set()
        db.execute("""
            CREATE TABLE IF NOT EXISTS endpoint_route_health(
              endpoint_id TEXT NOT NULL,capability TEXT NOT NULL,network_id TEXT NOT NULL,route_mode TEXT NOT NULL,
              successes INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,latency_ewma REAL,throughput_ewma REAL,
              cooldown_until REAL NOT NULL DEFAULT 0,last_status TEXT,last_updated REAL NOT NULL,
              success_ewma REAL,last_failure TEXT,last_failure_at REAL,effective_samples REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(endpoint_id,capability,network_id,route_mode))
        """)
        if legacy_columns:
            effective = "effective_samples" if "effective_samples" in legacy_columns else "successes+failures"
            db.execute(
                "INSERT OR REPLACE INTO endpoint_route_health("
                "endpoint_id,capability,network_id,route_mode,successes,failures,consecutive_failures,latency_ewma,"
                "throughput_ewma,cooldown_until,last_status,last_updated,success_ewma,last_failure,last_failure_at,effective_samples) "
                f"SELECT endpoint_id,capability,?,route_mode,successes,failures,consecutive_failures,latency_ewma,"
                f"throughput_ewma,cooldown_until,last_status,last_updated,success_ewma,last_failure,last_failure_at,{effective} "
                "FROM endpoint_route_health_legacy",
                (_DEFAULT_NETWORK_ID,),
            )
            db.execute("DROP TABLE endpoint_route_health_legacy")
        current_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(endpoint_route_health)")}
        if "effective_samples" not in current_columns:
            db.execute("ALTER TABLE endpoint_route_health ADD COLUMN effective_samples REAL NOT NULL DEFAULT 0")
            db.execute("UPDATE endpoint_route_health SET effective_samples=successes+failures")

    def _migrate_trend_table(self, db: sqlite3.Connection) -> None:
        exists = self._table_exists(db, "endpoint_trend")
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(endpoint_trend)")} if exists else set()
        if exists and "network_id" not in columns:
            db.execute("DROP INDEX IF EXISTS endpoint_trend_lookup")
            db.execute("ALTER TABLE endpoint_trend RENAME TO endpoint_trend_legacy")
            legacy = True
        else:
            legacy = False
        db.execute("""
            CREATE TABLE IF NOT EXISTS endpoint_trend(
              endpoint_id TEXT NOT NULL,capability TEXT NOT NULL,network_id TEXT NOT NULL,route_mode TEXT NOT NULL,
              bucket INTEGER NOT NULL,successes INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
              latency_sum REAL NOT NULL DEFAULT 0,throughput_sum REAL NOT NULL DEFAULT 0,
              performance_samples INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(endpoint_id,capability,network_id,route_mode,bucket))
        """)
        if legacy:
            db.execute(
                "INSERT OR REPLACE INTO endpoint_trend(endpoint_id,capability,network_id,route_mode,bucket,successes,"
                "failures,latency_sum,throughput_sum,performance_samples) "
                "SELECT endpoint_id,capability,?,route_mode,bucket,successes,failures,latency_sum,throughput_sum,performance_samples "
                "FROM endpoint_trend_legacy",
                (_DEFAULT_NETWORK_ID,),
            )
            db.execute("DROP TABLE endpoint_trend_legacy")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def _row(self, endpoint_id: str, capability: str, route_mode: str, network_id: str):
        with _LOCK, contextlib.closing(self._connect()) as db:
            return db.execute(
                "SELECT successes,failures,consecutive_failures,latency_ewma,throughput_ewma,cooldown_until,"
                "last_status,last_updated,success_ewma,last_failure,last_failure_at,effective_samples "
                "FROM endpoint_route_health WHERE endpoint_id=? AND capability=? AND network_id=? AND route_mode=?",
                (endpoint_id, capability, _network_id(network_id), _route_mode(route_mode)),
            ).fetchone()

    def evaluation(self, endpoint_id: str, capability: str, route_mode: str = "direct",
                   network_id: str = _DEFAULT_NETWORK_ID) -> dict[str, Any]:
        route = _route_mode(route_mode)
        network = _network_id(network_id)
        row = self._row(endpoint_id, capability, route, network)
        now = time.time()
        if not row:
            return {
                "endpointId": endpoint_id, "networkId": network, "routeMode": route,
                "samples": 0, "effectiveSamples": 0.0, "confidence": 0.0, "successRate": None,
                "recentSuccessRate": None, "decayedSuccessRate": 0.5, "latencyMs": None,
                "throughputKiBps": None, "quality": 0.5, "rawQuality": 0.5, "cooldownUntil": 0.0,
                "coolingDown": False, "lastFailure": "", "lastFailureAt": 0.0, "lastUpdated": 0.0,
                "ageSeconds": None,
            }
        (success, failure, _consecutive, latency, throughput, cooldown, status, updated, success_ewma,
         last_failure, last_failure_at, effective_samples) = row
        samples = int(success) + int(failure)
        age = max(0.0, now - float(updated or 0))
        decay = _decay(age)
        lifetime_rate = (float(success) + 2.0) / (samples + 4.0)
        recent_rate = float(success_ewma) if success_ewma is not None else lifetime_rate
        decayed_recent = 0.5 + (recent_rate - 0.5) * decay
        effective = max(0.0, float(effective_samples or 0.0) * decay)
        confidence = 1.0 - math.exp(-effective / _CONFIDENCE_SAMPLE_SCALE)
        learned_rate = 0.80 * decayed_recent + 0.20 * lifetime_rate
        blended_rate = confidence * learned_rate + (1.0 - confidence) * 0.5
        latency_seconds = float(latency) if latency is not None else 1.0
        throughput_bps = float(throughput or 0.0)
        raw_quality = _quality(blended_rate, latency_seconds, throughput_bps)
        quality = 0.5 + confidence * (raw_quality - 0.5)
        return {
            "endpointId": endpoint_id, "networkId": network, "routeMode": route, "samples": samples,
            "effectiveSamples": round(effective, 3), "confidence": round(confidence, 4),
            "successRate": (float(success) / samples) if samples else None, "recentSuccessRate": recent_rate,
            "decayedSuccessRate": round(decayed_recent, 4),
            "latencyMs": round(latency_seconds * 1000, 1) if latency is not None else None,
            "throughputKiBps": round(throughput_bps / 1024, 1) if throughput is not None else None,
            "quality": round(max(0.0, min(1.0, quality)), 4), "rawQuality": round(raw_quality, 4),
            "cooldownUntil": float(cooldown or 0), "coolingDown": float(cooldown or 0) > now,
            "lastFailure": str(last_failure or ("" if status in {None, "", "ok"} else status)),
            "lastFailureAt": float(last_failure_at or 0), "lastUpdated": float(updated or 0), "ageSeconds": round(age, 1),
        }

    def score(self, endpoint_id: str, capability: str, route_mode: str = "direct",
              network_id: str = _DEFAULT_NETWORK_ID) -> tuple[float, ...]:
        item = self.evaluation(endpoint_id, capability, route_mode, network_id)
        latency_seconds = float(item["latencyMs"]) / 1000.0 if item["latencyMs"] is not None else 1.0
        throughput_bps = float(item["throughputKiBps"]) * 1024.0 if item["throughputKiBps"] is not None else 0.0
        return (1.0 if item["coolingDown"] else 0.0, -float(item["quality"]), latency_seconds, -throughput_bps)

    @staticmethod
    def _normalize_candidates(candidates: Iterable[tuple[str, ...]]) -> list[tuple[str, str, str]]:
        values: list[tuple[str, str, str]] = []
        for item in candidates:
            if len(item) == 2:
                endpoint_id, route = item
                network = _DEFAULT_NETWORK_ID
            elif len(item) == 3:
                endpoint_id, route, network = item
            else:
                raise ValueError("health candidate must contain endpoint, route and optional network id")
            value = (str(endpoint_id), _route_mode(route), _network_id(network))
            if value not in values:
                values.append(value)
        return values

    @staticmethod
    def _selection_key(capability: str, candidates: Iterable[tuple[str, str, str]]) -> str:
        normalized = sorted(f"{endpoint_id}@{network}@{route}" for endpoint_id, route, network in candidates)
        digest = hashlib.sha256((capability + "\n" + "\n".join(normalized)).encode("utf-8")).hexdigest()[:24]
        return f"{capability}:{digest}"

    def rank(self, candidates: Iterable[tuple[str, ...]], capability: str, *, persist: bool = True) -> tuple[list[str], dict[str, Any]]:
        values = self._normalize_candidates(candidates)
        if not values:
            return [], {"selectedEndpointId": "", "reason": "no_candidates", "candidates": []}
        now = time.time()
        evaluations = {
            item: self.evaluation(item[0], capability, item[1], item[2]) for item in values
        }
        raw = sorted(values, key=lambda item: self.score(item[0], capability, item[1], item[2]))
        best = raw[0]
        key = self._selection_key(capability, values)
        with _LOCK, contextlib.closing(self._connect()) as db:
            selected_row = db.execute(
                "SELECT selected_endpoint_id,selected_route_mode,selected_quality,switched_at,updated_at "
                "FROM endpoint_selection WHERE selection_key=?", (key,),
            ).fetchone()
        incumbent = None
        switched_at = 0.0
        if selected_row:
            matching = [item for item in values if item[0] == str(selected_row[0]) and item[1] == _route_mode(str(selected_row[1]))]
            if matching:
                incumbent = matching[0]
                switched_at = float(selected_row[3] or 0.0)

        selected = best
        reason = "best_quality"
        threshold = 0.0
        advantage = 0.0
        hold_remaining = 0.0
        if incumbent is not None:
            incumbent_eval = evaluations[incumbent]
            best_eval = evaluations[best]
            if incumbent == best:
                selected = incumbent
                reason = "incumbent_best"
            elif bool(incumbent_eval["coolingDown"]):
                selected = best
                reason = "incumbent_cooldown"
            elif bool(best_eval["coolingDown"]) and not bool(incumbent_eval["coolingDown"]):
                selected = incumbent
                reason = "candidate_cooldown"
            else:
                advantage = float(best_eval["quality"]) - float(incumbent_eval["quality"])
                threshold = _HYSTERESIS_BASE + (1.0 - float(best_eval["confidence"])) * _HYSTERESIS_LOW_CONFIDENCE
                hold_remaining = max(0.0, _HYSTERESIS_HOLD_SECONDS - (now - switched_at))
                if hold_remaining > 0 and advantage < _HYSTERESIS_BREAK_ADVANTAGE:
                    selected = incumbent
                    reason = "hysteresis_hold"
                elif advantage < threshold:
                    selected = incumbent
                    reason = "hysteresis_margin"
                else:
                    selected = best
                    reason = "meaningfully_better"

        selected_eval = evaluations[selected]
        changed = incumbent is not None and selected != incumbent
        if persist:
            effective_switched_at = now if incumbent is None or changed else switched_at
            with _LOCK, contextlib.closing(self._connect()) as db:
                db.execute(
                    "INSERT INTO endpoint_selection(selection_key,capability,selected_endpoint_id,selected_route_mode,"
                    "selected_quality,switched_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(selection_key) DO UPDATE SET capability=excluded.capability,"
                    "selected_endpoint_id=excluded.selected_endpoint_id,selected_route_mode=excluded.selected_route_mode,"
                    "selected_quality=excluded.selected_quality,switched_at=excluded.switched_at,updated_at=excluded.updated_at",
                    (key, capability, selected[0], selected[1], float(selected_eval["quality"]), effective_switched_at, now),
                )
                db.commit()

        ordered = [selected, *(item for item in raw if item != selected)]
        explanation = {
            "selectionKey": key, "selectedEndpointId": selected[0], "selectedRouteMode": selected[1],
            "selectedNetworkId": selected[2], "reason": reason, "changed": changed,
            "quality": float(selected_eval["quality"]), "confidence": float(selected_eval["confidence"]),
            "advantage": round(advantage, 4), "switchThreshold": round(threshold, 4),
            "holdRemainingSeconds": round(hold_remaining, 1), "candidates": [evaluations[item] for item in ordered],
        }
        return [item[0] for item in ordered], explanation

    def success(self, endpoint_id: str, capability: str, latency: float, byte_count: int = 0,
                *, route_mode: str = "direct", network_id: str = _DEFAULT_NETWORK_ID) -> None:
        throughput = byte_count / max(latency, .001)
        self._update(endpoint_id, capability, route_mode, network_id, True, latency, throughput, "ok")

    def failure(self, endpoint_id: str, capability: str, status: str, *, route_mode: str = "direct",
                network_id: str = _DEFAULT_NETWORK_ID) -> None:
        if not connectivity.failure_learning_allowed():
            return
        self._update(endpoint_id, capability, route_mode, network_id, False, None, None, status)

    def _update(self, endpoint_id: str, capability: str, route_mode: str, network_id: str, ok: bool,
                latency: float | None, throughput: float | None, status: str) -> None:
        route = _route_mode(route_mode)
        network = _network_id(network_id)
        key = (endpoint_id, capability, network, route)
        now = time.time()
        bucket = int(now // _TREND_BUCKET_SECONDS) * _TREND_BUCKET_SECONDS
        with _LOCK, contextlib.closing(self._connect()) as db:
            prior = db.execute(
                "SELECT successes,failures,consecutive_failures,latency_ewma,throughput_ewma,success_ewma,last_failure,"
                "last_failure_at,last_updated,effective_samples FROM endpoint_route_health "
                "WHERE endpoint_id=? AND capability=? AND network_id=? AND route_mode=?", key,
            ).fetchone() or (0, 0, 0, None, None, None, None, None, now, 0.0)
            (successes, failures, consecutive, old_latency, old_throughput, old_success_ewma, last_failure,
             last_failure_at, old_updated, old_effective_samples) = prior
            elapsed = max(0.0, now - float(old_updated or now))
            decay = 1.0 if elapsed < 1.0 else _decay(elapsed)
            successes += int(ok)
            failures += int(not ok)
            consecutive = 0 if ok else consecutive + 1
            latency_value = latency if old_latency is None else (.25 * latency + .75 * old_latency) if latency is not None else old_latency
            throughput_value = throughput if old_throughput is None else (.25 * throughput + .75 * old_throughput) if throughput is not None else old_throughput
            prior_recent = 0.5 if old_success_ewma is None else 0.5 + (float(old_success_ewma) - 0.5) * decay
            recent_value = float(ok) if old_success_ewma is None else .22 * float(ok) + .78 * prior_recent
            effective_samples = float(old_effective_samples or 0.0) * decay + 1.0
            cooldown = 0.0 if ok or consecutive < 2 else now + min(900, 45 * (2 ** min(4, consecutive - 2)))
            if not ok:
                last_failure, last_failure_at = status, now
            db.execute(
                "INSERT OR REPLACE INTO endpoint_route_health("
                "endpoint_id,capability,network_id,route_mode,successes,failures,consecutive_failures,latency_ewma,"
                "throughput_ewma,cooldown_until,last_status,last_updated,success_ewma,last_failure,last_failure_at,"
                "effective_samples) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key, successes, failures, consecutive, latency_value, throughput_value, cooldown, status, now,
                 recent_value, last_failure, last_failure_at, effective_samples),
            )
            db.execute(
                "INSERT INTO endpoint_trend(endpoint_id,capability,network_id,route_mode,bucket,successes,failures,"
                "latency_sum,throughput_sum,performance_samples) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(endpoint_id,capability,network_id,route_mode,bucket) DO UPDATE SET "
                "successes=successes+excluded.successes,failures=failures+excluded.failures,"
                "latency_sum=latency_sum+excluded.latency_sum,throughput_sum=throughput_sum+excluded.throughput_sum,"
                "performance_samples=performance_samples+excluded.performance_samples",
                (*key, bucket, int(ok), int(not ok), float(latency or 0.0), float(throughput or 0.0), int(ok)),
            )
            if now >= self._next_prune:
                db.execute("DELETE FROM endpoint_trend WHERE bucket<?", (int(now - _TREND_RETENTION_SECONDS),))
                self._next_prune = now + 3600
            db.commit()

    def snapshot(self, endpoint_id: str, capability: str, route_mode: str = "direct",
                 network_id: str = _DEFAULT_NETWORK_ID) -> dict[str, Any]:
        return self.evaluation(endpoint_id, capability, route_mode, network_id)

    def trend(self, endpoint_id: str, capability: str, *, network_id: str = _DEFAULT_NETWORK_ID,
              hours: int = 12, bucket_seconds: int = 1800) -> dict[str, list[dict[str, Any]]]:
        horizon = max(1, min(48, int(hours))) * 3600
        output_bucket = max(_TREND_BUCKET_SECONDS, int(bucket_seconds))
        start = int(time.time() - horizon)
        network = _network_id(network_id)
        with _LOCK, contextlib.closing(self._connect()) as db:
            rows = db.execute(
                "SELECT route_mode,bucket,successes,failures,latency_sum,throughput_sum,performance_samples "
                "FROM endpoint_trend WHERE endpoint_id=? AND capability=? AND network_id=? AND bucket>=? ORDER BY bucket",
                (endpoint_id, capability, network, start),
            ).fetchall()
        grouped: dict[tuple[str, int], list[float]] = {}
        for route, bucket, successes, failures, latency_sum, throughput_sum, performance_samples in rows:
            target = int(bucket // output_bucket) * output_bucket
            values = grouped.setdefault((_route_mode(str(route)), target), [0.0, 0.0, 0.0, 0.0, 0.0])
            values[0] += int(successes)
            values[1] += int(failures)
            values[2] += float(latency_sum)
            values[3] += float(throughput_sum)
            values[4] += int(performance_samples)
        result: dict[str, list[dict[str, Any]]] = {mode: [] for mode in _ROUTE_MODES}
        for (route, bucket), values in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
            successes, failures, latency_sum, throughput_sum, performance_samples = values
            samples = int(successes + failures)
            success_rate = successes / samples if samples else 0.0
            latency = latency_sum / performance_samples if performance_samples else None
            throughput = throughput_sum / performance_samples if performance_samples else None
            result.setdefault(route, []).append({
                "at": bucket, "samples": samples, "successRate": round(success_rate, 4),
                "latencyMs": round(latency * 1000, 1) if latency is not None else None,
                "throughputKiBps": round(throughput / 1024, 1) if throughput is not None else None,
                "score": round(_quality(success_rate, latency, throughput), 4) if successes > 0 else 0.0,
            })
        return result
