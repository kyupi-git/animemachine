#!/usr/bin/env python3
"""Build and browse a small, provenance-aware anime metadata catalog."""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import math
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
from ..config import credentials as credential_store
from ..torrents import runtime as runtime_catalog
from ..integrations import qbt_runtime, connectivity, playback, subtitle_service, ani_rss
from . import archive_update, relation_graph, metadata_repair
from .image_fetcher import ImageFetcher
from ..library import audit as library_audit, external as external_library, history as library_history, layout as library_layout
from ..network import (connectivity as network_connectivity, diagnostics as network_diagnostics,
                       downloads as network_downloads, registry as network_registry,
                       sources as network_sources, tls as tls_support,
                       validators as network_validators, transport)
from ..torrents import mapper as torrent_mapper
from .. import metrics
from ..storage import AVAILABLE, status_for_path
from ..storage import preflight as storage_preflight
from ..storage import path_policy
from ..api import auth
from .. import __version__, application_update

from ..config.loader import (REGION_COUNTRIES, REGION_KEYS, explicitly_disabled,
                             load_resource_group_catalog, region_policy_enabled)


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
    "CN": ("中国", "国产", "國產", "中国大陆", "中國大陸", "中国动画", "中国動畫"),
    "HK": ("香港", "香港动画", "香港動畫"),
    "MO": ("澳门", "澳門", "澳门动画", "澳門動畫"),
    "TW": ("台湾", "臺灣", "台灣", "台湾动画", "臺灣動畫", "台灣動畫"),
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
ANI_RSS_USER_ACTIVITY = threading.Event()
ANI_RSS_USER_ACTIVITY_LOCK = threading.Lock()
ANI_RSS_USER_ACTIVITY_COUNT = 0


@contextlib.contextmanager
def ani_rss_user_operation():
    """Give explicit user Ani-RSS work priority over background synchronization."""
    global ANI_RSS_USER_ACTIVITY_COUNT
    with ANI_RSS_USER_ACTIVITY_LOCK:
        ANI_RSS_USER_ACTIVITY_COUNT += 1
        ANI_RSS_USER_ACTIVITY.set()
    try:
        with ANI_RSS_OPERATION_LOCK:
            yield
    finally:
        with ANI_RSS_USER_ACTIVITY_LOCK:
            ANI_RSS_USER_ACTIVITY_COUNT = max(0, ANI_RSS_USER_ACTIVITY_COUNT - 1)
            if not ANI_RSS_USER_ACTIVITY_COUNT:
                ANI_RSS_USER_ACTIVITY.clear()


