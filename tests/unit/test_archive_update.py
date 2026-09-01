import shutil
import sqlite3
import tempfile
import unittest
import contextlib
import hashlib
import io
from unittest import mock
from pathlib import Path

from animemachine.catalog import archive_update
from animemachine.catalog import service as catalog


SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "anime-catalog.sqlite3"


class ArchiveUpdateTests(unittest.TestCase):
    def test_import_stream_verifies_official_descriptor(self):
        payload = b"official archive"
        descriptor = {"name": "dump-2026-01-01.000000Z.zip", "size": len(payload),
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
            with contextlib.closing(sqlite3.connect(incoming)) as db, db:
                db.execute("UPDATE anime_work SET title_en='Updated title' WHERE bgm_id=265")
                db.execute("UPDATE metadata SET value='sha256:new' WHERE key='archive_digest'")
            archive_update.merge_metadata(target, incoming, catalog)
            with contextlib.closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute("SELECT title_en FROM anime_work WHERE bgm_id=265").fetchone()[0], "Updated title")
                self.assertEqual(db.execute("SELECT image_blob FROM anime_image WHERE anime_id=(SELECT id FROM anime_work WHERE bgm_id=265)").fetchone()[0], b"cached")
                self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='archive_digest'").fetchone()[0], "sha256:new")


if __name__ == "__main__":
    unittest.main()

