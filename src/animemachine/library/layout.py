"""Shared work-identity and physical-library layout policy.

The product and the private-library maintenance scripts both import this
module.  Archive subjects remain logical catalog records; only physical works
receive a directory and an acquisition target.
"""
from __future__ import annotations

import re
import os
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any
from pathlib import Path

from ..storage import AVAILABLE, StorageUnavailableError, status_for_path


SUPPLEMENT_RE = re.compile(
    r"(?ix)(?:"
    r"ピクチャー[ ・_-]*ドラマ|picture[ ・_-]*drama|"
    r"ミニ[ ・_-]*アニメ|mini[ ・_-]*anime|"
    r"ぷち(?:っと)?[ ・_-]*アニメ|プチ[ ・_-]*アニメ|"
    r"小劇場|小剧场|短編[ ・_-]*ドラマ|short[ ・_-]*animation|"
    r"ショート[ ・_-]*アニメ|anime[ ・_-]*shorts?|休憩[ ・_-]*時間|break[ ・_-]*time"
    r")"
)
SEASON_RE = re.compile(
    r"(?ix)\s*(?:"
    r"第\s*[二三四五六七八九十0-9]+\s*期|"
    r"[0-9]+(?:st|nd|rd|th)?\s*(?:season|シーズン)|"
    r"(?:season|シーズン)\s*[0-9]+|S[0-9]+"
    r")\s*$"
)
SPLIT_COUR_SUFFIX_RE = re.compile(
    r"(?ix)\s*(?:"
    r"[（(]?第?\s*(?P<jp>[二三四五六七八九十2-9])\s*(?:クール|cour)[）)]?|"
    r"[（(]?(?P<ordinal>[2-9])(?:nd|rd|th)?\s*(?:クール|cour)[）)]?|"
    r"[-–—]?\s*(?:Part|パート)\s*(?P<part>[2-9])|"
    r"[（(]?(?:後半(?:部分|クール)?|后半(?:部分)?|第二部分)[）)]?"
    r")\s*$"
)
PHYSICAL_OWNER_RELATIONS = frozenset({"main_story", "parent", "prequel"})
WORK_DIRECTORY_RE = re.compile(r"^『(?P<date>[^』]+)』『(?P<title>.*)』$")
SERIES_DIRECTORY_RE = re.compile(r"^『(?P<start>[^』]+)[－-](?P<end>[^』]+)』『「(?P<title>.*)」シリーズ』$")
WINDOWS_UNSAFE_TRANSLATION = str.maketrans({
    "<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／",
    "\\": "＼", "|": "｜", "?": "？", "*": "＊",
})


