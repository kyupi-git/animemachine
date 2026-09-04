"""Subtitle discovery and safe sidecar installation for archive releases.

Only configured provider APIs are queried.  External media roots are never
written; their subtitles are kept in AnimeMachine state and attached to
generated playlists.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from ..network import tls as tls_support
from ..storage import path_policy
from . import playback
from .. import __version__


MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm"}
SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".sub"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
NON_FEATURE = re.compile(r"(?i)(?:^|[\W_])(op|ed|ncop|nced|pv|cm|teaser|trailer|menu|sample|credit)(?:[\W_]|$)")
EPISODE = re.compile(r"(?i)(?:^|[^a-z0-9])(?:ep(?:isode)?|e|第)?\s*(\d{1,4})(?:\s*[-_. ]?v\d+)?(?:[^a-z0-9]|$)")
LANGUAGE_CODES = {"zh-Hans": ["zh-cn", "zh-tw"], "en": ["en"], "ja": ["ja"]}
MAX_PROVIDER_JSON_BYTES = 4 * 1024 * 1024
MAX_SUBTITLE_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_RATIO = 200


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u052f]+", "", value.casefold())


def _episode(value: str) -> int | None:
    match = EPISODE.search(Path(value).stem)
    return int(match.group(1)) if match else None


def _authorized_files(root: Path) -> list[Path]:
    canonical_root = path_policy.canonical_existing(root)
    if not canonical_root.is_dir():
        return []
    rows: list[Path] = []
    for path in canonical_root.rglob("*"):
        try:
            candidate = path_policy.authorize_existing(path, [canonical_root])
        except path_policy.PathAuthorizationError:
            continue
        if candidate.is_file():
            rows.append(candidate)
    return list(dict.fromkeys(rows))


def _main_media(root: Path) -> list[Path]:
    rows: list[Path] = []
    for path in _authorized_files(root):
        if path.suffix.casefold() in MEDIA_EXTENSIONS and not NON_FEATURE.search(path.stem):
            rows.append(path)
    # A movie can have no episode number; bonus clips are normally much smaller.
    rows.sort(key=lambda p: ((_episode(p.name) is None), _episode(p.name) or 0, p.name.casefold()))
    if rows and not any(_episode(p.name) is not None for p in rows):
        largest = max(p.stat().st_size for p in rows)
        rows = [p for p in rows if p.stat().st_size >= max(256 * 1024 * 1024, largest * .45)]
    return rows


def _embedded_subtitle(path: Path, ffprobe: str = "ffprobe") -> bool:
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return bool(json.loads(result.stdout or "{}").get("streams"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def inspect_target(path: str, *, ffprobe: str = "ffprobe", config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(path)
    if config is not None:
        root = path_policy.authorize_existing(root, playback.configured_media_roots(config))
    videos = _main_media(root)
    subtitles = [p for p in _authorized_files(root) if p.suffix.casefold() in SUBTITLE_EXTENSIONS]
    associated = 0
    for video in videos:
        stem = _normalized(video.stem)
        episode = _episode(video.name)
        if any(_normalized(sub.stem) == stem or (episode is not None and _episode(sub.name) == episode) for sub in subtitles):
            associated += 1
    embedded = bool(videos) and all(_embedded_subtitle(video, ffprobe) for video in videos)
    state = "embedded" if embedded else "sidecar_complete" if videos and associated == len(videos) else "missing"
    return {"state": state, "mainMedia": len(videos), "associated": associated,
            "videos": [str(p) for p in videos], "subtitles": [str(p) for p in subtitles]}


def _titles(db_path: Path, anime_id: int) -> tuple[list[str], int | None]:
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        row = db.execute("SELECT title_original,title_zh,title_en,start_month FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not row:
            raise ValueError("anime not found")
        aliases = [str(x[0]) for x in db.execute("SELECT title FROM anime_title WHERE anime_id=?", (anime_id,))]
    titles = [str(x) for x in row[:3] if x] + aliases
    return list(dict.fromkeys(titles)), int(str(row[3])[:4]) if row[3] and str(row[3])[:4].isdigit() else None


def _title_score(candidate: str, titles: list[str]) -> float:
    left = _normalized(candidate)
    if not left:
        return 0.0
    return max((1.0 if value and value in left else difflib.SequenceMatcher(None, left, value).ratio())
               for value in map(_normalized, titles))


def _json_request(url: str, *, headers: dict[str, str] | None = None,
                  data: dict[str, Any] | None = None, timeout: int = 25) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"User-Agent": f"AnimeMachine/{__version__}", "Accept": "application/json", **(headers or {})})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with tls_support.urlopen(request, timeout=timeout, max_bytes=MAX_PROVIDER_JSON_BYTES) as response:
        return json.load(response)


def _opensubtitles(provider: dict[str, Any], titles: list[str], year: int | None, languages: list[str]) -> list[dict[str, Any]]:
    key = os.getenv(str(provider.get("apiKeyEnv") or "OPEN_SUBTITLES_API_KEY"), "").strip()
    if not key:
        return []
    endpoints = list(dict.fromkeys(str(value).rstrip("/") for value in
                                   (provider.get("endpoints") or ["https://api.opensubtitles.com/api/v1"]) if str(value).strip()))
    query = urllib.parse.urlencode({"query": titles[0], "languages": ",".join(languages), **({"year": year} if year else {})})
    last: Exception | None = None
    for endpoint in endpoints:
        try:
            payload = _json_request(f"{endpoint}/subtitles?{query}", headers={"Api-Key": key})
            rows = []
            for item in payload.get("data", []):
                attr = item.get("attributes") or {}
                files = attr.get("files") or []
                release = str(attr.get("release") or attr.get("feature_details", {}).get("title") or "")
                score = _title_score(release, titles)
                if score < .78 or not files:
                    continue
                rows.append({"provider": "opensubtitles", "providerId": str(files[0].get("file_id")),
                             "title": release, "language": attr.get("language"), "format": files[0].get("file_name", "subtitle.srt").rsplit(".", 1)[-1],
                             "score": round(score, 3), "downloads": int(attr.get("download_count") or 0),
                             "endpoint": endpoint})
            return rows
        except Exception as exc:
            last = exc
    if last:
        raise last
    return []


def _assrt(provider: dict[str, Any], titles: list[str], year: int | None, languages: list[str]) -> list[dict[str, Any]]:
    token = os.getenv(str(provider.get("apiKeyEnv") or "ASSRT_API_TOKEN"), "").strip()
    if not token:
        return []
    endpoints = provider.get("endpoints") or ["https://api.assrt.net", "https://api.makedie.me"]
    last: Exception | None = None
    for endpoint in endpoints:
        try:
            payload = _json_request(f"{str(endpoint).rstrip('/')}/v1/sub/search?" + urllib.parse.urlencode({"q": titles[0], "cnt": 30, "pos": 0}), headers={"Authorization": f"Bearer {token}"})
            rows = []
            for item in (payload.get("sub") or {}).get("subs", []):
                title = str(item.get("native_name") or item.get("videoname") or item.get("filename") or "")
                score = _title_score(title, titles)
                if score >= .78:
                    rows.append({"provider": "assrt", "providerId": str(item.get("id")), "title": title,
                                 "language": "zh", "format": "archive", "score": round(score, 3),
                                 "downloads": int(item.get("vote_score") or 0), "endpoint": str(endpoint).rstrip("/")})
            return rows
        except Exception as exc:  # endpoint failover is deliberate
            last = exc
    if last:
        raise last
    return []


def search(db_path: Path, anime_id: int, target: str, config: dict[str, Any]) -> dict[str, Any]:
    root = path_policy.authorize_existing(target, playback.configured_media_roots(config))
    inspection = inspect_target(str(root), ffprobe=str(config.get("subtitles", {}).get("ffprobe") or "ffprobe"), config=config)
    if inspection["state"] != "missing":
        return {"state": inspection["state"], "inspection": inspection, "candidates": []}
    local = []
    for path in _authorized_files(root):
        if path.suffix.casefold() in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS:
            local.append({"provider": "local", "providerId": str(path), "title": path.name,
                          "language": "unknown", "format": path.suffix[1:].casefold(), "score": .9, "downloads": 0})
    titles, year = _titles(db_path, anime_id)
    policy = config.get("subtitles", {})
    ui = str(config.get("ui", {}).get("language") or "zh-Hans")
    languages = policy.get("languages", {}).get(ui) or LANGUAGE_CODES.get(ui, ["en"])
    functions = {"opensubtitles": _opensubtitles, "assrt": _assrt}
    enabled = [x for x in policy.get("providers", []) if x.get("enabled", True) and x.get("id") in functions]
    remote: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(enabled) or 1)) as pool:
        futures = [pool.submit(functions[x["id"]], x, titles, year, languages) for x in enabled]
        for future in concurrent.futures.as_completed(futures):
            try:
                remote.extend(future.result())
            except Exception:
                continue
    rows = local + remote
    language_order = {str(value).casefold(): index for index, value in enumerate(languages)}
    rows.sort(key=lambda x: (language_order.get(str(x.get("language") or "").casefold(), len(language_order)),
                             -float(x["score"]), -int(x.get("downloads") or 0), x["title"].casefold()))
    for row in rows:
        raw = json.dumps({k: row[k] for k in sorted(row)}, ensure_ascii=False, separators=(",", ":"))
        row["candidateId"] = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return {"state": "candidates" if rows else "not_found", "inspection": inspection, "candidates": rows}


def _safe_member_path(destination: Path, name: str) -> Path:
    if not name or "\x00" in name:
        raise ValueError("unsafe archive path")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError("unsafe archive path")
    target = (destination / normalized).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise ValueError("unsafe archive path")
    return target


def _validate_archive_budget(entries: list[tuple[str, int, int, bool]], destination: Path) -> None:
    files = 0
    total = 0
    for name, size, packed, link in entries:
        _safe_member_path(destination, name)
        if link:
            raise ValueError("subtitle archive contains a link")
        if name.endswith("/"):
            continue
        files += 1
        total += max(0, size)
        if files > MAX_ARCHIVE_FILES or size > MAX_ARCHIVE_FILE_BYTES or total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("subtitle archive exceeds extraction limits")
        if size > 1024 * 1024 and packed >= 0 and size / max(1, packed) > MAX_ARCHIVE_RATIO:
            raise ValueError("subtitle archive compression ratio is unsafe")


def _seven_zip_listing(archive: Path, tool: str, destination: Path) -> None:
    result = subprocess.run([tool, "l", "-slt", str(archive)], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("subtitle archive listing failed")
    entries: list[tuple[str, int, int, bool]] = []
    current: dict[str, str] = {}
    for line in (result.stdout or "").splitlines() + [""]:
        if not line.strip():
            if current.get("Path") and current.get("Size", "").isdigit():
                attributes = current.get("Attributes", "").casefold()
                entries.append((current["Path"], int(current["Size"]), int(current.get("Packed Size", "-1") or -1),
                                attributes.startswith("l") or " symbolic link" in attributes))
            current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    if not entries:
        raise ValueError("subtitle archive contains no files")
    _validate_archive_budget(entries, destination)


def _safe_extract(archive: Path, destination: Path, tool: str = "7z") -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.stat().st_size > MAX_SUBTITLE_DOWNLOAD_BYTES:
        raise ValueError("subtitle archive exceeds download limit")
    if archive.suffix.casefold() == ".zip":
        with zipfile.ZipFile(archive) as package:
            entries: list[tuple[str, int, int, bool]] = []
            for member in package.infolist():
                mode = (member.external_attr >> 16) & 0o170000
                entries.append((member.filename, int(member.file_size), int(member.compress_size), mode == stat.S_IFLNK))
            _validate_archive_budget(entries, destination)
            for member in package.infolist():
                target = _safe_member_path(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    else:
        _seven_zip_listing(archive, tool, destination)
        result = subprocess.run([tool, "x", "-y", f"-o{destination}", str(archive)], capture_output=True, timeout=120, check=False)
        if result.returncode:
            raise ValueError("subtitle archive extraction failed")
    files: list[Path] = []
    total = 0
    for item in destination.rglob("*"):
        if item.is_symlink():
            raise ValueError("subtitle archive contains a symbolic link")
        if not item.is_file():
            continue
        canonical = _safe_member_path(destination, str(item.relative_to(destination)))
        info = canonical.stat()
        if info.st_nlink > 1:
            raise ValueError("subtitle archive contains a hard link")
        total += info.st_size
        if info.st_size > MAX_ARCHIVE_FILE_BYTES or total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("subtitle archive exceeds extraction limits")
        if canonical.suffix.casefold() in SUBTITLE_EXTENSIONS:
            files.append(canonical)
    return files


def _download(candidate: dict[str, Any], config: dict[str, Any], destination: Path) -> list[Path]:
    provider = candidate["provider"]
    providers = {x.get("id"): x for x in config.get("subtitles", {}).get("providers", [])}
    if provider == "local":
        source = Path(candidate["providerId"])
        if source.suffix.casefold() in SUBTITLE_EXTENSIONS:
            target = destination / source.name
            shutil.copy2(source, target)
            return [target]
        return _safe_extract(source, destination, str(config.get("subtitles", {}).get("archiveTool") or "7z"))
    if provider == "opensubtitles":
        cfg = providers.get(provider, {})
        key = os.getenv(str(cfg.get("apiKeyEnv") or "OPEN_SUBTITLES_API_KEY"), "").strip()
        endpoint = str(candidate.get("endpoint") or (cfg.get("endpoints") or ["https://api.opensubtitles.com/api/v1"])[0]).rstrip("/")
        payload = _json_request(f"{endpoint}/download", headers={"Api-Key": key}, data={"file_id": int(candidate["providerId"])})
        url = str(payload["link"])
    elif provider == "assrt":
        cfg = providers.get(provider, {})
        token = os.getenv(str(cfg.get("apiKeyEnv") or "ASSRT_API_TOKEN"), "").strip()
        endpoint = str(candidate.get("endpoint") or (cfg.get("endpoints") or ["https://api.assrt.net"])[0]).rstrip("/")
        payload = _json_request(f"{endpoint}/v1/sub/detail?" + urllib.parse.urlencode({"id": candidate["providerId"]}), headers={"Authorization": f"Bearer {token}"})
        detail = (payload.get("sub") or {}).get("subs") or []
        if not detail:
            raise ValueError("provider returned no download")
        url = str(((detail[0].get("filelist") or [detail[0]])[0]).get("url") or "")
    else:
        raise ValueError("unsupported subtitle provider")
    if not url.startswith(("https://", "http://")):
        raise ValueError("provider returned an invalid download URL")
    suffix = Path(urllib.parse.urlparse(url).path).suffix.casefold()
    output = destination / ("download" + (suffix if suffix in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS else ".zip"))
    with tls_support.urlopen(urllib.request.Request(url, headers={"User-Agent": f"AnimeMachine/{__version__}"}),
                             timeout=60, max_bytes=MAX_SUBTITLE_DOWNLOAD_BYTES) as response, output.open("wb") as stream:
        shutil.copyfileobj(response, stream, length=1024 * 1024)
    return [output] if output.suffix.casefold() in SUBTITLE_EXTENSIONS else _safe_extract(output, destination, str(config.get("subtitles", {}).get("archiveTool") or "7z"))


def apply(anime_id: int, target: str, candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    root = path_policy.authorize_existing(target, playback.configured_media_roots(config))
    external_roots = [Path(str(x.get("path"))) for x in config.get("externalLibraries", []) if x.get("readOnly")]
    ani_rss_media = config.get("components", {}).get("aniRss", {}).get("mediaPath")
    if ani_rss_media:
        external_roots.append(Path(str(ani_rss_media)))
    external = any(path_policy.is_within(root, value) for value in external_roots if str(value))
    state_root = Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "subtitles"
    output_root = state_root / "external" / str(anime_id) if external else root
    if not external and not root.is_dir():
        raise ValueError("library target does not exist")
    videos = _main_media(root)
    if not videos:
        raise ValueError("no main media found")
    with tempfile.TemporaryDirectory(prefix="anm-subtitle-") as raw_temp:
        download_candidate = dict(candidate)
        if download_candidate.get("provider") == "local":
            source = path_policy.authorize_existing(str(download_candidate.get("providerId") or ""), [root])
            download_candidate["providerId"] = str(source)
        files = _download(download_candidate, config, Path(raw_temp))
        if not files:
            raise ValueError("download contains no supported subtitles")
        output_root.mkdir(parents=True, exist_ok=True)
        ordered_files = sorted(files, key=lambda p: (_episode(p.name) is None, _episode(p.name) or 0, p.name.casefold()))
        used: set[Path] = set()
        planned: list[tuple[Path, Path, Path]] = []
        language = str(candidate.get("language") or "und").replace("/", "-")
        for index, video in enumerate(videos):
            episode = _episode(video.name)
            source = next((p for p in ordered_files if p not in used and episode is not None and _episode(p.name) == episode), None)
            if source is None and index < len(ordered_files):
                source = next((p for p in ordered_files if p not in used), None)
            if source is None:
                continue
            used.add(source)
            planned.append((video, source, output_root / f"{video.stem}.{language}{source.suffix.casefold()}"))
        if len(planned) < max(1, len(videos) - 1):
            raise ValueError("subtitle set does not cover enough main media")
        existing = [p for p in _authorized_files(output_root) if p.suffix.casefold() in SUBTITLE_EXTENSIONS]
        if existing:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = state_root / "backups" / str(anime_id) / f"subtitles-{stamp}.zip"
            backup.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as package:
                for path in existing:
                    package.write(path, path.name)
            if not external:
                for path in existing:
                    path.unlink()
        installed = []
        for video, source, destination in planned:
            shutil.copy2(source, destination)
            installed.append({"media": str(video), "subtitle": str(destination)})
    return {"installed": len(installed), "external": external, "items": installed}
