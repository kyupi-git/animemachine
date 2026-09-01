from __future__ import annotations

import json
import contextlib
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from animemachine.integrations import ani_rss
from animemachine.torrents import runtime as runtime_catalog


class FakeAniRss(BaseHTTPRequestHandler):
    subscriptions = []
    seen_keys = []
    delete_paths = []

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
            data = {"total": len(self.subscriptions), "weekList": [{"items": self.subscriptions}]}
        elif path == "/api/mikan":
            data = {"weeks": [{"items": [{"url": "https://mikan.test/Home/Bangumi/1", "title": "作品"}]}]}
        elif path == "/api/mikanGroup":
            data = [{"label": "SubsPlease", "rss": "https://mikan.test/RSS/Bangumi?subgroupid=1",
                     "items": [{"title": "[SubsPlease] Work - 01 (1080p) [WEB-DL]", "size": 100}]}]
        elif path == "/api/rssToAni":
            data = {"id": "remote-1", "title": "作品", "url": body["url"], "enable": True}
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
        FakeAniRss.subscriptions = []; FakeAniRss.seen_keys = []; FakeAniRss.delete_paths = []
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


if __name__ == "__main__":
    unittest.main()

