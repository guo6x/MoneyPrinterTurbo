"""Product-level AI capability boundaries.

Provider-specific APIs stay behind these small contracts.  The registry only
publishes readiness metadata and never exposes credentials.  Image and Vision
implementations are intentionally unavailable by default; test code can use
the deterministic mock Vision provider without making a live-model claim.
"""

from __future__ import annotations

import json
import importlib
import os
import re
import tempfile
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from aidrama_studio.domain import ProductionInputSnapshot

from .ai import AIDramaAIError, generate_text, snapshot_llm_config
from .adapters.production_adapter import ProductionRuntimeAdapter, RuntimeSubmission


class CapabilityUnavailable(RuntimeError):
    """Raised when a provider boundary is known but not configured."""


def _safe_status_metadata(value: object) -> dict[str, object]:
    """Return a recursive, public-only projection of provider metadata."""

    secret_markers = {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "private_key",
        "signed_url",
        "raw_body",
        "authorization",
        "authorization_header",
    }

    def clean(item: object, key: str | None = None) -> object:
        lowered_key = key.casefold() if key is not None else ""
        if lowered_key in secret_markers or any(
            lowered_key.endswith("_" + marker)
            for marker in secret_markers
            if marker not in {"authorization", "authorization_header"}
        ):
            return "<redacted>"
        if isinstance(item, str):
            lowered = item.casefold()
            if (
                item.startswith(("sk-", "rk-", "sess-"))
                or "bearer " in lowered
                or "-----begin " in lowered
                or re.search(
                    r"[?&](?:token|sig|signature|x-amz-signature|access[_-]?key|api[_-]?key|credential|auth|expires)=",
                    lowered,
                )
            ):
                return "<redacted>"
            return item
        if isinstance(item, Mapping):
            return {str(raw_key): clean(child, str(raw_key)) for raw_key, child in item.items()}
        if isinstance(item, (tuple, list, set, frozenset)):
            return [clean(child) for child in item]
        return item

    result = clean(value)
    return result if isinstance(result, dict) else {}


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
    # Universal-runtime readiness dimensions.  These are additive projections
    # for legacy providers; ``available`` remains the compatibility field used
    # by existing UI/runtime code.  In particular, a paid provider may be
    # configured and runtime-available while create authorization is pending.
    runtime_available: bool | None = None
    create_authorized: bool | None = None
    authorization_required: bool | None = None
    # Internal provenance bit used by the universal-runtime bridge.  Legacy
    # providers omit ``runtime_available`` and rely on ``available``; an
    # explicitly supplied ``False`` must not be mistaken for that legacy
    # shape and promoted to ``True`` merely because a paid-create marker is
    # present.  It is intentionally excluded from equality/repr/public data.
    runtime_available_explicit: bool = field(
        default=False, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        reason_lower = str(self.reason or "").casefold()
        paid_authorization_marker = any(
            marker in reason_lower for marker in ("paid", "authoriz", "授权", "费用")
        )
        configured_value = self.configured
        if configured_value is None:
            configured_value = metadata.get("configured", self.available)
        object.__setattr__(self, "configured", configured_value is True)
        object.__setattr__(self, "verified", self.verified is True)
        runtime_value = self.runtime_available
        runtime_explicit = runtime_value is not None or "runtime_available" in metadata
        if runtime_value is None:
            runtime_value = metadata.get("runtime_available")
        if runtime_value is None:
            # A legacy status has no separate runtime signal, so preserve its
            # historical availability value as the best compatibility
            # projection.  New bridges can pass the explicit field.
            runtime_value = self.available
        object.__setattr__(self, "runtime_available_explicit", runtime_explicit)
        object.__setattr__(self, "runtime_available", runtime_value is True)

        required_value = self.authorization_required
        required_explicit = required_value is not None
        if required_value is None:
            if "authorization_required" in metadata:
                required_value = metadata["authorization_required"]
                required_explicit = True
            elif "requires_create_authorization" in metadata:
                required_value = metadata["requires_create_authorization"]
                required_explicit = True
        if not required_explicit:
            # Some existing paid adapters expose only a human-readable reason
            # and ``live_authorized``.  Infer the *need for a gate* from that
            # explicit marker, never from credential/configuration presence.
            required_value = bool(
                metadata.get("create_is_paid") is True
                or paid_authorization_marker
                and metadata.get("live_authorized") is not None
            )
        elif not isinstance(required_value, bool):
            # A malformed value crossing a JSON/configuration boundary must
            # never disable a paid-create gate.  Keep the status readable but
            # fail closed until an explicit boolean is supplied.
            required_value = True
        object.__setattr__(self, "authorization_required", required_value is True)

        if (
            not runtime_explicit
            and self.authorization_required
            and configured_value is True
            and paid_authorization_marker
        ):
            # ``available`` remains the legacy create-ready projection, while
            # this additive field records that read-only runtime operations
            # remain reachable before paid-create approval.
            runtime_value = True
        object.__setattr__(self, "runtime_available", runtime_value is True)

        authorized_value = self.create_authorized
        if authorized_value is None:
            # Canonical readiness declarations are always authoritative.
            # ``live_authorized``/``allow_paid_live_tests`` are legacy paid
            # smoke hints, however, and must not override an explicit
            # ``authorization_required=False`` declaration (a stale
            # ``live_authorized=False`` marker is common on free endpoints).
            for key in (
                "create_authorized",
                "authorized",
                "approved",
            ):
                if key in metadata:
                    authorized_value = metadata[key]
                    break
        if authorized_value is None and not (
            required_explicit and required_value is False
        ):
            for key in ("live_authorized", "allow_paid_live_tests"):
                if key in metadata:
                    authorized_value = metadata[key]
                    break
        # Authorization is never inferred from configuration/credential
        # presence.  Non-gated capabilities are trivially create-authorized;
        # gated capabilities default to false until an explicit signal exists.
        if authorized_value is None:
            authorized_value = not self.authorization_required
        object.__setattr__(self, "create_authorized", authorized_value is True)

    def public_dict(self) -> dict[str, object]:
        """Return safe readiness metadata (never key/token values)."""
        configured = self.configured
        if configured is None:
            configured = (
                self.metadata.get("configured", self.available)
                if isinstance(self.metadata, Mapping)
                else self.available
            )
        return {
            "capability": self.capability.value,
            "provider": self.provider,
            "available": self.available,
            "reason": self.reason,
            "metadata": _safe_status_metadata(self.metadata),
            "configured": configured,
            "verified": self.verified,
            "runtime_available": self.runtime_available,
            "create_authorized": self.create_authorized,
            "authorization_required": self.authorization_required,
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

    def synthesize_live_smoke(
        self,
        text: str,
        *,
        voice: str,
        language: str = "zh-CN",
        sample_rate: int = 48000,
    ) -> "TTSResult":
        """Run a provider-specific, single-submission live smoke when supported.

        The default is deliberately fail-closed: a provider must prove that it
        can suppress internal paid retries before the acceptance path may use
        it.
        """

        raise CapabilityUnavailable(
            "selected TTS provider does not expose a bounded live-smoke path"
        )


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


def _freeze_vision_value(value: object) -> object:
    """Deep-freeze one already-sanitized Vision input snapshot."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_vision_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_vision_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("Vision creative context 必须是可持久化数据")


def _thaw_vision_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_vision_value(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_vision_value(item) for item in value]
    return value


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
        object.__setattr__(
            self,
            "creative_context",
            _freeze_vision_value(dict(self.creative_context)),
        )

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
            "creative_context": _thaw_vision_value(self.creative_context),
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

    def __init__(
        self,
        config_snapshot: Mapping[str, object] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ):
        self._config_snapshot = dict(
            snapshot_llm_config() if config_snapshot is None else config_snapshot
        )
        runtime_env = dict(os.environ if env is None else env)
        # Alibaba Model Studio has one canonical credential identity.  The
        # legacy MPT qwen client still names its transient config field
        # ``qwen_api_key``; hydrate that in memory from DASHSCOPE_API_KEY (or
        # the explicit legacy alias) without writing config.toml or requiring
        # the user to store the same secret twice.
        if str(self._config_snapshot.get("llm_provider", "")).strip().casefold() == "qwen":
            canonical = str(runtime_env.get("DASHSCOPE_API_KEY", "") or "").strip()
            legacy_alias = str(runtime_env.get("QWEN_API_KEY", "") or "").strip()
            if not str(self._config_snapshot.get("qwen_api_key", "") or "").strip():
                self._config_snapshot["qwen_api_key"] = canonical or legacy_alias
        # Live-smoke authorization is an acceptance-only boundary.  Keep it
        # separate from the mature LLM configuration/readiness path so normal
        # creative generation remains backward compatible.  The registry
        # passes its frozen environment explicitly; direct callers retain the
        # process-environment fallback for compatibility.
        self._allow_paid_live_tests = (
            str((os.environ if env is None else env).get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", ""))
            == "1"
        )

    @property
    def status(self) -> CapabilityStatus:
        from .ai import llm_configuration_status
        from app.models.llm_provider import get_llm_provider

        ready, reason = llm_configuration_status(self._config_snapshot)
        provider_id = str(self._config_snapshot.get("llm_provider", "")).strip().lower()
        provider = get_llm_provider(provider_id)
        model = "runtime"
        credential_reference = None
        credential_present: bool | None = None
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
                "DASHSCOPE_API_KEY"
                if provider_id == "qwen" and provider.requires_api_key
                else (
                    provider.config_key("api_key").upper()
                    if provider.requires_api_key
                    else None
                )
            )
            if credential_reference is not None:
                credential_present = bool(
                    str(
                        self._config_snapshot.get(
                            provider.config_key("api_key"), ""
                        )
                    ).strip()
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
            "credential_present": credential_present,
            "upstream_provider_id": provider_id or "unspecified",
            "boundary_provider_id": self.provider_name,
            "configured": ready,
            "verification_state": "NOT_VERIFIED",
        }
        # Local LLMs (for example Ollama) do not incur a remote paid request,
        # so they intentionally omit this field.  Every non-local endpoint is
        # explicit about the acceptance-only authorization state; normal
        # readiness remains governed by ``llm_configuration_status`` above.
        if region != "LOCAL":
            metadata["live_authorized"] = self._allow_paid_live_tests
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


class MainlandUniversalLLMProvider(LLMProvider):
    """Exact Alibaba LLM binding over the canonical manifest runtime.

    Explicit Forge MAX qwen selection reaches ``DashScopeQwenChatCodec`` and
    ``RequestResponseDriver`` through this boundary instead of being projected
    back through the legacy MPT/config.toml seam. Secret values remain only in
    process memory and never enter status, RuntimePlan, persistence, or logs.
    """

    provider_name = "alibaba_model_studio"

    def __init__(
        self,
        *,
        credentials: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        runtime_factory: object | None = None,
        sessions: Mapping[str, object] | None = None,
    ) -> None:
        values = dict(os.environ if env is None else env)
        supplied = dict(credentials or {})
        canonical = str(
            supplied.get("DASHSCOPE_API_KEY")
            or values.get("DASHSCOPE_API_KEY")
            or ""
        ).strip()
        if not canonical:
            # Backward-compatible read alias only. The public identity remains
            # DASHSCOPE_API_KEY and no duplicate value is persisted.
            canonical = str(
                supplied.get("QWEN_API_KEY")
                or values.get("QWEN_API_KEY")
                or ""
            ).strip()
        self._credentials = {"DASHSCOPE_API_KEY": canonical}
        self._allow_paid_live_tests = (
            str(values.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "")) == "1"
        )
        self._workspace_base_url = str(
            values.get("DASHSCOPE_WORKSPACE_BASE_URL", "") or ""
        ).strip()
        self._runtime_factory = runtime_factory
        self._sessions = dict(sessions or {})

    @property
    def status(self) -> CapabilityStatus:
        configured = bool(self._credentials["DASHSCOPE_API_KEY"])
        return CapabilityStatus(
            CapabilityKind.LLM,
            self.provider_name,
            configured,
            "configured" if configured else "DASHSCOPE_API_KEY is not configured",
            {
                "model": "qwen-max",
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "DASHSCOPE_CN",
                "endpoint_profile_id": "DASHSCOPE_CN_BEIJING_V1",
                "credential_reference": "DASHSCOPE_API_KEY",
                "credential_present": configured,
                "configured": configured,
                "runtime_available": True,
                "verification_state": "NOT_VERIFIED",
                "live_authorized": self._allow_paid_live_tests,
                "upstream_provider_id": self.provider_name,
                "boundary_provider_id": self.provider_name,
                "native_universal_runtime": True,
            },
            configured=configured,
            verified=False,
            runtime_available=True,
        )

    def generate_structured(
        self, prompt: str, *, schema: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        del prompt, schema
        raise CapabilityUnavailable(
            "Alibaba LLM requires the project-scoped Universal Runtime"
        )

    def invoke_universal_text(
        self,
        selection: object,
        prompt: str,
        *,
        project_id: str,
    ) -> str:
        from uuid import uuid4

        from .model_runtime import (
            CapabilityKind as UniversalCapabilityKind,
            CapabilityRequest,
            CapabilityResult,
            MainlandProviderRuntime,
        )

        resolved = getattr(selection, "resolved", None)
        manifest = getattr(resolved, "manifest", None)
        if manifest is None:
            raise CapabilityUnavailable("frozen qwen manifest is required")
        if (
            str(getattr(manifest, "provider_id", "")) != self.provider_name
            or str(getattr(manifest, "model_id", "")) != "qwen-max"
            or str(getattr(manifest, "credential_reference", ""))
            != "DASHSCOPE_API_KEY"
        ):
            raise CapabilityUnavailable(
                "selected LLM is not the canonical qwen-max manifest"
            )
        credential = self._credentials["DASHSCOPE_API_KEY"]
        if not credential:
            raise CapabilityUnavailable(
                "DASHSCOPE_API_KEY is not configured; no Provider call was attempted"
            )
        runtime_type = self._runtime_factory or MainlandProviderRuntime
        options: dict[str, object] = {
            "credentials": {"DASHSCOPE_API_KEY": credential},
            "create_authorized": True,
            "sessions": self._sessions,
        }
        if self._workspace_base_url:
            options["dashscope_workspace_base_url"] = self._workspace_base_url
        runtime = runtime_type(**options)
        binding = runtime.binding_for(str(getattr(manifest, "id", "")))
        if binding.manifest.manifest_hash != getattr(manifest, "manifest_hash", None):
            raise CapabilityUnavailable("qwen manifest contract changed after selection")
        request = CapabilityRequest(
            request_id=uuid4().hex,
            project_id=project_id,
            capability=UniversalCapabilityKind.LLM,
            protocol_family=binding.manifest.protocol,
            provider_id=binding.manifest.provider_id,
            model_id=binding.manifest.model_id,
            manifest_id=binding.manifest.id,
            manifest_hash=binding.manifest.manifest_hash,
            codec_id=binding.manifest.codec_id,
            prompt_or_text=prompt,
            create_authorized=True,
            authorization_required=True,
        )
        result = runtime.submit(
            request,
            authorization={"create_authorized": True},
        )
        if not isinstance(result, CapabilityResult) or not result.succeeded:
            raise CapabilityUnavailable(
                "qwen Universal Runtime did not return success"
            )
        text = result.safe_metadata.get("text")
        if not isinstance(text, str) or not text.strip():
            raise CapabilityUnavailable("qwen Universal Runtime returned empty text")
        return text.strip()


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
        declared_profile = getattr(self.adapter, "runtime_profile_metadata", None)
        if isinstance(declared_profile, Mapping):
            metadata.update(dict(declared_profile))
        adapter_status = getattr(self.adapter, "status", None)
        if adapter_status is not None and hasattr(adapter_status, "metadata"):
            metadata.update(dict(adapter_status.metadata))
            reason = str(adapter_status.reason or reason)
            available = adapter_status.available is True
            explicit_configured = getattr(adapter_status, "configured", None)
            configured = (
                configured
                if explicit_configured is None
                else explicit_configured is True
            )
            verified = getattr(adapter_status, "verified", False) is True
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
            metadata.setdefault("credential_present", configured)
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

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        voice: str | None = None,
        voice_rate: float = 1.0,
        voice_volume: float = 1.0,
        allow_paid_live_tests: bool | None = None,
        env: Mapping[str, str] | None = None,
        azure_config: Mapping[str, object] | None = None,
        azure_speech_available: bool | None = None,
    ):
        values = os.environ if env is None else env
        self._env = values
        self._explicit_env = env is not None
        self.enabled = (
            str(values.get("AIDRAMA_TTS_ENABLED", "")) == "1"
            if enabled is None
            else bool(enabled)
        )
        self.voice = (
            str(voice)
            if voice is not None
            else str(
                values.get(
                    "AIDRAMA_TTS_VOICE",
                    "zh-CN-XiaoxiaoMultilingualNeural-V2-Female",
                )
            )
        )
        self.voice_rate = float(voice_rate)
        self.voice_volume = float(voice_volume)
        self.allow_paid_live_tests = (
            str(values.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "")) == "1"
            if allow_paid_live_tests is None
            else bool(allow_paid_live_tests)
        )
        self._azure_config = dict(azure_config) if azure_config is not None else None
        self._azure_speech_available = azure_speech_available

    @property
    def status(self) -> CapabilityStatus:
        return self._status_for_voice(self.voice)

    def _status_for_voice(self, selected_voice: str) -> CapabilityStatus:
        try:
            from app.services import voice as voice_runtime
        except Exception:
            return CapabilityStatus(CapabilityKind.TTS, self.provider_name, False, "MPT TTS seam unavailable", configured=False)

        voice_value = str(selected_voice or "").strip()
        is_azure_v2 = bool(voice_runtime.is_azure_v2_voice(voice_value))
        if is_azure_v2:
            azure = self._azure_settings()
            speech_key_present = bool(str(azure.get("speech_key") or "").strip())
            speech_region = str(azure.get("speech_region") or "").strip()
            runtime_ready = self._azure_runtime_available()
            mainland = speech_region.casefold().startswith("china")
            deployment_region = "MAINLAND_CHINA" if mainland else "INTERNATIONAL"
            endpoint_class = (
                "AZURE_SPEECH_CHINA" if mainland else "AZURE_SPEECH_PUBLIC"
            )
            configured = bool(
                self.enabled
                and voice_value
                and speech_key_present
                and speech_region
                and runtime_ready
            )
            # Normal product readiness reflects whether Azure TTS can run.
            # The acceptance-only paid authorization is enforced separately
            # by ``synthesize_live_smoke`` and the offline live preflight.
            available = configured
            if not self.enabled:
                reason = "TTS 未启用；设置 AIDRAMA_TTS_ENABLED=1 后才会调用语音服务"
            elif not voice_value:
                reason = "Azure TTS voice 未选择"
            elif not speech_key_present:
                reason = "Azure Speech credential unavailable"
            elif not speech_region:
                reason = "Azure Speech region unavailable"
            elif not runtime_ready:
                reason = "Azure Speech runtime unavailable"
            else:
                reason = "configured"
            return CapabilityStatus(
                CapabilityKind.TTS,
                self.provider_name,
                available,
                reason,
                {
                    "voice": voice_value,
                    "model": "AZURE_SPEECH_NEURAL_TTS",
                    "upstream_provider_id": "AZURE_SPEECH",
                    "deployment_region": deployment_region,
                    "service_region": speech_region,
                    "endpoint_class": endpoint_class,
                    "endpoint_profile_id": (
                        f"runtime:TTS:MPT_TTS:{endpoint_class}"
                    ),
                    "credential_reference": "AZURE_SPEECH_KEY",
                    "credential_present": speech_key_present,
                    "region_configured": bool(speech_region),
                    "runtime_ready": runtime_ready,
                    "live_authorized": self.allow_paid_live_tests,
                    "configured": configured,
                    "verification_state": "NOT_VERIFIED",
                },
                configured=configured,
                verified=False,
            )

        # Legacy Edge voices are also remote Microsoft speech, not LOCAL.
        # They do not use the Azure subscription key/region pair.
        if voice_value and not any(
            checker(voice_value)
            for checker in (
                voice_runtime.is_siliconflow_voice,
                voice_runtime.is_gemini_voice,
                voice_runtime.is_mimo_voice,
                voice_runtime.is_minimax_voice,
                voice_runtime.is_elevenlabs_voice,
                voice_runtime.is_chatterbox_voice,
                voice_runtime.is_no_voice,
            )
        ):
            configured = bool(self.enabled and voice_value)
            available = configured
            reason = (
                "configured"
                if available
                else "TTS 未启用或 voice 未选择"
            )
            return CapabilityStatus(
                CapabilityKind.TTS,
                self.provider_name,
                available,
                reason,
                {
                    "voice": voice_value,
                    "model": "MICROSOFT_EDGE_TTS",
                    "upstream_provider_id": "MICROSOFT_EDGE_TTS",
                    "deployment_region": "INTERNATIONAL",
                    "endpoint_class": "MICROSOFT_EDGE_TTS_PUBLIC",
                    "endpoint_profile_id": "runtime:TTS:MPT_TTS:MICROSOFT_EDGE_TTS_PUBLIC",
                    "live_authorized": self.allow_paid_live_tests,
                    "configured": configured,
                    "verification_state": "NOT_VERIFIED",
                },
                configured=configured,
                verified=False,
            )

        # Other MPT voice backends retain their existing runtime seam, but the
        # AIDrama readiness surface does not claim them ready without a
        # provider-specific credential/runtime check.
        return CapabilityStatus(
            CapabilityKind.TTS,
            self.provider_name,
            False,
            "selected TTS backend has no AIDrama readiness verifier",
            {
                "voice": voice_value,
                "model": "MPT voice seam",
                "deployment_region": "UNSPECIFIED",
                "endpoint_class": "MPT_TTS_UNVERIFIED",
                "endpoint_profile_id": "runtime:TTS:MPT_TTS:MPT_TTS_UNVERIFIED",
                "live_authorized": self.allow_paid_live_tests,
                "configured": False,
                "verification_state": "NOT_VERIFIED",
            },
            configured=False,
            verified=False,
        )

    def _azure_settings(self) -> Mapping[str, object]:
        if self._azure_config is not None:
            configured = self._azure_config
        elif self._explicit_env:
            # An injected environment is a deterministic boundary. Do not
            # silently merge a host's persisted Azure credentials into an
            # offline/test readiness check.
            configured = {}
        else:
            try:
                from app.config import config as mpt_config

                configured = mpt_config.azure
            except Exception:
                configured = {}
        return {
            "speech_key": str(
                self._env.get("AZURE_SPEECH_KEY", "")
                or configured.get("speech_key", "")
            ).strip(),
            "speech_region": str(
                self._env.get("AZURE_SPEECH_REGION", "")
                or configured.get("speech_region", "")
            ).strip(),
        }

    def _azure_runtime_available(self) -> bool:
        if self._azure_speech_available is not None:
            return bool(self._azure_speech_available)
        try:
            # Importing the SDK is local and side-effect free; it proves more
            # than ``find_spec`` alone, which can report a broken install as
            # ready while the actual runtime import would fail.
            return importlib.import_module("azure.cognitiveservices.speech") is not None
        except Exception:
            return False

    def synthesize(self, text: str, *, voice: str, language: str = "zh-CN", sample_rate: int = 48000) -> TTSResult:
        return self._synthesize(
            text,
            voice=voice,
            language=language,
            sample_rate=sample_rate,
            max_attempts=None,
        )

    def synthesize_live_smoke(
        self,
        text: str,
        *,
        voice: str,
        language: str = "zh-CN",
        sample_rate: int = 48000,
    ) -> TTSResult:
        """Synthesize once with no automatic second paid submission."""

        if not self.allow_paid_live_tests:
            raise CapabilityUnavailable(
                "TTS live smoke requires AIDRAMA_ALLOW_PAID_LIVE_TESTS=1"
            )
        return self._synthesize(
            text,
            voice=voice,
            language=language,
            sample_rate=sample_rate,
            max_attempts=1,
        )

    def _synthesize(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        sample_rate: int,
        max_attempts: int | None,
    ) -> TTSResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text 不能为空")
        selected_voice = str(voice or self.voice).strip()
        if not selected_voice:
            raise ValueError("voice 不能为空")
        status = self._status_for_voice(selected_voice)
        if not status.available:
            raise CapabilityUnavailable(status.reason)
        from app.services.voice import tts

        descriptor, filename = tempfile.mkstemp(prefix="aidrama-tts-", suffix=".mp3")
        os.close(descriptor)
        path = Path(filename)
        try:
            if max_attempts is None:
                result = tts(
                    text,
                    selected_voice,
                    self.voice_rate,
                    str(path),
                    self.voice_volume,
                )
            else:
                result = tts(
                    text,
                    selected_voice,
                    self.voice_rate,
                    str(path),
                    self.voice_volume,
                    max_attempts=max_attempts,
                )
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
            status = provider.status
            metadata = status.metadata
            if not isinstance(metadata, Mapping):
                return False
            if metadata.get("requires_explicit_selection") is True:
                return False
            return status.available is True
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
    from .adapters import (
        MainlandWanProductionAdapter,
        MainlandSeedanceProductionAdapter,
        MPTProductionAdapter,
    )
    from .providers import (
        GeminiVisionProvider,
        OpenAIImageProvider,
        build_universal_vision_providers,
    )
    from aidrama_studio.storage.repositories import ProjectRepository

    values = dict(os.environ if env is None else env)
    credential_store = None
    if env is None:
        try:
            from .credentials import WindowsCredentialStore
            from aidrama_studio.storage.database import get_default_paths
            store = WindowsCredentialStore(get_default_paths().root)
            credential_store = store
            for key in (
                "OPENAI_API_KEY",
                "DASHSCOPE_API_KEY",
                "DASHSCOPE_WORKSPACE_BASE_URL",
                "ARK_API_KEY",
                "GEMINI_API_KEY",
            ):
                secret = store.get(key)
                if secret:
                    values[key] = secret
        except Exception:
            # Environment configuration remains a development fallback. A
            # locked/corrupt credential store must not prevent offline use.
            pass

    class _RegistryCredentialStore:
        def __init__(self, snapshot: Mapping[str, str]) -> None:
            self._snapshot = dict(snapshot)

        def get(self, provider_id: str) -> str | None:
            return str(self._snapshot.get(provider_id, "") or "") or None

        def configured_providers(self) -> tuple[str, ...]:
            return tuple(
                sorted(key for key, value in self._snapshot.items() if value)
            )

    if credential_store is None:
        credential_store = _RegistryCredentialStore(values)
    wan_adapter = MainlandWanProductionAdapter(env=values)
    wan = RuntimeVideoProvider(wan_adapter, provider_name="WAN_VIDEO", mode=CapabilityKind.VIDEO_GENERATIVE)
    seedance = RuntimeVideoProvider(
        MainlandSeedanceProductionAdapter(env=values),
        provider_name="SEEDANCE",
        mode=CapabilityKind.VIDEO_GENERATIVE,
    )
    stock = RuntimeVideoProvider(MPTProductionAdapter(), provider_name="MPT_STOCK", mode=CapabilityKind.VIDEO_STOCK)
    # Preserve the existing Wan capability as the default compatibility
    # provider; a configured Seedance profile is selected explicitly through
    # ProviderProfileService without hiding the preserved Wan boundary.
    llm_snapshot = _llm_snapshot_for_registry_environment(values, explicit=env is not None)
    tts_provider = (
        MPTTTSProvider() if env is None else MPTTTSProvider(env=values)
    )
    vision_env = {
        key: str(values.get(key, "") or "")
        for key in (
            "AIDRAMA_ALLOW_PAID_LIVE_TESTS",
            "DASHSCOPE_WORKSPACE_BASE_URL",
        )
    }
    universal_vision = build_universal_vision_providers(
        ProjectRepository(),
        credential_store=credential_store,
        env=vision_env,
    )
    return CapabilityRegistry(
        [
            MainlandUniversalLLMProvider(
                credentials={
                    "DASHSCOPE_API_KEY": str(
                        values.get("DASHSCOPE_API_KEY", "") or ""
                    )
                },
                env=values,
            ),
            MPTLLMProvider(config_snapshot=llm_snapshot, env=values),
            wan,
            seedance,
            stock,
            OpenAIImageProvider(env=values),
            *universal_vision,
            GeminiVisionProvider(env=values),
            tts_provider,
        ]
    )


def _llm_snapshot_for_registry_environment(
    values: Mapping[str, str],
    *,
    explicit: bool,
) -> Mapping[str, object] | None:
    """Freeze LLM settings when a caller supplies an explicit environment.

    The existing MPT LLM seam stores its settings in ``config.toml`` rather
    than in process environment variables.  Other capability adapters accept
    an injected mapping for deterministic/offline checks, so silently reading
    ambient LLM credentials in that mode would make a preflight claim depend
    on the host.  Preserve non-secret config defaults, clear ambient secret
    fields, and overlay conventional provider environment aliases.
    """

    if not explicit:
        return None
    from .ai import snapshot_llm_config
    from app.models.llm_provider import get_llm_provider

    snapshot = dict(snapshot_llm_config())
    secret_markers = (
        "api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
    )
    for key in list(snapshot):
        if any(marker in str(key).casefold() for marker in secret_markers):
            snapshot[key] = ""

    normalized = {str(key).casefold(): value for key, value in values.items()}
    provider_id = str(
        normalized.get("mpt_llm_provider")
        or normalized.get("llm_provider")
        or snapshot.get("llm_provider", "")
    ).strip().lower()
    if provider_id:
        snapshot["llm_provider"] = provider_id
    provider = get_llm_provider(provider_id) if provider_id else None

    # Overlay exact config keys first (case-insensitive), then common
    # environment aliases used by deployment scripts.
    for key in list(snapshot):
        value = normalized.get(str(key).casefold())
        if value is not None:
            snapshot[key] = value
    if provider is not None:
        provider_prefix = provider.provider_id.upper()
        aliases = {
            provider.config_key("api_key"): (
                f"{provider_prefix}_API_KEY",
                "MPT_LLM_API_KEY",
            ),
            provider.config_key("model_name"): (
                f"{provider_prefix}_MODEL_NAME",
                f"{provider_prefix}_MODEL",
                "MPT_LLM_MODEL",
            ),
            provider.config_key("base_url"): (
                f"{provider_prefix}_BASE_URL",
                "MPT_LLM_BASE_URL",
            ),
        }
        for config_key, candidates in aliases.items():
            for candidate in candidates:
                if candidate.casefold() in normalized:
                    snapshot[config_key] = normalized[candidate.casefold()]
                    break
    return snapshot


__all__ = [
    "CapabilityKind", "CapabilityStatus", "CapabilityUnavailable", "CapabilityRegistry",
    "LLMProvider", "ImageGenerationProvider", "VideoGenerationProvider", "VisionAnalysisProvider", "TTSProvider",
    "ImageCandidate", "VisionAnalysis", "VisionAnalysisRequest", "VisionMediaInput", "TTSResult", "MPTLLMProvider", "MainlandUniversalLLMProvider", "RuntimeVideoProvider",
    "UnavailableImageProvider", "UnavailableVisionProvider", "UnavailableTTSProvider", "MPTTTSProvider", "DeterministicMockVisionProvider", "default_capability_registry",
]
