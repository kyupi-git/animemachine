import json
import tempfile
import unittest
from pathlib import Path

from animemachine.config.policy import (
    ConfigStore,
    FileSelection,
    build_infohash_plan,
    merge_split_cour,
    series_directory,
    should_merge_split_cour,
    strict_series_edge,
    work_directory,
)


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]


class ProductPolicyTests(unittest.TestCase):
    def test_exact_directory_contract(self):
        self.assertEqual(
            series_directory("2018_10", "2026_10", "青春ブタ野郎"),
            "『2018_10－2026_10』『「青春ブタ野郎」シリーズ』",
        )
        self.assertEqual(
            work_directory("2026_10", "青春ブタ野郎はディアフレンドの夢を見ない"),
            "『2026_10』『青春ブタ野郎はディアフレンドの夢を見ない』",
        )

    def test_split_cour_merges_only_with_same_season_evidence(self):
        first = {
            "id": "mt-2-a", "official_title": "無職転生Ⅱ ～異世界行ったら本気だす～",
            "start_date": "2023-07", "directory_date": "2023_07", "official_season_id": "mt2",
        }
        second = {
            "id": "mt-2-b", "official_title": "無職転生Ⅱ ～異世界行ったら本気だす～ (第2クール)",
            "start_date": "2024-04", "directory_date": "2024_04", "official_season_id": "mt2",
        }
        self.assertTrue(should_merge_split_cour(first, second))
        merged = merge_split_cour(first, second)
        self.assertEqual(merged["directory_date"], "2023_07")
        self.assertEqual(merged["official_title"], "無職転生Ⅱ ～異世界行ったら本気だす～")
        no_evidence = dict(second, official_season_id=None)
        self.assertFalse(should_merge_split_cour(first, no_evidence))

    def test_relation_grouping_is_explicit(self):
        self.assertTrue(strict_series_edge("sequel"))
        self.assertTrue(strict_series_edge("alternative_version"))
        self.assertFalse(strict_series_edge("same_setting"))
        self.assertFalse(strict_series_edge("other"))

    def test_one_infohash_plan_can_extend_existing_task(self):
        selection = FileSelection(7, "aobuta-2026", "『2026_10』『青春ブタ野郎はディアフレンドの夢を見ない』/episode.mkv", 123)
        plan = build_infohash_plan("a" * 40, "/Library/series", [selection], existing_task=True)
        self.assertEqual(plan["idempotency_key"], "torrent:" + "a" * 40)
        self.assertEqual(plan["action"], "extend_file_selection")
        self.assertEqual(len(plan["files"]), 1)
        self.assertTrue(plan["requires_confirmation"])

    def test_unicode_equivalent_final_paths_collide(self):
        rows = [
            FileSelection(1, "a", "Season/Ａ.mkv", 1),
            FileSelection(2, "a", "season/A.mkv. ", 1),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate final path"):
            build_infohash_plan("a" * 40, "/Library/series", rows)

    def test_example_config_round_trip(self):
        example = PROJECT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as raw:
            store = ConfigStore(Path(raw) / "config.json", example)
            config = store.read()
            self.assertEqual(config["download"]["defaultStartMode"], "stopped")
            self.assertTrue(config["torrentPolicy"]["oneTaskPerInfohash"])
            self.assertTrue(config["torrentPolicy"]["contentClasses"]["webrip"])
            self.assertFalse(config["torrentPolicy"]["resolutions"]["2160p"])
            self.assertEqual(config["torrentPolicy"]["subtitleDefaultsByLanguage"]["en"], ["ENG"])
            self.assertEqual(config["differentialPlanning"]["samePathSizePolicy"], "size_and_skip")
            store.write(config)
            self.assertEqual(json.loads((Path(raw) / "config.json").read_text(encoding="utf-8")), config)

    def test_legacy_config_without_performance_block_uses_safe_defaults(self):
        example = PROJECT / "config" / "config.example.json"
        config = json.loads(example.read_text(encoding="utf-8"))
        config.pop("performance", None)
        ConfigStore.validate(config)

    def test_invalid_same_size_policy_is_rejected(self):
        example = PROJECT / "config" / "config.example.json"
        config = json.loads(example.read_text(encoding="utf-8"))
        config["differentialPlanning"]["samePathSizePolicy"] = "trust_filename"
        with self.assertRaisesRegex(ValueError, "samePathSizePolicy"):
            ConfigStore.validate(config)


if __name__ == "__main__":
    unittest.main(verbosity=2)

