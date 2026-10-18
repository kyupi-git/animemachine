from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from animemachine.catalog import service
from animemachine.library import preview
from animemachine.torrents import runtime


class LibraryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "catalog.sqlite3"
        shutil.copyfile(Path(__file__).parents[1] / "fixtures/anime-catalog.sqlite3", self.database)
        service.ensure_catalog_features(self.database)

    def test_japanese_edition_title_is_projected_without_changing_primary_and_upgrade_is_once(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE anime_work SET title_ja='도굴왕',original_language='ko' WHERE id=1")
            db.execute("DELETE FROM anime_title WHERE anime_id=1")
            db.execute("INSERT OR IGNORE INTO anime_title VALUES(1,'ja','盗掘王','alias','bangumi-archive')")
            db.execute("UPDATE metadata SET value='16' WHERE key='feature_schema_version'")
        with mock.patch.object(service, "rebuild_theme_clusters", side_effect=AssertionError("expensive theme migration repeated")):
            service.ensure_catalog_features(self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(("도굴왕", "盗掘王"), db.execute("SELECT title_ja,directory_title FROM anime_work WHERE id=1").fetchone())
        with mock.patch.object(service, "migrate_catalog_features", side_effect=AssertionError("migration repeated")):
            service.ensure_catalog_features(self.database)

    def test_companion_short_requires_description_and_explicit_main_story(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM anime_relation WHERE anime_id IN (1,2,3)")
            db.execute("UPDATE anime_work SET title_ja='番外▽',media_code='web',summary='本編終了後に、ショートアニメが放送します。' WHERE id IN (1,2)")
            owner = db.execute("SELECT bgm_id FROM anime_work WHERE id=3").fetchone()[0]
            db.execute("INSERT INTO anime_relation(anime_id,related_bgm_id,related_title,relation_type,relation_code,strict_group,source) VALUES(1,?,'本編','本篇','main_story',0,'test')", (owner,))
            service.rebuild_physical_layout(db)
            self.assertEqual(("supplement", 3), db.execute("SELECT physical_role,physical_owner_anime_id FROM anime_work WHERE id=1").fetchone())
            self.assertEqual(("work", None), db.execute("SELECT physical_role,physical_owner_anime_id FROM anime_work WHERE id=2").fetchone())

    def test_preview_proposes_width_correction_and_preserves_files(self):
        library = self.root / "library"
        old = library / "『2022_01』『CUE!』"
        old.mkdir(parents=True)
        media = old / "episode.mkv"
        media.write_bytes(b"existing-media")
        stamp = media.stat().st_mtime_ns
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            runtime.migrate_overlay(db)
            db.execute("UPDATE anime_work SET title_ja='CUE!',directory_title='CUE!',directory_date='2022_01' WHERE id=1")
            bgm_id = db.execute("SELECT bgm_id FROM anime_work WHERE id=1").fetchone()[0]
            db.execute("""INSERT INTO runtime_work VALUES(1,1,?,NULL,?,'CUE!','2022_01',NULL,'existing','active',
                'standalone','test','exact',?,'2026-10-18')""", (str(old), old.name, json.dumps({"bangumiSubjectId": bgm_id})))
        config = json.loads(service.EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        config["deployment"]["libraryUncRoot"] = str(library)
        result = preview.inspect(self.database, config)
        self.assertEqual(1, result["count"])
        self.assertEqual(str(library / "『2022_01』『CUE！』"), result["items"][0]["proposed"])
        self.assertFalse(result["items"][0]["conflict"])
        self.assertEqual(b"existing-media", media.read_bytes())
        self.assertEqual(stamp, media.stat().st_mtime_ns)
        self.assertEqual([old], list(library.iterdir()))


if __name__ == "__main__":
    unittest.main()
