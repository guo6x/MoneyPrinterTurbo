"""Manifest-driven model settings, persistence, and runtime-plan selection.

The Settings page is a projection of the Universal Runtime registry.  This
module deliberately owns no model catalogue: every selectable model comes
from a ``ModelManifest`` and every durable choice is stored in the existing
``provider_selection_settings`` table.  Credential values are never read by
the projection; only a boolean presence check is used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aidrama_studio.domain import ProviderPreset
from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import CapabilityKind as LegacyCapabilityKind
from .model_runtime import (
    CapabilityKind,
    ModelResolutionError,
    ModelResolver,
    ProtocolFamily,
    RegionPolicy,
    build_mainland_codecs,
    dashscope_workspace_endpoint_profile,
    default_manifest_registry,
)
from .model_runtime.mainland_runtime import (
    DASHSCOPE_WORKSPACE_BASE_URL_KEY,
    MAINLAND_ENDPOINT_PROFILES,
)
from .provider_profiles import ProviderProfileService


CAPABILITY_LABELS: Mapping[CapabilityKind, str] = {
    CapabilityKind.LLM: "文本生成",
    CapabilityKind.IMAGE: "参考图生成",
    CapabilityKind.VIDEO: "视频生成",
    CapabilityKind.VISION: "画面分析",
    CapabilityKind.TTS: "配音",
}
CAPABILITY_ORDER = tuple(CAPABILITY_LABELS)

PROVIDER_LABELS: Mapping[str, str] = {
    "alibaba_model_studio": "阿里云百炼",
    "deepseek": "DeepSeek",
    "volcengine_ark": "火山引擎 Ark",
}

_CREDENTIAL_LABELS: Mapping[str, str] = {
    "DASHSCOPE_API_KEY": "阿里云百炼 / DashScope",
    "DEEPSEEK_API_KEY": "DeepSeek",
    "ARK_API_KEY": "火山引擎 / Ark",
}

_LEGACY_CAPABILITIES: Mapping[CapabilityKind, LegacyCapabilityKind] = {
    CapabilityKind.LLM: LegacyCapabilityKind.LLM,
    CapabilityKind.IMAGE: LegacyCapabilityKind.IMAGE,
    CapabilityKind.VIDEO: LegacyCapabilityKind.VIDEO_GENERATIVE,
    CapabilityKind.VISION: LegacyCapabilityKind.VISION,
    CapabilityKind.TTS: LegacyCapabilityKind.TTS,
}

_UNIVERSAL_CAPABILITIES: Mapping[str, CapabilityKind] = {
    legacy.value: universal for universal, legacy in _LEGACY_CAPABILITIES.items()
}

_REQUIRED_MODALITIES: Mapping[CapabilityKind, tuple[frozenset[str], frozenset[str]]] = {
    CapabilityKind.LLM: (frozenset({"text"}), frozenset({"text"})),
    CapabilityKind.IMAGE: (frozenset({"text"}), frozenset({"image"})),
    CapabilityKind.VIDEO: (frozenset({"text"}), frozenset({"video"})),
    CapabilityKind.VISION: (frozenset({"image"}), frozenset({"text"})),
    CapabilityKind.TTS: (frozenset({"text"}), frozenset({"audio"})),
}

_SUPPORTED_PROTOCOLS = frozenset(
    {ProtocolFamily.REQUEST_RESPONSE, ProtocolFamily.ASYNC_TASK, ProtocolFamily.STREAM}
)
_SUPPORTED_REGIONS = frozenset(
    {"MAINLAND_CHINA", "INTERNATIONAL", "LOCAL", "CUSTOM"}
)


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id.replace("_", " ").title())


def region_label(region: str) -> str:
    return {
        "MAINLAND_CHINA": "中国大陆",
        "INTERNATIONAL": "国际",
        "LOCAL": "本地",
        "CUSTOM": "自定义区域",
    }.get(str(region).upper(), "未声明")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _priority(manifest: object) -> int:
    value = _mapping(getattr(manifest, "selection_policy", {})).get("priority", 100)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


def _explicit_selection(manifest: object) -> bool:
    return _mapping(getattr(manifest, "selection_policy", {})).get(
        "requires_explicit_selection"
    ) is True


def _is_placeholder(manifest: object) -> bool:
    return _mapping(getattr(manifest, "metadata", {})).get("placeholder") is True


def _runtime_identity(manifest: object, name: str, fallback: object) -> str:
    value = _mapping(getattr(manifest, "metadata", {})).get(name, fallback)
    return str(value or fallback)


def _legacy_capability(capability: CapabilityKind | str) -> LegacyCapabilityKind:
    kind = CapabilityKind.coerce(capability)
    return _LEGACY_CAPABILITIES[kind]


def _universal_capability(capability: CapabilityKind | LegacyCapabilityKind | str) -> CapabilityKind:
    if isinstance(capability, CapabilityKind):
        return capability
    raw = str(getattr(capability, "value", capability)).strip().upper()
    if raw in _UNIVERSAL_CAPABILITIES:
        return _UNIVERSAL_CAPABILITIES[raw]
    return CapabilityKind.coerce(raw)


@dataclass(frozen=True, slots=True)
class SettingsModelOption:
    capability: CapabilityKind
    manifest_id: str
    manifest_hash: str
    provider_id: str
    provider_name: str
    runtime_provider_id: str
    model_id: str
    display_name: str
    deployment_region: str
    endpoint_profile_id: str
    runtime_endpoint_profile_id: str
    endpoint_class: str
    runtime_endpoint_class: str
    credential_reference: str | None
    registered: bool
    compatible: bool
    compatibility_reason: str
    configured: bool
    verified: bool
    runtime_available: bool
    create_authorized: bool
    authorization_required: bool
    explicit_selection_required: bool
    protocol: str
    codec_id: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    manifest: object = field(repr=False, compare=False)

    @property
    def credential_ready(self) -> bool:
        return self.credential_reference is None or self.configured

    @property
    def selectable(self) -> bool:
        return self.registered and self.compatible and self.runtime_available


@dataclass(frozen=True, slots=True)
class SettingsModelResolution:
    option: SettingsModelOption
    source: str
    inherited: bool = False


class SettingsModelService:
    """Project the registry and persist exact manifest identities."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        manifest_registry: object | None = None,
        credential_store: object | None = None,
        codec_ids: set[str] | frozenset[str] | None = None,
        endpoint_profile_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.manifest_registry = manifest_registry or default_manifest_registry(
            include_placeholders=False
        )
        self.resolver = ModelResolver(self.manifest_registry)
        self.credential_store = credential_store
        self.codec_ids = frozenset(codec_ids or build_mainland_codecs().keys())
        self.endpoint_profile_ids = frozenset(
            endpoint_profile_ids or MAINLAND_ENDPOINT_PROFILES.keys()
        )
        self.selection_service = ProviderProfileService(
            self.repository,
            manifest_registry=self.manifest_registry,
        )

    def inventory(
        self, capability: CapabilityKind | LegacyCapabilityKind | str | None = None
    ) -> tuple[SettingsModelOption, ...]:
        requested = _universal_capability(capability) if capability is not None else None
        try:
            manifests = tuple(
                self.manifest_registry.list(requested)
                if requested is not None
                else self.manifest_registry.list()
            )
        except TypeError:
            manifests = tuple(self.manifest_registry.list())
        options: list[SettingsModelOption] = []
        for manifest in manifests:
            try:
                actual = CapabilityKind.coerce(getattr(manifest, "capability"))
            except (AttributeError, TypeError, ValueError):
                continue
            if requested is not None and actual is not requested:
                continue
            if _is_placeholder(manifest):
                continue
            options.append(self._option(manifest, actual))
        options.sort(
            key=lambda item: (
                CAPABILITY_ORDER.index(item.capability),
                _priority(item.manifest),
                item.provider_name.casefold(),
                item.display_name.casefold(),
                item.manifest_id,
            )
        )
        return tuple(options)

    def selectable_inventory(
        self, capability: CapabilityKind | LegacyCapabilityKind | str
    ) -> tuple[SettingsModelOption, ...]:
        return tuple(item for item in self.inventory(capability) if item.selectable)

    def credential_requirements(self) -> tuple[dict[str, object], ...]:
        """Derive secret slots from manifests and non-secret endpoint declarations."""

        by_reference: dict[str, dict[str, object]] = {}
        for option in self.inventory():
            reference = option.credential_reference
            if not reference:
                continue
            item = by_reference.setdefault(
                reference,
                {
                    "key": reference,
                    "label": _CREDENTIAL_LABELS.get(reference, reference),
                    "description": "",
                    "secret": True,
                    "providers": set(),
                    "capabilities": set(),
                },
            )
            item["providers"].add(option.provider_name)
            item["capabilities"].add(CAPABILITY_LABELS[option.capability])
        requirements: list[dict[str, object]] = []
        for key in sorted(by_reference):
            item = by_reference[key]
            providers = "、".join(sorted(item.pop("providers")))
            capabilities = "、".join(sorted(item.pop("capabilities")))
            item["description"] = (
                f"用于 {providers} 的 {capabilities}；保存只更新连接状态，不会发起请求。"
            )
            requirements.append(item)
        if "DASHSCOPE_API_KEY" in by_reference:
            requirements.append(
                {
                    "key": DASHSCOPE_WORKSPACE_BASE_URL_KEY,
                    "label": "百炼业务空间 Base URL",
                    "description": (
                        "使用 sk-ws- 业务空间 Key 时填写；必须是北京区域、无凭据的 HTTPS /api/v1 地址。"
                    ),
                    "secret": False,
                    "input_label": "业务空间 Base URL",
                    "validation": "DASHSCOPE_WORKSPACE_URL",
                }
            )
        return tuple(requirements)

    def resolve(
        self,
        project_id: str | None,
        capability: CapabilityKind | LegacyCapabilityKind | str,
        *,
        frozen_identity: object | None = None,
    ) -> SettingsModelResolution:
        kind = _universal_capability(capability)
        if frozen_identity is not None:
            resolved = self.resolver.resolve(
                capability=kind,
                frozen_identity=frozen_identity,
            )
            return SettingsModelResolution(
                option=self._option(resolved.manifest, kind),
                source="FROZEN_RUNTIME_SELECTION",
            )

        settings = None
        source = "MANIFEST_DEFAULT"
        inherited = False
        if project_id is not None:
            settings = self.selection_service.get_settings(project_id)
            if settings is not None:
                source = "PROJECT_SELECTION"
        if settings is None:
            settings = self.selection_service.get_settings(None)
            if settings is not None:
                source = "GLOBAL_SELECTION"
                inherited = project_id is not None

        selected: SettingsModelOption | None = None
        if settings is not None and settings.preset is ProviderPreset.CUSTOM:
            token = settings.selections.get(_legacy_capability(kind).value)
            if token:
                selected = self._option_for_token(kind, token)
            if selected is None:
                raise ModelResolutionError(
                    f"saved {kind.value} manifest selection is unavailable; no fallback"
                )
        elif settings is not None:
            policy = (
                RegionPolicy.MAINLAND
                if settings.preset is ProviderPreset.MAINLAND
                else RegionPolicy.INTERNATIONAL
            )
            resolved = self.resolver.resolve(capability=kind, region_policy=policy)
            selected = self._option(resolved.manifest, kind)
        else:
            candidates = [
                item
                for item in self.selectable_inventory(kind)
                if not item.explicit_selection_required
            ]
            if candidates:
                selected = candidates[0]

        if selected is None:
            raise ModelResolutionError(
                f"no compatible registered manifest supports {kind.value}"
            )
        if not selected.selectable:
            raise ModelResolutionError(
                f"selected {kind.value} manifest is not runtime compatible; no fallback"
            )
        return SettingsModelResolution(selected, source, inherited)

    def save_selections(
        self,
        *,
        project_id: str | None,
        selections: Mapping[CapabilityKind | LegacyCapabilityKind | str, str],
    ) -> object:
        exact: dict[LegacyCapabilityKind, str] = {}
        for raw_capability, manifest_id in selections.items():
            kind = _universal_capability(raw_capability)
            option = self._option_for_token(kind, str(manifest_id).strip())
            if option is None or not option.selectable:
                raise ModelResolutionError(
                    f"selected manifest is incompatible with {kind.value}; no fallback"
                )
            exact[_legacy_capability(kind)] = option.manifest_id
        return self.selection_service.save_settings(
            project_id=project_id,
            preset=ProviderPreset.CUSTOM,
            selections=exact,
        )

    def runtime_plan_identity(
        self,
        project_id: str | None,
        capability: CapabilityKind | LegacyCapabilityKind | str,
    ) -> dict[str, object]:
        resolved = self.resolve(project_id, capability)
        option = resolved.option
        return {
            "provider_capability": _legacy_capability(option.capability).value,
            "provider_id": option.runtime_provider_id,
            "model_id": option.model_id,
            "endpoint_profile_id": option.runtime_endpoint_profile_id,
            "deployment_region": option.deployment_region,
            "endpoint_class": option.runtime_endpoint_class,
            "credential_reference": option.credential_reference,
            "selection_source": resolved.source,
            "provider_parameters": {
                "manifest_id": option.manifest_id,
                "manifest_hash": option.manifest_hash,
                "codec_id": option.codec_id,
            },
        }

    def _option_for_token(
        self, capability: CapabilityKind, token: str
    ) -> SettingsModelOption | None:
        if not token:
            return None
        exact = [item for item in self.inventory(capability) if item.manifest_id == token]
        if len(exact) == 1:
            return exact[0]
        endpoint = [
            item for item in self.inventory(capability) if item.endpoint_profile_id == token
        ]
        # Legacy endpoint selections remain readable only when they identify
        # one exact manifest. Shared endpoints never guess a model.
        return endpoint[0] if len(endpoint) == 1 else None

    def _option(self, manifest: object, capability: CapabilityKind) -> SettingsModelOption:
        compatible, reason = self._compatibility(manifest, capability)
        reference = str(getattr(manifest, "credential_reference", "") or "") or None
        configured = reference is None or self._credential_present(reference)
        readiness = getattr(manifest, "readiness", None)
        metadata = _mapping(getattr(manifest, "metadata", {}))
        endpoint = str(getattr(manifest, "endpoint_profile_id", "") or "")
        endpoint_class = str(getattr(manifest, "endpoint_class", "") or "")
        codec_id = str(getattr(manifest, "codec_id", "") or "")
        runtime_available = (
            compatible
            and codec_id in self.codec_ids
            and endpoint in self.endpoint_profile_ids
        )
        return SettingsModelOption(
            capability=capability,
            manifest_id=str(getattr(manifest, "id", "")),
            manifest_hash=str(getattr(manifest, "manifest_hash", "")),
            provider_id=str(getattr(manifest, "provider_id", "")),
            provider_name=provider_label(str(getattr(manifest, "provider_id", ""))),
            runtime_provider_id=_runtime_identity(
                manifest, "runtime_provider_id", getattr(manifest, "provider_id", "")
            ),
            model_id=str(getattr(manifest, "model_id", "")),
            display_name=str(getattr(manifest, "display_name", "")),
            deployment_region=str(getattr(manifest, "deployment_region", "")),
            endpoint_profile_id=endpoint,
            runtime_endpoint_profile_id=_runtime_identity(
                manifest, "runtime_endpoint_profile_id", endpoint
            ),
            endpoint_class=endpoint_class,
            runtime_endpoint_class=_runtime_identity(
                manifest, "runtime_endpoint_class", endpoint_class
            ),
            credential_reference=reference,
            registered=True,
            compatible=compatible,
            compatibility_reason=reason,
            configured=configured,
            verified=bool(getattr(readiness, "verified", False)),
            runtime_available=runtime_available,
            create_authorized=bool(getattr(readiness, "create_authorized", False)),
            authorization_required=bool(
                getattr(manifest, "authorization_required", False)
            ),
            explicit_selection_required=metadata.get("requires_explicit_selection")
            is True
            or _explicit_selection(manifest),
            protocol=str(getattr(getattr(manifest, "protocol", None), "value", "")),
            codec_id=codec_id,
            input_modalities=tuple(getattr(manifest, "input_modalities", ())),
            output_modalities=tuple(getattr(manifest, "output_modalities", ())),
            manifest=manifest,
        )

    def _compatibility(
        self, manifest: object, capability: CapabilityKind
    ) -> tuple[bool, str]:
        try:
            actual = CapabilityKind.coerce(getattr(manifest, "capability"))
            protocol = ProtocolFamily.coerce(getattr(manifest, "protocol"))
        except (AttributeError, TypeError, ValueError):
            return False, "manifest contract invalid"
        if actual is not capability:
            return False, "capability mismatch"
        if protocol not in _SUPPORTED_PROTOCOLS:
            return False, "protocol is not supported"
        region = str(getattr(manifest, "deployment_region", "")).upper()
        if region not in _SUPPORTED_REGIONS:
            return False, "deployment region is not supported"
        required_inputs, required_outputs = _REQUIRED_MODALITIES[capability]
        inputs = {str(item).casefold() for item in getattr(manifest, "input_modalities", ())}
        outputs = {str(item).casefold() for item in getattr(manifest, "output_modalities", ())}
        if not required_inputs.issubset(inputs):
            return False, "required input modality is unavailable"
        if not required_outputs.issubset(outputs):
            return False, "required output modality is unavailable"
        if not str(getattr(manifest, "endpoint_profile_id", "") or ""):
            return False, "endpoint profile is missing"
        return True, "compatible"

    def _credential_present(self, reference: str) -> bool:
        if self.credential_store is None:
            return False
        try:
            configured = getattr(self.credential_store, "configured", None)
            if callable(configured):
                return configured(reference) is True
            providers = getattr(self.credential_store, "configured_providers", None)
            return callable(providers) and reference in set(providers())
        except Exception:
            return False


def validate_connection_value(requirement: Mapping[str, object], value: str) -> str:
    """Validate one submitted setting without returning or logging secrets."""

    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError("连接配置不能为空")
    validation = str(requirement.get("validation") or "")
    if validation == "DASHSCOPE_WORKSPACE_URL":
        return dashscope_workspace_endpoint_profile(candidate).base_url
    if requirement.get("secret", True) is False:
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("连接地址无效") from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("连接地址必须是无凭据、无 query 的 HTTPS URL")
        return candidate.rstrip("/")
    return candidate


__all__ = [
    "CAPABILITY_LABELS",
    "CAPABILITY_ORDER",
    "SettingsModelOption",
    "SettingsModelResolution",
    "SettingsModelService",
    "provider_label",
    "region_label",
    "validate_connection_value",
]
