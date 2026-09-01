import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from animemachine.catalog.migrations import migrate


class CatalogMigrationTests(unittest.TestCase):
    def test_fresh_database_reaches_v2(self):
        with tempfile.TemporaryDirectory() as raw:
            db_path = Path(raw) / "catalog.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                report = migrate(db)
                db.commit()
                self.assertEqual(report["schemaVersion"], 2)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"metadata_snapshot", "torrent_resolution", "torrent_target_path", "torrent_manifest_file", "asset_provenance", "release_baseline", "upgrade_candidate"}.issubset(tables))
                self.assertEqual(
                    [row[1] for row in db.execute("PRAGMA table_info(supplement)")],
                    ["target_unc", "info_hash", "file_index", "reason", "status"],
                )


if __name__ == "__main__":
    unittest.main()
