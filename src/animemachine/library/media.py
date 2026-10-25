"""Shared main-feature classification for local, external and Torrent media."""
from __future__ import annotations

import re
from pathlib import Path


VIDEO_SUFFIXES = frozenset({'.mkv', '.mp4', '.m2ts', '.ts', '.avi', '.mov', '.webm', '.flv', '.wmv', '.ogm',
                            '.m4v', '.mpg', '.mpeg', '.ogv', '.asf', '.rm', '.rmvb', '.vob'})
BONUS = re.compile(
    r'(?i)(?:^|[/\\\s._\-\[\](){}&+,])(?:(?:ncop|nced|op|ed)\d{0,3}(?:pv|full|short)|ncop|nced|op(?:ening)?|ed(?:ing)?|cm|pv|menu|preview|trailer|yokoku|tokuten|'
    r'promotional|sp|creditless|mini(?:ature)?(?:[ ._-]+character)?[ ._-]+anime|'
    r'picture[ ._-]+drama|short[ ._-]+anime|series[ ._-]+extras|映像特典|予告|特典映像|特典|番宣|'
    r'メニュー|ミニアニメ|ピクチャードラマ)(?:\d{1,3})?(?:[/\\\s._\-\[\](){}&+,]|$)'
)


def is_bonus_video(value: str) -> bool:
    path = Path(value.replace('\\', '/'))
    generic = r'(?:specials?|samples?|bonus|extras?)'
    stem = re.sub(r'^\[[^\]]+\][ ._-]*', '', path.stem)
    return bool(BONUS.search(value) or
                any(re.fullmatch(r'(?i)' + generic + r'|sps?|pvs?|ncops?|nceds?|menus?|trailers?|previews?|bdmv|video_ts', part) for part in path.parts[:-1]) or
                re.search(r'(?i)^' + generic + r'(?:[ ._-]*\d+|$)', stem) or
                re.search(r'(?i)[\[({]' + generic + r'(?:[ ._-]*\d+)?[\])}]', path.stem) or
                re.search(r'(?i)(?:^|[ ._-])' + generic + r'(?:[ ._-]*\d+)?$', path.stem))


def is_main_video(path: Path, file_kind: str | None = None) -> bool:
    if path.suffix.casefold() not in VIDEO_SUFFIXES or (file_kind and file_kind != 'main_video'):
        return False
    # A verified inventory classified its path relative to the work directory.
    # Words in a series/work's own title must not turn every episode into a bonus.
    value = path.name if file_kind == 'main_video' else path.as_posix()
    return not is_bonus_video(value)
