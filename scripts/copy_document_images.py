#!/usr/bin/env python3
"""Copy documentation assets without operating-system folder metadata."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    shutil.copytree(
        args.source,
        args.destination,
        ignore=shutil.ignore_patterns("Thumbs.db", ".DS_Store", "desktop.ini"),
    )


if __name__ == "__main__":
    main()
