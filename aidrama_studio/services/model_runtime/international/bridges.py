"""Authorization-first bindings for the first international provider slice.

The bindings keep stable legacy transports in place while routing every new
CREATE through the frozen RequestResponseDriver authorization boundary.  No
binding selects a different provider or region when its exact manifest is
unavailable.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from aidrama_studio.services.ai_capabilities import (
    ImageCandidate,
    MPTLLMProvider,
    MPTTTSProvider,
    TTSResult,
    VisionAnalysisRequest,
    VisionMediaInput,
)
from aidrama_studio.services.providers import OpenAIImageProvider
from aidrama_studio.services.providers.gemini_vision import GeminiVisionProvider
from aidrama_studio.services.security import sanitize_persistent_metadata

from ..codecs import CodecError
from ..contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    EncodedRequest,
    ProtocolFamily,
    RuntimeOutcome,
)
from ..drivers import RequestResponseDriver
from ..manifest import ModelManifest
from ..resolver import InMemoryManifestRegistry
from .codecs import LegacyRequestResponseCodec, OpenAICompatibleChatCodec
from .manifests import (
    AZURE_SPEECH_MANIFEST,
    EDGE_TTS_MANIFEST,
    GEMINI_LLM_MANIFEST,
    GEMINI_VISION_MANIFEST,
    LEGACY_AZURE_SPEECH_CODEC_ID,
    LEGACY_EDGE_TTS_CODEC_ID,
    LEGACY_GEMINI_LLM_CODEC_ID,
    LEGACY_GEMINI_VISION_CODEC_ID,
    LEGACY_OPENAI_IMAGE_CODEC_ID,
    OPENAI_IMAGE_MANIFEST,
    OPENAI_LLM_MANIFEST,
    clone_manifest,
    manifest_with_runtime_status,
)


ImageCandidateSink = Callable[[ImageCandidate], ContentRef]
AudioResultSink = Callable[[TTSResult], ContentRef]
ContentPathResolver = Callable[[ContentRef], Path]


def _require_exact_request_identity(
    request: CapabilityRequest,
    manifest: ModelManifest,
) -> None:
    expected = {
        "provider_id": manifest.provider_id,
        "model_id": manifest.model_id,
        "manifest_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        "codec_id": manifest.codec_id,
    }
    for name, value in expected.items():
        if getattr(request, name) != value:
            raise CodecError(f"request {name} does not match exact international binding")
    if request.capability is not manifest.capability:
        raise CodecError("request capability does not match exact international binding")
    if request.protocol_family is not manifest.protocol:
        raise CodecError("request protocol does not match exact international binding")


@dataclass(frozen=True, slots=True)
class InternationalRuntimeBinding:
    """One exact manifest + codec + driver binding with no fallback."""

    manifest: ModelManifest
    codec: object
    driver: RequestResponseDriver
    migration: str = "WRAP"
    provider_boundary: str = ""

    def request(
        self,
        *,
        request_id: str,
        project_id: str = "",
        execution_id: str | None = None,
        prompt_or_text: str | None = None,
        inputs: tuple[ContentRef, ...] = (),
        structured_input: Mapping[str, Any] | None = None,
        provider_parameters: Mapping[str, Any] | None = None,
        runtime_plan_id: str | None = None,
        runtime_plan_hash: str | None = None,
        authorization_fingerprint: str | None = None,
        create_authorized: bool | None = None,
    ) -> CapabilityRequest:
        """Create a capability-neutral request frozen to this exact binding."""

        return CapabilityRequest(
            request_id=request_id,
            project_id=project_id,
            execution_id=execution_id,
            capability=self.manifest.capability,
            protocol_family=self.manifest.protocol,
            provider_id=self.manifest.provider_id,
            model_id=self.manifest.model_id,
            manifest_id=self.manifest.id,
            manifest_hash=self.manifest.manifest_hash,
            codec_id=self.manifest.codec_id,
            runtime_plan_id=runtime_plan_id,
            runtime_plan_hash=runtime_plan_hash,
            inputs=inputs,
            prompt_or_text=prompt_or_text,
            structured_input=structured_input or {},
            provider_parameters=provider_parameters or {},
            authorization_fingerprint=authorization_fingerprint,
            create_authorized=create_authorized,
            authorization_required=self.manifest.authorization_required,
        )

    def invoke(
        self,
        request: CapabilityRequest,
        *,
        authorization: object | None = None,
    ) -> CapabilityResult:
        _require_exact_request_identity(request, self.manifest)
        if not self.manifest.configured:
            raise CodecError("international provider is not configured; no fallback")
        if not self.manifest.runtime_available:
            raise CodecError("international provider runtime is unavailable; no fallback")
        return self.driver.invoke(
            request,
            self.codec,
            self.manifest,
            authorization=authorization,
        )


class InternationalRuntimeRegistry:
    """Exact binding registry; lookup failure never chooses another model."""

    def __init__(self, bindings: tuple[InternationalRuntimeBinding, ...] = ()) -> None:
        self._bindings: dict[str, InternationalRuntimeBinding] = {}
        for binding in bindings:
            self.register(binding)

    def register(self, binding: InternationalRuntimeBinding) -> InternationalRuntimeBinding:
        if not isinstance(binding, InternationalRuntimeBinding):
            raise TypeError("international registry accepts InternationalRuntimeBinding")
        if binding.manifest.id in self._bindings:
            raise ValueError(f"duplicate international manifest: {binding.manifest.id}")
        self._bindings[binding.manifest.id] = binding
        return binding

    def get(self, manifest_id: str) -> InternationalRuntimeBinding:
        try:
            return self._bindings[manifest_id]
        except KeyError as exc:
            raise LookupError(
                f"international manifest {manifest_id!r} is unavailable; no fallback"
            ) from exc

    def list(self) -> tuple[InternationalRuntimeBinding, ...]:
        return tuple(self._bindings.values())

    @property
    def manifests(self) -> tuple[ModelManifest, ...]:
        return tuple(binding.manifest for binding in self._bindings.values())

    @property
    def manifest_registry(self) -> InMemoryManifestRegistry:
        return InMemoryManifestRegistry(self.manifests)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleTransportConfig:
    """Transient OpenAI SDK transport configuration.

    The secret and base URL are intentionally absent from ModelManifest.  The
    config object is never serialized and hides its API key from ``repr``.
    """

    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 120.0

    def validate(self, *, official_openai: bool) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("OpenAI-compatible transport requires a transient API key")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("OpenAI-compatible timeout must be positive")
        try:
            parsed = urlsplit(self.base_url.rstrip("/"))
            port = parsed.port
        except ValueError as exc:
            raise ValueError("OpenAI-compatible base URL is invalid") from exc
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in ({"https"} if not loopback else {"http", "https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenAI-compatible base URL must be a credential-free HTTP(S) endpoint")
        if official_openai and (
            parsed.scheme != "https"
            or parsed.hostname != "api.openai.com"
            or port is not None
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError("official OpenAI manifest requires the official /v1 endpoint")


class OpenAICompatibleSDKTransport:
    """Thin transport over the repository's pinned OpenAI SDK."""

    def __init__(
        self,
        config: OpenAICompatibleTransportConfig,
        *,
        official_openai: bool,
    ) -> None:
        config.validate(official_openai=official_openai)
        self._config = config

    def send(
        self,
        encoded: EncodedRequest,
        context: CapabilityRequest | None = None,
    ) -> object:
        if not isinstance(encoded.payload, Mapping):
            raise TypeError("OpenAI-compatible codec did not produce a mapping")
        from openai import OpenAI

        # Both the runtime driver and SDK are configured for zero automatic
        # retries so one authorization can never silently multiply CREATEs.
        client = OpenAI(
            api_key=self._config.api_key,
            base_url=self._config.base_url.rstrip("/"),
            timeout=float(self._config.timeout_seconds),
            max_retries=0,
        )
        return client.chat.completions.create(**dict(encoded.payload))


