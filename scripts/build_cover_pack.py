#!/usr/bin/env python3
"""Prepare one public, verified seasonal cover bundle for every release platform."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def season_range(today: dt.date) -> tuple[str, str]:
    anchor = today + dt.timedelta(days=7)
    ordinal = anchor.year * 12 + ((anchor.month - 1) // 3) * 3
    def month(value: int) -> str:
        return f'{value // 12:04d}-{value % 12 + 1:02d}'
    return month(ordinal - 1), month(ordinal + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'src/animemachine/resources/season-covers.zip')
    parser.add_argument('--cache', type=Path, default=ROOT/'.local/cover-pack-build')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ['ANM_STATE_DIR'] = str(args.cache.resolve())
    from animemachine.catalog import service, cover_pack
    from animemachine.network import transport

    lower, upper = season_range(dt.datetime.now().astimezone().date())
    if args.output.is_file() and args.archive is None:
        try:
            existing = cover_pack.manifest(args.output)
            if existing.get('fromMonth') == lower and existing.get('toMonth') == upper:
                generated = dt.datetime.fromisoformat(existing['generatedAt'])
                if 0 <= (dt.datetime.now(dt.timezone.utc) - generated).total_seconds() < 86400:
                    print(f"[covers] Reusing {len(existing['covers'])} verified seasonal covers.", flush=True)
                    return 0
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            print('[covers] Regenerating the incomplete cover bundle.',flush=True)
    archive, descriptor = service.ensure_archive(args.cache/'archive', supplied=args.archive)
    with zipfile.ZipFile(archive) as source:
        subjects = {int(row['id']):row for row in service.iter_jsonlines(source,'subject.jsonlines')
                    if row.get('type') == 2 and lower <= str(row.get('date') or '')[:7] <= upper}
    works = service.build_items_from_archive(archive, None, {}, basic_only=True, selected_subjects=subjects)
    identities = [int(item.work['bgm_id']) for item in works
        if not (set(code for code,_ in item.manifest.get('_inferred_countries',[])) - {'JP'})
        or 'JP' in {code for code,_ in item.manifest.get('_inferred_countries',[])}]
    if not identities or len(identities) > cover_pack.MAX_COVERS:
        raise ValueError('seasonal selection is empty or exceeds the cover pack limit')
    database = args.cache/'covers.sqlite3'
    with contextlib.closing(sqlite3.connect(database)) as db, db:
        db.executescript('CREATE TABLE IF NOT EXISTS anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER UNIQUE);'
            'CREATE TABLE IF NOT EXISTS anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,'
            'source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);')
        db.executemany('INSERT OR IGNORE INTO anime_work VALUES(?,?)',((identity,identity) for identity in identities))
    transport.configure_host_limit(6)
    service.IMAGE_LIMITER = service.RateLimiter(0)
    def fetch(identity: int) -> int:
        image = service.get_anime_image(database,identity,network={'_aniRssApiKey':''},log_timing=False)
        return identity if image and image[1] != 'image/svg+xml' else 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        completed = list(pool.map(fetch,identities))
    failed = [identity for identity, result in zip(identities,completed) if not result]
    if failed:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch,failed))
    covers = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix='season-covers-', suffix='.zip', dir=args.output.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    checksum_file = temporary.with_name(temporary.name+'.sha256')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as pack, contextlib.closing(sqlite3.connect(database)) as db:
            for identity in identities:
                row = db.execute('SELECT mime_type,image_blob,source_url,fetched_at FROM anime_image WHERE anime_id=?',(identity,)).fetchone()
                if not row or row[1] is None:
                    continue
                content, mime = service.network_validators.image_bytes(bytes(row[1]),str(row[0]))
                extension = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp','image/gif':'gif'}[mime]
                filename = f'covers/{identity}.{extension}'
                pack.writestr(filename,content)
                covers.append({'bgmId':identity,'file':filename,'sha256':hashlib.sha256(content).hexdigest(),
                               'mime':mime,'sourceUrl':row[2],'fetchedAt':row[3]})
            if len(covers) < max(1,int(len(identities)*.75)):
                raise RuntimeError(f'Only {len(covers)}/{len(identities)} covers are available; retry release preparation')
            payload = {'schemaVersion':1,'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
                       'fromMonth':lower,'toMonth':upper,'archive':descriptor['name'],
                       'archiveDigest':descriptor.get('digest',''),
                       'selectedWorks':len(identities),'covers':covers}
            pack.writestr('manifest.json',json.dumps(payload,ensure_ascii=False,separators=(',',':')))
        if temporary.stat().st_size > cover_pack.MAX_PACK_BYTES:
            raise ValueError('seasonal cover bundle exceeds the release size limit')
        with temporary.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        checksum_file.write_text(f'{digest}  {args.output.name}\n',encoding='ascii')
        cover_pack.manifest(temporary)
        temporary.replace(args.output)
        checksum_file.replace(args.output.with_name(args.output.name+'.sha256'))
    finally:
        temporary.unlink(missing_ok=True)
        checksum_file.unlink(missing_ok=True)
    print(f'[covers] Prepared {len(covers)}/{len(identities)} covers for {lower}–{upper} ({args.output.stat().st_size/1048576:.1f} MiB).',flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
