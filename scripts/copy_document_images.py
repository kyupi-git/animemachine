#!/usr/bin/env python3
"""Copy portable documentation, using still previews for README demonstration GIFs."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


DEMO_PREVIEWS = {"demo_radar.gif": "release-radar", "demo_relationship.gif": "relationship-graph"}


def readme_previews(source: Path, directory: Path) -> dict[Path, str]:
    replacements = {}
    for suffix in ("", ".en", ".ja"):
        path = directory / f"README{suffix}.md"
        text = path.read_text(encoding="utf-8")
        for demo, preview in DEMO_PREVIEWS.items():
            old = f"docs/images/{demo}"
            if old not in text:
                continue
            image = f"{preview}{suffix}.png"
            if not (source / image).is_file():
                raise ValueError(f"Missing portable documentation preview: {image}")
            text = text.replace(old, f"docs/images/{image}")
        replacements[path] = text
    # A future manual chapter may need an animation; never leave a broken link.
    for path in directory.rglob("*.md"):
        text = replacements.get(path)
        text = path.read_text(encoding="utf-8") if text is None else text
        if any(name in text for name in DEMO_PREVIEWS):
            raise ValueError(f"Demonstration GIF is referenced outside the portable README previews: {path.name}")
    return replacements


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--readme-dir", type=Path, help="Use local still previews in the staged READMEs")
    args = parser.parse_args()
    replacements = readme_previews(args.source, args.readme_dir) if args.readme_dir else {}
    shutil.copytree(
        args.source,
        args.destination,
        ignore=shutil.ignore_patterns("Thumbs.db", ".DS_Store", "desktop.ini",
                                     *(DEMO_PREVIEWS if args.readme_dir else ())),
    )
    for path, text in replacements.items():
        path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
