"""Provider-neutral built-in manifest registry foundation.

The registry contains immutable real-model metadata plus offline placeholders.
Neither kind claims that a credential was verified or that paid create was
authorized.  The compatibility bridge remains available for legacy providers
that have not yet moved behind a native codec.
"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import CapabilityKind, ProtocolFamily
from .mainland_manifests import MAINLAND_MANIFESTS
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
    """Registry preloaded with real model data and safe offline placeholders."""

    def __init__(
        self,
        manifests: Iterable[object] | None = None,
        *,
        include_placeholders: bool = True,
        include_mainland: bool = True,
    ) -> None:
        initial = (
            *(MAINLAND_MANIFESTS if include_mainland else ()),
            *(BUILTIN_MANIFESTS if include_placeholders else ()),
        )
        super().__init__((*initial, *(manifests or ())))


def default_manifest_registry(
    *,
    include_placeholders: bool = True,
    include_mainland: bool = True,
) -> BuiltinManifestRegistry:
    return BuiltinManifestRegistry(
        include_placeholders=include_placeholders,
        include_mainland=include_mainland,
    )


builtin_manifest_registry = default_manifest_registry


__all__ = [
    "BUILTIN_MANIFESTS",
    "MAINLAND_MANIFESTS",
    "BuiltinManifestRegistry",
    "builtin_manifest_registry",
    "default_manifest_registry",
]
