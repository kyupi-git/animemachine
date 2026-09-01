"""Evidence-based completeness filtering for Torrent Collector."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from ..config.loader import load_resource_group_catalog
from .title_identity import norm, queries

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = PACKAGE_ROOT / "resources" / "config.example.json"

_FLAGS = re.IGNORECASE
ARCHIVE_SOURCE_RE = re.compile(r"(?i)(?:\bbd[ ._-]*(?:rip|remux|box)\b|\bbdmv\b|\bblu[ ._-]*ray\b|\bdvd[ ._-]*(?:rip|iso)\b|\bremux\b|\buhd\b|\biso\b)")
SERIAL_SOURCE_RE = re.compile(r"(?i)(?:\bweb[ ._-]*(?:dl|rip)\b|\bwebrip\b|\btv[ ._-]*rip\b|\bhdtv\b|\bbaha\b|\bcrunchyroll\b|\bweekly\b)")
MOVIE_RE = re.compile(r"(?i)(?:\bmovies?\b|\bfilms?\b|\btheatrical\b|\bgekijouban\b|\bgekijoban\b|\beiga\b|剧场版|劇場版|劇場アニメ|剧场动画|劇場動畫|动画电影|動畫電影|电影|電影|映画|(?<![A-Za-z0-9])M\d{1,3}(?![A-Za-z0-9]))")
COMPLETE_RE = re.compile(r"(?i)(?:\bcomplete(?:[ ._-]+(?:series|season|collection|bd[ ._-]*box))?\b|\bbatch\b|\bfin\b|(?<=\d)\s*fin\b|[\[【(（]\s*final\s*[\]】)）]|(?<=\d)\s*end\b|全集|合集|全套|全卷|全巻|全\s*\d+\s*(?:集|話|话|巻|卷)|bd[ ._-]*box)")
ALL_COMPLETE_RE = re.compile(
    r"(?i)(?:(?:vol(?:ume)?|disc|disk)[ ._-]*0*\d{1,3}\s*[-~～—–_+]\s*(?:(?:vol(?:ume)?|disc|disk)[ ._-]*)?0*\d{1,3}\s+all\b|"
    r"(?<!\d)0*\d{1,4}\s*[-~～—–_+]\s*0*\d{1,4}\s+all\b|[\[【(（]\s*all\s*[\]】)）])"
)
TERMINAL_RE = re.compile(r"(?i)(?:\bfin\b|(?<=\d)\s*fin\b|[\[【(（]\s*final\s*[\]】)）]|(?<=\d)\s*end\b|[\[【(（]\s*(?:end|完)\s*[\]】)）])")
PARTIAL_RE = re.compile(r"(?i)(?:missing[ ._-]*(?:episodes?|eps?)|(?:episodes?|eps?)[ ._-]*missing|缺集|缺失集数|缺失集數)")
PATCH_ONLY_RE = re.compile(r"(?i)(?:\bpatch[ ._-]*only\b|\bfix[ ._-]*only\b|\bservice[ ._-]*pack\b|字幕补丁|字幕補丁|补丁包|補丁包|仅补丁|僅補丁)")
EXTRA_RE = re.compile(r"(?:(?i:\b(?:special|ova|oad|ona|ncop|nced|ost|scans?|booklet|fonts?|menu|bonus|live)(?:[ ._-]*#?\d{1,3})?\b)|\b(?:SP|OP|ED|CM|PV|CD)(?:[ ._-]*#?\d{1,3})?\b)")
SEASON_RE = re.compile(r"(?i)(?:\bS\d{1,2}\b|\bseason[ ._-]*\d{1,2}\b|\bpart[ ._-]*\d{1,2}\b|\bcour[ ._-]*\d{1,2}\b|第?\s*[一二三四五六七八九十百0-9]+\s*(?:季|期))")
SEASON_RANGE_RE = re.compile(r"(?i)\bS\s*0*(\d{1,2})\s*[-~～—–+]\s*S?\s*0*(\d{1,2})\b")
MOVIE_RANGE_RE = re.compile(r"(?i)\bMovie\s*0*(\d{1,3})\s*[-~～—–+]\s*(?:Movie\s*)?0*(\d{1,3})\b")
VOL_RANGE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:vol(?:ume)?|disc|disk)[ ._-]*0*(\d{1,3})\s*[-~～—–_+]|第?\s*0*(\d{1,3})\s*(?:巻|卷)\s*[-~～—–_+]")
VOL_RANGE_FULL_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:vol(?:ume)?|disc|disk)[ ._-]*0*(\d{1,3})\s*[-~～—–_+]\s*(?:(?:vol(?:ume)?|disc|disk)[ ._-]*)?0*(\d{1,3})(?!\d)|第?\s*0*(\d{1,3})\s*(?:巻|卷)\s*[-~～—–_至到]\s*第?\s*0*(\d{1,3})\s*(?:巻|卷)")
VOL_SINGLE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:vol(?:ume)?|disc|disk)[ ._-]*0*(\d{1,3})(?!\s*[-~～—–_+]\s*(?:vol(?:ume)?|disc|disk)?\s*\d)(?!\d)|第?\s*0*(\d{1,3})\s*(?:巻|卷)(?!\s*[-~～—–_+至到])")
LABELED_RANGE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<label>tv|ep(?:isode)?|ova|oad|ona|sp)[ ._-]*0*(?P<a>\d{1,4})\s*[-~～—–_至到]\s*(?:(?:tv|ep(?:isode)?|ova|oad|ona|sp)[ ._-]*)?0*(?P<b>\d{1,4})(?!\d)")
SEASON_EP_RANGE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])S\d{1,2}[ ._-]*E0*(\d{1,4})\s*[-~～—–_+]\s*(?:S\d{1,2}[ ._-]*E)?0*(\d{1,4})(?!\d)")
BARE_RANGE_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[-~～—–_至到]\s*(\d{1,4})(?!\d)")
EP_SINGLE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:S\d{1,2}[ ._-]*E|EP(?:ISODE)?[ ._-]*|E[._-]?)(\d{1,4})(?!\s*[-~～—–_+]\s*\d)(?![A-Za-z0-9])|第\s*0*(\d{1,4})\s*(?:集|話|话)")
BRACKET_SINGLE_RE = re.compile(r"[\[【(（]\s*0*(\d{1,3})(?:\.5)?\s*(?:END|FIN|完)?\s*[\]】)）]", re.I)
DASH_SINGLE_RE = re.compile(r"(?<!\d)[-–—]\s*0*(\d{1,3})(?:\.5)?(?:v\d+)?(?=\s*(?:[\[【(（]|$))", re.I)
EP_LIST_RE = re.compile(r"(?i)(?:\bepisodes?\s*|\beps?\s*|(?<![A-Za-z0-9])[-–—]\s*)0*\d{1,4}(?:\s*[,，、&＆]\s*0*\d{1,4}){2,}")
DUAL_RANGE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*[-~～—–_]\s*(\d{1,3})\s*[（(]\s*(\d{1,3})\s*[-~～—–_]\s*(\d{1,3})\s*[）)]")
TECH_RANGE_CONTEXT_RE = re.compile(r"(?i)(?:\d{3,4}x\d{3,4}|\b(?:720|1080|1440|2160|4320)p\b|\b(?:8|10|12)bit\b|\bx26[45]\b|\bh\.?26[45]\b|\bav1\b|\b\d\.\d\b|\b(?:19|20)\d{2}[.-]\d{1,2}[.-]\d{1,2}\b)")
COLLECTOR_SUFFIX_RE = re.compile(r"\s*\[(?:P|nyaa|mikan|dmhy|acgrip|bangumi|nekobt|tokyotosho)-[0-9A-Za-z._-]{4,}\]\s*\.torrent$", re.I)


@dataclass(frozen=True)
class NumberRange:
    start: int
    end: int
    label: str = "episode"

    @property
    def span(self) -> int:
        return self.end - self.start + 1


@dataclass
class ReleaseEvidence:
    title: str
    normalized_title: str
    kind: str | None
    group: str | None
    episode_ranges: list[NumberRange] = field(default_factory=list)
    volume_ranges: list[NumberRange] = field(default_factory=list)
    season_components: list[str] = field(default_factory=list)
    movie_components: list[str] = field(default_factory=list)
    standalone_episode_tokens: list[int] = field(default_factory=list)
    standalone_volume_tokens: list[int] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    complete_markers: list[str] = field(default_factory=list)
    terminal_markers: list[str] = field(default_factory=list)
    partial_markers: list[str] = field(default_factory=list)
    dual_numbering: list[tuple[NumberRange, NumberRange]] = field(default_factory=list)
    source_class: str = "unknown"
    multi_component: bool = False
    movie: bool = False
    patch_only: bool = False
    episode_list: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogEvidence:
    status: str = "unavailable"  # exact | ambiguous | unavailable | no_match
    anime_id: int | None = None
    expected_episodes: int | None = None
    subject_type: str | None = None
    matched_query: str | None = None
    generation: str | None = None


@dataclass(frozen=True)
class FilterDecision:
    decision: str
    reason: str
    confidence: str
    kind: str | None
    group: str | None
    evidence: dict[str, Any]


class CatalogMatcher:
    """Read-only exact matcher against AnimeMachine's Bangumi Archive catalog."""

    def __init__(self, path: Path | None):
        self.path = path
        self._generation: str | None = None
        self._by_title: dict[str, set[int]] = {}
        self._works: dict[int, tuple[int | None, str | None]] = {}

    @staticmethod
    def _generation_token(path: Path) -> str:
        parts: list[str] = []
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                stat = candidate.stat()
                parts.append(f"{stat.st_mtime_ns}:{stat.st_size}:{getattr(stat, 'st_ino', 0)}")
            except OSError:
                parts.append("missing")
        return "|".join(parts)

    def _refresh(self) -> str | None:
        path = self.path
        if not path or not path.is_file():
            self._generation = None
            self._by_title = {}
            self._works = {}
            return None
        try:
            generation = self._generation_token(path)
            if generation == self._generation and self._by_title:
                return generation
            uri = path.resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=2.0)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=2000")
                by_title: dict[str, set[int]] = {}
                for row in db.execute("SELECT anime_id,title FROM anime_title"):
                    by_title.setdefault(norm(str(row["title"])), set()).add(int(row["anime_id"]))
                work_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(anime_work)")}
                type_column = next((name for name in ("media_type", "media_code", "type", "subject_type", "platform", "category") if name in work_columns), None)
                select_type = f",{type_column}" if type_column else ""
                works: dict[int, tuple[int | None, str | None]] = {}
                for row in db.execute(f"SELECT id,episode_count{select_type} FROM anime_work"):
                    expected = int(row["episode_count"] or 0) if "episode_count" in row.keys() else None
                    subject_type = str(row[type_column]) if type_column and row[type_column] is not None else None
                    works[int(row["id"])] = (expected, subject_type)
            finally:
                db.close()
            self._generation = generation
            self._by_title = by_title
            self._works = works
            return generation
        except (sqlite3.Error, OSError):
            # Do not retain a potentially stale inode/schema indefinitely after an error.
            self._generation = None
            self._by_title = {}
            self._works = {}
            return None

    def generation(self) -> str | None:
        """Return the current read-only Catalog generation, refreshing safely."""
        return self._refresh()

    def match(self, release_title: str, evidence: ReleaseEvidence | None = None) -> CatalogEvidence:
        generation = self._refresh()
        if not generation:
            return CatalogEvidence(status="unavailable")
        candidate_ids: set[int] = set()
        matched_query: str | None = None
        partial = bool(evidence and (evidence.episode_ranges or evidence.volume_ranges or evidence.standalone_episode_tokens))
        for query in queries(release_title, partial=partial):
            ids = self._by_title.get(norm(query), set())
            if len(ids) == 1:
                candidate_ids.update(ids)
                matched_query = query
            elif len(ids) > 1:
                candidate_ids.update(ids)
        if not candidate_ids:
            return CatalogEvidence(status="no_match", generation=generation)
        if len(candidate_ids) != 1:
            return CatalogEvidence(status="ambiguous", generation=generation)
        anime_id = next(iter(candidate_ids))
        work = self._works.get(anime_id)
        if work is None:
            return CatalogEvidence(status="no_match", generation=generation)
        expected, subject_type = work
        return CatalogEvidence(
            status="exact",
            anime_id=anime_id,
            expected_episodes=expected,
            subject_type=subject_type,
            matched_query=matched_query,
            generation=generation,
        )


