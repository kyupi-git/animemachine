"""Reversible, metadata-only corrections over already discovered media."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = '''
CREATE TABLE IF NOT EXISTS external_media_override(
 source_id TEXT NOT NULL,absolute_path TEXT NOT NULL,bgm_id INTEGER NOT NULL,
 episode_number REAL,revision INTEGER NOT NULL,
 original_json TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,updated_at REAL NOT NULL,
 PRIMARY KEY(source_id,absolute_path)
);
CREATE INDEX IF NOT EXISTS ix_media_override_path ON external_media_override(absolute_path);
'''
AUTO_FIELDS = ('anime_id','match_state','episode_number','evidence_json')


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def folders(db_path: Path, *, query: str = '', all_files: bool = False, offset: int = 0) -> dict[str, Any]:
    if not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0:
        raise ValueError('invalid_media_correction')
    with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='external_media_file'").fetchone():
            return {'items': [], 'hasMore': False}
        where = '' if all_files else " AND (f.match_state<>'verified' OR EXISTS(SELECT 1 FROM external_media_override o WHERE o.source_id=f.source_id AND o.absolute_path=f.absolute_path))"
        rows = db.execute('''SELECT f.*,s.root_path,s.kind,w.bgm_id,w.title_ja,w.title_zh_hans,w.title_en,
            o.revision FROM external_media_file f JOIN external_library_source s USING(source_id)
            LEFT JOIN anime_work w ON w.id=f.anime_id
            LEFT JOIN external_media_override o ON o.source_id=f.source_id AND o.absolute_path=f.absolute_path
            WHERE instr(lower(f.absolute_path),lower(?))>0''' + where +
            ' ORDER BY f.source_id,f.absolute_path LIMIT 501 OFFSET ?', (query, offset)).fetchall()
        result = []
        for row in rows[:500]:
            item = dict(row)
            item['directory'] = str(Path(item['absolute_path']).parent)
            item['filename'] = Path(item['absolute_path']).name
            item.pop('evidence_json', None)
            item['mtime_ns'] = str(item['mtime_ns'])
            item['size'] = str(item['size'])
            result.append(item)
        return {'items': result, 'hasMore': len(rows) > 500, 'offset': offset}


def apply_overrides(db: sqlite3.Connection, *, table: str = 'external_media_file', source_id: str | None = None,
                    refresh_original: bool = False) -> None:
    if table not in {'external_media_file', 'scan_file'}:
        raise ValueError('invalid_media_inventory')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='external_media_override'").fetchone():
        return
    if not db.execute('SELECT 1 FROM external_media_override LIMIT 1').fetchone():
        return
    rows = db.execute('''SELECT o.source_id,o.absolute_path,o.bgm_id,o.episode_number,o.size,o.mtime_ns,
        w.id FROM external_media_override o JOIN anime_work w ON w.bgm_id=o.bgm_id'''
        + (' WHERE o.source_id=?' if source_id is not None else ''), (source_id,) if source_id is not None else ()).fetchall()
    for source, path, bgm_id, episode, size, modified, anime_id in rows:
        if refresh_original:
            original = db.execute(f'SELECT {",".join(AUTO_FIELDS)} FROM {table} WHERE source_id=? AND absolute_path=? AND size=? AND mtime_ns=?',
                                  (source,path,size,modified)).fetchone()
            if original:
                db.execute('UPDATE external_media_override SET original_json=? WHERE source_id=? AND absolute_path=?',
                           (json.dumps(dict(zip(AUTO_FIELDS, original))),source,path))
        db.execute(f'''UPDATE {table} SET anime_id=?,episode_number=?,match_state='verified'
            WHERE source_id=? AND absolute_path=? AND size=? AND mtime_ns=?''',
            (anime_id, episode, source, path, size, modified))


def update(db_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    rows = request.get('files')
    undo = request.get('undo', False)
    bgm_id = request.get('bgmId')
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 500 or type(undo) is not bool
            or (not undo and (type(bgm_id) is not int or not 0 < bgm_id < 10**12))):
        raise ValueError('invalid_media_correction')
    with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute('BEGIN IMMEDIATE')
            work = db.execute('SELECT id FROM anime_work WHERE bgm_id=?', (bgm_id,)).fetchone() if not undo else None
            if not undo and work is None:
                raise ValueError('anime_not_found')
            seen = set()
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError('invalid_media_correction')
                source, path = item.get('sourceId'), item.get('path')
                if not isinstance(source, str) or not isinstance(path, str) or (source,path) in seen:
                    raise ValueError('invalid_media_correction')
                seen.add((source,path))
                row = db.execute('''SELECT f.*,s.root_path FROM external_media_file f
                    JOIN external_library_source s USING(source_id) WHERE f.source_id=? AND f.absolute_path=?''',
                    (source, path)).fetchone()
                if row is None or str(item.get('size')) != str(row['size']) or str(item.get('modified')) != str(row['mtime_ns']):
                    raise ValueError('media_inventory_changed')
                try:
                    Path(path).relative_to(Path(row['root_path']))
                except ValueError as exc:
                    raise ValueError('invalid_media_correction') from exc
                prior = db.execute('SELECT * FROM external_media_override WHERE source_id=? AND absolute_path=?',
                                   (source,path)).fetchone()
                if item.get('revision', 0) != (prior['revision'] if prior else 0):
                    raise ValueError('media_correction_conflict')
                if undo:
                    if prior:
                        original = json.loads(prior['original_json'])
                        if prior['size'] == row['size'] and prior['mtime_ns'] == row['mtime_ns']:
                            db.execute('''UPDATE external_media_file SET anime_id=?,match_state=?,episode_number=?,evidence_json=?
                                WHERE source_id=? AND absolute_path=?''', (*[original[key] for key in AUTO_FIELDS],source,path))
                        db.execute('DELETE FROM external_media_override WHERE source_id=? AND absolute_path=?', (source,path))
                    continue
                episode = item.get('episode')
                if episode is not None and (type(episode) not in {int,float} or not 0 <= episode <= 100000):
                    raise ValueError('invalid_episode')
                original = (prior['original_json'] if prior and prior['size'] == row['size'] and prior['mtime_ns'] == row['mtime_ns']
                            else json.dumps({key: row[key] for key in AUTO_FIELDS}))
                db.execute('''INSERT INTO external_media_override VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id,absolute_path) DO UPDATE SET bgm_id=excluded.bgm_id,
                    episode_number=excluded.episode_number,revision=excluded.revision,
                    original_json=excluded.original_json,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    updated_at=excluded.updated_at''',
                    (source,path,bgm_id,episode,(prior['revision'] if prior else 0)+1,
                     original,row['size'],row['mtime_ns'],time.time()))
            apply_overrides(db)
    return {'updated': len(rows), 'undone': undo}
