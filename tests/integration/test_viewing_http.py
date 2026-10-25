from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from animemachine.catalog import service
from animemachine.config.policy import ConfigStore
from animemachine.integrations import playback
from animemachine.torrents import runtime
from animemachine import application_update


class ViewingHTTPTests(unittest.TestCase):
    authentication = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "catalog.sqlite3"
        shutil.copyfile(Path(__file__).parents[1] / "fixtures/anime-catalog.sqlite3", self.database)
        self.env = mock.patch.dict(os.environ, {"ANM_AUTH_ENABLED": str(self.authentication).lower(),
            "ANM_AUTH_DB": str(self.root / "auth.sqlite3"), "ANM_ADMIN_USERNAME": "admin", "ANM_ADMIN_PASSWORD": "episode-test-040"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ConfigStore(self.root / "config.json", service.EXAMPLE_CONFIG)
        cfg = self.config.read()
        cfg["deployment"]["libraryUncRoot"] = str(self.root)
        self.config.write(cfg)
        handler = service.make_handler(self.database, self.config, start_warmup=False)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.headers = {}
        if self.authentication:
            with self.request("/api/auth/login", {"username": "admin", "password": "episode-test-040"}) as response:
                self.headers = {"Cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                                "X-CSRF-Token": json.load(response)["csrfToken"]}
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.anime_id, self.bgm_id = db.execute("SELECT id,bgm_id FROM anime_work ORDER BY id LIMIT 1").fetchone()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, method=None, headers=None):
        request = urllib.request.Request(path if path.startswith("http") else self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Content-Type": "application/json", **self.headers, **(headers or {})})
        return urllib.request.urlopen(request, timeout=10)

    def test_upgraded_native_library_indexes_on_demand_and_offers_editions(self):
        owner = self.root / 'Ordinary Show'
        for version in ('DVDRip', 'BDRip'):
            (owner / version).mkdir(parents=True)
            (owner / version / f'[Group] Show - 01 [{version}].mkv').write_bytes(b'media')
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            runtime.migrate_overlay(db)
            db.execute('DELETE FROM runtime_work')
            db.execute("INSERT INTO runtime_work(private_work_id,anime_id,target_unc,directory_name,official_title,date_code,library_state,scope_state,relation_state,origin,mapping_method,evidence_json,updated_at) VALUES(1,?,?,'Show','Show','2006_01','existing','active','standalone','preexisting_local','test','{}','now')",
                       (self.anime_id, str(owner)))
        payload = {}
        for _ in range(40):
            with self.request(f'/api/anime/{self.anime_id}/playback') as response:
                payload = json.load(response)
            if payload['available']:
                break
            self.assertTrue(payload.get('indexing'))
            time.sleep(.1)
        self.assertTrue(payload['available'])
        self.assertEqual(2, len(payload['editions']))
        self.assertEqual(1, payload['count'])
        with self.request(f'/api/anime/{self.anime_id}/playback?source=' + payload['editions'][0]['id']) as response:
            self.assertEqual(1, json.load(response)['count'])
        query = urllib.parse.urlencode({'source': str(owner / 'DVDRip'), 'edition': payload['editions'][0]['id']})
        with self.request(f'/api/anime/{self.anime_id}/playback?' + query) as response:
            scoped = json.load(response)
        self.assertEqual(1, len(scoped['editions']))
        self.assertIn('DVDRip', scoped['editions'][0]['label'])
        self.assertEqual(scoped['available'], scoped['editions'][0]['id'] == payload['editions'][0]['id'])

    def test_local_update_status_does_not_contact_release_provider(self):
        with mock.patch.object(application_update, 'status', side_effect=AssertionError('Internet forbidden')):
            with self.request('/api/update/status?local=1') as response:
                self.assertIn('currentVersion', json.load(response))

    def test_radar_unwatched_highlight_tracks_account_playback_not_detail_clicks(self):
        def radar():
            with self.request('/api/anime?radar=1&limit=all') as response:
                return next(row for row in json.load(response)['items'] if row['id'] == self.anime_id)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('''INSERT INTO ani_rss_subscription(remote_id,anime_id,title,enabled,subscription_kind,
                current_episode,remote_state,first_seen_at,last_seen_at,evidence_json)
                VALUES('watch-test',?,'Test',1,'follow',3,'enabled','now','now','{}')''', (self.anime_id,))
        with mock.patch.object(playback, 'latest_known_episodes', return_value={self.anime_id: 3}):
            self.assertTrue(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}') as response:
                response.read()
            self.assertTrue(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing') as response:
                record = json.load(response)
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 2, 'revision': record['revision'], 'bgmId': self.bgm_id}) as response:
                record = json.load(response)
            self.assertTrue(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 3, 'revision': record['revision'], 'bgmId': self.bgm_id}) as response:
                record = json.load(response)
            self.assertTrue(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 3, 'revision': record['revision'],
                    'bgmId': self.bgm_id, 'completeLatest': True, 'latestEpisode': 3}) as response:
                record = json.load(response)
            self.assertFalse(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 3, 'revision': record['revision'],
                    'bgmId': self.bgm_id, 'restoreCompleted': False, 'latestEpisode': 3}) as response:
                record = json.load(response)
            self.assertTrue(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 4, 'revision': record['revision'], 'bgmId': self.bgm_id}) as response:
                record = json.load(response)
            self.assertFalse(radar()['hasUnwatched'])
            with self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 3, 'revision': record['revision'], 'bgmId': self.bgm_id}) as response:
                response.read()
            with contextlib.closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE ani_rss_subscription SET enabled=0 WHERE remote_id='watch-test'")
            self.assertFalse(radar()['hasUnwatched'])
            self.assertEqual(3, radar()['viewing']['episode'])

    def test_subscription_http_accepts_exact_variants_and_preserves_policy_override(self):
        cfg = self.config.read()
        cfg['components']['downloadClient']['submissionEnabled'] = True
        self.config.write(cfg)
        parent = 'ar-' + 'a' * 24
        variant = parent + '~' + 'b' * 16
        with mock.patch.object(service.ani_rss, 'subscribe', return_value={'remoteId': 'verified'}) as subscribe:
            for resource, override in ((parent, False), (variant, True)):
                with self.request(f'/api/ani-rss/resources/{resource}/subscribe', {'policyOverride': override}) as response:
                    self.assertEqual(202, response.status)
                    self.assertEqual('verified', json.load(response)['remoteId'])
                self.assertEqual(resource, subscribe.call_args.args[1])
                self.assertEqual(override, subscribe.call_args.kwargs['policy_override'])
            for resource in (variant + 'b', parent + '~invalid'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request(f'/api/ani-rss/resources/{resource}/subscribe', {})
                with error.exception as response:
                    self.assertEqual(404, response.code)
            self.assertEqual(2, subscribe.call_count)

    def test_precise_latest_completion_and_changed_frontier_are_account_safe(self):
        media = self.root / 'Show - 01.mp4'
        media.write_bytes(b'synthetic-media')
        item = playback.PlaybackItem(playback.MediaLocator.local(media, media.stat().st_size), 'Ep01', 1,
                                     media.stat().st_size, 'managed', self.bgm_id)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('''INSERT INTO ani_rss_subscription(remote_id,anime_id,title,enabled,subscription_kind,
                current_episode,remote_state,first_seen_at,last_seen_at,evidence_json)
                VALUES('precise-test',?,'Test',1,'follow',1,'enabled','now','now','{}')''', (self.anime_id,))
        def highlighted():
            with self.request('/api/anime?radar=1&limit=all') as response:
                return next(row for row in json.load(response)['items'] if row['id'] == self.anime_id)['hasUnwatched']
        with mock.patch.object(playback, 'latest_known_episodes', return_value={self.anime_id: 1}), \
                mock.patch.object(playback, 'collect_items', return_value=[item]):
            with self.request(f'/api/anime/{self.anime_id}/playback/handoff', {'player': 'copy', 'enhanced': True}) as response:
                handoff = json.load(response)
            with self.request(handoff['playlistUrl']) as response:
                url = next(line for line in response.read().decode().splitlines() if line.startswith('http'))
            token = urllib.parse.urlparse(url).path.split('/')[4]
            with self.request(f'/api/playback/item/{token}/episode.anm') as response:
                manifest = json.load(response)
            for event, position, pending in ((1, 8999, True), (2, 9000, False)):
                with self.request(manifest['eventUrl'], {'event': event, 'instance': 'threshold-test',
                        'positionMs': position, 'durationMs': 10000}) as response:
                    self.assertTrue(json.load(response)['recorded'])
                self.assertEqual(pending, highlighted())
            with self.request(f'/api/anime/{self.anime_id}/viewing') as response:
                record = json.load(response)
            with mock.patch.object(playback, 'latest_known_episodes', return_value={self.anime_id: 2}):
                self.assertTrue(highlighted())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request(f'/api/anime/{self.anime_id}/viewing', {'episode': 1, 'bgmId': self.bgm_id,
                        'revision': record['revision'], 'completeLatest': True, 'latestEpisode': 1})
                self.assertEqual(409, error.exception.code)
                error.exception.close()

    def test_hide_search_restore_retains_identity_and_viewing(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            title = db.execute("SELECT title_ja FROM anime_work WHERE id=?", (self.anime_id,)).fetchone()[0]
        path = f"/api/hidden-works/{self.bgm_id}"
        with self.request(path, {"hidden": True}) as response:
            self.assertTrue(json.load(response)["hidden"])
        for mode in ("sort=title", "radar=1"):
            with self.request("/api/anime?limit=all&" + mode) as response:
                self.assertNotIn(self.anime_id, [item["id"] for item in json.load(response)["items"]])
        with self.request("/api/anime?limit=all&q=" + urllib.parse.quote(title)) as response:
            row = next(row for row in json.load(response)["items"] if row["id"] == self.anime_id)
            self.assertTrue(row["hidden"])
        with self.request("/api/hidden-works") as response:
            self.assertIn(self.bgm_id, [row["bgm_id"] for row in json.load(response)["items"]])
        with self.request(f"/api/anime/{self.anime_id}") as response:
            self.assertTrue(json.load(response)["hidden"])
        with mock.patch.object(playback, "viewing_identities", return_value={self.anime_id: self.bgm_id}), \
                mock.patch.object(service.catchup, "queue", return_value=([], [], [])) as queue:
            with self.request("/api/playback/catchup", {"preview": True, "from": "1995-10", "to": "1995-12"}) as response:
                response.read()
            self.assertNotIn(self.anime_id, [row["id"] for row in queue.call_args.args[1]])
        with self.request(path, {"hidden": False}) as response:
            self.assertFalse(json.load(response)["hidden"])
        with self.request("/api/anime?limit=all&sort=title") as response:
            self.assertIn(self.anime_id, [item["id"] for item in json.load(response)["items"]])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(path, {"hidden": "false"})
        self.assertEqual(400, error.exception.code)
        error.exception.close()

    def test_viewing_failure_keeps_radar_titles_available(self):
        with mock.patch.object(playback, 'latest_cached_episodes', side_effect=sqlite3.DatabaseError('unavailable')):
            with self.request('/api/anime?radar=1&limit=all') as response:
                payload = json.load(response)
        self.assertTrue(payload['items'])
        self.assertTrue(payload['viewingUnavailable'])
        self.assertFalse(any(row['hasUnwatched'] for row in payload['items']))

    def test_head_subtitles_playlist_do_not_mark_played_but_media_get_does(self):
        media = self.root / "Show - 01.mp4"
        media.write_bytes(b"synthetic-media")
        subtitle = media.with_suffix(".srt")
        subtitle.write_bytes(b"synthetic-subtitle")
        item = playback.PlaybackItem(playback.MediaLocator.local(media, media.stat().st_size), "Ep01", 1.0, media.stat().st_size, "managed")
        with mock.patch.object(playback, "collect_items", return_value=[item]):
            with self.request(f"/api/anime/{self.anime_id}/playback/handoff", {"player": "copy"}) as response:
                handoff = json.load(response)
        with self.request(handoff["playlistUrl"]) as response:
            body = response.read().decode()
        url = next(line for line in body.splitlines() if line.startswith("http"))
        sub = next(line.split("=", 1)[1] for line in body.splitlines() if line.startswith("#EXTVLCOPT"))
        for path, method in ((url, "HEAD"), (sub, "GET")):
            with self.request(path, method=method) as response:
                response.read()
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            self.assertIsNone(json.load(response)["episode"])
        recorded = threading.Event()
        original_started = playback.MediaTokenRegistry.started

        def record_access(*args, **kwargs):
            result = original_started(*args, **kwargs)
            recorded.set()
            return result

        # The stream must successfully write bytes before recording playback.
        # Receiving those bytes can precede the server's SQLite commit.
        with mock.patch.object(playback.MediaTokenRegistry, "started", record_access):
            with self.request(url) as response:
                self.assertEqual(b"synthetic-media", response.read())
            self.assertTrue(recorded.wait(5), "media access was not recorded")
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            anchor = json.load(response)
        self.assertEqual(1, anchor["episode"])
        # A later stream retry cannot undo a user's explicit correction.
        with self.request(f"/api/anime/{self.anime_id}/viewing", {"episode": 0, "revision": anchor["revision"], "bgmId": self.bgm_id}) as response:
            self.assertEqual(0, json.load(response)["episode"])
        with self.request(url) as response:
            response.read()
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            self.assertEqual(0, json.load(response)["episode"])

    def test_stable_identity_survives_catalog_id_change_and_stale_form_is_rejected(self):
        with self.request(f"/api/anime/{self.anime_id}/viewing", {"episode": 4, "revision": 0, "bgmId": self.bgm_id}) as response:
            self.assertEqual(4, json.load(response)["episode"])
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE anime_work SET id=id+1000")
        new_id = self.anime_id + 1000
        with self.request(f"/api/anime/{new_id}/viewing") as response:
            self.assertEqual(4, json.load(response)["episode"])
        with self.request("/api/viewing") as response:
            self.assertEqual(new_id, json.load(response)["items"][0]["animeId"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(f"/api/anime/{new_id}/viewing", {"episode": 9, "revision": 1, "bgmId": self.bgm_id + 1})
        self.assertEqual(409, error.exception.code)
        error.exception.close()

    def test_unknown_event_capability_does_not_write_and_bad_season_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/playback/started/unknown", {"event": 1})
        self.assertEqual(404, error.exception.code)
        error.exception.close()

    def test_catchup_handoff_plays_only_checked_works_in_requested_order(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            available = db.execute("SELECT id,bgm_id,title_ja FROM anime_work ORDER BY id LIMIT 3").fetchall()
        rows = [{"id": row[0], "bgm_id": row[1], "title_ja": row[2], "media_code": "tv", 'subscription_state': 'active'} for row in available]
        def collect(_database, identity, _config, **_kwargs):
            media = self.root / f"{identity}-01.mp4"
            media.write_bytes(b"synthetic-media")
            return [playback.PlaybackItem(playback.MediaLocator.local(media, 15), "Episode 1", 1, 15, "managed")]
        request = {"from": "2026-10", "to": "2026-12", "player": "copy"}
        with mock.patch.object(service, "query_catalog", return_value={"items": rows}), \
                mock.patch.object(playback, 'latest_known_episodes', return_value={row['id']: 1 for row in rows}), \
                mock.patch.object(playback, "collect_items", side_effect=collect):
            with self.request("/api/playback/catchup", {**request, "preview": True}) as response:
                self.assertEqual(3, len(json.load(response)["groups"]))
            for selection in (None, [], [True], [rows[0]["bgm_id"], rows[0]["bgm_id"]]):
                body = {**request, **({"selectedBgmIds": selection} if selection is not None else {})}
                with self.subTest(selection=selection), self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("/api/playback/catchup", body)
                error.exception.close()
                self.assertEqual(400, error.exception.code)
            # Preview data may become stale when a title is hidden or removed.
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/playback/catchup", {**request, "selectedBgmIds": [rows[0]["bgm_id"], 999999]})
            self.assertEqual(400, error.exception.code)
            error.exception.close()
            selected = [rows[2]["bgm_id"], rows[0]["bgm_id"]]
            with self.request("/api/playback/catchup", {**request, "selectedBgmIds": selected}) as response:
                handoff = json.load(response)
            self.assertEqual(selected, [group["bgmId"] for group in handoff["groups"]])
            self.assertEqual(2, handoff["count"])
            # Media removed or changed between preview and handoff must not
            # silently produce an incomplete, unexpectedly shortened playlist.
            with mock.patch.object(service.catchup, "validate", return_value=([], [], [])):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("/api/playback/catchup", {**request, "selectedBgmIds": selected})
                self.assertEqual(400, error.exception.code)
                error.exception.close()
            with self.request(handoff["playlistUrl"]) as response:
                playlist = response.read().decode()
            titles = [line for line in playlist.splitlines() if line.startswith("#EXTINF")]
            self.assertEqual(2, len(titles))
            self.assertIn(rows[2]["title_ja"], titles[0])
            self.assertIn(rows[0]["title_ja"], titles[1])

    def test_catchup_range_handoff_keeps_viewing_position(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            work = db.execute('SELECT id,bgm_id,title_ja FROM anime_work ORDER BY id LIMIT 1').fetchone()
        row = {'id': work[0], 'bgm_id': work[1], 'title_ja': work[2], 'media_code': 'tv', 'subscription_state': 'active'}
        media = []
        for ep in range(1, 5):
            path = self.root / f'episode-{ep}.mp4'
            path.write_bytes(f'episode-{ep}'.encode())
            media.append(playback.PlaybackItem(playback.MediaLocator.local(path, path.stat().st_size),
                                               f'Ep{ep}', ep, path.stat().st_size, 'managed'))
        with self.request(f'/api/anime/{row["id"]}/viewing',
                          {'episode': 2, 'positionMs': 12000, 'revision': 0, 'bgmId': row['bgm_id']}) as response:
            saved = json.load(response)
        request = {'from': '2026-10', 'to': '2026-12', 'selectedBgmIds': [row['bgm_id']], 'player': 'copy'}
        with mock.patch.object(service, 'query_catalog', return_value={'items': [row]}), \
             mock.patch.object(playback, 'latest_known_episodes', return_value={row['id']: 4}), \
             mock.patch.object(playback, 'collect_items', return_value=media):
            with self.request('/api/playback/catchup', {**request, 'preview': True}) as response:
                group = json.load(response)['groups'][0]
            self.assertEqual([2, 3, 4], group['episodes'])
            self.assertEqual([1, 2, 3, 4], group['availableEpisodes'])
            for lower, upper, expected in ((1, 4, [1, 2, 3, 4]), (3, 4, [3, 4])):
                with self.request('/api/playback/catchup', {**request, 'episodeRanges': [
                    {'bgmId': row['bgm_id'], 'from': lower, 'to': upper}]}) as response:
                    handoff = json.load(response)
                self.assertEqual(expected, handoff['groups'][0]['episodes'])
                with self.request(handoff['playlistUrl']) as response:
                    playlist = response.read().decode()
                self.assertEqual(len(expected), playlist.count('#EXTINF:'))
            for lower, upper in ((0, 4), (4, 2), (5, 6)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request('/api/playback/catchup', {**request, 'episodeRanges': [
                        {'bgmId': row['bgm_id'], 'from': lower, 'to': upper}]})
                self.assertEqual(400, error.exception.code)
                error.exception.close()
        with self.request(f'/api/anime/{row["id"]}/viewing') as response:
            latest = json.load(response)
        self.assertEqual((saved['revision'], saved['episode'], saved['positionMs']),
                         (latest['revision'], latest['episode'], latest['positionMs']))

    def test_catchup_fully_watched_title_is_excluded_even_with_explicit_range(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            work = db.execute('SELECT id,bgm_id,title_ja FROM anime_work ORDER BY id LIMIT 1').fetchone()
        row = {'id': work[0], 'bgm_id': work[1], 'title_ja': work[2], 'media_code': 'tv', 'subscription_state': 'active'}
        media = []
        for episode in (1, 2):
            path = self.root / f'replay-{episode}.mp4'
            path.write_bytes(b'video')
            media.append(playback.PlaybackItem(playback.MediaLocator.local(path, 5),
                                               f'Ep{episode}', episode, 5, 'managed'))
        with self.request(f'/api/anime/{row["id"]}/viewing',
                          {'episode': 2, 'revision': 0, 'bgmId': row['bgm_id']}) as response:
            response.read()
        request = {'from': '2026-10', 'to': '2026-12', 'selectedBgmIds': [row['bgm_id']], 'player': 'copy'}
        with mock.patch.object(service, 'query_catalog', return_value={'items': [row]}), \
             mock.patch.object(playback, 'latest_known_episodes', return_value={row['id']: 2}), \
             mock.patch.object(playback, 'collect_items', return_value=media):
            with self.request(f'/api/anime/{row["id"]}/viewing') as response:
                record = json.load(response)
            with self.request(f'/api/anime/{row["id"]}/viewing', {'bgmId': row['bgm_id'], 'episode': 2,
                    'revision': record['revision'], 'latestEpisode': 2, 'completeLatest': True}) as response:
                response.read()
            with self.request('/api/playback/catchup', {**request, 'preview': True}) as response:
                preview = json.load(response)
            self.assertEqual(0, preview['count'])
            self.assertEqual([], preview['groups'])
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/playback/catchup', request)
            self.assertEqual(400, error.exception.code)
            error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/playback/catchup', {**request, 'episodeRanges': [
                    {'bgmId': row['bgm_id'], 'from': 1, 'to': 2}]})
            self.assertEqual(400, error.exception.code)
            error.exception.close()

    def test_potplayer_catchup_handoff_reads_original_m3u_and_media(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            rows = [dict(id=int(row[0]), bgm_id=int(row[1]), title_ja=row[2], media_code="tv", subscription_state='active')
                    for row in db.execute("SELECT id,bgm_id,title_ja FROM anime_work ORDER BY id LIMIT 2")]
        self.assertEqual(2, len(rows))
        media = {}
        def collect(_database, identity, _config, **_kwargs):
            path = self.root / f"episode-{identity}.mp4"
            body = f"synthetic-media-{identity}".encode("ascii")
            path.write_bytes(body)
            media[identity] = body
            return [playback.PlaybackItem(playback.MediaLocator.local(path, len(body)),
                                          "Episode 1", 1, len(body), "managed",
                                          next(row["bgm_id"] for row in rows if row["id"] == identity))]
        with mock.patch.object(service, "query_catalog", return_value={"items": rows}), \
                mock.patch.object(playback, 'latest_known_episodes', return_value={row['id']: 1 for row in rows}), \
                mock.patch.object(playback, "collect_items", side_effect=collect):
            request = {"from": "2026-10", "to": "2026-12", "selectedBgmIds": [row["bgm_id"] for row in reversed(rows)],
                       "player": "potplayer", "enhanced": True}
            with self.request("/api/playback/catchup", request) as response:
                handoff = json.load(response)
        self.assertEqual(2, handoff["count"])
        self.assertEqual([row["bgm_id"] for row in reversed(rows)], [group["bgmId"] for group in handoff["groups"]])
        self.assertEqual(f'potplayer://{handoff["playlistUrl"]}', handoff["protocolUrl"])
        self.assertTrue(handoff["playlistUrl"].endswith(".m3u"))
        with self.request(handoff["playlistUrl"]) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("audio/x-mpegurl"))
            playlist = response.read().decode("utf-8")
        self.assertIn("#EXTM3U", playlist)
        urls = [line for line in playlist.splitlines() if line.startswith("http")]
        self.assertEqual(2, len(urls))
        for row, url in zip(reversed(rows), urls):
            self.assertIn(f"episode-{row['id']}.mp4", url)
            with self.request(url, method="HEAD") as response:
                self.assertEqual(200, response.status)
            with self.request(url) as response:
                self.assertEqual(media[row["id"]], response.read())

    def test_update_api_forwards_explicit_force_request(self):
        with mock.patch.object(service.application_update, "apply", return_value={"accepted": True, "restarting": True}) as apply:
            with self.request("/api/update/apply", {"force": True}) as response:
                self.assertEqual(202, response.status)
                self.assertTrue(json.load(response)["accepted"])
            apply.assert_called_once_with(host="127.0.0.1", port=self.server.server_port, force=True)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/update/apply", {"force": "true"})
            self.assertEqual(409, error.exception.code)
            error.exception.close()
            self.assertEqual(1, apply.call_count)

    def test_non_object_requests_fail_cleanly_without_breaking_other_pages(self):
        paths = [f'/api/anime/{self.anime_id}/playback/handoff', '/api/playback/catchup',
                 f'/api/anime/{self.anime_id}/viewing', '/api/images/refresh']
        for path in paths:
            for payload in ([], None, True, 'invalid'):
                with self.subTest(path=path, payload=payload):
                    request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json', **self.headers})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request, timeout=5)
                    with error.exception as response:
                        self.assertEqual(400, response.status)
                        self.assertEqual('JSON object required', json.load(response)['error'])
        with self.request('/api/health') as response:
            self.assertTrue(json.load(response)['ok'])

    def test_downloaded_precise_playlist_reads_current_resume_and_player_restart_keeps_recording(self):
        media = self.root / "Show - 01.mp4"
        media.write_bytes(b"synthetic-media")
        item = playback.PlaybackItem(playback.MediaLocator.local(media, media.stat().st_size), "Ep01", 1.0, media.stat().st_size, "managed", self.bgm_id)
        with self.request(f"/api/anime/{self.anime_id}/viewing", {"episode": 1, "positionMs": 14000, "revision": 0, "bgmId": self.bgm_id}) as response:
            response.read()
        with mock.patch.object(playback, "collect_items", return_value=[item]):
            with self.request(f"/api/anime/{self.anime_id}/playback/handoff", {"player": "copy", "enhanced": True}) as response:
                handoff = json.load(response)
        with self.request(handoff["playlistUrl"]) as response:
            media_url = next(line for line in response.read().decode().splitlines() if line.startswith("http"))
        self.assertTrue(media_url.endswith("Show%20-%2001.mp4?anm=1"))
        token = urllib.parse.urlparse(media_url).path.split("/")[4]
        manifest_url = f"/api/playback/item/{token}/episode.anm"
        with self.request(manifest_url) as response:
            manifest = json.load(response)
        self.assertEqual(9000, manifest["resumeMs"])
        self.assertEqual(f"Ep01 [ANM:{token[:12]}]", manifest["title"])
        # A player without the optional extension opens the same playlist entry
        # as ordinary video, including range requests, instead of parsing JSON.
        with self.request(media_url, headers={"Range": "bytes=0-8"}) as response:
            self.assertEqual(206, response.status)
            self.assertEqual("video/mp4", response.headers["Content-Type"])
            self.assertEqual(b"synthetic", response.read())
        for instance, event, position, expected in (("first", 9, 15000, True), ("restart", 1, 17000, True), ("first", 10, 16000, False)):
            with self.request(manifest["eventUrl"], {"event": event, "positionMs": position, "durationMs": 20000, "instance": instance}) as response:
                self.assertEqual(expected, json.load(response)["recorded"])
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            self.assertEqual(17000, json.load(response)["positionMs"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(manifest["eventUrl"], {"event": 2, "positionMs": -1, "instance": "restart"})
        self.assertEqual(400, error.exception.code)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/playback/catchup", {"from": "2026-02", "to": "2026-05"})
        self.assertEqual(400, error.exception.code)
        error.exception.close()


class AccountViewingHTTPTests(ViewingHTTPTests):
    authentication = True

    def test_account_identity_comes_from_session_and_viewer_can_correct_own_record(self):
        with self.request("/api/auth/users", {"username": "viewer", "password": "viewing-test-040", "role": "user"}) as response:
            response.read()
        with self.request(f"/api/anime/{self.anime_id}/viewing", {"episode": 4, "revision": 0, "bgmId": self.bgm_id}) as response:
            response.read()
        with self.request("/api/auth/login", {"username": "viewer", "password": "viewing-test-040"}) as response:
            self.headers = {"Cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                            "X-CSRF-Token": json.load(response)["csrfToken"]}
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            self.assertIsNone(json.load(response)["episode"])
        with self.request(f"/api/anime/{self.anime_id}/viewing", {"episode": 2, "revision": 0, "bgmId": self.bgm_id, "userId": 1}) as response:
            self.assertEqual(2, json.load(response)["episode"])
        # Folder paths are administrative information, including cached results.
        for method, body in (("GET", None), ("POST", {})):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/library/layout-preview", body, method)
            self.assertEqual(403, error.exception.code)
            error.exception.close()
        with self.request("/api/auth/login", {"username": "admin", "password": "episode-test-040"}) as response:
            self.headers = {"Cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                            "X-CSRF-Token": json.load(response)["csrfToken"]}
        with self.request(f"/api/anime/{self.anime_id}/viewing") as response:
            self.assertEqual(4, json.load(response)["episode"])


if __name__ == "__main__":
    unittest.main()
