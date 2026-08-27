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
from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import CapabilityKind, CapabilityRegistry, default_capability_registry


class ProductionRuntimeResolutionError(RuntimeError):
    pass


_POLL_ONLY_TASK_STATES = frozenset(
    {
        "SUBMITTING",
        "SUBMISSION_UNCERTAIN",
        "RECONCILIATION_REQUIRED",
        "PROVIDER_ACCEPTED",
        "PROVIDER_RUNNING",
        "POLLING_INTERRUPTED",
        "PROVIDER_SUCCEEDED_ARTIFACT_PENDING",
    }
)


class ProductionRuntimeResolver:
    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        repository: ProjectRepository | None = None,
    ) -> None:
        self.registry = registry or default_capability_registry()
        self.repository = repository or ProjectRepository()

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
        provider = next(
            (
                item
                for item in providers
                if self._provider_matches(item, provider_id)
                and self._endpoint_matches(item, runtime_plan)
            ),
            None,
        )
        if provider is None:
            raise ProductionRuntimeResolutionError(
                f"冻结 Provider/endpoint 不在当前能力清单中: {provider_id}"
            )
        try:
            status = provider.status
        except Exception as exc:
            raise ProductionRuntimeResolutionError(f"Provider readiness 不可用: {provider_id}") from exc
        status_metadata = dict(getattr(status, "metadata", {}) or {})
        task_state = str(getattr(task, "state", "") or "").strip().upper()
        provider_task_id = str(getattr(task, "provider_task_id", "") or "").strip()
        poll_only_ready = bool(
            getattr(status, "configured", False) is True
            and bool(provider_task_id)
            and task_state in _POLL_ONLY_TASK_STATES
            and status_metadata.get(
                "supports_poll_without_paid_create_authorization"
            )
            is True
        )
        if getattr(status, "available", False) is not True and not poll_only_ready:
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
        return self._pin_adapter(
            adapter,
            runtime_plan,
            model_id,
            provider_task=task,
        )

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
    def _endpoint_matches(provider: object, runtime_plan: RuntimePlan | None) -> bool:
        if runtime_plan is None or runtime_plan.endpoint_profile_id in {None, "", "LEGACY"}:
            return True
        try:
            metadata = dict(provider.status.metadata)
        except Exception:
            return False
        endpoint_id = str(metadata.get("endpoint_profile_id") or "")
        endpoint_class = str(metadata.get("endpoint_class") or "UNSPECIFIED")
        region = str(metadata.get("deployment_region") or "UNSPECIFIED")
        credential_reference = str(metadata.get("credential_reference") or "") or None
        return (
            endpoint_id == runtime_plan.endpoint_profile_id
            and endpoint_class == runtime_plan.endpoint_class
            and region == runtime_plan.deployment_region
            and (
                runtime_plan.credential_reference is None
                or credential_reference == runtime_plan.credential_reference
            )
        )

    def _pin_adapter(
        self,
        adapter: ProductionRuntimeAdapter,
        runtime_plan: RuntimePlan | None,
        model_id: str,
        provider_task: ProviderTask | None = None,
    ) -> ProductionRuntimeAdapter:
        bind_plan = getattr(adapter, "for_runtime_plan", None)
        if runtime_plan is not None and callable(bind_plan):
            return bind_plan(runtime_plan, provider_task=provider_task)
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
        from .adapters.wan_video import WanProductionAdapter, WanVideoClient

        if isinstance(adapter, SeedanceProductionAdapter):
            brief = (
                self.repository.get_generation_brief(runtime_plan.generation_brief_id)
                if runtime_plan is not None and runtime_plan.generation_brief_id
                else None
            )
            output_profile = (
                self.repository.get_output_profile(runtime_plan.output_profile_id)
                if runtime_plan is not None and runtime_plan.output_profile_id
                else None
            )
            if runtime_plan is not None and brief is None:
                raise ProductionRuntimeResolutionError(
                    "冻结 RuntimePlan 的 GenerationBrief 不存在"
                )
            from .reference_assets import ReferenceAssetService

            return SeedanceProductionAdapter(
                config=pinned_config,
                client=getattr(adapter, "_client", None),
                runtime_plan=runtime_plan,
                generation_brief=brief,
                output_profile=output_profile,
                reference_service=ReferenceAssetService(self.repository),
                downloader=getattr(adapter, "downloader", None),
                image_downloader=getattr(adapter, "image_downloader", None),
            )
        if isinstance(adapter, WanProductionAdapter):
            # Keep deterministic/injected transports usable during recovery
            # tests and in embedded hosts.  A real WanVideoClient is rebuilt
            # against the pinned config so a changed model/endpoint cannot
            # leak through its nested config; only its already-created HTTP
            # session is retained.  Duck-typed test clients are passed
            # through unchanged and therefore never turn a poll-only
            # reconciliation into an accidental network call.
            existing_client = getattr(adapter, "client", None)
            if type(existing_client) is WanVideoClient:
                pinned_client = WanVideoClient(
                    pinned_config,
                    session=getattr(existing_client, "session", None),
                )
            else:
                pinned_client = existing_client
            return WanProductionAdapter(
                config=pinned_config,
                client=pinned_client,
                reference_resolver=adapter.reference_resolver,
            )
        try:
            return adapter.__class__(config=pinned_config)
        except TypeError as exc:
            raise ProductionRuntimeResolutionError(
                f"无法以冻结配置重建 adapter: {adapter.__class__.__name__}"
            ) from exc


__all__ = ["ProductionRuntimeResolutionError", "ProductionRuntimeResolver"]
