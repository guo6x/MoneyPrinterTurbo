"""Provider-neutral contracts for the universal model runtime.

The objects in this module deliberately describe *what* the product wants to
run and the safe identity of the result.  They do not contain provider JSON,
credentials, signed URLs, or local filesystem paths.  Provider wire details
belong in :mod:`aidrama_studio.services.model_runtime.codecs`.

The contracts are frozen dataclasses.  Mapping values are recursively copied
to read-only ``MappingProxyType`` objects at construction time so a request or
result cannot be changed underneath a driver after it has been submitted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


_UNSET_PROTOCOL = object()


class RuntimeContractError(ValueError):
    """Raised when a capability request/result violates the runtime contract."""


class CapabilityKind(str, Enum):
    """Canonical product capabilities.

    ``VIDEO_GENERATIVE`` is retained as a source-compatible alias for older
    AIDrama code.  Its canonical serialized value is still ``VIDEO``.
    """

    LLM = "LLM"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    VIDEO_GENERATIVE = "VIDEO"  # compatibility alias; never a new capability
    VISION = "VISION"
    TTS = "TTS"

    @classmethod
    def coerce(cls, value: "CapabilityKind | str") -> "CapabilityKind":
        if isinstance(value, cls):
            return value
        # The legacy AIDrama capability enum is a separate ``str, Enum``
        # class (and therefore stringifies as ``CapabilityKind.X`` rather
        # than its value).  Read enum values before normalising so the
        # compatibility bridge can accept either enum without importing the
        # legacy provider layer here.
        if isinstance(value, Enum):
            value = value.value
        try:
            normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
            if normalized == "VIDEO_GENERATIVE":
                normalized = "VIDEO"
            return cls(normalized)
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError(f"unsupported capability: {value!r}") from exc


# A shorter name is convenient for callers and makes the contract read like
# the architecture document.  Keep CapabilityKind as the primary export for
# compatibility with the existing ai_capabilities module.
Capability = CapabilityKind
CapabilityContract = CapabilityKind
UniversalCapability = CapabilityKind
ModelCapability = CapabilityKind


class ProtocolFamily(str, Enum):
    """Lifecycle protocol, independent from the capability being served."""

    REQUEST_RESPONSE = "REQUEST_RESPONSE"
    ASYNC_TASK = "ASYNC_TASK"
    STREAM = "STREAM"

    @classmethod
    def coerce(cls, value: "ProtocolFamily | str") -> "ProtocolFamily":
        if isinstance(value, cls):
            return value
        if isinstance(value, Enum):
            value = value.value
        try:
            return cls(
                str(value).strip().upper().replace("-", "_").replace(" ", "_")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError(f"unsupported protocol family: {value!r}") from exc


# Short aliases used by a few integrations and by the architecture document.
Protocol = ProtocolFamily
ProtocolKind = ProtocolFamily


class RuntimeOutcome(str, Enum):
    """Provider-neutral lifecycle outcomes used by all drivers."""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ARTIFACT_PENDING = "ARTIFACT_PENDING"

    @classmethod
    def coerce(cls, value: "RuntimeOutcome | str") -> "RuntimeOutcome":
        if isinstance(value, cls):
            return value
        if isinstance(value, Enum):
            value = value.value
        try:
            return cls(str(value).strip().upper())
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError(f"unsupported runtime outcome: {value!r}") from exc


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-like values without changing their semantics."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return a JSON-friendly mutable copy of a frozen contract value."""

    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _require_text(name: str, value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RuntimeContractError(f"{name} must be text")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise RuntimeContractError(f"{name} must not be empty")
    return normalized


def _freeze_map(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise RuntimeContractError("mapping field must be a mapping")
    return _freeze(value)


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Immutable reference to an input or output artifact.

    A reference carries identity and provenance only.  Drivers/codecs may use
    a transient local path while executing, but that path is intentionally not
    representable here.
    """

    source_kind: str
    source_id: str
    role: str = ""
    mime_type: str = "application/octet-stream"
    sha256: str = ""
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", _require_text("source_kind", self.source_kind))
        object.__setattr__(self, "source_id", _require_text("source_id", self.source_id))
        object.__setattr__(self, "role", _require_text("role", self.role, allow_empty=True))
        object.__setattr__(self, "mime_type", _require_text("mime_type", self.mime_type))
        digest = _require_text("sha256", self.sha256, allow_empty=True).lower()
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise RuntimeContractError("sha256 must be a hexadecimal SHA-256 digest")
        object.__setattr__(self, "sha256", digest)
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise RuntimeContractError("size_bytes must be a non-negative integer")
        object.__setattr__(self, "metadata", _freeze_map(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "role": self.role,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
        }
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        if self.metadata:
            result["metadata"] = thaw(self.metadata)
        return result


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """Capability-neutral invocation request.

    ``provider_parameters`` is intentionally opaque.  It is frozen and passed
    unchanged to the selected codec; no product/service layer should inspect
    provider-specific keys.
    """

    request_id: str
    project_id: str = ""
    execution_id: str | None = None
    capability: CapabilityKind = CapabilityKind.LLM
    protocol_family: ProtocolFamily | str | object = _UNSET_PROTOCOL
    protocol: ProtocolFamily | str | None = None
    provider_id: str = ""
    model_id: str = ""
    manifest_id: str = ""
    manifest_hash: str = ""
    codec_id: str = ""
    runtime_plan_id: str | None = None
    runtime_plan_hash: str | None = None
    snapshot_id: str | None = None
    snapshot_hash: str | None = None
    inputs: tuple[ContentRef, ...] = ()
    prompt_or_text: str | None = None
    structured_input: Mapping[str, Any] = field(default_factory=dict)
    provider_parameters: Mapping[str, Any] = field(default_factory=dict)
    authorization_fingerprint: str | None = None
    deadline_at: str | None = None
    # Optional request-local readiness values are useful for compatibility
    # bridges.  A manifest remains the source of truth when one is supplied.
    create_authorized: bool | None = None
    authorization_required: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text("request_id", self.request_id))
        object.__setattr__(self, "project_id", _require_text("project_id", self.project_id, allow_empty=True))
        if self.execution_id is not None:
            object.__setattr__(self, "execution_id", _require_text("execution_id", self.execution_id))
        object.__setattr__(self, "capability", CapabilityKind.coerce(self.capability))
        # ``protocol`` is a compatibility alias for ``protocol_family``.  If
        # both spellings are supplied they must describe the same lifecycle;
        # silently preferring one would let a caller route a frozen request
        # through a different driver than the one it selected.
        protocol_family_supplied = self.protocol_family is not _UNSET_PROTOCOL
        normalized_protocol_family = (
            ProtocolFamily.coerce(self.protocol_family)
            if protocol_family_supplied
            else ProtocolFamily.REQUEST_RESPONSE
        )
        if self.protocol is not None:
            normalized_protocol = ProtocolFamily.coerce(self.protocol)
            if (
                protocol_family_supplied
                and normalized_protocol is not normalized_protocol_family
            ):
                raise RuntimeContractError(
                    "protocol and protocol_family conflict"
                )
        else:
            normalized_protocol = normalized_protocol_family
        object.__setattr__(self, "protocol_family", normalized_protocol)
        object.__setattr__(self, "protocol", normalized_protocol)
        for name in ("provider_id", "model_id", "manifest_id", "codec_id"):
            value = _require_text(name, getattr(self, name), allow_empty=True)
            object.__setattr__(self, name, value)
        for name in ("manifest_hash", "runtime_plan_hash", "snapshot_hash", "authorization_fingerprint"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_text(name, value, allow_empty=True))
        if self.prompt_or_text is not None:
            object.__setattr__(self, "prompt_or_text", str(self.prompt_or_text))
        if self.deadline_at is not None:
            object.__setattr__(self, "deadline_at", _require_text("deadline_at", self.deadline_at))
        refs: list[ContentRef] = []
        if not isinstance(self.inputs, Sequence) or isinstance(self.inputs, (str, bytes)):
            raise RuntimeContractError("inputs must be a sequence of ContentRef")
        for item in self.inputs:
            if not isinstance(item, ContentRef):
                raise RuntimeContractError("inputs must contain ContentRef values")
            refs.append(item)
        object.__setattr__(self, "inputs", tuple(refs))
        object.__setattr__(self, "structured_input", _freeze_map(self.structured_input))
        object.__setattr__(self, "provider_parameters", _freeze_map(self.provider_parameters))
        if self.create_authorized is not None and not isinstance(self.create_authorized, bool):
            raise RuntimeContractError("create_authorized must be bool or None")
        if self.authorization_required is not None and not isinstance(self.authorization_required, bool):
            raise RuntimeContractError("authorization_required must be bool or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "execution_id": self.execution_id,
            "capability": self.capability.value,
            "protocol_family": self.protocol_family.value,
            "protocol": self.protocol_family.value,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "codec_id": self.codec_id,
            "runtime_plan_id": self.runtime_plan_id,
            "runtime_plan_hash": self.runtime_plan_hash,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "inputs": [item.to_dict() for item in self.inputs],
            "prompt_or_text": self.prompt_or_text,
            "structured_input": thaw(self.structured_input),
            "provider_parameters": thaw(self.provider_parameters),
            "authorization_fingerprint": self.authorization_fingerprint,
            "deadline_at": self.deadline_at,
            "create_authorized": self.create_authorized,
            "authorization_required": self.authorization_required,
        }


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Safe, provider-neutral invocation result."""

    request_id: str
    outcome: RuntimeOutcome = RuntimeOutcome.SUCCEEDED
    protocol_reference: str | None = None
    provider_task_id: str | None = None
    outputs: tuple[ContentRef, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    safe_metadata: Mapping[str, Any] = field(default_factory=dict)
    error_category: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text("request_id", self.request_id))
        object.__setattr__(self, "outcome", RuntimeOutcome.coerce(self.outcome))
        for name in ("protocol_reference", "provider_task_id", "error_category"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_text(name, value))
        if not isinstance(self.retryable, bool):
            raise RuntimeContractError("retryable must be bool")
        outputs: list[ContentRef] = []
        if not isinstance(self.outputs, Sequence) or isinstance(self.outputs, (str, bytes)):
            raise RuntimeContractError("outputs must be a sequence of ContentRef")
        for item in self.outputs:
            if not isinstance(item, ContentRef):
                raise RuntimeContractError("outputs must contain ContentRef values")
            outputs.append(item)
        object.__setattr__(self, "outputs", tuple(outputs))
        object.__setattr__(self, "usage", _freeze_map(self.usage))
        object.__setattr__(self, "safe_metadata", _freeze_map(self.safe_metadata))

    @property
    def succeeded(self) -> bool:
        return self.outcome is RuntimeOutcome.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "outcome": self.outcome.value,
            "protocol_reference": self.protocol_reference,
            "provider_task_id": self.provider_task_id,
            "outputs": [item.to_dict() for item in self.outputs],
            "usage": thaw(self.usage),
            "safe_metadata": thaw(self.safe_metadata),
            "error_category": self.error_category,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class EncodedRequest:
    """Codec output consumed by a protocol transport."""

    payload: Any
    method: str = "POST"
    path: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", _require_text("method", self.method).upper())
        if self.path is not None:
            object.__setattr__(self, "path", _require_text("path", self.path))
        if not isinstance(self.headers, Mapping):
            raise RuntimeContractError("headers must be a mapping")
        headers = {str(key): str(value) for key, value in self.headers.items()}
        object.__setattr__(self, "headers", MappingProxyType(headers))
        if self.idempotency_key is not None:
            object.__setattr__(self, "idempotency_key", _require_text("idempotency_key", self.idempotency_key))
        if self.timeout_seconds is not None:
            if (
                isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or self.timeout_seconds <= 0
            ):
                raise RuntimeContractError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class DriverResponse:
    """Normalized transport response passed into codecs."""

    payload: Any
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or self.status_code < 100:
            raise RuntimeContractError("status_code must be a valid HTTP-like integer")
        if not isinstance(self.headers, Mapping):
            raise RuntimeContractError("headers must be a mapping")
        object.__setattr__(
            self,
            "headers",
            MappingProxyType({str(key): str(value) for key, value in self.headers.items()}),
        )

    @classmethod
    def from_value(cls, value: Any) -> "DriverResponse":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            # A fake transport may return an HTTP-shaped mapping.  Preserve
            # arbitrary provider payloads unless the reserved envelope keys
            # are present.
            # Only a numeric ``status`` is an HTTP-like envelope.  Provider
            # task payloads very commonly use ``status: RUNNING`` and must be
            # passed through untouched for the codec to decode.
            raw_status = value.get("status_code")
            if raw_status is None and isinstance(value.get("status"), (int, float)):
                raw_status = value.get("status")
            status = 200 if raw_status is None else raw_status
            has_envelope = any(key in value for key in ("status_code", "payload", "body", "headers"))
            payload = value.get("payload", value.get("body", value)) if has_envelope else value
            headers = value.get("headers", {}) if has_envelope else {}
            try:
                return cls(payload=payload, status_code=int(status), headers=headers)
            except (TypeError, ValueError) as exc:
                raise RuntimeContractError("malformed transport response") from exc
        return cls(payload=value)


@dataclass(frozen=True, slots=True)
class ProviderTaskIdentity:
    """Stable remote identity returned by an async create operation."""

    protocol_reference: str
    provider_task_id: str | None = None
    safe_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_reference", _require_text("protocol_reference", self.protocol_reference))
        if self.provider_task_id is not None:
            object.__setattr__(self, "provider_task_id", _require_text("provider_task_id", self.provider_task_id))
        object.__setattr__(self, "safe_metadata", _freeze_map(self.safe_metadata))

    @property
    def reference(self) -> str:
        return self.protocol_reference

    @property
    def remote_id(self) -> str:
        return self.protocol_reference

    @property
    def task_id(self) -> str:
        return self.provider_task_id or self.protocol_reference

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_reference": self.protocol_reference,
            "provider_task_id": self.provider_task_id,
            "safe_metadata": thaw(self.safe_metadata),
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class DriverSubmission:
    """Result of an async create; it never contains a raw provider body."""

    request_id: str
    protocol_reference: str
    provider_task_id: str | None = None
    outcome: RuntimeOutcome = RuntimeOutcome.SUBMITTED
    safe_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text("request_id", self.request_id))
        object.__setattr__(self, "protocol_reference", _require_text("protocol_reference", self.protocol_reference))
        if self.provider_task_id is not None:
            object.__setattr__(self, "provider_task_id", _require_text("provider_task_id", self.provider_task_id))
        object.__setattr__(self, "outcome", RuntimeOutcome.coerce(self.outcome))
        object.__setattr__(self, "safe_metadata", _freeze_map(self.safe_metadata))

    @property
    def remote_id(self) -> str:
        return self.protocol_reference

    @property
    def task_id(self) -> str:
        return self.provider_task_id or self.protocol_reference

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "protocol_reference": self.protocol_reference,
            "provider_task_id": self.provider_task_id,
            "outcome": self.outcome.value,
            "safe_metadata": thaw(self.safe_metadata),
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class DriverStatus:
    """Normalized async poll status."""

    protocol_reference: str
    outcome: RuntimeOutcome = RuntimeOutcome.RUNNING
    provider_task_id: str | None = None
    terminal: bool | None = None
    safe_metadata: Mapping[str, Any] = field(default_factory=dict)
    error_category: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_reference", _require_text("protocol_reference", self.protocol_reference))
        object.__setattr__(self, "outcome", RuntimeOutcome.coerce(self.outcome))
        if self.provider_task_id is not None:
            object.__setattr__(self, "provider_task_id", _require_text("provider_task_id", self.provider_task_id))
        if self.terminal is None:
            object.__setattr__(
                self,
                "terminal",
                self.outcome in {RuntimeOutcome.SUCCEEDED, RuntimeOutcome.FAILED, RuntimeOutcome.CANCELLED},
            )
        elif not isinstance(self.terminal, bool):
            raise RuntimeContractError("terminal must be bool or None")
        if self.error_category is not None:
            object.__setattr__(self, "error_category", _require_text("error_category", self.error_category))
        if not isinstance(self.retryable, bool):
            raise RuntimeContractError("retryable must be bool")
        object.__setattr__(self, "safe_metadata", _freeze_map(self.safe_metadata))

    @property
    def state(self) -> RuntimeOutcome:
        return self.outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_reference": self.protocol_reference,
            "outcome": self.outcome.value,
            "provider_task_id": self.provider_task_id,
            "terminal": self.terminal,
            "safe_metadata": thaw(self.safe_metadata),
            "error_category": self.error_category,
            "retryable": self.retryable,
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One provider stream event after codec decoding."""

    value: Any
    index: int = 0
    final: bool = False

    def __post_init__(self) -> None:
        # Stream values are often structured mappings.  Freeze JSON-like
        # values so an incremental result cannot be mutated after the driver
        # has observed it; opaque SDK objects are left untouched for codecs
        # that intentionally use a native value type.
        object.__setattr__(self, "value", _freeze(self.value))
        if not isinstance(self.index, int) or self.index < 0:
            raise RuntimeContractError("stream chunk index must be non-negative")
        if not isinstance(self.final, bool):
            raise RuntimeContractError("stream chunk final must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {"value": thaw(self.value), "index": self.index, "final": self.final}


Request = CapabilityRequest
Result = CapabilityResult
TaskIdentity = ProviderTaskIdentity
TaskStatus = DriverStatus
# Friendly names used by a few host integrations.  They are aliases rather
# than additional schemas, so every caller still shares the same immutable
# capability-neutral contract.
ModelRequest = CapabilityRequest
ModelResult = CapabilityResult
UniversalRequest = CapabilityRequest
UniversalResult = CapabilityResult


__all__ = [
    "Capability",
    "CapabilityContract",
    "CapabilityKind",
    "CapabilityRequest",
    "CapabilityResult",
    "ContentRef",
    "DriverResponse",
    "DriverStatus",
    "DriverSubmission",
    "EncodedRequest",
    "ProtocolFamily",
    "Protocol",
    "ProtocolKind",
    "ProviderTaskIdentity",
    "RuntimeContractError",
    "RuntimeOutcome",
    "Request",
    "Result",
    "ModelRequest",
    "ModelResult",
    "UniversalRequest",
    "UniversalResult",
    "StreamChunk",
    "TaskIdentity",
    "TaskStatus",
    "UniversalCapability",
    "ModelCapability",
    "thaw",
]
