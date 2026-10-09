"""Bounded content validation shared by every network consumer."""
from __future__ import annotations

import hashlib
import io
import json
import warnings
from typing import Any


IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")


def json_bytes(data: bytes, *, limit: int = 16 * 1024 * 1024) -> Any:
    if len(data) > limit:
        raise ValueError("JSON response exceeds configured limit")
    return json.loads(data.decode("utf-8-sig"))


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