def _load_group_policy() -> tuple[list[dict[str, Any]], list[list[Any]]]:
    data = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    policy = data["torrentPolicy"]
    archive_ids = {str(item).casefold() for item in policy.get("archiveGroupIds", [])}
    archive = [item for item in policy.get("resourceGroups", []) if str(item.get("id", "")).casefold() in archive_ids and item.get("enabled", True)]
    serial = load_resource_group_catalog().get("serialProfiles", {}).get("zh", [])
    return archive, serial


ARCHIVE_GROUPS, SERIAL_RULES = _load_group_policy()


@lru_cache(maxsize=512)
def _alias_pattern(value: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in re.split(r"[ ._-]+", value.strip()) if part]
    return re.compile(r"[ ._-]+".join(parts), re.I)


def _release_regions(title: str) -> list[str]:
    regions: list[str] = []
    for raw in re.findall(r"[\[【〖〔]([^\]】〗〕]{1,180})[\]】〗〕]", title):
        regions.append(raw.strip())
        regions.extend(part.strip() for part in re.split(r"\s*(?:&|＆|\+|＋|×|\||/|／)\s*", raw) if part.strip())
    stripped = title.strip()
    # Tolerate malformed/missing group brackets only at the release prefix.
    # Do not split on '-' because it is part of canonical names such as ANK-Raws.
    if stripped and stripped[0] not in "[【〖〔":
        cuts = [
            pos for token in ("[", "【", "〖", "〔", "]", "】", "〗", "〕", ":", "：", "|", "｜")
            if 0 < (pos := stripped.find(token)) <= 120
        ]
        if cuts:
            prefix = stripped[:min(cuts)].strip(" \t★☆[]【】〖〗〔〕")
        else:
            prefix = stripped[:120].strip(" \t★☆[]【】〖〗〔〕")
        if prefix:
            regions.append(prefix)
            regions.extend(part.strip() for part in re.split(r"\s*(?:&|＆|\+|＋|×|\||/|／)\s*", prefix) if part.strip())
    return list(dict.fromkeys(regions))


