from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
from animemachine.torrents import metainfo as index_torrents


class TorrentClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads((ROOT / "config" / "config.example.json").read_text(encoding="utf-8"))
        cls.files = [{"path": "01.mkv", "length": 1}]

    def classify_group(self, filename: str, name: str) -> str | None:
        return index_torrents.classify(filename, name, self.files, self.policy)["resourceGroup"]

    def test_cooperation_uses_highest_ranked_participant(self) -> None:
        self.assertEqual("VCB-Studio", self.classify_group(
            "[VCB-Studio & mawen1250] Title [BDRip 1080p].torrent",
            "[VCB-Studio & mawen1250] Title",
        ))

    def test_prefix_and_suffix_regions_match_without_scanning_work_title(self) -> None:
        self.assertEqual("ANi", self.classify_group(
            "[ANi] Title - 01 [1080P][CHT].torrent", "[ANi] Title - 01"
        ))
        self.assertEqual("LoliHouse", self.classify_group(
            "Title - LoliHouse.torrent", "Title - LoliHouse"
        ))
        self.assertEqual("Unknown", self.classify_group(
            "[Unknown] VCB-Studio Story [WEB-DL].torrent", "[Unknown] VCB-Studio Story"
        ))

    def test_serial_dash_episode_and_range_are_incremental_units(self) -> None:
        one = index_torrents.classify(
            "[ANi] 作品 - 13 [1080P][Baha][WEB-DL][CHT].torrent",
            "[ANi] 作品 - 13 [1080P][Baha][WEB-DL][CHT].mp4", self.files, self.policy)
        self.assertEqual("episode", one["releaseUnit"])
        self.assertEqual([13], one["episodeSequence"])
        three = index_torrents.classify(
            "[Group] 作品 - 01-03 [WEBRip].torrent", "[Group] 作品 - 01-03 [WEBRip]", self.files, self.policy)
        self.assertEqual([1, 2, 3], three["episodeSequence"])
        season_episode = index_torrents.classify(
            "[Nix-Raws] 中文标题 _ Japanese Title S01E20 [WEB-DL].torrent",
            "[Nix-Raws] Japanese Title S01 [WEB-DL]", self.files, self.policy)
        self.assertEqual([20], season_episode["episodeSequence"])
        bracket_episode = index_torrents.classify(
            "[Group][作品名][02][1080P][WebRip].torrent", "[Group] Work 02 (WebRip 1080p)", self.files, self.policy)
        self.assertEqual([2], bracket_episode["episodeSequence"])


if __name__ == "__main__":
    unittest.main()
