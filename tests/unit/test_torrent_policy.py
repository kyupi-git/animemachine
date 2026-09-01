from __future__ import annotations

import unittest

from animemachine.config.loader import (archive_group_enabled, canonical_resolution, option_enabled, resource_group_enabled,
                                serial_group_matches, torrent_policy_eligible)
from animemachine.torrents import metainfo as INDEXER


class TorrentPolicyTests(unittest.TestCase):
    def test_only_explicit_false_disables(self) -> None:
        self.assertTrue(option_enabled({"2160p": False, "1080p": True}, "unknown"))
        self.assertTrue(option_enabled({"CHS": True}, None))
        self.assertFalse(option_enabled({"CHS": True}, None, False))
        self.assertFalse(option_enabled({"2160p": False}, "2160P"))
        groups = [{"id": "known", "name": "Known-Raws", "aliases": ["Known"], "enabled": False}]
        self.assertFalse(resource_group_enabled(groups, "Known"))
        self.assertTrue(resource_group_enabled(groups, "New-Raws"))
        self.assertFalse(resource_group_enabled(groups, "New-Raws", False))
        self.assertEqual("480p-576p", canonical_resolution("540p"))

    def test_classifier_allows_unknown_but_rejects_disabled_known(self) -> None:
        files = [{"index": 0, "path": "Show 01.mkv", "length": 100}]
        policy = {"contentClasses": {"BDRip": True}, "resolutions": {"2160p": False},
                  "subtitles": {"CHS": True}, "resourceGroups": []}
        unknown = INDEXER.classify("Show.torrent", "Show", files, policy)
        self.assertNotIn("SourceClassDisabled", unknown["rejectReasons"])
        self.assertNotIn("ResolutionDisabled", unknown["rejectReasons"])
        disabled = INDEXER.classify("Show 2160p.torrent", "Show 2160p", files, policy)
        self.assertIn("ResolutionDisabled", disabled["rejectReasons"])
        other_off = {**policy, "allowUnlisted": {"resourceGroup": False}}
        retained = INDEXER.classify("[NewGroup] Show BDRip.torrent", "[NewGroup] Show BDRip", files, other_off)
        self.assertNotIn("ResourceGroupDisabled", retained["rejectReasons"])

    def test_hdtv_variants_share_tvrip_class(self) -> None:
        files = [{"index": 0, "path": "Show 01.ts", "length": 100}]
        for name in ("Show HDTVRip", "Show HDTV", "Show TVRip"):
            self.assertEqual("TVRip", INDEXER.classify(name, name, files)["sourceClass"])

    def test_archive_ignores_subtitles_but_serial_uses_language_profile(self) -> None:
        policy = {
            "contentClasses": {"BDRip": True, "WebRip": True},
            "resolutions": {"1080p": True},
            "resourceGroups": [
                {"id": "jsum", "name": "jsum", "enabled": True},
                {"id": "ani", "name": "ANi", "enabled": True},
            ],
            "archiveGroupIds": ["jsum"],
            "sourceFamilies": {"archive": ["BDRip"], "serial": ["WebRip"]},
            "allowUnlisted": {"resourceGroup": False, "sourceClass": True, "resolution": True, "subtitle": False},
            "serialSubtitle": {"language": "zh"},
        }
        self.assertTrue(torrent_policy_eligible(policy, "BDRip", "jsum", "1080p", "Unknown", "No subtitles"))
        self.assertTrue(torrent_policy_eligible(policy, "WebRip", "ANi", "1080p", "Unknown", "[ANi] Show Baha 1080p"))
        self.assertFalse(torrent_policy_eligible(policy, "WebRip", "ANi", "1080p", "Unknown", "[ANi] Show 1080p"))
        self.assertEqual("ani", serial_group_matches("[ANi] Show Baha", "ANi", "zh")[0]["id"])
        co_production = serial_group_matches(
            "[❀拨雪寻春&MingY&霜庭云花Sub❀] 作品 [简日内嵌]", "MingYSub", "zh")
        self.assertIn("haruhana", {item["id"] for item in co_production})
        self.assertIn("mingy", {item["id"] for item in co_production})
        japanese_raw = serial_group_matches("[NEST] Show 1080p", "NEST", "ja")
        self.assertEqual("nest", japanese_raw[0]["id"])
        self.assertFalse(japanese_raw[0]["subtitleMatched"])

    def test_serial_group_is_not_implicitly_promoted_to_archive(self) -> None:
        policy = {
            "resourceGroups": [
                {"id": "jsum", "name": "jsum", "aliases": [], "enabled": True},
                {"id": "subsplease", "name": "SubsPlease", "aliases": [], "enabled": True},
            ],
            "archiveGroupIds": ["jsum"],
            "allowUnlisted": {"resourceGroup": False},
        }
        self.assertTrue(archive_group_enabled(policy, "jsum"))
        self.assertFalse(archive_group_enabled(policy, "SubsPlease"))
        self.assertFalse(archive_group_enabled(policy, "Unlisted-Raws"))
        self.assertFalse(archive_group_enabled({**policy, "archiveGroupIds": []}, "jsum"))

    def test_single_volume_and_episode_are_incremental_candidates(self) -> None:
        volume = INDEXER.classify("[ANi] Show Vol.01 BDRip", "[ANi] Show Vol.01 BDRip",
                                  [{"index": 0, "path": "Show Vol.01/01.mkv", "length": 100}])
        episode = INDEXER.classify("[ANi] Show 01 WEBRip", "[ANi] Show Episode 01 WEBRip",
                                   [{"index": 0, "path": "Show E01.mkv", "length": 100}])
        self.assertEqual("volume", volume["releaseUnit"])
        self.assertEqual([1], volume["volumeSequence"])
        self.assertEqual("candidate", volume["eligibility"])
        self.assertEqual("episode", episode["releaseUnit"])
        self.assertEqual([1], episode["episodeSequence"])
        self.assertEqual("candidate", episode["eligibility"])

    def test_partial_volume_range_is_not_mislabeled_complete(self) -> None:
        item = INDEXER.classify("Show Vol.01-02 BDRip", "Show Vol.01-02 BDRip",
                                [{"index": 0, "path": "Vol.01/01.mkv", "length": 100},
                                 {"index": 1, "path": "Vol.02/02.mkv", "length": 100}])
        self.assertEqual("volume", item["releaseUnit"])
        self.assertEqual([1, 2], item["volumeSequence"])
        self.assertFalse(item["completeHint"])


if __name__ == "__main__":
    unittest.main()