def classify_title(title: str) -> tuple[str | None, str | None]:
    regions = _release_regions(title)
    archive_group: str | None = None
    for item in ARCHIVE_GROUPS:
        aliases = [str(item.get("name", "")), *map(str, item.get("aliases", []))]
        if any(any(_alias_pattern(alias).search(region) for alias in aliases if alias) for region in regions):
            archive_group = str(item["name"])
            break
    serial_group: str | None = None
    for _rule_id, display_name, group_keywords, subtitle_keywords in SERIAL_RULES:
        if not any(any(_alias_pattern(str(keyword)).search(region) for keyword in group_keywords if keyword != "*") for region in regions):
            continue
        if any(str(token).casefold() in title.casefold() for token in subtitle_keywords if token != "*") or "*" in subtitle_keywords:
            serial_group = str(display_name)
            break
    if archive_group:
        if SERIAL_SOURCE_RE.search(title) and not ARCHIVE_SOURCE_RE.search(title):
            return ("serial-zh", serial_group) if serial_group else (None, None)
        return "archive", archive_group
    if serial_group:
        return "serial-zh", serial_group
    return None, None


def strip_saved_suffix(value: str) -> str:
    value = value.strip()
    value = COLLECTOR_SUFFIX_RE.sub("", value)
    if value.lower().endswith(".torrent"):
        value = value[:-8]
    return value.strip()


