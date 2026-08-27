"""Capability/model resolution for the universal model runtime.

This module is intentionally small and compatibility-first.  It resolves an
immutable model manifest by capability and an explicit region/selection
policy; it does not know provider wire formats and it never mutates a frozen
runtime plan.  Existing AIDrama providers can be exposed through
``LegacyCapabilityBridge`` while their adapters continue to run unchanged.

The resolver is fail-closed at each boundary:

* unknown capabilities/protocols are rejected;
* a constrained region never falls through to another region;
* explicit provider/model/profile selections never fall back;
* a frozen identity must match exactly (including endpoint/region/hash when
  supplied).

The implementation uses duck typing for manifests and registries so it can
bridge older capability objects as well as the V1 ``ModelManifest`` and
``ManifestRegistry`` contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
import hashlib
import json
import re
from types import MappingProxyType, SimpleNamespace
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from .contracts import CapabilityKind, ProtocolFamily
from .readiness import ModelReadiness, readiness_from_manifest, readiness_from_status


class ModelResolutionError(RuntimeError):
    """Base error for fail-closed model resolution."""


class UnsupportedCapabilityError(ModelResolutionError):
    pass


class UnsupportedProtocolError(ModelResolutionError):
    pass


class RegionResolutionError(ModelResolutionError):
    pass


class FrozenIdentityError(ModelResolutionError):
    pass


class ModelUnavailableError(ModelResolutionError):
    pass


# Short aliases make the boundary pleasant to use from service code.
ResolutionError = ModelResolutionError
ResolverError = ModelResolutionError
RegionMismatchError = RegionResolutionError


class RegionPolicy(StrEnum):
    """Selection policy, deliberately distinct from deployment region."""

    MAINLAND = "MAINLAND"
    INTERNATIONAL = "INTERNATIONAL"
    CUSTOM = "CUSTOM"
    ANY = "ANY"

    @classmethod
    def coerce(cls, value: object | None) -> "RegionPolicy | None":
        if value is None or value == "":
            return None
        if isinstance(value, cls):
            return value
        # Legacy AIDrama region enums are separate ``Enum`` classes; their
        # ``str()`` representation is often ``ProviderRegion.MAINLAND``
        # rather than the portable value.  Read the enum value before
        # normalising so those callers cannot be rejected or misrouted.
        if isinstance(value, Enum):
            value = value.value
        normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "MAINLAND_CHINA": cls.MAINLAND,
            "CHINA": cls.MAINLAND,
            "CN": cls.MAINLAND,
            "INTERNATIONAL": cls.INTERNATIONAL,
            "GLOBAL": cls.INTERNATIONAL,
            "CUSTOM": cls.CUSTOM,
            "ANY": cls.ANY,
            "UNSPECIFIED": cls.ANY,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            raise RegionResolutionError(f"unsupported region policy: {value!r}") from exc


# Existing settings use ProviderPreset; exposing a local alias avoids making
# the new runtime depend on the domain persistence module.
SelectionRegion = RegionPolicy
RegionSelectionPolicy = RegionPolicy


class AvailabilityPolicy(StrEnum):
    ANY = "ANY"
    CONFIGURED = "CONFIGURED"
    RUNTIME_AVAILABLE = "RUNTIME_AVAILABLE"
    READY_FOR_CREATE = "READY_FOR_CREATE"
    # Friendly spellings used by embedding callers.
    AVAILABLE = "RUNTIME_AVAILABLE"
    READY = "READY_FOR_CREATE"


Availability = AvailabilityPolicy


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _coalesce_selector(
    name: str,
    *values: object,
    normalizer: Callable[[object], object] | None = None,
) -> str | None:
    """Coalesce exact selector aliases without allowing identity drift.

    Resolver callers commonly use ``model``, ``explicit_model`` and
    ``model_id`` (or the corresponding profile/region spellings).  These are
    aliases, not ranked preferences.  If two non-empty declarations disagree
    we must reject the request instead of silently choosing whichever keyword
    happened to be inspected first.
    """

    selected: str | None = None
    selected_key: str | None = None
    for value in values:
        if value is None:
            continue
        normalized_value = normalizer(value) if normalizer is not None else value
        if hasattr(normalized_value, "value"):
            normalized_value = getattr(normalized_value, "value")
        normalized = _text(normalized_value)
        if not normalized:
            continue
        if selected is None:
            selected = normalized
            selected_key = normalized.casefold()
            continue
        if normalized.casefold() != selected_key:
            raise ModelResolutionError(
                f"conflicting {name} selectors are not allowed: {selected!r} vs {normalized!r}"
            )
    return selected


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _requires_explicit_selection(manifest: object) -> bool:
    """Read the opt-in policy fail-closed.

    The manifest schema currently permits opaque selection metadata.  An
    absent/explicit ``False`` flag permits default selection; any malformed
    non-boolean value is treated as opt-in so it cannot accidentally make a
    provider the implicit fallback.
    """

    policy = _map(_manifest_value(manifest, "selection_policy", "selection", default={}))
    value = policy.get("requires_explicit_selection")
    return value is not None and value is not False


def _safe_public_mapping(value: object) -> dict[str, object]:
    """Recursively retain only non-secret compatibility metadata."""

    secret_markers = (
        "api_key",
        "apikey",
        "token",
        "secret",
        "credential",
        "access_token",
        "refresh_token",
        "password",
        "private_key",
        "signed_url",
        "raw_body",
    )

    def clean(item: object, key: str | None = None) -> object:
        lowered = key.casefold() if key is not None else ""
        if key is not None and (
            lowered in {"authorization", "authorization_header"}
            or any(lowered == marker or lowered.endswith("_" + marker) for marker in secret_markers)
        ):
            return None
        if isinstance(item, str):
            lowered_value = item.casefold()
            if (
                item.startswith(("sk-", "rk-", "sess-"))
                or "bearer " in lowered_value
                or "-----begin " in lowered_value
                or re.search(
                    r"[?&](?:token|sig|signature|x-amz-signature|access[_-]?key|api[_-]?key|credential|auth|expires)=",
                    lowered_value,
                )
            ):
                return None
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                cleaned = clean(raw_value, str(raw_key))
                if cleaned is not None:
                    result[str(raw_key)] = cleaned
            return result
        if isinstance(item, (tuple, list, set, frozenset)):
            return [clean(child) for child in item]
        return item

    cleaned = clean(value)
    return cleaned if isinstance(cleaned, dict) else {}


def _looks_like_private_compat_path(value: str) -> bool:
    """Return whether a compatibility identity looks like local path data.

    Compatibility manifests are persisted as public selection metadata.  A
    provider may expose a local endpoint/profile path by mistake; retaining it
    would disclose the host filesystem and make the resulting identity
    environment-specific.  Keep URL-shaped endpoint identifiers usable while
    rejecting rooted Windows/POSIX/UNC/home paths and ``file:`` URLs.
    """

    try:
        decoded = unquote(value)
    except Exception:
        decoded = value
    lowered = decoded.casefold()
    return bool(
        re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|[\\/]|~[\\/])", decoded)
        or lowered.startswith("file:")
    )


def _safe_compat_text(
    name: str,
    value: object,
    *,
    default: str | None = None,
) -> str:
    """Normalize a compatibility scalar and reject secret/path material.

    ``CompatibilityManifest`` is the public projection of legacy provider
    status.  Unlike provider codecs, it must never retain a signed URL,
    credential-bearing URL, token, or private path in an identity field.  The
    check is intentionally conservative and raises at the resolver boundary;
    the bridge then skips only the malformed provider row.
    """

    if value is None:
        if default is None:
            raise ModelResolutionError(f"{name} must not be empty")
        value = default
    if not isinstance(value, str):
        raise ModelResolutionError(f"{name} must be text")
    result = value.strip()
    if not result:
        raise ModelResolutionError(f"{name} must not be empty")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelResolutionError(f"{name} must contain valid Unicode text") from exc

    lowered = result.casefold()
    if _looks_like_private_compat_path(result):
        raise ModelResolutionError(f"{name} may not contain a private filesystem path")

    # URL userinfo is a credential even when the password is omitted (for
    # example ``https://user@example.test``).  Treat malformed URL authorities
    # as unsafe rather than serializing an unparseable identity.
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered):
        try:
            parsed = urlsplit(result)
            if parsed.username is not None or parsed.password is not None:
                raise ModelResolutionError(f"{name} may not contain URL credentials")
            query_items = parse_qsl(parsed.query, keep_blank_values=True)
        except ModelResolutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise ModelResolutionError(f"{name} contains an invalid URL") from exc
    else:
        query_items = []

    # Signed URLs and inline credential assignments are not stable model
    # identities.  Check both parsed query keys and the raw value so malformed
    # or scheme-less values fail closed as well.
    sensitive_query_key = re.compile(
        r"(?:^|[_-])(?:token|access[_-]?token|sig|signature|signed|"
        r"x[_-]?amz[_-]?(?:signature|credential|security[_-]?token)|"
        r"credential|auth(?:orization)?|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|password|secret|expires?)$",
        re.IGNORECASE,
    )
    if any(sensitive_query_key.search(unquote(str(key)).strip()) for key, _ in query_items):
        raise ModelResolutionError(f"{name} may not contain signed URL credentials")
    if re.search(
        r"[?&#](?:[^=&#]*(?:token|access[_-]?token|sig|signature|signed|"
        r"x[-_]?amz[-_]?(?:signature|credential|security[-_]?token)|"
        r"credential|auth(?:orization)?|api[-_]?key|access[-_]?key|"
        r"client[-_]?secret|password|secret|expires?)[^=&#]*)=",
        lowered,
    ):
        raise ModelResolutionError(f"{name} may not contain signed URL credentials")

    if (
        re.search(r"(?:^|[\s:=])(?:bearer|basic)\s+[^\s]+", lowered)
        or re.search(r"(?:^|[\s:=])(?:sk|rk|sess)-[A-Za-z0-9._~-]+", result, re.IGNORECASE)
        or re.search(r"(?:api[_-]?key|access[_-]?key|client[_-]?secret)\s*[:=]", lowered)
        or re.search(r"\bAKIA[0-9A-Z]{16}\b", result)
        or re.match(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", result)
        or "-----begin " in lowered
    ):
        raise ModelResolutionError(f"{name} may not contain credential material")

    return result


def _manifest_value(manifest: object, *names: str, default: object = None) -> object:
    for name in names:
        if isinstance(manifest, Mapping) and name in manifest:
            value = manifest[name]
        else:
            value = getattr(manifest, name, None)
        if value is not None:
            return value
    return default


def normalize_capability(value: object) -> CapabilityKind:
    try:
        if isinstance(value, Enum) and not isinstance(value, CapabilityKind):
            value = value.value
        if isinstance(value, str) and not isinstance(value, CapabilityKind):
            value = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        return CapabilityKind.coerce(value)  # type: ignore[arg-type]
    except Exception as exc:
        raise UnsupportedCapabilityError(f"unsupported capability: {value!r}") from exc


def normalize_protocol(value: object) -> ProtocolFamily:
    try:
        if isinstance(value, Enum) and not isinstance(value, ProtocolFamily):
            value = value.value
        if isinstance(value, str) and not isinstance(value, ProtocolFamily):
            value = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        return ProtocolFamily.coerce(value)  # type: ignore[arg-type]
    except Exception as exc:
        raise UnsupportedProtocolError(f"unsupported protocol family: {value!r}") from exc


def manifest_id(manifest: object) -> str:
    return _text(_manifest_value(manifest, "manifest_id", "id", default=""))


def manifest_provider_id(manifest: object) -> str:
    return _text(_manifest_value(manifest, "provider_id", "provider", default=""))


def manifest_model_id(manifest: object) -> str:
    return _text(_manifest_value(manifest, "model_id", "model", default=""))


def manifest_endpoint_id(manifest: object) -> str:
    return _text(
        _manifest_value(
            manifest,
            "endpoint_profile_id",
            "profile_id",
            "endpoint_id",
            default="",
        )
    )


def manifest_region(manifest: object) -> str:
    value = _manifest_value(
        manifest,
        "deployment_region",
        "region",
        "deployment",
        default="UNSPECIFIED",
    )
    return _normalize_region_value(value)


def manifest_endpoint_class(manifest: object) -> str:
    return _text(_manifest_value(manifest, "endpoint_class", default="UNSPECIFIED"), "UNSPECIFIED")


def manifest_hash(manifest: object) -> str:
    value = _manifest_value(manifest, "manifest_hash", "hash", default="")
    if callable(value):
        try:
            value = value()
        except TypeError:
            value = ""
    if value:
        return _text(value)
    # A manifest implementation may expose only a canonical serialisation.
    for name in ("canonical_hash", "compute_hash", "fingerprint"):
        candidate = getattr(manifest, name, None)
        if callable(candidate):
            try:
                result = candidate()
            except TypeError:
                continue
            if result:
                return _text(result)
    payload = _manifest_public_dict(manifest)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def manifest_capabilities(manifest: object) -> tuple[CapabilityKind, ...]:
    raw = _manifest_value(manifest, "capabilities", "capability", default=())
    if isinstance(raw, (str, bytes)):
        raw = (raw,)
    try:
        values = tuple(raw)
    except TypeError:
        values = ()
    result: list[CapabilityKind] = []
    for value in values:
        try:
            item = normalize_capability(value)
        except UnsupportedCapabilityError as exc:
            # A manifest with one unknown capability is malformed, even when
            # it also happens to list a known capability.  Silently dropping
            # the unknown value would make a typo look like a valid fallback.
            raise UnsupportedCapabilityError(
                f"manifest declares unsupported capability: {value!r}"
            ) from exc
        if item not in result:
            result.append(item)
    return tuple(result)


def manifest_protocol(manifest: object) -> ProtocolFamily:
    raw = _manifest_value(manifest, "protocol", "protocol_family", default=None)
    return normalize_protocol(raw)


def _manifest_public_dict(manifest: object) -> dict[str, object]:
    """Best-effort non-secret manifest projection for identity hashing."""

    names = (
        "manifest_version",
        "manifest_id",
        "id",
        "provider_id",
        "model_id",
        "capability",
        "capabilities",
        "protocol",
        "protocol_family",
        "deployment_region",
        "endpoint_profile_id",
        "endpoint_class",
        "codec_id",
        "codec_version",
        "selection_policy",
        "authorization",
        "readiness",
    )
    def json_safe(value: object) -> object:
        if hasattr(value, "value"):
            return json_safe(value.value)
        if isinstance(value, Mapping):
            return {
                str(key): json_safe(child)
                for key, child in value.items()
                if not any(
                    marker in str(key).casefold()
                    for marker in ("api_key", "secret", "token", "password", "cookie")
                )
            }
        if isinstance(value, (tuple, list, set, frozenset)):
            return [json_safe(item) for item in value]
        return value

    result: dict[str, object] = {}
    for name in names:
        value = _manifest_value(manifest, name, default=None)
        if value is None:
            continue
        result[name] = json_safe(value)
    return result


@dataclass(frozen=True, slots=True)
class FrozenModelIdentity:
    """The identity that a RuntimePlan pins for one model selection."""

    manifest_id: str = ""
    manifest_hash: str = ""
    provider_id: str = ""
    model_id: str = ""
    endpoint_profile_id: str = ""
    endpoint_class: str = ""
    deployment_region: str = ""
    capability: str = ""
    protocol: str = ""

    @classmethod
    def from_value(cls, value: object) -> "FrozenModelIdentity":
        if isinstance(value, cls):
            return value
        source = value if isinstance(value, Mapping) else value
        def get(*names: str) -> str:
            for name in names:
                if isinstance(source, Mapping):
                    raw = source.get(name)
                else:
                    raw = getattr(source, name, None)
                if raw is not None and str(raw).strip():
                    return _text(getattr(raw, "value", raw))
            return ""
        capability = get("capability", "provider_capability")
        protocol = get("protocol", "protocol_family")
        return cls(
            # ``profile_id`` identifies an endpoint/profile, not the
            # immutable manifest.  Keeping those dimensions separate avoids
            # rejecting a valid frozen profile supplied by older callers.
            manifest_id=get("manifest_id", "manifest"),
            manifest_hash=get("manifest_hash"),
            provider_id=get("provider_id", "provider"),
            model_id=get("model_id", "model"),
            endpoint_profile_id=get("endpoint_profile_id", "endpoint_id", "profile_id"),
            endpoint_class=get("endpoint_class"),
            deployment_region=get("deployment_region", "region"),
            capability=capability,
            protocol=protocol,
        )

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.manifest_id,
                self.manifest_hash,
                self.provider_id,
                self.model_id,
                self.endpoint_profile_id,
                self.endpoint_class,
                self.deployment_region,
                self.capability,
                self.protocol,
            )
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "endpoint_profile_id": self.endpoint_profile_id,
            "endpoint_class": self.endpoint_class,
            "deployment_region": self.deployment_region,
            "capability": self.capability,
            "protocol": self.protocol,
        }


@dataclass(frozen=True, slots=True)
class ResolverRequest:
    """Inputs accepted by :class:`ModelResolver`.

    ``model_id``/``manifest_id``/``provider_id`` are exact selectors.  A
    supplied selector is never treated as a preference and never falls back
    to a different provider.
    """

    capability: CapabilityKind | str
    model_id: str | None = None
    manifest_id: str | None = None
    provider_id: str | None = None
    endpoint_profile_id: str | None = None
    # Compatibility aliases for callers that use the shorter profile/model
    # terminology.  They are normalised to the canonical fields below.
    profile_id: str | None = None
    explicit_model: str | None = None
    model: str | None = None
    region_policy: RegionPolicy | str | None = None
    deployment_region: str | None = None
    region: str | None = None
    custom_region: str | None = None
    protocol: ProtocolFamily | str | None = None
    require_available: bool = False
    availability: AvailabilityPolicy | str | Callable[[object, ModelReadiness], bool] | None = None
    cost_preference: str | None = None
    quality_preference: str | None = None
    authorization: object | None = None
    frozen_identity: FrozenModelIdentity | Mapping[str, object] | object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", normalize_capability(self.capability))
        model_selector = _coalesce_selector(
            "model", self.model_id, self.explicit_model, self.model
        )
        object.__setattr__(self, "model_id", model_selector)
        profile_selector = _coalesce_selector(
            "endpoint profile",
            self.endpoint_profile_id,
            self.profile_id,
        )
        object.__setattr__(self, "endpoint_profile_id", profile_selector)
        deployment_selector = _coalesce_selector(
            "deployment region",
            self.deployment_region,
            self.region,
            normalizer=_normalize_region_value,
        )
        object.__setattr__(self, "deployment_region", deployment_selector)
        if self.custom_region and deployment_selector:
            custom = _normalize_region_value(self.custom_region)
            if custom != _normalize_region_value(deployment_selector):
                raise RegionResolutionError(
                    "deployment_region and custom_region selectors conflict"
                )
        if self.protocol is not None:
            object.__setattr__(self, "protocol", normalize_protocol(self.protocol))
        if self.region_policy is not None:
            object.__setattr__(self, "region_policy", RegionPolicy.coerce(self.region_policy))

    @property
    def explicit(self) -> bool:
        return any(
            _text(value)
            for value in (
                self.model_id,
                self.manifest_id,
                self.provider_id,
                self.endpoint_profile_id,
            )
        )


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Immutable resolver output used to construct a capability request."""

    manifest: object
    readiness: ModelReadiness
    capability: CapabilityKind
    protocol: ProtocolFamily
    provider_id: str
    model_id: str
    manifest_id: str
    manifest_hash: str
    endpoint_profile_id: str
    endpoint_class: str
    deployment_region: str

    @property
    def runtime_available(self) -> bool:
        return self.readiness.runtime_available

    @property
    def available(self) -> bool:
        return self.runtime_available

    @property
    def configured(self) -> bool:
        return self.readiness.configured

    @property
    def verified(self) -> bool:
        return self.readiness.verified

    @property
    def create_authorized(self) -> bool:
        return self.readiness.create_authorized

    @property
    def authorization_required(self) -> bool:
        return self.readiness.authorization_required

    @property
    def verification_state(self) -> str:
        return self.readiness.verification_state

    @property
    def identity(self) -> FrozenModelIdentity:
        return FrozenModelIdentity(
            manifest_id=self.manifest_id,
            manifest_hash=self.manifest_hash,
            provider_id=self.provider_id,
            model_id=self.model_id,
            endpoint_profile_id=self.endpoint_profile_id,
            endpoint_class=self.endpoint_class,
            deployment_region=self.deployment_region,
            capability=self.capability.value,
            protocol=self.protocol.value,
        )

    def freeze(self) -> FrozenModelIdentity:
        return self.identity

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity.as_dict(),
            "readiness": self.readiness.as_dict(),
        }

    public_dict = as_dict


