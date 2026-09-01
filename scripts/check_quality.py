#!/usr/bin/env python3
"""Run the source quality gates without rewriting existing code."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(arguments, cwd=ROOT, env=env, check=True)


def main() -> int:
    run([sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"])
    run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "scripts/build_info.py",
            "scripts/check_docs.py",
            "scripts/check_quality.py",
        ]
    )
    run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "scripts/build_info.py",
            "scripts/check_docs.py",
            "scripts/check_quality.py",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="anm-quality-") as raw:
        env = os.environ.copy()
        env["ANM_STATE_DIR"] = str(Path(raw) / "state")
        env["ANM_CONFIG_CACHE"] = str(Path(raw) / "config-cache.json")
        env["COVERAGE_FILE"] = str(Path(raw) / ".coverage")
        run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--source=animemachine",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
            ],
            env=env,
        )
        run([sys.executable, "-m", "coverage", "report", "--fail-under=55"], env=env)
    print("Quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