def _valid_range(a: int, b: int, context: str) -> bool:
    if a > b or a > 1500 or b > 1500:
        return False
    if a in {720, 1080, 1440} or b in {720, 1080, 1440}:
        return False
    if 1900 <= a <= 2099 or 1900 <= b <= 2099:
        return False
    if TECH_RANGE_CONTEXT_RE.fullmatch(context.strip()):
        return False
    return True


def parse_release_title(title: str) -> ReleaseEvidence:
    original = strip_saved_suffix(title)
    kind, group = classify_title(original)
    evidence = ReleaseEvidence(
        title=original,
        normalized_title=norm(original),
        kind=kind,
        group=group,
        source_class="archive" if ARCHIVE_SOURCE_RE.search(original) else ("serial" if SERIAL_SOURCE_RE.search(original) else "unknown"),
    )
    evidence.movie = bool(MOVIE_RE.search(original))
    evidence.movie_components = [m.group(0) for m in MOVIE_RE.finditer(original)]
    evidence.complete_markers = [m.group(0) for m in COMPLETE_RE.finditer(original)]
    evidence.complete_markers.extend(m.group(0) for m in ALL_COMPLETE_RE.finditer(original))
    evidence.terminal_markers = [m.group(0) for m in TERMINAL_RE.finditer(original)]
    evidence.patch_only = bool(PATCH_ONLY_RE.search(original))
    if evidence.patch_only:
        evidence.partial_markers.append("patch_only")
    if PARTIAL_RE.search(original):
        evidence.partial_markers.append("missing_episodes")
    evidence.episode_list = bool(EP_LIST_RE.search(original))
    if evidence.episode_list:
        evidence.partial_markers.append("episode_list")
    evidence.extras = [m.group(0) for m in EXTRA_RE.finditer(original)]
    evidence.season_components = [m.group(0) for m in SEASON_RE.finditer(original)]
    evidence.multi_component = bool(re.search(r"\+|＆|&", original)) and bool(evidence.season_components or evidence.extras or evidence.movie)

    occupied: list[tuple[int, int]] = []
    for m in VOL_RANGE_FULL_RE.finditer(original):
        nums = [int(value) for value in m.groups() if value is not None]
        if len(nums) >= 2:
            evidence.volume_ranges.append(NumberRange(nums[0], nums[1], "volume"))
            occupied.append(m.span())
    for m in VOL_SINGLE_RE.finditer(original):
        if any(a <= m.start() < b for a, b in occupied):
            continue
        value = next((int(x) for x in m.groups() if x is not None), None)
        if value is not None:
            evidence.standalone_volume_tokens.append(value)

    for m in DUAL_RANGE_RE.finditer(original):
        a, b, c, d = map(int, m.groups())
        if _valid_range(a, b, m.group(0)) and _valid_range(c, d, m.group(0)) and (b - a) == (d - c):
            local = NumberRange(a, b)
            absolute = NumberRange(c, d)
            evidence.dual_numbering.append((local, absolute))
            evidence.episode_ranges.append(local)
            occupied.append(m.span())

    for rx in (LABELED_RANGE_RE, SEASON_EP_RANGE_RE):
        for m in rx.finditer(original):
            if any(a <= m.start() < b for a, b in occupied):
                continue
            if rx is LABELED_RANGE_RE:
                a, b = int(m.group("a")), int(m.group("b")); label = str(m.group("label")).casefold()
            else:
                a, b = int(m.group(1)), int(m.group(2)); label = "episode"
            if _valid_range(a, b, m.group(0)):
                evidence.episode_ranges.append(NumberRange(a, b, label))
                occupied.append(m.span())

    protected = []
    for rx in (VOL_RANGE_FULL_RE, MOVIE_RANGE_RE, SEASON_RANGE_RE):
        protected.extend(m.span() for m in rx.finditer(original))
    for m in BARE_RANGE_RE.finditer(original):
        if any(a <= m.start() < b for a, b in occupied + protected):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        prefix = original[max(0, m.start() - 20):m.start()]
        suffix = original[m.end():m.end() + 16]
        if re.search(r"(?i)(?:x|h\.?|vol(?:ume)?|disc|disk|season|part|cour|movie|film|\bS)\s*$", prefix):
            continue
        if re.match(r"(?i)\s*(?:p\b|bit|bits|ch|hz|khz|fps)", suffix):
            continue
        if _valid_range(a, b, m.group(0)):
            evidence.episode_ranges.append(NumberRange(a, b))
            occupied.append(m.span())

    # Context-aware singles: extras such as S1+OAD1+OAD2 are components, not
    # the release unit. Numbered movie/season/disc/CD tokens are excluded.
    for rx in (EP_SINGLE_RE, BRACKET_SINGLE_RE, DASH_SINGLE_RE):
        for m in rx.finditer(original):
            if any(a <= m.start() < b for a, b in occupied + protected):
                continue
            value = next((int(x) for x in m.groups() if x is not None), None)
            if value is None or value in {720, 1080} or 1900 <= value <= 2099:
                continue
            around = original[max(0, m.start() - 14):m.end() + 8]
            if re.search(r"(?i)(?:movie|film|M|S|season|part|cour|disc|cd)\s*0*" + str(value) + r"\b", around):
                continue
            evidence.standalone_episode_tokens.append(value)
    evidence.standalone_episode_tokens = sorted(set(evidence.standalone_episode_tokens))
    evidence.standalone_volume_tokens = sorted(set(evidence.standalone_volume_tokens))
    return evidence



