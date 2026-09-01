from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
import unittest

from animemachine.torrents.collector_filter import (
    CatalogEvidence,
    CatalogMatcher,
    classify_title,
    decide_final,
    decide_title,
    parse_release_title,
)


class TorrentCollectorFilterTest(unittest.TestCase):
    def decision(self, title: str, expected: int | None = None, subject_type: str | None = None):
        evidence = parse_release_title(title)
        catalog = CatalogEvidence(
            status="exact" if expected is not None else "unavailable",
            expected_episodes=expected,
            subject_type=subject_type,
        )
        return evidence, decide_title(evidence, catalog)

    def test_hard_negative_samples(self) -> None:
        samples = [
            "[LoliHouse] Example - 12 [WebRip 1080p][END][简繁内封字幕]",
            "[Beatrice-Raws] Example (Vol 01) [BDRip]",
            "[Beatrice-Raws] Example (Vol 01-02) [BDRip]",
            "[Beatrice-Raws] Example (Vol 01-02 + SP) [BDRip]",
            "[Anime Time] One Piece - 222,363,537,599,602,835,965,966,1000 (Fixed) [1080p]",
            "[VCB-Studio] Missing Episodes [Batch] [BDRip]",
            "[VCB-Studio] Episode 12 [Complete] [BDRip]",
            "[VCB-Studio] Episode 12 [END] [BDRip]",
            "[VCB-Studio] Vol.02 [Complete] [BDRip]",
        ]
        for title in samples:
            with self.subTest(title=title):
                _evidence, decision = self.decision(title)
                self.assertEqual("reject", decision.decision)

        for title, expected in (
            ("[Beatrice-Raws] Hyouka (1-10) [BDRip]", 22),
            ("[Beatrice-Raws] Hyouka (01-16) [BDRip]", 22),
            ("[Beatrice-Raws] Kaiba 1-10 [BDRip]", 12),
        ):
            with self.subTest(title=title):
                _evidence, decision = self.decision(title, expected, "TV")
                self.assertEqual(("reject", "catalog_coverage_incomplete"), (decision.decision, decision.reason))

        _evidence, unknown_range = self.decision("[Beatrice-Raws] Unknown 1-6 [BDRip]")
        self.assertEqual(("defer", "range_needs_catalog"), (unknown_range.decision, unknown_range.reason))

    def test_catalog_short_form_and_continuation(self) -> None:
        samples = [
            ("[Beatrice-Raws] Itsudatte Bokura no Koi wa 10 cm Datta 1-6 [BDRip]", 6, "TV"),
            ("[Ohys-Raws] Pocket Monsters The Origin - 01~04 [BDRip]", 4, "TV"),
            ("[VCB-Studio] Example 01-12 [BDRip]", 12, "TV"),
            ("[VCB-Studio] Example S2 13-24 [BDRip]", 12, "TV"),
            ("[Beatrice-Raws] Example OVA 01-04 [DVDRip]", 4, "OVA"),
        ]
        for title, expected, subject_type in samples:
            with self.subTest(title=title):
                _evidence, decision = self.decision(title, expected, subject_type)
                self.assertEqual("accept", decision.decision)
                self.assertEqual("catalog_coverage_complete", decision.reason)

    def test_explicit_complete_and_movies(self) -> None:
        accepted = [
            "[VCB-Studio] Example [Fin] [BDRip]",
            "[LoliHouse] Example [01-12 合集][WebRip][简繁内封字幕]",
            "[VCB-Studio] Complete BD-BOX",
            "[VCB-Studio] Example Vol 1-9 ALL [BDRip]",
        ]
        for title in accepted:
            with self.subTest(title=title):
                _evidence, decision = self.decision(title)
                self.assertEqual("accept", decision.decision)
        for title in (
            "[VCB-Studio] Movie 2 [BDRip]",
            "[VCB-Studio] 剧场版27 [BDRip]",
            "[VCB-Studio] Movie 1-3 [BDRip]",
            "[银色子弹字幕组&VCB-Studio] Detective Conan M28 [MOVIE Fin] [BDRip]",
        ):
            with self.subTest(title=title):
                evidence, decision = self.decision(title)
                self.assertTrue(evidence.movie)
                self.assertFalse(evidence.standalone_episode_tokens)
                self.assertEqual("defer", decision.decision)
                self.assertEqual("movie_requires_manifest", decision.reason)

    def test_multicomponent_extras_are_not_release_single(self) -> None:
        for title in (
            "[VCB-Studio] Example [S1+OAD1+OAD2 RS] [BDRip]",
            "[VCB-Studio] Example [TV Reseed + OVA1 Rev] [BDRip]",
            "[VCB-Studio] Example [TV 01-12 + OVA + SP] [BDRip]",
            "[VCB-Studio] Example [S1+S2+OVA] [BDRip]",
            "[VCB-Studio] Example [TV + MOVIE] [BDRip]",
        ):
            with self.subTest(title=title):
                evidence, decision = self.decision(title)
                self.assertFalse(evidence.standalone_episode_tokens)
                self.assertNotEqual("single_episode", decision.reason)

    def test_number_disambiguation(self) -> None:
        title = "[VCB-Studio] Example 1920x1080 1080p 10bit x265 5.1 2026.05.17 S2 Part 2 Disc 1 CD1 [BDRip]"
        evidence = parse_release_title(title)
        self.assertFalse(evidence.standalone_episode_tokens)
        self.assertFalse(evidence.episode_ranges)
        self.assertTrue(evidence.standalone_volume_tokens)  # Disc 1 is a volume/disc token, never an episode.

    def test_dual_numbering_is_one_coverage(self) -> None:
        evidence = parse_release_title("[VCB-Studio] Example 01-07(27-33) [BDRip]")
        self.assertEqual(1, len(evidence.dual_numbering))
        self.assertEqual([(1, 7)], [(item.start, item.end) for item in evidence.episode_ranges])

    def test_all_is_only_completion_marker_in_completion_context(self) -> None:
        for title in (
            "[ANK-Raws] My Next Life as a Villainess All Routes Lead to Doom! [BDRip]",
            "[ANK-Raws+all subs] Love Live! [BDRip]",
            "[ANK-Raws] Jack-of-All-Trades [BDRip]",
        ):
            with self.subTest(title=title):
                evidence, decision = self.decision(title)
                self.assertFalse(any(marker.casefold() == "all" for marker in evidence.complete_markers))
                self.assertNotEqual("accept", decision.decision)
        _evidence, complete = self.decision("[VCB-Studio] Example Vol 1-9 ALL [BDRip]")
        self.assertEqual(("accept", "explicit_complete_volume_range"), (complete.decision, complete.reason))

    def test_malformed_known_group_prefix_is_tolerated_without_body_false_positive(self) -> None:
        positives = (
            "Beatrice-Raws]Example [BDRip]",
            "ANK-RawsEggPain-RawsAI-Raws-Example [BDRip]",
            "philosophy-raws Example [BDRip]",
            "★千夏字幕组＆ANK-Raws★【Example】 [BDRip]",
        )
        for title in positives:
            with self.subTest(title=title):
                kind, _group = classify_title(title)
                self.assertEqual("archive", kind)
        self.assertEqual((None, None), classify_title("[Unknown] A story about ANK-Raws [BDRip]"))

    def test_final_manifest_does_not_use_specials_to_prove_tv_completion(self) -> None:
        evidence = parse_release_title("[VCB-Studio] Example [Fin] [BDRip]")
        special_only = {
            "hasMainMedia": True,
            "mainMediaCount": 2,
            "primaryMainMediaCount": 0,
            "specialMediaCount": 2,
        }
        decision = decide_final(evidence, special_only, CatalogEvidence(status="unavailable"))
        self.assertEqual(("reject", "no_primary_main_media"), (decision.decision, decision.reason))

        ova = parse_release_title("[Beatrice-Raws] Example OVA 01-04 [DVDRip]")
        ova_catalog = CatalogEvidence(status="exact", expected_episodes=4, subject_type="OVA")
        ova_decision = decide_final(ova, special_only, ova_catalog)
        self.assertEqual("accept", ova_decision.decision)

    def test_movie_requires_primary_movie_media(self) -> None:
        evidence = parse_release_title("[VCB-Studio] Example Movie [BDRip]")
        special_only = {"hasMainMedia": True, "mainMediaCount": 1, "primaryMainMediaCount": 0, "specialMediaCount": 1}
        movie = {"hasMainMedia": True, "mainMediaCount": 1, "primaryMainMediaCount": 1, "specialMediaCount": 0}
        self.assertEqual("reject", decide_final(evidence, special_only).decision)
        self.assertEqual(("accept", "movie_main_media"), (decide_final(evidence, movie).decision, decide_final(evidence, movie).reason))


    def test_catalog_matcher_uses_unique_exact_normalized_titles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY, episode_count INTEGER, media_type TEXT, media_code TEXT);
                CREATE TABLE anime_title(anime_id INTEGER NOT NULL, title TEXT NOT NULL);
                INSERT INTO anime_work VALUES(1,6,'TV','tv');
                INSERT INTO anime_title VALUES(1,'Itsudatte Bokura no Koi wa 10 cm Datta');
            """)
            db.commit(); db.close()
            title = "[Beatrice-Raws] Itsudatte Bokura no Koi wa 10 cm Datta 1-6 [BDRip]"
            evidence = parse_release_title(title)
            catalog = CatalogMatcher(path).match(title, evidence)
            self.assertEqual(("exact", 1, 6), (catalog.status, catalog.anime_id, catalog.expected_episodes))
            decision = decide_title(evidence, catalog)
            self.assertEqual(("accept", "catalog_coverage_complete"), (decision.decision, decision.reason))

    def test_batch_alone_is_not_completion_proof(self) -> None:
        _evidence, decision = self.decision("[VCB-Studio] Example 01-10 [Batch] [BDRip]")
        self.assertEqual(("defer", "episode_batch_needs_catalog"), (decision.decision, decision.reason))

    def test_end_marker_does_not_override_single(self) -> None:
        _evidence, single = self.decision("[VCB-Studio] Example [12 END] [BDRip]")
        _evidence, complete = self.decision("[VCB-Studio] Example [01-12 END] [BDRip]")
        self.assertEqual(("reject", "single_episode"), (single.decision, single.reason))
        self.assertEqual("accept", complete.decision)


    def test_catalog_matcher_generation_tracks_wal_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            db = sqlite3.connect(path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.executescript("""
                CREATE TABLE anime_work(id INTEGER PRIMARY KEY, episode_count INTEGER, media_type TEXT);
                CREATE TABLE anime_title(anime_id INTEGER NOT NULL, title TEXT NOT NULL);
                INSERT INTO anime_work VALUES(1,12,'TV');
                INSERT INTO anime_title VALUES(1,'Old Show');
            """)
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            baseline = path.stat()

            matcher = CatalogMatcher(path)
            generation1 = matcher.generation()
            self.assertEqual("exact", matcher.match("Old Show").status)

            db.execute("INSERT INTO anime_work VALUES(2,4,'OVA')")
            db.execute("INSERT INTO anime_title VALUES(2,'New Show')")
            db.commit()
            os.utime(path, ns=(baseline.st_atime_ns, baseline.st_mtime_ns))
            generation2 = matcher.generation()
            self.assertNotEqual(generation1, generation2)
            self.assertEqual("exact", matcher.match("New Show").status)
            db.close()


if __name__ == "__main__":
    unittest.main()
