import unittest
import contextlib
import sqlite3
import tempfile
from unittest import mock
from pathlib import Path

from animemachine.integrations import qbt_runtime


class QbtRuntimeTests(unittest.TestCase):
    def test_cancelled_download_can_be_planned_again_without_losing_known_media(self):
        for state, media, expected in [('downloading', False, 'placeholder'), ('queued', True, 'existing'), ('existing', False, 'existing')]:
            with self.subTest(state=state, media=media), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'catalog.sqlite3'
                self.make_database(path, state, media)
                with mock.patch.object(qbt_runtime, 'fetch', return_value=[]), mock.patch.object(qbt_runtime.runtime_catalog, 'refresh_watch_matches'):
                    result = qbt_runtime.refresh(path, 'http://localhost', 'anm')
                with contextlib.closing(sqlite3.connect(path)) as db:
                    self.assertEqual(expected, db.execute('SELECT library_state FROM runtime_work').fetchone()[0])
                    self.assertEqual('missing', db.execute('SELECT qbt_state FROM runtime_submission').fetchone()[0])
                self.assertEqual(1, result['missing'])

    @staticmethod
    def make_database(path, state='downloading', media=False):
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE runtime_submission(info_hash TEXT,qbt_state TEXT,verified_at TEXT);
                CREATE TABLE runtime_work(private_work_id INTEGER,target_unc TEXT,library_state TEXT,origin TEXT,updated_at TEXT);
                CREATE TABLE runtime_torrent_work(info_hash TEXT,private_work_id INTEGER,anime_id INTEGER);
                CREATE TABLE runtime_asset(owner_path TEXT);
            ''')
            db.execute('INSERT INTO runtime_submission VALUES(?,?,?)', ('a'*40, 'downloading', '2026-01-01T00:00:00+00:00'))
            db.execute('INSERT INTO runtime_work VALUES(1,?,?,?,?)', ('/Library/Test', state, 'managed_submission', 'old'))
            db.execute('INSERT INTO runtime_torrent_work VALUES(?,1,1)', ('a'*40,))
            if media:
                db.execute("INSERT INTO runtime_asset VALUES('/Library/Test')")

    def test_category_change_and_failed_full_snapshot_do_not_release_download(self):
        for full in [[{'hash': 'a'*40, 'state': 'downloading'}], RuntimeError('offline')]:
            with self.subTest(full=full), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'catalog.sqlite3'
                self.make_database(path)
                with mock.patch.object(qbt_runtime, 'fetch', side_effect=[[], full]) as fetch, mock.patch.object(qbt_runtime.runtime_catalog, 'refresh_watch_matches'):
                    if isinstance(full, Exception):
                        with self.assertRaisesRegex(RuntimeError, 'offline'):
                            qbt_runtime.refresh(path, 'http://localhost', 'anm')
                    else:
                        qbt_runtime.refresh(path, 'http://localhost', 'anm')
                    self.assertEqual([mock.call('http://localhost', 'anm'), mock.call('http://localhost', None)], fetch.call_args_list)
                with contextlib.closing(sqlite3.connect(path)) as db:
                    self.assertEqual('downloading', db.execute('SELECT library_state FROM runtime_work').fetchone()[0])

    def test_duplicate_torrent_identity_fails_before_database_changes(self):
        row = {"hash": "a"*40, "state": "stoppedDL"}
        with mock.patch.object(qbt_runtime, "fetch", return_value=[row, {**row, "state": "uploading"}]), \
                mock.patch.object(qbt_runtime.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "duplicate torrent identities"):
                qbt_runtime.refresh(Path("unused.sqlite3"), "http://localhost", "AnimeMachine")
        connect.assert_not_called()

    def test_lifecycle(self):
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedDL","progress":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"downloading","progress":0,"downloaded":0}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0,"downloaded":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0.4,"downloaded":400}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedUP","progress":1}), "existing")


if __name__ == "__main__":
    unittest.main()