class _LegacyBoundaryTransport:
    def __init__(self, handler: Callable[[CapabilityRequest], CapabilityResult]) -> None:
        self._handler = handler

    def send(
        self,
        encoded: EncodedRequest,
        context: CapabilityRequest | None = None,
    ) -> CapabilityResult:
        if not isinstance(encoded.payload, CapabilityRequest):
            raise TypeError("legacy wrapper payload must be CapabilityRequest")
        if context is not None and context is not encoded.payload:
            raise ValueError("legacy wrapper request context changed")
        return self._handler(encoded.payload)


def _binding(
    *,
    manifest: ModelManifest,
    codec: object,
    transport: object,
    provider_boundary: str,
) -> InternationalRuntimeBinding:
    return InternationalRuntimeBinding(
        manifest=manifest,
        codec=codec,
        driver=RequestResponseDriver(transport, manifest=manifest, max_retries=0),
        migration="WRAP",
        provider_boundary=provider_boundary,
    )


def build_openai_compatible_llm_binding(
    *,
    manifest: ModelManifest = OPENAI_LLM_MANIFEST,
    config: OpenAICompatibleTransportConfig | None = None,
    transport: object | None = None,
) -> InternationalRuntimeBinding:
    """Bind official OpenAI or one explicit CUSTOM Chat-compatible endpoint."""

    if manifest.capability is not CapabilityKind.LLM:
        raise ValueError("OpenAI-compatible binding requires an LLM manifest")
    if manifest.protocol is not ProtocolFamily.REQUEST_RESPONSE:
        raise ValueError("OpenAI-compatible binding requires REQUEST_RESPONSE")
    if manifest.codec_id != OpenAICompatibleChatCodec().codec_id:
        raise ValueError("manifest does not select the OpenAI Chat codec")
    if manifest.deployment_region not in {"INTERNATIONAL", "CUSTOM"}:
        raise ValueError("OpenAI-compatible international binding rejects Mainland region")
    if (config is None) == (transport is None):
        raise ValueError("provide exactly one transient config or injected transport")
    if manifest.provider_id == "openai" and config is None:
        raise ValueError("official OpenAI binding requires validated official transport config")
    selected_manifest = manifest
    selected_transport = transport
    if config is not None:
        official = manifest.provider_id == "openai"
        selected_transport = OpenAICompatibleSDKTransport(
            config,
            official_openai=official,
        )
        selected_manifest = clone_manifest(
            manifest,
            readiness={
                "configured": True,
                "verified": False,
                "runtime_available": importlib.util.find_spec("openai") is not None,
                "create_authorized": False,
                "authorization_required": manifest.authorization_required,
            },
        )
    assert selected_transport is not None
    return _binding(
        manifest=selected_manifest,
        codec=OpenAICompatibleChatCodec(),
        transport=selected_transport,
        provider_boundary="OPENAI_SDK_CHAT_COMPLETIONS",
    )


