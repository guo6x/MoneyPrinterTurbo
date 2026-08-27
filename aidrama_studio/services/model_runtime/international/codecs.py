"""Provider codecs for the first international Universal Runtime slice.

Only this module knows the OpenAI Chat wire field names.  Compatibility
codecs carry a frozen capability request to an existing mature provider
boundary; they do not reinterpret or duplicate that provider's payload.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..codecs import CodecError, MalformedProviderResult, validate_request_against_manifest
from ..contracts import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    DriverResponse,
    EncodedRequest,
    ProtocolFamily,
    RuntimeOutcome,
    thaw,
)
from ..manifest import ModelManifest
from .manifests import OPENAI_CHAT_CODEC_ID


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dumped
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            return dumped
    raise MalformedProviderResult(f"{label} is not a mapping")


def _finite_number(value: object, *, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{label} must be numeric")
    if not math.isfinite(float(value)):
        raise CodecError(f"{label} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class OpenAICompatibleChatCodec:
    """Non-streaming OpenAI Chat Completions codec.

    Generic compatibility is deliberately narrow: one text prompt, optional
    provider-neutral generation controls, and optional JSON Schema only when
    the selected manifest explicitly declares structured-output support.
    Responses API state, media encoding, tools, streaming, and provider
    extensions are not inferred from Chat compatibility.
    """

    codec_id: str = OPENAI_CHAT_CODEC_ID
    codec_version: str = "1"

    def validate(
        self,
        request: CapabilityRequest,
        manifest: ModelManifest | None = None,
    ) -> None:
        validate_request_against_manifest(request, manifest, codec_id=self.codec_id)
        if request.capability is not CapabilityKind.LLM:
            raise CodecError("OpenAI Chat codec supports only LLM")
        if request.protocol_family is not ProtocolFamily.REQUEST_RESPONSE:
            raise CodecError("OpenAI Chat codec requires REQUEST_RESPONSE")
        if not isinstance(request.prompt_or_text, str) or not request.prompt_or_text.strip():
            raise CodecError("LLM prompt must not be empty")
        if request.inputs:
            raise CodecError("OpenAI Chat V1 bridge accepts text input only")

        allowed_structured = {"system_instruction", "output_schema"}
        unknown_structured = set(request.structured_input) - allowed_structured
        if unknown_structured:
            raise CodecError(
                "unsupported provider-neutral LLM input fields: "
                + ", ".join(sorted(unknown_structured))
            )
        system_instruction = request.structured_input.get("system_instruction")
        if system_instruction is not None and (
            not isinstance(system_instruction, str) or not system_instruction.strip()
        ):
            raise CodecError("system_instruction must be non-empty text")
        output_schema = request.structured_input.get("output_schema")
        if output_schema is not None:
            if not isinstance(output_schema, Mapping):
                raise CodecError("output_schema must be a mapping")
            if manifest is None or not manifest.supports.structured_output:
                raise CodecError("selected manifest does not declare structured output")

        allowed_parameters = {
            "temperature",
            "top_p",
            "max_output_tokens",
            "stop_sequences",
        }
        unknown_parameters = set(request.provider_parameters) - allowed_parameters
        if unknown_parameters:
            raise CodecError(
                "unsupported provider-neutral generation parameters: "
                + ", ".join(sorted(unknown_parameters))
            )
        for name in ("temperature", "top_p"):
            value = request.provider_parameters.get(name)
            if value is not None:
                number = float(_finite_number(value, label=name))
                if name == "temperature" and not 0 <= number <= 2:
                    raise CodecError("temperature must be between 0 and 2")
                if name == "top_p" and not 0 <= number <= 1:
                    raise CodecError("top_p must be between 0 and 1")
        max_output = request.provider_parameters.get("max_output_tokens")
        if max_output is not None and (
            isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0
        ):
            raise CodecError("max_output_tokens must be a positive integer")
        stops = request.provider_parameters.get("stop_sequences")
        if stops is not None:
            if isinstance(stops, (str, bytes)) or not isinstance(stops, Sequence):
                raise CodecError("stop_sequences must be a sequence of text")
            if not stops or len(stops) > 4 or any(
                not isinstance(item, str) or not item for item in stops
            ):
                raise CodecError("stop_sequences must contain one to four non-empty strings")

    def encode_request(
        self,
        request: CapabilityRequest,
        manifest: ModelManifest | None = None,
    ) -> EncodedRequest:
        self.validate(request, manifest)
        model = request.model_id or (manifest.model_id if manifest is not None else "")
        if not model:
            raise CodecError("OpenAI Chat model identity is required")
        messages: list[dict[str, str]] = []
        system_instruction = request.structured_input.get("system_instruction")
        if isinstance(system_instruction, str):
            messages.append({"role": "system", "content": system_instruction.strip()})
        messages.append({"role": "user", "content": str(request.prompt_or_text)})
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        parameters = request.provider_parameters
        if "temperature" in parameters:
            payload["temperature"] = parameters["temperature"]
        if "top_p" in parameters:
            payload["top_p"] = parameters["top_p"]
        if "max_output_tokens" in parameters:
            payload["max_completion_tokens"] = parameters["max_output_tokens"]
        if "stop_sequences" in parameters:
            payload["stop"] = thaw(parameters["stop_sequences"])
        output_schema = request.structured_input.get("output_schema")
        if isinstance(output_schema, Mapping):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "aidrama_output",
                    "strict": True,
                    "schema": thaw(output_schema),
                },
            }
        return EncodedRequest(
            payload=payload,
            method="POST",
            path="/chat/completions",
            headers={"Content-Type": "application/json"},
        )

    def decode_response(
        self,
        response: DriverResponse,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        if not 200 <= response.status_code < 300:
            raise MalformedProviderResult(
                f"OpenAI-compatible Chat returned HTTP {response.status_code}"
            )
        payload = _mapping(response.payload, label="OpenAI-compatible Chat response")
        choices = payload.get("choices")
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence) or not choices:
            raise MalformedProviderResult("OpenAI-compatible Chat response has no choices")
        choice = _mapping(choices[0], label="OpenAI-compatible Chat choice")
        message = _mapping(
            choice.get("message"), label="OpenAI-compatible Chat message"
        )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise MalformedProviderResult("OpenAI-compatible Chat response has no text")

        safe_metadata: dict[str, Any] = {"text": content}
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            safe_metadata["finish_reason"] = finish_reason
        response_id = payload.get("id")
        if isinstance(response_id, str) and response_id:
            safe_metadata["response_id"] = response_id
        response_model = payload.get("model")
        if isinstance(response_model, str) and response_model:
            safe_metadata["response_model"] = response_model

        if isinstance(request.structured_input.get("output_schema"), Mapping):
            try:
                structured = json.loads(content)
            except json.JSONDecodeError as exc:
                raise MalformedProviderResult(
                    "OpenAI-compatible structured response is invalid JSON"
                ) from exc
            if not isinstance(structured, Mapping):
                raise MalformedProviderResult(
                    "OpenAI-compatible structured response is not an object"
                )
            safe_metadata["structured_output"] = dict(structured)

        usage: dict[str, int] = {}
        raw_usage = payload.get("usage")
        if isinstance(raw_usage, Mapping):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens",
                "output_tokens",
            ):
                value = raw_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = value
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            usage=usage,
            safe_metadata=safe_metadata,
        )


@dataclass(frozen=True, slots=True)
class LegacyRequestResponseCodec:
    """Authorization-aware wrapper codec for a stable legacy provider.

    The encoded payload is the already-frozen provider-neutral request.  The
    paired bridge transport owns the call into the legacy boundary and must
    return a :class:`CapabilityResult`; no raw provider payload crosses this
    codec.
    """

    codec_id: str
    capability: CapabilityKind
    codec_version: str = "1"

    def validate(
        self,
        request: CapabilityRequest,
        manifest: ModelManifest | None = None,
    ) -> None:
        validate_request_against_manifest(request, manifest, codec_id=self.codec_id)
        if request.capability is not self.capability:
            raise CodecError(
                f"legacy codec {self.codec_id} requires {self.capability.value}"
            )
        if request.protocol_family is not ProtocolFamily.REQUEST_RESPONSE:
            raise CodecError("legacy wrapper requires REQUEST_RESPONSE")

    def encode_request(
        self,
        request: CapabilityRequest,
        manifest: ModelManifest | None = None,
    ) -> EncodedRequest:
        self.validate(request, manifest)
        return EncodedRequest(payload=request, method="POST")

    def decode_response(
        self,
        response: DriverResponse,
        request: CapabilityRequest,
    ) -> CapabilityResult:
        if not 200 <= response.status_code < 300:
            raise MalformedProviderResult(
                f"legacy provider boundary returned status {response.status_code}"
            )
        if not isinstance(response.payload, CapabilityResult):
            raise MalformedProviderResult(
                "legacy provider boundary did not return CapabilityResult"
            )
        if response.payload.request_id != request.request_id:
            raise MalformedProviderResult("legacy provider request identity changed")
        return response.payload


__all__ = ["LegacyRequestResponseCodec", "OpenAICompatibleChatCodec"]
