"""Bounded content validation shared by every network consumer."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import urllib.parse
import warnings
from typing import Any


IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")
IMAGE_ACCEPT = "image/webp,image/jpeg,image/png,image/gif"


def service_endpoint(value: str) -> str:
    """Validate an administrator-selected service; keep NAS/loopback/Docker access."""
    value = str(value).strip().rstrip("/")
    if any(ord(character) < 33 or ord(character) == 127 for character in value) or "\\" in value:
        raise ValueError("invalid_service_endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("invalid_service_endpoint") from exc
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or port == 0):
        raise ValueError("invalid_service_endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Docker service names and local DNS names intentionally remain valid.
        if host.casefold().rstrip(".") in {"metadata.google.internal", "metadata.goog"}:
            raise ValueError("invalid_service_endpoint")
    else:
        address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError("invalid_service_endpoint")
    if any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/")):
        raise ValueError("invalid_service_endpoint")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def json_bytes(data: bytes, *, limit: int = 16 * 1024 * 1024) -> Any:
    if len(data) > limit:
        raise ValueError("JSON response exceeds configured limit")
    return json.loads(data.decode("utf-8-sig"))


def bangumi_subject(payload: Any, subject_id: int) -> dict[str, Any]:
    """Reject error/mismatched mirror responses before accepting a cover source."""
    candidates = [payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("id")) == str(subject_id) and any(key in candidate for key in ("name", "name_cn")):
            if candidate.get("images") is not None and not isinstance(candidate["images"], dict):
                raise ValueError("invalid Bangumi cover metadata")
            return candidate
        candidates.extend(candidate[key] for key in ("data", "subject", "result") if isinstance(candidate.get(key), dict))
    raise ValueError("Bangumi subject response does not match the requested title")


def image_bytes(data: bytes, mime: str, *, limit: int = 12 * 1024 * 1024,
                max_pixels: int = 40_000_000) -> tuple[bytes, str]:
    if not data or len(data) > limit or not data.startswith(IMAGE_MAGIC):
        raise ValueError("invalid or oversized image response")
    # Validate the decoded content and detected format, preserving the original
    # bytes. Covers must not lose detail every time they cross an integration.
    return cached_image_bytes(data, mime, limit=limit, max_pixels=max_pixels)


def cached_image_bytes(data: bytes, mime: str, *, limit: int = 12 * 1024 * 1024,
                       max_pixels: int = 40_000_000) -> tuple[bytes, str]:
    """Validate an already-normalized cached image without recompressing it."""
    if not data or len(data) > limit or not data.startswith(IMAGE_MAGIC):
        raise ValueError("invalid or oversized cached image")
    from PIL import Image
    content_types = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width <= 0 or image.height <= 0 or image.width * image.height > max_pixels:
                    raise ValueError("cached image pixel count exceeds safety limit")
                detected = content_types.get(str(image.format or "").upper())
                if not detected:
                    raise ValueError("unsupported cached image format")
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("cached image pixel count exceeds safety limit") from exc
    return data, detected


def digest(data: bytes, expected_size: int | None = None, expected_sha256: str | None = None) -> str:
    if expected_size is not None and len(data) != int(expected_size):
        raise ValueError("asset size mismatch")
    actual = hashlib.sha256(data).hexdigest()
    if expected_sha256 and actual.casefold() != expected_sha256.casefold():
        raise ValueError("asset digest mismatch")
    return actual
