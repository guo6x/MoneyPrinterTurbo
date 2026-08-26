"""Capability/provider readiness for the product settings surface.

This service intentionally reports configuration *state*, never secret values.
It does not make network calls or claim that a provider is live merely because
an adapter exists.  The resulting records are suitable for rendering in
Streamlit and for deterministic unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .ai_capabilities import CapabilityKind, CapabilityRegistry, default_capability_registry
from .provider_profiles import ProviderProfileService
from .security import sanitize_error


class ReadinessState(StrEnum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CapabilityReadiness:
    capability: str
    provider: str
    state: ReadinessState
    detail: str

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY

    def as_public_dict(self) -> dict[str, str | bool]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "state": self.state.value,
            "detail": self.detail,
            "ready": self.ready,
        }


class ProviderReadinessService:
    """Report product capability readiness without exposing credentials."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        llm_status=None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ):
        # Preserve ``None`` as a meaningful production sentinel.  The default
        # capability registry uses it to augment environment values from the
        # Windows credential store; an explicitly supplied mapping must remain
        # deterministic for tests and embedding callers.
        self._env = env
        # Injection keeps this service free of import-time config side effects in
        # tests while production uses the existing AIDrama LLM seam.
        self._llm_status = llm_status
        self._registry = registry
        self._provider_profiles = provider_profiles

    def _capability_registry(self) -> CapabilityRegistry:
        # A caller may inject a registry for deterministic tests.  Otherwise
        # construct the same capability inventory used by Settings and the
        # runtime boundary.  Construction performs no network calls.
        if self._registry is not None:
            return self._registry
        if self._provider_profiles is not None:
            injected = getattr(self._provider_profiles, "registry", None)
            if injected is not None:
                return injected
        if self._env is None:
            return default_capability_registry()
        return default_capability_registry(env=self._env)

    def _profile_service(self) -> ProviderProfileService:
        registry = self._capability_registry()
        if self._provider_profiles is None:
            self._provider_profiles = ProviderProfileService(
                registry=registry
            )
        elif getattr(self._provider_profiles, "registry", None) is not registry:
            # Keep an injected profile service and an injected capability
            # registry on one runtime-inventory source. Otherwise readiness
            # can resolve a profile successfully and then report it missing
            # merely because status lookup used a different runtime
            # inventory. An explicit registry is authoritative.
            self._provider_profiles.registry = registry
        return self._provider_profiles

    @staticmethod
    def _unavailable_state(*, configured: bool, detail: str) -> ReadinessState:
        if not configured:
            return ReadinessState.UNAVAILABLE
        normalized = detail.casefold()
        configuration_error_markers = (
            "invalid",
            "error",
            "failed",
            "mismatch",
            "无效",
            "错误",
            "失败",
            "不匹配",
        )
        return (
            ReadinessState.ERROR
            if any(marker in normalized for marker in configuration_error_markers)
            else ReadinessState.UNAVAILABLE
        )

    def _from_registry(
        self,
        capability: CapabilityKind,
        *,
        project_id: str | None = None,
    ) -> CapabilityReadiness:
        try:
            resolved = self._profile_service().resolve(
                project_id,
                capability,
                require_available=False,
            )
        except Exception:
            return CapabilityReadiness(
                capability.value,
                "未配置",
                ReadinessState.ERROR,
                "capability configuration check failed",
            )
        profile = resolved.profile
        if profile is None:
            return CapabilityReadiness(
                capability.value,
                "未配置",
                ReadinessState.UNAVAILABLE,
                sanitize_error(
                    resolved.detail or "capability boundary unavailable"
                ),
            )
        detail = sanitize_error(
            resolved.detail
            or ("ready" if resolved.available else "unavailable")
        )
        # Do not trust a contradictory adapter status.  A provider that says
        # ``available`` while publishing an invalid constraint or an explicit
        # error must never become a normal-user READY state.
        runtime_metadata: Mapping[str, object] = {}
        try:
            runtime_status = self._profile_service()._runtime_status(profile)
            runtime_metadata = dict(
                getattr(runtime_status, "metadata", {}) or {}
            )
        except Exception:
            runtime_metadata = {}
        detail_lower = detail.casefold()
        error_markers = (
            "invalid",
            "error",
            "failed",
            "mismatch",
            "无效",
            "错误",
            "失败",
            "不匹配",
        )
        explicit_error = (
            (
                "provider_constraints_valid" in runtime_metadata
                and runtime_metadata.get("provider_constraints_valid") is not True
            )
            or any(marker in detail_lower for marker in error_markers)
        )
        credential_value = runtime_metadata.get("credential_present")
        credential_required = bool(profile.credential_reference)
        credential_missing = credential_required and credential_value is not True
        malformed_credential = (
            credential_value is not None
            and credential_value is not True
            and credential_value is not False
        )
        runtime_configured = runtime_metadata.get("configured")
        malformed_configured = (
            runtime_configured is not None and runtime_configured is not True
        )
        seedance_invalid = False
        if str(profile.provider_id or "").strip().casefold() in {
            "seedance",
            "seedance_video",
        }:
            expected_durations = list(range(4, 31))
            for source in (runtime_metadata, profile.profile):
                source_values = (
                    source if isinstance(source, Mapping) else {}
                )
                if (
                    source_values.get("requires_explicit_selection") is not True
                    or source_values.get("minimum_duration_seconds") != 4
                    or source_values.get("maximum_duration_seconds") != 30
                    or source_values.get("supported_durations")
                    != expected_durations
                ):
                    seedance_invalid = True
                    break
        # A provider can technically report ``available=True`` while its
        # configuration flag is false (for example, an adapter status seam
        # that only models runtime reachability).  Product readiness must not
        # turn that contradictory state into a false READY claim.
        if explicit_error or seedance_invalid:
            state = ReadinessState.ERROR
        elif (
            credential_missing
            or malformed_credential
            or runtime_configured is False
            or malformed_configured
            or not resolved.configured
        ):
            state = ReadinessState.UNAVAILABLE
        elif resolved.available:
            state = ReadinessState.READY
        else:
            state = self._unavailable_state(
                configured=resolved.configured,
                detail=detail,
            )
        return CapabilityReadiness(
            capability.value,
            profile.provider_id,
            state,
            detail,
        )

    def _llm(self, *, project_id: str | None = None) -> CapabilityReadiness:
        if self._llm_status is not None:
            try:
                ready, detail = self._llm_status()
                safe_detail = sanitize_error(detail or "unavailable")
                normalized_detail = safe_detail.casefold()
                error_markers = (
                    "invalid",
                    "error",
                    "failed",
                    "mismatch",
                    "无效",
                    "错误",
                    "失败",
                    "不匹配",
                )
                state = (
                    ReadinessState.ERROR
                    if any(marker in normalized_detail for marker in error_markers)
                    else ReadinessState.READY
                    if ready is True
                    else ReadinessState.UNAVAILABLE
                )
                return CapabilityReadiness(
                    "LLM",
                    "Configured provider",
                    state,
                    safe_detail,
                )
            except Exception:
                return CapabilityReadiness("LLM", "Configured provider", ReadinessState.ERROR, "LLM 配置检查失败")
        return self._from_registry(CapabilityKind.LLM, project_id=project_id)

    def _video(self, *, project_id: str | None = None) -> CapabilityReadiness:
        return self._from_registry(
            CapabilityKind.VIDEO_GENERATIVE, project_id=project_id
        )

    def _stock_video(self, *, project_id: str | None = None) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.VIDEO_STOCK, project_id=project_id)

    def _image(self, *, project_id: str | None = None) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.IMAGE, project_id=project_id)

    def _vision(self, *, project_id: str | None = None) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.VISION, project_id=project_id)

    def _tts(self, *, project_id: str | None = None) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.TTS, project_id=project_id)

    def list_capabilities(
        self, *, project_id: str | None = None
    ) -> tuple[CapabilityReadiness, ...]:
        return (
            self._llm(project_id=project_id),
            self._image(project_id=project_id),
            self._video(project_id=project_id),
            self._stock_video(project_id=project_id),
            self._vision(project_id=project_id),
            self._tts(project_id=project_id),
        )

    def snapshot(
        self, *, project_id: str | None = None
    ) -> dict[str, dict[str, str | bool]]:
        """Return a stable capability-keyed public snapshot."""

        return {
            item.capability: item.as_public_dict()
            for item in self.list_capabilities(project_id=project_id)
        }


__all__ = ["CapabilityReadiness", "ProviderReadinessService", "ReadinessState"]
