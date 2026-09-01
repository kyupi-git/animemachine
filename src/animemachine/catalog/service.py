#!/usr/bin/env python3
"""Build and browse a small, provenance-aware anime metadata catalog."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import zipfile

import httpx
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config.policy import ConfigStore, NON_GROUPING_RELATIONS, STRICT_SERIES_RELATIONS
from ..torrents import runtime as runtime_catalog
from ..integrations import qbt_runtime, connectivity, playback, subtitle_service, ani_rss
from . import archive_update, relation_graph, metadata_repair
from .image_fetcher import ImageFetcher
from ..library import audit as library_audit, history as library_history, layout as library_layout
from ..network import downloads as network_downloads, registry as network_registry, sources as network_sources, tls as tls_support, validators as network_validators, transport
from ..torrents import mapper as torrent_mapper
from .. import metrics
from ..storage import AVAILABLE, status_for_path
from ..storage import preflight as storage_preflight
from ..storage import path_policy
from ..api import auth
from .. import __version__

from ..config.loader import explicitly_disabled, load_resource_group_catalog


ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = ROOT.parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
RESOURCES = PACKAGE_ROOT / "resources"
DEFAULT_DB = Path(os.getenv("ANM_CATALOG_DB", str(PROJECT_ROOT / ".local" / "state" / "catalog" / "anime-catalog.sqlite3")))
DEFAULT_CACHE = Path(os.getenv("ANM_CACHE_DIR", str(PROJECT_ROOT / ".local" / "cache")))
DEFAULT_ARCHIVE_DIR = Path(os.getenv("ANM_ARCHIVE_DIR", str(PROJECT_ROOT / ".local" / "archive")))
DEFAULT_CONFIG = Path(os.getenv("ANM_RUNTIME_CONFIG", str(PROJECT_ROOT / ".local" / "state" / "runtime-config.json")))
EXAMPLE_CONFIG = RESOURCES / "config.example.json"
STATE_DIR = Path(os.getenv("ANM_STATE_DIR", str(PROJECT_ROOT / ".local" / "state")))
RUNTIME_DB = Path(os.getenv("ANM_RUNTIME_CATALOG_DB", str(STATE_DIR / "catalog" / "runtime.sqlite3")))
TORRENT_POOL = Path(os.getenv("ANM_TORRENT_POOL_DIR", "/Torrents"))
SYNC_STATUS_FILE = Path(os.getenv("ANM_SYNC_STATUS_FILE", str(STATE_DIR / "sync-status.json")))
LATEST_ARCHIVE_URL = "https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json"
USER_AGENT = f"AnimeMachine/{__version__} (ANM; metadata catalog)"
GENERIC_TAGS = {"tv", "剧场版", "日本", "日本动画", "动画", "anime", "ova", "oad", "web"}
DISPLAY_TAG_DATE_RE = re.compile(
    r"^(?:19|20)\d{2}(?:(?:年(?:\d{1,2}月|[春夏秋冬])?)|(?:[-./]\d{1,2}))?(?:番|新番|动画|動畫)?$",
    re.IGNORECASE,
)
DISPLAY_TAG_METADATA = {
    "bangumi", "bgm", "日本", "日本动画", "日本動畫", "tv", "ova", "oad", "web",
    "剧场版", "劇場版", "动画", "動畫", "anime", "新番",
}
PLATFORMS = {0: "其他", 1: "TV", 2: "OVA", 3: "剧场版", 4: "短片", 5: "WEB", 2006: "动态漫画"}
PLATFORM_CODES = {0: "other", 1: "tv", 2: "ova", 3: "movie", 4: "short", 5: "web", 2006: "motion_comic"}
RELATIONS = {1: "改编", 2: "前传", 3: "续集", 4: "总集篇", 5: "全集", 6: "番外篇", 7: "角色出演", 8: "相同世界观", 9: "不同世界观", 10: "不同演绎", 11: "衍生", 12: "主线故事", 14: "联动", 99: "其他"}
RELATION_CODES = {1: "adaptation", 2: "prequel", 3: "sequel", 4: "summary", 5: "full_story", 6: "side_story", 7: "character_appearance", 8: "same_setting", 9: "alternative_setting", 10: "alternative_version", 11: "spin_off", 12: "main_story", 14: "collaboration", 99: "other"}
STAFF_POSITIONS = {
    2: ("导演", "director"),
    6: ("音乐", "music"),
    8: ("角色设计", "character_design"),
    10: ("系列构成", "series_composition"),
    49: ("联合导演", "director"),
    67: ("动画制作", "studio"),
    74: ("总导演", "director"),
}
CHARACTER_ROLES = {1: "主角", 2: "配角", 3: "客串"}
THEME_CLUSTERS = {
    "action": ("动作", "動作", "戰鬥", "战斗", "action", "アクション", "バトル", "格斗", "格闘", "武打"),
    "adventure": ("冒险", "冒險", "探险", "探險", "adventure", "アドベンチャー"),
    "comedy": ("喜剧", "喜劇", "搞笑", "幽默", "comedy", "コメディ", "ギャグ"),
    "fantasy": ("奇幻", "幻想", "魔法", "超自然", "fantasy", "supernatural", "ファンタジー", "異世界", "异世界"),
    "romance": ("恋爱", "戀愛", "爱情", "愛情", "纯爱", "純愛", "romance", "romantic", "love story", "ラブコメ", "ラブストーリー", "恋愛"),
    "scifi": ("科幻", "科学幻想", "科學幻想", "science fiction", "sci-fi", "sci fi", "sf", "サイエンスフィクション", "サイエンス・フィクション"),
    "mystery": ("悬疑", "懸疑", "推理", "解谜", "解謎", "侦探", "偵探", "detective", "mystery", "ミステリー"),
    "horror": ("恐怖", "惊悚", "驚悚", "怪谈", "怪談", "horror", "ホラー", "スリラー"),
    "daily_life": ("日常", "空气系", "空氣系", "slice of life", "日常系"),
    "school": ("校园", "校園", "学园", "學園", "学園", "校园生活", "校園生活", "school", "school life"),
    "sports": ("运动", "運動", "体育", "體育", "sports", "スポーツ", "竞技", "競技"),
    "music": ("音乐", "音樂", "音乐番", "音樂番", "乐队", "樂隊", "アイドル", "偶像", "バンド", "music"),
    "mecha": ("机战", "機戰", "机器人", "機器人", "萝卜", "蘿蔔", "ロボット", "mecha"),
    "historical": ("历史", "歷史", "史实", "史實", "时代剧", "時代劇", "历史剧", "歷史劇", "historical"),
    "workplace": ("职场", "職場", "社会人", "职业动画", "職業動畫", "お仕事", "workplace"),
    "time_travel": ("穿越", "时空穿越", "時空穿越", "时间旅行", "時間旅行", "time travel", "time loop", "タイムトラベル", "タイムリープ"),
    "galgame": ("galgame", "gal game", "gal改", "美少女游戏", "美少女遊戲", "美少女ゲーム", "ギャルゲー", "ギャルゲ原作"),
    "yuri": ("百合", "百合向", "yuri", "girls love", "girl's love", "ガールズラブ"),
    "magical_girl": ("魔法少女", "magical girl", "mahou shoujo", "まほうしょうじょ"),
    "harem": ("后宫", "後宮", "逆后宫", "逆後宮", "harem", "reverse harem", "ハーレム", "逆ハーレム"),
    "children": ("儿童向", "兒童向", "子供向", "子供向け", "少儿向", "少兒向", "kids", "children", "kodomo"),
    "avant_garde": ("非常规表达", "非常規表達", "实验", "實驗", "实验动画", "實驗動畫", "实验性", "實驗性", "先锋动画", "先鋒動畫", "前卫动画", "前衛動畫", "意识流", "意識流", "抽象动画", "抽象動畫", "experimental animation", "avant-garde", "実験アニメ", "前衛アニメ", "抽象アニメ"),
    "josei": ("女性向", "女性漫画", "女性マンガ", "josei"),
}
# A raw Archive tag is evidence, not automatically a user-facing taxonomy value.
# Broad comedy labels need stronger semantics or corroboration before surfacing.
COMEDY_STRONG_ALIASES = ("喜剧", "喜劇", "comedy", "コメディ", "ギャグ")
COMEDY_WEAK_ALIASES = ("搞笑", "幽默")
COMEDY_AUXILIARY_ALIASES = (
    "吐槽", "恶搞", "惡搞", "黑色幽默", "无厘头", "無厘頭", "滑稽",
    "荒诞", "荒誕", "parody", "gag", "ギャグ",
)
THEME_RULE_VERSION = "archive-tag-confidence-v1"
# Fixed from the 2026-08-24 full Catalog snapshot; UI order is intentionally
# stable and does not fluctuate as a user's local database changes.
THEME_DISPLAY_ORDER = (
    "fantasy", "comedy", "action", "scifi", "romance", "children",
    "daily_life", "yuri", "school", "harem", "mecha", "music",
    "galgame", "adventure", "sports", "avant_garde", "mystery",
    "magical_girl", "time_travel", "historical", "horror", "josei",
    "workplace",
)
STUDIO_FILTER_MIN_WORKS = 20
STUDIO_FAMILIES = {
    "toei": ("東映アニメーション／東映動画", ("東映アニメーション", "東映動画", "toei animation")),
    "sunrise": ("サンライズ／バンダイナムコフィルムワークス", ("サンライズ", "sunrise", "バンダイナムコフィルムワークス", "bandai namco filmworks")),
    "tms": ("トムス・エンタテインメント／東京ムービー", ("トムス・エンタテインメント", "tms entertainment", "東京ムービー", "東京ムービー新社")),
    "ashi": ("葦プロダクション／プロダクション リード", ("葦プロダクション", "プロダクション リード", "production reed")),
    "aic": ("AIC", ("aic", "aic asta", "aic plus+", "aic spirits", "aic classic", "aic build")),
    "xebec": ("XEBEC", ("xebec", "xebec zwei", "xebec m2")),
    "olm": ("OLM", ("olm", "オー・エル・エム")),
    "pierrot_signpost": ("ぴえろ／スタジオ サインポスト", ("ぴえろ", "pierrot", "ぴえろプラス", "スタジオ サインポスト", "studio signpost")),
    "yumeta": ("ゆめ太カンパニー／TYOアニメーションズ／ハルフィルムメーカー", ("ゆめ太カンパニー", "tyoアニメーションズ", "tyo animations", "ハルフィルムメーカー")),
    "radix": ("RADIX", ("radix", "radix ace entertainment")),
}
COMMON_COUNTRIES = ("JP", "CN", "US", "GB", "FR", "KR", "RU", "CA", "DE")
COUNTRY_TAGS = {
    "CN": ("中国", "国产", "國產", "中国大陆", "中国动画", "中国動畫"),
    "US": ("美国", "美國", "美国动画", "american animation"),
    "GB": ("英国", "英國", "british animation"),
    "FR": ("法国", "法國", "法国动画", "french animation"),
    "KR": ("韩国", "韓國", "韩国动画", "한국 애니메이션"),
    "RU": ("俄罗斯", "俄羅斯", "苏联动画", "蘇聯動畫", "russian animation"),
    "CA": ("加拿大", "canadian animation"),
    "DE": ("德国", "德國", "german animation"),
    "JP": ("日本动画", "日本動畫", "日本アニメ"),
}


class RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


IMAGE_LIMITER = RateLimiter(0.8)
RECENT_LOGS: deque[dict[str, str]] = deque(maxlen=300)
OPTIONS_CACHE: dict[tuple[str, int, int, int], dict[str, Any]] = {}
OPTIONS_CACHE_LOCK = threading.Lock()
# Heavy catalog writers share one in-process gate. SQLite WAL keeps reads
# responsive, while this prevents startup sync, audits and client monitors from
# repeatedly timing out against each other.
DATABASE_MAINTENANCE_LOCK = threading.RLock()
# Targeted Ani-RSS calls use only their own overlay tables and remote API.
# Keeping this lock separate prevents a long torrent/library scan from making
# an interactive Ani-RSS query appear to depend on qBittorrent.
ANI_RSS_OPERATION_LOCK = threading.RLock()


def log_event(level: str, message: str, **details: Any) -> None:
    RECENT_LOGS.appendleft({"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                            "level": level.upper(), "message": message,
                            "details": json.dumps(details, ensure_ascii=False) if details else ""})


def instance_random_seed(db_path: Path) -> str:
    if not db_path.is_file():
        return ""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='instance_random_seed'").fetchone()
            return str(row[0]) if row and row[0] else ""
    except sqlite3.Error:
        return ""


def ensure_instance_random_seed(db_path: Path) -> tuple[str, bool]:
    """Persist one seed for a populated instance; empty bootstrap shells stay seedless."""
    if not db_path.is_file():
        return "", False
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            return "", False
        existing = db.execute("SELECT value FROM metadata WHERE key='instance_random_seed'").fetchone()
        if existing and existing[0]:
            return str(existing[0]), False
        count = db.execute("SELECT value FROM metadata WHERE key='record_count'").fetchone()
        if not count or int(count[0] or 0) <= 0:
            return "", False
        seed = secrets.token_urlsafe(18)
        db.execute("INSERT INTO metadata(key,value) VALUES('instance_random_seed',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (seed,))
        return seed, True


def rotate_instance_random_seed(db_path: Path) -> str:
    seed = secrets.token_urlsafe(18)
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("INSERT INTO metadata(key,value) VALUES('instance_random_seed',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (seed,))
    return seed


class JsonClient:
    def __init__(self, cache_dir: Path, cache_days: int, refresh: bool, interval: float) -> None:
        self.cache_dir = cache_dir
        self.cache_days = cache_days
        self.refresh = refresh
        self.limiter = RateLimiter(interval)
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, url: str, retries: int = 6) -> Any:
        cached = self._cache_path(url)
        if cached.exists() and not self.refresh:
            age = time.time() - cached.stat().st_mtime
            if self.cache_days == 0 or age <= self.cache_days * 86400:
                return json.loads(cached.read_text(encoding="utf-8"))

        last_error: Exception | None = None
        for attempt in range(retries):
            self.limiter.wait()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with tls_support.urlopen(request, timeout=45, max_bytes=4 * 1024 * 1024) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                tmp = cached.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, cached)
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60.0, 2.0**attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                delay = min(60.0, 2.0**attempt)
            print(f"[retry {attempt + 1}/{retries}] {url} in {delay:.1f}s: {last_error}", file=sys.stderr)
            time.sleep(delay)
        raise RuntimeError(f"request failed after {retries} attempts: {url}") from last_error


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with tls_support.urlopen(request, timeout=45, max_bytes=4 * 1024 * 1024) as response:
        return json.loads(response.read().decode("utf-8"))


def ensure_archive(archive_dir: Path, supplied: Path | None = None,
                   network: dict[str, Any] | None = None,
                   progress_callback: Callable[[dict[str, Any]], None] | None = None) -> tuple[Path, dict[str, Any]]:
    """Resolve, download and verify the current official Bangumi Archive."""
    def emit(phase: str, **details: Any) -> None:
        if progress_callback:
            progress_callback({"phase": phase, **details})

    emit("archive_resolve")
    if supplied:
        path = supplied.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        receipt = path.with_suffix(path.suffix + ".verified.json")
        stat = path.stat()
        actual_hash = file_sha256(path)
        verified_at = None
        if receipt.exists():
            try:
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                if int(saved.get("size", -1)) == stat.st_size and str(saved.get("sha256") or "").casefold() == actual_hash:
                    verified_at = saved.get("verified_at")
            except (OSError, ValueError, TypeError):
                pass
        write_archive_receipt(receipt, actual_hash, stat.st_size, source=path)
        emit("archive_ready", received=stat.st_size, total=stat.st_size)
        return path, {"name": path.name, "digest": f"sha256:{actual_hash}", "created_at": verified_at}

    archive_dir.mkdir(parents=True, exist_ok=True)
    print("[archive] Checking Bangumi Archive release metadata...", flush=True)
    network = network or {}
    descriptor, descriptor_endpoint = network_sources.fetch_json(
        network.get("archiveManifestEndpoints") or [LATEST_ARCHIVE_URL],
        timeout=float(network.get("probeTimeoutSeconds", 12)),
        cooldown=int(network.get("failureCooldownSeconds", 900)),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    descriptor["resolved_manifest_endpoint"] = descriptor_endpoint
    path = archive_dir / descriptor["name"]
    expected_size = int(descriptor["size"])
    expected_hash = str(descriptor["digest"]).removeprefix("sha256:").lower()
    receipt = path.with_suffix(path.suffix + ".verified.json")

    imports_dir = Path(os.getenv("ANM_IMPORTS_DIR", str(PROJECT_ROOT / "imports")))
    imported = imports_dir / descriptor["name"]
    if imported.is_file() and imported.stat().st_size == expected_size and file_sha256(imported) == expected_hash:
        write_archive_receipt(receipt, expected_hash, expected_size, source=imported)
        print(f"[archive] Using verified imported archive: {imported.name}", flush=True)
        emit("archive_ready", received=expected_size, total=expected_size)
        return imported, descriptor
    if path.exists() and receipt.exists() and path.stat().st_size == expected_size:
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            saved = {}
        if saved.get("sha256") == expected_hash and file_sha256(path) == expected_hash:
            write_archive_receipt(receipt, expected_hash, expected_size, source=path)
            print(f"[archive] Reusing verified archive: {path.name}", flush=True)
            emit("archive_ready", received=expected_size, total=expected_size)
            return path, descriptor

    # A user may have downloaded the official archive in a browser.  Adopt it
    # only after matching the signed release descriptor's exact size/hash.
    if path.exists() and not receipt.exists():
        if path.stat().st_size == expected_size and file_sha256(path) == expected_hash:
            write_archive_receipt(receipt, expected_hash, expected_size, source=path)
            print(f"[archive] Adopted and verified archive: {path.name}", flush=True)
            emit("archive_ready", received=expected_size, total=expected_size)
            return path, descriptor
        rejected = path.with_name(path.name + ".invalid-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        os.replace(path, rejected)
        print(f"[archive] Preserved invalid/unverified archive as: {rejected.name}", file=sys.stderr, flush=True)

    print(f"[archive] Downloading {descriptor['name']} ({expected_size / 1048576:.1f} MiB)...", flush=True)
    last_print = [0.0]
    def progress(value: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - last_print[0] >= 5:
            received = int(value["received"])
            metrics.progress(f"[archive] download {received / 1048576:.1f}/{expected_size / 1048576:.1f} MiB ({received / expected_size:.0%})")
            emit("archive_download", received=received, total=expected_size,
                 speed=float(value.get("speed", 0)))
            last_print[0] = now
    result = network_downloads.download_verified(
        network_sources.asset_urls(descriptor["browser_download_url"], network.get("archiveAssetProxyTemplates", [])),
        path, expected_size=expected_size, expected_sha256=expected_hash, progress=progress,
        segments=max(1, min(4, int(network.get("archiveDownloadSegments", 2)))),
        attempts_per_source=max(1, min(8, int(network.get("maximumAttemptsPerEndpoint", 3)))))
    write_archive_receipt(receipt, result["sha256"], result["size"], source=path)
    metrics.end_progress()
    print(f"[archive] Verified: {path}", flush=True)
    emit("archive_ready", received=expected_size, total=expected_size)
    return path, descriptor


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_archive_receipt(path: Path, digest: str, size: int, *, source: Path | None = None) -> None:
    payload: dict[str, Any] = {
        "sha256": digest, "size": int(size),
        "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if source is not None:
        try:
            stat = source.stat()
            payload.update({"mtime_ns": int(stat.st_mtime_ns), "device": int(stat.st_dev), "inode": int(stat.st_ino)})
        except OSError:
            pass
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def zip_member(archive: zipfile.ZipFile, basename: str) -> str:
    matches = [name for name in archive.namelist() if Path(name).name == basename]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {basename} in archive, found {len(matches)}")
    return matches[0]


def iter_jsonlines(archive: zipfile.ZipFile, basename: str) -> Iterable[dict[str, Any]]:
    member = zip_member(archive, basename)
    with archive.open(member) as stream:
        for line_no, raw in enumerate(stream, 1):
            if raw.strip():
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"invalid JSON in {basename}:{line_no}") from exc


def scan_rows(archive: zipfile.ZipFile, basename: str, predicate) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for count, row in enumerate(iter_jsonlines(archive, basename), 1):
        if predicate(row):
            rows.append(row)
        if count % 500_000 == 0:
            metrics.progress(f"[archive] {basename}: scanned {count:,}, selected {len(rows):,}")
    metrics.end_progress()
    return rows


def parse_archive_infobox(raw: str | None) -> dict[str, list[str]]:
    """Parse the common Bangumi wiki-field forms without interpreting markup as HTML."""
    if not raw:
        return {}
    fields: dict[str, list[str]] = {}
    current: str | None = None
    chunks: dict[str, list[str]] = {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^\s*\|\s*([^=]+?)\s*=\s*(.*)$", line)
        if match:
            current = match.group(1).strip()
            chunks.setdefault(current, []).append(match.group(2).strip())
        elif current and line.strip() not in {"}}", "}"}:
            chunks[current].append(line.strip())
    for key, lines in chunks.items():
        text = "\n".join(lines).strip().strip("{} ")
        bracketed = re.findall(r"\[\s*(?:[^\]|]+\|)?([^\]]+?)\s*\]", text)
        if bracketed:
            values = bracketed
        else:
            values = re.split(r"(?:<br\s*/?>|\n|、|;)", text, flags=re.IGNORECASE)
        cleaned = unique(re.sub(r"\{\{.*?\}\}|\[\[|\]\]", "", value).strip(" {}|") for value in values)
        if cleaned:
            fields[key] = cleaned
    return fields


def related_subject_kind(subject: dict[str, Any]) -> str:
    """Classify a related Archive subject without online lookups."""
    subject_type = int(subject.get("type", 0) or 0)
    if subject_type == 2:
        return "anime"
    if subject_type == 3:
        return "music"
    if subject_type == 4:
        return "game"
    if subject_type == 6:
        return "live_action"
    if subject_type != 1:
        return "other"
    platform = int(subject.get("platform", 0) or 0)
    infobox = str(subject.get("infobox") or "")
    tags = " ".join(str(x.get("name", "")) if isinstance(x, dict) else str(x) for x in subject.get("tags") or [])
    evidence = unicodedata.normalize("NFKC", f"{infobox} {tags}").casefold()
    if any(value in evidence for value in ("light novel", "lightnovel", "ライトノベル", "轻小说", "輕小說")):
        return "light_novel"
    if platform == 1001 or any(value in evidence for value in ("animanga/manga", "漫画", "漫畫", "コミック")):
        return "manga"
    if platform == 1002 or any(value in evidence for value in ("animanga/novel", "小说", "小說", "小説")):
        return "novel"
    return "book"


def related_subject_metadata(subject: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
    """Keep compact, redistributable Archive evidence for non-anime relations."""
    info = parse_archive_infobox(subject.get("infobox"))
    tags = [
        str(value.get("name", "") if isinstance(value, dict) else value).strip()
        for value in subject.get("tags") or []
    ]
    normalized_tags = " ".join(tags).casefold()
    kind = kind or related_subject_kind(subject)
    role = kind
    if kind == "music":
        title_evidence = unicodedata.normalize("NFKC", str(subject.get("name") or subject.get("name_cn") or "")).casefold()
        if any(value in title_evidence for value in (
            "theme song collection", "theme songs collection", "主題歌集", "主题曲集", "主題曲集",
        )):
            role = "theme_collection"
        role_rules = (
            ("character_song", ("角色歌", "キャラクターソング", "character song")),
            ("opening", ("op", "opening", "片头曲", "片頭曲", "オープニング")),
            ("ending", ("ed", "ending", "片尾曲", "エンディング")),
            ("soundtrack", ("ost", "soundtrack", "原声", "原聲", "サウンドトラック")),
        )
        if role != "theme_collection":
            role = next(
                (code for code, aliases in role_rules if any(
                    re.search(rf"(?:^|\W){re.escape(alias.casefold())}(?:$|\W)", normalized_tags)
                    if alias.isascii() and len(alias) <= 3 else alias.casefold() in normalized_tags
                    for alias in aliases
                )),
                "music",
            )

    def values(*keys: str) -> list[str]:
        return unique(value for key in keys for value in info.get(key, []))

    return {
        "title": str(subject.get("name") or subject.get("name_cn") or f"Bangumi #{subject.get('id', '')}"),
        "kind": kind,
        "role": role,
        "date": str(subject.get("date") or ""),
        "authors": values("作者", "原作者", "著者"),
        "publishers": values("出版社", "出版社・メーカー", "发行", "發行"),
        "artists": values("作画", "作畫", "艺术家", "藝術家", "表演者", "歌手", "作词", "作詞", "作曲"),
    }


def infer_language(title: str) -> str:
    if re.search(r"[\u3040-\u30ff]", title):
        return "ja"
    ascii_letters = len(re.findall(r"[A-Za-z]", title))
    visible = len(re.findall(r"\S", title)) or 1
    if ascii_letters / visible >= 0.45:
        return "en"
    if re.search(r"[\u3400-\u9fff]", title):
        return "zh"
    return "und"


def choose_display_english_title(titles: Iterable[dict[str, str] | str]) -> str | None:
    """Choose a readable English display title while retaining every alias for search.

    Archive aliases are community-entered and ordered for editing, not display.
    Keep the first candidate unless it has strong shorthand/truncation evidence;
    this avoids rewriting intentional lowercase brands merely for typography.
    """
    candidates = unique(
        str(item.get("title", "") if isinstance(item, dict) else item).strip()
        for item in titles
        if not isinstance(item, dict) or item.get("language") == "en"
    )
    if not candidates:
        return None
    current = candidates[0]
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

    def first_letter(value: str) -> str:
        match = re.search(r"[A-Za-z]", value)
        return match.group(0) if match else ""

    pure_latin_alternatives = [value for value in candidates[1:] if not cjk.search(value)]
    longer = [value for value in pure_latin_alternatives if len(value) >= len(current) + 4]
    first_word = re.search(r"[A-Za-z]+", current)
    missing_leading = [
        value for value in pure_latin_alternatives
        if first_word and first_word.group(0)[0].islower()
        and (other := re.search(r"[A-Za-z]+", value))
        and other.group(0)[1:].casefold() == first_word.group(0).casefold()
    ]
    compact_code = bool(re.fullmatch(r"(?:[A-Z0-9+._-]{2,4}|[a-z0-9+._-]{1,4})", current))
    suspicious = bool(missing_leading) or (compact_code and bool(longer))
    if not suspicious:
        return current

    replacements = missing_leading or pure_latin_alternatives or candidates[1:]
    if not replacements:
        return current

    def quality(value: str) -> tuple[float, int]:
        words = re.findall(r"[A-Za-z0-9]+", value)
        score = min(len(value), 72) * 0.12 + min(len(words), 10) * 2.5
        score += 12 if first_letter(value).isupper() else -12
        score -= 30 if cjk.search(value) else 0
        score -= 24 if re.fullmatch(r"[A-Za-z0-9+._-]{1,4}", value) else 0
        score -= 8 if "," in value else 0
        return score, -candidates.index(value)

    return max(replacements, key=quality)


def refresh_display_english_titles(db_path: Path) -> dict[str, int]:
    """Re-evaluate only suspicious English display choices from stored aliases."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=120)) as db:
        rows = list(db.execute("SELECT id,title_en FROM anime_work WHERE title_en IS NOT NULL"))
        updates: list[tuple[str, int]] = []
        for anime_id, current in rows:
            aliases = [str(current), *[
                str(row[0]) for row in db.execute(
                    "SELECT title FROM anime_title WHERE anime_id=? AND language='en' ORDER BY rowid",
                    (anime_id,),
                ) if str(row[0]) != str(current)
            ]]
            selected = choose_display_english_title(aliases)
            if selected and selected != current:
                updates.append((selected, int(anime_id)))
        with db:
            db.executemany("UPDATE anime_work SET title_en=? WHERE id=?", updates)
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('english_display_title_policy','quality-v1')"
            )
        return {"examined": len(rows), "updated": len(updates)}


