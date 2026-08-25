"""Safe, bounded streaming downloads for provider-owned result media."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import requests

from .streaming_artifact import StreamingArtifactSource


class ProviderResultDownloadError(RuntimeError):
    """A sanitized result-download failure that never includes its URL."""


@dataclass(frozen=True, slots=True)
class ProviderResultPolicy:
    allowed_hosts: tuple[str, ...]
    max_bytes: int
    timeout_seconds: float = 60.0
    max_redirects: int = 3
    accepted_content_types: tuple[str, ...] = (
        "video/mp4",
        "video/quicktime",
        "application/octet-stream",
    )

    def __post_init__(self) -> None:
        normalized = tuple(
            str(host).strip().lower().rstrip(".") for host in self.allowed_hosts
            if str(host).strip()
        )
        if not normalized:
            raise ValueError("provider result host allowlist cannot be empty")
        if any("/" in host or ":" in host or "*" in host for host in normalized):
            raise ValueError("provider result hosts must be DNS suffixes")
        if isinstance(self.max_bytes, bool) or int(self.max_bytes) <= 0:
            raise ValueError("provider result max_bytes must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("provider result timeout must be positive")
        if not 0 <= int(self.max_redirects) <= 5:
            raise ValueError("provider result redirect limit is invalid")
        object.__setattr__(self, "allowed_hosts", normalized)


class ProviderResultDownloader:
    """Validate each URL/redirect and stream bytes without materializing them."""

    _REDIRECTS = {301, 302, 303, 307, 308}

    def __init__(
        self,
        policy: ProviderResultPolicy,
        *,
        session: object | None = None,
        resolver: Callable[..., Iterable[object]] | None = None,
    ) -> None:
        self.policy = policy
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            # Result downloads never need ambient HTTP proxy credentials.
            self.session.trust_env = False
        self._resolver = resolver or socket.getaddrinfo

    def source(
        self,
        url: str,
        *,
        prefix_validator: Callable[[bytes], None] | None = None,
        accept: str = "video/*,application/octet-stream",
    ) -> StreamingArtifactSource:
        safe_url = self._validate_url(url, resolve=False)

        def write_to(sink: BinaryIO) -> None:
            self._stream(safe_url, sink, prefix_validator=prefix_validator, accept=accept)

        return StreamingArtifactSource(write_to, self.policy.max_bytes)

    def _stream(
        self,
        url: str,
        sink: BinaryIO,
        *,
        prefix_validator: Callable[[bytes], None] | None,
        accept: str,
    ) -> None:
        current = url
        redirects = 0
        while True:
            current = self._validate_url(current, resolve=True)
            response = None
            try:
                response = self.session.get(
                    current,
                    stream=True,
                    timeout=self.policy.timeout_seconds,
                    allow_redirects=False,
                    headers={"Accept": accept},
                )
                status = int(getattr(response, "status_code", 0))
                if status in self._REDIRECTS:
                    if redirects >= self.policy.max_redirects:
                        raise ProviderResultDownloadError("provider result redirect limit exceeded")
                    location = self._header(getattr(response, "headers", {}), "location")
                    if not location:
                        raise ProviderResultDownloadError("provider result redirect is missing location")
                    current = urljoin(current, location)
                    redirects += 1
                    continue
                if status < 200 or status >= 300:
                    raise ProviderResultDownloadError(f"provider result HTTP {status}")
                self._validate_headers(getattr(response, "headers", {}))
                self._copy_response(response, sink, prefix_validator)
                return
            except ProviderResultDownloadError:
                raise
            except requests.RequestException as exc:
                raise ProviderResultDownloadError(
                    f"provider result transport failed: {type(exc).__name__}"
                ) from exc
            except (OSError, ValueError, TypeError) as exc:
                raise ProviderResultDownloadError(
                    f"provider result download failed: {type(exc).__name__}"
                ) from exc
            finally:
                if response is not None and hasattr(response, "close"):
                    response.close()

    def _validate_url(self, value: str, *, resolve: bool) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProviderResultDownloadError("provider result URL is missing")
        if "\\" in value or any(ord(character) < 32 for character in value):
            raise ProviderResultDownloadError("provider result URL is invalid")
        try:
            parsed = urlsplit(value.strip())
            port = parsed.port
        except ValueError as exc:
            raise ProviderResultDownloadError("provider result URL is invalid") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise ProviderResultDownloadError("provider result URL must be credential-free HTTPS")
        try:
            host = parsed.hostname.lower().rstrip(".").encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ProviderResultDownloadError("provider result hostname is invalid") from exc
        if host not in self.policy.allowed_hosts:
            raise ProviderResultDownloadError("provider result host is not allowlisted")
        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            literal = None
        if literal is not None:
            self._require_global_ip(literal)
        elif resolve:
            self._resolve_public(host)
        return value.strip()

    def _resolve_public(self, host: str) -> None:
        try:
            answers = tuple(self._resolver(host, 443, type=socket.SOCK_STREAM))
        except (OSError, socket.gaierror) as exc:
            raise ProviderResultDownloadError("provider result host cannot resolve") from exc
        if not answers:
            raise ProviderResultDownloadError("provider result host cannot resolve")
        for answer in answers:
            try:
                raw = answer[4][0]  # getaddrinfo-compatible result
                address = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise ProviderResultDownloadError("provider result DNS answer is invalid") from exc
            self._require_global_ip(address)

    @staticmethod
    def _require_global_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if not address.is_global:
            raise ProviderResultDownloadError("provider result host resolves to a non-public address")

    def _validate_headers(self, headers: object) -> None:
        raw_length = self._header(headers, "content-length")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ProviderResultDownloadError("provider result content length is invalid") from exc
            if length < 0 or length > self.policy.max_bytes:
                raise ProviderResultDownloadError("provider result exceeds download limit")
        raw_type = self._header(headers, "content-type")
        if raw_type and self.policy.accepted_content_types:
            mime = raw_type.split(";", 1)[0].strip().lower()
            if mime not in {item.lower() for item in self.policy.accepted_content_types}:
                raise ProviderResultDownloadError("provider result media type is unsupported")

    def _copy_response(
        self,
        response: object,
        sink: BinaryIO,
        prefix_validator: Callable[[bytes], None] | None,
    ) -> None:
        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            raise ProviderResultDownloadError("provider result response is not streamable")
        total = 0
        pending = bytearray()
        validated = prefix_validator is None
        for raw_chunk in iterator(chunk_size=1024 * 1024):
            if not raw_chunk:
                continue
            if not isinstance(raw_chunk, (bytes, bytearray, memoryview)):
                raise ProviderResultDownloadError("provider result stream returned invalid bytes")
            chunk = bytes(raw_chunk)
            total += len(chunk)
            if total > self.policy.max_bytes:
                raise ProviderResultDownloadError("provider result exceeds download limit")
            if not validated:
                pending.extend(chunk)
                if len(pending) < 512:
                    continue
                prefix_validator(bytes(pending[:512]))
                sink.write(pending)
                pending.clear()
                validated = True
            else:
                sink.write(chunk)
        if not validated:
            prefix_validator(bytes(pending))
            sink.write(pending)
        if total <= 0:
            raise ProviderResultDownloadError("provider result is empty")

    @staticmethod
    def _header(headers: object, name: str) -> str:
        if not isinstance(headers, Mapping):
            return ""
        value = headers.get(name)
        if value is None:
            value = headers.get(name.title())
        return str(value or "").strip()


def validate_mp4_prefix(prefix: bytes) -> None:
    """Reject HTML/JSON/error bodies before they become canonical media."""

    if len(prefix) < 16 or b"ftyp" not in prefix[:128]:
        raise ProviderResultDownloadError("provider result is not a supported MP4 container")


def validate_image_prefix(prefix: bytes) -> None:
    if prefix.startswith(b"\xff\xd8\xff"):
        return
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    if prefix.startswith(b"RIFF") and len(prefix) >= 12 and prefix[8:12] == b"WEBP":
        return
    raise ProviderResultDownloadError("provider result is not a supported image")


__all__ = [
    "ProviderResultDownloadError",
    "ProviderResultDownloader",
    "ProviderResultPolicy",
    "validate_image_prefix",
    "validate_mp4_prefix",
]
