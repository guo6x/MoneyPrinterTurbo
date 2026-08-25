"""Provider-neutral capability profiles and frozen request planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from aidrama_studio.domain import (
    CapabilityProfile,
    ProductionInputSnapshot,
    ProviderDeploymentRegion,
    ProviderPreset,
    ProviderSelectionSettings,
    ProviderTask,
    ProviderVerificationState,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import CapabilityKind, CapabilityRegistry, CapabilityUnavailable
from .security import sanitize_persistent_metadata


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_SECRET_PROFILE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "client_secret",
    "private_key",
    "cookie",
    "set_cookie",
    "signed_url",
}


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in _SECRET_PROFILE_KEYS or lowered.endswith("_api_key"):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_secret_key(item) for item in value)
    return False


class ProviderProfileError(RuntimeError):
    pass


class ProviderSelectionState(StrEnum):
    READY = "READY"
    CONFIGURED = "CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ResolvedProviderSelection:
    capability: str
    preset: str
    state: ProviderSelectionState
    source: str
    profile: CapabilityProfile | None
    configured: bool
    available: bool
    verified: bool
    detail: str

    @property
    def ready(self) -> bool:
        return self.state is ProviderSelectionState.READY

    def as_public_dict(self) -> dict[str, object]:
        profile = self.profile
        return {
            "capability": self.capability,
            "preset": self.preset,
            "state": self.state.value,
            "source": self.source,
            "provider_id": profile.provider_id if profile else "UNAVAILABLE",
            "model_id": profile.model_id if profile else "UNAVAILABLE",
            "endpoint_profile_id": profile.endpoint_profile_id if profile else None,
            "deployment_region": profile.deployment_region.value if profile else None,
            "endpoint_class": profile.endpoint_class if profile else None,
            "configured": self.configured,
            "available": self.available,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DurationPlan:
    provider_duration_seconds: float
    target_creative_duration_seconds: float
    chunks: tuple[float, ...]
    strategy: str


@dataclass(frozen=True, slots=True)
class ReferenceTrace:
    role: str
    binding_id: str
    version_id: str
    storage_path: str


class ProviderProfileService:
    """Select configured capabilities without embedding provider logic in UI."""

    PRODUCT_CAPABILITIES = (
        CapabilityKind.LLM,
        CapabilityKind.IMAGE,
        CapabilityKind.VIDEO_GENERATIVE,
        CapabilityKind.VISION,
        CapabilityKind.TTS,
    )

    _PROVIDER_DEFAULTS: dict[str, tuple[ProviderDeploymentRegion, str, str | None]] = {
        "WAN_VIDEO": (ProviderDeploymentRegion.MAINLAND_CHINA, "DASHSCOPE_CN", "DASHSCOPE_API_KEY"),
        "SEEDANCE": (ProviderDeploymentRegion.MAINLAND_CHINA, "ARK_CN_BEIJING", "ARK_API_KEY"),
        "OPENAI_IMAGE": (ProviderDeploymentRegion.INTERNATIONAL, "OPENAI_PUBLIC", "OPENAI_API_KEY"),
        "GOOGLE_GEMINI_VISION": (ProviderDeploymentRegion.INTERNATIONAL, "GOOGLE_GEMINI_PUBLIC", "GEMINI_API_KEY"),
        "MPT_STOCK": (ProviderDeploymentRegion.LOCAL, "MPT_LOCAL", None),
        "MPT_TTS": (ProviderDeploymentRegion.LOCAL, "MPT_LOCAL_TTS", None),
    }

    def __init__(self, repository: ProjectRepository | None = None, *, registry: CapabilityRegistry | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry

    def register(
        self,
        *,
        capability: CapabilityKind | str,
        provider_id: str,
        model_id: str,
        profile: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        enabled: bool = True,
        endpoint_profile_id: str | None = None,
        deployment_region: ProviderDeploymentRegion | str = ProviderDeploymentRegion.UNSPECIFIED,
        endpoint_class: str = "UNSPECIFIED",
        endpoint_url: str | None = None,
        credential_reference: str | None = None,
        verification_state: ProviderVerificationState | str = ProviderVerificationState.NOT_VERIFIED,
        verified_at: str | None = None,
        selection_priority: int = 100,
    ) -> CapabilityProfile:
        capability_value = CapabilityKind(capability).value if not isinstance(capability, str) else capability
        try:
            capability_value = CapabilityKind(capability_value).value
            region = ProviderDeploymentRegion(deployment_region)
            verification = ProviderVerificationState(verification_state)
        except ValueError as exc:
            raise ProviderProfileError("Provider profile 枚举值无效") from exc
        provider_value = str(provider_id).strip()
        model_value = str(model_id).strip()
        endpoint_value = str(endpoint_profile_id or uuid4().hex).strip()
        endpoint_class_value = str(endpoint_class).strip()
        if not provider_value or not model_value or not endpoint_value or not endpoint_class_value:
            raise ProviderProfileError("Provider/model/endpoint profile 不能为空")
        safe_endpoint = self._validate_endpoint_url(endpoint_url, region)
        safe_credential_reference = self._validate_credential_reference(credential_reference)
        raw_profile = dict(profile or {})
        if _contains_secret_key(raw_profile):
            raise ProviderProfileError(
                "Provider profile 只能保存 credential reference，不能包含 secret"
            )
        safe_profile = sanitize_persistent_metadata(raw_profile)
        if not isinstance(safe_profile, dict):
            raise ProviderProfileError("Provider profile metadata 无效")
        now = _now()
        record = CapabilityProfile(
            id=uuid4().hex,
            project_id=project_id,
            capability=capability_value,
            provider_id=provider_value,
            model_id=model_value,
            endpoint_profile_id=endpoint_value,
            deployment_region=region,
            endpoint_class=endpoint_class_value,
            endpoint_url=safe_endpoint,
            credential_reference=safe_credential_reference,
            verification_state=verification,
            verified_at=verified_at,
            selection_priority=selection_priority,
            profile=safe_profile,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_capability_profile(record)

    def list(self, project_id: str | None = None, capability: CapabilityKind | str | None = None) -> list[CapabilityProfile]:
        value = CapabilityKind(capability).value if capability is not None and not isinstance(capability, str) else capability
        return self.repository.list_capability_profiles(project_id, value)

    def inventory(
        self,
        project_id: str | None,
        capability: CapabilityKind | str,
    ) -> tuple[CapabilityProfile, ...]:
        if project_id is not None and self.repository.get_project(project_id) is None:
            raise ProviderProfileError(f"项目不存在: {project_id}")
        value = CapabilityKind(capability).value if not isinstance(capability, str) else capability
        profiles = [profile for profile in self.list(project_id, value) if profile.enabled]
        if self.registry is not None:
            for index, provider in enumerate(self.registry.list(value)):
                status = provider.status
                provider_name = str(getattr(provider, "provider_name", status.provider))
                metadata = dict(status.metadata)
                model = str(metadata.get("model") or "runtime")
                defaults = self._PROVIDER_DEFAULTS.get(
                    provider_name.upper(),
                    (ProviderDeploymentRegion.UNSPECIFIED, f"{provider_name.upper()}_RUNTIME", None),
                )
                try:
                    region = ProviderDeploymentRegion(metadata.get("deployment_region", defaults[0]))
                except ValueError:
                    region = ProviderDeploymentRegion.UNSPECIFIED
                endpoint_class = str(metadata.get("endpoint_class") or defaults[1])
                endpoint_profile_id = str(
                    metadata.get("endpoint_profile_id")
                    or f"runtime:{value}:{provider_name}:{endpoint_class}"
                )
                duplicate = next(
                    (
                        item
                        for item in profiles
                        if item.provider_id.casefold() == provider_name.casefold()
                        and item.model_id == model
                        and item.endpoint_class == endpoint_class
                        and item.endpoint_profile_id == endpoint_profile_id
                    ),
                    None,
                )
                if duplicate is not None:
                    continue
                verification_raw = metadata.get("verification_state", "NOT_VERIFIED")
                try:
                    verification = ProviderVerificationState(str(verification_raw))
                except ValueError:
                    verification = ProviderVerificationState.NOT_VERIFIED
                public_metadata = sanitize_persistent_metadata(metadata)
                credential_reference = self._validate_credential_reference(
                    str(metadata.get("credential_reference") or defaults[2] or "")
                    or None
                )
                profiles.append(
                    CapabilityProfile(
                        id=endpoint_profile_id,
                        project_id=None,
                        capability=value,
                        provider_id=provider_name,
                        model_id=model,
                        endpoint_profile_id=endpoint_profile_id,
                        deployment_region=region,
                        endpoint_class=endpoint_class,
                        credential_reference=credential_reference,
                        verification_state=verification,
                        verified_at=str(metadata.get("verified_at") or "") or None,
                        selection_priority=int(metadata.get("selection_priority", 100 + index)),
                        profile=public_metadata if isinstance(public_metadata, dict) else {},
                        enabled=True,
                        created_at="runtime",
                        updated_at="runtime",
                    )
                )
        profiles.sort(
            key=lambda item: (
                0 if project_id is not None and item.project_id == project_id else 1,
                item.selection_priority,
                item.provider_id.casefold(),
                item.model_id,
                item.endpoint_profile_id,
                item.id,
            )
        )
        return tuple(profiles)

    def get_settings(self, project_id: str | None = None) -> ProviderSelectionSettings | None:
        return self.repository.get_provider_selection_settings(project_id)

    def save_settings(
        self,
        *,
        project_id: str | None,
        preset: ProviderPreset | str,
        selections: Mapping[CapabilityKind | str, str] | None = None,
    ) -> ProviderSelectionSettings:
        if project_id is not None and self.repository.get_project(project_id) is None:
            raise ProviderProfileError(f"项目不存在: {project_id}")
        try:
            preset_value = ProviderPreset(preset)
        except ValueError as exc:
            raise ProviderProfileError("Provider preset 无效") from exc
        normalized: dict[str, str] = {}
        for capability, profile_id in dict(selections or {}).items():
            value = CapabilityKind(capability).value
            if CapabilityKind(value) not in self.PRODUCT_CAPABILITIES:
                raise ProviderProfileError(f"能力不支持模型方案选择: {value}")
            selected_id = str(profile_id).strip()
            if not selected_id:
                continue
            if preset_value is ProviderPreset.CUSTOM and not any(
                item.id == selected_id or item.endpoint_profile_id == selected_id
                for item in self.inventory(project_id, value)
            ):
                raise ProviderProfileError(
                    f"自定义选择不属于当前作用域或 Provider inventory: {value}"
                )
            normalized[value] = selected_id
        now = _now()
        current = self.get_settings(project_id)
        settings = ProviderSelectionSettings(
            id=current.id if current else (f"PROJECT:{project_id}" if project_id else "GLOBAL"),
            project_id=project_id,
            preset=preset_value,
            selections=normalized,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        return self.repository.upsert_provider_selection_settings(settings)

    def resolve(
        self,
        project_id: str | None,
        capability: CapabilityKind | str,
        *,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
        require_available: bool = False,
    ) -> ResolvedProviderSelection:
        if project_id is not None and self.repository.get_project(project_id) is None:
            raise ProviderProfileError(f"项目不存在: {project_id}")
        value = CapabilityKind(capability).value
        inventory = self.inventory(project_id, value)

        if endpoint_profile_id or provider_id:
            profile = next(
                (
                    item
                    for item in inventory
                    if (not endpoint_profile_id or item.endpoint_profile_id == endpoint_profile_id or item.id == endpoint_profile_id)
                    and (not provider_id or item.provider_id == provider_id)
                ),
                None,
            )
            return self._resolved(
                value, ProviderPreset.CUSTOM.value, "JOB_OVERRIDE", profile,
                require_available=require_available,
                unavailable_detail="明确指定的 Provider endpoint 未配置；不会自动 fallback",
            )

        settings = self.get_settings(project_id) if project_id is not None else self.get_settings(None)
        source = "PROJECT_DEFAULT" if project_id is not None else "GLOBAL_DEFAULT"
        if settings is None and project_id is not None:
            settings = self.get_settings(None)
            source = "GLOBAL_DEFAULT"
        if settings is not None:
            if settings.preset is ProviderPreset.CUSTOM:
                selected_id = settings.selections.get(value)
                profile = next(
                    (
                        item
                        for item in inventory
                        if selected_id and (item.id == selected_id or item.endpoint_profile_id == selected_id)
                    ),
                    None,
                )
                return self._resolved(
                    value, settings.preset.value, source, profile,
                    require_available=require_available,
                    unavailable_detail="自定义方案未配置该能力；不会自动 fallback",
                )
            allowed_regions = self._preset_regions(settings.preset, CapabilityKind(value))
            configured_candidates = [
                item
                for item in inventory
                if item.deployment_region in allowed_regions
                and self._configured(item)
            ]
            profile = configured_candidates[0] if configured_candidates else None
            return self._resolved(
                value, settings.preset.value, source, profile,
                require_available=require_available,
                unavailable_detail=f"{settings.preset.value} 方案没有已配置的 {value} Provider",
            )

        # Compatibility only for installations that have never saved a V1
        # model scheme. Once a scope policy exists, failure is fail-closed and
        # never falls through to another provider or region.
        legacy = next((item for item in inventory if self._available(item)), None)
        return self._resolved(
            value, "LEGACY", "LEGACY_DEFAULT", legacy,
            require_available=require_available,
            unavailable_detail=f"能力 {value} 没有可用 Provider",
        )

    def select(
        self,
        project_id: str,
        capability: CapabilityKind | str,
        *,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
    ) -> CapabilityProfile:
        resolved = self.resolve(
            project_id,
            capability,
            provider_id=provider_id,
            endpoint_profile_id=endpoint_profile_id,
            require_available=True,
        )
        if resolved.profile is None or not resolved.available:
            raise CapabilityUnavailable(resolved.detail)
        return resolved.profile

    def public_selection(self, project_id: str | None = None) -> tuple[dict[str, object], ...]:
        return tuple(
            self.resolve(project_id, capability).as_public_dict()
            for capability in self.PRODUCT_CAPABILITIES
        )

    def _resolved(
        self,
        capability: str,
        preset: str,
        source: str,
        profile: CapabilityProfile | None,
        *,
        require_available: bool,
        unavailable_detail: str,
    ) -> ResolvedProviderSelection:
        if profile is None:
            return ResolvedProviderSelection(
                capability, preset, ProviderSelectionState.UNAVAILABLE, source,
                None, False, False, False, unavailable_detail,
            )
        configured = self._configured(profile)
        available = self._available(profile)
        verified = self._verified(profile)
        if not configured or (require_available and not available):
            detail = self._runtime_reason(profile) or unavailable_detail
            return ResolvedProviderSelection(
                capability, preset, ProviderSelectionState.UNAVAILABLE, source,
                profile, configured, available, verified,
                f"{detail}；不会自动 fallback",
            )
        state = ProviderSelectionState.READY if available else ProviderSelectionState.CONFIGURED
        return ResolvedProviderSelection(
            capability, preset, state, source, profile, configured, available,
            verified, self._runtime_reason(profile) or state.value,
        )

    def _runtime_status(self, profile: CapabilityProfile):
        if self.registry is None:
            return None
        for provider in self.registry.list(profile.capability):
            if str(getattr(provider, "provider_name", "")).casefold() != profile.provider_id.casefold():
                continue
            status = provider.status
            runtime_model = str(status.metadata.get("model") or "runtime")
            if runtime_model != profile.model_id:
                continue
            metadata = dict(status.metadata)
            if profile.endpoint_profile_id not in {"", "LEGACY"}:
                runtime_endpoint = str(metadata.get("endpoint_profile_id") or "")
                if runtime_endpoint and runtime_endpoint != profile.endpoint_profile_id:
                    continue
            runtime_class = str(metadata.get("endpoint_class") or "UNSPECIFIED")
            if profile.endpoint_class not in {"", "UNSPECIFIED"} and runtime_class != profile.endpoint_class:
                continue
            runtime_region = str(metadata.get("deployment_region") or "UNSPECIFIED")
            if (
                profile.deployment_region is not ProviderDeploymentRegion.UNSPECIFIED
                and runtime_region != profile.deployment_region.value
            ):
                continue
            runtime_credential = str(metadata.get("credential_reference") or "") or None
            if (
                profile.credential_reference is not None
                and runtime_credential != profile.credential_reference
            ):
                continue
            return status
        return None

    def _configured(self, profile: CapabilityProfile) -> bool:
        if not profile.enabled:
            return False
        if self.registry is None:
            return True
        status = self._runtime_status(profile)
        if status is None:
            return False
        configured = getattr(status, "configured", None)
        return bool(status.available if configured is None else configured)

    def _available(self, profile: CapabilityProfile) -> bool:
        if not profile.enabled:
            return False
        if self.registry is None:
            return True
        status = self._runtime_status(profile)
        return bool(status and status.available)

    def _verified(self, profile: CapabilityProfile) -> bool:
        if profile.verification_state is ProviderVerificationState.VERIFIED:
            return True
        status = self._runtime_status(profile)
        return bool(status and getattr(status, "verified", False))

    def _runtime_reason(self, profile: CapabilityProfile) -> str:
        status = self._runtime_status(profile)
        if status is None:
            return "Provider 不在当前 runtime inventory" if self.registry is not None else "configured"
        return str(status.reason or "configured")

    @staticmethod
    def _preset_regions(
        preset: ProviderPreset,
        capability: CapabilityKind,
    ) -> tuple[ProviderDeploymentRegion, ...]:
        if preset is ProviderPreset.MAINLAND:
            regions = [ProviderDeploymentRegion.MAINLAND_CHINA]
        elif preset is ProviderPreset.INTERNATIONAL:
            regions = [ProviderDeploymentRegion.INTERNATIONAL]
        else:
            return ()
        if capability is CapabilityKind.TTS:
            regions.append(ProviderDeploymentRegion.LOCAL)
        return tuple(regions)

    @staticmethod
    def _validate_endpoint_url(
        endpoint_url: str | None,
        region: ProviderDeploymentRegion,
    ) -> str | None:
        if endpoint_url is None or not str(endpoint_url).strip():
            return None
        value = str(endpoint_url).strip()
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderProfileError("remote Provider endpoint 必须是无凭据的 HTTPS URL")
        if parsed.query or parsed.fragment:
            raise ProviderProfileError("Provider endpoint 不得包含 query 或 fragment")
        if region is ProviderDeploymentRegion.LOCAL:
            raise ProviderProfileError("LOCAL Provider 不应配置 remote endpoint URL")
        return value.rstrip("/")

    @staticmethod
    def _validate_credential_reference(value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        reference = str(value).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", reference) or reference.casefold().startswith("sk-"):
            raise ProviderProfileError("credential_reference 必须是非 secret alias")
        return reference

    @staticmethod
    def plan_duration(
        target_seconds: float,
        *,
        minimum: float = 2.0,
        maximum: float = 5.0,
        allowed_durations: tuple[float, ...] | list[float] = (),
    ) -> DurationPlan:
        if target_seconds <= 0 or minimum <= 0 or maximum < minimum:
            raise ProviderProfileError("duration 参数无效")
        target = float(target_seconds)
        try:
            allowed = sorted({float(item) for item in allowed_durations if float(item) > 0})
        except (TypeError, ValueError) as exc:
            raise ProviderProfileError("supported duration 参数无效") from exc
        if allowed:
            if target <= allowed[-1]:
                provider_duration = next(
                    (item for item in allowed if item >= target), allowed[-1]
                )
                strategy = "EXACT" if provider_duration == target else "TRIM_TO_CREATIVE"
                return DurationPlan(provider_duration, target, (provider_duration,), strategy)
            chunks: list[float] = []
            remaining = target
            while remaining > 0:
                chosen = next((item for item in allowed if item >= remaining), allowed[-1])
                chunks.append(chosen)
                remaining = max(0.0, round(remaining - chosen, 6))
            return DurationPlan(max(chunks), target, tuple(chunks), "CHUNK_AND_CONTINUE")
        chunks: list[float] = []
        remaining = target
        while remaining > 0:
            chunk = min(maximum, max(minimum, remaining))
            # Avoid a final sub-minimum chunk by redistributing it into the
            # previous chunk; every paid request remains provider-valid.
            if remaining < minimum and chunks:
                chunks[-1] += remaining
                remaining = 0
            else:
                chunks.append(round(chunk, 3))
                remaining = round(remaining - chunk, 6)
        if len(chunks) > 1:
            strategy = "CHUNK_AND_CONTINUE"
        elif chunks[0] > target:
            strategy = "TRIM_TO_CREATIVE"
        else:
            strategy = "EXACT"
        return DurationPlan(max(chunks), target, tuple(chunks), strategy)

    def compile_reference_trace(self, snapshot: ProductionInputSnapshot, *, paths: Mapping[str, str]) -> tuple[ReferenceTrace, ...]:
        if not isinstance(snapshot, ProductionInputSnapshot):
            raise ProviderProfileError("snapshot 类型无效")
        traces: list[ReferenceTrace] = []
        for binding_id, version_id in snapshot.reference_asset_versions.items():
            path = paths.get(version_id)
            if not path or path.startswith("/") or "://" in path or ":" in path[:3]:
                raise ProviderProfileError("reference path 必须是项目相对路径")
            traces.append(ReferenceTrace("REFERENCE", str(binding_id), str(version_id), path.replace("\\", "/")))
        return tuple(traces)

    @staticmethod
    def idempotency_key(*, project_id: str, execution_id: str, plan_hash: str) -> str:
        return _hash({"project_id": project_id, "execution_id": execution_id, "plan_hash": plan_hash})

    def get_or_create_task(
        self,
        *,
        project_id: str,
        execution_id: str | None,
        capability: str,
        provider_id: str,
        model_id: str,
        idempotency_key: str,
        request_summary: Mapping[str, Any] | None = None,
    ) -> ProviderTask:
        existing = self.repository.get_provider_task_by_idempotency(project_id, idempotency_key)
        if existing is not None:
            return existing
        now = _now()
        task = ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=execution_id,
            capability=capability, provider_id=provider_id, model_id=model_id,
            idempotency_key=idempotency_key, state="INTENT", request_summary=dict(request_summary or {}),
            created_at=now, updated_at=now,
        )
        try:
            return self.repository.create_provider_task(task)
        except Exception:
            # A concurrent caller may have won the unique idempotency race;
            # return that durable intent rather than issuing a second request.
            raced = self.repository.get_provider_task_by_idempotency(project_id, idempotency_key)
            if raced is not None:
                return raced
            raise


__all__ = [
    "DurationPlan",
    "ProviderProfileError",
    "ProviderProfileService",
    "ProviderSelectionState",
    "ReferenceTrace",
    "ResolvedProviderSelection",
]