ModelSelection = ResolvedModel
ResolvedSelection = ResolvedModel
ResolvedModelSelection = ResolvedModel


class InMemoryManifestRegistry:
    """Tiny registry useful for tests and embedded hosts.

    The production registry supplied by ``manifest.py`` can be passed to the
    resolver directly; this class intentionally mirrors its common methods.
    """

    def __init__(self, manifests: Iterable[object] = ()) -> None:
        self._manifests: list[object] = []
        for item in manifests:
            self.register(item)

    def register(self, manifest: object) -> object:
        mid = manifest_id(manifest)
        if not mid:
            raise ModelResolutionError("manifest must have a non-empty manifest_id")
        if any(manifest_id(item) == mid for item in self._manifests):
            raise ModelResolutionError(f"duplicate manifest_id: {mid}")
        # Validate at registration, so malformed capability/protocol values
        # fail closed before a product request is attempted.
        if not manifest_capabilities(manifest):
            raise UnsupportedCapabilityError(f"manifest has no supported capability: {mid}")
        manifest_protocol(manifest)
        self._manifests.append(manifest)
        return manifest

    add = register

    def list(self, capability: CapabilityKind | str | None = None) -> tuple[object, ...]:
        if capability is None:
            return tuple(self._manifests)
        kind = normalize_capability(capability)
        return tuple(item for item in self._manifests if kind in manifest_capabilities(item))

    all = list
    manifests = list

    def get(self, key: str) -> object | None:
        value = _text(key)
        return next(
            (
                item
                for item in self._manifests
                if value in {manifest_id(item), manifest_endpoint_id(item)}
            ),
            None,
        )


