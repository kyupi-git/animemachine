import shutil
import sqlite3
import tempfile
import unittest
import contextlib
import hashlib
import io
import datetime as dt
from unittest import mock
from pathlib import Path

from animemachine.catalog import archive_update
from animemachine.catalog import service as catalog


SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "anime-catalog.sqlite3"


class ArchiveUpdateTests(unittest.TestCase):
    def test_same_and_older_archives_are_skipped_before_download_or_rebuild(self):
        for name, digest, created in (
                ('dump-2026-09-29.210337Z.zip', 'sha256:older', '2026-09-29T21:03:37Z'),
                ('dump-2026-10-06.210359Z.zip', 'sha256:different', '2026-10-06T21:03:59Z'),
                ('unknown.zip', 'sha256:current', None)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'catalog.sqlite3'
                shutil.copy2(SAMPLE, path)
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.executemany('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', [
                        ('archive_name', 'dump-2026-10-06.210359Z.zip'),
                        ('archive_created_at', '2026-10-09T00:00:00Z'),
                        ('archive_digest', 'sha256:current')])
                before = path.read_bytes()
                updater = archive_update.ArchiveUpdater(path, catalog)
                with mock.patch.object(catalog, 'resolve_archive_descriptor', return_value={
                        'name': name, 'digest': digest, 'created_at': created}), mock.patch.object(
                        catalog, 'ensure_archive') as download, mock.patch.object(catalog, 'write_database') as build:
                    updater._run()
                self.assertEqual('unchanged', updater.status()['state'])
                self.assertEqual('dump-2026-10-06.210359Z.zip', updater.status()['archiveName'])
                download.assert_not_called()
                build.assert_not_called()
                self.assertEqual(before, path.read_bytes())

    def test_newer_archive_is_downloaded_once_and_existing_covers_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog.sqlite3'
            shutil.copy2(SAMPLE, path)
            catalog.ensure_catalog_features(path)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.executemany('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', [
                    ('archive_name', 'dump-2026-10-06.210359Z.zip'), ('archive_digest', 'sha256:current')])
                db.execute("INSERT OR REPLACE INTO anime_image(anime_id,mime_type,image_blob) VALUES(1,'image/png',?)", (b'cached',))
            descriptor = {'name': 'dump-2026-10-13.210359Z.zip', 'digest': 'sha256:next',
                          'created_at': '2026-10-13T21:03:59Z'}
            def build(destination, _rows, meta):
                shutil.copy2(SAMPLE, destination)
                catalog.ensure_catalog_features(destination)
                with contextlib.closing(sqlite3.connect(destination)) as db, db:
                    db.executemany('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', [
                        ('archive_name', meta['name']), ('archive_digest', meta['digest']),
                        ('archive_created_at', meta['created_at'])])
            updater = archive_update.ArchiveUpdater(path, catalog)
            with mock.patch.object(catalog, 'resolve_archive_descriptor', return_value=descriptor), \
                    mock.patch.object(catalog, 'ensure_archive', return_value=(Path(folder) / 'new.zip', descriptor)) as download, \
                    mock.patch.object(catalog, 'all_anime_manifest', return_value=[]), \
                    mock.patch.object(catalog, 'build_items_from_archive', return_value=[]), \
                    mock.patch.object(catalog, 'write_database', side_effect=build):
                updater._run()
            self.assertEqual('complete', updater.status()['state'], updater.status())
            self.assertEqual(1, download.call_count)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual('sha256:next', db.execute("SELECT value FROM metadata WHERE key='archive_digest'").fetchone()[0])
                self.assertEqual(b'cached', db.execute('SELECT image_blob FROM anime_image WHERE anime_id=1').fetchone()[0])

    def test_archive_filename_timestamp_takes_precedence_over_verification_time(self):
        self.assertEqual(dt.datetime(2026, 10, 6, 21, 3, 59, tzinfo=dt.timezone.utc),
                         archive_update.archive_release_time('dump-2026-10-06.210359Z.zip', '2026-10-09T12:00:00Z'))
        self.assertIsNone(archive_update.archive_release_time('unknown.zip', 'invalid'))
        self.assertIsNone(archive_update.archive_release_time('unknown.zip', '2026-10-09T12:00:00'))

    def test_merge_cannot_publish_an_older_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            target, incoming = Path(folder) / 'target.sqlite3', Path(folder) / 'old.sqlite3'
            for path, name in ((target, 'dump-2026-10-06.210359Z.zip'), (incoming, 'dump-2026-09-29.210337Z.zip')):
                shutil.copy2(SAMPLE, path)
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('archive_name',?)", (name,))
            before = target.read_bytes()
            with self.assertRaisesRegex(ValueError, 'older Archive'):
                archive_update.merge_metadata(target, incoming, catalog)
            self.assertEqual(before, target.read_bytes())

    def test_weekly_schedule_uses_upstream_timezone_and_exact_boundary(self):
        zone = archive_update.ARCHIVE_TIMEZONE
        before = dt.datetime(2026, 10, 1, 2, 11, 59, tzinfo=zone)
        after = before + dt.timedelta(seconds=1)
        self.assertEqual(dt.datetime(2026, 9, 24, 2, 12, tzinfo=zone), archive_update.weekly_check(before))
        self.assertEqual(after, archive_update.weekly_check(after.astimezone(dt.timezone.utc)))

    def test_delayed_archive_gets_one_retry_after_24_hours_across_restart(self):
        now = dt.datetime(2026, 10, 1, 2, 12, tzinfo=archive_update.ARCHIVE_TIMEZONE)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(archive_update.threading, "Thread"):
            path = Path(folder) / "catalog.sqlite3"
            updater = archive_update.ArchiveUpdater(path, catalog)
            self.assertTrue(updater.tick(now))
            updater._set("unchanged", archiveCreatedAt="2026-09-22T21:03:37Z")
            updater._finish_schedule(now)
            updater = archive_update.ArchiveUpdater(path, catalog)
            self.assertFalse(updater.tick(now + dt.timedelta(hours=23, minutes=59)))
            self.assertTrue(updater.tick(now + dt.timedelta(days=1)))
            self.assertEqual(2, updater.status()["schedule"]["attempt"])
            updater._set("unchanged", archiveCreatedAt="2026-09-22T21:03:37Z")
            updater._finish_schedule(now + dt.timedelta(days=1))
            self.assertFalse(updater.tick(now + dt.timedelta(days=2)))
            self.assertFalse(updater.tick(now - dt.timedelta(days=7)))
            self.assertTrue(updater.tick(now + dt.timedelta(days=7)))

    def test_already_current_archive_finishes_weekly_cycle_without_retry(self):
        now = dt.datetime(2026, 10, 1, 2, 12, tzinfo=archive_update.ARCHIVE_TIMEZONE)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(archive_update.threading, "Thread"):
            updater = archive_update.ArchiveUpdater(Path(folder) / "catalog.sqlite3", catalog)
            updater.tick(now)
            updater._set("unchanged", archiveCreatedAt="2026-09-29T21:03:37Z")
            updater._finish_schedule(now)
            self.assertTrue(updater.status()["schedule"]["finished"])
            self.assertFalse(updater.tick(now + dt.timedelta(days=1)))

    def test_failed_or_invalid_descriptor_is_retryable_and_single_worker(self):
        now = dt.datetime(2026, 10, 1, 2, 12, tzinfo=archive_update.ARCHIVE_TIMEZONE)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(archive_update.threading, "Thread"):
            updater = archive_update.ArchiveUpdater(Path(folder) / "catalog.sqlite3", catalog)
            self.assertTrue(updater.tick(now))
            self.assertFalse(updater.tick(now + dt.timedelta(minutes=1)))
            updater._set("failed", error="offline")
            updater._finish_schedule(now)
            self.assertFalse(updater.status()["schedule"]["finished"])
            self.assertTrue(updater.tick(now + dt.timedelta(days=1)))
            updater._set("unchanged", archiveCreatedAt="invalid")
            updater._finish_schedule(now + dt.timedelta(days=1))
            self.assertTrue(updater.status()["schedule"]["finished"])

    def test_import_stream_verifies_official_descriptor(self):
        payload = b"official archive"
        descriptor = {"name": "dump-2026-01-01.000000Z.zip", "size": len(payload),
                      "browser_download_url": "https://github.com/example/archive.zip",
                      "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        class Store:
            @staticmethod
            def read():
                return {"metadata": {"network": {}}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
                catalog.network_sources, "fetch_json", return_value=(dict(descriptor), "manifest")):
            updater = archive_update.ArchiveUpdater(Path(folder) / "catalog.sqlite3", catalog, Store(),
                                                    archive_dir=Path(folder) / "archive")
            result = updater.import_stream(io.BytesIO(payload), len(payload), descriptor["name"])
            installed = Path(folder) / "archive" / descriptor["name"]
            self.assertTrue(result["installed"])
            self.assertEqual(payload, installed.read_bytes())
            self.assertTrue(installed.with_suffix(".zip.verified.json").is_file())

    def test_interrupted_update_is_marked_recoverable_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_dir = Path(folder) / "archive"
            archive_dir.mkdir()
            status = archive_dir / ".archive-update-state.json"
            status.write_text('{"state":"building"}\n', encoding="utf-8")
            updater = archive_update.ArchiveUpdater(Path(folder) / "catalog.sqlite3", catalog, archive_dir=archive_dir)
            self.assertEqual("interrupted", updater.status()["state"])
            self.assertEqual("building", updater.status()["previousState"])
            with mock.patch.object(updater, "start", return_value=True) as start:
                self.assertTrue(updater.recover_interrupted())
                start.assert_called_once_with()

    def test_merge_updates_changed_metadata_and_preserves_image_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            target, incoming = Path(folder) / "target.sqlite3", Path(folder) / "incoming.sqlite3"
            shutil.copy2(SAMPLE, target); shutil.copy2(SAMPLE, incoming)
            catalog.ensure_catalog_features(target); catalog.ensure_catalog_features(incoming)
            with contextlib.closing(sqlite3.connect(target)) as db, db:
                anime_id = db.execute("SELECT id FROM anime_work WHERE bgm_id=265").fetchone()[0]
                db.execute("INSERT OR REPLACE INTO anime_image(anime_id,mime_type,image_blob) VALUES(?,?,?)", (anime_id, "image/jpeg", b"cached"))
                db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(?,?,?,?)",
                           (anime_id, "bd", "2026-01-01", "bangumi-archive:infobox:BD発売日"))
            with contextlib.closing(sqlite3.connect(incoming)) as db, db:
                anime_id = db.execute("SELECT id FROM anime_work WHERE bgm_id=265").fetchone()[0]
                db.execute("UPDATE anime_work SET title_en='Updated title' WHERE bgm_id=265")
                db.execute("UPDATE metadata SET value='sha256:new' WHERE key='archive_digest'")
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('release_event_source_version',?)",
                           (catalog.RELEASE_EVENT_SOURCE_VERSION,))
                db.execute("INSERT INTO anime_release_event(anime_id,event_type,release_date,source) VALUES(?,?,?,?)",
                           (anime_id, "bd", "2026-08-27", "bangumi-archive:infobox:BD発売日"))
            archive_update.merge_metadata(target, incoming, catalog)
            with contextlib.closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute("SELECT title_en FROM anime_work WHERE bgm_id=265").fetchone()[0], "Updated title")
                self.assertEqual(db.execute("SELECT image_blob FROM anime_image WHERE anime_id=(SELECT id FROM anime_work WHERE bgm_id=265)").fetchone()[0], b"cached")
                self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='archive_digest'").fetchone()[0], "sha256:new")
                self.assertEqual(
                    db.execute("SELECT release_date FROM anime_release_event WHERE anime_id=(SELECT id FROM anime_work WHERE bgm_id=265) AND event_type='bd'").fetchall(),
                    [("2026-08-27",)],
                )
                self.assertEqual(
                    db.execute("SELECT value FROM metadata WHERE key='release_event_source_version'").fetchone()[0],
                    catalog.RELEASE_EVENT_SOURCE_VERSION,
                )


if __name__ == "__main__":
    unittest.main()
