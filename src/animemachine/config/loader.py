#!/usr/bin/env python3
"""Load and validate AnimeMachine's project-root config with fingerprint caching."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import re
from pathlib import Path
from typing import Any

from .runtime import apply_runtime_overrides

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(os.getenv("ANM_CONFIG_PATH", str(PROJECT_ROOT / "config.json")))
DEFAULT_CACHE = Path(os.getenv("ANM_CONFIG_CACHE", str(Path(os.getenv("ANM_STATE_DIR", str(PROJECT_ROOT / ".local" / "state"))) / "config-cache.json")))
RESOURCE_GROUP_CATALOG_CANDIDATES = (
    PACKAGE_ROOT / "resources" / "resource-groups.json",
    PROJECT_ROOT / "config" / "resource-groups.json",
)


class ConfigError(RuntimeError):
    pass


def canonical_resolution(value: Any) -> str:
    normalized = str(value or "unknown").strip().casefold()
    return "480p-576p" if normalized in {"480p", "540p", "576p"} else normalized


def option_enabled(options: dict[str, Any] | None, value: Any, allow_unlisted: bool = True) -> bool:
    """Unknown/unconfigured values are eligible; only an explicit false rejects."""
    normalized = str(value or "unknown").strip().casefold()
    states = {str(key).strip().casefold(): bool(enabled) for key, enabled in (options or {}).items()}
    return states.get(normalized, bool(allow_unlisted))


def resource_group_enabled(groups: list[dict[str, Any]] | None, value: Any, allow_unlisted: bool = True) -> bool:
    """Match configured group names/aliases; an unrecognized group remains eligible."""
    normalized = str(value or "unknown").strip().casefold()
    for item in groups or []:
        names = [item.get("name"), item.get("id"), *(item.get("aliases") or [])]
        if normalized in {str(name).strip().casefold() for name in names if name}:
            return bool(item.get("enabled", True))
    return bool(allow_unlisted)


def load_resource_group_catalog() -> dict[str, Any]:
    for path in RESOURCE_GROUP_CATALOG_CANDIDATES:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if data.get("schemaVersion") != 1:
                raise ConfigError(f"unsupported resource-group catalog: {path}")
            return data
    raise ConfigError("resource-groups.json is missing")


def _keyword_matches(text: str, keyword: str) -> bool:
    if keyword == "*":
        return True
    # Short ASCII group names (ANi, DKB, ASW...) require token boundaries;
    # otherwise ANi would accidentally match words such as "animation".
    if keyword.isascii() and re.fullmatch(r"[A-Za-z0-9]+", keyword):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", text, re.I))
    return keyword.casefold() in text.casefold()


def serial_profile_language(config: dict[str, Any], ui_language: str | None = None) -> str:
    configured = str(config.get("torrentPolicy", {}).get("serialSubtitle", {}).get("language", "auto"))
    candidate = str(ui_language or config.get("ui", {}).get("language", "zh")) if configured == "auto" else configured
    return "zh" if candidate.casefold().startswith("zh") else ("ja" if candidate.casefold().startswith("ja") else "en")


def serial_group_matches(text: str, group: str, language: str,
                         catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return ordered serial-release rules whose group and subtitle clauses both match."""
    rules = (catalog or load_resource_group_catalog()).get("serialProfiles", {}).get(language, [])
    haystack = f"{group or ''} {text or ''}"
    result = []
    for order, raw in enumerate(rules):
        rule_id, display_name, group_keywords, subtitle_keywords = raw
        # Co-productions often normalize to one canonical group while the
        # outer torrent filename retains every participant. Match against the
        # combined evidence so the highest-priority participating rule wins.
        group_text = haystack
        if not any(_keyword_matches(group_text, value) for value in group_keywords):
            continue
        subtitle_match = any(_keyword_matches(haystack, value) for value in subtitle_keywords)
        # Japanese playback does not require a subtitle track.  Keep the
        # configured group order while accepting a group-only match; unknown
        # groups still follow the explicit "other" switch.
        if not subtitle_match and language != "ja":
            continue
        result.append({"id": rule_id, "displayName": display_name, "order": order,
                       "wildcardGroup": "*" in group_keywords, "subtitleMatched": subtitle_match})
    return result


def serial_rule_enabled(policy: dict[str, Any], language: str, rule_id: str) -> bool:
    configured = policy.get("serialSubtitle", {}).get("enabledByLanguage", {})
    return True if language not in configured else rule_id in configured[language]


def source_family(policy: dict[str, Any], source_class: Any) -> str:
    value = str(source_class or "Unknown").casefold()
    families = policy.get("sourceFamilies", {})
    for family in ("archive", "serial"):
        if value in {str(item).casefold() for item in families.get(family, [])}:
            return family
    return "other"


