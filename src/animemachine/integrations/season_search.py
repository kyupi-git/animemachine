"""Sequential, recoverable Ani-RSS searches for a radar season."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

from . import ani_rss

SEARCH_ATTEMPTS = 2
RETRY_SECONDS = 2.0


def movie_search_candidates(db_path: Path, query: Callable[..., dict[str, Any]],
                            config: dict[str, Any], language: str, *, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Add recent premieres to discovery, without putting unverified films in radar."""
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    try:
        first = today.replace(year=today.year - 1)
    except ValueError:  # February 29.
        first = today.replace(year=today.year - 1, day=28)
    rows = query(db_path, {"radar": ["1"], "media_type": ["movie"], "sort": ["premiere"],
                          "limit": ["all"], "start_from": [first.strftime("%Y-%m")],
                          "start_to": [today.strftime("%Y-%m")], "language": [language]}, config)["items"]
    candidates = []
    for row in rows:
        if row.get("media_code") != "movie":
            continue
        try:
            premiered = dt.date.fromisoformat(str(row.get("raw_date")))
            within_year = first <= premiered <= today
        except ValueError:
            month = str(row.get("start_month") or "")
            within_year = (ani_rss._month_index(month) is not None
                           and first.strftime("%Y-%m") <= month <= today.strftime("%Y-%m"))
        if within_year:
            candidates.append(row)
    return candidates


class SeasonSearch:
    def __init__(self, db_path: Path, config_store: Any, query: Callable[..., dict[str, Any]],
                 operation: Callable[..., Any]) -> None:
        self.db_path, self.config_store, self.query, self.operation = db_path, config_store, query, operation
        self.path = db_path.parent / ".season-search-state.json"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self._status = value if isinstance(value, dict) else {"state": "idle"}
        except (OSError, ValueError):
            self._status = {"state": "idle"}
        if self._status.get("state") == "running":
            self._set(state="interrupted")

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {key: value for key, value in self._status.items() if key != "completedIds"}

    def _save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._status = value

    def _set(self, **values: Any) -> None:
        with self.lock:
            self._save({**self._status, **values, "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat()})

    def start(self, start: str, end: str, language: str, *, synchronize: bool = False) -> bool:
        if not all(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) for value in (start, end)):
            raise ValueError("invalid season dates")
        first = int(start[:4]) * 12 + int(start[5:])
        last = int(end[:4]) * 12 + int(end[5:])
        if last - first != 2:
            raise ValueError("season must span three months")
        config = self.config_store.read()
        if not ani_rss.state_available(ani_rss.state(self.db_path, config)):
            raise ValueError("Ani-RSS is unavailable")
        with self.lock:
            if self._status.get("state") == "running":
                return False
            resume = (self._status.get("state") in {"interrupted", "failed", "complete"}
                      and self._status.get("from") == start and self._status.get("to") == end
                      and bool(self._status.get("synchronize")) == synchronize)
            if self._status.get("state") == "complete" and not self._status.get("failedIds"):
                resume = False
            failures = set(self._status.get("failedIds", [])) if resume else set()
            completed = [value for value in self._status.get("completedIds", []) if value not in failures] if resume else []
            self._save({"state": "running", "from": start, "to": end, "done": len(completed),
                        "synchronize": synchronize, "phase": "sync" if synchronize else "search",
                        "total": 0, "failed": 0, "failedIds": [], "failures": [],
                        "completedIds": completed, "currentAnimeId": None, "error": None})
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, args=(start, end, language, config, synchronize),
                                       daemon=True, name="anm-season-search")
        self.thread.start()
        return True

    def _run(self, start: str, end: str, language: str, config: dict[str, Any], synchronize: bool = False) -> None:
        try:
            if synchronize:
                with self.operation():
                    result = ani_rss.sync(self.db_path, config, abort_event=self.stop)
                if self.stop.is_set():
                    self._set(state="interrupted", currentAnimeId=None)
                    return
                if result.get("state") != "ready":
                    raise RuntimeError("Ani-RSS synchronization failed")
                self._set(phase="search", syncResult=result)
            params = {"radar": ["1"], "sort": ["recent_episode"], "limit": ["all"],
                      "start_from": [start], "start_to": [end], "language": [language]}
            rows = self.query(self.db_path, params, config)["items"]
            candidates = movie_search_candidates(self.db_path, self.query, config, language)
            # Keep the visible season first, then discover delayed movie releases.
            rows = list({int(row["id"]): row for row in [*rows, *candidates]}.values())
            with self.lock:
                completed = set(self._status.get("completedIds", []))
            failures: dict[int, str] = {}
            ids = [int(row["id"]) for row in rows]
            completed.intersection_update(ids)
            self._set(total=len(ids), done=len(completed), completedIds=sorted(completed))
            identity = ani_rss._credential_fingerprint(ani_rss._secret(), ani_rss._settings(config)["endpoint"])
            rows_by_id = {int(row["id"]): row for row in rows}
            for anime_id in ids:
                if self.stop.is_set():
                    self._set(state="interrupted", currentAnimeId=None)
                    return
                if anime_id in completed:
                    continue
                row = rows_by_id[anime_id]
                self._set(currentAnimeId=anime_id, currentTitle=str(row.get("title_zh_hans_localized")
                          or row.get("title_zh_hans") or row.get("title_en") or row.get("title_ja") or ""))
                for attempt in range(SEARCH_ATTEMPTS):
                    with self.operation():
                        current = self.config_store.read()
                        if (not ani_rss.state_available(ani_rss.state(self.db_path, current))
                                or identity != ani_rss._credential_fingerprint(
                                    ani_rss._secret(), ani_rss._settings(current)["endpoint"])):
                            self._set(state="interrupted", error="Ani-RSS connection changed or became unavailable",
                                      currentAnimeId=None)
                            return
                        try:
                            result = ani_rss.search(self.db_path, anime_id, current)
                        except (ValueError, OSError, RuntimeError, sqlite3.Error, httpx.RequestError) as exc:
                            failures[anime_id] = f"{type(exc).__name__}: {exc}"
                            retryable = not isinstance(exc, (ValueError, sqlite3.IntegrityError))
                        else:
                            if result.get("stale"):
                                self._set(state="interrupted", error="Ani-RSS route changed during search",
                                          currentAnimeId=None)
                                return
                            failures.pop(anime_id, None)
                            break
                    if not retryable or attempt == SEARCH_ATTEMPTS - 1:
                        break
                    if self.stop.wait(RETRY_SECONDS):
                        self._set(state="interrupted", currentAnimeId=None)
                        return
                completed.add(anime_id)
                self._set(done=len(completed), failed=len(failures), completedIds=sorted(completed),
                          failedIds=sorted(failures),
                          failures=[{"animeId": key, "error": error} for key, error in failures.items()])
            self._set(state="complete", currentAnimeId=None)
        except Exception as exc:
            self._set(state="failed", error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.stop.set()
