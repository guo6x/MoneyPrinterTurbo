"""Provider-neutral built-in manifest registry foundation.

The entries here are deliberately metadata-only placeholders.  They make the
registry usable in offline tooling and tests without claiming that a provider
credential, SDK, or paid-create authorization exists.  The compatibility
bridge remains the source for currently implemented providers.
"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import CapabilityKind, ProtocolFamily
from .manifest import ModelManifest
from .resolver import InMemoryManifestRegistry


def _placeholder(
    capability: CapabilityKind,
    protocol: ProtocolFamily,
) -> ModelManifest:
    name = capability.value.lower()
    return ModelManifest(
        id=f"builtin:{name}:v1",
        display_name=f"AIDrama {capability.value} runtime placeholder",
        provider_id="builtin",
        capability=capability,
        protocol=protocol,
        model_id=f"builtin-{name}-v1",
        deployment_region="CUSTOM",
        endpoint_class="BUILTIN_PLACEHOLDER",
        codec_id="generic.json",
        readiness={
            "configured": False,
            "verified": False,
            "runtime_available": False,
            "create_authorized": False,
            "authorization_required": False,
        },
        selection_policy={"requires_explicit_selection": True},
        metadata={"placeholder": True, "offline_only": True},
    )


BUILTIN_MANIFESTS: tuple[ModelManifest, ...] = (
    _placeholder(CapabilityKind.LLM, ProtocolFamily.REQUEST_RESPONSE),
    _placeholder(CapabilityKind.IMAGE, ProtocolFamily.REQUEST_RESPONSE),
    _placeholder(CapabilityKind.VIDEO, ProtocolFamily.ASYNC_TASK),
    _placeholder(CapabilityKind.VISION, ProtocolFamily.ASYNC_TASK),
    _placeholder(CapabilityKind.TTS, ProtocolFamily.STREAM),
)


class BuiltinManifestRegistry(InMemoryManifestRegistry):
    """Manifest registry preloaded with safe offline placeholders."""

    def __init__(self, manifests: Iterable[object] | None = None, *, include_placeholders: bool = True) -> None:
        initial = BUILTIN_MANIFESTS if include_placeholders else ()
        super().__init__((*initial, *(manifests or ())))


def default_manifest_registry(*, include_placeholders: bool = True) -> BuiltinManifestRegistry:
    return BuiltinManifestRegistry(include_placeholders=include_placeholders)


builtin_manifest_registry = default_manifest_registry


__all__ = [
    "BUILTIN_MANIFESTS",
    "BuiltinManifestRegistry",
    "builtin_manifest_registry",
    "default_manifest_registry",
]
