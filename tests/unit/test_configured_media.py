"""Fresh local media works before any Torrent pool or Ani-RSS setup."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import httpx

from animemachine.catalog import service
from animemachine.config.policy import ConfigStore
from animemachine.integrations import playback
from animemachine.library import external
from animemachine.storage.sleep import StorageSleepingError
from animemachine.torrents import runtime


class ConfiguredMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'catalog.sqlite3'
        shutil.copyfile(Path(__file__).parents[1] / 'fixtures/anime-catalog.sqlite3', self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            runtime.migrate_overlay(db)
            self.identity = int(db.execute('SELECT id FROM anime_work ORDER BY id LIMIT 1').fetchone()[0])
            db.execute("UPDATE anime_work SET title_ja='Goal Test Series',title_zh_hans='Goal Test Series',"
                       "title_en='Goal Test Series',episode_count=2 WHERE id=?", (self.identity,))
            db.execute('DELETE FROM runtime_work')
            db.execute('DELETE FROM anime_title WHERE anime_id=?', (self.identity,))
        self.config = json.loads(service.EXAMPLE_CONFIG.read_text(encoding='utf-8'))
        self.config['deployment']['libraryUncRoot'] = str(self.root / 'not-configured')
        self.config['components']['aniRss']['mediaPath'] = ''
        self.config['externalLibraries'] = []
        self.media = self.root / 'media'
        self.video('01')

    def video(self, episode):
        path = self.media / 'complete' / 'Goal Test Series' / 'BDRip' / 'Disc 1' / f'[Group] Goal Test Series - {episode}.mkv'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic media')
        return path

    def test_each_directory_kind_plays_without_a_download_service_and_local_filter_agrees(self):
        for kind in ('local', 'ani-rss', 'generic'):
            with self.subTest(kind=kind):
                config = json.loads(json.dumps(self.config))
                if kind == 'local':
                    config['deployment']['libraryUncRoot'] = str(self.media)
                elif kind == 'ani-rss':
                    config['components']['aniRss']['mediaPath'] = str(self.media)
                else:
                    config['externalLibraries'] = [{'id':'test-external','kind':'generic','enabled':True,
                        'path':str(self.media),'readOnly':True,'scanMinutes':60}]
                sources = [source for source in external.configured_sources(config) if source['path'] == str(self.media)]
                with contextlib.closing(sqlite3.connect(self.database)) as db, db:
                    db.execute('DELETE FROM external_media_file') if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='external_media_file'").fetchone() else None
                    db.execute('DELETE FROM external_library_source') if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='external_library_source'").fetchone() else None
                result = external.scan(self.database, sources, force=True, config=config)
                self.assertEqual(1, result['verified'])
                with mock.patch.object(playback.ani_rss, '_client', side_effect=AssertionError('No Ani-RSS API needed')):
                    items = playback.collect_items(self.database, self.identity, config, cached_only=True)
                self.assertEqual(1, len(items))
                state = 'local' if kind == 'local' else 'external'
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    self.assertEqual(state, runtime.collection_state(db, self.identity, include_ani_rss=False))
                filtered = service.query_catalog(self.database, {'library_state':[state], 'limit':['all']}, config)
                self.assertIn(self.identity, {int(item['id']) for item in filtered['items']})

    def test_forced_verification_sees_new_files_before_regular_scan_is_due(self):
        self.config['deployment']['libraryUncRoot'] = str(self.media)
        sources = external.configured_sources(self.config)
        external.scan(self.database, sources, config=self.config)
        self.video('02')
        self.assertEqual(1, external.scan(self.database, sources, config=self.config)['deferred'])
        result = external.scan(self.database, sources, force=True, config=self.config)
        self.assertEqual(2, result['files'])

    def test_changed_or_disabled_roots_forget_only_derived_inventory(self):
        self.config['deployment']['libraryUncRoot'] = str(self.media)
        sources = external.configured_sources(self.config)
        external.reconcile_sources(self.database, self.config)
        external.scan(self.database, sources, force=True, config=self.config)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertIsNotNone(external.status(db,self.identity))
        next_config = json.loads(json.dumps(self.config))
        next_config['deployment']['libraryUncRoot'] = str(self.root/'unavailable-new-root')
        self.assertTrue(external.reconcile_sources(self.database, lambda: next_config))
        self.assertFalse(external.reconcile_sources(self.database, lambda: next_config))
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(external.status(db,self.identity))
        self.assertTrue(self.video('01').is_file())
        result = external.scan(self.database, sources, force=True, config=self.config)
        self.assertGreater(result['deferred'],0)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(external.status(db,self.identity))

    def test_inflight_scan_does_not_publish_after_root_configuration_changes(self):
        self.config['deployment']['libraryUncRoot'] = str(self.media)
        sources = external.configured_sources(self.config)
        external.reconcile_sources(self.database,self.config)
        original_files = external._media_files
        def change_root(root):
            changed = json.loads(json.dumps(self.config))
            changed['deployment']['libraryUncRoot'] = str(self.root/'replacement')
            external.reconcile_sources(self.database,changed)
            yield from original_files(root)
        with mock.patch.object(external,'_media_files',side_effect=change_root):
            result = external.scan(self.database,sources,force=True,config=self.config)
        self.assertGreater(result['deferred'],0)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(external.status(db,self.identity))

    def test_sleep_is_checked_before_any_media_storage_probe(self):
        self.config['deployment']['libraryUncRoot'] = str(self.media)
        with mock.patch.object(external, 'require_awake', side_effect=StorageSleepingError('local_library_sleeping')), \
             mock.patch.object(external, 'status_for_path', side_effect=AssertionError('No sleeping disk access')):
            result = external.scan(self.database, external.configured_sources(self.config), config=self.config)
        self.assertEqual(1, result['unavailable'])
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual('sleeping', db.execute('SELECT scan_state FROM external_library_source').fetchone()[0])

    def test_saving_new_library_starts_real_discovery_and_reports_the_files(self):
        store = ConfigStore(self.root / 'config.json', service.EXAMPLE_CONFIG)
        store.write(self.config)
        with mock.patch.dict(os.environ, {'ANM_AUTH_ENABLED':'false', 'ANM_AUTH_DB':str(self.root/'auth.sqlite3'),
                'ANM_CONFIG_PATH':str(store.path), 'ANM_STATE_DIR':str(self.root/'state')}):
            handler = service.make_handler(self.database, store, start_warmup=False)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
                    draft = json.loads(json.dumps(self.config))
                    draft['deployment']['libraryUncRoot'] = str(self.media)
                    response = client.post('/api/settings', json={'config':draft})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertTrue(response.json()['mediaScanStarted'])
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        state = client.get('/api/maintenance/status').raise_for_status().json()['library']
                        if state['state'] in {'complete', 'failed'}:
                            break
                        time.sleep(.03)
                    self.assertEqual('complete', state['state'], state)
                    self.assertEqual(1, state['files'])
                    self.assertEqual(1, state['total'])
            finally:
                server.shutdown(); server.server_close(); thread.join(2)
