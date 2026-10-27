from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from animemachine import backup
from animemachine.api import auth
from animemachine.config.policy import ConfigStore
from animemachine.integrations import follows, viewing


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        example = Path(__file__).parents[2]/'src/animemachine/resources/config.example.json'
        self.config = ConfigStore(self.root/'config.json',example)
        self.accounts = auth.Store(self.root/'auth/auth.sqlite3',enabled=True,
                                   bootstrap_username='admin',bootstrap_password='backup-test-pass')
        self.viewing = viewing.Store(self.root/'auth/viewing.sqlite3')
        self.follows = follows.Store(self.root/'auth/follows.sqlite3')
        self.manager = backup.Manager(self.root,self.config,self.accounts.path,auth_enabled=True)
        with contextlib.closing(sqlite3.connect(self.viewing.path)) as db,db:
            db.execute('INSERT INTO viewing_anchor(user_id,bgm_id,episode) VALUES(1,42,3)')

    def episode(self):
        with contextlib.closing(sqlite3.connect(self.viewing.path)) as db:
            return db.execute('SELECT episode FROM viewing_anchor WHERE bgm_id=42').fetchone()[0]

    def modified_package(self, change):
        payload = backup.read_package(self.manager.export());change(payload)
        body = json.dumps(payload).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('data.json',body)
            archive.writestr('manifest.json',json.dumps({'format':'AnimeMachine.backup','formatVersion':1,
                                                       'sha256':hashlib.sha256(body).hexdigest()}))
        return stream.getvalue()

    def stage(self):
        raw = self.manager.export()
        preview = self.manager.preview(io.BytesIO(raw),len(raw))
        with contextlib.closing(sqlite3.connect(self.viewing.path)) as db,db:
            db.execute('UPDATE viewing_anchor SET episode=9')
        self.manager.schedule(preview['token'])
        return self.manager.root/preview['token']

    def test_round_trip_restores_records_and_revokes_old_sessions(self):
        self.stage()
        session = self.accounts.login('admin','backup-test-pass')
        self.assertEqual(9,self.episode())
        self.assertTrue(self.manager.apply_pending())
        self.assertEqual(3,self.episode())
        self.assertIsNone(self.accounts.from_cookie(f'anm_session={session.token}'))
        self.assertIsNotNone(self.accounts.login('admin','backup-test-pass'))
        self.assertEqual('complete',self.manager.status()['state'])
        self.assertFalse(backup.pending(self.root))

    def test_failed_restore_rolls_back_every_live_store(self):
        directory = self.stage()
        original = backup.database_copy
        def fail_once(source,target):
            if source.parent==directory and target==self.viewing.path:
                raise OSError('injected write failure')
            return original(source,target)
        with mock.patch.object(backup,'database_copy',side_effect=fail_once):
            with self.assertRaisesRegex(OSError,'injected'):
                self.manager.apply_pending()
        self.assertEqual(9,self.episode())
        self.assertEqual('failed',self.manager.status()['state'])
        self.assertFalse(backup.pending(self.root))

    def test_interrupted_restore_resumes_before_serving_requests(self):
        directory = self.stage();original = backup.database_copy
        def power_loss(source,target):
            if source.parent==directory and target==self.viewing.path:
                raise SystemExit('simulated process exit')
            return original(source,target)
        with mock.patch.object(backup,'database_copy',side_effect=power_loss):
            with self.assertRaises(SystemExit):
                self.manager.apply_pending()
        self.assertTrue(backup.pending(self.root))
        self.assertTrue(self.manager.apply_pending())
        self.assertEqual(3,self.episode())

    def test_untrusted_archives_and_columns_are_rejected_before_live_changes(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('../config.json','{}')
        with self.assertRaisesRegex(ValueError,'backup_package_invalid'):
            backup.read_package(stream.getvalue())
        def inject(payload):
            data=payload['stores']['accounts']['auth_user']
            data['columns'][0]='id); DROP TABLE auth_user;--'
        raw=self.modified_package(inject)
        with self.assertRaises(ValueError):
            self.manager.preview(io.BytesIO(raw),len(raw))
        self.assertEqual(3,self.episode())
        self.assertIsNotNone(self.accounts.login('admin','backup-test-pass'))

    def test_damaged_queued_backup_does_not_block_startup(self):
        directory=self.stage()
        (directory/'package.zip').write_bytes(b'partial package')
        self.assertFalse(self.manager.apply_pending())
        self.assertEqual(9,self.episode())
        self.assertFalse(backup.pending(self.root))
        self.assertEqual('failed',self.manager.status()['state'])

    def test_interrupted_restore_with_damaged_package_rolls_back(self):
        directory=self.stage();original=backup.database_copy
        def power_loss(source,target):
            if source.parent==directory and target==self.viewing.path:
                raise SystemExit('simulated process exit')
            return original(source,target)
        with mock.patch.object(backup,'database_copy',side_effect=power_loss):
            with self.assertRaises(SystemExit):
                self.manager.apply_pending()
        (directory/'package.zip').unlink()
        self.assertFalse(self.manager.apply_pending())
        self.assertEqual(9,self.episode())
        self.assertFalse(backup.pending(self.root))

    def test_modified_package_after_preview_and_duplicate_restore_are_rejected(self):
        raw = self.manager.export();preview = self.manager.preview(io.BytesIO(raw),len(raw))
        self.manager.schedule(preview['token'])
        with self.assertRaisesRegex(ValueError,'backup_restore_pending'):
            self.manager.schedule(preview['token'])
        (self.manager.root/preview['token']/'package.zip').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'backup_checksum_failed'):
            self.manager.schedule(preview['token'])

    def test_foreign_media_or_credential_paths_cannot_be_imported(self):
        raw=self.modified_package(lambda payload: payload['credentials'].update({'../../arbitrary':'secret'}))
        with self.assertRaisesRegex(ValueError,'backup_package_invalid'):
            self.manager.preview(io.BytesIO(raw),len(raw))
        self.assertFalse((self.root/'arbitrary').exists())


if __name__=='__main__':
    unittest.main()
