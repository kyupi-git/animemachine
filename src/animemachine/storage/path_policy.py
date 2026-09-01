"""Canonical media-path authorization shared by playback and subtitle features."""
from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator


class PathAuthorizationError(ValueError):
    pass


def canonical_existing(path: Path | str) -> Path:
    candidate = Path(path)
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathAuthorizationError("path is unavailable") from exc


def identity(path: Path | str) -> str:
    return os.path.normcase(str(canonical_existing(path)))


def is_within(path: Path | str, root: Path | str) -> bool:
    try:
        candidate = canonical_existing(path)
        boundary = canonical_existing(root)
        candidate.relative_to(boundary)
        return True
    except (PathAuthorizationError, ValueError):
        return False


def authorize_existing(path: Path | str, roots: Iterable[Path | str]) -> Path:
    candidate = canonical_existing(path)
    for root in roots:
        try:
            boundary = canonical_existing(root)
            candidate.relative_to(boundary)
            return candidate
        except (PathAuthorizationError, ValueError):
            continue
    raise PathAuthorizationError("path is outside configured media roots")

def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    if left.st_dev and left.st_ino and right.st_dev and right.st_ino:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino
    return (
        left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_mode == right.st_mode
    )


@contextlib.contextmanager
def open_authorized(path: Path | str, roots: Iterable[Path | str]) -> Iterator[tuple[BinaryIO, Path, os.stat_result]]:
    """Open an authorized regular file and verify the path still names that file."""
    boundaries = tuple(roots)
    candidate = authorize_existing(path, boundaries)
    try:
        stream = candidate.open("rb")
    except OSError as exc:
        raise PathAuthorizationError("path is unavailable") from exc
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise PathAuthorizationError("path is not a regular file")
        current = authorize_existing(path, boundaries)
        try:
            current_stat = current.stat()
        except OSError as exc:
            raise PathAuthorizationError("path is unavailable") from exc
        if not _same_file(opened, current_stat):
            raise PathAuthorizationError("path changed during authorization")
        yield stream, current, opened
    finally:
        stream.close()