def parse_start_month(raw: str | None) -> tuple[str, str]:
    raw = (raw or "").strip()
    match = re.match(r"^(\d{4})[-/](\d{1,2})", raw)
    if match:
        value = f"{match.group(1)}-{int(match.group(2)):02d}"
        return value, value.replace("-", "_")
    match = re.match(r"^(\d{4})", raw)
    if match:
        value = f"{match.group(1)}-XX"
        return value, value.replace("-", "_")
    return "20XX-XX", "20XX_XX"


def choose_source_type(tags: list[str], info: dict[str, list[str]]) -> str | None:
    source_evidence = unicodedata.normalize("NFKC", " ".join(info.get("原作", []))).casefold()
    tag_evidence = unicodedata.normalize("NFKC", " ".join(tags)).casefold()
    rules = [
        ("轻小说改", ("轻小说", "輕小說", "ライトノベル", "light novel"), ("轻小说改", "輕小說改", "轻小说原作", "ライトノベル原作")),
        ("漫画改", ("漫画", "漫畫", "マンガ", "コミック"), ("漫画改", "漫畫改", "漫画原作", "漫畫原作", "マンガ原作", "コミック原作")),
        ("游戏改", ("游戏", "遊戲", "ゲーム", "game"), ("游戏改", "遊戲改", "游戏原作", "ゲーム原作", "game adaptation")),
        ("小说改", ("小说", "小說", "小説", "novel"), ("小说改", "小說改", "小说原作", "小説原作", "novel adaptation")),
        ("原创", ("原创", "原創", "オリジナル", "original"), ("原创", "原創", "オリジナルアニメ", "original anime")),
    ]
    for label, source_needles, tag_needles in rules:
        if any(needle.casefold() in source_evidence for needle in source_needles) or any(
            needle.casefold() in tag_evidence for needle in tag_needles
        ):
            return label
    return None


def source_code(label: str | None) -> str:
    return {"轻小说改": "light_novel", "漫画改": "manga", "游戏改": "game",
            "小说改": "novel", "原创": "original"}.get(label or "", "unknown")


SOURCE_KIND_LABELS = {
    "light_novel": "轻小说改",
    "manga": "漫画改",
    "game": "游戏改",
    "novel": "小说改",
}


def source_type_from_relations(relations: Iterable[dict[str, Any]]) -> str | None:
    """Infer an adaptation type only when Archive relation evidence is unambiguous."""
    kinds = {
        str(relation.get("related_subject_kind") or "")
        for relation in relations
        if relation.get("relation_code") == "adaptation"
        and str(relation.get("related_subject_kind") or "") in SOURCE_KIND_LABELS
    }
    return SOURCE_KIND_LABELS[next(iter(kinds))] if len(kinds) == 1 else None


def _tag_record(value: Any, rank: int = 0) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "name": str(value.get("name") or value.get("tag") or "").strip(),
            "count": max(0, int(value.get("count") or value.get("vote_count") or 0)),
            "rank": max(0, int(value.get("rank") or value.get("tag_rank") or rank)),
        }
    return {"name": str(value).strip(), "count": 0, "rank": max(0, rank)}


def _tag_matches(tag: str, alias: str) -> bool:
    clean_tag = unicodedata.normalize("NFKC", tag).casefold().strip()
    clean_alias = unicodedata.normalize("NFKC", alias).casefold().strip()
    return clean_tag == clean_alias or (len(clean_alias) >= 3 and clean_alias in clean_tag)


def filtered_archive_tags(values: Iterable[Any], limit: int = 16) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, value in enumerate(values, 1):
        record = _tag_record(value, rank)
        key = record["name"].casefold()
        if not record["name"] or key in GENERIC_TAGS or re.fullmatch(r"\d{4}", record["name"]) or key in seen:
            continue
        seen.add(key)
        result.append(record)
        if len(result) == limit:
            break
    return result


def descriptive_display_tag(tag: str, studios: Iterable[str]) -> bool:
    """Hide release/studio metadata while retaining voted descriptive tags."""
    raw = unicodedata.normalize("NFKC", str(tag)).strip()
    key = re.sub(r"[\s·・･._/\-]+", "", raw).casefold()
    if not raw or raw.casefold() in DISPLAY_TAG_METADATA or DISPLAY_TAG_DATE_RE.fullmatch(raw):
        return False
    for studio in studios:
        studio_key = re.sub(r"[\s·・･._/\-]+", "", unicodedata.normalize("NFKC", str(studio))).casefold()
        if studio_key and key == studio_key:
            return False
    return True


def derive_theme_evidence(tags: Iterable[Any]) -> list[dict[str, Any]]:
    """Derive stable UI themes while retaining auditable Archive evidence."""
    records = [_tag_record(value, index) for index, value in enumerate(tags, 1)]
    records = [record for record in records if record["name"]]
    top_count = max((record["count"] for record in records), default=0)
    output: list[dict[str, Any]] = []
    for code, aliases in THEME_CLUSTERS.items():
        matches = [record for record in records if any(_tag_matches(record["name"], alias) for alias in aliases)]
        if not matches:
            continue
        accepted = True
        confidence = 1.0
        reasons = ["semantic_alias"]
        if code == "comedy":
            strong = [r for r in records if any(_tag_matches(r["name"], x) for x in COMEDY_STRONG_ALIASES)]
            weak = [r for r in records if any(_tag_matches(r["name"], x) for x in COMEDY_WEAK_ALIASES)]
            auxiliary = [r for r in records if any(_tag_matches(r["name"], x) for x in COMEDY_AUXILIARY_ALIASES)]
            if strong:
                confidence, reasons = 1.0, ["strong_semantic_tag"]
            else:
                best = max(weak, key=lambda r: (r["count"], -r["rank"]), default={"count": 0, "rank": 0})
                ratio = (best["count"] / top_count) if top_count else 0.0
                prominent = bool(best["rank"] and best["rank"] <= 6 and ratio >= 0.25)
                corroborated = bool(auxiliary)
                accepted = corroborated or prominent
                confidence = 0.82 if corroborated else (min(0.9, 0.55 + 0.25 * ratio + 0.1 * (7 - best["rank"]) / 6) if prominent else 0.25)
                reasons = (["corroborating_comedy_tag"] if corroborated else []) + (["prominent_archive_tag"] if prominent else [])
                if not reasons:
                    reasons = ["weak_uncorroborated_tag"]
        output.append({
            "theme_code": code,
            "confidence": round(confidence, 4),
            "accepted": int(accepted),
            "evidence": {
                "rule": THEME_RULE_VERSION,
                "reasons": reasons,
                "tags": matches,
            },
        })
    return output


def theme_codes(tags: Iterable[Any]) -> list[str]:
    return [row["theme_code"] for row in derive_theme_evidence(tags) if row["accepted"]]


def rebuild_theme_clusters(db: sqlite3.Connection) -> None:
    """Recompute the derived taxonomy from retained Archive tags."""
    db.execute("DELETE FROM anime_theme")
    db.execute("DELETE FROM anime_theme_evidence")
    by_work: dict[int, list[dict[str, Any]]] = {}
    for anime_id, tag, count, rank in db.execute("SELECT anime_id,tag,vote_count,tag_rank FROM anime_tag"):
        by_work.setdefault(int(anime_id), []).append({"name": str(tag), "count": int(count), "rank": int(rank)})
    evidence_rows: list[tuple[Any, ...]] = []
    accepted_rows: list[tuple[int, str]] = []
    for anime_id, tags in by_work.items():
        for row in derive_theme_evidence(tags):
            evidence_rows.append((anime_id, row["theme_code"], row["confidence"], row["accepted"],
                                  json.dumps(row["evidence"], ensure_ascii=False), THEME_RULE_VERSION))
            if row["accepted"]:
                accepted_rows.append((anime_id, row["theme_code"]))
    db.executemany("INSERT OR IGNORE INTO anime_theme VALUES(?,?)", accepted_rows)
    db.executemany("INSERT OR REPLACE INTO anime_theme_evidence VALUES(?,?,?,?,?,?)", evidence_rows)


def refresh_tag_evidence_from_archive(db_path: Path, archive_path: Path) -> dict[str, int]:
    """Refresh retained tag votes/ranks in one Archive pass without touching runtime state."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=120)) as db:
        migrate_catalog_features(db)
        targets = {int(bgm_id): int(anime_id) for anime_id, bgm_id in db.execute("SELECT id,bgm_id FROM anime_work")}
        staged: list[tuple[int, str, int, int]] = []
        matched: set[int] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for subject in iter_jsonlines(archive, "subject.jsonlines"):
                bgm_id = int(subject.get("id", -1))
                if bgm_id not in targets:
                    continue
                anime_id = targets[bgm_id]
                matched.add(anime_id)
                staged.extend((anime_id, tag["name"], tag["count"], tag["rank"])
                              for tag in filtered_archive_tags(subject.get("tags") or []))
        with db:
            db.execute("CREATE TEMP TABLE refreshed_anime(id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO refreshed_anime VALUES(?)", [(value,) for value in matched])
            db.execute("DELETE FROM anime_tag WHERE anime_id IN (SELECT id FROM refreshed_anime)")
            db.executemany("INSERT OR IGNORE INTO anime_tag(anime_id,tag,vote_count,tag_rank) VALUES(?,?,?,?)", staged)
            rebuild_theme_clusters(db)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('theme_rule_version',?)", (THEME_RULE_VERSION,))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('tag_evidence_refreshed_at',?)",
                       (dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),))
        return {"works": len(matched), "tags": len(staged)}


def refresh_relation_subject_metadata_from_archive(db_path: Path, archive_path: Path) -> dict[str, int]:
    """Backfill original titles and compact related-subject evidence in one Archive pass."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=120)) as db:
        migrate_catalog_features(db)
        related_ids = {int(row[0]) for row in db.execute("SELECT DISTINCT related_bgm_id FROM anime_relation WHERE related_bgm_id IS NOT NULL")}
        metadata: dict[int, tuple[int, str, str, str]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for subject in iter_jsonlines(archive, "subject.jsonlines"):
                subject_id = int(subject.get("id", -1))
                if subject_id in related_ids:
                    detail = related_subject_metadata(subject)
                    metadata[subject_id] = (
                        int(subject.get("type", 0) or 0),
                        str(detail["kind"]),
                        str(detail["title"]),
                        json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                    )
        with db:
            db.execute("CREATE TEMP TABLE relation_subject_stage(bgm_id INTEGER PRIMARY KEY,subject_type INTEGER,subject_kind TEXT,original_title TEXT,meta_json TEXT)")
            db.executemany("INSERT INTO relation_subject_stage VALUES(?,?,?,?,?)",
                           [(subject_id, subject_type, kind, title, meta_json)
                            for subject_id, (subject_type, kind, title, meta_json) in metadata.items()])
            db.execute("""UPDATE anime_relation SET
                related_subject_type=(SELECT subject_type FROM relation_subject_stage s WHERE s.bgm_id=anime_relation.related_bgm_id),
                related_subject_kind=(SELECT subject_kind FROM relation_subject_stage s WHERE s.bgm_id=anime_relation.related_bgm_id),
                related_title=(SELECT original_title FROM relation_subject_stage s WHERE s.bgm_id=anime_relation.related_bgm_id),
                related_subject_meta_json=(SELECT meta_json FROM relation_subject_stage s WHERE s.bgm_id=anime_relation.related_bgm_id)
                WHERE related_bgm_id IN (SELECT bgm_id FROM relation_subject_stage)""")
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('relation_subject_metadata_refreshed_at',?)",
                       (dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),))
        return {"requested": len(related_ids), "resolved": len(metadata)}


def studio_key(name: str) -> tuple[str, str | None]:
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    for key, (label, aliases) in STUDIO_FAMILIES.items():
        if any(normalized == alias.casefold() or re.match(re.escape(alias.casefold()) + r"(?:\s|[/／×&+])", normalized) for alias in aliases):
            return f"family:{key}", label
    key = re.sub(r"(?:株式会社|有限会社|合同会社|incorporated|corporation|corp\.?|inc\.?|ltd\.?)", "", normalized)
    key = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", key)
    return f"name:{key or normalized}", None


def _studio_looks_independent(name: str, direct_names: set[str]) -> bool:
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    compact = re.sub(r"\s+", "", normalized)
    return (
        compact in direct_names
        or any(token in normalized for token in ("studio", "スタジオ", "animation", "アニメーション"))
    )


def split_studio_credit(name: str, direct_names: set[str] | None = None) -> list[str]:
    """Split collaboration credits without confusing a rename family with a co-producer."""
    source = unicodedata.normalize("NFKC", str(name)).strip()
    direct_names = direct_names or set()
    # Slash, multiplication, ampersand and plus are common collaboration separators.
    parts = unique(re.split(r"\s*(?:[/／×＆&]|\s+[xX+]\s+)\s*", source))
    output: list[str] = []
    for part in parts:
        match = re.fullmatch(r"(.+?)\s*[（(]([^()（）]+)[）)]", part)
        if match and _studio_looks_independent(match.group(2), direct_names):
            output.extend((match.group(1), match.group(2)))
        else:
            output.append(part)
    return unique(output)


def infer_country_codes(tags: Iterable[str], title: str, studios: Iterable[str]) -> list[tuple[str, str]]:
    clean_tags = {unicodedata.normalize("NFKC", str(tag)).strip().casefold() for tag in tags}
    result: dict[str, str] = {}
    for code, aliases in COUNTRY_TAGS.items():
        if any(tag == alias.casefold() or tag.startswith(alias.casefold() + "动画") for tag in clean_tags for alias in aliases):
            result[code] = "archive_tag"
    studio_text = " ".join(studios).casefold()
    studio_country = {
        "US": ("disney", "pixar", "warner bros", "dreamworks", "nickelodeon", "cartoon network", "mgm"),
        "CN": ("上海美术电影制片厂", "玄机", "若鸿", "咏声", "福煦", "艺画开天", "原力动画"),
        "RU": ("союзмультфильм",),
    }
    for code, aliases in studio_country.items():
        if any(alias in studio_text for alias in aliases):
            result.setdefault(code, "studio")
    if re.search(r"[\u3040-\u30ff]", title):
        result.setdefault("JP", "original_title_script")
    return sorted(result.items()) or [("OTHER", "insufficient_country_evidence")]


def rebuild_studio_clusters(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM anime_studio_cluster")
    raw = list(db.execute("SELECT anime_id,studio FROM anime_studio"))
    direct_names = {
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", component).casefold())
        for _, source in raw
        for component in re.split(r"\s*(?:[/／×＆&]|\s+[xX+]\s+)\s*", str(source))
        if component and not re.search(r"[（(].+[）)]", component)
    }
    counts: dict[tuple[str, str], int] = {}
    keyed: list[tuple[int, str, str, str | None]] = []
    for anime_id, name in raw:
        for component in split_studio_credit(str(name), direct_names):
            key, fixed = studio_key(component)
            keyed.append((int(anime_id), component, key, fixed))
            counts[(key, component)] = counts.get((key, component), 0) + 1
    labels: dict[str, str] = {}
    for _, name, key, fixed in keyed:
        if fixed:
            labels[key] = fixed
        elif key not in labels or counts[(key, name)] > counts.get((key, labels[key]), -1):
            labels[key] = name
    db.executemany("INSERT OR IGNORE INTO anime_studio_cluster(anime_id,cluster_key,cluster_name,studio_name) VALUES(?,?,?,?)",
                   [(anime_id, key, labels[key], name) for anime_id, name, key, _ in keyed])


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def wikidata_titles(client: JsonClient, manifest: list[dict[str, Any]]) -> dict[str, dict[str, list[str] | str]]:
    qids = [str(item["wikidata_id"]) for item in manifest if item.get("wikidata_id")]
    if not qids:
        return {}
    params = urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(qids), "props": "labels|aliases", "languages": "en|ja|zh|zh-hans", "format": "json", "origin": "*"
    })
    payload = client.get(f"https://www.wikidata.org/w/api.php?{params}")
    output: dict[str, dict[str, list[str] | str]] = {}
    for qid, entity in (payload.get("entities") or {}).items():
        labels = entity.get("labels") or {}
        aliases = entity.get("aliases") or {}
        entry: dict[str, list[str] | str] = {}
        for lang in ("en", "ja", "zh-hans", "zh"):
            if lang in labels:
                entry[lang] = labels[lang]["value"]
            if lang in aliases:
                entry[f"{lang}_aliases"] = [x["value"] for x in aliases[lang]]
        output[qid] = entry
    return output


@dataclass
class BuildItem:
    manifest: dict[str, Any]
    work: dict[str, Any]
    titles: list[dict[str, str]]
    tags: list[dict[str, Any]]
    staff: list[dict[str, Any]]
    cast: list[dict[str, Any]]
    relations: list[dict[str, Any]]