def build_gemini_llm_binding(
    *,
    config_snapshot: Mapping[str, object],
    env: Mapping[str, str] | None = None,
) -> InternationalRuntimeBinding:
    """Wrap the mature MPT Gemini generation path behind the Core driver."""

    snapshot = dict(config_snapshot)
    if str(snapshot.get("llm_provider", "")).strip().lower() != "gemini":
        raise ValueError("Gemini LLM binding requires an explicit Gemini config snapshot")
    configured_base_url = str(snapshot.get("gemini_base_url", "")).strip()
    if configured_base_url:
        try:
            parsed = urlsplit(configured_base_url.rstrip("/"))
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Gemini LLM base URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "generativelanguage.googleapis.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("official Gemini manifest cannot use a CUSTOM endpoint")
    provider = MPTLLMProvider(config_snapshot=snapshot, env=env)
    manifest = manifest_with_runtime_status(GEMINI_LLM_MANIFEST, provider.status)
    codec = LegacyRequestResponseCodec(
        codec_id=LEGACY_GEMINI_LLM_CODEC_ID,
        capability=CapabilityKind.LLM,
    )

    def handle(request: CapabilityRequest) -> CapabilityResult:
        if not isinstance(request.prompt_or_text, str) or not request.prompt_or_text.strip():
            raise ValueError("Gemini LLM prompt must not be empty")
        schema = request.structured_input.get("output_schema")
        if schema is not None and not isinstance(schema, Mapping):
            raise ValueError("Gemini output_schema must be a mapping")
        unknown = set(request.structured_input) - {"output_schema"}
        if unknown or request.provider_parameters or request.inputs:
            raise ValueError("Gemini legacy wrapper received unsupported V1 fields")
        result = provider.generate_structured(
            request.prompt_or_text,
            schema=schema if isinstance(schema, Mapping) else None,
        )
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            safe_metadata={"structured_output": result},
        )

    return _binding(
        manifest=manifest,
        codec=codec,
        transport=_LegacyBoundaryTransport(handle),
        provider_boundary="MPTLLMProvider:gemini",
    )


