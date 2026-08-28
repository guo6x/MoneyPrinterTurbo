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
class ProviderDisclosure:
    """Safe, explicit notice required immediately before remote transfer.

    The disclosure is deliberately limited to provider routing and the
    *categories* of content being sent.  It never contains prompts, source
    documents, credentials, endpoint URLs, or provider response data.
    ``fingerprint`` binds the notice to the exact selected profile, so a
    settings/profile change invalidates a previously prepared disclosure.
    """

    capability: str
    provider_id: str
    model_id: str
    deployment_region: str
    endpoint_profile_id: str
    endpoint_class: str
    transmitted_content_types: tuple[str, ...]
    disclosure_version: str
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "deployment_region": self.deployment_region,
            "endpoint_profile_id": self.endpoint_profile_id,
            "endpoint_class": self.endpoint_class,
            "transmitted_content_types": list(self.transmitted_content_types),
            "disclosure_version": self.disclosure_version,
            "fingerprint": self.fingerprint,
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

    DISCLOSURE_VERSION = "provider-disclosure-v1"
    # These are category labels, never user content.  Keeping an allow-list
    # prevents a disclosure from becoming an accidental prompt/source dump.
    DISCLOSURE_CONTENT_TYPES = frozenset({
        "TEXT_BRIEF",
        "TEXT_CONSTRAINTS",
        "TEXT_TIMELINE",
        "REFERENCE_VERSION",
        "REFERENCE_IMAGE",
        "VIDEO_ARTIFACT",
        "SAMPLED_FRAME",
    })

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

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        manifest_registry: object | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry
        self.manifest_registry = manifest_registry

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
        manifest_registry = self.manifest_registry
        if manifest_registry is None:
            # A normal runtime does not pay the cost of projecting the whole
            # manifest registry.  If Settings persisted an exact manifest ID,
            # however, materialize that one immutable profile so downstream
            # services consume the saved choice instead of silently falling
            # back to the legacy registry default.
            settings = self.get_settings(project_id) if project_id is not None else None
            if settings is None and project_id is not None:
                settings = self.get_settings(None)
            if settings is None and project_id is None:
                settings = self.get_settings(None)
            selected = (
                settings.selections.get(value)
                if settings is not None and settings.preset is ProviderPreset.CUSTOM
                else None
            )
            if selected:
                try:
                    from .model_runtime import default_manifest_registry

                    candidate_registry = default_manifest_registry(
                        include_placeholders=False
                    )
                    if candidate_registry.get(selected) is not None:
                        manifest_registry = candidate_registry
                except Exception:
                    manifest_registry = None
        if manifest_registry is not None:
            profiles.extend(
                profile
                for profile in self._manifest_profiles(manifest_registry, value)
                if not any(existing.id == profile.id for existing in profiles)
            )
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

    @staticmethod
    def _manifest_profiles(
        manifest_registry: object,
        legacy_capability: str,
    ) -> tuple[CapabilityProfile, ...]:
        """Project registered model data into the existing selection seam."""

        try:
            from .model_runtime import CapabilityKind as ModelCapabilityKind

            universal = {
                CapabilityKind.LLM.value: ModelCapabilityKind.LLM,
                CapabilityKind.IMAGE.value: ModelCapabilityKind.IMAGE,
                CapabilityKind.VIDEO_GENERATIVE.value: ModelCapabilityKind.VIDEO,
                CapabilityKind.VISION.value: ModelCapabilityKind.VISION,
                CapabilityKind.TTS.value: ModelCapabilityKind.TTS,
            }[legacy_capability]
            manifests = tuple(manifest_registry.list(universal))
        except (AttributeError, KeyError, TypeError, ValueError):
            return ()

        projected: list[CapabilityProfile] = []
        for manifest in manifests:
            metadata = getattr(manifest, "metadata", {})
            metadata = metadata if isinstance(metadata, Mapping) else {}
            if metadata.get("placeholder") is True:
                continue
            manifest_id = str(getattr(manifest, "id", "") or "")
            provider_id = str(
                metadata.get("runtime_provider_id")
                or getattr(manifest, "provider_id", "")
            )
            model_id = str(getattr(manifest, "model_id", "") or "")
            endpoint_profile_id = str(
                metadata.get("runtime_endpoint_profile_id")
                or getattr(manifest, "endpoint_profile_id", "")
                or "LEGACY"
            )
            endpoint_class = str(
                metadata.get("runtime_endpoint_class")
                or getattr(manifest, "endpoint_class", "")
                or "UNSPECIFIED"
            )
            if not all((manifest_id, provider_id, model_id)):
                continue
            region_value = str(
                getattr(manifest, "deployment_region", "UNSPECIFIED")
                or "UNSPECIFIED"
            )
            try:
                region = ProviderDeploymentRegion(region_value)
            except ValueError:
                region = ProviderDeploymentRegion.UNSPECIFIED
            selection_policy = getattr(manifest, "selection_policy", {})
            selection_policy = (
                selection_policy if isinstance(selection_policy, Mapping) else {}
            )
            try:
                priority = int(selection_policy.get("priority", 100))
            except (TypeError, ValueError):
                priority = 100
            duration = getattr(manifest, "duration", None)
            duration_data = (
                duration.to_dict() if callable(getattr(duration, "to_dict", None)) else {}
            )
            resolution = getattr(manifest, "resolution", None)
            resolution_data = (
                resolution.to_dict()
                if callable(getattr(resolution, "to_dict", None))
                else {}
            )
            profile_data: dict[str, object] = {
                "manifest_id": manifest_id,
                "manifest_hash": str(getattr(manifest, "manifest_hash", "")),
                "codec_id": str(getattr(manifest, "codec_id", "")),
                "requires_explicit_selection": selection_policy.get(
                    "requires_explicit_selection"
                )
                is True,
            }
            supports = getattr(manifest, "supports", None)
            supports_data = (
                supports.to_dict()
                if callable(getattr(supports, "to_dict", None))
                else {}
            )
            if supports_data.get("first_frame") is True:
                profile_data["requires_first_frame"] = True
            minimum = duration_data.get("minimum")
            maximum = duration_data.get("maximum")
            if minimum is not None:
                profile_data["minimum_duration_seconds"] = minimum
            if maximum is not None:
                profile_data["maximum_duration_seconds"] = maximum
            discrete = duration_data.get("discrete_values")
            if isinstance(discrete, list) and discrete:
                profile_data["supported_durations"] = discrete
            limits = getattr(manifest, "limits", {})
            limits = limits if isinstance(limits, Mapping) else {}
            preferred_duration = limits.get("preferred_shot_duration_seconds")
            if preferred_duration is not None:
                profile_data["preferred_shot_duration_seconds"] = preferred_duration
            if (
                "supported_durations" not in profile_data
                and limits.get("duration_integer_only") is True
                and isinstance(minimum, (int, float))
                and isinstance(maximum, (int, float))
                and float(minimum).is_integer()
                and float(maximum).is_integer()
            ):
                profile_data["supported_durations"] = list(
                    range(int(minimum), int(maximum) + 1)
                )
            resolutions = resolution_data.get("supported")
            if isinstance(resolutions, list) and resolutions:
                profile_data["supported_native_resolutions"] = resolutions
                profile_data["provider_resolution"] = resolutions[0]
            credential_reference = str(
                getattr(manifest, "credential_reference", "") or ""
            ) or None
            projected.append(
                CapabilityProfile(
                    id=manifest_id,
                    project_id=None,
                    capability=legacy_capability,
                    provider_id=provider_id,
                    model_id=model_id,
                    endpoint_profile_id=endpoint_profile_id,
                    deployment_region=region,
                    endpoint_class=endpoint_class,
                    credential_reference=credential_reference,
                    verification_state=(
                        ProviderVerificationState.VERIFIED
                        if bool(getattr(manifest, "verified", False))
                        else ProviderVerificationState.NOT_VERIFIED
                    ),
                    selection_priority=max(0, min(10000, priority)),
                    profile=profile_data,
                    enabled=True,
                    created_at="manifest",
                    updated_at="manifest",
                )
            )
        return tuple(projected)

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
            if preset_value is ProviderPreset.CUSTOM:
                selected = self._exact_profile(
                    self.inventory(project_id, value),
                    selection_token=selected_id,
                )
                if selected is None:
                    raise ProviderProfileError(
                        f"自定义选择不属于当前作用域或 Provider inventory: {value}"
                    )
                # Persist the model/manifest identity, never a shared endpoint
                # alias that could silently resolve to a different model.
                selected_id = selected.id
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
        model_id: str | None = None,
        endpoint_profile_id: str | None = None,
        profile_id: str | None = None,
        require_available: bool = False,
    ) -> ResolvedProviderSelection:
        if project_id is not None and self.repository.get_project(project_id) is None:
            raise ProviderProfileError(f"项目不存在: {project_id}")
        value = CapabilityKind(capability).value
        inventory = self.inventory(project_id, value)

        if endpoint_profile_id or provider_id or model_id or profile_id:
            profile = self._exact_profile(
                inventory,
                selection_token=profile_id or endpoint_profile_id,
                provider_id=provider_id,
                model_id=model_id,
            )
            if (
                profile is not None
                and profile_id
                and endpoint_profile_id
                and profile.endpoint_profile_id != endpoint_profile_id
            ):
                raise ProviderProfileError(
                    "Provider profile 与 endpoint identity 不匹配；不会 fallback"
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
                profile = self._exact_profile(
                    inventory,
                    selection_token=selected_id,
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
                and not self._requires_explicit_selection(item)
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
        legacy = next(
            (
                item
                for item in inventory
                if self._available(item)
                and not self._requires_explicit_selection(item)
            ),
            None,
        )
        return self._resolved(
            value, "LEGACY", "LEGACY_DEFAULT", legacy,
            require_available=require_available,
            unavailable_detail=f"能力 {value} 没有可用 Provider",
        )

    @staticmethod
    def _exact_profile(
        inventory: tuple[CapabilityProfile, ...],
        *,
        selection_token: str | None,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> CapabilityProfile | None:
        """Resolve model identity before accepting legacy endpoint aliases.

        Manifest/profile IDs are exact. Endpoint aliases remain compatible
        only when they identify one capability-scoped model. Shared endpoints
        such as DashScope image generation must fail closed instead of taking
        the inventory's first model.
        """

        token = str(selection_token or "").strip()
        provider = str(provider_id or "").strip().casefold()
        model = str(model_id or "").strip()

        def identity_matches(item: CapabilityProfile) -> bool:
            return (
                (not provider or item.provider_id.casefold() == provider)
                and (not model or item.model_id == model)
            )

        if token:
            exact = [
                item
                for item in inventory
                if item.id == token and identity_matches(item)
            ]
            if len(exact) == 1:
                return exact[0]
            endpoints = [
                item
                for item in inventory
                if item.endpoint_profile_id == token and identity_matches(item)
            ]
            if len(endpoints) == 1:
                return endpoints[0]
            if len(endpoints) > 1:
                raise ProviderProfileError(
                    "Provider endpoint 对应多个模型；必须选择 exact manifest/profile ID"
                )
            return None

        candidates = [item for item in inventory if identity_matches(item)]
        if len(candidates) == 1:
            return candidates[0]
        if (provider or model) and len(candidates) > 1:
            raise ProviderProfileError(
                "Provider/model 对应多个 profile；必须选择 exact manifest/profile ID"
            )
        return None

    def select(
        self,
        project_id: str,
        capability: CapabilityKind | str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        endpoint_profile_id: str | None = None,
        profile_id: str | None = None,
    ) -> CapabilityProfile:
        resolved = self.resolve(
            project_id,
            capability,
            provider_id=provider_id,
            model_id=model_id,
            endpoint_profile_id=endpoint_profile_id,
            profile_id=profile_id,
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

    def create_disclosure(
        self,
        project_id: str,
        capability: CapabilityKind | str,
        *,
        transmitted_content_types: tuple[str, ...] | list[str] = (),
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
    ) -> ProviderDisclosure:
        """Freeze a safe routing/content notice for one exact profile.

        This method performs no network operation.  It fails closed when the
        selected profile is unavailable, ensuring callers cannot issue a
        remote request through an implicit fallback.
        """

        resolved = self.resolve(
            project_id,
            capability,
            provider_id=provider_id,
            endpoint_profile_id=endpoint_profile_id,
            require_available=True,
        )
        if resolved.profile is None or not resolved.available:
            raise ProviderProfileError(resolved.detail)
        content_types = self._normalize_disclosure_content_types(
            transmitted_content_types
        )
        profile = resolved.profile
        payload = self._disclosure_payload(
            capability=CapabilityKind(capability).value,
            profile=profile,
            content_types=content_types,
        )
        fingerprint = _hash(payload)
        return ProviderDisclosure(
            capability=payload["capability"],
            provider_id=payload["provider_id"],
            model_id=payload["model_id"],
            deployment_region=payload["deployment_region"],
            endpoint_profile_id=payload["endpoint_profile_id"],
            endpoint_class=payload["endpoint_class"],
            transmitted_content_types=tuple(content_types),
            disclosure_version=self.DISCLOSURE_VERSION,
            fingerprint=fingerprint,
        )

    # Explicit aliases make the boundary easy to consume from adapters and
    # preserve a single canonical implementation.
    disclosure = create_disclosure

    def require_disclosure(
        self,
        project_id: str,
        capability: CapabilityKind | str,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
        *,
        transmitted_content_types: tuple[str, ...] | list[str] = (),
    ) -> dict[str, object]:
        """Return a validated public disclosure or fail before any call."""

        if disclosure is None:
            return self.create_disclosure(
                project_id,
                capability,
                transmitted_content_types=transmitted_content_types,
            ).as_dict()
        value = disclosure.as_dict() if isinstance(disclosure, ProviderDisclosure) else dict(disclosure)
        if not self.validate_disclosure(project_id, capability, value):
            raise ProviderProfileError("Provider disclosure 缺失或 fingerprint 已过期；不会调用 Provider")
        return value

    def validate_disclosure(
        self,
        project_id: str,
        capability: CapabilityKind | str,
        disclosure: ProviderDisclosure | Mapping[str, object] | None,
    ) -> bool:
        """Validate routing, content categories, and current profile hash."""

        if disclosure is None:
            return False
        value = disclosure.as_dict() if isinstance(disclosure, ProviderDisclosure) else dict(disclosure)
        try:
            capability_value = CapabilityKind(capability).value
            if str(value.get("capability")) != capability_value:
                return False
            if str(value.get("disclosure_version")) != self.DISCLOSURE_VERSION:
                return False
            content_types = self._normalize_disclosure_content_types(
                value.get("transmitted_content_types") or ()
            )
            resolved = self.resolve(project_id, capability_value, require_available=True)
            if resolved.profile is None or not resolved.available:
                return False
            payload = self._disclosure_payload(
                capability=capability_value,
                profile=resolved.profile,
                content_types=content_types,
            )
            if any(str(value.get(key)) != str(expected) for key, expected in payload.items()):
                return False
            return str(value.get("fingerprint")) == _hash(payload)
        except (ProviderProfileError, TypeError, ValueError):
            return False

    @classmethod
    def _normalize_disclosure_content_types(cls, values: object) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise ProviderProfileError("disclosure content types 必须为 category list")
        try:
            normalized = tuple(dict.fromkeys(str(item).strip().upper() for item in values))
        except TypeError as exc:
            raise ProviderProfileError("disclosure content types 无效") from exc
        if any(not item or item not in cls.DISCLOSURE_CONTENT_TYPES for item in normalized):
            raise ProviderProfileError("disclosure content type 不受支持")
        return normalized

    @staticmethod
    def _disclosure_payload(*, capability: str, profile: CapabilityProfile, content_types: tuple[str, ...]) -> dict[str, object]:
        return {
            "capability": capability,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "deployment_region": profile.deployment_region.value,
            "endpoint_profile_id": profile.endpoint_profile_id,
            "endpoint_class": profile.endpoint_class,
            "transmitted_content_types": list(content_types),
        }

    def provider_for_selection(self, resolved: ResolvedProviderSelection) -> object:
        """Resolve the concrete registered runtime without cross-provider fallback."""

        profile = resolved.profile
        if profile is None or not resolved.available or self.registry is None:
            raise ProviderProfileError("Provider selection 不可用；不会自动 fallback")
        for provider in self.registry.list(profile.capability):
            if str(getattr(provider, "provider_name", "")).casefold() != profile.provider_id.casefold():
                continue
            try:
                status = provider.status
            except Exception:
                continue
            metadata = dict(getattr(status, "metadata", {}) or {})
            if str(metadata.get("model") or "runtime") != profile.model_id:
                continue
            endpoint_id = str(metadata.get("endpoint_profile_id") or "")
            if endpoint_id and endpoint_id != profile.endpoint_profile_id:
                continue
            endpoint_class_raw = metadata.get("endpoint_class")
            endpoint_class = str(endpoint_class_raw or "UNSPECIFIED")
            if endpoint_class_raw is not None and profile.endpoint_class not in {"", "UNSPECIFIED"} and endpoint_class != profile.endpoint_class:
                continue
            region_raw = metadata.get("deployment_region")
            region = str(region_raw or "UNSPECIFIED")
            if region_raw is not None and profile.deployment_region is not ProviderDeploymentRegion.UNSPECIFIED and region != profile.deployment_region.value:
                continue
            return provider
        raise ProviderProfileError("冻结 Provider/model/endpoint 不在当前 runtime inventory；不会自动 fallback")

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
            runtime_class_raw = metadata.get("endpoint_class")
            runtime_class = str(runtime_class_raw or "UNSPECIFIED")
            if runtime_class_raw is not None and profile.endpoint_class not in {"", "UNSPECIFIED"} and runtime_class != profile.endpoint_class:
                continue
            runtime_region_raw = metadata.get("deployment_region")
            runtime_region = str(runtime_region_raw or "UNSPECIFIED")
            if (
                runtime_region_raw is not None
                and
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
        return (
            status.available is True
            if configured is None
            else configured is True
        )

    def _available(self, profile: CapabilityProfile) -> bool:
        if not profile.enabled:
            return False
        if self.registry is None:
            return True
        status = self._runtime_status(profile)
        return bool(status and status.available is True)

    def _verified(self, profile: CapabilityProfile) -> bool:
        if profile.verification_state is ProviderVerificationState.VERIFIED:
            return True
        status = self._runtime_status(profile)
        return bool(status and getattr(status, "verified", False) is True)

    @staticmethod
    def _requires_explicit_selection(profile: CapabilityProfile) -> bool:
        # Seedance is an opt-in paid runtime by provider identity, not merely
        # by a mutable metadata marker.  A project-scoped profile copied from
        # an older installation (or tampered to omit the marker) must never
        # become the implicit MAINLAND/legacy selection or inherit generic
        # video limits.  Keep the metadata check for any future explicitly
        # gated provider as well.
        provider_id = str(profile.provider_id or "").strip().casefold()
        if provider_id in {"seedance", "seedance_video"}:
            return True
        metadata = profile.profile if isinstance(profile.profile, Mapping) else {}
        return metadata.get("requires_explicit_selection") is True

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
    "ProviderDisclosure",
    "ProviderProfileError",
    "ProviderProfileService",
    "ProviderSelectionState",
    "ReferenceTrace",
    "ResolvedProviderSelection",
]
