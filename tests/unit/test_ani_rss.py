from __future__ import annotations

import base64
import json
import io
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
                     "items": self.resource_items if self.resource_items is not None else
                     [{"title": "[SubsPlease] Work - 01 (1080p) [WEB-DL]", "size": 100}]}]
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
        FakeAniRss.subscriptions = []; FakeAniRss.seen_keys = []; FakeAniRss.delete_paths = []; FakeAniRss.disconnect_once = False; FakeAniRss.fail_file_requests = 0; FakeAniRss.file_requests = 0; FakeAniRss.list_total_override = None; FakeAniRss.resource_items = None
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

    def test_movie_search_and_cached_upgrade_keep_earliest_publication_without_episode_numbers(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("ALTER TABLE anime_work ADD COLUMN media_code TEXT")
            db.execute("ALTER TABLE anime_work ADD COLUMN raw_date TEXT")
            db.execute("UPDATE anime_work SET media_code='movie',start_month='2026-01',raw_date='2026-01-15'")
        FakeAniRss.resource_items = [
            {"title": "Movie BDRip (1080p)", "createdAt": "2026-07-01T00:30:00+08:00", "size": 100},
            {"title": "Movie BDRip (1080p)", "createdAt": "2026-08-02T00:30:00+08:00", "size": 100},
        ]
        ani_rss.sync(self.db_path, self.config)
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            expected = (1, "2026-06-30T16:30:00+00:00", "2026-08-01T16:30:00+00:00")
            self.assertEqual(expected, db.execute("SELECT * FROM ani_rss_movie_release_state").fetchone())
            self.assertIsNone(db.execute("SELECT * FROM ani_rss_release_state").fetchone())
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM ani_rss_movie_release_state")
        ani_rss.reconcile_cached_resources(self.db_path)
        ani_rss.reconcile_cached_resources(self.db_path)
        FakeAniRss.resource_items = [FakeAniRss.resource_items[1]]
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(expected, db.execute("SELECT * FROM ani_rss_movie_release_state").fetchone())
        self.config["components"]["aniRss"]["endpoint"] = "http://127.0.0.1:1"
        ani_rss.sync(self.db_path, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM ani_rss_movie_release_state").fetchone()[0])

    def test_repeated_groups_and_shared_urls_remain_scoped_to_each_work(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO anime_work VALUES(2,456,'作品','作品','Work','2026-07',12)")
        entry = {'title': '[SubsPlease] Work Complete Batch (1080p) [WEB-DL]', 'size': 100,
                 'torrent': 'https://mikan.test/shared.torrent'}
        group = {'label': 'SubsPlease', 'rss': 'https://mikan.test/RSS/Bangumi?subgroupid=1', 'items': [entry, dict(entry)]}
        client = mock.Mock()
        def call(path, **_kwargs):
            return {'weeks': [{'items': [{'url': 'https://mikan.test/Home/Bangumi/1', 'title': '作品'}]}]} if path == 'mikan' else [group, dict(group)]
        client.call.side_effect = call
        with mock.patch.object(ani_rss, '_client', return_value=client):
            first = ani_rss.search(self.db_path, 1, self.config)
            before = ani_rss.resources(self.db_path, 1)
            second = ani_rss.search(self.db_path, 2, self.config)
            repeat = ani_rss.search(self.db_path, 1, self.config)
        self.assertEqual([2, 2, 2], [item['found'] for item in (first, second, repeat)])
        self.assertEqual(before, ani_rss.resources(self.db_path, 1))
        self.assertTrue({item['resourceId'] for item in before}.isdisjoint(
            item['resourceId'] for item in ani_rss.resources(self.db_path, 2)))

    def test_existing_collection_action_stays_idempotent_after_resource_id_upgrade(self):
        FakeAniRss.resource_items = [{'title': '[SubsPlease] Work Complete Batch (1080p) [WEB-DL]', 'size': 100,
                                     'torrent': 'https://mikan.test/synthetic.torrent'}]
        ani_rss.search(self.db_path, 1, self.config)
        resource = next(item for item in ani_rss.resources(self.db_path, 1) if item['kind'] == 'collection')
        import hashlib
        legacy_id = 'ar-' + hashlib.sha256(b'collection\0https://mikan.test/synthetic.torrent').hexdigest()[:24]
        key = hashlib.sha256(f'1\0collection\0{legacy_id}'.encode()).hexdigest()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('INSERT INTO ani_rss_action VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       ('prior-action', key, 1, legacy_id, 'existing', 'collection', 'submitted', ani_rss.utcnow(), ani_rss.utcnow(), None, '{}'))
        with mock.patch.object(ani_rss, '_client') as client:
            result = ani_rss.subscribe(self.db_path, resource['resourceId'], self.config)
        self.assertTrue(result['idempotent'])
        self.assertEqual('prior-action', result['actionId'])
        client.assert_not_called()

    def _follow_waiting_for_media(self):
        FakeAniRss.media = {}
        ani_rss.search(self.db_path, 1, self.config)
        result = ani_rss.subscribe(self.db_path, ani_rss.resources(self.db_path, 1)[0]["resourceId"], self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            probe = json.loads(db.execute("SELECT evidence_json FROM ani_rss_action WHERE action_id=?", (result["actionId"],)).fetchone()[0])["mediaProbe"]
        return result, dt.datetime.fromisoformat(probe["nextAt"])

    def test_different_subtitle_rss_reuses_same_work_and_season(self):
        ani_rss.search(self.db_path, 1, self.config)
        resource = ani_rss.resources(self.db_path, 1)[0]
        client = mock.Mock()
        client.call.return_value = {"id": "new-id", "url": "https://mikan.test/new-rss", "bgmUrl": "https://bgm.tv/subject/123", "season": 2}
        client.subscriptions.return_value = [
            {"id": "prior-season", "url": "https://mikan.test/prior", "bgmUrl": "https://bgm.tv/subject/123", "season": 1},
            {"id": "existing-id", "url": "https://mikan.test/other-rss", "bgmUrl": "https://bgm.tv/subject/123", "season": 2}]
        with mock.patch.object(ani_rss, "_client", return_value=client), mock.patch.object(ani_rss, "sync"):
            result = ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
        self.assertEqual(result["remoteId"], "existing-id")
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["rssToAni"])

    def test_collection_resolves_destination_and_remains_idempotent_without_subscription(self):
        FakeAniRss.resource_items = [{"title": "[SubsPlease] Work Complete Batch (1080p) [WEB-DL]", "size": 100,
                                      "torrent": "https://mikan.test/synthetic.torrent"}]
        ani_rss.search(self.db_path, 1, self.config)
        resource = next(row for row in ani_rss.resources(self.db_path, 1) if row["kind"] == "collection")
        client = mock.Mock()
        client.subscriptions.return_value = []
        def call(path, **_kwargs):
            if path == "rssToAni":
                return {"id": "collection-only", "url": "rss", "bgmUrl": "https://bgm.tv/subject/123"}
            if path == "downloadPath":
                return {"downloadPath": "/Library/Work/Season 02"}
            return True
        client.call.side_effect = call
        with mock.patch.object(ani_rss, "_client", return_value=client), mock.patch.object(ani_rss.tls_support, "urlopen", return_value=contextlib.closing(io.BytesIO(b"synthetic torrent"))):
            first = ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
            second = ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        body = next(call.kwargs["body"] for call in client.call.call_args_list if call.args[0] == "startCollection")
        self.assertEqual(body["ani"]["customDownloadPathTemplate"], "/Library/Work/Season 02")
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["rssToAni", "downloadPath", "startCollection"])

    def test_conversion_to_different_work_cannot_create_subscription(self):
        ani_rss.search(self.db_path, 1, self.config)
        resource = ani_rss.resources(self.db_path, 1)[0]
        client = mock.Mock()
        client.call.return_value = {"id": "wrong", "bgmUrl": "https://bgm.tv/subject/456"}
        with mock.patch.object(ani_rss, "_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "different work"):
                ani_rss.subscribe(self.db_path, resource["resourceId"], self.config)
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["rssToAni"])

    def test_new_follow_probes_only_its_playlist_and_keeps_regular_cadence(self):
        result, due = self._follow_waiting_for_media()
        client = mock.Mock()
        client.play_list.return_value = [{"filename": "/remote/Work - 01.mkv", "name": "Work - 01.mkv", "size": 100}]
        before = ani_rss.state(self.db_path, self.config)
        with mock.patch.object(ani_rss, "_client", return_value=client):
            refreshed = ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due)
            ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due + dt.timedelta(seconds=5))
        self.assertEqual(1, refreshed["refreshed"])
        client.play_list.assert_called_once()
        client.subscriptions.assert_not_called()
        client.call.assert_not_called()
        after = ani_rss.state(self.db_path, self.config)
        self.assertEqual(before["last_success_at"], after["last_success_at"])
        self.assertFalse(ani_rss.sync_due(self.db_path, self.config, now=due))
        self.assertGreater(after["successful_generation"], before["successful_generation"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM ani_rss_media WHERE remote_id=?", (result["remoteId"],)).fetchone()[0])

    def test_new_follow_retry_deadlines_survive_calls_and_eventually_stop(self):
        _, due = self._follow_waiting_for_media()
        client = mock.Mock()
        client.play_list.side_effect = RuntimeError("offline")
        with mock.patch.object(ani_rss, "_client", return_value=client):
            for attempt in range(len(ani_rss._NEW_MEDIA_DELAYS)):
                ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due)
                with contextlib.closing(sqlite3.connect(self.db_path)) as db:
                    probe = json.loads(db.execute("SELECT evidence_json FROM ani_rss_action").fetchone()[0])["mediaProbe"]
                self.assertEqual(attempt + 1, probe["attempts"])
                if probe["state"] == "pending":
                    ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due + dt.timedelta(seconds=1))
                due = dt.datetime.fromisoformat(probe["nextAt"])
            ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due)
        self.assertEqual(len(ani_rss._NEW_MEDIA_DELAYS), client.play_list.call_count)
        self.assertEqual("expired", probe["state"])

    def test_new_follow_probe_does_not_follow_a_changed_credential(self):
        _, due = self._follow_waiting_for_media()
        os.environ["ANM_ANI_RSS_API_KEY"] = "different-key"
        with mock.patch.object(ani_rss, "_client", side_effect=AssertionError("must not use a different provider")):
            self.assertEqual(0, ani_rss.refresh_new_subscription_media(self.db_path, self.config, now=due)["refreshed"])

    def test_http_search_uses_ani_rss_episode_publication_and_length_fields(self):
        published = dt.datetime(2026, 7, 1, 10, tzinfo=dt.timezone.utc)
        FakeAniRss.resource_items = [{"title": "A release with no number in its title", "episode": 1.0,
                                     "length": 123, "pubDate": int(published.timestamp() * 1000)}]
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual((1, published.isoformat()), db.execute(
                "SELECT current_episode,last_episode_update_at FROM ani_rss_release_state WHERE anime_id=1").fetchone())
            self.assertEqual((1, 123), db.execute(
                "SELECT sequence_last,total_bytes FROM ani_rss_resource WHERE anime_id=1").fetchone())

    def test_http_report_backfills_known_publication_without_repromoting_episodes(self):
        published = dt.datetime(2026, 7, 1, 10, tzinfo=dt.timezone.utc)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.migrate(db)
            ani_rss.record_release_progress(db, 1, 1, "2026-07-02T00:00:00Z")
        FakeAniRss.resource_items = [{"title": "No episode in this title", "episode": 1.0,
                                     "pubDate": int(published.timestamp() * 1000)}]
        ani_rss.search(self.db_path, 1, self.config)
        FakeAniRss.resource_items[0]["pubDate"] += 86400000
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual((1, published.isoformat()), db.execute(
                "SELECT current_episode,last_episode_update_at FROM ani_rss_release_state WHERE anime_id=1").fetchone())

    def _sequel_evidence(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
                ALTER TABLE anime_work ADD COLUMN media_code TEXT DEFAULT 'tv';
                ALTER TABLE anime_work ADD COLUMN episode_start_number INTEGER DEFAULT 1;
                CREATE TABLE anime_relation_edge(source_anime_id INTEGER,target_anime_id INTEGER,relation_code TEXT,grouping INTEGER);
                INSERT INTO anime_work(id,title_ja,episode_count) VALUES(2,'First season',14);
                INSERT INTO anime_relation_edge VALUES(2,1,'sequel',1);
            """)

    def test_real_mikan_fields_and_mixed_season_numbering(self):
        self._sequel_evidence()
        FakeAniRss.resource_items = [
            {"title": "[Group] Work S2 [15][1080P][WEB-DL]", "createdAt": "2026-10-01 18:03:00",
             "formatSize": "707.5 MB", "torrent": "https://mikan.test/15.torrent"},
            {"title": "[Group] Work S2 - 01 [1080P][WEB-DL]", "createdAt": "2026-10-01 18:16:00",
             "formatSize": "410.57 MB", "torrent": "https://mikan.test/01.torrent"},
        ]
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual((1, "2026-10-01T10:03:00+00:00"), db.execute(
                "SELECT current_episode,last_episode_update_at FROM ani_rss_release_state WHERE anime_id=1").fetchone())
            self.assertEqual((1, 1, int(707.5 * 1048576) + int(410.57 * 1048576)), db.execute(
                "SELECT sequence_first,sequence_last,total_bytes FROM ani_rss_resource WHERE resource_kind='follow'").fetchone())
            self.assertEqual(12, db.execute("SELECT episode_count FROM anime_work WHERE id=1").fetchone()[0])

    def test_cached_resource_evidence_is_repaired_offline_and_idempotently(self):
        self._sequel_evidence()
        FakeAniRss.resource_items = [{"title": "Work - 15 [1080P]", "createdAt": "2026-10-01 18:03:00",
                                     "formatSize": "707.5 MB"}]
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE ani_rss_resource SET sequence_first=15,sequence_last=15,total_bytes=0")
            db.execute("UPDATE ani_rss_release_state SET current_episode=15,last_episode_update_at=NULL")
        with mock.patch.object(ani_rss, "_client", side_effect=AssertionError("must use cache")):
            repaired = ani_rss.reconcile_cached_resources(self.db_path)
            repeated = ani_rss.reconcile_cached_resources(self.db_path)
        self.assertEqual(1, repaired["updated"])
        self.assertEqual(0, repeated["updated"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual((1, "2026-10-01T10:03:00+00:00"), db.execute(
                "SELECT current_episode,last_episode_update_at FROM ani_rss_release_state").fetchone())

    def test_ambiguous_sequel_chain_does_not_guess_an_episode_offset(self):
        self._sequel_evidence()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO anime_work(id,title_ja,episode_count) VALUES(3,'Other predecessor',14)")
            db.execute("INSERT INTO anime_relation_edge VALUES(3,1,'sequel',1)")
        FakeAniRss.resource_items = [{"title": "Work - 15 [1080P]"}]
        ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(15, db.execute("SELECT current_episode FROM ani_rss_release_state").fetchone()[0])

    def test_formatted_sizes_and_dates_reject_malformed_evidence(self):
        for invalid in (", MB", "NaN", -1, True, "2 XB", "1,2 MB", "1e99"):
            self.assertEqual(0, ani_rss._resource_size({"formatSize": invalid}), invalid)
        self.assertEqual(1024, ani_rss._resource_size({"length": 0, "formatSize": "1 KiB"}))
        self.assertEqual(1234567, ani_rss._resource_size({"formatSize": "1,234,567 B"}))
        self.assertEqual("2026-10-01T10:03:00+00:00", ani_rss._resource_published_at({"createdAt": "2026-10-01T18:03:00+08:00"}))
        self.assertIsNone(ani_rss._resource_published_at({"pubDate": "2026-10-01 18:03:00"}))

    def test_search_records_unsubscribed_new_episode_without_repromoting_repeats(self):
        episode = 1
        def call(path, **kwargs):
            if path == "mikan":
                return {"weeks": [{"items": [{"url": "https://mikan.test/work", "title": "Work"}]}]}
            if path == "mikanGroup":
                return [{"label": "SubsPlease", "rss": "https://mikan.test/feed",
                         "items": [{"title": f"[SubsPlease] Work - {episode:02d} (1080p) [WEB-DL]", "size": 100}]}]
            raise AssertionError(path)
        client = mock.Mock()
        client.call.side_effect = call
        with mock.patch.object(ani_rss, "_client", return_value=client):
            for number, stamp in ((1, "2026-09-01"), (2, "2026-09-02"), (2, "2026-09-03")):
                episode = number
                with mock.patch.object(ani_rss, "utcnow", return_value=stamp + "T00:00:00+00:00"):
                    ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual((2, "2026-09-02T00:00:00+00:00"), db.execute(
                "SELECT current_episode,last_episode_update_at FROM ani_rss_release_state WHERE anime_id=1").fetchone())

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

    def test_new_episode_evidence_requires_episode_advance_not_only_redownload_time(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        first_time = int((now - dt.timedelta(hours=3)).timestamp() * 1000)
        redownload_time = int((now - dt.timedelta(hours=2)).timestamp() * 1000)
        next_episode_time = int((now - dt.timedelta(hours=1)).timestamp() * 1000)
        subscription = {
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "enable": True, "currentEpisodeNumber": 8, "totalEpisodeNumber": 12,
            "lastDownloadTime": first_time,
        }
        FakeAniRss.subscriptions = [subscription]
        ani_rss.sync(self.db_path, self.config)
        baseline = ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        self.assertIsNone(baseline["lastEpisodeUpdateAt"])

        subscription["lastDownloadTime"] = redownload_time
        ani_rss.sync(self.db_path, self.config)
        redownloaded = ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        self.assertNotEqual(baseline["lastDownloadAt"], redownloaded["lastDownloadAt"])
        self.assertIsNone(redownloaded["lastEpisodeUpdateAt"])

        subscription["currentEpisodeNumber"] = 9
        subscription["lastDownloadTime"] = next_episode_time
        ani_rss.sync(self.db_path, self.config)
        advanced = ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        self.assertEqual(9, advanced["currentEpisode"])
        self.assertNotEqual(redownloaded["lastEpisodeUpdateAt"], advanced["lastEpisodeUpdateAt"])
        self.assertEqual(advanced["lastDownloadAt"], advanced["lastEpisodeUpdateAt"])

    def test_episode_counter_and_clock_rollback_do_not_reannounce_old_episodes(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        subscription = {"id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
                        "enable": True, "totalEpisodeNumber": 12}
        FakeAniRss.subscriptions = [subscription]
        def observe(episode, hours):
            subscription.update(currentEpisodeNumber=episode,
                                lastDownloadTime=int((now - dt.timedelta(hours=hours)).timestamp() * 1000))
            ani_rss.sync(self.db_path, self.config)
            return ani_rss.subscriptions_for_anime(self.db_path, 1)[0]
        observe(8, 8)
        advanced = observe(9, 7)
        observe(2, 6)
        restored = observe(9, 5)
        self.assertEqual(advanced["lastEpisodeUpdateAt"], restored["lastEpisodeUpdateAt"])
        observe(9, 9)
        self.assertEqual(advanced["lastEpisodeUpdateAt"], observe(10, 8)["lastEpisodeUpdateAt"])
        self.assertGreater(observe(11, 1)["lastEpisodeUpdateAt"], advanced["lastEpisodeUpdateAt"])

    def test_active_remote_id_remap_starts_fresh_episode_evidence_baseline(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO anime_work VALUES(2,456,'作品二','作品二','Work Two','2026-07',24)")
            db.execute("INSERT INTO anime_title VALUES(2,'作品二')")
        first_time = int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        second_time = int(dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc).timestamp() * 1000)
        subscription = {
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "enable": True, "currentEpisodeNumber": 8, "totalEpisodeNumber": 12,
            "lastDownloadTime": first_time,
        }
        FakeAniRss.subscriptions = [subscription]
        ani_rss.sync(self.db_path, self.config)
        subscription.update({
            "title": "作品二", "bgmUrl": "https://bgm.tv/subject/456",
            "currentEpisodeNumber": 9, "totalEpisodeNumber": 24, "lastDownloadTime": second_time,
        })
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual([], ani_rss.subscriptions_for_anime(self.db_path, 1))
        remapped = ani_rss.subscriptions_for_anime(self.db_path, 2)[0]
        self.assertEqual(9, remapped["currentEpisode"])
        self.assertEqual(24, remapped["totalEpisode"])
        self.assertIsNone(remapped["lastEpisodeUpdateAt"])

    def test_remap_drops_old_media_even_if_new_playlist_cannot_refresh(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO anime_work VALUES(2,456,'作品二','作品二','Work Two','2026-07',24)")
            db.execute("INSERT INTO anime_title VALUES(2,'作品二')")
        subscription = {"id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
                        "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
                        "currentEpisodeNumber": 8}
        FakeAniRss.subscriptions = [subscription]
        FakeAniRss.media = {"/remote/Work E08.mkv": b"synthetic"}
        ani_rss.sync(self.db_path, self.config)
        self.assertEqual(1, ani_rss.subscriptions_for_anime(self.db_path, 1)[0]["playableCount"])
        subscription.update(bgmUrl="https://bgm.tv/subject/456", currentEpisodeNumber=1)
        with mock.patch.object(ani_rss.Client, "play_list", side_effect=OSError("offline")):
            ani_rss.sync(self.db_path, self.config)
        self.assertEqual(0, ani_rss.subscriptions_for_anime(self.db_path, 2)[0]["playableCount"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM ani_rss_media").fetchone()[0])

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

    def test_current_ani_rss_media_path_field_is_synchronized(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-current", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
            "currentEpisodeNumber": 3, "totalEpisodeNumber": 12,
            "customDownloadPathTemplate": "/media/current/Work",
        }]
        ani_rss.sync(self.db_path, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT remote_media_path,evidence_json FROM ani_rss_subscription WHERE remote_id='remote-current'"
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
        self.assertEqual("/media/current/Work", row["remote_media_path"])
        self.assertEqual("/media/current/Work", evidence["customDownloadPathTemplate"])
        self.assertEqual("/media/current/Work", evidence["downloadPath"])

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
        self.assertEqual("image/png", first[1])
        self.assertEqual(output.getvalue(), first[0])
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
        self.assertEqual("image/png", refreshed[1])
        self.assertEqual(changed.getvalue(), refreshed[0])

    def test_successful_sync_releases_stale_no_cover_when_ani_rss_has_cover(self):
        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (8, 8), (30, 60, 90)).save(output, "PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-cover-recovery", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "cover": "/remote/cover.png",
            "enable": True, "currentEpisodeNumber": 2, "totalEpisodeNumber": 12,
        }]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""CREATE TABLE anime_image(
                anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,source_url TEXT,
                etag TEXT,fetched_at TEXT,error TEXT)""")
            db.execute(
                "INSERT INTO anime_image(anime_id,fetched_at,error) VALUES(1,?, 'no_cover')",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            )

        ani_rss.sync(self.db_path, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            fetched_at, error = db.execute(
                "SELECT fetched_at,error FROM anime_image WHERE anime_id=1").fetchone()
        self.assertIsNone(fetched_at)
        self.assertIsNone(error)

        recovered = service.get_anime_image(self.db_path, 1, network={
            "bangumiSubjectCacheEndpoints": [], "bangumiApiEndpoints": [], "bangumiImageEndpoints": []
        }, log_timing=False)
        self.assertIsNotNone(recovered)
        self.assertEqual("image/png", recovered[1])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            source = db.execute("SELECT source_url FROM anime_image WHERE anime_id=1").fetchone()[0]
        self.assertEqual("ani-rss://remote-cover-recovery/cover", source)

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

        resynced = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("ready", resynced["state"])
        self.assertTrue(resynced["resourceRefreshRequired"])
        self.assertEqual("ready", ani_rss.state(self.db_path, self.config)["connection_state"])
        self.assertEqual([], ani_rss.resources(self.db_path, 1))
        self.assertTrue(ani_rss.background_search_due(self.db_path, 1, self.config))

    def test_failed_switch_to_new_endpoint_discards_previous_resource_cache(self):
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        self.assertEqual(1, ani_rss.search(self.db_path, 1, self.config)["found"])
        self.assertTrue(ani_rss.resources(self.db_path, 1))

        changed = json.loads(json.dumps(self.config))
        changed["components"]["aniRss"]["endpoint"] = "http://127.0.0.1:1"
        failed = ani_rss.sync(self.db_path, changed)
        self.assertEqual("error", failed["state"])
        self.assertTrue(failed["resourceRefreshRequired"])
        self.assertEqual([], ani_rss.resources(self.db_path, 1))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM ani_rss_search_state WHERE anime_id=1"
            ).fetchone())

    def test_late_resource_search_from_previous_credential_cannot_repopulate_cache(self):
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        started = threading.Event()
        release = threading.Event()
        outcome: dict[str, object] = {}
        failures: list[BaseException] = []

        class SlowSearchClient:
            def call(self, path, **kwargs):
                if path == "mikan":
                    return {"weeks": [{"items": [{"url": "https://mikan.test/Home/Bangumi/late",
                                                     "title": "作品"}]}]}
                if path == "mikanGroup":
                    started.set()
                    release.wait(5)
                    return [{"label": "LateGroup", "rss": "https://mikan.test/RSS/late",
                             "items": [{"title": "[LateGroup] Work - 01 (1080p) [WEB-DL]",
                                        "size": 100}]}]
                raise AssertionError(path)

        def run_search():
            try:
                outcome.update(ani_rss.search(self.db_path, 1, self.config))
            except BaseException as exc:  # surface thread failures in the test process
                failures.append(exc)

        with mock.patch.object(ani_rss, "_client", return_value=SlowSearchClient()):
            thread = threading.Thread(target=run_search)
            thread.start()
            self.assertTrue(started.wait(5))
            os.environ["ANM_ANI_RSS_API_KEY"] = "rotated-key"
            switched = ani_rss.sync(self.db_path, self.config)
            self.assertTrue(switched["resourceRefreshRequired"])
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertTrue(outcome.get("stale"))
        self.assertEqual([], ani_rss.resources(self.db_path, 1))

    def test_resource_search_discards_results_when_proxy_route_changes_mid_attempt(self):
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        with mock.patch.object(ani_rss, "_route_revision", side_effect=[10, 11]):
            result = ani_rss.search(self.db_path, 1, self.config)
        self.assertTrue(result.get("stale"))
        self.assertEqual([], ani_rss.resources(self.db_path, 1))

    def test_malformed_about_payload_fails_closed(self):
        original_call = ani_rss.Client.call

        def malformed_about(client, path, **kwargs):
            if path == "about":
                return []
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=malformed_about):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        self.assertEqual("RuntimeError", result["errorType"])

    def test_conflicting_subscription_identity_keeps_last_good_mapping(self):
        first = {"id": "same-id", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123", "enable": True}
        FakeAniRss.subscriptions = [first]
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        FakeAniRss.subscriptions.append({**first, "bgmUrl": "https://bgm.tv/subject/456"})
        result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute("SELECT bgm_id,missed_successful_syncs FROM ani_rss_subscription WHERE remote_id='same-id'").fetchone()
        self.assertEqual((123, 0), row)

    def test_malformed_listani_payload_fails_closed_without_attribute_error(self):
        original_call = ani_rss.Client.call

        def malformed_list(client, path, **kwargs):
            if path == "listAni":
                return []
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=malformed_list):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        self.assertEqual("RuntimeError", result["errorType"])
        self.assertEqual("error", ani_rss.state(self.db_path, self.config)["connection_state"])

    def test_malformed_nested_listani_payload_does_not_age_existing_subscription(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
        }]
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        original_call = ani_rss.Client.call

        def malformed_nested(client, path, **kwargs):
            if path == "listAni":
                return {"total": 0, "weekList": [{"items": {"unexpected": "object"}}]}
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=malformed_nested):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT remote_state,missed_successful_syncs,deleted_at FROM ani_rss_subscription WHERE remote_id='remote-a'"
            ).fetchone()
        self.assertEqual(("enabled", 0, None), row)

    def test_missing_listani_week_list_fails_closed_without_aging_existing_subscription(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
        }]
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        original_call = ani_rss.Client.call

        def missing_week_list(client, path, **kwargs):
            if path == "listAni":
                return {"total": 0}
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=missing_week_list):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT remote_state,missed_successful_syncs,deleted_at FROM ani_rss_subscription WHERE remote_id='remote-a'"
            ).fetchone()
        self.assertEqual(("enabled", 0, None), row)

    def test_legacy_listani_without_total_syncs_but_does_not_infer_remote_deletion(self):
        FakeAniRss.subscriptions = [
            {"id": "remote-a", "title": "作品 A", "bgmUrl": "https://bgm.tv/subject/123",
             "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True},
            {"id": "remote-b", "title": "作品 B", "bgmUrl": "https://bgm.tv/subject/123",
             "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True},
        ]
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        original_call = ani_rss.Client.call
        remaining = FakeAniRss.subscriptions[0]

        def legacy_list_without_total(client, path, **kwargs):
            if path == "listAni":
                return {"weekList": [{"items": [remaining]}]}
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=legacy_list_without_total):
            for _ in range(3):
                result = ani_rss.sync(self.db_path, self.config)
                self.assertTrue(result["listingComplete"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT remote_state,missed_successful_syncs,deleted_at FROM ani_rss_subscription WHERE remote_id='remote-b'"
            ).fetchone()
        self.assertEqual(("enabled", 0, None), row)

    def test_malformed_nested_playlist_is_not_coerced_to_empty(self):
        client = ani_rss.Client(self.config["components"]["aniRss"]["endpoint"], "secret-test")
        with mock.patch.object(client, "call", return_value={"items": {"unexpected": "object"}}):
            with self.assertRaisesRegex(RuntimeError, "playList.items"):
                client.play_list("https://mikan.test/RSS/Bangumi?bangumiId=123")

    def test_malformed_listani_total_fails_closed_without_aging_existing_subscription(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
        }]
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
        original_call = ani_rss.Client.call

        def malformed_total(client, path, **kwargs):
            if path == "listAni":
                return {"total": "not-a-number", "weekList": [{"items": []}]}
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=malformed_total):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("error", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT remote_state,missed_successful_syncs,deleted_at FROM ani_rss_subscription WHERE remote_id='remote-a'"
            ).fetchone()
        self.assertEqual(("enabled", 0, None), row)

    def test_null_playlist_is_not_coerced_to_empty_or_allowed_to_erase_media(self):
        FakeAniRss.subscriptions = [{
            "id": "remote-a", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/RSS/Bangumi?bangumiId=123", "enable": True,
        }]
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            before = db.execute("SELECT COUNT(*) FROM ani_rss_media WHERE remote_id='remote-a'").fetchone()[0]
        original_call = ani_rss.Client.call

        def null_playlist(client, path, **kwargs):
            if path == "playList":
                return None
            return original_call(client, path, **kwargs)

        with mock.patch.object(ani_rss.Client, "call", new=null_playlist):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertFalse(result["snapshotComplete"])
        self.assertEqual(1, result["mediaFailures"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            after = db.execute("SELECT COUNT(*) FROM ani_rss_media WHERE remote_id='remote-a'").fetchone()[0]
        self.assertEqual(before, after)

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

    def test_cached_cover_endpoint_failure_ignores_unrelated_proxy_change_for_local_endpoint(self):
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
        # Local/LAN Ani-RSS is always direct. An unrelated proxy toggle must not
        # release its endpoint cooldown and make the image queue retry early.
        with mock.patch.object(ani_rss.network_transport, "proxy_revision", return_value=999):
            self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertEqual(first_requests, FakeAniRss.file_requests)

    def test_remote_cover_cooldown_expires_on_relevant_route_generation_change(self):
        endpoint = "https://ani-rss.example"
        fingerprint = "credential-generation"
        with mock.patch.object(ani_rss.network_transport, "proxy_route", return_value={
                "mode": "environment_proxy", "proxy": "http://proxy.example:8080",
                "revision": 10, "reason": "environment"}):
            ani_rss._cover_endpoint_result(endpoint, fingerprint, healthy=False)
            self.assertFalse(ani_rss._cover_endpoint_available(endpoint, fingerprint))
        with mock.patch.object(ani_rss.network_transport, "proxy_route", return_value={
                "mode": "direct", "proxy": "", "revision": 11, "reason": "fallback"}):
            self.assertTrue(ani_rss._cover_endpoint_available(endpoint, fingerprint))

    def test_cached_cover_failure_does_not_block_rotated_credential_after_resync(self):
        from PIL import Image
        output = __import__("io").BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-cover-credential", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/rss-cover-credential", "cover": "/remote/cover.png", "enable": True,
            "currentEpisodeNumber": 1, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        FakeAniRss.fail_file_requests = 1
        self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        first_requests = FakeAniRss.file_requests

        os.environ["ANM_ANI_RSS_API_KEY"] = "rotated-key"
        self.assertEqual("ready", ani_rss.sync(self.db_path, self.config)["state"])
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
        with mock.patch.object(ani_rss, "_cover_endpoint_result") as endpoint_result:
            self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        endpoint_result.assert_not_called()

    def test_route_change_gates_ready_snapshot_until_revalidated(self):
        from PIL import Image
        output = __import__("io").BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
        FakeAniRss.media["/remote/cover.png"] = output.getvalue()
        FakeAniRss.subscriptions = [{
            "id": "remote-route-gate", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
            "url": "https://mikan.test/rss-route-gate", "cover": "/remote/cover.png", "enable": True,
            "currentEpisodeNumber": 1, "totalEpisodeNumber": 12,
        }]
        ani_rss.sync(self.db_path, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('ani_rss_route_revision','10')")
        before_files = FakeAniRss.file_requests
        with mock.patch.object(ani_rss, "_route_revision", return_value=11):
            current = ani_rss.state(self.db_path, self.config)
            self.assertEqual("unknown", current["connection_state"])
            self.assertEqual("manual", current["effective_mode"])
            self.assertEqual([], ani_rss.resources(self.db_path, 1, self.config))
            self.assertIsNone(ani_rss.cached_cover(self.db_path, 1, self.config))
        self.assertEqual(before_files, FakeAniRss.file_requests)
        with mock.patch.object(ani_rss, "_route_revision", return_value=None):
            self.assertEqual("ready", ani_rss.state(self.db_path, self.config)["connection_state"])

    def test_sync_due_retries_immediately_when_proxy_route_changes(self):
        FakeAniRss.subscriptions = []
        result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("ready", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('ani_rss_route_revision','10')")
            stamp = dt.datetime.fromisoformat(db.execute(
                "SELECT last_success_at FROM ani_rss_state WHERE singleton=1"
            ).fetchone()[0])
        with mock.patch.object(ani_rss, "_route_revision", return_value=10):
            self.assertFalse(ani_rss.sync_due(
                self.db_path, self.config, now=stamp + dt.timedelta(minutes=1)))
        with mock.patch.object(ani_rss, "_route_revision", return_value=11):
            self.assertTrue(ani_rss.sync_due(
                self.db_path, self.config, now=stamp + dt.timedelta(minutes=1)))

    def test_sync_records_route_generation_from_start_of_attempt(self):
        FakeAniRss.subscriptions = []
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        route = {"revision": 10}
        original_snapshot = ani_rss.Client.subscription_snapshot

        def changing_snapshot(client):
            result = original_snapshot(client)
            route["revision"] = 11
            return result

        def proxy_route(_url):
            return {"reason": "proxy", "revision": route["revision"], "mode": "proxy", "proxy": "http://proxy"}

        with mock.patch.object(ani_rss.network_transport, "proxy_route", side_effect=proxy_route), \
                mock.patch.object(ani_rss.Client, "subscription_snapshot", new=changing_snapshot):
            result = ani_rss.sync(self.db_path, self.config)
        self.assertEqual("ready", result["state"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            recorded = db.execute(
                "SELECT value FROM metadata WHERE key='ani_rss_route_revision'"
            ).fetchone()[0]
            stamp = dt.datetime.fromisoformat(db.execute(
                "SELECT last_success_at FROM ani_rss_state WHERE singleton=1"
            ).fetchone()[0])
        self.assertEqual("10", recorded)
        with mock.patch.object(ani_rss, "_route_revision", return_value=11):
            self.assertTrue(ani_rss.sync_due(
                self.db_path, self.config, now=stamp + dt.timedelta(minutes=1)))

    def test_remote_bypass_route_still_tracks_proxy_revision(self):
        with mock.patch.object(ani_rss.network_transport, "proxy_route", return_value={"reason": "bypass", "revision": 7}):
            self.assertEqual(7, ani_rss._route_revision({"endpoint": "https://ani-rss.example"}))
        with mock.patch.object(ani_rss.network_transport, "proxy_route", return_value={"reason": "local", "revision": 8}):
            self.assertIsNone(ani_rss._route_revision({"endpoint": "http://127.0.0.1:7789"}))

    def test_failed_resource_search_records_error_and_uses_short_retry(self):
        class BrokenClient:
            def call(self, *_args, **_kwargs):
                raise RuntimeError("endpoint unavailable")

        with mock.patch.object(ani_rss, "_client", return_value=BrokenClient()):
            with self.assertRaises(RuntimeError):
                ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT last_attempt_at,error_text FROM ani_rss_search_state WHERE anime_id=1"
            ).fetchone()
        self.assertIn("RuntimeError", row[1])
        attempted = dt.datetime.fromisoformat(row[0])
        self.assertFalse(ani_rss.background_search_due(
            self.db_path, 1, self.config, now=attempted + dt.timedelta(minutes=1)))
        self.assertTrue(ani_rss.background_search_due(
            self.db_path, 1, self.config, now=attempted + dt.timedelta(minutes=6)))

    def test_malformed_nested_resource_search_records_short_retry_error(self):
        class MalformedClient:
            def call(self, path, **_kwargs):
                if path == "mikan":
                    return {"weeks": [{"items": {"unexpected": "object"}}]}
                raise AssertionError(path)

        with mock.patch.object(ani_rss, "_client", return_value=MalformedClient()):
            with self.assertRaisesRegex(RuntimeError, r"mikan\.weeks\[\]\.items"):
                ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            attempted, error_text = db.execute(
                "SELECT last_attempt_at,error_text FROM ani_rss_search_state WHERE anime_id=1"
            ).fetchone()
        self.assertIn("RuntimeError", error_text)
        self.assertTrue(ani_rss.background_search_due(
            self.db_path, 1, self.config,
            now=dt.datetime.fromisoformat(attempted) + dt.timedelta(minutes=6),
        ))

    def test_malformed_nested_mikan_group_records_error_without_replacing_resources(self):
        class MalformedGroupClient:
            def call(self, path, **_kwargs):
                if path == "mikan":
                    return {"weeks": [{"items": [{
                        "url": "https://mikan.test/Home/Bangumi/malformed", "title": "Work"
                    }]}]}
                if path == "mikanGroup":
                    return [{"label": "Broken", "rss": "https://mikan.test/RSS/broken",
                             "items": {"unexpected": "object"}}]
                raise AssertionError(path)

        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.migrate(db)
            db.execute(
                "INSERT INTO ani_rss_resource VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("keep", 1, "ani-rss", "follow", "Known good", "Group", "webrip", 1080, None,
                 1, 1, 1, 100, 1, "00000000", "{}", ani_rss.utcnow(), "2999-01-01T00:00:00+00:00"),
            )
        with mock.patch.object(ani_rss, "_client", return_value=MalformedGroupClient()):
            with self.assertRaisesRegex(RuntimeError, r"mikanGroup\[\]\.items"):
                ani_rss.search(self.db_path, 1, self.config)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM ani_rss_resource WHERE anime_id=1 AND resource_id='keep'"
            ).fetchone()[0])
            self.assertIn("RuntimeError", db.execute(
                "SELECT error_text FROM ani_rss_search_state WHERE anime_id=1"
            ).fetchone()[0])

    def test_resource_search_tries_english_title_after_other_titles_miss(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET title_zh_hans='中文名',title_ja='日本語名',title_en='Work' WHERE id=1")
        queries: list[str] = []

        class NameAwareClient:
            def call(self, path, **kwargs):
                if path == "mikan":
                    name = str((kwargs.get("params") or {}).get("text") or "")
                    queries.append(name)
                    if name != "Work":
                        return {"weeks": []}
                    return {"weeks": [{"items": [{"url": "https://mikan.test/Home/Bangumi/english",
                                                     "title": "Work"}]}]}
                if path == "mikanGroup":
                    return [{"label": "EnglishGroup", "rss": "https://mikan.test/RSS/english",
                             "items": [{"title": "[EnglishGroup] Work - 01 (1080p) [WEB-DL]",
                                        "size": 100}]}]
                raise AssertionError(path)

        with mock.patch.object(ani_rss, "_client", return_value=NameAwareClient()):
            result = ani_rss.search(self.db_path, 1, self.config)
        self.assertEqual(["中文名", "日本語名", "Work"], queries)
        self.assertEqual(1, result["found"])

    def test_search_recovers_verified_subscription_page_without_keyword_lookup(self):
        FakeAniRss.subscriptions = [{"id": "known", "title": "作品", "bgmUrl": "https://bgm.tv/subject/123",
                                    "url": "https://mikan.test/RSS/Bangumi?bangumiId=4072&subgroupid=583", "enable": True}]
        ani_rss.sync(self.db_path, self.config)
        client = mock.Mock()
        client.call.return_value = [{"label": "ReleaseGroup", "bgmUrl": "https://bgm.tv/subject/123",
                                     "rss": "https://mikan.test/RSS/Bangumi?bangumiId=4072&subgroupid=1",
                                     "items": [{"title": "Work - 01 (1080p)", "size": 100}]}]
        with mock.patch.object(ani_rss, "_client", return_value=client):
            result = ani_rss.search(self.db_path, 1, self.config)
        self.assertEqual(1, result["found"])
        client.call.assert_called_once_with("mikanGroup", params={"url": "https://mikan.test/Home/Bangumi/4072"}, timeout=60)

    def test_short_query_keeps_correct_sequel_and_rejects_other_seasons(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE anime_work SET title_zh_hans='相同作品 第二季',title_ja='日本語 第2期',title_en=NULL WHERE id=1")
        client = mock.Mock()
        def call(path, **kwargs):
            if path == "mikan":
                if kwargs["params"]["text"] != "相同作品":
                    return {"weeks": []}
                return {"weeks": [{"items": [
                    {"title": "相同作品", "bgmId": "999", "url": "https://mikan.test/Home/Bangumi/1"},
                    {"title": "相同作品 第二季", "bgmId": "123", "url": "https://mikan.test/Home/Bangumi/2"}]}]}
            self.assertEqual("https://mikan.test/Home/Bangumi/2", kwargs["params"]["url"])
            return [{"label": "ReleaseGroup", "bgmUrl": "https://bgm.tv/subject/123", "rss": "https://mikan.test/rss",
                     "items": [{"title": "Work - 01 (1080p)", "size": 100}]}]
        client.call.side_effect = call
        with mock.patch.object(ani_rss, "_client", return_value=client):
            self.assertEqual(1, ani_rss.search(self.db_path, 1, self.config)["found"])
        group_calls = [call for call in client.call.call_args_list if call.args[0] == "mikanGroup"]
        self.assertEqual(1, len(group_calls))

    def test_resource_page_identity_mismatch_and_empty_groups_never_report_available(self):
        for groups in ([{"bgmUrl": "https://bgm.tv/subject/999", "rss": "https://mikan.test/rss",
                         "items": [{"title": "Other - 01 (1080p)"}]}],
                       [{"bgmUrl": "https://bgm.tv/subject/123", "rss": "https://mikan.test/rss", "items": []}]):
            client = mock.Mock()
            client.call.side_effect = [{"weeks": [{"items": [{"title": "作品", "url": "https://mikan.test/Home/Bangumi/1"}]}]}, groups]
            with mock.patch.object(ani_rss, "_client", return_value=client):
                self.assertEqual(0, ani_rss.search(self.db_path, 1, self.config)["found"])

    def test_subscription_refresh_requests_only_selected_media_and_preserves_regular_sync(self):
        FakeAniRss.subscriptions = [
            {"id": "one", "bgmUrl": "https://bgm.tv/subject/123", "url": "https://mikan.test/rss1", "enable": True},
            {"id": "two", "bgmUrl": "https://bgm.tv/subject/123", "url": "https://mikan.test/rss2", "enable": True}]
        ani_rss.sync(self.db_path, self.config)
        before = ani_rss.state(self.db_path, self.config)["last_success_at"]
        with mock.patch.object(ani_rss.Client, "play_list", autospec=True, return_value=[]) as playlist:
            result = ani_rss.sync(self.db_path, self.config, media_remote_ids={"two"})
        self.assertEqual(["https://mikan.test/rss2"], [call.args[1] for call in playlist.call_args_list])
        self.assertEqual(before, ani_rss.state(self.db_path, self.config)["last_success_at"])
        self.assertFalse(result["snapshotComplete"])
        self.assertEqual(1, result["mediaSubscriptions"])

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
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM ani_rss_episode_state WHERE remote_id='remote-a'"
            ).fetchone()[0])

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
            db.execute("UPDATE ani_rss_search_state SET last_attempt_at=? WHERE anime_id=1",
                       ((later + dt.timedelta(days=2)).isoformat(),))
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

    def test_background_refresh_does_not_count_stale_provider_result_as_refreshed(self):
        self.assertTrue(ani_rss.sync(self.db_path, self.config)["snapshotComplete"])
        with mock.patch.object(ani_rss, "search", return_value={"animeId": 1, "found": 0,
                                                                 "eligible": 0, "stale": True}):
            result = ani_rss.refresh_background_resources(self.db_path, self.config)
        self.assertTrue(result["started"])
        self.assertEqual(0, result["refreshed"])
        self.assertEqual(0, result["failed"])

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

    def test_background_search_checks_this_seasons_tv_before_older_or_other_works(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("ALTER TABLE anime_work ADD COLUMN media_code TEXT DEFAULT 'tv'")
            db.executemany("INSERT INTO anime_work VALUES(?,?,?,?,?,?,?,?)", [
                (2, 124, 'Movie', None, None, '2026-10', 1, 'movie'),
                (3, 125, 'TV', None, None, '2026-10', 12, 'tv'),
                (4, 126, 'Future', None, None, '2026-11', 12, 'tv'),
            ])
        self.assertEqual([3, 2, 1], ani_rss.automatic_search_ids(self.db_path, today=dt.date(2026, 10, 2)))

    def test_reported_episode_is_preferred_and_fractional_specials_are_not_rounded(self):
        for value, expected in ((1.0, 1), ("2", 2), (1.5, None), (float("inf"), None)):
            with self.subTest(episode=value):
                self.assertEqual(expected, ani_rss._resource_episode({"episode": value, "title": "Work - 09 [1080p]"}))
        self.assertEqual(9, ani_rss._resource_episode({"title": "Work - 09 [1080p]"}))

    def test_first_report_without_update_timestamp_changes_progress_generation(self):
        ani_rss.sync(self.db_path, self.config)
        before = ani_rss.state(self.db_path, self.config)["release_progress_generation"]
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            ani_rss.record_release_progress(db, 1, 1, "2026-09-01T00:00:00Z")
        after = ani_rss.state(self.db_path, self.config)
        self.assertNotEqual(before, after["release_progress_generation"])
        self.assertIsNone(after["last_release_update_at"])

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
