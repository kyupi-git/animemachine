from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from animemachine.catalog import bootstrap, service as catalog


class CatalogBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "作品 # & ' library"
        self.root.mkdir()
        self.path = self.root / 'catalog.sqlite3'
        self.archive = self.root / 'dump-2026-10-13.210353Z.zip'
        subjects = [{'id': n, 'type': 2, 'name': f'作品 {n}', 'name_cn': f'译名 {n}',
                     'date': date, 'platform': 1, 'tags': [], 'infobox': ''}
                    for n, date in ((1, '1980-01-01'), (2, '2026-10-01'), (3, '2025-11-01'), (4, '2027-01-01'))]
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('subject.jsonlines', '\n'.join(json.dumps(row) for row in subjects))
            for name in ('subject-persons', 'subject-characters', 'person-characters', 'subject-relations', 'person', 'character', 'episode'):
                archive.writestr(name + '.jsonlines', '')
        self.descriptor = {'name': self.archive.name, 'digest': 'sha256:' + catalog.file_sha256(self.archive)}
        self.resolve = mock.patch.object(catalog, 'ensure_archive', return_value=(self.archive, self.descriptor))
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        catalog.write_database(self.path, [], {'name': 'bootstrap-pending'})

    def prepare(self, ready=lambda: None):
        return bootstrap.prepare(catalog, self.path, self.root, {}, ready=ready,
                                 progress=lambda value: None, today=dt.date(2026, 10, 20))

    def test_legacy_complete_catalog_is_used_without_remote_or_rebuild(self):
        sample = Path(__file__).parents[1] / 'fixtures/anime-catalog.sqlite3'
        shutil.copyfile(sample, self.path)
        before = self.path.read_bytes()
        with mock.patch.object(catalog, 'ensure_archive') as resolve:
            self.assertFalse(self.prepare())
        resolve.assert_not_called()
        self.assertEqual(before, self.path.read_bytes())

    def test_partial_is_usable_before_full_and_ids_covers_overlay_survive(self):
        observed = []
        seeds = []
        def ready():
            observed.append(bootstrap.metadata(self.path)['catalog_scope'])
            seeds.append(catalog.instance_random_seed(self.path))
            self.assertTrue(seeds[-1], 'cover prefetch requires a stable instance seed before readiness')
            if observed[-1] != 'season':
                return
            with contextlib.closing(sqlite3.connect(self.path)) as db, db:
                identity = db.execute('SELECT id FROM anime_work WHERE bgm_id=2').fetchone()[0]
                observed.append(identity)
                db.execute("INSERT INTO anime_image(anime_id,mime_type,image_blob) VALUES(?,'image/png',?)", (identity,b'cache'))
                db.execute("INSERT INTO ani_rss_search_state(anime_id,last_attempt_at,result_count) VALUES(?,'test',3)", (identity,))
        self.assertTrue(self.prepare(ready))
        self.assertEqual('season', observed[0])
        self.assertEqual('complete', observed[-1])
        self.assertEqual(seeds[0], seeds[-1])
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(observed[1], db.execute('SELECT id FROM anime_work WHERE bgm_id=2').fetchone()[0])
            self.assertEqual(b'cache', db.execute('SELECT image_blob FROM anime_image').fetchone()[0])
            self.assertEqual(3, db.execute('SELECT result_count FROM ani_rss_search_state').fetchone()[0])
            self.assertEqual(4, db.execute('SELECT COUNT(*) FROM anime_work').fetchone()[0])
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
        self.assertFalse(bootstrap.pending(self.path))

    def test_restart_reuses_completed_full_stage_after_failed_publication(self):
        real_merge = bootstrap.merge_metadata
        count = 0
        def merge(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('interrupted before full publication')
            return real_merge(*args)
        with mock.patch.object(bootstrap, 'merge_metadata', side_effect=merge):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.prepare()
        self.assertTrue(bootstrap.pending(self.path))
        with mock.patch.object(catalog, 'build_items_from_archive') as parse:
            self.assertTrue(self.prepare())
        parse.assert_not_called()
        self.assertFalse(bootstrap.pending(self.path))

    def test_failed_full_build_preserves_recent_catalog_and_restarts_without_reselecting(self):
        real_build = catalog.build_items_from_archive
        def build(*args, **kwargs):
            if not kwargs.get('basic_only'):
                raise OSError('simulated full disk')
            return real_build(*args, **kwargs)
        with mock.patch.object(catalog, 'build_items_from_archive', side_effect=build):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual('season', bootstrap.metadata(self.path)['catalog_scope'])
        with mock.patch.object(bootstrap, 'select_recent', side_effect=AssertionError('must resume')):
            self.assertTrue(self.prepare())

    def test_recent_selection_includes_prior_year_movies_and_next_quarter(self):
        rows = bootstrap.select_recent(catalog, self.archive, dt.date(2026, 12, 29))
        self.assertEqual({2,3,4}, set(rows))

    def test_supplied_archive_receipt_is_written_to_state_not_the_import_source(self):
        # Exercise the real resolver despite the controlled fake used by prepare().
        self.resolve.stop()
        cache = self.root / 'writable-cache'
        source_files = set(self.root.iterdir())
        archive, descriptor = catalog.ensure_archive(cache, supplied=self.archive)
        self.assertEqual(self.archive.resolve(), archive)
        self.assertEqual(self.descriptor['digest'], descriptor['digest'])
        self.assertTrue((cache / (self.archive.name + '.verified.json')).is_file())
        self.assertFalse(self.archive.with_suffix('.zip.verified.json').exists())
        self.assertEqual(source_files | {cache}, set(self.root.iterdir()))

    def test_changed_archive_is_rejected_without_replacing_usable_partial(self):
        real_build = catalog.build_items_from_archive
        with mock.patch.object(catalog, 'build_items_from_archive', side_effect=lambda *a, **kw:
                real_build(*a, **kw) if kw.get('basic_only') else (_ for _ in ()).throw(RuntimeError('stop'))):
            with self.assertRaises(RuntimeError):
                self.prepare()
        before = self.path.read_bytes()
        with mock.patch.object(catalog, 'ensure_archive', return_value=(self.archive, {**self.descriptor,'digest':'changed'})):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                self.prepare()
        self.assertEqual(before, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
