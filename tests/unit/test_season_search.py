import contextlib
import datetime as dt
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from animemachine.integrations import ani_rss
from animemachine.integrations.season_search import SeasonSearch, movie_search_candidates


class SeasonSearchTests(unittest.TestCase):
    def test_movie_discovery_covers_one_year_and_excludes_future_or_older_premieres(self):
        query = mock.Mock(return_value={"items": [
            {"id": index, "media_code": media, "raw_date": date, "start_month": month}
            for index, media, date, month in [
                (1, "movie", "2025-10-09", "2025-10"),
                (2, "movie", "2025-10-08", "2025-10"),
                (3, "movie", "2026-10-10", "2026-10"),
                (4, "movie", "2026-03-01", "2026-03"),
                (5, "tv", "2026-03-01", "2026-03"),
                (6, "movie", "unknown", "2026-09"),
                (7, "movie", "unknown", "2026-00"),
            ]]})
        rows = movie_search_candidates(Path("unused"), query, {}, "en", today=dt.date(2026, 10, 9))
        self.assertEqual([1, 4, 6], [row["id"] for row in rows])
        self.assertEqual(["2025-10"], query.call_args.args[1]["start_from"])
        movie_search_candidates(Path("unused"), query, {}, "en", today=dt.date(2028, 2, 29))
        self.assertEqual(["2027-02"], query.call_args.args[1]["start_from"])

    def test_season_search_discovers_older_movies_once_after_visible_titles(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ani_rss, "state", return_value={
                "connection_state": "ready", "credentialConfigured": True}):
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 3}, {"id": 2}]})
            scan = SeasonSearch(Path(folder) / "catalog.sqlite3", store, query, contextlib.nullcontext)
            with mock.patch("animemachine.integrations.season_search.movie_search_candidates", return_value=[
                    {"id": 1, "media_code": "movie"}, {"id": 2, "media_code": "movie"}]), mock.patch.object(
                    ani_rss, "search", return_value={"found": 1}) as search:
                scan.start("2026-10", "2026-12", "zh-Hans")
                scan.thread.join(5)
            self.assertEqual([3, 2, 1], [call.args[1] for call in search.call_args_list])
            self.assertEqual({"state": "complete", "done": 3, "total": 3, "failed": 0},
                             {key: scan.status()[key] for key in ("state", "done", "total", "failed")})

    def test_database_conflict_is_counted_and_remaining_works_still_finish(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ani_rss, 'state', return_value={'connection_state': 'ready', 'credentialConfigured': True}):
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={'items': [{'id': 1}, {'id': 2}, {'id': 3}]})
            scan = SeasonSearch(Path(folder) / 'catalog.sqlite3', store, query, contextlib.nullcontext)
            with mock.patch.object(ani_rss, 'search', side_effect=[{'found': 1}, sqlite3.IntegrityError('duplicate'), {'found': 1}]) as search:
                scan.start('2026-09', '2026-11', 'zh-Hans')
                scan.thread.join(5)
            self.assertEqual([1, 2, 3], [call.args[1] for call in search.call_args_list])
            self.assertEqual('complete', scan.status()['state'])
            self.assertEqual(3, scan.status()['done'])
            self.assertEqual(1, scan.status()['failed'])
            self.assertEqual([2], scan.status()['failedIds'])
    def test_explicit_sync_precedes_resource_lookup_and_failure_preserves_search_progress(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
                ani_rss, "state", return_value={"connection_state": "ready", "credentialConfigured": True}):
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 1, "title_en": "Work"}]})
            scan = SeasonSearch(Path(folder) / "catalog.sqlite3", store, query, contextlib.nullcontext)
            calls = []
            with mock.patch.object(ani_rss, "sync", side_effect=lambda *a, **k: calls.append("sync") or {"state": "ready"}), mock.patch.object(
                    ani_rss, "search", side_effect=lambda *a: calls.append("search") or {"found": 1}):
                scan.start("2026-09", "2026-11", "en", synchronize=True)
                scan.thread.join(5)
            self.assertEqual(["sync", "search"], calls)
            self.assertEqual("complete", scan.status()["state"])
            with mock.patch.object(ani_rss, "sync", return_value={"state": "error"}), mock.patch.object(ani_rss, "search") as search:
                scan.start("2026-09", "2026-11", "en", synchronize=True)
                scan.thread.join(5)
            search.assert_not_called()
            self.assertEqual("failed", scan.status()["state"])
            self.assertEqual("sync", scan.status()["phase"])

    def test_search_is_sequential_and_duplicate_start_does_not_create_second_worker(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ani_rss, "state", return_value={"connection_state": "ready", "credentialConfigured": True}))
            entered, release = threading.Event(), threading.Event()
            def search(_path, anime_id, _config):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test search timed out")
                return {"found": anime_id}
            remote = stack.enter_context(mock.patch.object(ani_rss, "search", side_effect=search))
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 3}, {"id": 1}, {"id": 2}]})
            scan = SeasonSearch(Path(folder) / "catalog.sqlite3", store, query, contextlib.nullcontext)
            self.addCleanup(scan.close)
            try:
                self.assertTrue(scan.start("2026-09", "2026-11", "zh-Hans"))
                self.assertTrue(entered.wait(5))
                self.assertFalse(scan.start("2026-09", "2026-11", "zh-Hans"))
            finally:
                release.set()
                scan.thread.join(5)
            self.assertEqual([3, 1, 2], [call.args[1] for call in remote.call_args_list])
            self.assertEqual("complete", scan.status()["state"])
            self.assertEqual(3, scan.status()["done"])
            self.assertEqual(["1"], query.call_args.args[1]["radar"])

    def test_restart_resumes_unfinished_ids_and_offline_start_fails_fast(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ani_rss, "state") as state:
            state.return_value = {"connection_state": "ready", "credentialConfigured": True}
            path = Path(folder) / "catalog.sqlite3"
            status = path.parent / ".season-search-state.json"
            status.write_text(json.dumps({"state": "running", "from": "2026-12", "to": "2027-02",
                                          "completedIds": [1], "failed": 0}), encoding="utf-8")
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 1}, {"id": 2}]})
            scan = SeasonSearch(path, store, query, contextlib.nullcontext)
            self.assertEqual("interrupted", scan.status()["state"])
            with mock.patch.object(ani_rss, "search", return_value={"found": 1}) as search:
                scan.start("2026-12", "2027-02", "en")
                scan.thread.join(5)
                self.assertEqual([2], [call.args[1] for call in search.call_args_list])
                self.assertEqual(2, scan.status()["done"])
            state.return_value = {"connection_state": "failed", "credentialConfigured": False}
            with self.assertRaisesRegex(ValueError, "unavailable"):
                scan.start("2026-12", "2027-02", "en")
            self.assertEqual("complete", scan.status()["state"])

    def test_failed_work_is_retried_then_counted_without_stopping_other_works(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ani_rss, "state", return_value={"connection_state": "ready", "credentialConfigured": True}):
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 1}, {"id": 2}, {"id": 3}]})
            scan = SeasonSearch(Path(folder) / "catalog.sqlite3", store, query, contextlib.nullcontext)
            with mock.patch("animemachine.integrations.season_search.RETRY_SECONDS", 0), mock.patch.object(
                    ani_rss, "search", side_effect=[{"found": 1}, RuntimeError("offline"), RuntimeError("offline"), {"found": 1}]) as search:
                scan.start("2026-09", "2026-11", "ja")
                scan.thread.join(5)
            self.assertEqual("complete", scan.status()["state"])
            self.assertEqual(3, scan.status()["done"])
            self.assertEqual(1, scan.status()["failed"])
            self.assertEqual([2], scan.status()["failedIds"])
            self.assertEqual([1, 2, 2, 3], [call.args[1] for call in search.call_args_list])
            self.assertNotIn("completedIds", scan.status())
            with mock.patch.object(ani_rss, "search", return_value={"found": 1}) as search:
                resumed = SeasonSearch(scan.db_path, store, query, contextlib.nullcontext)
                resumed.start("2026-09", "2026-11", "ja")
                resumed.thread.join(5)
                self.assertEqual([2], [call.args[1] for call in search.call_args_list])
                self.assertEqual(0, resumed.status()["failed"])
                self.assertEqual(3, resumed.status()["done"])

    def test_transient_failure_recovers_and_failed_job_keeps_its_cursor(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ani_rss, "state", return_value={"connection_state": "ready", "credentialConfigured": True}):
            path = Path(folder) / "catalog.sqlite3"
            path.parent.joinpath(".season-search-state.json").write_text(json.dumps({
                "state": "failed", "from": "2026-09", "to": "2026-11", "completedIds": [1], "failed": 0,
            }), encoding="utf-8")
            store = mock.Mock()
            store.read.return_value = {}
            query = mock.Mock(return_value={"items": [{"id": 1}, {"id": 2}]})
            scan = SeasonSearch(path, store, query, contextlib.nullcontext)
            with mock.patch("animemachine.integrations.season_search.RETRY_SECONDS", 0), mock.patch.object(
                    ani_rss, "search", side_effect=[RuntimeError("temporary"), {"found": 1}]) as search:
                scan.start("2026-09", "2026-11", "en")
                scan.thread.join(5)
            self.assertEqual([2, 2], [call.args[1] for call in search.call_args_list])
            self.assertEqual({"state": "complete", "done": 2, "failed": 0},
                             {key: scan.status()[key] for key in ("state", "done", "failed")})
