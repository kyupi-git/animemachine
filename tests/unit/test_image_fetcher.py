from __future__ import annotations

import contextlib
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from animemachine.catalog.image_fetcher import (
    IMAGE_PRIORITY_MAINTENANCE,
    ImageFetcher,
    _clear_transient_negative_cache,
)


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

    def test_confirmed_offline_mode_does_not_queue_remote_image_work(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.pending_ids = set()
        fetcher.pending_priority = {}
        fetcher.deferred_refresh = {}
        fetcher.last_results = {}
        fetcher.closed = threading.Event()
        fetcher.network_offline = True
        fetcher.start = lambda: None

        self.assertFalse(ImageFetcher.enqueue(fetcher, 7, {}, priority="foreground"))
        self.assertTrue(fetcher.requests.empty())


    def test_suspected_outage_is_mirrored_without_pausing_remote_image_work(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.workers = 4
        fetcher.background_paused = False
        fetcher.background_concurrency = 4
        fetcher.background_bandwidth_kib = 0
        fetcher.foreground_pressure = False
        fetcher.external_reserve = 0
        fetcher.network_offline = False
        fetcher.network_learning_suppressed = False
        fetcher.start = lambda: None

        ImageFetcher.set_network_state(fetcher, offline=False, suppress_learning=True)
        control = fetcher.requests.get(timeout=1)
        self.assertFalse(control["networkOffline"])
        self.assertTrue(control["networkLearningSuppressed"])
        self.assertFalse(fetcher.network_offline)
        self.assertTrue(fetcher.network_learning_suppressed)

    def test_foreground_request_promotes_pending_background_item(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.pending_ids = {7}
        fetcher.pending_priority = {7: 1}
        fetcher.deferred_refresh = {}
        fetcher.last_results = {}
        fetcher.closed = threading.Event()
        fetcher.start = lambda: None

        self.assertTrue(ImageFetcher.enqueue(fetcher, 7, {"probeTimeoutSeconds": 8}, priority="foreground"))
        queued = fetcher.requests.get(timeout=1)
        self.assertEqual((0, 7), queued[:2])
        self.assertEqual(0, fetcher.pending_priority[7])
        self.assertFalse(queued[3])

    def test_budget_update_is_exposed_by_snapshot_listener(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.results = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.closed = threading.Event()
        fetcher.budget_status = {"effectiveConcurrency": 8, "reason": "idle"}
        listener = threading.Thread(target=ImageFetcher._listen, args=(fetcher,), daemon=True)
        listener.start()
        fetcher.results.put({"control": "budget", "effectiveConcurrency": 3, "reason": "foreground+cpu"})
        deadline = time.monotonic() + 1
        while fetcher.budget_status.get("effectiveConcurrency") != 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(3, fetcher.budget_status["effectiveConcurrency"])
        self.assertEqual("foreground+cpu", fetcher.budget_status["reason"])
        fetcher.closed.set()
        listener.join(1)

    def test_manual_refresh_while_pending_is_run_after_active_request(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.results = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.pending_ids = {7}
        fetcher.pending_priority = {7: 0}
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

    def test_maintenance_refresh_is_lowest_priority_and_does_not_defer_when_pending(self):
        fetcher = object.__new__(ImageFetcher)
        fetcher.requests = queue.Queue()
        fetcher.lock = threading.RLock()
        fetcher.pending_ids = set()
        fetcher.pending_priority = {}
        fetcher.deferred_refresh = {}
        fetcher.last_results = {}
        fetcher.closed = threading.Event()
        fetcher.start = lambda: None

        self.assertTrue(ImageFetcher.enqueue(fetcher, 8, {}, refresh=True, priority="maintenance"))
        queued = fetcher.requests.get(timeout=1)
        self.assertEqual((IMAGE_PRIORITY_MAINTENANCE, 8), queued[:2])
        self.assertTrue(queued[3])

        self.assertTrue(ImageFetcher.enqueue(fetcher, 8, {}, refresh=True, priority="maintenance"))
        self.assertNotIn(8, fetcher.deferred_refresh)


if __name__ == "__main__":
    unittest.main()
