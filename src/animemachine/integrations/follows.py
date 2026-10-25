"""Account-owned release intents, kept independently of replaceable metadata."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config.loader import archive_group_id, serial_group_matches, serial_profile_language, source_family
from ..torrents import runtime
from . import ani_rss


class Conflict(ValueError):
    pass


def criteria(value: Any) -> dict[str, list[str] | None]:
    """Empty means current preferences; null means every variant of one group."""
    if not isinstance(value, dict) or len(value) > 200:
        raise ValueError("invalid_follow_groups")
    result = {}
    for group, platforms in value.items():
        if not isinstance(group, str) or not group.strip() or len(group) > 120:
            raise ValueError("invalid_follow_groups")
        if platforms is not None and (not isinstance(platforms, list) or not platforms or len(platforms) > 32
                or any(not isinstance(item, str) or not item or len(item) > 40 for item in platforms)):
            raise ValueError("invalid_follow_variants")
        result[group] = None if platforms is None else sorted(set(platforms))
    return result


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS release_follow(
                user_id INTEGER NOT NULL,bgm_id INTEGER NOT NULL,mode TEXT NOT NULL,
                groups_json TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
                updated_at REAL NOT NULL,next_check_at REAL NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,lease TEXT,lease_until REAL NOT NULL DEFAULT 0,
                matches_json TEXT NOT NULL DEFAULT '[]',error_code TEXT,
                PRIMARY KEY(user_id,bgm_id,mode))""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def public(row: sqlite3.Row) -> dict[str, Any]:
        return {"bgmId": row["bgm_id"], "mode": row["mode"], "groups": json.loads(row["groups_json"]),
                "state": row["state"], "revision": row["revision"], "updatedAt": row["updated_at"],
                "nextCheckAt": row["next_check_at"], "matches": json.loads(row["matches_json"]),
                "error": row["error_code"]}

    def snapshot(self, user_id: int, bgm_id: int | None = None) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM release_follow WHERE user_id=? AND (? IS NULL OR bgm_id=?)",
                              (user_id, bgm_id, bgm_id)).fetchall()
        return [self.public(row) for row in rows]

    def save(self, user_id: int, bgm_id: int, mode: str, groups: Any, *, revision: int | None) -> dict[str, Any]:
        if (not isinstance(mode, str) or mode not in {"serial", "archive"}
                or type(bgm_id) is not int or not 0 < bgm_id < 10**12):
            raise ValueError("invalid_follow")
        selected = criteria(groups)
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("invalid_follow_revision")
        now = time.time()
        with contextlib.closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT revision FROM release_follow WHERE user_id=? AND bgm_id=? AND mode=?",
                               (user_id, bgm_id, mode)).fetchone()
            current = int(prior[0]) if prior else 0
            if revision != current:
                raise Conflict("follow_changed_reload")
            db.execute("""INSERT INTO release_follow(user_id,bgm_id,mode,groups_json,state,revision,updated_at)
                VALUES(?,?,?,?, 'pending',?,?) ON CONFLICT(user_id,bgm_id,mode) DO UPDATE SET
                groups_json=excluded.groups_json,state='pending',revision=excluded.revision,
                updated_at=excluded.updated_at,next_check_at=0,failures=0,lease=NULL,lease_until=0,
                matches_json='[]',error_code=NULL""",
                       (user_id, bgm_id, mode, json.dumps(selected), current + 1, now))
        return next(row for row in self.snapshot(user_id, bgm_id) if row["mode"] == mode)

    def cancel(self, user_id: int, bgm_id: int, mode: str, revision: int) -> None:
        if type(revision) is not int:
            raise ValueError("invalid_follow_revision")
        with contextlib.closing(self.connect()) as db, db:
            changed = db.execute("""UPDATE release_follow SET state='cancelled',revision=revision+1,
                lease=NULL,lease_until=0,updated_at=? WHERE user_id=? AND bgm_id=? AND mode=? AND revision=?""",
                (time.time(), user_id, bgm_id, mode, revision)).rowcount
            if not changed:
                raise Conflict("follow_changed_reload")

    def claim(self, *, now: float, enabled_users: set[int]) -> dict[str, Any] | None:
        if not enabled_users:
            return None
        with contextlib.closing(self.connect()) as db, db:
            query = """SELECT * FROM release_follow WHERE
                state NOT IN ('cancelled','subscribed','subscription_exists') AND next_check_at<=? AND lease_until<=?
                AND user_id IN (SELECT value FROM json_each(?)) ORDER BY next_check_at,updated_at LIMIT 1"""
            arguments = (now, now, json.dumps(sorted(enabled_users)))
            if db.execute(query, arguments).fetchone() is None:
                return None
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(query, arguments).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE release_follow SET lease=?,lease_until=? WHERE user_id=? AND bgm_id=? AND mode=?",
                       (token, now + 1800, row["user_id"], row["bgm_id"], row["mode"]))
            return {**dict(row), "lease": token}

    def current(self, job: dict[str, Any]) -> bool:
        with contextlib.closing(self.connect()) as db:
            return bool(db.execute("""SELECT 1 FROM release_follow WHERE user_id=? AND bgm_id=? AND mode=?
                AND revision=? AND lease=? AND state NOT IN ('cancelled','subscribed')""",
                (job["user_id"], job["bgm_id"], job["mode"], job["revision"], job["lease"])).fetchone())

    def finish(self, job: dict[str, Any], state: str, matches: list[dict[str, Any]], *,
               now: float, interval: float, error: str | None = None) -> None:
        with contextlib.closing(self.connect()) as db, db:
            db.execute("""UPDATE release_follow SET state=?,matches_json=?,error_code=?,next_check_at=?,
                failures=?,lease=NULL,lease_until=0,updated_at=? WHERE user_id=? AND bgm_id=? AND mode=?
                AND revision=? AND lease=?""",
                (state, json.dumps(matches), error, now + interval, job["failures"] + 1 if error else 0, now,
                 job["user_id"], job["bgm_id"], job["mode"], job["revision"], job["lease"]))


def selected_resource(resource: dict[str, Any], groups: dict[str, Any], config: dict[str, Any]) -> bool:
    if not resource.get("eligible"):
        return False
    if not groups:
        return True
    group = str(resource.get("resourceGroup") or "")
    text = str(resource.get("variantTitle") or resource.get("name") or resource.get("title") or "")
    policy = config.get("torrentPolicy", {})
    family = resource.get("sourceFamily") or source_family(policy, resource.get("sourceClass"))
    identities = {group.casefold(), archive_group_id(policy, group).casefold()}
    if family == "serial":
        identities.update(row["id"].casefold() for row in serial_group_matches(text, group, serial_profile_language(config)))
    platform = str(resource.get("releaseVariant", {}).get("platform") or "unknown").casefold()
    return any(key.casefold() in identities and (values is None or platform in {v.casefold() for v in values})
               for key, values in groups.items())


def check_one(store: Store, db_path: Path, config: dict[str, Any], *, enabled_users: set[int],
              can_submit: Callable[[], bool], operation: Callable = contextlib.nullcontext,
              owner_enabled: Callable[[int], bool] = lambda _owner: True,
              schedule_torrent: Callable[[int], Any] | None = None, now: float | None = None) -> bool:
    """Claim one bounded unit; network requests never hold an SQLite transaction."""
    now = time.time() if now is None else now
    job = store.claim(now=now, enabled_users=enabled_users)
    if job is None:
        return False
    interval = max(300, int(config.get("components", {}).get("discovery", {}).get("pollMinutes", 30)) * 60)
    try:
        with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
            row = db.execute("SELECT id FROM anime_work WHERE bgm_id=?", (job["bgm_id"],)).fetchone()
        if row is None:
            store.finish(job, "pending", [], now=now, interval=interval, error="metadata_preparing")
            return True
        anime_id = int(row[0])
        try:
            remote_ready = ani_rss.state_available(ani_rss.state(db_path, config))
        except (OSError, RuntimeError, sqlite3.Error, httpx.HTTPError):
            if job['mode'] != 'archive':
                raise
            remote_ready = False
        releases: list[dict[str, Any]] = []
        if remote_ready:
            try:
                with operation():
                    if not store.current(job):
                        return True
                    result = ani_rss.search(db_path, anime_id, config)
                    if result.get("stale"):
                        raise RuntimeError("provider_changed")
                    releases = ani_rss.resources(db_path, anime_id, config)
            except (OSError, RuntimeError, httpx.HTTPError):
                if job["mode"] != "archive":
                    raise
                remote_ready = False
        with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
            identity = db.execute("SELECT bgm_id FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if identity is None or int(identity[0]) != job["bgm_id"]:
            raise RuntimeError("catalog_identity_changed")
        if job["mode"] == "archive":
            if schedule_torrent is not None:
                schedule_torrent(anime_id)
            with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
                db.row_factory = sqlite3.Row
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_torrent'").fetchone():
                    releases.extend({**item, "provider": "torrent", "kind": "collection"}
                                    for item in runtime.torrents_for_anime(db, anime_id, config))
        desired = "follow" if job["mode"] == "serial" else "collection"
        candidates = [row for row in releases if row.get("kind") == desired and (
            desired == "follow" or row.get("sourceFamily") == "archive")]
        chosen = [row for row in candidates if selected_resource(row, json.loads(job["groups_json"]), config)]
        evidence = [{key: row.get(key) for key in ("provider", "resourceId", "infoHash", "resourceGroup",
                    "title", "name", "releaseVariant")} for row in chosen[:50]]
        state = "available" if chosen else "unmatched" if candidates else "pending"
        if (job["mode"] == "serial" and chosen and can_submit() and store.current(job)
                and owner_enabled(job["user_id"])):
            with operation():
                if can_submit() and store.current(job) and owner_enabled(job["user_id"]):
                    submitted = ani_rss.subscribe(db_path, str(chosen[0]["resourceId"]), config,
                                                  expected_bgm_id=job["bgm_id"], create_only=True)
                    state = "subscription_exists" if submitted.get("state") == "existing" else "subscribed"
        error = None if remote_ready or job["mode"] == "archive" else "ani_rss_unavailable"
        store.finish(job, state, evidence, now=now, interval=interval, error=error)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, httpx.HTTPError):
        # Remote diagnostics stay in the provider log; stored/UI errors contain no URLs or credentials.
        retry = min(21600, 300 * 2 ** min(int(job["failures"]), 6))
        prior = json.loads(job["matches_json"])
        store.finish(job, "available" if prior else "pending", prior, now=now, interval=retry,
                     error="resource_check_failed")
    return True
