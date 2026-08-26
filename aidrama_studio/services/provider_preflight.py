"""Zero-network acceptance preflight for the five V1 AI capabilities.

The preflight inspects only frozen provider-selection records and local
``CapabilityStatus`` metadata. It never invokes a provider method, validates a
credential remotely, or exposes a credential value (including its length,
prefix, suffix, or hash).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    default_capability_registry,
)
from .provider_profiles import ProviderProfileService


_PREFLIGHT_FIELDS = {
    CapabilityKind.LLM: "LLM_PROFILE_READY",
    CapabilityKind.IMAGE: "IMAGE_PROFILE_READY",
    CapabilityKind.VIDEO_GENERATIVE: "VIDEO_PROFILE_READY",
    CapabilityKind.VISION: "VISION_PROFILE_READY",
    CapabilityKind.TTS: "TTS_PROFILE_READY",
}

# Seedance 2.5 is deliberately an opt-in runtime.  Keep this contract local
# to the offline gate so a malformed/tampered runtime profile cannot silently
# inherit the generic 2--15 second video fallback.
_SEEDANCE_SUPPORTED_DURATIONS = list(range(4, 31))


@dataclass(frozen=True, slots=True)
class OfflineProfilePreflight:
    capability: str
    ready: bool
    provider_id: str
    model_id: str
    endpoint_profile_id: str
    endpoint_class: str
    deployment_region: str
    provider_region: str
    credential_name: str | None
    credential_status: str
    paid_authorization_status: str
    provider_constraints_status: str
    detail: str

    def as_public_dict(self) -> dict[str, object]:
        """Return names/status only; no secret-derived characteristics."""

        return {
            "capability": self.capability,
            "ready": self.ready,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "endpoint_profile_id": self.endpoint_profile_id,
            "endpoint_class": self.endpoint_class,
            "deployment_region": self.deployment_region,
            "provider_region": self.provider_region,
            "credential": {
                "name": self.credential_name,
                "status": self.credential_status,
            },
            "paid_authorization": self.paid_authorization_status,
            "provider_constraints": self.provider_constraints_status,
            "detail": self.detail,
        }


class OfflineLivePreflightService:
    """Evaluate exact selected profiles without performing network I/O."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        if provider_profiles is not None:
            self.provider_profiles = provider_profiles
            # A caller commonly injects a profile service as the source of
            # truth and omits ``registry``.  Reusing a separate default
            # registry here would make exact profile matching fail (or, worse,
            # inspect a different runtime inventory).  Derive the registry
            # from the injected service whenever possible.
            self.registry = (
                registry
                or getattr(provider_profiles, "registry", None)
                or default_capability_registry()
            )
            # A preflight must inspect the same immutable runtime inventory
            # that resolved the selected profile. If callers provide both
            # objects, an explicit registry is authoritative; silently
            # resolving against one registry and checking status in another
            # could report a false pass (or false failure) after a provider
            # configuration change.
            if getattr(provider_profiles, "registry", None) is not self.registry:
                provider_profiles.registry = self.registry
        else:
            self.registry = registry or default_capability_registry()
            self.provider_profiles = ProviderProfileService(
                self.repository,
                registry=self.registry,
            )

    def run(
        self, project_id: str | None = None
    ) -> tuple[OfflineProfilePreflight, ...]:
        return tuple(
            self._check(project_id, capability) for capability in _PREFLIGHT_FIELDS
        )

    def snapshot(self, project_id: str | None = None) -> dict[str, object]:
        checks = self.run(project_id)
        return {
            **{
                _PREFLIGHT_FIELDS[CapabilityKind(item.capability)]: item.ready
                for item in checks
            },
            "profiles": {
                item.capability: item.as_public_dict() for item in checks
            },
        }

    def report_lines(self, project_id: str | None = None) -> tuple[str, ...]:
        snapshot = self.snapshot(project_id)
        return tuple(
            f"{field}={'PASS' if bool(snapshot[field]) else 'FAIL'}"
            for field in _PREFLIGHT_FIELDS.values()
        )

    def _check(
        self,
        project_id: str | None,
        capability: CapabilityKind,
    ) -> OfflineProfilePreflight:
        try:
            resolved = self.provider_profiles.resolve(
                project_id,
                capability,
                require_available=False,
            )
        except Exception:
            return self._unavailable(
                capability,
                # Provider/profile exceptions are intentionally not copied
                # into the public report; they may contain arbitrary secret
                # text or signed URLs.
                detail="profile resolution failed",
                constraints="ERROR",
            )
        profile = resolved.profile
        if profile is None:
            return self._unavailable(
                capability,
                detail="exact provider profile is not selected",
            )
        matched = self._status_for_profile(profile)
        if matched is None:
            return OfflineProfilePreflight(
                capability=capability.value,
                ready=False,
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                endpoint_profile_id=profile.endpoint_profile_id,
                endpoint_class=profile.endpoint_class,
                deployment_region=profile.deployment_region.value,
                provider_region=profile.deployment_region.value,
                credential_name=profile.credential_reference,
                credential_status=(
                    "UNKNOWN" if profile.credential_reference else "NOT_REQUIRED"
                ),
                paid_authorization_status="UNKNOWN",
                provider_constraints_status="ERROR",
                detail="selected provider runtime metadata is unavailable",
            )
        status, provider = matched
        metadata = dict(status.metadata or {})
        identity_ready = bool(
            profile.provider_id
            and profile.model_id
            and profile.endpoint_profile_id not in {"", "LEGACY"}
            and profile.endpoint_class not in {"", "UNSPECIFIED"}
            and profile.deployment_region.value != "UNSPECIFIED"
        )
        credential_name = profile.credential_reference
        if credential_name:
            # A credential alias requires an explicit boolean proof from the
            # provider status. Inferring presence from ``configured`` or
            # ``available`` turns malformed/legacy metadata into a false-ready
            # live profile and can hide a missing secret.
            raw_credential_present = metadata.get("credential_present")
            # Status metadata is a contract, not a truthy UI hint.  Values
            # such as the string ``"false"`` must not become a false-ready
            # credential pass.
            credential_present = raw_credential_present is True
            credential_status = "PRESENT" if credential_present else "MISSING"
        else:
            credential_present = True
            credential_status = "NOT_REQUIRED"
        deployment_region = str(
            metadata.get("deployment_region")
            or profile.deployment_region.value
            or "UNSPECIFIED"
        ).upper()
        if deployment_region == "LOCAL":
            # Local runtimes do not submit a remote/paid request and may omit
            # the acceptance-only authorization marker.
            authorized = True
            authorization_status = "NOT_REQUIRED"
        elif "live_authorized" in metadata:
            # Remote acceptance paths must opt in explicitly.  Truthiness is
            # intentionally not enough: only the literal boolean ``True`` is
            # an authorization signal.
            authorized = metadata.get("live_authorized") is True
            authorization_status = "AUTHORIZED" if authorized else "MISSING"
        else:
            # Missing authorization metadata for a remote profile is unknown,
            # never an implicit pass.
            authorized = False
            authorization_status = "MISSING"
        constraints_ready = self._constraints_ready(
            status,
            provider,
            provider_id=profile.provider_id,
            profile_metadata=profile.profile,
        )
        if capability is CapabilityKind.TTS and str(
            metadata.get("upstream_provider_id") or ""
        ) == "AZURE_SPEECH":
            constraints_ready = bool(
                constraints_ready
                and metadata.get("region_configured") is True
                and metadata.get("runtime_ready") is True
                and str(metadata.get("voice") or "").strip()
            )
        ready = bool(
            identity_ready
            and resolved.configured
            and resolved.available
            and credential_present
            and authorized
            and constraints_ready
        )
        # Do not carry arbitrary provider error text into the public preflight
        # record. Even after redaction, an unknown credential-like value could
        # survive a provider's custom message. These bounded reasons preserve
        # useful state while guaranteeing that only the credential alias (when
        # needed) crosses the boundary.
        if not credential_present:
            detail = f"{credential_name or 'credential'} is not configured"
        elif not authorized and deployment_region != "LOCAL":
            detail = "paid live authorization is required"
        elif not identity_ready:
            detail = "provider identity is incomplete"
        elif not constraints_ready:
            detail = "provider constraints are invalid or unavailable"
        elif not resolved.configured:
            detail = "provider is not configured"
        elif not resolved.available:
            detail = "provider is unavailable"
        else:
            detail = "configured"
        return OfflineProfilePreflight(
            capability=capability.value,
            ready=ready,
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            endpoint_profile_id=profile.endpoint_profile_id,
            endpoint_class=profile.endpoint_class,
            deployment_region=profile.deployment_region.value,
            provider_region=str(
                metadata.get("service_region")
                or metadata.get("provider_region")
                or profile.deployment_region.value
            ),
            credential_name=credential_name,
            credential_status=credential_status,
            paid_authorization_status=authorization_status,
            provider_constraints_status=("PASS" if constraints_ready else "ERROR"),
            detail=detail,
        )

    def _status_for_profile(
        self, profile
    ) -> tuple[CapabilityStatus, object] | None:
        for provider in self.registry.list(profile.capability):
            if str(getattr(provider, "provider_name", "")).casefold() != (
                profile.provider_id.casefold()
            ):
                continue
            try:
                status = provider.status
            except Exception:
                return None
            try:
                metadata = dict(status.metadata or {})
            except (AttributeError, TypeError, ValueError):
                return None
            runtime_model = metadata.get("model")
            if not isinstance(runtime_model, str) or not runtime_model.strip():
                # Exact model identity is part of the frozen profile. Missing
                # metadata is not equivalent to the legacy string ``runtime``
                # at this acceptance boundary.
                continue
            if runtime_model != profile.model_id:
                continue
            # A selected profile is only valid when the runtime publishes the
            # exact same endpoint identity.  Missing metadata is not a match;
            # accepting it would let a stale/ambiguous endpoint pass preflight.
            endpoint = str(metadata.get("endpoint_profile_id") or "")
            if endpoint != profile.endpoint_profile_id:
                continue
            endpoint_class = str(metadata.get("endpoint_class") or "UNSPECIFIED")
            if endpoint_class != profile.endpoint_class:
                continue
            region = str(metadata.get("deployment_region") or "UNSPECIFIED")
            if region != profile.deployment_region.value:
                continue
            runtime_credential = str(metadata.get("credential_reference") or "") or None
            if runtime_credential != profile.credential_reference:
                # Credential aliases are part of the selected profile identity;
                # accepting a different or omitted alias could report a stale
                # secret source as ready.
                continue
            return status, provider
        return None

    @staticmethod
    def _constraints_ready(
        status: CapabilityStatus,
        provider: object,
        *,
        provider_id: str | None = None,
        profile_metadata: Mapping[str, object] | None = None,
    ) -> bool:
        raw_metadata = getattr(status, "metadata", {})
        if not isinstance(raw_metadata, Mapping):
            return False
        metadata: Mapping[str, object] = raw_metadata
        if "provider_constraints_valid" in metadata:
            # Only the literal boolean True is a positive assertion.  A
            # malformed string such as ``"false"`` must fail closed.
            explicit = metadata.get("provider_constraints_valid") is True
        else:
            reason = str(status.reason or "").casefold()
            invalid_markers = (
                "invalid",
                "error",
                "failed",
                "mismatch",
                "无效",
                "错误",
                "失败",
                "不匹配",
            )
            explicit = not any(marker in reason for marker in invalid_markers)
        if not explicit:
            return False

        # Seedance must never be treated as the generic video fallback.  The
        # runtime and the selected persisted profile must both prove the exact
        # opt-in marker and the official integer duration set.
        provider_names = {
            str(provider_id or "").strip().casefold(),
            str(status.provider or "").strip().casefold(),
            str(getattr(provider, "provider_name", "")).strip().casefold(),
            str(
                getattr(getattr(provider, "adapter", None), "provider_id", "")
            ).strip().casefold(),
        }
        if provider_names & {"seedance", "seedance_video"}:
            for source in (metadata, profile_metadata or {}):
                if source.get("requires_explicit_selection") is not True:
                    return False
                if source.get("minimum_duration_seconds") != 4:
                    return False
                if source.get("maximum_duration_seconds") != 30:
                    return False
                supported = source.get("supported_durations")
                if supported != _SEEDANCE_SUPPORTED_DURATIONS:
                    return False

        config = getattr(provider, "config", None)
        if config is None:
            config = getattr(getattr(provider, "adapter", None), "config", None)
        if config is None:
            # Config-less boundaries (the mature MPT seams and the local stock
            # runtime) have no declarative config object for this helper to
            # inspect.  Keep their existing status contract, but never let a
            # third-party/unknown provider become ready merely because it
            # omitted a config object. Test-only providers are explicitly not
            # live-smoke capable.
            if metadata.get("test_only") is True:
                return False
            if metadata.get("provider_constraints_valid") is True:
                return True
            return bool(
                provider_names
                & {"mpt_llm", "mpt_tts", "mpt_stock", "mpt_stock_video"}
            )
        model = str(getattr(config, "model", "") or "").strip()
        if hasattr(config, "model") and not model:
            return False
        base_url = str(getattr(config, "base_url", "") or "").strip()
        if not base_url:
            return OfflineLivePreflightService._known_config_constraints(
                config,
                provider_id=provider_id,
                metadata=metadata,
            )
        try:
            parsed = urlsplit(base_url.rstrip("/"))
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
        ):
            return False
        expected_hosts = {
            "OPENAI_PUBLIC": "api.openai.com",
            "GOOGLE_GEMINI_PUBLIC": "generativelanguage.googleapis.com",
            "DASHSCOPE_CN": "dashscope.aliyuncs.com",
            "ARK_CN_BEIJING": "ark.cn-beijing.volces.com",
        }
        expected_paths = {
            "OPENAI_PUBLIC": {"/v1"},
            "GOOGLE_GEMINI_PUBLIC": {"/v1", "/v1beta"},
            "DASHSCOPE_CN": {"/api/v1"},
            "ARK_CN_BEIJING": {"/api/v3"},
        }
        endpoint_class = str(metadata.get("endpoint_class") or "")
        expected_host = expected_hosts.get(endpoint_class)
        if expected_host and parsed.hostname.casefold() != expected_host:
            return False
        expected_path = expected_paths.get(endpoint_class)
        if expected_path and parsed.path.rstrip("/") not in expected_path:
            return False
        return OfflineLivePreflightService._known_config_constraints(
            config,
            provider_id=provider_id,
            metadata=metadata,
        )

    @staticmethod
    def _known_config_constraints(
        config: object,
        *,
        provider_id: str | None,
        metadata: Mapping[str, object],
    ) -> bool:
        """Validate built-in config fields without invoking arbitrary code.

        A preflight is an offline inspection boundary.  Calling ``validate``
        on an injected/third-party config object could perform I/O or trigger
        paid work, so only declarative fields on known built-in config types
        are inspected here.
        """

        qualified_name = f"{type(config).__module__}.{type(config).__name__}"
        provider_value = str(provider_id or metadata.get("provider") or "").casefold()

        def positive(value: object) -> bool:
            if isinstance(value, bool):
                return False
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return False
            return (
                number > 0
                and number == number
                and number not in {float("inf"), float("-inf")}
            )

        if qualified_name.endswith(".OpenAIImageProviderConfig"):
            model = str(getattr(config, "model", "") or "").strip().casefold()
            return bool(
                model.startswith("gpt-image-")
                and positive(getattr(config, "timeout_seconds", 0))
            )

        if qualified_name.endswith(".GeminiVisionProviderConfig"):
            if not positive(getattr(config, "timeout_seconds", 0)):
                return False
            if not positive(getattr(config, "poll_interval_seconds", 0)):
                return False
            if not positive(getattr(config, "max_processing_seconds", 0)):
                return False
            if not positive(getattr(config, "max_file_bytes", 0)):
                return False
            if not positive(getattr(config, "max_total_upload_bytes", 0)):
                return False
            base_url = str(getattr(config, "base_url", "") or "").rstrip("/")
            try:
                path = urlsplit(base_url).path.rstrip("/")
            except ValueError:
                return False
            return path in {"/v1", "/v1beta"}

        if qualified_name.endswith(".SeedanceProviderConfig") or provider_value == "seedance":
            try:
                max_reference_images = int(getattr(config, "max_reference_images", 0))
            except (TypeError, ValueError, OverflowError):
                return False
            if not 1 <= max_reference_images <= 20:
                return False
            return all(
                positive(getattr(config, field, 0))
                for field in (
                    "timeout_seconds",
                    "max_reference_image_bytes",
                    "max_request_media_bytes",
                    "max_download_bytes",
                )
            )

        if qualified_name.endswith(".WanProviderConfig") or provider_value == "wan_video":
            duration = getattr(config, "duration_seconds", None)
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
            ):
                return False
            try:
                duration_int = int(duration)
            except (TypeError, ValueError, OverflowError):
                return False
            if duration_int != duration or not 2 <= duration_int <= 15:
                return False
            resolution = str(getattr(config, "resolution", "")).upper()
            if resolution not in {"720P", "1080P"}:
                return False
            return positive(getattr(config, "request_timeout_seconds", 0)) and positive(
                getattr(config, "max_download_bytes", 0)
            )

        # Unknown providers must publish an explicit declarative constraint
        # assertion. No arbitrary validator callback is executed, and a
        # missing assertion is not a readiness pass merely because a model
        # and endpoint string happen to look plausible.
        return metadata.get("provider_constraints_valid") is True

    @staticmethod
    def _unavailable(
        capability: CapabilityKind,
        *,
        detail: str,
        constraints: str = "NOT_CHECKED",
    ) -> OfflineProfilePreflight:
        return OfflineProfilePreflight(
            capability=capability.value,
            ready=False,
            provider_id="UNSELECTED",
            model_id="UNSELECTED",
            endpoint_profile_id="UNSELECTED",
            endpoint_class="UNSELECTED",
            deployment_region="UNSELECTED",
            provider_region="UNSELECTED",
            credential_name=None,
            credential_status="NOT_CHECKED",
            paid_authorization_status="NOT_CHECKED",
            provider_constraints_status=constraints,
            detail=detail,
        )


__all__ = ["OfflineLivePreflightService", "OfflineProfilePreflight"]
