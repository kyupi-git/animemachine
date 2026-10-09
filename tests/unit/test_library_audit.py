import unittest
import tempfile
import contextlib
import hashlib
import sqlite3
from unittest import mock
from pathlib import Path

from animemachine.library import audit as library_audit
from animemachine.torrents import runtime


class LibraryAuditTests(unittest.TestCase):
    def test_distribution_similarity_uses_kind_episode_and_size(self):
        expected = [library_audit._signature("Episode 01.mkv", 1000, "main_video"),
                    library_audit._signature("Scans/a.png", 100, "scans")]
        same = [library_audit._signature("Show - 01.mkv", 990, "main_video"),
                library_audit._signature("Booklet/a.png", 99, "scans")]
        partial = [library_audit._signature("Show - 01.mkv", 990, "main_video")]
        self.assertGreaterEqual(library_audit.distribution_similarity(expected, same), 95)
        self.assertGreater(library_audit.distribution_similarity(expected, partial), 80)

    def test_threshold_states(self):
        self.assertEqual(library_audit._state(95), "complete")
        self.assertEqual(library_audit._state(80), "near_complete")
        self.assertEqual(library_audit._state(60), "partial_high")
        self.assertEqual(library_audit._state(30), "partial")
        self.assertEqual(library_audit._state(29.9), "incomplete")

    def test_signature_keeps_korean_and_cyrillic_title_tokens(self):
        korean = library_audit._signature("도굴왕 01.mkv", 1000, "main_video")
        russian = library_audit._signature("Смешарики 01.mkv", 1000, "main_video")
        self.assertIn("도굴왕", korean["tokens"])
        self.assertIn("смешарики", russian["tokens"])

    def test_observed_reuses_physical_root_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "Episode 01.mkv"
            media.write_bytes(b"first")
            cache = {}
            first = library_audit._observed([root], cache)
            media.unlink()
            second = library_audit._observed([root], cache)
        self.assertEqual(first, second)
        self.assertEqual(1, len(second))

    def test_explicit_empty_scope_does_not_audit_everything(self):
        with tempfile.TemporaryDirectory() as folder:
            result = library_audit.audit(Path(folder) / "catalog.sqlite3", {}, anime_ids=[])
        self.assertEqual(result["total"], 0)

    def test_hash_verification_rejects_file_outside_owner_path(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.closing(sqlite3.connect(":memory:")) as db:
            runtime.migrate_overlay(db)
            owner = Path(folder) / "owner"
            owner.mkdir()
            outside = Path(folder) / "outside.mkv"
            outside.write_bytes(b"private")
            db.execute("INSERT INTO runtime_asset(final_path,owner_path,bytes,sha256,replacement_state,evidence_json,verified_at) VALUES(?,?,?,?,'current','{}','now')",
                       (str(outside), str(owner), 7, hashlib.sha256(b"private").hexdigest()))
            result = library_audit._verify_hash_baselines(db, [str(owner)])
        self.assertEqual((result["compared"], result["unavailable"]), (0, 1))

    def test_completed_download_does_not_hide_missing_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            owner = Path(folder) / "media"
            owner.mkdir()
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE anime_work(id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO anime_work VALUES(1)")
                runtime.migrate_overlay(db)
                db.execute("INSERT INTO runtime_work(private_work_id,anime_id,target_unc,directory_name,official_title,date_code,library_state,scope_state,relation_state,origin,mapping_method,evidence_json,updated_at) VALUES(1,1,?,'work','work','2026_10','existing','active','standalone','managed_submission','test','{}','now')", (str(owner),))
                db.execute("INSERT INTO runtime_submission VALUES('test','path','test','[]','completed','now',1)")
            expected = [library_audit._signature("Episode 01.mkv", 1000, "main_video")]
            progress = []
            with mock.patch.object(runtime, "torrents_for_anime", return_value=[{"infoHash": "test", "eligible": True}]), mock.patch.object(library_audit, "_expected", return_value=expected):
                result = library_audit.audit(path, {"deployment": {"libraryUncRoot": str(owner)}}, anime_ids=[1], progress=progress.append)
            with contextlib.closing(sqlite3.connect(path)) as db:
                score, state = db.execute("SELECT similarity,state FROM runtime_completeness").fetchone()
        self.assertEqual((score, state), (0, "incomplete"))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(progress[0]["processed"], 1)


if __name__ == "__main__":
    unittest.main()
