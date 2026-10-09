from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.integrations import ani_rss
from animemachine.library import layout, placeholders
from animemachine.storage import StorageUnavailableError
from animemachine.torrents import runtime, scanner


PROJECT = Path(__file__).resolve().parents[2]


class LibraryPlaceholderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.library = self.root / "Library 空目录 $ %"
        self.library.mkdir()
        self.metadata = self.root / "metadata.sqlite3"
        self.operational = self.root / "runtime.sqlite3"
        shutil.copy2(PROJECT / "tests/fixtures/anime-catalog.sqlite3", self.metadata)
        self.config = json.loads((PROJECT / "config/config.example.json").read_text(encoding="utf-8"))
        self.config["deployment"]["libraryUncRoot"] = str(self.library)
        with contextlib.closing(sqlite3.connect(self.metadata)) as db, db:
            runtime.migrate_overlay(db)
            ani_rss.migrate(db)
            self.anime_id, self.bgm_id = db.execute("SELECT id,bgm_id FROM anime_work WHERE title_ja='新世紀エヴァンゲリオン'").fetchone()
            db.execute("""INSERT INTO ani_rss_subscription(remote_id,anime_id,title,bgm_id,enabled,subscription_kind,
                remote_state,first_seen_at,last_seen_at,evidence_json) VALUES('test',?, 'Test',?,1,'follow','subscribed','now','now','{}')""",
                (self.anime_id, self.bgm_id))

    def run_create(self, **kwargs):
        return placeholders.create(self.metadata, self.operational, self.config, **kwargs)

    def target(self):
        with contextlib.closing(sqlite3.connect(self.operational)) as db:
            return Path(db.execute("SELECT target_unc FROM anime_work").fetchone()[0])

    def test_creation_is_idempotent_and_survives_archive_rename(self):
        first = self.run_create()
        target = self.target()
        self.assertEqual((first["created"], first["total"]), (1, 1))
        self.assertTrue(target.is_dir())
        (target / "User.txt").write_text("keep", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute("UPDATE anime_work SET title_ja='新しい名称',start_month='1996-01' WHERE id=?", (self.anime_id,))
        second = self.run_create()
        self.assertEqual((second["created"], second["existing"]), (0, 1))
        self.assertEqual(self.target(), target)
        self.assertEqual((target / "User.txt").read_text(), "keep")
        runtime.sync_overlay(self.metadata, self.operational)
        with contextlib.closing(sqlite3.connect(self.metadata)) as db:
            self.assertEqual(db.execute("SELECT library_state FROM runtime_work").fetchone()[0], "placeholder")
            self.assertEqual(db.execute("SELECT state FROM library_history_event").fetchone()[0], "applied")

    def test_existing_media_is_reused_and_not_overwritten(self):
        target = self.library / "『1995_10』『新世紀エヴァンゲリオン』"
        target.mkdir()
        media = target / "Episode 01.mkv"
        media.write_bytes(b"user media")
        result = self.run_create()
        self.assertEqual((result["created"], result["existing"]), (0, 1))
        self.assertEqual(self.target(), target)
        self.assertEqual(media.read_bytes(), b"user media")
        with contextlib.closing(sqlite3.connect(self.metadata)) as db:
            self.assertEqual(db.execute("SELECT library_state FROM runtime_work").fetchone()[0], "existing")

    def test_missing_mount_is_not_created(self):
        missing = self.root / "Not mounted"
        self.config["deployment"]["libraryUncRoot"] = str(missing)
        with self.assertRaises(StorageUnavailableError):
            self.run_create()
        self.assertFalse(missing.exists())

    def test_duplicate_subscriptions_share_one_physical_target(self):
        with contextlib.closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute("INSERT INTO runtime_watch(anime_id,source_info_hash,release_unit,created_at,updated_at) VALUES(?,'synthetic','episode','now','now')", (self.anime_id,))
        result = self.run_create()
        self.assertEqual((result["created"], result["total"]), (1, 1))

    def test_ambiguous_paths_are_skipped(self):
        for series in ("『1995_10－2000_01』『「Series A」シリーズ』", "『1995_10－2000_01』『「Series B」シリーズ』"):
            (self.library / series / "『1995_10』『新世紀エヴァンゲリオン』").mkdir(parents=True)
        result = self.run_create()
        self.assertEqual((result["created"], result["skipped"]), (0, 1))

    def test_target_escape_and_links_are_rejected(self):
        with self.assertRaises(ValueError):
            placeholders._guard(self.library, self.root / "outside")
        with self.assertRaises(ValueError):
            placeholders._guard(self.library, self.library / ".." / "escape")
        with mock.patch.object(Path, "is_junction", return_value=True):
            with self.assertRaises(ValueError):
                placeholders._guard(self.library, self.library / "linked" / "child")

    def test_existing_path_index_skips_linked_top_directories(self):
        (self.library / "『1995_10』『新世紀エヴァンゲリオン』").mkdir()
        with mock.patch.object(Path, "is_junction", return_value=True):
            self.assertEqual(layout.ExistingPathIndex(self.library).rows, [])

    def test_database_failure_rolls_back_only_new_empty_directories(self):
        real = scanner.connect(self.operational)
        wrapped = mock.Mock(wraps=real)
        def execute(statement, *args):
            if statement.startswith("INSERT INTO anime_work("):
                raise sqlite3.OperationalError("synthetic registration failure")
            return real.execute(statement, *args)
        wrapped.execute.side_effect = execute
        with mock.patch.object(scanner, "connect", return_value=wrapped):
            result = self.run_create()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(list(self.library.iterdir()), [])
        with contextlib.closing(sqlite3.connect(self.metadata)) as db:
            self.assertEqual(db.execute("SELECT state FROM library_history_event").fetchone()[0], "failed")

    def test_native_subscription_works_without_ani_rss(self):
        with contextlib.closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute("DELETE FROM ani_rss_subscription")
            db.execute("INSERT INTO runtime_watch(anime_id,source_info_hash,release_unit,created_at,updated_at) VALUES(?,'native','episode','now','now')", (self.anime_id,))
        self.config["components"]["aniRss"]["mode"] = "manual"
        result = self.run_create()
        self.assertEqual((result["created"], result["total"]), (1, 1))

    def test_interrupted_creation_reuses_folder_and_finishes_history(self):
        target = self.library / "『1995_10』『新世紀エヴァンゲリオン』"
        target.mkdir()
        with contextlib.closing(sqlite3.connect(self.metadata)) as db, db:
            from animemachine.library import history
            history.migrate(db)
            db.execute("INSERT INTO library_history_event(transaction_id,operation,target_path,object_kind,product_created,state,details_json,created_at) VALUES('interrupted','create_directory',?,'directory',1,'planned','{}','now')", (str(target),))
        result = self.run_create()
        self.assertEqual((result["created"], result["existing"]), (0, 1))
        with contextlib.closing(sqlite3.connect(self.metadata)) as db:
            self.assertEqual(db.execute("SELECT state FROM library_history_event").fetchone()[0], "applied")
