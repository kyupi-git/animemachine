from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from animemachine.torrents import search, scanner, collector
from animemachine.torrents.title_identity import queries


def synthetic_torrent(name: str) -> bytes:
    value = name.encode("utf-8")
    return b"d4:infod6:lengthi1024e4:name" + str(len(value)).encode() + b":" + value + b"12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"


class TorrentSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "catalog.sqlite3"
        shutil.copyfile(Path(__file__).parents[1] / "fixtures/anime-catalog.sqlite3", self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.anime_id = db.execute("SELECT id FROM anime_work WHERE bgm_id=265").fetchone()[0]

    def test_one_source_failure_does_not_hide_exact_candidates_and_repeat_does_not_rewrite(self):
        rows = [{"title": "[ANi] 新世紀エヴァンゲリオン - 01 [1080p][WEB-DL]", "download_url": "https://example.test/one"},
                {"title": "[ANi] Different Work - 01 [1080p][WEB-DL]", "download_url": "https://example.test/other"}]
        def healthy(term, page):
            return rows, False
        def broken(term, page):
            raise OSError("offline")
        download = mock.Mock(return_value=(synthetic_torrent("Show - 01.mkv"), "https://example.test/one"))
        cache = self.root / "cache"
        result = search.discover(self.database, self.anime_id, cache,
                                 sources={"healthy": healthy, "broken": broken}, download=download)
        self.assertEqual(1, result["saved"])
        self.assertEqual(1, result["sourcesFailed"])
        download.assert_called_once()
        path = next(cache.glob("*.torrent"))
        stamp = path.stat().st_mtime_ns
        search.discover(self.database, self.anime_id, cache, sources={"healthy": healthy}, download=download)
        self.assertEqual(stamp, path.stat().st_mtime_ns)
        self.assertEqual(["cache", "catalog.sqlite3"], sorted(path.name for path in self.root.iterdir()))

    def test_invalid_torrent_response_is_not_published(self):
        def source(term, page):
            return [{"title": "[ANi] 新世紀エヴァンゲリオン - 01 [1080p][WEB-DL]", "download_url": "https://example.test/html"}], False
        cache = self.root / "cache"
        result = search.discover(self.database, self.anime_id, cache, sources={"source": source},
                                 download=lambda *_: (b"<!doctype html>blocked", "https://example.test/html"))
        self.assertEqual(0, result["saved"])
        self.assertGreater(result["failed"], 0)
        self.assertEqual([], list(cache.iterdir()))

    def test_short_formal_titles_are_kept_as_exact_queries(self):
        self.assertIn("TO", queries("[Group] TO [BDRip 1080p]"))
        self.assertIn("ギョ", queries("[Group] ギョ [BDRip 1080p]"))
        self.assertIn("龙珠超 比鲁斯", queries("【DBFC字幕组】【龙珠超 比鲁斯】【001】【中日双语字幕】【1080P】【MP4】"))
        self.assertNotIn("001", queries("【Group】【Title】【001】"))
        self.assertIn("画完这个就去死", queries("[MingY] 画完这个就去死 / Kore Kaite Shine [12][1080p]", partial=True))

    def test_mikan_title_search_follows_public_rss_and_preserves_selected_origin(self):
        page = '<a href="/Home/Bangumi/123">Title</a><a href="/Home/Bangumi/123">Title</a>'
        rss = b'<rss><channel><item><title>Title - 01</title><link>/Home/Episode/abc</link><enclosure url="/Download/abc.torrent"/></item></channel></rss>'
        with mock.patch.object(collector, "get_text", return_value=(page, "https://mikan.sakiko.de/Home/Search?searchstr=Title")), \
             mock.patch.object(collector, "http_request", return_value=(rss, "application/rss+xml", "https://mikan.sakiko.de/RSS/Bangumi?bangumiId=123")) as fetch:
            rows, _ = collector.fetch_mikan("Title", 1)
        self.assertEqual(1, len(rows))
        self.assertEqual("https://mikan.sakiko.de/Download/abc.torrent", rows[0]["download_url"])
        fetch.assert_called_once_with("https://mikan.sakiko.de/RSS/Bangumi?bangumiId=123")

    def test_scanning_one_root_does_not_mark_other_indexed_roots_missing(self):
        config = Path(__file__).parents[2] / "config/config.example.json"
        database = self.root / "runtime.sqlite3"
        first, second = self.root / "pool", self.root / "cache"
        for root, title in ((first, "First"), (second, "Second")):
            root.mkdir()
            (root / f"{title}.torrent").write_bytes(synthetic_torrent(f"{title}.mkv"))
        for root in (first, second, first):
            arguments = ["scanner", str(root), "--db", str(database), "--config", str(config),
                         "--queue-output", str(self.root / "queue.json")]
            with mock.patch.object(sys, "argv", arguments):
                self.assertEqual(0, scanner.main())
        with contextlib.closing(sqlite3.connect(database)) as db:
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM torrent_source WHERE presence_state='present'").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM torrent WHERE metadata_state='missing'").fetchone()[0])
        (first / "First.torrent").unlink()
        with mock.patch.object(sys, "argv", ["scanner", str(first), "--db", str(database), "--config", str(config),
                                           "--queue-output", str(self.root / "queue.json")]):
            self.assertEqual(0, scanner.main())
        with contextlib.closing(sqlite3.connect(database)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM torrent_source WHERE presence_state='present'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM torrent WHERE metadata_state='missing'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
