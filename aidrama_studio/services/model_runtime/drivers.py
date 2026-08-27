"""Lifecycle drivers for REQUEST_RESPONSE, ASYNC_TASK, and STREAM protocols.

Drivers own common lifecycle behavior and intentionally know nothing about
provider JSON.  Every provider-specific field enters/leaves through a codec.
The async driver has an especially strict boundary: ``poll``, ``reconcile``,
and ``collect`` can never call ``create``.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from .codecs import (
    AsyncTaskCodec,
    CodecError,
    MalformedProviderResult,
    ProviderCodec,
    StreamCodec,
    validate_request_against_manifest,
)
from .contracts import (
    CapabilityRequest,
    CapabilityResult,
    DriverResponse,
    DriverStatus,
    DriverSubmission,
    EncodedRequest,
    ProviderTaskIdentity,
    ProtocolFamily,
    RuntimeContractError,
    RuntimeOutcome,
    StreamChunk,
)
from .manifest import ModelManifest


class DriverError(RuntimeContractError):
    """Base protocol-driver error."""


class TransportError(DriverError):
    """Raised for transport failures or non-success status responses."""


class CreateAuthorizationError(DriverError):
    """Raised before transport when a paid/create authorization is absent."""


class UnsupportedOperationError(DriverError):
    """Raised only for required lifecycle operations that a driver lacks."""


class MalformedTransportResponse(TransportError):
    """Raised when a transport cannot be normalized to a DriverResponse."""


@runtime_checkable
class ProtocolDriver(Protocol):
    """Structural marker for lifecycle drivers.

    Concrete drivers expose family-specific methods; this marker only binds
    the common ``family`` identity so registries can reject unsupported
    protocols before a transport call.
    """

    family: ProtocolFamily


Driver = ProtocolDriver


@runtime_checkable
class RequestResponseTransport(Protocol):
    def send(self, request: EncodedRequest, context: CapabilityRequest | None = None) -> Any: ...


@runtime_checkable
class AsyncTaskTransport(Protocol):
    def create(self, request: EncodedRequest, context: CapabilityRequest | None = None) -> Any: ...

    def poll(self, reference: str, context: CapabilityRequest | None = None) -> Any: ...

    def fetch_result(self, reference: str, context: CapabilityRequest | None = None) -> Any: ...


@runtime_checkable
class StreamTransport(Protocol):
    def open(self, request: EncodedRequest, context: CapabilityRequest | None = None) -> Any: ...


def _call_compatible(function: Any, *args: Any) -> Any:
    """Call a fake/real transport with only the positional args it accepts.

    This keeps the seam pleasant for tiny test doubles (``send(encoded)``)
    while supporting production transports that also receive the frozen
    capability request.  A ``TypeError`` raised *inside* a function is not
    swallowed: argument arity is determined before invocation.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind is parameter.VAR_POSITIONAL for parameter in signature.parameters.values()):
        return function(*args)
    count = min(len(positional), len(args))
    return function(*args[:count])


def _transport_response(value: Any) -> DriverResponse:
    try:
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
            return DriverResponse(payload=value[1], status_code=value[0])
        return DriverResponse.from_value(value)
    except (TypeError, ValueError, RuntimeContractError) as exc:
        raise MalformedTransportResponse("transport response is malformed") from exc


def _require_result(result: Any, request: CapabilityRequest) -> CapabilityResult:
    if not isinstance(result, CapabilityResult):
        raise MalformedProviderResult("codec did not return CapabilityResult")
    if result.request_id != request.request_id:
        raise MalformedProviderResult("codec result request identity mismatch")
    return result


def _require_status(status: Any, reference: str) -> DriverStatus:
    if not isinstance(status, DriverStatus):
        raise MalformedProviderResult("codec did not return DriverStatus")
    if status.protocol_reference != reference:
        raise MalformedProviderResult("codec status reference identity mismatch")
    return status


def _coerce_identity(value: Any) -> ProviderTaskIdentity:
    """Require the codec to return a provider-neutral task identity.

    Provider JSON names (``task_id``, ``id``, and similar) belong in the
    codec.  Keeping this boundary strict prevents a transport/driver from
    accidentally learning a provider payload shape during reconciliation.
    """

    if isinstance(value, ProviderTaskIdentity):
        return value
    raise MalformedProviderResult("codec did not return a stable task identity")


