"""Stable, filesystem-free edition grouping for local playback candidates."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def attributes(path: Path) -> tuple[str, ...]:
    name = path.stem
    group = re.match(r'^\[([^\]]{2,100})\]', name)
    group_name = group[1] if group and not re.fullmatch(r'(?i)[0-9.]+|(?:1080|720|2160)p', group[1]) else ''
    context = ' '.join([path.parent.name, name])
    source = 'BDRip' if re.search(r'(?i)(?<![a-z0-9])(?:bd(?:rip)?|blu[ ._-]?ray)(?![a-z0-9])', context) else (
        'DVDRip' if re.search(r'(?i)(?<![a-z0-9])dvd(?:rip)?(?![a-z0-9])', context) else (
            'WEB' if re.search(r'(?i)(?<![a-z0-9])web(?:[ ._-]?(?:dl|rip))?(?![a-z0-9])', context) else ''))
    resolution = re.search(r'(?i)(2160|1080|720|480)[pi]?|(?:1920|1280|3840)[x×](1080|720|2160)', context)
    quality = (resolution[2] or resolution[1]) + 'p' if resolution else ''
    codec = re.search(r'(?i)(?<![a-z0-9])(x26[45]|hevc|avc)(?![a-z0-9])', context)
    languages = [label for label, pattern in (('CHS', r'(?i)(?<![a-z0-9])(?:chs|sc|gb)(?![a-z0-9])|简体'),
                                              ('CHT', r'(?i)(?<![a-z0-9])(?:cht|tc|big5)(?![a-z0-9])|繁體|繁体'))
                 if re.search(pattern, context)]
    parent = path.parent
    # Disc/episode subdivision belongs to the same release, not a new version.
    while re.fullmatch(r'(?i)(?:dis[ck]|bd|dvd|vol(?:ume)?|ep(?:isode)?)[ ._-]*\d+|\d+', parent.name):
        parent = parent.parent
    return (os.path.normcase(str(parent)), group_name, source, quality, codec[1].lower() if codec else '', '+'.join(languages))


def _render(parts: tuple[str, ...]) -> tuple[str, str]:
    identifier = hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()[:24]
    labels = list(dict.fromkeys(value for value in parts[1:] if value))
    return identifier, ' · '.join(labels) or Path(parts[0]).name


def identify(path: Path) -> tuple[str, str]:
    return _render(attributes(path))


def classify(paths: list[Path]) -> dict[Path, tuple[str, str]]:
    rows = {path: attributes(path) for path in paths}
    options: dict[tuple[str, ...], dict[int, set[str]]] = {}
    for parts in rows.values():
        bucket = options.setdefault(parts[:3], {3: set(), 4: set()})
        for index in bucket:
            if parts[index]:
                bucket[index].add(parts[index])
    result = {}
    for path, original in rows.items():
        parts = list(original)
        for index, values in options[original[:3]].items():
            # Missing technical fields are compatible only with one known value
            # within the same folder, release group and source medium.
            if not parts[index] and len(values) == 1:
                parts[index] = next(iter(values))
        result[path] = _render(tuple(parts))
    return result


def quality(path: Path) -> tuple[int, int]:
    _, label = identify(path)
    resolution = re.search(r'(2160|1080|720|480)p', label)
    return (2 if 'BDRip' in label else 1 if 'WEB' in label else 0,
            int(resolution[1]) if resolution else 0)
