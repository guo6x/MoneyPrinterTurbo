"""Compatibility adapters for the existing AIDrama provider boundaries.

This module is deliberately thin.  It does not rewrite provider services or
their payloads; it only gives staged migrations a provider-neutral invocation
seam while the legacy object remains the owner of its current behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from .contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    DriverStatus,
    DriverSubmission,
    RuntimeOutcome,
)
from .readiness import ModelReadiness, readiness_from_status
from .resolver import (
    CompatibilityBridge,
    CompatibilityManifest,
    LegacyCapabilityBridge,
    LegacyProviderBridge,
)


class LegacyCapabilityAdapter:
    """Call an existing capability object without exposing provider fields."""

    def __init__(self, provider: object, manifest: object | None = None) -> None:
        self.provider = provider
        self.manifest = manifest

    @property
    def capability(self) -> CapabilityKind:
        raw = getattr(self.provider, "capability", getattr(self.manifest, "capability", "LLM"))
        return CapabilityKind.coerce(raw)

    @property
    def status(self) -> ModelReadiness:
        return readiness_from_status(
            getattr(self.provider, "status", None),
            manifest=self.manifest,
        )

    def invoke(self, request: CapabilityRequest) -> CapabilityResult | DriverSubmission:
        """Invoke only the selected legacy capability method.

        The adapter intentionally returns safe lifecycle records and does not
        attempt to normalize provider payloads.  A native codec should be
        introduced before a provider's wire result is persisted.
        """

        kind = request.capability
        provider = self.provider
        if kind is CapabilityKind.LLM and callable(getattr(provider, "generate_structured", None)):
            value = provider.generate_structured(request.prompt_or_text or "", schema=request.structured_input or None)
            if not isinstance(value, Mapping):
                raise TypeError("legacy LLM result must be a mapping")
            return CapabilityResult(
                request_id=request.request_id,
                outcome=RuntimeOutcome.SUCCEEDED,
                safe_metadata={"structured_output": dict(value)},
            )
        if kind is CapabilityKind.IMAGE and callable(getattr(provider, "generate_candidate", None)):
            candidate = provider.generate_candidate(
                request.prompt_or_text or "",
                project_id=request.project_id,
                metadata=request.provider_parameters,
            )
            return CapabilityResult(
                request_id=request.request_id,
                outcome=RuntimeOutcome.SUCCEEDED,
                safe_metadata={"candidate_status": getattr(candidate, "lifecycle_status", "DRAFT")},
            )
        if kind is CapabilityKind.VISION and callable(getattr(provider, "analyze", None)):
            value = provider.analyze(request=request.structured_input)
            return CapabilityResult(
                request_id=request.request_id,
                outcome=RuntimeOutcome.SUCCEEDED,
                safe_metadata={"analysis": getattr(value, "metadata", {})},
            )
        if kind is CapabilityKind.TTS and callable(getattr(provider, "synthesize", None)):
            value = provider.synthesize(
                request.prompt_or_text or "",
                voice=str(request.provider_parameters.get("voice", "")),
                language=str(request.provider_parameters.get("language", "zh-CN")),
                sample_rate=int(request.provider_parameters.get("sample_rate", 48000)),
            )
            return CapabilityResult(
                request_id=request.request_id,
                outcome=RuntimeOutcome.SUCCEEDED,
                safe_metadata={"mime_type": getattr(value, "mime_type", "")},
            )
        if kind is CapabilityKind.VIDEO and callable(getattr(provider, "submit", None)):
            submission = provider.submit(request.structured_input)
            reference = str(
                getattr(submission, "runtime_reference", getattr(submission, "provider_task_id", ""))
                or ""
            ).strip()
            if not reference:
                raise RuntimeError("legacy async provider returned no stable task identity")
            return DriverSubmission(
                request_id=request.request_id,
                protocol_reference=reference,
                provider_task_id=reference,
                safe_metadata={"compatibility": True},
            )
        raise TypeError(f"legacy provider does not implement capability {kind.value}")

    def poll(self, reference: str) -> DriverStatus:
        getter = getattr(self.provider, "get_status", None)
        if not callable(getter):
            raise TypeError("legacy provider does not expose async polling")
        raw = str(getter(reference)).upper()
        aliases = {
            "QUEUED": RuntimeOutcome.RUNNING,
            "PENDING": RuntimeOutcome.RUNNING,
            "RUNNING": RuntimeOutcome.RUNNING,
            "SUCCEEDED": RuntimeOutcome.SUCCEEDED,
            "FAILED": RuntimeOutcome.FAILED,
            "CANCELLED": RuntimeOutcome.CANCELLED,
        }
        outcome = aliases.get(raw, RuntimeOutcome.FAILED)
        return DriverStatus(protocol_reference=reference, outcome=outcome)

    def cancel(self, reference: str) -> bool:
        cancel = getattr(self.provider, "cancel", None)
        return bool(cancel(reference)) if callable(cancel) else False


LegacyProviderAdapter = LegacyCapabilityAdapter
CompatibilityProviderAdapter = LegacyCapabilityAdapter


def adapt_legacy_provider(provider: object, manifest: object | None = None) -> LegacyCapabilityAdapter:
    return LegacyCapabilityAdapter(provider, manifest)


__all__ = [
    "CompatibilityBridge",
    "CompatibilityManifest",
    "CompatibilityProviderAdapter",
    "LegacyCapabilityAdapter",
    "LegacyCapabilityBridge",
    "LegacyProviderAdapter",
    "LegacyProviderBridge",
    "adapt_legacy_provider",
]
