from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
from animemachine.config.loader import ConfigError, load_config
from animemachine.config.policy import ConfigStore


class ConfigTests(unittest.TestCase):
    def test_project_config_contains_policy_not_work_history(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as directory:
            config, metadata = load_config(source, Path(directory) / "config-cache.json")
        self.assertEqual(Path(metadata["source"]), source)
        self.assertFalse({
            "workTitleAliases", "seriesOverrides", "seriesReconciliationModes", "semanticEnglishTitles"
        } & config["naming"].keys())
        self.assertTrue(config["relations"]["mergeNativeUndividedCollectionsWithSharedAttachments"])
        self.assertTrue(config["torrentPolicy"]["acquisitionMethods"]["magnet"])
        self.assertEqual(config["metadata"]["workUniverse"], "bangumi-archive-only")

    def test_cache_hit_and_safety_validation(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            cache_path = root / "cache.json"
            config_path.write_bytes(source.read_bytes())
            first, first_meta = load_config(config_path, cache_path)
            previous = Path.cwd()
            try:
                os.chdir(root)
                second, second_meta = load_config(Path("config.json"), Path("cache.json"))
            finally:
                os.chdir(previous)
            self.assertFalse(first_meta["cacheHit"])
            self.assertTrue(second_meta["cacheHit"])
            self.assertEqual(first, second)

            invalid = json.loads(source.read_text(encoding="utf-8"))
            invalid["download"] = dict(invalid["download"], defaultStartMode="start", allowExplicitAutoStart=False)
            config_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path, cache_path)

    def test_store_and_loader_share_validation_and_environment_is_overlay_only(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_bytes(source.read_bytes())
            store = ConfigStore(config_path, source)
            invalid = json.loads(source.read_text(encoding="utf-8"))
            invalid["performance"]["poolScanWorkers"] = "invalid"
            with self.assertRaises(ValueError):
                store.validate(invalid)
            config_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path, root / "cache.json")

            config_path.write_bytes(source.read_bytes())
            persisted = store.read_persistent()["deployment"]["libraryUncRoot"]
            with mock.patch.dict(os.environ, {"ANM_LIBRARY_DIR": str(root / "runtime-library")}, clear=False):
                self.assertEqual(store.read()["deployment"]["libraryUncRoot"], str(root / "runtime-library"))
            self.assertEqual(store.read_persistent()["deployment"]["libraryUncRoot"], persisted)


if __name__ == "__main__":
    unittest.main()
