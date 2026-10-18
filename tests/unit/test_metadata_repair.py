from __future__ import annotations

import concurrent.futures
import contextlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from animemachine.catalog import metadata_repair, service
from animemachine.torrents import mapper


class MetadataRepairTests(unittest.TestCase):
    def test_network_does_not_hold_catalog_lock_and_replaced_identity_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "catalog.sqlite3"
            fixture = Path(__file__).parents[1] / "fixtures/anime-catalog.sqlite3"
            shutil.copyfile(fixture, db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.executescript(metadata_repair.SCHEMA)
                aid = db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0]
                db.execute("INSERT INTO metadata_repair_queue(anime_id,reason,updated_at) VALUES(?,'test','now')", (aid,))
            lock = threading.RLock()
            waiting, release = threading.Event(), threading.Event()
            def fetch(*args, **kwargs):
                waiting.set()
                if not release.wait(3):
                    raise AssertionError("test network was not released")
                return {"summary": "belongs to the old identity", "eps": 12}, "test"
            config = {"metadata": {"onlineRepair": {"enabled": True}}}
            with mock.patch.object(metadata_repair.network_sources, "fetch_json", side_effect=fetch), \
                 mock.patch.object(metadata_repair.time, "sleep"), \
                 concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(metadata_repair.run_batch, db_path, config, operation_lock=lock)
                try:
                    self.assertTrue(waiting.wait(2))
                    self.assertTrue(lock.acquire(timeout=.2), "remote repair blocked unrelated Catalog work")
                    try:
                        replacement = root / "replacement.sqlite3"
                        shutil.copyfile(fixture, replacement)
                        with contextlib.closing(sqlite3.connect(replacement)) as db, db:
                            db.execute("UPDATE anime_work SET bgm_id=9999999,summary='new identity' WHERE id=?", (aid,))
                        replacement.replace(db_path)
                    finally:
                        lock.release()
                finally:
                    release.set()
                self.assertEqual(0, future.result(timeout=3)["repaired"])
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                self.assertEqual((9999999, "new identity"), db.execute("SELECT bgm_id,summary FROM anime_work WHERE id=?", (aid,)).fetchone())

    def test_mapper_uses_materialized_series_membership(self):
        fixture = Path(__file__).parents[1] / "fixtures/anime-catalog.sqlite3"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            shutil.copyfile(fixture, path)
            service.ensure_catalog_features(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                aid, component = db.execute("SELECT anime_id,component_id FROM anime_series_component LIMIT 1").fetchone()
                expected = {row[0] for row in db.execute("SELECT anime_id FROM anime_series_component WHERE component_id=?", (component,))}
                queries = []
                db.set_trace_callback(queries.append)
                actual = mapper.relation_component(db, aid)
                self.assertEqual(expected, {row["id"] for row in actual})
                self.assertFalse(any("anime_relation r" in query for query in queries))


if __name__ == "__main__":
    unittest.main()
