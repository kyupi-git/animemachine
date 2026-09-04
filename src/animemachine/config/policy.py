"""Deterministic AnimeMachine (ANM) naming and planning rules.

This module is intentionally independent from qBittorrent.  It produces plans;
an adapter may apply an approved plan later.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .loader import ConfigError, _validate
from .runtime import apply_runtime_overrides


STRICT_SERIES_RELATIONS = frozenset({
    "prequel", "sequel", "parent", "main_story", "side_story", "spin_off",
    "summary", "full_story", "alternative_version",
})
NON_GROUPING_RELATIONS = frozenset({
    "same_setting", "character_appearance", "collaboration", "other",
})
SPLIT_COUR_EVIDENCE = frozenset({"same_official_season", "split_cour"})
COUR_SUFFIX = re.compile(
    r"\s*(?:[（(]?第?\s*(?:2|二)\s*(?:クール|cour)[）)]?|[-–—]\s*(?:Part|Season)\s*2)\s*$",
    re.IGNORECASE,
)
ILLEGAL_WINDOWS = str.maketrans({"<": "＜", ">": "＞", ":": "：", '"': "”", "/": "／", "\\": "＼", "|": "｜", "?": "？", "*": "＊"})


def collision_key(value: str) -> str:
    """Approximate the case/Unicode/trailing-dot behavior of common NAS clients."""
    return "/".join(
        unicodedata.normalize("NFKC", part).casefold().rstrip(" .")
        for part in value.replace("\\", "/").split("/")
    )


def safe_title(title: str) -> str:
    value = re.sub(r"\s+", " ", title.translate(ILLEGAL_WINDOWS)).strip().rstrip(". ")
    if not value:
        raise ValueError("title is empty after normalization")
    return value


def directory_date(year: int | None, month: int | None) -> str:
    if year is None:
        return "20XX_XX"
    if not 1 <= year <= 9999:
        raise ValueError("year must be 1..9999")
    if month is None:
        return f"{year:04d}_XX"
    if not 1 <= month <= 12:
        raise ValueError("month must be 1..12")
    return f"{year:04d}_{month:02d}"


def work_directory(date: str, official_title: str) -> str:
    return f"『{date}』『{safe_title(official_title)}』"


def series_directory(start: str, end: str, franchise_root: str) -> str:
    return f"『{start}－{end}』『「{safe_title(franchise_root)}」シリーズ』"


def strip_cour_suffix(title: str) -> str:
    return safe_title(COUR_SUFFIX.sub("", title))


def should_merge_split_cour(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Merge only with explicit same-season evidence; title similarity is insufficient."""
    left_season = left.get("official_season_id")
    right_season = right.get("official_season_id")
    same_id = bool(left_season and left_season == right_season)
    relation = str(right.get("relation_to_left") or left.get("relation_to_right") or "").casefold()
    explicit = relation in SPLIT_COUR_EVIDENCE
    if not (same_id or explicit):
        return False
    return strip_cour_suffix(str(left["official_title"])).casefold() == strip_cour_suffix(str(right["official_title"])).casefold()


def merge_split_cour(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if not should_merge_split_cour(left, right):
        raise ValueError("works are not proven parts of the same official season")
    first, second = sorted((left, right), key=lambda item: item["start_date"])
    merged = dict(first)
    merged["official_title"] = strip_cour_suffix(first["official_title"])
    merged["directory_date"] = first["directory_date"]
    merged["members"] = [item.get("id") for item in (first, second)]
    merged["evidence"] = "same_official_season"
    return merged


def strict_series_edge(relation: str) -> bool:
    value = relation.casefold().strip()
    if value in NON_GROUPING_RELATIONS:
        return False
    return value in STRICT_SERIES_RELATIONS


@dataclass(frozen=True)
class FileSelection:
    index: int
    target_work_id: str
    relative_path: str
    size: int


def build_infohash_plan(
    infohash: str,
    series_save_path: str,
    selections: Iterable[FileSelection],
    *,
    existing_task: bool = False,
) -> dict[str, Any]:
    """Return one job/update per hash, even when files serve multiple children."""
    normalized_hash = infohash.strip().casefold()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", normalized_hash):
        raise ValueError("unsupported infohash")
    rows = sorted(selections, key=lambda item: item.index)
    if not rows:
        raise ValueError("at least one file must be selected")
    if len({row.index for row in rows}) != len(rows):
        raise ValueError("duplicate torrent file index")
    paths = [row.relative_path.replace("\\", "/").lstrip("/") for row in rows]
    if any(".." in Path(path).parts or not path for path in paths):
        raise ValueError("selection escapes the save path")
    if len({collision_key(path) for path in paths}) != len(paths):
        raise ValueError("duplicate final path")
    return {
        "idempotency_key": f"torrent:{normalized_hash}",
        "infohash": normalized_hash,
        "action": "extend_file_selection" if existing_task else "create_stopped_task",
        "save_path": series_save_path,
        "files": [
            {"index": row.index, "work_id": row.target_work_id, "new_path": path, "size": row.size, "priority": 1}
            for row, path in zip(rows, paths)
        ],
        "selected_bytes": sum(row.size for row in rows),
        "requires_confirmation": True,
    }


class ConfigStore:
    """Small atomic JSON store used by the reference Web implementation."""

    def __init__(self, path: Path, example: Path) -> None:
        self.path = path
        self.example = example

    def read_persistent(self) -> dict[str, Any]:
        source = self.path if self.path.exists() else self.example
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        self.validate(data)
        return data

    def read(self) -> dict[str, Any]:
        effective = apply_runtime_overrides(self.read_persistent())
        self.validate(effective)
        return effective

    def validate_for_write(self, data: dict[str, Any]) -> None:
        self.validate(data)
        self.validate(apply_runtime_overrides(data))

    def write(self, data: dict[str, Any]) -> None:
        self.validate_for_write(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix="config-", suffix=".json", dir=self.path.parent)
        os.close(fd)
        temp = Path(raw)
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def validate(data: dict[str, Any]) -> None:
        try:
            _validate(data)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