def compact(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def portable_directory_component(value: str) -> str:
    """Return a readable directory component valid on all supported hosts.

    Catalog titles remain unchanged.  Only the physical path representation
    uses full-width equivalents for characters rejected by Windows, which also
    keeps a Windows-created library usable from Linux/NAS containers.
    """
    translated = unicodedata.normalize("NFC", str(value)).translate(WINDOWS_UNSAFE_TRANSLATION)
    translated = "".join("＿" if ord(ch) < 32 else ch for ch in translated)
    while translated.endswith((" ", ".")):
        translated = translated[:-1] + ("　" if translated[-1] == " " else "．")
    return translated or "＿"


def format_work_directory(template: str, *, date: str, title: str) -> str:
    return portable_directory_component(template.format(date=date, title=title))


def format_series_directory(template: str, *, start: str, end: str, title: str) -> str:
    return portable_directory_component(template.format(start=start, end=end, title=title))


def is_supplement_title(title: str | None) -> bool:
    """Return true only for explicit short-form attachment branding.

    Episode count, duration and WEB/OVA media type are deliberately not enough:
    those signals also describe independent works.
    """
    return bool(SUPPLEMENT_RE.search(title or ""))


def physical_role(work: Mapping[str, Any]) -> str:
    materialized = str(work.get("physical_role") or "").casefold()
    if materialized.startswith("supplement"):
        return "supplement"
    return "supplement" if is_supplement_title(str(work.get("title_ja") or work.get("official_title") or "")) else "work"


def strip_season_suffix(title: str) -> str:
    clean = re.sub(r"\s+", " ", title).strip()
    return SEASON_RE.sub("", clean).strip() or clean


def split_cour_identity(title: str | None) -> tuple[str, int] | None:
    """Return a normalized same-season base and later-cour number.

    A plain ``Season 2`` is deliberately excluded: it is an independent
    season, unlike ``Part 2`` or ``第2クール``.
    """
    value = unicodedata.normalize("NFKC", title or "").strip()
    match = SPLIT_COUR_SUFFIX_RE.search(value)
    if not match:
        return None
    token = match.group("jp") or match.group("ordinal") or match.group("part") or "2"
    japanese = {"二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    number = japanese.get(token, int(token) if token.isdigit() else 2)
    base = SPLIT_COUR_SUFFIX_RE.sub("", value).strip()
    return compact(base), number


def choose_franchise_root(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Choose the earliest formal main work that best explains the component titles.

    Archive relations can enter a component through a miniature or side work.
    Title-family support is therefore evaluated before chronology: a formal
    first title that recurs in sequel titles outranks an unrelated short-form
    title.  No acronym or invented franchise label is produced.
    """
    values = list(rows)
    if not values:
        raise ValueError("series component is empty")
    formal = [row for row in values if physical_role(row) == "work"] or values

    title_keys = {
        int(row.get("id") or index): compact(str(row.get("title_ja") or row.get("official_title") or ""))
        for index, row in enumerate(formal, 1)
    }

    def key(row: Mapping[str, Any]) -> tuple[int, int, int, str, int]:
        title = str(row.get("title_ja") or row.get("official_title") or "")
        title_key = compact(title)
        family_support = sum(
            1 for other in title_keys.values()
            if len(title_key) >= 4 and title_key != other and title_key in other
        )
        return (
            1 if SEASON_RE.search(title) else 0,
            1 if str(row.get("relation_role") or "") in {"side_story", "spin_off", "summary"} else 0,
            -family_support,
            str(row.get("start_month") or row.get("directory_date") or "9999-XX"),
            len(title),
        )

    return min(formal, key=key)


def franchise_title(rows: Iterable[Mapping[str, Any]]) -> str:
    root = choose_franchise_root(rows)
    title = str(root.get("directory_title") or root.get("title_ja") or root.get("official_title") or "").strip()
    if not title:
        raise ValueError("series root lacks a title")
    return strip_season_suffix(title)


def find_supplement_owner(
    supplement: Mapping[str, Any],
    component: Iterable[Mapping[str, Any]],
    relations: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Resolve an attachment owner from explicit relations, then nearest date.

    A date-only fallback is accepted only inside an already verified component
    and only when it yields a unique nearest formal work.
    """
    values = list(component)
    formal = [row for row in values if physical_role(row) == "work"]
    by_bgm = {int(row["bgm_id"]): row for row in formal if row.get("bgm_id") is not None}
    direct: list[Mapping[str, Any]] = []
    for edge in relations:
        if int(edge.get("anime_id") or -1) != int(supplement.get("id") or -2):
            continue
        if str(edge.get("relation_code") or "") in PHYSICAL_OWNER_RELATIONS:
            owner = by_bgm.get(int(edge.get("related_bgm_id") or -1))
            if owner is not None:
                direct.append(owner)
    if len({int(row.get("id") or 0) for row in direct}) == 1:
        return direct[0]
    if direct or not formal:
        return None
    source = str(supplement.get("start_month") or "")
    if not re.fullmatch(r"\d{4}-\d{2}", source):
        return None
    sy, sm = map(int, source.split("-"))
    ranked: list[tuple[int, Mapping[str, Any]]] = []
    for row in formal:
        date = str(row.get("start_month") or "")
        if re.fullmatch(r"\d{4}-\d{2}", date):
            year, month = map(int, date.split("-"))
            ranked.append((abs((year * 12 + month) - (sy * 12 + sm)), row))
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1] if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0]) else None


def verified_compound_title(primary: str, aliases: Iterable[str], existing: str | None) -> str:
    """Preserve an existing official-title + verified semantic alias spelling.

    Archive aliases prove that the suffix exists, but do not label whether it
    is a translation, romanization or official secondary branding.  Therefore
    a new compound title is not invented here; an existing compound spelling
    is retained only when its suffix is an exact Archive alias.  The Japanese
    primary is canonicalized even if the old prefix contains a typo.
    """
    if not existing:
        return primary
    normalized_existing = unicodedata.normalize("NFKC", existing).strip()
    candidates = sorted({str(alias).strip() for alias in aliases if str(alias).strip()}, key=len, reverse=True)
    for alias in candidates:
        if normalized_existing.casefold().endswith(alias.casefold()) and compact(alias) != compact(primary):
            if normalized_existing.casefold().startswith(unicodedata.normalize("NFKC", primary).casefold()):
                return existing.strip()
            return f"{primary} {alias}"
    return primary


