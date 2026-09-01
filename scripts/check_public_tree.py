#!/usr/bin/env python3
"""Fail source/release checks on runtime state, credentials, private paths or secrets."""
from __future__ import annotations

import argparse
import io
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "dist", "build", "__pycache__", ".codex", ".agents"}
SOURCE_PRIVATE_DIRS = {".local", ".anm-history", "archive", "audit", "state"}
SOURCE_PRIVATE_FILES = {"AGENTS.md", "config.json"}
SOURCE_PRIVATE_PREFIXES = {"deploy/private", "tools/private-library"}
FORBIDDEN_NAMES = {"config.json", ".env", ".env.local"}
ALLOWED_RUNTIME_FIXTURES = {"tests/fixtures/anime-catalog.sqlite3"}
ALLOWED_SECRET_FIXTURES = {"tests/unit/test_anm_cli.py", "tests/unit/test_auth.py"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".ps1", ".cmd", ".sh", ".command", ".example", ".txt"}
SECRET = re.compile(r"(?i)(?:api[_-]?key|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_+/=-]{20,}")
PRIVATE = re.compile(r"(?:\\\\192\.168\.|[A-Z]:\\(?:Users|Codex)\\)", re.I)
RUNTIME_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3", ".log", ".tmp"}
THIRD_PARTY_APP_PREFIX = "app/"
OWN_APP_PREFIX = "app/animemachine/"


def _relative_name(name: str) -> str:
    parts = list(PurePosixPath(name.replace("\\", "/")).parts)
    if len(parts) > 1 and parts[0].lower().startswith("animemachine-"):
        parts = parts[1:]
    return "/".join(parts)


def _filename_failures(relative: str) -> list[str]:
    p = PurePosixPath(relative)
    failures: list[str] = []
    parts = p.parts
    if not relative or any(part in SKIP_DIRS for part in parts):
        return failures
    if relative.startswith("deploy/private/") or relative.startswith("tools/private-library/"):
        failures.append(f"private publication path: {relative}")
    if ".local" in parts:
        failures.append(f"runtime state directory: {relative}")
    if p.name in FORBIDDEN_NAMES and relative != "config/config.example.json":
        failures.append(f"private filename: {relative}")
    if p.suffix.casefold() in RUNTIME_SUFFIXES and relative not in ALLOWED_RUNTIME_FIXTURES:
        failures.append(f"runtime artifact: {relative}")
    return failures


def _content_failures(relative: str, data: bytes) -> list[str]:
    # Local Windows Releases vendor locked dependencies under app/.  Their
    # source may legitimately contain password/token parser fixtures, so apply
    # project-secret heuristics only to AnimeMachine's own installed package.
    if relative.startswith(THIRD_PARTY_APP_PREFIX) and not relative.startswith(OWN_APP_PREFIX):
        return []
    suffix = PurePosixPath(relative).suffix.casefold()
    if suffix not in TEXT_SUFFIXES:
        return []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return []
    failures: list[str] = []
    name = PurePosixPath(relative).name
    if SECRET.search(text) and name not in {".env.example", ".env.local.example"} and relative not in ALLOWED_SECRET_FIXTURES:
        failures.append(f"possible secret: {relative}")
    if PRIVATE.search(text):
        failures.append(f"private path: {relative}")
    return failures


def _directory_entries(root: Path) -> Iterable[tuple[str, bytes]]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        parts = PurePosixPath(relative).parts
        if (any(part in SKIP_DIRS or part in SOURCE_PRIVATE_DIRS for part in parts)
                or relative in SOURCE_PRIVATE_FILES
                or any(relative == prefix or relative.startswith(prefix + "/")
                       for prefix in SOURCE_PRIVATE_PREFIXES)):
            continue
        yield relative, path.read_bytes()


def _archive_entries(path: Path) -> Iterable[tuple[str, bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                yield _relative_name(info.filename), archive.read(info)
        return
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is not None:
                    yield _relative_name(member.name), stream.read()
    except tarfile.TarError as exc:
        raise ValueError(f"unsupported archive: {path}") from exc


def scan(target: Path) -> list[str]:
    target = target.resolve(strict=True)
    entries = _directory_entries(target) if target.is_dir() else _archive_entries(target)
    failures: list[str] = []
    for relative, data in entries:
        failures.extend(_filename_failures(relative))
        failures.extend(_content_failures(relative, data))
    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", type=Path, default=[ROOT])
    args = parser.parse_args()
    failures: list[str] = []
    for target in args.targets:
        for failure in scan(target):
            failures.append(f"{target}: {failure}")
    if failures:
        print("\n".join(sorted(set(failures))))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
