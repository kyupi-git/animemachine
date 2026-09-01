#!/usr/bin/env python3
"""Read the canonical version and generate Release build metadata."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.-]+)?")


def project_version() -> str:
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"invalid VERSION: {value!r}")
    return value


def git_value(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_timestamp() -> str:
    epoch = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    if epoch.isdigit():
        value = dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
    else:
        committed = git_value("show", "-s", "--format=%cI", "HEAD")
        try:
            value = dt.datetime.fromisoformat(committed) if committed else dt.datetime.now(dt.timezone.utc)
        except ValueError:
            value = dt.datetime.now(dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_repository_version(version: str) -> None:
    """Reject current publication metadata that drifted from VERSION."""
    paths = [
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "README.ja.md",
        ROOT / "deploy" / "compose" / "torrent-collector.yaml",
        *(ROOT / "deploy" / "compose").glob("[0-9][0-9]-*/compose.yaml"),
        *(ROOT / "deploy" / "compose").glob("[0-9][0-9]-*/.env.example"),
    ]
    pattern = re.compile(r"ghcr\.io/kyupi-git/animemachine:(\d+\.\d+\.\d+)")
    mismatches: list[str] = []
    for path in paths:
        for found in pattern.findall(path.read_text(encoding="utf-8")):
            if found != version:
                mismatches.append(f"{path.relative_to(ROOT)}={found}")
    if mismatches:
        raise ValueError("publication version drift: " + ", ".join(mismatches))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-version", action="store_true")
    parser.add_argument("--check-version")
    parser.add_argument("--check-tag")
    parser.add_argument("--check-repository", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-type", default="local-release")
    parser.add_argument("--platform", dest="target_platform", default=platform.system().casefold())
    parser.add_argument("--python-version", default=platform.python_version())
    args = parser.parse_args()
    version = project_version()
    if args.print_version:
        print(version)
    if args.check_version and args.check_version != version:
        raise SystemExit(f"requested version {args.check_version!r} does not match VERSION {version!r}")
    if args.check_tag and args.check_tag.removeprefix("v") != version:
        raise SystemExit(f"tag {args.check_tag!r} does not match VERSION {version!r}")
    if args.check_repository:
        check_repository_version(version)
    if args.output:
        payload = {
            "version": version,
            "git_commit": git_value("rev-parse", "HEAD") or os.getenv("GITHUB_SHA", "unknown"),
            "python_version": args.python_version,
            "platform": args.target_platform,
            "build_timestamp": build_timestamp(),
            "build_type": args.build_type,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
