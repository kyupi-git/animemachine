from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest

from animemachine.torrents.collector_filter import FilterDecision
from animemachine.torrents.collector_state import CollectorState
from animemachine.torrents.metainfo import inspect_bytes


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError(type(value))


def sample_torrent(name=b"show.mkv"):
    return bencode({b"info": {b"length": 123, b"name": name, b"piece length": 16384, b"pieces": b"x" * 20}})


class TorrentCollectorStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "torrents"
        self.db_path = root / "state" / "collector.sqlite3"
        self.quarantine = root / "quarantine"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_state(self, attempts=3):
        return CollectorState(self.db_path, self.out, quarantine_dir=self.quarantine, max_retry_attempts=attempts)

    def test_migrates_real_legacy_tables_without_data_loss(self) -> None:
        self.db_path.parent.mkdir(parents=True)
        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE seen_results(result_key TEXT PRIMARY KEY, seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE torrent_files(sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE torrent_infohashes(infohash TEXT PRIMARY KEY, sha256 TEXT NOT NULL, filename TEXT NOT NULL, saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE native_jobs(jobset TEXT NOT NULL,source TEXT NOT NULL,term TEXT NOT NULL,next_page INTEGER NOT NULL DEFAULT 1,expected_more INTEGER NOT NULL DEFAULT 1,done INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(jobset,source,term));
            CREATE TABLE native_page_signatures(jobset TEXT NOT NULL,source TEXT NOT NULL,term TEXT NOT NULL,signature TEXT NOT NULL,page INTEGER NOT NULL,PRIMARY KEY(jobset,source,term,signature));
            CREATE TABLE retry_queue(result_key TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,details_url TEXT NOT NULL DEFAULT '',download_url TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO seen_results(result_key) VALUES('nyaa:123');
            INSERT INTO retry_queue(result_key,source,title,attempts,last_error) VALUES('nyaa:404','nyaa','x',7,'404');
        """)
        db.commit(); db.close()
        state = self.make_state()
        try:
            self.assertIsNotNone(state.db.execute("SELECT 1 FROM seen_results WHERE result_key='nyaa:123'").fetchone())
            columns = {row[1] for row in state.db.execute("PRAGMA table_info(retry_queue)")}
            self.assertTrue({"state", "next_retry_at", "error_class"}.issubset(columns))
            retry = state.db.execute("SELECT state FROM retry_queue WHERE result_key='nyaa:404'").fetchone()
            self.assertEqual("terminal", retry[0])
            self.assertTrue(state.needs_legacy_discovery_crawl())
        finally:
            state.close()

    def test_atomic_save_dedupe_and_missing_file_recovery(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "1", "title": "[VCB-Studio] Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent()
            metadata = inspect_bytes(raw)
            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            decision = FilterDecision("accept", "explicit_complete", "high", "archive", "VCB-Studio", {})
            state.record_decision(key, decision, metadata=metadata, saved_filename=filename, collector_owned=True)
            self.assertTrue(state.result_complete(key))
            saved2, filename2 = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertFalse(saved2)
            self.assertEqual(filename, filename2)
            (self.out / filename).unlink()
            stats = state.reconcile()
            self.assertEqual(1, stats["missing"])
            self.assertFalse(state.result_complete(key))
        finally:
            state.close()

    def test_retry_terminal_and_dead_letter(self) -> None:
        state = self.make_state(attempts=2)
        try:
            item = {"id": "1", "title": "x"}
            key = state.record_discovery("nyaa", item)
            self.assertEqual("terminal", state.queue_retry("nyaa", item, key, "HTTP 404", retryable=False, error_class="terminal_http_404"))
            key2 = state.record_discovery("nyaa", {"id": "2", "title": "y"})
            self.assertEqual("retryable", state.queue_retry("nyaa", item, key2, "timeout", retryable=True))
            self.assertEqual("dead_letter", state.queue_retry("nyaa", item, key2, "timeout", retryable=True))
            self.assertEqual(0, len(state.due_retries(100)))
        finally:
            state.close()


    def test_old_filter_decision_is_reevaluated(self) -> None:
        state = self.make_state()
        try:
            key = state.record_discovery("nyaa", {"id": "3", "title": "[VCB-Studio] Example [Fin] [BDRip]"})
            with state.db:
                state.db.execute("UPDATE discoveries SET decision='reject',filter_ruleset_id='old-filter' WHERE result_key=?", (key,))
                state.db.execute("INSERT OR IGNORE INTO seen_results(result_key) VALUES(?)", (key,))
            self.assertFalse(state.result_complete(key))
            self.assertTrue(state.needs_filter_evaluation(key))
        finally:
            state.close()

    def test_catalog_generation_requeues_deferred_discovery(self) -> None:
        state = self.make_state()
        try:
            key = state.record_discovery("nyaa", {"id": "catalog-1", "title": "[VCB-Studio] Example 01-06 [BDRip]"})
            decision = FilterDecision("defer", "range_needs_catalog", "medium", "archive", "VCB-Studio", {"catalog": {"generation": "g1"}})
            state.record_decision(key, decision)
            self.assertEqual([], state.discoveries_needing_reevaluation("g1"))
            rows = state.discoveries_needing_reevaluation("g2")
            self.assertEqual([key], [str(row["result_key"]) for row in rows])
            self.assertEqual(1, state.reevaluation_pending_count("g2"))
        finally:
            state.close()

    def test_atomic_save_crash_intent_recovers_database_identity(self) -> None:
        state = self.make_state()
        raw = sample_torrent()
        metadata = inspect_bytes(raw)
        key = state.record_discovery("nyaa", {"id": "crash-save", "title": "[VCB-Studio] Example [Fin] [BDRip]"})
        filename = "recovered.torrent"
        (self.out / filename).write_bytes(raw)
        with state.db:
            state.db.execute(
                "INSERT INTO pending_saves(result_key,filename,sha256,infohash_v1,infohash_v2,created_at) VALUES(?,?,?,?,?,?)",
                (key, filename, hashlib.sha256(raw).hexdigest(), metadata.get("infoHashV1"), metadata.get("infoHashV2"), "2026-08-31T00:00:00+00:00"),
            )
        state.close()

        recovered = self.make_state()
        try:
            row = recovered.get_discovery(key)
            self.assertEqual(filename, row["saved_filename"])
            self.assertEqual(1, int(row["collector_owned"]))
            self.assertEqual(0, recovered.db.execute("SELECT COUNT(*) FROM pending_saves").fetchone()[0])
            identity = recovered.db.execute("SELECT filename FROM torrent_identities WHERE identity=?", (metadata["infoHashV1"],)).fetchone()
            self.assertEqual(filename, identity[0])
        finally:
            recovered.close()

    def test_rename_reconciliation_updates_discovery_without_duplicate(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "rename", "title": "[VCB-Studio] Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent()
            metadata = inspect_bytes(raw)
            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            renamed = "renamed-by-user.torrent"
            os.replace(self.out / filename, self.out / renamed)
            stats = state.reconcile()
            self.assertGreaterEqual(stats["renamed"], 1)
            self.assertEqual(renamed, state.get_discovery(key)["saved_filename"])
            self.assertEqual(1, len(list(self.out.glob("*.torrent"))))
        finally:
            state.close()


    def test_discovery_result_key_is_namespaced_and_stable_without_source_id(self) -> None:
        state = self.make_state()
        try:
            item = {"title": "[VCB-Studio] Example [Fin] [BDRip]"}
            key1 = state.record_discovery("nyaa", item)
            key2 = state.record_discovery("nyaa", item)
            key3 = state.record_discovery("mikan", item)
            self.assertTrue(key1.startswith("nyaa:title-"))
            self.assertEqual(key1, key2)
            self.assertNotEqual(key1, key3)
        finally:
            state.close()

    def test_search_generation_jobs_and_page_signatures_are_isolated(self) -> None:
        state = self.make_state()
        try:
            self.assertEqual((1, True, False), state.native_job_get("search-a", "nyaa", "VCB-Studio"))
            state.native_job_update("search-a", "nyaa", "VCB-Studio", 7, False, done=True)
            self.assertEqual((7, False, True), state.native_job_get("search-a", "nyaa", "VCB-Studio"))
            self.assertEqual((1, True, False), state.native_job_get("search-b", "nyaa", "VCB-Studio"))

            self.assertIsNone(state.signature_seen("search-a", "nyaa", "VCB-Studio", "abc"))
            state.remember_signature("search-a", "nyaa", "VCB-Studio", 3, "abc")
            row = state.signature_seen("search-a", "nyaa", "VCB-Studio", "abc")
            self.assertIsNotNone(row)
            self.assertEqual(3, int(row["page"]))
            self.assertIsNone(state.signature_seen("search-b", "nyaa", "VCB-Studio", "abc"))
        finally:
            state.close()

    def test_manual_legacy_reject_is_report_only(self) -> None:
        state = self.make_state()
        try:
            raw = sample_torrent(b"Episode 12.mkv")
            path = self.out / "legacy-truncated.torrent"
            path.write_bytes(raw)
            state.reconcile()
            reject = FilterDecision("reject", "single_episode", "high", "archive", "VCB-Studio", {})
            stats = state.audit_existing(lambda _title, _meta: reject, mode="quarantine")
            self.assertEqual(0, stats["quarantined"])
            self.assertTrue(path.exists())
        finally:
            state.close()

    def test_quarantine_must_not_be_inside_pool(self) -> None:
        bad = CollectorState(self.db_path, self.out, quarantine_dir=self.out / "quarantine")
        try:
            with self.assertRaises(ValueError):
                bad._validated_quarantine_dir()
        finally:
            bad.close()

    def test_quarantine_move_and_restore_crash_recovery(self) -> None:
        state = self.make_state()
        raw = sample_torrent()
        source = self.out / "owned.torrent"
        source.write_bytes(raw)
        self.quarantine.mkdir(parents=True)
        target = self.quarantine / source.name
        sha = hashlib.sha256(raw).hexdigest()
        with state.db:
            cursor = state.db.execute(
                """INSERT INTO quarantine_moves(original_path,quarantine_path,sha256,reason,created_at,move_state)
                   VALUES(?,?,?,?,?,?)""",
                (str(source), str(target), sha, "single_episode", "2026-08-31T00:00:00+00:00", "planned"),
            )
            move_id = int(cursor.lastrowid)
        os.replace(source, target)
        state.close()

        moved = self.make_state()
        try:
            row = moved.db.execute("SELECT move_state FROM quarantine_moves WHERE id=?", (move_id,)).fetchone()
            self.assertEqual("moved", row[0])
            restored_path = self.out / "restored-after-crash.torrent"
            with moved.db:
                moved.db.execute(
                    "UPDATE quarantine_moves SET move_state='restore_planned',restored_path=? WHERE id=?",
                    (str(restored_path), move_id),
                )
            os.replace(target, restored_path)
        finally:
            moved.close()

        restored = self.make_state()
        try:
            row = restored.db.execute("SELECT move_state,restored_at FROM quarantine_moves WHERE id=?", (move_id,)).fetchone()
            self.assertEqual("restored", row["move_state"])
            self.assertIsNotNone(row["restored_at"])
            self.assertTrue(restored_path.is_file())
        finally:
            restored.close()

    def test_atomic_save_dedupe_clears_stale_pending_intent(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "pending-dedupe", "title": "[VCB-Studio] Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent()
            metadata = inspect_bytes(raw)
            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            with state.db:
                state.db.execute(
                    "INSERT INTO pending_saves(result_key,filename,sha256,infohash_v1,infohash_v2,created_at) VALUES(?,?,?,?,?,?)",
                    (key, filename, hashlib.sha256(raw).hexdigest(), metadata.get("infoHashV1"), metadata.get("infoHashV2"), "2026-08-31T00:00:00+00:00"),
                )
            saved2, filename2 = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertFalse(saved2)
            self.assertEqual(filename, filename2)
            self.assertEqual(0, state.db.execute("SELECT COUNT(*) FROM pending_saves WHERE result_key=?", (key,)).fetchone()[0])
        finally:
            state.close()

    def test_restore_quarantine_recovers_collector_provenance_after_reconcile(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "quarantine-owned", "title": "[VCB-Studio] Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent()
            metadata = inspect_bytes(raw)
            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            decision = FilterDecision("accept", "explicit_complete", "high", "archive", "VCB-Studio", {})
            state.record_decision(key, decision, metadata=metadata, saved_filename=filename, collector_owned=True)

            reject = FilterDecision("reject", "single_episode", "high", "archive", "VCB-Studio", {})
            stats = state.audit_existing(lambda _title, _meta: reject, mode="quarantine")
            self.assertEqual(1, stats["quarantined"])
            move = state.db.execute("SELECT id FROM quarantine_moves WHERE result_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            self.assertIsNotNone(move)

            state.reconcile()
            self.assertIsNone(state.get_discovery(key)["sha256"])
            self.assertTrue(state.restore_quarantine(int(move["id"])))
            restored = state.get_discovery(key)
            self.assertEqual(1, int(restored["collector_owned"]))
            self.assertEqual(hashlib.sha256(raw).hexdigest(), restored["sha256"])
            self.assertTrue((self.out / str(restored["saved_filename"])).is_file())
        finally:
            state.close()


    def test_same_infohash_different_metainfo_envelope_keeps_existing_file_identity(self) -> None:
        state = self.make_state()
        try:
            info = {b"length": 123, b"name": b"show.mkv", b"piece length": 16384, b"pieces": b"x" * 20}
            raw1 = bencode({b"announce": b"https://a.example/announce", b"info": info})
            raw2 = bencode({b"announce": b"https://b.example/announce", b"comment": b"changed", b"info": info})
            metadata1 = inspect_bytes(raw1)
            metadata2 = inspect_bytes(raw2)
            self.assertEqual(metadata1["infoHashV1"], metadata2["infoHashV1"])
            self.assertNotEqual(hashlib.sha256(raw1).hexdigest(), hashlib.sha256(raw2).hexdigest())

            key1 = state.record_discovery("nyaa", {"id": "same-info-1", "title": "[VCB-Studio] Example [Fin] [BDRip]"})
            saved1, filename = state.atomic_save(raw1, "[VCB-Studio] Example [Fin] [BDRip]", "nyaa", key1, metadata1)
            self.assertTrue(saved1)

            key2 = state.record_discovery("mikan", {"id": "same-info-2", "title": "[VCB-Studio] Example [Fin] [BDRip]"})
            saved2, filename2 = state.atomic_save(raw2, "[VCB-Studio] Example [Fin] [BDRip]", "mikan", key2, metadata2)
            self.assertFalse(saved2)
            self.assertEqual(filename, filename2)
            actual_sha = hashlib.sha256((self.out / filename).read_bytes()).hexdigest()
            self.assertEqual(hashlib.sha256(raw1).hexdigest(), actual_sha)
            self.assertEqual(1, state.db.execute("SELECT COUNT(*) FROM torrent_files").fetchone()[0])
            row = state.db.execute("SELECT sha256 FROM torrent_files WHERE filename=?", (filename,)).fetchone()
            self.assertEqual(actual_sha, row["sha256"])
            discovery = state.get_discovery(key2)
            self.assertEqual(actual_sha, discovery["sha256"])
            self.assertEqual(1, int(discovery["collector_owned"]))
            self.assertEqual(0, state.reconcile()["missing"])
        finally:
            state.close()

    def test_changed_discovery_title_invalidates_previous_terminal_decision(self) -> None:
        state = self.make_state()
        try:
            key = state.record_discovery(
                "nyaa", {"id": "edited-title", "title": "[LoliHouse] Example - 06 [WebRip 1080p][简繁内封字幕]"}
            )
            reject = FilterDecision("reject", "single_episode", "high", "serial-zh", "LoliHouse", {})
            state.record_decision(key, reject)
            self.assertTrue(state.result_complete(key))

            state.record_discovery(
                "nyaa", {"id": "edited-title", "title": "[LoliHouse] Example [01-12 合集][WebRip][简繁内封字幕]"}
            )
            row = state.get_discovery(key)
            self.assertEqual("defer", row["decision"])
            self.assertEqual("discovery_title_changed", row["decision_reason"])
            self.assertFalse(state.result_complete(key))
            self.assertTrue(state.needs_filter_evaluation(key))
            self.assertIsNone(state.db.execute("SELECT 1 FROM seen_results WHERE result_key=?", (key,)).fetchone())
        finally:
            state.close()

    def test_atomic_save_secondary_collision_never_overwrites_existing_file(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "collision-save", "title": "[VCB-Studio] Collision Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent(b"collision-show.mkv")
            metadata = inspect_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            token = str(metadata.get("infoHashV1") or metadata.get("infoHashV2") or sha)[:12]
            base = "[VCB-Studio] Collision Example [Fin] [BDRip]"
            initial_name = f"{base} [nyaa-{token}].torrent"
            initial = self.out / initial_name
            initial.write_bytes(b"unrelated-initial")
            suffix = hashlib.sha256((initial_name + sha).encode()).hexdigest()[:8]
            fallback = self.out / f"{base} [nyaa-{token}-{suffix}].torrent"
            fallback.write_bytes(b"unrelated-fallback")

            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            self.assertNotEqual(initial.name, filename)
            self.assertNotEqual(fallback.name, filename)
            self.assertEqual(b"unrelated-initial", initial.read_bytes())
            self.assertEqual(b"unrelated-fallback", fallback.read_bytes())
            self.assertEqual(raw, (self.out / filename).read_bytes())
        finally:
            state.close()

    def test_quarantine_and_restore_secondary_collisions_never_overwrite(self) -> None:
        state = self.make_state()
        try:
            item = {"id": "collision-quarantine", "title": "[VCB-Studio] Quarantine Example [Fin] [BDRip]"}
            key = state.record_discovery("nyaa", item)
            raw = sample_torrent(b"quarantine-show.mkv")
            metadata = inspect_bytes(raw)
            saved, filename = state.atomic_save(raw, item["title"], "nyaa", key, metadata)
            self.assertTrue(saved)
            decision = FilterDecision("accept", "explicit_complete", "high", "archive", "VCB-Studio", {})
            state.record_decision(key, decision, metadata=metadata, saved_filename=filename, collector_owned=True)

            sha = hashlib.sha256(raw).hexdigest()
            self.quarantine.mkdir(parents=True, exist_ok=True)
            quarantine_primary = self.quarantine / filename
            quarantine_primary.write_bytes(b"keep-quarantine-primary")
            quarantine_fallback = self.quarantine / f"{Path(filename).stem}-{sha[:8]}.torrent"
            quarantine_fallback.write_bytes(b"keep-quarantine-fallback")

            reject = FilterDecision("reject", "single_episode", "high", "archive", "VCB-Studio", {})
            stats = state.audit_existing(lambda _title, _meta: reject, mode="quarantine")
            self.assertEqual(1, stats["quarantined"])
            self.assertEqual(b"keep-quarantine-primary", quarantine_primary.read_bytes())
            self.assertEqual(b"keep-quarantine-fallback", quarantine_fallback.read_bytes())
            move = state.db.execute(
                "SELECT * FROM quarantine_moves WHERE result_key=? ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            self.assertIsNotNone(move)
            moved_path = Path(str(move["quarantine_path"]))
            self.assertNotIn(moved_path, {quarantine_primary, quarantine_fallback})
            self.assertEqual(raw, moved_path.read_bytes())

            original = Path(str(move["original_path"]))
            original.write_bytes(b"keep-restore-primary")
            restore_fallback = original.with_name(f"{original.stem}-{sha[:8]}{original.suffix}")
            restore_fallback.write_bytes(b"keep-restore-fallback")
            self.assertTrue(state.restore_quarantine(int(move["id"])))
            updated = state.db.execute("SELECT restored_path FROM quarantine_moves WHERE id=?", (move["id"],)).fetchone()
            restored_path = Path(str(updated["restored_path"]))
            self.assertNotIn(restored_path, {original, restore_fallback})
            self.assertEqual(b"keep-restore-primary", original.read_bytes())
            self.assertEqual(b"keep-restore-fallback", restore_fallback.read_bytes())
            self.assertEqual(raw, restored_path.read_bytes())
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
