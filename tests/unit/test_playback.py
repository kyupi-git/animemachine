from __future__ import annotations

import unittest
import contextlib
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from animemachine.integrations import playback


class PlaybackTokenTests(unittest.TestCase):
    def test_episode_request_renews_whole_playlist_until_hard_expiry(self) -> None:
        registry = playback.MediaTokenRegistry()
        with mock.patch.object(playback.time, "time", return_value=1000):
            first, second = registry.issue_many([Path("one.mkv"), Path("two.mkv")], 60, 180)
        with mock.patch.object(playback.time, "time", return_value=1050):
            self.assertEqual(Path("one.mkv"), registry.resolve(first))
        with mock.patch.object(playback.time, "time", return_value=1105):
            self.assertEqual(Path("two.mkv"), registry.resolve(second))
        with mock.patch.object(playback.time, "time", return_value=1181):
            self.assertIsNone(registry.resolve(first))

    def test_episode_labels_are_player_friendly(self) -> None:
        self.assertEqual("Ep01", playback._episode_label(1.0))
        self.assertEqual("Ep1.5", playback._episode_label(1.5))

    def test_bare_title_number_outside_known_run_is_not_an_episode(self) -> None:
        path = Path("靠死亡游戏混饭吃。 44 - CLOUDY BEACH.mkv")
        self.assertIsNone(playback._episode(path, expected_count=12))
        self.assertEqual(44.0, playback._episode(Path("Show - Ep44.mkv"), expected_count=12))
        self.assertEqual(3.0, playback._episode(Path("Show - 03.mkv"), expected_count=12))

    def test_numbered_menus_and_bonus_videos_are_not_main_episodes(self) -> None:
        for name in ("Menu01_1.mkv", "[Menu01_1].mkv", "[NCOP01].mkv", "NCED02.mkv", "PV03.mp4", "CM01.mkv",
                     "Show [SP01].mkv", "Show [Mini Character Anime 01].mkv", "Show Picture Drama 02.mkv"):
            self.assertFalse(playback._is_main_video(Path(name)), name)
        self.assertTrue(playback._is_main_video(Path("Show - 01.mkv")))

    def test_default_queue_keeps_one_best_copy_per_episode(self) -> None:
        root = Path("library")
        items = [
            playback.PlaybackItem(playback.MediaLocator.local(root / "Group A - 01.mkv", 100), "A", 1.0, 100, "preexisting"),
            playback.PlaybackItem(playback.MediaLocator.local(root / "Group B - 01.mkv", 120), "B", 1.0, 120, "preexisting"),
            playback.PlaybackItem(playback.MediaLocator.local(root / "Group B - 02.mkv", 110), "C", 2.0, 110, "preexisting"),
            playback.PlaybackItem(playback.MediaLocator.local(root / "Copy - 02.mkv", 110), "D", 2.0, 110, "preexisting"),
        ]
        selected = playback._default_main_queue(items)
        self.assertEqual(["Group B - 01.mkv", "Group B - 02.mkv"], [item.name for item in selected])

    def test_default_queue_keeps_distinct_episodes_with_identical_file_sizes(self) -> None:
        root = Path("library")
        items = [
            playback.PlaybackItem(playback.MediaLocator.local(root / "Show - 01.mkv", 100), "Ep01", 1.0, 100, "preexisting"),
            playback.PlaybackItem(playback.MediaLocator.local(root / "Show - 02.mkv", 100), "Ep02", 2.0, 100, "preexisting"),
        ]
        selected = playback._default_main_queue(items)
        self.assertEqual(["Show - 01.mkv", "Show - 02.mkv"], [item.name for item in selected])

    def test_bracketed_media_name_finds_language_sidecar_without_glob_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            media = Path(raw) / "[Group] Show [01].mkv"
            subtitle = Path(raw) / "[Group] Show [01].sc.ass"
            media.write_bytes(b"video")
            subtitle.write_bytes(b"subtitle")
            item = playback.PlaybackItem(playback.MediaLocator.local(media, 5), media.stem, 1.0, 5, "preexisting")
            self.assertEqual(subtitle, playback._subtitle_for(item, 1))

    def test_selected_library_source_limits_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); first = root / "first"; second = root / "second"
            first.mkdir(); second.mkdir()
            (first / "Show - 01.mkv").write_bytes(b"a")
            (second / "Show - 02.mkv").write_bytes(b"b")
            db_path = root / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,episode_count INTEGER)")
                db.execute("CREATE TABLE runtime_work(anime_id INTEGER,target_unc TEXT,library_state TEXT)")
                db.execute("INSERT INTO anime_work VALUES(1,'Show',2)")
                db.executemany("INSERT INTO runtime_work VALUES(1,?,'existing')", [(str(first),), (str(second),)])
            config = {"deployment": {"libraryUncRoot": str(root)}, "externalLibraries": []}
            items = playback.collect_items(db_path, 1, config, source=str(second))
            self.assertEqual(["Show - 02.mkv"], [item.path.name for item in items])

    def test_playlist_start_keeps_every_episode_and_rotates_selected_first(self) -> None:
        items = [
            playback.PlaybackItem(
                playback.MediaLocator.remote(
                    f"Show - {index:02d}.mkv", f"Show - {index:02d}.mkv", "mkv", index, "ani-rss-test"
                ),
                f"Ep{index:02d}",
                float(index),
                index,
                "ani-rss",
            )
            for index in range(1, 4)
        ]
        body, ordered = playback.playlist_payload(
            Path("unused.sqlite3"),
            1,
            {"playback": {}},
            playback.MediaTokenRegistry(),
            "http://127.0.0.1:8877",
            start=2,
            force_http=True,
            items=items,
        )
        text = body.decode("utf-8")
        self.assertEqual(["Ep02", "Ep03", "Ep01"], [item.title for item in ordered])
        self.assertEqual(3, text.count("#EXTINF:"))
        self.assertLess(text.index("Ep02"), text.index("Ep03"))
        self.assertLess(text.index("Ep03"), text.index("Ep01"))

    def test_vlc_and_potplayer_handoffs_use_synchronized_native_urls(self) -> None:
        target = "http://127.0.0.1:8877/api/playback/playlist/token/AnimeMachine-1.m3u"
        self.assertEqual(f"vlc://{target}", playback.player_protocol_url("vlc", target))
        self.assertEqual(f"potplayer://{target}", playback.player_protocol_url("potplayer", target))
        self.assertTrue(playback.player_protocol_url("iina", target).startswith("iina://weblink?url="))
        with self.assertRaises(ValueError):
            playback.player_protocol_url("vlc", "file:///private/video.mkv")

    def test_playback_diagnostics_tracks_range_resume_upstream_and_rate(self) -> None:
        diagnostics = playback.PlaybackDiagnostics(maximum=4)
        locator = playback.MediaLocator.remote("Show - 01.mkv", "Show - 01.mkv", "mkv", 4096, "ani-rss")
        with mock.patch.object(playback.time, "time", side_effect=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0]):
            diagnostics.begin("token-value", locator, "bytes=1024-")
            diagnostics.upstream("token-value", "HTTP 206")
            diagnostics.transfer("token-value", 2048)
            diagnostics.resume("token-value")
            diagnostics.finish("token-value", "complete")
        item = diagnostics.snapshot()[0]
        self.assertEqual("bytes=1024-", item["range"])
        self.assertEqual(2, item["resumeCount"])
        self.assertEqual("HTTP 206", item["upstream"])
        self.assertGreater(item["rateBps"], 0)
        self.assertEqual("complete", item["state"])
        self.assertNotIn("token-value", item["token"])


if __name__ == "__main__":
    unittest.main()

