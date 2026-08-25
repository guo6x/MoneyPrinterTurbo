"""Product-level AI capability boundaries.

Provider-specific APIs stay behind these small contracts.  The registry only
publishes readiness metadata and never exposes credentials.  Image and Vision
implementations are intentionally unavailable by default; test code can use
the deterministic mock Vision provider without making a live-model claim.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
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
    TTS = "TTS"


@dataclass(frozen=True)
class CapabilityStatus:
    capability: CapabilityKind
    provider: str
    available: bool
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    configured: bool | None = None
    verified: bool = False

    def public_dict(self) -> dict[str, object]:
        """Return safe readiness metadata (never key/token values)."""
        return {
            "capability": self.capability.value,
            "provider": self.provider,
            "available": self.available,
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "configured": self.available if self.configured is None else self.configured,
            "verified": self.verified,
        }


class LLMProvider(ABC):
    capability = CapabilityKind.LLM
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def generate_structured(self, prompt: str, *, schema: Mapping[str, object] | None = None) -> dict[str, object]: ...

    def generate_json_text(self, prompt: str) -> str:
        """Return JSON text while keeping product calls on this capability seam."""
        return json.dumps(
            self.generate_structured(prompt),
            ensure_ascii=False,
            sort_keys=True,
        )

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
    def analyze(self, *, request: "VisionAnalysisRequest") -> "VisionAnalysis": ...


class TTSProvider(ABC):
    capability = CapabilityKind.TTS
    provider_name = "abstract"

    @property
    @abstractmethod
    def status(self) -> CapabilityStatus: ...

    @abstractmethod
    def synthesize(self, text: str, *, voice: str, language: str = "zh-CN", sample_rate: int = 48000) -> "TTSResult": ...


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


@dataclass(frozen=True)
class VisionMediaInput:
    """One immutable local input selected for a Vision request.

    ``path`` is deliberately excluded from :meth:`public_dict`; durable
    provenance records contain the canonical source id/hash, never a private
    absolute filesystem path.
    """

    source_kind: str
    source_id: str
    path: Path
    mime_type: str
    sha256: str
    role: str = ""
    time_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.source_kind.strip() or not self.source_id.strip():
            raise ValueError("Vision media source identity 不能为空")
        if not self.mime_type.strip():
            raise ValueError("Vision media MIME 不能为空")
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ValueError("Vision media SHA-256 无效")
        if self.time_seconds is not None and self.time_seconds < 0:
            raise ValueError("Vision frame timestamp 无效")

    def public_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "role": self.role,
        }
        if self.time_seconds is not None:
            value["time_seconds"] = self.time_seconds
        return value


@dataclass(frozen=True)
class VisionAnalysisRequest:
    """Provider-neutral, exact Vision input snapshot."""

    project_id: str
    execution_id: str
    artifact_id: str
    video: VisionMediaInput
    frames: tuple[VisionMediaInput, ...] = ()
    references: tuple[VisionMediaInput, ...] = ()
    frame_manifest_id: str | None = None
    generation_brief_hash: str | None = None
    prompt_template_version: str = "aidrama-vision-qc-v1"
    creative_context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.execution_id.strip() or not self.artifact_id.strip():
            raise ValueError("Vision request scope 不能为空")
        if self.video.source_kind != "VIDEO_ARTIFACT":
            raise ValueError("Vision request video source kind 无效")
        if self.generation_brief_hash is not None and (
            len(self.generation_brief_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.generation_brief_hash)
        ):
            raise ValueError("Vision GenerationBrief hash 无效")

    @property
    def reference_version_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.references)

    def public_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "execution_id": self.execution_id,
            "artifact_id": self.artifact_id,
            "frame_manifest_id": self.frame_manifest_id,
            "generation_brief_hash": self.generation_brief_hash,
            "prompt_template_version": self.prompt_template_version,
            "video": self.video.public_dict(),
            "frames": [item.public_dict() for item in self.frames],
            "references": [item.public_dict() for item in self.references],
        }


@dataclass(frozen=True)
class TTSResult:
    provider: str
    audio: bytes | None
    mime_type: str
    duration_seconds: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class MPTLLMProvider(LLMProvider):
    """Stable product boundary over the existing MPT LLM seam."""

    provider_name = "MPT_LLM"

    def __init__(self, config_snapshot: Mapping[str, object] | None = None):
        self._config_snapshot = dict(config_snapshot or snapshot_llm_config())

    @property
    def status(self) -> CapabilityStatus:
        from .ai import llm_configuration_status
        from app.models.llm_provider import get_llm_provider

        ready, reason = llm_configuration_status(self._config_snapshot)
        provider_id = str(self._config_snapshot.get("llm_provider", "")).strip().lower()
        provider = get_llm_provider(provider_id)
        model = "runtime"
        credential_reference = None
        endpoint_id = "unspecified"
        endpoint_class = "MPT_LLM_UNSPECIFIED"
        region = "UNSPECIFIED"
        if provider is not None:
            model = provider.resolve_model_name(
                str(self._config_snapshot.get(provider.config_key("model_name"), ""))
            ) or "runtime"
            resolved_base_url = provider.resolve_base_url(
                str(self._config_snapshot.get(provider.config_key("base_url"), ""))
            )
            credential_reference = (
                provider.config_key("api_key").upper()
                if provider.requires_api_key
                else None
            )
            service_endpoint = provider.find_service_endpoint(resolved_base_url)
            if provider_id == "moonshot" and service_endpoint is not None:
                endpoint_id = service_endpoint.endpoint_id
                region = (
                    "MAINLAND_CHINA"
                    if endpoint_id == "china"
                    else "INTERNATIONAL" if endpoint_id == "global" else "UNSPECIFIED"
                )
            else:
                standard_base_url = provider.effective_default_base_url.rstrip("/")
                resolved_standard = (
                    not provider.requires_base_url
                    or bool(standard_base_url)
                    and resolved_base_url.rstrip("/") == standard_base_url
                )
                if provider_id == "ollama":
                    endpoint_id, region = "local", "LOCAL"
                elif resolved_standard:
                    endpoint_id = "default"
                    if provider_id in {
                        "baidu", "deepseek", "hunyuan", "modelscope", "qwen",
                        "shengsuanyun", "siliconflow", "volcengine",
                    }:
                        region = "MAINLAND_CHINA"
                    elif provider_id in {
                        "azure", "cloudflare", "evolink", "gemini", "grok",
                        "groq", "minimax", "mimo", "openai", "pollinations",
                    }:
                        region = "INTERNATIONAL"
            endpoint_class = (
                f"MPT_LLM_{provider_id.upper()}_{endpoint_id.upper()}"
                if provider_id
                else "MPT_LLM_UNSPECIFIED"
            )
        metadata = {
            "model": model,
            "deployment_region": region,
            "endpoint_class": endpoint_class,
            "endpoint_profile_id": (
                f"runtime:LLM:MPT_LLM:{provider_id or 'unspecified'}:{endpoint_id}"
            ),
            "credential_reference": credential_reference,
            "upstream_provider_id": provider_id or "unspecified",
            "boundary_provider_id": self.provider_name,
            "configured": ready,
            "verification_state": "NOT_VERIFIED",
        }
        return CapabilityStatus(
            CapabilityKind.LLM, self.provider_name, ready, reason, metadata,
            configured=ready, verified=False,
        )

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

    def generate_json_text(self, prompt: str) -> str:
        if not self.status.available:
            raise CapabilityUnavailable(self.status.reason)
        try:
            return generate_text(prompt, self._config_snapshot)
        except AIDramaAIError as exc:
            raise CapabilityUnavailable(str(exc)) from exc

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
        metadata = {
            "mode": self.capability.value,
            "model": str(getattr(config, "model", getattr(self.adapter, "model_id", "runtime"))),
        }
        adapter_status = getattr(self.adapter, "status", None)
        if adapter_status is not None and hasattr(adapter_status, "metadata"):
            metadata.update(dict(adapter_status.metadata))
            reason = str(adapter_status.reason or reason)
            available = bool(adapter_status.available)
            explicit_configured = getattr(adapter_status, "configured", None)
            configured = configured if explicit_configured is None else bool(explicit_configured)
            verified = bool(getattr(adapter_status, "verified", False))
        else:
            available = configured
            verified = False
        defaults = {
            "WAN_VIDEO": ("MAINLAND_CHINA", "DASHSCOPE_CN", "DASHSCOPE_API_KEY"),
            "SEEDANCE": ("MAINLAND_CHINA", "ARK_CN_BEIJING", "ARK_API_KEY"),
            "MPT_STOCK": ("LOCAL", "MPT_LOCAL", None),
        }.get(self.provider_name.upper(), ("UNSPECIFIED", f"{self.provider_name.upper()}_RUNTIME", None))
        metadata.setdefault("deployment_region", defaults[0])
        metadata.setdefault("endpoint_class", defaults[1])
        metadata.setdefault("endpoint_profile_id", f"runtime:{self.capability.value}:{self.provider_name}:{defaults[1]}")
        if defaults[2]:
            metadata.setdefault("credential_reference", defaults[2])
        metadata["configured"] = configured
        metadata.setdefault("verification_state", "NOT_VERIFIED")
        return CapabilityStatus(
            self.capability, self.provider_name, available, reason, metadata,
            configured=configured, verified=verified,
        )

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

    def analyze(self, *, request: VisionAnalysisRequest) -> VisionAnalysis:
        raise CapabilityUnavailable(self.status.reason)


class UnavailableTTSProvider(TTSProvider):
    provider_name = "UNCONFIGURED_TTS"

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(CapabilityKind.TTS, self.provider_name, False, "no TTS provider configured")

    def synthesize(self, text: str, *, voice: str, language: str = "zh-CN", sample_rate: int = 48000) -> TTSResult:
        raise CapabilityUnavailable(self.status.reason)


class MPTTTSProvider(TTSProvider):
    """Canonical boundary over the existing MPT voice implementation.

    TTS is opt-in so opening a page never causes a remote request.  The
    provider only returns redacted metadata; credentials remain in the
    existing provider configuration/environment seam.
    """

    provider_name = "MPT_TTS"

    def __init__(self, *, enabled: bool | None = None, voice: str | None = None, voice_rate: float = 1.0, voice_volume: float = 1.0):
        self.enabled = (os.environ.get("AIDRAMA_TTS_ENABLED", "") == "1") if enabled is None else bool(enabled)
        self.voice = voice or os.environ.get("AIDRAMA_TTS_VOICE", "zh-CN-XiaoxiaoMultilingualNeural-V2-Female")
        self.voice_rate = float(voice_rate)
        self.voice_volume = float(voice_volume)

    @property
    def status(self) -> CapabilityStatus:
        try:
            import app.services.voice  # noqa: F401
        except Exception:
            return CapabilityStatus(CapabilityKind.TTS, self.provider_name, False, "MPT TTS seam unavailable", configured=False)
        if not self.enabled:
            return CapabilityStatus(CapabilityKind.TTS, self.provider_name, False, "TTS 未启用；设置 AIDRAMA_TTS_ENABLED=1 后才会调用语音服务", {"voice": self.voice, "deployment_region": "LOCAL", "endpoint_class": "MPT_LOCAL_TTS"}, configured=False)
        return CapabilityStatus(CapabilityKind.TTS, self.provider_name, True, "configured", {"voice": self.voice, "model": "MPT voice seam", "deployment_region": "LOCAL", "endpoint_class": "MPT_LOCAL_TTS", "configured": True, "verification_state": "NOT_VERIFIED"}, configured=True)

    def synthesize(self, text: str, *, voice: str, language: str = "zh-CN", sample_rate: int = 48000) -> TTSResult:
        if not self.status.available:
            raise CapabilityUnavailable(self.status.reason)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text 不能为空")
        selected_voice = str(voice or self.voice).strip()
        if not selected_voice:
            raise ValueError("voice 不能为空")
        from app.services.voice import tts

        descriptor, filename = tempfile.mkstemp(prefix="aidrama-tts-", suffix=".mp3")
        os.close(descriptor)
        path = Path(filename)
        try:
            result = tts(text, selected_voice, self.voice_rate, str(path), self.voice_volume)
            if not path.is_file() or path.stat().st_size <= 0:
                raise CapabilityUnavailable("TTS provider returned no audio")
            duration = getattr(result, "audio_duration_seconds", None) if result is not None else None
            return TTSResult(self.provider_name, path.read_bytes(), "audio/mpeg", duration, {"voice": selected_voice, "language": language, "sample_rate": sample_rate})
        finally:
            try:
                path.unlink()
            except OSError:
                pass


class DeterministicMockVisionProvider(VisionAnalysisProvider):
    """Deterministic fake used only for unit tests and local decision plumbing."""

    provider_name = "MOCK_VISION"

    def __init__(self, metrics: Mapping[str, Mapping[str, object]] | None = None):
        self._metrics = dict(metrics or {"SHOT_ALIGNMENT": {"score": 1.0, "status": "PASS"}})

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(CapabilityKind.VISION, self.provider_name, True, "deterministic test provider", {"test_only": True})

    def analyze(self, *, request: VisionAnalysisRequest) -> VisionAnalysis:
        reference_ids = list(request.reference_version_ids)
        return VisionAnalysis(
            self.provider_name,
            self._metrics,
            metadata={
                "model": "deterministic-vision-v1",
                "test_only": True,
                "prompt_template_sha256": "0" * 64,
                "reference_comparison": {
                    "compared_reference_version_ids": reference_ids,
                    "findings": [],
                },
                "input_provenance": request.public_dict(),
            },
        )


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
    from .adapters import MPTProductionAdapter, SeedanceProductionAdapter, WanProductionAdapter
    from .providers import GeminiVisionProvider, OpenAIImageProvider

    values = dict(os.environ if env is None else env)
    if env is None:
        try:
            from .credentials import WindowsCredentialStore
            from aidrama_studio.storage.database import get_default_paths
            store = WindowsCredentialStore(get_default_paths().root)
            for key in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "ARK_API_KEY", "GEMINI_API_KEY"):
                secret = store.get(key)
                if secret:
                    values[key] = secret
        except Exception:
            # Environment configuration remains a development fallback. A
            # locked/corrupt credential store must not prevent offline use.
            pass
    from .adapters.wan_video import WanProviderConfig
    from .adapters.seedance_video import SeedanceProviderConfig

    wan_adapter = WanProductionAdapter(
        config=WanProviderConfig(
            api_key=str(values.get("DASHSCOPE_API_KEY", "")).strip(),
            base_url=str(values.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")).strip(),
            model=str(values.get("WAN_VIDEO_MODEL", "wan2.7-i2v-2026-04-25")).strip(),
        )
    )
    wan = RuntimeVideoProvider(wan_adapter, provider_name="WAN_VIDEO", mode=CapabilityKind.VIDEO_GENERATIVE)
    seedance = RuntimeVideoProvider(SeedanceProductionAdapter(config=SeedanceProviderConfig(
        api_key=str(values.get("ARK_API_KEY", "")).strip(),
        base_url=str(values.get("SEEDANCE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")).strip(),
        model=str(
            values.get("SEEDANCE_VIDEO_MODEL", "doubao-seedance-2-5-260628")
        ).strip(),
        allow_paid_live_tests=str(values.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "")) == "1",
    )), provider_name="SEEDANCE", mode=CapabilityKind.VIDEO_GENERATIVE)
    stock = RuntimeVideoProvider(MPTProductionAdapter(), provider_name="MPT_STOCK", mode=CapabilityKind.VIDEO_STOCK)
    # Preserve the existing Wan capability as the default compatibility
    # provider; a configured Seedance profile is selected explicitly through
    # ProviderProfileService without hiding the preserved Wan boundary.
    return CapabilityRegistry(
        [
            MPTLLMProvider(),
            wan,
            seedance,
            stock,
            OpenAIImageProvider(env=values),
            GeminiVisionProvider(env=values),
            MPTTTSProvider(),
        ]
    )


__all__ = [
    "CapabilityKind", "CapabilityStatus", "CapabilityUnavailable", "CapabilityRegistry",
    "LLMProvider", "ImageGenerationProvider", "VideoGenerationProvider", "VisionAnalysisProvider", "TTSProvider",
    "ImageCandidate", "VisionAnalysis", "VisionAnalysisRequest", "VisionMediaInput", "TTSResult", "MPTLLMProvider", "RuntimeVideoProvider",
    "UnavailableImageProvider", "UnavailableVisionProvider", "UnavailableTTSProvider", "MPTTTSProvider", "DeterministicMockVisionProvider", "default_capability_registry",
]