ManifestRegistry = InMemoryManifestRegistry
ResolverRegistry = InMemoryManifestRegistry
ModelManifestRegistry = InMemoryManifestRegistry


class ModelResolver:
    """Resolve manifests by capability, policy, and immutable identity."""

    def __init__(
        self,
        registry: object | None = None,
        *,
        readiness_resolver: Callable[[object], object] | Mapping[object, object] | None = None,
        legacy_bridge: "LegacyCapabilityBridge | None" = None,
    ) -> None:
        if registry is None and legacy_bridge is not None:
            registry = legacy_bridge.manifest_registry()
        # A custom registry may deliberately implement ``__bool__``/``__len__``
        # (for example while it is lazily loading manifests).  Presence of a
        # registry object, rather than its truthiness, determines whether the
        # resolver should use it.
        self.registry = registry if registry is not None else InMemoryManifestRegistry()
        self.readiness_resolver = readiness_resolver
        self.legacy_bridge = legacy_bridge

    def manifests(self, capability: CapabilityKind | str | None = None) -> tuple[object, ...]:
        return self._registry_items(capability)

    def resolve(
        self,
        request: ResolverRequest | None = None,
        *,
        capability: CapabilityKind | str | None = None,
        model_id: str | None = None,
        explicit_model: str | None = None,
        model: str | None = None,
        manifest_id: str | None = None,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
        profile_id: str | None = None,
        profile: str | None = None,
        region_policy: RegionPolicy | str | None = None,
        deployment_region: str | None = None,
        region: str | None = None,
        custom_region: str | None = None,
        protocol: ProtocolFamily | str | None = None,
        require_available: bool = False,
        availability: AvailabilityPolicy | str | Callable[[object, ModelReadiness], bool] | None = None,
        cost_preference: str | None = None,
        quality_preference: str | None = None,
        authorization: object | None = None,
        frozen_identity: FrozenModelIdentity | Mapping[str, object] | object | None = None,
        frozen: FrozenModelIdentity | Mapping[str, object] | object | None = None,
    ) -> ResolvedModel:
        if request is not None:
            # A concise positional ``resolve("IMAGE", ...)`` call is a
            # useful compatibility spelling.  Treat only a capability enum
            # or known capability string this way; malformed values still
            # fail through the canonical unsupported-capability path below.
            if not isinstance(request, ResolverRequest) and capability is None:
                try:
                    capability = normalize_capability(request)
                except UnsupportedCapabilityError:
                    raise ModelResolutionError("request must be ResolverRequest")
                request = None
        if request is not None:
            if not isinstance(request, ResolverRequest):
                raise ModelResolutionError("request must be ResolverRequest")
            # Explicit kwargs may refine a request, but never silently replace
            # its identity.  Selector aliases are exact declarations; a
            # contradictory override fails closed instead of changing a
            # frozen RuntimePlan target by keyword precedence.
            if capability is not None and normalize_capability(capability) is not request.capability:
                raise UnsupportedCapabilityError(
                    "request capability and override conflict"
                )
            selected_model = _coalesce_selector(
                "model",
                request.model_id,
                request.explicit_model,
                request.model,
                model_id,
                explicit_model,
                model,
            )
            selected_manifest = _coalesce_selector(
                "manifest",
                request.manifest_id,
                manifest_id,
            )
            selected_provider = _coalesce_selector(
                "provider",
                request.provider_id,
                provider_id,
            )
            selected_profile = _coalesce_selector(
                "endpoint profile",
                request.endpoint_profile_id,
                request.profile_id,
                endpoint_profile_id,
                profile_id,
                profile,
            )
            selected_region = _coalesce_selector(
                "deployment region",
                request.deployment_region,
                request.region,
                deployment_region,
                region,
                normalizer=_normalize_region_value,
            )
            selected_custom_region = _coalesce_selector(
                "custom region",
                request.custom_region,
                custom_region,
                normalizer=_normalize_region_value,
            )
            if selected_region and selected_custom_region:
                if _normalize_region_value(selected_region) != _normalize_region_value(selected_custom_region):
                    raise RegionResolutionError(
                        "deployment_region and custom_region selectors conflict"
                    )
            selected_protocol = request.protocol
            if protocol is not None:
                override_protocol = normalize_protocol(protocol)
                if selected_protocol is not None and normalize_protocol(selected_protocol) is not override_protocol:
                    raise UnsupportedProtocolError(
                        "request protocol and override conflict"
                    )
                selected_protocol = override_protocol
            selected_region_policy = request.region_policy
            if region_policy is not None:
                override_policy = RegionPolicy.coerce(region_policy)
                if (
                    selected_region_policy is not None
                    and RegionPolicy.coerce(selected_region_policy) is not override_policy
                ):
                    raise RegionResolutionError(
                        "request region policy and override conflict"
                    )
                selected_region_policy = override_policy
            selected_frozen = request.frozen_identity
            supplied_frozen = frozen_identity if frozen_identity is not None else frozen
            if selected_frozen is not None and supplied_frozen is not None:
                if FrozenModelIdentity.from_value(selected_frozen) != FrozenModelIdentity.from_value(supplied_frozen):
                    raise FrozenIdentityError(
                        "request frozen identity and override conflict"
                    )
            elif supplied_frozen is not None:
                selected_frozen = supplied_frozen

            # Build one normalized request for matching.  Alias fields are
            # intentionally cleared after coalescing so they cannot be
            # reconsidered with a different precedence on a later call.
            values = {
                "capability": request.capability,
                "model_id": selected_model,
                "manifest_id": selected_manifest,
                "provider_id": selected_provider,
                "endpoint_profile_id": selected_profile,
                "region_policy": selected_region_policy,
                "deployment_region": selected_region,
                "custom_region": selected_custom_region,
                "protocol": selected_protocol,
                "require_available": request.require_available or require_available,
                "availability": request.availability if availability is None else availability,
                "cost_preference": request.cost_preference if cost_preference is None else cost_preference,
                "quality_preference": request.quality_preference if quality_preference is None else quality_preference,
                "authorization": request.authorization if authorization is None else authorization,
                "frozen_identity": selected_frozen,
            }
            request = ResolverRequest(**values)
        else:
            if capability is None:
                raise UnsupportedCapabilityError("capability is required")
            selected_model = _coalesce_selector(
                "model", model_id, explicit_model, model
            )
            selected_profile = _coalesce_selector(
                "endpoint profile", endpoint_profile_id, profile_id, profile
            )
            selected_region = _coalesce_selector(
                "deployment region",
                deployment_region,
                region,
                normalizer=_normalize_region_value,
            )
            selected_frozen = frozen_identity
            if selected_frozen is not None and frozen is not None:
                if FrozenModelIdentity.from_value(selected_frozen) != FrozenModelIdentity.from_value(frozen):
                    raise FrozenIdentityError(
                        "frozen_identity and frozen selectors conflict"
                    )
            elif selected_frozen is None:
                selected_frozen = frozen
            request = ResolverRequest(
                capability=capability,
                model_id=selected_model,
                manifest_id=manifest_id,
                provider_id=provider_id,
                endpoint_profile_id=selected_profile,
                region_policy=region_policy,
                deployment_region=selected_region,
                custom_region=custom_region,
                protocol=protocol,
                require_available=require_available,
                availability=availability,
                cost_preference=cost_preference,
                quality_preference=quality_preference,
                authorization=authorization,
                frozen_identity=selected_frozen,
            )

        candidates = list(self._registry_items(request.capability))
        # Do not trust a duck-typed registry to honour its capability filter.
        # Validate every returned manifest and explicitly retain only the
        # requested capability.  A malformed capability/protocol fails the
        # whole boundary rather than being skipped in favour of a fallback.
        capability_candidates: list[object] = []
        for item in candidates:
            if request.capability in manifest_capabilities(item):
                capability_candidates.append(item)
            try:
                manifest_protocol(item)
            except UnsupportedProtocolError:
                raise
        candidates = capability_candidates
        if not candidates:
            raise UnsupportedCapabilityError(
                f"no model manifests support capability {request.capability.value}"
            )

        candidates = [item for item in candidates if self._matches_identity(item, request)]
        if not candidates:
            raise ModelResolutionError("explicit model/provider/profile selection is unavailable; no fallback")

        # Never silently pick a deployment from another region.  Callers may
        # explicitly opt into ``ANY``; otherwise a multi-region implicit
        # lookup must state MAINLAND/INTERNATIONAL/CUSTOM.  An explicit model
        # or provider selector does not by itself identify a deployment: the
        # same model commonly has mainland and international manifests.  Only
        # an exact region selector (or an explicit ANY policy) may disambiguate
        # that set without another policy input.
        normalized_region_policy = RegionPolicy.coerce(request.region_policy)
        if (
            not _text(request.deployment_region)
            and (
                normalized_region_policy is None
                or (
                    normalized_region_policy is RegionPolicy.CUSTOM
                    and not request.custom_region
                )
            )
        ):
            # An implicit lookup may proceed only when every candidate is in
            # the same deployment region.  ``UNSPECIFIED`` is itself an
            # unknown region, so mixing it with a known region must not turn
            # into an accidental cross-region fallback.  An all-unknown
            # legacy inventory remains selectable for compatibility; callers
            # can still require an exact region via ``deployment_region`` or
            # an explicit model/profile selection.
            candidate_regions = {manifest_region(item) for item in candidates}
            if len(candidate_regions) > 1:
                raise RegionResolutionError(
                    "region policy is required when models span multiple regions; cross-region fallback is disabled"
                )

        candidates = [item for item in candidates if self._matches_region(item, request)]
        if not candidates:
            policy = request.region_policy or request.deployment_region or "requested"
            raise RegionResolutionError(
                f"no model matches region policy {policy!r}; cross-region fallback is disabled"
            )

        if request.protocol is not None:
            candidates = [item for item in candidates if manifest_protocol(item) is request.protocol]
            if not candidates:
                raise UnsupportedProtocolError("requested protocol is not supported by the selected model")

        # Providers marked as explicit-only are deliberately opt-in.  Their
        # manifests remain visible for explicit selection but can never win a
        # legacy/default capability lookup.
        if not request.explicit:
            implicit_candidates = [
                item
                for item in candidates
                if not _requires_explicit_selection(item)
            ]
            if not implicit_candidates:
                raise ModelResolutionError(
                    "all matching models require explicit selection; no implicit fallback"
                )
            candidates = implicit_candidates

        scored: list[tuple[tuple[object, ...], object, ModelReadiness]] = []
        for item in candidates:
            readiness = self._readiness(
                item,
                request.authorization,
                capability=request.capability,
            )
            if not self._availability_matches(item, readiness, request.availability, request.require_available):
                continue
            scored.append((self._score(item, readiness, request), item, readiness))
        if not scored:
            raise ModelUnavailableError("model is configured but not runtime available under the requested policy")
        scored.sort(key=lambda entry: entry[0])
        _, selected, readiness = scored[0]
        result = self._resolved(selected, readiness, capability=request.capability)

        frozen_value = request.frozen_identity
        if frozen_value is not None:
            self.assert_frozen(result, frozen_value)
        return result

    select = resolve
    resolve_model = resolve

    def assert_frozen(
        self,
        selection: ResolvedModel,
        frozen: FrozenModelIdentity | Mapping[str, object] | object,
    ) -> None:
        identity = FrozenModelIdentity.from_value(frozen)
        if identity.is_empty:
            raise FrozenIdentityError("frozen model identity is empty or malformed")
        actual = selection.identity
        mismatches: list[str] = []
        for name in (
            "manifest_id",
            "manifest_hash",
            "provider_id",
            "model_id",
            "endpoint_profile_id",
            "endpoint_class",
            "deployment_region",
            "capability",
            "protocol",
        ):
            expected = getattr(identity, name)
            if not expected or (
                name in {"endpoint_profile_id", "endpoint_class", "deployment_region"}
                and expected.upper() in {"UNSPECIFIED", "LEGACY"}
            ):
                continue
            observed = getattr(actual, name)
            # RuntimePlan stores enum values as strings and may use a legacy
            # region spelling; normalize only those representations.
            if name == "deployment_region":
                expected = _normalize_region_value(expected)
                observed = _normalize_region_value(observed)
            if name == "capability":
                try:
                    expected = normalize_capability(expected).value
                except UnsupportedCapabilityError:
                    pass
            if name == "protocol":
                try:
                    expected = normalize_protocol(expected).value
                except UnsupportedProtocolError:
                    pass
            if expected != observed:
                mismatches.append(f"{name}: expected {expected!r}, got {observed!r}")
        if mismatches:
            raise FrozenIdentityError("frozen model identity mismatch; selection cannot mutate: " + "; ".join(mismatches))

    def readiness(self, selection_or_manifest: object, *, authorization: object | None = None) -> ModelReadiness:
        manifest = getattr(selection_or_manifest, "manifest", selection_or_manifest)
        if isinstance(selection_or_manifest, ResolvedModel) and authorization is None:
            return selection_or_manifest.readiness
        requested_capability = (
            selection_or_manifest.capability
            if isinstance(selection_or_manifest, ResolvedModel)
            else None
        )
        return self._readiness(
            manifest,
            authorization,
            capability=requested_capability,
        )

    def provider_for(self, selection: ResolvedModel) -> object:
        """Return the exact legacy provider for a bridged selection.

        This method is deliberately strict: it does not call a registry's
        generic ``get`` because that method may fall back to a preferred
        provider.
        """

        if self.legacy_bridge is None:
            raise ModelResolutionError("no compatibility provider bridge is configured")
        return self.legacy_bridge.provider_for(selection)

    # ---- registry/readiness internals ---------------------------------

    def _registry_items(self, capability: CapabilityKind | str | None) -> tuple[object, ...]:
        registry = self.registry
        if isinstance(registry, Mapping):
            requested = normalize_capability(capability) if capability is not None else None
            values: list[object] = []
            for key, item in registry.items():
                # A mapping registry may be keyed by capability, manifest ID,
                # provider ID, or another host-defined identity.  Only filter
                # by the key when it is recognisably a canonical capability;
                # otherwise retain the value and let manifest capability
                # validation below decide.  This avoids treating a manifest
                # ID such as ``openai:gpt:v1`` as an unsupported capability.
                key_capability: CapabilityKind | None = None
                try:
                    key_capability = normalize_capability(key)
                except UnsupportedCapabilityError:
                    pass
                if requested is not None and key_capability is not None and key_capability is not requested:
                    continue
                if isinstance(item, (list, tuple, set, frozenset)):
                    values.extend(item)
                else:
                    values.append(item)
            return tuple(values)
        for method_name in ("list", "all", "manifests", "items"):
            method = getattr(registry, method_name, None)
            if not callable(method):
                continue
            try:
                value = method(capability) if capability is not None else method()
            except TypeError:
                try:
                    value = method()
                except TypeError:
                    continue
            if isinstance(value, Mapping):
                value = tuple(value.values())
            try:
                return tuple(value)
            except TypeError:
                return (value,)
        try:
            values = tuple(registry)
        except TypeError:
            values = ()
        if capability is None:
            return values
        kind = normalize_capability(capability)
        return tuple(item for item in values if kind in manifest_capabilities(item))

    @staticmethod
    def _matches_identity(manifest: object, request: ResolverRequest) -> bool:
        checks = (
            (request.manifest_id, manifest_id(manifest)),
            (request.model_id, manifest_model_id(manifest)),
            (request.provider_id, manifest_provider_id(manifest)),
            (request.endpoint_profile_id, manifest_endpoint_id(manifest)),
        )
        return all(not expected or _text(expected).casefold() == observed.casefold() for expected, observed in checks)

    @staticmethod
    def _matches_region(manifest: object, request: ResolverRequest) -> bool:
        region = manifest_region(manifest)
        if _text(request.deployment_region):
            return region == _normalize_region_value(request.deployment_region)
        policy = RegionPolicy.coerce(request.region_policy)
        if policy is None or policy is RegionPolicy.ANY:
            return True
        if policy is RegionPolicy.MAINLAND:
            return region == "MAINLAND_CHINA"
        if policy is RegionPolicy.INTERNATIONAL:
            return region == "INTERNATIONAL"
        # CUSTOM is intentionally exact.  A custom policy without an exact
        # region is ambiguous for a default lookup.  An explicit profile/model
        # is already an exact selection and may retain its declared region.
        custom = request.custom_region
        if not custom:
            return request.explicit
        return region == _normalize_region_value(custom)

    def _readiness(
        self,
        manifest: object,
        authorization: object | None,
        *,
        capability: CapabilityKind | None = None,
    ) -> ModelReadiness:
        resolver = self.readiness_resolver
        status: object | None = None
        if callable(resolver):
            status = resolver(manifest)
        elif isinstance(resolver, Mapping):
            keys = (
                manifest_id(manifest),
                (manifest_provider_id(manifest), manifest_model_id(manifest)),
                manifest_provider_id(manifest),
            )
            for key in keys:
                if key in resolver:
                    status = resolver[key]
                    break
        # A compatibility bridge has richer provider status than a static
        # manifest and should be queried before manifest defaults.
        if status is None and self.legacy_bridge is not None:
            status = self.legacy_bridge.status_for(manifest)
        if status is not None:
            self._assert_status_identity(manifest, status, capability=capability)
            return readiness_from_status(status, manifest=manifest, authorization=authorization)
        return readiness_from_manifest(manifest, authorization=authorization)

    @staticmethod
    def _assert_status_identity(
        manifest: object,
        status: object,
        *,
        capability: CapabilityKind | None = None,
    ) -> None:
        """Reject a runtime status belonging to another endpoint/region.

        A stale status must not make a manifest appear available through a
        different deployment.  Missing metadata remains compatible with old
        providers; an explicitly supplied mismatch fails closed.
        """

        metadata = _map(
            status.get("metadata") if isinstance(status, Mapping) else getattr(status, "metadata", None)
        )
        manifest_capability_values = manifest_capabilities(manifest)
        checks = (
            (
                "provider_id",
                manifest_provider_id(manifest),
                metadata.get("provider_id", metadata.get("provider")),
            ),
            ("model", manifest_model_id(manifest), metadata.get("model", metadata.get("model_id"))),
            (
                "endpoint_profile_id",
                manifest_endpoint_id(manifest),
                metadata.get("endpoint_profile_id", metadata.get("endpoint_id")),
            ),
            ("endpoint_class", manifest_endpoint_class(manifest), metadata.get("endpoint_class")),
            ("deployment_region", manifest_region(manifest), metadata.get("deployment_region")),
            ("protocol", manifest_protocol(manifest).value, metadata.get("protocol", metadata.get("protocol_family"))),
        )
        for name, expected, observed in checks:
            if observed is None or str(observed).upper() in {"", "UNSPECIFIED", "LEGACY"} or expected in ("", "UNSPECIFIED"):
                continue
            normalized_expected = (
                _normalize_region_value(expected)
                if name == "deployment_region"
                else (
                    normalize_capability(expected).value
                    if name == "capability"
                    else (
                        normalize_protocol(expected).value
                        if name == "protocol"
                        else _text(expected)
                    )
                )
            )
            normalized_observed = (
                _normalize_region_value(observed)
                if name == "deployment_region"
                else (
                    normalize_capability(observed).value
                    if name == "capability"
                    else (
                        normalize_protocol(observed).value
                        if name == "protocol"
                        else _text(observed)
                    )
                )
            )
            if normalized_expected != normalized_observed:
                raise RegionResolutionError(
                    f"runtime status {name} does not match frozen manifest; cross-region/provider fallback is disabled"
                )
        observed_capability = metadata.get("capability")
        if observed_capability is not None and str(observed_capability).upper() not in {
            "",
            "UNSPECIFIED",
            "LEGACY",
        }:
            try:
                normalized_observed_capability = normalize_capability(observed_capability)
            except UnsupportedCapabilityError as exc:
                raise RegionResolutionError(
                    "runtime status capability is unsupported; cross-provider fallback is disabled"
                ) from exc
            expected_capabilities = (
                (capability,)
                if capability is not None
                else manifest_capability_values
            )
            if normalized_observed_capability not in expected_capabilities:
                raise RegionResolutionError(
                    "runtime status capability does not match frozen manifest; "
                    "cross-provider fallback is disabled"
                )

    @staticmethod
    def _availability_matches(
        manifest: object,
        readiness: ModelReadiness,
        policy: AvailabilityPolicy | str | Callable[[object, ModelReadiness], bool] | None,
        require_available: bool,
    ) -> bool:
        if callable(policy):
            try:
                try:
                    return bool(policy(manifest, readiness))
                except TypeError:
                    # A one-argument predicate is a convenient compatibility
                    # spelling for callers that only inspect the manifest.
                    return bool(policy(manifest))
            except Exception as exc:
                raise ModelUnavailableError("availability policy failed closed") from exc
        if isinstance(policy, bool):
            policy = (
                AvailabilityPolicy.RUNTIME_AVAILABLE
                if policy
                else AvailabilityPolicy.ANY
            )
        if policy is None:
            policy = AvailabilityPolicy.RUNTIME_AVAILABLE if require_available else AvailabilityPolicy.ANY
        try:
            policy_value = AvailabilityPolicy(str(policy).upper())
        except ValueError as exc:
            raise ModelUnavailableError(f"unsupported availability policy: {policy!r}") from exc
        if policy_value is AvailabilityPolicy.ANY:
            return True
        if policy_value is AvailabilityPolicy.CONFIGURED:
            return readiness.configured
        if policy_value is AvailabilityPolicy.RUNTIME_AVAILABLE:
            # ``runtime_available`` is intentionally independent from
            # configuration.  Do not collapse the two dimensions while
            # applying a runtime-availability filter.
            return readiness.runtime_available
        return readiness.ready_for_create

    @staticmethod
    def _score(manifest: object, readiness: ModelReadiness, request: ResolverRequest) -> tuple[object, ...]:
        selection_policy = _map(
            _manifest_value(manifest, "selection_policy", "selection", default={})
        )
        priority = _manifest_value(
            manifest,
            "selection_priority",
            "priority",
            default=selection_policy.get("priority", 100),
        )
        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            priority_value = 100
        # Lower tuple values win.  Explicit selectors already narrowed the
        # set, while preferences only order otherwise equivalent candidates.
        raw_pricing = _manifest_value(manifest, "pricing", "cost", default={})
        pricing = _map(raw_pricing)
        # Pricing is informational metadata.  Preferences can order declared
        # candidates, but they never grant authorization or make an
        # unavailable model usable.
        cost = _text(
            _manifest_value(manifest, "cost", default=None)
            or pricing.get(
                "cost",
                pricing.get(
                    "tier",
                    pricing.get(
                        "cost_preference", selection_policy.get("cost", "")
                    ),
                ),
            )
        )
        quality = _text(
            _manifest_value(manifest, "quality", default=None)
            or pricing.get(
                "quality",
                pricing.get("quality_preference", selection_policy.get("quality", "")),
            )
        )
        if request.cost_preference:
            cost_rank = 0 if cost.casefold() == request.cost_preference.casefold() else 1
        else:
            cost_rank = 0
        if request.quality_preference:
            quality_rank = 0 if quality.casefold() == request.quality_preference.casefold() else 1
        else:
            quality_rank = 0
        explicit_penalty = 1 if _requires_explicit_selection(manifest) and not request.explicit else 0
        # Runtime available/configured candidates win over merely declared
        # manifests when availability was not a hard filter.
        readiness_rank = 0 if readiness.runtime_available else 1
        configured_rank = 0 if readiness.configured else 1
        return (explicit_penalty, readiness_rank, configured_rank, cost_rank, quality_rank, priority_value, manifest_id(manifest))

    @staticmethod
    def _resolved(
        manifest: object,
        readiness: ModelReadiness,
        *,
        capability: CapabilityKind | None = None,
    ) -> ResolvedModel:
        capabilities = manifest_capabilities(manifest)
        if not capabilities:
            raise UnsupportedCapabilityError("manifest has no supported capability")
        selected_capability = capability or capabilities[0]
        if selected_capability not in capabilities:
            raise UnsupportedCapabilityError(
                f"manifest does not support capability {selected_capability.value}"
            )
        return ResolvedModel(
            manifest=manifest,
            readiness=readiness,
            capability=selected_capability,
            protocol=manifest_protocol(manifest),
            provider_id=manifest_provider_id(manifest),
            model_id=manifest_model_id(manifest),
            manifest_id=manifest_id(manifest),
            manifest_hash=manifest_hash(manifest),
            endpoint_profile_id=manifest_endpoint_id(manifest),
            endpoint_class=manifest_endpoint_class(manifest),
            deployment_region=manifest_region(manifest),
        )


