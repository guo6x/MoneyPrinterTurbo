"""Provider codec contracts and small generic codec helpers.

Codecs are the only layer allowed to know provider request/result field names.
The built-in JSON codec is intentionally boring: it is useful for test/fake
transports and compatibility bridges, while real providers can implement the
same protocols with native payloads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import math
import re
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    DriverResponse,
    DriverStatus,
    EncodedRequest,
    ProviderTaskIdentity,
    RuntimeContractError,
    RuntimeOutcome,
    ProtocolFamily,
    StreamChunk,
    thaw,
)
from .manifest import ModelManifest


class CodecError(RuntimeContractError):
    """Base error for validation/encoding/decoding failures."""


class MalformedProviderResult(CodecError):
    """Raised when a provider response cannot be safely decoded."""


def _strict_bool(value: object, *, name: str, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise MalformedProviderResult(f"{name} must be boolean")
    return value


def _optional_text(value: object, *, name: str) -> str | None:
    """Validate an optional remote identifier without coercing secrets/types."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MalformedProviderResult(f"{name} must be non-empty text")
    return value.strip()


def validate_request_against_manifest(
    request: CapabilityRequest,
    manifest: ModelManifest | None,
    *,
    codec_id: str | None = None,
) -> None:
    """Fail closed when a request is sent to a mismatched frozen profile."""

    if manifest is None:
        return

    def field(name: str, *aliases: str, default: object = None) -> object:
        for candidate in (name, *aliases):
            if isinstance(manifest, Mapping) and candidate in manifest:
                value = manifest[candidate]
            else:
                value = getattr(manifest, candidate, None)
            if value is not None:
                return value
        return default

    try:
        raw_capability = field("capability")
        raw_capabilities = field("capabilities", default=None)
        declared_singular = (
            (CapabilityKind.coerce(raw_capability),)
            if raw_capability is not None
            else ()
        )
        if raw_capabilities is not None:
            if isinstance(raw_capabilities, (str, bytes)):
                raw_capabilities = (raw_capabilities,)
            declared_plural = tuple(
                CapabilityKind.coerce(value) for value in raw_capabilities
            )
        else:
            declared_plural = ()
        if declared_singular and declared_plural and set(declared_singular) != set(
            declared_plural
        ):
            raise CodecError("manifest capability aliases conflict")
        manifest_capabilities = declared_plural or declared_singular
    except Exception as exc:
        if isinstance(exc, CodecError):
            raise
        raise CodecError("manifest capability is unsupported") from exc
    if request.capability not in manifest_capabilities:
        raise CodecError(
            f"request capability {request.capability.value} does not match manifest capabilities "
            f"{tuple(item.value for item in manifest_capabilities)!r}"
        )
    try:
        manifest_protocol = ProtocolFamily.coerce(
            field("protocol", "protocol_family", default="")
        )
    except Exception as exc:
        raise CodecError("manifest protocol is unsupported") from exc
    if request.protocol_family is not manifest_protocol:
        raise CodecError(
            f"request protocol {request.protocol_family.value} does not match manifest {manifest_protocol.value}"
        )
    manifest_identity = str(field("id", "manifest_id", default="") or "")
    if request.manifest_id and request.manifest_id != manifest_identity:
        raise CodecError("request manifest identity does not match frozen manifest")
    raw_digest = field("manifest_hash", default="")
    manifest_digest = str(raw_digest or "")
    if callable(raw_digest):
        manifest_digest = str(raw_digest())
    if request.manifest_hash and request.manifest_hash != manifest_digest:
        raise CodecError("request manifest hash does not match frozen manifest")
    manifest_provider = str(field("provider_id", "provider", default="") or "")
    if request.provider_id and request.provider_id != manifest_provider:
        raise CodecError("request provider identity does not match frozen manifest")
    manifest_model = str(field("model_id", "model", default="") or "")
    if request.model_id and request.model_id != manifest_model:
        raise CodecError("request model identity does not match frozen manifest")
    manifest_codec = str(field("codec_id", default="") or "")
    if codec_id and manifest_codec and codec_id != manifest_codec:
        raise CodecError("request codec identity does not match frozen manifest")


