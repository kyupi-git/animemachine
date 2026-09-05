import sqlite3
import contextlib
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from animemachine.library import external as external_library


class ExternalLibraryTests(unittest.TestCase):
    def test_ani_rss_scan_is_read_only_and_maps_exact_title(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "番剧" / "Y" / "夜曲 第二季 (2025)"
            media.mkdir(parents=True)
            video = media / "[Group] 夜曲 第二季 (2025) S02E13.mp4"
            video.write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'夜曲2','夜曲 第二季',NULL,'2025-07','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'夜曲 第二季')")
                db.commit()
            result = external_library.scan(db_path, [{"id":"ani-rss","kind":"ani-rss","enabled":True,
                                                      "path":str(root / 'media'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)
            self.assertEqual(video.read_bytes(), b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.row_factory = sqlite3.Row
                current = external_library.status(db, 1)
            self.assertEqual(current["state"], "external")
            self.assertEqual(current["observedEpisodes"], [13])
            self.assertEqual(str(media), current["targets"][0]["path"])
            self.assertEqual(1, current["targets"][0]["fileCount"])
            self.assertNotEqual(str(video), current["targets"][0]["path"])
            video.unlink()
            self.assertFalse(video.exists())

    def test_modern_ani_rss_layout_uses_title_before_season_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "番剧" / "正相反的你与我 (2026)" / "Season 2"
            media.mkdir(parents=True)
            (media / "[Group] S02E13.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(25547,'正反対な君と僕 第2期','正相反的你与我 第二季',NULL,'2026-07','tv')")
                db.execute("INSERT INTO anime_title VALUES(25547,'正相反的你与我 第二季')")
                db.commit()
            result = external_library.scan(db_path, [{"id":"ani-rss","kind":"ani-rss","enabled":True,
                                                      "path":str(root / 'media'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(25547, db.execute("SELECT anime_id FROM external_media_file").fetchone()[0])

    def test_subscription_identity_disambiguates_same_year_seasons(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "番剧" / "正相反的你与我 (2026)" / "Season 2"
            media.mkdir(parents=True)
            (media / "S02E13.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);
                    CREATE TABLE ani_rss_subscription(remote_id TEXT,anime_id INTEGER,title TEXT,deleted_at TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'正反対な君と僕','相反的你和我',NULL,'2026-01','tv')")
                db.execute("INSERT INTO anime_work VALUES(2,'正反対な君と僕 第2期','相反的你和我 第二季',NULL,'2026-07','tv')")
                db.executemany("INSERT INTO anime_title VALUES(?,?)", [(1, "正相反的你与我"), (2, "相反的你和我 第二季")])
                db.execute("INSERT INTO ani_rss_subscription VALUES('x',2,'正相反的你与我 (2026)',NULL)")
                db.commit()
            result = external_library.scan(db_path, [{"id":"ani-rss","kind":"ani-rss","enabled":True,
                                                      "path":str(root / 'media'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(2, db.execute("SELECT anime_id FROM external_media_file").fetchone()[0])

    def test_scan_interval_defers_before_probing_storage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "作品名"
            media.mkdir(parents=True)
            (media / "作品名 - EP01.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'作品名',NULL,NULL,'2026-01','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'作品名')")
                db.commit()
            source = {"id":"ani-rss","kind":"ani-rss","enabled":True,
                      "path":str(root / "media"),"readOnly":True,"scanMinutes":60}
            first = external_library.scan(db_path, [source])
            self.assertEqual(1, first["sources"])
            with mock.patch.object(external_library, "status_for_path", side_effect=AssertionError("storage probe should be deferred")):
                second = external_library.scan(db_path, [source])
            self.assertEqual(0, second["sources"])
            self.assertEqual(1, second["deferred"])

    def test_changed_source_path_bypasses_scan_interval(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            first_media = root / "media-a" / "作品一"
            second_media = root / "media-b" / "作品二"
            first_media.mkdir(parents=True)
            second_media.mkdir(parents=True)
            (first_media / "作品一 - EP01.mkv").write_bytes(b"first")
            (second_media / "作品二 - EP01.mkv").write_bytes(b"second")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.executemany("INSERT INTO anime_work VALUES(?,?,NULL,NULL,'2026-01','tv')", [(1, '作品一'), (2, '作品二')])
                db.executemany("INSERT INTO anime_title VALUES(?,?)", [(1, '作品一'), (2, '作品二')])
                db.commit()
            source = {"id":"ani-rss","kind":"ani-rss","enabled":True,
                      "path":str(root / "media-a"),"readOnly":True,"scanMinutes":60}
            self.assertEqual(1, external_library.scan(db_path, [source])["sources"])
            source["path"] = str(root / "media-b")
            second = external_library.scan(db_path, [source])
            self.assertEqual(1, second["sources"])
            self.assertEqual(0, second["deferred"])
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                rows = db.execute("SELECT anime_id,absolute_path FROM external_media_file WHERE source_id='ani-rss'").fetchall()
            self.assertEqual([(2, str((second_media / '作品二 - EP01.mkv').absolute()))], rows)

    def test_future_scan_timestamp_does_not_defer_indefinitely(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "作品名"
            media.mkdir(parents=True)
            (media / "作品名 - EP01.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'作品名',NULL,NULL,'2026-01','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'作品名')")
                db.commit()
            source = {"id":"ani-rss","kind":"ani-rss","enabled":True,
                      "path":str(root / "media"),"readOnly":True,"scanMinutes":60}
            external_library.scan(db_path, [source])
            with contextlib.closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE external_library_source SET last_scan_at='2099-01-01T00:00:00+00:00' WHERE source_id='ani-rss'")
            original = external_library.status_for_path
            with mock.patch.object(external_library, "status_for_path", wraps=original) as probe:
                second = external_library.scan(db_path, [source])
            self.assertEqual(1, second["sources"])
            probe.assert_called_once()

    def test_overlapping_external_scan_is_skipped_instead_of_queued(self):
        source = {"id": "ani-rss", "kind": "ani-rss", "enabled": True,
                  "path": "/unused", "readOnly": True}
        lock = external_library._source_scan_lock("ani-rss")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            result = external_library.scan(Path("/unused/catalog.sqlite3"), [source])
        finally:
            lock.release()
        self.assertEqual(0, result["sources"])
        self.assertEqual(1, result["deferred"])

    def test_busy_source_does_not_block_independent_external_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "other" / "作品名"
            media.mkdir(parents=True)
            (media / "作品名 - EP01.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'作品名',NULL,NULL,'2026-01','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'作品名')")
                db.commit()
            busy = external_library._source_scan_lock("ani-rss-media")
            self.assertTrue(busy.acquire(blocking=False))
            try:
                result = external_library.scan(db_path, [{
                    "id": "other", "kind": "generic", "enabled": True,
                    "path": str(root / "other"), "readOnly": True,
                }])
            finally:
                busy.release()
            self.assertEqual(1, result["sources"])
            self.assertEqual(0, result["deferred"])
            self.assertEqual(1, result["verified"])

    def test_writable_external_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("CREATE TABLE anime_work(id INTEGER,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT); CREATE TABLE anime_title(anime_id INTEGER,title TEXT);")
            with self.assertRaises(ValueError):
                external_library.scan(db_path, [{"id":"x","kind":"generic","enabled":True,"path":raw,"readOnly":False}])

    def test_regional_title_variant_uses_conservative_year_media_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "番剧" / "Y" / "欢迎来到实力至上主义教室 第四季 (2026)"
            media.mkdir(parents=True)
            (media / "S04E01.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'4th','欢迎来到实力至上主义的教室 第四季',NULL,'2026-04','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'欢迎来到实力至上主义的教室 第四季')")
                db.execute("INSERT INTO anime_work VALUES(2,'Other','完全不同的动画标题',NULL,'2026-04','tv')")
                db.execute("INSERT INTO anime_title VALUES(2,'完全不同的动画标题')")
                db.commit()
            result = external_library.scan(db_path, [{"id":"ani-rss","kind":"ani-rss","enabled":True,
                                                      "path":str(root / 'media'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)

    def test_wrong_franchise_year_requires_very_strong_unique_title(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "media" / "番剧" / "Y" / "欢迎来到实力至上主义教室 第四季 (2017)"
            media.mkdir(parents=True)
            (media / "S04E01.mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                for anime_id, season, year in ((1, "第一季", "2017-07"), (4, "第四季", "2026-04")):
                    title = f"欢迎来到实力至上主义的教室 {season}"
                    db.execute("INSERT INTO anime_work VALUES(?,?,?,NULL,?,'tv')", (anime_id, title, title, year))
                    db.execute("INSERT INTO anime_title VALUES(?,?)", (anime_id, title))
                db.commit()
            result = external_library.scan(db_path, [{"id":"ani-rss","kind":"ani-rss","enabled":True,
                                                      "path":str(root / 'media'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)

    def test_generic_dirty_tree_uses_filename_when_ancestors_are_technical(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db_path = root / "catalog.sqlite3"
            media = root / "incoming" / "misc" / "Season 1"
            media.mkdir(parents=True)
            (media / "[Group] 作品名 - EP01 [1080p].mkv").write_bytes(b"media")
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.executescript("""CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_zh_hans TEXT,title_en TEXT,start_month TEXT,media_code TEXT);
                    CREATE TABLE anime_title(anime_id INTEGER,title TEXT);""")
                db.execute("INSERT INTO anime_work VALUES(1,'作品名',NULL,NULL,'2025-01','tv')")
                db.execute("INSERT INTO anime_title VALUES(1,'作品名')")
                db.commit()
            result = external_library.scan(db_path, [{"id":"generic","kind":"generic","enabled":True,
                                                      "path":str(root / 'incoming'),"readOnly":True}])
            self.assertEqual(result["verified"], 1)


if __name__ == "__main__":
    unittest.main()

