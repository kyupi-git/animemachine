from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from animemachine.library import layout as library_layout


class LibraryLayoutTests(unittest.TestCase):
    def test_supplement_is_not_selected_as_series_root(self) -> None:
        rows = [
            {"id": 1, "bgm_id": 101, "title_ja": "組長娘と世話係 ピクチャードラマ", "start_month": "2022-01"},
            {"id": 2, "bgm_id": 102, "title_ja": "組長娘と世話係", "start_month": "2022-07"},
        ]
        self.assertEqual("組長娘と世話係", library_layout.franchise_title(rows))

    def test_season_suffix_never_names_franchise(self) -> None:
        rows = [
            {"id": 2, "title_ja": "ワールドトリガー 2ndシーズン", "start_month": "2021-01"},
            {"id": 1, "title_ja": "ワールドトリガー", "start_month": "2014-10"},
        ]
        self.assertEqual("ワールドトリガー", library_layout.franchise_title(rows))

    def test_miniature_entry_never_names_franchise(self) -> None:
        rows = [
            {"id": 1, "title_ja": "Re:ゼロから始める休憩時間", "start_month": "2016-04"},
            {"id": 2, "title_ja": "Re:ゼロから始める異世界生活", "start_month": "2016-04"},
            {"id": 3, "title_ja": "Re:ゼロから始める異世界生活 4th season 奪還編", "start_month": "2026-04"},
        ]
        self.assertEqual("Re:ゼロから始める異世界生活", library_layout.franchise_title(rows))

    def test_repeated_formal_title_family_outranks_earlier_unrelated_side_work(self) -> None:
        rows = [
            {"id": 1, "title_ja": "星空外伝", "start_month": "2015-01", "relation_role": "side_story"},
            {"id": 2, "title_ja": "星空物語", "start_month": "2016-01"},
            {"id": 3, "title_ja": "星空物語 第2期", "start_month": "2018-01"},
        ]
        self.assertEqual("星空物語", library_layout.franchise_title(rows))

    def test_direct_main_story_owns_picture_drama(self) -> None:
        supplement = {"id": 1, "bgm_id": 101, "title_ja": "作品 ピクチャードラマ", "start_month": "2022-01"}
        owner = {"id": 2, "bgm_id": 102, "title_ja": "作品", "start_month": "2022-07"}
        result = library_layout.find_supplement_owner(
            supplement, [supplement, owner],
            [{"anime_id": 1, "related_bgm_id": 102, "relation_code": "main_story"}],
        )
        self.assertEqual(2, result["id"])

    def test_existing_verified_compound_alias_is_preserved_and_prefix_corrected(self) -> None:
        self.assertEqual(
            "機巧少女は傷つかない Unbreakable Machine-Doll",
            library_layout.verified_compound_title(
                "機巧少女は傷つかない",
                ["Machine-Doll wa Kizutsukanai", "Unbreakable Machine-Doll"],
                "機巧少女が傷つかない Unbreakable Machine-Doll",
            ),
        )
        self.assertEqual(
            "組長娘と世話係",
            library_layout.verified_compound_title(
                "組長娘と世話係", ["The Yakuza's Guide to Babysitting"], None
            ),
        )
        self.assertEqual(
            "C - The Money of Soul and Possibility Control",
            library_layout.verified_compound_title(
                "C", ["C - The Money of Soul and Possibility Control"],
                "C - The Money of Soul and Possibility Control",
            ),
        )

    def test_misc_container_is_never_a_work(self) -> None:
        self.assertTrue(library_layout.is_ignored_library_container("『0000_00』『OTHERS』"))
        self.assertFalse(library_layout.is_ignored_library_container("『2000_01』『作品』"))

    def test_existing_compound_directory_wins_over_new_short_title(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "『2013_10』『機巧少女が傷つかない Unbreakable Machine-Doll』"
            target.mkdir()
            match = library_layout.ExistingPathIndex(root).resolve(
                "2013_10", "機巧少女は傷つかない",
                ["機巧少女が傷つかない", "Unbreakable Machine-Doll"],
            )
            self.assertIsNotNone(match)
            self.assertEqual(target, match["path"])

    def test_verified_compound_beats_duplicate_exact_short_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            short = root / "『2013_10』『機巧少女は傷つかない』"
            compound = root / "『2013_10』『機巧少女が傷つかない Unbreakable Machine-Doll』"
            short.mkdir(); compound.mkdir()
            (short / "old.mkv").write_bytes(b"x")
            (compound / "owned.mkv").write_bytes(b"x")
            match = library_layout.ExistingPathIndex(root).resolve(
                "2013_10", "機巧少女は傷つかない",
                ["機巧少女が傷つかない", "Unbreakable Machine-Doll"],
            )
            self.assertEqual(compound, match["path"])

    def test_split_cour_suffix_is_not_a_new_season(self) -> None:
        self.assertEqual(
            (library_layout.compact("無職転生Ⅱ ～異世界行ったら本気だす～"), 2),
            library_layout.split_cour_identity("無職転生Ⅱ ～異世界行ったら本気だす～ 第2クール"),
        )
        self.assertEqual(
            (library_layout.compact("ケンガンアシュラ Season2"), 2),
            library_layout.split_cour_identity("ケンガンアシュラ Season2 Part 2"),
        )
        self.assertIsNone(library_layout.split_cour_identity("作品 Season 2"))

    def test_physical_directory_component_is_portable_and_readable(self) -> None:
        self.assertEqual(
            "『2025_04』『華Doll＊-Reinterpretation of Flowering-』",
            library_layout.format_work_directory(
                "『{date}』『{title}』", date="2025_04",
                title="華Doll*-Reinterpretation of Flowering-",
            ),
        )
        self.assertEqual("A／B：C？", library_layout.portable_directory_component("A/B:C?"))


if __name__ == "__main__":
    unittest.main()

