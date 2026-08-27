"""Provider/model readiness for the universal model runtime.

The legacy capability inventory exposes a single ``available`` flag.  That
flag is useful for backwards compatibility, but it is not enough for a
runtime which may be configured, locally usable, verified, and authorised for
a paid create at different times.  This module keeps those facts independent
and provides a small, secret-free compatibility adapter for legacy status
objects.

No network calls are made here.  In particular, ``create_authorized`` is
never inferred from the presence of a credential or from ``configured``.
Callers have to pass an explicit authorisation signal when a manifest says a
create is paid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Callable, Mapping


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _freeze_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_metadata(child) for key, child in value.items()}
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(_freeze_metadata(child) for child in value)
    return value


def _bool(value: object, default: bool = False) -> bool:
    """Parse the deliberately small set of boolean values used at boundaries."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "authorized", "authorised", "ready"}:
            return True
        if normalized in {"0", "false", "no", "off", "unauthorized", "unauthorised", "unready"}:
            return False
    return default


def _gate_required(value: object, default: bool = False) -> bool:
    """Interpret an authorization requirement fail-closed."""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    # Unknown/non-boolean requirement metadata must never open a paid path.
    return True


def _gate_authorized(value: object) -> bool:
    """Only a literal ``True`` can authorize a potentially paid CREATE."""

    return value is True


def _manifest_authorization(manifest: object | None) -> Mapping[str, object]:
    if manifest is None:
        return {}
    if isinstance(manifest, Mapping):
        value = manifest.get("authorization", manifest.get("authorisation"))
        if isinstance(value, Mapping):
            return value
        value = manifest.get("cost_authorization")
        return value if isinstance(value, Mapping) else {}
    value = getattr(manifest, "authorization", None)
    if value is None:
        value = getattr(manifest, "authorisation", None)
    if isinstance(value, Mapping):
        return value
    if value is not None:
        # ``manifest.AuthorizationMetadata`` is a frozen dataclass rather
        # than a mapping.  Read only its two public, non-secret flags.
        values = {
            name: getattr(value, name)
            for name in ("create_is_paid", "requires_create_authorization")
            if hasattr(value, name)
        }
        if values:
            return values
    # A few early manifest drafts used a nested ``cost`` object.  Supporting
    # it here keeps the bridge tolerant without putting pricing decisions in
    # the resolver.
    value = getattr(manifest, "cost_authorization", None)
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class ModelReadiness:
    """Independent readiness facts for one model endpoint.

    ``runtime_available`` describes whether the local/runtime boundary can
    currently be used (for example, credentials and SDK setup are valid).
    ``create_authorized`` is an explicit gate checked immediately before a
    paid create.  A model can therefore be configured and runtime available
    while still requiring a human/acceptance authorisation.
    """

    configured: bool = False
    verified: bool = False
    runtime_available: bool = False
    create_authorized: bool = False
    authorization_required: bool = False
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep metadata immutable and prevent accidental retention of a
        # mutable provider status dictionary in a frozen selection.
        object.__setattr__(self, "metadata", _freeze_metadata(_mapping(self.metadata)))
        for name in (
            "configured",
            "verified",
            "runtime_available",
            "create_authorized",
            "authorization_required",
        ):
            # Do not use Python truthiness for boundary values: the string
            # ``"false"`` must not become an affirmative readiness signal.
            normalized = (
                _gate_required(getattr(self, name))
                if name == "authorization_required"
                else _gate_authorized(getattr(self, name))
                if name == "create_authorized"
                else _bool(getattr(self, name))
            )
            object.__setattr__(self, name, normalized)

    @property
    def available(self) -> bool:
        """Compatibility projection used by the existing provider seam."""

        return self.runtime_available

    @property
    def ready(self) -> bool:
        """Whether non-create runtime operations are safe to invoke."""

        return self.configured and self.runtime_available

    @property
    def ready_for_create(self) -> bool:
        """Whether a create may proceed after the caller's final gate."""

        return self.ready and (
            not self.authorization_required or self.create_authorized
        )

    @property
    def authorization_pending(self) -> bool:
        return self.authorization_required and not self.create_authorized

    @property
    def verification_state(self) -> str:
        return "VERIFIED" if self.verified else "NOT_VERIFIED"

    @property
    def live_authorized(self) -> bool:
        return self.create_authorized

    def as_dict(self) -> dict[str, object]:
        """Return a public, secret-free readiness record."""

        return {
            "configured": self.configured,
            "verified": self.verified,
            "runtime_available": self.runtime_available,
            "create_authorized": self.create_authorized,
            "authorization_required": self.authorization_required,
            # ``available`` is retained as a compatibility projection; it is
            # intentionally not used to derive any of the independent flags.
            "available": self.runtime_available,
            "ready": self.ready,
            "ready_for_create": self.ready_for_create,
            "authorization_pending": self.authorization_pending,
            "verification_state": self.verification_state,
            "live_authorized": self.live_authorized,
            "reason": str(self.reason or ""),
            "metadata": _public_metadata(self.metadata),
        }

    public_dict = as_dict