RuntimeModelResolver = ModelResolver
UniversalModelResolver = ModelResolver
ManifestResolver = ModelResolver
ModelResolverRequest = ResolverRequest


def _normalize_region_value(value: object) -> str:
    normalized = (
        _text(getattr(value, "value", value), "UNSPECIFIED")
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    ) or "UNSPECIFIED"
    return {
        "MAINLAND": "MAINLAND_CHINA",
        "CHINA": "MAINLAND_CHINA",
        "CN": "MAINLAND_CHINA",
        "GLOBAL": "INTERNATIONAL",
    }.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class CompatibilityManifest:
    """Secret-free manifest view generated from a legacy provider status."""

    manifest_id: str
    display_name: str
    provider_id: str
    capability: CapabilityKind
    protocol: ProtocolFamily
    model_id: str
    deployment_region: str = "UNSPECIFIED"
    endpoint_class: str = "UNSPECIFIED"
    endpoint_profile_id: str = "LEGACY"
    credential_reference: str | None = None
    authorization: Mapping[str, object] = field(default_factory=dict)
    readiness: Mapping[str, object] = field(default_factory=dict)
    selection_policy: Mapping[str, object] = field(default_factory=dict)
    selection_priority: int = 100
    manifest_version: int = 1
    codec_id: str = "legacy.compatibility"
    codec_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", normalize_capability(self.capability))
        object.__setattr__(self, "protocol", normalize_protocol(self.protocol))
        if (
            isinstance(self.manifest_version, bool)
            or not isinstance(self.manifest_version, int)
            or self.manifest_version < 1
        ):
            raise ModelResolutionError("manifest_version must be a positive integer")
        if isinstance(self.selection_priority, bool) or not isinstance(
            self.selection_priority, int
        ):
            raise ModelResolutionError("selection_priority must be an integer")
        for name in ("authorization", "readiness", "selection_policy"):
            if not isinstance(getattr(self, name), Mapping):
                raise ModelResolutionError(f"{name} must be a mapping")
        # Compatibility manifests are persisted/public identity records.  All
        # scalar identity/display fields therefore use the same strict,
        # secret/path-safe boundary; provider wire values never reach
        # ``as_dict`` or ``manifest_hash`` unchecked.
        scalar_values = {
            "manifest_id": self.manifest_id,
            "display_name": self.display_name,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "deployment_region": _normalize_region_value(self.deployment_region),
            "endpoint_class": self.endpoint_class,
            "endpoint_profile_id": self.endpoint_profile_id,
            "codec_id": self.codec_id,
        }
        for name, value in scalar_values.items():
            object.__setattr__(self, name, _safe_compat_text(name, value))
        # ``codec_version`` historically is an integer on the compatibility
        # projection (native manifests use a textual version).  Preserve that
        # wire-compatible shape while still rejecting token/path-like string
        # values if an older provider supplies them.
        codec_version = self.codec_version
        if isinstance(codec_version, bool) or not isinstance(codec_version, (int, str)):
            raise ModelResolutionError("codec_version must be text or an integer")
        if isinstance(codec_version, str):
            object.__setattr__(self, "codec_version", _safe_compat_text("codec_version", codec_version))
        elif codec_version < 1:
            raise ModelResolutionError("codec_version must be positive")
        reference = self.credential_reference
        if reference is not None:
            safe_reference = _safe_public_mapping({"credential_reference": reference}).get(
                "credential_reference"
            )
            object.__setattr__(
                self,
                "credential_reference",
                (
                    _text(safe_reference)
                    if safe_reference
                    and re.fullmatch(
                        r"(?:[A-Za-z][A-Za-z0-9_.-]*:)?[A-Za-z][A-Za-z0-9_.:-]{0,127}",
                        _text(safe_reference),
                    )
                    else None
                ),
            )
        safe_authorization = _safe_public_mapping(self.authorization)
        for name in ("create_is_paid", "requires_create_authorization"):
            if name in safe_authorization and not isinstance(
                safe_authorization[name], bool
            ):
                # A malformed declaration can never disable a paid-create
                # gate.  Keep the compatibility row usable for diagnostics,
                # but normalize the lifecycle decision fail-closed.
                safe_authorization[name] = True
        object.__setattr__(
            self,
            "authorization",
            MappingProxyType(safe_authorization),
        )
        safe_readiness = _safe_public_mapping(self.readiness)
        for name in ("configured", "verified", "runtime_available"):
            if name in safe_readiness and not isinstance(safe_readiness[name], bool):
                safe_readiness[name] = False
        if "create_authorized" in safe_readiness and not isinstance(
            safe_readiness["create_authorized"], bool
        ):
            safe_readiness["create_authorized"] = False
        if "authorization_required" in safe_readiness and not isinstance(
            safe_readiness["authorization_required"], bool
        ):
            safe_readiness["authorization_required"] = True
        object.__setattr__(
            self,
            "readiness",
            MappingProxyType(safe_readiness),
        )
        object.__setattr__(
            self,
            "selection_policy",
            MappingProxyType(_safe_public_mapping(self.selection_policy)),
        )

    # Match the native ``ModelManifest`` identity/property surface so a
    # resolver or a later migration slice does not need to branch on whether
    # the selected row came from the legacy bridge.
    @property
    def id(self) -> str:
        return self.manifest_id

    @property
    def label(self) -> str:
        return self.display_name

    @property
    def protocol_family(self) -> ProtocolFamily:
        return self.protocol

    @property
    def provider(self) -> str:
        return self.provider_id

    @property
    def model(self) -> str:
        return self.model_id

    @property
    def endpoint_id(self) -> str:
        return self.endpoint_profile_id

    @property
    def canonical_hash(self) -> str:
        return self.manifest_hash

    @property
    def capabilities(self) -> tuple[CapabilityKind, ...]:
        return (self.capability,)

    @property
    def configured(self) -> bool:
        return self.readiness.get("configured") is True

    @property
    def verified(self) -> bool:
        return self.readiness.get("verified") is True

    @property
    def runtime_available(self) -> bool:
        return self.readiness.get("runtime_available") is True

    @property
    def available(self) -> bool:
        return self.runtime_available

    @property
    def create_authorized(self) -> bool:
        return self.readiness.get("create_authorized") is True

    @property
    def authorization_required(self) -> bool:
        return (
            self.authorization.get("requires_create_authorization") is True
            or self.readiness.get("authorization_required") is True
        )

    @property
    def can_create(self) -> bool:
        return not self.authorization_required or self.create_authorized

    @property
    def ready(self) -> bool:
        return self.configured and self.runtime_available

    @property
    def ready_for_create(self) -> bool:
        return self.ready and self.can_create

    @property
    def authorization_pending(self) -> bool:
        return self.authorization_required and not self.create_authorized

    @property
    def verification_state(self) -> str:
        return "VERIFIED" if self.verified else "NOT_VERIFIED"

    @property
    def manifest_hash(self) -> str:
        payload = {
            "manifest_version": self.manifest_version,
            "manifest_id": self.manifest_id,
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
            "authorization": dict(self.authorization),
            "readiness": dict(self.readiness),
            "selection_policy": dict(self.selection_policy),
            "selection_priority": self.selection_priority,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "manifest_id": self.manifest_id,
            "display_name": self.display_name,
            "provider_id": self.provider_id,
            "capability": self.capability.value,
            "capabilities": [self.capability.value],
            "protocol": self.protocol.value,
            "protocol_family": self.protocol.value,
            "model_id": self.model_id,
            "deployment_region": self.deployment_region,
            "endpoint_class": self.endpoint_class,
            "endpoint_profile_id": self.endpoint_profile_id,
            "credential_reference": self.credential_reference,
            "authorization": dict(self.authorization),
            "readiness": dict(self.readiness),
            "configured": self.configured,
            "verified": self.verified,
            "verification_state": self.verification_state,
            "runtime_available": self.runtime_available,
            "available": self.available,
            "create_authorized": self.create_authorized,
            "authorization_required": self.authorization_required,
            "ready": self.ready,
            "ready_for_create": self.ready_for_create,
            "authorization_pending": self.authorization_pending,
            "cost_authorization": dict(self.authorization),
            "selection_policy": dict(self.selection_policy),
            "selection_priority": self.selection_priority,
            "manifest_hash": self.manifest_hash,
            "codec_id": self.codec_id,
            "codec_version": self.codec_version,
        }

    def serialize(self) -> str:
        """Return the same safe, deterministic JSON view as ``ModelManifest``.

        Compatibility manifests are intentionally lightweight dataclasses, but
        callers should not need to branch on whether a manifest came from the
        native registry or the legacy bridge.  Only ``as_dict`` (which already
        contains redacted metadata) participates in this serialization.
        """

        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    canonical_json = serialize
    to_json = serialize

    def validate(self) -> "CompatibilityManifest":
        """Validate/return this immutable compatibility projection."""

        # Construction normalizes all fields; serializing here additionally
        # proves that the public projection remains JSON-safe.
        self.serialize()
        return self

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CompatibilityManifest":
        if not isinstance(value, Mapping):
            raise ModelResolutionError("compatibility manifest payload must be a mapping")
        payload = dict(value)

        def first(*names: str, default: object = None) -> object:
            for name in names:
                if name in payload and payload[name] is not None:
                    return payload[name]
            return default

        raw_capabilities = first("capability", "capabilities", default=None)
        if isinstance(raw_capabilities, (tuple, list)):
            if len(raw_capabilities) != 1:
                raise ModelResolutionError(
                    "compatibility manifest must declare exactly one capability"
                )
            raw_capabilities = raw_capabilities[0]
        # ``readiness`` is the canonical identity block.  The flat fields in
        # ``as_dict`` are projections for diagnostics and must not be merged
        # back into a sparse nested block: doing so changes the manifest hash
        # on a serialize/deserialize round trip (notably when a legacy row
        # omitted ``create_authorized`` and ``authorization_required``).
        raw_readiness = payload.get("readiness")
        has_nested_readiness = isinstance(raw_readiness, Mapping)
        if "readiness" in payload and raw_readiness is not None and not has_nested_readiness:
            raise ModelResolutionError("compatibility manifest readiness must be a mapping")
        readiness = dict(raw_readiness) if has_nested_readiness else {}
        readiness_fields = (
            "configured",
            "verified",
            "runtime_available",
            "create_authorized",
            "authorization_required",
        )
        if has_nested_readiness:
            # Validate a projection when it repeats a canonical nested value,
            # while deliberately ignoring projection-only fields that the
            # legacy manifest did not include in its identity block.
            for name in readiness_fields:
                if name not in payload:
                    continue
                projected = payload[name]
                if not isinstance(projected, bool):
                    raise ModelResolutionError(f"compatibility manifest {name} must be bool")
                if name in readiness and readiness[name] != projected:
                    raise ModelResolutionError(
                        f"compatibility manifest readiness.{name} conflicts with flat {name}"
                    )
            for alias, canonical in (("available", "runtime_available"), ("live_authorized", "create_authorized")):
                if alias not in payload:
                    continue
                projected = payload[alias]
                if not isinstance(projected, bool):
                    raise ModelResolutionError(f"compatibility manifest {alias} must be bool")
                if canonical in readiness and readiness[canonical] != projected:
                    raise ModelResolutionError(
                        f"compatibility manifest readiness.{canonical} conflicts with flat {alias}"
                    )
        else:
            # For legacy payloads with no nested block, accept the flat
            # readiness spellings as the only available source of state.
            for name in readiness_fields:
                if name in payload:
                    readiness[name] = payload[name]
            for alias, canonical in (("available", "runtime_available"), ("live_authorized", "create_authorized")):
                if alias not in payload:
                    continue
                projected = payload[alias]
                if canonical in readiness and readiness[canonical] != projected:
                    raise ModelResolutionError(
                        f"compatibility manifest {canonical} conflicts with flat {alias}"
                    )
                readiness.setdefault(canonical, projected)
        authorization = first(
            "authorization", "cost_authorization", default={}
        )
        if not isinstance(authorization, Mapping):
            authorization = {}
        declared_hash = first("manifest_hash", default=None)
        manifest = cls(
            manifest_id=first("manifest_id", "id", default=""),
            display_name=first(
                "display_name",
                "label",
                default=first("manifest_id", "id", default=""),
            ),
            provider_id=first("provider_id", "provider", default=""),
            capability=raw_capabilities if raw_capabilities is not None else "LLM",
            protocol=first("protocol", "protocol_family", default="REQUEST_RESPONSE"),
            model_id=first("model_id", "model", default=""),
            deployment_region=first("deployment_region", "region", default="UNSPECIFIED"),
            endpoint_class=first("endpoint_class", default="UNSPECIFIED"),
            endpoint_profile_id=first(
                "endpoint_profile_id", "endpoint_id", default="LEGACY"
            ),
            credential_reference=first(
                "credential_reference", "credential_ref", default=None
            ),
            authorization=authorization,
            readiness=readiness,
            selection_policy=first("selection_policy", default={}),
            selection_priority=first("selection_priority", "priority", default=100),
            manifest_version=first("manifest_version", default=1),
            codec_id=first("codec_id", default="legacy.compatibility"),
            codec_version=first("codec_version", default=1),
        )
        if declared_hash is not None and _text(declared_hash) != manifest.manifest_hash:
            raise ModelResolutionError("compatibility manifest hash does not match canonical identity")
        return manifest

    @classmethod
    def from_json(cls, value: str) -> "CompatibilityManifest":
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ModelResolutionError("compatibility manifest JSON is malformed") from exc
        return cls.from_dict(parsed)

    model_dump = as_dict
    to_dict = as_dict


