"""First international provider slice for Universal Model Runtime V1.

Importing this package is offline and side-effect free.  Provider SDKs and
legacy services are invoked only after a caller builds an exact binding and
passes the Core create-authorization gate.
"""

from .bridges import (
    AudioResultSink,
    ContentPathResolver,
    INTERNATIONAL_PROVIDER_MIGRATION,
    ImageCandidateSink,
    InternationalRuntimeBinding,
    InternationalRuntimeRegistry,
    OpenAICompatibleSDKTransport,
    OpenAICompatibleTransportConfig,
    build_azure_speech_binding,
    build_edge_tts_binding,
    build_gemini_llm_binding,
    build_gemini_vision_binding,
    build_openai_compatible_llm_binding,
    build_openai_image_binding,
)
from .codecs import LegacyRequestResponseCodec, OpenAICompatibleChatCodec
from .manifests import (
    AZURE_SPEECH_MANIFEST,
    EDGE_TTS_MANIFEST,
    GEMINI_LLM_MANIFEST,
    GEMINI_VISION_MANIFEST,
    INTERNATIONAL_MODEL_MANIFESTS,
    LEGACY_AZURE_SPEECH_CODEC_ID,
    LEGACY_EDGE_TTS_CODEC_ID,
    LEGACY_GEMINI_LLM_CODEC_ID,
    LEGACY_GEMINI_VISION_CODEC_ID,
    LEGACY_OPENAI_IMAGE_CODEC_ID,
    OPENAI_CHAT_CODEC_ID,
    OPENAI_IMAGE_MANIFEST,
    OPENAI_LLM_MANIFEST,
    clone_manifest,
    custom_openai_compatible_manifest,
    international_model_manifests,
    manifest_with_runtime_status,
)


INTERNATIONAL_VIDEO = "DEFERRED"


__all__ = [
    "AZURE_SPEECH_MANIFEST",
    "AudioResultSink",
    "ContentPathResolver",
    "EDGE_TTS_MANIFEST",
    "GEMINI_LLM_MANIFEST",
    "GEMINI_VISION_MANIFEST",
    "INTERNATIONAL_MODEL_MANIFESTS",
    "INTERNATIONAL_PROVIDER_MIGRATION",
    "INTERNATIONAL_VIDEO",
    "ImageCandidateSink",
    "InternationalRuntimeBinding",
    "InternationalRuntimeRegistry",
    "LEGACY_AZURE_SPEECH_CODEC_ID",
    "LEGACY_EDGE_TTS_CODEC_ID",
    "LEGACY_GEMINI_LLM_CODEC_ID",
    "LEGACY_GEMINI_VISION_CODEC_ID",
    "LEGACY_OPENAI_IMAGE_CODEC_ID",
    "LegacyRequestResponseCodec",
    "OPENAI_CHAT_CODEC_ID",
    "OPENAI_IMAGE_MANIFEST",
    "OPENAI_LLM_MANIFEST",
    "OpenAICompatibleChatCodec",
    "OpenAICompatibleSDKTransport",
    "OpenAICompatibleTransportConfig",
    "build_azure_speech_binding",
    "build_edge_tts_binding",
    "build_gemini_llm_binding",
    "build_gemini_vision_binding",
    "build_openai_compatible_llm_binding",
    "build_openai_image_binding",
    "clone_manifest",
    "custom_openai_compatible_manifest",
    "international_model_manifests",
    "manifest_with_runtime_status",
]