def _manifest_from(codec: Any, explicit: ModelManifest | None) -> object | None:
    if explicit is not None:
        return explicit
    candidate = getattr(codec, "manifest", None)
    # Compatibility manifests are intentionally duck-typed.  This lets the
    # existing provider inventory adopt a driver without importing the new
    # concrete manifest class, while still ignoring arbitrary codec attrs.
    if candidate is not None and (
        (
            isinstance(candidate, Mapping)
            and any(
                name in candidate
                for name in (
                    "capability",
                    "capabilities",
                    "protocol",
                    "protocol_family",
                    "model_id",
                )
            )
        )
        or any(
            hasattr(candidate, name)
            for name in (
                "capability",
                "capabilities",
                "protocol",
                "protocol_family",
                "model_id",
            )
        )
    ):
        return candidate
    return None


def _validate_family(request: CapabilityRequest, family: ProtocolFamily) -> None:
    if request.protocol_family is not family:
        raise DriverError(
            f"request protocol {request.protocol_family.value} cannot run on {family.value} driver"
        )


def _codec_validate(codec: Any, request: CapabilityRequest, manifest: ModelManifest | None) -> None:
    if not hasattr(codec, "encode_request"):
        raise CodecError("codec does not implement encode_request")
    validator = getattr(codec, "validate", None)
    if validator is None:
        # Validation against the frozen manifest was already performed by the
        # driver; a tiny codec is allowed to expose only encode/decode hooks.
        return
    try:
        _call_compatible(validator, request, manifest)
    except (CodecError, RuntimeContractError):
        raise
    except Exception as exc:
        raise CodecError("codec validation failed") from exc


def _codec_encode(codec: Any, request: CapabilityRequest, manifest: ModelManifest | None) -> EncodedRequest:
    try:
        encoded = _call_compatible(codec.encode_request, request, manifest)
    except (CodecError, RuntimeContractError):
        raise
    except Exception as exc:
        raise CodecError("codec request encoding failed") from exc
    if not isinstance(encoded, EncodedRequest):
        raise CodecError("codec encode_request must return EncodedRequest")
    return encoded


class RequestResponseDriver:
    """One-shot request → response lifecycle driver."""

    family = ProtocolFamily.REQUEST_RESPONSE

    def __init__(
        self,
        transport: Any,
        *,
        manifest: ModelManifest | None = None,
        max_retries: int = 0,
    ) -> None:
        self.transport = transport
        self.manifest = manifest
        if not isinstance(max_retries, int) or max_retries < 0 or max_retries > 5:
            raise DriverError("max_retries must be between 0 and 5")
        self.max_retries = max_retries

    def invoke(
        self,
        request: CapabilityRequest,
        codec: ProviderCodec,
        manifest: ModelManifest | None = None,
        *,
        authorization: object | None = None,
    ) -> CapabilityResult:
        _validate_family(request, self.family)
        selected_manifest = _manifest_from(
            codec, manifest if manifest is not None else self.manifest
        )
        # A request/response generation call is still a provider CREATE even
        # though it has no separate task identity.  Apply the same explicit
        # authorization gate used by async creates before encoding or any
        # transport call; non-gated/free models remain unaffected.
        AsyncTaskDriver._check_create_authorization(
            request, selected_manifest, authorization
        )
        validate_request_against_manifest(request, selected_manifest, codec_id=getattr(codec, "codec_id", None))
        _codec_validate(codec, request, selected_manifest)
        encoded = _codec_encode(codec, request, selected_manifest)
        method = getattr(self.transport, "send", None)
        if method is None:
            method = (
                getattr(self.transport, "request", None)
                or getattr(self.transport, "invoke", None)
                or getattr(self.transport, "call", None)
                or getattr(self.transport, "post", None)
            )
        if method is None and callable(self.transport):
            method = self.transport
        if method is None:
            raise TransportError("request/response transport has no send method")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                try:
                    raw_response = _call_compatible(method, encoded, request)
                except Exception as exc:
                    raise TransportError("request/response transport failed") from exc
                response = _transport_response(raw_response)
                if response.status_code < 200 or response.status_code >= 300:
                    raise TransportError(f"request/response transport returned {response.status_code}")
                try:
                    result = codec.decode_response(response, request)
                except (MalformedProviderResult, CodecError, RuntimeContractError):
                    raise
                except Exception as exc:
                    raise MalformedProviderResult("codec response decoding failed") from exc
                return _require_result(result, request)
            except TransportError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
        raise TransportError("request/response transport failed") from last_error

    # ``submit`` is a useful compatibility spelling for one-shot protocols.
    submit = invoke
    execute = invoke
    run = invoke