def build_openai_image_binding(
    *,
    image_sink: ImageCandidateSink,
    provider: OpenAIImageProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> InternationalRuntimeBinding:
    """Wrap OpenAI image generation while preserving DRAFT persistence."""

    selected = provider or OpenAIImageProvider(env=env)
    try:
        parsed = urlsplit(selected.config.base_url.rstrip("/"))
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OpenAI image base URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.openai.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("official OpenAI image manifest cannot use a CUSTOM endpoint")
    manifest = manifest_with_runtime_status(OPENAI_IMAGE_MANIFEST, selected.status)
    codec = LegacyRequestResponseCodec(
        codec_id=LEGACY_OPENAI_IMAGE_CODEC_ID,
        capability=CapabilityKind.IMAGE,
    )

    def handle(request: CapabilityRequest) -> CapabilityResult:
        if not isinstance(request.prompt_or_text, str) or not request.prompt_or_text.strip():
            raise ValueError("OpenAI image prompt must not be empty")
        if request.inputs or request.structured_input:
            raise ValueError("OpenAI image V1 wrapper supports text-to-image only")
        candidate = selected.generate_candidate(
            request.prompt_or_text,
            project_id=request.project_id,
            metadata=request.provider_parameters,
        )
        output = image_sink(candidate)
        if not isinstance(output, ContentRef):
            raise TypeError("image sink must return ContentRef")
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(output,),
            safe_metadata={"candidate_status": candidate.lifecycle_status},
        )

    return _binding(
        manifest=manifest,
        codec=codec,
        transport=_LegacyBoundaryTransport(handle),
        provider_boundary="OpenAIImageProvider",
    )


def _vision_media(
    item: ContentRef,
    *,
    content_resolver: ContentPathResolver,
) -> VisionMediaInput:
    path = Path(content_resolver(item))
    if not path.is_absolute():
        raise ValueError("Vision content resolver must return an absolute path")
    raw_time = item.metadata.get("time_seconds")
    time_seconds: float | None = None
    if raw_time is not None:
        if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
            raise ValueError("Vision frame time_seconds must be numeric")
        time_seconds = float(raw_time)
    return VisionMediaInput(
        source_kind=item.source_kind,
        source_id=item.source_id,
        path=path,
        mime_type=item.mime_type,
        sha256=item.sha256,
        role=item.role,
        time_seconds=time_seconds,
    )


