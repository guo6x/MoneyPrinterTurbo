"""Frozen Mainland provider manifests for the first Universal Runtime slice.

The entries in this module are data only.  Endpoint URLs, HTTP behavior, and
provider payloads remain in the provider runtime/codec modules.  A manifest is
never evidence that a credential was verified or that a paid create was
authorized.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .contracts import CapabilityKind, ProtocolFamily
from .manifest import ModelManifest


DASHSCOPE_CN_ENDPOINT_PROFILE = "DASHSCOPE_CN_BEIJING_V1"
DEEPSEEK_CN_ENDPOINT_PROFILE = "DEEPSEEK_CN_PUBLIC_V1"
ARK_CN_BEIJING_ENDPOINT_PROFILE = "ARK_CN_BEIJING_V3"


MAINLAND_PRIMARY_MANIFEST_IDS: Mapping[CapabilityKind, str] = MappingProxyType({
    CapabilityKind.LLM: "mainland:alibaba:qwen-max:v1",
    CapabilityKind.IMAGE: "mainland:alibaba:qwen-image-3.0:v1",
    CapabilityKind.VIDEO: "mainland:alibaba:wan2.7-i2v-2026-04-25:v1",
    CapabilityKind.VISION: "mainland:alibaba:qwen3-vl-flash:v1",
    CapabilityKind.TTS: "mainland:alibaba:qwen3-tts-flash:v1",
})


MAINLAND_COMPATIBILITY_MANIFEST_IDS: Mapping[str, str] = MappingProxyType({
    "DEEPSEEK_LLM": "mainland:deepseek:deepseek-v4-pro:v1",
    "SEEDANCE_VIDEO": "mainland:volcengine:doubao-seedance-2-5-260628:v1",
})


# The migration decision is intentionally kept next to the registered model
# data so a later cleanup cannot silently reinterpret an existing seam.
MAINLAND_MIGRATION_CLASSIFICATION: Mapping[str, str] = MappingProxyType({
    "MPT_LLM_PROVIDER": "WRAP",
    "QWEN_NATIVE_PROVIDER_SEAM": "WRAP",
    "DEEPSEEK_OPENAI_COMPATIBLE_SEAM": "KEEP",
    "WAN_PRODUCTION_ADAPTER": "WRAP",
    "SEEDANCE_PRODUCTION_ADAPTER": "WRAP",
    "MPT_TTS_PROVIDER_FACADE": "KEEP",
    "MPT_TTS_PROVIDER_INTERNAL_DISPATCH": "DEPRECATE_LATER",
})


def _readiness(
    credential_reference: str,
    credential_presence: Mapping[str, bool],
    *,
    create_authorized: bool,
    runtime_available: bool,
) -> dict[str, bool]:
    return {
        "configured": credential_presence.get(credential_reference) is True,
        "verified": False,
        # Runtime availability means that the codec/driver and required local
        # artifact sink are present.  It remains independent from credential
        # configuration and paid authorization.
        "runtime_available": runtime_available is True,
        "create_authorized": create_authorized is True,
        "authorization_required": True,
    }


def _authorization() -> dict[str, bool]:
    return {
        "create_is_paid": True,
        "requires_create_authorization": True,
    }


def build_mainland_manifests(
    *,
    credential_presence: Mapping[str, bool] | None = None,
    create_authorized: bool = False,
    artifact_sink_available: bool = False,
) -> tuple[ModelManifest, ...]:
    """Build the exact V1 Mainland model inventory without reading secrets."""

    present = dict(credential_presence or {})
    dashscope_ready = _readiness(
        "DASHSCOPE_API_KEY",
        present,
        create_authorized=create_authorized,
        runtime_available=True,
    )
    dashscope_media_ready = _readiness(
        "DASHSCOPE_API_KEY",
        present,
        create_authorized=create_authorized,
        runtime_available=artifact_sink_available,
    )
    deepseek_ready = _readiness(
        "DEEPSEEK_API_KEY",
        present,
        create_authorized=create_authorized,
        runtime_available=True,
    )
    ark_ready = _readiness(
        "ARK_API_KEY",
        present,
        create_authorized=create_authorized,
        runtime_available=artifact_sink_available,
    )

    return (
        ModelManifest(
            id=MAINLAND_PRIMARY_MANIFEST_IDS[CapabilityKind.LLM],
            display_name="Qwen Max (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.LLM,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="qwen-max",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            codec_id="dashscope.qwen.chat.v1",
            input_modalities=("text",),
            output_modalities=("text", "json"),
            supported_modes=("chat", "structured_output"),
            supports={"structured_output": True, "streaming": False},
            authorization=_authorization(),
            readiness=dashscope_ready,
            selection_policy={"priority": 10, "profile": "MAINLAND_QUALITY"},
            limits={"max_messages": 64},
            pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "runtime_provider_id": "MPT_LLM",
                "runtime_endpoint_profile_id": "runtime:LLM:MPT_LLM:qwen:default",
                "runtime_endpoint_class": "MPT_LLM_QWEN_DEFAULT",
            },
        ),
        ModelManifest(
            id=MAINLAND_PRIMARY_MANIFEST_IDS[CapabilityKind.IMAGE],
            display_name="Qwen Image 3.0 (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.IMAGE,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="qwen-image-3.0",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            # Qwen Image 3.0's recommended synchronous API uses the same
            # messages/size/result protocol already implemented by this codec.
            codec_id="dashscope.zimage.v1",
            input_modalities=("text",),
            output_modalities=("image",),
            supported_modes=("text_to_image",),
            resolution=(
                "1280*720",
                "720*1280",
                "1024*1024",
            ),
            reference={"images": False, "videos": False, "max_count": 0},
            authorization=_authorization(),
            readiness=dashscope_media_ready,
            selection_policy={"priority": 10, "profile": "MAINLAND_CHEAP"},
            limits={
                "image_count": 1,
                "dimension_min": 512,
                "dimension_max": 2048,
                "aspect_ratio_min": "1:8",
                "aspect_ratio_max": "8:1",
                "result_retention_hours": 24,
            },
            pricing={"status": "PRICE_UNVERIFIED", "unit": "IMAGE"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "requires_artifact_sink": True,
            },
        ),
        ModelManifest(
            id="mainland:alibaba:z-image-turbo:v1",
            display_name="Z-Image Turbo (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.IMAGE,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="z-image-turbo",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            codec_id="dashscope.zimage.v1",
            input_modalities=("text",),
            output_modalities=("image",),
            supported_modes=("text_to_image",),
            resolution=(
                "1280*720",
                "720*1280",
                "1024*1024",
                "512*512",
                "2048*2048",
            ),
            reference={"images": False, "videos": False, "max_count": 0},
            authorization=_authorization(),
            readiness=dashscope_media_ready,
            selection_policy={"priority": 50, "profile": "MAINLAND_COMPATIBILITY"},
            limits={
                "image_count": 1,
                "dimension_min": 512,
                "dimension_max": 2048,
                "result_retention_hours": 24,
            },
            pricing={"status": "PRICE_UNVERIFIED", "unit": "IMAGE"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "requires_artifact_sink": True,
            },
        ),
        ModelManifest(
            id=MAINLAND_PRIMARY_MANIFEST_IDS[CapabilityKind.VIDEO],
            display_name="Wan 2.7 I2V (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.VIDEO,
            protocol=ProtocolFamily.ASYNC_TASK,
            model_id="wan2.7-i2v-2026-04-25",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            codec_id="dashscope.wan.i2v.v1",
            input_modalities=("text", "image"),
            output_modalities=("video",),
            supported_modes=("image_to_video",),
            duration={"minimum": 2, "maximum": 15},
            resolution=("720P", "1080P"),
            reference={"images": True, "videos": False, "max_count": 1},
            supports={"first_frame": True, "cancellation": False},
            authorization=_authorization(),
            readiness=dashscope_media_ready,
            selection_policy={"priority": 10, "profile": "MAINLAND_CHEAP"},
            limits={
                "duration_integer_only": True,
                "result_retention_hours": 24,
                "create_retry_safe": False,
            },
            lifecycle={
                "persist_remote_identity_before_poll": True,
                "poll_without_paid_create": True,
                "reconcile_resubmits": False,
            },
            pricing={"status": "PRICE_UNVERIFIED", "unit": "GENERATED_SECOND"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "legacy_implementation": "WanProductionAdapter",
                "migration_action": "WRAP",
                "runtime_provider_id": "WAN_VIDEO",
                "requires_artifact_sink": True,
                "supports_process_local_input_resolver": True,
            },
        ),
        ModelManifest(
            id=MAINLAND_PRIMARY_MANIFEST_IDS[CapabilityKind.VISION],
            display_name="Qwen3 VL Flash (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.VISION,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="qwen3-vl-flash",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            codec_id="dashscope.qwen.vl.v1",
            input_modalities=("text", "image", "video"),
            output_modalities=("text", "json"),
            supported_modes=("media_analysis", "structured_output"),
            reference={"images": True, "videos": True, "max_count": 16},
            supports={"multi_reference": True, "structured_output": True},
            authorization=_authorization(),
            readiness=dashscope_ready,
            selection_policy={"priority": 10, "profile": "MAINLAND_CHEAP"},
            limits={"direct_video_model_dependent": True},
            pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "supports_process_local_input_resolver": True,
            },
        ),
        ModelManifest(
            id=MAINLAND_PRIMARY_MANIFEST_IDS[CapabilityKind.TTS],
            display_name="Qwen3 TTS Flash (Mainland China)",
            provider_id="alibaba_model_studio",
            capability=CapabilityKind.TTS,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="qwen3-tts-flash",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DASHSCOPE_CN",
            endpoint_profile_id=DASHSCOPE_CN_ENDPOINT_PROFILE,
            credential_reference="DASHSCOPE_API_KEY",
            codec_id="dashscope.qwen.tts.v1",
            input_modalities=("text",),
            output_modalities=("audio",),
            supported_modes=("speech_synthesis",),
            authorization=_authorization(),
            readiness=dashscope_media_ready,
            selection_policy={"priority": 10, "profile": "MAINLAND_CHEAP"},
            limits={"result_count": 1},
            pricing={"status": "PRICE_UNVERIFIED", "unit": "CHARACTERS"},
            metadata={
                "provider_family": "ALIBABA",
                "region_scope": "MAINLAND_ONLY",
                "requires_artifact_sink": True,
            },
        ),
        # Existing Mainland-compatible implementations remain explicit
        # alternatives.  Neither is part of automatic primary selection.
        ModelManifest(
            id=MAINLAND_COMPATIBILITY_MANIFEST_IDS["DEEPSEEK_LLM"],
            display_name="DeepSeek V4 Pro (Mainland endpoint)",
            provider_id="deepseek",
            capability=CapabilityKind.LLM,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            model_id="deepseek-v4-pro",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="DEEPSEEK_CN_PUBLIC",
            endpoint_profile_id=DEEPSEEK_CN_ENDPOINT_PROFILE,
            credential_reference="DEEPSEEK_API_KEY",
            codec_id="openai.chat.v1",
            input_modalities=("text",),
            output_modalities=("text", "json"),
            supported_modes=("chat", "structured_output"),
            supports={"structured_output": True},
            authorization=_authorization(),
            readiness=deepseek_ready,
            selection_policy={"priority": 30, "requires_explicit_selection": True},
            pricing={"status": "PRICE_UNVERIFIED", "unit": "TOKENS"},
            metadata={
                "migration_action": "KEEP",
                "region_scope": "MAINLAND_ONLY",
                "runtime_provider_id": "MPT_LLM",
                "runtime_endpoint_profile_id": "runtime:LLM:MPT_LLM:deepseek:default",
                "runtime_endpoint_class": "MPT_LLM_DEEPSEEK_DEFAULT",
            },
        ),
        ModelManifest(
            id=MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"],
            display_name="Seedance 2.5 (Beijing)",
            provider_id="volcengine_ark",
            capability=CapabilityKind.VIDEO,
            protocol=ProtocolFamily.ASYNC_TASK,
            model_id="doubao-seedance-2-5-260628",
            deployment_region="MAINLAND_CHINA",
            endpoint_class="ARK_CN_BEIJING",
            endpoint_profile_id=ARK_CN_BEIJING_ENDPOINT_PROFILE,
            credential_reference="ARK_API_KEY",
            codec_id="ark.seedance.v1",
            input_modalities=("text", "image", "video", "audio"),
            output_modalities=("video", "image"),
            supported_modes=("text_to_video", "reference_to_video"),
            duration={"minimum": 4, "maximum": 30},
            resolution=("480P", "720P", "1080P"),
            reference={"images": True, "videos": True, "max_count": 50},
            supports={
                "first_frame": True,
                "last_frame": True,
                "multi_reference": True,
                "audio_reference": True,
                "cancellation": False,
            },
            authorization=_authorization(),
            readiness=ark_ready,
            selection_policy={"priority": 40, "requires_explicit_selection": True},
            limits={
                "duration_integer_only": True,
                "task_retention_days": 7,
                "result_retention_hours": 24,
                "create_retry_safe": False,
            },
            lifecycle={
                "persist_remote_identity_before_poll": True,
                "poll_without_paid_create": True,
                "reconcile_resubmits": False,
            },
            pricing={"status": "PRICE_UNVERIFIED", "unit": "PROVIDER_USAGE"},
            metadata={
                "legacy_implementation": "SeedanceProductionAdapter",
                "migration_action": "WRAP",
                "region_scope": "MAINLAND_ONLY",
                "runtime_provider_id": "SEEDANCE",
                "runtime_endpoint_profile_id": (
                    "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING"
                ),
                "runtime_endpoint_class": "ARK_CN_BEIJING",
                "requires_artifact_sink": True,
                "supports_process_local_input_resolver": True,
            },
        ),
    )


MAINLAND_MANIFESTS = build_mainland_manifests()


__all__ = [
    "ARK_CN_BEIJING_ENDPOINT_PROFILE",
    "DASHSCOPE_CN_ENDPOINT_PROFILE",
    "DEEPSEEK_CN_ENDPOINT_PROFILE",
    "MAINLAND_COMPATIBILITY_MANIFEST_IDS",
    "MAINLAND_MANIFESTS",
    "MAINLAND_MIGRATION_CLASSIFICATION",
    "MAINLAND_PRIMARY_MANIFEST_IDS",
    "build_mainland_manifests",
]
