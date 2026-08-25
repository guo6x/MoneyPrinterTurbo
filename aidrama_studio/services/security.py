"""Central error redaction and bounded runtime logging."""

from __future__ import annotations

import re
import sys
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


def sanitize_error(value: object, *, max_length: int = 2000) -> str:
    text = str(value or "")
    # Strip traceback content at the first marker. Persisting a stack leaks
    # source/user paths and is rarely actionable for normal users.
    text = text.split("Traceback (most recent call last):", 1)[0]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "<redacted>", text)
    text = _WINDOWS_PATH.sub("<local-path>", text)
    text = _POSIX_PRIVATE.sub("<local-path>", text)
    # Signed/provider URLs are not durable error data. Preserve only origin.
    words: list[str] = []
    for word in text.replace("\n", " ").split():
        if word.startswith(("http://", "https://")):
            try:
                parsed = urlsplit(word.rstrip(".,;"))
                word = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except ValueError:
                word = "<url>"
        words.append(word)
    return " ".join(words).strip()[: max(1, int(max_length))]


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


__all__ = ["configure_runtime_logging", "sanitize_error"]
