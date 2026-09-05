#!/usr/bin/env python3
"""Canonical source-tree verification for AnimeMachine."""
from __future__ import annotations

import argparse
import compileall
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_dependencies() -> None:
    missing: list[str] = []
    for module in ("certifi", "httpx", "PIL", "truststore"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise RuntimeError("missing runtime dependencies: " + ", ".join(missing) + "; run: python -m pip install -e .[test]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-dependency-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.skip_dependency_check:
        check_dependencies()
    if not compileall.compile_dir(SRC, quiet=1) or not compileall.compile_dir(TESTS, quiet=1):
        raise RuntimeError("Python byte-compilation failed")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(SRC), str(TESTS), env.get("PYTHONPATH", "")) if value
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="anm-test-runtime-") as runtime_dir:
        env["ANM_STATE_DIR"] = str(Path(runtime_dir) / "state")
        env["ANM_CONFIG_CACHE"] = str(Path(runtime_dir) / "config-cache.json")
        for path in sorted(TESTS.rglob("test_*.py")):
            module = ".".join(path.relative_to(TESTS).with_suffix("").parts)
            with tempfile.TemporaryFile(mode="w+b") as output:
                completed = subprocess.run(
                    [sys.executable, "-m", "unittest", module],
                    cwd=ROOT, env=env, stdout=output, stderr=output, timeout=180, check=False,
                )
                output.seek(0)
                transcript = output.read().decode("utf-8", errors="replace")
            if transcript:
                sys.stdout.write(transcript)
            if completed.returncode != 0:
                raise RuntimeError(f"test module failed: {module}")
            if "resource_tracker:" in transcript and "leaked" in transcript:
                raise RuntimeError(f"multiprocessing resources leaked in: {module}")

        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for the JavaScript syntax check")
        for script in sorted((SRC / "animemachine" / "web" / "static").glob("*.js")):
            run([node, "--check", str(script)])

        env["ANM_CONFIG_PATH"] = str(SRC / "animemachine" / "resources" / "config.example.json")
        run([sys.executable, "-m", "animemachine.cli", "validate-config"], env=env)
    run([sys.executable, str(ROOT / "scripts" / "check_public_tree.py")], env=env)
    run([sys.executable, str(ROOT / "scripts" / "check_docs.py")], env=env)
    run([sys.executable, str(ROOT / "scripts" / "build_info.py"), "--check-repository"], env=env)
    print("Canonical verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