# Intuitive aliases used by callers which call the record a state/snapshot.
ReadinessSnapshot = ModelReadiness
ModelReadinessState = ModelReadiness
ReadinessState = ModelReadiness


def _public_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Drop values which must never cross the manifest/readiness boundary."""

    # Match secret-bearing keys precisely (or by a safe suffix).  In
    # particular, ``authorization_required`` is a public readiness flag and
    # must not be mistaken for an Authorization header.
    secret_markers = (
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "private_key",
        "cookie",
        "signed_url",
    )
    exact_secret_keys = {"authorization", "authorization_header"}

    def clean(item: object, key: str | None = None) -> object:
        if key is not None:
            lowered = key.casefold()
            if lowered in exact_secret_keys or any(
                lowered == marker or lowered.endswith("_" + marker)
                for marker in secret_markers
            ):
                return "<redacted>"
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
                return "<redacted>"
        if isinstance(item, Mapping):
            return {str(k): clean(v, str(k)) for k, v in item.items()}
        if isinstance(item, (tuple, list, set, frozenset)):
            return [clean(v) for v in item]
        return item

    result = clean(value)
    return result if isinstance(result, dict) else {}


def readiness_from_status(
    status: object | None = None,
    *,
    manifest: object | None = None,
    authorization: object | None = None,
    authorization_required: bool | None = None,
    create_authorized: bool | None = None,
    runtime_available: bool | None = None,
) -> ModelReadiness:
    """Convert a legacy ``CapabilityStatus``-like object to independent facts.

    ``status`` is intentionally duck typed so this bridge works with the
    existing ``CapabilityStatus`` and with small test doubles.  Explicit
    keyword arguments win over status metadata, then over manifest metadata.
    """

    # Accept both the legacy status object and a plain mapping.  The latter is
    # common when a readiness snapshot has crossed a JSON boundary.
    status_values = status if isinstance(status, Mapping) else None
    if status_values is not None:
        nested_metadata = _mapping(status_values.get("metadata"))
        status_metadata = {
            **dict(nested_metadata),
            **{
                str(key): value
                for key, value in status_values.items()
                if str(key) != "metadata"
            },
        }
    else:
        status_metadata = _mapping(getattr(status, "metadata", None))

    def status_value(name: str, default: object = None) -> object:
        if status_values is not None:
            return status_values.get(name, default)
        return getattr(status, name, default)

    manifest_auth = _manifest_authorization(manifest)
    auth_metadata = _mapping(authorization)

    configured_value = status_value("configured")
    if configured_value is None:
        configured_value = status_metadata.get("configured")
    if configured_value is None:
        # Before the remediation, ``available`` was the only signal.  Use it
        # only as a legacy fallback; a modern explicit ``configured=False``
        # remains authoritative.
        configured_value = status_value("available", False)

    verified_value = status_value("verified")
    if verified_value is None:
        verified_value = status_metadata.get("verified")

    # Preserve the distinction between an explicitly reported
    # ``runtime_available=False`` and a legacy status which only had
    # ``available=False`` because paid-create authorization was not granted.
    # CapabilityStatus exposes a provenance bit for this purpose; arbitrary
    # test/compatibility objects with a runtime field are treated as explicit.
    runtime_explicit = runtime_available is not None
    if runtime_available is not None:
        runtime_value = runtime_available
    elif "runtime_available" in status_metadata:
        runtime_value = status_metadata.get("runtime_available")
        runtime_explicit = True
    else:
        runtime_attr = status_value("runtime_available")
        if runtime_attr is not None:
            runtime_value = runtime_attr
            runtime_explicit = status_value("runtime_available_explicit", True) is not False
        else:
            runtime_value = status_metadata.get("runtime_ready")
            if "runtime_ready" in status_metadata:
                runtime_explicit = True
    if runtime_value is None:
        runtime_value = status_value("available", False)

    required_value = authorization_required
    if required_value is None:
        required_value = auth_metadata.get("requires_create_authorization")
    if required_value is None:
        required_value = manifest_auth.get("requires_create_authorization")
    if required_value is None:
        required_value = manifest_auth.get("required")
    if required_value is None:
        required_value = status_metadata.get("authorization_required")
    if required_value is None:
        required_value = status_metadata.get("requires_create_authorization")
    if required_value is None:
        required_value = False

    # A manifest's declaration is authoritative for the create lifecycle.
    # A status object may omit the field entirely (or carry a stale false
    # default), but it must not erase an explicit paid-create requirement.
    manifest_required = manifest_auth.get("requires_create_authorization")
    if _gate_required(manifest_required, False):
        required_value = True

    configured = _bool(configured_value)
    # Authorization requirements are security-sensitive: only an explicit
    # boolean ``False`` can state that no gate is needed.  Unknown/string
    # values fail closed as ``True`` rather than accidentally opening CREATE.
    required = _gate_required(required_value)
    # Legacy provider statuses historically exposed ``available=False`` while
    # a paid CREATE authorization was pending.
    # Preserve the useful runtime distinction when the status explicitly
    # carries an authorization marker; ordinary credential/configuration
    # failures remain unavailable.
    if not runtime_explicit and runtime_value is False:
        reason_text = str(status_value("reason", "") or "").casefold()
        authorization_marker = any(
            marker in reason_text
            for marker in ("paid", "authoriz", "授权", "费用")
        ) or any(
            key in status_metadata
            for key in (
                "create_authorized",
                "authorization_required",
                "requires_create_authorization",
                "live_authorized",
            )
        )
        if configured and required and authorization_marker:
            runtime_value = True

    authorized_value = create_authorized
    if authorized_value is None:
        # Caller-provided authorization may be a mapping (for example the
        # frozen RuntimePlan authorization payload) or a simple bool.
        if isinstance(authorization, Mapping):
            for key in (
                "create_authorized",
                "authorized",
                "approved",
                "allow_paid_live_tests",
                "live_authorized",
            ):
                if key in authorization:
                    authorized_value = authorization[key]
                    break
        elif authorization is not None:
            authorized_value = authorization
    # ``CapabilityStatus`` normalizes its additive readiness fields in
    # ``__post_init__``.  Prefer that explicit object-level value over a stale
    # legacy ``live_authorized`` diagnostic nested in metadata (for example a
    # non-gated capability may retain an old ``False`` marker).  This keeps
    # the independent authorization dimension intact without deriving it from
    # configuration or from the human-readable reason.
    if authorized_value is None:
        status_authorized = status_value("create_authorized")
        if status_authorized is None:
            status_authorized = status_value("authorized")
        if status_authorized is None:
            status_authorized = status_value("approved")
        if status_authorized is None:
            status_authorized = status_value("live_authorized")
        if status_authorized is None:
            status_authorized = status_value("allow_paid_live_tests")
        if status_authorized is not None:
            authorized_value = status_authorized
    if authorized_value is None:
        for source in (status_metadata, manifest_auth):
            for key in (
                "create_authorized",
                "authorized",
                "approved",
                "allow_paid_live_tests",
                "live_authorized",
            ):
                if key in source:
                    authorized_value = source[key]
                    break
            if authorized_value is not None:
                break
    # Never make a paid create appear authorised merely because the model is
    # configured.  The default is deliberately false.
    authorized = _gate_authorized(authorized_value)
    metadata = dict(status_metadata)
    metadata.pop("api_key", None)
    metadata.pop("access_token", None)
    reason = str(status_value("reason", "") or status_metadata.get("reason", ""))
    return ModelReadiness(
        configured=configured,
        verified=_bool(verified_value),
        runtime_available=_bool(runtime_value),
        create_authorized=authorized,
        authorization_required=required,
        reason=reason,
        metadata=_public_metadata(metadata),
    )


def readiness_from_manifest(
    manifest: object,
    *,
    status: object | None = None,
    authorization: object | None = None,
    status_resolver: Callable[[object], object] | None = None,
) -> ModelReadiness:
    """Resolve readiness for a manifest without performing I/O."""

    if status is None and status_resolver is not None:
        status = status_resolver(manifest)
    # Manifest drafts use either a flat ``readiness`` mapping or direct flags.
    raw = (
        manifest.get("readiness")
        if isinstance(manifest, Mapping)
        else getattr(manifest, "readiness", None)
    )
    raw_map = _mapping(raw)
    if status is None and raw_map:
        if isinstance(manifest, Mapping):
            merged = {
                **dict(raw_map),
                **{
                    key: manifest[key]
                    for key in (
                        "configured",
                        "verified",
                        "runtime_available",
                        "available",
                        "create_authorized",
                        "authorization_required",
                    )
                    if key in manifest
                },
            }
            status = _StatusView(merged)
        else:
            status = _StatusView(raw_map)
    if status is None:
        keys = (
            "configured",
            "verified",
            "runtime_available",
            "available",
            "create_authorized",
            "authorization_required",
        )
        status = _StatusView(
            {
                key: (
                    manifest[key]
                    if isinstance(manifest, Mapping)
                    else getattr(manifest, key)
                )
                for key in keys
                if (key in manifest if isinstance(manifest, Mapping) else hasattr(manifest, key))
            }
        )
    return readiness_from_status(status, manifest=manifest, authorization=authorization)


@dataclass(frozen=True, slots=True)
class _StatusView:
    values: Mapping[str, object]

    @property
    def metadata(self) -> Mapping[str, object]:
        return self.values

    @property
    def configured(self) -> object:
        return self.values.get("configured")

    @property
    def verified(self) -> object:
        return self.values.get("verified")

    @property
    def available(self) -> object:
        return self.values.get("runtime_available", self.values.get("available", False))

    @property
    def reason(self) -> str:
        return str(self.values.get("reason", "") or "")


# Compatibility names: these are functions rather than a second mutable
# state model, so old and new call sites share precisely the same semantics.
coerce_readiness = readiness_from_status
assess_readiness = readiness_from_manifest


__all__ = [
    "ModelReadiness",
    "ModelReadinessState",
    "ReadinessSnapshot",
    "ReadinessState",
    "readiness_from_status",
    "readiness_from_manifest",
    "coerce_readiness",
    "assess_readiness",
]