class LegacyCapabilityBridge:
    """Expose the existing ``CapabilityRegistry`` through model manifests.

    The bridge is intentionally read-only.  It holds provider objects only in
    memory for exact adapter lookup; generated manifests contain status
    metadata and credential *references*, never credential values.
    """

    def __init__(self, legacy_registry: object | None = None, *, env: Mapping[str, str] | None = None) -> None:
        if legacy_registry is None:
            from aidrama_studio.services.ai_capabilities import default_capability_registry

            legacy_registry = default_capability_registry(env=env) if env is not None else default_capability_registry()
        self.legacy_registry = legacy_registry
        self._entries: tuple[tuple[CompatibilityManifest, object, object], ...] | None = None

    def _build(self) -> tuple[tuple[CompatibilityManifest, object, object], ...]:
        if self._entries is not None:
            return self._entries
        rows: list[tuple[CompatibilityManifest, object, object]] = []
        registry = self.legacy_registry
        capabilities = []
        if hasattr(registry, "all_status"):
            try:
                capabilities = list(registry.all_status().keys())
            except Exception:
                capabilities = []
        if not capabilities and hasattr(registry, "status"):
            try:
                capabilities = list(registry.status().keys())
            except Exception:
                capabilities = []
        if not capabilities:
            # CapabilityRegistry.list is the only source available on a few
            # test doubles; probe canonical kinds without making assumptions.
            capabilities = [kind.value for kind in CapabilityKind]

        for raw_capability in capabilities:
            try:
                capability = normalize_capability(raw_capability)
            except UnsupportedCapabilityError:
                # VIDEO_STOCK is intentionally outside the universal model
                # contract; leave it in the legacy registry untouched.
                continue
            providers: tuple[object, ...] = ()
            for list_key in (raw_capability, capability, capability.value):
                try:
                    providers = tuple(registry.list(list_key))
                except Exception:
                    continue
                if providers:
                    break
            for provider in providers:
                provider_name = _text(getattr(provider, "provider_name", provider.__class__.__name__))
                try:
                    status = provider.status
                except Exception:
                    status = SimpleNamespace(available=False, configured=False, verified=False, metadata={}, reason="status unavailable")
                metadata = dict(_map(getattr(status, "metadata", None)))
                model = _text(metadata.get("model") or getattr(provider, "model_id", "runtime"), "runtime")
                region = _normalize_region_value(metadata.get("deployment_region", "UNSPECIFIED"))
                endpoint_class = _text(metadata.get("endpoint_class"), "UNSPECIFIED")
                endpoint = _text(metadata.get("endpoint_profile_id"), f"runtime:{capability.value}:{provider_name}:{endpoint_class}")
                raw_credential_reference = metadata.get("credential_reference")
                safe_credential_reference = _safe_public_mapping(
                    {"credential_reference": raw_credential_reference}
                ).get("credential_reference")
                if safe_credential_reference and not re.fullmatch(
                    r"(?:[A-Za-z][A-Za-z0-9_.-]*:)?[A-Za-z][A-Za-z0-9_.:-]{0,127}",
                    _text(safe_credential_reference),
                ):
                    safe_credential_reference = None
                protocol_raw = metadata.get("protocol")
                if protocol_raw is None:
                    protocol_raw = "ASYNC_TASK" if capability is CapabilityKind.VIDEO else "REQUEST_RESPONSE"
                try:
                    protocol = normalize_protocol(protocol_raw)
                except UnsupportedProtocolError:
                    # A malformed provider metadata record should not become a
                    # silently usable manifest.
                    continue
                requires_auth = bool(
                    getattr(status, "authorization_required", False) is True
                    or
                    metadata.get("requires_create_authorization")
                    or metadata.get("authorization_required")
                    or metadata.get("create_is_paid")
                    or (
                        "live_authorized" in metadata
                        and region not in {"LOCAL", "UNSPECIFIED"}
                    )
                )
                raw_configured = getattr(status, "configured", None)
                if raw_configured is None:
                    raw_configured = metadata.get("configured", False)
                status_configured = raw_configured is True
                raw_verified = getattr(status, "verified", None)
                if raw_verified is None:
                    raw_verified = metadata.get("verified", False)
                status_reason = _text(getattr(status, "reason", ""))
                runtime_available = getattr(status, "runtime_available", None)
                if runtime_available is None:
                    runtime_available = metadata.get("runtime_available")
                if runtime_available is None:
                    runtime_available = bool(
                        getattr(status, "available", False) is True
                        or (
                            status_configured
                            and requires_auth
                            and any(
                                marker in status_reason.casefold()
                                for marker in ("paid", "authoriz", "授权", "费用")
                            )
                        )
                    )
                # Only safe, non-secret metadata is copied into the manifest.
                safe_readiness = {
                    "configured": status_configured,
                    "verified": raw_verified is True,
                    "runtime_available": runtime_available,
                    "create_authorized": getattr(status, "create_authorized", None),
                    "authorization_required": getattr(status, "authorization_required", None),
                    "reason": status_reason,
                    "credential_present": metadata.get("credential_present"),
                    "endpoint_profile_id": endpoint,
                    "endpoint_class": endpoint_class,
                    "deployment_region": region,
                }
                authorization = {
                    "create_is_paid": requires_auth,
                    "requires_create_authorization": requires_auth,
                }
                identity_suffix = hashlib.sha256(
                    f"{region}|{endpoint_class}|{endpoint}".encode("utf-8")
                ).hexdigest()[:12]
                try:
                    raw_priority = metadata.get("selection_priority", 100)
                    selection_priority = int(raw_priority or 100)
                except (TypeError, ValueError, OverflowError):
                    # A malformed legacy hint is not an identity failure, but
                    # it must not abort inventory construction or influence
                    # selection unpredictably.
                    selection_priority = 100
                try:
                    manifest = CompatibilityManifest(
                        manifest_id=(
                            f"legacy:{capability.value.lower()}:{provider_name.lower()}:"
                            f"{model.lower()}:{identity_suffix}"
                        ),
                        display_name=f"{provider_name} {model}",
                        provider_id=provider_name,
                        capability=capability,
                        protocol=protocol,
                        model_id=model,
                        deployment_region=region,
                        endpoint_class=endpoint_class,
                        endpoint_profile_id=endpoint,
                        credential_reference=(
                            _text(safe_credential_reference)
                            if safe_credential_reference
                            else None
                        ),
                        authorization=authorization,
                        readiness=safe_readiness,
                        selection_policy={"requires_explicit_selection": metadata.get("requires_explicit_selection") is True},
                        selection_priority=selection_priority,
                    )
                except ModelResolutionError:
                    # One malformed legacy status must not poison the whole
                    # capability inventory.  The unsafe row is omitted; no
                    # coercion or redaction is allowed to create a misleading
                    # public identity.
                    continue
                rows.append((manifest, provider, status))
        self._entries = tuple(rows)
        return self._entries

    def manifests(self, capability: CapabilityKind | str | None = None) -> tuple[CompatibilityManifest, ...]:
        values = tuple(row[0] for row in self._build())
        if capability is None:
            return values
        kind = normalize_capability(capability)
        return tuple(item for item in values if kind in manifest_capabilities(item))

    list = manifests

    def manifest_registry(self) -> InMemoryManifestRegistry:
        return InMemoryManifestRegistry(self.manifests())

    def status_for(self, manifest: object) -> object | None:
        target = manifest_id(manifest)
        return next((row[2] for row in self._build() if manifest_id(row[0]) == target), None)

    def provider_for(self, selection_or_manifest: object) -> object:
        target_manifest = getattr(selection_or_manifest, "manifest", selection_or_manifest)
        target = manifest_id(target_manifest)
        # Match all frozen identity dimensions to avoid accidentally returning
        # a provider with the same name but a changed endpoint/model.
        for item, provider, _ in self._build():
            if manifest_id(item) != target:
                continue
            if manifest_provider_id(item) != manifest_provider_id(target_manifest):
                continue
            if manifest_model_id(item) != manifest_model_id(target_manifest):
                continue
            if manifest_endpoint_id(item) != manifest_endpoint_id(target_manifest):
                continue
            return provider
        raise ModelResolutionError("frozen compatibility provider is not present; no fallback")


