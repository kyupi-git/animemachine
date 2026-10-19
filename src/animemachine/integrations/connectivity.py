"""Read-only connectivity probes for AnimeMachine-managed services."""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
from ..network import tls as tls_support
from ..network.validators import service_endpoint
from .. import __version__


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": f"AnimeMachine/{__version__} (ANM)", **(headers or {})})
    try:
        with tls_support.urlopen(request, timeout=8, max_bytes=1024 * 1024, follow_redirects=False) as response:
            return int(response.status), response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(4096).decode("utf-8", "replace")
    except httpx.HTTPStatusError as exc:
        return int(exc.response.status_code), exc.response.content[:4096].decode("utf-8", "replace")


def _secret(environment_name: str, file_environment_name: str) -> str:
    value = os.getenv(environment_name, "").strip()
    path = Path(os.getenv(file_environment_name, ""))
    if not value and str(path) not in {"", "."} and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    return value


def probe(kind: str, endpoint: str, api_key: str | None = None, *, use_saved_key: bool = True) -> dict[str, Any]:
    """Probe only read endpoints. A supplied key is used only for this request."""
    base = service_endpoint(endpoint)
    if kind == "qbittorrent":
        key = str(api_key or "").strip() or (_secret("ANM_QBT_API_KEY", "ANM_QBT_API_KEY_FILE") if use_saved_key else "")
        headers = {"Authorization": f"Bearer {key}", "X-API-Key": key} if key else {}
        status, body = _get(base + "/api/v2/app/version", headers)
        good = 200 <= status < 300
        return {"kind": kind, "reachable": status < 500, "authenticated": good,
                "status": status, "version": body.strip() if good else None,
                "message": "ok" if good else ("credentials_required" if status in {401, 403} else
                                               "service_redirect" if 300 <= status < 400 else "connection_failed")}
    raise ValueError("unsupported connection kind")