def compound_title_from_evidence(primary: str, aliases: Iterable[str], evidence_titles: Iterable[str],
                                 existing: str | None = None) -> str:
    """Use bilingual branding only when one observed title contains both parts."""
    preserved = verified_compound_title(primary, aliases, existing)
    if preserved != primary:
        return preserved
    primary_key = compact(primary)
    latin_aliases = sorted({str(alias).strip() for alias in aliases
                            if re.search(r"[A-Za-z]", str(alias)) and compact(alias) != primary_key}, key=len, reverse=True)
    for observed in evidence_titles:
        observed_key = compact(observed)
        if not primary_key or primary_key not in observed_key:
            continue
        for alias in latin_aliases:
            if len(compact(alias)) >= 5 and compact(alias) in observed_key:
                return f"{primary} {alias}"
    return primary


class ExistingPathIndex:
    """Small structural index used before creating any physical target."""

    def __init__(self, root: Path, ignored: Iterable[str] = ()) -> None:
        self.root = root
        self.rows: list[dict[str, Any]] = []
        self.by_date: dict[str, list[dict[str, Any]]] = {}
        self.by_path: dict[str, dict[str, Any]] = {}
        storage = status_for_path(root, timeout=4.0)
        if storage.state != AVAILABLE:
            raise StorageUnavailableError(f"library storage unavailable: {root}")
        try:
            for top in root.iterdir():
                if not stat.S_ISDIR(top.stat().st_mode) or is_ignored_library_container(top.name, ignored):
                    continue
                # The generic work pattern also matches a series directory.  Test
                # the more specific grammar first or every series is indexed as a
                # single top-level work and all of its children become invisible.
                if SERIES_DIRECTORY_RE.fullmatch(top.name):
                    for child in top.iterdir():
                        match = WORK_DIRECTORY_RE.fullmatch(child.name) if stat.S_ISDIR(child.stat().st_mode) else None
                        if match:
                            self._append(child, top, match.group("date"), match.group("title"))
                    continue
                work = WORK_DIRECTORY_RE.fullmatch(top.name)
                if work:
                    self._append(top, None, work.group("date"), work.group("title"))
        except OSError as exc:
            raise StorageUnavailableError(exc.errno or 0, f"library storage unavailable: {root}") from exc

    def _append(self, path: Path, series: Path | None, date: str, title: str) -> None:
        has_media = False
        try:
            pending = [str(path)]
            while pending and not has_media:
                with os.scandir(pending.pop()) as entries:
                    for item in entries:
                        if item.is_dir(follow_symlinks=False):
                            pending.append(item.path)
                        elif item.is_file(follow_symlinks=False) and Path(item.name).suffix.casefold() in {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm"}:
                            has_media = True
                            break
        except OSError as exc:
            raise StorageUnavailableError(exc.errno or 0, f"library storage unavailable: {self.root}") from exc
        row = {"path": path, "series": series, "date": date, "title": title, "hasMedia": has_media}
        self.rows.append(row)
        self.by_date.setdefault(date, []).append(row)
        self.by_path[str(path).replace("/", "\\").casefold()] = row

    def exact(self, path: Path | str) -> dict[str, Any] | None:
        return self.by_path.get(str(path).replace("/", "\\").casefold())

    def resolve(self, date: str, primary: str, aliases: Iterable[str]) -> dict[str, Any] | None:
        keys = {compact(primary), *(compact(alias) for alias in aliases)} - {""}
        alias_values = [str(alias).strip() for alias in aliases if str(alias).strip()]
        ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
        for row in self.by_date.get(date, ()):
            title_key = compact(row["title"])
            exact = title_key in keys
            alias_hits = sum(1 for key in keys if len(key) >= 4 and key in title_key)
            if not exact and alias_hits < 1:
                continue
            compound = verified_compound_title(primary, alias_values, row["title"]) != primary
            ranked.append(((1 if compound else 0, 1 if row["hasMedia"] else 0,
                            1 if exact else 0, alias_hits), row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            return None
        return ranked[0][1]


def is_ignored_library_container(name: str, configured: Iterable[str] = ()) -> bool:
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    defaults = {"『0000_00』『others』"}
    return normalized in defaults | {unicodedata.normalize("NFKC", value).casefold().strip() for value in configured}
