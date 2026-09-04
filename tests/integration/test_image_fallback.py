from __future__ import annotations

import io
import contextlib
import sqlite3
import tempfile
import threading
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

from animemachine.catalog import service
from animemachine.config.policy import ConfigStore


class ImageFallbackTests(unittest.TestCase):
    def database(self, root: str) -> Path:
        path=Path(root)/"catalog.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER); CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);")
            db.execute("INSERT INTO anime_work VALUES(1,2)")
            db.commit()
        return path

    def test_api_and_image_fallback_are_cached(self):
        buffer=io.BytesIO(); Image.new("RGB",(20,30),"red").save(buffer,"PNG")
        def validated(*_args, **kwargs):
            data, mime = kwargs["validator"](buffer.getvalue(), "image/png")
            return data, mime, "https://bgmimg.anibt.net/pic/cover/l/a.jpg"
        with tempfile.TemporaryDirectory() as raw:
            db=self.database(raw)
            with mock.patch.object(service.network_sources,"fetch_json",return_value=({"images":{"large":"https://lain.bgm.tv/pic/cover/l/a.jpg"}},"mirror")), mock.patch.object(service.network_sources,"fetch_binary",side_effect=validated) as binary:
                data,mime=service.get_anime_image(db,1)
                self.assertEqual("image/webp",mime); self.assertTrue(data)
                service.get_anime_image(db,1); self.assertEqual(1,binary.call_count)

    def test_refresh_keeps_identical_cached_blob_and_only_advances_validation_time(self):
        buffer = io.BytesIO(); Image.new("RGB", (20, 30), "red").save(buffer, "PNG")
        image, mime = service.network_validators.image_bytes(buffer.getvalue(), "image/png")
        with tempfile.TemporaryDirectory() as raw:
            db = self.database(raw)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error) VALUES(1,?,?,?,?,NULL)",
                    (mime, image, "https://old.invalid/a.webp", "2026-08-01T00:00:00+00:00"),
                )
                connection.commit()
            subject = {"images": {"large": "https://lain.bgm.tv/pic/cover/l/a.jpg"}}
            with mock.patch.object(service.network_sources, "fetch_json", return_value=(subject, "direct")), \
                    mock.patch.object(service.network_sources, "fetch_binary", return_value=(image, mime, "https://lain.bgm.tv/pic/cover/l/a.jpg")):
                refreshed = service.get_anime_image(db, 1, refresh=True)
            self.assertEqual((image, mime), refreshed)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                blob, source_url, fetched_at, error = connection.execute(
                    "SELECT image_blob,source_url,fetched_at,error FROM anime_image WHERE anime_id=1"
                ).fetchone()
            self.assertEqual(image, blob)
            self.assertEqual("https://lain.bgm.tv/pic/cover/l/a.jpg", source_url)
            self.assertNotEqual("2026-08-01T00:00:00+00:00", fetched_at)
            self.assertIsNone(error)

    def test_refresh_failure_keeps_healthy_cache_eligible_for_future_checks(self):
        buffer = io.BytesIO(); Image.new("RGB", (20, 30), "red").save(buffer, "PNG")
        image, mime = service.network_validators.image_bytes(buffer.getvalue(), "image/png")
        with tempfile.TemporaryDirectory() as raw:
            db = self.database(raw)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error) VALUES(1,?,?,?,?,NULL)",
                    (mime, image, "https://old.invalid/a.webp", "2026-08-01T00:00:00+00:00"),
                )
                connection.commit()
            with mock.patch.object(service.network_sources, "fetch_json", side_effect=RuntimeError("offline")):
                refreshed = service.get_anime_image(db, 1, refresh=True)
            self.assertEqual((image, mime), refreshed)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                blob, fetched_at, error = connection.execute(
                    "SELECT image_blob,fetched_at,error FROM anime_image WHERE anime_id=1"
                ).fetchone()
            self.assertEqual(image, blob)
            self.assertNotEqual("2026-08-01T00:00:00+00:00", fetched_at)
            self.assertIsNone(error)

    def test_corrupt_persistent_cache_is_replaced_atomically(self):
        buffer=io.BytesIO(); Image.new("RGB",(20,30),"blue").save(buffer,"PNG")
        with tempfile.TemporaryDirectory() as raw:
            db=self.database(raw)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.execute("INSERT INTO anime_image(anime_id,mime_type,image_blob) VALUES(1,'image/png',?)", (b"broken",))
                connection.commit()
            def validated(*_args, **kwargs):
                data, mime = kwargs["validator"](buffer.getvalue(), "image/png")
                return data, mime, "https://lain.bgm.tv/pic/cover/l/a.jpg"
            with mock.patch.object(service.network_sources,"fetch_json",return_value=({"images":{"large":"https://lain.bgm.tv/pic/cover/l/a.jpg"}},"direct")), mock.patch.object(service.network_sources,"fetch_binary",side_effect=validated):
                data,mime=service.get_anime_image(db,1)
            self.assertEqual("image/webp", mime)
            self.assertGreater(len(data), len(b"broken"))
            with contextlib.closing(sqlite3.connect(db)) as connection:
                blob,error=connection.execute("SELECT image_blob,error FROM anime_image WHERE anime_id=1").fetchone()
            self.assertEqual(data, blob); self.assertIsNone(error)

    def test_configured_official_endpoint_keeps_bundled_failovers(self):
        buffer=io.BytesIO(); Image.new("RGB",(20,30),"purple").save(buffer,"PNG")
        seen=[]
        def subject(endpoints, **_kwargs):
            seen.extend(endpoints)
            return {"images":{"large":"https://lain.bgm.tv/pic/cover/l/a.jpg"}}, endpoints[-1]
        def validated(*_args, **kwargs):
            data,mime=kwargs["validator"](buffer.getvalue(),"image/png")
            return data,mime,"https://bgmimg.anibt.net/pic/cover/l/a.jpg"
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(service.network_sources,"fetch_json",side_effect=subject), mock.patch.object(service.network_sources,"fetch_binary",side_effect=validated):
            service.get_anime_image(self.database(raw),1,network={"bangumiApiEndpoints":["https://api.bgm.tv"]})
        self.assertTrue(any("bgmapi.anibt.net" in endpoint for endpoint in seen))
        self.assertTrue(any("api.bangumi.pro" in endpoint for endpoint in seen))

    def test_cover_candidates_prefer_400px_and_keep_original_failover(self):
        with tempfile.TemporaryDirectory() as raw:
            db=self.database(raw)
            seen=[]
            def validated(endpoints, **kwargs):
                seen.extend(endpoints)
                buffer=io.BytesIO(); Image.new("RGB",(20,30),"green").save(buffer,"PNG")
                data,mime=kwargs["validator"](buffer.getvalue(),"image/png")
                return data,mime,endpoints[0]
            subject={"images":{"large":"https://lain.bgm.tv/pic/cover/l/a.jpg","medium":"https://lain.bgm.tv/pic/cover/m/a.jpg"}}
            with mock.patch.object(service.network_sources,"fetch_json",return_value=(subject,"direct")), mock.patch.object(service.network_sources,"fetch_binary",side_effect=validated):
                service.get_anime_image(db,1)
            self.assertEqual("https://lain.bgm.tv/r/400/pic/cover/l/a.jpg", seen[0])
            self.assertIn("https://lain.bgm.tv/pic/cover/l/a.jpg", seen)
            self.assertTrue(any("bgmimg.anibt.net/r/400/pic/cover/l/a.jpg" in url for url in seen))

    def test_all_sources_failed_returns_http_safe_placeholder(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(service.network_sources,"fetch_json",side_effect=RuntimeError("offline")):
            db=self.database(raw)
            data,mime=service.get_anime_image(db,1)
            self.assertEqual("image/svg+xml",mime); self.assertIn(b"<svg",data)
            with contextlib.closing(sqlite3.connect(db)) as connection:
                error=connection.execute("SELECT error FROM anime_image WHERE anime_id=1").fetchone()[0]
            self.assertIn("offline", error)

    def test_transient_cover_failure_is_immediately_retryable_after_network_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            db = self.database(raw)
            with mock.patch.object(service.network_sources, "fetch_json", side_effect=RuntimeError("offline")):
                service.get_anime_image(db, 1)
            _cached, state = service.get_cached_anime_image(db, 1)
            self.assertEqual("transient_error", state)

            buffer = io.BytesIO(); Image.new("RGB", (20, 30), "blue").save(buffer, "PNG")
            image, mime = service.network_validators.image_bytes(buffer.getvalue(), "image/png")
            subject = {"images": {"large": "https://lain.bgm.tv/pic/cover/l/recovered.jpg"}}
            with mock.patch.object(service.network_sources, "fetch_json", return_value=(subject, "direct")), \
                    mock.patch.object(service.network_sources, "fetch_binary", return_value=(image, mime, "https://lain.bgm.tv/pic/cover/l/recovered.jpg")):
                recovered = service.get_anime_image(db, 1)
            self.assertIsNotNone(recovered)
            self.assertNotEqual("image/svg+xml", recovered[1])
            cached, state = service.get_cached_anime_image(db, 1)
            self.assertIsNotNone(cached)
            self.assertEqual("available", state)

    def test_placeholder_response_is_explicit_and_well_formed(self):
        class Fetcher:
            def enqueue(self, *_args, **_kwargs): return True
            def pending(self, *_args, **_kwargs): return False
            def result(self, *_args, **_kwargs): return None
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict("os.environ", {
                "ANM_AUTH_ENABLED":"false", "ANM_AUTH_DB":str(Path(raw)/"auth.sqlite3")}):
            db=self.database(raw)
            handler=service.make_handler(db,ConfigStore(Path(raw)/"config.json",service.EXAMPLE_CONFIG),
                                         submission_enabled=False,image_fetcher=Fetcher())
            server=ThreadingHTTPServer(("127.0.0.1",0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/anime/1/image") as response:
                    body=response.read()
                    self.assertEqual(200,response.status)
                    self.assertEqual("image/svg+xml",response.headers.get_content_type())
                    self.assertEqual("loading",response.headers["X-AnimeMachine-Image-Status"])
                    self.assertEqual("1",response.headers["Retry-After"])
                    self.assertEqual(len(body),int(response.headers["Content-Length"]))
            finally:
                server.shutdown(); server.server_close(); thread.join(2)
