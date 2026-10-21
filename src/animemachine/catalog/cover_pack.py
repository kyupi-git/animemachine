"""Seed the normal image cache from the release's verified seasonal covers."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from ..network.validators import image_bytes


DEFAULT_PACK = Path(__file__).resolve().parents[1] / 'resources' / 'season-covers.zip'
MAX_PACK_BYTES = 128 * 1024 * 1024
MAX_COVERS = 512


def manifest(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_PACK_BYTES:
        raise ValueError('seasonal cover pack exceeds the size limit')
    checksum_text = path.with_name(path.name + '.sha256').read_text(encoding='ascii').split()
    checksum = checksum_text[0] if checksum_text else ''
    if not re.fullmatch(r'[a-fA-F0-9]{64}', checksum):
        raise ValueError('invalid seasonal cover pack checksum')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum.casefold():
            raise ValueError('seasonal cover pack checksum mismatch')
    with zipfile.ZipFile(path) as pack:
        entries = pack.infolist()
        if (len(entries) > MAX_COVERS + 1 or len({entry.filename for entry in entries}) != len(entries)
                or sum(entry.file_size for entry in entries) > MAX_PACK_BYTES
                or pack.getinfo('manifest.json').file_size > 1024 * 1024):
            raise ValueError('invalid seasonal cover pack structure')
        payload = json.loads(pack.read('manifest.json'))
        if not isinstance(payload, dict) or payload.get('schemaVersion') != 1:
            raise ValueError('unsupported seasonal cover pack')
        covers = payload.get('covers')
        if not isinstance(covers, list) or not 1 <= len(covers) <= MAX_COVERS:
            raise ValueError('invalid seasonal cover list')
        identities = set()
        files = {'manifest.json'}
        for item in covers:
            if not isinstance(item, dict) or type(item.get('bgmId')) is not int or item['bgmId'] <= 0:
                raise ValueError('invalid seasonal cover identity')
            bgm_id = item['bgmId']
            filename = str(item.get('file') or '')
            if bgm_id in identities or not re.fullmatch(rf'covers/{bgm_id}\.(jpg|png|webp|gif)', filename):
                raise ValueError('duplicate or invalid seasonal cover entry')
            if not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256') or '')):
                raise ValueError('invalid seasonal cover digest')
            timestamp = dt.datetime.fromisoformat(str(item.get('fetchedAt') or ''))
            if timestamp.tzinfo is None:
                raise ValueError('seasonal cover timestamp needs a timezone')
            url = urllib.parse.urlsplit(str(item.get('sourceUrl') or ''))
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('invalid seasonal cover source')
            info = pack.getinfo(filename)
            if info.file_size > 12 * 1024 * 1024:
                raise ValueError('oversized seasonal cover')
            identities.add(bgm_id)
            files.add(filename)
        if files != {entry.filename for entry in entries}:
            raise ValueError('unexpected seasonal cover pack content')
    return payload


def seed(db_path: Path, pack_path: Path | None = None) -> int:
    """Match Bangumi identities, validate before writing, and preserve existing covers."""
    path = pack_path or DEFAULT_PACK
    if not path.is_file():
        return 0
    payload = manifest(path)
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        missing = {int(bgm): int(anime_id) for anime_id, bgm in db.execute(
            'SELECT w.id,w.bgm_id FROM anime_work w LEFT JOIN anime_image i ON i.anime_id=w.id '
            'WHERE i.image_blob IS NULL')}
    prepared = []
    with zipfile.ZipFile(path) as pack:
        for item in payload['covers']:
            anime_id = missing.get(item['bgmId'])
            if anime_id is None:
                continue
            content = pack.read(item['file'])
            if hashlib.sha256(content).hexdigest() != item['sha256']:
                raise ValueError('seasonal cover image checksum mismatch')
            content, mime = image_bytes(content, str(item.get('mime') or ''))
            prepared.append((anime_id, mime, content, item['sourceUrl'], item['fetchedAt']))
    if not prepared:
        return 0
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        before = db.total_changes
        db.executemany('INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error) '
            'VALUES(?,?,?,?,?,NULL) ON CONFLICT(anime_id) DO UPDATE SET '
            'mime_type=excluded.mime_type,image_blob=excluded.image_blob,source_url=excluded.source_url,'
            'fetched_at=excluded.fetched_at,error=NULL WHERE anime_image.image_blob IS NULL', prepared)
        return db.total_changes - before