# Configuration and credential writes share one transaction gate so concurrent
# administrator requests cannot interleave with a rollback.
SETTINGS_WRITE_LOCK = threading.RLock()


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
            except (urllib.error.HTTPError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = int(exc.code) if isinstance(exc, urllib.error.HTTPError) else int(exc.response.status_code)
                if status not in {429, 500, 502, 503, 504}:
                    raise
                headers = exc.headers if isinstance(exc, urllib.error.HTTPError) else exc.response.headers
                retry_after = headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60.0, 2.0**attempt)
            except (urllib.error.URLError, httpx.RequestError, TimeoutError, json.JSONDecodeError) as exc:
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
    descriptor_urls = list(dict.fromkeys([
        *(network.get("archiveManifestEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("archive_descriptor")),
        LATEST_ARCHIVE_URL,
    ]))
    descriptor, descriptor_endpoint = network_sources.fetch_json(
        descriptor_urls, timeout=float(network.get("probeTimeoutSeconds", 12)),
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
    try:
        _registry_endpoints, registry_payload = network_registry.load()
        default_asset_proxies = registry_payload.get("archiveAssetProxies", [])
    except (OSError, ValueError):
        default_asset_proxies = []
    asset_proxies = list(dict.fromkeys([
        *(network.get("archiveAssetProxyTemplates") or []), *default_asset_proxies,
    ]))
    result = network_downloads.download_verified(
        network_sources.asset_urls(descriptor["browser_download_url"], asset_proxies),
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


def _archive_infobox_chunks(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return {}
    current: str | None = None
    chunks: dict[str, list[str]] = {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^\s*\|\s*([^=]+?)\s*=\s*(.*)$", line)
        if match:
            current = match.group(1).strip()
            chunks.setdefault(current, []).append(match.group(2).strip())
        elif current and line.strip() not in {"}}", "}"}:
            chunks[current].append(line.strip())
    return chunks


def _split_archive_plain_values(text: str) -> list[str]:
    """Split wiki plain-text lists while preserving explicit grouped company/alias notation."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    output: list[str] = []
    current: list[str] = []
    closing = {"〖": "〗", "【": "】", "（": "）", "(": ")"}
    stack: list[str] = []
    for char in text:
        if char in closing:
            stack.append(closing[char])
        elif stack and char == stack[-1]:
            stack.pop()
        if not stack and char in {"\n", "、", ";", "；"}:
            output.append("".join(current))
            current = []
        else:
            current.append(char)
    output.append("".join(current))
    return output


def parse_archive_infobox(raw: str | None) -> dict[str, list[str]]:
    """Parse the common Bangumi wiki-field forms without interpreting markup as HTML."""
    fields: dict[str, list[str]] = {}
    for key, lines in _archive_infobox_chunks(raw).items():
        text = "\n".join(lines).strip().strip("{} ")
        bracketed = re.findall(r"\[\s*(?:[^\]|]+\|)?([^\]]+?)\s*\]", text)
        if bracketed:
            values = bracketed
        else:
            values = _split_archive_plain_values(text)
        cleaned = unique(re.sub(r"\{\{.*?\}\}|\[\[|\]\]", "", value).strip(" {}|") for value in values)
        if cleaned:
            fields[key] = cleaned
    return fields


def parse_archive_alias_entries(raw: str | None) -> list[tuple[str, str | None]]:
    """Return Archive aliases together with an optional wiki label such as 日文版/英文版."""
    entries: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for key, lines in _archive_infobox_chunks(raw).items():
        if key not in {"别名", "別名"}:
            continue
        text = "\n".join(lines).strip().strip("{} ")
        bracketed = re.findall(r"\[\s*([^\]]+?)\s*\]", text)
        if bracketed:
            raw_entries = bracketed
        else:
            raw_entries = _split_archive_plain_values(text)
        for raw_entry in raw_entries:
            parts = [part.strip() for part in str(raw_entry).split("|", 1)]
            label, value = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
            value = re.sub(r"\{\{.*?\}\}|\[\[|\]\]", "", value).strip(" {}|")
            label = re.sub(r"\{\{.*?\}\}|\[\[|\]\]", "", label or "").strip(" {}|") or None
            identity = ((label or "").casefold(), value.casefold())
            if value and identity not in seen:
                seen.add(identity)
                entries.append((value, label))
    return entries


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
    if re.search(r"[\uac00-\ud7af]", title):
        return "ko"
    if re.search(r"[\u0400-\u052f]", title):
        return "ru"
    # Han text must win over a Latin-ratio heuristic.  Otherwise aliases such
    # as "Re：从零开始的异世界生活 Memory Snow" are incorrectly tagged as
    # English merely because the Latin suffix is long.
    if re.search(r"[\u3400-\u9fff]", title):
        return "zh"
    ascii_letters = len(re.findall(r"[A-Za-z]", title))
    visible = len(re.findall(r"\S", title)) or 1
    if ascii_letters / visible >= 0.45:
        return "en"
    return "und"


def alias_language(title: str, label: str | None = None) -> str:
    normalized = unicodedata.normalize("NFKC", str(label or "")).casefold()
    labelled = (
        ("ja", ("日文", "日版", "日本版", "日本語")),
        ("en", ("英文", "英版", "美版", "英语", "英語")),
        ("ko", ("韩文", "韓文", "韩版", "韓版", "한국어")),
        ("zh-Hant", ("繁体", "繁體", "台版", "港版", "台湾", "臺灣")),
        ("zh-Hans", ("简体", "簡體", "中文版", "大陆版", "大陸版")),
    )
    for language, markers in labelled:
        if any(marker.casefold() in normalized for marker in markers):
            return language
    return infer_language(title)


def infer_original_language(title: str, countries: Iterable[tuple[str, str]] = ()) -> str:
    """Infer conservatively: Japanese behavior is the fallback unless non-Japanese evidence is explicit."""
    script = infer_language(title)
    if script in {"ja", "ko", "ru"}:
        return script
    codes = {str(code).upper() for code, _ in countries if str(code).upper() != "OTHER"}
    country_languages = {"CN": "zh", "HK": "zh", "MO": "zh", "TW": "zh", "KR": "ko", "US": "en", "GB": "en", "CA": "en", "FR": "fr", "DE": "de", "RU": "ru"}
    non_japanese = [country_languages[code] for code in country_languages if code in codes]
    if len(set(non_japanese)) == 1 and "JP" not in codes:
        return non_japanese[0]
    if script == "zh" and codes.intersection({"CN", "HK", "MO", "TW"}) and "JP" not in codes:
        return "zh"
    return "ja"


def cast_language(person_name: str, original_language: str) -> str:
    """Keep Japanese catalog semantics unchanged; infer only when a non-Japanese original gives evidence."""
    if original_language == "ja":
        return "ja"
    detected = infer_language(person_name)
    if detected in {"ja", "ko", "ru", "en"}:
        return detected
    if detected == "zh" and original_language == "zh":
        return "zh"
    if detected == "zh" and original_language == "ko":
        return "ja"
    return "und"


def _valid_english_display_title(title: str) -> bool:
    """Reject titles containing scripts that cannot be an English display name.

    Latin-only brands, numerals and punctuation remain valid; non-Latin aliases are
    still retained in ``anime_title`` for search and original-title display.
    """
    value = unicodedata.normalize("NFKC", str(title or "")).strip()
    if not value:
        return False
    # An ``en`` alias can be polluted by far more scripts than the common CJK
    # cases. Accept alphabetic characters only when Unicode classifies them as
    # Latin; digits, punctuation and symbols remain valid (for titles such as 86).
    return all("LATIN" in unicodedata.name(char, "") for char in value if char.isalpha())


def choose_display_english_title(titles: Iterable[dict[str, str] | str]) -> str | None:
    """Choose a readable, script-valid English display title while retaining aliases for search.

    Archive aliases are community-entered and ordered for editing, not display.
    Keep the first valid candidate unless it has strong shorthand/truncation evidence;
    this avoids rewriting intentional lowercase brands merely for typography.
    """
    candidates = unique(
        str(item.get("title", "") if isinstance(item, dict) else item).strip()
        for item in titles
        if (not isinstance(item, dict) or item.get("language") == "en")
        and _valid_english_display_title(str(item.get("title", "") if isinstance(item, dict) else item))
    )
    if not candidates:
        return None
    current = candidates[0]
    non_latin = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u052f]")

    def first_letter(value: str) -> str:
        match = re.search(r"[A-Za-z]", value)
        return match.group(0) if match else ""

    pure_latin_alternatives = [value for value in candidates[1:] if not non_latin.search(value)]
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
        score -= 30 if non_latin.search(value) else 0
        score -= 24 if re.fullmatch(r"[A-Za-z0-9+._-]{1,4}", value) else 0
        score -= 8 if "," in value else 0
        return score, -candidates.index(value)

    return max(replacements, key=quality)


def _refresh_display_english_titles_db(db: sqlite3.Connection) -> dict[str, int]:
    """Fill or improve English display titles from the aliases already stored in the catalog."""
    grouped: dict[int, tuple[str | None, list[str]]] = {}
    for anime_id, current, alias in db.execute(
            "SELECT w.id,w.title_en,t.title FROM anime_work w "
            "LEFT JOIN anime_title t ON t.anime_id=w.id AND t.language='en' "
            "ORDER BY w.id,t.rowid"):
        key = int(anime_id)
        if key not in grouped:
            grouped[key] = (str(current) if current else None, [])
        if alias and str(alias) not in grouped[key][1]:
            grouped[key][1].append(str(alias))
    updates: list[tuple[str | None, int]] = []
    for anime_id, (current, aliases) in grouped.items():
        candidates = ([current] if current else []) + [value for value in aliases if value != current]
        selected = choose_display_english_title(candidates)
        if selected != current:
            updates.append((selected, anime_id))
    db.executemany("UPDATE anime_work SET title_en=? WHERE id=?", updates)
    db.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('english_display_title_policy','quality-v4')"
    )
    return {"examined": len(grouped), "updated": len(updates)}


def refresh_display_english_titles(db_path: Path) -> dict[str, int]:
    """Refresh English display choices, including older rows whose title_en was never populated."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=120)) as db, db:
        return _refresh_display_english_titles_db(db)


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
    key = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u052f]+", "", key)
    return f"name:{key or normalized}", None


def _studio_looks_independent(name: str, direct_names: set[str]) -> bool:
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    compact = re.sub(r"\s+", "", normalized)
    return (
        compact in direct_names
        or any(token in normalized for token in (
            "studio", "スタジオ", "animation", "アニメーション", "动漫", "動漫", "动画", "動畫",
            "制作", "製作", "影业", "影業", "pictures", "entertainment",
        ))
    )


def _studio_alias_group(name: str, direct_names: set[str]) -> tuple[list[str], str | None]:
    """Recognize explicit alias/legal-name groups used by non-Japanese Archive credits."""
    source = unicodedata.normalize("NFKC", str(name)).strip()
    group = re.fullmatch(r"(.+?)\s*[〖【]([^〖〗【】]+)[〗】]", source)
    if group:
        preferred = group.group(1).strip()
        members = unique(re.split(r"\s*(?:、|;|；|，|,)\s*", group.group(2)))
        if _studio_looks_independent(preferred, direct_names) and members:
            return unique([preferred, *members]), preferred
    parenthetical = re.fullmatch(r"(.+?)\s*[（(]([^()（）]+)[）)]", source)
    if parenthetical and _studio_looks_independent(parenthetical.group(2), direct_names):
        outer, inner = parenthetical.group(1).strip(), parenthetical.group(2).strip()
        # A legal/company name followed by a shorter brand is normally an alias declaration.
        if _studio_looks_independent(outer, direct_names):
            preferred = inner if len(inner) <= len(outer) else outer
            return [outer, inner], preferred
    return [], None


def split_studio_credit(name: str, direct_names: set[str] | None = None) -> list[str]:
    """Split collaboration credits without confusing a rename/brand family with a co-producer."""
    source = unicodedata.normalize("NFKC", str(name)).strip()
    direct_names = direct_names or set()
    parts = unique(re.split(r"\s*(?:[/／×＆&]|\s+[xX+]\s+)\s*", source))
    output: list[str] = []
    for part in parts:
        aliases, _ = _studio_alias_group(part, direct_names)
        if aliases:
            output.extend(aliases)
            continue
        match = re.fullmatch(r"(.+?)\s*[（(]([^()（）]+)[）)]", part)
        if match and _studio_looks_independent(match.group(2), direct_names):
            output.extend((match.group(1), match.group(2)))
        else:
            output.append(part)
    return unique(output)


def infer_country_codes(tags: Iterable[str], title: str, studios: Iterable[str], info: dict[str, list[str]] | None = None) -> list[tuple[str, str]]:
    clean_tags = {unicodedata.normalize("NFKC", str(tag)).strip().casefold() for tag in tags}
    result: dict[str, str] = {}
    for code, aliases in COUNTRY_TAGS.items():
        if any(tag == alias.casefold() or tag.startswith(alias.casefold() + "动画") for tag in clean_tags for alias in aliases):
            result[code] = "archive_tag"
    country_fields = {"国家/地区", "國家/地區", "制片国家/地区", "製片國家/地區", "制作国家/地区", "製作國家/地區", "製作国", "制作国", "国家", "國家", "地区", "地區"}
    country_text = " ".join(value for key, values in (info or {}).items() if key in country_fields for value in values).casefold()
    country_markers = {
        "CN": ("中国大陆", "中國大陸", "中国", "中國", "mainland china", "china"),
        "HK": ("香港", "hong kong"), "MO": ("澳门", "澳門", "macao", "macau"),
        "TW": ("台湾", "臺灣", "台灣", "taiwan"),
        "JP": ("日本", "japan"), "KR": ("韩国", "韓國", "한국", "south korea", "korea"),
        "US": ("美国", "美國", "united states", "usa"),
        "GB": ("英国", "英國", "united kingdom", "britain"), "IE": ("爱尔兰", "愛爾蘭", "ireland"),
        "FR": ("法国", "法國", "france"), "DE": ("德国", "德國", "germany"),
        "RU": ("俄罗斯", "俄羅斯", "russia"), "UA": ("乌克兰", "烏克蘭", "ukraine"),
        "IT": ("意大利", "義大利", "italy"), "ES": ("西班牙", "spain"), "PT": ("葡萄牙", "portugal"),
        "NL": ("荷兰", "荷蘭", "netherlands"), "BE": ("比利时", "比利時", "belgium"),
        "CH": ("瑞士", "switzerland"), "AT": ("奥地利", "奧地利", "austria"),
        "PL": ("波兰", "波蘭", "poland"), "CZ": ("捷克", "czechia", "czech republic"),
        "SK": ("斯洛伐克", "slovakia"), "HU": ("匈牙利", "hungary"),
        "RO": ("罗马尼亚", "羅馬尼亞", "romania"), "BG": ("保加利亚", "保加利亞", "bulgaria"),
        "GR": ("希腊", "希臘", "greece"), "TR": ("土耳其", "turkey", "türkiye"),
        "SE": ("瑞典", "sweden"), "NO": ("挪威", "norway"), "DK": ("丹麦", "丹麥", "denmark"),
        "FI": ("芬兰", "芬蘭", "finland"), "IS": ("冰岛", "冰島", "iceland"),
        "EE": ("爱沙尼亚", "愛沙尼亞", "estonia"), "LV": ("拉脱维亚", "拉脫維亞", "latvia"),
        "LT": ("立陶宛", "lithuania"), "BY": ("白俄罗斯", "白俄羅斯", "belarus"),
        "MD": ("摩尔多瓦", "摩爾多瓦", "moldova"), "RS": ("塞尔维亚", "塞爾維亞", "serbia"),
        "HR": ("克罗地亚", "克羅地亞", "croatia"), "SI": ("斯洛文尼亚", "斯洛文尼亞", "slovenia"),
        "BA": ("波斯尼亚和黑塞哥维那", "波斯尼亞和黑塞哥維那", "bosnia and herzegovina"),
        "ME": ("黑山", "montenegro"), "MK": ("北马其顿", "北馬其頓", "north macedonia"),
        "AL": ("阿尔巴尼亚", "阿爾巴尼亞", "albania"), "CY": ("塞浦路斯", "cyprus"),
        "MT": ("马耳他", "馬耳他", "malta"), "LU": ("卢森堡", "盧森堡", "luxembourg"),
        "LI": ("列支敦士登", "liechtenstein"), "MC": ("摩纳哥", "摩納哥", "monaco"),
        "AD": ("安道尔", "安道爾", "andorra"), "SM": ("圣马力诺", "聖馬力諾", "san marino"),
        "VA": ("梵蒂冈", "梵蒂岡", "vatican"),
        "CA": ("加拿大", "canada"),
    }
    for code, markers in country_markers.items():
        if country_text and any(marker in country_text for marker in markers):
            result.setdefault(code, "archive_infobox")
    studio_text = " ".join(studios).casefold()
    studio_country = {
        "US": ("disney", "pixar", "warner bros", "dreamworks", "nickelodeon", "cartoon network", "mgm"),
        "CN": ("上海美术电影制片厂", "玄机", "若鸿", "咏声", "福煦", "艺画开天", "原力动画", "方特动漫", "华强方特"),
        "RU": ("союзмультфильм",),
    }
    for code, aliases in studio_country.items():
        if any(alias in studio_text for alias in aliases):
            result.setdefault(code, "studio")
    if re.search(r"[\u3040-\u30ff]", title):
        result.setdefault("JP", "original_title_script")
    elif re.search(r"[\uac00-\ud7af]", title):
        result.setdefault("KR", "original_title_script")
    elif re.search(r"[\u0400-\u052f]", title):
        result.setdefault("RU", "original_title_script")
    return sorted(result.items()) or [("OTHER", "insufficient_country_evidence")]


def rebuild_studio_clusters(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM anime_studio_cluster")
    raw = list(db.execute("SELECT anime_id,studio FROM anime_studio"))
    direct_names = {
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", component).casefold())
        for _, source in raw
        for component in re.split(r"\s*(?:[/／×＆&]|\s+[xX+]\s+)\s*", str(source))
        if component and not re.search(r"[（(〖【].+[）)〗】]", component)
    }
    language_by_anime: dict[int, str] = {}
    try:
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(anime_work)")}
        if "original_language" in columns:
            language_by_anime = {int(anime_id): str(language or "ja") for anime_id, language in db.execute("SELECT id,original_language FROM anime_work")}
    except sqlite3.Error:
        language_by_anime = {}

    parent: dict[str, str] = {}
    preferred_labels: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    alias_rows: dict[int, list[tuple[list[str], str | None]]] = {}
    for anime_id, source in raw:
        # Dynamic alias inference is deliberately non-Japanese only; legacy Japanese families remain fixed above.
        if language_by_anime and language_by_anime.get(int(anime_id), "ja") == "ja":
            continue
        groups: list[tuple[list[str], str | None]] = []
        for part in unique(re.split(r"\s*(?:[/／×＆&]|\s+[xX+]\s+)\s*", str(source))):
            aliases, preferred = _studio_alias_group(part, direct_names)
            if len(aliases) >= 2:
                keys = [studio_key(alias)[0] for alias in aliases]
                for key in keys[1:]:
                    union(keys[0], key)
                groups.append((aliases, preferred))
        alias_rows[int(anime_id)] = groups

    # Compress and carry any preferred brand label to the final root.
    for groups in alias_rows.values():
        for aliases, preferred in groups:
            if preferred:
                preferred_labels[find(studio_key(aliases[0])[0])] = preferred

    counts: dict[tuple[str, str], int] = {}
    keyed: list[tuple[int, str, str, str | None]] = []
    for anime_id, name in raw:
        for component in split_studio_credit(str(name), direct_names):
            key, fixed = studio_key(component)
            dynamic = find(key) if key in parent else key
            keyed.append((int(anime_id), component, dynamic, fixed))
            counts[(dynamic, component)] = counts.get((dynamic, component), 0) + 1
    labels: dict[str, str] = {}
    for _, name, key, fixed in keyed:
        preferred = preferred_labels.get(find(key) if key in parent else key)
        if fixed:
            labels[key] = fixed
        elif preferred:
            labels[key] = preferred
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
        info_studios = unique(info.get("动画制作", []) + info.get("動畫製作", []))
        countries = infer_country_codes(raw_tags, str(subject.get("name") or ""), info_studios, info)
        manifest_country = str(manifest_item.get("country_code") or "").strip().upper()
        if manifest_country and manifest_country not in {code for code, _ in countries}:
            countries = sorted([*countries, (manifest_country, "manifest")])
        original_language = infer_original_language(str(subject.get("name") or ""), countries)
        manifest_item["_inferred_countries"] = countries

        titles: list[dict[str, str]] = []
        if subject.get("name"):
            titles.append({"language": original_language, "title": subject["name"], "title_type": "primary", "source": "bangumi-archive"})
        if subject.get("name_cn"):
            titles.append({"language": "zh-Hans", "title": subject["name_cn"], "title_type": "primary", "source": "bangumi-archive"})
        alias_entries = parse_archive_alias_entries(subject.get("infobox"))
        has_labelled_english = any(label and alias_language(alias, label) == "en" for alias, label in alias_entries)
        for alias, label in alias_entries:
            language = alias_language(alias, label)
            if (original_language != "ja" and not label and has_labelled_english and language == "en"
                    and re.fullmatch(r"[A-Za-z0-9._+\-]+", alias)):
                language = "und"
            titles.append({"language": language, "title": alias, "title_type": "alias", "source": "bangumi-archive"})
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
                    "character_role": char_roles.get(character["id"], "其他"),
                    "language": cast_language(str(person["name"]), original_language), "source": "bangumi-archive"
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
        studios = unique(info_studios + archive_studios)
        countries = infer_country_codes(raw_tags, str(subject.get("name") or ""), studios, info)
        if manifest_country and manifest_country not in {code for code, _ in countries}:
            countries = sorted([*countries, (manifest_country, "manifest")])
        final_original_language = infer_original_language(str(subject.get("name") or ""), countries)
        if final_original_language != original_language:
            original_language = final_original_language
            for title in dedup_titles:
                if (title.get("source") == "bangumi-archive" and title.get("title_type") == "primary"
                        and title.get("title") == subject.get("name")):
                    title["language"] = original_language
            for credit in cast:
                credit["language"] = cast_language(str(credit.get("person_name") or ""), original_language)
        manifest_item["_inferred_countries"] = countries
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
            "original_language": original_language,
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
            uses_wikidata = any(
                title.get("source") == "wikidata" for row in rows for title in row.titles
            )
            metadata_rows = [
                ("schema_version", "1"), ("built_at", now),
                ("record_count", str(len(rows))),
                ("sources", "Bangumi Archive" + ("; Wikidata labels/aliases" if uses_wikidata else "")),
                ("license_notice", "Bangumi entries: CC BY-SA 3.0" + ("; Wikidata: CC0" if uses_wikidata else "")),
                ("build_state", "enriching"),
                ("feature_schema_version", "14"),
                ("english_display_title_policy", "quality-v4")
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
                country_rows = row.manifest.get("_inferred_countries") or infer_country_codes(
                    [x["name"] for x in row.tags], row.work["title_ja"], (row.work.get("studio") or "").split(" / "))
                db.executemany("INSERT OR IGNORE INTO anime_country VALUES(?,?,?)",
                               [(anime_id, code, evidence) for code, evidence in country_rows])
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


def localized_archive_title(db: sqlite3.Connection, anime_id: int, language: str,
                            original_title: str = "") -> str | None:
    """Return the best stored localized title without changing the Archive primary title."""
    rows = db.execute(
        """SELECT title FROM anime_title
           WHERE anime_id=? AND language=? AND trim(title)<>'' AND title<>?
           ORDER BY CASE source WHEN 'bangumi-archive' THEN 0 ELSE 1 END,
                    CASE title_type WHEN 'primary' THEN 0 WHEN 'label' THEN 1 ELSE 2 END,
                    rowid""",
        (anime_id, language, original_title),
    ).fetchall()
    if language == "en":
        return choose_display_english_title(str(row[0]) for row in rows if row and row[0])
    return str(rows[0][0]) if rows and rows[0][0] else None


def localized_display_title(db: sqlite3.Connection, anime_id: int, language: str,
                            original_title: str, original_language: str = "ja",
                            stored_zh_hans: str | None = None, stored_en: str | None = None) -> str:
    """Return the best title for the current UI language without inventing translations."""
    original = str(original_title or "").strip()
    ui_language = str(language or "en")
    ui_base = ui_language.split("-", 1)[0].lower()
    original_base = str(original_language or "ja").split("-", 1)[0].lower()
    if ui_language == "zh-Hans":
        translated = str(stored_zh_hans or "").strip() or localized_archive_title(
            db, anime_id, "zh-Hans", original
        )
        # ``original_language`` is currently coarse-grained to ``zh`` for both
        # simplified and traditional Chinese works. Prefer an explicit
        # Simplified-Chinese title when one exists instead of treating every
        # Chinese-script primary title as already matching zh-Hans.
        return translated or original
    if ui_base == "en":
        if original_base == "en":
            return original
        translated = choose_display_english_title([str(stored_en or "")]) or localized_archive_title(
            db, anime_id, "en", original
        )
        # Missing translations should not leak into ``title_en`` or be invented.
        # For display only, fall back to the Archive primary/original title so the
        # English UI remains usable even when upstream metadata has no English name.
        return translated or original
    if ui_base == "ja":
        if original_base == "ja":
            return original
        return localized_archive_title(db, anime_id, "ja", original) or original
    if ui_base == original_base:
        return original
    return original


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
        tags_by_work: dict[int, list[str]] = {}
        studios_by_work: dict[int, list[str]] = {}
        for anime_id, tag in db.execute("SELECT anime_id,tag FROM anime_tag"):
            tags_by_work.setdefault(int(anime_id), []).append(str(tag))
        for anime_id, studio in db.execute("SELECT anime_id,studio FROM anime_studio"):
            studios_by_work.setdefault(int(anime_id), []).append(str(studio))
        countries = [(int(anime_id), code, evidence) for anime_id, title in db.execute("SELECT id,title_ja FROM anime_work")
                     for code, evidence in infer_country_codes(tags_by_work.get(int(anime_id), []), str(title), studios_by_work.get(int(anime_id), []))]
        db.executemany("INSERT OR IGNORE INTO anime_country VALUES(?,?,?)", countries)
        countries_by_work: dict[int, list[tuple[str, str]]] = {}
        for anime_id, code, evidence in db.execute("SELECT anime_id,country_code,evidence FROM anime_country"):
            countries_by_work.setdefault(int(anime_id), []).append((str(code), str(evidence)))
        changed_languages: list[tuple[str, int]] = []
        for anime_id, title, current_language in db.execute("SELECT id,title_ja,original_language FROM anime_work"):
            inferred = infer_original_language(str(title), countries_by_work.get(int(anime_id), []))
            if str(current_language or "ja") == "ja" and inferred != "ja":
                changed_languages.append((inferred, int(anime_id)))
        db.executemany("UPDATE anime_work SET original_language=? WHERE id=?", changed_languages)
        # Older catalogs marked every Archive voice actor as Japanese. Re-run the same conservative
        # script inference used by fresh builds so an upgraded non-Japanese work does not relabel an
        # existing Japanese dub as the work's original language through the UI's unknown fallback.
        language_by_changed_id = {anime_id: language for language, anime_id in changed_languages}
        cast_updates: list[tuple[str, int, int]] = []
        for rowid, anime_id, person_name in db.execute(
                "SELECT rowid,anime_id,person_name FROM anime_cast WHERE language='ja'"):
            original_language = language_by_changed_id.get(int(anime_id))
            if original_language:
                cast_updates.append((cast_language(str(person_name), original_language), int(rowid), int(anime_id)))
        db.executemany("UPDATE anime_cast SET language=? WHERE rowid=? AND anime_id=?", cast_updates)
        _refresh_display_english_titles_db(db)
        rebuild_studio_clusters(db)
        relation_graph.rebuild(db)
        rebuild_physical_layout(db)
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('feature_schema_version','14')")


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
        title_policy = db.execute("SELECT value FROM metadata WHERE key='english_display_title_policy'").fetchone()
        relation_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_relation_edge'").fetchone()
        return ("media_code" in columns and "physical_role" in columns
                and bool(version and version[0] == "14")
                and bool(title_policy and title_policy[0] == "quality-v4")
                and bool(relation_table))

    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        if ready(db):
            return
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db:
        if not ready(db):
            migrate_catalog_features(db)



def localized_watches(db_path: Path, language: str) -> list[dict[str, Any]]:
    """Return runtime watches with canonical work titles localized for the active UI."""
    items = runtime_catalog.watches(db_path)
    if not items:
        return items
    with contextlib.closing(sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
        for item in items:
            row = db.execute(
                "SELECT title_ja,title_zh_hans,title_en,original_language FROM anime_work WHERE id=?",
                (int(item["animeId"]),),
            ).fetchone()
            if row:
                item["title"] = localized_display_title(
                    db, int(item["animeId"]), language, str(row[0] or ""),
                    str(row[3] or "ja"), row[1], row[2],
                )
    return items

def query_catalog(db_path: Path, params: dict[str, list[str]], config: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_catalog_features(db_path)
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as feature_db:
        has_ani_rss_media_table = bool(feature_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ani_rss_media'").fetchone())
    config = config or ConfigStore(DEFAULT_CONFIG, EXAMPLE_CONFIG).read()
    config = {**config, "ui": {**config.get("ui", {}), "language": (params.get("language") or [config.get("ui", {}).get("language", "en")])[0]}}
    ani_state = ani_rss.state(db_path, config)
    ani_connection_ready = ani_rss.state_available(ani_state)
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
    raw_regions = policy.get("regions") if isinstance(policy.get("regions"), dict) else {}
    region_states = {key: bool(raw_regions.get(key, True)) for key in REGION_KEYS}
    if all(region_states.values()):
        region_sql = "1"
    elif not any(region_states.values()):
        region_sql = "0"
    else:
        all_known = sorted(set().union(*(REGION_COUNTRIES.values())))
        enabled_known = sorted(set().union(*(
            REGION_COUNTRIES[key] for key in REGION_KEYS if key != "other" and region_states[key]
        ))) if any(region_states[key] for key in REGION_KEYS if key != "other") else []
        region_clauses: list[str] = []
        if enabled_known:
            literals = ",".join("'" + code + "'" for code in enabled_known)
            region_clauses.append(f"EXISTS(SELECT 1 FROM anime_country arc WHERE arc.anime_id=w.id AND arc.country_code IN ({literals}))")
        if region_states["other"]:
            known_literals = ",".join("'" + code + "'" for code in all_known)
            region_clauses.append(
                "(NOT EXISTS(SELECT 1 FROM anime_country aro WHERE aro.anime_id=w.id) OR "
                f"EXISTS(SELECT 1 FROM anime_country aro WHERE aro.anime_id=w.id AND aro.country_code NOT IN ({known_literals})))"
            )
        region_sql = "(" + " OR ".join(region_clauses) + ")" if region_clauses else "0"
    # Region permission is a Catalog-level visibility policy, not a way to
    # recategorize disabled works as “No available source”. A disabled region
    # therefore removes its cards from normal catalog queries entirely.
    if region_sql != "1":
        where.append(f"({region_sql})")
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
    eligibility_sql += f" AND ({region_sql}))"
    ani_rss_sql = (
        f"(({region_sql}) AND (EXISTS(SELECT 1 FROM ani_rss_resource ar WHERE ar.anime_id=w.id AND ar.eligible=1 AND julianday(ar.expires_at)>=julianday('now')) OR EXISTS(SELECT 1 FROM ani_rss_subscription ans WHERE ans.anime_id=w.id AND ans.deleted_at IS NULL)))"
        if ani_connection_ready else "0"
    )
    if availability == {"__none__"}:
        where.append("0")
    elif availability:
        source_clauses: list[str] = []
        if "torrent" in availability:
            source_clauses.append(eligibility_sql)
        if "ani-rss" in availability:
            source_clauses.append(ani_rss_sql)
        if "unavailable" in availability:
            source_clauses.append(f"(NOT {eligibility_sql} AND NOT {ani_rss_sql})")
        if source_clauses:
            where.append("(" + " OR ".join(source_clauses) + ")")
            # Torrent eligibility may appear once directly and once inside the
            # no-source branch; duplicate its policy parameters accordingly.
            eligibility_uses = int("torrent" in availability) + int("unavailable" in availability)
            values.extend(eligibility_values * eligibility_uses)
        else:
            where.append("0")
    raw_library_states = set(selected_values("library_state"))
    legacy_library_map = {
        "existing": "local", "external": "external", "queued": "submitted",
        "downloading": "submitted", "placeholder": "not_in_library",
        "occupied_review": "not_in_library", "absent": "not_in_library",
        "not_in_library_catalog": "not_in_library",
    }
    library_states = {legacy_library_map.get(item, item) for item in raw_library_states}
    if library_states == {"__none__"}:
        where.append("0")
    elif library_states:
        owner_expr = "COALESCE(w.physical_owner_anime_id,w.id)"
        local_sql = f"EXISTS(SELECT 1 FROM runtime_work lrw WHERE lrw.anime_id={owner_expr} AND lrw.library_state='existing')"
        ani_media_sql = (f"EXISTS(SELECT 1 FROM ani_rss_media lam JOIN ani_rss_subscription las ON las.remote_id=lam.remote_id WHERE lam.anime_id={owner_expr} AND las.deleted_at IS NULL)"
                         if has_ani_rss_media_table and ani_connection_ready else "0")
        external_sql = f"(EXISTS(SELECT 1 FROM external_media_file lem WHERE lem.anime_id={owner_expr} AND lem.match_state='verified') OR {ani_media_sql})"
        submitted_sql = f"EXISTS(SELECT 1 FROM runtime_work srw WHERE srw.anime_id={owner_expr} AND srw.library_state IN ('queued','downloading'))"
        clauses: list[str] = []
        if "local" in library_states:
            clauses.append(local_sql)
        if "external" in library_states:
            clauses.append(f"(NOT {local_sql} AND {external_sql})")
        if "submitted" in library_states:
            clauses.append(f"(NOT {local_sql} AND NOT {external_sql} AND {submitted_sql})")
        if "not_in_library" in library_states:
            clauses.append(f"(NOT {local_sql} AND NOT {external_sql} AND NOT {submitted_sql})")
        where.append("(" + " OR ".join(clauses) + ")" if clauses else "0")

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
        ani_rss_media_counts: dict[int, int] = {}
        ani_rss_counts: dict[int, int] = {}
        ani_rss_managed: set[int] = set()
        if has_runtime and ids:
            effective_ids = sorted(set(physical_ids.values()))
            marks = ",".join("?" for _ in effective_ids)
            torrent_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(DISTINCT info_hash) FROM runtime_torrent_work WHERE anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            external_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(*) FROM external_media_file WHERE match_state='verified' AND anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            if has_ani_rss_media_table and ani_connection_ready:
                ani_rss_media_counts = {int(a): int(n) for a, n in db.execute(
                    f"""SELECT arm.anime_id,COUNT(*) FROM ani_rss_media arm
                    JOIN ani_rss_subscription ars ON ars.remote_id=arm.remote_id
                    WHERE ars.deleted_at IS NULL AND arm.anime_id IN ({marks})
                    GROUP BY arm.anime_id""", effective_ids)}
            ani_rss_counts = {int(a): int(n) for a, n in db.execute(
                f"SELECT anime_id,COUNT(*) FROM ani_rss_resource WHERE eligible=1 AND julianday(expires_at)>=julianday('now') AND anime_id IN ({marks}) GROUP BY anime_id", effective_ids)}
            ani_rss_managed = {int(row[0]) for row in db.execute(
                f"SELECT DISTINCT anime_id FROM ani_rss_subscription WHERE deleted_at IS NULL AND anime_id IN ({marks})", effective_ids)}
            for anime_id in ids:
                usable_counts[anime_id] = sum(1 for item in runtime_catalog.torrents_for_anime(db, anime_id, config) if item["eligible"])
        for row in rows:
            anime_id = row["id"]
            original_title = str(row.get("title_ja") or "")
            row["title_ja_localized"] = localized_archive_title(db, int(anime_id), "ja", original_title)
            row["title_zh_hans_localized"] = localized_archive_title(db, int(anime_id), "zh-Hans", original_title)
            row["title_en_localized"] = localized_archive_title(db, int(anime_id), "en", original_title)
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
            row["ani_rss_media_count"] = ani_rss_media_counts.get(owner_id, 0)
            row["has_external_media"] = bool(
                external_counts.get(owner_id, 0) or ani_rss_media_counts.get(owner_id, 0))
            region_enabled = region_policy_enabled(policy, row["countries"])
            row["ani_rss_resource_count"] = (
                ani_rss_counts.get(owner_id, 0) if region_enabled and ani_connection_ready else 0)
            row["ani_rss_managed"] = (
                region_enabled and ani_connection_ready and owner_id in ani_rss_managed)
            ani_search_available = (region_enabled and ani_connection_ready
                                    and ani_state.get("effective_mode") in {"prefer", "fallback"})
            row["ani_rss_search_available"] = ani_search_available
            row["ani_rss_auto_available"] = (ani_search_available
                                               and ani_rss.automatic_search_eligible(str(row.get("start_month") or "")))
            library = runtime_catalog.library_status(db, anime_id, include_ani_rss=ani_connection_ready) if has_runtime else None
            row["library_state"] = runtime_catalog.collection_state(db, anime_id, include_ani_rss=ani_connection_ready) if has_runtime else "not_in_library"
            row["library_internal_state"] = library["state"] if library else "not_in_library_catalog"
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
        ani_state = ani_rss.state(db_path, config)
        ani_connection_ready = ani_rss.state_available(ani_state)
        for node in payload["nodes"]:
            node_id = int(node["id"])
            owner_id = runtime_catalog.physical_anime_id(db, node_id)
            candidates = runtime_catalog.torrents_for_anime(db, node_id, config)
            library = runtime_catalog.library_status(db, node_id, include_ani_rss=ani_connection_ready)
            eligible = [item for item in candidates if item["eligible"]]
            countries = [row[0] for row in db.execute("SELECT country_code FROM anime_country WHERE anime_id=? ORDER BY country_code", (node_id,))]
            region_enabled = region_policy_enabled(config.get("torrentPolicy", {}), countries)
            remote_resources = ani_rss.resources(db_path, owner_id, config) if region_enabled and ani_connection_ready else []
            remote_eligible = [item for item in remote_resources if item["eligible"]]
            start_row = db.execute("SELECT start_month FROM anime_work WHERE id=?", (node_id,)).fetchone()
            ani_search_available = (region_enabled and ani_connection_ready
                                    and ani_state.get("effective_mode") in {"prefer", "fallback"})
            ani_auto = (ani_search_available and bool(start_row)
                        and ani_rss.automatic_search_eligible(str(start_row[0] or "")))
            blocked_states = {
                "queued", "downloading", "occupied_review", "deprecated",
                "upgrade_staged", "upgrade_blocked",
            }
            node.update({
                "title_ja_localized": localized_archive_title(
                    db, int(node["id"]), "ja", str(node.get("title_ja") or "")
                ),
                "title_zh_hans_localized": localized_archive_title(
                    db, int(node["id"]), "zh-Hans", str(node.get("title_ja") or "")
                ),
                "title_en_localized": localized_archive_title(
                    db, int(node["id"]), "en", str(node.get("title_ja") or "")
                ),
                "torrent_count": len(candidates),
                "usable_torrent_count": len(eligible),
                "library_state": runtime_catalog.collection_state(db, node_id, include_ani_rss=ani_connection_ready),
                "library_internal_state": library["state"],
                "library_managed": bool(library["managed"]),
                "ani_rss_resource_count": len(remote_eligible),
                "ani_rss_search_available": ani_search_available,
                "ani_rss_auto_available": ani_auto,
                "selectable": bool(eligible or remote_eligible or ani_search_available) and library["state"] not in blocked_states,
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
        strict_nodes = [node for node in payload["nodes"] if node.get("strict_member")]
        if strict_nodes:
            series_root = library_layout.choose_franchise_root(strict_nodes)
            payload["seriesTitle"] = library_layout.strip_season_suffix(localized_display_title(
                db, int(series_root["id"]), str(config.get("ui", {}).get("language") or "en"),
                str(series_root.get("title_ja") or ""), str(series_root.get("original_language") or "ja"),
                series_root.get("title_zh_hans"), series_root.get("title_en"),
            ))
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
        return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u052f]+", "", value)

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
        original_title = str(result.get("title_ja") or "")
        result["title_ja_localized"] = localized_archive_title(db, int(anime_id), "ja", original_title)
        result["title_zh_hans_localized"] = localized_archive_title(db, int(anime_id), "zh-Hans", original_title)
        result["title_en_localized"] = localized_archive_title(db, int(anime_id), "en", original_title)
        result["titles"] = dict_rows(db.execute("SELECT language,title,title_type,source FROM anime_title WHERE anime_id=? ORDER BY language,title_type,title", (anime_id,)))
        result["staff"] = dict_rows(db.execute("SELECT name,role,role_type,source FROM anime_staff WHERE anime_id=? ORDER BY role_type,role,name", (anime_id,)))
        result["cast"] = dict_rows(db.execute("SELECT character_name,person_name,character_role,CASE WHEN language IS NULL OR language IN ('','und') THEN original_language ELSE language END language,source FROM anime_cast JOIN anime_work ON anime_work.id=anime_cast.anime_id WHERE anime_cast.anime_id=? ORDER BY CASE character_role WHEN '主角' THEN 0 WHEN '配角' THEN 1 ELSE 2 END, character_name LIMIT 30", (anime_id,)))
        result["relations"] = dict_rows(db.execute("""SELECT ar.related_bgm_id,ar.related_title,ar.relation_type,
            ar.relation_code,ar.strict_group,ar.source,ar.related_subject_type,ar.related_subject_kind,
            ar.related_subject_meta_json,
            related.id AS related_anime_id,related.title_ja AS related_title_ja,
            related.title_zh_hans AS related_title_zh_hans,related.title_en AS related_title_en,
            related.original_language AS related_original_language
            FROM anime_relation ar LEFT JOIN anime_work related ON related.bgm_id=ar.related_bgm_id
            WHERE ar.anime_id=? AND ar.relation_code<>'other'
            ORDER BY ar.relation_type,ar.related_title""", (anime_id,)))
        ui_language = str(config.get("ui", {}).get("language") or "en")
        for relation in result["relations"]:
            related_anime_id = relation.get("related_anime_id")
            relation["related_display_title"] = (
                localized_display_title(
                    db, int(related_anime_id), ui_language,
                    str(relation.get("related_title_ja") or relation.get("related_title") or ""),
                    str(relation.get("related_original_language") or "ja"),
                    relation.get("related_title_zh_hans"), relation.get("related_title_en"),
                )
                if related_anime_id is not None
                else str(relation.get("related_title") or "")
            )
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
        ani_state = ani_rss.state(db_path, config)
        ani_connection_ready = ani_rss.state_available(ani_state)
        result["library"] = runtime_catalog.library_status(
            db, anime_id, include_ani_rss=ani_connection_ready
        ) if has_runtime else {"state": "not_in_library_catalog", "managed": False, "inspectionMode": "none", "targets": []}
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
            "state": ani_state,
            "subscriptions": ani_rss.subscriptions_for_anime(db_path, anime_id) if ani_connection_ready else [],
            "resources": ani_rss.resources(db_path, anime_id, config) if ani_connection_ready else [],
        }
        return result


def _image_air_date(raw_date: str | None, start_month: str | None) -> dt.date | None:
    raw = str(raw_date or "").strip()
    for pattern in (r"^(\d{4})-(\d{1,2})-(\d{1,2})", r"^(\d{4})/(\d{1,2})/(\d{1,2})"):
        match = re.match(pattern, raw)
        if match:
            with contextlib.suppress(ValueError):
                return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    month = str(start_month or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})$", month)
    if match:
        with contextlib.suppress(ValueError):
            year, month_value = int(match.group(1)), int(match.group(2))
            return dt.date(year, month_value, calendar.monthrange(year, month_value)[1])
    return None


def _add_calendar_months(value: dt.date, months: int) -> dt.date:
    index = value.year * 12 + value.month - 1 + int(months)
    year, month = divmod(index, 12)
    month += 1
    return dt.date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _image_refresh_due_values(raw_date: str | None, start_month: str | None, fetched_at: str | None,
                              *, now: dt.datetime | None = None) -> bool:
    if not fetched_at:
        return False
    air_date = _image_air_date(raw_date, start_month)
    if air_date is None:
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current_date = current.date()
    if _add_calendar_months(air_date, -6) <= current_date < _add_calendar_months(air_date, -2):
        interval_seconds = 72 * 3600
    elif _add_calendar_months(air_date, -2) <= current_date < _add_calendar_months(air_date, 1):
        interval_seconds = 24 * 3600
    elif _add_calendar_months(air_date, 1) <= current_date < _add_calendar_months(air_date, 2):
        interval_seconds = 72 * 3600
    else:
        return False
    try:
        fetched = dt.datetime.fromisoformat(str(fetched_at))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return True
    fetched = fetched.astimezone(dt.timezone.utc)
    if fetched > current.astimezone(dt.timezone.utc) + dt.timedelta(minutes=1):
        return True
    return (current - fetched).total_seconds() >= interval_seconds


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
            error = str(row["error"])
            if error == "no_cover":
                with contextlib.suppress(ValueError):
                    fetched = dt.datetime.fromisoformat(str(row["fetched_at"]))
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=dt.timezone.utc)
                    now = dt.datetime.now(dt.timezone.utc)
                    fetched = fetched.astimezone(dt.timezone.utc)
                    if fetched <= now + dt.timedelta(minutes=1) and (now - fetched).total_seconds() < 86400:
                        return None, "no_cover"
            else:
                return None, "transient_error"
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


def image_network_config(config: dict[str, Any]) -> dict[str, Any]:
    """Build the process-safe image worker configuration from one settings snapshot."""
    network = dict(config.get("metadata", {}).get("network", {}) or {})
    ani_settings = dict(config.get("components", {}).get("aniRss", {}) or {})
    if ani_settings:
        network["_aniRssConfig"] = {"components": {"aniRss": ani_settings}}
        # The image worker uses multiprocessing ``spawn``. Pass the current
        # effective credential with each task so Web-saved credential changes
        # are visible without restarting the worker process. This field never
        # leaves the in-process worker queue or enters public configuration.
        network["_aniRssApiKey"] = ani_rss._secret()
    return network


def get_anime_image(db_path: Path, anime_id: int, *, refresh: bool = False,
                    network: dict[str, Any] | None = None,
                    log_timing: bool = True) -> tuple[bytes, str] | None:
    """Fetch one official cover and atomically persist it; intended for ImageFetcher only."""
    cached, cache_state = get_cached_anime_image(db_path, anime_id)
    if cache_state == "not_found":
        return None
    if cached is not None and not refresh:
        return cached
    if cache_state == "no_cover" and not refresh:
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
    started = time.monotonic()
    # A healthy Ani-RSS already owns a local copy of many subscription covers.
    # Reuse that cache first; existing AnimeMachine covers reach this branch only
    # on an explicit refresh, so background work never replaces a good local image.
    try:
        ani_config = network_config.get("_aniRssConfig")
        credential_is_explicit = "_aniRssApiKey" in network_config
        ani_key = str(network_config.get("_aniRssApiKey") or "").strip()
        remote_cover = None
        if ani_key or not credential_is_explicit:
            remote_cover = ani_rss.cached_cover(
                db_path, anime_id, ani_config if isinstance(ani_config, dict) else None,
                api_key=ani_key if credential_is_explicit else None)
        if remote_cover is not None:
            remote_data, remote_mime, remote_source = remote_cover
            data, mime = network_validators.image_bytes(remote_data, remote_mime)
            fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
            with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
                db.execute("PRAGMA busy_timeout=30000")
                if cached is not None and data == cached[0] and mime == cached[1]:
                    db.execute("UPDATE anime_image SET source_url=?,fetched_at=?,error=NULL WHERE anime_id=?",
                               (remote_source, fetched_at, anime_id))
                else:
                    db.execute("""INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error)
                        VALUES(?,?,?,?,?,NULL) ON CONFLICT(anime_id) DO UPDATE SET
                        mime_type=excluded.mime_type,image_blob=excluded.image_blob,source_url=excluded.source_url,
                        fetched_at=excluded.fetched_at,error=NULL""",
                               (anime_id, mime, data, remote_source, fetched_at))
            if log_timing:
                print(f"[timing] image.fetch={time.monotonic() - started:.3f}s source=ani-rss", flush=True)
            return data, mime
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Ani-RSS is an optimization, never a dependency for cover loading.
        pass
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
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            db.execute("PRAGMA busy_timeout=30000")
            if cached is not None and data == cached[0] and mime == cached[1]:
                db.execute(
                    "UPDATE anime_image SET source_url=?,fetched_at=?,error=NULL WHERE anime_id=?",
                    (final_url, fetched_at, anime_id),
                )
            else:
                db.execute("INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at,error) VALUES(?,?,?,?,?,NULL) ON CONFLICT(anime_id) DO UPDATE SET mime_type=excluded.mime_type,image_blob=excluded.image_blob,source_url=excluded.source_url,fetched_at=excluded.fetched_at,error=NULL",
                           (anime_id, mime, data, final_url, fetched_at))
        if log_timing:
            print(f"[timing] image.fetch={time.monotonic() - started:.3f}s source={urllib.parse.urlparse(final_url).netloc}", flush=True)
        return data, mime
    except Exception as exc:
        error = "no_cover" if isinstance(exc, LookupError) else f"{type(exc).__name__}: {exc}"
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            db.execute("PRAGMA busy_timeout=30000")
            if cached is not None:
                # Keep the last successful fetch timestamp. The cached cover is
                # still usable, but a failed refresh must remain due so the
                # low-priority maintenance pass can retry after connectivity
                # recovers instead of suppressing checks for another 24/72 h.
                db.execute(
                    "UPDATE anime_image SET error=? WHERE anime_id=?",
                    (f"refresh_failed:{error}", anime_id),
                )
            else:
                db.execute("INSERT INTO anime_image(anime_id,fetched_at,error) VALUES(?,?,?) ON CONFLICT(anime_id) DO UPDATE SET fetched_at=excluded.fetched_at,error=excluded.error",
                           (anime_id, checked_at, error))
        log_event("INFO" if error == "no_cover" else "ERROR", "cover_fetch_failed",
                  animeId=anime_id, bgmId=bgm_id, error=error)
        if cached is not None:
            return cached
        return placeholder_image()


def placeholder_image() -> tuple[bytes, str]:
    return (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420"><rect width="300" height="420" fill="#d9e4df"/><path d="M95 195h110v30H95z" fill="#8aa39a"/></svg>', "image/svg+xml")


class BackgroundTaskBudget:
    """Coordinate non-interactive background modules against one adaptive slot budget."""
    def __init__(self, image_fetcher: ImageFetcher | None, interactive: Callable[[], bool]) -> None:
        self.image_fetcher = image_fetcher
        self.interactive = interactive
        self.lock = threading.Condition(threading.RLock())
        self.active: dict[str, int] = {}

    def _capacity(self) -> int:
        if network_connectivity.is_offline() or self.interactive():
            return 0
        if self.image_fetcher is None:
            return 1
        snapshot = self.image_fetcher.snapshot()
        budget = dict(snapshot.get("budget") or {})
        raw = budget.get("adaptiveCapacity", snapshot.get("backgroundConcurrency", 1))
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 1

    def _external_capacity(self, capacity: int) -> int:
        if capacity <= 0:
            return 0
        return max(1, min(3, max(1, capacity // 3)))

    def _reserve_images(self) -> None:
        if self.image_fetcher is None:
            return
        setter = getattr(self.image_fetcher, "set_background_reserve", None)
        if callable(setter):
            setter(sum(self.active.values()))

    @contextlib.contextmanager
    def lease(self, category: str, *, stop_event: threading.Event | None = None):
        acquired = False
        while not acquired:
            if stop_event is not None and stop_event.is_set():
                yield False
                return
            with self.lock:
                capacity = self._capacity()
                external_capacity = self._external_capacity(capacity)
                if sum(self.active.values()) < external_capacity:
                    self.active[category] = self.active.get(category, 0) + 1
                    self._reserve_images()
                    acquired = True
                    break
            if stop_event is not None:
                if stop_event.wait(.15):
                    yield False
                    return
            else:
                time.sleep(.15)
        try:
            yield True
        finally:
            if acquired:
                with self.lock:
                    remaining = max(0, self.active.get(category, 0) - 1)
                    if remaining:
                        self.active[category] = remaining
                    else:
                        self.active.pop(category, None)
                    self._reserve_images()
                    self.lock.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            capacity = self._capacity()
            return {
                "adaptiveCapacity": capacity,
                "externalCapacity": self._external_capacity(capacity),
                "leased": sum(self.active.values()),
                "active": dict(self.active),
                "interactive": bool(self.interactive()),
            }


_ACTIVE_BACKGROUND_BUDGET_LOCK = threading.RLock()
_ACTIVE_BACKGROUND_BUDGET: BackgroundTaskBudget | None = None


def set_active_background_budget(value: BackgroundTaskBudget | None) -> None:
    global _ACTIVE_BACKGROUND_BUDGET
    with _ACTIVE_BACKGROUND_BUDGET_LOCK:
        _ACTIVE_BACKGROUND_BUDGET = value


@contextlib.contextmanager
def background_task_lease(category: str, *, stop_event: threading.Event | None = None):
    with _ACTIVE_BACKGROUND_BUDGET_LOCK:
        budget = _ACTIVE_BACKGROUND_BUDGET
    if budget is None:
        yield True
        return
    with budget.lease(category, stop_event=stop_event) as allowed:
        yield allowed


class PerformanceBaseline:
    """Record one-shot startup and first-use timings, retaining recent runs for comparison."""
    _EVENT_KEYS = {
        "catalogReady": "catalogReadyMs",
        "firstScreen": "firstScreenMs",
        "firstCover": "firstCoverMs",
        "warmComplete": "warmCompleteMs",
    }

    def __init__(self, started_monotonic: float | None = None, path: Path | None = None) -> None:
        self.started = float(started_monotonic if started_monotonic is not None else time.monotonic())
        self.path = path
        self.lock = threading.RLock()
        self.values: dict[str, int] = {}
        self.started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self.previous = self._load_previous()
        self._persist()

    def _load_previous(self) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            runs = payload.get("runs", []) if isinstance(payload, dict) else []
            return [dict(run) for run in runs[-9:] if isinstance(run, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def _current(self) -> dict[str, Any]:
        return {
            "startedAt": self.started_at,
            "version": __version__,
            **{key: self.values.get(key) for key in self._EVENT_KEYS.values()},
        }

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"schemaVersion": 1, "runs": [*self.previous, self._current()]},
                           ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            pass

    def mark(self, event: str) -> bool:
        key = self._EVENT_KEYS.get(str(event))
        if key is None:
            return False
        with self.lock:
            if key in self.values:
                return False
            self.values[key] = max(0, int(round((time.monotonic() - self.started) * 1000)))
            self._persist()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = self._current()
            current["previous"] = dict(self.previous[-1]) if self.previous else None
            return current


class CatalogWarmup:
    """Prefetch recent covers first, then continue through the complete catalog."""
    def __init__(self, db_path: Path, config_store: ConfigStore, image_fetcher: ImageFetcher | None,
                 interactive: Callable[[], bool], ready_event: threading.Event | None = None,
                 started_callback: Callable[[dict[str, Any]], None] | None = None,
                 background_budget: BackgroundTaskBudget | None = None,
                 performance: PerformanceBaseline | None = None) -> None:
        self.db_path = db_path
        self.config_store = config_store
        self.image_fetcher = image_fetcher
        self.interactive = interactive
        self.ready_event = ready_event
        self.started_callback = started_callback
        self.background_budget = background_budget or BackgroundTaskBudget(image_fetcher, interactive)
        self.performance = performance
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.generation = 0
        self.marker: tuple[str, str, int] | None = None
        self.start_reported = False
        self.state_path = Path(os.getenv("ANM_IMAGE_PRELOAD_STATE", str(db_path.parent / "image-preload-state.json")))
        self.state = self._load_state()
        self.throughput_ewma = 0.0
        self.progress_mark = time.monotonic()
        self.next_image_maintenance = time.monotonic()
        self.watcher = threading.Thread(target=self._watch, daemon=True, name="anm-catalog-warmup-watch")
        self._apply_controls()

    def _default_controls(self) -> dict[str, Any]:
        workers = int(getattr(self.image_fetcher, "workers", 16)) if self.image_fetcher is not None else 16
        return {"paused": False, "concurrency": workers, "bandwidthKiBps": 0}

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("schemaVersion") == 1:
                controls = self._default_controls()
                raw_controls = value.get("controls")
                if isinstance(raw_controls, dict):
                    if isinstance(raw_controls.get("paused"), bool):
                        controls["paused"] = raw_controls["paused"]
                    with contextlib.suppress(TypeError, ValueError):
                        controls["concurrency"] = max(
                            1, min(int(controls["concurrency"]), int(raw_controls.get("concurrency")))
                        )
                    with contextlib.suppress(TypeError, ValueError):
                        controls["bandwidthKiBps"] = max(
                            0, min(1024 * 1024, int(raw_controls.get("bandwidthKiBps")))
                        )
                value["controls"] = controls
                value.setdefault("stages", {})
                value.setdefault("current", {})
                value.setdefault("retryPending", 0)
                value.setdefault("remainingErrors", 0)
                value.setdefault("preparedThroughMonth", "")
                return value
        except (OSError, ValueError, TypeError):
            pass
        return {
            "schemaVersion": 1, "state": "idle", "stage": "", "catalogMarker": None,
            "stages": {}, "controls": self._default_controls(), "current": {}, "retryPending": 0,
            "remainingErrors": 0, "preparedThroughMonth": "", "updatedAt": "", "error": "",
        }

    def _persist(self) -> None:
        with self.lock:
            payload = json.dumps(self.state, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError as exc:
            log_event("WARNING", "image_preload_state_failed", errorType=type(exc).__name__)

    def _apply_controls(self) -> None:
        if self.image_fetcher is None:
            return
        setter = getattr(self.image_fetcher, "set_background_limits", None)
        if not callable(setter):
            return
        controls = dict(self.state.get("controls") or {})
        setter(
            paused=bool(controls.get("paused")),
            concurrency=int(controls.get("concurrency") or getattr(self.image_fetcher, "workers", 16)),
            bandwidth_kib=int(controls.get("bandwidthKiBps") or 0),
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            payload = json.loads(json.dumps(self.state, ensure_ascii=False))
        stages = dict(payload.get("stages") or {})
        base_total = sum(int((stages.get(name) or {}).get("total") or 0) for name in ("recent", "history"))
        base_done = sum(int((stages.get(name) or {}).get("done") or 0) for name in ("recent", "history"))
        retry_pending = int(payload.get("retryPending") or 0)
        if payload.get("stage") == "retry":
            retry_pending = int((stages.get("retry") or {}).get("failed") or retry_pending)
        payload["overallTotal"] = base_total
        payload["overallDone"] = min(base_total, base_done) if base_total else base_done
        payload["estimatedRemaining"] = max(0, base_total - base_done) + max(0, retry_pending)
        with self.lock:
            rate = float(self.throughput_ewma)
        payload["throughputItemsPerSecond"] = round(rate, 3) if rate > 0 else None
        if payload.get("state") == "warm":
            payload["estimatedSeconds"] = 0
        elif rate > 0.01 and payload["estimatedRemaining"] > 0:
            payload["estimatedSeconds"] = int(math.ceil(payload["estimatedRemaining"] / rate))
        else:
            payload["estimatedSeconds"] = None
        snapshotter = getattr(self.image_fetcher, "snapshot", None) if self.image_fetcher is not None else None
        payload["fetcher"] = snapshotter() if callable(snapshotter) else {}
        payload["resourceBudget"] = dict(payload["fetcher"].get("budget") or {})
        payload["backgroundBudget"] = self.background_budget.snapshot()
        return payload

    def control(self, *, paused: bool | None = None, concurrency: int | None = None,
                bandwidth_kib: int | None = None) -> dict[str, Any]:
        with self.lock:
            controls = dict(self.state.get("controls") or self._default_controls())
            if paused is not None:
                controls["paused"] = bool(paused)
            if concurrency is not None:
                maximum = int(getattr(self.image_fetcher, "workers", 32)) if self.image_fetcher is not None else 32
                controls["concurrency"] = max(1, min(maximum, int(concurrency)))
            if bandwidth_kib is not None:
                controls["bandwidthKiBps"] = max(0, min(1024 * 1024, int(bandwidth_kib)))
            self.state["controls"] = controls
            if self.state.get("state") in {"warming", "paused"}:
                self.state["state"] = "paused" if controls.get("paused") else "warming"
            self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self._apply_controls()
        self._persist()
        return self.snapshot()

    def start(self) -> None:
        if not self.watcher.is_alive():
            if self.ready_event is None or self.ready_event.is_set():
                if self.performance is not None:
                    self.performance.mark("catalogReady")
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

    def _prepare_state(self, marker: tuple[str, str, int]) -> None:
        encoded = [str(marker[0]), str(marker[1]), int(marker[2])]
        with self.lock:
            if self.state.get("catalogMarker") != encoded:
                controls = dict(self.state.get("controls") or self._default_controls())
                self.state = {
                    "schemaVersion": 1, "state": "idle", "stage": "", "catalogMarker": encoded,
                    "stages": {}, "controls": controls, "current": {}, "retryPending": 0,
                    "remainingErrors": 0, "preparedThroughMonth": "", "updatedAt": "", "error": "",
                }
        self._persist()

    def _maintenance_due_ids(self, *, limit: int = 512) -> list[int]:
        now = dt.datetime.now(dt.timezone.utc)
        month_index = now.year * 12 + now.month - 1
        lower_index = month_index - 2
        upper_index = month_index + 6
        lower_month = f"{lower_index // 12:04d}-{lower_index % 12 + 1:02d}"
        upper_month = f"{upper_index // 12:04d}-{upper_index % 12 + 1:02d}"
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
                rows = db.execute(
                    "SELECT w.id,w.raw_date,w.start_month,i.fetched_at FROM anime_work w "
                    "JOIN anime_image i ON i.anime_id=w.id "
                    "WHERE w.start_month>=? AND w.start_month<=? AND i.fetched_at IS NOT NULL "
                    "AND ((i.image_blob IS NOT NULL AND (COALESCE(i.error,'')='' OR i.error LIKE 'refresh_failed:%')) "
                    "OR (i.image_blob IS NULL AND i.error='no_cover')) "
                    "ORDER BY i.fetched_at ASC,w.id ASC",
                    (lower_month, upper_month),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        maximum = max(1, min(4096, int(limit)))
        return [
            int(anime_id) for anime_id, raw_date, start_month, fetched_at in rows
            if _image_refresh_due_values(raw_date, start_month, fetched_at, now=now)
        ][:maximum]

    def _retry_due_ids(self, *, limit: int = 512) -> list[int]:
        """Return uncached covers that still need a low-priority retry."""
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
                rows = db.execute(
                    "SELECT w.id FROM anime_work w LEFT JOIN anime_image i ON i.anime_id=w.id "
                    "WHERE i.image_blob IS NULL AND (i.anime_id IS NULL OR COALESCE(i.error,'')<>'no_cover') "
                    "ORDER BY CASE WHEN i.anime_id IS NULL OR i.fetched_at IS NULL THEN 0 ELSE 1 END, "
                    "i.fetched_at ASC,w.id ASC LIMIT ?",
                    (max(1, min(4096, int(limit))),),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [int(row[0]) for row in rows]

    def _watch(self) -> None:
        while not self.stop_event.wait(.1):
            if self.ready_event is not None and not self.ready_event.is_set():
                continue
            if self.performance is not None:
                self.performance.mark("catalogReady")
            marker = self._catalog_marker()
            if marker and marker != self.marker:
                self.kick(marker[0], marker=marker)
            now = time.monotonic()
            with self.lock:
                maintenance_ready = self.state.get("state") == "warm"
            if now >= self.next_image_maintenance and maintenance_ready and not network_connectivity.is_offline():
                self.next_image_maintenance = now + 12 * 3600
                retry_ids = self._retry_due_ids()
                if retry_ids:
                    try:
                        network = image_network_config(self.config_store.read())
                        self._enqueue(retry_ids, network, priority="retry", refresh=True)
                    except (OSError, ValueError, RuntimeError, sqlite3.Error):
                        pass
                due_ids = self._maintenance_due_ids()
                if due_ids:
                    try:
                        network = image_network_config(self.config_store.read())
                        self._enqueue(due_ids, network, priority="maintenance", refresh=True)
                    except (OSError, ValueError, RuntimeError, sqlite3.Error):
                        pass

    def kick(self, seed: str | None = None, *, marker: tuple[str, str, int] | None = None) -> None:
        marker = marker or self._catalog_marker()
        if not marker:
            return
        seed = str(seed or marker[0])
        self._prepare_state(marker)
        with self.lock:
            self.marker = (seed, marker[1], marker[2])
            self.generation += 1
            generation = self.generation
            paused = bool(self.state.get("controls", {}).get("paused"))
        self._set_state("paused" if paused else "warming", "recent")
        threading.Thread(target=self._run, args=(seed, generation), daemon=True,
                         name=f"anm-catalog-warmup-{generation}").start()

    def _current(self, generation: int) -> bool:
        with self.lock:
            return not self.stop_event.is_set() and generation == self.generation

    def _set_state(self, state: str, stage: str = "", *, error: str = "") -> None:
        if state == "warm" and self.performance is not None:
            self.performance.mark("warmComplete")
        with self.lock:
            self.state["state"] = state
            self.state["stage"] = stage
            self.state["error"] = error
            self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self._persist()

    def _update_stage(self, name: str, *, done: int | None = None, total: int | None = None,
                      failed: int | None = None) -> None:
        with self.lock:
            stages = self.state.setdefault("stages", {})
            stage = dict(stages.get(name) or {"done": 0, "total": 0, "failed": 0})
            if total is not None:
                stage["total"] = max(0, int(total))
            if done is not None:
                done_value = max(0, int(done))
                stage_total = int(stage.get("total") or 0)
                stage["done"] = min(done_value, stage_total) if stage_total else done_value
            if failed is not None:
                stage["failed"] = max(0, int(failed))
            stages[name] = stage
            self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self._persist()

    def _advance_stage(self, name: str, amount: int, *, failed: int | None = None) -> None:
        amount = max(0, int(amount))
        with self.lock:
            current = dict(self.state.setdefault("stages", {}).get(name) or {})
            done = int(current.get("done") or 0) + amount
            total = int(current.get("total") or 0)
            now = time.monotonic()
            elapsed = max(.001, now - self.progress_mark)
            if amount > 0:
                observed = amount / elapsed
                self.throughput_ewma = observed if self.throughput_ewma <= 0 else .30 * observed + .70 * self.throughput_ewma
            self.progress_mark = now
        self._update_stage(name, done=min(done, total) if total else done, failed=failed)

    def _set_retry_pending(self, count: int) -> None:
        with self.lock:
            self.state["retryPending"] = max(0, int(count))
            self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self._persist()

    def _mark_prepared_through(self, anime_ids: Iterable[int]) -> None:
        values = [int(value) for value in anime_ids]
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        try:
            with contextlib.closing(sqlite3.connect(
                    f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
                row = db.execute(
                    f"SELECT MIN(start_month) FROM anime_work WHERE id IN ({placeholders}) "
                    "AND start_month IS NOT NULL AND start_month<>''", values,
                ).fetchone()
        except sqlite3.Error:
            return
        month = str(row[0] or "") if row else ""
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return
        changed = False
        with self.lock:
            current = str(self.state.get("preparedThroughMonth") or "")
            if not current or month < current:
                self.state["preparedThroughMonth"] = month
                self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
                changed = True
        if changed:
            self._persist()

    def _set_current(self, anime_ids: Iterable[int], stage: str) -> None:
        values = [int(value) for value in anime_ids]
        current: dict[str, Any] = {}
        if values:
            anime_id = values[0]
            title = f"#{anime_id}"
            title_fields: dict[str, Any] = {}
            try:
                with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
                    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(anime_work)")}
                    if {"title_ja", "title_zh_hans", "title_en", "original_language"}.issubset(columns):
                        row = db.execute(
                            "SELECT title_ja,title_zh_hans,title_en,original_language FROM anime_work WHERE id=?",
                            (anime_id,),
                        ).fetchone()
                        if row and row[0]:
                            title = str(row[0])
                            original_language = str(row[3] or "ja")
                            title_fields = {
                                "title_ja": title,
                                "title_zh_hans": str(row[1] or ""),
                                "title_en": str(row[2] or ""),
                                "original_language": original_language,
                                "title_zh_hans_localized": localized_archive_title(db, anime_id, "zh-Hans", title) or "",
                                "title_en_localized": localized_archive_title(db, anime_id, "en", title) or "",
                                "title_ja_localized": localized_archive_title(db, anime_id, "ja", title) or "",
                            }
                    else:
                        available = [name for name in ("title_zh_hans", "title_en", "title_ja") if name in columns]
                        if available:
                            expression = ",".join(f"NULLIF({name},'')" for name in available)
                            row = db.execute(
                                f"SELECT COALESCE({expression},?) FROM anime_work WHERE id=?",
                                (title, anime_id),
                            ).fetchone()
                            if row and row[0]:
                                title = str(row[0])
            except (OSError, sqlite3.Error):
                pass
            current = {"animeId": anime_id, "title": title, **title_fields,
                       "batchSize": len(values), "stage": stage}
        with self.lock:
            self.state["current"] = current
            self.state["updatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        self._persist()

    def _pause_point(self, generation: int, stage: str) -> bool:
        while self._current(generation):
            if self.image_fetcher is not None:
                setter = getattr(self.image_fetcher, "set_foreground_pressure", None)
                if callable(setter):
                    setter(bool(self.interactive()))
            with self.lock:
                paused = bool(self.state.get("controls", {}).get("paused"))
            offline = network_connectivity.is_offline()
            if not paused and not offline:
                if self.state.get("state") in {"paused", "waiting_network"}:
                    self._set_state("warming", stage)
                    with self.lock:
                        self.progress_mark = time.monotonic()
                return True
            target_state = "paused" if paused else "waiting_network"
            if self.state.get("state") != target_state or self.state.get("stage") != stage:
                self._set_state(target_state, stage)
            self.stop_event.wait(.2)
        return False

    @staticmethod
    def _recent_range() -> tuple[str, str]:
        today = dt.date.today()
        end_index = today.year * 12 + today.month - 1
        start_index = end_index - 5
        return (f"{start_index // 12:04d}-{start_index % 12 + 1:02d}",
                f"{end_index // 12:04d}-{end_index % 12 + 1:02d}")

    def _params(self, config: dict[str, Any], seed: str, offset: int, page_size: int) -> dict[str, list[str]]:
        defaults = config.get("ui", {}).get("filterDefaults", {})
        params: dict[str, list[str]] = {
            "media_type": [str(value) for value in defaults.get("mediaTypes", ["tv", "movie"])],
            "limit": [str(page_size)], "offset": [str(offset)],
            "language": [str(config.get("ui", {}).get("language") or "zh-Hans")],
            "sort": ["random"], "direction": ["asc"], "seed": [seed],
        }
        try:
            torrents = int(runtime_catalog.runtime_stats(self.db_path).get("torrents", 0))
        except (OSError, sqlite3.Error, ValueError):
            torrents = 0
        availability = defaults.get("availability", ["torrent", "ani-rss"]) if torrents else ["torrent", "ani-rss", "unavailable"]
        params["availability"] = [str(value) for value in availability]
        params["library_state"] = [str(value) for value in defaults.get("libraryStates", [
            "local", "external", "submitted", "not_in_library",
        ])]
        return params

    def _ani_prefetch(self, anime_id: int, config: dict[str, Any], generation: int) -> None:
        if not self._current(generation):
            return
        with self.background_budget.lease("aniRss", stop_event=self.stop_event) as allowed:
            if not allowed or not self._current(generation):
                return
            try:
                if ani_rss.background_search_due(self.db_path, anime_id, config):
                    ani_rss.search(self.db_path, anime_id, config)
            except (OSError, ValueError, RuntimeError, sqlite3.Error, urllib.error.URLError):
                return

    def _wait_for_images(self, anime_ids: Iterable[int], generation: int, stage: str) -> bool:
        if self.image_fetcher is None:
            return True
        values = list(anime_ids)
        setter = getattr(self.image_fetcher, "set_foreground_pressure", None)
        while self._current(generation):
            if not self._pause_point(generation, stage):
                return False
            if callable(setter):
                setter(bool(self.interactive()))
            if not any(self.image_fetcher.pending(anime_id) for anime_id in values):
                return True
            self.stop_event.wait(.1)
        return False

    def _announce(self, *, total: int, page_size: int, prefetch_pages: int,
                  network: dict[str, Any], ani_enabled: bool) -> None:
        with self.lock:
            if self.start_reported:
                return
            self.start_reported = True
        snapshotter = getattr(self.image_fetcher, "snapshot", None) if self.image_fetcher is not None else None
        snapshot = snapshotter() if callable(snapshotter) else {"workers": 0, "hostLimit": 0}
        details = {
            "recentWorks": int(total),
            "pageSize": int(page_size),
            "windowPages": int(prefetch_pages),
            "workers": int(snapshot.get("workers", 0)),
            "hostLimit": int(snapshot.get("hostLimit", 0)),
            "timeout": float(network.get("probeTimeoutSeconds", 12)),
            "aniRss": bool(ani_enabled),
        }
        if self.started_callback is not None:
            try:
                self.started_callback(details)
                return
            except Exception as exc:
                log_event("WARNING", "startup_report_failed", errorType=type(exc).__name__)
        print(
            f"[images] preload started priority-pages={prefetch_pages} recent={total} then=older-catalog "
            f"workers={details['workers']} hostLimit={details['hostLimit']} Ani-RSS={'on' if ani_enabled else 'off'}",
            flush=True,
        )

    def _enqueue(self, anime_ids: Iterable[int], network: dict[str, Any], *, priority: str | int = "prefetch",
                 refresh: bool = False, generation: int | None = None, stage: str = "") -> bool:
        if self.image_fetcher is None:
            return True
        values = [int(anime_id) for anime_id in anime_ids]
        for anime_id in values:
            while True:
                if generation is not None and not self._pause_point(generation, stage):
                    return False
                if self.image_fetcher.enqueue(anime_id, network, priority=priority, refresh=refresh):
                    break
                if network_connectivity.is_offline():
                    continue
                raise RuntimeError("image_fetcher_enqueue_failed")
        return True

    def _result_counts(self, anime_ids: Iterable[int]) -> tuple[int, int, int, set[int]]:
        available = no_image = failed = 0
        retry: set[int] = set()
        if self.image_fetcher is None:
            return available, no_image, failed, retry
        for anime_id in anime_ids:
            result = str(self.image_fetcher.result(int(anime_id)) or "error:unknown")
            if result == "available":
                available += 1
            elif result == "no_image":
                no_image += 1
            else:
                failed += 1
                retry.add(int(anime_id))
        return available, no_image, failed, retry

    def _direct_batches(self, where: str, values: tuple[Any, ...], batch_size: int) -> tuple[int, Iterable[list[int]]]:
        with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM anime_work WHERE {where}", values).fetchone()[0])

        def batches() -> Iterable[list[int]]:
            offset = 0
            while True:
                with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
                    rows = db.execute(
                        f"SELECT id FROM anime_work WHERE {where} "
                        "ORDER BY CASE WHEN start_month IS NULL OR start_month='' THEN 1 ELSE 0 END, start_month DESC, id DESC "
                        "LIMIT ? OFFSET ?",
                        (*values, batch_size, offset),
                    ).fetchall()
                ids = [int(row[0]) for row in rows]
                if not ids:
                    break
                yield ids
                offset += len(ids)
        return total, batches()

    def _cached_count(self, where: str, values: tuple[Any, ...]) -> int:
        with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_image'").fetchone():
                return 0
            row = db.execute(
                f"SELECT COUNT(*) FROM anime_work w JOIN anime_image i ON i.anime_id=w.id WHERE ({where}) "
                "AND (i.image_blob IS NOT NULL OR i.error='no_cover')", values,
            ).fetchone()
            return int(row[0] if row else 0)

    def _needed_ids(self, anime_ids: Iterable[int]) -> list[int]:
        values = [int(value) for value in anime_ids]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
            rows = db.execute(
                f"SELECT w.id FROM anime_work w LEFT JOIN anime_image i ON i.anime_id=w.id "
                f"WHERE w.id IN ({placeholders}) AND (i.anime_id IS NULL OR "
                "(i.image_blob IS NULL AND COALESCE(i.error,'')<>'no_cover'))", values,
            ).fetchall()
        needed = {int(row[0]) for row in rows}
        return [value for value in values if value in needed]

    def _history_batches_by_month(self, before_month: str, batch_size: int) -> Iterable[tuple[int, list[int]]]:
        with contextlib.closing(sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
            months = [str(row[0]) for row in db.execute(
                "SELECT DISTINCT start_month FROM anime_work "
                "WHERE start_month<? AND start_month IS NOT NULL AND start_month<>'' "
                "ORDER BY start_month DESC", (before_month,))]
        group_index = 0
        for month in months:
            _total, batches = self._direct_batches("start_month=?", (month,), batch_size)
            for anime_ids in batches:
                yield group_index, anime_ids
            group_index += 1
        _unknown_total, unknown_batches = self._direct_batches(
            "start_month IS NULL OR start_month=''", (), batch_size)
        for anime_ids in unknown_batches:
            yield group_index, anime_ids

    def _future_batches_by_month(self, after_month: str, batch_size: int) -> Iterable[tuple[int, list[int]]]:
        with contextlib.closing(sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
            months = [str(row[0]) for row in db.execute(
                "SELECT DISTINCT start_month FROM anime_work "
                "WHERE start_month>? AND start_month IS NOT NULL AND start_month<>'' "
                "ORDER BY start_month ASC", (after_month,))]
        for group_index, month in enumerate(months):
            _total, batches = self._direct_batches("start_month=?", (month,), batch_size)
            for anime_ids in batches:
                yield group_index, anime_ids

    def _run(self, seed: str, generation: int) -> None:
        ani_pool: concurrent.futures.ThreadPoolExecutor | None = None
        ani_scan_lease: Any = None
        try:
            config = self.config_store.read()
            raw_page_size = config.get("ui", {}).get("pageSize", 12)
            try:
                page_size = min(100, max(1, int(raw_page_size)))
            except (TypeError, ValueError):
                page_size = 12
            prefetch_pages = max(1, min(8, int(os.getenv("ANM_IMAGE_PREFETCH_PAGES", "8"))))
            batch_size = max(page_size, min(400, page_size * prefetch_pages))
            network = image_network_config(config)
            ani_settings = config.get("components", {}).get("aniRss", {})
            try:
                ani_state = ani_rss.state(self.db_path, config)
                ani_enabled = (ani_rss.state_available(ani_state)
                               and ani_state.get("effective_mode") in {"prefer", "fallback"})
            except (OSError, sqlite3.Error, ValueError):
                ani_enabled = False
            ani_workers = max(1, min(4, int(os.getenv("ANM_ANI_RSS_PREFETCH_WORKERS", "3"))))
            ani_scan_acquired = False
            if ani_enabled:
                ani_scan_lease = ani_rss.background_resource_scan_lease()
                ani_scan_acquired = bool(ani_scan_lease.__enter__())
            ani_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=ani_workers, thread_name_prefix="anm-ani-prefetch"
            ) if ani_enabled and ani_scan_acquired else None
            ani_prefetch_enabled = ani_pool is not None
            ani_futures: list[concurrent.futures.Future[Any]] = []
            ani_scheduled: set[int] = set()
            def schedule_ani(values: Iterable[int]) -> None:
                if ani_pool is None:
                    return
                for raw in values:
                    anime_id = int(raw)
                    if anime_id in ani_scheduled:
                        continue
                    ani_scheduled.add(anime_id)
                    ani_futures.append(ani_pool.submit(self._ani_prefetch, anime_id, config, generation))
            started = time.monotonic()
            with self.lock:
                self.throughput_ewma = 0.0
                self.progress_mark = started
            retry_ids: set[int] = set()
            totals = {"available": 0, "noImage": 0, "failed": 0}
            self._set_current([], "")
            self._set_retry_pending(0)
            with self.lock:
                self.state["remainingErrors"] = 0
            start_month, end_month = self._recent_range()
            recent_where = "start_month>=? AND start_month<=?"
            history_where = "start_month<? OR start_month>? OR start_month IS NULL OR start_month=''"
            recent_all, _ = self._direct_batches(recent_where, (start_month, end_month), batch_size)
            history_total, _ = self._direct_batches(history_where, (start_month, end_month), batch_size)
            self._update_stage("recent", total=recent_all,
                               done=min(recent_all, self._cached_count(recent_where, (start_month, end_month))))
            self._update_stage("history", total=history_total,
                               done=min(history_total, self._cached_count(history_where, (start_month, end_month))))
            self._update_stage("retry", total=0, done=0, failed=0)
            self._set_state("paused" if self.state.get("controls", {}).get("paused") else "warming", "recent")
            self._announce(total=recent_all, page_size=page_size, prefetch_pages=prefetch_pages,
                           network=network, ani_enabled=ani_prefetch_enabled)

            # The configured random landing pages are the highest background priority.
            # Process the bounded page window strictly page-by-page so page 1 completes
            # before page 2, while interactive requests can still promote any item to 0.
            for page_index in range(prefetch_pages):
                if not self._current(generation) or not self._pause_point(generation, "recent"):
                    break
                payload = query_catalog(
                    self.db_path, self._params(config, seed, page_index * page_size, page_size), config)
                anime_ids = [int(item["id"]) for item in payload.get("items", [])]
                if not anime_ids:
                    break
                needed = self._needed_ids(anime_ids)
                if not self.start_reported:
                    self._announce(total=recent_all, page_size=page_size, prefetch_pages=prefetch_pages,
                                   network=network, ani_enabled=ani_prefetch_enabled)
                if ani_pool is not None:
                    marks = ",".join("?" for _ in anime_ids)
                    with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=15)) as db:
                        priority_recent = [int(row[0]) for row in db.execute(
                            f"SELECT id FROM anime_work WHERE id IN ({marks}) AND start_month>=? AND start_month<=? ORDER BY id",
                            [*anime_ids, start_month, end_month],
                        )]
                    schedule_ani(priority_recent)
                if not needed:
                    continue
                self._set_current(needed, "recent")
                if not self._enqueue(needed, network, priority=1 + page_index, generation=generation, stage="recent"):
                    break
                if not self._wait_for_images(needed, generation, "recent"):
                    break
                available, no_image, failed, retry = self._result_counts(needed)
                totals["available"] += available
                totals["noImage"] += no_image
                totals["failed"] += failed
                retry_ids.difference_update(set(needed) - retry)
                retry_ids.update(retry)
                self._set_retry_pending(len(retry_ids))
                self._set_current([], "recent")

            # Priority-page downloads may have filled part of the recent/history stages.
            self._update_stage("recent", total=recent_all,
                               done=min(recent_all, self._cached_count(recent_where, (start_month, end_month))))
            self._update_stage("history", total=history_total,
                               done=min(history_total, self._cached_count(history_where, (start_month, end_month))))

            _recent_total, recent_batches = self._direct_batches(
                recent_where, (start_month, end_month), batch_size)
            for anime_ids in recent_batches:
                if not self._current(generation) or not self._pause_point(generation, "recent"):
                    break
                needed = self._needed_ids(anime_ids)
                if needed:
                    self._set_current(needed, "recent")
                    if not self._enqueue(needed, network, priority="prefetch", generation=generation, stage="recent"):
                        break
                    if not self.start_reported:
                        self._announce(total=recent_all, page_size=page_size, prefetch_pages=prefetch_pages,
                                       network=network, ani_enabled=ani_prefetch_enabled)
                    if not self._wait_for_images(needed, generation, "recent"):
                        break
                    available, no_image, failed, retry = self._result_counts(needed)
                    totals["available"] += available
                    totals["noImage"] += no_image
                    totals["failed"] += failed
                    retry_ids.difference_update(set(needed) - retry)
                    retry_ids.update(retry)
                    self._set_retry_pending(len(retry_ids))
                    self._advance_stage("recent", len(needed), failed=len(retry_ids))
                    self._set_current([], "recent")
                self._mark_prepared_through(anime_ids)
                schedule_ani(anime_ids)
                stage = self.snapshot().get("stages", {}).get("recent", {})
                progress_pct = 100.0 * int(stage.get("done") or 0) / recent_all if recent_all else 100.0
                elapsed = max(.001, time.monotonic() - started)
                ani_done = sum(1 for future in ani_futures if future.done() and not future.cancelled())
                metrics.progress(
                    f"[images] recent {stage.get('done', 0)}/{recent_all} ({progress_pct:.1f}%) "
                    f"available={totals['available']} noImage={totals['noImage']} errors={totals['failed']} "
                    f"rate={max(1, int(stage.get('done') or 0)) / elapsed:.2f}/s "
                    f"Ani-RSS={ani_done}/{len(ani_futures)} elapsed={elapsed:.1f}s"
                )

            self._set_state("paused" if self.state.get("controls", {}).get("paused") else "warming", "history")
            for month_index, anime_ids in self._history_batches_by_month(start_month, batch_size):
                if not self._current(generation) or not self._pause_point(generation, "history"):
                    break
                needed = self._needed_ids(anime_ids)
                if needed:
                    self._set_current(needed, "history")
                    if not self._enqueue(needed, network, priority=min(800, 100 + month_index), generation=generation, stage="history"):
                        break
                    if not self._wait_for_images(needed, generation, "history"):
                        break
                    available, no_image, failed, retry = self._result_counts(needed)
                    totals["available"] += available
                    totals["noImage"] += no_image
                    totals["failed"] += failed
                    retry_ids.difference_update(set(needed) - retry)
                    retry_ids.update(retry)
                    self._set_retry_pending(len(retry_ids))
                    self._advance_stage("history", len(needed), failed=len(retry_ids))
                    self._set_current([], "history")
                self._mark_prepared_through(anime_ids)
                # background_search_due rejects month 25+; queue order therefore follows
                # image history exactly from month 7 through the rolling 24-month cutoff.
                schedule_ani(anime_ids)
                stage = self.snapshot().get("stages", {}).get("history", {})
                progress_pct = 100.0 * int(stage.get("done") or 0) / history_total if history_total else 100.0
                elapsed = max(.001, time.monotonic() - started)
                metrics.progress(
                    f"[images] history {stage.get('done', 0)}/{history_total} ({progress_pct:.1f}%) "
                    f"available={totals['available']} noImage={totals['noImage']} errors={totals['failed']} "
                    f"elapsed={elapsed:.1f}s"
                )

            for month_index, anime_ids in self._future_batches_by_month(end_month, batch_size):
                if not self._current(generation) or not self._pause_point(generation, "history"):
                    break
                needed = self._needed_ids(anime_ids)
                if not needed:
                    continue
                self._set_current(needed, "history")
                if not self._enqueue(needed, network, priority=min(850, 820 + month_index), generation=generation, stage="history"):
                    break
                if not self._wait_for_images(needed, generation, "history"):
                    break
                available, no_image, failed, retry = self._result_counts(needed)
                totals["available"] += available
                totals["noImage"] += no_image
                totals["failed"] += failed
                retry_ids.difference_update(set(needed) - retry)
                retry_ids.update(retry)
                self._set_retry_pending(len(retry_ids))
                self._advance_stage("history", len(needed), failed=len(retry_ids))
                self._set_current([], "history")

            self._set_state("paused" if self.state.get("controls", {}).get("paused") else "warming", "retry")
            retry_total = len(retry_ids)
            self._set_retry_pending(retry_total)
            self._update_stage("retry", total=retry_total, done=0, failed=retry_total)
            for _round in range(2):
                if not retry_ids or not self._current(generation) or not self._pause_point(generation, "retry"):
                    break
                self._set_current(sorted(retry_ids), "retry")
                if not self._enqueue(retry_ids, network, priority="retry", refresh=True, generation=generation, stage="retry"):
                    break
                if not self._wait_for_images(retry_ids, generation, "retry"):
                    break
                retry_ids = {
                    anime_id for anime_id in retry_ids
                    if str(self.image_fetcher.result(anime_id) if self.image_fetcher is not None else "").startswith("error:")
                }
                self._set_retry_pending(len(retry_ids))
                self._update_stage("retry", done=retry_total - len(retry_ids), failed=len(retry_ids))
                self._set_current([], "retry")
                if retry_ids:
                    self.stop_event.wait(1.0)

            if self._current(generation):
                ani_done = sum(1 for future in ani_futures if future.done() and not future.cancelled())
                processed = recent_all + history_total
                self._set_retry_pending(0)
                self._set_current([], "warm")
                with self.lock:
                    self.state["remainingErrors"] = len(retry_ids)
                self._set_state("warm", "warm")
                metrics.progress(
                    f"[images] preload complete works={processed} remainingErrors={len(retry_ids)} "
                    f"Ani-RSS={ani_done}/{len(ani_futures)} elapsed={time.monotonic() - started:.1f}s",
                    final=True,
                )
                log_event("INFO", "catalog_warmup_complete", seed=seed, works=processed,
                          remainingErrors=len(retry_ids), aniRss=ani_prefetch_enabled)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            metrics.end_progress()
            if self._current(generation):
                self._set_state("failed", self.state.get("stage") or "", error=f"{type(exc).__name__}: {exc}")
                log_event("ERROR", "catalog_warmup_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if ani_pool is not None:
                # Keep the scan lease until all already-running calls finish. Queued
                # work may be cancelled, but a later scheduled pass must never overlap.
                ani_pool.shutdown(wait=True, cancel_futures=not self._current(generation))
            if ani_scan_lease is not None:
                ani_scan_lease.__exit__(None, None, None)


_ADMIN_GET_PATHS = frozenset({
    "/api/logs", "/api/history", "/api/archive/update", "/api/maintenance/status", "/api/auth/users",
    "/api/diagnostics/network", "/api/diagnostics/playback", "/api/images/preload", "/api/system/health",
    "/api/update/status",
})
_ADMIN_POST_PATHS = frozenset({
    "/api/archive/update", "/api/archive/import", "/api/images/refresh", "/api/metadata/repair",
    "/api/catalog/reshuffle", "/api/ani-rss/sync", "/api/connections/test",
    "/api/connections/qbittorrent/credential", "/api/connections/ani-rss/credential",
    "/api/connections/subtitles/credentials", "/api/settings", "/api/library/audit", "/api/auth/users",
    "/api/diagnostics/network/recheck", "/api/images/preload/control", "/api/update/apply",
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
                  warmup_ready: threading.Event | None = None,
                  warmup_started_callback: Callable[[dict[str, Any]], None] | None = None,
                  start_warmup: bool = True,
                  startup_started_monotonic: float | None = None,
                  restart_callback: Callable[[], None] | None = None):
    credential_store.load_into_environment(STATE_DIR)
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
    playback_diagnostics = playback.PlaybackDiagnostics()
    runtime_health_lock = threading.RLock()
    runtime_health: dict[str, dict[str, Any]] = {
        "qbittorrent": {"status": "warning", "detail": "unknown", "updatedAt": 0.0},
    }
    health_cache_lock = threading.Lock()
    health_cache: dict[str, Any] = {"expires": 0.0, "payload": None}

    def invalidate_health_cache() -> None:
        with health_cache_lock:
            health_cache["expires"] = 0.0
            health_cache["payload"] = None

    def set_runtime_health(component: str, *, status: str, detail: str) -> None:
        with runtime_health_lock:
            runtime_health[component] = {
                "status": "normal" if status == "normal" else "warning",
                "detail": str(detail or "unknown"),
                "updatedAt": time.time(),
            }
        invalidate_health_cache()

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

    performance = PerformanceBaseline(startup_started_monotonic, STATE_DIR / "performance-baseline.json")
    background_budget = BackgroundTaskBudget(
        image_fetcher, interactive=lambda: time.monotonic() < interactive_until[0])
    set_active_background_budget(background_budget)
    catalog_warmup = CatalogWarmup(
        db_path, config_store, image_fetcher,
        interactive=lambda: time.monotonic() < interactive_until[0], ready_event=warmup_ready,
        started_callback=warmup_started_callback, background_budget=background_budget, performance=performance,
    )
    if start_warmup:
        catalog_warmup.start()

    def startup_status() -> dict[str, Any]:
        preload = catalog_warmup.snapshot()
        catalog_ready = bool(warmup_ready is None or warmup_ready.is_set())
        preload_state = str(preload.get("state") or "idle")
        if not catalog_ready:
            state = "Starting"
        elif preload_state == "warm":
            state = "Warm"
        elif preload_state in {"warming", "paused"}:
            state = "Warming"
        else:
            state = "Ready"
        if catalog_ready:
            performance.mark("catalogReady")
        return {
            "state": state,
            "catalogReady": catalog_ready,
            "preloadState": preload_state,
            "stage": str(preload.get("stage") or ""),
            "preload": preload,
            "performance": performance.snapshot(),
        }

    def system_health_status() -> dict[str, Any]:
        now = time.time()
        with health_cache_lock:
            cached = health_cache.get("payload")
            if cached is not None and float(health_cache.get("expires") or 0) > now:
                return json.loads(json.dumps(cached, ensure_ascii=False))

        current = config_store.read()
        items: list[dict[str, Any]] = []

        network_payload = network_diagnostics.snapshot(db_path, current)
        network_items = list(network_payload.get("items") or [])
        degraded_services: list[str] = []
        sampled = 0
        for service in ("bangumi_api", "bangumi_image"):
            service_items = [item for item in network_items if item.get("service") == service]
            sampled_items = [item for item in service_items if int(item.get("samples") or 0) > 0]
            sampled += len(sampled_items)
            if sampled_items and not any(
                float(item.get("recentSuccessRate") or 0) >= 0.5
                and float(item.get("cooldownUntil") or 0) <= now
                for item in sampled_items
            ):
                degraded_services.append(service)
        network_status = "warning" if degraded_services or sampled == 0 else "normal"
        items.append({"id": "network", "status": network_status,
                      "detail": "degraded" if degraded_services else "ready" if sampled else "unknown",
                      "info": {"route": str((network_payload.get("route") or {}).get("mode") or "direct"),
                               "sampled": sampled, "degraded": len(degraded_services)}})

        ani_settings = current.get("components", {}).get("aniRss", {})
        ani_mode = str(ani_settings.get("mode") or "manual")
        ani_state = ani_rss.state(db_path, current)
        ani_connection = str(ani_state.get("connectionState") or ani_state.get("connection_state") or "unknown")
        if ani_mode == "manual":
            items.append({"id": "aniRss", "status": "normal", "detail": "manual",
                          "info": {"mode": ani_mode, "connectionState": ani_connection}})
        elif ani_connection == "ready":
            items.append({"id": "aniRss", "status": "normal", "detail": "ready",
                          "info": {"mode": ani_mode, "connectionState": ani_connection}})
        else:
            items.append({"id": "aniRss", "status": "warning", "detail": "unavailable",
                          "info": {"mode": ani_mode, "connectionState": ani_connection}})

        qbt = current.get("components", {}).get("downloadClient", {})
        configured_submission = qbt.get("submissionEnabled")
        qbt_enabled = bool(submission_enabled if configured_submission is None else configured_submission)
        if not qbt_enabled:
            qbt_summary = {"id": "qbittorrent", "status": "normal", "detail": "disabled"}
        elif not str(qbt.get("endpoint") or "").strip():
            qbt_summary = {"id": "qbittorrent", "status": "warning", "detail": "unavailable"}
        else:
            with runtime_health_lock:
                observed = dict(runtime_health.get("qbittorrent") or {})
            qbt_summary = {"id": "qbittorrent", "status": observed.get("status", "warning"),
                           "detail": observed.get("detail", "unknown")}
        qbt_summary["info"] = {"enabled": qbt_enabled, "updatedAt": float(observed.get("updatedAt") or 0) if qbt_enabled and str(qbt.get("endpoint") or "").strip() else 0}
        items.append(qbt_summary)

        try:
            storage = storage_preflight.check_config(current, timeout=1.5)
        except (OSError, ValueError, RuntimeError):
            storage = {}
        storage_warning = False
        library_state = str((storage.get("library") or {}).get("state") or "not_configured")
        if library_state != AVAILABLE:
            storage_warning = True
        for key, value in storage.items():
            state = str(value.get("state") or "")
            if key != "library" and state not in {"", "not_configured", AVAILABLE}:
                storage_warning = True
        storage_warnings = sum(1 for key, value in storage.items()
                               if (key == "library" and str(value.get("state") or "") != AVAILABLE)
                               or (key != "library" and str(value.get("state") or "") not in {"", "not_configured", AVAILABLE}))
        items.append({"id": "storage", "status": "warning" if storage_warning else "normal",
                      "detail": "unavailable" if storage_warning else "ready",
                      "info": {"checked": len(storage), "warnings": storage_warnings}})

        preload = catalog_warmup.snapshot()
        preload_state = str(preload.get("state") or "idle")
        catalog_ready = bool(warmup_ready is None or warmup_ready.is_set())
        preload_warning = preload_state == "failed" or not catalog_ready
        preload_detail = "failed" if preload_state == "failed" else (
            "warming" if preload_state in {"warming", "paused"} else "warm" if preload_state == "warm" else "ready"
        )
        items.append({"id": "imagePreload", "status": "warning" if preload_warning else "normal",
                      "detail": preload_detail, "info": {"state": preload_state,
                      "stage": str(preload.get("stage") or ""),
                      "estimatedRemaining": int(preload.get("estimatedRemaining") or 0),
                      "current": dict(preload.get("current") or {})}})

        playback_config = current.get("playback", {})
        sessions = playback_diagnostics.snapshot()
        playback_degraded = any(
            any(token in str(item.get(field) or "").casefold() for token in ("error", "failed", "unavailable"))
            for item in sessions for field in ("state", "upstream")
        )
        if not bool(playback_config.get("enabled", True)):
            playback_summary = {"id": "playback", "status": "normal", "detail": "disabled"}
        else:
            playback_summary = {"id": "playback", "status": "warning" if playback_degraded else "normal",
                                "detail": "degraded" if playback_degraded else "ready"}
        playback_summary["info"] = {"sessions": len(sessions),
                                    "degraded": sum(1 for item in sessions if any(
                                        token in str(item.get(field) or "").casefold()
                                        for token in ("error", "failed", "unavailable") for field in ("state", "upstream")))}
        items.append(playback_summary)

        payload = {
            "status": "warning" if any(item["status"] == "warning" for item in items) else "normal",
            "items": items,
            "updatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        }
        with health_cache_lock:
            health_cache["payload"] = payload
            health_cache["expires"] = now + 10.0
        return json.loads(json.dumps(payload, ensure_ascii=False))

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
                with ani_rss_user_operation():
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
        network = image_network_config(config_store.read())
        accepted = [anime_id for anime_id in queue if image_fetcher.enqueue(anime_id, network, refresh=True)]
        rejected = len(queue) - len(accepted)
        with maintenance_lock:
            maintenance["images"]["failed"] = rejected
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
                maintenance["images"]["failed"] = rejected + sum(
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
                    with background_budget.lease("metadata") as allowed:
                        if not allowed:
                            break
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
            remote_jobs = payload.get("aniRssJobs", [])
            if remote_jobs:
                with ani_rss_user_operation():
                    for job in remote_jobs:
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

    def combined_network_diagnostics(*, force: bool = False) -> dict[str, Any]:
        current = config_store.read()
        if force:
            probe_started = time.monotonic()
            try:
                online = network_diagnostics.connectivity_probe(db_path, current, timeout=2.5)
            except Exception:
                if not network_connectivity.is_offline():
                    raise
                online = network_diagnostics.internet_canary_probe(timeout=2.5)
            network_connectivity.note_probe(online, started_at=probe_started)
            image_fetcher.set_network_state(
                offline=network_connectivity.is_offline(),
                suppress_learning=not network_connectivity.failure_learning_allowed(),
            )
        payload = (network_diagnostics.recheck(db_path, current) if force
                   else network_diagnostics.snapshot(db_path, current))
        update_payload = application_update.network_diagnostics(force=force)
        payload["items"] = [*(payload.get("items") or []), *(update_payload.get("items") or [])]
        payload["applicationUpdate"] = update_payload
        return payload

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
            # BaseHTTPRequestHandler.send_error() emits a preliminary
            # ``code NNN, message ...`` log and then logs the same request with
            # its HTTP status. Keep the request record only, otherwise every
            # ordinary 4xx/5xx appears twice in the console.
            if fmt.startswith("code %d"):
                return
            try:
                status = int(args[1])
            except (IndexError, TypeError, ValueError):
                status = 0
            if 200 <= status < 400:
                return
            # The login page intentionally probes the session endpoint before
            # credentials are entered. Its 401 is normal protocol state, not a
            # server failure; keep real authentication failures visible.
            if status == HTTPStatus.UNAUTHORIZED and urllib.parse.urlsplit(
                    str(getattr(self, "path", ""))).path == "/api/auth/session":
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
            requested_header = self.headers.get("Range", "").strip()
            playback_diagnostics.begin(token, locator, requested_header)
            if locator.source_type == "ani-rss":
                if not locator.remote_filename:
                    playback_diagnostics.finish(token, "unavailable")
                    self.send_error(HTTPStatus.NOT_FOUND, "remote media unavailable")
                    return
                requested = requested_header
                if requested and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", requested):
                    playback_diagnostics.finish(token, "invalid-range")
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                forwarded_range = requested or ("bytes=0-0" if head_only else "")
                response_started = False
                current_start = 0
                current_end: int | None = None
                expected_length: int | None = None
                sent = 0
                current_range = forwarded_range
                config = config_store.read()
                current_state = ani_rss.state(db_path, config)
                if not ani_rss.state_available(current_state):
                    playback_diagnostics.finish(token, "unavailable")
                    self.send_error(HTTPStatus.NOT_FOUND, "remote media unavailable")
                    return

                def content_range(value: str) -> tuple[int, int, int | None] | None:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", value.strip())
                    if not match:
                        return None
                    return int(match.group(1)), int(match.group(2)), (None if match.group(3) == "*" else int(match.group(3)))

                def write_remote(response: httpx.Response, *, first: bool) -> None:
                    nonlocal response_started, current_start, current_end, expected_length, sent
                    status = int(response.status_code)
                    playback_diagnostics.upstream(token, f"HTTP {status}")
                    headers = response.headers
                    parsed_range = content_range(str(headers.get("Content-Range") or ""))
                    if (current_range and status != HTTPStatus.PARTIAL_CONTENT
                            and not (head_only and not requested and status == HTTPStatus.OK)):
                        raise ani_rss.RemoteFileError(HTTPStatus.BAD_GATEWAY)
                    if status == HTTPStatus.PARTIAL_CONTENT:
                        if parsed_range is None:
                            raise ani_rss.RemoteFileError(HTTPStatus.BAD_GATEWAY)
                        if not first and parsed_range[0] != current_start + sent:
                            raise ani_rss.RemoteFileError(HTTPStatus.BAD_GATEWAY)
                    if not first:
                        for chunk in response.iter_raw(1024 * 1024):
                            if chunk:
                                self.wfile.write(chunk)
                                sent += len(chunk)
                                playback_diagnostics.transfer(token, len(chunk))
                        return

                    if head_only and not requested:
                        total = locator.size
                        if parsed_range is not None and parsed_range[2] is not None:
                            total = parsed_range[2]
                        elif status == HTTPStatus.OK and str(headers.get("Content-Length") or "").isdigit():
                            total = int(headers["Content-Length"])
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", headers.get("Content-Type") or playback.media_mime(locator))
                        if total > 0:
                            self.send_header("Content-Length", str(total))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(locator.name)}")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        response_started = True
                        return

                    if parsed_range is not None:
                        current_start, current_end, _ = parsed_range
                        expected_length = current_end - current_start + 1
                    elif str(headers.get("Content-Length") or "").isdigit():
                        expected_length = int(headers["Content-Length"])
                        current_end = expected_length - 1 if expected_length > 0 else None
                    self.send_response(status)
                    self.send_header("Content-Type", headers.get("Content-Type") or playback.media_mime(locator))
                    if expected_length is not None:
                        self.send_header("Content-Length", str(expected_length))
                    if parsed_range is not None:
                        self.send_header("Content-Range", headers["Content-Range"])
                    self.send_header("Accept-Ranges", "bytes")
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
                            sent += len(chunk)
                            playback_diagnostics.transfer(token, len(chunk))

                try:
                    try:
                        with ani_rss.stream_media(config, locator.remote_filename, current_range) as response:
                            write_remote(response, first=True)
                    except httpx.RequestError:
                        if not response_started or head_only:
                            raise
                    if head_only or expected_length is None or sent >= expected_length:
                        playback_diagnostics.finish(token)
                        return
                    for delay in (0.15, 0.5):
                        resume_at = current_start + sent
                        if current_end is not None and resume_at > current_end:
                            playback_diagnostics.finish(token)
                            return
                        current_range = f"bytes={resume_at}-{'' if current_end is None else current_end}"
                        playback_diagnostics.resume(token)
                        if delay:
                            time.sleep(delay)
                        try:
                            with ani_rss.stream_media(config, locator.remote_filename, current_range) as response:
                                write_remote(response, first=False)
                        except (httpx.RequestError, ani_rss.RemoteFileError):
                            continue
                        if expected_length is None or sent >= expected_length:
                            playback_diagnostics.finish(token)
                            return
                    playback_diagnostics.finish(token, "incomplete")
                    return
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    playback_diagnostics.finish(token, "client-disconnected")
                    return
                except ani_rss.RemoteFileError as exc:
                    playback_diagnostics.upstream(token, f"error HTTP {exc.status}")
                    playback_diagnostics.finish(token, "upstream-error")
                    if response_started:
                        return
                    if exc.status == 404:
                        status = HTTPStatus.NOT_FOUND
                    elif exc.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                        status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
                    else:
                        status = HTTPStatus.BAD_GATEWAY
                    self.send_error(status, "Ani-RSS media unavailable")
                except (httpx.RequestError, OSError) as exc:
                    playback_diagnostics.upstream(token, f"{type(exc).__name__}")
                    playback_diagnostics.finish(token, "upstream-error")
                    if not response_started:
                        self.send_error(HTTPStatus.BAD_GATEWAY, "Ani-RSS media unavailable")
                return

            path = locator.local_path
            if locator.source_type != "local" or path is None:
                playback_diagnostics.finish(token, "unavailable")
                self.send_error(HTTPStatus.NOT_FOUND, "media token expired or file unavailable")
                return
            try:
                with playback.open_authorized_media(path, config_store.read()) as (stream, path, file_stat):
                    start, end = 0, file_stat.st_size - 1
                    status = HTTPStatus.OK
                    requested = requested_header
                    if requested:
                        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                        if not match or (not match.group(1) and not match.group(2)):
                            playback_diagnostics.finish(token, "invalid-range")
                            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                            return
                        if match.group(1):
                            start = int(match.group(1)); end = int(match.group(2) or end)
                        else:
                            suffix = int(match.group(2)); start = max(0, file_stat.st_size - suffix)
                        end = min(end, file_stat.st_size - 1)
                        if start > end or start >= file_stat.st_size:
                            playback_diagnostics.finish(token, "invalid-range")
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
                        playback_diagnostics.finish(token)
                        return
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                        playback_diagnostics.transfer(token, len(chunk))
                    playback_diagnostics.finish(token, "complete" if remaining == 0 else "incomplete")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                playback_diagnostics.finish(token, "client-disconnected")
                return
            except path_policy.PathAuthorizationError:
                playback_diagnostics.finish(token, "unavailable")
                self.send_error(HTTPStatus.NOT_FOUND, "media token expired or file unavailable")
            except OSError:
                playback_diagnostics.finish(token, "error")
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
                startup = startup_status()
                ready = bool(startup["catalogReady"])
                marker = hashlib.sha256(str(db_path.resolve()).encode("utf-8")).hexdigest()[:16]
                self.json_response({"ok": ready, "service": "AnimeMachine", "version": __version__, "instanceId": marker,
                                    "kind": "readiness", "state": startup["state"]},
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
            if parsed.path == "/api/startup/state":
                self.json_response(startup_status())
                return
            if parsed.path == "/api/diagnostics/network":
                self.json_response(combined_network_diagnostics(force=False))
                return
            if parsed.path == "/api/diagnostics/playback":
                self.json_response({"items": playback_diagnostics.snapshot()})
                return
            if parsed.path == "/api/images/preload":
                self.json_response(catalog_warmup.snapshot())
                return
            if parsed.path == "/api/system/health":
                self.json_response(system_health_status())
                return
            if parsed.path == "/api/update/status":
                try:
                    query = urllib.parse.parse_qs(parsed.query)
                    force = (query.get("force") or [""])[0].casefold() in {"1", "true", "yes"}
                    self.json_response(application_update.status(force=force))
                except (OSError, ValueError, RuntimeError, httpx.HTTPError, json.JSONDecodeError) as exc:
                    self.json_response({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.BAD_GATEWAY)
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
                params = urllib.parse.parse_qs(parsed.query)
                cfg = config_store.read()
                language = (params.get("language") or [cfg.get("ui", {}).get("language", "en")])[0]
                self.json_response({"items": localized_watches(db_path, language)})
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
                image, cache_state = get_cached_anime_image(db_path, anime_id)
                pending = bool(image_fetcher and image_fetcher.pending(anime_id))
                result = image_fetcher.result(anime_id) if image_fetcher is not None else None
                if image is not None:
                    state = "available"
                elif cache_state == "no_cover" or result == "no_image":
                    state = "no_image"
                elif pending and (cache_state == "transient_error" or str(result or "").startswith("error:")):
                    state = "retrying"
                elif pending:
                    state = "loading"
                elif cache_state == "transient_error" or str(result or "").startswith("error:"):
                    state = "error"
                else:
                    state = "missing"
                self.json_response({"animeId": anime_id, "pending": pending, "state": state, "result": result})
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
                elif cache_state == "no_cover":
                    self.binary_response(*placeholder_image(), cache_seconds=300,
                                         headers={"X-AnimeMachine-Image-Status": "no_image"})
                else:
                    pending = bool(image_fetcher and image_fetcher.pending(anime_id))
                    queued = False
                    if image_fetcher is not None:
                        queued = image_fetcher.enqueue(
                            anime_id, image_network_config(config_store.read()),
                            refresh=cache_state == "transient_error" and not pending, priority="foreground")
                    if queued:
                        status = "retrying" if cache_state == "transient_error" else "loading"
                    else:
                        status = "error" if cache_state == "transient_error" else "missing"
                    self.binary_response(*placeholder_image(), cache_seconds=0 if queued else 30,
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
                with SETTINGS_WRITE_LOCK:
                    config_store.write(payload)
                try:
                    transport.reset()
                except Exception as exc:
                    log_event("ERROR", "config_transport_reset_failed", error=f"{type(exc).__name__}: {exc}")
                try:
                    current = config_store.read()
                    storage = storage_preflight.check_config(current, timeout=3.0)
                except Exception as exc:
                    log_event("ERROR", "config_storage_preflight_failed", error=f"{type(exc).__name__}: {exc}")
                    storage = {}
                self.json_response({
                    "saved": True, "config": payload, "effective": "immediate",
                    "storage": storage,
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
            if parsed.path == "/api/diagnostics/performance":
                try:
                    request = self.read_json()
                    event = str(request.get("event") or "") if isinstance(request, dict) else ""
                    if event not in {"firstScreen", "firstCover"}:
                        raise ValueError("unsupported performance event")
                    performance.mark(event)
                    self.json_response(performance.snapshot())
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/auth/logout":
                auth_store.logout(self.auth_session)
                self.json_response({"authenticated": False}, headers={
                    "Set-Cookie": "anm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                return
            if parsed.path == "/api/diagnostics/network/recheck":
                try:
                    self.json_response(combined_network_diagnostics(force=True))
                except (OSError, ValueError, RuntimeError, sqlite3.Error, httpx.HTTPError) as exc:
                    self.json_response({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.BAD_GATEWAY)
                return
            if parsed.path == "/api/images/preload/control":
                try:
                    request = self.read_json()
                    if not isinstance(request, dict):
                        raise ValueError("JSON object required")
                    paused = request.get("paused") if "paused" in request else None
                    concurrency = request.get("concurrency") if "concurrency" in request else None
                    bandwidth = request.get("bandwidthKiBps") if "bandwidthKiBps" in request else None
                    if paused is not None and not isinstance(paused, bool):
                        raise ValueError("paused must be boolean")
                    self.json_response(catalog_warmup.control(paused=paused, concurrency=concurrency, bandwidth_kib=bandwidth))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/update/apply":
                try:
                    result = application_update.apply(host=self.server.server_address[0], port=int(self.server.server_port))
                    self.json_response(result, HTTPStatus.ACCEPTED)
                    if restart_callback is not None:
                        threading.Timer(.35, restart_callback).start()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
                    self.json_response({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.CONFLICT)
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
                    if not isinstance(request, dict) or not isinstance(request.get("enabled"), bool):
                        raise ValueError("enabled must be boolean")
                    changed = auth_store.set_enabled(int(user_match.group(1)), request["enabled"],
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
                    with ani_rss_user_operation():
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
                    with ani_rss_user_operation():
                        result = ani_rss.delete_subscription(db_path, remote_id, config_store.read(),
                                                             delete_files=delete_files)
                    self.json_response(result)
                except (ValueError, RuntimeError, OSError, urllib.error.URLError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/ani-rss/sync":
                with ani_rss_user_operation():
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
                    anime_id, image_network_config(config_store.read()), refresh=True))
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
            if parsed.path == "/api/settings":
                try:
                    request = self.read_json()
                    draft = request.get("config") if isinstance(request, dict) else None
                    supplied = request.get("credentials", {}) if isinstance(request, dict) else {}
                    if not isinstance(draft, dict) or not isinstance(supplied, dict):
                        raise ValueError("config and credentials must be JSON objects")
                    mapping = {
                        "qbittorrent": "ANM_QBT_API_KEY",
                        "aniRss": "ANM_ANI_RSS_API_KEY",
                        "assrt": "ASSRT_API_TOKEN",
                        "opensubtitles": "OPEN_SUBTITLES_API_KEY",
                    }
                    values = {
                        environment: str(supplied.get(field) or "").strip()
                        for field, environment in mapping.items()
                        if str(supplied.get(field) or "").strip()
                    }
                    # Complete all validation and the rollback-safe commit under one write gate.
                    with SETTINGS_WRITE_LOCK:
                        config_store.validate_for_write(draft)
                        had_persistent_config = config_store.path.is_file()
                        previous = config_store.read_persistent()
                        try:
                            config_store.write(draft)
                            credential_store.store_many(values, STATE_DIR)
                        except Exception:
                            with contextlib.suppress(OSError, ValueError):
                                if had_persistent_config:
                                    config_store.write(previous)
                                else:
                                    config_store.path.unlink(missing_ok=True)
                            raise
                    # Post-commit diagnostics must never turn a completed save into an apparent failure.
                    invalidate_health_cache()
                    try:
                        transport.reset()
                    except Exception as exc:
                        log_event("ERROR", "settings_transport_reset_failed", error=f"{type(exc).__name__}: {exc}")
                    try:
                        current = config_store.read()
                        storage = storage_preflight.check_config(current, timeout=3.0)
                    except Exception as exc:
                        log_event("ERROR", "settings_storage_preflight_failed", error=f"{type(exc).__name__}: {exc}")
                        storage = {}
                    self.json_response({
                        "saved": True, "config": draft, "effective": "immediate",
                        "credentialsConfigured": sorted(field for field, environment in mapping.items() if environment in values),
                        "storage": storage,
                    })
                except (ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/test":
                try:
                    request = self.read_json()
                    kind = str(request.get("kind") or "")
                    transient_key = str(request.get("apiKey") or "").strip() or None
                    with network_connectivity.recovery_probe():
                        if kind == "ani-rss":
                            current = config_store.read()
                            current = json.loads(json.dumps(current))
                            current.setdefault("components", {}).setdefault("aniRss", {})["endpoint"] = str(request.get("endpoint") or "")
                            result = ani_rss.probe(current, transient_key)
                        else:
                            result = connectivity.probe(kind, str(request.get("endpoint") or ""), transient_key)
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
                    with SETTINGS_WRITE_LOCK:
                        credential_store.store("ANM_QBT_API_KEY", key, STATE_DIR)
                    invalidate_health_cache()
                    self.json_response({"configured": True, "persistence": "state"})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/ani-rss/credential":
                try:
                    request = self.read_json()
                    key = str(request.get("apiKey") or "").strip()
                    if not key:
                        raise ValueError("apiKey must not be empty")
                    with SETTINGS_WRITE_LOCK:
                        credential_store.store("ANM_ANI_RSS_API_KEY", key, STATE_DIR)
                    invalidate_health_cache()
                    self.json_response({"configured": True, "persistence": "state"})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/connections/subtitles/credentials":
                try:
                    request = self.read_json()
                    fields = (("assrt", "ASSRT_API_TOKEN"), ("opensubtitles", "OPEN_SUBTITLES_API_KEY"))
                    values = {
                        environment: str(request.get(field) or "").strip()
                        for field, environment in fields
                        if str(request.get(field) or "").strip()
                    }
                    if not values:
                        raise ValueError("at least one subtitle credential is required")
                    with SETTINGS_WRITE_LOCK:
                        credential_store.store_many(values, STATE_DIR)
                    invalidate_health_cache()
                    changed = [field for field, environment in fields if environment in values]
                    self.json_response({"configured": changed, "persistence": "state"})
                except (ValueError, OSError, json.JSONDecodeError) as exc:
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
    Handler.background_budget = background_budget
    Handler.set_runtime_health = staticmethod(set_runtime_health)
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



def _startup_image_source_status(db_path: Path, config: dict[str, Any], *, timeout: float = 3.0) -> dict[str, int]:
    network = config.get("metadata", {}).get("network", {})
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            row = db.execute(
                "SELECT bgm_id FROM anime_work WHERE bgm_id IS NOT NULL ORDER BY start_month DESC,id DESC LIMIT 1"
            ).fetchone()
        bgm_id = int(row[0]) if row else 2
    except (OSError, ValueError, sqlite3.Error):
        bgm_id = 2
    bucket = str(bgm_id)[0]
    cache_bases = list(dict.fromkeys([
        *(str(value).rstrip("/") for value in network.get("bangumiSubjectCacheEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("bangumi_subject_cache")),
    ]))
    api_bases = list(dict.fromkeys([
        *(str(value).rstrip("/") for value in network.get("bangumiApiEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("bangumi_api")),
    ]))
    subject_urls = [f"{base}/{bucket}/{bgm_id}.json" for base in cache_bases]
    subject_urls.extend(f"{base}/v0/subjects/{bgm_id}" for base in api_bases)

    def subject_probe(url: str) -> tuple[bool, list[str]]:
        try:
            response = transport.request("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                         timeout=timeout, max_bytes=4 * 1024 * 1024)
            payload = json.loads(response.content.decode("utf-8"))
            return True, _cover_urls_from_subject(payload)
        except (OSError, ValueError, RuntimeError, httpx.HTTPError, json.JSONDecodeError):
            return False, []

    subject_ok = 0
    cover_urls: list[str] = []
    if subject_urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(subject_urls)), thread_name_prefix="anm-startup-subject") as pool:
            for ok, covers in pool.map(subject_probe, subject_urls):
                subject_ok += int(ok)
                if covers and not cover_urls:
                    cover_urls = covers

    image_bases = list(dict.fromkeys([
        *(str(value).rstrip("/") for value in network.get("bangumiImageEndpoints") or []),
        *(item.base_url for item in network_registry.for_service("bangumi_image")),
    ]))
    image_urls: list[str] = []
    if cover_urls:
        original = cover_urls[0]
        parsed = urllib.parse.urlparse(original)
        for base in image_bases:
            target = urllib.parse.urlparse(base)
            image_urls.append(urllib.parse.urlunparse((target.scheme, target.netloc, parsed.path, parsed.params,
                                                       parsed.query, parsed.fragment)))

    def image_probe(url: str) -> bool:
        try:
            response = transport.request(
                "GET", url, headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/jpeg,image/png"},
                timeout=timeout, max_bytes=12 * 1024 * 1024,
            )
            network_validators.image_bytes(response.content, response.headers.get("content-type", ""))
            return True
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            return False

    image_ok = 0
    if image_urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(image_urls)), thread_name_prefix="anm-startup-image") as pool:
            image_ok = sum(1 for ok in pool.map(image_probe, image_urls) if ok)
    return {"subjectOk": subject_ok, "subjectTotal": len(subject_urls),
            "imageOk": image_ok, "imageTotal": len(image_urls)}


def _startup_self_check(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    network = config.get("metadata", {}).get("network", {})
    route_target = next(iter(network.get("bangumiApiEndpoints") or []), "https://api.bgm.tv")
    result: dict[str, Any] = {"proxy": transport.proxy_route(str(route_target))}

    qbt = config.get("components", {}).get("downloadClient", {})
    qbt_endpoint = str(qbt.get("endpoint") or "").strip()
    if qbt_endpoint:
        try:
            result["qbittorrent"] = connectivity.probe("qbittorrent", qbt_endpoint)
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            result["qbittorrent"] = {"reachable": False, "authenticated": False, "message": "connection_failed"}
    else:
        result["qbittorrent"] = {"message": "not_configured"}

    ani_settings = config.get("components", {}).get("aniRss", {})
    ani_endpoint = str(ani_settings.get("endpoint") or "").strip()
    if ani_endpoint:
        result["aniRss"] = ani_rss.probe(config)
    else:
        result["aniRss"] = {"message": "not_configured"}

    try:
        result["storage"] = storage_preflight.check_config(config, timeout=3.0)
    except (OSError, ValueError, RuntimeError):
        result["storage"] = {}
    result["images"] = _startup_image_source_status(db_path, config, timeout=3.0)
    return result


def _connection_summary(value: dict[str, Any]) -> str:
    message = str(value.get("message") or "")
    if value.get("authenticated"):
        version = str(value.get("version") or "").strip()
        normalized_version = version if version.casefold().startswith("v") else (f"v{version}" if version else "")
        return "ready" + (f" ({normalized_version})" if normalized_version else "")
    if message == "credentials_required":
        return "credentials not configured"
    if message == "not_configured":
        return "not configured"
    if value.get("reachable"):
        return "reachable, authentication failed"
    return "unreachable"


def _proxy_summary(value: dict[str, Any]) -> str:
    mode = str(value.get("mode") or "direct")
    labels = {
        "direct": "Direct",
        "environment_proxy": "Environment proxy",
        "windows_system_proxy": "Windows system proxy",
        "system_proxy": "System proxy",
    }
    label = labels.get(mode, mode)
    proxy = str(value.get("proxy") or "")
    return f"{label} ({proxy})" if proxy else label


def _storage_summary(values: dict[str, dict[str, object]]) -> str:
    parts: list[str] = []
    for item in values.values():
        name = str(item.get("name") or "Storage")
        state = str(item.get("state") or "unknown")
        path = str(item.get("path") or "").strip()
        labels = {
            "available": "ready", "not_configured": "not configured", "permission_denied": "permission denied",
            "host_unreachable": "unreachable", "mount_failed": "unreachable", "authentication_failed": "authentication failed",
        }
        status = labels.get(state, state.replace("_", " "))
        parts.append(f"{name} {path} [{status}]" if path else f"{name} [{status}]")
    return "; ".join(parts) if parts else "not configured"

def _print_access_info(host: str, port: int, config: dict[str, Any], db_path: Path, *,
                       instance_seed: str | None = None,
                       archive_meta: dict[str, Any] | None = None,
                       record_count: int | None = None,
                       self_check: dict[str, Any] | None = None,
                       preload: dict[str, Any] | None = None) -> None:
    lines: list[tuple[str, str]] = [*_browser_urls(host, port)]
    if Path("/.dockerenv").exists() and not os.getenv("ANM_PUBLIC_URL", "").strip():
        lines.append(("Docker LAN URL", f"http://<Docker-host-LAN-IP>:{port}"))
    stored = _catalog_access_metadata(db_path)
    seed = str(instance_seed or stored.get("instance_random_seed") or "").strip()
    if seed:
        lines.append(("Random seed", seed))
    archive_meta = archive_meta or {}
    archive_name = str(archive_meta.get("name") or stored.get("archive_name") or "").strip()
    if archive_name:
        lines.append(("Bangumi Archive", archive_name))
    count = record_count if record_count is not None else stored.get("record_count")
    if count not in {None, ""}:
        with contextlib.suppress(TypeError, ValueError):
            lines.append(("Catalog works", f"{int(count):,}"))

    checks = self_check or {}
    if checks:
        lines.append(("Network route", _proxy_summary(dict(checks.get("proxy") or {}))))
        lines.append(("Ani-RSS", _connection_summary(dict(checks.get("aniRss") or {}))))
        lines.append(("qBittorrent", _connection_summary(dict(checks.get("qbittorrent") or {}))))
        lines.append(("Storage", _storage_summary(dict(checks.get("storage") or {}))))
        images = dict(checks.get("images") or {})
        subject_ok, subject_total = int(images.get("subjectOk", 0)), int(images.get("subjectTotal", 0))
        image_ok, image_total = int(images.get("imageOk", 0)), int(images.get("imageTotal", 0))
        lines.append(("Image sources", f"subject {subject_ok}/{subject_total}; cover {image_ok}/{image_total}"))
    if preload is not None:
        lines.append(("Image preload", "started: landing pages -> recent 6 months -> older catalog"))

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
          print_access_info: bool = True) -> bool:
    startup_started_monotonic = time.monotonic()
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
    application_update.reconcile_upgrade_state()
    network_connectivity.reset()
    # Load persisted Web-managed credentials before spawning the image worker.
    # The child process inherits only the environment that exists at spawn time;
    # per-task image configuration below keeps later credential changes current.
    credential_store.load_into_environment(STATE_DIR)
    config_store = ConfigStore(config_path, EXAMPLE_CONFIG)
    image_fetcher = ImageFetcher(db_path)
    image_fetcher.start()
    access_reported = threading.Event()
    restart_requested = threading.Event()

    def warmup_started(details: dict[str, Any]) -> None:
        if not print_access_info or access_reported.is_set():
            return
        config = config_store.read()
        checks = _startup_self_check(db_path, config)
        _print_access_info(host, port, config, db_path, self_check=checks, preload=details)
        access_reported.set()

    handler = make_handler(
        db_path, config_store,
        submission_enabled=submission_enabled, plan_dir=plan_dir,
        image_fetcher=image_fetcher, warmup_ready=warmup_ready,
        warmup_started_callback=warmup_started, start_warmup=False,
        startup_started_monotonic=startup_started_monotonic,
        restart_callback=restart_requested.set,
    )
    server = ThreadingHTTPServer((host, port), handler)
    stop_monitor = threading.Event()
    handler.recover_interrupted_submissions()
    handler.recover_interrupted_maintenance()

    def monitor_network() -> None:
        last_prewarm = 0.0
        last_probe = 0.0
        interactive_probe_defer = min(120.0, network_connectivity.MAX_FAILED_PROBE_GAP_SECONDS * .65)
        while not stop_monitor.is_set() and not restart_requested.is_set():
            try:
                now = time.monotonic()
                offline = network_connectivity.is_offline()
                interactive = bool(handler.background_budget.snapshot().get("interactive"))
                should_probe = offline or not interactive or now - last_probe >= interactive_probe_defer
                if should_probe:
                    current = config_store.read()
                    probe_started = time.monotonic()
                    last_probe = probe_started
                    try:
                        online = network_diagnostics.connectivity_probe(db_path, current, timeout=2.5)
                    except Exception:
                        # A recovery verdict must not depend on Catalog/config health; a neutral canary
                        # remains available even if the normal probe path itself is damaged.
                        if not offline:
                            raise
                        online = network_diagnostics.internet_canary_probe(timeout=2.5)
                    state = network_connectivity.note_probe(online, started_at=probe_started)
                    now = time.monotonic()
                    confirmed_offline = network_connectivity.is_offline()
                    image_fetcher.set_network_state(
                        offline=confirmed_offline, suppress_learning=not network_connectivity.failure_learning_allowed())
                    if online and not confirmed_offline and (
                        bool(state.get("recovered")) or now - last_prewarm >= 15 * 60
                    ):
                        with handler.background_budget.lease("network", stop_event=stop_monitor) as allowed:
                            if allowed and not network_connectivity.is_offline():
                                network_diagnostics.prewarm(db_path, current, timeout=2.5)
                                last_prewarm = time.monotonic()
                else:
                    image_fetcher.set_network_state(
                        offline=offline, suppress_learning=not network_connectivity.failure_learning_allowed())
            except Exception as exc:
                # Probe implementation/configuration errors are not evidence of a network outage.
                log_event("WARNING", "network_monitor_probe_failed", errorType=type(exc).__name__)
                # Always converge the isolated image worker to the parent state. Otherwise a
                # persistent config/probe error immediately after recovery could leave stale
                # worker-local offline suppression behind.
                with contextlib.suppress(Exception):
                    image_fetcher.set_network_state(
                        offline=network_connectivity.is_offline(),
                        suppress_learning=not network_connectivity.failure_learning_allowed(),
                    )
            interval = 60.0 if network_connectivity.is_offline() else 30.0
            if stop_monitor.wait(interval):
                break

    def monitor_qbt() -> None:
        while not stop_monitor.is_set():
            try:
                if background_ready is None or background_ready.is_set():
                    current = config_store.read()
                    configured = current.get("components", {}).get("downloadClient", {}).get("submissionEnabled")
                    if bool(submission_enabled if configured is None else configured):
                        with DATABASE_MAINTENANCE_LOCK:
                            refreshed = qbt_runtime.refresh(db_path, current["components"]["downloadClient"]["endpoint"],
                                                            current["components"]["downloadClient"]["category"])
                            handler.set_runtime_health("qbittorrent", status="normal", detail="ready")
                            completed_ids = [int(value) for value in refreshed.get("completedAnimeIds", [])]
                            if completed_ids:
                                library_audit.audit(db_path, current, anime_ids=completed_ids,
                                                    throttle=lambda: time.sleep(0.08))
            except Exception as exc:
                handler.set_runtime_health("qbittorrent", status="warning", detail="unavailable")
                log_event("ERROR", "qbt_state_refresh_failed", error=f"{type(exc).__name__}: {exc}")
            if stop_monitor.wait(10):
                break

    def monitor_ani_rss() -> None:
        """Keep Ani-RSS API state, resources, and optional mounted media on one scheduler."""
        media_scan_thread: threading.Thread | None = None
        resource_scan_thread: threading.Thread | None = None
        next_resource_scan_at = 0.0
        resource_scan_epoch = 0

        def start_media_scan(source: dict[str, Any]) -> None:
            nonlocal media_scan_thread
            if media_scan_thread is not None and media_scan_thread.is_alive():
                return

            def run() -> None:
                with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                    external_library.scan(db_path, [source])

            media_scan_thread = threading.Thread(
                target=run, daemon=True, name="anm-ani-rss-media-scan")
            media_scan_thread.start()

        def start_resource_scan(current: dict[str, Any]) -> bool:
            nonlocal resource_scan_thread, next_resource_scan_at
            if resource_scan_thread is not None and resource_scan_thread.is_alive():
                return False
            scan_epoch = resource_scan_epoch

            def run() -> None:
                nonlocal next_resource_scan_at
                try:
                    scan_started_at = time.monotonic()
                    result = ani_rss.refresh_background_resources(
                        db_path, current, stop_event=stop_monitor, abort_event=ANI_RSS_USER_ACTIVITY)
                    # Only an actually acquired scan advances the cadence. If the
                    # shared lease is busy, keep this pass due so the scheduler
                    # retries after the previous scan finishes instead of losing a
                    # whole poll interval.
                    if result.get("started") and scan_epoch == resource_scan_epoch:
                        poll_minutes = max(5, int(
                            current.get("components", {}).get("discovery", {}).get("pollMinutes", 30)))
                        retry_minutes = min(poll_minutes, 5) if result.get("failed") else poll_minutes
                        next_resource_scan_at = scan_started_at + retry_minutes * 60.0
                    if result.get("started") and (result.get("refreshed") or result.get("failed")):
                        log_event("INFO" if not result.get("failed") else "WARNING",
                                  "ani_rss_resource_refresh_complete",
                                  refreshed=int(result.get("refreshed", 0)),
                                  failed=int(result.get("failed", 0)))
                except (OSError, ValueError, RuntimeError, sqlite3.Error, urllib.error.URLError):
                    # Optional discovery must never affect the API snapshot, local
                    # Torrent planning, image fallback, or Web availability.
                    log_event("WARNING", "ani_rss_resource_refresh_failed")

            resource_scan_thread = threading.Thread(
                target=run, daemon=True, name="anm-ani-rss-resource-refresh")
            resource_scan_thread.start()
            return True

        while not stop_monitor.is_set() and not restart_requested.is_set():
            interval = 30.0
            try:
                current = config_store.read()
                settings = current.get("components", {}).get("aniRss", {}) or {}
                interval = min(60.0, max(15.0, float(settings.get("syncMinutes", 30)) * 15.0))
                result: dict[str, Any] | None = None
                if ani_rss.sync_due(db_path, current) and not ANI_RSS_USER_ACTIVITY.is_set():
                    # Background API work never queues ahead of an explicit user request.
                    acquired = ANI_RSS_OPERATION_LOCK.acquire(blocking=False)
                    if acquired:
                        try:
                            if (not stop_monitor.is_set() and not ANI_RSS_USER_ACTIVITY.is_set()
                                    and ani_rss.sync_due(db_path, current)):
                                result = ani_rss.sync(db_path, current, abort_event=ANI_RSS_USER_ACTIVITY)
                        finally:
                            ANI_RSS_OPERATION_LOCK.release()
                if result and result.get("resourceRefreshRequired"):
                    # Endpoint/API-key changes invalidate the search overlay. Keep
                    # discovery due until the replacement Ani-RSS generation is
                    # healthy, then repopulate it immediately rather than waiting
                    # for a stale cadence deadline from the previous instance. An
                    # older in-flight scan cannot advance this new generation's
                    # deadline when it eventually returns.
                    resource_scan_epoch += 1
                    next_resource_scan_at = 0.0
                if result and result.get("state") == "ready":
                    log_event("INFO", "ani_rss_sync_complete",
                              subscriptions=int(result.get("subscriptions", 0)),
                              mediaItems=int(result.get("mediaItems", 0)),
                              snapshotComplete=bool(result.get("snapshotComplete", False)))

                # Resource discovery is scheduled independently from image warm-up.
                # This closes the first-start race where Ani-RSS becomes ready just
                # after warm-up took its initial health snapshot. Per-work due state
                # and the shared non-blocking lease prevent duplicate/overlap scans.
                now_mono = time.monotonic()
                if now_mono >= next_resource_scan_at:
                    ani_state = ani_rss.state(db_path, current)
                    if (ani_rss.state_available(ani_state)
                            and str(ani_state.get("effective_mode") or "manual") in {"prefer", "fallback"}):
                        start_resource_scan(current)

                # The mounted Ani-RSS directory is useful even when its HTTP API is
                # unconfigured or temporarily down. Scan it independently without
                # blocking the API cadence; a still-running pass is never queued.
                source = ani_rss.media_source(current)
                if source.get("enabled") and source.get("path"):
                    start_media_scan(source)
            except Exception as exc:
                # Ani-RSS is optional: a dead/misconfigured endpoint must never
                # block the Web server, image fallback, torrent scans or playback.
                log_event("WARNING", "ani_rss_sync_failed", errorType=type(exc).__name__)
            if stop_monitor.wait(interval):
                break

    def monitor_application_updates() -> None:
        while not stop_monitor.is_set() and not restart_requested.is_set():
            try:
                if network_connectivity.is_offline():
                    if stop_monitor.wait(30):
                        break
                    continue
                current = config_store.read()
                if application_update.automatic_check_due(current):
                    with handler.background_budget.lease("network", stop_event=stop_monitor) as allowed:
                        if not allowed:
                            break
                        if network_connectivity.is_offline():
                            continue
                        local_now = dt.datetime.now().astimezone()
                        automatic = application_update._automatic_settings(current)
                        mode = str(automatic.get("mode") or "notify")
                        release = application_update.status(force=True)
                        latest = str(release.get("latestVersion") or "")
                        if mode == "install" and release.get("updateAvailable") and release.get("canUpdate"):
                            application_update.record_automatic_result(
                                date=local_now.date().isoformat(), mode=mode, status_value="installing", latest_version=latest)
                            application_update.apply(host=host, port=int(server.server_port))
                            restart_requested.set()
                            break
                        state = "available" if release.get("updateAvailable") else "latest"
                        if release.get("updateAvailable") and not release.get("canUpdate"):
                            state = "unavailable"
                        application_update.record_automatic_result(
                            date=local_now.date().isoformat(), mode=mode, status_value=state, latest_version=latest,
                            message=str(release.get("reason") or ""))
            except Exception as exc:
                local_now = dt.datetime.now().astimezone()
                try:
                    current = config_store.read()
                    mode = str(application_update._automatic_settings(current).get("mode") or "notify")
                    if application_update._automatic_settings(current).get("enabled"):
                        application_update.record_automatic_result(
                            date=local_now.date().isoformat(), mode=mode, status_value="failed", message=type(exc).__name__)
                except (OSError, ValueError, RuntimeError):
                    pass
                log_event("WARNING", "application_update_check_failed", errorType=type(exc).__name__)
            if stop_monitor.wait(30):
                break

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="anm-web-server")
    server_thread.start()
    # The listening socket is already bound; verify that the serving loop is accepting
    # connections before starting bootstrap/warmup work or publishing access details.
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((probe_host, server.server_port), timeout=.25):
                break
        except OSError:
            time.sleep(.05)
    else:
        server.shutdown(); server.server_close(); image_fetcher.close()
        raise RuntimeError("Web service failed to become reachable")

    # Container bootstrap starts only after schema migrations and Web startup finish;
    # this keeps the initial shell responsive while the first Archive is built.
    if ready_callback is not None:
        ready_callback()
    threading.Thread(target=monitor_ani_rss, daemon=True, name="anm-ani-rss-sync-monitor").start()
    handler.catalog_warmup.start()
    threading.Thread(target=monitor_network, daemon=True, name="anm-network-monitor").start()
    threading.Thread(target=monitor_qbt, daemon=True, name="anm-qbt-state-monitor").start()
    threading.Thread(target=monitor_application_updates, daemon=True, name="anm-application-update-monitor").start()
    try:
        while server_thread.is_alive() and not restart_requested.is_set():
            server_thread.join(.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        handler.catalog_warmup.close()
        set_active_background_budget(None)
        server.shutdown()
        server_thread.join(2)
        server.server_close()
        image_fetcher.close()
    return restart_requested.is_set()


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
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve_parser.add_argument("--submission-enabled", action=argparse.BooleanOptionalAction, default=True)
    demo_parser = sub.add_parser("demo", help="build the sample and serve it")
    build_args(demo_parser)
    demo_parser.add_argument("--host", default="0.0.0.0")
    demo_parser.add_argument("--port", type=int, default=8787)
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
