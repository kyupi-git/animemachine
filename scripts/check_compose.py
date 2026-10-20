#!/usr/bin/env python3
"""Validate each deployment as an isolated single-file download, with no environment file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    templates = [ROOT / "deploy/compose/compose.yaml",
                 *sorted((ROOT / "deploy/compose/templates").glob("[0-9][0-9]-*/compose.yaml")),
                 ROOT / "deploy/compose/advanced/collector/compose.yaml"]
    for template in templates:
        with tempfile.TemporaryDirectory(prefix="anm-compose-") as raw:
            directory = Path(raw)
            shutil.copyfile(template, directory / "compose.yaml")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("ANM_", "QBT_", "ANI_RSS_", "TORRENT_COLLECTOR_", "COMPOSE_"))}
            env["DOCKER_CONFIG"] = str(directory / "docker-config")
            Path(env["DOCKER_CONFIG"]).mkdir()
            result = subprocess.run(["docker", "compose", "-f", str(directory / "compose.yaml"),
                                     "config", "--format", "json"], env=env, cwd=directory,
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            services = json.loads(result.stdout)["services"]
            if any("bootstrap" in name or "service_completed_successfully" in str(service)
                   for name, service in services.items()):
                raise ValueError(f"one-shot service in {template}")
            if "animemachine" in services and services["animemachine"].get("depends_on"):
                raise ValueError("optional components must not delay application startup")
            if "torrent-collector" in services and "animemachine" in services:
                app = {row["target"]: row for row in services["animemachine"]["volumes"]}
                collector = {row["target"]: row for row in services["torrent-collector"]["volumes"]}
                if app["/Torrents"]["source"] != collector["/torrents"]["source"]:
                    raise ValueError("collector and application do not share the same pool")
            if template.name == "compose.yaml" and template.parent.name == "04-full-stack":
                if (ROOT / "deploy/compose/compose.yaml").read_bytes() != template.read_bytes():
                    raise ValueError("default template has drifted from the full stack")
            print(f"OK {template.relative_to(ROOT).as_posix()}: {', '.join(services)}")


if __name__ == "__main__":
    main()
