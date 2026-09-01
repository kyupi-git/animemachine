"""Verified HTTPS using native trust plus a portable Mozilla CA fallback."""
from __future__ import annotations

import os
import ssl
import urllib.error
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    try:
        import truststore  # type: ignore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    except ImportError:
        context = ssl.create_default_context()
    try:
        import certifi  # type: ignore
        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    custom = (os.getenv("ANM_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        if not path.is_file():
            raise RuntimeError(f"configured CA bundle does not exist: {path}")
        context.load_verify_locations(cafile=str(path))
    return context


def urlopen(target: Any, **kwargs: Any) -> Any:
    from .transport import open_url
    kwargs.pop("context", None)
    return open_url(target, **kwargs)


def is_certificate_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if isinstance(current, urllib.error.URLError) and isinstance(current.reason, BaseException):
            current = current.reason
            continue
        current = current.__cause__ or current.__context__
    return False


def error_hint(exc: BaseException) -> str:
    if is_certificate_error(exc):
        return "TLS certificate verification failed; configure ANM_CA_BUNDLE for a private/proxy CA"
    return f"{type(exc).__name__}: {exc}"
