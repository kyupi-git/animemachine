import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.integrations import playback
from animemachine.library import audit, inventory
from animemachine.storage.sleep import StorageSleepingError
from animemachine.torrents import runtime


class LocalMediaInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.owner = root / 'library' / '『2017_01』『風夏』'
        self.owner.mkdir(parents=True)
        self.database = root / 'catalog.sqlite3'
        shutil.copyfile(Path(__file__).parents[1] / 'fixtures/anime-catalog.sqlite3', self.database)
        self.config = {'deployment': {'libraryUncRoot': str(root / 'library')}}
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            runtime.migrate_overlay(db)
            self.anime_id, = db.execute('SELECT id FROM anime_work ORDER BY id LIMIT 1').fetchone()
            db.execute('DELETE FROM runtime_work')
            db.execute("INSERT INTO runtime_work(private_work_id,anime_id,target_unc,directory_name,official_title,"
                       "date_code,library_state,scope_state,relation_state,origin,mapping_method,evidence_json,updated_at) "
                       "VALUES(1,?,?,'風夏','風夏','2017_01','existing','active','standalone','preexisting_local','test','{}','now')",
                       (self.anime_id, str(self.owner)))

    def file(self, relative):
        target = self.owner / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'local media')
        return target

    def scan(self):
        with mock.patch.object(runtime, 'torrents_for_anime', return_value=[]):
            return audit.audit(self.database, self.config, anime_ids=[self.anime_id])

    def test_deep_local_files_play_from_inventory_without_live_network_walk(self):
        self.file('BDRip/Disc 1/風夏 - 01.mkv')
        self.file('BDRip/Disc 2/風夏 - 02.mkv')
        self.file('BDRip/映像特典/NCOP01.mkv')
        self.file('BDRip/Extras/01.mkv')
        self.file('BDRip/Specials/01.mkv')
        self.assertEqual(1, self.scan()['updated'])
        with mock.patch.object(playback, '_scan_preexisting', return_value=iter(())):
            items = playback.collect_items(self.database, self.anime_id, self.config)
        self.assertEqual([1, 2], [item.episode for item in items])
        self.assertTrue(all(item.origin == 'preexisting' for item in items))
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM runtime_asset').fetchone()[0])

    def test_interrupted_unavailable_or_sleeping_scan_preserves_known_files(self):
        self.file('Show - 01.mkv')
        self.scan()
        with mock.patch.object(audit, '_observed', side_effect=StorageSleepingError('local_library_sleeping')):
            self.assertEqual(1, self.scan()['skipped'])
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM runtime_local_file').fetchone()[0])

    def test_successful_rescan_drops_removed_files_and_updates_catchup_cache(self):
        first = self.file('Show - 01.mkv')
        self.file('Show - 02.mkv')
        self.scan()
        first.unlink()
        self.scan()
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM runtime_local_file').fetchone()[0])
        with mock.patch.object(playback.ani_rss, 'state', return_value={}):
            latest = playback.latest_cached_episodes(self.database, [self.anime_id], self.config)
        self.assertEqual(2, latest[self.anime_id])

    def test_work_title_containing_special_does_not_hide_its_main_feature(self):
        previous = self.owner
        self.owner = previous.parent / 'Special'
        self.owner.mkdir()
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE runtime_work SET target_unc=?', (str(self.owner),))
        self.file('Show - 01.mkv')
        self.scan()
        with mock.patch.object(playback, '_scan_preexisting', return_value=iter(())):
            items = playback.collect_items(self.database, self.anime_id, self.config)
        self.assertEqual(1, len(items))
        with mock.patch.object(playback.ani_rss, 'state', return_value={}):
            self.assertEqual(1, playback.latest_cached_episodes(self.database, [self.anime_id], self.config)[self.anime_id])

    def test_mixed_dvd_bd_and_release_groups_are_selectable_without_episode_duplicates(self):
        for group, release in [('A', 'DVDRip'), ('A', 'BDRip'), ('B', 'BDRip')]:
            for number in (1, 2):
                self.file(f'[{group}Group]/{release}/Disc {number}/[{group}Group] Show - {number:02d} [{release} 1080p].mkv')
        self.scan()
        editions = playback.available_editions(self.database, self.anime_id, self.config)
        self.assertEqual(3, len(editions))
        self.assertTrue(all(row['count'] == 2 for row in editions))
        for edition in editions:
            items = playback.collect_items(self.database, self.anime_id, self.config, source=edition['id'])
            self.assertEqual([1, 2], [item.episode for item in items])
            self.assertEqual(1, len({str(item.path.parent.parent) for item in items}))
        self.assertEqual(2, len(playback.collect_items(self.database, self.anime_id, self.config)))

    def test_title_words_and_fractional_episodes_are_not_mistaken_for_samples(self):
        self.file('[VCB-Studio] Shomin Sample [01][1080p].mkv')
        self.file('[VCB-Studio] Shomin Sample [11.5][1080p].mkv')
        self.file('[VCB-Studio] Shomin Sample [Sample].mkv')
        self.file('Sample/01.mkv')
        self.file('Specials/01.mkv')
        self.file('SPs/[Group] Show [BGM Making 01].mkv')
        self.file('[Group] Show [11_Yokoku].mkv')
        self.file('[Group] Show [OP1PV].mkv')
        self.scan()
        items = playback.collect_items(self.database, self.anime_id, self.config, cached_only=True)
        self.assertEqual([1, 11.5], [item.episode for item in items])

    def test_missing_quality_field_does_not_split_one_release_into_partial_versions(self):
        self.file('[Group] Show [01][DVDRip].avi')
        self.file('[Group] Show [02][DVDRip 480p].avi')
        self.scan()
        editions = playback.available_editions(self.database, self.anime_id, self.config)
        self.assertEqual(1, len(editions))
        self.assertEqual(2, editions[0]['count'])
        self.assertEqual(2, len(playback.collect_items(self.database, self.anime_id, self.config, source=editions[0]['id'])))

    def test_unnumbered_movie_editions_do_not_repeat_the_same_feature(self):
        self.file('BDRip/[Group] Movie.mkv')
        self.file('DVDRip/[Group] Movie.avi')
        self.scan()
        editions = playback.available_editions(self.database, self.anime_id, self.config)
        self.assertEqual(2, len(editions))
        items = playback.collect_items(self.database, self.anime_id, self.config)
        self.assertEqual(1, len(items))
        self.assertIn('BDRip', str(items[0].path))

    def test_cached_edition_list_never_touches_media_and_removed_roots_are_excluded(self):
        self.file('Show - 01.mkv')
        self.scan()
        with mock.patch.object(playback, 'authorize_media_path', side_effect=AssertionError('filesystem forbidden')):
            self.assertEqual(1, len(playback.available_editions(self.database, self.anime_id, self.config, str(self.owner))))
        other = {'deployment': {'libraryUncRoot': str(self.owner.parent.parent / 'different')}}
        self.assertEqual([], playback.collect_items(self.database, self.anime_id, other, cached_only=True))

    @unittest.skipIf(os.name == 'nt', 'Case-sensitive filesystem behavior')
    def test_distinct_posix_paths_do_not_share_a_scan_snapshot(self):
        self.assertNotEqual(inventory.owner_key('/library/Show'), inventory.owner_key('/library/show'))