def _has_strong_complete(evidence: ReleaseEvidence) -> bool:
    return any(marker.strip(" []【】()（）").casefold() != "batch" for marker in evidence.complete_markers)

def _coverage_complete(rng: NumberRange, expected: int, evidence: ReleaseEvidence) -> bool:
    if expected <= 0:
        return False
    if rng.start == 1 and rng.end == expected:
        return True
    if rng.start == 0 and rng.end == expected:
        return True  # episode 0 is treated as an extra/pre-air unit
    if rng.span == expected and rng.start > 1 and evidence.season_components:
        return True  # absolute-numbered independent season/part
    return False


def decide_title(evidence: ReleaseEvidence, catalog: CatalogEvidence | None = None) -> FilterDecision:
    catalog = catalog or CatalogEvidence()
    base = {"title": evidence.to_dict(), "catalog": asdict(catalog)}
    if evidence.kind is None:
        return FilterDecision("reject", "source_or_group_not_eligible", "high", None, None, base)
    if "missing_episodes" in evidence.partial_markers:
        return FilterDecision("reject", "missing_episodes", "high", evidence.kind, evidence.group, base)
    if evidence.episode_list:
        return FilterDecision("reject", "episode_list", "high", evidence.kind, evidence.group, base)
    if evidence.patch_only:
        return FilterDecision("reject", "patch_only", "high", evidence.kind, evidence.group, base)
    if evidence.movie:
        return FilterDecision("defer", "movie_requires_manifest", "medium", evidence.kind, evidence.group, base)

    if evidence.standalone_volume_tokens:
        return FilterDecision("reject", "single_volume", "high", evidence.kind, evidence.group, base)
    if evidence.volume_ranges:
        if _has_strong_complete(evidence):
            return FilterDecision("accept", "explicit_complete_volume_range", "high", evidence.kind, evidence.group, base)
        if evidence.complete_markers:
            return FilterDecision("defer", "volume_batch_needs_total", "medium", evidence.kind, evidence.group, base)
        return FilterDecision("reject", "partial_volume_range", "high", evidence.kind, evidence.group, base)

    if evidence.standalone_episode_tokens and not evidence.multi_component:
        if catalog.status == "exact" and catalog.expected_episodes == 1:
            return FilterDecision("accept", "catalog_one_shot", "high", evidence.kind, evidence.group, base)
        return FilterDecision("reject", "single_episode", "high", evidence.kind, evidence.group, base)

    if evidence.episode_ranges:
        primary = evidence.episode_ranges[0]
        if catalog.status == "exact" and catalog.expected_episodes:
            if _coverage_complete(primary, catalog.expected_episodes, evidence):
                return FilterDecision("accept", "catalog_coverage_complete", "high", evidence.kind, evidence.group, base)
            if primary.span < catalog.expected_episodes or (primary.start == 1 and primary.end < catalog.expected_episodes):
                return FilterDecision("reject", "catalog_coverage_incomplete", "high", evidence.kind, evidence.group, base)
            return FilterDecision("defer", "catalog_coverage_conflict", "medium", evidence.kind, evidence.group, base)
        if _has_strong_complete(evidence):
            return FilterDecision("accept", "explicit_complete_episode_range", "high", evidence.kind, evidence.group, base)
        if evidence.complete_markers:
            return FilterDecision("defer", "episode_batch_needs_catalog", "medium", evidence.kind, evidence.group, base)
        return FilterDecision("defer", "range_needs_catalog", "medium", evidence.kind, evidence.group, base)

    if _has_strong_complete(evidence):
        return FilterDecision("accept", "explicit_complete", "high", evidence.kind, evidence.group, base)
    if evidence.complete_markers:
        return FilterDecision("defer", "batch_needs_manifest", "medium", evidence.kind, evidence.group, base)
    return FilterDecision("defer", "needs_manifest_or_catalog", "low", evidence.kind, evidence.group, base)


