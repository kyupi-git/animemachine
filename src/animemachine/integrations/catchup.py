"""Build a mixed-work TV queue from inventory; never discover or download media."""
from __future__ import annotations

import contextlib
import dataclasses
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import ani_rss, playback, viewing
from ..storage import path_policy
from ..storage.sleep import sleeping_path


def season_params(request: dict[str, Any]) -> dict[str, list[str]]:
    start, end = str(request.get("from") or ""), str(request.get("to") or "")
    if not all(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) for value in (start, end)):
        raise ValueError("invalid season dates")
    first, last = int(start[:4]) * 12 + int(start[5:]), int(end[:4]) * 12 + int(end[5:])
    if last - first != 2:
        raise ValueError("season must span three months")
    params = {"radar": ["1"], "media_type": ["tv"], "sort": ["recent_episode"],
              "limit": ["all"], "start_from": [start], "start_to": [end],
              "language": [str(request.get("language") or "en")]}
    # Reuse the radar's global sort when a user explicitly changes a column.
    if request.get("sort"):
        params.update(sort=[str(request["sort"])], direction=[str(request.get("direction") or "asc")],
                      radar_grouped=["0"])
    return params


def queue(db_path: Path, rows: list[dict[str, Any]], config: dict[str, Any],
          store: viewing.Store, user_id: int) -> tuple[list[playback.PlaybackItem], list[int], list[dict[str, Any]]]:
    items: list[playback.PlaybackItem] = []
    owners: list[int] = []
    groups: list[dict[str, Any]] = []
    remote_available = ani_rss.state_available(ani_rss.state(db_path, config))
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for row in rows:
            if row.get("media_code") != "tv":
                continue
            anime_id = int(row["id"])
            current = db.execute("SELECT bgm_id FROM anime_work WHERE id=?", (anime_id,)).fetchone()
            if current is None or int(current[0]) != int(row["bgm_id"]):
                continue
            candidates = playback.collect_items(db_path, anime_id, config, cached_only=True)
            candidates = [item for item in candidates if item.bgm_id is None or item.bgm_id == int(row["bgm_id"])]
            if remote_available and {"ani_rss_media", "ani_rss_subscription"} <= tables:
                for episode, title, name, size, filename, extension, remote_id in db.execute("""
                    SELECT m.episode,m.title,m.name,m.size,m.filename,m.extension,m.remote_id
                    FROM ani_rss_media m JOIN ani_rss_subscription s ON s.remote_id=m.remote_id
                    WHERE m.anime_id=? AND s.anime_id=? AND s.deleted_at IS NULL""", (anime_id, anime_id)):
                    if playback._is_main_video(Path(name)):
                        candidates.append(playback.PlaybackItem(
                            playback.MediaLocator.remote(filename, name, extension, size or 0, remote_id),
                            title or name, playback.playback_episode(db, anime_id, episode), size or 0, "ani-rss"))
            record = store.get(user_id, int(row["bgm_id"]))
            anchor = record["episode"]
            selected = [item for item in playback._default_main_queue(candidates)
                        if item.episode is not None and (anchor is None or item.episode >= anchor)]
            selected.sort(key=lambda item: item.episode)
            if not selected:
                continue
            title = str(row.get("title_zh_hans_localized") if config.get("ui", {}).get("language") == "zh-Hans"
                        else row.get("title_en_localized") if config.get("ui", {}).get("language") == "en"
                        else row.get("title_ja_localized"))
            title = title if title and title != "None" else str(row.get("title_ja") or anime_id)
            items.extend(dataclasses.replace(item, title=f"{title} · {playback._episode_label(item.episode)}", bgm_id=int(row["bgm_id"]))
                         for item in selected)
            owners.extend([anime_id] * len(selected))
            groups.append({"animeId": anime_id, "bgmId": int(row["bgm_id"]), "title": title, "episode": anchor,
                           "positionMs": record["positionMs"],
                           "episodes": [item.episode for item in selected], "count": len(selected)})
    return items, owners, groups


def validate(items: list[playback.PlaybackItem], owners: list[int], groups: list[dict[str, Any]],
             config: dict[str, Any]) -> tuple[list[playback.PlaybackItem], list[int], list[dict[str, Any]]]:
    """Validate local inventory at handoff, not while previewing or polling."""
    selected, identities = [], []
    for item, owner in zip(items, owners):
        if item.locator.source_type == "local":
            if sleeping_path(item.path, config):
                continue
            try:
                path = playback.authorize_media_path(item.path, config)
                if not path.is_file() or (item.bytes and path.stat().st_size != item.bytes):
                    continue
            except (path_policy.PathAuthorizationError, OSError):
                continue
        selected.append(item)
        identities.append(owner)
    updated = []
    for group in groups:
        episodes = [item.episode for item, owner in zip(selected, identities) if owner == group["animeId"]]
        if episodes:
            updated.append({**group, "episodes": episodes, "count": len(episodes)})
    return selected, identities, updated
