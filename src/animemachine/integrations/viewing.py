"""Account-scoped episode anchors, independent of the replaceable Catalog."""
from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from pathlib import Path
from typing import Any


def episode_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_episode")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100000:
        raise ValueError("invalid_episode")
    return number


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS viewing_anchor(
                user_id INTEGER NOT NULL,bgm_id INTEGER NOT NULL,episode REAL,
                revision INTEGER NOT NULL DEFAULT 0,epoch INTEGER NOT NULL DEFAULT 0,
                session_order INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(user_id,bgm_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(viewing_anchor)")}
            for name, declaration in (("position_ms", "INTEGER NOT NULL DEFAULT 0"),
                                      ("duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                                      ("media_key", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE viewing_anchor ADD COLUMN {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _read(db: sqlite3.Connection, user_id: int, bgm_id: int) -> dict[str, Any]:
        row = db.execute("SELECT * FROM viewing_anchor WHERE user_id=? AND bgm_id=?",
                         (user_id, bgm_id)).fetchone()
        return ({"bgmId": bgm_id, "episode": row["episode"], "revision": row["revision"],
                 "epoch": row["epoch"], "source": row["source"], "updatedAt": row["updated_at"],
                 "positionMs": row["position_ms"], "durationMs": row["duration_ms"], "mediaKey": row["media_key"]}
                if row else {"bgmId": bgm_id, "episode": None, "revision": 0,
                             "epoch": 0, "source": "", "updatedAt": 0,
                             "positionMs": 0, "durationMs": 0, "mediaKey": ""})

    def get(self, user_id: int, bgm_id: int) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as db:
            return self._read(db, user_id, bgm_id)

    def snapshot(self, user_id: int) -> list[dict[str, Any]]:
        with contextlib.closing(self._connect()) as db:
            ids = [int(row[0]) for row in db.execute(
                "SELECT bgm_id FROM viewing_anchor WHERE user_id=?", (user_id,))]
            return [self._read(db, user_id, bgm_id) for bgm_id in ids]

    def normalize_catalog_episodes(self, catalog: Path) -> int:
        """Migrate earlier cumulative anchors using authoritative season evidence."""
        from ..catalog.episodes import playback_episode
        changed = 0
        with contextlib.closing(self._connect()) as db, db, contextlib.closing(sqlite3.connect(
                f"file:{catalog.as_posix()}?mode=ro", uri=True)) as metadata:
            rows = db.execute("SELECT user_id,bgm_id,episode FROM viewing_anchor WHERE episode IS NOT NULL").fetchall()
            for row in rows:
                work = metadata.execute("SELECT id FROM anime_work WHERE bgm_id=?", (row["bgm_id"],)).fetchone()
                if work is None:
                    continue
                number = playback_episode(metadata, int(work[0]), row["episode"])
                if number != row["episode"]:
                    changed += db.execute("""UPDATE viewing_anchor SET episode=?,revision=revision+1,epoch=epoch+1
                        WHERE user_id=? AND bgm_id=? AND episode=?""",
                        (number, row["user_id"], row["bgm_id"], row["episode"])).rowcount
        return changed

    def correct(self, user_id: int, bgm_id: int, episode: Any, revision: int, position_ms: int = 0) -> dict[str, Any]:
        number = episode_number(episode)
        position_ms, _ = validate_position(position_ms, 0)
        if number is None:
            position_ms = 0
        with contextlib.closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._read(db, user_id, bgm_id)
            if prior["revision"] != revision:
                raise Conflict("viewing_conflict")
            db.execute("""INSERT INTO viewing_anchor(user_id,bgm_id,episode,revision,epoch,source,updated_at)
                VALUES(?,?,?,?,?,'manual',?) ON CONFLICT(user_id,bgm_id) DO UPDATE SET
                episode=excluded.episode,revision=excluded.revision,epoch=excluded.epoch,
                source=excluded.source,updated_at=excluded.updated_at,position_ms=0,duration_ms=0,media_key=''""",
                (user_id, bgm_id, number, revision + 1, prior["epoch"] + 1, time.time()))
            db.execute("UPDATE viewing_anchor SET position_ms=? WHERE user_id=? AND bgm_id=?", (position_ms, user_id, bgm_id))
            return {**self._read(db, user_id, bgm_id), "previousEpisode": prior["episode"], "previousPositionMs": prior["positionMs"]}

    def started(self, user_id: int, bgm_id: int, episode: float, epoch: int,
                session_order: int, source: str, *, position_ms: int | None = None,
                duration_ms: int = 0, media_key: str = "") -> bool:
        number = episode_number(episode)
        if position_ms is not None:
            position_ms, duration_ms = validate_position(position_ms, duration_ms)
        with contextlib.closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._read(db, user_id, bgm_id)
            order = db.execute("SELECT session_order FROM viewing_anchor WHERE user_id=? AND bgm_id=?",
                               (user_id, bgm_id)).fetchone()
            if prior["epoch"] != epoch or (order and order[0] > session_order):
                return False
            # Range retries and repeated reads of the same episode do not write to disk.
            same_media = prior["episode"] == number and (not media_key or not prior["mediaKey"] or prior["mediaKey"] == media_key)
            if position_ms is None:
                position_ms = prior["positionMs"] if same_media else 0
                duration_ms = prior["durationMs"] if same_media else 0
            if (same_media and order and order[0] == session_order and prior["source"] == source
                    and prior["positionMs"] == position_ms and prior["durationMs"] == duration_ms):
                return False
            db.execute("""INSERT INTO viewing_anchor(user_id,bgm_id,episode,revision,epoch,session_order,source,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,bgm_id) DO UPDATE SET
                episode=excluded.episode,revision=excluded.revision,session_order=excluded.session_order,
                source=excluded.source,updated_at=excluded.updated_at""",
                (user_id, bgm_id, number, prior["revision"] + 1, epoch, session_order, source, time.time()))
            db.execute("UPDATE viewing_anchor SET position_ms=?,duration_ms=?,media_key=? WHERE user_id=? AND bgm_id=?",
                       (position_ms, duration_ms, media_key or (prior["mediaKey"] if same_media else ""), user_id, bgm_id))
            return True


def validate_position(position: Any, duration: Any) -> tuple[int, int]:
    if type(position) is not int or type(duration) is not int or not 0 <= position <= 604800000 or not 0 <= duration <= 604800000:
        raise ValueError("invalid_playback_position")
    if duration and position > duration + 2000:
        raise ValueError("invalid_playback_position")
    return position, duration