def decide_final(evidence: ReleaseEvidence, manifest: dict[str, Any] | None,
                 catalog: CatalogEvidence | None = None) -> FilterDecision:
    title_decision = decide_title(evidence, catalog)
    base = dict(title_decision.evidence)
    base["manifest"] = manifest or {}
    if title_decision.decision == "reject":
        return FilterDecision("reject", title_decision.reason, title_decision.confidence, evidence.kind, evidence.group, base)
    if not manifest or not manifest.get("hasMainMedia"):
        return FilterDecision("reject", "no_main_media", "high", evidence.kind, evidence.group, base)
    if evidence.patch_only:
        return FilterDecision("reject", "patch_only", "high", evidence.kind, evidence.group, base)
    primary_main_count = int(manifest.get("primaryMainMediaCount", manifest.get("mainMediaCount", 0)) or 0)
    special_media_count = int(manifest.get("specialMediaCount", 0) or 0)
    catalog = catalog or CatalogEvidence()
    special_subject = str(catalog.subject_type or "").casefold() in {"ova", "oad", "ona", "special", "sp"}
    special_range = bool(evidence.episode_ranges) and all(
        item.label in {"ova", "oad", "ona", "sp"} for item in evidence.episode_ranges
    )
    if evidence.movie:
        if primary_main_count < 1:
            return FilterDecision("reject", "no_main_movie_media", "high", evidence.kind, evidence.group, base)
        return FilterDecision("accept", "movie_main_media", "high", evidence.kind, evidence.group, base)
    if title_decision.decision == "accept":
        if primary_main_count < 1 and not ((special_subject or special_range) and special_media_count >= 1):
            return FilterDecision("reject", "no_primary_main_media", "high", evidence.kind, evidence.group, base)
        return FilterDecision("accept", title_decision.reason, title_decision.confidence, evidence.kind, evidence.group, base)
    if catalog.status == "exact" and catalog.expected_episodes == 1 and (
        primary_main_count >= 1 or (special_subject and special_media_count >= 1)
    ):
        return FilterDecision("accept", "catalog_one_shot", "high", evidence.kind, evidence.group, base)
    if evidence.kind == "archive" and primary_main_count >= 2 and not evidence.partial_markers:
        return FilterDecision("accept", "manifest_archive_collection", "medium", evidence.kind, evidence.group, base)
    return FilterDecision("defer", "insufficient_completion_evidence", "low", evidence.kind, evidence.group, base)