class AsyncTaskDriver:
    """Create/poll/result lifecycle with strict no-resubmit reconciliation."""

    family = ProtocolFamily.ASYNC_TASK

    def __init__(
        self,
        transport: Any,
        *,
        manifest: ModelManifest | None = None,
    ) -> None:
        self.transport = transport
        self.manifest = manifest
        # Request context is retained only in memory for convenience.  Durable
        # identity remains the caller's responsibility and is never recreated.
        self._requests: dict[str, CapabilityRequest] = {}

    def _selected_manifest(self, codec: Any, manifest: ModelManifest | None) -> ModelManifest | None:
        return _manifest_from(
            codec, manifest if manifest is not None else self.manifest
        )

    @staticmethod
    def _check_create_authorization(
        request: CapabilityRequest,
        manifest: object | None,
        authorization: object | None = None,
    ) -> None:
        def manifest_field(name: str, default: object = None) -> object:
            if isinstance(manifest, Mapping):
                return manifest.get(name, default)
            return getattr(manifest, name, default) if manifest is not None else default

        manifest_authorization = manifest_field("authorization", {})
        if not isinstance(manifest_authorization, Mapping):
            manifest_authorization = {
                key: getattr(manifest_authorization, key)
                for key in ("create_is_paid", "requires_create_authorization")
                if hasattr(manifest_authorization, key)
            }
        explicit_authorized: bool | None = None
        if isinstance(authorization, Mapping):
            for key in (
                "create_authorized",
                "authorized",
                "approved",
                "allow_paid_live_tests",
                "live_authorized",
            ):
                if key in authorization:
                    explicit_authorized = authorization[key] is True
                    break
        elif authorization is not None:
            explicit_authorized = authorization is True
        if manifest is not None:
            manifest_readiness = manifest_field("readiness", {})
            if not isinstance(manifest_readiness, Mapping):
                manifest_readiness = {}
            # Authorization requirements are security-sensitive.  A literal
            # ``False`` can state that a gate is not needed; malformed values
            # (for example the string ``"false"`` from a JSON boundary) fail
            # closed instead of disabling a paid-create gate.
            requirement_values = (
                manifest_field("authorization_required", None),
                manifest_authorization.get("requires_create_authorization"),
                manifest_readiness.get("authorization_required"),
                request.authorization_required,
            )
            required = any(
                value is True
                or (value is not None and not isinstance(value, bool))
                for value in requirement_values
            )
            # Request/RuntimePlan approval is the final, per-create source of
            # authorization.  A manifest's static readiness flag is only a
            # default and must not erase an explicit request approval.
            manifest_authorized = manifest_field("create_authorized", None)
            if manifest_authorized is None:
                manifest_authorized = manifest_readiness.get("create_authorized")
            authorized = (
                explicit_authorized
                if explicit_authorized is not None
                else (
                    request.create_authorized
                    if request.create_authorized is not None
                    else manifest_authorized
                )
            )
            # Only a literal boolean ``True`` is approval.  Do not let a
            # truthy string/number from a compatibility payload authorize a
            # paid provider create.
            authorized = authorized is True
        else:
            required = bool(request.authorization_required)
            # A required gate is fail-closed: ``None`` is not approval.
            authorized = (
                explicit_authorized
                if explicit_authorized is not None
                else request.create_authorized is True
            )
        if required and not authorized:
            raise CreateAuthorizationError(
                "create authorization is required before submitting this model task"
            )

    def create(
        self,
        request: CapabilityRequest,
        codec: AsyncTaskCodec,
        manifest: ModelManifest | None = None,
        *,
        authorization: object | None = None,
    ) -> DriverSubmission:
        _validate_family(request, self.family)
        selected_manifest = self._selected_manifest(codec, manifest)
        # Authorization is checked before encoding and, critically, before
        # *any* transport create call.
        self._check_create_authorization(request, selected_manifest, authorization)
        validate_request_against_manifest(request, selected_manifest, codec_id=getattr(codec, "codec_id", None))
        _codec_validate(codec, request, selected_manifest)
        encoded = _codec_encode(codec, request, selected_manifest)
        method = getattr(self.transport, "create", None)
        if method is None:
            method = (
                getattr(self.transport, "create_task", None)
                or getattr(self.transport, "submit", None)
                or getattr(self.transport, "submit_task", None)
            )
        if method is None:
            raise TransportError("async transport has no create method")
        try:
            response = _transport_response(_call_compatible(method, encoded, request))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError("async create transport failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise TransportError(f"async create transport returned {response.status_code}")
        try:
            identity = _call_compatible(codec.decode_task_identity, response, request)
        except (CodecError, RuntimeContractError):
            raise
        except Exception as exc:
            raise MalformedProviderResult("async create identity decoding failed") from exc
        identity = _coerce_identity(identity)
        self._requests[identity.protocol_reference] = request
        return DriverSubmission(
            request_id=request.request_id,
            protocol_reference=identity.protocol_reference,
            provider_task_id=identity.provider_task_id,
            outcome=RuntimeOutcome.SUBMITTED,
            safe_metadata=identity.safe_metadata,
        )

    # Audit terminology uses submit; both names have identical create-once
    # semantics.  Passing an existing identity is always reconciliation and
    # can never reach transport.create.
    def submit(
        self,
        request: CapabilityRequest,
        codec: AsyncTaskCodec,
        manifest: ModelManifest | None = None,
        *,
        existing_reference: str | None = None,
        existing_identity: str | ProviderTaskIdentity | None = None,
        reference: str | None = None,
        authorization: object | None = None,
    ) -> DriverSubmission | DriverStatus:
        aliases = [
            value.protocol_reference if isinstance(value, ProviderTaskIdentity) else value
            for value in (existing_reference, existing_identity, reference)
            if value is not None
        ]
        normalized_reference: str | None = None
        for value in aliases:
            if not isinstance(value, str) or not value.strip():
                raise DriverError("existing async identity must be non-empty text")
            if normalized_reference is None:
                normalized_reference = value.strip()
            elif normalized_reference != value.strip():
                raise DriverError("conflicting existing async identities")
        if normalized_reference is not None:
            return self.reconcile(normalized_reference, codec, request=request)
        return self.create(request, codec, manifest, authorization=authorization)

    def _request_for(self, reference: str, request: CapabilityRequest | None) -> CapabilityRequest | None:
        if request is not None:
            return request
        return self._requests.get(reference)

    def poll(
        self,
        reference: str,
        codec: AsyncTaskCodec,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        if not isinstance(reference, str) or not reference.strip():
            raise DriverError("async protocol reference is required")
        context = self._request_for(reference, request)
        method = getattr(self.transport, "poll", None)
        if method is None:
            method = (
                getattr(self.transport, "get_status", None)
                or getattr(self.transport, "poll_task", None)
            )
        if method is None:
            method = getattr(self.transport, "get_task", None)
        if method is None:
            method = getattr(self.transport, "status", None)
        if method is None:
            raise TransportError("async transport has no poll method")
        try:
            response = _transport_response(_call_compatible(method, reference, context))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError("async poll transport failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise TransportError(f"async poll transport returned {response.status_code}")
        try:
            status = _call_compatible(codec.decode_task_state, response, reference, context)
        except (CodecError, RuntimeContractError):
            raise
        except Exception as exc:
            raise MalformedProviderResult("async poll state decoding failed") from exc
        return _require_status(status, reference)

    def reconcile(
        self,
        reference: str,
        codec: AsyncTaskCodec,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        """Poll an existing remote identity without ever creating another task."""

        return self.poll(reference, codec, request=request)

    def collect(
        self,
        reference: str,
        codec: AsyncTaskCodec,
        request: CapabilityRequest | None = None,
    ) -> CapabilityResult:
        if not isinstance(reference, str) or not reference.strip():
            raise DriverError("async protocol reference is required")
        context = self._request_for(reference, request)
        if context is None:
            raise DriverError("collect requires the original request context")
        method = getattr(self.transport, "fetch_result", None)
        if method is None:
            method = (
                getattr(self.transport, "result", None)
                or getattr(self.transport, "get_result", None)
                or getattr(self.transport, "fetch", None)
            )
        if method is None:
            method = getattr(self.transport, "get", None)
        if method is None:
            raise TransportError("async transport has no result method")
        try:
            response = _transport_response(_call_compatible(method, reference, context))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError("async result transport failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise TransportError(f"async result transport returned {response.status_code}")
        try:
            result = _call_compatible(codec.decode_result, response, reference, context)
        except (CodecError, RuntimeContractError):
            raise
        except Exception as exc:
            raise MalformedProviderResult("async result decoding failed") from exc
        return _require_result(result, context)

    def cancel(
        self,
        reference: str,
        codec: AsyncTaskCodec,
        request: CapabilityRequest | None = None,
    ) -> bool:
        """Best-effort optional cancellation; unsupported does not fail create."""

        if not isinstance(reference, str) or not reference.strip():
            raise DriverError("async protocol reference is required")
        method = getattr(self.transport, "cancel", None)
        encoder = getattr(codec, "encode_cancel", None)
        if method is None or encoder is None:
            return False
        context = self._request_for(reference, request)
        try:
            encoded = _call_compatible(encoder, reference, context)
            if not isinstance(encoded, EncodedRequest):
                return False
            # Provider transports commonly accept either the encoded payload
            # or the opaque remote reference.  Prefer the encoded request when
            # the parameter name indicates payload; otherwise pass reference.
            try:
                first_name = next(iter(inspect.signature(method).parameters)).lower()
            except (StopIteration, TypeError, ValueError):
                first_name = "request"
            first = reference if any(token in first_name for token in ("ref", "task", "id")) else encoded
            value = _call_compatible(method, first, context)
            response = _transport_response(value)
            if response.status_code < 200 or response.status_code >= 300:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(response.payload, bool):
                return response.payload
            if isinstance(response.payload, Mapping):
                return bool(response.payload.get("cancelled", response.payload.get("success", True)))
            return True
        # Cancellation is an optional best-effort operation.  A provider SDK
        # may raise an arbitrary exception for an unsupported endpoint; that
        # must not turn the optional hook into a mandatory lifecycle failure.
        except Exception:
            return False

    # Explicit recovery spellings all route through the same poll-only or
    # best-effort implementation; none can submit a second task.
    poll_existing = poll
    reconcile_existing = reconcile
    fetch_result = collect
    result = collect


class StreamDriver:
    """Open a stream, decode incremental chunks, and finalize once."""

    family = ProtocolFamily.STREAM

    def __init__(self, transport: Any, *, manifest: ModelManifest | None = None) -> None:
        self.transport = transport
        self.manifest = manifest

    def invoke(
        self,
        request: CapabilityRequest,
        codec: StreamCodec,
        manifest: ModelManifest | None = None,
    ) -> CapabilityResult:
        _validate_family(request, self.family)
        selected_manifest = _manifest_from(
            codec, manifest if manifest is not None else self.manifest
        )
        validate_request_against_manifest(request, selected_manifest, codec_id=getattr(codec, "codec_id", None))
        _codec_validate(codec, request, selected_manifest)
        encoded = _codec_encode(codec, request, selected_manifest)
        method = getattr(self.transport, "open", None)
        if method is None:
            method = getattr(self.transport, "open_stream", None) or getattr(self.transport, "stream", None)
        if method is None and callable(self.transport):
            method = self.transport
        if method is None:
            raise TransportError("stream transport has no open method")
        try:
            opened = _call_compatible(method, encoded, request)
        except Exception as exc:
            raise TransportError("stream transport failed") from exc
        chunks: list[StreamChunk] = []
        try:
            iterable = opened
            # Accept the same small HTTP-shaped envelope as the other
            # drivers.  Provider-specific stream event decoding still belongs
            # entirely to the codec.
            if isinstance(opened, Mapping):
                # A mapping returned directly by a stream transport is only
                # valid when it is an explicit HTTP-shaped envelope.  Treat
                # an ordinary provider object/mapping as malformed instead
                # of iterating its keys as fake stream chunks.
                if not any(
                    key in opened for key in ("status_code", "payload", "body", "headers")
                ):
                    raise MalformedProviderResult(
                        "stream transport returned a mapping without an envelope"
                    )
                response = _transport_response(opened)
                if response.status_code < 200 or response.status_code >= 300:
                    raise TransportError(
                        f"stream transport returned {response.status_code}"
                    )
                iterable = response.payload
            elif isinstance(opened, DriverResponse) or (
                isinstance(opened, tuple)
                and len(opened) == 2
                and isinstance(opened[0], int)
            ):
                response = _transport_response(opened)
                if response.status_code < 200 or response.status_code >= 300:
                    raise TransportError(
                        f"stream transport returned {response.status_code}"
                    )
                iterable = response.payload
            if hasattr(opened, "__enter__") and hasattr(opened, "__exit__"):
                with opened as entered:
                    iterable = entered
                    self._decode_iterable(iterable, codec, request, chunks)
            else:
                self._decode_iterable(iterable, codec, request, chunks)
        except (CodecError, MalformedProviderResult, RuntimeContractError):
            raise
        except Exception as exc:
            raise TransportError("stream iteration failed") from exc
        try:
            result = _call_compatible(codec.finalize, tuple(chunks), request)
        except (CodecError, RuntimeContractError):
            raise
        except Exception as exc:
            raise MalformedProviderResult("stream finalization failed") from exc
        return _require_result(result, request)

    @staticmethod
    def _decode_iterable(
        iterable: Any,
        codec: StreamCodec,
        request: CapabilityRequest,
        chunks: list[StreamChunk],
    ) -> None:
        if isinstance(iterable, (str, bytes)) or not isinstance(iterable, Iterable):
            raise MalformedProviderResult("stream transport did not return an iterable")
        for index, raw_chunk in enumerate(iterable):
            try:
                decoded = _call_compatible(codec.decode_chunk, raw_chunk, index, request)
            except (CodecError, RuntimeContractError):
                raise
            except Exception as exc:
                raise MalformedProviderResult("stream chunk decoding failed") from exc
            if not isinstance(decoded, StreamChunk):
                raise MalformedProviderResult("codec did not return StreamChunk")
            if decoded.index != index:
                # Normalize accidental provider indexes while preserving the
                # chunk value; lifecycle ordering belongs to the driver.
                decoded = StreamChunk(value=decoded.value, index=index, final=decoded.final)
            chunks.append(decoded)

    # ``stream`` is an intuitive alias for callers that model this as an
    # iterator operation rather than a generic invocation.
    stream = invoke
    execute = invoke
    run = invoke


# Compatibility aliases matching the architecture document's protocol names.
RequestResponseProtocolDriver = RequestResponseDriver
AsyncProtocolDriver = AsyncTaskDriver
StreamProtocolDriver = StreamDriver
RequestResponseProtocol = RequestResponseDriver
AsyncTaskProtocol = AsyncTaskDriver
StreamProtocol = StreamDriver


__all__ = [
    "AsyncProtocolDriver",
    "AsyncTaskDriver",
    "AsyncTaskProtocol",
    "AsyncTaskTransport",
    "CreateAuthorizationError",
    "DriverError",
    "Driver",
    "MalformedTransportResponse",
    "RequestResponseDriver",
    "ProtocolDriver",
    "RequestResponseProtocol",
    "RequestResponseProtocolDriver",
    "RequestResponseTransport",
    "StreamDriver",
    "StreamProtocol",
    "StreamProtocolDriver",
    "StreamTransport",
    "TransportError",
    "UnsupportedOperationError",
]
