"""Provider-neutral capability profiles and frozen request planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from aidrama_studio.domain import CapabilityProfile, ProductionInputSnapshot, ProviderTask
from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import CapabilityKind, CapabilityRegistry, CapabilityUnavailable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ProviderProfileError(RuntimeError):
    pass


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
    ) -> CapabilityProfile:
        capability_value = CapabilityKind(capability).value if not isinstance(capability, str) else capability
        now = _now()
        record = CapabilityProfile(
            id=uuid4().hex,
            project_id=project_id,
            capability=capability_value,
            provider_id=provider_id,
            model_id=model_id,
            profile=dict(profile or {}),
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_capability_profile(record)

    def list(self, project_id: str | None = None, capability: CapabilityKind | str | None = None) -> list[CapabilityProfile]:
        value = CapabilityKind(capability).value if capability is not None and not isinstance(capability, str) else capability
        return self.repository.list_capability_profiles(project_id, value)

    def select(self, project_id: str, capability: CapabilityKind | str, *, provider_id: str | None = None) -> CapabilityProfile:
        if self.repository.get_project(project_id) is None:
            raise ProviderProfileError(f"项目不存在: {project_id}")
        value = CapabilityKind(capability).value if not isinstance(capability, str) else capability
        profiles = [profile for profile in self.list(project_id, value) if profile.enabled]
        if provider_id:
            profiles = [profile for profile in profiles if profile.provider_id == provider_id]
        if profiles:
            return profiles[0]
        if self.registry is not None:
            provider = self.registry.get(value)
            if provider is not None:
                status = provider.status
                if status.available:
                    return CapabilityProfile(
                        id=f"runtime:{project_id}:{value}", project_id=project_id,
                        capability=value, provider_id=str(getattr(provider, "provider_name", "provider")),
                        model_id=str(status.metadata.get("model", "runtime")), profile=dict(status.metadata),
                        created_at="runtime", updated_at="runtime",
                    )
        raise CapabilityUnavailable(f"能力 {value} 没有可用 Provider")

    @staticmethod
    def plan_duration(target_seconds: float, *, minimum: float = 2.0, maximum: float = 5.0) -> DurationPlan:
        if target_seconds <= 0 or minimum <= 0 or maximum < minimum:
            raise ProviderProfileError("duration 参数无效")
        target = float(target_seconds)
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
        return DurationPlan(max(chunks), target, tuple(chunks), "CHUNK_AND_CONTINUE" if len(chunks) > 1 else "SINGLE_SHOT")

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


__all__ = ["DurationPlan", "ProviderProfileError", "ProviderProfileService", "ReferenceTrace"]
