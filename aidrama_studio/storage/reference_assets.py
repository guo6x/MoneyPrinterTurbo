from __future__ import annotations

import hashlib
import re
from pathlib import Path


MAX_REFERENCE_IMAGE_BYTES = 15 * 1024 * 1024
_FORMATS = {
    ".jpg": ("image/jpeg", ".jpg"),
    ".jpeg": ("image/jpeg", ".jpg"),
    ".png": ("image/png", ".png"),
    ".webp": ("image/webp", ".webp"),
}
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def sanitize_filename(filename: str) -> str:
    """Return safe display metadata; it is never used as the canonical blob path."""
    name = Path(str(filename).replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(" .")
    if not name:
        name = "reference"
    stem = Path(name).stem.upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        name = f"_{name}"
    return name[:255]


def validate_image_input(data: bytes, filename: str, mime_type: str) -> tuple[str, str, str]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("image data must be bytes")
    payload = bytes(data)
    if not payload:
        raise ValueError("image data is empty")
    if len(payload) > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("image exceeds 15 MB limit")
    safe_name = sanitize_filename(filename)
    extension = Path(safe_name).suffix.lower()
    expected = _FORMATS.get(extension)
    normalized_mime = str(mime_type).strip().lower()
    if expected is None or normalized_mime != expected[0]:
        raise ValueError("filename extension and MIME type must be JPEG, PNG, or WebP")
    if normalized_mime == "image/jpeg":
        valid_signature = payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")
    elif normalized_mime == "image/png":
        valid_signature = payload.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in payload[-32:]
    else:
        valid_signature = payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    if not valid_signature:
        raise ValueError("image signature does not match the declared format")
    return safe_name, normalized_mime, expected[1]


def image_sha256(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def reference_blob_path(projects_root: Path, project_id: str, asset_id: str, digest: str, suffix: str) -> tuple[Path, str]:
    relative = Path("assets") / "references" / asset_id / f"{digest}{suffix}"
    project_root = (projects_root / project_id).resolve()
    target = (projects_root / project_id / relative).resolve()
    if project_root not in target.parents:
        raise ValueError("reference storage path escapes project root")
    return target, relative.as_posix()


def store_immutable_blob(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(bytes(data))
    except FileExistsError:
        # Hash-derived names are immutable and collision-safe; never overwrite.
        return
