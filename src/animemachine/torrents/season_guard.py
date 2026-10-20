"""Keep current-season release streams coherent without changing stored inventory."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..catalog.episodes import playback_episode


SEASONS = re.compile(r'(?i)(?<![A-Za-z0-9])S0*(\d{1,2})(?=E\d|[^A-Za-z0-9]|$)|\bseason\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+season\b|第\s*([一二三四五六七八九十\d]+)\s*[季期]')


def season_numbers(title: str) -> set[int]:
    result = set()
    digits = {char: value for value, char in enumerate('零一二三四五六七八九')}
    for match in SEASONS.finditer(title):
        value = next(value for value in match.groups() if value)
        if value.isdecimal():
            result.add(int(value))
        elif '十' in value:
            left, right = value.split('十', 1)
            result.add(digits.get(left, 1) * 10 + digits.get(right, 0))
        elif value in digits:
            result.add(digits[value])
    return result


def live_frontier(db: sqlite3.Connection, anime_id: int, config: dict[str, Any]) -> int | None:
    from ..integrations import ani_rss
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'ani_rss_release_state', 'ani_rss_state'} <= tables:
        return None
    filename = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not filename or not ani_rss.state_available(ani_rss.state(Path(filename), config)):
        return None
    row = db.execute('SELECT current_episode FROM ani_rss_release_state WHERE anime_id=?', (anime_id,)).fetchone()
    number = playback_episode(db, anime_id, row[0]) if row and row[0] is not None else None
    return int(number) if number and number >= 1 else None


def filter_streams(db: sqlite3.Connection, anime_id: int, items: list[dict[str, Any]],
                   fingerprint: Any, config: dict[str, Any], *, today: dt.date | None = None) -> list[dict[str, Any]]:
    cursor = db.execute('SELECT * FROM anime_work WHERE id=?', (anime_id,))
    row = cursor.fetchone()
    if row is None:
        return []
    work = dict(zip((field[0] for field in cursor.description), row))
    if work.get('media_code') not in {'tv', 'web', 'ova'}:
        return items
    titles = [str(work.get(key) or '') for key in ('title_ja','title_zh_hans','title_en')]
    titles.extend(row[0] for row in db.execute('SELECT title FROM anime_title WHERE anime_id=?', (anime_id,)))
    expected_seasons = set().union(*(season_numbers(title) for title in titles))
    retained = []
    for item in items:
        declared = season_numbers(str(item.get('name') or ''))
        if len(expected_seasons) == 1 and declared and declared != expected_seasons:
            continue
        retained.append(item)
    today = today or dt.datetime.now().astimezone().date()
    try:
        month = dt.date.fromisoformat(str(work.get('start_month')) + '-01')
    except ValueError:
        return retained
    total = int(work.get('episode_count') or 0)
    frontier = live_frontier(db, anime_id, config)
    end = month + dt.timedelta(days=31 + (max(total, 12) - 1) * 7 + 21)
    airing = month <= today <= end and not (frontier and total and frontier >= total)
    if not airing:
        return retained
    streams: dict[tuple, list[dict[str, Any]]] = {}
    for item in retained:
        if not item.get('eligible', True) and not item.get('canOverride'):
            continue
        numbers = [playback_episode(db, anime_id, value, total) for value in item.get('episodeSequence', [])]
        episodes = {int(value) for value in numbers if value is not None and value >= 1 and value.is_integer()}
        # An airing work's stream needs explicit episode evidence.
        if not episodes:
            continue
        if frontier and max(episodes) > frontier:
            continue
        if total and max(episodes) > total:
            continue
        item['episodeSequence'] = sorted(episodes)
        # Excluded episodes cannot silently fill a selectable stream's gaps.
        streams.setdefault((*fingerprint(item), bool(item.get('eligible', True))), []).append(item)
    allowed = set()
    for members in streams.values():
        episodes = {value for item in members for value in item.get('episodeSequence', [])}
        if not episodes:
            allowed.update(id(item) for item in members)
            continue
        latest = max(episodes)
        if len(episodes) != latest or (frontier and frontier - latest > 1):
            continue
        allowed.update(id(item) for item in members)
    return [item for item in retained if id(item) in allowed]
