"""Archive-backed Japanese edition names, preserving source identities and names."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
import zipfile
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_entity_name(
  entity_kind TEXT NOT NULL,entity_id INTEGER NOT NULL,
  primary_name TEXT NOT NULL,japanese_name TEXT NOT NULL,
  PRIMARY KEY(entity_kind,entity_id)
);
"""


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


GENERATION_KEYS = ('archive_name', 'archive_digest', 'record_count', 'catalog_scope')


def projection_stamp(metadata: dict[str, Any]) -> str:
    return json.dumps([*[metadata.get(key) for key in GENERATION_KEYS], 3])


def _valid_japanese(value: str) -> bool:
    return bool(value.strip()) and not any(
        unicodedata.category(char).startswith('L') and not (
            char.isascii() or '\u3040' <= char <= '\u30ff' or '\u3400' <= char <= '\u9fff'
            or '\uff66' <= char <= '\uff9f' or '\U00020000' <= char <= '\U0003134f'
        ) for char in value)


def _display_name(value: str, kind: str) -> str:
    if kind != 'person':
        return value.strip()
    value = re.split(r'[=＝]', value, maxsplit=1)[0].strip() or value.strip()
    annotation = re.search(r'\s*[（(]([^（）()]*)[）)]\s*$', value)
    if annotation and re.search(r'[\u3040-\u30ff]', annotation[1]) and not re.search(r'[\u3400-\u9fff]', annotation[1]):
        value = value[:annotation.start()].strip()
    return value


def prepare(row: dict[str, Any], kind: str = 'person') -> dict[str, Any]:
    from .service import parse_archive_alias_entries
    from .names import search_name

    original = str(row.get('name') or '').strip()
    aliases = parse_archive_alias_entries(row.get('infobox'))
    labelled = [_display_name(name, kind) for name, label in aliases if label
                and re.search(r'(?i)日文|日本語|日本名|japanese', label)
                and not re.search(r'(?i)假名|仮名|読み|读音|讀音|kana|ふりがな|昵称|暱稱|愛称|本名', label)
                and _valid_japanese(_display_name(name, kind))]
    inferred = [_display_name(name, kind) for name, label in aliases
                if not label and re.search(r'[\u3040-\u30ff]', name) and _valid_japanese(_display_name(name, kind))]
    japanese = _display_name(original, kind)
    if not _valid_japanese(original):
        japanese = next(iter(labelled or inferred), japanese)
    elif re.search(r'[\u3400-\u9fff]', japanese) and not re.search(r'[\u3040-\u30ff]', japanese):
        # Use existing source spellings, never a generated reverse transliteration.
        # A Japanese-script nickname or legal name is not an edition-name label.
        def glyphs(name: str) -> int:
            return sum(search_name(char) != char for char in name if '\u3400' <= char <= '\u9fff')
        for name in labelled:
            if kind == 'person' and not re.search(r'[\u3400-\u9fff]', name):
                continue
            if (glyphs(name) > glyphs(japanese)
                    or (glyphs(japanese) == 0 and re.search(r'[\u3040-\u30ff]', name))):
                japanese = name
                break
    return {'id': int(row['id']), 'name': original, 'japanese_name': japanese}


def projection(kind: str, row: dict[str, Any]) -> tuple[str, int, str, str] | None:
    japanese = str(row.get('japanese_name') or row['name'])
    if japanese != row['name']:
        return kind, int(row['id']), str(row['name']), japanese
    return None


