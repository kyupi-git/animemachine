"""Runtime-only environment overlays for AnimeMachine configuration."""
from __future__ import annotations

import copy
import os
from typing import Any


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def apply_runtime_overrides(source: dict[str, Any]) -> dict[str, Any]:
    """Return a validated-config-shaped copy with deployment environment overlays.

    Environment variables are intentionally never written back to config.json.
    """
    data = copy.deepcopy(source)
    components = data.setdefault("components", {})
    download_client = components.setdefault("downloadClient", {})
    if value := _value("ANM_MANAGED_QBITTORRENT_URL"):
        download_client["endpoint"] = value

    ani_rss = components.setdefault("aniRss", {})
    for env_name, key in (
        ("ANM_ANI_RSS_URL", "endpoint"),
        ("ANM_ANI_RSS_MODE", "mode"),
        ("ANM_ANI_RSS_MEDIA_DIR", "mediaPath"),
    ):
        if value := _value(env_name):
            ani_rss[key] = value

    deployment = data.setdefault("deployment", {})
    for env_name, key in (
        ("ANM_LIBRARY_DIR", "libraryUncRoot"),
        ("ANM_QBT_LIBRARY_DIR", "qbtLibraryRoot"),
        ("ANM_TORRENT_POOL_DIR", "torrentPoolRoot"),
    ):
        if value := _value(env_name):
            deployment[key] = value

    external_path = _value("ANM_EXTERNAL_LIBRARY_DIR") or _value("ANM_ANI_RSS_LIBRARY_DIR")
    if external_path:
        libraries = data.get("externalLibraries")
        if isinstance(libraries, list) and libraries:
            libraries[0]["path"] = external_path

    network = data.setdefault("metadata", {}).setdefault("network", {})
    for env_name, key in (
        ("ANM_ARCHIVE_MANIFEST_ENDPOINTS", "archiveManifestEndpoints"),
        ("ANM_ARCHIVE_ASSET_PROXY_TEMPLATES", "archiveAssetProxyTemplates"),
    ):
        if value := _value(env_name):
            network[key] = [part.strip() for part in value.split(";") if part.strip()]
    return data