CompatibilityBridge = LegacyCapabilityBridge
LegacyProviderBridge = LegacyCapabilityBridge


def compatibility_registry(
    legacy_registry: object | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[InMemoryManifestRegistry, LegacyCapabilityBridge]:
    """Build a manifest registry and bridge for the current provider seam."""

    bridge = LegacyCapabilityBridge(legacy_registry, env=env)
    return bridge.manifest_registry(), bridge


build_compatibility_registry = compatibility_registry


def resolve_model(
    registry: object,
    *,
    capability: CapabilityKind | str,
    **kwargs: object,
) -> ResolvedModel:
    """One-shot convenience wrapper around :class:`ModelResolver`."""

    return ModelResolver(registry).resolve(capability=capability, **kwargs)


__all__ = [
    "AvailabilityPolicy",
    "Availability",
    "RegionPolicy",
    "SelectionRegion",
    "RegionSelectionPolicy",
    "ResolverRequest",
    "FrozenModelIdentity",
    "ResolvedModel",
    "ModelSelection",
    "ResolvedSelection",
    "ResolvedModelSelection",
    "ModelResolver",
    "ManifestResolver",
    "ModelResolverRequest",
    "RuntimeModelResolver",
    "UniversalModelResolver",
    "InMemoryManifestRegistry",
    "ModelManifestRegistry",
    "ManifestRegistry",
    "ResolverRegistry",
    "CompatibilityManifest",
    "LegacyCapabilityBridge",
    "LegacyProviderBridge",
    "CompatibilityBridge",
    "compatibility_registry",
    "build_compatibility_registry",
    "resolve_model",
    "normalize_capability",
    "normalize_protocol",
    "manifest_id",
    "manifest_provider_id",
    "manifest_model_id",
    "manifest_endpoint_id",
    "manifest_region",
    "manifest_capabilities",
    "manifest_protocol",
    "ModelResolutionError",
    "ResolutionError",
    "ResolverError",
    "UnsupportedCapabilityError",
    "UnsupportedProtocolError",
    "RegionResolutionError",
    "RegionMismatchError",
    "FrozenIdentityError",
    "ModelUnavailableError",
]
