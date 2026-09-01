from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_info.py"


class BuildInfoTests(unittest.TestCase):
    def test_version_and_build_info_share_the_canonical_source(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        printed = subprocess.run(
            [sys.executable, str(SCRIPT), "--print-version"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(version, printed)
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "BUILD-INFO.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output",
                    str(output),
                    "--build-type",
                    "test",
                    "--platform",
                    "test-platform",
                ],
                check=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(version, payload["version"])
        self.assertEqual("test", payload["build_type"])
        self.assertEqual("test-platform", payload["platform"])
        self.assertRegex(payload["git_commit"], r"^(?:[0-9a-f]{40}|unknown)$")
        self.assertRegex(payload["build_timestamp"], r"^\d{4}-\d{2}-\d{2}T")

    def test_mismatched_version_and_tag_are_rejected(self) -> None:
        for option in ("--check-version", "--check-tag"):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), option, "v9.9.9"], capture_output=True, text=True
            )
            self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