def build_gemini_vision_binding(
    *,
    content_resolver: ContentPathResolver,
    provider: GeminiVisionProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> InternationalRuntimeBinding:
    """Wrap Gemini Vision with transient path resolution and no path persistence."""

    selected = provider or GeminiVisionProvider(env=env)
    manifest = manifest_with_runtime_status(GEMINI_VISION_MANIFEST, selected.status)
    codec = LegacyRequestResponseCodec(
        codec_id=LEGACY_GEMINI_VISION_CODEC_ID,
        capability=CapabilityKind.VISION,
    )

    def handle(request: CapabilityRequest) -> CapabilityResult:
        if not request.project_id or not request.execution_id:
            raise ValueError("Gemini Vision project and execution identities are required")
        videos = [item for item in request.inputs if item.source_kind == "VIDEO_ARTIFACT"]
        if len(videos) != 1:
            raise ValueError("Gemini Vision requires exactly one VIDEO_ARTIFACT")
        video_ref = videos[0]
        remaining = [item for item in request.inputs if item is not video_ref]
        frame_refs = [
            item
            for item in remaining
            if "FRAME" in item.source_kind.upper() or item.role.upper() in {"FRAME", "SAMPLED_FRAME"}
        ]
        reference_refs = [item for item in remaining if item not in frame_refs]
        allowed = {
            "artifact_id",
            "frame_manifest_id",
            "generation_brief_hash",
            "prompt_template_version",
            "creative_context",
        }
        unknown = set(request.structured_input) - allowed
        if unknown or request.provider_parameters:
            raise ValueError("Gemini Vision wrapper received unsupported V1 fields")
        creative_context = request.structured_input.get("creative_context", {})
        if not isinstance(creative_context, Mapping):
            raise ValueError("Gemini Vision creative_context must be a mapping")
        vision_request = VisionAnalysisRequest(
            project_id=request.project_id,
            execution_id=request.execution_id,
            artifact_id=str(
                request.structured_input.get("artifact_id") or video_ref.source_id
            ),
            video=_vision_media(video_ref, content_resolver=content_resolver),
            frames=tuple(
                _vision_media(item, content_resolver=content_resolver)
                for item in frame_refs
            ),
            references=tuple(
                _vision_media(item, content_resolver=content_resolver)
                for item in reference_refs
            ),
            frame_manifest_id=(
                str(request.structured_input["frame_manifest_id"])
                if request.structured_input.get("frame_manifest_id") is not None
                else None
            ),
            generation_brief_hash=(
                str(request.structured_input["generation_brief_hash"])
                if request.structured_input.get("generation_brief_hash") is not None
                else None
            ),
            prompt_template_version=str(
                request.structured_input.get(
                    "prompt_template_version", "aidrama-gemini-vision-qc-v1"
                )
            ),
            creative_context=creative_context,
        )
        analysis = selected.analyze(request=vision_request)
        safe_metadata = sanitize_persistent_metadata(
            {
                "provider": analysis.provider,
                "analysis_kind": analysis.analysis_kind,
                "metrics": analysis.metrics,
                "analysis": analysis.metadata,
            }
        )
        if not isinstance(safe_metadata, Mapping):
            raise TypeError("Gemini Vision metadata sanitizer returned invalid data")
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            safe_metadata=safe_metadata,
        )

    return _binding(
        manifest=manifest,
        codec=codec,
        transport=_LegacyBoundaryTransport(handle),
        provider_boundary="GeminiVisionProvider",
    )


