"""Shared resource-discovery boundary, independent of user preferences."""
from __future__ import annotations

import contextlib
import re
import sqlite3
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Callable


ADULT_TAGS = frozenset({'nsfw', 'r18', 'r18+', 'r-18', 'r-18+', 'hentai', '成人动画', '成人動畫', '里番', '裏番', '18禁'})


def marked(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def excluded_text(value: str) -> bool:
    text = unicodedata.normalize('NFKC', value).casefold()
    tags = re.findall(r'[\[【〖(]([^\[\]【】〖〗()\r\n]{1,200})[\]】〗)]', text)
    return any(tag.strip() in ADULT_TAGS for tag in tags)


def require_plan_allowed(db_path: Path, plan: dict[str,Any]) -> None:
    identities = {int(value) for item in plan.get('assessments',[]) for value in item.get('animeIds',[])}
    with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
        if any(blocked(db,identity) for identity in identities):
            raise ValueError('content_excluded')


def blocked(db: sqlite3.Connection, anime_id: int) -> bool:
    columns = {str(row[1]) for row in db.execute('PRAGMA table_info(anime_work)')}
    if 'is_nsfw' in columns:
        row = db.execute('SELECT is_nsfw FROM anime_work WHERE id=?', (anime_id,)).fetchone()
        if row:
            return marked(row[0])
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='anime_tag'").fetchone():
        return any(unicodedata.normalize('NFKC', str(row[0])).strip().casefold() in ADULT_TAGS
                   for row in db.execute('SELECT tag FROM anime_tag WHERE anime_id=?', (anime_id,)))
    return False


def require_allowed(db_path: Path, anime_id: int) -> None:
    with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        if blocked(db, anime_id):
            raise ValueError('content_excluded')


def backfill_archive(db_path: Path, archive_path: Path, *, throttle: Callable[[], Any] = lambda: None,
                     progress: Callable[[dict[str,int]], None] = lambda _value: None) -> bool:
    """Recover upstream flags once for a pre-existing catalog, using its own dump."""
    from ..catalog.service import iter_jsonlines
    with contextlib.closing(sqlite3.connect(db_path,timeout=15)) as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('archive_name','archive_digest','resource_content_archive')"))
        identities = {int(row[0]) for row in db.execute('SELECT bgm_id FROM anime_work')}
        archive_stamp = metadata.get('archive_digest') or metadata.get('archive_name') or ''
        stamp = f'{archive_stamp}:{len(identities)}'
        if not archive_stamp or metadata.get('resource_content_archive')==stamp:
            return False
    updates = []
    with zipfile.ZipFile(archive_path) as archive:
        for index,subject in enumerate(iter_jsonlines(archive,'subject.jsonlines'),1):
            identity = subject.get('id')
            if identity in identities:
                updates.append((int(marked(subject.get('nsfw'))),identity))
            if index%2000==0:
                throttle();progress({'done':len(updates),'total':len(identities)})
    if len({identity for _,identity in updates})!=len(identities):
        raise ValueError('archive_subjects_incomplete')
    with contextlib.closing(sqlite3.connect(db_path,timeout=15)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        current = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('archive_name','archive_digest')"))
        if current.get('archive_digest',current.get('archive_name')) != metadata.get('archive_digest',metadata.get('archive_name')):
            return False
        db.executemany('UPDATE anime_work SET is_nsfw=? WHERE bgm_id=?',updates)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('resource_content_archive',?)",(stamp,))
    progress({'done':len(updates),'total':len(identities)})
    return True
