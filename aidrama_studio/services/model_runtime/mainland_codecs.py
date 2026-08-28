"""Provider codecs for the first Mainland Universal Runtime slice.

All Alibaba, DashScope, DeepSeek, Wan, and Seedance request/response JSON is
owned here.  Protocol drivers and product services see only frozen universal
requests, encoded requests, and provider-neutral results.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import ipaddress
import json
import math
from typing import Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .codecs import (
    CodecError,
    MalformedProviderResult,
    validate_request_against_manifest,
)
from .contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    DriverResponse,
    DriverStatus,
    EncodedRequest,
    ProtocolFamily,
    ProviderTaskIdentity,
    RuntimeOutcome,
)
from .manifest import ModelManifest


_MAX_INLINE_ARTIFACT_BYTES = 64 * 1024 * 1024
_ALIBABA_MAINLAND_RESULT_HOST_SUFFIXES = (
    ".oss-cn-beijing.aliyuncs.com",
    ".oss-cn-hangzhou.aliyuncs.com",
    ".oss-cn-shanghai.aliyuncs.com",
    ".oss-cn-shenzhen.aliyuncs.com",
)
_SEEDANCE_MAINLAND_RESULT_HOSTS = frozenset(
    {"ark-content-generation-v2-cn-beijing.tos-cn-beijing.volces.com"}
)


@runtime_checkable
class ProviderArtifactSink(Protocol):
    """Persist an ephemeral provider result before it crosses the codec seam."""

    def persist_bytes(
        self,
        data: bytes,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef: ...

    def persist_remote(
        self,
        url: str,
        *,
        request_id: str,
        role: str,
        mime_type: str,
        safe_metadata: Mapping[str, object],
    ) -> ContentRef: ...


@runtime_checkable
class ProviderInputResolver(Protocol):
    """Resolve durable content identity to a process-local provider URI."""

    def resolve(self, reference: ContentRef) -> str: ...


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MalformedProviderResult(f"{name} must be an object")
    return value


def _sequence(value: object, *, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise MalformedProviderResult(f"{name} must be an array")
    return value


def _non_empty_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MalformedProviderResult(f"{name} must be non-empty text")
    return value.strip()


def _request_text(request: CapabilityRequest) -> str:
    value = request.prompt_or_text
    if not isinstance(value, str) or not value.strip():
        raise CodecError("prompt_or_text is required")
    return value.strip()


def _manifest_model(request: CapabilityRequest, manifest: ModelManifest | None) -> str:
    value = request.model_id or (manifest.model_id if manifest is not None else "")
    if not value:
        raise CodecError("a frozen model identity is required")
    return value


def _validate_common(
    request: CapabilityRequest,
    manifest: ModelManifest | None,
    *,
    codec_id: str,
    capability: CapabilityKind,
    protocol: ProtocolFamily,
) -> None:
    validate_request_against_manifest(request, manifest, codec_id=codec_id)
    if request.capability is not capability:
        raise CodecError(f"{codec_id} requires {capability.value}")
    if request.protocol_family is not protocol:
        raise CodecError(f"{codec_id} requires {protocol.value}")
    _manifest_model(request, manifest)


def _optional_mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CodecError(f"{name} must be an object")
    return value


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CodecError(f"{name} must be finite")
    return result


def _manifest_duration_bounds(
    manifest: ModelManifest | Mapping[str, object] | None,
    *,
    defaults: tuple[float, float] = (4.0, 30.0),
) -> tuple[float, float]:
    """Read duration authority from the selected manifest, not product code."""

    raw = getattr(manifest, "duration", None)
    if isinstance(manifest, Mapping):
        raw = manifest.get("duration", raw)
    if raw is None:
        return defaults
    minimum = getattr(raw, "minimum", None)
    maximum = getattr(raw, "maximum", None)
    if isinstance(raw, Mapping):
        minimum = raw.get("minimum", raw.get("min", minimum))
        maximum = raw.get("maximum", raw.get("max", maximum))
    try:
        lower = float(minimum) if minimum is not None else defaults[0]
        upper = float(maximum) if maximum is not None else defaults[1]
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodecError("Seedance manifest duration contract is malformed") from exc
    if not math.isfinite(lower) or not math.isfinite(upper) or lower < 0 or lower > upper:
        raise CodecError("Seedance manifest duration contract is malformed")
    return lower, upper


def _generation_parameters(request: CapabilityRequest) -> dict[str, object]:
    """Map provider-neutral generation controls to provider JSON fields."""

    raw = request.provider_parameters
    result: dict[str, object] = {}
    for key in ("temperature", "top_p"):
        if key in raw:
            number = _finite_number(raw[key], name=key)
            if number < 0:
                raise CodecError(f"{key} must be non-negative")
            result[key] = number
    if "max_output_tokens" in raw:
        value = raw["max_output_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CodecError("max_output_tokens must be a positive integer")
        result["max_tokens"] = value
    if "seed" in raw:
        value = raw["seed"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodecError("seed must be a non-negative integer")
        result["seed"] = value
    return result


def _messages(request: CapabilityRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    system_prompt = request.structured_input.get("system_prompt")
    if system_prompt is not None:
        messages.append(
            {"role": "system", "content": _non_empty_text(system_prompt, name="system_prompt")}
        )
    messages.append({"role": "user", "content": _request_text(request)})
    return messages


def _response_schema(request: CapabilityRequest) -> Mapping[str, Any] | None:
    value = request.structured_input.get("response_schema")
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise CodecError("response_schema must be a non-empty object")
    return value


def _safe_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "image_count",
        "characters",
        "duration_seconds",
    }
    result: dict[str, int | float] = {}
    for key, raw in value.items():
        if str(key) not in allowed or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        if isinstance(raw, float) and not math.isfinite(raw):
            continue
        result[str(key)] = raw
    return result


def _structured_text_metadata(
    text: str,
    request: CapabilityRequest,
    *,
    finish_reason: object = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"text": text}
    if finish_reason is not None:
        metadata["finish_reason"] = str(finish_reason)
    if _response_schema(request) is not None:
        try:
            structured = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise MalformedProviderResult("provider structured output is not valid JSON") from exc
        if not isinstance(structured, (Mapping, list)):
            raise MalformedProviderResult("provider structured output must be an object or array")
        metadata["structured_output"] = structured
    return metadata


def _dashscope_text_response(
    response: DriverResponse,
    request: CapabilityRequest,
) -> CapabilityResult:
    payload = _mapping(response.payload, name="DashScope response")
    output = _mapping(payload.get("output"), name="DashScope output")
    finish_reason: object = None
    text: object = output.get("text")
    choices = output.get("choices")
    if choices is not None:
        items = _sequence(choices, name="DashScope choices")
        if not items:
            raise MalformedProviderResult("DashScope choices must not be empty")
        choice = _mapping(items[0], name="DashScope choice")
        message = _mapping(choice.get("message"), name="DashScope message")
        text = message.get("content")
        finish_reason = choice.get("finish_reason")
    if isinstance(text, Sequence) and not isinstance(text, (str, bytes, bytearray)):
        parts: list[str] = []
        for item in text:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        text = "".join(parts)
    normalized = _non_empty_text(text, name="DashScope response text")
    return CapabilityResult(
        request_id=request.request_id,
        outcome=RuntimeOutcome.SUCCEEDED,
        usage=_safe_usage(payload.get("usage")),
        safe_metadata=_structured_text_metadata(
            normalized, request, finish_reason=finish_reason
        ),
    )


def _validate_http_success(response: DriverResponse, provider: str) -> None:
    if response.status_code < 200 or response.status_code >= 300:
        raise MalformedProviderResult(
            f"{provider} returned HTTP-like status {response.status_code}"
        )


def _provider_input_uri(
    reference: ContentRef,
    resolver: ProviderInputResolver | None = None,
) -> str:
    value = (
        resolver.resolve(reference) if resolver is not None else reference.source_id
    )
    if not isinstance(value, str) or not value.strip():
        raise CodecError("provider input resolver returned no URI")
    value = value.strip()
    if value.startswith("data:"):
        if resolver is None:
            raise CodecError("data URIs must come from a process-local input resolver")
        if ";base64," not in value:
            raise CodecError("provider data URI must use base64 encoding")
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CodecError("provider media reference URI is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (resolver is None and parsed.query)
    ):
        raise CodecError(
            "provider media reference must be a safe HTTPS URI or use an input resolver"
        )
    return value


def _validate_ephemeral_result_url(
    value: object,
    *,
    provider: str,
) -> str:
    """Accept dynamic provider storage hosts without accepting local targets."""

    url = _non_empty_text(value, name=f"{provider} result URL")
    if "\\" in url or any(ord(character) < 32 for character in url):
        raise MalformedProviderResult(f"{provider} result URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise MalformedProviderResult(f"{provider} result URL is invalid") from exc
    raw_hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not raw_hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise MalformedProviderResult(
            f"{provider} result URL is outside the public HTTPS scope"
        )
    if raw_hostname == "localhost" or raw_hostname.endswith(".localhost"):
        raise MalformedProviderResult(
            f"{provider} result URL is outside the public HTTPS scope"
        )
    try:
        literal = ipaddress.ip_address(raw_hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise MalformedProviderResult(
            f"{provider} result URL is outside the public HTTPS scope"
        )
    return url


def _validate_result_url(
    value: object,
    *,
    provider: str,
    exact_hosts: frozenset[str] | None = None,
    allowed_suffixes: tuple[str, ...] = (),
) -> str:
    url = _validate_ephemeral_result_url(value, provider=provider)
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    allowed = bool(exact_hosts and hostname in exact_hosts) or any(
        hostname.endswith(suffix) for suffix in allowed_suffixes
    )
    if not allowed:
        raise MalformedProviderResult(
            f"{provider} result URL is outside the selected Mainland endpoint scope"
        )
    return url


def _decode_base64(value: object, *, provider: str) -> bytes:
    encoded = _non_empty_text(value, name=f"{provider} base64 result")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise MalformedProviderResult(f"{provider} base64 result is malformed") from exc
    if not data or len(data) > _MAX_INLINE_ARTIFACT_BYTES:
        raise MalformedProviderResult(f"{provider} inline artifact size is invalid")
    return data


def _persist_remote(
    sink: ProviderArtifactSink | None,
    url: str,
    *,
    request: CapabilityRequest,
    role: str,
    mime_type: str,
    provider: str,
) -> ContentRef:
    if sink is None:
        raise MalformedProviderResult(
            f"{provider} result requires an artifact sink before success can be reported"
        )
    result = sink.persist_remote(
        url,
        request_id=request.request_id,
        role=role,
        mime_type=mime_type,
        safe_metadata={"provider": provider},
    )
    if not isinstance(result, ContentRef):
        raise MalformedProviderResult("artifact sink did not return ContentRef")
    return result


def _persist_bytes(
    sink: ProviderArtifactSink | None,
    data: bytes,
    *,
    request: CapabilityRequest,
    role: str,
    mime_type: str,
    provider: str,
) -> ContentRef:
    if sink is None:
        raise MalformedProviderResult(
            f"{provider} result requires an artifact sink before success can be reported"
        )
    result = sink.persist_bytes(
        data,
        request_id=request.request_id,
        role=role,
        mime_type=mime_type,
        safe_metadata={"provider": provider},
    )
    if not isinstance(result, ContentRef):
        raise MalformedProviderResult("artifact sink did not return ContentRef")
    return result


@dataclass(frozen=True, slots=True)
class DashScopeQwenChatCodec:
    codec_id: str = "dashscope.qwen.chat.v1"
    codec_version: str = "1"
    path: str = "/services/aigc/text-generation/generation"

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.LLM,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
        )
        _request_text(request)
        if request.inputs:
            raise CodecError("Qwen text chat does not accept media inputs")

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        parameters = {"result_format": "message", **_generation_parameters(request)}
        schema = _response_schema(request)
        if schema is not None:
            parameters["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(
                        request.structured_input.get("schema_name") or "aidrama_response"
                    ),
                    "strict": True,
                    "schema": dict(schema),
                },
            }
        return EncodedRequest(
            payload={
                "model": _manifest_model(request, manifest),
                "input": {"messages": _messages(request)},
                "parameters": parameters,
            },
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        _validate_http_success(response, "DashScope Qwen")
        return _dashscope_text_response(response, request)


@dataclass(frozen=True, slots=True)
class OpenAIChatCodec:
    """Strict OpenAI Chat codec used by the preserved DeepSeek seam."""

    codec_id: str = "openai.chat.v1"
    codec_version: str = "1"
    path: str = "/chat/completions"

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.LLM,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
        )
        _request_text(request)
        if request.inputs:
            raise CodecError("OpenAI Chat text codec does not accept media inputs")

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        payload: dict[str, object] = {
            "model": _manifest_model(request, manifest),
            "messages": _messages(request),
            **_generation_parameters(request),
        }
        schema = _response_schema(request)
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        return EncodedRequest(
            payload=payload,
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        _validate_http_success(response, "OpenAI-compatible Chat")
        payload = _mapping(response.payload, name="OpenAI-compatible response")
        choices = _sequence(payload.get("choices"), name="OpenAI-compatible choices")
        if not choices:
            raise MalformedProviderResult("OpenAI-compatible choices must not be empty")
        choice = _mapping(choices[0], name="OpenAI-compatible choice")
        message = _mapping(choice.get("message"), name="OpenAI-compatible message")
        text = _non_empty_text(message.get("content"), name="OpenAI-compatible content")
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            usage=_safe_usage(payload.get("usage")),
            safe_metadata=_structured_text_metadata(
                text, request, finish_reason=choice.get("finish_reason")
            ),
        )


@dataclass(frozen=True, slots=True)
class DashScopeQwenVisionCodec:
    codec_id: str = "dashscope.qwen.vl.v1"
    codec_version: str = "1"
    path: str = "/services/aigc/multimodal-generation/generation"
    input_resolver: ProviderInputResolver | None = field(
        default=None, repr=False, compare=False
    )

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.VISION,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
        )
        _request_text(request)
        if not request.inputs:
            raise CodecError("Qwen Vision requires at least one media input")
        for item in request.inputs:
            if not (
                item.mime_type.startswith("image/") or item.mime_type.startswith("video/")
            ):
                raise CodecError("Qwen Vision accepts only image or video inputs")
            _provider_input_uri(item, self.input_resolver)

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        content: list[dict[str, object]] = []
        for item in request.inputs:
            key = "video" if item.mime_type.startswith("video/") else "image"
            content.append({key: _provider_input_uri(item, self.input_resolver)})
        content.append({"text": _request_text(request)})
        parameters: dict[str, object] = _generation_parameters(request)
        schema = _response_schema(request)
        if schema is not None:
            parameters["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(
                        request.structured_input.get("schema_name") or "aidrama_vision_qc"
                    ),
                    "strict": True,
                    "schema": dict(schema),
                },
            }
        return EncodedRequest(
            payload={
                "model": _manifest_model(request, manifest),
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            },
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        _validate_http_success(response, "DashScope Qwen Vision")
        return _dashscope_text_response(response, request)


@dataclass(frozen=True, slots=True)
class DashScopeZImageCodec:
    codec_id: str = "dashscope.zimage.v1"
    codec_version: str = "1"
    path: str = "/services/aigc/multimodal-generation/generation"
    artifact_sink: ProviderArtifactSink | None = field(
        default=None, repr=False, compare=False
    )

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.IMAGE,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
        )
        _request_text(request)
        if request.inputs:
            raise CodecError("Z-Image Turbo V1 is text-only")

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        parameters: dict[str, object] = {}
        size = request.provider_parameters.get("resolution", "1024*1024")
        if not isinstance(size, str):
            raise CodecError("Z-Image dimensions must be integers from 512 to 2048")
        dimensions = size.split("*")
        if len(dimensions) != 2 or not all(item.isdecimal() for item in dimensions):
            raise CodecError("Z-Image dimensions must be integers from 512 to 2048")
        width, height = (int(item) for item in dimensions)
        if (
            size != f"{width}*{height}"
            or not 512 <= width <= 2048
            or not 512 <= height <= 2048
        ):
            raise CodecError("Z-Image dimensions must be integers from 512 to 2048")
        parameters["size"] = size
        if "seed" in request.provider_parameters:
            seed = request.provider_parameters["seed"]
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                raise CodecError("seed must be a non-negative integer")
            parameters["seed"] = seed
        if "prompt_extend" in request.provider_parameters:
            prompt_extend = request.provider_parameters["prompt_extend"]
            if not isinstance(prompt_extend, bool):
                raise CodecError("prompt_extend must be boolean")
            parameters["prompt_extend"] = prompt_extend
        return EncodedRequest(
            payload={
                "model": _manifest_model(request, manifest),
                "input": {
                    "messages": [
                        {"role": "user", "content": [{"text": _request_text(request)}]}
                    ]
                },
                "parameters": parameters,
            },
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        _validate_http_success(response, "DashScope Z-Image")
        payload = _mapping(response.payload, name="DashScope image response")
        candidates = self._result_candidates(payload)
        if len(candidates) != 1:
            raise MalformedProviderResult("DashScope image response must contain one result")
        kind, value = candidates[0]
        if kind == "url":
            url = _validate_ephemeral_result_url(
                value,
                provider="DashScope Z-Image",
            )
            output = _persist_remote(
                self.artifact_sink,
                url,
                request=request,
                role="generated_image",
                mime_type="image/png",
                provider="alibaba_model_studio",
            )
        else:
            output = _persist_bytes(
                self.artifact_sink,
                _decode_base64(value, provider="DashScope Z-Image"),
                request=request,
                role="generated_image",
                mime_type="image/png",
                provider="alibaba_model_studio",
            )
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(output,),
            usage=_safe_usage(payload.get("usage")),
            safe_metadata={"candidate_status": "DRAFT"},
        )

    @staticmethod
    def _result_candidates(payload: Mapping[str, Any]) -> list[tuple[str, object]]:
        output = payload.get("output")
        result: list[tuple[str, object]] = []
        if isinstance(output, Mapping):
            choices = output.get("choices")
            if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        continue
                    message = choice.get("message")
                    if not isinstance(message, Mapping):
                        continue
                    content = message.get("content")
                    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                        continue
                    for item in content:
                        if not isinstance(item, Mapping):
                            continue
                        for key in ("image", "url"):
                            if item.get(key):
                                result.append(("url", item[key]))
                        if item.get("b64_json"):
                            result.append(("base64", item["b64_json"]))
            results = output.get("results")
            if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
                for item in results:
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("url"):
                        result.append(("url", item["url"]))
                    elif item.get("b64_json"):
                        result.append(("base64", item["b64_json"]))
        return result


def _async_output(payload: object, *, provider: str) -> Mapping[str, Any]:
    root = _mapping(payload, name=f"{provider} response")
    output = root.get("output")
    return output if isinstance(output, Mapping) else root


def _async_task_id(payload: Mapping[str, Any], *, provider: str) -> str:
    for key in ("task_id", "taskId", "id"):
        if payload.get(key):
            return _non_empty_text(payload[key], name=f"{provider} task identity")
    raise MalformedProviderResult(f"{provider} response has no stable task identity")


def _async_status(
    payload: Mapping[str, Any],
    *,
    provider: str,
    aliases: Mapping[str, RuntimeOutcome],
) -> tuple[RuntimeOutcome, str | None]:
    raw = payload.get("task_status", payload.get("status", payload.get("state")))
    state = _non_empty_text(raw, name=f"{provider} task status").upper()
    outcome = aliases.get(state)
    if outcome is None:
        raise MalformedProviderResult(f"{provider} returned an unknown task status")
    task_id: str | None = None
    for key in ("task_id", "taskId", "id"):
        if payload.get(key):
            task_id = _non_empty_text(payload[key], name=f"{provider} task identity")
            break
    return outcome, task_id


@dataclass(frozen=True, slots=True)
class DashScopeWanI2VCodec:
    codec_id: str = "dashscope.wan.i2v.v1"
    codec_version: str = "1"
    path: str = "/services/aigc/video-generation/video-synthesis"
    artifact_sink: ProviderArtifactSink | None = field(
        default=None, repr=False, compare=False
    )
    input_resolver: ProviderInputResolver | None = field(
        default=None, repr=False, compare=False
    )

    _STATUS_ALIASES: ClassVar[Mapping[str, RuntimeOutcome]] = {
        "PENDING": RuntimeOutcome.RUNNING,
        "QUEUED": RuntimeOutcome.RUNNING,
        "SUBMITTED": RuntimeOutcome.RUNNING,
        "RUNNING": RuntimeOutcome.RUNNING,
        "PROCESSING": RuntimeOutcome.RUNNING,
        "SUCCEEDED": RuntimeOutcome.SUCCEEDED,
        "SUCCESS": RuntimeOutcome.SUCCEEDED,
        "COMPLETED": RuntimeOutcome.SUCCEEDED,
        "FAILED": RuntimeOutcome.FAILED,
        "ERROR": RuntimeOutcome.FAILED,
        "CANCELLED": RuntimeOutcome.CANCELLED,
        "CANCELED": RuntimeOutcome.CANCELLED,
        "UNKNOWN": RuntimeOutcome.FAILED,
    }

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.VIDEO,
            protocol=ProtocolFamily.ASYNC_TASK,
        )
        _request_text(request)
        if len(request.inputs) != 1 or not request.inputs[0].mime_type.startswith("image/"):
            raise CodecError("Wan I2V requires exactly one first-frame image")
        _provider_input_uri(request.inputs[0], self.input_resolver)
        self._duration_resolution(request)

    @staticmethod
    def _duration_resolution(request: CapabilityRequest) -> tuple[int, str]:
        raw_duration = request.provider_parameters.get("duration_seconds", 5)
        duration = _finite_number(raw_duration, name="duration_seconds")
        if not duration.is_integer() or not 2 <= int(duration) <= 15:
            raise CodecError("Wan duration_seconds must be an integer from 2 to 15")
        resolution = str(request.provider_parameters.get("resolution", "720P")).upper()
        if resolution not in {"720P", "1080P"}:
            raise CodecError("Wan resolution must be 720P or 1080P")
        return int(duration), resolution

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        duration, resolution = self._duration_resolution(request)
        return EncodedRequest(
            payload={
                "model": _manifest_model(request, manifest),
                "input": {
                    "prompt": _request_text(request),
                    "media": [
                        {
                            "type": "first_frame",
                            "url": _provider_input_uri(
                                request.inputs[0], self.input_resolver
                            ),
                        }
                    ],
                },
                "parameters": {"duration": duration, "resolution": resolution},
            },
            method="POST",
            path=self.path,
            headers={
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        del response, request
        raise CodecError("Wan uses the ASYNC_TASK lifecycle")

    def decode_task_identity(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> ProviderTaskIdentity:
        del request
        _validate_http_success(response, "DashScope Wan")
        task_id = _async_task_id(
            _async_output(response.payload, provider="DashScope Wan"),
            provider="DashScope Wan",
        )
        return ProviderTaskIdentity(
            protocol_reference=task_id,
            provider_task_id=task_id,
            safe_metadata={"remote_identity_persist_required": True},
        )

    def decode_task_state(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        del request
        _validate_http_success(response, "DashScope Wan")
        output = _async_output(response.payload, provider="DashScope Wan")
        outcome, task_id = _async_status(
            output, provider="DashScope Wan", aliases=self._STATUS_ALIASES
        )
        if task_id is not None and task_id != reference:
            raise MalformedProviderResult("DashScope Wan task identity changed")
        error_category = "PROVIDER_TASK_FAILED" if outcome is RuntimeOutcome.FAILED else None
        return DriverStatus(
            protocol_reference=reference,
            provider_task_id=task_id or reference,
            outcome=outcome,
            error_category=error_category,
            retryable=False,
        )

    def decode_result(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        _validate_http_success(response, "DashScope Wan")
        output = _async_output(response.payload, provider="DashScope Wan")
        outcome, task_id = _async_status(
            output, provider="DashScope Wan", aliases=self._STATUS_ALIASES
        )
        if outcome is not RuntimeOutcome.SUCCEEDED:
            raise MalformedProviderResult("DashScope Wan result is not successful")
        if task_id is not None and task_id != reference:
            raise MalformedProviderResult("DashScope Wan task identity changed")
        raw_url: object = output.get("video_url")
        results = output.get("results")
        if raw_url is None and isinstance(results, Sequence) and results:
            first = results[0]
            if isinstance(first, Mapping):
                raw_url = first.get("url", first.get("video_url"))
        url = _validate_ephemeral_result_url(
            raw_url,
            provider="DashScope Wan",
        )
        artifact = _persist_remote(
            self.artifact_sink,
            url,
            request=request,
            role="generated_video",
            mime_type="video/mp4",
            provider="alibaba_model_studio",
        )
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            protocol_reference=reference,
            provider_task_id=task_id or reference,
            outputs=(artifact,),
            usage=_safe_usage(response.payload.get("usage") if isinstance(response.payload, Mapping) else None),
        )

    def encode_cancel(
        self, reference: str, request: CapabilityRequest | None = None
    ) -> EncodedRequest:
        del reference, request
        raise CodecError("Wan I2V cancellation is not supported")


@dataclass(frozen=True, slots=True)
class ArkSeedanceCodec:
    codec_id: str = "ark.seedance.v1"
    codec_version: str = "1"
    path: str = "/contents/generations/tasks"
    artifact_sink: ProviderArtifactSink | None = field(
        default=None, repr=False, compare=False
    )
    input_resolver: ProviderInputResolver | None = field(
        default=None, repr=False, compare=False
    )

    _STATUS_ALIASES: ClassVar[Mapping[str, RuntimeOutcome]] = {
        "WAITING": RuntimeOutcome.RUNNING,
        "QUEUED": RuntimeOutcome.RUNNING,
        "SUBMITTED": RuntimeOutcome.RUNNING,
        "PROCESSING": RuntimeOutcome.RUNNING,
        "RUNNING": RuntimeOutcome.RUNNING,
        "IN_PROGRESS": RuntimeOutcome.RUNNING,
        "COMPLETED": RuntimeOutcome.SUCCEEDED,
        "SUCCEEDED": RuntimeOutcome.SUCCEEDED,
        "SUCCESS": RuntimeOutcome.SUCCEEDED,
        "FAILED": RuntimeOutcome.FAILED,
        "ERROR": RuntimeOutcome.FAILED,
        "CANCELLED": RuntimeOutcome.CANCELLED,
        "CANCELED": RuntimeOutcome.CANCELLED,
        "EXPIRED": RuntimeOutcome.FAILED,
    }

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.VIDEO,
            protocol=ProtocolFamily.ASYNC_TASK,
        )
        _request_text(request)
        if len(request.inputs) > 50:
            raise CodecError("Seedance accepts at most 50 media references")
        for item in request.inputs:
            if not item.mime_type.startswith(("image/", "video/", "audio/")):
                raise CodecError("Seedance media reference type is unsupported")
            _provider_input_uri(item, self.input_resolver)
        minimum, maximum = _manifest_duration_bounds(manifest)
        duration = _finite_number(
            request.provider_parameters.get("duration_seconds", minimum),
            name="duration_seconds",
        )
        if not duration.is_integer() or not minimum <= duration <= maximum:
            raise CodecError(
                "Seedance duration_seconds must be an integer from "
                f"{minimum:g} to {maximum:g}"
            )

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        content: list[dict[str, object]] = [
            {"type": "text", "text": _request_text(request)}
        ]
        type_keys = {
            "image": ("image_url", "reference_image"),
            "video": ("video_url", "reference_video"),
            "audio": ("audio_url", "reference_audio"),
        }
        for item in request.inputs:
            media_kind = item.mime_type.split("/", 1)[0]
            value_key, default_role = type_keys[media_kind]
            role = item.role.strip() or default_role
            content.append(
                {
                    "type": value_key,
                    value_key: {
                        "url": _provider_input_uri(item, self.input_resolver)
                    },
                    "role": role,
                }
            )
        parameters = request.provider_parameters
        minimum, _maximum = _manifest_duration_bounds(manifest)
        duration = int(
            _finite_number(parameters.get("duration_seconds", minimum), name="duration_seconds")
        )
        resolution = str(parameters.get("resolution", "720P")).lower()
        if resolution not in {"480p", "720p", "1080p"}:
            raise CodecError("Seedance resolution must be 480P, 720P, or 1080P")
        ratio = str(parameters.get("aspect_ratio", "16:9"))
        if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4", "adaptive"}:
            raise CodecError("Seedance aspect_ratio is unsupported")
        payload: dict[str, object] = {
            "model": _manifest_model(request, manifest),
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
            "generate_audio": parameters.get("generate_audio") is True,
            "watermark": parameters.get("watermark") is True,
            "output_format": "mp4",
            "return_last_frame": parameters.get("return_last_frame") is True,
        }
        if "seed" in parameters:
            seed = parameters["seed"]
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                raise CodecError("Seedance seed must be a non-negative integer")
            payload["seed"] = seed
        return EncodedRequest(
            payload=payload,
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        del response, request
        raise CodecError("Seedance uses the ASYNC_TASK lifecycle")

    def decode_task_identity(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> ProviderTaskIdentity:
        del request
        _validate_http_success(response, "Ark Seedance")
        task_id = _async_task_id(
            _async_output(response.payload, provider="Ark Seedance"),
            provider="Ark Seedance",
        )
        return ProviderTaskIdentity(
            protocol_reference=task_id,
            provider_task_id=task_id,
            safe_metadata={"remote_identity_persist_required": True},
        )

    def decode_task_state(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest | None = None,
    ) -> DriverStatus:
        del request
        _validate_http_success(response, "Ark Seedance")
        output = _async_output(response.payload, provider="Ark Seedance")
        outcome, task_id = _async_status(
            output, provider="Ark Seedance", aliases=self._STATUS_ALIASES
        )
        if task_id is not None and task_id != reference:
            raise MalformedProviderResult("Ark Seedance task identity changed")
        return DriverStatus(
            protocol_reference=reference,
            provider_task_id=task_id or reference,
            outcome=outcome,
            error_category=(
                "PROVIDER_TASK_FAILED" if outcome is RuntimeOutcome.FAILED else None
            ),
            retryable=False,
        )

    def decode_result(
        self,
        response: DriverResponse,
        reference: str,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        _validate_http_success(response, "Ark Seedance")
        output = _async_output(response.payload, provider="Ark Seedance")
        outcome, task_id = _async_status(
            output, provider="Ark Seedance", aliases=self._STATUS_ALIASES
        )
        if outcome is not RuntimeOutcome.SUCCEEDED:
            raise MalformedProviderResult("Ark Seedance result is not successful")
        if task_id is not None and task_id != reference:
            raise MalformedProviderResult("Ark Seedance task identity changed")
        content = _mapping(output.get("content"), name="Ark Seedance result content")
        video_url = _validate_result_url(
            content.get("video_url"),
            provider="Ark Seedance",
            exact_hosts=_SEEDANCE_MAINLAND_RESULT_HOSTS,
        )
        outputs = [
            _persist_remote(
                self.artifact_sink,
                video_url,
                request=request,
                role="generated_video",
                mime_type="video/mp4",
                provider="volcengine_ark",
            )
        ]
        if content.get("last_frame_url"):
            last_frame_url = _validate_result_url(
                content["last_frame_url"],
                provider="Ark Seedance",
                exact_hosts=_SEEDANCE_MAINLAND_RESULT_HOSTS,
            )
            outputs.append(
                _persist_remote(
                    self.artifact_sink,
                    last_frame_url,
                    request=request,
                    role="last_frame",
                    mime_type="image/jpeg",
                    provider="volcengine_ark",
                )
            )
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            protocol_reference=reference,
            provider_task_id=task_id or reference,
            outputs=tuple(outputs),
            usage=_safe_usage(output.get("usage")),
        )

    def encode_cancel(
        self, reference: str, request: CapabilityRequest | None = None
    ) -> EncodedRequest:
        del reference, request
        raise CodecError("Seedance cancellation is not supported by the frozen V1 contract")


@dataclass(frozen=True, slots=True)
class DashScopeQwenTTSCodec:
    codec_id: str = "dashscope.qwen.tts.v1"
    codec_version: str = "1"
    path: str = "/services/aigc/multimodal-generation/generation"
    artifact_sink: ProviderArtifactSink | None = field(
        default=None, repr=False, compare=False
    )

    def validate(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> None:
        _validate_common(
            request,
            manifest,
            codec_id=self.codec_id,
            capability=CapabilityKind.TTS,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
        )
        text = _request_text(request)
        if len(text) > 10_000:
            raise CodecError("Qwen TTS text exceeds the V1 request limit")
        if request.inputs:
            raise CodecError("Qwen TTS V1 does not accept media inputs")

    def encode_request(
        self, request: CapabilityRequest, manifest: ModelManifest | None = None
    ) -> EncodedRequest:
        self.validate(request, manifest)
        raw = request.provider_parameters
        voice = str(raw.get("voice", "Cherry")).strip()
        if not voice:
            raise CodecError("Qwen TTS voice is required")
        sample_rate = raw.get("sample_rate", 24_000)
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise CodecError("Qwen TTS sample_rate must be a positive integer")
        audio_format = str(raw.get("audio_format", "mp3")).lower()
        if audio_format not in {"mp3", "wav"}:
            raise CodecError("Qwen TTS audio_format must be mp3 or wav")
        input_payload: dict[str, object] = {
            "text": _request_text(request),
            "voice": voice,
        }
        instruction = request.structured_input.get("instruction")
        if instruction is not None:
            input_payload["instruction"] = _non_empty_text(
                instruction, name="TTS instruction"
            )
        return EncodedRequest(
            payload={
                "model": _manifest_model(request, manifest),
                "input": input_payload,
                "parameters": {
                    "format": audio_format,
                    "sample_rate": sample_rate,
                },
            },
            method="POST",
            path=self.path,
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self, response: DriverResponse, request: CapabilityRequest
    ) -> CapabilityResult:
        _validate_http_success(response, "DashScope Qwen TTS")
        audio_format = str(request.provider_parameters.get("audio_format", "mp3")).lower()
        mime_type = "audio/wav" if audio_format == "wav" else "audio/mpeg"
        if isinstance(response.payload, (bytes, bytearray)):
            artifact = _persist_bytes(
                self.artifact_sink,
                bytes(response.payload),
                request=request,
                role="synthesized_speech",
                mime_type=mime_type,
                provider="alibaba_model_studio",
            )
            usage: Mapping[str, Any] = {}
        else:
            payload = _mapping(response.payload, name="DashScope TTS response")
            output = _mapping(payload.get("output"), name="DashScope TTS output")
            audio = output.get("audio")
            audio_value = audio if isinstance(audio, Mapping) else output
            if audio_value.get("url"):
                url = _validate_result_url(
                    audio_value["url"],
                    provider="DashScope Qwen TTS",
                    allowed_suffixes=_ALIBABA_MAINLAND_RESULT_HOST_SUFFIXES,
                )
                artifact = _persist_remote(
                    self.artifact_sink,
                    url,
                    request=request,
                    role="synthesized_speech",
                    mime_type=mime_type,
                    provider="alibaba_model_studio",
                )
            else:
                raw_data = audio_value.get("data", audio_value.get("b64_json"))
                artifact = _persist_bytes(
                    self.artifact_sink,
                    _decode_base64(raw_data, provider="DashScope Qwen TTS"),
                    request=request,
                    role="synthesized_speech",
                    mime_type=mime_type,
                    provider="alibaba_model_studio",
                )
            usage = _safe_usage(payload.get("usage"))
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(artifact,),
            usage=usage,
        )


def build_mainland_codecs(
    *,
    artifact_sink: ProviderArtifactSink | None = None,
    input_resolver: ProviderInputResolver | None = None,
) -> dict[str, object]:
    """Return one codec instance per registered Mainland codec identity."""

    codecs: tuple[object, ...] = (
        DashScopeQwenChatCodec(),
        OpenAIChatCodec(),
        DashScopeQwenVisionCodec(input_resolver=input_resolver),
        DashScopeZImageCodec(artifact_sink=artifact_sink),
        DashScopeWanI2VCodec(
            artifact_sink=artifact_sink, input_resolver=input_resolver
        ),
        ArkSeedanceCodec(
            artifact_sink=artifact_sink, input_resolver=input_resolver
        ),
        DashScopeQwenTTSCodec(artifact_sink=artifact_sink),
    )
    return {str(getattr(codec, "codec_id")): codec for codec in codecs}


__all__ = [
    "ArkSeedanceCodec",
    "DashScopeQwenChatCodec",
    "DashScopeQwenTTSCodec",
    "DashScopeQwenVisionCodec",
    "DashScopeWanI2VCodec",
    "DashScopeZImageCodec",
    "OpenAIChatCodec",
    "ProviderArtifactSink",
    "ProviderInputResolver",
    "build_mainland_codecs",
]
