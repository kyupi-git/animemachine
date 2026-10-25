import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from animemachine import application_update as update, offline_update


class OfflineUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.asset = self.root / 'animemachine-9.0.0-py3-none-any.whl'
        metadata = 'Name: animemachine\nVersion: 9.0.0\nRequires-Dist: httpx==0.28.1\n'
        with zipfile.ZipFile(self.asset, 'w') as archive:
            archive.writestr('animemachine/__main__.py', '')
            archive.writestr('animemachine/web/static/index.html', '<html></html>')
            archive.writestr('animemachine-9.0.0.dist-info/METADATA', metadata)
        self.package = self.root / 'docker.anm-update.zip'
        offline_update.create(self.asset, self.package, version='9.0.0', kind='docker')
        self.env = mock.patch.dict(os.environ, {'ANM_INSTALL_MODE': 'docker', 'ANM_STATE_DIR': str(self.root / 'state'),
                                               'ANM_DOCKER_UPDATE_RUNTIME': '2'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def unpack(self, package=None):
        destination = self.root / 'unpack'
        destination.mkdir(exist_ok=True)
        return offline_update.unpack(package or self.package, destination, mode='docker', current_version='0.5.0',
                                     asset_name=update._asset_name)

    def test_offline_activation_never_queries_release_service_and_preserves_rollback_pointer(self):
        with mock.patch.object(update, 'status', side_effect=AssertionError('network forbidden')), \
                mock.patch.object(update, '_validate_docker_dependencies'):
            result = update.receive_offline(io.BytesIO(self.package.read_bytes()), self.package.stat().st_size,
                                            host='127.0.0.1', port=8787)
        self.assertTrue(result['accepted'])
        root = self.root / 'state/application-update'
        self.assertEqual('9.0.0', (root / 'current').read_text().strip())
        self.assertTrue((root / 'pending.json').exists())
        self.assertEqual('restarting', update.local_status()['upgradeResult']['status'])

    def test_invalid_bundle_checksum_and_extra_paths_are_rejected(self):
        for case in ('checksum', 'extra', 'platform', 'older'):
            with self.subTest(case=case):
                package = self.root / (case + '.zip')
                with zipfile.ZipFile(self.package) as source:
                    manifest = json.loads(source.read('manifest.json'))
                if case == 'checksum': manifest['sha256'] = '0' * 64
                if case == 'platform': manifest['kind'] = 'portable'
                if case == 'older': manifest['version'] = '0.4.0'
                with zipfile.ZipFile(package, 'w') as target:
                    target.writestr('manifest.json', json.dumps(manifest))
                    target.write(self.asset, self.asset.name)
                    if case == 'extra': target.writestr('../unexpected.py', '')
                with self.assertRaises(ValueError): self.unpack(package)
                for path in (self.root / 'unpack').iterdir(): path.unlink()
        self.assertFalse((self.root / 'state/application-update/current').exists())

    def test_incomplete_upload_and_concurrent_update_do_not_change_installation(self):
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            update.receive_offline(io.BytesIO(b'bad'), 50, host='localhost', port=8787)
        with update._UPLOAD_LOCK:
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                update.receive_offline(io.BytesIO(b''), 10, host='localhost', port=8787)

    def test_stale_failed_reinstall_is_history_not_current_install_failure(self):
        update._record_state(lastUpgrade={'status': 'failed', 'version': update.__version__, 'message': 'ValueError'})
        result = update.local_status()
        self.assertTrue(result['upgradeResult']['historical'])
        self.assertEqual('failed', update._read_state()['lastUpgrade']['status'])
        update._CACHE = (time.monotonic(), {'currentVersion': update.__version__, 'latestVersion': update.__version__,
                                          'upgradeResult': {'status': 'installed'}})
        with mock.patch.object(update, '_release_payload', side_effect=AssertionError('network forbidden')):
            self.assertTrue(update.status()['upgradeResult']['historical'])
