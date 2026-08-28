"""Immutable, provider-neutral model manifests.

A manifest is selection metadata, not a provider request.  It describes the
capability/protocol pair, model limits and non-secret readiness/authorization
state.  Wire fields, credentials and result bodies are intentionally absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .contracts import (
    CapabilityKind,
    ProtocolFamily,
    RuntimeContractError,
    thaw,
)


class ManifestValidationError(RuntimeContractError):
    """Raised when a model manifest is malformed or contains unsafe data."""


# ``None`` is a meaningful value for a few manifest fields, while capability
# and protocol have historical defaults.  A private sentinel lets the
# constructor distinguish an omitted canonical field from an explicitly
# supplied default when validating the compatibility aliases below.
_UNSET = object()


def _text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{name} must be text")
    result = value.strip()
    if not allow_empty and not result:
        raise ManifestValidationError(f"{name} must not be empty")
    return result


def _safe_text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    """Normalize a scalar manifest field and reject secret/path material."""

    result = _text(name, value, allow_empty=allow_empty)
    # Reuse the recursive safety checks used by mapping fields.  The
    # credential_reference slot is intentionally validated separately because
    # it contains an identifier (never the credential value).
    _freeze_json(result, path=name)
    return result


def _tuple_text(name: str, value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ManifestValidationError(f"{name} must be a sequence")
    result = tuple(_text(name, item) for item in value)
    if len(set(result)) != len(result):
        raise ManifestValidationError(f"{name} must not contain duplicates")
    return result


def _looks_like_private_path(value: str, path: str) -> bool:
    """Return whether *value* looks like a local/private filesystem path.

    Manifest metadata is durable selection data.  Absolute paths (and local
    path fields even when relative) are execution-environment details and can
    disclose a user's filesystem.  Keep endpoint URLs and ordinary labels
    usable, but reject path-shaped values before they can reach ``serialize``.
    """

    lowered_path = path.rsplit(".", 1)[-1].casefold().replace("-", "_")
    path_field = bool(
        re.search(
            r"(?:^|_)(?:path|file_path|filepath|filename|file_name|directory)(?:$|_)",
            lowered_path,
        )
    )
    # POSIX, drive-letter, UNC, rooted Windows, home-relative, and file://
    # paths are all local/private even when they happen to be represented as
    # a generic metadata value rather than under a path-named key.
    absolute = bool(
        re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|[\\/]|~[\\/])", value)
        or value.casefold().startswith("file:")
    )
    # A path-named field is not allowed to carry a relative path either.  A
    # slash in an ordinary URL is fine because the key itself is not path-like.
    return absolute or path_field


def _reject_unsafe_scalar(name: str, value: str) -> None:
    """Keep identity/display strings free of credentials and local paths."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ManifestValidationError(f"{name} must contain valid Unicode text") from exc
    lowered = value.casefold()
    url_userinfo = False
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered):
        try:
            parsed = urlsplit(value)
        except ValueError:
            parsed = None
        url_userinfo = bool(
            parsed is not None
            and (parsed.username is not None or parsed.password is not None)
        )
    if _looks_like_private_path(value, name) or url_userinfo:
        raise ManifestValidationError(f"{name} may not contain private path/URL credentials")
    if (
        lowered.startswith(("sk-", "rk-", "sess-"))
        or "bearer " in lowered
        or re.search(r"(?:api[_-]?key|access[_-]?key)\s*=", lowered)
        or re.search(r"^[a-z][a-z0-9+.-]*://[^/?#\s@]*(?::|%3a)[^/?#\s@]*@", lowered)
        or "-----begin " in lowered
        or re.search(
            r"[?&#](?:[^=&#]*(?:token|signature|credential|password|secret|api[_-]?key|access[_-]?key)[^=&#]*|sig|auth|key|session|expires)=",
            lowered,
        )
        or re.search(r"\bAKIA[0-9A-Z]{16}\b", value)
        or re.match(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", value)
    ):
        raise ManifestValidationError(f"{name} may not contain credential material")


def _freeze_json(value: Any, *, path: str = "metadata") -> Any:
    """Freeze JSON-like metadata and reject obvious secret-bearing fields."""

    secret_fragments = (
        "api_key",
        "apikey",
        "token",
        "credential",
        "secret",
        "password",
        "private_key",
        "access_token",
        "refresh_token",
        "signed_url",
        "raw_body",
        "authorization_header",
        "authorization",
        "access_key",
        "signature",
        "cookie",
        "session_token",
        "client_secret",
    )
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ManifestValidationError(f"{path} must contain valid Unicode text") from exc
        lowered_value = value.casefold()
        url_userinfo = False
        if re.match(r"^[a-z][a-z0-9+.-]*://", lowered_value):
            try:
                parsed_url = urlsplit(value)
            except ValueError:
                parsed_url = None
            url_userinfo = bool(
                parsed_url is not None
                and (parsed_url.username is not None or parsed_url.password is not None)
            )
        if _looks_like_private_path(value, path):
            raise ManifestValidationError(f"{path} may not contain a filesystem path")
        if (
            lowered_value.startswith(("sk-", "rk-", "sess-"))
            or "bearer " in lowered_value
            or re.search(r"(?:api[_-]?key|access[_-]?key)\s*=", lowered_value)
            or url_userinfo
            or re.search(r"^[a-z][a-z0-9+.-]*://[^/?#\s@]*(?::|%3a)[^/?#\s@]*@", lowered_value)
            or re.search(
                r"[?&#](?:[^=&#]*(?:token|signature|credential|password|secret|api[_-]?key|access[_-]?key)[^=&#]*|sig|auth|key|session|expires)=",
                lowered_value,
            )
            or re.search(r"\bAKIA[0-9A-Z]{16}\b", value)
            or re.match(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", value)
            or "-----begin " in lowered_value
        ):
            raise ManifestValidationError(f"{path} may not contain credential material")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            _reject_unsafe_scalar(f"{path}.{key}", key)
            lowered = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower().replace("-", "_")
            if (
                lowered in {"auth", "authentication"}
                or lowered.startswith("auth_")
                or any(fragment in lowered for fragment in secret_fragments)
            ):
                raise ManifestValidationError(f"{path}.{key} may not contain secrets")
            if key in result:
                raise ManifestValidationError(f"{path} contains duplicate key {key!r}")
            result[key] = _freeze_json(raw_value, path=f"{path}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError) as exc:
            raise ManifestValidationError(f"{path} must contain finite numbers") from exc
        if not finite:
            raise ManifestValidationError(f"{path} must contain finite numbers")
        return value
    raise ManifestValidationError(f"{path} must contain JSON-compatible values")


def _as_mapping(value: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{name} must be a mapping")
    frozen = _freeze_json(value, path=name)
    return frozen


def _mapping_alias(
    value: Mapping[str, Any],
    canonical: str,
    alias: str,
    *,
    default: Any = None,
) -> Any:
    """Read a schema alias while rejecting contradictory declarations."""

    has_canonical = canonical in value
    has_alias = alias in value
    if has_canonical and has_alias and value[canonical] != value[alias]:
        raise ManifestValidationError(f"{canonical} and {alias} conflict")
    if has_canonical:
        return value[canonical]
    if has_alias:
        return value[alias]
    return default


@dataclass(frozen=True, slots=True)
class DurationSpec:
    minimum: float | None = None
    maximum: float | None = None
    discrete_values: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        minimum = self.minimum
        maximum = self.maximum
        for name, number in (("minimum", minimum), ("maximum", maximum)):
            if number is None:
                continue
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ManifestValidationError(f"duration.{name} must be non-negative")
            try:
                finite_number = float(number)
            except (OverflowError, TypeError, ValueError) as exc:
                raise ManifestValidationError(f"duration.{name} must be non-negative") from exc
            if finite_number < 0 or not math.isfinite(finite_number):
                raise ManifestValidationError(f"duration.{name} must be non-negative")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ManifestValidationError("duration.minimum cannot exceed maximum")
        if isinstance(self.discrete_values, (str, bytes)) or not isinstance(
            self.discrete_values, Sequence
        ):
            raise ManifestValidationError("duration.discrete_values must contain numbers")
        try:
            raw_values = tuple(self.discrete_values)
            if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw_values):
                raise TypeError("duration values must be numeric")
            values = tuple(float(item) for item in raw_values)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ManifestValidationError(
                "duration.discrete_values must contain numbers"
            ) from exc
        if (
            any(item < 0 or not math.isfinite(item) for item in values)
            or len(set(values)) != len(values)
        ):
            raise ManifestValidationError("duration.discrete_values must be unique and non-negative")
        if minimum is not None and any(item < minimum for item in values):
            raise ManifestValidationError("duration.discrete_values fall below minimum")
        if maximum is not None and any(item > maximum for item in values):
            raise ManifestValidationError("duration.discrete_values exceed maximum")
        object.__setattr__(self, "minimum", float(minimum) if minimum is not None else None)
        object.__setattr__(self, "maximum", float(maximum) if maximum is not None else None)
        object.__setattr__(self, "discrete_values", values)

    @classmethod
    def from_value(cls, value: "DurationSpec | Mapping[str, Any] | None") -> "DurationSpec":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            for key in ("minimum", "min", "maximum", "max"):
                raw_number = value.get(key)
                if raw_number is not None and (
                    isinstance(raw_number, bool) or not isinstance(raw_number, (int, float))
                ):
                    raise ManifestValidationError(f"duration.{key} must be non-negative")
            for key in ("discrete_values", "discrete"):
                raw_values = value.get(key)
                if raw_values is None:
                    continue
                if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, Sequence):
                    raise ManifestValidationError("duration.discrete_values must contain numbers")
                for item in raw_values:
                    if isinstance(item, bool) or not isinstance(item, (int, float)):
                        raise ManifestValidationError("duration.discrete_values must contain numbers")
            return cls(
                minimum=_mapping_alias(value, "minimum", "min"),
                maximum=_mapping_alias(value, "maximum", "max"),
                discrete_values=tuple(
                    _mapping_alias(value, "discrete_values", "discrete", default=()) or ()
                ),
            )
        raise ManifestValidationError("duration must be DurationSpec or mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "discrete_values": list(self.discrete_values),
        }


@dataclass(frozen=True, slots=True)
class ResolutionSpec:
    supported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = _tuple_text("resolution.supported", self.supported)
        object.__setattr__(
            self,
            "supported",
            tuple(_safe_text("resolution.supported", item) for item in normalized),
        )

    @classmethod
    def from_value(cls, value: "ResolutionSpec | Mapping[str, Any] | Sequence[str] | None") -> "ResolutionSpec":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                supported=tuple(
                    _mapping_alias(value, "supported", "resolutions", default=()) or ()
                )
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return cls(supported=tuple(value))
        raise ManifestValidationError("resolution must be ResolutionSpec, mapping, or sequence")

    def to_dict(self) -> dict[str, Any]:
        return {"supported": list(self.supported)}


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    images: bool = False
    videos: bool = False
    max_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.images, bool) or not isinstance(self.videos, bool):
            raise ManifestValidationError("reference images/videos must be bool")
        if isinstance(self.max_count, bool) or not isinstance(self.max_count, int) or self.max_count < 0:
            raise ManifestValidationError("reference.max_count must be non-negative")

    @classmethod
    def from_value(cls, value: "ReferenceSpec | Mapping[str, Any] | None") -> "ReferenceSpec":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            for key in ("images", "images_supported", "videos", "videos_supported"):
                if key in value and not isinstance(value[key], bool):
                    raise ManifestValidationError("reference images/videos must be bool")
            for key in ("max_count", "max"):
                if key in value and (
                    isinstance(value[key], bool)
                    or not isinstance(value[key], int)
                    or value[key] < 0
                ):
                    raise ManifestValidationError("reference.max_count must be a non-negative integer")
            images = _mapping_alias(value, "images", "images_supported", default=False)
            videos = _mapping_alias(value, "videos", "videos_supported", default=False)
            if not isinstance(images, bool) or not isinstance(videos, bool):
                raise ManifestValidationError("reference images/videos must be bool")
            raw_max = _mapping_alias(value, "max_count", "max", default=0)
            if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max < 0:
                raise ManifestValidationError("reference.max_count must be a non-negative integer")
            return cls(images=images, videos=videos, max_count=raw_max)
        raise ManifestValidationError("reference must be ReferenceSpec or mapping")

    def to_dict(self) -> dict[str, Any]:
        return {"images": self.images, "videos": self.videos, "max_count": self.max_count}


@dataclass(frozen=True, slots=True)
class FeatureSupport:
    negative_prompt: bool = False
    seed: bool = False
    first_frame: bool = False
    last_frame: bool = False
    multi_reference: bool = False
    audio_reference: bool = False
    structured_output: bool = False
    streaming: bool = False
    cancellation: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if not isinstance(getattr(self, name), bool):
                raise ManifestValidationError(f"supports.{name} must be bool")

    @classmethod
    def from_value(cls, value: "FeatureSupport | Mapping[str, Any] | None") -> "FeatureSupport":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            unknown = set(value) - set(cls.__dataclass_fields__)
            if unknown:
                raise ManifestValidationError(
                    "supports contains unsupported fields: " + ", ".join(sorted(map(str, unknown)))
                )
            valid: dict[str, bool] = {}
            for name in cls.__dataclass_fields__:
                if name in value:
                    if not isinstance(value[name], bool):
                        raise ManifestValidationError(f"supports.{name} must be bool")
                    valid[name] = value[name]
            return cls(**valid)
        raise ManifestValidationError("supports must be FeatureSupport or mapping")

    def to_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AuthorizationMetadata:
    create_is_paid: bool = False
    requires_create_authorization: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.create_is_paid, bool) or not isinstance(self.requires_create_authorization, bool):
            raise ManifestValidationError("authorization flags must be bool")

    @classmethod
    def from_value(cls, value: "AuthorizationMetadata | Mapping[str, Any] | None") -> "AuthorizationMetadata":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            for key in (
                "create_is_paid",
                "paid_create",
                "requires_create_authorization",
                "authorization_required",
            ):
                if key in value and not isinstance(value[key], bool):
                    raise ManifestValidationError("authorization flags must be bool")
            paid = _mapping_alias(value, "create_is_paid", "paid_create", default=False)
            required = _mapping_alias(
                value,
                "requires_create_authorization",
                "authorization_required",
                default=False,
            )
            if not isinstance(paid, bool) or not isinstance(required, bool):
                raise ManifestValidationError("authorization flags must be bool")
            return cls(create_is_paid=paid, requires_create_authorization=required)
        raise ManifestValidationError("authorization must be AuthorizationMetadata or mapping")

    def to_dict(self) -> dict[str, bool]:
        return {
            "create_is_paid": self.create_is_paid,
            "requires_create_authorization": self.requires_create_authorization,
        }


