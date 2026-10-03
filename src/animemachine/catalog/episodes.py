"""Work-local episode progress shared by Catalog and resource discovery."""
from __future__ import annotations

import contextlib
import math
import sqlite3
from typing import Any, Iterable


def _legacy_episode_start_number(db: sqlite3.Connection, anime_id: int, episode_count: int) -> int | None:
    """Infer a cumulative episode start only from an unambiguous same-format sequel chain.

    Fresh Catalogs persist Bangumi Archive ``episode.sort`` directly.  This fallback keeps
    existing Catalogs useful until their next Archive refresh without guessing across branches,
    movies, specials, or works whose episode totals are unknown.
    """
    if episode_count <= 0 or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_relation_edge'").fetchone():
        return None
    current = db.execute("SELECT media_code FROM anime_work WHERE id=?", (anime_id,)).fetchone()
    if not current:
        return None
    media_code = str(current[0] or "")
    offset = 0
    node = anime_id
    seen = {node}
    while True:
        predecessors = db.execute(
            """SELECT e.source_anime_id,w.episode_count,w.media_code
               FROM anime_relation_edge e JOIN anime_work w ON w.id=e.source_anime_id
               WHERE e.target_anime_id=? AND e.relation_code='sequel' AND e.grouping=1""",
            (node,),
        ).fetchall()
        if not predecessors:
            return offset + 1 if offset else None
        if len(predecessors) != 1:
            return None
        predecessor_id, predecessor_count, predecessor_media = predecessors[0]
        predecessor_id = int(predecessor_id)
        if predecessor_id in seen or str(predecessor_media or "") != media_code or int(predecessor_count or 0) <= 0:
            return None
        seen.add(predecessor_id)
        offset += int(predecessor_count)
        node = predecessor_id


def _episode_start_number(db: sqlite3.Connection, anime_id: int, episode_count: int,
                          stored: Any = None) -> int | None:
    with contextlib.suppress(TypeError, ValueError):
        value = int(stored)
        if value > 1:
            return value
    inferred = _legacy_episode_start_number(db, anime_id, episode_count)
    return inferred or (_episode_number(stored) or None)


def _episode_number(raw: Any) -> int:
    with contextlib.suppress(TypeError, ValueError, OverflowError):
        value = float(raw or 0)
        if math.isfinite(value) and value > 0:
            return int(value)
    return 0


def _local_episode_number(raw: Any, episode_count: int, episode_start: int | None) -> int:
    value = _episode_number(raw)
    if value <= 0:
        return 0
    # Ani-RSS normally reports work-local numbers. Only reinterpret a value when it
    # exceeds this work's known total and Archive/sequel evidence can map it back
    # into that total. This avoids changing already-local numbering.
    if episode_count > 0 and value > episode_count and episode_start and episode_start > 1 and value >= episode_start:
        local = value - episode_start + 1
        if 1 <= local <= episode_count:
            return local
    return value


def _episode_progress(db: sqlite3.Connection, anime_id: int, episode_count: Any,
                      episode_start: Any, currents: Iterable[Any], totals: Iterable[Any]) -> dict[str, int | None]:
    known_total = _episode_number(episode_count)
    raw_currents = list(currents)
    raw_totals = list(totals)
    needs_offset = known_total > 0 and any(
        _episode_number(value) > known_total for value in [*raw_currents, *raw_totals]
    )
    start = _episode_start_number(db, anime_id, known_total, episode_start) if needs_offset else None
    normalized_currents = [_local_episode_number(value, known_total, start) for value in raw_currents]
    normalized_totals = [_local_episode_number(value, known_total, start) for value in raw_totals]
    current = max(normalized_currents, default=0)
    # Archive episode_count is the work-local total.  Do not let an Ani-RSS
    # franchise-wide total replace it after current progress has been localized.
    # Only fall back to runtime totals when Archive is evidently stale (the
    # observed local current already exceeds its known count) or has no count.
    if known_total > 0 and (not current or current <= known_total):
        total = known_total
    else:
        valid_totals = [value for value in normalized_totals if value > 0 and (not current or value >= current)]
        total = max(valid_totals, default=0)
    return {"current": current or None, "total": total or None}