@runtime_checkable
class ProviderCodec(Protocol):
    """Minimum codec contract shared by all protocol families."""

    codec_id: str
    codec_version: str

    def validate(self, request: CapabilityRequest, manifest: ModelManifest | None = None) -> None: ...

    def encode_request(
        self,
        request: CapabilityRequest,
        manifest: ModelManifest | None = None,
    ) -> EncodedRequest: ...

    def decode_response(
        self,
        response: DriverResponse,
        request: CapabilityRequest,
    ) -> CapabilityResult: ...


@runtime_checkable
class AsyncTaskCodec(ProviderCodec, Protocol):
    """Additional codec hooks for the ASYNC_TASK lifecycle."""

    def decode_task_identity(
        self,
        response: DriverResponse,
        request: CapabilityRequest,
    ) -> ProviderTaskIdentity: ...

    def decode_task_state(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus: ...

    def decode_result(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest,
    ) -> CapabilityResult: ...

    def encode_cancel(
        self,
        reference: str,
        request: CapabilityRequest | None = None,
    ) -> EncodedRequest: ...


@runtime_checkable
class StreamCodec(ProviderCodec, Protocol):
    """Additional codec hooks for incremental stream events."""

    def decode_chunk(
        self,
        chunk: Any,
        index: int,
        request: CapabilityRequest,
    ) -> StreamChunk: ...

    def finalize(
        self,
        chunks: tuple[StreamChunk, ...],
        request: CapabilityRequest,
    ) -> CapabilityResult: ...


def _safe_output(value: Any) -> ContentRef:
    if isinstance(value, ContentRef):
        return value
    if not isinstance(value, Mapping):
        raise MalformedProviderResult("provider output is not a ContentRef")
    required_text = ("source_kind", "source_id")
    for name in required_text:
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise MalformedProviderResult(f"provider output {name} must be non-empty text")
    for name in ("role", "mime_type", "sha256"):
        if name in value and not isinstance(value[name], str):
            raise MalformedProviderResult(f"provider output {name} must be text")
    output_metadata = value.get("metadata", {})
    if not isinstance(output_metadata, Mapping):
        raise MalformedProviderResult("provider output metadata is malformed")
    _assert_safe_mapping(output_metadata, name="provider_output.metadata")
    try:
        return ContentRef(
            source_kind=value["source_kind"],
            source_id=value["source_id"],
            role=value.get("role", ""),
            mime_type=value.get("mime_type", "application/octet-stream"),
            sha256=value.get("sha256", ""),
            size_bytes=value.get("size_bytes"),
            metadata=output_metadata,
        )
    except (KeyError, TypeError, ValueError, RuntimeContractError) as exc:
        raise MalformedProviderResult("provider output is malformed") from exc


def _assert_safe_mapping(value: Mapping[str, Any], *, name: str) -> None:
    secret_markers = (
        "api_key",
        "apikey",
        "token",
        "secret",
        "access_token",
        "refresh_token",
        "password",
        "private_key",
        "signed_url",
        "raw_body",
    )

    def walk(item: object, path: str) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise MalformedProviderResult(f"{name}.{path} contains a non-finite number")
            return
        if isinstance(item, str):
            lowered = item.casefold()
            if (
                item.startswith(("sk-", "rk-", "sess-"))
                or "bearer " in lowered
                or "-----begin " in lowered
                or re.search(
                    r"[?&](?:token|sig|signature|x-amz-signature|access[_-]?key|api[_-]?key|credential|auth|expires)=",
                    lowered,
                )
            ):
                raise MalformedProviderResult(f"{name}.{path} contains secret material")
            return
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                lowered = key.casefold()
                if lowered in {"authorization", "authorization_header"} or any(
                    lowered == marker or lowered.endswith("_" + marker)
                    for marker in secret_markers
                ):
                    raise MalformedProviderResult(f"{name}.{path}.{key} contains secret material")
                walk(child, f"{path}.{key}")
        elif isinstance(item, (tuple, list, set, frozenset)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        else:
            raise MalformedProviderResult(f"{name}.{path} contains a non-JSON value")

    walk(value, name)


def result_from_mapping(
    value: Mapping[str, Any],
    request: CapabilityRequest,
    *,
    default_outcome: RuntimeOutcome | str | None = None,
    protocol_reference: str | None = None,
    provider_task_id: str | None = None,
) -> CapabilityResult:
    """Decode a sanitized mapping returned by a fake/compatibility codec.

    Raw response bodies are never copied into ``safe_metadata``.  Only fields
    explicitly named by the capability contract are retained.
    """

    if not isinstance(value, Mapping):
        raise MalformedProviderResult("provider result must be a mapping")
    outcome_value = value.get("outcome", default_outcome)
    if outcome_value is None:
        raise MalformedProviderResult("provider result is missing outcome")
    try:
        outcome = RuntimeOutcome.coerce(outcome_value)
        outputs_value = value.get("outputs", ())
        if isinstance(outputs_value, (str, bytes)) or not isinstance(outputs_value, Iterable):
            raise MalformedProviderResult("provider outputs must be a sequence")
        outputs = tuple(_safe_output(item) for item in outputs_value)
        request_id = value.get("request_id", request.request_id)
        if not isinstance(request_id, str):
            raise MalformedProviderResult("provider result request_id must be text")
        if request_id != request.request_id:
            raise MalformedProviderResult("provider result request identity mismatch")
        usage = value.get("usage", {})
        safe_metadata = value.get("safe_metadata", value.get("metadata", {}))
        if not isinstance(usage, Mapping) or not isinstance(safe_metadata, Mapping):
            raise MalformedProviderResult("usage and safe_metadata must be mappings")
        _assert_safe_mapping(usage, name="usage")
        _assert_safe_mapping(safe_metadata, name="safe_metadata")
        return CapabilityResult(
            request_id=request.request_id,
            outcome=outcome,
            protocol_reference=protocol_reference or value.get("protocol_reference"),
            provider_task_id=provider_task_id or value.get("provider_task_id"),
            outputs=outputs,
            usage=usage,
            safe_metadata=safe_metadata,
            error_category=value.get("error_category"),
            retryable=bool(
                _strict_bool(value.get("retryable"), name="retryable", default=False)
            ),
        )
    except MalformedProviderResult:
        raise
    except (TypeError, ValueError, RuntimeContractError) as exc:
        raise MalformedProviderResult("provider result is malformed") from exc


@dataclass(frozen=True, slots=True)
class JsonProviderCodec:
    """Generic codec for tests and OpenAI-shaped compatibility seams.

    It does not assert any provider field names beyond the neutral contract;
    native codecs should replace it whenever a provider has different wire
    semantics.
    """

    codec_id: str = "generic.json"
    codec_version: str = "1"
    method: str = "POST"
    path: str | None = None

    def validate(self, request: CapabilityRequest, manifest: ModelManifest | None = None) -> None:
        validate_request_against_manifest(request, manifest, codec_id=self.codec_id)
        if not request.request_id:
            raise CodecError("request_id is required")

    def encode_request(self, request: CapabilityRequest, manifest: ModelManifest | None = None) -> EncodedRequest:
        self.validate(request, manifest)
        payload: dict[str, Any] = {
            "request_id": request.request_id,
            "prompt_or_text": request.prompt_or_text,
            "structured_input": thaw(request.structured_input),
            "provider_parameters": thaw(request.provider_parameters),
            "inputs": [item.to_dict() for item in request.inputs],
        }
        return EncodedRequest(payload=payload, method=self.method, path=self.path)

    def decode_response(self, response: DriverResponse, request: CapabilityRequest) -> CapabilityResult:
        if response.status_code < 200 or response.status_code >= 300:
            raise MalformedProviderResult(f"provider returned HTTP-like status {response.status_code}")
        if isinstance(response.payload, CapabilityResult):
            if response.payload.request_id != request.request_id:
                raise MalformedProviderResult("provider result request identity mismatch")
            return response.payload
        if not isinstance(response.payload, Mapping):
            raise MalformedProviderResult("provider response payload is not a mapping")
        # Do not infer success from an arbitrary provider body.  A codec must
        # prove the lifecycle outcome explicitly so malformed results fail
        # closed instead of being persisted as successful work.
        return result_from_mapping(response.payload, request)


@dataclass(frozen=True, slots=True)
class JsonAsyncTaskCodec(JsonProviderCodec):
    """Neutral async codec useful for deterministic driver tests."""

    def decode_task_identity(self, response: DriverResponse, request: CapabilityRequest) -> ProviderTaskIdentity:
        if response.status_code < 200 or response.status_code >= 300 or not isinstance(response.payload, Mapping):
            raise MalformedProviderResult("async create response is malformed")
        payload = response.payload
        identity = payload.get("protocol_reference", payload.get("task_id", payload.get("id")))
        if not isinstance(identity, str) or not identity.strip():
            raise MalformedProviderResult("async create response has no stable remote identity")
        provider_task_id = payload.get("provider_task_id", payload.get("task_id", payload.get("id")))
        provider_task_id = _optional_text(provider_task_id, name="async_identity.provider_task_id")
        metadata = payload.get("safe_metadata", payload.get("metadata", {}))
        if not isinstance(metadata, Mapping):
            raise MalformedProviderResult("async identity metadata is malformed")
        _assert_safe_mapping(metadata, name="async_identity")
        try:
            return ProviderTaskIdentity(
                protocol_reference=identity,
                provider_task_id=provider_task_id,
                safe_metadata=metadata,
            )
        except RuntimeContractError as exc:
            raise MalformedProviderResult("async create identity is malformed") from exc

    def decode_task_state(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        if response.status_code < 200 or response.status_code >= 300 or not isinstance(response.payload, Mapping):
            raise MalformedProviderResult("async poll response is malformed")
        payload = response.payload
        raw_state = payload.get("outcome", payload.get("state", payload.get("status")))
        if raw_state is None:
            raise MalformedProviderResult("async poll response has no state")
        state_aliases = {
            "PENDING": RuntimeOutcome.RUNNING,
            "QUEUED": RuntimeOutcome.RUNNING,
            "PROCESSING": RuntimeOutcome.RUNNING,
            "COMPLETED": RuntimeOutcome.SUCCEEDED,
            "SUCCESS": RuntimeOutcome.SUCCEEDED,
            "ERROR": RuntimeOutcome.FAILED,
            "CANCELED": RuntimeOutcome.CANCELLED,
        }
        raw_state = state_aliases.get(str(raw_state).strip().upper(), raw_state)
        try:
            outcome = RuntimeOutcome.coerce(raw_state)
        except RuntimeContractError as exc:
            raise MalformedProviderResult("async poll response has unknown state") from exc
        task_id = payload.get("provider_task_id", payload.get("task_id", payload.get("id")))
        task_id = _optional_text(task_id, name="async_status.provider_task_id")
        metadata = payload.get("safe_metadata", payload.get("metadata", {}))
        if not isinstance(metadata, Mapping):
            raise MalformedProviderResult("async status metadata is malformed")
        _assert_safe_mapping(metadata, name="async_status")
        try:
            return DriverStatus(
                protocol_reference=reference,
                outcome=outcome,
                provider_task_id=task_id,
                terminal=_strict_bool(payload.get("terminal"), name="terminal"),
                safe_metadata=metadata,
                error_category=payload.get("error_category"),
                retryable=bool(
                    _strict_bool(payload.get("retryable"), name="retryable", default=False)
                ),
            )
        except RuntimeContractError as exc:
            raise MalformedProviderResult("async status is malformed") from exc

    def decode_result(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        if response.status_code < 200 or response.status_code >= 300 or not isinstance(response.payload, Mapping):
            raise MalformedProviderResult("async result response is malformed")
        return result_from_mapping(
            response.payload,
            request,
            protocol_reference=reference,
        )

    def encode_cancel(self, reference: str, request: CapabilityRequest | None = None) -> EncodedRequest:
        return EncodedRequest(payload={"protocol_reference": reference}, method="POST", path=f"{self.path or ''}/cancel")


@dataclass(frozen=True, slots=True)
class JsonStreamCodec(JsonProviderCodec):
    """Neutral stream codec that assembles text/value chunks."""

    def decode_chunk(self, chunk: Any, index: int, request: CapabilityRequest) -> StreamChunk:
        if isinstance(chunk, StreamChunk):
            return chunk
        if isinstance(chunk, Mapping):
            value = chunk.get("value", chunk.get("text", chunk.get("delta")))
            if value is None:
                raise MalformedProviderResult("stream chunk has no value")
            return StreamChunk(
                value=value,
                index=index,
                final=bool(
                    _strict_bool(chunk.get("final"), name="stream.final", default=False)
                ),
            )
        if isinstance(chunk, (str, bytes, int, float)):
            return StreamChunk(value=chunk, index=index)
        raise MalformedProviderResult("stream chunk is malformed")

    def finalize(self, chunks: tuple[StreamChunk, ...], request: CapabilityRequest) -> CapabilityResult:
        values = [chunk.value for chunk in chunks]
        _assert_safe_mapping({"stream_values": values}, name="stream")
        # Keep stream assembly provider-neutral.  A concrete codec can emit a
        # ContentRef or structured result instead.
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            safe_metadata={"chunk_count": len(chunks), "stream_values": values},
        )


class CallableCodec:
    """Adapter for tiny compatibility seams and tests.

    Every callback is optional only where the protocol does not need it; a
    missing required callback raises a codec error rather than guessing.
    """

    def __init__(
        self,
        *,
        codec_id: str = "callable.codec",
        codec_version: str = "1",
        validate: Callable[[CapabilityRequest, ModelManifest | None], None] | None = None,
        encode_request: Callable[[CapabilityRequest, ModelManifest | None], EncodedRequest] | None = None,
        decode_response: Callable[[DriverResponse, CapabilityRequest], CapabilityResult] | None = None,
        decode_task_identity: Callable[[DriverResponse, CapabilityRequest], ProviderTaskIdentity] | None = None,
        decode_task_state: Callable[[DriverResponse, str, CapabilityRequest | None], DriverStatus] | None = None,
        decode_result: Callable[[DriverResponse, str, CapabilityRequest], CapabilityResult] | None = None,
        encode_cancel: Callable[[str, CapabilityRequest | None], EncodedRequest] | None = None,
        decode_chunk: Callable[[Any, int, CapabilityRequest], StreamChunk] | None = None,
        finalize: Callable[[tuple[StreamChunk, ...], CapabilityRequest], CapabilityResult] | None = None,
    ) -> None:
        self.codec_id = codec_id
        self.codec_version = codec_version
        self._validate = validate
        self._encode_request = encode_request
        self._decode_response = decode_response
        self._decode_task_identity = decode_task_identity
        self._decode_task_state = decode_task_state
        self._decode_result = decode_result
        self._encode_cancel = encode_cancel
        self._decode_chunk = decode_chunk
        self._finalize = finalize

    def validate(self, request: CapabilityRequest, manifest: ModelManifest | None = None) -> None:
        validate_request_against_manifest(request, manifest, codec_id=self.codec_id)
        if self._validate is not None:
            self._validate(request, manifest)

    def encode_request(self, request: CapabilityRequest, manifest: ModelManifest | None = None) -> EncodedRequest:
        if self._encode_request is None:
            return JsonProviderCodec(codec_id=self.codec_id, codec_version=self.codec_version).encode_request(request, manifest)
        result = self._encode_request(request, manifest)
        if not isinstance(result, EncodedRequest):
            raise CodecError("encode_request callback must return EncodedRequest")
        return result

    def decode_response(self, response: DriverResponse, request: CapabilityRequest) -> CapabilityResult:
        if self._decode_response is None:
            return JsonProviderCodec(codec_id=self.codec_id, codec_version=self.codec_version).decode_response(response, request)
        result = self._decode_response(response, request)
        if not isinstance(result, CapabilityResult):
            raise MalformedProviderResult("decode_response callback must return CapabilityResult")
        return result

    def decode_task_identity(self, response: DriverResponse, request: CapabilityRequest) -> ProviderTaskIdentity:
        if self._decode_task_identity is None:
            raise CodecError("codec does not support async task identity decoding")
        result = self._decode_task_identity(response, request)
        if not isinstance(result, ProviderTaskIdentity):
            raise MalformedProviderResult("decode_task_identity callback must return ProviderTaskIdentity")
        return result

    def decode_task_state(self, response: DriverResponse, reference: str, request: CapabilityRequest | None = None) -> DriverStatus:
        if self._decode_task_state is None:
            raise CodecError("codec does not support async state decoding")
        result = self._decode_task_state(response, reference, request)
        if not isinstance(result, DriverStatus):
            raise MalformedProviderResult("decode_task_state callback must return DriverStatus")
        return result

    def decode_result(self, response: DriverResponse, reference: str, request: CapabilityRequest) -> CapabilityResult:
        if self._decode_result is None:
            raise CodecError("codec does not support async result decoding")
        result = self._decode_result(response, reference, request)
        if not isinstance(result, CapabilityResult):
            raise MalformedProviderResult("decode_result callback must return CapabilityResult")
        return result

    def encode_cancel(self, reference: str, request: CapabilityRequest | None = None) -> EncodedRequest:
        if self._encode_cancel is None:
            raise CodecError("codec does not support cancellation")
        result = self._encode_cancel(reference, request)
        if not isinstance(result, EncodedRequest):
            raise CodecError("encode_cancel callback must return EncodedRequest")
        return result

    def decode_chunk(self, chunk: Any, index: int, request: CapabilityRequest) -> StreamChunk:
        if self._decode_chunk is None:
            raise CodecError("codec does not support stream decoding")
        result = self._decode_chunk(chunk, index, request)
        if not isinstance(result, StreamChunk):
            raise MalformedProviderResult("decode_chunk callback must return StreamChunk")
        return result

    def finalize(self, chunks: tuple[StreamChunk, ...], request: CapabilityRequest) -> CapabilityResult:
        if self._finalize is None:
            raise CodecError("codec does not support stream finalization")
        result = self._finalize(chunks, request)
        if not isinstance(result, CapabilityResult):
            raise MalformedProviderResult("finalize callback must return CapabilityResult")
        return result


# Common aliases used by compatibility code and tests.
ProviderCodecProtocol = ProviderCodec
AsyncCodec = AsyncTaskCodec
StreamCodecProtocol = StreamCodec
GenericProviderCodec = JsonProviderCodec


__all__ = [
    "AsyncCodec",
    "AsyncTaskCodec",
    "CallableCodec",
    "CodecError",
    "GenericProviderCodec",
    "JsonAsyncTaskCodec",
    "JsonProviderCodec",
    "JsonStreamCodec",
    "MalformedProviderResult",
    "ProviderCodec",
    "ProviderCodecProtocol",
    "StreamCodec",
    "StreamCodecProtocol",
    "result_from_mapping",
    "validate_request_against_manifest",
]