@dataclass(frozen=True, slots=True)
class ReadinessState:
    """Independent readiness dimensions; none is inferred from another."""

    configured: bool = False
    verified: bool = False
    runtime_available: bool = False
    create_authorized: bool = False
    authorization_required: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if not isinstance(getattr(self, name), bool):
                raise ManifestValidationError(f"readiness.{name} must be bool")

    @classmethod
    def from_value(cls, value: "ReadinessState | Mapping[str, Any] | None") -> "ReadinessState":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            values: dict[str, bool] = {}
            for name in cls.__dataclass_fields__:
                if name in value:
                    if not isinstance(value[name], bool):
                        raise ManifestValidationError(f"readiness.{name} must be bool")
                    values[name] = value[name]
            # Compatibility spellings used by the audit/schema projection.
            # They are normalized only when the canonical flag is absent, and
            # contradictory declarations fail closed instead of overriding a
            # frozen readiness identity.
            if "verification_state" in value:
                raw_state = value["verification_state"]
                if isinstance(raw_state, bool):
                    alias_verified = raw_state
                else:
                    normalized_state = str(raw_state).strip().upper()
                    if normalized_state in {"VERIFIED", "READY"}:
                        alias_verified = True
                    elif normalized_state in {"NOT_VERIFIED", "UNVERIFIED"}:
                        alias_verified = False
                    else:
                        raise ManifestValidationError("unsupported readiness.verification_state")
                if "verified" in values and values["verified"] is not alias_verified:
                    raise ManifestValidationError("readiness.verified and verification_state conflict")
                values.setdefault("verified", alias_verified)
            if "paid_create_requires_authorization" in value:
                raw_required = value["paid_create_requires_authorization"]
                if not isinstance(raw_required, bool):
                    raise ManifestValidationError("readiness.paid_create_requires_authorization must be bool")
                if "authorization_required" in values and values["authorization_required"] is not raw_required:
                    raise ManifestValidationError(
                        "readiness.authorization_required and paid_create_requires_authorization conflict"
                    )
                values.setdefault("authorization_required", raw_required)
            for canonical, alias in (("runtime_available", "available"), ("create_authorized", "live_authorized")):
                if alias in value:
                    raw_alias = value[alias]
                    if not isinstance(raw_alias, bool):
                        raise ManifestValidationError(f"readiness.{alias} must be bool")
                    if canonical in values and values[canonical] is not raw_alias:
                        raise ManifestValidationError(f"readiness.{canonical} and {alias} conflict")
                    values.setdefault(canonical, raw_alias)
            return cls(**values)
        # Accept the richer runtime readiness projection at the manifest
        # boundary without importing it (which would create a module cycle).
        # Only the five immutable flag fields cross this seam; diagnostic
        # reason/metadata stay runtime-local.
        if all(hasattr(value, name) for name in cls.__dataclass_fields__):
            return cls(
                **{
                    name: getattr(value, name)
                    for name in cls.__dataclass_fields__
                }
            )
        raise ManifestValidationError("readiness must be ReadinessState or mapping")

    def to_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in self.__dataclass_fields__}

    @property
    def available(self) -> bool:
        """Compatibility projection for the runtime-availability flag."""

        return self.runtime_available

    @property
    def live_authorized(self) -> bool:
        return self.create_authorized

    @property
    def verification_state(self) -> str:
        return "VERIFIED" if self.verified else "NOT_VERIFIED"


