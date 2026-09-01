from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from animemachine.catalog import service
from animemachine.catalog.image_fetcher import ImageFetcher
from animemachine.config.policy import ConfigStore


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class DaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class SlowCoverSource(BaseHTTPRequestHandler):
    release = threading.Event()
    lock = threading.Lock()
    active = 0

    def log_message(self, *_args):
        return

    def do_GET(self):
        if self.path.startswith("/v0/subjects/"):
            anime_id = self.path.rsplit("/", 1)[-1]
            body = json.dumps({"images": {"large": f"http://127.0.0.1:{self.server.server_port}/cover/{anime_id}.png"}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path.startswith("/cover/"):
            with type(self).lock:
                type(self).active += 1
            type(self).release.wait(120)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(200); self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG))); self.end_headers(); self.wfile.write(PNG)
            return
        self.send_error(404)


class ImageFetcherIsolationTests(unittest.TestCase):
    def database(self, root: str) -> Path:
        path = Path(root) / "catalog.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
              CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER,physical_owner_anime_id INTEGER);
              CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);
              CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            db.executemany("INSERT INTO anime_work(id,bgm_id) VALUES(?,?)", ((value, value) for value in range(1, 51)))
            db.executemany("INSERT INTO metadata VALUES(?,?)", (("record_count", "50"), ("archive_name", "test")))
        return path

    def test_twenty_long_downloads_do_not_delay_health_or_stats(self):
        SlowCoverSource.release = threading.Event(); SlowCoverSource.active = 0
        source = DaemonServer(("127.0.0.1", 0), SlowCoverSource)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True); source_thread.start()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw, mock.patch.dict(os.environ, {
                "ANM_AUTH_ENABLED": "false", "ANM_AUTH_DB": str(Path(raw) / "auth.sqlite3")}, clear=False):
            image_db = self.database(raw)
            web_db = Path(raw) / "web-catalog.sqlite3"
            shutil.copy2(Path(__file__).resolve().parents[1] / "fixtures" / "anime-catalog.sqlite3", web_db)
            fetcher = ImageFetcher(image_db, workers=12, host_limit=4)
            store = ConfigStore(Path(raw) / "config.json", service.EXAMPLE_CONFIG)
            current = store.read()
            network = current.setdefault("metadata", {}).setdefault("network", {})
            network.update({
                "bangumiApiEndpoints": [f"http://127.0.0.1:{source.server_port}"],
                "bangumiImageEndpoints": [f"http://127.0.0.1:{source.server_port}"],
                "probeTimeoutSeconds": 120, "maximumAttemptsPerEndpoint": 1,
            })
            store.write(current)
            network = current["metadata"]["network"]
            handler = service.make_handler(web_db, store,
                                           submission_enabled=False, image_fetcher=fetcher)
            handler.log_message = lambda *_args: None
            web = DaemonServer(("127.0.0.1", 0), handler)
            web_thread = threading.Thread(target=web.serve_forever, daemon=True); web_thread.start()
            base = f"http://127.0.0.1:{web.server_port}"
            try:
                with contextlib.closing(sqlite3.connect(web_db)) as db:
                    anime_id = int(db.execute("SELECT id FROM anime_work ORDER BY id LIMIT 1").fetchone()[0])
                ordinary_paths = ["/api/health", "/api/stats", "/api/anime?limit=20&start_from=2020-01",
                                  f"/api/anime/{anime_id}?language=zh-Hans",
                                  f"/api/anime/{anime_id}/relations/graph?language=zh-Hans"]
                for path in ordinary_paths:
                    urllib.request.urlopen(base + path, timeout=3).read()
                for anime_id in range(1, 21):
                    self.assertTrue(fetcher.enqueue(anime_id, network))
                    self.assertTrue(fetcher.enqueue(anime_id, network))
                self.assertEqual(20, fetcher.snapshot()["pending"])
                deadline = time.monotonic() + 12
                while SlowCoverSource.active < 2 and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertGreaterEqual(SlowCoverSource.active, 2)
                latencies = []; duration = max(2.0, float(os.getenv("ANM_IMAGE_ISOLATION_SECONDS", "2")))
                sustained_until = time.monotonic() + duration; index = 0
                while time.monotonic() < sustained_until:
                    started = time.monotonic()
                    path = ordinary_paths[index % len(ordinary_paths)]
                    with urllib.request.urlopen(base + path, timeout=3) as response:
                        self.assertEqual(200, response.status); response.read()
                    latencies.append(time.monotonic() - started)
                    index += 1
                    time.sleep(.2)
                # A task may finish with a bounded failure while the latency probe is
                # running.  It must remain pending or publish a result; neither state
                # is allowed to disappear silently.
                self.assertTrue(all(fetcher.pending(anime_id) or fetcher.result(anime_id) is not None
                                    for anime_id in range(1, 21)))
                self.assertGreaterEqual(sum(fetcher.pending(anime_id) for anime_id in range(1, 21)),
                                        SlowCoverSource.active)
                self.assertLessEqual(SlowCoverSource.active, 4)
                self.assertLess(sorted(latencies)[int(len(latencies) * .95) - 1], .75)
            finally:
                SlowCoverSource.release.set()
                web.shutdown(); web.server_close(); web_thread.join(2)
                fetcher.close()
        source.shutdown(); source.server_close(); source_thread.join(2)


if __name__ == "__main__":
    unittest.main()
