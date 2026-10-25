from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.integrations import catchup, playback, viewing


class ViewingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = viewing.Store(self.path / "viewing.sqlite3")

    def test_accounts_restart_and_manual_epoch(self):
        self.assertTrue(self.store.started(1, 10, 3, 0, 100, "media"))
        self.assertIsNone(self.store.get(2, 10)["episode"])
        anchor = viewing.Store(self.store.path).get(1, 10)
        result = self.store.correct(1, 10, 1, anchor["revision"])
        self.assertEqual(3, result["previousEpisode"])
        self.assertFalse(self.store.started(1, 10, 4, 0, 200, "media"))
        self.assertTrue(self.store.started(1, 10, 2, result["epoch"], 300, "potplayer"))
        with self.assertRaises(viewing.Conflict):
            self.store.correct(1, 10, 9, anchor["revision"])

    def test_latest_completion_is_explicit_or_measured_and_does_not_mark_future_updates(self):
        record = self.store.correct(1, 10, 3, 0, latest_episode=3)
        self.assertEqual({10}, self.store.unwatched(1, {10: 3}))
        record = self.store.correct(1, 10, 3, record['revision'], latest_episode=3, complete_latest=True)
        self.assertEqual(set(), self.store.unwatched(1, {10: 3}))
        self.assertEqual({10}, self.store.unwatched(2, {10: 3}))
        self.assertEqual({10}, self.store.unwatched(1, {10: 4}))
        record = self.store.correct(1, 10, 3, record['revision'], latest_episode=3)
        self.assertEqual({10}, self.store.unwatched(1, {10: 3}))
        self.assertTrue(self.store.started(1, 10, 3, record['epoch'], 100, 'potplayer', position_ms=8999, duration_ms=10000))
        self.assertEqual({10}, self.store.unwatched(1, {10: 3}))
        self.assertTrue(self.store.started(1, 10, 3, record['epoch'], 100, 'potplayer', position_ms=9000, duration_ms=10000))
        self.assertEqual(set(), self.store.unwatched(1, {10: 3}))
        record = self.store.get(1, 10)
        record = self.store.correct(1, 10, 100, record['revision'], latest_episode=3)
        self.assertEqual(set(), self.store.unwatched(1, {10: 3}))
        self.assertEqual({10}, self.store.unwatched(1, {10: 4}))
        self.store.correct(1, 10, None, record['revision'], latest_episode=3)
        self.assertEqual({10}, self.store.unwatched(1, {10: 3}))

    def test_completion_threshold_upgrade_runs_once_and_respects_later_manual_corrections(self):
        self.store.started(1, 10, 3, 0, 100, 'potplayer', position_ms=9500, duration_ms=10000)
        with contextlib.closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE viewing_episode SET completed=0')
            db.execute('PRAGMA user_version=0')
        upgraded = viewing.Store(self.store.path)
        self.assertEqual(set(), upgraded.unwatched(1, {10: 3}))
        upgraded.correct(1, 10, 3, upgraded.get(1, 10)['revision'], latest_episode=3)
        self.assertEqual({10}, viewing.Store(self.store.path).unwatched(1, {10: 3}))

    def test_cumulative_episode_anchors_migrate_without_losing_position(self):
        catalog = self.path / 'catalog.sqlite3'
        with contextlib.closing(sqlite3.connect(catalog)) as db, db:
            db.execute('CREATE TABLE anime_work(id INTEGER,bgm_id INTEGER,episode_count INTEGER,episode_start_number INTEGER)')
            db.execute('INSERT INTO anime_work VALUES(1,10,13,171)')
        self.store.started(1, 10, 172, 0, 100, 'potplayer', position_ms=14000, duration_ms=30000, media_key='same-file')
        self.store.started(2, 10, 3, 0, 100, 'media')
        self.assertEqual(1, self.store.normalize_catalog_episodes(catalog))
        migrated = self.store.get(1, 10)
        self.assertEqual((2, 14000, 'same-file', 1), (migrated['episode'], migrated['positionMs'], migrated['mediaKey'], migrated['epoch']))
        self.assertEqual(3, self.store.get(2, 10)['episode'])
        self.assertEqual(0, self.store.normalize_catalog_episodes(catalog))
        with contextlib.closing(sqlite3.connect(catalog)) as db:
            self.assertEqual(1.5, playback.playback_episode(db, 1, 171.5))
            self.assertEqual(3, playback.playback_episode(db, 1, 3))

    def test_new_session_prevents_old_device_overwrite_and_allows_rewatch(self):
        self.store.started(1, 10, 8, 0, 200, "media")
        self.assertFalse(self.store.started(1, 10, 12, 0, 100, "media"))
        self.assertTrue(self.store.started(1, 10, 3, 0, 300, "media"))
        self.assertEqual(3, self.store.get(1, 10)["episode"])

    def test_clear_and_undo_are_revision_checked(self):
        first = self.store.correct(1, 10, 0, 0)
        cleared = self.store.correct(1, 10, None, first["revision"])
        restored = self.store.correct(1, 10, cleared["previousEpisode"], cleared["revision"])
        self.assertEqual(0, restored["episode"])
        for bad in (True, -1, "nan", "inf", 100001):
            with self.assertRaises(ValueError):
                self.store.correct(1, 10, bad, restored["revision"])

    def test_precise_position_resume_bounds_and_old_epoch(self):
        registry = playback.MediaTokenRegistry()
        locator = playback.MediaLocator.local(self.path / "01.mp4", 100)
        key = playback.media_identity(locator)
        binding = playback.ViewingBinding(1, 10, 1, 0, 100, key)
        token = registry.issue_many([locator], 900, 900, [binding])[0]
        self.assertTrue(registry.started(token, self.store, native_event=1, position_ms=123456, duration_ms=1440000))
        self.assertEqual(118456, registry.resume_ms(token, self.store))
        self.assertFalse(registry.started(token, self.store, native_event=1, position_ms=5000))
        other = registry.issue_many([locator], 900, 900, [playback.ViewingBinding(1, 10, 1, 0, 100, "other")])[0]
        self.assertEqual(0, registry.resume_ms(other, self.store))
        prior = self.store.get(1, 10)
        corrected = self.store.correct(1, 10, 1, prior["revision"], 3000)
        self.assertEqual(123456, corrected["previousPositionMs"])
        self.assertEqual(0, registry.resume_ms(token, self.store))
        self.assertFalse(registry.started(token, self.store, native_event=2, position_ms=200000))
        for position, duration in ((-1, 0), (True, 0), (10000, 1000), (604800001, 0)):
            with self.assertRaises(ValueError):
                viewing.validate_position(position, duration)

    def test_native_percent_threshold_and_completed_rewatch(self):
        self.assertTrue(self.store.started(1, 10, 2, 0, 100, 'potplayer', position_ms=2000, duration_ms=10000))
        saved = self.store.get(1, 10)
        self.assertFalse(self.store.started(1, 10, 3, 0, 200, 'potplayer', position_ms=100, duration_ms=10000))
        self.assertEqual(saved, self.store.get(1, 10))
        self.assertTrue(self.store.started(1, 10, 3, 0, 200, 'potplayer', position_ms=101, duration_ms=10000))
        self.assertFalse(viewing.completed(8999, 10000))
        self.assertTrue(viewing.completed(9000, 10000))
        self.assertTrue(self.store.started(1, 10, 3, 0, 200, 'potplayer', position_ms=9900, duration_ms=10000))
        self.assertTrue(self.store.started(1, 10, 3, 0, 200, 'potplayer', position_ms=2000, duration_ms=10000))
        self.assertTrue(self.store.episode_position(1, 10, 3)['completed'])
        record = self.store.get(1, 10)
        self.store.correct(1, 10, 3, record['revision'])
        self.assertFalse(self.store.episode_position(1, 10, 3)['completed'])

    def test_independent_episode_position_survives_rewatch(self):
        registry = playback.MediaTokenRegistry()
        def issue(number, order):
            locator = playback.MediaLocator.local(self.path / f'{number}.mp4')
            return registry.issue_many([locator], 900, 900,
                [playback.ViewingBinding(1, 10, number, 0, order, playback.media_identity(locator))])[0]
        first, second = issue(1, 100), issue(2, 200)
        registry.started(first, self.store, native_event=1, position_ms=8500, duration_ms=10000)
        registry.started(second, self.store, native_event=1, position_ms=6000, duration_ms=10000)
        replay = issue(1, 300)
        self.assertEqual(3500, registry.resume_ms(replay, self.store))
        registry.started(replay, self.store, native_event=1, position_ms=7000, duration_ms=10000)
        self.assertEqual(1000, registry.resume_ms(second, self.store))
        self.assertEqual(6000, self.store.episode_position(1, 10, 2)['positionMs'])
        self.assertEqual(1, self.store.get(1, 10)['episode'])

    def test_catchup_ranges_and_completion_do_not_modify_anchors(self):
        db_path = self.path / 'catalog.sqlite3'
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE anime_work(id INTEGER,bgm_id INTEGER)')
            db.execute('INSERT INTO anime_work VALUES(1,10)')
        self.store.correct(1, 10, 2, 0, 12000)
        before = self.store.get(1, 10)
        media = [playback.PlaybackItem(playback.MediaLocator.local(self.path / f'{ep}.mp4'),
                  'episode', ep, 100, 'managed') for ep in range(1, 5)]
        rows = [{'id': 1, 'bgm_id': 10, 'media_code': 'tv', 'title_ja': 'Test', 'subscription_state': 'active'}]
        with mock.patch.object(playback, 'collect_items', return_value=media), \
             mock.patch.object(playback, 'latest_known_episodes', return_value={1: 4}), \
             mock.patch.object(catchup.ani_rss, 'state', return_value={}):
            for ranges, expected in ((None, [2, 3, 4]), ({10: (1, 4)}, [1, 2, 3, 4]),
                                     ({10: (3, 4)}, [3, 4]), ({10: (2.5, 3.5)}, [3])):
                result, _, groups = catchup.queue(db_path, rows, {}, self.store, 1, ranges=ranges)
                self.assertEqual(expected, [item.episode for item in result])
                self.assertEqual([1, 2, 3, 4], groups[0]['availableEpisodes'])
                self.assertEqual(before, self.store.get(1, 10))
            self.assertRaises(ValueError, catchup.queue, db_path, rows, {}, self.store, 1, ranges={10: (0, 4)})
            self.store.started(1, 10, 2, before['epoch'], 300, 'potplayer', position_ms=9900, duration_ms=10000)
            self.assertEqual([3, 4], [x.episode for x in catchup.queue(db_path, rows, {}, self.store, 1)[0]])
            self.store.started(1, 10, 2, before['epoch'], 300, 'potplayer', position_ms=2500, duration_ms=10000)
            self.assertEqual([3, 4], [x.episode for x in catchup.queue(db_path, rows, {}, self.store, 1)[0]])
            record = self.store.get(1, 10)
            self.store.correct(1, 10, 100, record['revision'], latest_episode=3)
            self.assertEqual([4], [x.episode for x in catchup.queue(db_path, rows, {}, self.store, 1)[0]])

    def test_stale_catchup_selection_omits_empty_work(self):
        db_path = self.path / 'catalog.sqlite3'
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE anime_work(id INTEGER,bgm_id INTEGER)')
            db.execute('INSERT INTO anime_work VALUES(1,10)')
        self.store.started(1, 10, 2, 0, 100, 'potplayer', position_ms=9900, duration_ms=10000)
        media = [playback.PlaybackItem(playback.MediaLocator.local(self.path / f'{ep}.mp4'),
                  'episode', ep, 100, 'managed') for ep in (1, 2)]
        rows = [{'id': 1, 'bgm_id': 10, 'media_code': 'tv', 'title_ja': 'Test', 'subscription_state': 'active'}]
        with mock.patch.object(playback, 'collect_items', return_value=media), \
             mock.patch.object(playback, 'latest_known_episodes', return_value={1: 2}), \
             mock.patch.object(catchup.ani_rss, 'state', return_value={}):
            self.assertEqual(([], [], []), catchup.queue(db_path, rows, {}, self.store, 1, ranges={}))
            self.assertEqual(([], [], []), catchup.queue(db_path, rows, {}, self.store, 1, ranges={10: (1, 2)}))

    def test_catchup_range_payload_validation(self):
        self.assertEqual({}, catchup.episode_ranges([10], None))
        self.assertEqual({10: (1., 4.)}, catchup.episode_ranges([10], [{'bgmId': 10, 'from': 1, 'to': 4}]))
        for payload in ([{'bgmId': 10, 'from': 4, 'to': 1}], [{'bgmId': 10, 'from': True, 'to': 4}],
                        [{'bgmId': 10, 'from': float('nan'), 'to': 4}], [{'bgmId': 999, 'from': 1, 'to': 4}],
                        [{'bgmId': 10, 'from': 1, 'to': 4}] * 2, {'10': [1, 4]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                catchup.episode_ranges([10], payload)

    def test_episode_change_clears_position_and_media_access_preserves_same_file_position(self):
        self.store.started(1, 10, 1, 0, 100, "potplayer", position_ms=123000, duration_ms=1400000, media_key="file")
        self.store.started(1, 10, 1, 0, 200, "media", media_key="file")
        self.assertEqual(123000, self.store.get(1, 10)["positionMs"])
        self.store.started(1, 10, 2, 0, 200, "media", media_key="file2")
        self.assertEqual(0, self.store.get(1, 10)["positionMs"])

    def test_tokens_do_not_mark_queued_or_resolved_items_or_subtitles(self):
        registry = playback.MediaTokenRegistry()
        media = playback.MediaLocator.local(self.path / "Show - 01.mkv")
        subtitle = playback.MediaLocator.local(self.path / "Show - 01.ass")
        token, sub = registry.issue_many([media, subtitle], 900, 900,
                                        [playback.ViewingBinding(1, 10, 1, 0, 100), None])
        registry.resolve(token)
        self.assertIsNone(self.store.get(1, 10)["episode"])
        self.assertFalse(registry.started(sub, self.store))
        self.assertTrue(registry.started(token, self.store))
        self.assertFalse(registry.started(token, self.store))
        self.assertEqual(1, self.store.get(1, 10)["revision"])

    def test_native_claim_ignores_prefetch_and_accepts_real_events(self):
        registry = playback.MediaTokenRegistry()
        token = registry.issue_many([playback.MediaLocator.local(self.path / "01.mp4")], 900, 900,
                                    [playback.ViewingBinding(1, 10, 1, 0, 100)])[0]
        self.assertTrue(registry.claim(token))
        self.assertFalse(registry.started(token, self.store))
        self.assertIsNone(self.store.get(1, 10)["episode"])
        self.assertTrue(registry.started(token, self.store, native_event=1))
        self.assertFalse(registry.started(token, self.store, native_event=1))

    def test_catchup_includes_anchor_zero_and_skips_unknown_episodes_and_other_types(self):
        db_path = self.path / "catalog.sqlite3"
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE anime_work(id INTEGER,bgm_id INTEGER)")
            db.executemany("INSERT INTO anime_work VALUES(?,?)", [(1, 1), (2, 2), (3, 3)])
            db.commit()
        def collect(db_path, anime_id, config, **kwargs):
            last = {1: 4, 2: 5, 3: 12}[anime_id]
            numbers = list(range(0 if anime_id == 2 else 1, last + 1)) + [None]
            return [playback.PlaybackItem(playback.MediaLocator.local(self.path / f"{anime_id}-{ep}.mkv"),
                                          "episode", ep, 100, "managed") for ep in numbers]
        for anime_id, episode in ((1, 3), (2, 4), (3, 10)):
            self.store.correct(1, anime_id, episode, 0)
        rows = [{"id": anime_id, "bgm_id": anime_id, "media_code": "tv", "title_ja": str(anime_id),
                 'subscription_state': 'active'} for anime_id in (1, 2, 3)]
        rows.append({"id": 999, 'bgm_id': 999, "media_code": "movie"})
        with mock.patch.object(playback, "collect_items", side_effect=collect), \
             mock.patch.object(playback, 'latest_known_episodes', return_value={1: 4, 2: 5, 3: 12}), \
             mock.patch.object(catchup.ani_rss, "state", return_value={}):
            items, owners, groups = catchup.queue(db_path, rows, {}, self.store, 1)
        self.assertEqual([3, 4, 4, 5, 10, 11, 12], [item.episode for item in items])
        self.assertEqual([1, 1, 2, 2, 3, 3, 3], owners)
        self.assertEqual(3, len(groups))
        self.assertEqual(3, self.store.get(1, 1)["episode"])

    def test_calendar_quarter_validation(self):
        self.assertEqual(["2026-10"], catchup.season_params({"from": "2026-10", "to": "2026-12"})["start_from"])
        self.assertEqual(["2026-09"], catchup.season_params({"from": "2026-09", "to": "2026-11"})["start_from"])
        for start, end in (("2026-02", "2026-05"), ("2026-10", "2026-11"), ("oops", "2026-12")):
            with self.assertRaises(ValueError):
                catchup.season_params({"from": start, "to": end})

    def test_catchup_requires_explicit_selection_and_preserves_stable_identity_order(self):
        rows = [{"id": 10, "bgm_id": 100}, {"id": 20, "bgm_id": 200}, {"id": 30, "bgm_id": 300}]
        self.assertEqual([rows[2], rows[0]], catchup.selected_rows(rows, [300, 100]))
        # A stale selection must not silently play only the remaining works.
        for selection in (None, [], True, "100", [100, 100], [True], ["100"], [-1], [None], [[100]],
                          [999], [999, 100], list(range(1, 1002))):
            with self.subTest(selection=repr(selection)[:40]), self.assertRaises(ValueError):
                catchup.selected_rows(rows, selection)

    def test_optional_bridge_matches_its_source_and_build_manifest(self):
        folder = Path(__file__).parents[2] / "src/animemachine/resources/potplayer"
        manifest = json.loads((folder / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256((folder / "anm_playback.c").read_bytes()).hexdigest(), manifest["sourceSha256"])
        binary = (folder / "anm_playback.dll").read_bytes()
        self.assertEqual(hashlib.sha256(binary).hexdigest(), manifest["dllSha256"])
        offset = int.from_bytes(binary[60:64], "little")
        self.assertEqual(b"PE\0\0", binary[offset:offset + 4])
        self.assertEqual(0x8664, int.from_bytes(binary[offset + 4:offset + 6], "little"))

    def test_native_events_out_of_order_do_not_rewind_the_same_work(self):
        registry = playback.MediaTokenRegistry()
        locators = [playback.MediaLocator.local(self.path / f"{ep}.mp4") for ep in (1, 2)]
        first, second = registry.issue_many(locators, 900, 900,
            [playback.ViewingBinding(1, 10, ep, 0, 100) for ep in (1, 2)])
        registry.started(second, self.store, native_event=2)
        self.assertFalse(registry.started(first, self.store, native_event=1))
        self.assertEqual(2, self.store.get(1, 10)["episode"])
        self.assertTrue(registry.started(first, self.store, native_event=3))
        self.assertEqual(1, self.store.get(1, 10)["episode"])

    def test_missing_and_sleeping_local_media_are_skipped_before_handoff(self):
        path = self.path / "missing/01.mp4"
        item = playback.PlaybackItem(playback.MediaLocator.local(path, 100), "Ep01", 1.0, 100, "managed")
        groups = [{"animeId": 10, "episodes": [1], "count": 1}]
        cfg = {"deployment": {"libraryUncRoot": str(self.path)}}
        self.assertEqual(([], [], []), catchup.validate([item], [10], groups, cfg))
        with mock.patch.object(catchup, "sleeping_path", return_value=True), \
             mock.patch.object(playback, "authorize_media_path", side_effect=AssertionError("sleeping disk touched")):
            self.assertEqual(([], [], []), catchup.validate([item], [10], groups, cfg))

    def test_mixed_work_playlist_keeps_each_subtitle_inside_its_own_authorization(self):
        cfg = {"deployment": {"libraryUncRoot": str(self.path)}}
        queue = []
        for owner in (10, 20):
            path = self.path / f"{owner}-01.mp4"
            path.write_bytes(b"media")
            path.with_suffix(".srt").write_bytes(b"subtitles")
            queue.append(playback.PlaybackItem(playback.MediaLocator.local(path, 5), str(owner), 1.0, 5, "managed", owner + 1000))
        registry = playback.MediaTokenRegistry()
        body, _ = playback.playlist_payload(self.path / "unused.sqlite3", 0, cfg, registry, "http://localhost",
            items=queue, anime_ids=[10, 20], force_http=True, viewing_store=self.store, user_id=1)
        tokens = [line.split("/media/", 1)[1].split("/", 1)[0] for line in body.decode().splitlines() if line.startswith("#EXTVLCOPT")]
        self.assertEqual([10, 20], [registry.resolve(token).subtitle_anime_id for token in tokens])
        for token in tokens:
            self.assertFalse(registry.started(token, self.store))
        self.assertEqual([], self.store.snapshot(1))


if __name__ == "__main__":
    unittest.main()
