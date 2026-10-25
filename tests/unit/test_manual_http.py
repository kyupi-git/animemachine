"""Bundled help must work before login without reaching an external website."""
import contextlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import httpx

from animemachine.catalog import service
from animemachine.config.policy import ConfigStore


class ManualHttpTests(unittest.TestCase):
    def test_unauthenticated_help_assets_and_compose_download(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {
                'ANM_AUTH_ENABLED': 'true', 'ANM_AUTH_DB': str(Path(raw) / 'auth.sqlite3'),
                'ANM_ADMIN_USERNAME': 'test-admin', 'ANM_ADMIN_PASSWORD': 'local-test-password'}):
            root = Path(raw)
            database = root / 'catalog.sqlite3'
            with contextlib.closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            store = ConfigStore(root / 'config.json', service.EXAMPLE_CONFIG)
            handler = service.make_handler(database, store, start_warmup=False)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
                    for locale, title in [('zh-Hans', '首次打开'), ('en', 'First login'), ('ja', '初回ログイン')]:
                        response = client.get(f'/help/{locale}/first-use.html').raise_for_status()
                        self.assertIn(title, response.text)
                        self.assertIn('text/html', response.headers['content-type'])
                    for asset in re.findall(r'(?:src|href)="(/help/assets/[^"?#]+\.(?:js|css))"', response.text):
                        asset_response = client.get(asset).raise_for_status()
                        self.assertTrue(asset_response.content)
                        self.assertEqual(200, client.head(asset).status_code)
                    compose = client.get('/help/downloads/compose.yaml').raise_for_status()
                    expected = Path(__file__).resolve().parents[2] / 'deploy/compose/compose.yaml'
                    self.assertEqual(expected.read_bytes(), compose.content)
                    self.assertEqual(200, client.head('/help/zh-Hans/').status_code)
                    self.assertEqual(401, client.get('/api/capabilities').status_code)
                    self.assertEqual(404, client.get('/help/not-a-chapter.html').status_code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
