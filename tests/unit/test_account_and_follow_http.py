"""Authenticated account and release-tracking workflows through the actual handler."""
import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import httpx

from animemachine.api import auth
from animemachine.catalog import service
from animemachine.config.policy import ConfigStore
from animemachine.integrations import ani_rss
from animemachine.torrents import runtime


class AccountFollowHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'catalog.sqlite3'
        shutil.copyfile(Path(__file__).parents[1] / 'fixtures/anime-catalog.sqlite3', self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            runtime.migrate_overlay(db)
            ani_rss.migrate(db)
        self.initial = self.root / 'initial-admin.txt'
        self.initial.write_text('ANM_ADMIN_USERNAME=admin\nANM_ADMIN_PASSWORD=old-admin-password\n', encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'ANM_AUTH_ENABLED':'true','ANM_AUTH_DB':str(self.root/'auth.sqlite3'),
            'ANM_STATE_DIR':str(self.root),'ANM_ADMIN_USERNAME':'admin','ANM_ADMIN_PASSWORD':'old-admin-password',
            'ANM_BOOTSTRAP_CREDENTIAL_FILE':str(self.initial)})
        patch.start()
        self.addCleanup(patch.stop)
        config = ConfigStore(self.root/'config.json', service.EXAMPLE_CONFIG)
        handler = service.make_handler(self.database, config, start_warmup=False)
        self.server = ThreadingHTTPServer(('127.0.0.1',0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.users = auth.Store(self.root/'auth.sqlite3', enabled=True)
        self.users.create_user('viewer','old-viewer-password','user')

    def client(self, username, password):
        client = httpx.Client(base_url=self.base, trust_env=False)
        self.addCleanup(client.close)
        result = client.post('/api/auth/login',json={'username':username,'password':password}).raise_for_status().json()
        client.headers['X-CSRF-Token'] = result['csrfToken']
        return client

    def test_both_roles_change_password_with_csrf_and_keep_current_session(self):
        for username, old in [('admin','old-admin-password'),('viewer','old-viewer-password')]:
            with self.subTest(username=username):
                client = self.client(username, old)
                other = self.client(username, old)
                body = {'currentPassword':old,'newPassword':'new-account-password'}
                self.assertEqual(403, client.post('/api/auth/password', json=body, headers={'X-CSRF-Token':''}).status_code)
                wrong = client.post('/api/auth/password', json={**body,'currentPassword':'wrong'})
                self.assertEqual('current_password_incorrect',wrong.json()['error'])
                self.assertEqual(200,client.post('/api/auth/password',json=body).status_code)
                self.assertEqual(200,client.get('/api/auth/session').status_code)
                self.assertEqual(401,other.get('/api/auth/session').status_code)
                self.assertIsNone(self.users.login(username,old))
                self.assertIsNotNone(self.users.login(username,'new-account-password'))
        self.assertFalse(self.initial.exists())

    def test_release_tracking_is_account_scoped_and_stale_edits_conflict(self):
        admin = self.client('admin','old-admin-password')
        viewer = self.client('viewer','old-viewer-password')
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            identity = db.execute('SELECT id,bgm_id FROM anime_work ORDER BY id LIMIT 1').fetchone()
        path = f'/api/anime/{identity[0]}/follow'
        body = {'mode':'serial','revision':0,'groups':{'kirara-fantasia':['Baha']}}
        self.assertEqual(202,viewer.post(path,json=body).status_code)
        self.assertEqual(409,viewer.post(path,json=body).status_code)
        self.assertEqual([],admin.get('/api/follows').json()['items'])
        record = viewer.get('/api/follows').json()['items'][0]
        self.assertEqual(identity[1],record['bgmId'])
        detail = viewer.get(f'/api/anime/{identity[0]}').raise_for_status().json()
        self.assertEqual('pending',detail['follows'][0]['state'])
        self.assertEqual(200,viewer.post(path,json={'mode':'serial','cancel':True,'revision':1}).status_code)
        self.assertEqual('cancelled',viewer.get('/api/follows').json()['items'][0]['state'])
        for mode in [[],None,True,'invalid']:
            self.assertEqual(400,viewer.post(path,json={**body,'mode':mode}).status_code)

    def test_connection_path_feedback_reuses_cached_read_only_health_and_is_admin_only(self):
        admin = self.client('admin', 'old-admin-password')
        viewer = self.client('viewer', 'old-viewer-password')
        storage = {'library': {'path': '/Library', 'state': 'sleeping', 'access': 'rw'}}
        with mock.patch.object(service.storage_preflight, 'check_config', return_value=storage) as check:
            first = admin.get('/api/system/health').raise_for_status().json()
            second = admin.get('/api/system/health').raise_for_status().json()
            self.assertEqual(storage, first['storage'])
            self.assertEqual(storage, second['storage'])
            check.assert_called_once()
            self.assertFalse(check.call_args.kwargs.get('verify_write', False))
            self.assertEqual(403, viewer.get('/api/system/health').status_code)
