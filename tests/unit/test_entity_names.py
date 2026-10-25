import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from animemachine.catalog import entity_names, service


class EntityNamesTests(unittest.TestCase):
    def test_readings_nicknames_legal_names_and_alias_chains_are_not_display_names(self):
        self.assertEqual('アダム＝フォン＝テスト', entity_names.prepare(
            {'id': 4, 'name': 'アダム＝フォン＝テスト'}, 'character')['japanese_name'])
        self.assertEqual('=LOVE', entity_names.prepare({'id': 5, 'name': '=LOVE'})['japanese_name'])
        cases = [
            ({'id': 4906, 'name': '寺島拓篤', 'infobox': '{{Infobox\n|别名={\n[日文名|寺岛拓笃＝石川ゆうすけ＝御勒院鹰磨]\n}\n}}'}, '寺島拓篤'),
            ({'id': 3898, 'name': '速水奨', 'infobox': '{{Infobox\n|别名={\n[本名|大濱靖（おおはまやすし，Oohama Yasushi）]\n}\n}}'}, '速水奨'),
            ({'id': 7498, 'name': '小松未可子', 'infobox': '{{Infobox\n|别名={\n[昵称|みかこし]\n}\n}}'}, '小松未可子'),
            ({'id': 1, 'name': '大濱靖（おおはまやすし，Oohama Yasushi）'}, '大濱靖'),
            ({'id': 2, 'name': '早见沙织', 'infobox': '{{Infobox\n|别名={\n[日文名|早見沙織]\n}\n}}'}, '早見沙織'),
            ({'id': 3, 'name': '田村由香里', 'infobox': '{{Infobox\n|别名={\n[日文名|田村ゆかり]\n}\n}}'}, '田村ゆかり'),
        ]
        for data, expected in cases:
            with self.subTest(name=data['name']):
                self.assertEqual(expected, entity_names.prepare(data)['japanese_name'])
                self.assertEqual(data['name'], entity_names.prepare(data)['name'])

    def test_explicit_japanese_names_preserve_identity_and_original_names(self):
        entity = entity_names.prepare({'id': 137889, 'name': '데지르 아르망',
            'infobox': '{{Infobox\n|别名={\n[英文名|Desir Herrman]\n[日文名|デジール・アルマン]\n}\n}}'})
        self.assertEqual('デジール・アルマン', entity['japanese_name'])
        self.assertEqual('데지르 아르망', entity['name'])
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            entity_names.migrate(db)
            db.execute('INSERT INTO archive_entity_name VALUES(?,?,?,?)', entity_names.projection('character', entity))
            result = {'original_language': 'ja', 'cast': [{'character_id': 137889,
                'character_name': entity['name'], 'person_id': 1, 'person_name': '寺島拓篤'}]}
            entity_names.project_details(db, result)
            self.assertEqual('デジール・アルマン', result['cast'][0]['character_name'])
            self.assertEqual('데지르 아르망', result['cast'][0]['character_name_original'])
            result['original_language'] = 'ko'
            result['cast'][0]['character_name'] = entity['name']
            entity_names.project_details(db, result)
            self.assertEqual(entity['name'], result['cast'][0]['character_name'])

    def test_no_invented_names_and_japanese_production_evidence(self):
        for name in ('寺島拓篤', 'Aimer', '이름'):
            self.assertEqual(name, entity_names.prepare({'id': 1, 'name': name})['japanese_name'])
        self.assertEqual('ja', service.infer_original_language('도굴왕', [('JP', 'archive_infobox')]))
        self.assertEqual('ko', service.infer_original_language('도굴왕', [('KR', 'archive_infobox')]))
        self.assertEqual('ko', service.infer_original_language('도굴왕', [('JP', 'studio')]))
        row = {'id': 1, 'name': '小林裕介', 'infobox': '{{Infobox\n|别名={\n[纯假名|こばやし ゆうすけ]\n}\n}}'}
        self.assertEqual('小林裕介', entity_names.prepare(row)['japanese_name'])
        row = {'id': 2, 'name': '早见沙织', 'infobox': '{{Infobox\n|别名={\n[日文名|早見沙織]\n[纯假名|はやみ さおり]\n}\n}}'}
        self.assertEqual('早見沙織', entity_names.prepare(row)['japanese_name'])

    def test_cached_archive_heals_upgrade_and_reloads_changed_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'catalog.sqlite3'
            shutil.copyfile(Path(__file__).parents[1] / 'fixtures/anime-catalog.sqlite3', database)
            archive_dir = root / 'metadata/archive'
            archive_dir.mkdir(parents=True)
            archive_path = archive_dir / 'dump-test.zip'
            def write_archive(japanese):
                with zipfile.ZipFile(archive_path, 'w') as archive:
                    archive.writestr('character.jsonlines', json.dumps({'id': 137889, 'name': '데지르 아르망',
                        'infobox': '{{Infobox\n|别名={\n[日文名|' + japanese + ']\n}\n}}'}) + '\n')
                    archive.writestr('person.jsonlines', '')
            write_archive('デジール・アルマン')
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('archive_name',?)", (archive_path.name,))
                db.execute('DELETE FROM anime_cast')
                db.execute("INSERT INTO anime_cast VALUES(1,137889,'데지르 아르망',1,'寺島拓篤','主角','ja','bangumi-archive')")
            with mock.patch.dict(os.environ, {'ANM_STATE_DIR': str(root)}):
                self.assertTrue(entity_names.refresh_cached(database))
                self.assertFalse(entity_names.refresh_cached(database))
                write_archive('デジール')
                with contextlib.closing(sqlite3.connect(database)) as db, db:
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('archive_digest','changed')")
                self.assertTrue(entity_names.refresh_cached(database))
            with contextlib.closing(sqlite3.connect(database)) as db:
                self.assertEqual('デジール', db.execute('SELECT japanese_name FROM archive_entity_name').fetchone()[0])
