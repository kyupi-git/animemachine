#!/usr/bin/env python3
"""Rebuild the optional 64-bit player bridge with Zig 0.14.1."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "src/animemachine/resources/potplayer"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zig", required=True, type=Path)
    args = parser.parse_args()
    zig = str(args.zig.resolve(strict=True))
    version = subprocess.check_output([zig, "version"], text=True).strip()
    if version != "0.14.1":
        raise RuntimeError("use Zig 0.14.1 for the published bridge")
    source = FOLDER / "anm_playback.c"
    flags = ["-target", "x86_64-windows-gnu", "-shared", "-O2", "-s", "-Wall", "-Wextra", "-Werror"]
    with tempfile.TemporaryDirectory(prefix="anm-player-build-") as raw:
        output = Path(raw) / "anm_playback.dll"
        subprocess.run([zig, "cc", *flags, "-o", str(output), str(source), "-luser32", "-lwinhttp"], check=True)
        body = output.read_bytes()
    (FOLDER / "anm_playback.dll").write_bytes(body)
    manifest = {"compiler": f"Zig {version}", "flags": flags,
                "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "dllSha256": hashlib.sha256(body).hexdigest()}
    (FOLDER / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("PotPlayer bridge built and checksummed.")


if __name__ == "__main__":
    main()
