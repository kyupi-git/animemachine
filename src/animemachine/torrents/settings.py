"""Secret-free, derived settings for the separately running Collector."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..config.collector import settings
from ..config.loader import serial_profile_language, torrent_policy_eligible


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {'components': {'torrentCollector': settings(config)},
            'torrentPolicy': copy.deepcopy(config.get('torrentPolicy', {})),
            'ui': {'language': config.get('ui', {}).get('language', 'zh-Hans')}}


def publish(catalog: Path, config: dict[str, Any]) -> None:
    path = catalog.with_name('collector-settings.json')
    encoded = json.dumps(snapshot(config), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if path.is_file() and path.read_text(encoding='utf-8') == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='collector-settings-', suffix='.pending', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(encoded)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(catalog: Path | None, previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if catalog is None:
        return previous
    try:
        raw = catalog.with_name('collector-settings.json').read_bytes()
        if len(raw) > 1024 * 1024:
            return previous
        config = json.loads(raw)
        return snapshot(config)
    except (OSError, ValueError, TypeError, AttributeError):
        return previous


def admitted(title: str, metadata: dict[str, Any], config: dict[str, Any]) -> bool:
    resolution = metadata.get('videoHeight')
    return torrent_policy_eligible(config.get('torrentPolicy', {}),
        str(metadata.get('sourceClass') or 'unknown'), str(metadata.get('resourceGroup') or 'unknown'),
        f'{resolution}p' if resolution else 'unknown', str(metadata.get('subtitleLanguageHint') or 'unknown'),
        text=title, ui_language=serial_profile_language(config))