def _build_tts_binding(
    *,
    manifest_template: ModelManifest,
    codec_id: str,
    voice: str,
    audio_sink: AudioResultSink,
    provider: MPTTTSProvider | None,
    env: Mapping[str, str] | None,
    paid_one_call: bool,
) -> InternationalRuntimeBinding:
    voice_identity = str(voice).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", voice_identity):
        raise ValueError("TTS voice must be a safe exact manifest identity")
    selected = provider or MPTTTSProvider(voice=voice, env=env)
    status = selected._status_for_voice(voice)
    manifest = manifest_with_runtime_status(manifest_template, status)
    status_metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    manifest_metadata = dict(manifest.metadata)
    endpoint_profile_id = manifest.endpoint_profile_id
    if paid_one_call:
        service_region = str(status_metadata.get("service_region", "")).strip().lower()
        if manifest.configured and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", service_region):
            raise ValueError("configured Azure Speech binding requires an exact safe service region")
        if service_region:
            endpoint_profile_id = f"runtime:TTS:AZURE_SPEECH:{service_region}"
            manifest_metadata["service_region"] = service_region
    manifest_metadata["voice"] = voice_identity
    manifest = clone_manifest(
        manifest,
        id=f"{manifest_template.id}:{voice_identity}",
        display_name=f"{manifest_template.display_name} - {voice_identity}",
        model_id=f"{manifest.model_id}:{voice_identity}",
        endpoint_profile_id=endpoint_profile_id,
        metadata=manifest_metadata,
    )
    codec = LegacyRequestResponseCodec(
        codec_id=codec_id,
        capability=CapabilityKind.TTS,
    )

    def handle(request: CapabilityRequest) -> CapabilityResult:
        if not isinstance(request.prompt_or_text, str) or not request.prompt_or_text.strip():
            raise ValueError("TTS text must not be empty")
        if request.inputs or request.structured_input:
            raise ValueError("TTS V1 wrapper accepts text and neutral parameters only")
        allowed = {"voice", "language", "sample_rate"}
        unknown = set(request.provider_parameters) - allowed
        if unknown:
            raise ValueError("TTS wrapper received unsupported V1 parameters")
        selected_voice = str(request.provider_parameters.get("voice") or voice_identity).strip()
        if selected_voice != voice_identity:
            raise ValueError("TTS voice does not match frozen manifest binding")
        language = str(request.provider_parameters.get("language", "zh-CN"))
        sample_rate = request.provider_parameters.get("sample_rate", 48000)
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("TTS sample_rate must be a positive integer")
        if paid_one_call:
            result = selected.synthesize_live_smoke(
                request.prompt_or_text,
                voice=selected_voice,
                language=language,
                sample_rate=sample_rate,
            )
        else:
            result = selected.synthesize(
                request.prompt_or_text,
                voice=selected_voice,
                language=language,
                sample_rate=sample_rate,
            )
        output = audio_sink(result)
        if not isinstance(output, ContentRef):
            raise TypeError("audio sink must return ContentRef")
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(output,),
            safe_metadata={
                "provider": result.provider,
                "mime_type": result.mime_type,
                "duration_seconds": result.duration_seconds,
                "voice": selected_voice,
                "language": language,
                "sample_rate": sample_rate,
            },
        )

    return _binding(
        manifest=manifest,
        codec=codec,
        transport=_LegacyBoundaryTransport(handle),
        provider_boundary="MPTTTSProvider",
    )


def build_azure_speech_binding(
    *,
    voice: str,
    audio_sink: AudioResultSink,
    provider: MPTTTSProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> InternationalRuntimeBinding:
    """Bind Azure Speech with the existing max-attempts=1 paid seam."""

    return _build_tts_binding(
        manifest_template=AZURE_SPEECH_MANIFEST,
        codec_id=LEGACY_AZURE_SPEECH_CODEC_ID,
        voice=voice,
        audio_sink=audio_sink,
        provider=provider,
        env=env,
        paid_one_call=True,
    )


def build_edge_tts_binding(
    *,
    voice: str,
    audio_sink: AudioResultSink,
    provider: MPTTTSProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> InternationalRuntimeBinding:
    """Bind the existing remote Edge TTS compatibility seam."""

    return _build_tts_binding(
        manifest_template=EDGE_TTS_MANIFEST,
        codec_id=LEGACY_EDGE_TTS_CODEC_ID,
        voice=voice,
        audio_sink=audio_sink,
        provider=provider,
        env=env,
        paid_one_call=False,
    )


INTERNATIONAL_PROVIDER_MIGRATION = MappingProxyType(
    {
        "MPTLLMProvider": ("KEEP", "WRAP"),
        "OpenAICompatibleChat": ("KEEP", "WRAP"),
        "OpenAIImageProvider": ("KEEP", "WRAP"),
        "GeminiVisionProvider": ("KEEP", "WRAP"),
        "MPTTTSProvider": ("KEEP", "WRAP"),
        "AzureSpeechBackend": ("KEEP", "MIGRATE"),
        "EdgeTTSBackend": ("KEEP", "DEPRECATE_LATER"),
        "InternationalVideo": ("DEFERRED",),
    }
)


__all__ = [
    "AudioResultSink",
    "ContentPathResolver",
    "INTERNATIONAL_PROVIDER_MIGRATION",
    "ImageCandidateSink",
    "InternationalRuntimeBinding",
    "InternationalRuntimeRegistry",
    "OpenAICompatibleSDKTransport",
    "OpenAICompatibleTransportConfig",
    "build_azure_speech_binding",
    "build_edge_tts_binding",
    "build_gemini_llm_binding",
    "build_gemini_vision_binding",
    "build_openai_compatible_llm_binding",
    "build_openai_image_binding",
]