# Friendly aliases used by callers that prefer the architecture document's
# terminology.
DurationConstraint = DurationSpec
ResolutionConstraint = ResolutionSpec
ReferenceConstraint = ReferenceSpec
ModelReadiness = ReadinessState
ManifestReadiness = ReadinessState
Duration = DurationSpec
Resolution = ResolutionSpec
Reference = ReferenceSpec
Supports = FeatureSupport
Authorization = AuthorizationMetadata


def _alias_text_value(name: str, canonical: Any, alias: Any) -> tuple[str, str]:
    """Normalize two text spellings for an alias conflict check."""

    return _text(name, canonical), _text(name, alias)


def _alias_sequence_value(name: str, value: Any) -> tuple[str, ...]:
    """Normalize a sequence alias using the same strict rules as the field."""

    return _tuple_text(name, value)


@dataclass(frozen=True, slots=True, init=False)
class ModelManifest:
    """Immutable model registration and selection metadata.

    The constructor accepts both the concise V1 spelling (``id``,
    ``capability``, ``protocol``) and the audit spelling (``manifest_id``,
    ``capabilities``, ``protocol_family``).  Internally there is exactly one
    capability and one protocol family; aliases cannot create an ambiguous
    manifest.
    """

    id: str
    display_name: str
    provider_id: str
    capability: CapabilityKind
    protocol: ProtocolFamily
    model_id: str
    deployment_region: str
    endpoint_class: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    supported_modes: tuple[str, ...]
    duration: DurationSpec
    resolution: ResolutionSpec
    reference: ReferenceSpec
    supports: FeatureSupport
    authorization: AuthorizationMetadata
    readiness: ReadinessState
    manifest_version: int
    codec_id: str
    # Provider codec versions are commonly represented as either a semantic
    # string (``"1.2"``) or the integer value used by the manifest YAML
    # schema (``1``).  Keep the original safe scalar shape rather than making
    # a serialized manifest fail solely because of that harmless spelling.
    codec_version: int | str
    endpoint_profile_id: str | None
    credential_reference: str | None
    selection_policy: Mapping[str, Any]
    limits: Mapping[str, Any]
    parameter_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    lifecycle: Mapping[str, Any]
    native_features: tuple[str, ...]
    pricing: Mapping[str, Any]
    metadata: Mapping[str, Any]

    def __init__(
        self,
        id: str | None = None,
        display_name: str | None = None,
        provider_id: str = "",
        capability: CapabilityKind | str | object = _UNSET,
        protocol: ProtocolFamily | str | object = _UNSET,
        model_id: str = "",
        deployment_region: str = "CUSTOM",
        endpoint_class: str = "CUSTOM",
        input_modalities: Sequence[str] = (),
        output_modalities: Sequence[str] = (),
        supported_modes: Sequence[str] = (),
        duration: DurationSpec | Mapping[str, Any] | None = None,
        resolution: ResolutionSpec | Mapping[str, Any] | Sequence[str] | None = None,
        reference: ReferenceSpec | Mapping[str, Any] | None = None,
        supports: FeatureSupport | Mapping[str, Any] | None = None,
        authorization: AuthorizationMetadata | Mapping[str, Any] | None = None,
        readiness: ReadinessState | Mapping[str, Any] | None = None,
        manifest_version: int = 1,
        codec_id: str = "generic.json",
        codec_version: int | str = "1",
        endpoint_profile_id: str | None = None,
        credential_reference: str | None = None,
        selection_policy: Mapping[str, Any] | None = None,
        limits: Mapping[str, Any] | None = None,
        parameter_schema: Mapping[str, Any] | None = None,
        result_schema: Mapping[str, Any] | None = None,
        lifecycle: Mapping[str, Any] | None = None,
        native_features: Sequence[str] = (),
        pricing: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **aliases: Any,
    ) -> None:
        capability_supplied = capability is not _UNSET
        protocol_supplied = protocol is not _UNSET
        if not capability_supplied:
            capability = CapabilityKind.LLM
        if not protocol_supplied:
            protocol = ProtocolFamily.REQUEST_RESPONSE

        # Audit/schema aliases.  When both spellings are present they must
        # describe the same value; silently allowing one to override the
        # other would let a serialized manifest mutate its frozen identity.
        if "manifest_id" in aliases:
            alias_id = aliases.pop("manifest_id")
            if id is not None:
                canonical_id, normalized_alias_id = _alias_text_value("id", id, alias_id)
                if canonical_id != normalized_alias_id:
                    raise ManifestValidationError("id and manifest_id conflict")
            else:
                id = alias_id
        if "label" in aliases:
            alias_label = aliases.pop("label")
            if display_name is not None:
                canonical_label, normalized_alias_label = _alias_text_value(
                    "display_name", display_name, alias_label
                )
                if canonical_label != normalized_alias_label:
                    raise ManifestValidationError("display_name and label conflict")
            else:
                display_name = alias_label
        if "protocol_family" in aliases:
            alias_protocol = aliases.pop("protocol_family")
            try:
                canonical_protocol = ProtocolFamily.coerce(protocol)
                normalized_alias_protocol = ProtocolFamily.coerce(alias_protocol)
            except Exception as exc:
                raise ManifestValidationError("unsupported manifest protocol") from exc
            if protocol_supplied and canonical_protocol is not normalized_alias_protocol:
                raise ManifestValidationError("protocol and protocol_family conflict")
            if not protocol_supplied:
                protocol = alias_protocol
        if "capabilities" in aliases:
            values = aliases.pop("capabilities")
            if isinstance(values, (str, bytes)):
                values = (values,)
            if not isinstance(values, Sequence) or len(values) != 1:
                raise ManifestValidationError("a V1 manifest must declare exactly one capability")
            alias_capability = values[0]
            try:
                canonical_capability = CapabilityKind.coerce(capability)
                normalized_alias_capability = CapabilityKind.coerce(alias_capability)
            except Exception as exc:
                raise ManifestValidationError("unsupported manifest capability") from exc
            if capability_supplied and canonical_capability is not normalized_alias_capability:
                raise ManifestValidationError("capability and capabilities conflict")
            if not capability_supplied:
                capability = alias_capability
        if "supported_input_types" in aliases:
            alias_inputs = _alias_sequence_value("input_modalities", aliases.pop("supported_input_types"))
            canonical_inputs = _alias_sequence_value("input_modalities", input_modalities)
            if canonical_inputs and canonical_inputs != alias_inputs:
                raise ManifestValidationError("input_modalities and supported_input_types conflict")
            if not canonical_inputs:
                input_modalities = alias_inputs
        if "supported_output_types" in aliases:
            alias_outputs = _alias_sequence_value("output_modalities", aliases.pop("supported_output_types"))
            canonical_outputs = _alias_sequence_value("output_modalities", output_modalities)
            if canonical_outputs and canonical_outputs != alias_outputs:
                raise ManifestValidationError("output_modalities and supported_output_types conflict")
            if not canonical_outputs:
                output_modalities = alias_outputs
        if "supported_modes" in aliases:
            supported_modes = aliases.pop("supported_modes")
        if "credential_ref" in aliases:
            alias_credential_reference = aliases.pop("credential_ref")
            if credential_reference is not None:
                canonical_credential, normalized_alias_credential = _alias_text_value(
                    "credential_reference", credential_reference, alias_credential_reference
                )
                if canonical_credential != normalized_alias_credential:
                    raise ManifestValidationError("credential_reference and credential_ref conflict")
            else:
                credential_reference = alias_credential_reference
        if "references" in aliases:
            alias_reference = ReferenceSpec.from_value(aliases.pop("references"))
            if reference is not None:
                canonical_reference = ReferenceSpec.from_value(reference)
                if canonical_reference != alias_reference:
                    raise ManifestValidationError("reference and references conflict")
            else:
                reference = alias_reference
        # Early schema drafts called the non-secret billing/authorization
        # block ``cost_authorization``.  Accept its two lifecycle flags as a
        # compatibility spelling, while keeping pricing informational.
        cost_authorization = aliases.pop("cost_authorization", None)
        if cost_authorization is not None:
            if not isinstance(cost_authorization, Mapping):
                raise ManifestValidationError("cost_authorization must be a mapping")
            for canonical_name, alias_name in (
                ("create_is_paid", "paid_create"),
                ("requires_create_authorization", "paid_create_requires_authorization"),
                ("requires_create_authorization", "authorization_required"),
                ("authorization_required", "paid_create_requires_authorization"),
            ):
                if (
                    canonical_name in cost_authorization
                    and alias_name in cost_authorization
                    and cost_authorization[canonical_name] != cost_authorization[alias_name]
                ):
                    raise ManifestValidationError(
                        f"cost_authorization.{canonical_name} and {alias_name} conflict"
                    )
            cost_auth = AuthorizationMetadata.from_value(
                {
                    "create_is_paid": cost_authorization.get(
                        "create_is_paid",
                        cost_authorization.get("paid_create", cost_authorization.get("billing_class") == "PAID"),
                    ),
                    "requires_create_authorization": cost_authorization.get(
                        "requires_create_authorization",
                        cost_authorization.get(
                            "authorization_required",
                            cost_authorization.get("paid_create_requires_authorization", False),
                        ),
                    ),
                }
            )
            if authorization is None:
                authorization = cost_auth
            elif AuthorizationMetadata.from_value(authorization) != cost_auth:
                raise ManifestValidationError("authorization and cost_authorization conflict")

        def _readiness_mapping(raw: Any) -> dict[str, Any]:
            if raw is None:
                return {}
            if isinstance(raw, Mapping):
                return dict(raw)
            if isinstance(raw, ReadinessState):
                return raw.to_dict()
            if raw is not None and all(hasattr(raw, name) for name in ReadinessState.__dataclass_fields__):
                return {
                    name: getattr(raw, name)
                    for name in ReadinessState.__dataclass_fields__
                }
            raise ManifestValidationError("readiness must be ReadinessState or mapping")

        if "paid_create_requires_authorization" in aliases:
            alias_required = aliases.pop("paid_create_requires_authorization")
            if not isinstance(alias_required, bool):
                raise ManifestValidationError("paid_create_requires_authorization must be bool")
            readiness_values = _readiness_mapping(readiness)
            if (
                "authorization_required" in readiness_values
                and readiness_values["authorization_required"] is not alias_required
            ):
                raise ManifestValidationError(
                    "authorization_required conflicts with paid_create_requires_authorization"
                )
            readiness_values["authorization_required"] = alias_required
            readiness = readiness_values
        # Flat lifecycle spellings are accepted for small integrations, but
        # remain normalized into the nested authorization/readiness records.
        flat_auth: dict[str, Any] = {}
        for alias_name, canonical_name in (
            ("create_is_paid", "create_is_paid"),
            ("paid_create", "create_is_paid"),
            ("requires_create_authorization", "requires_create_authorization"),
        ):
            if alias_name in aliases:
                flat_auth[canonical_name] = aliases.pop(alias_name)
        # ``authorization_required`` is the runtime/readiness projection, not
        # a synonym for the static authorization declaration.  Keeping it out
        # of ``flat_auth`` is important when a paid model is declared without
        # an additional create gate: a serialized manifest may legitimately
        # contain ``authorization.requires_create_authorization=false`` and
        # ``readiness.authorization_required=true`` as independent facts.
        if flat_auth:
            if authorization is None:
                authorization = flat_auth
            elif isinstance(authorization, Mapping):
                merged_auth = dict(authorization)
                for key, value in flat_auth.items():
                    if key in merged_auth and merged_auth[key] != value:
                        raise ManifestValidationError(
                            f"authorization.{key} conflicts with flat manifest field"
                        )
                    merged_auth[key] = value
                authorization = merged_auth
        declared_manifest_hash = aliases.pop("manifest_hash", None)
        if "metadata" in aliases:
            metadata = aliases.pop("metadata")
        if "verification_state" in aliases:
            # A legacy readiness spelling is accepted as a boolean only; an
            # unknown textual state fails closed rather than guessing.
            verification_state = aliases.pop("verification_state")
            if isinstance(verification_state, bool):
                verified_alias = verification_state
            elif str(verification_state).upper() in {"VERIFIED", "READY"}:
                verified_alias = True
            elif str(verification_state).upper() in {"NOT_VERIFIED", "UNVERIFIED"}:
                verified_alias = False
            else:
                raise ManifestValidationError("unsupported verification_state")
            readiness_values = _readiness_mapping(readiness)
            if "verified" in readiness_values and readiness_values["verified"] is not verified_alias:
                raise ManifestValidationError("verified conflicts with verification_state")
            readiness_values["verified"] = verified_alias
            readiness = readiness_values
        # Direct readiness fields are deliberately independent and override a
        # nested readiness mapping when supplied.
        readiness_values = _readiness_mapping(readiness)
        for name in (
            "configured",
            "verified",
            "runtime_available",
            "create_authorized",
            "authorization_required",
        ):
            if name in aliases:
                readiness_values[name] = aliases.pop(name)
        if "available" in aliases:
            available_alias = aliases.pop("available")
            if (
                "runtime_available" in readiness_values
                and readiness_values["runtime_available"] is not available_alias
            ):
                raise ManifestValidationError(
                    "runtime_available and available conflict"
                )
            readiness_values.setdefault("runtime_available", available_alias)
        if "live_authorized" in aliases:
            live_authorized_alias = aliases.pop("live_authorized")
            if (
                "create_authorized" in readiness_values
                and readiness_values["create_authorized"] is not live_authorized_alias
            ):
                raise ManifestValidationError(
                    "create_authorized and live_authorized conflict"
                )
            readiness_values.setdefault("create_authorized", live_authorized_alias)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise ManifestValidationError(f"unexpected ModelManifest fields: {unknown}")

        object.__setattr__(self, "id", _safe_text("id", id if id is not None else ""))
        object.__setattr__(
            self,
            "display_name",
            _safe_text(
                "display_name",
                display_name if display_name is not None else (id if id is not None else ""),
            ),
        )
        object.__setattr__(self, "provider_id", _safe_text("provider_id", provider_id))
        try:
            normalized_capability = CapabilityKind.coerce(capability)
        except Exception as exc:
            raise ManifestValidationError("unsupported manifest capability") from exc
        try:
            normalized_protocol = ProtocolFamily.coerce(protocol)
        except Exception as exc:
            raise ManifestValidationError("unsupported manifest protocol") from exc
        object.__setattr__(self, "capability", normalized_capability)
        object.__setattr__(self, "protocol", normalized_protocol)
        object.__setattr__(self, "model_id", _safe_text("model_id", model_id))
        object.__setattr__(self, "deployment_region", _safe_text("deployment_region", deployment_region))
        object.__setattr__(self, "endpoint_class", _safe_text("endpoint_class", endpoint_class))
        object.__setattr__(
            self,
            "input_modalities",
            tuple(_safe_text("input_modalities", item) for item in _tuple_text("input_modalities", input_modalities)),
        )
        object.__setattr__(
            self,
            "output_modalities",
            tuple(_safe_text("output_modalities", item) for item in _tuple_text("output_modalities", output_modalities)),
        )
        object.__setattr__(
            self,
            "supported_modes",
            tuple(_safe_text("supported_modes", item) for item in _tuple_text("supported_modes", supported_modes)),
        )
        object.__setattr__(self, "duration", DurationSpec.from_value(duration))
        object.__setattr__(self, "resolution", ResolutionSpec.from_value(resolution))
        object.__setattr__(self, "reference", ReferenceSpec.from_value(reference))
        object.__setattr__(self, "supports", FeatureSupport.from_value(supports))
        auth = AuthorizationMetadata.from_value(authorization)
        # Explicit authorization_required is a readiness state, but the model
        # authorization declaration should also be visible in readiness so a
        # caller can render the independent status dimensions consistently.
        if (
            "authorization_required" not in readiness_values
            and "paid_create_requires_authorization" not in readiness_values
        ):
            readiness_values["authorization_required"] = auth.requires_create_authorization
        object.__setattr__(self, "authorization", auth)
        object.__setattr__(self, "readiness", ReadinessState.from_value(readiness_values))
        if isinstance(manifest_version, bool) or not isinstance(manifest_version, int) or manifest_version < 1:
            raise ManifestValidationError("manifest_version must be a positive integer")
        object.__setattr__(self, "manifest_version", manifest_version)
        object.__setattr__(self, "codec_id", _safe_text("codec_id", codec_id))
        if isinstance(codec_version, bool) or not isinstance(codec_version, (int, str)):
            raise ManifestValidationError("codec_version must be a positive integer or text")
        if isinstance(codec_version, int):
            if codec_version < 1:
                raise ManifestValidationError("codec_version must be a positive integer")
        else:
            codec_version = _safe_text("codec_version", codec_version)
        object.__setattr__(self, "codec_version", codec_version)
        if endpoint_profile_id is not None:
            endpoint_profile_id = _safe_text("endpoint_profile_id", endpoint_profile_id)
        object.__setattr__(self, "endpoint_profile_id", endpoint_profile_id)
        if credential_reference is not None:
            credential_reference = _text("credential_reference", credential_reference)
            _reject_unsafe_scalar("credential_reference", credential_reference)
            # References identify a secret slot (for example
            # ``OPENAI_API_KEY`` or ``vault:openai_api_key``), never the
            # secret itself.  The scalar safety check above rejects common
            # token forms such as ``sk-...`` and ``Bearer ...``.
            if not re.fullmatch(
                r"(?:[A-Za-z][A-Za-z0-9_.-]*:)?[A-Za-z][A-Za-z0-9_.:-]{0,127}",
                credential_reference,
            ):
                raise ManifestValidationError("credential_reference must be an identifier, not a credential value")
        object.__setattr__(self, "credential_reference", credential_reference)
        object.__setattr__(self, "selection_policy", _as_mapping(selection_policy, "selection_policy"))
        object.__setattr__(self, "limits", _as_mapping(limits, "limits"))
        object.__setattr__(self, "parameter_schema", _as_mapping(parameter_schema, "parameter_schema"))
        object.__setattr__(self, "result_schema", _as_mapping(result_schema, "result_schema"))
        object.__setattr__(self, "lifecycle", _as_mapping(lifecycle, "lifecycle"))
        object.__setattr__(
            self,
            "native_features",
            tuple(_safe_text("native_features", item) for item in _tuple_text("native_features", native_features)),
        )
        object.__setattr__(self, "pricing", _as_mapping(pricing, "pricing"))
        object.__setattr__(self, "metadata", _as_mapping(metadata, "metadata"))
        for name in (
            "id",
            "display_name",
            "provider_id",
            "model_id",
            "deployment_region",
            "endpoint_class",
            "codec_id",
            "codec_version",
            "endpoint_profile_id",
        ):
            value = getattr(self, name)
            if value is not None:
                # ``codec_version`` may be the integer form accepted above;
                # all other identity fields are text and are checked for
                # credential/path material at this boundary.
                if name == "codec_version" and isinstance(value, int):
                    continue
                _reject_unsafe_scalar(name, value)
        if declared_manifest_hash is not None:
            declared = _text("manifest_hash", declared_manifest_hash)
            if not re.fullmatch(r"[0-9a-fA-F]{64}", declared):
                raise ManifestValidationError("manifest_hash must be a SHA-256 digest")
            if declared.casefold() != self.manifest_hash:
                raise ManifestValidationError("manifest_hash does not match canonical manifest")

    @property
    def manifest_id(self) -> str:
        return self.id

    @property
    def label(self) -> str:
        return self.display_name

    @property
    def provider(self) -> str:
        return self.provider_id

    @property
    def model(self) -> str:
        return self.model_id

    @property
    def endpoint_id(self) -> str | None:
        return self.endpoint_profile_id

    @property
    def canonical_hash(self) -> str:
        return self.manifest_hash

    @property
    def hash(self) -> str:
        return self.manifest_hash

    @property
    def capabilities(self) -> tuple[CapabilityKind, ...]:
        return (self.capability,)

    @property
    def protocol_family(self) -> ProtocolFamily:
        return self.protocol

    @property
    def configured(self) -> bool:
        return self.readiness.configured

    @property
    def verified(self) -> bool:
        return self.readiness.verified

    @property
    def runtime_available(self) -> bool:
        return self.readiness.runtime_available

    @property
    def create_authorized(self) -> bool:
        return self.readiness.create_authorized

    @property
    def authorization_required(self) -> bool:
        return self.readiness.authorization_required or self.authorization.requires_create_authorization

    @property
    def cost_authorization(self) -> AuthorizationMetadata:
        """Compatibility spelling for the non-secret authorization block."""

        return self.authorization

    @property
    def available(self) -> bool:
        return self.runtime_available

    @property
    def verification_state(self) -> str:
        return self.readiness.verification_state

    @property
    def can_create(self) -> bool:
        """Whether a CREATE call is authorized, independent of configuration."""

        return not self.authorization_required or self.create_authorized

    @property
    def manifest_hash(self) -> str:
        encoded = json.dumps(
            self.contract_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def contract_payload(self) -> dict[str, Any]:
        """Stable model contract identity, excluding mutable readiness.

        Credentials, local artifact-sink availability, verification, and paid
        authorization may change between processes without changing what the
        selected model/codec contract means. Frozen RuntimePlans therefore
        hash only the stable manifest contract.
        """

        payload = self.canonical_payload()
        payload.pop("readiness", None)
        return payload

    def canonical_payload(self) -> dict[str, Any]:
        """Canonical, non-secret payload used for identity hashing."""

        return {
            "manifest_version": self.manifest_version,
            "id": self.id,
            "display_name": self.display_name,
            "provider_id": self.provider_id,
            "capability": self.capability.value,
            "protocol": self.protocol.value,
            "model_id": self.model_id,
            "deployment_region": self.deployment_region,
            "endpoint_class": self.endpoint_class,
            "endpoint_profile_id": self.endpoint_profile_id,
            "credential_reference": self.credential_reference,
            "codec_id": self.codec_id,
            "codec_version": self.codec_version,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supported_modes": list(self.supported_modes),
            "duration": self.duration.to_dict(),
            "resolution": self.resolution.to_dict(),
            "reference": self.reference.to_dict(),
            "supports": self.supports.to_dict(),
            "authorization": self.authorization.to_dict(),
            "readiness": self.readiness.to_dict(),
            "selection_policy": thaw(self.selection_policy),
            "limits": thaw(self.limits),
            "parameter_schema": thaw(self.parameter_schema),
            "result_schema": thaw(self.result_schema),
            "lifecycle": thaw(self.lifecycle),
            "native_features": list(self.native_features),
            "pricing": thaw(self.pricing),
            "metadata": thaw(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.canonical_payload()
        value["manifest_hash"] = self.manifest_hash
        # Audit/schema aliases are useful for external readers while the
        # canonical keys above remain stable for hashing.
        value["manifest_id"] = self.id
        value["label"] = self.display_name
        value["capabilities"] = [self.capability.value]
        value["protocol_family"] = self.protocol.value
        value["cost_authorization"] = self.authorization.to_dict()
        value["verification_state"] = self.readiness.verification_state
        value["paid_create_requires_authorization"] = self.readiness.authorization_required
        # Independent readiness dimensions are exposed for diagnostics and
        # Settings consumers but deliberately do not contribute to the stable
        # manifest contract hash.
        value.update(self.readiness.to_dict())
        value["available"] = self.runtime_available
        value["live_authorized"] = self.create_authorized
        return value

    as_dict = to_dict
    public_dict = to_dict

    def serialize(self) -> str:
        """Serialize only safe manifest metadata as canonical JSON."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    canonical_json = serialize
    model_dump = to_dict

    def validate(self) -> "ModelManifest":
        """Validate and return this immutable manifest for fluent callers."""

        # Accessing the canonical payload exercises JSON-safe nested fields;
        # construction already performed structural validation.
        json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelManifest":
        if not isinstance(value, Mapping):
            raise ManifestValidationError("manifest payload must be a mapping")
        return cls(**dict(value))

    @classmethod
    def from_json(cls, value: str) -> "ModelManifest":
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError("manifest JSON is malformed") from exc
        return cls.from_dict(parsed)

    to_json = serialize

    def __hash__(self) -> int:
        return hash(self.manifest_hash)


__all__ = [
    "AuthorizationMetadata",
    "Authorization",
    "Duration",
    "DurationConstraint",
    "DurationSpec",
    "FeatureSupport",
    "ManifestValidationError",
    "ModelManifest",
    "ModelReadiness",
    "ManifestReadiness",
    "ReadinessState",
    "ReferenceConstraint",
    "Reference",
    "ReferenceSpec",
    "ResolutionConstraint",
    "Resolution",
    "ResolutionSpec",
    "Supports",
]
