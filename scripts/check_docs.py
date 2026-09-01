#!/usr/bin/env python3
"""Validate local Markdown links, images, and translated document structure."""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+['\"][^'\"]*['\"])?\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
TRANSLATED_FAMILIES = (
    ("README.md", "README.en.md", "README.ja.md"),
    ("docs/guide.md", "docs/guide.en.md", "docs/guide.ja.md"),
    ("docs/architecture.md", "docs/architecture.en.md", "docs/architecture.ja.md"),
)


def markdown_files() -> list[Path]:
    return sorted([*ROOT.glob("*.md"), *ROOT.joinpath("docs").glob("*.md")])


def local_target(source: Path, raw: str) -> Path | None:
    value = urllib.parse.unquote(raw.strip()).split("#", 1)[0].split("?", 1)[0]
    if not value or value.startswith(("#", "mailto:", "data:")):
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    return (source.parent / value).resolve()


def main() -> int:
    errors: list[str] = []
    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw = match.group(1) or match.group(2) or ""
            target = local_target(source, raw)
            if target is None:
                continue
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(f"{source.relative_to(ROOT)}: link escapes repository: {raw}")
                continue
            if not target.exists():
                errors.append(f"{source.relative_to(ROOT)}: missing local target: {raw}")
    for family in TRANSLATED_FAMILIES:
        paths = [ROOT / name for name in family]
        missing = [name for name, path in zip(family, paths) if not path.is_file()]
        if missing:
            errors.append("missing translated document: " + ", ".join(missing))
            continue
        levels = [[len(mark) for mark in HEADING_RE.findall(path.read_text(encoding="utf-8"))] for path in paths]
        if levels[1:] != [levels[0], levels[0]]:
            errors.append("translated heading structure differs: " + ", ".join(family))
    if errors:
        print("Documentation validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"Documentation validation passed ({len(markdown_files())} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
