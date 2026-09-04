from __future__ import annotations

import base64
import json
import contextlib
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from animemachine.config.policy import ConfigStore
from animemachine.catalog import service
from animemachine.integrations import ani_rss
from animemachine.torrents import runtime as runtime_catalog


class FakeAniRss(BaseHTTPRequestHandler):
    subscriptions = []
    seen_keys = []
    delete_paths = []
    disconnect_once = False
    fail_file_requests = 0
    file_requests = 0
    list_total_override = None
    media = {
        "/remote/Work - 01.mkv": b"0123456789abcdef",
        "/remote/Work - 02.mkv": b"ABCDEFGHIJKLMNOP",
    }

    def log_message(self, *_args):
        return

    def do_POST(self):
        type(self).seen_keys.append(self.headers.get("api-key"))
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"null") if length else None
        path = self.path.split("?", 1)[0]
        data = None
        if path == "/api/about":
            data = {"version": "test"}
        elif path == "/api/listAni":
            total = (len(self.subscriptions) if type(self).list_total_override is None
                     else int(type(self).list_total_override))
            data = {"total": total, "weekList": [{"items": self.subscriptions}]}
        elif path == "/api/mikan":
            data = {"weeks": [{"items": [{"url": "https://mikan.test/Home/Bangumi/1", "title": "作品"}]}]}
        elif path == "/api/mikanGroup":
            data = [{"label": "SubsPlease", "rss": "https://mikan.test/RSS/Bangumi?subgroupid=1",
                     "items": [{"title": "[SubsPlease] Work - 01 (1080p) [WEB-DL]", "size": 100}]}]
        elif path == "/api/rssToAni":
            data = {"id": "remote-1", "title": "作品", "url": body["url"], "enable": True}
        elif path == "/api/playList":
            data = [
                {"filename": name, "name": name.rsplit("/", 1)[-1], "size": len(payload)}
                for name, payload in self.media.items()
            ]
        elif path == "/api/addAni":
            self.subscriptions.append(body); data = True
        elif path == "/api/deleteAni":
            type(self).delete_paths.append(self.path)
            ids = set(body or []); type(self).subscriptions = [item for item in self.subscriptions if item.get("id") not in ids]; data = True
        else:
            self.send_error(404); return
        payload = json.dumps({"code": 200, "message": "ok", "data": data}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def do_GET(self):
        type(self).file_requests += 1
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/file":
            self.send_error(404)
            return
        if type(self).fail_file_requests > 0:
            type(self).fail_file_requests -= 1
            self.send_error(502)
            return
        value = (urllib.parse.parse_qs(parsed.query).get("filename") or [""])[0]
        filename = value
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
            if decoded in self.media:
                filename = decoded
        except (ValueError, UnicodeDecodeError):
            pass
        payload = self.media.get(filename)
        if payload is None:
            self.send_error(404)
            return
        requested = self.headers.get("Range", "").strip()
        if requested:
            match = __import__("re").fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416); self.send_header("Content-Range", f"bytes */{len(payload)}"); self.end_headers(); return
            if match.group(1):
                start = int(match.group(1)); end = int(match.group(2) or len(payload) - 1)
            else:
                suffix = int(match.group(2)); start = max(0, len(payload) - suffix); end = len(payload) - 1
            end = min(end, len(payload) - 1)
            if start > end or start >= len(payload):
                self.send_response(416); self.send_header("Content-Range", f"bytes */{len(payload)}"); self.end_headers(); return
            body = payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "image/png" if filename.casefold().endswith(".png") else "video/x-matroska")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if type(self).disconnect_once:
            type(self).disconnect_once = False
            self.wfile.write(payload[:5]); self.wfile.flush(); self.close_connection = True
            self.connection.close(); return
        self.wfile.write(payload)


class AniRssTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
              CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,episode_count INTEGER);
              CREATE TABLE anime_title(anime_id INTEGER,normalized_title TEXT);
            """)
            db.execute("INSERT INTO anime_work VALUES(1,123,'作品','作品','Work','2026-07',12)")
            db.execute("INSERT INTO anime_title VALUES(1,'作品')")
        FakeAniRss.subscriptions = []; FakeAniRss.seen_keys = []; FakeAniRss.delete_paths = []; FakeAniRss.disconnect_once = False; FakeAniRss.fail_file_requests = 0; FakeAniRss.file_requests = 0; FakeAniRss.list_total_override = None
        with ani_rss._COVER_FAILURE_LOCK:
            ani_rss._COVER_FAILURES.clear()
        FakeAniRss.media = {
            "/remote/Work - 01.mkv": b"0123456789abcdef",
            "/remote/Work - 02.mkv": b"ABCDEFGHIJKLMNOP",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAniRss)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.config = {"components": {"aniRss": {"endpoint": f"http://127.0.0.1:{self.server.server_port}",
                                                     "mode": "prefer", "mediaPath": "/Media",
                                                     "syncMinutes": 15, "deleteGraceSyncs": 2}},
                       "torrentPolicy": {"allowUnlisted": {"resourceGroup": True}, "resourceGroups": [],
                                         "contentClasses": {}, "resolutions": {}, "subtitles": {}}}
        self.old_key = os.environ.get("ANM_ANI_RSS_API_KEY"); os.environ["ANM_ANI_RSS_API_KEY"] = "secret-test"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
        if self.old_key is None: os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        else: os.environ["ANM_ANI_RSS_API_KEY"] = self.old_key

    def test_probe_can_use_transient_key_without_persisting_it(self):
        os.environ["ANM_ANI_RSS_API_KEY"] = "saved-key"
        FakeAniRss.seen_keys = []
        self.assertTrue(ani_rss.probe(self.config, "temporary-key")["authenticated"])
        self.assertEqual("saved-key", os.environ["ANM_ANI_RSS_API_KEY"])
        self.assertTrue(FakeAniRss.seen_keys)
        self.assertTrue(all(key == "temporary-key" for key in FakeAniRss.seen_keys))

    def test_probe_search_subscribe_and_deletion_grace(self):
        self.assertTrue(ani_rss.probe(self.config)["authenticated"])
        result = ani_rss.search(self.db_path, 1, self.config)
        self.assertEqual(result["found"], 1)
        resource = ani_rss.resources(self.db_path, 1)[0]
        submitted = ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
        self.assertEqual(submitted["state"], "submitted")
        self.assertTrue(all(key == "secret-test" for key in FakeAniRss.seen_keys))
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual(len(ani_rss.subscriptions_for_anime(self.db_path, 1)), 1)
        FakeAniRss.subscriptions = []
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual(len(ani_rss.subscriptions_for_anime(self.db_path, 1)), 1)
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual(len(ani_rss.subscriptions_for_anime(self.db_path, 1)), 0)
        resubmitted = ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
        self.assertFalse(resubmitted["idempotent"])
        self.assertEqual(len(FakeAniRss.subscriptions), 1)

    def test_sync_updates_future_episodes_and_keeps_duplicate_subscriptions_separate(self):
        FakeAniRss.subscriptions = [
            {"id": "remote-a", "title": "作品 A", "bgmUrl": "https://bgm.tv/subject/123", "enable": True, "currentEpisodeNumber": 8, "totalEpisodeNumber": 12},
            {"id": "remote-b", "title": "作品 B", "bgmUrl": "https://bgm.tv/subject/123", "enable": True, "currentEpisodeNumber": 8, "totalEpisodeNumber": 12},
        ]
        ani_rss.sync(self.db_path, self.config)
        rows = ani_rss.subscriptions_for_anime(self.db_path, 1)
        self.assertEqual({"remote-a", "remote-b"}, {row["remoteId"] for row in rows})
        FakeAniRss.subscriptions[0]["currentEpisodeNumber"] = 9
        ani_rss.sync(self.db_path, self.config)
        updated = {row["remoteId"]: row for row in ani_rss.subscriptions_for_anime(self.db_path, 1)}
        self.assertEqual(9, updated["remote-a"]["currentEpisode"])
        self.assertEqual(8, updated["remote-b"]["currentEpisode"])

    def test_periodic_sync_materializes_and_refreshes_playable_media(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        FakeAniRss.media = {
            "/remote/Work E01.mkv": b"1" * 16,
            "/remote/Work E02.mkv": b"2" * 16,
        }
        first = ani_rss.sync(self.db_path, self.config)
        self.assertEqual(2, first["mediaItems"])
        self.assertEqual([1.0, 2.0], [item.episode for item in ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")])
        first_row = ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        self.assertEqual(2, first_row["playableCount"])
        self.assertEqual([1.0, 2.0], first_row["playableEpisodes"])

        FakeAniRss.media["/remote/Work E03.mkv"] = b"3" * 16
        FakeAniRss.subscriptions[0]["currentEpisodeNumber"] = 3
        second = ani_rss.sync(self.db_path, self.config)
        self.assertEqual(3, second["mediaItems"])
        self.assertEqual([1.0, 2.0, 3.0], [item.episode for item in ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")])
        row = ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        self.assertEqual(3, row["currentEpisode"])
        self.assertEqual(3, row["playableCount"])
        self.assertEqual([1.0, 2.0, 3.0], row["playableEpisodes"])

    def test_configured_sync_interval_refreshes_new_playable_episode_on_due_tick(self):
        self.config["components"]["aniRss"]["syncMinutes"] = 30
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        FakeAniRss.media = {
            "/remote/Work E01.mkv": b"1" * 16,
            "/remote/Work E02.mkv": b"2" * 16,
        }
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            success = dt.datetime.fromisoformat(db.execute(
                "SELECT last_success_at FROM ani_rss_state WHERE singleton=1").fetchone()[0])
        self.assertFalse(ani_rss.sync_due(self.db_path, self.config, now=success + dt.timedelta(minutes=29)))
        self.assertTrue(ani_rss.sync_due(self.db_path, self.config, now=success + dt.timedelta(minutes=30)))

        FakeAniRss.media["/remote/Work E03.mkv"] = b"3" * 16
        FakeAniRss.subscriptions[0]["currentEpisodeNumber"] = 3
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        self.assertEqual([1.0, 2.0, 3.0], [
            item.episode for item in ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")])

    def test_media_refresh_retains_previous_url_when_listani_temporarily_omits_it(self):
        subscription = {
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }
        FakeAniRss.subscriptions = [subscription]
        FakeAniRss.media = {
            "/remote/Work E01.mkv": b"1" * 16,
            "/remote/Work E02.mkv": b"2" * 16,
        }
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])

        # A temporarily incomplete listAni response must not discard the last
        # verified subscription URL; playList still needs that URL to refresh.
        subscription.pop("url")
        subscription["currentEpisodeNumber"] = 3
        FakeAniRss.media["/remote/Work E03.mkv"] = b"3" * 16
        second = ani_rss.sync(self.db_path, self.config)
        self.assertTrue(second["snapshotComplete"])
        self.assertEqual([1.0, 2.0, 3.0], [
            item.episode for item in ani_rss.playback_items(
                self.db_path, 1, self.config, "remote-a")])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            evidence = json.loads(db.execute(
                "SELECT evidence_json FROM ani_rss_subscription WHERE remote_id='remote-a'").fetchone()[0])
        self.assertEqual("https://mikan.test/RSS/Bangumi?bangumiId=123", evidence["url"])

    def test_truncated_listani_never_ages_unseen_subscription_toward_deletion(self):
        FakeAniRss.subscriptions = [
            {"id": "remote-a", "title": "作品 A", "bgmUrl": "https://bgm.tv/subject/123",
             "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True},
            {"id": "remote-b", "title": "作品 B", "bgmUrl": "https://bgm.tv/subject/123",
             "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True},
        ]
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])

        # Simulate a structurally valid but truncated page: listAni still says
        # there are two subscriptions while only one row is returned.
        FakeAniRss.subscriptions = [FakeAniRss.subscriptions[0]]
        FakeAniRss.list_total_override = 2
        for _ in range(2):
            result = ani_rss.sync(self.db_path, self.config)
            self.assertFalse(result["listingComplete"])
            self.assertFalse(result["snapshotComplete"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT remote_state,missed_successful_syncs,deleted_at FROM ani_rss_subscription WHERE remote_id='remote-b'").fetchone()
        self.assertEqual(("enabled", 0, None), row)

        # Once listAni again proves a complete one-row snapshot, the normal
        # deletion grace resumes and eventually confirms the remote deletion.
        FakeAniRss.list_total_override = None
        ani_rss.sync(self.db_path, self.config)
        ani_rss.sync(self.db_path, self.config)
        self.assertNotIn("remote-b", {item["remoteId"] for item in ani_rss.subscriptions_for_anime(self.db_path, 1)})

    def test_partial_listani_preserves_subscription_state_and_media_path(self):
        subscription = {
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 7, "totalEpisodeNumber": 12, "downloadPath": "/media/Work",
            "score": 8.5, "completed": False,
        }
        FakeAniRss.subscriptions = [subscription]
        ani_rss.sync(self.db_path, self.config)
        for key in ("enable", "currentEpisodeNumber", "totalEpisodeNumber", "downloadPath", "score", "completed"):
            subscription.pop(key, None)
        ani_rss.sync(self.db_path, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM ani_rss_subscription WHERE remote_id='remote-a'").fetchone()
            evidence = json.loads(row["evidence_json"])
        self.assertEqual(1, row["enabled"])
        self.assertGreaterEqual(int(row["current_episode"]), 7)
        self.assertEqual(12, row["total_episode"])
        self.assertEqual("/media/Work", row["remote_media_path"])
        self.assertEqual(8.5, evidence["score"])
        self.assertFalse(evidence["completed"])

    def test_image_worker_network_snapshot_tracks_current_ani_rss_credential(self):
        os.environ["ANM_ANI_RSS_API_KEY"] = "first-key"
        first = service.image_network_config(self.config)
        os.environ["ANM_ANI_RSS_API_KEY"] = "second-key"
        second = service.image_network_config(self.config)
        os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        removed = service.image_network_config(self.config)
        self.assertEqual("first-key", first["_aniRssApiKey"])
        self.assertEqual("second-key", second["_aniRssApiKey"])
        self.assertEqual("", removed["_aniRssApiKey"])
        self.assertEqual(self.config["components"]["aniRss"]["endpoint"],
                         second["_aniRssConfig"]["components"]["aniRss"]["endpoint"])

    def test_periodic_sync_refreshes_media_for_disabled_subscription(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-disabled", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": False,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        FakeAniRss.media = {
            "/remote/Work E01.mkv": b"1" * 16,
            "/remote/Work E02.mkv": b"2" * 16,
        }
        first = ani_rss.sync(self.db_path, self.config)
        self.assertTrue(first["snapshotComplete"])
        self.assertEqual([1.0, 2.0], [
            item.episode for item in ani_rss.playback_items(
                self.db_path, 1, self.config, "remote-disabled")])

        FakeAniRss.media["/remote/Work E03.mkv"] = b"3" * 16
        second = ani_rss.sync(self.db_path, self.config)
        self.assertTrue(second["snapshotComplete"])
        self.assertEqual([1.0, 2.0, 3.0], [
            item.episode for item in ani_rss.playback_items(
                self.db_path, 1, self.config, "remote-disabled")])

    def test_partial_media_snapshot_retries_before_full_sync_interval(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        with mock.patch.object(ani_rss.Client, "play_list", side_effect=RuntimeError("temporary")):
            failed = ani_rss.sync(self.db_path, self.config)
        self.assertFalse(failed["snapshotComplete"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            attempt = dt.datetime.fromisoformat(db.execute(
                "SELECT last_attempt_at FROM ani_rss_state WHERE singleton=1").fetchone()[0])
        self.assertFalse(ani_rss.sync_due(self.db_path, self.config, now=attempt + dt.timedelta(minutes=4)))
        self.assertTrue(ani_rss.sync_due(self.db_path, self.config, now=attempt + dt.timedelta(minutes=5)))

    def test_background_sync_can_defer_media_for_explicit_user_activity(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        stop = threading.Event(); stop.set()
        with mock.patch.object(ani_rss.Client, "play_list") as playlist:
            result = ani_rss.sync(self.db_path, self.config, abort_event=stop)
        playlist.assert_not_called()
        self.assertEqual(1, result["mediaDeferred"])
        self.assertEqual(0, result["mediaFailures"])

    def test_playlist_refresh_failure_preserves_last_successful_media_snapshot(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        FakeAniRss.media = {
            "/remote/Work E01.mkv": b"1" * 16,
            "/remote/Work E02.mkv": b"2" * 16,
        }
        ani_rss.sync(self.db_path, self.config)
        before = [item.filename for item in ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")]
        with mock.patch.object(ani_rss.Client, "play_list", side_effect=RuntimeError("playlist temporarily unavailable")):
            failed = ani_rss.sync(self.db_path, self.config)
        after = [item.filename for item in ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")]
        self.assertEqual(1, failed["mediaFailures"])
        self.assertEqual(before, after)

    def test_anirss_cover_is_copied_before_external_image_fallback_and_not_replaced_implicitly(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 40, 60)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""CREATE TABLE anime_image(
                anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,source_url TEXT,
                etag TEXT,fetched_at TEXT,error TEXT)""")
        ani_rss.sync(self.db_path, self.config)
        first = service.get_anime_image(self.db_path, 1, network={
            "bangumiSubjectCacheEndpoints": [], "bangumiApiEndpoints": [], "bangumiImageEndpoints": []
        }, log_timing=False)
        self.assertIsNotNone(first)
        self.assertEqual("image/webp", first[1])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute("SELECT image_blob,source_url FROM anime_image WHERE anime_id=1").fetchone()
        self.assertEqual(first[0], row[0])
        self.assertTrue(str(row[1]).startswith("ani-rss://remote-a/cover"))

        changed = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 100, 50)).save(changed, "PNG")
        FakeAniRss.media["/remote/cover.png"] = changed.getvalue()
        unchanged = service.get_anime_image(self.db_path, 1, refresh=False, network={}, log_timing=False)
        self.assertEqual(first, unchanged)
        refreshed = service.get_anime_image(self.db_path, 1, refresh=True, network={}, log_timing=False)
        self.assertNotEqual(first[0], refreshed[0])
        self.assertEqual("image/webp", refreshed[1])

    def test_sync_due_skips_cleanly_without_ani_rss_credential(self):
        os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        self.assertFalse(ani_rss.sync_due(self.db_path, self.config))
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1))

    def test_removed_credential_disables_previous_ready_state_and_cached_cover(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 40, 60)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        self.assertIsNotNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        state = ani_rss.state(self.db_path, self.config)
        self.assertEqual("unconfigured", state["connection_state"])
        self.assertEqual("manual", state["effective_mode"])
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))

    def test_changed_credential_invalidates_ready_snapshot_until_resync(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 40, 60)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        ani_rss.search(self.db_path, 1, self.config)
        self.assertEqual("ready", ani_rss.state(self.db_path, self.config)["connection_state"])
        self.assertIsNotNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertTrue(ani_rss.resources(self.db_path, 1, self.config))

        os.environ["ANM_ANI_RSS_API_KEY"] = "rotated-key"
        state = ani_rss.state(self.db_path, self.config)
        self.assertEqual("unknown", state["connection_state"])
        self.assertEqual("manual", state["effective_mode"])
        self.assertTrue(ani_rss.sync_due(self.db_path, self.config))
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertEqual([], ani_rss.resources(self.db_path, 1, self.config))
        with self.assertRaisesRegex(ValueError, "playback source is unavailable"):
            ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")

        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        self.assertEqual("ready", ani_rss.state(self.db_path, self.config)["connection_state"])

    def test_removed_credential_disables_cached_api_playback_snapshot(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual(2, len(ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")))
        os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        with self.assertRaisesRegex(ValueError, "playback source is unavailable"):
            ani_rss.playback_items(self.db_path, 1, self.config, "remote-a")

    def test_cached_cover_rejects_stale_endpoint_and_local_endpoint_bypasses_proxy(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 40, 60)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        with mock.patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1"}, clear=False):
            self.assertIsNotNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        changed = json.loads(json.dumps(self.config))
        changed["components"]["aniRss"]["endpoint"] = "http://127.0.0.1:1"
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, changed))
        changed_mode = json.loads(json.dumps(self.config))
        changed_mode["components"]["aniRss"]["mode"] = "manual"
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, changed_mode))

    def test_cached_cover_uses_bounded_fail_fast_file_request(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), (20, 40, 60)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        calls = []

        def fail_fast(client, filename, *, limit=12 * 1024 * 1024, retries=3):
            calls.append((client.timeout, filename, retries))
            raise ani_rss.RemoteFileError(502)

        with mock.patch.object(ani_rss.Client, "file_bytes", autospec=True, side_effect=fail_fast):
            self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertTrue(calls)
        self.assertLessEqual(calls[0][0], 4)
        self.assertEqual(1, calls[0][2])

    def test_cached_cover_endpoint_failure_short_circuits_queue_and_proxy_change_retries(self):
        from PIL import Image
        output = __import__("io").BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-cover-breaker", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/rss-cover-breaker", "cover": "/remote/cover.png", "enable": True,
            "currentEpisodeNumber": 1, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        FakeAniRss.fail_file_requests = 1
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        first_requests = FakeAniRss.file_requests
        self.assertGreater(first_requests, 0)
        # A queue of image refreshes must not each wait on the same broken file API.
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertEqual(first_requests, FakeAniRss.file_requests)
        # A live proxy/network revision invalidates the short circuit immediately.
        with mock.patch.object(ani_rss.network_transport, "proxy_revision", return_value=999):
            self.assertIsNotNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertGreater(FakeAniRss.file_requests, first_requests)

    def test_cached_cover_rejects_non_image_file_even_when_file_api_returns_success(self):
        FakeAniRss.media["/remote/cover.png"] = b"this is not an image"
        FakeAniRss.subscriptions = [{
            "id": "remote-invalid-cover", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/rss-invalid-cover", "cover": "/remote/cover.png", "enable": True,
            "currentEpisodeNumber": 1, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))

    def test_sync_due_recovers_from_future_wall_clock_timestamp(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.migrate(db)
            endpoint = self.config["components"]["aniRss"]["endpoint"]
            fingerprint = ani_rss._credential_fingerprint("secret-test", endpoint)
            db.execute("""INSERT INTO ani_rss_state VALUES(1,?,?,?,?,'prefer',?,?,NULL,1,?)
                ON CONFLICT(singleton) DO UPDATE SET endpoint=excluded.endpoint,configured_mode=excluded.configured_mode,
                connection_state='ready',effective_mode='prefer',last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,last_error=NULL,credential_fingerprint=excluded.credential_fingerprint""",
                (endpoint, "test", "ready", "prefer", future, future, fingerprint))
        self.assertTrue(ani_rss.sync_due(self.db_path, self.config, now=dt.datetime.now(dt.timezone.utc)))

    def test_explicit_delete_is_confirmed_and_immediately_removed_locally(self):
        FakeAniRss.subscriptions = [
            {"id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123", "enable": True, "currentEpisodeNumber": 8, "totalEpisodeNumber": 12},
        ]
        ani_rss.sync(self.db_path, self.config)
        result = ani_rss.delete_subscription(self.db_path, "remote-a", self.config, delete_files=True)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["deleteFiles"])
        self.assertEqual([], FakeAniRss.subscriptions)
        self.assertIn("deleteFiles=true", FakeAniRss.delete_paths[-1])
        self.assertEqual([], ani_rss.subscriptions_for_anime(self.db_path, 1))

    def test_default_sync_interval_is_thirty_minutes(self):
        self.assertEqual(30, ani_rss._settings({})["syncMinutes"])

    def test_invalid_connection_forces_manual_mode(self):
        os.environ.pop("ANM_ANI_RSS_API_KEY", None)
        result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual(result["effectiveMode"], "manual")
        self.assertEqual(ani_rss.state(self.db_path, self.config)["effective_mode"], "manual")

    def test_explicit_ani_rss_plan_does_not_require_qbittorrent(self):
        ani_rss.sync(self.db_path, self.config)
        ani_rss.search(self.db_path, 1, self.config)
        resource = ani_rss.resources(self.db_path, 1)[0]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            runtime_catalog.migrate_overlay(db)
        local, remote = ani_rss.partition_plan(self.db_path, {
            "animeIds": [1], "resourceSelections": {"1": resource["resourceId"]},
        }, self.config)
        self.assertEqual([], local["animeIds"])
        self.assertEqual(resource["resourceId"], remote[0]["resourceId"])
        plan = ani_rss.attach_plan(self.db_path, None, {"animeIds": [1]}, remote)
        self.assertEqual(1, plan["taskCount"])
        self.assertEqual("ani-rss", plan["aniRssJobs"][0]["provider"])

    def test_plan_routing_modes_force_ani_rss_or_skip_without_local_torrent(self):
        ani_rss.sync(self.db_path, self.config)
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            runtime_catalog.migrate_overlay(db)
        local, remote = ani_rss.partition_plan(self.db_path, {
            "animeIds": [1], "routingMode": "ani-rss",
        }, self.config)
        self.assertEqual([], local["animeIds"])
        self.assertEqual(1, len(remote))
        self.assertEqual([], local["_skippedWorks"])
        local, remote = ani_rss.partition_plan(self.db_path, {
            "animeIds": [1], "routingMode": "torrent",
        }, self.config)
        self.assertEqual([], local["animeIds"])
        self.assertEqual([], remote)
        self.assertEqual(1, len(local["_skippedWorks"]))

    def test_ani_rss_plan_skips_remote_requests_after_credential_change(self):
        ani_rss.sync(self.db_path, self.config)
        ani_rss.search(self.db_path, 1, self.config)
        os.environ["ANM_ANI_RSS_API_KEY"] = "rotated-key"
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            runtime_catalog.migrate_overlay(db)
        with mock.patch.object(ani_rss, "search") as remote_search:
            local, remote = ani_rss.partition_plan(self.db_path, {
                "animeIds": [1], "routingMode": "ani-rss",
            }, self.config)
        remote_search.assert_not_called()
        self.assertEqual([], remote)
        self.assertEqual([], local["animeIds"])
        self.assertEqual(1, len(local["_skippedWorks"]))

    def test_background_search_window_and_refresh_due_are_rolling_24_months(self):
        today = dt.date(2026, 9, 3)
        self.assertTrue(ani_rss.automatic_search_eligible("2024-10", today=today))
        self.assertTrue(ani_rss.automatic_search_eligible("2026-09", today=today))
        self.assertFalse(ani_rss.automatic_search_eligible("2024-09", today=today))
        self.assertFalse(ani_rss.automatic_search_eligible("2026-10", today=today))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET start_month='2024-10' WHERE id=1")
        now = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)
        self.config.setdefault("components", {}).setdefault("discovery", {})["pollMinutes"] = 30
        self.assertTrue(ani_rss.background_search_due(self.db_path, 1, self.config, now=now))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.migrate(db)
            db.execute("INSERT OR REPLACE INTO ani_rss_search_state VALUES(?,?,?,?,?)",
                       (1, (now - dt.timedelta(minutes=10)).isoformat(), now.isoformat(), 1, None))
        self.assertFalse(ani_rss.background_search_due(self.db_path, 1, self.config, now=now))
        later = now + dt.timedelta(minutes=21)
        self.assertTrue(ani_rss.background_search_due(self.db_path, 1, self.config, now=later))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET start_month='2024-09' WHERE id=1")
        self.assertFalse(ani_rss.background_search_due(self.db_path, 1, self.config, now=later))

    def test_expected_session_probe_and_send_error_detail_do_not_duplicate_console_noise(self):
        store = ConfigStore(Path(self.tmp.name) / "log-config.json", service.EXAMPLE_CONFIG)
        handler = service.make_handler(self.db_path, store, submission_enabled=False, start_warmup=False)

        class RequestStub:
            path = "/api/auth/session"

            @staticmethod
            def address_string():
                return "127.0.0.1"

        stub = RequestStub()
        with mock.patch("builtins.print") as emitted:
            handler.log_message(stub, '"%s" %s %s', "GET /api/auth/session HTTP/1.1", 401, "-")
        emitted.assert_not_called()

        stub.path = "/api/auth/login"
        with mock.patch("builtins.print") as emitted:
            handler.log_message(stub, '"%s" %s %s', "POST /api/auth/login HTTP/1.1", 401, "-")
        emitted.assert_called_once()

        stub.path = "/api/playback/media/example"
        with mock.patch("builtins.print") as emitted:
            handler.log_message(stub, "code %d, message %s", 502, "Ani-RSS media unavailable")
        emitted.assert_not_called()

    def test_periodic_resource_refresh_starts_after_first_healthy_sync(self):
        # Before the first successful connection the optional resource pass is
        # a no-op; once sync establishes ready state it runs without requiring
        # a second process start or image warm-up restart.
        before = ani_rss.refresh_background_resources(self.db_path, self.config)
        self.assertFalse(before["started"])
        self.assertEqual("unavailable", before["reason"])

        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        first = ani_rss.refresh_background_resources(self.db_path, self.config)
        self.assertTrue(first["started"])
        self.assertEqual(1, first["refreshed"])
        self.assertEqual(1, len(ani_rss.resources(self.db_path, 1, self.config)))

        second = ani_rss.refresh_background_resources(self.db_path, self.config)
        self.assertTrue(second["started"])
        self.assertEqual(0, second["refreshed"])

    def test_background_resource_scan_lease_skips_overlapping_pass(self):
        with ani_rss.background_resource_scan_lease() as first:
            self.assertTrue(first)
            with ani_rss.background_resource_scan_lease() as overlapping:
                self.assertFalse(overlapping)
        with ani_rss.background_resource_scan_lease() as after_completion:
            self.assertTrue(after_completion)

    def test_background_search_order_prioritizes_recent_six_months_then_walks_backward(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?)", [
                (2, 124, 'A', 'A', 'A', '2026-04', 12),
                (3, 125, 'B', 'B', 'B', '2026-03', 12),
                (4, 126, 'C', 'C', 'C', '2025-10', 12),
                (5, 127, 'D', 'D', 'D', '2024-09', 12),
            ])
        ordered = ani_rss.automatic_search_ids(self.db_path, today=dt.date(2026, 9, 3))
        self.assertEqual({1, 2}, set(ordered[:2]))
        self.assertLess(ordered.index(3), ordered.index(4))
        self.assertNotIn(5, ordered)

    def test_remote_playback_without_media_mount_supports_range_queue_and_resume(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 2, "totalEpisodeNumber": 2,
        }]
        ani_rss.sync(self.db_path, self.config)
        store = ConfigStore(Path(self.tmp.name) / "config.json", service.EXAMPLE_CONFIG)
        current = store.read()
        current.setdefault("components", {}).setdefault("aniRss", {}).update({
            "endpoint": self.config["components"]["aniRss"]["endpoint"],
            "mode": "prefer", "mediaPath": "",
        })
        current.setdefault("playback", {})["enabled"] = True
        store.write(current)
        old_auth = os.environ.get("ANM_AUTH_ENABLED")
        old_auth_db = os.environ.get("ANM_AUTH_DB")
        os.environ["ANM_AUTH_ENABLED"] = "false"
        os.environ["ANM_AUTH_DB"] = str(Path(self.tmp.name) / "auth.sqlite3")
        handler = service.make_handler(self.db_path, store, submission_enabled=False, start_warmup=False)
        web = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=web.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{web.server_port}"
        try:
            source = urllib.parse.quote("ani-rss:remote-a", safe="")
            with urllib.request.urlopen(f"{base}/api/anime/1/playlist.m3u?source={source}", timeout=5) as response:
                playlist = response.read().decode("utf-8")
            media_urls = [line for line in playlist.splitlines() if line.startswith("http://")]
            self.assertEqual(2, len(media_urls))

            # A media token issued while Ani-RSS was healthy must stop making
            # upstream requests immediately if the credential is removed.
            # This keeps a stale player request from hanging on an unavailable
            # optional integration.
            file_requests = FakeAniRss.file_requests
            os.environ.pop("ANM_ANI_RSS_API_KEY", None)
            with self.assertRaises(urllib.error.HTTPError) as unavailable:
                urllib.request.urlopen(media_urls[0], timeout=5)
            self.assertEqual(404, unavailable.exception.code)
            self.assertEqual(file_requests, FakeAniRss.file_requests)
            os.environ["ANM_ANI_RSS_API_KEY"] = "secret-test"

            head = urllib.request.Request(media_urls[0], method="HEAD")
            with urllib.request.urlopen(head, timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertEqual("bytes", response.headers["Accept-Ranges"])
                self.assertEqual(16, int(response.headers["Content-Length"]))

            ranged = urllib.request.Request(media_urls[0], headers={"Range": "bytes=4-7"})
            with urllib.request.urlopen(ranged, timeout=5) as response:
                self.assertEqual(206, response.status)
                self.assertEqual("bytes 4-7/16", response.headers["Content-Range"])
                self.assertEqual(b"4567", response.read())

            # PotPlayer-style speculative requests should not surface a transient
            # Ani-RSS 502 when the immediately retried media stream is healthy.
            FakeAniRss.fail_file_requests = 1
            with urllib.request.urlopen(media_urls[0], timeout=5) as response:
                self.assertEqual(b"0123456789abcdef", response.read())

            FakeAniRss.disconnect_once = True
            with urllib.request.urlopen(media_urls[0], timeout=5) as response:
                self.assertEqual(b"0123456789abcdef", response.read())
            with urllib.request.urlopen(media_urls[1], timeout=5) as response:
                self.assertEqual(b"ABCDEFGHIJKLMNOP", response.read())
        finally:
            web.shutdown(); web.server_close(); thread.join(2)
            if old_auth is None: os.environ.pop("ANM_AUTH_ENABLED", None)
            else: os.environ["ANM_AUTH_ENABLED"] = old_auth
            if old_auth_db is None: os.environ.pop("ANM_AUTH_DB", None)
            else: os.environ["ANM_AUTH_DB"] = old_auth_db


if __name__ == "__main__":
    unittest.main()

