from __future__ import annotations

import contextlib
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from animemachine.catalog.image_fetcher import ImageFetcher, _clear_transient_negative_cache


class ImageFetcherTests(unittest.TestCase):
    def test_service_restart_clears_only_transient_negative_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT)")
                db.execute("INSERT INTO anime_image(anime_id,fetched_at,error) VALUES(1,'2026-08-31T00:00:00+00:00','ReadTimeout: slow')")
                db.execute("INSERT INTO anime_image(anime_id,fetched_at,error) VALUES(2,'2026-08-31T00:00:00+00:00','no_cover')")
            _clear_transient_negative_cache(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                rows = dict(db.execute("SELECT anime_id,error FROM anime_image ORDER BY anime_id"))
            self.assertIsNone(rows[1])
            self.assertEqual("no_cover", rows[2])

    def test_manual_refresh_while_pending_is_run_after_active_request(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.results = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.pending_ids = {7}
        fetcher.deferred_refresh = {}
        fetcher.last_results = {}
        fetcher.closed = threading.Event()
        fetcher.start = lambda: None

        self.assertTrue(ImageFetcher.enqueue(fetcher, 7, {"probeTimeoutSeconds": 8}, refresh=True))
        self.assertIn(7, fetcher.deferred_refresh)
        listener = threading.Thread(target=ImageFetcher._listen, args=(fetcher,), daemon=True)
        listener.start()
        fetcher.results.put((7, "error:remote"))
        queued = fetcher.requests.get(timeout=1)
        self.assertEqual((0, 7), queued[:2])
        self.assertTrue(queued[3])
        self.assertIn(7, fetcher.pending_ids)
        fetcher.results.put((7, "available"))
        deadline = time.monotonic() + 1
        while 7 in fetcher.pending_ids and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotIn(7, fetcher.pending_ids)
        self.assertEqual("available", fetcher.last_results[7])
        fetcher.closed.set()
        listener.join(1)


if __name__ == "__main__":
    unittest.main()
