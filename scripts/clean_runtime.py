#!/usr/bin/env python3
"""Remove application build caches without walking data, media or virtual environments."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil


def clean(root: Path, *, include_builds: bool = False) -> int:
    root = root.resolve(strict=True)
    removed = 0

    def redirected(path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except FileNotFoundError:
            return False

    def remove(path: Path) -> None:
        nonlocal removed
        # A junction/symlink may redirect outside the application directory.
        if redirected(path):
            return
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("cleanup target escapes application directory")
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
        elif path.is_file():
            path.unlink()
            removed += 1

    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "src/animemachine.egg-info"):
        remove(root / name)
    if include_builds:
        remove(root / "dist")
    for name in ("src/animemachine", "tests", "scripts", "app/animemachine"):
        start = root / name
        if not start.is_dir() or redirected(start) or not start.resolve().is_relative_to(root):
            continue
        for directory, children, files in os.walk(start, followlinks=False):
            parent = Path(directory)
            for child in children[:]:
                path = parent / child
                if redirected(path):
                    children.remove(child)
                elif child == "__pycache__":
                    remove(path)
                    children.remove(child)
            for filename in files:
                if Path(filename).suffix in {".pyc", ".pyo"}:
                    remove(parent / filename)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--include-builds", action="store_true")
    args = parser.parse_args()
    count = clean(args.root, include_builds=args.include_builds)
    print(f"Removed {count} application cache/build entries. Media and application data were preserved.")


if __name__ == "__main__":
    main()
