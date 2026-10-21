"""Explicit, site-scoped browser permission commands for Windows playback."""
from __future__ import annotations

import base64
from pathlib import Path
import re
import urllib.parse


def permission_command(origin: str) -> str:
    """Build a command for the user to inspect/copy; never execute it on the server."""
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", parsed.netloc)):
        raise ValueError("invalid_site_origin")
    # Also validate the port before putting this value into PowerShell source.
    _ = parsed.port
    site = f"{parsed.scheme}://{parsed.netloc}"
    script = (Path(__file__).parents[1] / "resources/windows/Allow-BrowserPlayback.ps1").read_text(encoding="utf-8")
    script = '\n'.join(line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith('#'))
    source = "& {\n" + script + "\n} -Origin '" + site + "'"
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    command = f"powershell.exe -NoLogo -NoProfile -EncodedCommand {encoded}"
    if len(command) > 8000:
        raise ValueError("site_origin_too_long")
    return command
