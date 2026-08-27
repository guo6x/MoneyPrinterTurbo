"""International provider manifests for Universal Runtime V1.

This module is metadata-only.  It never reads credentials, imports provider
SDKs, or performs network I/O.  Runtime configuration is projected onto these
immutable templates by :mod:`bridges` without storing secret values or private
paths in a manifest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..contracts import CapabilityKind, ProtocolFamily
from ..manifest import ModelManifest


OPENAI_CHAT_CODEC_ID = "openai.chat.completions.v1"
LEGACY_GEMINI_LLM_CODEC_ID = "legacy.gemini.llm.v1"
LEGACY_OPENAI_IMAGE_CODEC_ID = "legacy.openai.image.v1"
LEGACY_GEMINI_VISION_CODEC_ID = "legacy.gemini.vision.v1"
LEGACY_AZURE_SPEECH_CODEC_ID = "legacy.azure.speech.v1"
LEGACY_EDGE_TTS_CODEC_ID = "legacy.edge.tts.v1"


def _readiness(*, authorization_required: bool) -> dict[str, bool]:
    return {
        "configured": False,
        "verified": False,
        "runtime_available": False,
        "create_authorized": False,
        "authorization_required": authorization_required,
    }


def _paid_authorization() -> dict[str, bool]:
    return {
        "create_is_paid": True,
        "requires_create_authorization": True,
    }


OPENAI_LLM_MANIFEST = ModelManifest(
    id="international:llm:openai:gpt-5.5:v1",
    display_name="OpenAI GPT 5.5",
    provider_id="openai",
    capability=CapabilityKind.LLM,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="gpt-5.5",
    deployment_region="INTERNATIONAL",
    endpoint_class="OPENAI_PUBLIC",
    endpoint_profile_id="runtime:LLM:OPENAI:OPENAI_PUBLIC",
    credential_reference="OPENAI_API_KEY",
    codec_id=OPENAI_CHAT_CODEC_ID,
    codec_version="1",
    input_modalities=("TEXT",),
    output_modalities=("TEXT", "JSON"),
    supported_modes=("CHAT_COMPLETIONS", "STRUCTURED_OUTPUT"),
    supports={"structured_output": True, "streaming": False},
    authorization=_paid_authorization(),
    readiness=_readiness(authorization_required=True),
    selection_policy={"requires_explicit_selection": True},
    parameter_schema={
        "provider_neutral_fields": (
            "system_instruction",
            "output_schema",
            "temperature",
            "top_p",
            "max_output_tokens",
            "stop_sequences",
        )
    },
    lifecycle={"create": "REQUEST_RESPONSE", "automatic_retries": 0},
    native_features=("OPENAI_CHAT_COMPATIBLE",),
    pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS"},
    metadata={"migration": "WRAP", "response_api_compatible": False},
)


GEMINI_LLM_MANIFEST = ModelManifest(
    id="international:llm:gemini:gemini-3.1-pro-preview:v1",
    display_name="Google Gemini 3.1 Pro Preview",
    provider_id="gemini",
    capability=CapabilityKind.LLM,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="gemini-3.1-pro-preview",
    deployment_region="INTERNATIONAL",
    endpoint_class="GOOGLE_GEMINI_PUBLIC",
    endpoint_profile_id="runtime:LLM:GEMINI:GOOGLE_GEMINI_PUBLIC",
    credential_reference="GEMINI_API_KEY",
    codec_id=LEGACY_GEMINI_LLM_CODEC_ID,
    codec_version="1",
    input_modalities=("TEXT",),
    output_modalities=("TEXT", "JSON"),
    supported_modes=("GENERATE_CONTENT", "STRUCTURED_OUTPUT"),
    supports={"structured_output": True, "streaming": False},
    authorization=_paid_authorization(),
    readiness=_readiness(authorization_required=True),
    selection_policy={"requires_explicit_selection": True},
    lifecycle={"create": "REQUEST_RESPONSE", "automatic_retries": 0},
    native_features=("GEMINI_NATIVE",),
    pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS"},
    metadata={"migration": "WRAP", "legacy_boundary": "MPTLLMProvider"},
)


OPENAI_IMAGE_MANIFEST = ModelManifest(
    id="international:image:openai:gpt-image-2:v1",
    display_name="OpenAI GPT Image 2",
    provider_id="openai",
    capability=CapabilityKind.IMAGE,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="gpt-image-2",
    deployment_region="INTERNATIONAL",
    endpoint_class="OPENAI_PUBLIC",
    endpoint_profile_id="runtime:IMAGE:OPENAI:OPENAI_PUBLIC",
    credential_reference="OPENAI_API_KEY",
    codec_id=LEGACY_OPENAI_IMAGE_CODEC_ID,
    codec_version="1",
    input_modalities=("TEXT",),
    output_modalities=("IMAGE",),
    supported_modes=("TEXT_TO_IMAGE",),
    supports={},
    authorization=_paid_authorization(),
    readiness=_readiness(authorization_required=True),
    selection_policy={"requires_explicit_selection": True},
    limits={"images_per_create": 1},
    lifecycle={"create": "REQUEST_RESPONSE", "candidate_state": "DRAFT"},
    native_features=("BASE64_RESULT", "ALLOWLISTED_HTTPS_RESULT"),
    pricing={"status": "PRICE_UNVERIFIED", "unit": "IMAGE_TOKENS"},
    metadata={"migration": "WRAP", "legacy_boundary": "OpenAIImageProvider"},
)


GEMINI_VISION_MANIFEST = ModelManifest(
    id="international:vision:gemini:gemini-3.7-flash:v1",
    display_name="Google Gemini Vision 3.7 Flash",
    provider_id="gemini",
    capability=CapabilityKind.VISION,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="gemini-3.7-flash",
    deployment_region="INTERNATIONAL",
    endpoint_class="GOOGLE_GEMINI_PUBLIC",
    endpoint_profile_id="runtime:VISION:GEMINI:GOOGLE_GEMINI_PUBLIC",
    credential_reference="GEMINI_API_KEY",
    codec_id=LEGACY_GEMINI_VISION_CODEC_ID,
    codec_version="1",
    input_modalities=("VIDEO", "IMAGE", "TEXT"),
    output_modalities=("JSON",),
    supported_modes=("VIDEO_QC", "FRAME_QC", "REFERENCE_COMPARISON"),
    reference={"images": True, "videos": True, "max_count": 64},
    supports={"multi_reference": True, "structured_output": True},
    authorization=_paid_authorization(),
    readiness=_readiness(authorization_required=True),
    selection_policy={"requires_explicit_selection": True},
    lifecycle={
        "create": "REQUEST_RESPONSE_WRAPPER",
        "remote_files": "DELETE_IN_FINALLY",
        "interaction_store": False,
    },
    native_features=("DIRECT_VIDEO", "SAMPLED_FRAMES", "JSON_SCHEMA"),
    pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS_AND_MEDIA"},
    metadata={"migration": "WRAP", "legacy_boundary": "GeminiVisionProvider"},
)


AZURE_SPEECH_MANIFEST = ModelManifest(
    id="international:tts:azure:neural-speech:v1",
    display_name="Microsoft Azure Neural Speech",
    provider_id="azure_speech",
    capability=CapabilityKind.TTS,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="AZURE_SPEECH_NEURAL_TTS",
    deployment_region="INTERNATIONAL",
    endpoint_class="AZURE_SPEECH_PUBLIC",
    endpoint_profile_id="runtime:TTS:AZURE_SPEECH:AZURE_SPEECH_PUBLIC",
    credential_reference="AZURE_SPEECH_KEY",
    codec_id=LEGACY_AZURE_SPEECH_CODEC_ID,
    codec_version="1",
    input_modalities=("TEXT",),
    output_modalities=("AUDIO",),
    supported_modes=("NEURAL_TTS",),
    authorization=_paid_authorization(),
    readiness=_readiness(authorization_required=True),
    selection_policy={"requires_explicit_selection": True},
    lifecycle={"create": "REQUEST_RESPONSE", "automatic_retries": 0},
    native_features=("VOICE", "PROSODY", "MP3"),
    pricing={"status": "PRICE_UNVERIFIED", "unit": "CHARACTERS"},
    metadata={"migration": "WRAP", "legacy_boundary": "MPTTTSProvider"},
)


EDGE_TTS_MANIFEST = ModelManifest(
    id="international:tts:microsoft-edge:remote-speech:v1",
    display_name="Microsoft Edge TTS compatibility",
    provider_id="microsoft_edge_tts",
    capability=CapabilityKind.TTS,
    protocol=ProtocolFamily.REQUEST_RESPONSE,
    model_id="MICROSOFT_EDGE_TTS",
    deployment_region="INTERNATIONAL",
    endpoint_class="MICROSOFT_EDGE_TTS_PUBLIC",
    endpoint_profile_id="runtime:TTS:MICROSOFT_EDGE_TTS:PUBLIC",
    codec_id=LEGACY_EDGE_TTS_CODEC_ID,
    codec_version="1",
    input_modalities=("TEXT",),
    output_modalities=("AUDIO",),
    supported_modes=("REMOTE_SPEECH_COMPATIBILITY",),
    authorization={
        "create_is_paid": False,
        "requires_create_authorization": False,
    },
    readiness=_readiness(authorization_required=False),
    selection_policy={"requires_explicit_selection": True},
    lifecycle={"create": "REQUEST_RESPONSE_WRAPPER", "native_transport": "STREAM"},
    native_features=("VOICE", "RATE", "VOLUME", "MP3"),
    pricing={"status": "NO_PROVIDER_CHARGE_NO_SLA", "unit": "REMOTE_SERVICE"},
    metadata={
        "migration": "WRAP",
        "legacy_boundary": "MPTTTSProvider",
        "deprecation": "DEPRECATE_LATER_IF_SLA_REQUIRED",
    },
)


INTERNATIONAL_MODEL_MANIFESTS: tuple[ModelManifest, ...] = (
    OPENAI_LLM_MANIFEST,
    GEMINI_LLM_MANIFEST,
    OPENAI_IMAGE_MANIFEST,
    GEMINI_VISION_MANIFEST,
    AZURE_SPEECH_MANIFEST,
    EDGE_TTS_MANIFEST,
)


def clone_manifest(manifest: ModelManifest, **changes: Any) -> ModelManifest:
    """Return a validated copy with only explicit safe metadata changes."""

    payload = manifest.canonical_payload()
    payload.update(changes)
    return ModelManifest(**payload)


def manifest_with_runtime_status(
    manifest: ModelManifest,
    status: object,
    *,
    expected_region: str = "INTERNATIONAL",
) -> ModelManifest:
    """Project a legacy provider status without weakening manifest identity.

    Configuration, verification, runtime availability, and create
    authorization remain independent.  Provider status may refine the model
    and endpoint identity only when it stays inside the frozen international
    region; a mismatch fails closed.
    """

    metadata = getattr(status, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    observed_region = str(metadata.get("deployment_region", expected_region)).strip().upper()
    if observed_region != expected_region:
        raise ValueError(
            f"legacy provider region {observed_region!r} does not match {expected_region!r}"
        )
    configured = getattr(status, "configured", False) is True
    verified = getattr(status, "verified", False) is True
    runtime_available = getattr(status, "runtime_available", False) is True
    create_authorized = getattr(status, "create_authorized", False) is True
    model_id = str(metadata.get("model", manifest.model_id)).strip() or manifest.model_id
    endpoint_class = str(
        metadata.get("endpoint_class", manifest.endpoint_class)
    ).strip() or manifest.endpoint_class
    endpoint_profile_id = str(
        metadata.get("endpoint_profile_id", manifest.endpoint_profile_id or "")
    ).strip() or manifest.endpoint_profile_id
    return clone_manifest(
        manifest,
        model_id=model_id,
        endpoint_class=endpoint_class,
        endpoint_profile_id=endpoint_profile_id,
        readiness={
            "configured": configured,
            "verified": verified,
            "runtime_available": runtime_available,
            "create_authorized": create_authorized,
            "authorization_required": manifest.authorization_required,
        },
    )


def custom_openai_compatible_manifest(
    *,
    provider_slug: str,
    model_id: str,
    endpoint_profile_id: str,
    credential_reference: str | None = None,
    configured: bool = False,
    verified: bool = False,
    runtime_available: bool = False,
    create_authorized: bool = False,
    authorization_required: bool = True,
) -> ModelManifest:
    """Build an explicit CUSTOM/BYOM Chat manifest.

    The endpoint URL and credential value intentionally are not accepted.
    Callers keep both in transient runtime configuration and provide only a
    safe endpoint identity plus optional secret-slot reference here.
    """

    slug = str(provider_slug).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", slug):
        raise ValueError("provider_slug must be a safe CUSTOM provider identifier")
    if slug in {"openai", "gemini", "azure", "azure_speech", "microsoft_edge_tts"}:
        raise ValueError("CUSTOM provider cannot impersonate an official provider")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id is required")
    if not isinstance(endpoint_profile_id, str) or not endpoint_profile_id.strip():
        raise ValueError("endpoint_profile_id is required")
    for name, value in {
        "configured": configured,
        "verified": verified,
        "runtime_available": runtime_available,
        "create_authorized": create_authorized,
        "authorization_required": authorization_required,
    }.items():
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be boolean")
    return ModelManifest(
        id=f"custom:llm:{slug}:{model_id.strip()}:v1",
        display_name=f"Custom OpenAI-compatible {slug}",
        provider_id=f"custom:{slug}",
        capability=CapabilityKind.LLM,
        protocol=ProtocolFamily.REQUEST_RESPONSE,
        model_id=model_id.strip(),
        deployment_region="CUSTOM",
        endpoint_class="CUSTOM_OPENAI_CHAT",
        endpoint_profile_id=endpoint_profile_id.strip(),
        credential_reference=credential_reference,
        codec_id=OPENAI_CHAT_CODEC_ID,
        codec_version="1",
        input_modalities=("TEXT",),
        output_modalities=("TEXT", "JSON"),
        supported_modes=("CHAT_COMPLETIONS",),
        supports={"structured_output": False, "streaming": False},
        authorization={
            "create_is_paid": authorization_required,
            "requires_create_authorization": authorization_required,
        },
        readiness={
            "configured": configured,
            "verified": verified,
            "runtime_available": runtime_available,
            "create_authorized": create_authorized,
            "authorization_required": authorization_required,
        },
        selection_policy={"requires_explicit_selection": True},
        lifecycle={"create": "REQUEST_RESPONSE", "automatic_retries": 0},
        native_features=("OPENAI_CHAT_COMPATIBLE",),
        pricing={"status": "PRICE_UNVERIFIED", "unit": "PROVIDER_DEFINED"},
        metadata={"migration": "WRAP", "byom": True, "official_provider": False},
    )


def international_model_manifests() -> tuple[ModelManifest, ...]:
    return INTERNATIONAL_MODEL_MANIFESTS


__all__ = [
    "AZURE_SPEECH_MANIFEST",
    "EDGE_TTS_MANIFEST",
    "GEMINI_LLM_MANIFEST",
    "GEMINI_VISION_MANIFEST",
    "INTERNATIONAL_MODEL_MANIFESTS",
    "LEGACY_AZURE_SPEECH_CODEC_ID",
    "LEGACY_EDGE_TTS_CODEC_ID",
    "LEGACY_GEMINI_LLM_CODEC_ID",
    "LEGACY_GEMINI_VISION_CODEC_ID",
    "LEGACY_OPENAI_IMAGE_CODEC_ID",
    "OPENAI_CHAT_CODEC_ID",
    "OPENAI_IMAGE_MANIFEST",
    "OPENAI_LLM_MANIFEST",
    "clone_manifest",
    "custom_openai_compatible_manifest",
    "international_model_manifests",
    "manifest_with_runtime_status",
]
