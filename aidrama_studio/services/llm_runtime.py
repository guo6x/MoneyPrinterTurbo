"""Canonical LLM resolution, bounded structured generation and safe audit."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, TypeVar
from uuid import uuid4

from aidrama_studio.storage.repositories import ProjectRepository

from .ai import AIDramaAIError
from .ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    default_capability_registry,
)
from .model_runtime import UniversalLLMRuntime, UniversalLLMRuntimeError, UniversalLLMSelection
from .provider_profiles import ProviderDisclosure, ProviderProfileService
from .runtime_foundation import AIInvocationService
from .security import sanitize_error, sanitize_persistent_metadata


ValidatedValue = TypeVar("ValidatedValue")
LLM_LIVE_SMOKE_PROMPT = "Reply with exactly: OK"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class LLMInvocationError(AIDramaAIError):
    """Safe canonical LLM error suitable for product services."""


class _OutputInvalid(Exception):
    """Carry invalid output in memory only so one repair can be attempted."""

    def __init__(self, raw: str, cause: Exception):
        super().__init__("OUTPUT_INVALID")
        self.raw = raw
        self.cause = cause


@dataclass(frozen=True)
class _FrozenLLMContext:
    provider: object
    universal_runtime: UniversalLLMRuntime
    universal_selection: UniversalLLMSelection
    actual_provider_id: str
    boundary_provider_id: str
    model_id: str
    endpoint_profile_id: str
    deployment_region: str
    endpoint_class: str
    credential_reference: str | None
    selection_source: str
    correlation_id: str
    input_source_ids: tuple[str, ...]
    disclosure: Mapping[str, object]


class LLMInvocationGateway:
    """Resolve one exact LLM endpoint and audit every remote attempt.

    A complete structured operation resolves the selection exactly once. Its
    primary request and optional single repair therefore cannot drift after a
    Settings change. Validation happens before terminal success is recorded.
    Prompts, responses, credentials and provider request dumps are never
    persisted.
    """

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry or default_capability_registry()
        self.provider_profiles = provider_profiles or ProviderProfileService(
            self.repository,
            registry=self.registry,
        )
        self.universal_runtime = UniversalLLMRuntime(self.registry)
        self.invocations = AIInvocationService(self.repository)

    def readiness(self, project_id: str) -> tuple[bool, str]:
        try:
            resolved = self.provider_profiles.resolve(
                project_id,
                CapabilityKind.LLM,
                require_available=True,
            )
        except Exception as exc:
            return False, sanitize_error(exc)
        if resolved.profile is None or not resolved.available:
            return False, resolved.detail
        profile = resolved.profile
        return (
            True,
            f"{profile.provider_id} / {profile.model_id} · "
            f"{profile.deployment_region.value} / {profile.endpoint_class}",
        )

    def run_live_smoke(
        self,
        project_id: str,
        *,
        correlation_id: str | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
    ) -> str:
        """Issue the exact acceptance prompt once, with no repair attempt."""

        context = self._freeze_context(
            project_id,
            input_source_ids=(),
            correlation_id=correlation_id,
            disclosure=disclosure,
        )

        # ``live_authorized`` is intentionally an explicit provider-status
        # signal.  Do this check after exact profile freezing but before the
        # invocation ledger or provider method, guaranteeing zero calls when a
        # remote smoke has not been opt-ed into.  Only LOCAL providers may omit
        # the field; unknown or non-local endpoints fail closed.
        try:
            status_metadata = dict(getattr(context.provider.status, "metadata", {}) or {})
        except Exception as exc:
            raise LLMInvocationError(
                "LLM live smoke readiness check failed;不会调用 Provider"
            ) from exc
        deployment_region = str(
            status_metadata.get("deployment_region") or "UNSPECIFIED"
        ).upper()
        if deployment_region != "LOCAL" and status_metadata.get("live_authorized") is not True:
            raise LLMInvocationError(
                "LLM live smoke requires AIDRAMA_ALLOW_PAID_LIVE_TESTS=1"
            )

        def validate(raw: str) -> str:
            value = raw.strip()
            if value != "OK":
                raise ValueError("LLM smoke response must be exactly OK")
            return value

        try:
            return self._invoke_validated(
                project_id,
                context,
                LLM_LIVE_SMOKE_PROMPT,
                operation="LLM_LIVE_SMOKE",
                attempt_kind="PRIMARY",
                validator=validate,
            )
        except _OutputInvalid as exc:
            raise LLMInvocationError(
                "LLM live smoke response was not exactly OK"
            ) from exc.cause

    def generate_json_text(
        self,
        project_id: str,
        prompt: str,
        *,
        operation: str,
        input_source_ids: tuple[str, ...] | list[str] = (),
        attempt_kind: str = "PRIMARY",
        correlation_id: str | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> str:
        """Run one audited call whose only validation is non-empty text."""

        context = self._freeze_context(
            project_id,
            input_source_ids=input_source_ids,
            correlation_id=correlation_id,
            disclosure=disclosure,
        )

        def validate(raw: str) -> str:
            if not raw.strip():
                raise ValueError("empty structured output")
            return raw.strip()

        try:
            return self._invoke_validated(
                project_id,
                context,
                prompt,
                operation=operation,
                attempt_kind=attempt_kind,
                validator=validate,
                provenance=provenance,
            )
        except _OutputInvalid as exc:
            raise LLMInvocationError(
                "LLM structured generation returned invalid output"
            ) from exc.cause

    def generate_validated_json(
        self,
        project_id: str,
        prompt: str,
        *,
        operation: str,
        validator: Callable[[str], ValidatedValue],
        repair_prompt_builder: Callable[[str, Exception], str] | None = None,
        input_source_ids: tuple[str, ...] | list[str] = (),
        correlation_id: str | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> ValidatedValue:
        """Return domain-validated structured output with at most one repair."""

        if not callable(validator):
            raise LLMInvocationError("LLM validator 无效")
        context = self._freeze_context(
            project_id,
            input_source_ids=input_source_ids,
            correlation_id=correlation_id,
            disclosure=disclosure,
        )
        try:
            return self._invoke_validated(
                project_id,
                context,
                prompt,
                operation=operation,
                attempt_kind="PRIMARY",
                validator=validator,
                provenance=provenance,
            )
        except _OutputInvalid as primary_invalid:
            if repair_prompt_builder is None:
                raise LLMInvocationError(
                    "LLM 输出不符合结构要求"
                ) from primary_invalid.cause
            try:
                repair_prompt = repair_prompt_builder(
                    primary_invalid.raw,
                    primary_invalid.cause,
                )
            except Exception as exc:
                raise LLMInvocationError("LLM 修复提示构建失败") from exc
            if not isinstance(repair_prompt, str) or not repair_prompt.strip():
                raise LLMInvocationError("LLM 修复提示不能为空")
            try:
                return self._invoke_validated(
                    project_id,
                    context,
                    repair_prompt,
                    operation=operation,
                    attempt_kind="REPAIR",
                    validator=validator,
                    provenance=provenance,
                )
            except _OutputInvalid as repair_invalid:
                raise LLMInvocationError(
                    "LLM 输出在一次修复后仍不符合结构要求"
                ) from repair_invalid.cause

    def _freeze_context(
        self,
        project_id: str,
        *,
        input_source_ids: tuple[str, ...] | list[str],
        correlation_id: str | None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None,
    ) -> _FrozenLLMContext:
        if self.repository.get_project(project_id) is None:
            raise LLMInvocationError(f"项目不存在: {project_id}")
        resolved = self.provider_profiles.resolve(
            project_id,
            CapabilityKind.LLM,
            require_available=True,
        )
        profile = resolved.profile
        if profile is None or not resolved.available:
            raise LLMInvocationError(resolved.detail)
        try:
            safe_disclosure = self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.LLM,
                disclosure,
                transmitted_content_types=("TEXT_BRIEF", "TEXT_CONSTRAINTS"),
            )
        except Exception as exc:
            raise LLMInvocationError(
                "Provider disclosure 缺失或已过期；不会调用 Provider"
            ) from exc
        try:
            universal_selection = self.universal_runtime.resolve(profile)
        except UniversalLLMRuntimeError as exc:
            raise LLMInvocationError(str(exc)) from exc
        provider = universal_selection.provider
        try:
            metadata = dict(getattr(provider.status, "metadata", {}) or {})
        except Exception as exc:
            raise LLMInvocationError("selected universal LLM provider status is unavailable") from exc
        actual_provider_id = str(
            metadata.get("upstream_provider_id") or profile.provider_id
        ).strip()
        if not actual_provider_id:
            raise LLMInvocationError("LLM Provider 身份无效")
        return _FrozenLLMContext(
            provider=provider,
            universal_runtime=self.universal_runtime,
            universal_selection=universal_selection,
            actual_provider_id=actual_provider_id,
            boundary_provider_id=profile.provider_id,
            model_id=profile.model_id,
            endpoint_profile_id=profile.endpoint_profile_id,
            deployment_region=profile.deployment_region.value,
            endpoint_class=profile.endpoint_class,
            credential_reference=profile.credential_reference,
            selection_source=resolved.source,
            correlation_id=correlation_id or uuid4().hex,
            input_source_ids=tuple(str(item) for item in input_source_ids),
            disclosure=safe_disclosure,
        )

    def _invoke_validated(
        self,
        project_id: str,
        context: _FrozenLLMContext,
        prompt: str,
        *,
        operation: str,
        attempt_kind: str,
        validator: Callable[[str], ValidatedValue],
        provenance: Mapping[str, object] | None = None,
    ) -> ValidatedValue:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMInvocationError("LLM prompt 不能为空")
        attempt = str(attempt_kind or "PRIMARY").strip().upper()
        if attempt not in {"PRIMARY", "REPAIR"}:
            raise LLMInvocationError("LLM attempt kind 无效")
        # Re-check immediately before recording/sending the request.  A
        # settings change between preparation and the first transfer makes
        # the frozen disclosure stale and must result in zero provider calls.
        if attempt == "PRIMARY" and not self.provider_profiles.validate_disclosure(
            project_id, CapabilityKind.LLM, context.disclosure
        ):
            raise LLMInvocationError(
                "Provider disclosure fingerprint 已过期；不会调用 Provider"
            )
        base_id = f"{context.correlation_id[:48]}-{attempt.lower()}"
        started_at = _now()
        safe_summary = sanitize_persistent_metadata(
            {
                "correlation_id": context.correlation_id,
                "operation": str(operation),
                "attempt_kind": attempt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "prompt_length": len(prompt),
                "selection_source": context.selection_source,
                "boundary_provider_id": context.boundary_provider_id,
                "endpoint_profile_id": context.endpoint_profile_id,
                "deployment_region": context.deployment_region,
                "endpoint_class": context.endpoint_class,
                "credential_reference": context.credential_reference,
                "provider_disclosure": dict(context.disclosure),
                "llm_runtime": "UNIVERSAL",
                "model_manifest_id": context.universal_selection.manifest_id,
                "model_manifest_hash": context.universal_selection.manifest_hash,
                "protocol": context.universal_selection.protocol.value,
                "structured_output_state": "PENDING",
                "provenance": dict(provenance or {}),
            }
        )
        summary = dict(safe_summary) if isinstance(safe_summary, Mapping) else {}
        self._record(
            project_id,
            context,
            status="STARTED",
            request_summary=summary,
            started_at=started_at,
            invocation_id=f"{base_id}-started",
        )
        try:
            raw = context.universal_runtime.invoke_text(
                context.universal_selection,
                prompt,
                project_id=project_id,
            )
            if not isinstance(raw, str) or not raw.strip():
                raise LLMInvocationError("LLM structured generation returned empty text")
        except Exception as exc:
            reason = sanitize_error(exc) or "LLM generation failed"
            self._record(
                project_id,
                context,
                status="FAILED",
                request_summary=summary
                | {"error_code": "PROVIDER_ERROR", "error": reason, "structured_output_state": "NOT_RETURNED"},
                started_at=started_at,
                finished_at=_now(),
                invocation_id=f"{base_id}-failed",
            )
            if isinstance(exc, LLMInvocationError):
                raise
            raise LLMInvocationError(reason) from exc
        raw = raw.strip()
        try:
            value = validator(raw)
        except Exception as exc:
            self._record(
                project_id,
                context,
                status="FAILED",
                request_summary=summary | {"error_code": "OUTPUT_INVALID", "structured_output_state": "INVALID"},
                started_at=started_at,
                finished_at=_now(),
                invocation_id=f"{base_id}-failed",
            )
            raise _OutputInvalid(raw, exc) from exc
        self._record(
            project_id,
            context,
            status="SUCCEEDED",
            request_summary=summary | {"structured_output_state": "VALID"},
            started_at=started_at,
            finished_at=_now(),
            invocation_id=f"{base_id}-succeeded",
        )
        return value

    def _record(
        self,
        project_id: str,
        context: _FrozenLLMContext,
        *,
        status: str,
        request_summary: Mapping[str, Any],
        started_at: str,
        invocation_id: str,
        finished_at: str | None = None,
    ) -> None:
        self.invocations.record(
            project_id,
            capability=CapabilityKind.LLM.value,
            provider_id=context.actual_provider_id,
            model_id=context.model_id,
            status=status,
            input_source_ids=context.input_source_ids,
            request_summary=request_summary,
            started_at=started_at,
            finished_at=finished_at,
            invocation_id=invocation_id,
        )

__all__ = [
    "LLM_LIVE_SMOKE_PROMPT",
    "LLMInvocationError",
    "LLMInvocationGateway",
]