def archive_group_enabled(policy: dict[str, Any], group: Any) -> bool:
    configured = policy.get("resourceGroups", [])
    archive_ids = {str(value).casefold() for value in policy.get("archiveGroupIds", [])}
    normalized = str(group or "unknown").strip().casefold()
    for item in configured:
        names = [item.get("name"), item.get("id"), *(item.get("aliases") or [])]
        if normalized in {str(name).strip().casefold() for name in names if name}:
            return bool(item.get("enabled", True)) and str(item.get("id", "")).casefold() in archive_ids
    return bool(policy.get("allowUnlisted", {}).get("resourceGroup", False))


def torrent_policy_eligible(policy: dict[str, Any], source_class: Any, group: Any,
                            resolution: Any, subtitle: Any, text: str = "",
                            ui_language: str | None = None) -> bool:
    allow = policy.get("allowUnlisted", {})
    if not option_enabled(policy.get("contentClasses"), source_class, allow.get("sourceClass", True)):
        return False
    if not option_enabled(policy.get("resolutions"), canonical_resolution(resolution), allow.get("resolution", True)):
        return False
    family = source_family(policy, source_class)
    if family == "archive":
        return archive_group_enabled(policy, group)
    if family == "serial":
        pseudo = {"torrentPolicy": policy, "ui": {"language": ui_language or "zh"}}
        language = serial_profile_language(pseudo, ui_language)
        matches = serial_group_matches(text, str(group or ""), language)
        return (any(serial_rule_enabled(policy, language, match["id"]) for match in matches)
                if matches else bool(allow.get("resourceGroup", False)))
    return (resource_group_enabled(policy.get("resourceGroups"), group, allow.get("resourceGroup", False))
            and option_enabled(policy.get("subtitles"), subtitle, allow.get("subtitle", True)))


def explicitly_disabled(options: dict[str, Any] | None) -> list[str]:
    return [str(key).strip().casefold() for key, enabled in (options or {}).items() if enabled is False]


