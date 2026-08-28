"""Universal request/response runtime for structured LLM product actions.

The creative services need the model's raw text briefly in memory so their
existing parsers can distinguish invalid JSON from a valid domain object and
perform the one allowed repair.  The universal result contract deliberately
does not persist raw responses, therefore this adapter retains the text only
inside the request transport until the request/response driver succeeds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import uuid4

from .codecs import JsonProviderCodec
from .contracts import (
    CapabilityKind,
    CapabilityRequest,
    DriverResponse,
    ProtocolFamily,
)
from .drivers import DriverError, RequestResponseDriver
from .builtins import default_manifest_registry
from .resolver import (
    ModelResolver,
    ModelResolutionError,
    ResolvedModel,
    compatibility_registry,
)


class UniversalLLMRuntimeError(RuntimeError):
    """A fail-closed model-resolution or protocol invocation error."""


@dataclass(frozen=True, slots=True)
class UniversalLLMSelection:
    """Frozen manifest identity and the capability object selected for it."""

    resolved: ResolvedModel
    provider: object

    @property
    def provider_id(self) -> str:
        return self.resolved.provider_id

    @property
    def model_id(self) -> str:
        return self.resolved.model_id

    @property
    def manifest_id(self) -> str:
        return self.resolved.manifest_id

    @property
    def manifest_hash(self) -> str:
        return self.resolved.manifest_hash

    @property
    def protocol(self) -> ProtocolFamily:
        return self.resolved.protocol


class _LegacyLLMTransport:
    """Protocol transport over a selected capability object.

    The transport intentionally exposes only the neutral request/response
    envelope to the driver.  Text never appears in ``CapabilityResult`` or
    durable metadata; it is consumed immediately by ``take_text``.
    """

    def __init__(self, provider: object) -> None:
        self.provider = provider
        self._texts: dict[str, str] = {}
        self.last_error: Exception | None = None

    def send(self, _encoded: object, request: CapabilityRequest) -> DriverResponse:
        generate = getattr(self.provider, "generate_json_text", None)
        if not callable(generate):
            raise UniversalLLMRuntimeError(
                "selected universal LLM provider has no structured text operation"
            )
        try:
            raw = generate(str(request.prompt_or_text or ""))
        except Exception as exc:
            self.last_error = exc
            raise
        if not isinstance(raw, str) or not raw.strip():
            raise UniversalLLMRuntimeError("LLM structured generation returned empty text")
        text = raw.strip()
        self._texts[request.request_id] = text
        return DriverResponse(
            status_code=200,
            payload={
                "request_id": request.request_id,
                "outcome": "SUCCEEDED",
                "safe_metadata": {
                    "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "response_length": len(text),
                },
            },
        )

    def take_text(self, request_id: str) -> str:
        try:
            return self._texts.pop(request_id)
        except KeyError as exc:
            raise UniversalLLMRuntimeError(
                "universal LLM runtime completed without a transient response"
            ) from exc


class UniversalLLMRuntime:
    """Resolve an LLM manifest and execute it through the universal protocol.

    Legacy capability objects remain the provider implementation during this
    migration, but routing is no longer a direct service-to-provider call:
    capability profile -> compatibility manifest -> model resolver ->
    REQUEST_RESPONSE driver -> provider-neutral result envelope.
    """

    def __init__(
        self,
        legacy_registry: object,
        *,
        manifest_registry: object | None = None,
    ) -> None:
        self.legacy_registry = legacy_registry
        self.manifest_registry, self.bridge = compatibility_registry(legacy_registry)
        self.resolver = ModelResolver(self.manifest_registry)
        self.native_manifest_registry = manifest_registry or default_manifest_registry(
            include_placeholders=False
        )
        self.native_resolver = ModelResolver(self.native_manifest_registry)

    def resolve(self, profile: object) -> UniversalLLMSelection:
        profile_metadata = getattr(profile, "profile", {})
        canonical_manifest_id = (
            str(profile_metadata.get("manifest_id") or "").strip()
            if isinstance(profile_metadata, dict)
            else ""
        )
        if canonical_manifest_id:
            try:
                resolved = self.native_resolver.resolve(
                    capability=CapabilityKind.LLM,
                    manifest_id=canonical_manifest_id,
                    provider_id=str(getattr(profile, "provider_id")),
                    model_id=str(getattr(profile, "model_id")),
                    endpoint_profile_id=str(getattr(profile, "endpoint_profile_id")),
                    deployment_region=str(
                        getattr(
                            getattr(profile, "deployment_region"),
                            "value",
                            "UNSPECIFIED",
                        )
                    ),
                    protocol=ProtocolFamily.REQUEST_RESPONSE,
                    require_available=False,
                )
                expected_hash = str(profile_metadata.get("manifest_hash") or "")
                if expected_hash and expected_hash != resolved.manifest_hash:
                    raise ModelResolutionError("selected manifest hash changed")
                provider = self._native_provider_for(resolved)
            except (AttributeError, ModelResolutionError, TypeError, ValueError) as exc:
                raise UniversalLLMRuntimeError(
                    "selected canonical LLM manifest is unavailable; no provider fallback"
                ) from exc
            return UniversalLLMSelection(resolved=resolved, provider=provider)
        try:
            resolved = self.resolver.resolve(
                capability=CapabilityKind.LLM,
                provider_id=str(getattr(profile, "provider_id")),
                model_id=str(getattr(profile, "model_id")),
                endpoint_profile_id=str(getattr(profile, "endpoint_profile_id")),
                deployment_region=str(
                    getattr(getattr(profile, "deployment_region"), "value", "UNSPECIFIED")
                ),
                protocol=ProtocolFamily.REQUEST_RESPONSE,
                require_available=True,
            )
            provider = self.bridge.provider_for(resolved)
        except (AttributeError, ModelResolutionError, TypeError, ValueError) as exc:
            raise UniversalLLMRuntimeError(
                "selected LLM manifest is unavailable; no provider fallback"
            ) from exc
        return UniversalLLMSelection(resolved=resolved, provider=provider)

    def _native_provider_for(self, resolved: ResolvedModel) -> object:
        providers = getattr(self.legacy_registry, "list", None)
        if not callable(providers):
            raise ModelResolutionError("native provider registry is unavailable")
        for provider in providers("LLM"):
            if str(getattr(provider, "provider_name", "")) != resolved.provider_id:
                continue
            invoke = getattr(provider, "invoke_universal_text", None)
            if not callable(invoke):
                continue
            status = getattr(provider, "status", None)
            metadata = dict(getattr(status, "metadata", {}) or {})
            if (
                getattr(status, "available", False) is True
                and str(metadata.get("model") or "") == resolved.model_id
                and str(metadata.get("endpoint_profile_id") or "")
                == resolved.endpoint_profile_id
                and str(metadata.get("credential_reference") or "")
                == str(getattr(resolved.manifest, "credential_reference", "") or "")
            ):
                return provider
        raise ModelResolutionError(
            "canonical LLM provider is not configured; no provider fallback"
        )

    def invoke_text(
        self,
        selection: UniversalLLMSelection,
        prompt: str,
        *,
        project_id: str,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise UniversalLLMRuntimeError("LLM prompt is required")
        if selection.protocol is not ProtocolFamily.REQUEST_RESPONSE:
            raise UniversalLLMRuntimeError("LLM manifest protocol is not request/response")
        native_invoke = getattr(selection.provider, "invoke_universal_text", None)
        if callable(native_invoke):
            try:
                return native_invoke(
                    selection,
                    prompt,
                    project_id=project_id,
                )
            except Exception as exc:
                raise UniversalLLMRuntimeError(str(exc)) from exc
        request = CapabilityRequest(
            request_id=uuid4().hex,
            project_id=project_id,
            capability=CapabilityKind.LLM,
            protocol_family=selection.protocol,
            provider_id=selection.provider_id,
            model_id=selection.model_id,
            manifest_id=selection.manifest_id,
            manifest_hash=selection.manifest_hash,
            codec_id=str(getattr(selection.resolved.manifest, "codec_id", "legacy.compatibility")),
            prompt_or_text=prompt,
            # A normal creative generation is an explicit product action;
            # this is distinct from the acceptance-only paid live-smoke flag.
            create_authorized=True,
        )
        transport = _LegacyLLMTransport(selection.provider)
        codec = JsonProviderCodec(codec_id=request.codec_id)
        driver = RequestResponseDriver(transport, manifest=selection.resolved.manifest)
        try:
            result = driver.invoke(
                request,
                codec,
                authorization={"create_authorized": True},
            )
        except (DriverError, ValueError, TypeError) as exc:
            if transport.last_error is not None:
                raise UniversalLLMRuntimeError(str(transport.last_error)) from exc
            raise UniversalLLMRuntimeError("universal LLM request/response invocation failed") from exc
        if not result.succeeded:
            raise UniversalLLMRuntimeError("universal LLM did not return success")
        return transport.take_text(request.request_id)


__all__ = [
    "UniversalLLMRuntime",
    "UniversalLLMRuntimeError",
    "UniversalLLMSelection",
]
