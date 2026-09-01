#!/usr/bin/env python3
"""Read-only .torrent inventory using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config.loader import canonical_resolution, load_resource_group_catalog, option_enabled


GROUP_RULES = [
    ("VCB-Studio", r"(?<![a-z0-9])vcb(?:-studio|-s)?(?![a-z0-9])"),
    ("jsum", r"(?<![a-z0-9])j[ ._-]*sum(?![a-z0-9])"),
    ("philosophy-raws", r"(?<![a-z0-9])philosophy[ ._-]*raws(?![a-z0-9])"),
    ("Beatrice-Raws", r"(?<![a-z0-9])beatrice[ ._-]*raws(?![a-z0-9])"),
    ("ANK-Raws", r"(?<![a-z0-9])ank(?:[ ._-]*raws)?(?![a-z0-9])"),
    ("Yousei-Raws", r"(?<![a-z0-9])yousei[ ._-]*raws(?![a-z0-9])"),
    ("Kawaiika-Raws", r"(?<![a-z0-9])kawaiika[ ._-]*raws(?![a-z0-9])"),
]
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".webm", ".flv", ".wmv"}
GROUP_REGION = re.compile(r"[\[【〖〔]([^\]】〗〕]{1,240})[\]】〗〕]")
GROUP_SPLIT = re.compile(r"\s*(?:&|＆|\+|＋|×|\||/|／)\s*")
GROUP_SEPARATOR = re.compile(r"[ ._-]+")
CLASSIFIER_VERSION = "2026-08-31.metainfo-v2-strict-v5"
MAX_TORRENT_BYTES = 16 * 1024 * 1024
MAX_BENCODE_DEPTH = 64
MAX_BENCODE_ITEMS = 750_000
MAX_TORRENT_FILES = 100_000


class BencodeError(ValueError):
    pass


class Decoder:
    """Strict bounded bencode decoder that preserves the raw info span."""

    def __init__(self, data: bytes, *, max_depth: int = MAX_BENCODE_DEPTH, max_items: int = MAX_BENCODE_ITEMS):
        if len(data) > MAX_TORRENT_BYTES:
            raise BencodeError("torrent metadata exceeds size limit")
        self.data = data
        self.pos = 0
        self.max_depth = max_depth
        self.max_items = max_items
        self.items = 0
        self.info_span: tuple[int, int] | None = None

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise BencodeError(message)

    def parse(self, root: bool = False, depth: int = 0):
        self._require(depth <= self.max_depth, "bencode nesting exceeds limit")
        self.items += 1
        self._require(self.items <= self.max_items, "bencode item count exceeds limit")
        self._require(self.pos < len(self.data), "unexpected end")
        marker = self.data[self.pos:self.pos + 1]
        if marker == b"i":
            self.pos += 1
            end = self.data.find(b"e", self.pos)
            self._require(end >= 0, "unterminated integer")
            raw = self.data[self.pos:end]
            self._require(bool(re.fullmatch(rb"(?:0|-?[1-9][0-9]*)", raw)), "invalid integer encoding")
            value = int(raw)
            self.pos = end + 1
            return value
        if marker == b"l":
            self.pos += 1
            value = []
            while True:
                self._require(self.pos < len(self.data), "unterminated list")
                if self.data[self.pos:self.pos + 1] == b"e":
                    self.pos += 1
                    return value
                value.append(self.parse(depth=depth + 1))
        if marker == b"d":
            self.pos += 1
            value = {}
            previous: bytes | None = None
            while True:
                self._require(self.pos < len(self.data), "unterminated dictionary")
                if self.data[self.pos:self.pos + 1] == b"e":
                    self.pos += 1
                    return value
                key = self.parse(depth=depth + 1)
                self._require(isinstance(key, bytes), "dictionary key is not a byte string")
                if previous is not None:
                    self._require(key > previous, "dictionary keys are not strictly sorted")
                previous = key
                start = self.pos
                item = self.parse(depth=depth + 1)
                if root and key == b"info":
                    self.info_span = (start, self.pos)
                value[key] = item
        if marker.isdigit():
            colon = self.data.find(b":", self.pos)
            self._require(colon >= 0, "missing byte string colon")
            raw_length = self.data[self.pos:colon]
            self._require(bool(re.fullmatch(rb"(?:0|[1-9][0-9]*)", raw_length)), "invalid byte string length")
            length = int(raw_length)
            start = colon + 1
            end = start + length
            self._require(end <= len(self.data), "byte string exceeds input")
            self.pos = end
            return self.data[start:end]
        raise BencodeError(f"invalid marker at {self.pos}")


def read_torrent_file(path: Path, *, max_bytes: int = MAX_TORRENT_BYTES) -> bytes:
    """Read a local .torrent only after a size check and stop if it grows concurrently."""
    stat = path.stat()
    if stat.st_size < 0 or stat.st_size > max_bytes:
        raise BencodeError("torrent metadata exceeds size limit")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise BencodeError("torrent metadata exceeds size limit")
    return raw


def text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "gb18030", "shift_jis"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _v2_file_tree(tree: dict, prefix: tuple[str, ...] = ()) -> list[dict]:
    if b"" in tree and len(tree) != 1:
        raise BencodeError("v2 file leaf cannot contain child entries")
    result: list[dict] = []
    for raw_name, node in tree.items():
        if not isinstance(raw_name, bytes) or not isinstance(node, dict):
            raise BencodeError("invalid v2 file tree")
        if raw_name == b"":
            if not prefix or b"length" not in node or not isinstance(node[b"length"], int):
                raise BencodeError("invalid v2 file leaf")
            length = int(node[b"length"])
            if length < 0:
                raise BencodeError("negative v2 file length")
            pieces_root = node.get(b"pieces root")
            if length > 0 and (not isinstance(pieces_root, bytes) or len(pieces_root) != 32):
                raise BencodeError("invalid v2 pieces root")
            result.append({"path": "/".join(prefix), "length": length})
            if len(result) > MAX_TORRENT_FILES:
                raise BencodeError("torrent file count exceeds limit")
            continue
        name = text(raw_name)
        if name in {".", ".."} or "\x00" in name:
            # The collector never materializes these paths, but exposes them as
            # metadata only and marks them as unsafe for downstream consumers.
            name = name.replace("\x00", "�")
        result.extend(_v2_file_tree(node, prefix + (name,)))
    return result


def torrent_files(info: dict) -> list[dict]:
    if b"files" in info:
        if not isinstance(info[b"files"], list):
            raise BencodeError("invalid v1 files list")
        result = []
        for index, item in enumerate(info[b"files"]):
            if index >= MAX_TORRENT_FILES:
                raise BencodeError("torrent file count exceeds limit")
            if not isinstance(item, dict) or not isinstance(item.get(b"length"), int):
                raise BencodeError("invalid v1 file entry")
            if int(item[b"length"]) < 0:
                raise BencodeError("negative v1 file length")
            parts = item.get(b"path.utf-8") or item.get(b"path") or []
            attr = text(item.get(b"attr"))
            if (not isinstance(parts, list) or not parts) and "p" in attr:
                parts = [".pad", str(index)]
            if not isinstance(parts, list) or not parts:
                raise BencodeError("invalid v1 file path")
            if not all(isinstance(part, (bytes, str)) for part in parts):
                raise BencodeError("invalid v1 file path component")
            result.append({
                "index": index,
                "path": "/".join(text(part) for part in parts),
                "length": int(item[b"length"]),
                "attr": attr,
            })
        if not result:
            raise BencodeError("empty v1 files list")
        return result
    if int(info.get(b"meta version", 0) or 0) == 2 and b"file tree" in info:
        if not isinstance(info[b"file tree"], dict):
            raise BencodeError("invalid v2 file tree root")
        return [dict(item, index=index) for index, item in enumerate(_v2_file_tree(info[b"file tree"]))]
    if b"length" not in info or not isinstance(info.get(b"length"), int):
        raise BencodeError("torrent has no valid file description")
    if int(info[b"length"]) < 0:
        raise BencodeError("negative v1 file length")
    name = text(info.get(b"name.utf-8") or info.get(b"name"))
    return [{"index": 0, "path": name, "length": int(info[b"length"]), "attr": text(info.get(b"attr"))}]


VIDEO_EXTENSIONS = MEDIA_EXTENSIONS
SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".idx", ".sub"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".ape", ".m4a", ".aac", ".mp3", ".opus"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}
DISC_EXTENSIONS = {".iso"}
BONUS_VIDEO_RE = re.compile(r"(?i)(?:^|[ /_.\-\[\]()])(?:ncop|nced|creditless|menu|pv|cm|trailer|teaser|sample|bonus|preview)(?:$|[ /_.\-\[\]()0-9])")
SPECIAL_VIDEO_RE = re.compile(r"(?i)(?:^|[ /_.\-\[\]()])(?:specials?|ova|oad|ona|sp)(?:[ ._#-]*\d{1,3})?(?:$|[ /_.\-\[\]()])")
SAMPLE_RE = re.compile(r"(?i)(?:^|[ /_.\-\[\]()])sample(?:$|[ /_.\-\[\]()0-9])")
MENU_RE = re.compile(r"(?i)(?:^|[ /_.\-\[\]()])menu(?:$|[ /_.\-\[\]()0-9])")
SCAN_RE = re.compile(r"(?i)(?:scan|booklet|cover|artwork)")
CD_RE = re.compile(r"(?i)(?:^|[ /_.\-\[\]()])(?:cd|ost|original soundtrack)(?:$|[ /_.\-\[\]()0-9])")


def classify_manifest_file(path: str) -> str:
    lower = path.casefold().replace("\\", "/")
    suffix = Path(lower).suffix
    if suffix in DISC_EXTENSIONS:
        return "disc_image"
    if suffix in VIDEO_EXTENSIONS:
        if SAMPLE_RE.search(lower):
            return "sample"
        if MENU_RE.search(lower):
            return "menu"
        if BONUS_VIDEO_RE.search(lower):
            return "bonus_video"
        if SPECIAL_VIDEO_RE.search(lower):
            return "special_video"
        return "main_video"
    if suffix in AUDIO_EXTENSIONS:
        return "cd_audio" if CD_RE.search(lower) else "attachment"
    if suffix in IMAGE_EXTENSIONS:
        return "scans" if SCAN_RE.search(lower) else "images"
    if suffix in FONT_EXTENSIONS:
        return "fonts"
    if suffix in SUBTITLE_EXTENSIONS:
        return "subtitle"
    return "attachment"


def manifest_summary(files: list[dict]) -> dict:
    counts: dict[str, int] = {}
    classified = []
    for item in files:
        kind = classify_manifest_file(str(item["path"]))
        counts[kind] = counts.get(kind, 0) + 1
        classified.append({**item, "kind": kind})
    primary_count = counts.get("main_video", 0) + counts.get("disc_image", 0)
    special_count = counts.get("special_video", 0)
    main_count = primary_count + special_count
    return {
        "files": classified,
        "kindCounts": counts,
        "mainMediaCount": main_count,
        "primaryMainMediaCount": primary_count,
        "specialMediaCount": special_count,
        "hasPrimaryMainMedia": primary_count > 0,
        "hasMainMedia": main_count > 0,
    }


def _v2_piece_roots(tree: dict) -> list[tuple[str, int, bytes | None]]:
    result: list[tuple[str, int, bytes | None]] = []

    def walk(node: dict, prefix: tuple[str, ...] = ()) -> None:
        if b"" in node and len(node) != 1:
            raise BencodeError("v2 file leaf cannot contain child entries")
        for raw_name, child in node.items():
            if not isinstance(raw_name, bytes) or not isinstance(child, dict):
                raise BencodeError("invalid v2 file tree")
            if raw_name == b"":
                if not prefix or not isinstance(child.get(b"length"), int):
                    raise BencodeError("invalid v2 file leaf")
                result.append(("/".join(prefix), int(child[b"length"]), child.get(b"pieces root")))
            else:
                walk(child, prefix + (text(raw_name),))

    walk(tree)
    return result


def _validate_info_layout(root: dict, info: dict, meta_version: int, files: list[dict]) -> None:
    name = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(name, bytes) or not name:
        raise BencodeError("torrent info has no valid name")
    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        raise BencodeError("invalid piece length")
    if meta_version == 2 and (piece_length < 16 * 1024 or piece_length & (piece_length - 1)):
        raise BencodeError("invalid v2 piece length")

    has_v1 = meta_version != 2 or b"pieces" in info
    if has_v1:
        pieces = info.get(b"pieces")
        if not isinstance(pieces, bytes) or len(pieces) % 20:
            raise BencodeError("invalid v1 pieces field")
        total = sum(int(item["length"]) for item in files)
        expected_pieces = (total + piece_length - 1) // piece_length if total else 0
        if len(pieces) // 20 != expected_pieces:
            raise BencodeError("v1 piece count does not match payload length")

    if meta_version == 2:
        tree = info.get(b"file tree")
        if not isinstance(tree, dict) or not tree:
            raise BencodeError("v2 torrent has no valid file tree")
        v2_files = _v2_file_tree(tree)
        if not v2_files:
            raise BencodeError("v2 torrent has no files")
        piece_roots = _v2_piece_roots(tree)
        layers = root.get(b"piece layers", {})
        if not isinstance(layers, dict):
            raise BencodeError("invalid v2 piece layers")
        for root_hash, layer in layers.items():
            if not isinstance(root_hash, bytes) or len(root_hash) != 32 or not isinstance(layer, bytes) or len(layer) % 32:
                raise BencodeError("invalid v2 piece layer entry")
        for _path, length, pieces_root in piece_roots:
            if length > piece_length:
                layer = layers.get(pieces_root)
                expected_hashes = (length + piece_length - 1) // piece_length
                if not isinstance(pieces_root, bytes) or len(pieces_root) != 32 or not isinstance(layer, bytes):
                    raise BencodeError("missing v2 piece layer")
                if len(layer) != expected_hashes * 32:
                    raise BencodeError("v2 piece layer length does not match file length")
        if b"pieces" in info:
            v1_content = [item for item in files if "p" not in str(item.get("attr") or "")]
            v1_total = sum(int(item["length"]) for item in v1_content)
            v2_total = sum(int(item["length"]) for item in v2_files)
            if v1_total != v2_total:
                raise BencodeError("hybrid v1/v2 payload lengths disagree")
            if [str(item["path"]) for item in v1_content] != [str(item["path"]) for item in v2_files]:
                raise BencodeError("hybrid v1/v2 file order or paths disagree")


def _group_rules(policy: dict | None) -> list[tuple[str, tuple[re.Pattern[str], ...]]]:
    if not policy:
        return GROUP_RULES
    rules = []
    for item in sorted(policy.get("resourceGroups", []), key=lambda value: (int(value.get("tier", 999)), int(value.get("order", 999)))):
        aliases = [item.get("name", ""), *item.get("aliases", [])]
        tokens = [r"[ ._-]+".join(re.escape(part) for part in GROUP_SEPARATOR.split(value) if part) for value in aliases if value]
        if tokens:
            rules.append((item["name"], tuple(re.compile(rf"(?:{token})", re.IGNORECASE) for token in tokens)))
    known = {name.casefold() for name, _ in rules}
    for profile in load_resource_group_catalog().get("serialProfiles", {}).values():
        for _rule_id, display_name, keywords, _subtitle_keywords in profile:
            if "*" in keywords or display_name.casefold() in known:
                continue
            tokens = [r"[ ._-]+".join(re.escape(part) for part in GROUP_SEPARATOR.split(value) if part) for value in keywords]
            rules.append((display_name, tuple(re.compile(rf"(?:{token})", re.IGNORECASE) for token in tokens)))
            known.add(display_name.casefold())
    return rules or GROUP_RULES


def _group_regions(*values: str) -> list[str]:
    regions: list[str] = []
    for value in values:
        for raw in GROUP_REGION.findall(value):
            raw = raw.strip()
            if raw:
                regions.append(raw)
                regions.extend(part for part in GROUP_SPLIT.split(raw) if part)
        stripped = value.strip()
        if stripped:
            prefix = re.split(r"\s*(?:[-:：|｜])\s*", stripped, maxsplit=1)[0].strip()
            if 0 < len(prefix) <= 80:
                regions.append(prefix)
            suffix = re.search(r"-\s*([^\[\]【】]{1,80})\s*$", stripped)
            if suffix:
                regions.append(suffix.group(1).strip())
    return list(dict.fromkeys(region.casefold() for region in regions))


def _participating_groups(filename: str, info_name: str, policy: dict | None) -> list[str]:
    regions = _group_regions(Path(filename).stem, info_name)
    result: list[str] = []
    for group, matchers in _group_rules(policy):
        if isinstance(matchers, str):
            matchers = (re.compile(matchers, re.IGNORECASE),)
        if any(any(pattern.fullmatch(region) for pattern in matchers) for region in regions):
            result.append(group)
    return result


def classify(filename: str, info_name: str, files: list[dict], policy: dict | None = None) -> dict:
    combined = " ".join([filename, info_name] + [item["path"] for item in files[:50]])
    lower = combined.lower()
    participating_groups = _participating_groups(filename, info_name, policy)
    group = participating_groups[0] if participating_groups else None
    declared_group_tokens = []
    for value in (Path(filename).stem, info_name):
        match = re.match(r"\[([^\[\]]{1,240})\]", value)
        if match:
            for token in re.split(r"[&+×]", match.group(1)):
                token = token.strip()
                if token and token.casefold() not in {item.casefold() for item in declared_group_tokens}:
                    declared_group_tokens.append(token)
    if group is None and declared_group_tokens:
        group = declared_group_tokens[0]
    has_simplified = bool(re.search(r"(?<![a-z0-9])(?:chs|sc|gb)(?![a-z0-9])|简体|简中|简繁|zh[ ._-]*hans", lower, re.IGNORECASE))
    has_traditional = bool(re.search(r"(?<![a-z0-9])(?:cht|tc|big5)(?![a-z0-9])|繁体|繁中|简繁|zh[ ._-]*hant", lower, re.IGNORECASE))
    if has_simplified and has_traditional:
        language_hint = "CHS+CHT"
    elif has_simplified:
        language_hint = "CHS"
    elif has_traditional:
        language_hint = "CHT"
    else:
        language_hint = "Unknown"
    if re.search(r"web[ ._-]?(?:rip|dl)|webrip", lower):
        source = "WebRip"
    elif re.search(r"bd[ ._-]?remux|(?<![a-z])remux(?![a-z])", lower):
        source = "Remux"
    elif re.search(r"(?<![a-z])bdmv(?![a-z])", lower):
        source = "BDMV"
    elif re.search(r"(?<![a-z])(?:bd|dvd)?[ ._-]?iso(?![a-z])", lower):
        source = "ISO"
    elif re.search(r"dvd[ ._-]?rip|dvd[59]", lower):
        source = "DVDRip"
    elif re.search(r"tv[ ._-]?rip|hdtv[ ._-]?rip|hdtv|pdtv|sat[ ._-]?rip", lower):
        source = "TVRip"
    elif re.search(r"vhs[ ._-]?rip", lower):
        source = "VHSRip"
    elif re.search(r"(?:laser[ ._-]?disc|ld)[ ._-]?rip", lower):
        source = "LDRip"
    elif re.search(r"bd[ ._-]?rip|blu[ ._-]?ray", lower):
        source = "BDRip"
    else:
        source = "Unknown"
    media = [item for item in files if Path(item["path"]).suffix.lower() in MEDIA_EXTENSIONS]
    volume_numbers = set(re.findall(r"\bvol(?:ume)?[ ._-]*0*(\d+)\b", lower))
    for start, end in re.findall(r"\bvol(?:ume)?[ ._-]*0*(\d+)\s*[-~+]\s*(?:vol(?:ume)?[ ._-]*)?0*(\d+)", lower):
        if int(end) >= int(start) and int(end) - int(start) <= 100:
            volume_numbers.update(str(value) for value in range(int(start), int(end) + 1))
    episode_numbers = {int(value) for value in re.findall(r"(?:\bep(?:isode)?[ ._-]*|\be)(0*\d{1,4})\b", lower)}
    if source in {"WebRip", "TVRip"}:
        serial_end = r"(?=\s*(?:\[|\(|\.(?:mkv|mp4|m2ts|ts|webm)\b|$))"
        for start, end in re.findall(rf"\s-\s*0*(\d{{1,4}})\s*[-~+]\s*0*(\d{{1,4}}){serial_end}", lower):
            if int(end) >= int(start) and int(end) - int(start) <= 100:
                episode_numbers.update(range(int(start), int(end) + 1))
        episode_numbers.update(int(value) for value in re.findall(
            rf"\s-\s*0*(\d{{1,4}})(?:v\d+)?{serial_end}", lower))
        episode_numbers.update(int(value) for value in re.findall(r"\bs\d{1,3}e0*(\d{1,4})\b", lower))
        episode_numbers.update(int(value) for value in re.findall(r"\[0*(\d{1,4})\](?=\s*\[)", lower))
        episode_numbers.update(int(value) for value in re.findall(
            r"\s0*(\d{1,4})(?=\s*\((?:web[ ._-]?(?:dl|rip)|hdtv|tv[ ._-]?rip)\b)", lower))
    single_episode = len(media) <= 2 and bool(episode_numbers)
    reject = []
    configured = (policy or {}).get("torrentPolicy", policy or {})
    allow_unlisted = configured.get("allowUnlisted", {})
    if not option_enabled(configured.get("contentClasses", {"BDRip": True}), source, allow_unlisted.get("sourceClass", True)):
        reject.append("SourceClassDisabled")
    flags=[]
    if re.search(r"(?<![a-z0-9])bd[ ._-]?box(?![a-z0-9])",lower): flags.append("BDBOX")
    if re.search(r"(?<![a-z0-9])fin(?:al)?(?![a-z0-9])",lower): flags.append("FIN")
    if re.search(r"(?<![a-z0-9])rev(?:ision)?(?:\d+)?(?![a-z0-9])",lower): flags.append("REV")
    if re.search(r"(?<![a-z0-9])re[ ._-]?seed(?![a-z0-9])",lower): flags.append("RESEED")
    quality=[]
    for height in (4320,2160,1080,720,576,540,480):
        match=re.search(rf"(?<!\d){height}([pi])(?![a-z0-9])",lower)
        if match: quality=[height,match.group(1)];break
    bit_depth=10 if re.search(r"(?<!\d)10[ ._-]?bit(?!\d)|ma10p|hi10p|yuv420p10|p010",lower) else (8 if re.search(r"(?<!\d)8[ ._-]?bit(?!\d)|yuv420p8",lower) else None)
    resolution = canonical_resolution(f"{quality[0]}{quality[1]}" if quality else "unknown")
    if not option_enabled(configured.get("resolutions", {}), resolution, allow_unlisted.get("resolution", True)):
        reject.append("ResolutionDisabled")
    volume_sequence = sorted(int(value) for value in volume_numbers)
    episode_sequence = sorted(episode_numbers)
    explicit_complete = bool(flags) or bool(re.search(r"\bcomplete\b|\bbatch\b|全集|全\s*\d+\s*(?:話|话|集)|s\d+\s*[-+]\s*s\d+", lower))
    if explicit_complete:
        release_unit = "collection"
    elif volume_sequence:
        release_unit = "volume"
    elif episode_sequence or single_episode:
        release_unit = "episode"
    else:
        release_unit = "collection" if len(media) >= 3 else "unknown"
    complete_hint = release_unit == "collection"
    eligibility = "reject" if reject else ("candidate" if release_unit in {"collection", "volume", "episode"} or len(media) == 1 else "review")
    return {
        "resourceGroup": group,
        "participatingGroups": participating_groups,
        "declaredGroupTokens": declared_group_tokens,
        "subtitleLanguageHint": language_hint,
        "sourceClass": source,
        "mediaFileCount": len(media),
        "completeHint": complete_hint,
        "releaseFlags": flags,
        "collectionHint": "BDBOX" in flags,
        "releaseUnit": release_unit,
        "volumeSequence": volume_sequence,
        "episodeSequence": episode_sequence,
        "videoHeight": quality[0] if quality else None,
        "videoScan": quality[1] if quality else None,
        "bitDepth": bit_depth,
        "eligibility": eligibility,
        "rejectReasons": reject,
    }


def inspect_bytes(raw: bytes, *, filename: str = "torrent.torrent", include_files: bool = True,
                  policy: dict | None = None) -> dict:
    decoder = Decoder(raw)
    root = decoder.parse(root=True)
    if decoder.pos != len(raw) or not isinstance(root, dict) or b"info" not in root or not decoder.info_span:
        raise BencodeError("invalid torrent root/info dictionary")
    info = root[b"info"]
    if not isinstance(info, dict):
        raise BencodeError("info is not a dictionary")
    meta_version = int(info.get(b"meta version", 0) or 0)
    if meta_version not in (0, 2):
        raise BencodeError(f"unsupported meta version: {meta_version}")
    files = torrent_files(info)
    _validate_info_layout(root, info, meta_version, files)
    info_start, info_end = decoder.info_span
    raw_info = raw[info_start:info_end]
    has_v1_layout = meta_version != 2 or b"pieces" in info
    infohash_v1 = hashlib.sha1(raw_info).hexdigest() if has_v1_layout else None
    infohash_v2 = hashlib.sha256(raw_info).hexdigest() if meta_version == 2 else None
    canonical = infohash_v1 or infohash_v2
    if not canonical:
        raise BencodeError("torrent has no supported identity")
    info_name = text(info.get(b"name.utf-8") or info.get(b"name"))
    summary = manifest_summary(files)
    record = {
        "torrentPath": filename,
        "torrentBytes": len(raw),
        "creationDateUtc": datetime.fromtimestamp(root[b"creation date"], timezone.utc).isoformat()
            if isinstance(root.get(b"creation date"), int) and root[b"creation date"] >= 0 else None,
        "createdBy": text(root.get(b"created by")) or None,
        "infoHash": canonical,
        "infoHashV1": infohash_v1,
        "infoHashV2": infohash_v2,
        "metaVersion": 2 if meta_version == 2 else 1,
        "hybrid": bool(infohash_v1 and infohash_v2),
        "name": info_name,
        "fileCount": len(files),
        "totalBytes": sum(item["length"] for item in files),
        "manifestSummary": {key: value for key, value in summary.items() if key != "files"},
        **classify(filename, info_name, files, policy),
    }
    if include_files:
        record["files"] = summary["files"]
    return record


def inspect(path: Path, include_files: bool, policy: dict | None = None) -> dict:
    raw = read_torrent_file(path)
    record = inspect_bytes(raw, filename=path.name, include_files=include_files, policy=policy)
    stat = path.stat()
    record["torrentPath"] = str(path)
    record["mtimeUtc"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return record


def iter_torrents(root: Path, name_regex: re.Pattern | None = None):
    if root.is_file():
        if root.suffix.lower() == ".torrent" and (not name_regex or name_regex.search(root.name)):
            yield root
        return
    for directory, _, filenames in os.walk(root):
        for filename in sorted(filenames):
            if filename.lower().endswith(".torrent") and (not name_regex or name_regex.search(filename)):
                yield Path(directory) / filename


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-files", action="store_true")
    parser.add_argument("--name-regex", help="case-insensitive filename filter applied before parsing")
    parser.add_argument("--config", type=Path, help="ANM schema-v2 config; controls enabled source classes and group aliases")
    args = parser.parse_args()
    name_regex = re.compile(args.name_regex, re.IGNORECASE) if args.name_regex else None
    policy = None
    if args.config:
        from ..config.loader import load_config
        policy = load_config(args.config, args.config.parent / "state" / "config-cache.json")[0]["torrentPolicy"]
    records, errors = [], []
    for path in iter_torrents(args.root, name_regex):
        if args.limit and len(records) + len(errors) >= args.limit:
            break
        try:
            records.append(inspect(path, args.include_files, policy))
        except Exception as exc:  # keep a large inventory resumable/auditable
            errors.append({"torrentPath": str(path), "error": f"{type(exc).__name__}: {exc}"})
    payload = {
        "schemaVersion": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "root": str(args.root),
        "records": records,
        "errors": errors,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