def build_items_from_archive(archive_path: Path, manifest: list[dict[str, Any]] | None,
                             wd: dict[str, dict[str, list[str] | str]]) -> list[BuildItem]:
    target_ids = {int(item["bgm_id"]) for item in (manifest or [])}
    manifest_by_id = {int(item["bgm_id"]): item for item in (manifest or [])}
    print(f"[archive] Reading {'all animation works' if manifest is None else f'{len(target_ids)} selected works'} from {archive_path.name}...", flush=True)
    with zipfile.ZipFile(archive_path) as archive:
        subjects: dict[int, dict[str, Any]] = {}
        related_subjects: dict[int, dict[str, Any]] = {}
        for row in iter_jsonlines(archive, "subject.jsonlines"):
            subject_id = int(row.get("id", -1))
            related_subjects[subject_id] = {key: row.get(key) for key in (
                "id", "type", "name", "name_cn", "infobox", "platform", "series", "tags", "date"
            )}
            if (manifest is None and int(row.get("type", -1)) == 2) or subject_id in target_ids:
                subjects[subject_id] = row
        if manifest is None:
            target_ids = set(subjects)
            manifest_by_id = {subject_id: {"bgm_id": subject_id} for subject_id in target_ids}
        missing = sorted(target_ids - subjects.keys())
        if missing:
            raise RuntimeError(f"subjects absent from archive: {missing}; enable a future online supplement or choose archived IDs")

        subject_persons = scan_rows(archive, "subject-persons.jsonlines", lambda r: int(r.get("subject_id", -1)) in target_ids)
        subject_characters = scan_rows(archive, "subject-characters.jsonlines", lambda r: int(r.get("subject_id", -1)) in target_ids)
        person_characters = scan_rows(archive, "person-characters.jsonlines", lambda r: int(r.get("subject_id", -1)) in target_ids)
        relation_rows_raw = scan_rows(archive, "subject-relations.jsonlines", lambda r: int(r.get("subject_id", -1)) in target_ids)
        episode_count: dict[int, int] = {}
        selected_episodes = 0
        for count, row in enumerate(iter_jsonlines(archive, "episode.jsonlines"), 1):
            sid = int(row.get("subject_id", -1))
            if sid in target_ids and int(row.get("type", -1)) == 0:
                episode_count[sid] = episode_count.get(sid, 0) + 1
                selected_episodes += 1
            if count % 500_000 == 0:
                metrics.progress(f"[archive] episode.jsonlines: scanned {count:,}, selected {selected_episodes:,}")
        metrics.end_progress()

        person_ids = {int(row["person_id"]) for row in subject_persons + person_characters}
        character_ids = {int(row["character_id"]) for row in subject_characters + person_characters}
        people = {int(row["id"]): row for row in scan_rows(archive, "person.jsonlines", lambda r: int(r.get("id", -1)) in person_ids)}
        characters = {int(row["id"]): row for row in scan_rows(archive, "character.jsonlines", lambda r: int(r.get("id", -1)) in character_ids)}

    positions_by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in subject_persons:
        positions_by_subject.setdefault(int(row["subject_id"]), []).append(row)
    chars_by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in subject_characters:
        chars_by_subject.setdefault(int(row["subject_id"]), []).append(row)
    actors_by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in person_characters:
        actors_by_subject.setdefault(int(row["subject_id"]), []).append(row)
    relations_by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in relation_rows_raw:
        relations_by_subject.setdefault(int(row["subject_id"]), []).append(row)
    output: list[BuildItem] = []
    related_evidence_cache: dict[int, tuple[str, str]] = {}
    for bgm_id in sorted(target_ids):
        subject = subjects[bgm_id]
        manifest_item = manifest_by_id[bgm_id]
        info = parse_archive_infobox(subject.get("infobox"))
        raw_tag_records = [_tag_record(tag, rank) for rank, tag in enumerate(subject.get("tags") or [], 1)]
        raw_tags = [tag["name"] for tag in raw_tag_records]
        start_month, directory_date = parse_start_month(subject.get("date"))

        titles: list[dict[str, str]] = []
        if subject.get("name"):
            titles.append({"language": "ja", "title": subject["name"], "title_type": "primary", "source": "bangumi-archive"})
        if subject.get("name_cn"):
            titles.append({"language": "zh-Hans", "title": subject["name_cn"], "title_type": "primary", "source": "bangumi-archive"})
        for alias in info.get("别名", []) + info.get("別名", []):
            titles.append({"language": infer_language(alias), "title": alias, "title_type": "alias", "source": "bangumi-archive"})
        wd_entry = wd.get(str(manifest_item.get("wikidata_id", "")), {})
        for lang in ("en", "ja", "zh-hans", "zh"):
            normalized = "zh-Hans" if lang == "zh-hans" else lang
            label = wd_entry.get(lang)
            if isinstance(label, str):
                titles.append({"language": normalized, "title": label, "title_type": "label", "source": "wikidata"})
            aliases = wd_entry.get(f"{lang}_aliases") or []
            if isinstance(aliases, list):
                titles.extend({"language": normalized, "title": str(alias), "title_type": "alias", "source": "wikidata"} for alias in aliases)
        dedup_titles: list[dict[str, str]] = []
        seen_titles: set[tuple[str, str]] = set()
        for title in titles:
            key = (title["language"], title["title"].casefold())
            if title["title"].strip() and key not in seen_titles:
                seen_titles.add(key)
                dedup_titles.append(title)

        staff: list[dict[str, Any]] = []
        for link in positions_by_subject.get(bgm_id, []):
            person = people.get(int(link["person_id"]))
            if not person:
                continue
            position = int(link.get("position", -1))
            role, role_type = STAFF_POSITIONS.get(position, (f"职员 #{position}", "staff"))
            staff.append({"person_id": person["id"], "name": person["name"], "role": role, "role_type": role_type, "source": "bangumi-archive"})

        char_roles = {int(row["character_id"]): CHARACTER_ROLES.get(int(row.get("type", 0)), "其他") for row in chars_by_subject.get(bgm_id, [])}
        cast: list[dict[str, Any]] = []
        for link in actors_by_subject.get(bgm_id, []):
            person = people.get(int(link["person_id"]))
            character = characters.get(int(link["character_id"]))
            if person and character:
                cast.append({
                    "character_id": character["id"], "character_name": character["name"],
                    "person_id": person["id"], "person_name": person["name"],
                    "character_role": char_roles.get(character["id"], "其他"), "language": "ja", "source": "bangumi-archive"
                })
        cast.sort(key=lambda x: ({"主角": 0, "配角": 1, "客串": 2}.get(x["character_role"], 3), x["character_name"]))

        relations: list[dict[str, Any]] = []
        for link in relations_by_subject.get(bgm_id, []):
            related_id = int(link["related_subject_id"])
            related = related_subjects.get(related_id, {})
            relation_id = int(link.get("relation_type", 99))
            relation_code = RELATION_CODES.get(relation_id, "other")
            cached_evidence = related_evidence_cache.get(related_id)
            if cached_evidence is None:
                related_kind = related_subject_kind(related)
                cached_evidence = (
                    related_kind,
                    json.dumps(related_subject_metadata(related, related_kind), ensure_ascii=False, separators=(",", ":")),
                )
                related_evidence_cache[related_id] = cached_evidence
            related_kind, related_meta_json = cached_evidence
            relations.append({
                "related_bgm_id": related_id, "related_title": related.get("name") or related.get("name_cn") or f"Bangumi #{related_id}",
                "relation_type": RELATIONS.get(relation_id, f"关系 #{relation_id}"), "relation_code": relation_code,
                "strict_group": int(relation_code in STRICT_SERIES_RELATIONS), "source": "bangumi-archive",
                "related_subject_type": int(related.get("type", 0) or 0),
                "related_subject_kind": related_kind,
                "related_subject_meta_json": related_meta_json,
            })

        archive_studios = [x["name"] for x in staff if x["role_type"] == "studio"]
        studios = unique(info.get("动画制作", []) + info.get("動畫製作", []) + archive_studios)
        filtered_tags = filtered_archive_tags(subject.get("tags") or [])
        english = choose_display_english_title(dedup_titles)
        source_label = choose_source_type(raw_tags, info) or source_type_from_relations(relations)
        platform = int(subject.get("platform") or 0)
        work = {
            "bgm_id": bgm_id, "wikidata_id": manifest_item.get("wikidata_id"),
            "title_ja": subject["name"], "title_zh_hans": subject.get("name_cn") or None, "title_en": english,
            "media_type": PLATFORMS.get(platform, f"平台 #{subject.get('platform')}"), "media_code": PLATFORM_CODES.get(platform, "other"),
            "start_month": start_month, "directory_date": directory_date, "raw_date": subject.get("date"),
            "episode_count": episode_count.get(bgm_id) or None, "source_type": source_label, "source_code": source_code(source_label),
            "original_language": "ja",
            "country_code": manifest_item.get("country_code"), "studio": " / ".join(studios) or None,
            "summary": subject.get("summary"), "source_url": f"https://bgm.tv/subject/{bgm_id}"
        }
        output.append(BuildItem(manifest_item, work, dedup_titles, filtered_tags, staff, cast, relations))
        if len(output) % 500 == 0 or len(output) == len(target_ids):
            metrics.progress(f"[catalog] prepared {len(output):,}/{len(target_ids):,} works")
    metrics.end_progress()
    return output


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE anime_work(
  id INTEGER PRIMARY KEY, bgm_id INTEGER NOT NULL UNIQUE, wikidata_id TEXT,
  title_ja TEXT NOT NULL, title_zh_hans TEXT, title_en TEXT, media_type TEXT, media_code TEXT,
  start_month TEXT NOT NULL, directory_date TEXT NOT NULL, raw_date TEXT,
  episode_count INTEGER, source_type TEXT, source_code TEXT, original_language TEXT NOT NULL DEFAULT 'ja', country_code TEXT, studio TEXT,
  summary TEXT, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
  physical_role TEXT NOT NULL DEFAULT 'work', physical_owner_anime_id INTEGER
);
CREATE TABLE anime_title(
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  language TEXT NOT NULL, title TEXT NOT NULL, title_type TEXT NOT NULL, source TEXT NOT NULL,
  UNIQUE(anime_id, language, title)
);
CREATE TABLE anime_tag(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE, tag TEXT NOT NULL, vote_count INTEGER NOT NULL DEFAULT 0, tag_rank INTEGER NOT NULL DEFAULT 0, UNIQUE(anime_id, tag));
CREATE TABLE anime_theme(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE, theme_code TEXT NOT NULL, UNIQUE(anime_id, theme_code));
CREATE TABLE anime_theme_evidence(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,theme_code TEXT NOT NULL,confidence REAL NOT NULL,accepted INTEGER NOT NULL,evidence_json TEXT NOT NULL,rule_version TEXT NOT NULL,UNIQUE(anime_id,theme_code));
CREATE TABLE anime_studio(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE, studio TEXT NOT NULL, UNIQUE(anime_id, studio));
CREATE TABLE anime_studio_cluster(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,cluster_key TEXT NOT NULL,cluster_name TEXT NOT NULL,studio_name TEXT NOT NULL,UNIQUE(anime_id,cluster_key,studio_name));
CREATE TABLE anime_country(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,country_code TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(anime_id,country_code));
CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY REFERENCES anime_work(id) ON DELETE CASCADE, mime_type TEXT, image_blob BLOB, source_url TEXT, etag TEXT, fetched_at TEXT, error TEXT);
CREATE TABLE anime_staff(
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  person_id INTEGER, name TEXT NOT NULL, role TEXT NOT NULL, role_type TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE anime_cast(
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  character_id INTEGER, character_name TEXT, person_id INTEGER, person_name TEXT NOT NULL,
  character_role TEXT, language TEXT, source TEXT NOT NULL
);
CREATE TABLE anime_relation(
  anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  related_bgm_id INTEGER, related_title TEXT, relation_type TEXT NOT NULL,
  relation_code TEXT NOT NULL, strict_group INTEGER NOT NULL CHECK(strict_group IN (0,1)), source TEXT NOT NULL,
  related_subject_type INTEGER, related_subject_kind TEXT, related_subject_meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_work_start ON anime_work(start_month);
CREATE INDEX idx_work_type ON anime_work(media_type);
CREATE INDEX idx_work_source ON anime_work(source_type);
CREATE INDEX idx_title_value ON anime_title(title);
CREATE INDEX idx_tag_value ON anime_tag(tag);
CREATE INDEX idx_theme_value ON anime_theme(theme_code);
CREATE INDEX idx_theme_evidence_anime ON anime_theme_evidence(anime_id,accepted);
CREATE INDEX idx_studio_value ON anime_studio(studio);
CREATE INDEX idx_staff_name ON anime_staff(name);
CREATE INDEX idx_cast_name ON anime_cast(person_name);
CREATE INDEX idx_staff_role_name_anime ON anime_staff(role_type,name,anime_id);
CREATE INDEX idx_cast_person_anime ON anime_cast(person_name,anime_id);
CREATE INDEX idx_tag_anime ON anime_tag(anime_id);
CREATE INDEX idx_theme_anime ON anime_theme(anime_id);
CREATE INDEX idx_studio_anime ON anime_studio(anime_id);
CREATE INDEX idx_studio_cluster_name ON anime_studio_cluster(cluster_name,anime_id);
CREATE INDEX idx_country_code ON anime_country(country_code,anime_id);
CREATE INDEX idx_staff_anime ON anime_staff(anime_id);
CREATE INDEX idx_cast_anime ON anime_cast(anime_id);
"""


def write_database(path: Path, rows: list[BuildItem], archive_meta: dict[str, Any] | None = None,
                   instance_seed: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_path = path.with_suffix(path.suffix + ".next")
    publish_path.unlink(missing_ok=True)
    scratch_root = Path(os.getenv("ANM_CATALOG_BUILD_DIR", tempfile.gettempdir()))
    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
        descriptor, scratch_name = tempfile.mkstemp(
            prefix=f"{path.stem}-", suffix=".sqlite3", dir=scratch_root)
        os.close(descriptor)
        temp_path = Path(scratch_name)
        temp_path.unlink()
    except OSError:
        temp_path = publish_path
    db: sqlite3.Connection | None = None
    try:
        with contextlib.closing(sqlite3.connect(temp_path)) as db, db:
            db.execute("PRAGMA busy_timeout=60000")
            # The build file is disposable and not visible to readers. Avoid
            # WAL/random-write amplification on Docker Desktop and network
            # bind mounts; the validated result is published atomically below.
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            db.executescript(SCHEMA)
            runtime_catalog.migrate_overlay(db)
            ani_rss.migrate(db)
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            metadata_rows = [
                ("schema_version", "1"), ("built_at", now),
                ("record_count", str(len(rows))),
                ("sources", "Bangumi Archive; Wikidata labels/aliases"),
                ("license_notice", "Bangumi entries: CC BY-SA 3.0; Wikidata: CC0"),
                ("build_state", "enriching"),
                ("feature_schema_version", "13")
            ]
            if archive_meta:
                metadata_rows.extend([
                    ("archive_name", str(archive_meta.get("name") or "")),
                    ("archive_created_at", str(archive_meta.get("created_at") or "")),
                    ("archive_digest", str(archive_meta.get("digest") or ""))
                ])
            if instance_seed:
                metadata_rows.append(("instance_random_seed", instance_seed))
            db.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", metadata_rows)
            total_rows = len(rows)
            print(f"[catalog] Writing {total_rows:,} works to database...", flush=True)
            for ordinal, row in enumerate(sorted(rows, key=lambda x: (x.work["start_month"], x.work["bgm_id"])), 1):
                columns = list(row.work)
                cursor = db.execute(
                    f"INSERT INTO anime_work({','.join(columns)},fetched_at) VALUES({','.join('?' for _ in columns)},?)",
                    [row.work[x] for x in columns] + [now]
                )
                anime_id = cursor.lastrowid
                db.executemany("INSERT OR IGNORE INTO anime_title VALUES(?,?,?,?,?)", [
                    (anime_id, x["language"], x["title"], x["title_type"], x["source"]) for x in row.titles
                ])
                db.executemany("INSERT OR IGNORE INTO anime_tag VALUES(?,?,?,?)", [(anime_id, x["name"], x["count"], x["rank"]) for x in row.tags])
                theme_rows = derive_theme_evidence(row.tags)
                db.executemany("INSERT OR IGNORE INTO anime_theme VALUES(?,?)", [(anime_id, x["theme_code"]) for x in theme_rows if x["accepted"]])
                db.executemany("INSERT OR REPLACE INTO anime_theme_evidence VALUES(?,?,?,?,?,?)", [
                    (anime_id, x["theme_code"], x["confidence"], x["accepted"], json.dumps(x["evidence"], ensure_ascii=False), THEME_RULE_VERSION)
                    for x in theme_rows
                ])
                db.executemany("INSERT OR IGNORE INTO anime_studio VALUES(?,?)", [(anime_id, x) for x in (row.work.get("studio") or "").split(" / ") if x])
                db.executemany("INSERT OR IGNORE INTO anime_country VALUES(?,?,?)", [(anime_id, code, evidence) for code, evidence in infer_country_codes([x["name"] for x in row.tags], row.work["title_ja"], (row.work.get("studio") or "").split(" / "))])
                db.executemany("INSERT INTO anime_staff VALUES(?,?,?,?,?,?)", [
                    (anime_id, x["person_id"], x["name"], x["role"], x["role_type"], x["source"]) for x in row.staff if x["name"]
                ])
                db.executemany("INSERT INTO anime_cast VALUES(?,?,?,?,?,?,?,?)", [
                    (anime_id, x["character_id"], x["character_name"], x["person_id"], x["person_name"], x["character_role"], x["language"], x["source"])
                    for x in row.cast if x["person_name"]
                ])
                db.executemany("INSERT INTO anime_relation VALUES(?,?,?,?,?,?,?,?,?,?)", [
                    (anime_id, x["related_bgm_id"], x["related_title"], x["relation_type"], x["relation_code"], x["strict_group"], x["source"], x["related_subject_type"], x["related_subject_kind"], x["related_subject_meta_json"]) for x in row.relations
                ])
                if ordinal % 5000 == 0 or ordinal == total_rows:
                    metrics.progress(f"[catalog] rows {ordinal:,}/{total_rows:,}")
            metrics.end_progress()
            print("[catalog] Building indexes and relation graph...", flush=True)
            rebuild_studio_clusters(db)
            relation_graph.rebuild(db, force=True)
            print("[catalog] Finalizing physical library layout...", flush=True)
            rebuild_physical_layout(db)
            db.execute("UPDATE metadata SET value='ready' WHERE key='build_state'")
            print("[catalog] Validating and optimizing database...", flush=True)
            db.execute("PRAGMA optimize")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("catalog integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("catalog foreign key check failed")
        db.close()
        db = None
        if temp_path != publish_path:
            with temp_path.open("rb") as source, publish_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if publish_path.stat().st_size != temp_path.stat().st_size:
                raise OSError("catalog publish copy is incomplete")
        deadline = time.monotonic() + 30
        while True:
            try:
                os.replace(publish_path, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
        temp_path.unlink(missing_ok=True)
        publish_path.unlink(missing_ok=True)


def load_manifest(path: Path | None, ids: str | None) -> list[dict[str, Any]]:
    if ids:
        return [{"bgm_id": int(x.strip())} for x in ids.split(",") if x.strip()]
    source = path or RESOURCES / "sample-subjects.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("manifest must be a non-empty JSON array")
    return payload


def all_anime_manifest(archive_path: Path) -> list[dict[str, Any]]:
    """Select every archived animation subject without external lookups."""
    print("[archive] Selecting animation subjects from Bangumi Archive...", flush=True)
    with zipfile.ZipFile(archive_path) as archive:
        rows = [
            {"bgm_id": int(row["id"])}
            for row in iter_jsonlines(archive, "subject.jsonlines")
            if int(row.get("type", -1)) == 2
        ]
    if not rows:
        raise RuntimeError("Bangumi Archive contains no animation subjects")
    print(f"[archive] Selected {len(rows):,} animation subjects", flush=True)
    return rows


def build(args: argparse.Namespace) -> Path:
    progress_callback = getattr(args, "progress_callback", None)
    with metrics.stage("archive.resolve"):
        archive_path, archive_meta = ensure_archive(args.archive_dir, args.archive, getattr(args, "network_config", None),
                                                    progress_callback)
    with metrics.stage("archive.select"):
        manifest = None if args.all_anime else load_manifest(args.manifest, args.ids)
    client = JsonClient(args.cache, args.cache_days, args.refresh, args.request_interval)
    wd = wikidata_titles(client, manifest or [])
    if progress_callback:
        progress_callback({"phase": "catalog_parse"})
    with metrics.stage("archive.parse"):
        rows = build_items_from_archive(archive_path, manifest, wd)
    instance_seed = instance_random_seed(args.db) or secrets.token_urlsafe(18)
    print("[catalog] Archive parsing complete.", flush=True)
    archive_parsed_callback = getattr(args, "archive_parsed_callback", None)
    if archive_parsed_callback is not None:
        archive_parsed_callback(instance_seed, archive_meta, len(rows))
    print("[catalog] Building final database; catalog is not ready yet.", flush=True)
    if progress_callback:
        progress_callback({"phase": "catalog_write", "records": len(rows)})
    with metrics.stage("catalog.write"):
        write_database(args.db, rows, archive_meta, instance_seed)
    if progress_callback:
        progress_callback({"phase": "catalog_ready", "records": len(rows)})
    print(f"[catalog] Database ready: {args.db} ({args.db.stat().st_size:,} bytes)", flush=True)
    return args.db


def dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def migrate_catalog_features(db: sqlite3.Connection) -> None:
    """Add product UI indexes without rebuilding the immutable archive rows."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(anime_work)")}
    with db:
        if "media_code" not in columns:
            db.execute("ALTER TABLE anime_work ADD COLUMN media_code TEXT")
        if "source_code" not in columns:
            db.execute("ALTER TABLE anime_work ADD COLUMN source_code TEXT")
        if "original_language" not in columns:
            db.execute("ALTER TABLE anime_work ADD COLUMN original_language TEXT NOT NULL DEFAULT 'ja'")
        if "physical_role" not in columns:
            db.execute("ALTER TABLE anime_work ADD COLUMN physical_role TEXT NOT NULL DEFAULT 'work'")
        if "physical_owner_anime_id" not in columns:
            db.execute("ALTER TABLE anime_work ADD COLUMN physical_owner_anime_id INTEGER")
        tag_columns = {row[1] for row in db.execute("PRAGMA table_info(anime_tag)")}
        if "vote_count" not in tag_columns:
            db.execute("ALTER TABLE anime_tag ADD COLUMN vote_count INTEGER NOT NULL DEFAULT 0")
        if "tag_rank" not in tag_columns:
            db.execute("ALTER TABLE anime_tag ADD COLUMN tag_rank INTEGER NOT NULL DEFAULT 0")
        relation_columns = {row[1] for row in db.execute("PRAGMA table_info(anime_relation)")}
        if "related_subject_type" not in relation_columns:
            db.execute("ALTER TABLE anime_relation ADD COLUMN related_subject_type INTEGER")
        if "related_subject_kind" not in relation_columns:
            db.execute("ALTER TABLE anime_relation ADD COLUMN related_subject_kind TEXT")
        if "related_subject_meta_json" not in relation_columns:
            db.execute("ALTER TABLE anime_relation ADD COLUMN related_subject_meta_json TEXT NOT NULL DEFAULT '{}'")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS anime_theme(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,theme_code TEXT NOT NULL,UNIQUE(anime_id,theme_code));
        CREATE TABLE IF NOT EXISTS anime_theme_evidence(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,theme_code TEXT NOT NULL,confidence REAL NOT NULL,accepted INTEGER NOT NULL,evidence_json TEXT NOT NULL,rule_version TEXT NOT NULL,UNIQUE(anime_id,theme_code));
        CREATE TABLE IF NOT EXISTS anime_studio(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,studio TEXT NOT NULL,UNIQUE(anime_id,studio));
        CREATE TABLE IF NOT EXISTS anime_studio_cluster(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,cluster_key TEXT NOT NULL,cluster_name TEXT NOT NULL,studio_name TEXT NOT NULL,UNIQUE(anime_id,cluster_key,studio_name));
        CREATE TABLE IF NOT EXISTS anime_country(anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,country_code TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(anime_id,country_code));
        CREATE TABLE IF NOT EXISTS anime_image(anime_id INTEGER PRIMARY KEY REFERENCES anime_work(id) ON DELETE CASCADE,mime_type TEXT,image_blob BLOB,source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);
        CREATE INDEX IF NOT EXISTS idx_theme_value ON anime_theme(theme_code);
        CREATE INDEX IF NOT EXISTS idx_theme_evidence_anime ON anime_theme_evidence(anime_id,accepted);
        CREATE INDEX IF NOT EXISTS idx_studio_value ON anime_studio(studio);
        CREATE INDEX IF NOT EXISTS idx_tag_anime ON anime_tag(anime_id);
        CREATE INDEX IF NOT EXISTS idx_theme_anime ON anime_theme(anime_id);
        CREATE INDEX IF NOT EXISTS idx_studio_anime ON anime_studio(anime_id);
        CREATE INDEX IF NOT EXISTS idx_studio_cluster_name ON anime_studio_cluster(cluster_name,anime_id);
        CREATE INDEX IF NOT EXISTS idx_country_code ON anime_country(country_code,anime_id);
        CREATE INDEX IF NOT EXISTS idx_staff_anime ON anime_staff(anime_id);
        CREATE INDEX IF NOT EXISTS idx_cast_anime ON anime_cast(anime_id);
        CREATE INDEX IF NOT EXISTS idx_staff_role_name_anime ON anime_staff(role_type,name,anime_id);
        CREATE INDEX IF NOT EXISTS idx_cast_person_anime ON anime_cast(person_name,anime_id);
        """)
        db.execute("""UPDATE anime_work SET media_code=CASE media_type WHEN 'TV' THEN 'tv' WHEN 'OVA' THEN 'ova'
            WHEN '剧场版' THEN 'movie' WHEN '短片' THEN 'short' WHEN 'WEB' THEN 'web' WHEN '动态漫画' THEN 'motion_comic'
            ELSE 'other' END WHERE media_code IS NULL""")
        db.execute("""UPDATE anime_work SET source_code=CASE source_type WHEN '轻小说改' THEN 'light_novel'
            WHEN '漫画改' THEN 'manga' WHEN '游戏改' THEN 'game' WHEN '小说改' THEN 'novel'
            WHEN '原创' THEN 'original' ELSE 'unknown' END WHERE source_code IS NULL""")
        if db.execute("SELECT COUNT(*) FROM anime_studio").fetchone()[0] == 0:
            rows = [(row[0], name.strip()) for row in db.execute("SELECT id,studio FROM anime_work WHERE studio IS NOT NULL")
                    for name in str(row[1]).split(" / ") if name.strip()]
            db.executemany("INSERT OR IGNORE INTO anime_studio VALUES(?,?)", rows)
        rebuild_theme_clusters(db)
        rebuild_studio_clusters(db)
        if db.execute("SELECT COUNT(*) FROM anime_country").fetchone()[0] == 0:
            tags_by_work: dict[int, list[str]] = {}
            studios_by_work: dict[int, list[str]] = {}
            for anime_id, tag in db.execute("SELECT anime_id,tag FROM anime_tag"):
                tags_by_work.setdefault(int(anime_id), []).append(str(tag))
            for anime_id, studio in db.execute("SELECT anime_id,studio FROM anime_studio"):
                studios_by_work.setdefault(int(anime_id), []).append(str(studio))
            countries = [(int(anime_id), code, evidence) for anime_id, title in db.execute("SELECT id,title_ja FROM anime_work")
                         for code, evidence in infer_country_codes(tags_by_work.get(int(anime_id), []), str(title), studios_by_work.get(int(anime_id), []))]
            db.executemany("INSERT OR IGNORE INTO anime_country VALUES(?,?,?)", countries)
        relation_graph.rebuild(db)
        rebuild_physical_layout(db)
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('feature_schema_version','13')")


def rebuild_physical_layout(db: sqlite3.Connection) -> None:
    """Materialize logical-subject to physical-library ownership."""
    db.execute("UPDATE anime_work SET physical_role='work',physical_owner_anime_id=NULL")
    works = {
        int(row[0]): {
            "id": int(row[0]), "bgm_id": int(row[1]), "title_ja": row[2],
            "start_month": row[3], "directory_date": row[4],
        }
        for row in db.execute("SELECT id,bgm_id,title_ja,start_month,directory_date FROM anime_work")
    }
    bgm_to_id = {int(row["bgm_id"]): int(row["id"]) for row in works.values()}
    relations: dict[int, list[dict[str, Any]]] = {}
    for row in db.execute("SELECT anime_id,related_bgm_id,relation_code FROM anime_relation"):
        relations.setdefault(int(row[0]), []).append({
            "anime_id": int(row[0]), "related_bgm_id": row[1], "relation_code": row[2]
        })
    components: dict[int, list[dict[str, Any]]] = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_series_component'").fetchone():
        for anime_id, component_id in db.execute("SELECT anime_id,component_id FROM anime_series_component"):
            components.setdefault(int(component_id), []).append(works[int(anime_id)])
    component_by_anime = {int(row["id"]): rows for rows in components.values() for row in rows}
    updates: list[tuple[str, int | None, int]] = []
    for anime_id, work in works.items():
        if library_layout.physical_role(work) != "supplement":
            continue
        owner = library_layout.find_supplement_owner(
            work, component_by_anime.get(anime_id, []), relations.get(anime_id, [])
        )
        owner_id = int(owner["id"]) if owner is not None else None
        updates.append(("supplement" if owner_id else "supplement_review", owner_id, anime_id))
    db.executemany("UPDATE anime_work SET physical_role=?,physical_owner_anime_id=? WHERE id=?", updates)
    # A later cour is a logical Archive subject but normally shares one
    # physical season directory and release manifest with its first cour.
    # Merge only when an explicit later-cour suffix, a strict relation and the
    # same relation component all agree; title similarity alone is not proof.
    component_ids = {
        int(anime_id): int(component_id)
        for anime_id, component_id in db.execute("SELECT anime_id,component_id FROM anime_series_component")
    } if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_series_component'").fetchone() else {}
    adjacency: set[tuple[int, int]] = set()
    for anime_id, related_bgm_id, relation_code in db.execute(
            "SELECT anime_id,related_bgm_id,relation_code FROM anime_relation WHERE relation_code IN ('prequel','sequel','parent','main_story')"):
        related_id = bgm_to_id.get(int(related_bgm_id or 0))
        if related_id:
            adjacency.add((int(anime_id), related_id))
            adjacency.add((related_id, int(anime_id)))
    by_base: dict[str, list[tuple[int, int]]] = {}
    for anime_id, work in works.items():
        identity = library_layout.split_cour_identity(str(work["title_ja"]))
        if identity:
            by_base.setdefault(identity[0], []).append((anime_id, identity[1]))
        else:
            by_base.setdefault(library_layout.compact(str(work["title_ja"])), []).append((anime_id, 1))
    split_updates: list[tuple[str, int, int]] = []
    for anime_id, work in works.items():
        identity = library_layout.split_cour_identity(str(work["title_ja"]))
        if not identity:
            continue
        base, cour = identity
        candidates = []
        for candidate_id, candidate_cour in by_base.get(base, []):
            if candidate_id == anime_id or candidate_cour >= cour:
                continue
            if component_ids.get(candidate_id) != component_ids.get(anime_id):
                continue
            if (anime_id, candidate_id) not in adjacency:
                continue
            candidates.append((candidate_cour, str(works[candidate_id]["start_month"]), candidate_id))
        if candidates:
            # Prefer the lowest cour and then the earliest dated owner.
            owner_id = min(candidates, key=lambda value: (value[0], value[1], value[2]))[2]
            split_updates.append(("split_cour", owner_id, anime_id))
    direct_owner = {child_id: owner_id for _, owner_id, child_id in split_updates}
    flattened: list[tuple[str, int, int]] = []
    for role, owner_id, child_id in split_updates:
        seen = {child_id}
        while owner_id in direct_owner and owner_id not in seen:
            seen.add(owner_id)
            owner_id = direct_owner[owner_id]
        flattened.append((role, owner_id, child_id))
    db.executemany("UPDATE anime_work SET physical_role=?,physical_owner_anime_id=? WHERE id=?", flattened)


def ensure_catalog_features(db_path: Path) -> None:
    def ready(db: sqlite3.Connection) -> bool:
        columns = {row[1] for row in db.execute("PRAGMA table_info(anime_work)")}
        version = db.execute("SELECT value FROM metadata WHERE key='feature_schema_version'").fetchone()
        relation_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_relation_edge'").fetchone()
        return "media_code" in columns and "physical_role" in columns and bool(version and version[0] == "13") and bool(relation_table)

    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        if ready(db):
            return
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        if not ready(db):
            migrate_catalog_features(db)


def query_catalog(db_path: Path, params: dict[str, list[str]], config: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_catalog_features(db_path)
    config = config or ConfigStore(DEFAULT_CONFIG, EXAMPLE_CONFIG).read()
    config = {**config, "ui": {**config.get("ui", {}), "language": (params.get("language") or [config.get("ui", {}).get("language", "en")])[0]}}
    base_where = ["COALESCE(w.physical_role,'work') NOT IN ('supplement','supplement_review')"]
    where: list[str] = []
    values: list[Any] = []

    def value(name: str) -> str:
        return (params.get(name) or [""])[0].strip()

    def selected_values(name: str) -> list[str]:
        return [str(item).strip() for item in params.get(name, []) if str(item).strip()]

    q = value("q")
    keyword_clause = "EXISTS(SELECT 1 FROM anime_title t WHERE t.anime_id=w.id AND t.title LIKE ?)"
    if q:
        where.append(keyword_clause)
        values.append(f"%{q}%")
    media_types = selected_values("media_type")
    if media_types:
        ordinary_media = [code for code in media_types if code != "other"]
        media_clauses: list[str] = []
        if ordinary_media:
            media_clauses.append("w.media_code IN (" + ",".join("?" for _ in ordinary_media) + ")")
            values.extend(ordinary_media)
        if "other" in media_types:
            media_clauses.append("w.media_code NOT IN ('tv','movie','web','ova')")
        where.append("(" + " OR ".join(media_clauses) + ")")
    source_type = value("source_type")
    if source_type:
        if source_type == "other":
            where.append("w.source_code NOT IN ('light_novel','manga','game','novel','original')")
        else:
            where.append("w.source_code=?")
            values.append(source_type)
    series = value("series")
    if series == "yes":
        where.append("EXISTS(SELECT 1 FROM anime_series_component sc WHERE sc.anime_id=w.id AND (SELECT COUNT(*) FROM anime_series_component sx JOIN anime_work wx ON wx.id=sx.anime_id WHERE sx.component_id=sc.component_id AND COALESCE(wx.physical_role,'work')='work')>1)")
    elif series == "no":
        where.append("NOT EXISTS(SELECT 1 FROM anime_series_component sc WHERE sc.anime_id=w.id AND (SELECT COUNT(*) FROM anime_series_component sx JOIN anime_work wx ON wx.id=sx.anime_id WHERE sx.component_id=sc.component_id AND COALESCE(wx.physical_role,'work')='work')>1)")
    studio = value("studio")
    if studio:
        if studio == "__other__":
            where.append("EXISTS(SELECT 1 FROM anime_studio_cluster ast WHERE ast.anime_id=w.id AND ast.cluster_key IN (SELECT cluster_key FROM anime_studio_cluster GROUP BY cluster_key HAVING COUNT(DISTINCT anime_id)<?))")
            values.append(STUDIO_FILTER_MIN_WORKS)
        else:
            where.append("EXISTS(SELECT 1 FROM anime_studio_cluster ast WHERE ast.anime_id=w.id AND ast.cluster_name=?)")
            values.append(studio)
    country = value("country")
    if country:
        where.append("EXISTS(SELECT 1 FROM anime_country ac WHERE ac.anime_id=w.id AND ac.country_code=?)")
        values.append("OTHER" if country == "other" else country)
    start_from, start_to = value("start_from"), value("start_to")
    date_clauses: list[str] = []
    if start_from:
        date_clauses.append("w.start_month>=?")
        values.append(start_from)
    if start_to:
        date_clauses.append("w.start_month<=?")
        values.append(start_to)
    if date_clauses:
        where.append("(" + " AND ".join(date_clauses) + ")")
    era = value("era") or value("decade")
    current_year = dt.datetime.now().year
    # The UI synchronizes a concrete era into the date range. Avoid counting
    # that same preference twice in expanded keyword-search relevance.
    if date_clauses:
        pass
    elif era == "before1980":
        where.append("w.start_month GLOB '[0-9][0-9][0-9][0-9]-*' AND CAST(substr(w.start_month,1,4) AS INTEGER)<1980")
    elif era in {"1980s", "1990s"}:
        where.append("CAST(substr(w.start_month,1,4) AS INTEGER) BETWEEN ? AND ?")
        base = int(era[:4]); values.extend([base, base + 9])
    elif era == "future_or_unknown":
        where.append("(w.start_month NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' OR CAST(substr(w.start_month,1,4) AS INTEGER)>=?)")
        values.append(current_year + 1)
    elif era.isdigit():
        where.append("substr(w.start_month,1,4)=?"); values.append(era)
    elif re.fullmatch(r"\d{4}s", era):
        base = int(era[:4]); where.append("CAST(substr(w.start_month,1,4) AS INTEGER) BETWEEN ? AND ?"); values.extend([base, base + 9])
    exists_filters = {
        "director": ("anime_staff", "s", "s.anime_id=w.id AND s.role_type='director' AND s.name LIKE ?"),
        "voice_actor": ("anime_cast", "c", "c.anime_id=w.id AND c.person_name LIKE ?"),
        "tag": ("anime_theme", "g", "g.anime_id=w.id AND g.theme_code=?")
    }
    for name, (table, alias, condition) in exists_filters.items():
        selected = value(name)
        if selected:
            where.append(f"EXISTS(SELECT 1 FROM {table} {alias} WHERE {condition})")
            values.append(f"%{selected}%" if name != "tag" else selected)

    availability = set(selected_values("availability"))
    policy = config.get("torrentPolicy", {})
    disabled_classes = explicitly_disabled(policy.get("contentClasses", {}))
    disabled_resolutions = explicitly_disabled(policy.get("resolutions", {}))
    enabled_classes = [str(name).casefold() for name, enabled in policy.get("contentClasses", {}).items() if enabled]
    enabled_resolutions = [str(name).casefold() for name, enabled in policy.get("resolutions", {}).items() if enabled]
    allow_unlisted = policy.get("allowUnlisted", {})
    eligibility_sql = """EXISTS(SELECT 1 FROM runtime_torrent_work rtw
        JOIN runtime_torrent rt ON rt.info_hash=rtw.info_hash
        JOIN runtime_work rw ON rw.private_work_id=rtw.private_work_id
        WHERE rtw.anime_id=w.id AND rt.scan_state!='reject' AND rt.metadata_state='available'
          AND rw.scope_state NOT IN ('excluded','excluded_long_running')"""
    eligibility_values: list[Any] = []
    resolution_expr = "CASE WHEN lower(COALESCE(CAST(rt.video_height AS TEXT)||rt.video_scan,'unknown')) IN ('480p','540p','576p') THEN '480p-576p' ELSE lower(COALESCE(CAST(rt.video_height AS TEXT)||rt.video_scan,'unknown')) END"
    dimensions = [
        ("sourceClass", "lower(COALESCE(rt.source_class,'unknown'))", enabled_classes, disabled_classes),
        ("resolution", resolution_expr, enabled_resolutions, disabled_resolutions),
    ]
    for dimension, expression, enabled_values, disabled_values in dimensions:
        if allow_unlisted.get(dimension, True):
            if disabled_values:
                eligibility_sql += f" AND {expression} NOT IN (" + ",".join("?" for _ in disabled_values) + ")"
                eligibility_values.extend(disabled_values)
        else:
            eligibility_sql += f" AND {expression} IN (" + ",".join("?" for _ in enabled_values) + ")" if enabled_values else " AND 0"
            eligibility_values.extend(enabled_values)
    eligibility_sql += ")"
    external_media_sql = "EXISTS(SELECT 1 FROM external_media_file em WHERE em.anime_id=w.id AND em.match_state='verified')"
    ani_rss_sql = "(EXISTS(SELECT 1 FROM ani_rss_resource ar WHERE ar.anime_id=w.id AND ar.eligible=1 AND julianday(ar.expires_at)>=julianday('now')) OR EXISTS(SELECT 1 FROM ani_rss_subscription ans WHERE ans.anime_id=w.id AND ans.deleted_at IS NULL))"
    if availability == {"__none__"}:
        where.append("0")
    elif availability == {"available"}:
        # Availability means either an eligible download candidate or verified
        # read-only media.  It must not mislabel ani-rss content as absent.
        where.append(f"({eligibility_sql} OR {external_media_sql} OR {ani_rss_sql})")
        values.extend(eligibility_values)
    elif availability == {"unavailable"}:
        where.append(f"(NOT {eligibility_sql} AND NOT {external_media_sql} AND NOT {ani_rss_sql})")
        values.extend(eligibility_values)
    library_states = set(selected_values("library_state"))
    if library_states == {"__none__"}:
        where.append("0")
    elif library_states:
        clauses: list[str] = []
        ordinary = sorted(library_states - {"absent", "external"})
        if ordinary:
            clauses.append("EXISTS(SELECT 1 FROM runtime_work rw WHERE rw.anime_id=w.id AND rw.library_state IN (" + ",".join("?" for _ in ordinary) + "))")
            values.extend(ordinary)
        if "external" in library_states:
            clauses.append("EXISTS(SELECT 1 FROM external_media_file em WHERE em.anime_id=w.id AND em.match_state='verified')")
        if "absent" in library_states:
            clauses.append("NOT EXISTS(SELECT 1 FROM runtime_work rw WHERE rw.anime_id=w.id) AND NOT EXISTS(SELECT 1 FROM external_media_file em WHERE em.anime_id=w.id AND em.match_state='verified')")
        where.append("(" + " OR ".join(clauses) + ")")

    raw_limit = value("limit") or "12"
    limit = 1000000 if raw_limit == "all" else min(max(int(raw_limit), 1), 500)
    offset = max(int(value("offset") or 0), 0)
    locale = value("language") or "zh-Hans"
    title_expr = "COALESCE(w.title_zh_hans,w.title_ja)" if locale == "zh-Hans" else ("COALESCE(w.title_en,w.title_ja)" if locale == "en" else "w.title_ja")
    sort = value("sort") or "random"
    seed = value("seed") or instance_random_seed(db_path) or "anm"
    direction = "DESC" if value("direction") == "desc" else "ASC"
    primary = {
        "date": "w.start_month", "title": title_expr,
        "studio": "COALESCE((SELECT min(cluster_name) FROM anime_studio_cluster ast WHERE ast.anime_id=w.id),'')",
        "type": "w.media_code",
    }.get(sort, "w.start_month")
    pending_expr = "CASE WHEN EXISTS(SELECT 1 FROM anime_studio_cluster asp WHERE asp.anime_id=w.id) THEN 0 ELSE 1 END"
    order = (f"{pending_expr} ASC,seeded_rank(w.id,?) ASC,w.start_month,w.media_code,{title_expr}" if sort == "random"
             else f"{primary} {direction},w.start_month ASC,w.media_code ASC,{title_expr} ASC")
    order_values = [seed] if sort == "random" else []
    predicate = " WHERE " + " AND ".join(base_where + where)
    search_expanded = bool(q and len(where) > 1)
    filter_dimension_count = max(0, len(where) - 1) if q else 0
    if search_expanded:
        # A keyword search always ranges over the whole title/alias catalog.
        # Other UI filters become relevance dimensions instead of hard WHERE
        # predicates.  This keeps exact filter matches first while still
        # allowing the user to find an older or differently classified work.
        filter_clauses = where[1:]
        filter_values = values[1:]
        filter_score_expr = " + ".join(f"CASE WHEN ({clause}) THEN 1 ELSE 0 END" for clause in filter_clauses)
        keyword_rank_expr = """CASE
            WHEN EXISTS(SELECT 1 FROM anime_title kt WHERE kt.anime_id=w.id AND lower(kt.title)=lower(?)) THEN 3
            WHEN EXISTS(SELECT 1 FROM anime_title kt WHERE kt.anime_id=w.id AND lower(kt.title) LIKE lower(?)) THEN 2
            ELSE 1 END"""
        predicate = " WHERE " + " AND ".join(base_where + [keyword_clause])
        values = [f"%{q}%"]
        ranked_prefix = (f"WITH ranked AS (SELECT w.*, ({filter_score_expr}) AS filter_match_count, "
                         f"({keyword_rank_expr}) AS keyword_match_rank FROM anime_work w{predicate}) SELECT w.* FROM ranked w")
        ranking_prefix = (f"CASE WHEN w.filter_match_count={filter_dimension_count} THEN 0 "
                          f"WHEN w.filter_match_count=0 THEN 2 ELSE 1 END ASC,"
                          "w.filter_match_count DESC,"
                          "CASE WHEN w.filter_match_count=0 THEN w.keyword_match_rank ELSE 0 END DESC,")
        row_values = filter_values + [q, f"{q}%"] + values
    else:
        ranked_prefix = f"SELECT w.* FROM anime_work w{predicate}"
        ranking_prefix = ""
        row_values = values
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.create_function("seeded_rank", 2, lambda anime_id, s: hashlib.sha256(f"{s}:{anime_id}".encode()).hexdigest(), deterministic=True)
        has_runtime = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_work'").fetchone())
        total = db.execute(f"SELECT count(*) FROM anime_work w{predicate}", values).fetchone()[0]
        # Page first, then enrich only the visible rows.  Doing correlated
        # aggregates before ORDER BY made a 30k-work catalog needlessly scan
        # child tables for every work.
        rows = dict_rows(db.execute(
            f"{ranked_prefix} ORDER BY {ranking_prefix}{order} LIMIT ? OFFSET ?",
            row_values + order_values + [limit, offset]))
        ids = [int(row["id"]) for row in rows]
        physical_ids = {
            anime_id: runtime_catalog.physical_anime_id(db, anime_id) for anime_id in ids
        }
        torrent_counts: dict[int, int] = {}
        usable_counts: dict[int, int] = {}
        external_counts: dict[int, int] = {}
        ani_rss_counts: dict[int, int] = {}
        ani_rss_managed: set[int] = set()
        ani_state = ani_rss.state(db_path, config)
        if has_runtime and ids:
            effective_ids = sorted(set(physical_ids.values()))
            marks = ",".join("?" for _ in effective_ids)
            torrent_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(DISTINCT info_hash) FROM runtime_torrent_work WHERE anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            external_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(*) FROM external_media_file WHERE match_state='verified' AND anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            ani_rss_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(*) FROM ani_rss_resource WHERE eligible=1 AND julianday(expires_at)>=julianday('now') AND anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            ani_rss_managed = {int(row[0]) for row in db.execute(
                f"SELECT DISTINCT anime_id FROM ani_rss_subscription WHERE deleted_at IS NULL AND anime_id IN ({marks})", effective_ids)}
            for anime_id in ids:
                usable_counts[anime_id] = sum(1 for item in runtime_catalog.torrents_for_anime(db, anime_id, config) if item["eligible"])
        for row in rows:
            anime_id = row["id"]
            row["global_search_only"] = bool(
                search_expanded
                and int(row.get("filter_match_count") or 0) < filter_dimension_count
            )
            row["directors"] = db.execute("SELECT group_concat(name,' / ') FROM anime_staff WHERE anime_id=? AND role_type='director'", (anime_id,)).fetchone()[0]
            row["voice_actors"] = db.execute("SELECT group_concat(person_name,' / ') FROM (SELECT DISTINCT person_name FROM anime_cast WHERE anime_id=? AND (language=(SELECT original_language FROM anime_work WHERE id=?) OR language IS NULL OR language IN ('','und')) LIMIT 6)", (anime_id, anime_id)).fetchone()[0]
            row["tags"] = db.execute("SELECT group_concat(theme_code,' / ') FROM anime_theme WHERE anime_id=?", (anime_id,)).fetchone()[0]
            row["studios"] = [x[0] for x in db.execute("SELECT DISTINCT cluster_name FROM anime_studio_cluster WHERE anime_id=? ORDER BY cluster_name", (anime_id,))]
            row["countries"] = [x[0] for x in db.execute("SELECT country_code FROM anime_country WHERE anime_id=? ORDER BY country_code", (anime_id,))]
            owner_id = physical_ids.get(anime_id, anime_id)
            row["torrent_count"] = torrent_counts.get(owner_id, 0)
            row["usable_torrent_count"] = usable_counts.get(anime_id, 0)
            row["external_media_count"] = external_counts.get(owner_id, 0)
            row["has_external_media"] = external_counts.get(owner_id, 0) > 0
            row["ani_rss_resource_count"] = ani_rss_counts.get(owner_id, 0)
            row["ani_rss_managed"] = owner_id in ani_rss_managed
            row["ani_rss_auto_available"] = (ani_state.get("connection_state") == "ready"
                                                   and ani_state.get("effective_mode") in {"prefer", "fallback"})
            library = runtime_catalog.library_status(db, anime_id) if has_runtime else None
            row["library_state"] = library["state"] if library else None
            row["library_managed"] = bool(library and library["managed"])
            row["completeness"] = runtime_catalog.completeness_for_anime(db, anime_id)
            component = db.execute(
                "SELECT member_count FROM anime_series_component WHERE anime_id=?", (anime_id,)
            ).fetchone()
            row["series_member_count"] = int(component[0]) if component else 1
    return {"total": total, "limit": limit, "offset": offset, "items": rows,
            "searchExpanded": search_expanded, "filterDimensionCount": filter_dimension_count}


def catalog_options(db_path: Path) -> dict[str, Any]:
    ensure_catalog_features(db_path)
    stat = db_path.stat()
    cache_key = (
        str(db_path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        dt.datetime.now().year,
    )
    with OPTIONS_CACHE_LOCK:
        cached = OPTIONS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        def distinct(sql: str) -> list[str]:
            return [str(x[0]) for x in db.execute(sql) if x[0]]
        result = {
            "media_types": ["tv", "movie", "web", "ova", "other"],
            "source_types": ["light_novel", "manga", "game", "novel", "original", "other"],
            "studios": distinct(f"SELECT cluster_name FROM anime_studio_cluster GROUP BY cluster_key,cluster_name HAVING COUNT(DISTINCT anime_id)>={STUDIO_FILTER_MIN_WORKS} ORDER BY cluster_name") + ["__other__"],
            "countries": [code for code in COMMON_COUNTRIES if db.execute("SELECT 1 FROM anime_country WHERE country_code=? LIMIT 1", (code,)).fetchone()] + ["other"],
            "directors": distinct("SELECT name FROM anime_staff WHERE role_type='director' GROUP BY name HAVING COUNT(DISTINCT anime_id)>=5 ORDER BY COUNT(DISTINCT anime_id) DESC,name LIMIT 250"),
            "voice_actors": distinct("SELECT person_name FROM anime_cast GROUP BY person_name HAVING COUNT(DISTINCT anime_id)>=10 ORDER BY COUNT(DISTINCT anime_id) DESC,person_name LIMIT 300"),
            "tags": list(THEME_DISPLAY_ORDER),
            "eras": ["future_or_unknown", *[str(y) for y in range(dt.datetime.now().year, 1999, -1)], "1990s", "1980s", "before1980"]
        }
        try:
            result["observed_resource_groups"] = [
                {"name": str(name), "count": int(count)}
                for name, count in db.execute("""SELECT effective_group,COUNT(*) FROM runtime_torrent
                    WHERE effective_group IS NOT NULL AND trim(effective_group)<>'' GROUP BY effective_group
                    ORDER BY COUNT(*) DESC,effective_group""")]
        except sqlite3.OperationalError:
            result["observed_resource_groups"] = []
    with OPTIONS_CACHE_LOCK:
        OPTIONS_CACHE.clear()
        OPTIONS_CACHE[cache_key] = result
    return result


def catalog_relation_graph(db_path: Path, anime_id: int, config: dict[str, Any]) -> dict[str, Any] | None:
    """Return a normalized Archive relation graph enriched with local runtime state."""
    ensure_catalog_features(db_path)
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        payload = relation_graph.graph_rows(db, anime_id)
        if not payload:
            return None
        nodes_by_id = {int(node["id"]): node for node in payload["nodes"]}
        node_ids = sorted(nodes_by_id)
        marks = ",".join("?" for _ in node_ids)
        source_by_id = {
            int(row[0]): str(row[1] or "unknown")
            for row in db.execute(
                f"SELECT id,source_code FROM anime_work WHERE id IN ({marks})", node_ids
            )
        } if node_ids else {}
        annotations: dict[int, list[dict[str, Any]]] = {value: [] for value in node_ids}
        if node_ids:
            for row in db.execute(
                f"""SELECT anime_id,related_bgm_id,related_title,relation_code,
                    related_subject_type,related_subject_kind,related_subject_meta_json
                    FROM anime_relation WHERE anime_id IN ({marks})
                    AND COALESCE(related_subject_type,0)<>2
                    ORDER BY anime_id,related_subject_kind,related_title""",
                node_ids,
            ):
                item = dict(row)
                try:
                    meta = json.loads(item.get("related_subject_meta_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    meta = {}
                kind = str(item.get("related_subject_kind") or meta.get("kind") or "other")
                relation_code = str(item.get("relation_code") or "other")
                source = source_by_id.get(int(item["anime_id"]), "unknown")
                if kind == "music":
                    category = "music"
                elif relation_code == "adaptation":
                    category = "original" if kind == source and source not in {"unknown", "original"} else "adaptation"
                elif relation_code != "other" and kind != "other":
                    category = "related"
                else:
                    continue
                title = str(meta.get("title") or item.get("related_title") or f"Bangumi #{item['related_bgm_id']}")
                annotations[int(item["anime_id"])].append({
                    "category": category,
                    "kind": kind,
                    "role": str(meta.get("role") or kind),
                    "title": title,
                    "bgmId": int(item["related_bgm_id"]),
                    "url": f"https://bgm.tv/subject/{int(item['related_bgm_id'])}",
                    "date": str(meta.get("date") or ""),
                    "authors": list(meta.get("authors") or []),
                    "publishers": list(meta.get("publishers") or []),
                    "artists": list(meta.get("artists") or []),
                })
        for node in payload["nodes"]:
            candidates = runtime_catalog.torrents_for_anime(db, int(node["id"]), config)
            library = runtime_catalog.library_status(db, int(node["id"]))
            eligible = [item for item in candidates if item["eligible"]]
            blocked_states = {
                "queued", "downloading", "occupied_review", "deprecated",
                "upgrade_staged", "upgrade_blocked",
            }
            node.update({
                "torrent_count": len(candidates),
                "usable_torrent_count": len(eligible),
                "library_state": library["state"],
                "library_managed": bool(library["managed"]),
                "selectable": bool(eligible) and library["state"] not in blocked_states,
                "eligible_info_hashes": [item["infoHash"] for item in eligible],
                "preferred_info_hash": eligible[0]["infoHash"] if eligible else None,
                "preferred_collection": bool(eligible and eligible[0]["collection"]),
                "selection_warning": library["state"] in {
                    "existing", "queued", "downloading", "occupied_review", "upgrade_staged"
                },
                "completeness": runtime_catalog.completeness_for_anime(db, int(node["id"])),
                "related_subjects": sorted({
                    (item["category"], item["bgmId"]): item
                    for item in annotations.get(int(node["id"]), [])
                }.values(), key=lambda item: (
                    {"music": 0, "original": 1, "adaptation": 2, "related": 3}.get(item["category"], 9),
                    {"opening": 0, "ending": 1, "theme_collection": 2, "character_song": 3,
                     "soundtrack": 4, "music": 5}.get(item["role"], 9),
                    item["title"].casefold(), item["bgmId"],
                )),
            })
        return payload


def people_options(db_path: Path, role: str, query: str = "", limit: int = 40) -> list[dict[str, Any]]:
    ensure_catalog_features(db_path)
    limit = min(max(int(limit), 1), 100)
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        if role == "director":
            sql, values = """SELECT name,COUNT(DISTINCT anime_id) n FROM anime_staff
                WHERE role_type='director' AND name LIKE ? GROUP BY name ORDER BY n DESC,name LIMIT ?""", (f"%{query.strip()}%", limit)
        elif role == "voice_actor":
            sql, values = """SELECT person_name,COUNT(DISTINCT anime_id) n FROM anime_cast
                WHERE person_name LIKE ? GROUP BY person_name ORDER BY n DESC,person_name LIMIT ?""", (f"%{query.strip()}%", limit)
        else:
            raise ValueError("role must be director or voice_actor")
        return [{"name": str(name), "works": int(count)} for name, count in db.execute(sql, values)]


def original_source_summary(relations: Iterable[dict[str, Any]], source: str | None,
                            work_titles: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Return the original-work title and author credits from Archive-only relation evidence."""
    source = str(source or "")
    if source not in SOURCE_KIND_LABELS:
        return "", []
    candidates: list[tuple[str, list[str]]] = []
    for relation in relations:
        if str(relation.get("relation_code") or "") != "adaptation":
            continue
        kind = str(relation.get("related_subject_kind") or "")
        if kind != source:
            continue
        try:
            meta = json.loads(str(relation.get("related_subject_meta_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        title = str(meta.get("title") or relation.get("related_title") or "").strip()
        authors = unique(str(value).strip() for value in meta.get("authors") or [])
        candidates.append((title, authors))
    if not candidates:
        return "", []
    if len(candidates) == 1:
        return candidates[0]

    def normalized_title(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value)

    expected = {normalized_title(value) for value in work_titles if str(value).strip()}
    exact = [candidate for candidate in candidates if normalized_title(candidate[0]) in expected]
    return exact[0] if len(exact) == 1 else ("", [])


def ranked_display_themes(theme_evidence: Iterable[dict[str, Any]], limit: int = 8) -> list[str]:
    """Rank accepted sidebar themes by the strongest matching Archive tag vote count."""
    ranked: list[tuple[str, int, int]] = []
    for row in theme_evidence:
        if not bool(row.get("accepted")):
            continue
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        tags = evidence.get("tags") if isinstance(evidence, dict) else []
        normalized = [_tag_record(tag, index + 1) for index, tag in enumerate(tags or [])]
        normalized = [tag for tag in normalized if tag["name"]]
        score = max((int(tag["count"]) for tag in normalized), default=0)
        best_rank = min((int(tag["rank"]) for tag in normalized if int(tag["count"]) == score), default=999999)
        ranked.append((str(row.get("theme_code") or ""), score, best_rank))
    ranked = [row for row in ranked if row[0]]
    if not ranked:
        return []
    top_votes = max(score for _code, score, _rank in ranked)
    if top_votes <= 0:
        return []
    minimum_votes = max(3, min(20, max(1, top_votes // 100)))
    ranked = [row for row in ranked if row[1] >= minimum_votes]
    order = {code: index for index, code in enumerate(THEME_DISPLAY_ORDER)}
    ranked.sort(key=lambda row: (-row[1], row[2], order.get(row[0], 999), row[0]))
    return [code for code, _score, _rank in ranked[:max(0, limit)]]


def catalog_detail(db_path: Path, anime_id: int, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    ensure_catalog_features(db_path)
    config = config or ConfigStore(DEFAULT_CONFIG, EXAMPLE_CONFIG).read()
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM anime_work WHERE id=?", (anime_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["titles"] = dict_rows(db.execute("SELECT language,title,title_type,source FROM anime_title WHERE anime_id=? ORDER BY language,title_type,title", (anime_id,)))
        result["staff"] = dict_rows(db.execute("SELECT name,role,role_type,source FROM anime_staff WHERE anime_id=? ORDER BY role_type,role,name", (anime_id,)))
        result["cast"] = dict_rows(db.execute("SELECT character_name,person_name,character_role,CASE WHEN language IS NULL OR language IN ('','und') THEN original_language ELSE language END language,source FROM anime_cast JOIN anime_work ON anime_work.id=anime_cast.anime_id WHERE anime_cast.anime_id=? ORDER BY CASE character_role WHEN '主角' THEN 0 WHEN '配角' THEN 1 ELSE 2 END, character_name LIMIT 30", (anime_id,)))
        result["relations"] = dict_rows(db.execute("""SELECT ar.related_bgm_id,ar.related_title,ar.relation_type,
            ar.relation_code,ar.strict_group,ar.source,ar.related_subject_type,ar.related_subject_kind,
            ar.related_subject_meta_json,
            related.id AS related_anime_id
            FROM anime_relation ar LEFT JOIN anime_work related ON related.bgm_id=ar.related_bgm_id
            WHERE ar.anime_id=? AND ar.relation_code<>'other'
            ORDER BY ar.relation_type,ar.related_title""", (anime_id,)))
        result["original_name"], result["original_authors"] = original_source_summary(
            result["relations"], result.get("source_code"),
            (str(result.get("title_ja") or ""), str(result.get("title_zh_hans") or "")),
        )
        component = db.execute("SELECT member_count FROM anime_series_component WHERE anime_id=?", (anime_id,)).fetchone()
        result["series_member_count"] = int(component[0]) if component else 1
        result["tags"] = [x[0] for x in db.execute("SELECT tag FROM anime_tag WHERE anime_id=? ORDER BY tag", (anime_id,))]
        result["studios"] = [x[0] for x in db.execute("SELECT DISTINCT cluster_name FROM anime_studio_cluster WHERE anime_id=? ORDER BY cluster_name", (anime_id,))]
        result["studio_sources"] = [x[0] for x in db.execute("SELECT studio FROM anime_studio WHERE anime_id=? ORDER BY studio", (anime_id,))]
        result["themes"] = [x[0] for x in db.execute("SELECT theme_code FROM anime_theme WHERE anime_id=? ORDER BY theme_code", (anime_id,))]
        result["theme_evidence"] = [
            {"theme_code": code, "confidence": float(confidence), "accepted": bool(accepted), "evidence": json.loads(evidence)}
            for code, confidence, accepted, evidence in db.execute(
                "SELECT theme_code,confidence,accepted,evidence_json FROM anime_theme_evidence WHERE anime_id=? ORDER BY theme_code", (anime_id,)
            )
        ]
        result["display_tags"] = ranked_display_themes(result["theme_evidence"])
        result["countries"] = [x[0] for x in db.execute("SELECT country_code FROM anime_country WHERE anime_id=? ORDER BY country_code", (anime_id,))]
        has_runtime = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_work'").fetchone())
        result["library"] = runtime_catalog.library_status(db, anime_id) if has_runtime else {"state": "not_in_library_catalog", "managed": False, "inspectionMode": "none", "targets": []}
        if has_runtime:
            serial_classes = {str(value).casefold() for value in config.get("torrentPolicy", {}).get("sourceFamilies", {}).get("serial", [])}
            for target in result["library"].get("targets", []):
                classes = {str(value[0]).casefold() for value in db.execute("""SELECT DISTINCT t.source_class
                    FROM runtime_work rw JOIN runtime_torrent_work tw ON tw.private_work_id=rw.private_work_id
                    JOIN runtime_torrent t ON t.info_hash=tw.info_hash WHERE rw.anime_id=? AND rw.target_unc=?""",
                    (runtime_catalog.physical_anime_id(db, anime_id), target.get("path"))) if value[0]}
                target["subtitleApplicable"] = not classes or bool(classes - serial_classes)
        result["torrents"] = runtime_catalog.torrents_for_anime(db, anime_id, config) if has_runtime else []
        result["ani_rss"] = {
            "state": ani_rss.state(db_path, config),
            "subscriptions": ani_rss.subscriptions_for_anime(db_path, anime_id),
            "resources": ani_rss.resources(db_path, anime_id, config),
        }
        return result


def get_cached_anime_image(db_path: Path, anime_id: int) -> tuple[tuple[bytes, str] | None, str]:
    """Read and validate a persistent cover without performing network I/O."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT ai.mime_type,ai.image_blob,ai.fetched_at,ai.error FROM anime_work aw LEFT JOIN anime_image ai ON ai.anime_id=aw.id WHERE aw.id=?", (anime_id,)).fetchone()
        if not row:
            return None, "not_found"
        if row["image_blob"] is not None:
            try:
                return network_validators.cached_image_bytes(
                    bytes(row["image_blob"]), str(row["mime_type"] or "application/octet-stream")), "available"
            except (ValueError, OSError):
                with db:
                    db.execute("UPDATE anime_image SET mime_type=NULL,image_blob=NULL,source_url=NULL,error='corrupt_cache' WHERE anime_id=?", (anime_id,))
                log_event("ERROR", "cover_cache_invalid", animeId=anime_id)
                return None, "missing"
        if row["error"] and row["fetched_at"]:
            with contextlib.suppress(ValueError):
                age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(row["fetched_at"]))
                if age.total_seconds() < (86400 if str(row["error"]) == "no_cover" else 120):
                    return None, "negative"
        return None, "missing"


def _scaled_cover_url(url: str, size: int = 400) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc or "/pic/" not in parsed.path or "/r/" in parsed.path:
        return ""
    return urllib.parse.urlunparse(parsed._replace(path=f"/r/{int(size)}{parsed.path}"))


def _cover_urls_from_subject(payload: Any) -> list[str]:
    queue: deque[Any] = deque([payload])
    seen: set[int] = set()
    while queue:
        value = queue.popleft()
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        images = value.get("images")
        if isinstance(images, dict):
            values = {key: str(images.get(key) or "").strip() for key in ("large", "common", "medium", "small")}
            values = {key: candidate for key, candidate in values.items() if candidate and "no_icon" not in candidate}
            if values:
                preferred = [values.get("medium", "")] if "/r/400/" in values.get("medium", "") else []
                scaled = [_scaled_cover_url(values.get(key, "")) for key in ("large", "common", "medium", "small")]
                raw = [values.get(key, "") for key in ("medium", "common", "large", "small")]
                return list(dict.fromkeys(candidate for candidate in [*preferred, *scaled, *raw] if candidate))
        for key in ("data", "subject", "result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                queue.append(nested)
    return []


def _cover_url_from_subject(payload: Any) -> str:
    urls = _cover_urls_from_subject(payload)
    return urls[0] if urls else ""


def get_anime_image(db_path: Path, anime_id: int, *, refresh: bool = False,
                    network: dict[str, Any] | None = None,
                    log_timing: bool = True) -> tuple[bytes, str] | None:
    """Fetch one official cover and atomically persist it; intended for ImageFetcher only."""
    cached, cache_state = get_cached_anime_image(db_path, anime_id)
    if cache_state == "not_found":
        return None
    if cached is not None and not refresh:
        return cached
    if cache_state == "negative" and not refresh:
        return placeholder_image()
    network_config = network or {}
    request_timeout = max(1.0, float(network_config.get("probeTimeoutSeconds", 12)))
    attempts = 1
    failure_cooldown = int(network_config.get("failureCooldownSeconds", 900))
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        row = db.execute("SELECT bgm_id FROM anime_work WHERE id=?", (anime_id,)).fetchone()
    if not row:
        return None
    bgm_id = int(row[0])
    cache_bases = list(dict.fromkeys([
        *(network_config.get("bangumiSubjectCacheEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("bangumi_subject_cache")),
    ]))
    bases = list(dict.fromkeys([
        *(network_config.get("bangumiApiEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("bangumi_api")),
    ]))
    bucket = str(bgm_id)[0]
    subject_urls = [f"{str(base).rstrip('/')}/{bucket}/{bgm_id}.json" for base in cache_bases]
    subject_urls.extend(f"{str(base).rstrip('/')}/v0/subjects/{bgm_id}" for base in bases)
    started = time.monotonic()
    try:
        IMAGE_LIMITER.wait()
        subject, _ = network_sources.fetch_json(
            subject_urls, timeout=request_timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, attempts=attempts,
            hedge_delays=(0, .2, .5, .9))
        cover_urls = _cover_urls_from_subject(subject)
        if not cover_urls:
            raise LookupError("no_cover")
        image_bases = list(dict.fromkeys([
            *(network_config.get("bangumiImageEndpoints") or []),
            *(item.base_url for item in network_registry.for_service("bangumi_image")),
        ]))
        urls: list[str] = []
        for original in cover_urls:
            parsed = urllib.parse.urlparse(original)
            urls.append(original)
            urls.extend(urllib.parse.urlunparse((urllib.parse.urlparse(base).scheme, urllib.parse.urlparse(base).netloc,
                        parsed.path, parsed.params, parsed.query, parsed.fragment)) for base in image_bases)
        urls = list(dict.fromkeys(urls))
        data, mime, final_url = network_sources.fetch_binary(
            urls, timeout=request_timeout, cooldown=failure_cooldown, limit=12*1024*1024,
            headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/jpeg,image/png"},
            validator=network_validators.image_bytes, attempts=attempts,
            hedge_delays=(0, .15, .4, .8))
        with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error) VALUES(?,?,?,?,?,NULL) ON CONFLICT(anime_id) DO UPDATE SET mime_type=excluded.mime_type,image_blob=excluded.image_blob,source_url=excluded.source_url,fetched_at=excluded.fetched_at,error=NULL",
                       (anime_id, mime, data, final_url, dt.datetime.now(dt.timezone.utc).isoformat()))
        if log_timing:
            print(f"[timing] image.fetch={time.monotonic() - started:.3f}s source={urllib.parse.urlparse(final_url).netloc}", flush=True)
        return data, mime
    except Exception as exc:
        error = "no_cover" if isinstance(exc, LookupError) else f"{type(exc).__name__}: {exc}"
        with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("INSERT INTO anime_image(anime_id,fetched_at,error) VALUES(?,?,?) ON CONFLICT(anime_id) DO UPDATE SET fetched_at=excluded.fetched_at,error=excluded.error",
                       (anime_id, dt.datetime.now(dt.timezone.utc).isoformat(), error))
        log_event("INFO" if error == "no_cover" else "ERROR", "cover_fetch_failed",
                  animeId=anime_id, bgmId=bgm_id, error=error)
        if cached is not None:
            return cached
        return placeholder_image()


def placeholder_image() -> tuple[bytes, str]:
    return (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420"><rect width="300" height="420" fill="#d9e4df"/><path d="M95 195h110v30H95z" fill="#8aa39a"/></svg>', "image/svg+xml")


class CatalogWarmup:
    """Prefetch the seeded default catalog in page order without blocking requests."""
    def __init__(self, db_path: Path, config_store: ConfigStore, image_fetcher: ImageFetcher | None,
                 interactive: Callable[[], bool], ready_event: threading.Event | None = None) -> None:
        self.db_path = db_path
        self.config_store = config_store
        self.image_fetcher = image_fetcher
        self.interactive = interactive
        self.ready_event = ready_event
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.generation = 0
        self.marker: tuple[str, str, int] | None = None
        self.watcher = threading.Thread(target=self._watch, daemon=True, name="anm-catalog-warmup-watch")

    def start(self) -> None:
        if not self.watcher.is_alive():
            if self.ready_event is None or self.ready_event.is_set():
                marker = self._catalog_marker()
                if marker:
                    self.kick(marker[0], marker=marker)
            self.watcher.start()

    def close(self) -> None:
        self.stop_event.set()

    def _catalog_marker(self) -> tuple[str, str, int] | None:
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
                values = dict(db.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('instance_random_seed','archive_digest','record_count')"))
            seed = str(values.get("instance_random_seed") or "")
            count = int(values.get("record_count") or 0)
            return (seed, str(values.get("archive_digest") or ""), count) if seed and count > 0 else None
        except (OSError, ValueError, sqlite3.Error):
            return None

    def _watch(self) -> None:
        while not self.stop_event.wait(.1):
            if self.ready_event is not None and not self.ready_event.is_set():
                continue
            marker = self._catalog_marker()
            if marker and marker != self.marker:
                self.kick(marker[0], marker=marker)

    def kick(self, seed: str | None = None, *, marker: tuple[str, str, int] | None = None) -> None:
        marker = marker or self._catalog_marker()
        if not marker:
            return
        seed = str(seed or marker[0])
        with self.lock:
            self.marker = (seed, marker[1], marker[2])
            self.generation += 1
            generation = self.generation
        threading.Thread(target=self._run, args=(seed, generation), daemon=True,
                         name=f"anm-catalog-warmup-{generation}").start()

    def _current(self, generation: int) -> bool:
        with self.lock:
            return not self.stop_event.is_set() and generation == self.generation

    @staticmethod
    def _recent_range() -> tuple[str, str]:
        today = dt.date.today()
        end_index = today.year * 12 + today.month - 1
        start_index = end_index - 5
        return (f"{start_index // 12:04d}-{start_index % 12 + 1:02d}",
                f"{end_index // 12:04d}-{end_index % 12 + 1:02d}")

    def _params(self, config: dict[str, Any], seed: str, offset: int, page_size: int) -> dict[str, list[str]]:
        start_month, end_month = self._recent_range()
        defaults = config.get("ui", {}).get("filterDefaults", {})
        params: dict[str, list[str]] = {
            "start_from": [start_month], "start_to": [end_month],
            "country": [str(defaults.get("country") or "JP")],
            "media_type": [str(value) for value in defaults.get("mediaTypes", ["tv", "movie"])],
            "limit": [str(page_size)], "offset": [str(offset)],
            "language": [str(config.get("ui", {}).get("language") or "zh-Hans")],
            "sort": ["random"], "direction": ["asc"], "seed": [seed],
        }
        try:
            torrents = int(runtime_catalog.runtime_stats(self.db_path).get("torrents", 0))
        except (OSError, sqlite3.Error, ValueError):
            torrents = 0
        availability = defaults.get("availability", ["available"]) if torrents else ["available", "unavailable"]
        params["availability"] = [str(value) for value in availability]
        params["library_state"] = [str(value) for value in defaults.get("libraryStates", [
            "existing", "placeholder", "queued", "downloading", "external", "absent",
        ])]
        return params

    def _ani_prefetch(self, anime_id: int, config: dict[str, Any], generation: int) -> None:
        while self._current(generation) and self.interactive():
            if self.stop_event.wait(.2):
                return
        if not self._current(generation):
            return
        try:
            with contextlib.closing(sqlite3.connect(self.db_path, timeout=15)) as db:
                fresh = db.execute(
                    "SELECT 1 FROM ani_rss_resource WHERE anime_id=? AND julianday(expires_at)>=julianday('now') LIMIT 1",
                    (anime_id,)).fetchone()
            if not fresh:
                ani_rss.search(self.db_path, anime_id, config)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, urllib.error.URLError):
            return

    def _wait_for_images(self, anime_ids: list[int], generation: int) -> bool:
        if self.image_fetcher is None:
            return True
        while self._current(generation):
            if not any(self.image_fetcher.pending(anime_id) for anime_id in anime_ids):
                return True
            self.stop_event.wait(.1)
        return False

    def _run(self, seed: str, generation: int) -> None:
        ani_pool: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            config = self.config_store.read()
            raw_page_size = config.get("ui", {}).get("pageSize", 12)
            try:
                page_size = min(100, max(1, int(raw_page_size)))
            except (TypeError, ValueError):
                page_size = 12
            prefetch_pages = max(1, min(8, int(os.getenv("ANM_IMAGE_PREFETCH_PAGES", "8"))))
            network = config.get("metadata", {}).get("network", {})
            ani_settings = config.get("components", {}).get("aniRss", {})
            ani_enabled = str(ani_settings.get("mode") or "prefer").casefold() == "prefer"
            if ani_enabled:
                try:
                    ani_enabled = bool(ani_rss.state(self.db_path, config).get("credentialConfigured"))
                except (OSError, sqlite3.Error, ValueError):
                    ani_enabled = False
            ani_workers = max(1, min(4, int(os.getenv("ANM_ANI_RSS_PREFETCH_WORKERS", "2"))))
            ani_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=ani_workers, thread_name_prefix="anm-ani-prefetch"
            ) if ani_enabled else None
            ani_futures: list[concurrent.futures.Future[Any]] = []
            offset = 0
            total: int | None = None
            started = time.monotonic()
            available = unavailable = failed = 0
            announced = False
            while self._current(generation) and (total is None or offset < total):
                window_ids: list[int] = []
                pages_loaded = 0
                while self._current(generation) and pages_loaded < prefetch_pages and (total is None or offset < total):
                    payload = query_catalog(self.db_path, self._params(config, seed, offset, page_size), config)
                    total = int(payload.get("total", 0))
                    anime_ids = [int(item["id"]) for item in payload.get("items", [])]
                    if not announced:
                        snapshot = self.image_fetcher.snapshot() if self.image_fetcher is not None else {"workers": 0, "hostLimit": 0}
                        subject_urls = {
                            *(str(value).rstrip("/") for value in network.get("bangumiSubjectCacheEndpoints") or []),
                            *(str(value).rstrip("/") for value in network.get("bangumiApiEndpoints") or []),
                            *(item.base_url for item in network_registry.for_service("bangumi_subject_cache")),
                            *(item.base_url for item in network_registry.for_service("bangumi_api")),
                        }
                        image_urls = {
                            *(str(value).rstrip("/") for value in network.get("bangumiImageEndpoints") or []),
                            *(item.base_url for item in network_registry.for_service("bangumi_image")),
                        }
                        print(
                            f"[images] preload start works={total} pageSize={page_size} windowPages={prefetch_pages} "
                            f"images={'on' if self.image_fetcher is not None else 'off'} "
                            f"workers={snapshot.get('workers', 0)} hostLimit={snapshot.get('hostLimit', 0)} "
                            f"subjectSources={len(subject_urls)} imageSources={len(image_urls)} "
                            f"timeout={network.get('probeTimeoutSeconds', 12)}s Ani-RSS={'on' if ani_enabled else 'off'}",
                            flush=True,
                        )
                        announced = True
                    if not anime_ids:
                        break
                    window_ids.extend(anime_ids)
                    if self.image_fetcher is not None:
                        for anime_id in anime_ids:
                            self.image_fetcher.enqueue(anime_id, network, priority="prefetch")
                    if ani_pool is not None:
                        for anime_id in anime_ids:
                            ani_futures.append(ani_pool.submit(self._ani_prefetch, anime_id, config, generation))
                    offset += len(anime_ids)
                    pages_loaded += 1
                if not window_ids:
                    break
                if not self._wait_for_images(window_ids, generation):
                    break
                if self.image_fetcher is not None:
                    for anime_id in window_ids:
                        result = str(self.image_fetcher.result(anime_id) or "error:unknown")
                        if result == "available":
                            available += 1
                        elif result == "unavailable":
                            unavailable += 1
                        else:
                            failed += 1
                elapsed = max(.001, time.monotonic() - started)
                progress_pct = (100.0 * offset / total) if total else 100.0
                pending = self.image_fetcher.snapshot().get("pending", 0) if self.image_fetcher is not None else 0
                ani_done = sum(1 for future in ani_futures if future.done() and not future.cancelled())
                metrics.progress(
                    f"[images] {offset}/{total or offset} ({progress_pct:.1f}%) "
                    f"available={available} unavailable={unavailable} errors={failed} "
                    f"pending={pending} rate={offset / elapsed:.2f}/s "
                    f"Ani-RSS={ani_done}/{len(ani_futures)} elapsed={elapsed:.1f}s"
                )
            if self._current(generation):
                ani_done = sum(1 for future in ani_futures if future.done() and not future.cancelled())
                metrics.progress(
                    f"[images] preload complete images={offset}/{total or offset} "
                    f"Ani-RSS={ani_done}/{len(ani_futures)} elapsed={time.monotonic() - started:.1f}s",
                    final=True,
                )
                log_event("INFO", "catalog_warmup_complete", seed=seed, works=int(total or 0),
                          aniRss=bool(ani_enabled))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            metrics.end_progress()
            if self._current(generation):
                log_event("ERROR", "catalog_warmup_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if ani_pool is not None:
                ani_pool.shutdown(wait=False, cancel_futures=not self._current(generation))


_ADMIN_GET_PATHS = frozenset({
    "/api/logs", "/api/history", "/api/archive/update", "/api/maintenance/status", "/api/auth/users",
})
_ADMIN_POST_PATHS = frozenset({
    "/api/archive/update", "/api/archive/import", "/api/images/refresh", "/api/metadata/repair",
    "/api/catalog/reshuffle", "/api/ani-rss/sync", "/api/connections/test",
    "/api/connections/qbittorrent/credential", "/api/connections/ani-rss/credential",
    "/api/connections/subtitles/credentials", "/api/library/audit", "/api/auth/users",
})


def requires_admin(method: str, path: str) -> bool:
    method = method.upper()
    if method == "GET":
        return path in _ADMIN_GET_PATHS
    if method == "POST":
        return (
            path in _ADMIN_POST_PATHS
            or path.startswith("/api/history/")
            or re.fullmatch(r"/api/auth/users/\d+", path) is not None
        )
    return method in {"PUT", "DELETE"}


def make_handler(db_path: Path, config_store: ConfigStore, *, submission_enabled: bool = True,
                  plan_dir: Path | None = None, image_fetcher: ImageFetcher | None = None,
                  warmup_ready: threading.Event | None = None):
    static_dir = PACKAGE_ROOT / "web" / "static"
    plans = plan_dir or (db_path.parent / "plans")
    archive_updater = archive_update.ArchiveUpdater(
        db_path, sys.modules[__name__], config_store,
        archive_dir=Path(os.getenv("ANM_ARCHIVE_DIR", str(db_path.parent / "archive"))),
        operation_lock=DATABASE_MAINTENANCE_LOCK,
    )
    auth_store = auth.Store(
        Path(os.getenv("ANM_AUTH_DB", str(STATE_DIR / "auth" / "auth.sqlite3"))),
        enabled=os.getenv("ANM_AUTH_ENABLED", "false").casefold() == "true",
        bootstrap_username=os.getenv("ANM_ADMIN_USERNAME", ""),
        bootstrap_password=os.getenv("ANM_ADMIN_PASSWORD", ""),
    )
    maintenance_lock = threading.Lock()
    maintenance: dict[str, dict[str, Any]] = {
        "images": {"state": "idle", "done": 0, "total": 0, "priorityDone": 0, "priorityTotal": 0, "failed": 0},
        "metadata": {"state": "idle", "processed": 0, "repaired": 0, "failed": 0},
        "library": {"state": "idle", "done": 0, "total": 0, "updated": 0, "skipped": 0},
        "torrentSearch": {},
        "aniRssSearch": {},
    }
    media_tokens = playback.MediaTokenRegistry()
    playlist_tokens = playback.PlaylistTokenRegistry()
    login_lock = threading.Lock()
    login_failures: dict[str, deque[float]] = {}
    interactive_until = [0.0]
    catalog_work_lock = threading.Lock()
    submission_guard_lock = threading.Lock()
    submission_reservations: dict[str, int] = {}
    recovery_lock = threading.Lock()
    recovery_file = Path(os.getenv("ANM_MAINTENANCE_RECOVERY_FILE", str(db_path.parent / "maintenance-recovery.json")))

    def read_recovery() -> dict[str, dict[str, Any]]:
        with recovery_lock:
            try:
                payload = json.loads(recovery_file.read_text(encoding="utf-8"))
                return dict(payload) if isinstance(payload, dict) else {}
            except (OSError, ValueError, TypeError):
                return {}

    def write_recovery(name: str, payload: dict[str, Any] | None) -> None:
        with recovery_lock:
            try:
                current = json.loads(recovery_file.read_text(encoding="utf-8")) if recovery_file.is_file() else {}
            except (OSError, ValueError, TypeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            if payload is None:
                current.pop(name, None)
            else:
                current[name] = payload
            try:
                if not current:
                    recovery_file.unlink(missing_ok=True)
                    return
                recovery_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = recovery_file.with_suffix(recovery_file.suffix + ".tmp")
                temporary.write_text(json.dumps(current, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
                temporary.replace(recovery_file)
            except OSError as exc:
                log_event("WARNING", "maintenance_recovery_state_failed", task=name, errorType=type(exc).__name__)

    def submission_is_enabled() -> bool:
        configured = config_store.read().get("components", {}).get("downloadClient", {}).get("submissionEnabled")
        return bool(submission_enabled if configured is None else configured)


    def reserve_submission_space(plan_id: str, plan: dict[str, Any]) -> int:
        config = config_store.read()
        if config.get("download", {}).get("requireFreeSpaceCheck", True) is not True:
            return 0
        jobs = list(plan.get("jobs") or [])
        if not jobs:
            return 0
        minimum_tib = float(config.get("storageGuard", {}).get("minimumFreeTiB", 0.1))
        minimum_bytes = int(minimum_tib * (1024 ** 4))
        planned_bytes = sum(max(0, int(job.get("selectedBytes") or 0)) for job in jobs)
        library_root_value = str(config.get("deployment", {}).get("libraryUncRoot") or "").strip()
        if not library_root_value:
            raise ValueError("library root is not configured for the free-space check")
        library_root = Path(library_root_value)
        storage = status_for_path(library_root, require_write=True, timeout=4.0)
        if storage.state != AVAILABLE:
            raise ValueError(f"library storage is unavailable: {storage.state}")
        try:
            free_bytes = int(shutil.disk_usage(library_root).free)
        except OSError as exc:
            raise ValueError(f"cannot read free space for library root: {library_root}") from exc
        already_reserved = sum(submission_reservations.values())
        remaining = free_bytes - already_reserved - planned_bytes
        if remaining < minimum_bytes:
            raise ValueError(
                f"insufficient free space: submission would leave {remaining / (1024 ** 4):.3f} TiB; "
                f"minimum reserve is {minimum_tib:g} TiB"
            )
        submission_reservations[plan_id] = planned_bytes
        return planned_bytes

    def authorize_subtitle_target(anime_id: int, target: str) -> tuple[bool, str]:
        config = config_store.read()
        try:
            requested = playback.authorize_media_path(target, config)
        except path_policy.PathAuthorizationError:
            return False, ""
        requested_key = path_policy.identity(requested)
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            physical_id = runtime_catalog.physical_anime_id(connection, anime_id)
            runtime_rows = list(connection.execute("""SELECT rw.target_unc,lower(COALESCE(t.source_class,''))
                FROM runtime_work rw
                LEFT JOIN runtime_torrent_work tw ON tw.private_work_id=rw.private_work_id
                LEFT JOIN runtime_torrent t ON t.info_hash=tw.info_hash
                WHERE rw.anime_id=?""", (physical_id,)))
            external_rows = list(connection.execute("""SELECT absolute_path FROM external_media_file
                WHERE anime_id=? AND match_state='verified'""", (physical_id,))) if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_media_file'").fetchone() else []
        source_classes: set[str] = set()
        matched_runtime = False
        for raw_target, source_class in runtime_rows:
            if not raw_target:
                continue
            try:
                candidate = playback.authorize_media_path(str(raw_target), config)
                if path_policy.identity(candidate) == requested_key:
                    matched_runtime = True
                    if source_class:
                        source_classes.add(str(source_class).casefold())
            except path_policy.PathAuthorizationError:
                continue
        matched_external = False
        for (raw_file,) in external_rows:
            try:
                candidate = playback.authorize_media_path(Path(str(raw_file)).parent, config)
                if path_policy.identity(candidate) == requested_key:
                    matched_external = True
                    break
            except path_policy.PathAuthorizationError:
                continue
        if not matched_runtime and not matched_external:
            return False, ""
        serial = {str(value).casefold() for value in config.get("torrentPolicy", {}).get("sourceFamilies", {}).get("serial", [])}
        if matched_runtime and source_classes and not (source_classes - serial) and not matched_external:
            return False, str(requested)
        return True, str(requested)

    def subtitle_target_allowed(anime_id: int, target: str) -> bool:
        return authorize_subtitle_target(anime_id, target)[0]

    def mark_interactive(seconds: float = 8.0) -> None:
        interactive_until[0] = max(interactive_until[0], time.monotonic() + seconds)

    def background_cooperate() -> None:
        if time.monotonic() < interactive_until[0]:
            time.sleep(0.12)

    catalog_warmup = CatalogWarmup(
        db_path, config_store, image_fetcher,
        interactive=lambda: time.monotonic() < interactive_until[0], ready_event=warmup_ready,
    )
    catalog_warmup.start()

    def write_sync_status(phase: str, state: str, details: dict[str, Any]) -> None:
        payload = {"schemaVersion": 1, "phase": phase, "state": state,
                   "updatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                   "details": details, "stats": details}
        SYNC_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SYNC_STATUS_FILE.with_suffix(SYNC_STATUS_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(SYNC_STATUS_FILE)

    def start_library_audit(anime_id: int | None = None) -> bool:
        key = str(anime_id) if anime_id is not None else "all"
        with maintenance_lock:
            if maintenance["library"].get("state") == "running":
                return False
            maintenance["library"] = {"state": "running", "scope": key, "done": 0,
                                      "total": 1 if anime_id is not None else 0, "updated": 0, "skipped": 0}
        def progress(stats: dict[str, int]) -> None:
            with maintenance_lock:
                maintenance["library"].update(done=int(stats.get("processed", 0)),
                                                total=int(stats.get("total", 0)),
                                                updated=int(stats.get("updated", 0)),
                                                skipped=int(stats.get("skipped", 0)))
        write_recovery("libraryAudit", {"animeId": anime_id})
        def worker() -> None:
            try:
                with DATABASE_MAINTENANCE_LOCK:
                    audit_ids = None
                    if anime_id is not None:
                        with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as audit_db:
                            audit_ids = [runtime_catalog.physical_anime_id(audit_db, anime_id)]
                    summary = library_audit.audit(db_path, config_store.read(),
                        anime_ids=audit_ids,
                        progress=progress, throttle=background_cooperate)
                with maintenance_lock:
                    maintenance["library"].update(state="complete", done=summary["total"], **summary)
                log_event("INFO", "library_audit_complete", scope=key, **summary)
            except Exception as exc:
                with maintenance_lock:
                    maintenance["library"].update(state="failed", error=f"{type(exc).__name__}: {exc}")
                log_event("ERROR", "library_audit_failed", scope=key, error=f"{type(exc).__name__}: {exc}")
            finally:
                write_recovery("libraryAudit", None)
        try:
            threading.Thread(target=worker, daemon=True, name=f"anm-library-audit-{key}").start()
        except RuntimeError:
            write_recovery("libraryAudit", None)
            raise
        return True

    def start_torrent_search(anime_id: int) -> bool:
        key = str(anime_id)
        with maintenance_lock:
            prior = maintenance["torrentSearch"].get(key, {})
            if prior.get("state") == "running":
                return False
            maintenance["torrentSearch"][key] = {"state": "running", "phase": "pool_discovery",
                                                  "animeId": anime_id, "mapped": 0}
        def update(stats: dict[str, int]) -> None:
            with maintenance_lock:
                maintenance["torrentSearch"][key].update(phase="torrent_mapping", **stats)
            write_sync_status("torrent_mapping", "running", {"animeId": anime_id, **stats})
        def worker() -> None:
            try:
                with catalog_work_lock, DATABASE_MAINTENANCE_LOCK:
                    current = config_store.read()
                    torrent_pool = Path(str(current.get("deployment", {}).get("torrentPoolRoot") or TORRENT_POOL))
                    with contextlib.closing(sqlite3.connect(db_path, timeout=60)) as meta:
                        effective_id = runtime_catalog.physical_anime_id(meta, anime_id)
                        aliases = [str(row[0]) for row in meta.execute(
                            "SELECT title FROM anime_title WHERE anime_id IN (?,?) ORDER BY length(title) DESC",
                            (anime_id, effective_id))]
                    pool_storage = status_for_path(torrent_pool, timeout=4.0)
                    if pool_storage.state != AVAILABLE:
                        with maintenance_lock:
                            maintenance["torrentSearch"][key].update(
                                state="unavailable", phase="pool_unavailable", storageState=pool_storage.state
                            )
                        write_sync_status("torrent_search", "unavailable",
                                          {"animeId": anime_id, "storageState": pool_storage.state})
                        log_event("WARNING", "torrent_pool_unavailable", animeId=anime_id, storageState=pool_storage.state)
                        return
                    queue = STATE_DIR / "review" / "unmapped-torrents.json"
                    command = [sys.executable, "-m", "animemachine.torrents.scanner", str(torrent_pool), "--db", str(RUNTIME_DB),
                               "--config", str(config_store.path), "--queue-output", str(queue),
                               "--progress-file", str(SYNC_STATUS_FILE), "--workers", "2"]
                    for alias in aliases[:12]:
                        if len(torrent_mapper.norm(alias)) >= 3:
                            command.extend(["--name-query", alias])
                    completed = subprocess.run(command, text=True, capture_output=True, timeout=1800)
                    if completed.returncode == 3:
                        with maintenance_lock:
                            maintenance["torrentSearch"][key].update(
                                state="unavailable", phase="pool_unavailable", storageState="unavailable"
                            )
                        write_sync_status("torrent_search", "unavailable",
                                          {"animeId": anime_id, "storageState": "unavailable"})
                        log_event("WARNING", "torrent_pool_unavailable", animeId=anime_id, storageState="unavailable")
                        return
                    if completed.returncode not in {0, 2}:
                        raise RuntimeError((completed.stderr or completed.stdout or "targeted pool scan failed")[-1000:])
                    result = torrent_mapper.remap_one_work(db_path, RUNTIME_DB, current, effective_id, progress=update)
                    overlay = runtime_catalog.sync_overlay(db_path, RUNTIME_DB)
                with maintenance_lock:
                    maintenance["torrentSearch"][key].update(state="complete", phase="ready", **result,
                                                                overlayTorrents=int(overlay.get("torrents", 0)))
                write_sync_status("ready", "complete", {"animeId": anime_id, **result})
                log_event("INFO", "torrent_search_complete", animeId=anime_id, **result)
            except Exception as exc:
                with maintenance_lock:
                    maintenance["torrentSearch"][key].update(state="failed", error=f"{type(exc).__name__}: {exc}")
                write_sync_status("torrent_search", "error", {"animeId": anime_id, "errorType": type(exc).__name__})
                log_event("ERROR", "torrent_search_failed", animeId=anime_id, error=f"{type(exc).__name__}: {exc}")
        threading.Thread(target=worker, daemon=True, name=f"anm-torrent-search-{anime_id}").start()
        return True

    def start_ani_rss_search(anime_id: int) -> bool:
        key = str(anime_id)
        with maintenance_lock:
            prior = maintenance["aniRssSearch"].get(key, {})
            if prior.get("state") == "running":
                return False
            maintenance["aniRssSearch"][key] = {"state": "running", "animeId": anime_id, "found": 0}
        def worker() -> None:
            try:
                with ANI_RSS_OPERATION_LOCK:
                    result = ani_rss.search(db_path, anime_id, config_store.read())
                with maintenance_lock:
                    maintenance["aniRssSearch"][key].update(state="complete", **result)
                log_event("INFO", "ani_rss_search_complete", animeId=anime_id,
                          found=int(result.get("found", 0)))
            except Exception as exc:
                with maintenance_lock:
                    maintenance["aniRssSearch"][key].update(
                        state="failed", error=f"{type(exc).__name__}: {exc}")
                log_event("ERROR", "ani_rss_search_failed", animeId=anime_id,
                          errorType=type(exc).__name__)
        threading.Thread(target=worker, daemon=True, name=f"anm-ani-rss-search-{anime_id}").start()
        return True

    def start_image_refresh(priority_ids: list[int]) -> bool:
        if image_fetcher is None:
            return False
        with maintenance_lock:
            if maintenance["images"]["state"] == "running":
                return False
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                all_ids = [int(row[0]) for row in db.execute("SELECT id FROM anime_work ORDER BY id")]
            all_id_set = set(all_ids)
            priority = list(dict.fromkeys(value for value in priority_ids if value in all_id_set))
            priority_set = set(priority)
            queue = priority + [value for value in all_ids if value not in priority_set]
            maintenance["images"] = {"state": "running", "done": 0, "total": len(queue), "priorityDone": 0, "priorityTotal": len(priority), "failed": 0}
        network = config_store.read().get("metadata", {}).get("network", {})
        accepted = [anime_id for anime_id in queue if image_fetcher.enqueue(anime_id, network, refresh=True)]
        def monitor() -> None:
            while True:
                remaining = sum(1 for anime_id in accepted if image_fetcher.pending(anime_id))
                priority_remaining = sum(1 for anime_id in priority if image_fetcher.pending(anime_id))
                with maintenance_lock:
                    state = maintenance["images"]
                    state["done"] = len(queue) - remaining
                    state["priorityDone"] = len(priority) - priority_remaining
                if not remaining:
                    break
                time.sleep(5)
            with maintenance_lock:
                maintenance["images"]["failed"] = sum(
                    1 for anime_id in accepted if str(image_fetcher.result(anime_id) or "").startswith(("error", "unavailable")))
                maintenance["images"]["state"] = "complete"
            log_event("INFO", "image_refresh_complete", **maintenance["images"])
        threading.Thread(target=monitor, daemon=True, name="anm-image-refresh-monitor").start()
        return True

    def start_metadata_repair() -> bool:
        with maintenance_lock:
            if maintenance["metadata"]["state"] == "running":
                return False
            maintenance["metadata"] = {"state": "running", "processed": 0, "repaired": 0, "failed": 0}
        write_recovery("metadataRepair", {})
        def worker() -> None:
            try:
                queued = metadata_repair.enqueue(db_path)
                while True:
                    result = metadata_repair.run_batch(db_path, config_store.read())
                    with maintenance_lock:
                        for key in ("processed", "repaired", "failed"):
                            maintenance["metadata"][key] += int(result[key])
                    if not result["processed"] or (result["failed"] and not result["repaired"]):
                        break
                    time.sleep(2)
                with maintenance_lock:
                    maintenance["metadata"]["state"] = "complete"
                    maintenance["metadata"]["queued"] = queued
                log_event("INFO", "metadata_repair_complete", **maintenance["metadata"])
            except Exception as exc:
                with maintenance_lock:
                    maintenance["metadata"].update(state="failed", error=f"{type(exc).__name__}: {exc}")
                log_event("ERROR", "metadata_repair_failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                write_recovery("metadataRepair", None)
        try:
            threading.Thread(target=worker, daemon=True, name="anm-metadata-repair").start()
        except RuntimeError:
            write_recovery("metadataRepair", None)
            raise
        return True

    def recover_interrupted_maintenance() -> dict[str, bool]:
        pending = read_recovery()
        result = {"archiveUpdate": archive_updater.recover_interrupted(), "libraryAudit": False, "metadataRepair": False}
        library = pending.get("libraryAudit")
        if isinstance(library, dict):
            raw_id = library.get("animeId")
            anime_id = int(raw_id) if raw_id is not None and str(raw_id).isdigit() else None
            result["libraryAudit"] = start_library_audit(anime_id)
        if isinstance(pending.get("metadataRepair"), dict):
            result["metadataRepair"] = start_metadata_repair()
        return result

    def submit_in_background(plan_id: str, plan_path: Path) -> None:
        audit_path = plans / f"{plan_id}.audit.json"
        environment = os.environ.copy()
        secret_file = os.getenv("ANM_QBT_API_KEY_FILE") or os.getenv("ANM_QBITTORRENT_BOOTSTRAP_SECRET_FILE")
        if not environment.get("ANM_QBT_API_KEY") and secret_file and Path(secret_file).is_file():
            environment["ANM_QBT_API_KEY"] = Path(secret_file).read_text(encoding="utf-8").strip()
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            if payload.get("jobs"):
                command = [sys.executable, "-m", "animemachine.torrents.submit", str(plan_path), "--apply", "--audit-output", str(audit_path)]
                if os.getenv("ANM_QBT_ALLOW_AUTH_BYPASS", "false").casefold() == "true":
                    command.append("--allow-auth-bypass")
                completed = subprocess.run(command,
                                           env=environment, text=True, capture_output=True, timeout=3600)
                if completed.returncode:
                    raise RuntimeError((completed.stderr or completed.stdout or "submission worker failed")[-1000:])
            current = config_store.read()
            for job in payload.get("aniRssJobs", []):
                ani_rss.subscribe(db_path, str(job["resourceId"]), current)
            runtime_catalog.finish_plan_submission(db_path, plan_id, success=True)
            if payload.get("jobs"):
                refreshed = qbt_runtime.refresh(db_path, current["components"]["downloadClient"]["endpoint"],
                                                current["components"]["downloadClient"]["category"])
                completed_ids = [int(value) for value in refreshed.get("completedAnimeIds", [])]
                if completed_ids:
                    library_audit.audit(db_path, current, anime_ids=completed_ids, throttle=background_cooperate)
        except Exception as exc:
            retryable = not isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError))
            runtime_catalog.finish_plan_submission(
                db_path, plan_id, success=False, error=f"{type(exc).__name__}: {exc}", retryable=retryable)
        finally:
            with submission_guard_lock:
                submission_reservations.pop(plan_id, None)

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {
            **SimpleHTTPRequestHandler.extensions_map,
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".m3u": "audio/x-mpegurl; charset=utf-8",
            ".ps1": "text/plain; charset=utf-8",
        }

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

        def end_headers(self) -> None:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            try:
                status = int(args[1])
            except (IndexError, TypeError, ValueError):
                status = 0
            if 200 <= status < 400:
                return
            rendered = fmt % args
            rendered = re.sub(r"(/api/playback/(?:media|playlist)/)[^/?\s]+", r"\1[redacted]", rendered)
            print(f"[web] {self.address_string()} {rendered}", flush=True)

        def write_body(self, body: bytes) -> bool:
            """Write a response without treating browser cancellation as a server fault."""
            try:
                self.wfile.write(body)
                return True
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                self.close_connection = True
                return False

        def copyfile(self, source: Any, outputfile: Any) -> None:
            try:
                super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                self.close_connection = True

        def json_response(self, payload: Any, status: int = 200, *, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.write_body(body)

        def session(self) -> auth.Session | None:
            return auth_store.from_cookie(self.headers.get("Cookie", ""))

        def visible_config(self) -> dict[str, Any]:
            config = config_store.read_persistent()
            if getattr(self, "auth_session", None) is not None and self.auth_session.role == "admin":
                return config
            # Keep product policy visible to ordinary users while withholding local paths, endpoints and network details.
            safe = json.loads(json.dumps(config))
            deployment = safe.setdefault("deployment", {})
            for key in ("libraryUncRoot", "qbtLibraryRoot", "torrentPoolRoot"):
                deployment[key] = ""
            components = safe.setdefault("components", {})
            components.setdefault("downloadClient", {})["endpoint"] = ""
            ani = components.setdefault("aniRss", {})
            ani["endpoint"] = ""
            ani["mediaPath"] = ""
            for source in safe.get("externalLibraries", []):
                source["path"] = ""
            network = safe.setdefault("metadata", {}).setdefault("network", {})
            for key in list(network):
                if key.casefold().endswith("endpoints") or "proxy" in key.casefold():
                    network[key] = [] if isinstance(network[key], list) else ""
            playback_config = safe.setdefault("playback", {})
            playback_config["publicBaseUrl"] = ""
            playback_config["directPathMappings"] = []
            return safe

        def _safe_no_auth_write(self) -> bool:
            host = self.headers.get("Host", "").strip()
            if not host or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
                return False
            try:
                hostname = urllib.parse.urlparse("//" + host).hostname or ""
                normalized = hostname.strip("[]").casefold()
                trusted = normalized == "localhost" or normalized in {socket.gethostname().casefold(), socket.getfqdn().casefold()}
                if not trusted:
                    address = ipaddress.ip_address(normalized)
                    trusted = address.is_loopback or address.is_private or address.is_link_local
            except ValueError:
                trusted = False
            if not trusted or self.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
                return False
            origin = self.headers.get("Origin", "").strip()
            if origin:
                parsed = urllib.parse.urlparse(origin)
                if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != host.casefold():
                    return False
            return True

        def authorize(self, *, admin: bool = False, csrf: bool = False) -> bool:
            session = self.session()
            if not session:
                self.json_response({"error": "authentication_required"}, HTTPStatus.UNAUTHORIZED)
                return False
            if admin and session.role != "admin":
                self.json_response({"error": "administrator_required"}, HTTPStatus.FORBIDDEN)
                return False
            if csrf:
                if auth_store.enabled:
                    if not auth_store.csrf_valid(session, self.headers.get("X-CSRF-Token", "")):
                        self.json_response({"error": "csrf_validation_failed"}, HTTPStatus.FORBIDDEN)
                        return False
                elif not self._safe_no_auth_write():
                    self.json_response({"error": "cross_site_request_rejected"}, HTTPStatus.FORBIDDEN)
                    return False
            self.auth_session = session
            return True

        def binary_response(self, body: bytes, mime: str, *, cache_seconds: int = 86400,
                            headers: dict[str, str] | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"private, max-age={max(0, int(cache_seconds))}")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.write_body(body)

        def public_base_url(self, config: dict[str, Any]) -> str:
            explicit = str(config.get("playback", {}).get("publicBaseUrl") or "").strip().rstrip("/")
            if explicit:
                parsed = urllib.parse.urlparse(explicit)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError("playback.publicBaseUrl must be an absolute HTTP(S) URL")
                return explicit
            host = self.headers.get("Host", "")
            if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
                raise ValueError("invalid Host header")
            scheme = "http"
            if config.get("security", {}).get("trustReverseProxyAuthentication"):
                forwarded = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().casefold()
                if forwarded in {"http", "https"}:
                    scheme = forwarded
            return f"{scheme}://{host}"

        def playlist_response(self, body: bytes, anime_id: int, disposition: str) -> None:
            filename = f"AnimeMachine-{anime_id}.m3u"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.write_body(body)

        def playlist_token_response(self, token: str, anime_id: int, disposition: str) -> None:
            resolved = playlist_tokens.resolve(token)
            if not resolved or resolved[1] != anime_id:
                self.send_error(HTTPStatus.NOT_FOUND, "playlist token expired or unavailable")
                return
            self.playlist_response(resolved[0], anime_id, disposition)

        def media_response(self, token: str, *, head_only: bool = False) -> None:
            locator = media_tokens.resolve(token)
            if not locator:
                self.send_error(HTTPStatus.NOT_FOUND, "media token expired or unavailable")
                return
            if locator.source_type == "ani-rss":
                if not locator.remote_filename:
                    self.send_error(HTTPStatus.NOT_FOUND, "remote media unavailable")
                    return
                requested = self.headers.get("Range", "").strip()
                if requested and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", requested):
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                forwarded_range = requested or ("bytes=0-0" if head_only else "")
                response_started = False
                try:
                    with ani_rss.stream_media(config_store.read(), locator.remote_filename, forwarded_range) as response:
                        status = int(response.status_code)
                        headers = response.headers
                        if head_only and not requested:
                            total = locator.size
                            content_range = str(headers.get("Content-Range") or "")
                            match = re.fullmatch(r"bytes \d+-\d+/(\d+)", content_range)
                            if match:
                                total = int(match.group(1))
                            elif status == HTTPStatus.OK and str(headers.get("Content-Length") or "").isdigit():
                                total = int(headers["Content-Length"])
                            self.send_response(HTTPStatus.OK)
                            self.send_header("Content-Type", headers.get("Content-Type") or playback.media_mime(locator))
                            if total > 0:
                                self.send_header("Content-Length", str(total))
                            self.send_header("Accept-Ranges", headers.get("Accept-Ranges") or "bytes")
                            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(locator.name)}")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.end_headers()
                            response_started = True
                            return
                        self.send_response(status)
                        self.send_header("Content-Type", headers.get("Content-Type") or playback.media_mime(locator))
                        if headers.get("Content-Length"):
                            self.send_header("Content-Length", headers["Content-Length"])
                        if headers.get("Content-Range"):
                            self.send_header("Content-Range", headers["Content-Range"])
                        self.send_header("Accept-Ranges", headers.get("Accept-Ranges") or "bytes")
                        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(locator.name)}")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        response_started = True
                        if head_only:
                            return
                        for chunk in response.iter_raw(1024 * 1024):
                            if chunk:
                                self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return
                except ani_rss.RemoteFileError as exc:
                    if response_started:
                        return
                    if exc.status == 404:
                        status = HTTPStatus.NOT_FOUND
                    elif exc.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                        status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
                    else:
                        status = HTTPStatus.BAD_GATEWAY
                    self.send_error(status, "Ani-RSS media unavailable")
                except (httpx.RequestError, OSError):
                    if not response_started:
                        self.send_error(HTTPStatus.BAD_GATEWAY, "Ani-RSS media unavailable")
                return

            path = locator.local_path
            if locator.source_type != "local" or path is None:
                self.send_error(HTTPStatus.NOT_FOUND, "media token expired or file unavailable")
                return
            try:
                with playback.open_authorized_media(path, config_store.read()) as (stream, path, file_stat):
                    start, end = 0, file_stat.st_size - 1
                    status = HTTPStatus.OK
                    requested = self.headers.get("Range", "")
                    if requested:
                        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                        if not match or (not match.group(1) and not match.group(2)):
                            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                            return
                        if match.group(1):
                            start = int(match.group(1)); end = int(match.group(2) or end)
                        else:
                            suffix = int(match.group(2)); start = max(0, file_stat.st_size - suffix)
                        end = min(end, file_stat.st_size - 1)
                        if start > end or start >= file_stat.st_size:
                            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                            self.send_header("Content-Range", f"bytes */{file_stat.st_size}")
                            self.end_headers()
                            return
                        status = HTTPStatus.PARTIAL_CONTENT
                    length = max(0, end - start + 1)
                    self.send_response(status)
                    self.send_header("Content-Type", playback.media_mime(locator))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", playback.etag(path, file_stat.st_size, file_stat.st_mtime_ns))
                    self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(path.name)}")
                    self.send_header("Cache-Control", "no-store")
                    if status == HTTPStatus.PARTIAL_CONTENT:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_stat.st_size}")
                    self.end_headers()
                    if head_only:
                        return
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            except path_policy.PathAuthorizationError:
                self.send_error(HTTPStatus.NOT_FOUND, "media token expired or file unavailable")
            except OSError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "media file unavailable")

        def read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid JSON body size") from exc
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("invalid JSON body size")
            previous_timeout = self.connection.gettimeout()
            self.connection.settimeout(15)
            try:
                raw = self.rfile.read(length)
            finally:
                self.connection.settimeout(previous_timeout)
            if len(raw) != length:
                raise ValueError("incomplete JSON request body")
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/.well-known/appspecific/com.chrome.devtools.json":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            media_match = re.fullmatch(r"/api/playback/media/([A-Za-z0-9_-]+)/[^/]+", parsed.path)
            if media_match:
                self.media_response(media_match.group(1))
                return
            playlist_token_match = re.fullmatch(r"/api/playback/playlist/([A-Za-z0-9_-]+)/AnimeMachine-(\d+)\.m3u", parsed.path)
            if playlist_token_match:
                disposition = "attachment" if (urllib.parse.parse_qs(parsed.query).get("download") or [""])[0] in {"1", "true"} else "inline"
                self.playlist_token_response(playlist_token_match.group(1), int(playlist_token_match.group(2)), disposition)
                return
            if parsed.path in {"/api/health", "/api/health/live"}:
                marker = hashlib.sha256(str(db_path.resolve()).encode("utf-8")).hexdigest()[:16]
                self.json_response({"ok": True, "service": "AnimeMachine", "version": __version__, "instanceId": marker, "kind": "liveness"})
                return
            if parsed.path == "/api/health/ready":
                ready = bool(warmup_ready is None or warmup_ready.is_set())
                marker = hashlib.sha256(str(db_path.resolve()).encode("utf-8")).hexdigest()[:16]
                self.json_response({"ok": ready, "service": "AnimeMachine", "version": __version__, "instanceId": marker, "kind": "readiness"},
                                   HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/api/auth/session":
                session = self.session()
                if not session:
                    self.json_response({"error": "authentication_required"}, HTTPStatus.UNAUTHORIZED)
                else:
                    self.json_response({"authenticated": True, "username": session.username,
                                        "role": session.role, "csrfToken": session.csrf,
                                        "authEnabled": auth_store.enabled})
                return
            if parsed.path.startswith("/api/") and not self.authorize():
                return
            if requires_admin("GET", parsed.path) and self.auth_session.role != "admin":
                self.json_response({"error": "administrator_required"}, HTTPStatus.FORBIDDEN)
                return
            if parsed.path == "/api/auth/users":
                self.json_response({"items": auth_store.users()})
                return
            if parsed.path == "/api/anime":
                try:
                    mark_interactive()
                    self.json_response(query_catalog(db_path, urllib.parse.parse_qs(parsed.query), config_store.read()))
                except (ValueError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/options":
                self.json_response(catalog_options(db_path))
                return
            if parsed.path == "/api/resource-groups":
                source = load_resource_group_catalog().get("serialProfiles", {})
                self.json_response({"serialProfiles": {
                    language: [{"id": row[0], "displayName": row[1], "wildcard": "*" in row[2]}
                               for row in rows]
                    for language, rows in source.items()
                }})
                return
            if parsed.path == "/api/people":
                params = urllib.parse.parse_qs(parsed.query)
                try:
                    self.json_response(people_options(db_path, (params.get("role") or [""])[0],
                                                      (params.get("q") or [""])[0], int((params.get("limit") or [40])[0])))
                except (ValueError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/logs":
                self.json_response({"items": list(RECENT_LOGS)})
                return
            if parsed.path == "/api/history":
                params = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((params.get("limit") or [200])[0])
                    self.json_response({"items": library_history.list_events(db_path, limit)})
                except (ValueError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/watches":
                self.json_response({"items": runtime_catalog.watches(db_path)})
                return
            if parsed.path == "/api/archive/update":
                self.json_response(archive_updater.status())
                return
            if parsed.path == "/api/maintenance/status":
                with maintenance_lock:
                    self.json_response(json.loads(json.dumps(maintenance)))
                return
            torrent_search_match = re.fullmatch(r"/api/anime/(\d+)/torrents/search", parsed.path)
            if torrent_search_match:
                key = torrent_search_match.group(1)
                with maintenance_lock:
                    self.json_response(dict(maintenance["torrentSearch"].get(key, {"state": "idle", "animeId": int(key)})))
                return
            ani_rss_search_match = re.fullmatch(r"/api/anime/(\d+)/ani-rss/search", parsed.path)
            if ani_rss_search_match:
                key = ani_rss_search_match.group(1)
                with maintenance_lock:
                    self.json_response(dict(maintenance["aniRssSearch"].get(
                        key, {"state": "idle", "animeId": int(key)})))
                return
            if parsed.path == "/api/ani-rss/status":
                self.json_response(ani_rss.state(db_path, config_store.read()))
                return
            match = re.fullmatch(r"/api/anime/(\d+)", parsed.path)
            if match:
                mark_interactive()
                cfg = config_store.read()
                language = (urllib.parse.parse_qs(parsed.query).get("language") or [cfg.get("ui", {}).get("language", "en")])[0]
                cfg = {**cfg, "ui": {**cfg.get("ui", {}), "language": language}}
                detail = catalog_detail(db_path, int(match.group(1)), cfg)
                self.json_response(detail or {"error": "not found"}, 200 if detail else 404)
                return
            graph_match = re.fullmatch(r"/api/anime/(\d+)/relations/graph", parsed.path)
            if graph_match:
                cfg = config_store.read()
                language = (urllib.parse.parse_qs(parsed.query).get("language") or [cfg.get("ui", {}).get("language", "en")])[0]
                cfg = {**cfg, "ui": {**cfg.get("ui", {}), "language": language}}
                graph = catalog_relation_graph(db_path, int(graph_match.group(1)), cfg)
                self.json_response(graph or {"error": "not found"}, 200 if graph else 404)
                return
            playback_match = re.fullmatch(r"/api/anime/(\d+)/playback", parsed.path)
            if playback_match:
                try:
                    mark_interactive(30)
                    cfg = config_store.read()
                    if cfg.get("playback", {}).get("enabled", True) is not True:
                        raise ValueError("playback is disabled")
                    anime_id = int(playback_match.group(1))
                    source = (urllib.parse.parse_qs(parsed.query).get("source") or [""])[0]
                    entries = playback.collect_items(db_path, anime_id, cfg, source=source)
                    remote_source = source.startswith("ani-rss:") or bool(entries and all(item.origin == "ani-rss" for item in entries))
                    self.json_response({
                        "available": bool(entries), "count": len(entries),
                        "sourceType": "ani-rss" if remote_source else "local",
                        "aniRssMediaPathState": playback.ani_rss_media_path_state(cfg) if remote_source else "not_applicable",
                        "items": [
                            {"index": index, "title": item.title, "episode": item.episode,
                             "bytes": item.bytes, "origin": item.origin}
                            for index, item in enumerate(entries, 1)
                        ],
                    })
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            subtitle_match = re.fullmatch(r"/api/anime/(\d+)/subtitles", parsed.path)
            if subtitle_match:
                try:
                    mark_interactive(30)
                    target = (urllib.parse.parse_qs(parsed.query).get("target") or [""])[0]
                    if not target:
                        raise ValueError("target is required")
                    allowed, authorized_target = authorize_subtitle_target(int(subtitle_match.group(1)), target)
                    if not allowed:
                        self.json_response({"applicable": False, "inspection": None, "state": "unavailable"})
                        return
                    current = config_store.read()
                    payload = subtitle_service.inspect_target(authorized_target, ffprobe=str(current.get("subtitles", {}).get("ffprobe") or "ffprobe"), config=current)
                    self.json_response({"applicable": True, "inspection": payload, "state": payload["state"]})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            playlist_match = re.fullmatch(r"/api/anime/(\d+)/playlist\.(m3u|m3u8)", parsed.path)
            if playlist_match:
                try:
                    mark_interactive(120)
                    cfg = config_store.read()
                    if cfg.get("playback", {}).get("enabled", True) is not True:
                        raise ValueError("playback is disabled")
                    params = urllib.parse.parse_qs(parsed.query)
                    start = int((params.get("start") or [1])[0])
                    source = (params.get("source") or [""])[0]
                    disposition = "attachment" if (params.get("download") or [""])[0] in {"1", "true"} else "inline"
                    body, _ = playback.playlist_payload(db_path, int(playlist_match.group(1)), cfg, media_tokens,
                                                        self.public_base_url(cfg), start=start, source=source)
                    self.playlist_response(body, int(playlist_match.group(1)), disposition)
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            image_status_match = re.fullmatch(r"/api/anime/(\d+)/image/status", parsed.path)
            if image_status_match:
                anime_id = int(image_status_match.group(1))
                self.json_response({
                    "animeId": anime_id,
                    "pending": bool(image_fetcher and image_fetcher.pending(anime_id)),
                    "result": image_fetcher.result(anime_id) if image_fetcher is not None else None,
                })
                return
            image_match = re.fullmatch(r"/api/anime/(\d+)/image", parsed.path)
            if image_match:
                mark_interactive(2)
                anime_id = int(image_match.group(1))
                image, cache_state = get_cached_anime_image(db_path, anime_id)
                if image is not None:
                    self.binary_response(*image, headers={"X-AnimeMachine-Image-Status": "available"})
                elif cache_state == "not_found":
                    self.send_error(HTTPStatus.NOT_FOUND, "image unavailable")
                else:
                    queued = False
                    if cache_state != "negative" and image_fetcher is not None:
                        queued = image_fetcher.enqueue(
                            anime_id, config_store.read().get("metadata", {}).get("network", {}))
                    status = "queued" if queued else "unavailable"
                    self.binary_response(*placeholder_image(), cache_seconds=0 if queued else 300,
                                         headers={"X-AnimeMachine-Image-Status": status,
                                                  **({"Retry-After": "1"} if queued else {})})
                return
            if parsed.path == "/api/stats":
                with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
                    payload = dict(db.execute("SELECT key,value FROM metadata"))
                payload["version"] = __version__
                payload["runtime"] = runtime_catalog.runtime_stats(db_path)
                sync_file = Path(os.getenv("ANM_SYNC_STATUS_FILE", str(Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "sync-status.json")))
                try:
                    payload["sync"] = json.loads(sync_file.read_text(encoding="utf-8")) if sync_file.is_file() else {"state": "idle", "phase": "idle"}
                except (OSError, ValueError):
                    payload["sync"] = {"state": "unknown", "phase": "unknown"}
                self.json_response(payload)
                return
            if parsed.path == "/api/capabilities":
                secret_file = os.getenv("ANM_QBT_API_KEY_FILE", "").strip()
                ani_secret_file = os.getenv("ANM_ANI_RSS_API_KEY_FILE", "").strip()
                effective_submission = submission_is_enabled()
                self.json_response({"planPreview": True, "submissionAvailable": True,
                                    "submissionEnabled": effective_submission,
                                    "submissionMode": "stopped", "oneTaskPerInfohash": True,
                                    "auth": {"enabled": auth_store.enabled, "username": self.auth_session.username,
                                             "role": self.auth_session.role},
                                    "qbtCredentialConfigured": bool(
                                        os.getenv("ANM_QBT_API_KEY", "").strip()
                                        or (secret_file and Path(secret_file).is_file())
                                    ),
                                    "aniRssCredentialConfigured": bool(
                                        os.getenv("ANM_ANI_RSS_API_KEY", "").strip()
                                        or (ani_secret_file and Path(ani_secret_file).is_file())
                                    ),
                                    "playback": {"m3u": True, "m3u8Compatibility": True,
                                                 "webPlayback": False, "transcoding": False}})
                return
            plan_match = re.fullmatch(r"/api/plans/([0-9a-f]{32})", parsed.path)
            if plan_match:
                plan = runtime_catalog.get_plan(db_path, plan_match.group(1))
                self.json_response(plan or {"error": "not found"}, 200 if plan else 404)
                return
            if parsed.path == "/api/config":
                self.json_response(self.visible_config())
                return
            if parsed.path == "/api/policy":
                self.json_response({
                    "strictSeriesRelations": sorted(STRICT_SERIES_RELATIONS),
                    "nonGroupingRelations": sorted(NON_GROUPING_RELATIONS),
                    "oneTaskPerInfohash": True,
                    "splitCourRequiresExplicitSameSeasonEvidence": True,
                })
                return
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_HEAD(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            media_match = re.fullmatch(r"/api/playback/media/([A-Za-z0-9_-]+)/[^/]+", parsed.path)
            if media_match:
                self.media_response(media_match.group(1), head_only=True)
                return
            playlist_token_match = re.fullmatch(r"/api/playback/playlist/([A-Za-z0-9_-]+)/AnimeMachine-(\d+)\.m3u", parsed.path)
            if playlist_token_match:
                disposition = "attachment" if (urllib.parse.parse_qs(parsed.query).get("download") or [""])[0] in {"1", "true"} else "inline"
                self.playlist_token_response(playlist_token_match.group(1), int(playlist_token_match.group(2)), disposition)
                return
            super().do_HEAD()

        def do_PUT(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if not self.authorize(admin=True, csrf=True):
                return
            if parsed.path != "/api/config":
                self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self.read_json()
                config_store.write(payload)
                transport.reset()
                current = config_store.read()
                self.json_response({
                    "saved": True, "config": config_store.read_persistent(), "effective": "immediate",
                    "storage": storage_preflight.check_config(current, timeout=3.0),
                })
            except (ValueError, json.JSONDecodeError, OSError) as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_DELETE(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if not self.authorize(admin=True, csrf=True):
                return
            match = re.fullmatch(r"/api/watches/(\d+)", parsed.path)
            if not match:
                self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            deleted = runtime_catalog.delete_watch(db_path, int(match.group(1)))
            self.json_response({"deleted": deleted}, 200 if deleted else HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/auth/login":
                try:
                    client = str(self.client_address[0])
                    now = time.monotonic()
                    with login_lock:
                        if client not in login_failures and len(login_failures) >= 2048:
                            stale = [key for key, values in login_failures.items() if not values or values[-1] < now - 300]
                            for key in stale:
                                login_failures.pop(key, None)
                            if len(login_failures) >= 2048:
                                login_failures.pop(next(iter(login_failures)))
                        failures = login_failures.setdefault(client, deque())
                        while failures and failures[0] < now - 300:
                            failures.popleft()
                        if len(failures) >= 5:
                            self.json_response({"error": "too_many_login_attempts"}, HTTPStatus.TOO_MANY_REQUESTS)
                            return
                    request = self.read_json()
                    session = auth_store.login(str(request.get("username") or ""), str(request.get("password") or ""))
                    if not session:
                        with login_lock:
                            failures.append(now)
                        self.json_response({"error": "invalid_credentials"}, HTTPStatus.UNAUTHORIZED)
                        return
                    with login_lock:
                        login_failures.pop(client, None)
                    trust_proxy = bool(config_store.read().get("security", {}).get("trustReverseProxyAuthentication"))
                    secure = trust_proxy and self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().casefold() == "https"
                    cookie = f"anm_session={session.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={auth.SESSION_SECONDS}"
                    if secure:
                        cookie += "; Secure"
                    self.json_response({"authenticated": True, "username": session.username, "role": session.role,
                                        "csrfToken": session.csrf}, headers={"Set-Cookie": cookie})
                except (ValueError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not self.authorize(csrf=True):
                return
            if requires_admin("POST", parsed.path) and self.auth_session.role != "admin":
                self.json_response({"error": "administrator_required"}, HTTPStatus.FORBIDDEN)
                return
            if parsed.path == "/api/auth/logout":
                auth_store.logout(self.auth_session)
                self.json_response({"authenticated": False}, headers={
                    "Set-Cookie": "anm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                return
            if parsed.path == "/api/catalog/reshuffle":
                seed = rotate_instance_random_seed(db_path)
                catalog_warmup.kick(seed)
                self.json_response({"seed": seed})
                return
            if parsed.path == "/api/auth/users":
                try:
                    request = self.read_json()
                    self.json_response(auth_store.create_user(str(request.get("username") or ""),
                                                               str(request.get("password") or ""),
                                                               str(request.get("role") or "user")), HTTPStatus.CREATED)
                except (ValueError, sqlite3.IntegrityError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            user_match = re.fullmatch(r"/api/auth/users/(\d+)", parsed.path)
            if user_match:
                try:
                    request = self.read_json()
                    changed = auth_store.set_enabled(int(user_match.group(1)), bool(request.get("enabled")),
                                                     actor_id=self.auth_session.user_id)
                    self.json_response({"updated": changed})
                except (ValueError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            playback_handoff_match = re.fullmatch(r"/api/anime/(\d+)/playback/handoff", parsed.path)
            if playback_handoff_match:
                try:
                    request = self.read_json()
                    cfg = config_store.read()
                    if cfg.get("playback", {}).get("enabled", True) is not True:
                        raise ValueError("playback is disabled")
                    anime_id = int(playback_handoff_match.group(1))
                    player = str(request.get("player") or "system").casefold()
                    mode = str(request.get("mode") or "playlist").casefold()
                    source = str(request.get("source") or "")
                    if player not in {"system", "copy", "vlc", "potplayer", "iina"}:
                        raise ValueError("unsupported player")
                    if mode not in {"playlist", "episode"}:
                        raise ValueError("unsupported playback mode")
                    base_url = self.public_base_url(cfg)
                    result: dict[str, Any] = {"player": player, "mode": mode}
                    if mode == "playlist":
                        start = int(request.get("start") or 1)
                        body, items = playback.playlist_payload(db_path, anime_id, cfg, media_tokens, base_url,
                                                                start=start, source=source, force_http=True)
                        idle, maximum = playback.token_policy(cfg)
                        token = playlist_tokens.issue(body, anime_id, idle, maximum)
                        target = f"{base_url.rstrip('/')}/api/playback/playlist/{token}/AnimeMachine-{anime_id}.m3u"
                        result.update({"url": target, "playlistUrl": target, "count": len(items)})
                    else:
                        episode_value = request.get("episode")
                        index_value = request.get("index")
                        episode = float(episode_value) if episode_value is not None else None
                        index = int(index_value) if index_value is not None else None
                        target, item = playback.episode_media(db_path, anime_id, cfg, media_tokens, base_url,
                                                              source=source, episode=episode, index=index)
                        result.update({"url": target, "episode": item.episode, "title": item.title})
                    if player in {"vlc", "potplayer"}:
                        result["protocolUrl"] = playback.player_protocol_url(player, target)
                    elif player == "iina":
                        result["protocolUrl"] = playback.player_protocol_url(player, target)
                    self.json_response(result)
                except (ValueError, OSError, RuntimeError, sqlite3.Error, json.JSONDecodeError, ani_rss.RemoteFileError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            ani_rss_search_match = re.fullmatch(r"/api/anime/(\d+)/ani-rss/search", parsed.path)
            if ani_rss_search_match:
                anime_id = int(ani_rss_search_match.group(1))
                started = start_ani_rss_search(anime_id)
                with maintenance_lock:
                    payload = dict(maintenance["aniRssSearch"].get(str(anime_id), {}))
                self.json_response({"started": started, **payload},
                                   HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            subscribe_match = re.fullmatch(r"/api/ani-rss/resources/(ar-[0-9a-f]{24})/subscribe", parsed.path)
            if subscribe_match:
                try:
                    result = ani_rss.subscribe(db_path, subscribe_match.group(1), config_store.read())
                    self.json_response(result, HTTPStatus.ACCEPTED)
                except (ValueError, RuntimeError, OSError, urllib.error.URLError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            ani_rss_delete_match = re.fullmatch(r"/api/ani-rss/subscriptions/([^/]+)/delete", parsed.path)
            if ani_rss_delete_match:
                try:
                    request = self.read_json()
                    remote_id = urllib.parse.unquote(ani_rss_delete_match.group(1))
                    delete_files = request.get("deleteFiles", False) is True
                    if delete_files and self.auth_session.role != "admin":
                        self.json_response({"error": "administrator_required_for_file_deletion"}, HTTPStatus.FORBIDDEN)
                        return
                    result = ani_rss.delete_subscription(db_path, remote_id, config_store.read(),
                                                         delete_files=delete_files)
                    self.json_response(result)
                except (ValueError, RuntimeError, OSError, urllib.error.URLError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/ani-rss/sync":
                result = ani_rss.sync(db_path, config_store.read())
                self.json_response(result, 200 if result.get("state") == "ready" else HTTPStatus.BAD_GATEWAY)
                return
            torrent_search_match = re.fullmatch(r"/api/anime/(\d+)/torrents/search", parsed.path)
            if torrent_search_match:
                anime_id = int(torrent_search_match.group(1))
                started = start_torrent_search(anime_id)
                with maintenance_lock:
                    payload = dict(maintenance["torrentSearch"].get(str(anime_id), {}))
                self.json_response({"started": started, **payload}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            audit_match = re.fullmatch(r"/api/anime/(\d+)/library/audit", parsed.path)
            if audit_match:
                anime_id = int(audit_match.group(1))
                started = start_library_audit(anime_id)
                self.json_response({"started": started, "animeId": anime_id}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            subtitle_search_match = re.fullmatch(r"/api/anime/(\d+)/subtitles/search", parsed.path)
            if subtitle_search_match:
                try:
                    request = self.read_json()
                    anime_id = int(subtitle_search_match.group(1))
                    target = str(request.get("target") or "")
                    allowed, authorized_target = authorize_subtitle_target(anime_id, target) if target else (False, "")
                    if not allowed:
                        raise ValueError("subtitle target is unavailable or not authorized for this work")
                    self.json_response(subtitle_service.search(db_path, anime_id, authorized_target, config_store.read()))
                except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            subtitle_apply_match = re.fullmatch(r"/api/anime/(\d+)/subtitles/apply", parsed.path)
            if subtitle_apply_match:
                try:
                    request = self.read_json()
                    anime_id = int(subtitle_apply_match.group(1))
                    target = str(request.get("target") or "")
                    candidate_id = str(request.get("candidateId") or "")
                    allowed, authorized_target = authorize_subtitle_target(anime_id, target) if target else (False, "")
                    if not allowed or not candidate_id:
                        raise ValueError("invalid subtitle request")
                    found = subtitle_service.search(db_path, anime_id, authorized_target, config_store.read())
                    candidate = next((x for x in found.get("candidates", []) if x.get("candidateId") == candidate_id), None)
                    if not candidate:
                        raise ValueError("subtitle candidate expired; search again")
                    result = subtitle_service.apply(anime_id, authorized_target, candidate, config_store.read())
                    log_event("INFO", "subtitle_applied", animeId=anime_id, target=authorized_target,
                              provider=candidate.get("provider"), installed=result.get("installed"))
                    self.json_response(result)
                except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            image_match = re.fullmatch(r"/api/anime/(\d+)/image/refresh", parsed.path)
            if image_match:
                anime_id = int(image_match.group(1))
                queued = bool(image_fetcher and image_fetcher.enqueue(
                    anime_id, config_store.read().get("metadata", {}).get("network", {}), refresh=True))
                self.json_response({"refreshed": False, "queued": queued,
                                    "imageUrl": f"/api/anime/{anime_id}/image?v={int(time.time())}"},
                                   HTTPStatus.ACCEPTED if queued else HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/api/plans":
                try:
                    request = self.read_json()
                    current = config_store.read()
                    local_request, remote_jobs = ani_rss.partition_plan(db_path, request, current)
                    skipped_works = local_request.pop("_skippedWorks", [])
                    plan = (runtime_catalog.create_plan(db_path, current, local_request, plan_dir)
                            if local_request.get("animeIds") else None)
                    plan = ani_rss.attach_plan(db_path, plan, request, remote_jobs, plan_dir)
                    plan["skippedWorks"] = skipped_works
                    self.json_response(plan, HTTPStatus.CREATED)
                except (ValueError, KeyError, sqlite3.Error, OSError, RuntimeError, urllib.error.URLError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except Exception as exc:
                    log_event("ERROR", "plan_build_failed", error=f"{type(exc).__name__}: {exc}")
                    self.json_response({"error": "internal plan error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if parsed.path == "/api/archive/update":
                started = archive_updater.start()
                self.json_response({"started": started, **archive_updater.status()}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            if parsed.path == "/api/archive/import":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    filename = urllib.parse.unquote(self.headers.get("X-Archive-Name", "").strip())
                    result = archive_updater.import_stream(self.rfile, length, filename)
                    started = archive_updater.start()
                    self.json_response({**result, "updateStarted": started}, HTTPStatus.ACCEPTED)
                except (ValueError, OSError, RuntimeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/images/refresh":
                request = self.read_json()
                ids = [int(value) for value in request.get("priorityAnimeIds", []) if str(value).isdigit()]
                started = start_image_refresh(ids)
                self.json_response({"started": started, **maintenance["images"]}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            if parsed.path == "/api/metadata/repair":
                started = start_metadata_repair()
                self.json_response({"started": started, **maintenance["metadata"]}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            if parsed.path == "/api/connections/test":
                try:
                    request = self.read_json()
                    kind = str(request.get("kind") or "")
                    if kind == "ani-rss":
                        current = config_store.read()
                        current = json.loads(json.dumps(current))
                        current.setdefault("components", {}).setdefault("aniRss", {})["endpoint"] = str(request.get("endpoint") or "")
                        result = ani_rss.probe(current)
                    else:
                        result = connectivity.probe(kind, str(request.get("endpoint") or ""))
                    log_event("INFO" if result.get("reachable") else "ERROR", "connection_probe", result=result)
                    self.json_response(result)
                except (ValueError, OSError, urllib.error.URLError) as exc:
                    log_event("ERROR", "connection_probe_failed", error=f"{type(exc).__name__}: {exc}")
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/qbittorrent/credential":
                try:
                    request = self.read_json()
                    key = str(request.get("apiKey") or "").strip()
                    if not key:
                        raise ValueError("apiKey must not be empty")
                    # Deliberately process-local: never persist, echo or log a
                    # qBittorrent credential. Docker Secret/environment values
                    # remain the durable deployment mechanism.
                    os.environ["ANM_QBT_API_KEY"] = key
                    self.json_response({"configured": True, "persistence": "process"})
                except (ValueError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/ani-rss/credential":
                try:
                    request = self.read_json()
                    key = str(request.get("apiKey") or "").strip()
                    if not key:
                        raise ValueError("apiKey must not be empty")
                    os.environ["ANM_ANI_RSS_API_KEY"] = key
                    self.json_response({"configured": True, "persistence": "process"})
                except (ValueError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/subtitles/credentials":
                try:
                    request = self.read_json()
                    changed = []
                    for field, environment in (("assrt", "ASSRT_API_TOKEN"), ("opensubtitles", "OPEN_SUBTITLES_API_KEY")):
                        value = str(request.get(field) or "").strip()
                        if value:
                            os.environ[environment] = value
                            changed.append(field)
                    if not changed:
                        raise ValueError("at least one subtitle credential is required")
                    self.json_response({"configured": changed, "persistence": "process"})
                except (ValueError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/library/audit":
                started = start_library_audit()
                self.json_response({"started": started}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            restore_match = re.fullmatch(r"/api/history/(\d+)/restore", parsed.path)
            if restore_match:
                try:
                    current = config_store.read()
                    library_root = Path(str(current["deployment"]["libraryUncRoot"]))
                    history_root = Path(os.getenv("ANM_HISTORY_DIR", str(Path(os.getenv("ANM_STATE_DIR", "/Data/state")) / "history")))
                    restored = library_history.restore_removed(
                        db_path, library_root, history_root, int(restore_match.group(1)))
                    log_event("INFO", "library_history_restored", eventId=int(restore_match.group(1)))
                    self.json_response(restored)
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            submit_match = re.fullmatch(r"/api/plans/([0-9a-f]{32})/submit", parsed.path)
            if submit_match:
                if not submission_is_enabled():
                    self.json_response({"error": "live submission is disabled in this deployment; preview was preserved"}, HTTPStatus.CONFLICT)
                else:
                    try:
                        request = self.read_json()
                        if request.get("confirmStopped") is not True:
                            raise ValueError("confirmStopped=true is required")
                        plan_id = submit_match.group(1)
                        plan_path = plans / f"{plan_id}.approved.json"
                        with submission_guard_lock:
                            plan = runtime_catalog.get_plan(db_path, plan_id)
                            if not plan or plan.get("state") != "preview":
                                raise ValueError("plan is missing, stale, or already submitted")
                            reserve_submission_space(plan_id, plan)
                            try:
                                runtime_catalog.stage_plan_submission(db_path, plan_id, plan_path)
                            except Exception:
                                submission_reservations.pop(plan_id, None)
                                raise
                        threading.Thread(target=submit_in_background, args=(plan_id, plan_path), daemon=True).start()
                        self.json_response({"planId": plan_id, "state": "submitting", "defaultStartMode": "stopped"}, HTTPStatus.ACCEPTED)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def recover_interrupted_submissions() -> None:
        if not submission_is_enabled():
            return
        for recovered in runtime_catalog.recoverable_plan_submissions(db_path):
            plan_id = str(recovered["planId"])
            plan_path = plans / f"{plan_id}.approved.json"
            try:
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text(
                    json.dumps(recovered["payload"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                runtime_catalog.finish_plan_submission(
                    db_path, plan_id, success=False, error=f"{type(exc).__name__}: {exc}", retryable=True)
                continue
            threading.Thread(
                target=submit_in_background, args=(plan_id, plan_path), daemon=True,
                name=f"anm-submit-recovery-{plan_id[:8]}",
            ).start()

    Handler.catalog_warmup = catalog_warmup
    Handler.recover_interrupted_submissions = staticmethod(recover_interrupted_submissions)
    Handler.recover_interrupted_maintenance = staticmethod(recover_interrupted_maintenance)
    return Handler


def _configured_secret(value_name: str, file_name: str, default_file: str = "") -> str:
    value = os.getenv(value_name, "").strip()
    path_value = os.getenv(file_name, default_file).strip()
    if value or not path_value:
        return value
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def _browser_urls(host: str, port: int) -> list[tuple[str, str]]:
    public_url = os.getenv("ANM_PUBLIC_URL", "").strip().rstrip("/")
    urls: list[tuple[str, str]] = []
    if public_url:
        urls.append(("Public/LAN URL", public_url))
    wildcard = host in {"0.0.0.0", "::", ""}
    local_host = "127.0.0.1" if wildcard else host
    urls.append(("Local URL", f"http://{local_host}:{port}"))
    if wildcard and not Path("/.dockerenv").exists():
        addresses: set[str] = set()
        try:
            for entry in socket.getaddrinfo(socket.gethostname(), port, socket.AF_INET, socket.SOCK_STREAM):
                address = str(entry[4][0])
                if address and not address.startswith("127.") and not address.startswith("169.254."):
                    addresses.add(address)
        except OSError:
            pass
        for address in sorted(addresses):
            urls.append(("LAN URL", f"http://{address}:{port}"))
    return list(dict.fromkeys(urls))


def _catalog_access_metadata(db_path: Path) -> dict[str, str]:
    if not db_path.is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as db:
            return {
                str(key): str(value)
                for key, value in db.execute(
                    "SELECT key,value FROM metadata WHERE key IN "
                    "('instance_random_seed','archive_name','archive_created_at','record_count')"
                )
            }
    except (OSError, sqlite3.Error):
        return {}


def _print_access_info(host: str, port: int, config: dict[str, Any], db_path: Path, *,
                       instance_seed: str | None = None,
                       archive_meta: dict[str, Any] | None = None,
                       record_count: int | None = None) -> None:
    qbt = config.get("components", {}).get("downloadClient", {})
    ani = config.get("components", {}).get("aniRss", {})
    lines: list[tuple[str, str]] = [*_browser_urls(host, port)]
    if Path("/.dockerenv").exists() and not os.getenv("ANM_PUBLIC_URL", "").strip():
        lines.append(("Docker LAN URL", f"http://<Docker-host-LAN-IP>:{port}"))
    qbt_endpoint = str(qbt.get("endpoint") or "").strip()
    ani_endpoint = str(ani.get("endpoint") or "").strip()
    if qbt_endpoint:
        lines.append(("qBittorrent API", qbt_endpoint))
    if ani_endpoint:
        lines.append(("Ani-RSS API", ani_endpoint))
    admin_user = os.getenv("ANM_ADMIN_USERNAME", "").strip()
    if admin_user:
        lines.append(("AnimeMachine user", admin_user))
    credential_file = os.getenv("ANM_ENV_FILE", "").strip()
    if credential_file:
        lines.append(("Credential file", credential_file))
    config_file = os.getenv("ANM_CONFIG_PATH", "").strip()
    if config_file:
        lines.append(("Config", config_file))
    state_dir = os.getenv("ANM_STATE_DIR", "").strip()
    if state_dir:
        lines.append(("State directory", state_dir))
    stored = _catalog_access_metadata(db_path)
    seed = str(instance_seed or stored.get("instance_random_seed") or "").strip()
    if seed:
        lines.append(("Random seed", seed))
    archive_meta = archive_meta or {}
    archive_name = str(archive_meta.get("name") or stored.get("archive_name") or "").strip()
    archive_created_at = str(archive_meta.get("created_at") or stored.get("archive_created_at") or "").strip()
    if archive_name:
        lines.append(("Bangumi Archive", archive_name))
    if archive_created_at:
        lines.append(("Archive created", archive_created_at))
    count = record_count if record_count is not None else stored.get("record_count")
    if count not in {None, ""}:
        with contextlib.suppress(TypeError, ValueError):
            lines.append(("Catalog works", f"{int(count):,}"))
    lines.append(("Database", str(db_path)))
    width = max((len(label) for label, _value in lines), default=16)
    print("\n========== AnimeMachine access ==========", flush=True)
    for label, value in lines:
        print(f"{label:<{width}} : {value}", flush=True)
    print("=========================================\n", flush=True)


def serve(db_path: Path, host: str, port: int, config_path: Path = DEFAULT_CONFIG,
          *, submission_enabled: bool = True, plan_dir: Path | None = None,
          ready_callback: Any | None = None,
          background_ready: threading.Event | None = None,
          warmup_ready: threading.Event | None = None,
          print_access_info: bool = True) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}; run build first")
    with contextlib.closing(sqlite3.connect(db_path, timeout=120)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=120000")
        migrate_catalog_features(db)
        runtime_catalog.migrate_overlay(db)
        library_history.migrate(db)
        ani_rss.migrate(db)
    ensure_instance_random_seed(db_path)
    image_fetcher = ImageFetcher(db_path)
    image_fetcher.start()
    handler = make_handler(db_path, ConfigStore(config_path, EXAMPLE_CONFIG),
                           submission_enabled=submission_enabled, plan_dir=plan_dir,
                           image_fetcher=image_fetcher, warmup_ready=warmup_ready)
    server = ThreadingHTTPServer((host, port), handler)
    stop_monitor = threading.Event()
    # Container bootstrap must start only after all schema migrations finish;
    # otherwise the bootstrap writer and Web startup writer can race on the
    # same persistent SQLite database.
    if ready_callback is not None:
        ready_callback()
    handler.recover_interrupted_submissions()
    handler.recover_interrupted_maintenance()
    def monitor_qbt() -> None:
        store = ConfigStore(config_path, EXAMPLE_CONFIG)
        while not stop_monitor.wait(10):
            try:
                if background_ready is not None and not background_ready.is_set():
                    continue
                current = store.read()
                configured = current.get("components", {}).get("downloadClient", {}).get("submissionEnabled")
                if not bool(submission_enabled if configured is None else configured):
                    continue
                with DATABASE_MAINTENANCE_LOCK:
                    refreshed = qbt_runtime.refresh(db_path, current["components"]["downloadClient"]["endpoint"],
                                                    current["components"]["downloadClient"]["category"])
                    completed_ids = [int(value) for value in refreshed.get("completedAnimeIds", [])]
                    if completed_ids:
                        library_audit.audit(db_path, current, anime_ids=completed_ids,
                                            throttle=lambda: time.sleep(0.08))
            except Exception as exc:
                log_event("ERROR", "qbt_state_refresh_failed", error=f"{type(exc).__name__}: {exc}")
    threading.Thread(target=monitor_qbt, daemon=True, name="anm-qbt-state-monitor").start()
    if print_access_info:
        _print_access_info(host, port, ConfigStore(config_path, EXAMPLE_CONFIG).read(), db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        server.server_close()
        handler.catalog_warmup.close()
        image_fetcher.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    def build_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--manifest", type=Path)
        target.add_argument("--ids", help="comma-separated Bangumi subject IDs")
        target.add_argument("--all-anime", action="store_true", help="build every animation subject in Bangumi Archive")
        target.add_argument("--db", type=Path, default=DEFAULT_DB)
        target.add_argument("--archive", type=Path, help="use an existing Bangumi Archive zip")
        target.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
        target.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
        target.add_argument("--cache-days", type=int, default=30)
        target.add_argument("--refresh", action="store_true")
        target.add_argument("--request-interval", type=float, default=0.4)

    build_parser = sub.add_parser("build", help="build the SQLite catalog")
    build_args(build_parser)
    serve_parser = sub.add_parser("serve", help="serve the interactive catalog")
    serve_parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve_parser.add_argument("--submission-enabled", action=argparse.BooleanOptionalAction, default=True)
    demo_parser = sub.add_parser("demo", help="build the sample and serve it")
    build_args(demo_parser)
    demo_parser.add_argument("--host", default="127.0.0.1")
    demo_parser.add_argument("--port", type=int, default=8765)
    demo_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        build(args)
    elif args.command == "serve":
        serve(args.db, args.host, args.port, args.config, submission_enabled=args.submission_enabled)
    elif args.command == "demo":
        serve(build(args), args.host, args.port, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
