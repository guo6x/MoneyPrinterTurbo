"""Exact-endpoint runtime bindings for the Mainland provider manifests.

This module joins model data to the frozen protocol drivers and provider
codecs.  It performs no network request at construction time, never selects a
different endpoint/provider after manifest selection, and never retries a
create operation.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .codecs import validate_request_against_manifest
from .contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    DriverResponse,
    DriverStatus,
    DriverSubmission,
    EncodedRequest,
    ProtocolFamily,
)
from .drivers import AsyncTaskDriver, DriverError, RequestResponseDriver, TransportError
from .mainland_codecs import (
    ProviderArtifactSink,
    ProviderInputResolver,
    build_mainland_codecs,
)
from .mainland_manifests import (
    ARK_CN_BEIJING_ENDPOINT_PROFILE,
    DASHSCOPE_CN_ENDPOINT_PROFILE,
    DEEPSEEK_CN_ENDPOINT_PROFILE,
    MAINLAND_PRIMARY_MANIFEST_IDS,
    build_mainland_manifests,
)
from .manifest import ModelManifest
from .resolver import InMemoryManifestRegistry


@dataclass(frozen=True, slots=True)
class MainlandEndpointProfile:
    """Non-secret, region-pinned HTTP protocol endpoint."""

    id: str
    deployment_region: str
    base_url: str
    credential_reference: str
    task_path_template: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "deployment_region", "base_url", "credential_reference"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DriverError(f"endpoint profile {name} must be non-empty text")
            object.__setattr__(self, name, value.strip())
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError as exc:
            raise DriverError("endpoint profile base_url is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DriverError("endpoint profile must use a credential-free HTTPS URL")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if self.task_path_template is not None:
            template = self.task_path_template.strip()
            if not template.startswith("/") or template.count("{reference}") != 1:
                raise DriverError("async endpoint profile requires one {reference} path slot")
            object.__setattr__(self, "task_path_template", template)


MAINLAND_ENDPOINT_PROFILES: Mapping[str, MainlandEndpointProfile] = MappingProxyType(
    {
        DASHSCOPE_CN_ENDPOINT_PROFILE: MainlandEndpointProfile(
            id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            deployment_region="MAINLAND_CHINA",
            base_url="https://dashscope.aliyuncs.com/api/v1",
            credential_reference="DASHSCOPE_API_KEY",
            task_path_template="/tasks/{reference}",
        ),
        DEEPSEEK_CN_ENDPOINT_PROFILE: MainlandEndpointProfile(
            id=DEEPSEEK_CN_ENDPOINT_PROFILE,
            deployment_region="MAINLAND_CHINA",
            base_url="https://api.deepseek.com",
            credential_reference="DEEPSEEK_API_KEY",
        ),
        ARK_CN_BEIJING_ENDPOINT_PROFILE: MainlandEndpointProfile(
            id=ARK_CN_BEIJING_ENDPOINT_PROFILE,
            deployment_region="MAINLAND_CHINA",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            credential_reference="ARK_API_KEY",
            task_path_template="/contents/generations/tasks/{reference}",
        ),
    }
)


DASHSCOPE_WORKSPACE_BASE_URL_KEY = "DASHSCOPE_WORKSPACE_BASE_URL"
_DASHSCOPE_WORKSPACE_HOST_SUFFIX = ".cn-beijing.maas.aliyuncs.com"


def dashscope_workspace_endpoint_profile(
    base_url: str,
) -> MainlandEndpointProfile:
    """Build the pinned Beijing workspace endpoint without allowing key exfiltration."""

    candidate = str(base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise DriverError("DashScope workspace base URL is invalid") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    workspace_label = (
        host[: -len(_DASHSCOPE_WORKSPACE_HOST_SUFFIX)]
        if host.endswith(_DASHSCOPE_WORKSPACE_HOST_SUFFIX)
        else ""
    )
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api/v1"
        or not workspace_label
        or "." in workspace_label
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", workspace_label
        )
        is None
    ):
        raise DriverError(
            "DashScope workspace base URL must be a Beijing workspace /api/v1 endpoint"
        )
    return MainlandEndpointProfile(
        id=DASHSCOPE_CN_ENDPOINT_PROFILE,
        deployment_region="MAINLAND_CHINA",
        base_url=f"https://{host}/api/v1",
        credential_reference="DASHSCOPE_API_KEY",
        task_path_template="/tasks/{reference}",
    )


class MainlandHTTPTransport:
    """No-retry bearer HTTP transport that does not inspect provider JSON."""

    def __init__(
        self,
        profile: MainlandEndpointProfile,
        credential: str,
        *,
        session: Any | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise DriverError("transport timeout must be positive")
        self.profile = profile
        self._credential = str(credential or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def send(
        self,
        request: EncodedRequest,
        context: CapabilityRequest | None = None,
    ) -> DriverResponse:
        del context
        return self._send(request)

    def create(
        self,
        request: EncodedRequest,
        context: CapabilityRequest | None = None,
    ) -> DriverResponse:
        del context
        # There is deliberately one attempt and no transport retry loop.
        return self._send(request)

    def poll(
        self,
        reference: str,
        context: CapabilityRequest | None = None,
    ) -> DriverResponse:
        del context
        return self._send(self._task_request(reference))

    def fetch_result(
        self,
        reference: str,
        context: CapabilityRequest | None = None,
    ) -> DriverResponse:
        del context
        # Result reconciliation is a GET against the persisted remote
        # identity.  It never routes back through create.
        return self._send(self._task_request(reference))

    def _task_request(self, reference: str) -> EncodedRequest:
        if not isinstance(reference, str) or not reference.strip():
            raise TransportError("remote task identity is required")
        template = self.profile.task_path_template
        if template is None:
            raise TransportError("selected endpoint does not support async tasks")
        safe_reference = quote(reference.strip(), safe="")
        return EncodedRequest(
            payload=None,
            method="GET",
            path=template.format(reference=safe_reference),
        )

    def _send(self, request: EncodedRequest) -> DriverResponse:
        if not isinstance(request, EncodedRequest):
            raise TransportError("transport requires EncodedRequest")
        if not self._credential:
            raise TransportError(
                f"credential reference {self.profile.credential_reference} is not configured"
            )
        path = request.path
        if not isinstance(path, str) or not path.startswith("/") or "://" in path:
            raise TransportError("encoded request path is invalid")
        headers = dict(request.headers)
        if any(key.casefold() == "authorization" for key in headers):
            raise TransportError("provider codecs must not embed credentials")
        headers["Authorization"] = f"Bearer {self._credential}"
        body: dict[str, object] = {}
        if request.payload is not None:
            if not isinstance(request.payload, Mapping):
                raise TransportError("HTTP provider payload must be an object")
            body["json"] = dict(request.payload)
        try:
            response = self.session.request(
                request.method,
                self.profile.base_url + path,
                headers=headers,
                timeout=request.timeout_seconds or self.timeout_seconds,
                **body,
            )
        except requests.RequestException as exc:
            raise TransportError(
                f"{self.profile.id} transport failed: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise TransportError(f"{self.profile.id} transport failed") from exc
        try:
            status_code = int(response.status_code)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransportError("provider transport returned no valid status") from exc
        raw_content_type = str(response.headers.get("Content-Type", ""))
        content_type = raw_content_type.split(";", 1)[0].strip().casefold()
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise TransportError("provider returned malformed JSON") from exc
        else:
            payload = bytes(response.content)
        safe_headers: dict[str, str] = {}
        for name in ("Content-Type", "X-Request-Id", "Request-Id"):
            value = response.headers.get(name)
            if value:
                safe_headers[name] = str(value)
        return DriverResponse(
            payload=payload,
            status_code=status_code,
            headers=safe_headers,
        )


class ContentAddressedArtifactSink(ProviderArtifactSink):
    """Persist media before a signed provider result URL leaves the codec."""

    _SUFFIXES = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
    }

    def __init__(
        self,
        root: str | Path,
        *,
        session: Any | None = None,
        resolver: Any | None = None,
        timeout_seconds: float = 120.0,
        max_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_absolute():
            raise DriverError("artifact sink root must be absolute")
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise DriverError("artifact sink timeout and size limit must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self._resolver = resolver or socket.getaddrinfo

    def persist_bytes(
        self,
        data: bytes,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef:
        if not isinstance(data, bytes) or not data or len(data) > self.max_bytes:
            raise TransportError("provider artifact size is invalid")
        return self._persist(
            data,
            request_id=request_id,
            role=role,
            mime_type=mime_type,
            safe_metadata=safe_metadata,
        )

    def persist_remote(
        self,
        url: str,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef:
        safe_url = self._validate_remote_url(url)
        persisted_metadata: dict[str, object] = {}
        provider = safe_metadata.get("provider")
        if isinstance(provider, str) and provider.strip() and "://" not in provider:
            persisted_metadata["provider"] = provider.strip()
        try:
            response = self.session.get(
                safe_url,
                headers={"Accept": mime_type + ",application/octet-stream"},
                timeout=self.timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TransportError(
                f"provider artifact fetch failed: {type(exc).__name__}"
            ) from exc
        status_code = int(response.status_code)
        if 300 <= status_code < 400:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            raise TransportError("provider artifact redirects are not allowed")
        if status_code < 200 or status_code >= 300:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            raise TransportError(
                f"provider artifact fetch returned {status_code}"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="provider-artifact-",
            suffix=".tmp",
            dir=self.root,
            delete=False,
        )
        temporary_path: Path | None = Path(handle.name)
        digest = hashlib.sha256()
        prefix = bytearray()
        size = 0
        try:
            with handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TransportError("provider artifact exceeds size limit")
                    if len(prefix) < 16:
                        prefix.extend(chunk[: 16 - len(prefix)])
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size <= 0:
                raise TransportError("provider artifact is empty")
            self._validate_prefix(bytes(prefix), mime_type)
            target = self.root / f"{digest.hexdigest()}{self._suffix(mime_type)}"
            if target.exists():
                temporary_path.unlink()
            else:
                os.replace(temporary_path, target)
            temporary_path = None
        except Exception:
            try:
                if temporary_path is not None and temporary_path.is_file():
                    temporary_path.unlink()
            except OSError:
                pass
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        return self._reference(
            digest.hexdigest(),
            size,
            request_id=request_id,
            role=role,
            mime_type=mime_type,
            safe_metadata=persisted_metadata,
        )

    def _validate_remote_url(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TransportError("provider artifact URL is invalid")
        url = value.strip()
        if "\\" in url or any(ord(character) < 32 for character in url):
            raise TransportError("provider artifact URL is invalid")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise TransportError("provider artifact URL is invalid") from exc
        raw_hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme.casefold() != "https"
            or not raw_hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.fragment
        ):
            raise TransportError("provider artifact URL must use public HTTPS")
        if raw_hostname == "localhost" or raw_hostname.endswith(".localhost"):
            raise TransportError("provider artifact destination is not public")
        if "%" in raw_hostname:
            raise TransportError("provider artifact hostname is invalid")
        try:
            address = ipaddress.ip_address(raw_hostname)
        except ValueError:
            address = None
        if address is not None:
            self._require_public_address(address)
            return url
        try:
            hostname = raw_hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise TransportError("provider artifact hostname is invalid") from exc
        self._resolve_public_addresses(hostname)
        return url

    def _resolve_public_addresses(self, hostname: str) -> None:
        try:
            answers = tuple(self._resolver(hostname, 443, type=socket.SOCK_STREAM))
        except (OSError, socket.gaierror) as exc:
            raise TransportError("provider artifact hostname cannot resolve") from exc
        if not answers:
            raise TransportError("provider artifact hostname cannot resolve")
        for answer in answers:
            try:
                raw_address = answer[4][0]
                address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise TransportError("provider artifact DNS answer is invalid") from exc
            self._require_public_address(address)

    @staticmethod
    def _require_public_address(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> None:
        if not address.is_global:
            raise TransportError("provider artifact destination is not public")

    def _persist(
        self,
        data: bytes,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef:
        suffix = self._suffix(mime_type)
        self._validate_prefix(data[:16], mime_type)
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{digest}{suffix}"
        temporary_path: Path | None = None
        if not target.exists():
            handle = tempfile.NamedTemporaryFile(
                prefix="provider-artifact-",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            )
            temporary_path = Path(handle.name)
            try:
                with handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except OSError:
                        pass
        return self._reference(
            digest,
            len(data),
            request_id=request_id,
            role=role,
            mime_type=mime_type,
            safe_metadata=safe_metadata,
        )

    @classmethod
    def _suffix(cls, mime_type: str) -> str:
        suffix = cls._SUFFIXES.get(mime_type)
        if suffix is None:
            raise TransportError("provider artifact MIME type is unsupported")
        return suffix

    @staticmethod
    def _validate_prefix(prefix: bytes, mime_type: str) -> None:
        valid = {
            "image/jpeg": prefix.startswith(b"\xff\xd8\xff"),
            "image/png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP",
            "video/mp4": len(prefix) >= 12 and prefix[4:8] == b"ftyp",
            "audio/mpeg": prefix.startswith(b"ID3")
            or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0),
            "audio/wav": prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE",
        }.get(mime_type, False)
        if not valid:
            raise TransportError("provider artifact signature does not match MIME type")

    @staticmethod
    def _reference(
        digest: str,
        size_bytes: int,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef:
        metadata = {
            "request_id": request_id,
            "content_addressed": True,
            **dict(safe_metadata),
        }
        return ContentRef(
            source_kind="CONTENT_ADDRESSED_ARTIFACT",
            source_id=f"sha256:{digest}",
            role=role,
            mime_type=mime_type,
            sha256=digest,
            size_bytes=size_bytes,
            metadata=metadata,
        )

    def path_for(self, reference: ContentRef) -> Path:
        if reference.source_kind != "CONTENT_ADDRESSED_ARTIFACT" or not reference.sha256:
            raise DriverError("content reference does not belong to this artifact sink")
        suffix = self._suffix(reference.mime_type)
        return self.root / f"{reference.sha256}{suffix}"


class FrozenFileInputResolver(ProviderInputResolver):
    """Resolve an exact frozen image identity to a transient base64 data URI."""

    _MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

    def __init__(
        self,
        paths: Mapping[str, str | Path],
        *,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise DriverError("input resolver size limit must be positive")
        resolved: dict[str, Path] = {}
        for raw_identity, raw_path in paths.items():
            identity = str(raw_identity or "").strip()
            if not identity:
                raise DriverError("input resolver identity must be non-empty")
            path = Path(raw_path).expanduser().resolve()
            if identity in resolved and resolved[identity] != path:
                raise DriverError("input resolver identity is ambiguous")
            resolved[identity] = path
        self._paths = MappingProxyType(resolved)
        self.max_bytes = int(max_bytes)

    def resolve(self, reference: ContentRef) -> str:
        path = self._paths.get(reference.source_id)
        if path is None:
            raise DriverError("frozen content identity is not available to the input resolver")
        if reference.mime_type not in self._MIME_TYPES:
            raise DriverError("frozen file resolver supports image inputs only")
        if not path.is_file():
            raise DriverError("frozen input artifact is unavailable")
        size = path.stat().st_size
        if size <= 0 or size > self.max_bytes:
            raise DriverError("frozen input artifact size is invalid")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DriverError("frozen input artifact could not be read") from exc
        digest = hashlib.sha256(data).hexdigest()
        if not reference.sha256 or digest != reference.sha256:
            raise DriverError("frozen input artifact hash changed")
        ContentAddressedArtifactSink._validate_prefix(data[:16], reference.mime_type)
        return (
            f"data:{reference.mime_type};base64,"
            + base64.b64encode(data).decode("ascii")
        )


@dataclass(frozen=True, slots=True)
class MainlandProviderBinding:
    manifest: ModelManifest
    codec: object = field(repr=False)
    driver: object = field(repr=False)
    endpoint: MainlandEndpointProfile


class MainlandProviderRuntime:
    """Exact manifest-to-codec-to-driver bindings with no fallback routing."""

    def __init__(
        self,
        *,
        credentials: Mapping[str, str] | None = None,
        create_authorized: bool = False,
        artifact_sink: ProviderArtifactSink | None = None,
        input_resolver: ProviderInputResolver | None = None,
        sessions: Mapping[str, object] | None = None,
        dashscope_workspace_base_url: str | None = None,
    ) -> None:
        self._credentials = {
            str(key): str(value or "").strip()
            for key, value in (credentials or {}).items()
        }
        presence = {key: bool(value) for key, value in self._credentials.items()}
        self.manifests = build_mainland_manifests(
            credential_presence=presence,
            create_authorized=create_authorized,
            artifact_sink_available=artifact_sink is not None,
        )
        self.manifest_registry = InMemoryManifestRegistry(self.manifests)
        codecs = build_mainland_codecs(
            artifact_sink=artifact_sink,
            input_resolver=input_resolver,
        )
        endpoint_profiles = dict(MAINLAND_ENDPOINT_PROFILES)
        if dashscope_workspace_base_url:
            endpoint_profiles[DASHSCOPE_CN_ENDPOINT_PROFILE] = (
                dashscope_workspace_endpoint_profile(dashscope_workspace_base_url)
            )
        self.endpoint_profiles = MappingProxyType(endpoint_profiles)
        supplied_sessions = dict(sessions or {})
        bindings: dict[str, MainlandProviderBinding] = {}
        for manifest in self.manifests:
            endpoint_id = manifest.endpoint_profile_id
            endpoint = self.endpoint_profiles.get(str(endpoint_id))
            if endpoint is None:
                raise DriverError("manifest endpoint profile is not registered")
            self._assert_endpoint_identity(manifest, endpoint)
            codec = codecs.get(manifest.codec_id)
            if codec is None:
                raise DriverError("manifest codec is not registered")
            transport = MainlandHTTPTransport(
                endpoint,
                self._credentials.get(endpoint.credential_reference, ""),
                session=supplied_sessions.get(endpoint.id),
            )
            driver: object
            if manifest.protocol is ProtocolFamily.REQUEST_RESPONSE:
                driver = RequestResponseDriver(transport, manifest=manifest, max_retries=0)
            elif manifest.protocol is ProtocolFamily.ASYNC_TASK:
                driver = AsyncTaskDriver(transport, manifest=manifest)
            else:
                raise DriverError("Mainland V1 slice does not register stream manifests")
            bindings[manifest.id] = MainlandProviderBinding(
                manifest=manifest,
                codec=codec,
                driver=driver,
                endpoint=endpoint,
            )
        self._bindings = MappingProxyType(bindings)

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        artifact_sink: ProviderArtifactSink | None = None,
        input_resolver: ProviderInputResolver | None = None,
        sessions: Mapping[str, object] | None = None,
    ) -> "MainlandProviderRuntime":
        values = os.environ if env is None else env
        credentials = {
            name: str(values.get(name, "")).strip()
            for name in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY")
        }
        return cls(
            credentials=credentials,
            create_authorized=(
                str(values.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "")) == "1"
            ),
            artifact_sink=artifact_sink,
            input_resolver=input_resolver,
            sessions=sessions,
            dashscope_workspace_base_url=str(
                values.get(DASHSCOPE_WORKSPACE_BASE_URL_KEY, "") or ""
            ).strip()
            or None,
        )

    @staticmethod
    def _assert_endpoint_identity(
        manifest: ModelManifest, endpoint: MainlandEndpointProfile
    ) -> None:
        if manifest.deployment_region != endpoint.deployment_region:
            raise DriverError("manifest and endpoint deployment regions differ")
        if manifest.credential_reference != endpoint.credential_reference:
            raise DriverError("manifest and endpoint credential references differ")
        if manifest.deployment_region != "MAINLAND_CHINA":
            raise DriverError("Mainland runtime refuses non-Mainland endpoint profiles")

    def binding_for(self, manifest_id: str) -> MainlandProviderBinding:
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise DriverError("frozen manifest identity is required")
        binding = self._bindings.get(manifest_id.strip())
        if binding is None:
            raise DriverError("frozen Mainland manifest is not registered; no fallback")
        return binding

    def primary_manifest(self, capability: CapabilityKind | str) -> ModelManifest:
        kind = CapabilityKind.coerce(capability)
        manifest_id = MAINLAND_PRIMARY_MANIFEST_IDS[kind]
        return self.binding_for(manifest_id).manifest

    def submit(
        self,
        request: CapabilityRequest,
        *,
        existing_reference: str | None = None,
        authorization: object | None = None,
    ) -> CapabilityResult | DriverSubmission | DriverStatus:
        if not request.manifest_id:
            raise DriverError("request must carry a frozen manifest identity")
        binding = self.binding_for(request.manifest_id)
        validate_request_against_manifest(
            request,
            binding.manifest,
            codec_id=str(getattr(binding.codec, "codec_id", "")),
        )
        if existing_reference is None and not binding.manifest.runtime_available:
            raise DriverError(
                "selected model runtime is unavailable; no provider create was attempted"
            )
        if binding.manifest.protocol is ProtocolFamily.REQUEST_RESPONSE:
            if existing_reference is not None:
                raise DriverError("request/response protocols have no remote task identity")
            if not isinstance(binding.driver, RequestResponseDriver):
                raise DriverError("manifest is bound to the wrong protocol driver")
            return binding.driver.invoke(
                request,
                binding.codec,
                binding.manifest,
                authorization=authorization,
            )
        if not isinstance(binding.driver, AsyncTaskDriver):
            raise DriverError("manifest is bound to the wrong protocol driver")
        # AsyncTaskDriver.submit routes an existing identity directly to
        # reconcile.  No create authorization check or create transport call
        # is reachable from that branch.
        return binding.driver.submit(
            request,
            binding.codec,
            binding.manifest,
            existing_reference=existing_reference,
            authorization=authorization,
        )

    create = submit

    def poll(
        self,
        manifest_id: str,
        reference: str,
        *,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        binding = self._async_binding(manifest_id)
        return binding.driver.poll(reference, binding.codec, request=request)

    def reconcile(
        self,
        manifest_id: str,
        reference: str,
        *,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        binding = self._async_binding(manifest_id)
        return binding.driver.reconcile(reference, binding.codec, request=request)

    def fetch_result(
        self,
        manifest_id: str,
        reference: str,
        *,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        binding = self._async_binding(manifest_id)
        validate_request_against_manifest(
            request,
            binding.manifest,
            codec_id=str(getattr(binding.codec, "codec_id", "")),
        )
        return binding.driver.collect(reference, binding.codec, request=request)

    def _async_binding(self, manifest_id: str) -> MainlandProviderBinding:
        binding = self.binding_for(manifest_id)
        if not isinstance(binding.driver, AsyncTaskDriver):
            raise DriverError("selected manifest is not asynchronous")
        return binding


__all__ = [
    "ContentAddressedArtifactSink",
    "FrozenFileInputResolver",
    "MAINLAND_ENDPOINT_PROFILES",
    "MainlandEndpointProfile",
    "MainlandHTTPTransport",
    "MainlandProviderBinding",
    "MainlandProviderRuntime",
]
