"""Product-level AI capability boundaries.

Provider-specific APIs stay behind these small contracts.  The registry only
publishes readiness metadata and never exposes credentials.  Image and Vision
implementations are intentionally unavailable by default; test code can use
the deterministic mock Vision provider without making a live-model claim.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from aidrama_studio.domain import ProductionInputSnapshot

from .ai import AIDramaAIError, generate_text, snapshot_llm_config
from .adapters.production_adapter import ProductionRuntimeAdapter, RuntimeSubmission


class CapabilityUnavailable(RuntimeError):
    """Raised when a provider boundary is known but not configured."""


class CapabilityKind(str, Enum):
    LLM = "LLM"
    IMAGE = "IMAGE"
    VIDEO_GENERATIVE = "VIDEO_GENERATIVE"
    VIDEO_STOCK = "VIDEO_STOCK"
    VISION = "VISION"


@dataclass(frozen=True)
class CapabilityStatus:
    capability: CapabilityKind
    provider: str
    available: bool
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def public_dict(self) -> dict[str, object]:
        """Return safe readiness metadata (never key/token values)."""
        return {
            "capability": self.capability.value,
            "provider": self.provider,
            "available": self.available,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class LLMProvider(ABC):
    capability = CapabilityKind.LLM
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def generate_structured(self, prompt: str, *, schema: Mapping[str, object] | None = None) -> dict[str, object]: ...

    def repair_structured(self, value: Mapping[str, object], *, schema: Mapping[str, object] | None = None) -> dict[str, object]:
        return dict(value)


class ImageGenerationProvider(ABC):
    capability = CapabilityKind.IMAGE
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def generate_candidate(self, prompt: str, *, project_id: str, metadata: Mapping[str, object] | None = None) -> "ImageCandidate": ...


class VideoGenerationProvider(ABC):
    capability = CapabilityKind.VIDEO_GENERATIVE
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def validate(self, snapshot: ProductionInputSnapshot) -> bool: ...

    @abstractmethod
    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission: ...

    @abstractmethod
    def get_status(self, runtime_reference: str) -> str: ...

    @abstractmethod
    def cancel(self, runtime_reference: str) -> bool: ...


class VisionAnalysisProvider(ABC):
    capability = CapabilityKind.VISION
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def analyze(self, *, artifact_path: str, context: Mapping[str, object] | None = None) -> "VisionAnalysis": ...


@dataclass(frozen=True)
class ImageCandidate:
    """A generated image candidate; it can only enter ReferenceAsset DRAFT."""

    project_id: str
    provider: str
    prompt: str
    content: bytes | None = None
    mime_type: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    lifecycle_status: str = "DRAFT"

    def __post_init__(self) -> None:
        if self.lifecycle_status != "DRAFT":
            raise ValueError("generated image candidates must remain DRAFT until human lock")


@dataclass(frozen=True)
class VisionAnalysis:
    provider: str
    metrics: Mapping[str, Mapping[str, object]]
    analysis_kind: str = "AI_ANALYSIS"
    metadata: Mapping[str, object] = field(default_factory=dict)


class MPTLLMProvider(LLMProvider):
    """Stable product boundary over the existing MPT LLM seam."""

    provider_name = "MPT_LLM"

    def __init__(self, config_snapshot: Mapping[str, object] | None = None):
        self._config_snapshot = dict(config_snapshot or snapshot_llm_config())

    @property
    def status(self) -> CapabilityStatus:
        from .ai import llm_configuration_status

        ready, reason = llm_configuration_status()
        return CapabilityStatus(CapabilityKind.LLM, self.provider_name, ready, reason)

    def generate_structured(self, prompt: str, *, schema: Mapping[str, object] | None = None) -> dict[str, object]:
        if not self.status.available:
            raise CapabilityUnavailable(self.status.reason)
        enriched = prompt
        if schema:
            enriched += "\nReturn only JSON matching this schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        try:
            raw = generate_text(enriched, self._config_snapshot)
            value = json.loads(raw)
        except (AIDramaAIError, json.JSONDecodeError) as exc:
            raise CapabilityUnavailable("LLM structured generation failed") from exc
        if not isinstance(value, dict):
            raise CapabilityUnavailable("LLM structured response must be an object")
        return value

    def repair_structured(self, value: Mapping[str, object], *, schema: Mapping[str, object] | None = None) -> dict[str, object]:
        return self.generate_structured("Repair this structured JSON without changing its intent:\n" + json.dumps(dict(value), ensure_ascii=False), schema=schema)


class RuntimeVideoProvider(VideoGenerationProvider):
    """Expose a frozen ProductionRuntimeAdapter as product VIDEO capability."""

    def __init__(self, adapter: ProductionRuntimeAdapter, *, provider_name: str | None = None, mode: CapabilityKind = CapabilityKind.VIDEO_GENERATIVE):
        self.adapter = adapter
        self.provider_name = provider_name or getattr(adapter, "name", adapter.__class__.__name__)
        self.capability = mode

    @property
    def status(self) -> CapabilityStatus:
        configured = True
        reason = "ready"
        config = getattr(self.adapter, "config", None)
        if config is not None and hasattr(config, "api_key"):
            configured = bool(str(config.api_key).strip())
            reason = "configured" if configured else "provider credential unavailable"
        return CapabilityStatus(self.capability, self.provider_name, configured, reason, {"mode": self.capability.value})

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        return self.adapter.validate(snapshot)

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        return self.adapter.submit(snapshot)

    def get_status(self, runtime_reference: str) -> str:
        return self.adapter.get_status(runtime_reference)

    def cancel(self, runtime_reference: str) -> bool:
        return self.adapter.cancel(runtime_reference)


class UnavailableImageProvider(ImageGenerationProvider):
    provider_name = "UNCONFIGURED_IMAGE"

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(CapabilityKind.IMAGE, self.provider_name, False, "no image provider configured")

    def generate_candidate(self, prompt: str, *, project_id: str, metadata: Mapping[str, object] | None = None) -> ImageCandidate:
        raise CapabilityUnavailable(self.status.reason)


class UnavailableVisionProvider(VisionAnalysisProvider):
    provider_name = "UNCONFIGURED_VISION"

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(CapabilityKind.VISION, self.provider_name, False, "no Vision provider configured")

    def analyze(self, *, artifact_path: str, context: Mapping[str, object] | None = None) -> VisionAnalysis:
        raise CapabilityUnavailable(self.status.reason)


class DeterministicMockVisionProvider(VisionAnalysisProvider):
    """Deterministic fake used only for unit tests and local decision plumbing."""

    provider_name = "MOCK_VISION"

    def __init__(self, metrics: Mapping[str, Mapping[str, object]] | None = None):
        self._metrics = dict(metrics or {"SHOT_ALIGNMENT": {"score": 1.0, "status": "PASS"}})

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(CapabilityKind.VISION, self.provider_name, True, "deterministic test provider", {"test_only": True})

    def analyze(self, *, artifact_path: str, context: Mapping[str, object] | None = None) -> VisionAnalysis:
        return VisionAnalysis(self.provider_name, self._metrics, metadata={"artifact_path": artifact_path, "test_only": True})


class CapabilityRegistry:
    """Project-independent registry of capability boundaries."""

    def __init__(self, providers: Sequence[object] | None = None):
        # Keep every provider registered for a capability.  A single selected
        # provider is still exposed through ``get`` for backwards
        # compatibility, but registering a second provider must not silently
        # erase the first one.
        self._providers: dict[CapabilityKind, list[object]] = {}
        self._preferred: dict[CapabilityKind, str] = {}
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: object, *, preferred: bool = False) -> None:
        capability = getattr(provider, "capability", None)
        if isinstance(capability, str):
            capability = CapabilityKind(capability)
        if not isinstance(capability, CapabilityKind):
            raise ValueError("provider must declare a CapabilityKind")
        providers = self._providers.setdefault(capability, [])
        provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__))
        if not any(item is provider for item in providers):
            providers.append(provider)
        if preferred or capability not in self._preferred:
            self._preferred[capability] = provider_name

    def list(self, capability: CapabilityKind | str) -> tuple[object, ...]:
        """Return all providers for a capability in registration order."""

        return tuple(self._providers.get(CapabilityKind(capability), ()))

    @staticmethod
    def _available(provider: object) -> bool:
        try:
            return bool(provider.status.available)
        except (AttributeError, TypeError):
            return False

    def get(self, capability: CapabilityKind | str) -> object | None:
        kind = CapabilityKind(capability)
        providers = self._providers.get(kind, [])
        if not providers:
            return None
        preferred_name = self._preferred.get(kind)
        preferred = next((item for item in providers if str(getattr(item, "provider_name", item.__class__.__name__)) == preferred_name), None)
        if preferred is not None and self._available(preferred):
            return preferred
        # Prefer a configured provider over an unavailable boundary while
        # retaining deterministic registration order.
        return next((item for item in providers if self._available(item)), preferred or providers[0])

    def status(self) -> dict[str, CapabilityStatus]:
        return {key.value: value.status for key, value in ((kind, self.get(kind)) for kind in self._providers) if value is not None}

    def public_status(self) -> dict[str, dict[str, object]]:
        return {key: value.public_dict() for key, value in self.status().items()}

    def all_status(self) -> dict[str, tuple[CapabilityStatus, ...]]:
        """Expose the complete inventory without exposing credentials."""

        return {
            kind.value: tuple(provider.status for provider in providers)
            for kind, providers in self._providers.items()
        }


def default_capability_registry(*, env: Mapping[str, str] | None = None) -> CapabilityRegistry:
    """Build the product's explicit capability inventory.

    Wan and the existing MPT stock runtime are intentionally separate
    capabilities.  Constructing this registry performs no network calls and
    never turns an absent credential into a live-model claim.
    """
    from .adapters import MPTProductionAdapter, WanProductionAdapter

    if env is None:
        wan_adapter = WanProductionAdapter()
    else:
        from .adapters.wan_video import WanProviderConfig

        wan_adapter = WanProductionAdapter(
            config=WanProviderConfig(
                api_key=str(env.get("DASHSCOPE_API_KEY", "")).strip(),
                base_url=str(env.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")).strip(),
                model=str(env.get("WAN_VIDEO_MODEL", "wan2.7-i2v-2026-04-25")).strip(),
            )
        )
    wan = RuntimeVideoProvider(wan_adapter, provider_name="WAN_VIDEO", mode=CapabilityKind.VIDEO_GENERATIVE)
    stock = RuntimeVideoProvider(MPTProductionAdapter(), provider_name="MPT_STOCK", mode=CapabilityKind.VIDEO_STOCK)
    return CapabilityRegistry([MPTLLMProvider(), wan, stock, UnavailableImageProvider(), UnavailableVisionProvider()])


__all__ = [
    "CapabilityKind", "CapabilityStatus", "CapabilityUnavailable", "CapabilityRegistry",
    "LLMProvider", "ImageGenerationProvider", "VideoGenerationProvider", "VisionAnalysisProvider",
    "ImageCandidate", "VisionAnalysis", "MPTLLMProvider", "RuntimeVideoProvider",
    "UnavailableImageProvider", "UnavailableVisionProvider", "DeterministicMockVisionProvider", "default_capability_registry",
]
