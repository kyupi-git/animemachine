"""Release-stream identities shared by Torrent and Ani-RSS selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
import re

from .metainfo import classify


PLATFORM = r"(?<![A-Za-z0-9])(?:Baha|Bahamut|CR|Crunchyroll|NF|Netflix|ABEMA|AMZN|Amazon|ADN|ViuTV|Disney[ +]*|DSNP|B[ ._-]*Global|BiliBili|U[ ._-]*NEXT|Hulu|DMM|FOD)(?![A-Za-z0-9])"
CODECS = {'HEVC': r'HEVC|[hx][ .-]?265', 'AVC': r'AVC|[hx][ .-]?264', 'AV1': r'AV1'}
CONTAINERS = r'(?<![A-Za-z0-9])(?:MKV|MP4|M2TS|TS|AVI)(?![A-Za-z0-9])'
SUB_MODES = {'soft': r'内封|內封|soft[ ._-]*sub', 'hard': r'内嵌|內嵌|hard[ ._-]*sub',
             'external': r'外挂|外掛|external[ ._-]*sub'}
SUBS = {
    'CHS': r'(?<![A-Za-z0-9])(?:CHS|SC|GB)(?![A-Za-z0-9])|简体|简中|简日|简繁|簡體|簡中|簡日|簡繁|zh[ ._-]*hans',
    'CHT': r'(?<![A-Za-z0-9])(?:CHT|TC|BIG5)(?![A-Za-z0-9])|繁体|繁中|繁日|简繁|繁體|簡繁|zh[ ._-]*hant',
}


@dataclass(frozen=True)
class Variant:
    platform: str
    subtitle: str
    resolution: int | None
    codec: str
    bitDepth: int | None
    container: str = ''
    subtitleMode: str = ''

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def stream(self) -> str:
        # Existing resolution/subtitle/bit-depth fields remain authoritative.
        return ':'.join((self.platform, self.codec, self.container, self.subtitleMode)) if any(
            (self.platform, self.codec, self.container, self.subtitleMode)) else ''

    def as_dict(self) -> dict:
        return asdict(self)

    def patterns(self) -> list[str]:
        """Ani-RSS applies every match expression (AND); expressions also work in Java."""
        patterns = []
        platform = PLATFORM
        if self.platform:
            aliases = {'Baha': 'Baha|Bahamut', 'CR': 'CR|Crunchyroll', 'NF': 'NF|Netflix',
                       'AMZN': 'AMZN|Amazon', 'DSNP': r'Disney[ +]*|DSNP',
                       'B-Global': r'B[ ._-]*Global', 'U-NEXT': r'U[ ._-]*NEXT'}
            platform = r'(?<![A-Za-z0-9])(?:' + aliases.get(self.platform, re.escape(self.platform)) + r')(?![A-Za-z0-9])'
        patterns.append('(?i)' + platform if self.platform else '(?i)^(?!.*(?:' + platform + ')).*$')
        if self.resolution:
            patterns.append(r'(?i)(?<!\d)' + str(self.resolution) + r'[pi]?(?!\d)')
        else:
            patterns.append(r'(?i)^(?!.*(?<!\d)(?:4320|2160|1440|1080|720|576|540|480)[pi](?!\d)).*$')
        if self.codec:
            patterns.append(r'(?i)(?<![A-Za-z0-9])(?:' + CODECS[self.codec] + r')(?![A-Za-z0-9])')
        else:
            patterns.append(r'(?i)^(?!.*(?<![A-Za-z0-9])(?:' + '|'.join(CODECS.values()) + r')(?![A-Za-z0-9])).*$')
        patterns.append(r'(?i)(?<![A-Za-z0-9])' + self.container + r'(?![A-Za-z0-9])' if self.container
                        else '(?i)^(?!.*(?:' + CONTAINERS + ')).*$')
        for name, pattern in SUB_MODES.items():
            patterns.append('(?i)' + pattern if name in self.subtitleMode.split('+')
                            else '(?i)^(?!.*(?:' + pattern + ')).*$')
        for tag, pattern in SUBS.items():
            if tag in self.subtitle:
                patterns.append('(?i)' + pattern)
            else:
                patterns.append('(?i)^(?!.*(?:' + pattern + ')).*$')
        if self.bitDepth:
            bits = r'(?:10[ .-]*bit|hi10p|ma10p|main10)' if self.bitDepth == 10 else str(self.bitDepth) + r'[ .-]*bit'
            patterns.append(r'(?i)(?<!\d)' + bits + r'(?![A-Za-z0-9])')
        else:
            patterns.append(r'(?i)^(?!.*(?<!\d)(?:8[ .-]*bit|10[ .-]*bit|12[ .-]*bit|hi10p|ma10p|main10)(?![A-Za-z0-9])).*$')
        return patterns


@lru_cache(maxsize=4096)
def release_variant(title: str) -> Variant:
    parsed = classify('', title, [], {})
    platforms = [match.group().upper() for match in re.finditer(PLATFORM, title, re.I)]
    canonical = {'BAHA':'Baha', 'BAHAMUT':'Baha', 'CRUNCHYROLL':'CR', 'NETFLIX':'NF',
                 'AMAZON':'AMZN', 'DISNEY':'DSNP', 'VIUTV':'ViuTV', 'HULU':'Hulu', 'BILIBILI':'BiliBili'}
    platform = canonical.get(platforms[0].strip(' +'), platforms[0].strip(' +')) if platforms else ''
    if re.fullmatch(r'B[ ._-]*GLOBAL', platform):
        platform = 'B-Global'
    elif re.fullmatch(r'U[ ._-]*NEXT', platform):
        platform = 'U-NEXT'
    subtitles = '+'.join(tag for tag, pattern in SUBS.items() if re.search(pattern, title, re.I)) or 'Unknown'
    codec = next((name for name, pattern in CODECS.items() if re.search(
        r'(?i)(?<![A-Za-z0-9])(?:' + pattern + r')(?![A-Za-z0-9])', title)), '')
    container = re.search(CONTAINERS, title, re.I)
    mode = '+'.join(name for name, pattern in SUB_MODES.items() if re.search(pattern, title, re.I))
    return Variant(platform, subtitles, parsed['videoHeight'], codec, parsed['bitDepth'],
                   container.group().upper() if container else '', mode)
