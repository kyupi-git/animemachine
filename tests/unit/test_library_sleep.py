import contextlib
import copy
import datetime as dt
import json
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from animemachine.config.loader import ConfigError, _validate
from animemachine.integrations import playback, subtitle_service
from animemachine.library import audit, layout, placeholders
from animemachine.storage import path_policy, preflight, sleep, status

ROOT = Path(__file__).resolve().parents[2]


class LibrarySleepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = self.root / 'library'
        self.external = self.root / 'external'
        self.library.mkdir()
        self.external.mkdir()
        self.config = json.loads((ROOT / 'config/config.example.json').read_text(encoding='utf-8'))
        self.config['deployment']['libraryUncRoot'] = str(self.library)
        self.config['library']['sleep'] = {'enabled': True, 'start': '23:00', 'end': '08:00'}
        status.clear_cache()

    def test_live_quiet_hours_recheck_only_copies_required_settings_and_preserves_overrides(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        from animemachine.config.runtime import apply_runtime_overrides
        with mock.patch.dict(os.environ, {'ANM_CONFIG_PATH': str(path), 'ANM_LIBRARY_DIR': str(self.external)}), \
                mock.patch('animemachine.config.runtime.apply_runtime_overrides', wraps=apply_runtime_overrides) as apply:
            current = sleep.current_config()
            self.assertEqual(str(self.external), current['deployment']['libraryUncRoot'])
            self.assertEqual(self.config['library']['sleep'], current['library']['sleep'])
            self.assertEqual({'deployment', 'library'}, set(apply.call_args.args[0]))
            current['library']['sleep']['enabled'] = False
            self.assertTrue(sleep.current_config()['library']['sleep']['enabled'], 'callers cannot change cached settings')
            self.config['library']['sleep']['start'] = '22:00'
            path.write_text(json.dumps(self.config) + '\n', encoding='utf-8')
            self.assertEqual('22:00', sleep.current_config()['library']['sleep']['start'])
            os.environ['ANM_LIBRARY_DIR'] = str(self.library)
            self.assertEqual(str(self.library), sleep.current_config()['deployment']['libraryUncRoot'])

    def test_daily_intervals_have_exclusive_end_and_support_midnight(self):
        for clock, expected in [('22:59', False), ('23:00', True), ('23:59', True), ('00:00', True), ('07:59', True), ('08:00', False)]:
            with self.subTest(clock=clock):
                self.assertEqual(expected, sleep.sleeping(self.config, now=dt.datetime.fromisoformat('2026-10-16T' + clock)))
        self.config['library']['sleep'].update(start='12:00', end='14:00')
        for clock, expected in [('11:59', False), ('12:00', True), ('13:59', True), ('14:00', False)]:
            self.assertEqual(expected, sleep.sleeping(self.config, now=dt.datetime.fromisoformat('2026-10-16T' + clock)))
        self.config['library']['sleep']['enabled'] = False
        self.assertFalse(sleep.sleeping(self.config, now=dt.datetime(2026, 10, 16, 13)))
        del self.config['library']['sleep']
        self.assertFalse(sleep.sleeping(self.config))

    def test_invalid_schedules_are_rejected_without_changing_legacy_config(self):
        for value in [None, [], {'enabled': 'yes'}, {'start': '24:00'}, {'end': '7:00'}, {'enabled': True, 'start': '08:00', 'end': '08:00'}]:
            with self.subTest(value=value):
                invalid = copy.deepcopy(self.config)
                invalid['library']['sleep'] = value
                with self.assertRaises(ConfigError):
                    _validate(invalid)
        del self.config['library']['sleep']
        self.assertIs(self.config, _validate(self.config))

    def test_disabled_schedule_does_not_block_other_settings(self):
        self.config['library']['sleep'] = {'enabled': False, 'start': '08:00', 'end': '08:00'}
        self.assertIs(self.config, _validate(self.config))
        self.assertFalse(sleep.sleeping(self.config))
        self.config['library']['sleep']['enabled'] = True
        with self.assertRaises(ConfigError):
            _validate(self.config)

    def test_sleep_overrides_cached_available_status_without_disk_access(self):
        with mock.patch.object(sleep, 'sleeping', return_value=False), mock.patch.object(status, 'probe_path', return_value=(status.AVAILABLE, '')):
            self.assertTrue(status.status_for_path(self.library, config=self.config).available)
        with mock.patch.object(sleep, 'sleeping', return_value=True), mock.patch.object(status, 'probe_path') as probe, mock.patch.object(Path, 'stat') as stat:
            self.assertEqual('sleeping', status.status_for_path(self.library / 'work', require_write=True, config=self.config).state)
            self.assertFalse(probe.called)
            self.assertFalse(stat.called)

    def test_regular_status_reads_do_not_request_write_probes(self):
        profile = status.StorageProfile('library', 'Library', self.library, 'rw', False, 'local-path')
        with mock.patch.object(sleep, 'sleeping', return_value=False), mock.patch.object(status, 'probe_path', return_value=(status.AVAILABLE, '')) as probe:
            status.status_for_profile(profile, config=self.config)
            status.status_for_profile(profile, config=self.config)
            probe.assert_called_once()
            self.assertFalse(probe.call_args.kwargs['require_write'])
            status.status_for_profile(profile, config=self.config, require_write=True)
            self.assertEqual(2, probe.call_count)
            self.assertTrue(probe.call_args.kwargs['require_write'])

    def test_sleeping_root_never_gets_resolved_when_external_media_is_authorized(self):
        target = self.external / 'episode.mkv'
        target.write_bytes(b'media')
        self.config['externalLibraries'] = [{'enabled': True, 'readOnly': True, 'path': str(self.external)}]
        original = Path.resolve
        resolved = []
        def resolve(path, *args, **kwargs):
            resolved.append(path)
            return original(path, *args, **kwargs)
        with mock.patch.object(sleep, 'current_config', return_value=self.config), mock.patch.object(sleep, 'sleeping', return_value=True), mock.patch.object(Path, 'resolve', resolve):
            self.assertEqual(target, playback.authorize_media_path(target, self.config))
            self.assertNotIn(self.library, resolved)
            with playback.open_authorized_media(target, self.config) as (stream, _, __):
                self.assertEqual(b'media', stream.read())
            with self.assertRaises(path_policy.PathAuthorizationError):
                playback.authorize_media_path(self.library / 'episode.mkv', self.config)
            with self.assertRaises(path_policy.PathAuthorizationError):
                subtitle_service.inspect_target(str(self.library), config=self.config)

    def test_maintenance_is_blocked_before_library_io_and_cached_inventory_survives(self):
        database = self.root / 'catalog.sqlite3'
        shutil.copy2(ROOT / 'tests/fixtures/anime-catalog.sqlite3', database)
        with mock.patch.object(sleep, 'current_config', return_value=self.config), mock.patch.object(sleep, 'sleeping', return_value=True), mock.patch.object(Path, 'iterdir') as entries, mock.patch.object(status, 'probe_path') as probe:
            with self.assertRaises(sleep.StorageSleepingError):
                layout.ExistingPathIndex(self.library, config=self.config)
            with self.assertRaises(sleep.StorageSleepingError):
                placeholders.create(database, self.root / 'runtime.sqlite3', self.config)
            result = audit.audit(database, self.config)
            self.assertEqual(0, result['updated'])
            self.assertFalse(entries.called)
            self.assertFalse(probe.called)
        with contextlib.closing(sqlite3.connect(database)) as db:
            self.assertEqual(10, db.execute('SELECT count(*) FROM anime_work').fetchone()[0])

    def test_diagnostics_skip_library_but_still_check_other_paths(self):
        self.config['deployment']['torrentPoolRoot'] = str(self.root / 'torrents')
        with mock.patch.object(sleep, 'sleeping', return_value=True), mock.patch.object(status, 'probe_path', return_value=(status.AVAILABLE, '')) as probe:
            result = preflight.check_config(self.config)
        self.assertEqual('sleeping', result['library']['state'])
        self.assertNotIn(self.library, [call.args[0] for call in probe.call_args_list])
        self.assertTrue(probe.called)

    def test_docker_permission_bootstrap_does_not_touch_a_sleeping_library(self):
        spec = importlib.util.spec_from_file_location('anm_docker_sleep_test', ROOT / 'packaging/docker/entrypoint.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.os = mock.Mock(wraps=os)
        module.os.name = 'posix'
        module.os.geteuid = mock.Mock(return_value=0)
        with mock.patch.object(module, '_numeric_id', return_value=0), mock.patch.object(module, '_writable_paths', return_value=[self.library, self.external]), mock.patch.object(sleep, 'sleeping_path', side_effect=lambda path: path == self.library), mock.patch.object(Path, 'mkdir', autospec=True) as mkdir, mock.patch.object(Path, 'stat', autospec=True, return_value=os.stat_result((0,) * 10)) as stat:
            module._drop_privileges(['run'])
        self.assertEqual([self.external], [call.args[0] for call in mkdir.call_args_list])
        self.assertEqual([self.external], [call.args[0] for call in stat.call_args_list])


if __name__ == '__main__':
    unittest.main()