FILTER_RULESET_MATERIAL = json.dumps({
    "version": "evidence-v2",
    "movie": MOVIE_RE.pattern,
    "complete": COMPLETE_RE.pattern,
    "all_complete": ALL_COMPLETE_RE.pattern,
    "partial": PARTIAL_RE.pattern,
    "patch_only": PATCH_ONLY_RE.pattern,
    "extras": EXTRA_RE.pattern,
    "season": [SEASON_RE.pattern, SEASON_RANGE_RE.pattern],
    "volume": VOL_RANGE_FULL_RE.pattern,
    "episode_range": [LABELED_RANGE_RE.pattern, SEASON_EP_RANGE_RE.pattern, BARE_RANGE_RE.pattern],
    "single": [EP_SINGLE_RE.pattern, BRACKET_SINGLE_RE.pattern, DASH_SINGLE_RE.pattern],
    "episode_list": EP_LIST_RE.pattern,
    "decision": "no-span-threshold;catalog-exact;ambiguous-range-defer;manifest-required-movie;extras-not-primary",
}, ensure_ascii=False, sort_keys=True).encode("utf-8")
FILTER_RULESET_ID = hashlib.sha256(FILTER_RULESET_MATERIAL).hexdigest()[:16]

SEARCH_RULESET_MATERIAL = json.dumps({
    "archive_groups": [(item.get("id"), item.get("name"), item.get("aliases", [])) for item in ARCHIVE_GROUPS],
    "serial_rules": SERIAL_RULES,
    "group_location_policy": "bracket-or-prefix-only",
}, ensure_ascii=False, sort_keys=True).encode("utf-8")
SEARCH_RULESET_ID = hashlib.sha256(SEARCH_RULESET_MATERIAL).hexdigest()[:16]


def catalog_path_from_env() -> Path | None:
    raw = os.getenv("TORRENT_COLLECTOR_CATALOG_PATH") or os.getenv("ANM_CATALOG_DB") or "/Data/state/catalog/anime-catalog.sqlite3"
    return Path(raw) if raw else None
