"""Resolve a frozen RuntimePlan to the exact configured runtime adapter.

The Streamlit page never constructs provider adapters.  A packaged/background
runner uses this service after reading the durable ProviderTask and RuntimePlan
from SQLite.  Credentials are resolved at execution time from the canonical
credential/capability inventory and are never copied into the plan.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any

from aidrama_studio.domain import ProviderTask, RuntimePlan
from aidrama_studio.services.adapters import ProductionRuntimeAdapter

from .ai_capabilities import CapabilityKind, CapabilityRegistry, default_capability_registry


class ProductionRuntimeResolutionError(RuntimeError):
    pass


class ProductionRuntimeResolver:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or default_capability_registry()

    def resolve(
        self,
        task: ProviderTask,
        runtime_plan: RuntimePlan | None = None,
    ) -> ProductionRuntimeAdapter:
        capability = runtime_plan.provider_capability if runtime_plan is not None else task.capability
        provider_id = runtime_plan.provider_id if runtime_plan is not None else task.provider_id
        model_id = runtime_plan.model_id if runtime_plan is not None else task.model_id
        try:
            providers = self.registry.list(CapabilityKind(capability))
        except ValueError as exc:
            raise ProductionRuntimeResolutionError(f"不支持的 runtime capability: {capability}") from exc
        provider = next((item for item in providers if self._provider_matches(item, provider_id)), None)
        if provider is None:
            raise ProductionRuntimeResolutionError(f"冻结 Provider 不在当前能力清单中: {provider_id}")
        try:
            status = provider.status
        except Exception as exc:
            raise ProductionRuntimeResolutionError(f"Provider readiness 不可用: {provider_id}") from exc
        if not bool(getattr(status, "available", False)):
            reason = str(getattr(status, "reason", "provider unavailable"))
            raise ProductionRuntimeResolutionError(f"Provider 尚未就绪: {provider_id} ({reason})")
        authorization = runtime_plan.authorization if runtime_plan is not None else task.request_summary
        if capability == CapabilityKind.VIDEO_GENERATIVE.value and authorization.get("approved") is not True:
            raise ProductionRuntimeResolutionError("冻结 RuntimePlan 缺少明确付费授权")
        adapter = getattr(provider, "adapter", provider)
        if not isinstance(adapter, ProductionRuntimeAdapter):
            required = ("validate", "submit", "cancel", "get_status")
            if not all(callable(getattr(adapter, name, None)) for name in required):
                raise ProductionRuntimeResolutionError(f"Provider 没有 ProductionRuntimeAdapter: {provider_id}")
        return self._pin_adapter(adapter, runtime_plan, model_id)

    @staticmethod
    def _provider_matches(provider: object, provider_id: str) -> bool:
        adapter = getattr(provider, "adapter", provider)
        candidates = {
            str(getattr(provider, "provider_name", "")),
            str(getattr(adapter, "provider_id", "")),
            str(getattr(adapter, "name", "")),
            adapter.__class__.__name__,
        }
        target = provider_id.casefold()
        return any(candidate and candidate.casefold() == target for candidate in candidates)

    @staticmethod
    def _pin_adapter(
        adapter: ProductionRuntimeAdapter,
        runtime_plan: RuntimePlan | None,
        model_id: str,
    ) -> ProductionRuntimeAdapter:
        config = getattr(adapter, "config", None)
        if config is None or not is_dataclass(config):
            configured_model = str(getattr(adapter, "model_id", model_id))
            if configured_model not in {model_id, "runtime", ""}:
                raise ProductionRuntimeResolutionError("当前 adapter 无法恢复冻结 model")
            return adapter

        changes: dict[str, Any] = {}
        config_fields = getattr(config, "__dataclass_fields__", {})
        if "model" in config_fields:
            changes["model"] = model_id
        if runtime_plan is not None:
            parameters = runtime_plan.provider_parameters
            if "duration_seconds" in config_fields:
                value = parameters.get("duration_seconds", runtime_plan.provider_generation_duration)
                changes["duration_seconds"] = int(round(float(value)))
            if "resolution" in config_fields:
                value = parameters.get("provider_resolution") or parameters.get("resolution")
                if value:
                    changes["resolution"] = str(value)
        pinned_config = replace(config, **changes)

        # Recreate known HTTP adapters so nested clients also use the frozen
        # non-secret config.  Test/local adapters without a dataclass config
        # are returned above unchanged.
        from .adapters.seedance_video import SeedanceProductionAdapter
        from .adapters.wan_video import WanProductionAdapter

        if isinstance(adapter, SeedanceProductionAdapter):
            return SeedanceProductionAdapter(config=pinned_config)
        if isinstance(adapter, WanProductionAdapter):
            return WanProductionAdapter(
                config=pinned_config,
                reference_resolver=adapter.reference_resolver,
            )
        try:
            return adapter.__class__(config=pinned_config)
        except TypeError as exc:
            raise ProductionRuntimeResolutionError(
                f"无法以冻结配置重建 adapter: {adapter.__class__.__name__}"
            ) from exc


__all__ = ["ProductionRuntimeResolutionError", "ProductionRuntimeResolver"]
