from __future__ import annotations

import json
import copy
import contextlib
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from animemachine.torrents import runtime as runtime_catalog


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]


class RuntimeCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "catalog.sqlite3"
        shutil.copy2(PROJECT / "tests" / "fixtures" / "anime-catalog.sqlite3", self.db_path)
        self.config = json.loads((PROJECT / "config" / "config.example.json").read_text(encoding="utf-8"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            runtime_catalog.migrate_overlay(db)
            anime = db.execute("SELECT id,title_ja FROM anime_work WHERE start_month>='2010-01' ORDER BY start_month LIMIT 1").fetchone()
            self.anime_id = int(anime["id"])
            library_root = Path(self.temp.name) / "library"
            library_root.mkdir(parents=True, exist_ok=True)
            self.config["deployment"]["libraryUncRoot"] = str(library_root)
            target = str(library_root / f"『2010_01』『{anime['title_ja']}』")
            self.target = Path(target)
            db.execute("INSERT INTO runtime_work VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                1, self.anime_id, target, None, Path(target).name, anime["title_ja"], "2010_01", None,
                "placeholder", "active", "standalone", "catalog", "test", "{}", runtime_catalog.utcnow()))
            info_hash = "a" * 40
            self.info_hash = info_hash
            db.execute("INSERT INTO runtime_torrent VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                info_hash, str(Path(self.temp.name) / "work.torrent"), "torrent", None, "Test complete",
                "BDRip", "VCB-Studio", "CHS", "candidate", None, 1, 1000,
                "2020-01-01T00:00:00+00:00", "test", "[]", 0, 1080, "p", 10, "available",
                "collection", "[]", "[]"))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (info_hash, self.anime_id, 1, "primary", 1, "[]", "{}"))
            db.execute("INSERT INTO runtime_torrent_file VALUES(?,?,?,?,?)", (info_hash, 0, "Episode 01.mkv", 1000, "main_video"))
            db.execute("INSERT INTO runtime_file_map VALUES(?,?,?,?,?,?,?)", (info_hash, 0, "Episode 01.mkv", "Episode 01.mkv", 1000, 1, "test"))
            db.commit()
        (Path(self.temp.name) / "work.torrent").write_bytes(b"placeholder")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_work_state_fingerprint_changes_without_timestamp_change(self) -> None:
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE anime_work(work_id INTEGER,library_state TEXT,scope_state TEXT)")
            db.execute("INSERT INTO anime_work VALUES(1,'absent','active')")
            before = runtime_catalog._work_state_signal(db)
            db.execute("UPDATE anime_work SET library_state='existing'")
            self.assertNotEqual(before, runtime_catalog._work_state_signal(db))

    def test_verified_torrent_and_stopped_preview_plan(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            choices = runtime_catalog.torrents_for_anime(db, self.anime_id, self.config)
        self.assertEqual(1, len(choices))
        self.assertTrue(choices[0]["eligible"])
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        self.assertFalse(plan["approved"])
        self.assertEqual("preview", plan["state"])
        self.assertEqual("create", plan["jobs"][0]["operation"])
        self.assertEqual("NoSubfolder", plan["jobs"][0]["contentLayout"])
        self.assertEqual(str(self.target).replace(str(self.config["deployment"]["libraryUncRoot"]), self.config["deployment"]["qbtLibraryRoot"]).replace("\\", "/"), plan["jobs"][0]["savePath"])

    def test_split_cour_inherits_owner_resources_and_plan(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            logical_id = int(db.execute(
                "SELECT id FROM anime_work WHERE id<>? ORDER BY id LIMIT 1", (self.anime_id,)
            ).fetchone()[0])
            db.execute("UPDATE anime_work SET physical_role='split_cour',physical_owner_anime_id=? WHERE id=?",
                       (self.anime_id, logical_id))
            db.commit()
            db.row_factory = sqlite3.Row
            self.assertEqual(self.anime_id, runtime_catalog.physical_anime_id(db, logical_id))
            self.assertEqual(self.info_hash, runtime_catalog.torrents_for_anime(db, logical_id, self.config)[0]["infoHash"])
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [logical_id]})
        self.assertEqual(self.info_hash, plan["jobs"][0]["infoHash"])
        self.assertEqual(str(self.target).replace(str(self.config["deployment"]["libraryUncRoot"]), self.config["deployment"]["qbtLibraryRoot"]).replace("\\", "/"), plan["jobs"][0]["savePath"])

    def test_single_work_manifest_does_not_require_redundant_file_map(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM runtime_file_map WHERE info_hash=?", (self.info_hash,))
            db.commit()
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        self.assertEqual(1, plan["taskCount"])
        self.assertEqual("add_missing", plan["jobs"][0]["files"][0]["action"])

    def test_manual_torrent_choice_overrides_automatic_ranking(self) -> None:
        alternate = "b" * 40
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO runtime_torrent SELECT ?,torrent_path,asset_kind,magnet_uri,'Alternate',source_class,effective_group,language_hint,scan_state,scan_reason,file_count,total_bytes,torrent_created_at,created_by,release_flags_json,collection_hint,video_height,video_scan,bit_depth,metadata_state,release_unit,volume_sequence_json,episode_sequence_json FROM runtime_torrent WHERE info_hash=?", (alternate, self.info_hash))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (alternate, self.anime_id, 1, "primary", 2, "[]", "{}"))
            db.execute("INSERT INTO runtime_torrent_file SELECT ?,file_index,source_path,length,file_kind FROM runtime_torrent_file WHERE info_hash=?", (alternate, self.info_hash))
            db.execute("INSERT INTO runtime_file_map SELECT ?,file_index,source_path,target_relative_path,length,selected,selection_reason FROM runtime_file_map WHERE info_hash=?", (alternate, self.info_hash))
            db.commit()
        automatic = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        manual = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id], "torrentSelections": {str(self.anime_id): alternate}})
        self.assertEqual(self.info_hash, automatic["jobs"][0]["infoHash"])
        self.assertEqual(alternate, manual["jobs"][0]["infoHash"])

    def test_mixed_manual_and_automatic_choices_reject_cross_hash_collision(self) -> None:
        alternate = "b" * 40
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            other_id = int(db.execute("SELECT id FROM anime_work WHERE id!=? AND start_month>='2010-01' ORDER BY id LIMIT 1", (self.anime_id,)).fetchone()[0])
            db.execute("INSERT INTO runtime_torrent SELECT ?,torrent_path,asset_kind,magnet_uri,'Alternate',source_class,effective_group,language_hint,scan_state,scan_reason,file_count,total_bytes,torrent_created_at,created_by,release_flags_json,collection_hint,video_height,video_scan,bit_depth,metadata_state,release_unit,volume_sequence_json,episode_sequence_json FROM runtime_torrent WHERE info_hash=?", (alternate, self.info_hash))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (alternate, other_id, 1, "primary", 1, "[]", "{}"))
            db.execute("INSERT INTO runtime_torrent_file SELECT ?,file_index,source_path,length,file_kind FROM runtime_torrent_file WHERE info_hash=?", (alternate, self.info_hash))
            db.execute("INSERT INTO runtime_file_map SELECT ?,file_index,source_path,target_relative_path,length,selected,selection_reason FROM runtime_file_map WHERE info_hash=?", (alternate, self.info_hash))
            db.commit()
        with self.assertRaisesRegex(ValueError, "mixed selection collision"):
            runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id, other_id]})

    def test_resolution_is_hard_filter_but_archive_subtitle_is_not(self) -> None:
        disabled = copy.deepcopy(self.config)
        disabled["torrentPolicy"]["resolutions"]["1080p"] = False
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            self.assertFalse(runtime_catalog.torrents_for_anime(db, self.anime_id, disabled)[0]["eligible"])
        disabled = copy.deepcopy(self.config)
        disabled["torrentPolicy"]["subtitles"]["CHS"] = False
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            self.assertTrue(runtime_catalog.torrents_for_anime(db, self.anime_id, disabled)[0]["eligible"])

    def test_unknown_values_follow_other_switch(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("""UPDATE runtime_torrent SET source_class='UnlistedRip',effective_group=NULL,
                          language_hint=NULL,video_height=NULL,video_scan=NULL WHERE info_hash=?""", (self.info_hash,))
            db.commit(); db.row_factory = sqlite3.Row
            choice = runtime_catalog.torrents_for_anime(db, self.anime_id, self.config)[0]
        self.assertFalse(choice["eligible"])
        self.assertEqual("resource_group_disabled", choice["eligibilityReason"])
        permissive = copy.deepcopy(self.config)
        permissive["torrentPolicy"]["allowUnlisted"] = {"resourceGroup": True, "sourceClass": True, "resolution": True, "subtitle": True}
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            self.assertTrue(runtime_catalog.torrents_for_anime(db, self.anime_id, permissive)[0]["eligible"])
        strict = copy.deepcopy(self.config)
        strict["torrentPolicy"]["allowUnlisted"] = {"resourceGroup": False, "sourceClass": False, "resolution": False, "subtitle": False}
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            self.assertFalse(runtime_catalog.torrents_for_anime(db, self.anime_id, strict)[0]["eligible"])

    def test_completed_increment_creates_exact_fingerprint_watch(self) -> None:
        later = "c" * 40
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("UPDATE runtime_torrent SET release_unit='episode',episode_sequence_json='[1]' WHERE info_hash=?", (self.info_hash,))
            self.assertEqual(1, runtime_catalog.ensure_completion_watch(db, self.info_hash))
            db.execute("INSERT INTO runtime_torrent SELECT ?,torrent_path,asset_kind,magnet_uri,'Episode 02',source_class,effective_group,language_hint,scan_state,scan_reason,file_count,total_bytes,torrent_created_at,created_by,release_flags_json,collection_hint,video_height,video_scan,bit_depth,metadata_state,release_unit,volume_sequence_json,'[2]' FROM runtime_torrent WHERE info_hash=?", (later, self.info_hash))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (later, self.anime_id, 1, "primary", 2, "[]", "{}"))
            self.assertEqual(1, runtime_catalog.refresh_watch_matches(db))
            db.commit()
        rows = runtime_catalog.watches(self.db_path)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["pendingCount"])
        self.assertTrue(runtime_catalog.delete_watch(self.db_path, rows[0]["watchId"]))

    def test_automatic_plan_accumulates_nonoverlapping_volumes(self) -> None:
        later = "d" * 40
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE runtime_torrent SET release_unit='volume',volume_sequence_json='[1]' WHERE info_hash=?", (self.info_hash,))
            db.execute("INSERT INTO runtime_torrent SELECT ?,torrent_path,asset_kind,magnet_uri,'Vol.02',source_class,effective_group,language_hint,scan_state,scan_reason,file_count,total_bytes,torrent_created_at,created_by,release_flags_json,collection_hint,video_height,video_scan,bit_depth,metadata_state,'volume','[2]',episode_sequence_json FROM runtime_torrent WHERE info_hash=?", (later, self.info_hash))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (later, self.anime_id, 1, "primary", 2, "[]", "{}"))
            db.execute("INSERT INTO runtime_torrent_file VALUES(?,?,?,?,?)", (later, 0, "Episode 02.mkv", 1000, "main_video"))
            db.execute("INSERT INTO runtime_file_map VALUES(?,?,?,?,?,?,?)", (later, 0, "Episode 02.mkv", "Episode 02.mkv", 1000, 1, "test"))
            for info_hash in (self.info_hash, later):
                db.execute("INSERT INTO runtime_torrent_file VALUES(?,?,?,?,?)", (info_hash, 1, "Fonts/fonts.7z", 500, "attachment"))
                db.execute("INSERT INTO runtime_file_map VALUES(?,?,?,?,?,?,?)", (info_hash, 1, "Fonts/fonts.7z", "Fonts/fonts.7z", 500, 1, "test"))
            db.commit()
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        manual = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id], "torrentSelections": {str(self.anime_id): self.info_hash}})
        self.assertEqual(2, plan["taskCount"])
        self.assertEqual({self.info_hash, later}, {job["infoHash"] for job in plan["jobs"]})
        self.assertEqual({self.info_hash, later}, {job["infoHash"] for job in manual["jobs"]})
        self.assertEqual(1, sum(item["action"] == "not_selected" for job in plan["jobs"] for item in job["files"]))

    def test_complete_lower_group_outranks_incomplete_preferred_group(self) -> None:
        complete = "e" * 40
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE runtime_torrent SET collection_hint=0,release_unit='volume',volume_sequence_json='[1]' WHERE info_hash=?", (self.info_hash,))
            db.execute("""INSERT INTO runtime_torrent SELECT ?,torrent_path,asset_kind,magnet_uri,'Complete collection',
                source_class,'ANK-Raws',language_hint,scan_state,scan_reason,file_count,total_bytes,torrent_created_at,
                created_by,release_flags_json,1,video_height,video_scan,bit_depth,metadata_state,'collection','[]','[]'
                FROM runtime_torrent WHERE info_hash=?""", (complete, self.info_hash))
            db.execute("INSERT INTO runtime_torrent_work VALUES(?,?,?,?,?,?,?)", (complete, self.anime_id, 1, "primary", 2, "[]", "{}"))
            db.execute("INSERT INTO runtime_torrent_file SELECT ?,file_index,source_path,length,file_kind FROM runtime_torrent_file WHERE info_hash=?", (complete, self.info_hash))
            db.commit(); db.row_factory = sqlite3.Row
            choices = runtime_catalog.torrents_for_anime(db, self.anime_id, self.config)
        self.assertEqual(complete, choices[0]["infoHash"])
        self.assertEqual("complete", choices[0]["resourceCompleteness"]["status"])

    def test_queued_plan_is_built_without_blocking_request(self) -> None:
        queued = runtime_catalog.queue_plan(self.db_path, {"animeIds": [self.anime_id]})
        self.assertEqual("building", queued["state"])
        runtime_catalog.build_queued_plan(self.db_path, self.config, queued["planId"], {"animeIds": [self.anime_id]})
        built = runtime_catalog.get_plan(self.db_path, queued["planId"])
        self.assertIsNotNone(built)
        self.assertEqual("preview", built["state"])
        self.assertEqual(1, built["taskCount"])

    def test_preexisting_media_is_not_inspected(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("UPDATE runtime_work SET library_state='existing',origin='preexisting_local'")
            db.commit()
            status = runtime_catalog.library_status(db, self.anime_id)
        self.assertFalse(status["managed"])
        self.assertEqual("not_inspected_preexisting", status["inspectionMode"])
        self.assertNotIn("expectedFiles", status["targets"][0])

    def test_managed_completeness_uses_provenance(self) -> None:
        stamp = runtime_catalog.utcnow()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            target = db.execute("SELECT target_unc FROM runtime_work").fetchone()[0]
            db.execute("UPDATE runtime_work SET library_state='existing',origin='managed_submission'")
            db.execute("INSERT INTO runtime_submission VALUES(?,?,?,?,?,?,?)", (self.info_hash, target, "anm", '["anm"]', "stoppedUP", stamp, 1))
            db.execute("INSERT INTO runtime_asset VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (1, target + "/Episode 01.mkv", target, 1000, None, None, self.info_hash, 0, "work.torrent", "current", "{}", stamp))
            db.commit()
            status = runtime_catalog.library_status(db, self.anime_id)
        self.assertTrue(status["managed"])
        self.assertEqual(0, status["targets"][0]["missingMainMedia"])
        self.assertEqual(1, status["targets"][0]["observedMainMedia"])

    def test_existing_same_target_and_exact_size_uses_fast_skip_without_hash(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "Episode 01.mkv").write_bytes(b"x" * 1000)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE runtime_work SET library_state='existing',origin='preexisting_local'")
            db.commit()
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        self.assertEqual(0, plan["taskCount"])
        file = plan["assessments"][0]["files"][0]
        self.assertEqual("skip_unchanged", file["action"])
        self.assertEqual("same_target_path_and_exact_size", file["reason"])
        self.assertEqual("canonical_path_and_exact_bytes", file["verification"])
        self.assertIsNone(file["sha256"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM runtime_local_file_hash").fetchone()[0])

    def test_exact_mode_blocks_unmanaged_same_size_without_comparable_digest(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "Episode 01.mkv").write_bytes(b"x" * 1000)
        strict = copy.deepcopy(self.config)
        strict["differentialPlanning"]["samePathSizePolicy"] = "hash_and_skip"
        plan = runtime_catalog.create_plan(self.db_path, strict, {"animeIds": [self.anime_id]})
        self.assertEqual(0, plan["taskCount"])
        file = plan["assessments"][0]["files"][0]
        self.assertEqual("conflict_review", file["action"])
        self.assertEqual("exactComparisonUnavailable", file["warning"])
        self.assertEqual(hashlib.sha256(b"x" * 1000).hexdigest(), file["sha256"])

    def test_exact_mode_verifies_managed_file_against_provenance_digest(self) -> None:
        self.target.mkdir(parents=True)
        payload = b"x" * 1000
        final_path = self.target / "Episode 01.mkv"
        final_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO runtime_asset VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                1, str(final_path), str(self.target), 1000, digest, None, self.info_hash, 0,
                "work.torrent", "current", "{}", runtime_catalog.utcnow()))
            db.commit()
        strict = copy.deepcopy(self.config)
        strict["differentialPlanning"]["samePathSizePolicy"] = "hash_and_skip"
        plan = runtime_catalog.create_plan(self.db_path, strict, {"animeIds": [self.anime_id]})
        file = plan["assessments"][0]["files"][0]
        self.assertEqual("skip_unchanged", file["action"])
        self.assertEqual("sha256_reference", file["verification"])
        self.assertEqual(digest, file["sha256"])

    def test_directory_occupying_file_target_is_a_conflict(self) -> None:
        (self.target / "Episode 01.mkv").mkdir(parents=True)
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        self.assertEqual(0, plan["taskCount"])
        file = plan["assessments"][0]["files"][0]
        self.assertEqual("conflict_review", file["action"])
        self.assertEqual("targetNotRegularFile", file["warning"])

    def test_proven_revision_is_staged_not_overwritten(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "Episode 01.mkv").write_bytes(b"old")
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE runtime_work SET library_state='existing',origin='preexisting_local'")
            db.execute("UPDATE runtime_torrent SET release_flags_json='[\"REV\"]'")
            db.commit()
        plan = runtime_catalog.create_plan(self.db_path, self.config, {"animeIds": [self.anime_id]})
        file = plan["jobs"][0]["files"][0]
        self.assertEqual("stage_replace", file["action"])
        self.assertTrue(file["newPath"].startswith(".anm-staging/"))
        self.assertEqual(b"old", (self.target / "Episode 01.mkv").read_bytes())

    def test_stale_json_manifest_backfill_never_erases_newer_index_rows(self) -> None:
        runtime_db = Path(self.temp.name) / "runtime.sqlite3"
        manifest = Path(self.temp.name) / "old-manifests.json"
        with contextlib.closing(sqlite3.connect(runtime_db)) as db:
            db.execute("CREATE TABLE torrent(info_hash TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE torrent_manifest_file(info_hash TEXT,file_index INTEGER,source_path TEXT,length INTEGER,PRIMARY KEY(info_hash,file_index))")
            db.executemany("INSERT INTO torrent VALUES(?)", [("old",), ("new",)])
            db.execute("INSERT INTO torrent_manifest_file VALUES('new',0,'Episode 02.mkv',200)")
            db.commit()
        manifest.write_text(json.dumps({"records": [{"infoHash": "old", "files": [{"index": 0, "path": "Episode 01.mkv", "length": 100}]}], "errors": []}), encoding="utf-8")
        runtime_catalog.backfill_manifests(runtime_db, manifest)
        with contextlib.closing(sqlite3.connect(runtime_db)) as db:
            self.assertEqual([("new", 0), ("old", 0)], list(db.execute("SELECT info_hash,file_index FROM torrent_manifest_file ORDER BY info_hash")))


if __name__ == "__main__":
    unittest.main()
