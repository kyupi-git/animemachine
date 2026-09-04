from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
from animemachine.config.loader import ConfigError, load_config, region_for_country, region_policy_enabled
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

    def test_region_policy_defaults_groups_and_co_productions(self) -> None:
        self.assertEqual("china", region_for_country("HK"))
        self.assertEqual("china", region_for_country("TW"))
        self.assertEqual("europe", region_for_country("GB"))
        self.assertEqual("europe", region_for_country("RU"))
        self.assertEqual("other", region_for_country("CA"))
        self.assertTrue(region_policy_enabled({}, []))
        policy = {"regions": {"china": False, "japan": True, "korea": False, "usa": False, "europe": False, "other": False}}
        self.assertFalse(region_policy_enabled(policy, ["CN"]))
        self.assertFalse(region_policy_enabled(policy, ["OTHER"]))
        self.assertTrue(region_policy_enabled(policy, ["CN", "JP"]))

    def test_application_update_schedule_defaults_and_validation(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        config, _metadata = load_config(source, None)
        self.assertEqual(
            {"enabled": False, "mode": "notify", "time": "04:35"},
            config["applicationUpdate"]["automaticCheck"],
        )
        base = json.loads(source.read_text(encoding="utf-8"))
        for key, value in (("enabled", "yes"), ("mode", "always"), ("time", "25:99")):
            invalid = json.loads(json.dumps(base))
            invalid["applicationUpdate"]["automaticCheck"][key] = value
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path, Path(directory) / "cache.json")

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

    def test_config_store_accepts_utf8_bom_like_loader(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
            store = ConfigStore(config_path, source)
            self.assertEqual(2, store.read()["schemaVersion"])
            loaded, _ = load_config(config_path, root / "cache.json")
            self.assertEqual(2, loaded["schemaVersion"])

    def test_runtime_environment_overrides_are_revalidated(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_bytes(source.read_bytes())
            store = ConfigStore(config_path, source)
            cases = (
                {"ANM_MANAGED_QBITTORRENT_URL": "not-a-url"},
                {"ANM_ANI_RSS_MODE": "invalid-mode"},
                {"ANM_ARCHIVE_MANIFEST_ENDPOINTS": ";"},
            )
            for environment in cases:
                with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=False):
                    with self.assertRaises(ValueError):
                        store.read()
                    with self.assertRaises(ConfigError):
                        load_config(config_path, root / "cache.json")

    def test_published_schema_matches_default_and_runtime_required_fields(self) -> None:
        schema = json.loads((PROJECT_ROOT / "config" / "config.schema.json").read_text(encoding="utf-8"))
        self.assertIn(12, schema["properties"]["ui"]["properties"]["pageSize"]["enum"])
        self.assertIn("historyDirectoryName", schema["properties"]["deployment"]["required"])
        self.assertIn("downloadClient", schema["properties"]["components"]["required"])

    def test_runtime_required_fields_fail_validation_before_runtime_view(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        base = json.loads(source.read_text(encoding="utf-8"))
        cases = []
        missing_history = json.loads(json.dumps(base))
        missing_history["deployment"].pop("historyDirectoryName")
        cases.append(missing_history)
        missing_client = json.loads(json.dumps(base))
        missing_client["components"].pop("downloadClient")
        cases.append(missing_client)
        missing_start_mode = json.loads(json.dumps(base))
        missing_start_mode["download"].pop("defaultStartMode")
        cases.append(missing_start_mode)
        for path in (
            ("components", "discovery"),
            ("components", "aniRss"),
            ("metadata", "archive"),
            ("metadata", "onlineRepair"),
            ("metadata", "network"),
            ("metadata", "images"),
            ("subtitles", "languages"),
            ("ui", "filterDefaults"),
            ("library", "completeness"),
            ("library", "completeness", "thresholds"),
            ("relations", "supplementEpisodeHeuristic"),
            ("torrentPolicy", "acquisitionMethods"),
            ("torrentPolicy", "allowUnlisted"),
            ("torrentPolicy", "incrementalAcquisition"),
            ("torrentPolicy", "sourceFamilies"),
            ("torrentPolicy", "serialSubtitle"),
            ("torrentPolicy", "strategyOrder"),
            ("torrentPolicy", "subtitles"),
            ("performance",),
            ("playback",),
            ("subtitles",),
        ):
            invalid_shape = json.loads(json.dumps(base))
            target = invalid_shape
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = [] if path[-1] != "strategyOrder" else {}
            cases.append(invalid_shape)
        for key in ("security", "ui", "catalog", "differentialPlanning", "storageGuard"):
            missing_top_level = json.loads(json.dumps(base))
            missing_top_level.pop(key)
            cases.append(missing_top_level)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path, Path(directory) / "cache.json")

    def test_runtime_numeric_settings_fail_validation_before_background_tasks(self) -> None:
        source = PROJECT_ROOT / "config" / "config.example.json"
        base = json.loads(source.read_text(encoding="utf-8"))
        cases = (
            (("components", "discovery", "pollMinutes"), "invalid"),
            (("components", "discovery", "pollMinutes"), 0),
            (("metadata", "network", "probeTimeoutSeconds"), "invalid"),
            (("metadata", "network", "probeTimeoutSeconds"), 0),
            (("metadata", "network", "failureCooldownSeconds"), -1),
            (("metadata", "onlineRepair", "batchSize"), "invalid"),
            (("metadata", "onlineRepair", "batchSize"), 0),
            (("runtime", "metadataRequestDelaySeconds"), "invalid"),
            (("runtime", "metadataRequestDelaySeconds"), -1),
        )
        for path, value in cases:
            invalid = json.loads(json.dumps(base))
            target = invalid
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.json"
                config_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path, Path(directory) / "cache.json")


if __name__ == "__main__":
    unittest.main()
