"""Small real image payloads for security/asset tests."""

from __future__ import annotations

import io

from PIL import Image


def image_bytes(format_name: str = "PNG", *, color: str = "red") -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (2, 2), color=color)
    image.save(buffer, format=format_name)
    return buffer.getvalue()


def png_bytes(*, color: str = "red") -> bytes:
    return image_bytes("PNG", color=color)


def jpeg_bytes(*, color: str = "red") -> bytes:
    return image_bytes("JPEG", color=color)


def webp_bytes(*, color: str = "red") -> bytes:
    return image_bytes("WEBP", color=color)
