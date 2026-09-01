from __future__ import annotations

import sqlite3
import contextlib
import tempfile
import unittest
from pathlib import Path

from animemachine.library import history as library_history


class LibraryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / "library"; self.library.mkdir()
        self.history = self.root / "history"; self.history.mkdir()
        self.db = self.root / "catalog.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_move_records_paths_without_copying_payload(self):
        source = self.library / "old"; source.mkdir(); (source / "a.mkv").write_bytes(b"a")
        target = self.library / "new"
        event = library_history.mutate(self.db, self.library, self.history, operation="rename", source=source, target=target)
        self.assertFalse(source.exists()); self.assertTrue((target / "a.mkv").is_file())
        self.assertIsNone(event["backupPath"])
        self.assertEqual("applied", library_history.list_events(self.db)[0]["state"])

    def test_remove_is_recoverable_and_restore_refuses_collision(self):
        source = self.library / "obsolete.mkv"; source.write_bytes(b"payload")
        event = library_history.mutate(self.db, self.library, self.history, operation="remove", source=source)
        self.assertFalse(source.exists()); self.assertTrue(Path(event["backupPath"]).is_file())
        restored = library_history.restore_removed(self.db, self.library, self.history, event["eventId"])
        self.assertEqual(source.resolve(), Path(restored["restoredPath"]).resolve()); self.assertEqual(b"payload", source.read_bytes())
        with self.assertRaises(ValueError):
            library_history.restore_removed(self.db, self.library, self.history, event["eventId"])

    def test_product_created_change_is_intentionally_omitted(self):
        source = self.library / "empty.txt"; source.write_text("placeholder", encoding="utf-8")
        target = self.library / "empty-renamed.txt"
        library_history.mutate(self.db, self.library, self.history, operation="rename", source=source,
                               target=target, product_created=True)
        self.assertTrue(target.is_file())
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            library_history.migrate(db)
        self.assertEqual([], library_history.list_events(self.db))

    def test_rejects_escape_and_collision(self):
        source = self.library / "a"; source.write_text("a", encoding="utf-8")
        occupied = self.library / "b"; occupied.write_text("b", encoding="utf-8")
        with self.assertRaises(ValueError):
            library_history.mutate(self.db, self.library, self.history, operation="move", source=source, target=occupied)
        with self.assertRaises(ValueError):
            library_history.mutate(self.db, self.library, self.history, operation="move", source=source,
                                   target=self.root / "outside")


if __name__ == "__main__":
    unittest.main()