def explicitly_disabled_groups(groups: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("name")).strip().casefold() for item in (groups or [])
            if item.get("name") and item.get("enabled", True) is False]


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ConfigError("config schemaVersion must be 2")
    required = ("deployment", "components", "metadata", "library", "naming", "relations", "scope", "torrentPolicy", "download", "runtime", "safety")
    missing = [key for key in required if not isinstance(data.get(key), dict)]
    if missing:
        raise ConfigError(f"missing config objects: {', '.join(missing)}")
    if data["metadata"].get("workUniverse") != "bangumi-archive-only":
        raise ConfigError("metadata.workUniverse must be bangumi-archive-only")
    policy = data["torrentPolicy"]
    if policy.get("oneTaskPerInfohash") is not True or policy.get("reuseTaskForAdditionalFiles") is not True:
        raise ConfigError("one infohash/task with reusable file selection is mandatory")
    methods = policy.get("acquisitionMethods", {})
    if methods.get("torrent") is not True or methods.get("magnet") is not True:
        raise ConfigError("torrent and magnet acquisition methods must be supported")
    allowed_orders = {
        "releaseStrategyPriority": {"bdrip_collection", "bdrip_volume", "webrip_collection", "webrip_episode", "tvrip_collection", "tvrip_episode", "other"},
        "collectionRevisionPriority": {"collection_revision", "collection", "revision", "ordinary"},
        "attachmentPriority": {"with_attachments", "without_attachments"},
        "creationDatePriority": {"newest", "oldest"},
        "sizePriority": {"larger", "smaller"},
        "bitDepthPriority": {"10bit", "8bit", "unknown"},
    }
    for key, allowed in allowed_orders.items():
        values = policy.get(key, [])
        if len(values) != len(set(values)) or set(values) != allowed:
            raise ConfigError(f"torrentPolicy.{key} must contain each supported value exactly once")
    incremental = policy.get("incrementalAcquisition", {})
    required_fingerprint = {"sourceClass", "resourceGroup", "subtitle", "resolution", "videoScan", "bitDepth"}
    if incremental.get("enabled") is not True or incremental.get("watchAfterCompletion") is not True:
        raise ConfigError("incremental episode/volume acquisition and completion watches must remain supported")
    if set(incremental.get("exactFingerprintDimensions", [])) != required_fingerprint:
        raise ConfigError("incrementalAcquisition exact fingerprint is incomplete")
    repair = data["metadata"].get("onlineRepair", {})
    if repair.get("enabled") and repair.get("deferUntilLocalReady") is not True:
        raise ConfigError("online metadata repair must run after local readiness")
    network = data["metadata"].get("network", {})
    if not network.get("archiveManifestEndpoints") or not network.get("bangumiApiEndpoints"):
        raise ConfigError("metadata network requires Archive manifest and Bangumi API endpoint pools")
    storage = data.get("storageGuard", {})
    if not isinstance(storage, dict):
        raise ConfigError("storageGuard must be an object")
    try:
        minimum_free = float(storage.get("minimumFreeTiB", 0.1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("storageGuard.minimumFreeTiB must be a finite non-negative number") from exc
    if not math.isfinite(minimum_free) or minimum_free < 0:
        raise ConfigError("storageGuard.minimumFreeTiB must be a finite non-negative number")
    if not isinstance(policy.get("resolutions"), dict) or not policy["resolutions"]:
        raise ConfigError("torrentPolicy.resolutions must define known choices")
    subtitle_policy = data.get("subtitles", {})
    if subtitle_policy:
        if not isinstance(subtitle_policy.get("providers", []), list):
            raise ConfigError("subtitles.providers must be a list")
        for provider in subtitle_policy.get("providers", []):
            if provider.get("id") not in {"assrt", "opensubtitles"}:
                raise ConfigError("unsupported subtitle provider")
            endpoints = provider.get("endpoints", [])
            if provider.get("enabled", True) and (not endpoints or not all(str(value).startswith("https://") for value in endpoints)):
                raise ConfigError("subtitle providers require HTTPS endpoints")
    ani_rss = data.get("components", {}).get("aniRss", {})
    if ani_rss:
        endpoint = str(ani_rss.get("endpoint") or "")
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigError("components.aniRss.endpoint must be an absolute HTTP(S) URL")
        if ani_rss.get("mode") not in {"prefer", "fallback", "manual"}:
            raise ConfigError("components.aniRss.mode must be prefer, fallback, or manual")
        try:
            sync_minutes = int(ani_rss.get("syncMinutes", 0))
            delete_grace = int(ani_rss.get("deleteGraceSyncs", 2))
        except (TypeError, ValueError) as exc:
            raise ConfigError("components.aniRss timing values must be integers") from exc
        if sync_minutes < 5:
            raise ConfigError("components.aniRss.syncMinutes must be at least 5")
        if not 1 <= delete_grace <= 10:
            raise ConfigError("components.aniRss.deleteGraceSyncs must be between 1 and 10")
    for source in data.get("externalLibraries", []):
        if source.get("readOnly") is not True:
            raise ConfigError("external libraries must be explicitly read-only")
        try:
            scan_minutes = int(source.get("scanMinutes", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError("external library scanMinutes must be an integer") from exc
        if scan_minutes < 5:
            raise ConfigError("external library scanMinutes must be at least 5")
    performance = data.get("performance", {})
    limits = {"initialTorrentBatch": (0, 10000), "poolScanWorkers": (0, 16),
              "poolCommitEvery": (25, 2000), "libraryCommitEvery": (10, 2000),
              "asyncPlanThreshold": (10, 10000)}
    defaults = {"initialTorrentBatch": 500, "poolScanWorkers": 0,
                "poolCommitEvery": 100, "libraryCommitEvery": 100,
                "asyncPlanThreshold": 25}
    for key, (low, high) in limits.items():
        try:
            value = int(performance.get(key, defaults[key]))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"performance.{key} must be an integer") from exc
        if not low <= value <= high:
            raise ConfigError(f"performance.{key} must be between {low} and {high}")
    if performance.get("catalogReadDuringSync", True) is not True:
        raise ConfigError("performance.catalogReadDuringSync must remain enabled")
    playback = data.get("playback", {})
    idle = int(playback.get("playlistIdleSeconds", playback.get("playlistTtlSeconds", 43200)))
    maximum = int(playback.get("playlistMaximumSeconds", 604800))
    if idle < 900 or idle > 172800 or maximum < idle or maximum > 2592000:
        raise ConfigError("playback session lifetime is invalid")
    for mapping in playback.get("directPathMappings", []):
        if not str(mapping.get("serverPathPrefix") or "").strip() or not str(mapping.get("clientPathPrefix") or "").strip():
            raise ConfigError("playback direct path mappings require serverPathPrefix and clientPathPrefix")
    if not isinstance(policy.get("contentClasses"), dict) or not policy["contentClasses"]:
        raise ConfigError("torrentPolicy.contentClasses must define known choices")
    allow_unlisted = policy.get("allowUnlisted", {})
    if any(not isinstance(allow_unlisted.get(key, True), bool) for key in ("resourceGroup", "sourceClass", "resolution", "subtitle")):
        raise ConfigError("torrentPolicy.allowUnlisted values must be boolean")
    families = policy.get("sourceFamilies", {})
    if not all(isinstance(families.get(name), list) and families[name] for name in ("archive", "serial")):
        raise ConfigError("torrentPolicy.sourceFamilies must define archive and serial classes")
    if policy.get("serialSubtitle", {}).get("language", "auto") not in {"auto", "zh", "en", "ja"}:
        raise ConfigError("torrentPolicy.serialSubtitle.language is invalid")
    group_ids = {str(item.get("id")) for item in policy.get("resourceGroups", [])}
    if not policy.get("archiveGroupIds") or not set(map(str, policy["archiveGroupIds"])).issubset(group_ids):
        raise ConfigError("torrentPolicy.archiveGroupIds must reference configured resource groups")
    download = data["download"]
    if download.get("defaultStartMode", "stopped") != "stopped":
        raise ConfigError("download.defaultStartMode must remain stopped")
    differential = data.get("differentialPlanning", {})
    if not isinstance(differential, dict):
        raise ConfigError("differentialPlanning must be an object")
    staging = str(differential.get("stagingDirectoryName", ".anm-staging"))
    if differential.get("samePathSizePolicy", "size_and_skip") not in {"size_and_skip", "hash_and_skip"}:
        raise ConfigError("differentialPlanning.samePathSizePolicy must be size_and_skip or hash_and_skip")
    if not staging or staging in {".", ".."} or "/" in staging or "\\" in staging:
        raise ConfigError("differentialPlanning.stagingDirectoryName must be one safe directory name")
    secret_names = {"password", "api_key", "apikey", "token", "secret"}
    def reject_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower().replace("-", "").replace("_", "")
                if normalized in {name.replace("_", "") for name in secret_names}:
                    raise ConfigError(f"embedded secret field is forbidden: {'.'.join(path + (key,))}")
                reject_secrets(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value): reject_secrets(child, path + (str(index),))
    reject_secrets(data)
    return data


def _runtime_view(data: dict[str, Any]) -> dict[str, Any]:
    """Expose stable legacy-shaped runtime keys without persisting duplicate policy."""
    result = dict(data)
    deployment = data["deployment"]
    client = data["components"]["downloadClient"]
    archive_ids = {str(value).casefold() for value in data["torrentPolicy"].get("archiveGroupIds", [])}
    groups = [item for item in data["torrentPolicy"].get("resourceGroups", [])
              if item.get("enabled", True) and str(item.get("id", "")).casefold() in archive_ids]
    tiers: list[list[str]] = []
    for tier in sorted({int(item.get("tier", 999)) for item in groups}):
        tiers.append([item["name"] for item in sorted((x for x in groups if int(x.get("tier", 999)) == tier), key=lambda x: int(x.get("order", 999)))])
    enabled = data["torrentPolicy"]["contentClasses"]
    result["libraryRuntime"] = {
        "uncRoot": deployment["libraryUncRoot"], "qbtRoot": deployment["qbtLibraryRoot"],
        "historyDirectoryName": deployment["historyDirectoryName"],
    }
    result["torrentRuntime"] = {"poolRoot": deployment["torrentPoolRoot"], "groupTiers": tiers, "contentClasses": enabled}
    result["qbittorrentRuntime"] = {
        "endpoint": client["endpoint"], "category": client["category"], "tags": client["tags"],
        "addStopped": data["download"]["defaultStartMode"] == "stopped",
    }
    # Transitional aliases keep deterministic local tools working while the product package adopts v2 names.
    result["library"] = {**data["library"], **result["libraryRuntime"]}
    result["torrent"] = {**data["torrentPolicy"], **result["torrentRuntime"]}
    result["qbittorrent"] = result["qbittorrentRuntime"]
    return result


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(config_path: Path = DEFAULT_CONFIG, cache_path: Path | None = DEFAULT_CACHE) -> tuple[dict[str, Any], dict[str, Any]]:
    # All callers share one cache identity even when one passes a relative path
    # and another passes the equivalent absolute path.
    config_path = config_path.resolve()
    cache_path = cache_path.resolve() if cache_path is not None else None
    stat = config_path.stat()
    identity = {
        "source": str(config_path),
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
    }
    if cache_path is not None and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if all(cached.get(k) == v for k, v in identity.items()):
                return _runtime_view(apply_runtime_overrides(_validate(cached["config"]))), {**identity, "sha256": cached["sha256"], "cacheHit": True}
        except (OSError, ValueError, KeyError, ConfigError):
            pass

    raw = config_path.read_bytes()
    data = _validate(json.loads(raw.decode("utf-8-sig")))
    fingerprint = hashlib.sha256(raw).hexdigest()
    if cache_path is not None:
        _atomic_json(cache_path, {**identity, "sha256": fingerprint, "config": data})
    return _runtime_view(apply_runtime_overrides(data)), {**identity, "sha256": fingerprint, "cacheHit": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--show", action="store_true", help="print normalized config instead of summary")
    args = parser.parse_args()
    config, metadata = load_config(args.config, args.cache)
    print(json.dumps(config if args.show else metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
