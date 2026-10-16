"""Canonical media-path authorization shared by playback and subtitle features."""
from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator

from .sleep import require_awake, sleeping_path


class PathAuthorizationError(ValueError):
    pass


def is_junction(path: Path | str) -> bool:
    """Identify Windows mount-point links without following their targets."""
    try:
        info = Path(path).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    tag = getattr(info, "st_reparse_tag", None)
    return tag is not None and tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def canonical_existing(path: Path | str) -> Path:
    candidate = Path(path)
    try:
        require_awake(candidate)
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


def authorize_existing(path: Path | str, roots: Iterable[Path | str], *, config: dict[str, Any] | None = None) -> Path:
    if sleeping_path(path, config):
        raise PathAuthorizationError("local_library_sleeping")
    candidate = canonical_existing(path)
    for root in roots:
        if sleeping_path(root, config):
            continue
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


def walk_files(root: Path, *, config: dict[str, Any] | None = None) -> Iterator[Path]:
    """Walk without following links and recheck quiet hours before each directory."""
    pending = [root]
    while pending:
        require_awake(root, config)
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                require_awake(root, config)
                if entry.is_symlink() or is_junction(entry.path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


@contextlib.contextmanager
def open_authorized(path: Path | str, roots: Iterable[Path | str], *, config: dict[str, Any] | None = None) -> Iterator[tuple[BinaryIO, Path, os.stat_result]]:
    """Open an authorized regular file and verify the path still names that file."""
    boundaries = tuple(roots)
    candidate = authorize_existing(path, boundaries, config=config)
    try:
        stream = candidate.open("rb")
    except OSError as exc:
        raise PathAuthorizationError("path is unavailable") from exc
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise PathAuthorizationError("path is not a regular file")
        current = authorize_existing(path, boundaries, config=config)
        try:
            current_stat = current.stat()
        except OSError as exc:
            raise PathAuthorizationError("path is unavailable") from exc
        if not _same_file(opened, current_stat):
            raise PathAuthorizationError("path changed during authorization")
        yield stream, current, opened
    finally:
        stream.close()

