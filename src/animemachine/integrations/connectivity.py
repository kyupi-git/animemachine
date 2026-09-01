"""Read-only connectivity probes for AnimeMachine-managed services."""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from ..network import tls as tls_support
from .. import __version__


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": f"AnimeMachine/{__version__} (ANM)", **(headers or {})})
    try:
        with tls_support.urlopen(request, timeout=8, max_bytes=1024 * 1024) as response:
            return int(response.status), response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(4096).decode("utf-8", "replace")


def _secret(environment_name: str, file_environment_name: str) -> str:
    value = os.getenv(environment_name, "").strip()
    path = Path(os.getenv(file_environment_name, ""))
    if not value and str(path) not in {"", "."} and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    return value


def probe(kind: str, endpoint: str) -> dict[str, Any]:
    """Probe only read endpoints. Credentials stay in process environment."""
    base = endpoint.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("endpoint must start with http:// or https://")
    if kind == "qbittorrent":
        key = _secret("ANM_QBT_API_KEY", "ANM_QBT_API_KEY_FILE")
        headers = {"Authorization": f"Bearer {key}", "X-API-Key": key} if key else {}
        status, body = _get(base + "/api/v2/app/version", headers)
        return {"kind": kind, "reachable": status < 500, "authenticated": status < 400,
                "status": status, "version": body.strip() if status < 400 else None,
                "message": "ok" if status < 400 else ("credentials_required" if status in {401, 403} else "connection_failed")}
    raise ValueError("unsupported connection kind")
