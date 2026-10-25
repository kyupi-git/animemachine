"""Bounded, checksummed offline envelopes for existing release installers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile
from typing import Any


MAX_BYTES = 1024 * 1024 * 1024


def create(asset: Path, output: Path, *, version: str, kind: str) -> None:
    if kind not in {'docker', 'portable'} or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('invalid offline package metadata')
    size = asset.stat().st_size
    if not 0 < size < MAX_BYTES - 65536:
        raise ValueError('offline package size is invalid')
    with asset.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'format': 1, 'version': version, 'kind': kind, 'asset': asset.name, 'size': size, 'sha256': digest}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            archive.write(asset, asset.name)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def unpack(package: Path, destination: Path, *, mode: str, current_version: str,
           asset_name: Any, allow_same: bool = False) -> dict[str, Any]:
    from .application_update import _version_tuple

    if not 0 < package.stat().st_size <= MAX_BYTES:
        raise ValueError('offline_package_invalid')
    try:
        with zipfile.ZipFile(package) as archive:
            manifest_info = archive.getinfo('manifest.json')
            if manifest_info.file_size > 65536:
                raise ValueError('offline_package_invalid')
            manifest = json.loads(archive.read(manifest_info))
            if not isinstance(manifest, dict) or manifest.get('format') != 1:
                raise ValueError('offline_package_invalid')
            version = str(manifest.get('version') or '')
            target = _version_tuple(version)
            if target is None or version != '.'.join(map(str, target)):
                raise ValueError('offline_package_invalid')
            if target < _version_tuple(current_version) or (target == _version_tuple(current_version) and not allow_same):
                raise ValueError('offline_package_not_newer')
            name = asset_name(version, mode)
            if manifest.get('kind') != mode or not name or manifest.get('asset') != name:
                raise ValueError('offline_package_platform_mismatch')
            if len(archive.infolist()) != 2 or set(archive.namelist()) != {'manifest.json', name}:
                raise ValueError('offline_package_invalid')
            info = archive.getinfo(name)
            size = manifest.get('size')
            digest = str(manifest.get('sha256') or '')
            if not isinstance(size, int) or not 0 < size <= MAX_BYTES or size != info.file_size or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('offline_package_invalid')
            target_path = destination / name
            hasher, written = hashlib.sha256(), 0
            with archive.open(info) as source, target_path.open('xb') as output:
                while block := source.read(1024 * 1024):
                    written += len(block)
                    if written > size:
                        raise ValueError('offline_package_invalid')
                    hasher.update(block)
                    output.write(block)
            if written != size or hasher.hexdigest() != digest:
                raise ValueError('offline_package_checksum_failed')
            return {'latestVersion': version, 'asset': {'name': name, 'size': size, 'sha256': digest},
                    'updateAvailable': True, 'canUpdate': True, 'offline': True}
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('offline_package_invalid') from exc
