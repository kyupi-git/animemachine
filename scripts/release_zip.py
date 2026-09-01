"""Create a Release ZIP while preserving portable executable bits."""

from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path


EXECUTABLE_SUFFIXES = {".sh", ".command"}


def _entry(path: Path, archive_name: str, *, directory: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo.from_file(path, archive_name)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    if directory:
        info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
    else:
        mode = 0o755 if path.suffix in EXECUTABLE_SUFFIXES else 0o644
        info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def create_archive(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for path in sorted((source, *source.rglob("*")), key=lambda item: item.as_posix()):
            relative = path.relative_to(source.parent).as_posix()
            if path.is_dir():
                archive.writestr(_entry(path, f"{relative}/", directory=True), b"")
                continue
            info = _entry(path, relative, directory=False)
            with path.open("rb") as reader, archive.open(info, "w") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    create_archive(args.source, args.destination)


if __name__ == "__main__":
    main()