def project_details(db: sqlite3.Connection, result: dict[str, Any]) -> None:
    if result.get('original_language') != 'ja' or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_entity_name'").fetchone():
        return
    original = str(result.get('title_ja') or '')
    if original and not _valid_japanese(original):
        from .service import localized_archive_title
        japanese = localized_archive_title(db, int(result['id']), 'ja', original)
        if japanese and _valid_japanese(japanese):
            result['archive_title'] = original
            result['title_ja'] = japanese
    cast, staff = result.get('cast', []), result.get('staff', [])
    identities = {int(row[key]) for row in [*cast, *staff] for key in ('character_id', 'person_id')
                  if row.get(key) is not None}
    names = {}
    for offset in range(0, len(identities), 400):
        chunk = sorted(identities)[offset:offset + 400]
        marks = ','.join('?' for _ in chunk)
        names.update({(kind, identity): value for kind, identity, value in db.execute(
            f'SELECT entity_kind,entity_id,japanese_name FROM archive_entity_name WHERE entity_id IN ({marks})', chunk)})
    for rows, fields in ((cast, (('character', 'character_id', 'character_name'), ('person', 'person_id', 'person_name'))),
                         (staff, (('person', 'person_id', 'name'),))):
        for row in rows:
            for kind, key, field in fields:
                name = names.get((kind, row.get(key)))
                if name and name != row[field]:
                    row[field + '_original'] = row[field]
                    row[field] = name


def refresh_cached(db_path: Path) -> bool:
    """Heal upgraded catalogs once from their existing verified Archive, without Internet I/O."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
        stamp = projection_stamp(metadata)
        if metadata.get('entity_name_projection') == stamp:
            return False
        character_ids = {int(row[0]) for row in db.execute('SELECT DISTINCT character_id FROM anime_cast WHERE character_id IS NOT NULL')}
        person_ids = {int(row[0]) for row in db.execute('SELECT person_id FROM anime_cast WHERE person_id IS NOT NULL UNION SELECT person_id FROM anime_staff WHERE person_id IS NOT NULL')}
    name = str(metadata.get('archive_name') or '')
    if not name or Path(name).name != name:
        return False
    state_root = Path(os.getenv('ANM_STATE_DIR', str(db_path.parent.parent)))
    candidates = (state_root / 'metadata' / 'archive' / name, Path(os.getenv('ANM_IMPORTS_DIR', '/Imports')) / name)
    archive = next((path for path in candidates if path.is_file()), None)
    if archive is None:
        return False
    from .service import iter_jsonlines
    prepared = []
    with zipfile.ZipFile(archive) as source:
        for kind, filename, identities in (('character', 'character.jsonlines', character_ids),
                                           ('person', 'person.jsonlines', person_ids)):
            if not identities:
                continue
            for row in iter_jsonlines(source, filename):
                if int(row.get('id', -1)) in identities:
                    entry = projection(kind, prepare(row, kind))
                    if entry:
                        prepared.append(entry)
    # The same verified Archive can also repair book-format classification on
    # upgrade. Keep distinct subjects even when their titles are identical.
    with zipfile.ZipFile(archive) as source:
        if 'subject.jsonlines' in source.namelist():
            from .service import refresh_relation_subject_metadata_from_archive
            repaired = refresh_relation_subject_metadata_from_archive(db_path, archive, expected_generation=metadata)
            if repaired.get('superseded'):
                return False
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        migrate(db)
        with db:
            db.execute('BEGIN IMMEDIATE')
            # A concurrent Archive merge wins; retry against that generation.
            current = dict(db.execute('SELECT key,value FROM metadata'))
            if any(current.get(key) != metadata.get(key) for key in GENERATION_KEYS):
                return False
            # Production-country evidence identifies the animation edition; the
            # script of the source IP's primary name does not change its language.
            db.execute("""UPDATE anime_work SET original_language='ja' WHERE original_language<>'ja'
                AND id IN (SELECT anime_id FROM anime_country WHERE country_code='JP'
                           AND evidence IN ('archive_tag','archive_infobox','manifest'))""")
            db.execute('DELETE FROM archive_entity_name')
            db.executemany('INSERT INTO archive_entity_name VALUES(?,?,?,?)', prepared)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('entity_name_projection',?)", (stamp,))
            from .service import rebuild_directory_titles
            rebuild_directory_titles(db)
    return True
