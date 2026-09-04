from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from animemachine.integrations import subtitle_service


class SubtitleServiceTests(unittest.TestCase):
    def test_title_normalization_keeps_non_japanese_scripts(self) -> None:
        self.assertEqual(subtitle_service._normalized("도굴왕 - 01"), "도굴왕01")
        self.assertEqual(subtitle_service._normalized("Смешарики - 01"), "смешарики01")

    def test_sidecar_detection_ignores_bonus_video(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Show Ep01.mkv").write_bytes(b"x")
            (root / "Show Ep01.zh-cn.ass").write_text("subtitle", encoding="utf-8")
            (root / "Show NCOP.mkv").write_bytes(b"x")
            with mock.patch.object(subtitle_service, "_embedded_subtitle", return_value=False):
                result = subtitle_service.inspect_target(str(root))
            self.assertEqual(result["state"], "sidecar_complete")
            self.assertEqual(result["mainMedia"], 1)

    def test_local_apply_renames_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "library"
            state = Path(raw) / "state"
            root.mkdir()
            (root / "Show Ep01.mkv").write_bytes(b"video")
            loose = root / "subtitle 01.ass"
            loose.write_text("Dialogue: test", encoding="utf-8")
            config = {"deployment": {"libraryUncRoot": str(root)}, "externalLibraries": [], "subtitles": {}}
            with mock.patch.dict(os.environ, {"ANM_STATE_DIR": str(state)}):
                result = subtitle_service.apply(1, str(root), {
                    "provider": "local", "providerId": str(loose), "language": "zh-cn"
                }, config)
            self.assertEqual(result["installed"], 1)
            self.assertTrue((root / "Show Ep01.zh-cn.ass").is_file())
            self.assertTrue(any((state / "subtitles" / "backups" / "1").glob("*.zip")))

    def test_apply_rejects_target_and_local_candidate_outside_media_roots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "library"
            outside = base / "outside"
            root.mkdir(); outside.mkdir()
            (root / "Show Ep01.mkv").write_bytes(b"video")
            outside_subtitle = outside / "Show Ep01.ass"
            outside_subtitle.write_text("subtitle", encoding="utf-8")
            config = {"deployment": {"libraryUncRoot": str(root)}, "externalLibraries": [], "subtitles": {}}
            with self.assertRaises(Exception) as caught:
                subtitle_service.apply(1, str(outside), {
                    "provider": "local", "providerId": str(outside_subtitle), "language": "zh-cn"
                }, config)
            self.assertIn("outside configured media roots", str(caught.exception))
            with self.assertRaises(Exception) as caught:
                subtitle_service.apply(1, str(root), {
                    "provider": "local", "providerId": str(outside_subtitle), "language": "zh-cn"
                }, config)
            self.assertIn("outside configured media roots", str(caught.exception))

    def test_opensubtitles_falls_back_to_next_endpoint(self) -> None:
        provider = {"apiKeyEnv": "TEST_OPEN_SUBTITLES_KEY",
                    "endpoints": ["https://first.invalid/api/v1", "https://second.invalid/api/v1"]}
        payload = {"data": [{"attributes": {"release": "Example Anime", "language": "zh-cn",
                    "download_count": 3, "files": [{"file_id": 7, "file_name": "Example Anime.ass"}]}}]}
        with mock.patch.dict(os.environ, {"TEST_OPEN_SUBTITLES_KEY": "secret"}), \
                mock.patch.object(subtitle_service, "_json_request", side_effect=[OSError("offline"), payload]) as request:
            rows = subtitle_service._opensubtitles(provider, ["Example Anime"], 2026, ["zh-cn"])
        self.assertEqual(2, request.call_count)
        self.assertEqual("https://second.invalid/api/v1", rows[0]["endpoint"])
        self.assertEqual("7", rows[0]["providerId"])

    def test_search_rejects_wrong_remote_title(self) -> None:
        self.assertLess(subtitle_service._title_score("Completely Different Work", ["Example Anime"]), .78)


if __name__ == "__main__":
    unittest.main()

