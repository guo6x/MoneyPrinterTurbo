"""Central error redaction and bounded runtime logging."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from loguru import logger


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)\S+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|signature|sig|x-amz-signature)\s*[:=]\s*)[^\s&,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:[\\/](?:[^\s:'\"<>|]+[\\/])*[^\s:'\"<>|]*")
_POSIX_PRIVATE = re.compile(r"(?<!\w)/(?:home|Users|var|tmp)/[^\s:'\"<>|]+(?:/[^\s:'\"<>|]+)*")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_METADATA_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "set_cookie",
}


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ").,;]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if not host:
            return "<url>" + trailing
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        safe = urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
        return safe + trailing
    except (TypeError, ValueError):
        return "<url>" + trailing


def sanitize_error(value: object, *, max_length: int = 2000) -> str:
    text = str(value or "")
    # Strip traceback content at the first marker. Persisting a stack leaks
    # source/user paths and is rarely actionable for normal users.
    text = text.split("Traceback (most recent call last):", 1)[0]
    # Remove URL credentials/query before token patterns insert angle-bracket
    # redaction markers that would otherwise split the URL token.
    text = _URL_PATTERN.sub(_sanitize_url_match, text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "<redacted>", text)
    text = _WINDOWS_PATH.sub("<local-path>", text)
    text = _POSIX_PRIVATE.sub("<local-path>", text)
    # Signed/provider URLs are not durable error data. This also catches
    # embedded forms such as ``url=https://...`` rather than only whitespace
    # delimited URL tokens.
    text = _URL_PATTERN.sub(_sanitize_url_match, text)
    return " ".join(text.replace("\n", " ").split()).strip()[: max(1, int(max_length))]


def sanitize_persistent_metadata(value: object) -> object:
    """Recursively remove secrets, signed URLs, and private absolute paths."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            text_key = str(key)
            lowered = text_key.casefold()
            if (
                lowered in _SENSITIVE_METADATA_KEYS
                or "signed_url" in lowered
                or lowered == "url"
                or lowered.endswith("_url")
            ):
                continue
            result[text_key] = sanitize_persistent_metadata(child)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_persistent_metadata(item) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        return sanitize_error(value, max_length=8000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_error(value, max_length=1000)


def configure_runtime_logging(root: Path) -> Path:
    """Use one redacted, rotating AppData log sink."""
    log_dir = Path(root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "aidrama.log"

    def patch(record: dict[str, Any]) -> None:
        record["message"] = sanitize_error(record.get("message", ""), max_length=4000)
        if record.get("exception") is not None:
            record["extra"]["exception_type"] = getattr(record["exception"].type, "__name__", "Exception")
            record["exception"] = None

    logger.remove()
    logger.configure(patcher=patch)
    logger.add(sys.stderr, level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", backtrace=False, diagnose=False)
    logger.add(target, level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", rotation="5 MB", retention="7 days", compression="zip", enqueue=True, backtrace=False, diagnose=False)
    return target


__all__ = [
    "configure_runtime_logging",
    "sanitize_error",
    "sanitize_persistent_metadata",
]
