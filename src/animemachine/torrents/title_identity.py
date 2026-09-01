"""Shared conservative title identity helpers for torrent mapping and collection."""
from __future__ import annotations

import re
import unicodedata

TECH = re.compile(r"(?i)(?:bd[ ._-]*(?:rip|remux|box)|bdmv|dvd[ ._-]*(?:rip|iso)|web[ ._-]*(?:dl|rip)|remux|iso|1080[pi]?|720p|2160p|x26[45]|hevc|avc|flac|aac|10bit|ma10p|hi10p|chs|cht|\bsc\b|\btc\b|complete|batch|fin|reseed|rev)")


def norm(value: str | None) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKC", value or "").casefold()
        if ch.isalnum() and ch not in {"x", "×", "✕", "✖"}
    )


def queries(name: str, partial: bool = False) -> list[str]:
    values: list[str] = []
    remainder = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", name).strip()
    remainder = re.split(
        r"\s*[\[(](?:BD[ ._-]*(?:RIP|REMUX|BOX)|BDMV|DVD[ ._-]*(?:RIP|ISO)|WEB[ ._-]*(?:DL|RIP)|REMUX|ISO|Ma\d|Hi\d|1080|720|2160|x26|HEVC|AVC|FLAC|AAC|YUV)",
        remainder,
        maxsplit=1,
        flags=re.I,
    )[0]
    remainder = re.sub(r"(?i)\.(?:torrent|mkv|mp4|m2ts)$", "", remainder).strip(" -_")
    remainder = re.sub(
        r"(?i)(?<!\d)\s*(?:-|_)\s*(?:(?:ep?|episode)\s*)?\d{1,4}(?:v\d+)?(?:\s*[-~+]\s*\d{1,4})?$",
        "",
        remainder,
    ).strip(" -_")
    if partial:
        unit = r"(?:tv|ep(?:isode)?|ova|oad|ona|sp|vol(?:ume)?|disc|disk)"
        extras = r"(?:sp|special|ova|oad|ona|ncop|nced|op|ed|cm|pv)(?:[ ._-]*#?\d{1,3})?"
        range_tail = rf"(?:(?:{unit})[ ._-]*)?#?0*\d{{1,4}}(?:\.5)?\s*[-~～—–_至到]\s*(?:(?:{unit})[ ._-]*)?#?0*\d{{1,4}}(?:\.5)?(?:\s*(?:fin|end))?(?:\s*\+\s*{extras})*"
        remainder = re.sub(rf"(?i)\s*[\[(（【]\s*{range_tail}\s*[\])）】]\s*$", "", remainder).strip(" -_")
        remainder = re.sub(rf"(?i)\s+{range_tail}\s*$", "", remainder).strip(" -_")
        remainder = re.sub(r"(?i)\s+s\d{1,3}e\d{1,4}(?:v\d+)?$", "", remainder).strip(" -_")
        remainder = re.sub(r"\s*\[0*\d{1,4}\]\s*$", "", remainder).strip(" -_")
        remainder = re.sub(r"\s+0*\d{1,4}(?:v\d+)?$", "", remainder).strip(" -_")
    descriptive_head = re.split(
        r"\s+-\s+(?=(?:TV|Movie|OVA|OAD|ONA|Special|SP|Season|Complete|Batch)(?:\b|\s|\+))",
        remainder,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    candidates = [remainder, descriptive_head]
    candidates.extend(re.split(r"\s*(?:_|\||｜)\s*", remainder))
    for candidate in candidates:
        candidate = re.split(
            r"\s*[\[(](?:BD\b|BD[ ._-]*RIP|Ma\d|Hi\d|1080|720|2160|x26|HEVC|AVC|FLAC|AAC|YUV)",
            candidate,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" -_")
        if len(norm(candidate)) >= 3:
            values.append(candidate.strip())
    for group in re.findall(r"\[([^\[\]]+)\]", name):
        if len(norm(group)) >= 3 and not TECH.search(group):
            values.append(group.strip())
    return list(dict.fromkeys(values))
