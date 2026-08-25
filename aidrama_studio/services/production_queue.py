"""Non-blocking UI facade for durable production jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from aidrama_studio.domain import ProductionJobStatus, ProviderTask
from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityUnavailable,
    default_capability_registry,
)
from .production import ProductionService, ProductionServiceError
from .production_orchestrator import ProductionOrchestrator, ProductionOrchestratorError
from .production_execution import ProductionExecutionService, ProductionExecutionServiceError
from .provider_profiles import ProviderProfileError, ProviderProfileService
from .runtime_foundation import GenerationBriefCompiler, RuntimeFoundationError, RuntimePlanService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionQueueError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProductionAuthorizationPreview:
    provider_id: str
    model_id: str
    shot_count: int
    max_paid_attempts: int
    estimated_provider_requests: int
    duration_requests_by_shot: dict[str, int]


class ProductionQueueService:
    """Queue/cancel/read facade; it never performs provider work inline."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.production_service = production_service or ProductionService(repository or ProjectRepository())
        self.repository = self.production_service.repository
        self.registry = registry or default_capability_registry()
        self.provider_profiles = provider_profiles or ProviderProfileService(self.repository, registry=self.registry)
        self.execution_service = ProductionExecutionService(self.repository, production_service=self.production_service)
        self.brief_compiler = GenerationBriefCompiler(self.repository)
        self.runtime_plans = RuntimePlanService(self.repository)

    def run_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization: Mapping[str, object] | None = None,
        provider_id: str | None = None,
    ):
        return self.enqueue_job(
            project_id,
            production_job_id,
            authorization=authorization,
            provider_id=provider_id,
        )

    resume_job = run_job

    def preview_authorization(
        self,
        project_id: str,
        production_job_id: str,
        *,
        provider_id: str | None = None,
        max_paid_attempts: int = 1,
    ) -> ProductionAuthorizationPreview:
        if isinstance(max_paid_attempts, bool) or not 1 <= int(max_paid_attempts) <= 3:
            raise ProductionQueueError("每个镜头最大付费尝试次数必须为 1–3")
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(project_id, job.shot_plan_revision_id)
            if not readiness.get("ready"):
                raise ProductionQueueError("ProductionJob 尚未 READY")
            shots = self.production_service.create_production_shots(project_id, job.id)
            profile = self.provider_profiles.select(
                project_id,
                CapabilityKind.VIDEO_GENERATIVE,
                provider_id=provider_id,
            )
        except (ProductionServiceError, ProviderProfileError, CapabilityUnavailable) as exc:
            raise ProductionQueueError(str(exc)) from exc
        minimum, maximum = self._duration_limits(profile.profile)
        request_counts: dict[str, int] = {}
        shot_revision = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if shot_revision is None:
            raise ProductionQueueError("ProductionJob 缺少 Shot Plan revision")
        durations = {shot.id: float(shot.duration_seconds) for shot in shot_revision["content"].shots}
        for production_shot in shots:
            duration = durations.get(production_shot.shot_id)
            if duration is None:
                raise ProductionQueueError(f"ProductionShot 缺少冻结时长: {production_shot.shot_id}")
            duration_plan = self.provider_profiles.plan_duration(duration, minimum=minimum, maximum=maximum)
            if len(duration_plan.chunks) != 1:
                raise ProductionQueueError(
                    f"镜头 {production_shot.shot_id} 需要 {len(duration_plan.chunks)} 次 provider 请求；"
                    "当前 V1 单镜头执行只允许一次已授权请求，请先调整镜头时长"
                )
            request_counts[production_shot.shot_id] = 1
        count = len(shots)
        return ProductionAuthorizationPreview(
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            shot_count=count,
            max_paid_attempts=int(max_paid_attempts),
            estimated_provider_requests=sum(request_counts.values()) * int(max_paid_attempts),
            duration_requests_by_shot=request_counts,
        )

    def enqueue_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization: Mapping[str, object] | None = None,
        provider_id: str | None = None,
    ) -> ProviderTask:
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(project_id, job.shot_plan_revision_id)
            if not readiness.get("ready"):
                raise ProductionQueueError("ProductionJob 尚未 READY")
            self.production_service.create_production_shots(project_id, job.id)
        except ProductionServiceError as exc:
            raise ProductionQueueError(str(exc)) from exc
        active = [task for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id and task.state in {"QUEUED", "RUNNING", "PAUSED", "RECONCILIATION_REQUIRED"}]
        if active:
            return active[-1]
        authorization = dict(authorization or {})
        if authorization.get("approved") is not True:
            raise ProductionQueueError("开始生成前必须明确批准本次有界付费请求")
        max_paid_attempts = authorization.get("max_paid_attempts", 1)
        try:
            max_paid_attempts = int(max_paid_attempts)
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("max_paid_attempts 无效") from exc
        preview = self.preview_authorization(
            project_id,
            production_job_id,
            provider_id=provider_id,
            max_paid_attempts=max_paid_attempts,
        )
        # Bind the approval to the exact provider/model/request bound shown to
        # the user.  Callers cannot approve one preview and silently execute a
        # different provider or a larger request budget.
        for key, expected in (
            ("provider_id", preview.provider_id),
            ("model_id", preview.model_id),
            ("estimated_provider_requests", preview.estimated_provider_requests),
        ):
            supplied = authorization.get(key)
            if supplied is not None and supplied != expected:
                raise ProductionQueueError(f"付费授权与当前 {key} 不匹配，请重新确认")
        authorization_id = str(authorization.get("authorization_id") or uuid4().hex)
        authorized_at = str(authorization.get("authorized_at") or _now())
        frozen_authorization = {
            "approved": True,
            "authorization_id": authorization_id,
            "authorized_at": authorized_at,
            "max_paid_attempts": preview.max_paid_attempts,
            "estimated_provider_requests": preview.estimated_provider_requests,
            "provider_id": preview.provider_id,
            "model_id": preview.model_id,
        }
        try:
            provider_profile = self.provider_profiles.select(
                project_id,
                CapabilityKind.VIDEO_GENERATIVE,
                provider_id=preview.provider_id,
            )
            snapshot = self.execution_service.create_input_snapshot(project_id, job.id)
            shot_plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
            if shot_plan is None:
                raise ProductionQueueError("ProductionJob 缺少 Shot Plan revision")
            shot_by_id = {shot.id: shot for shot in shot_plan["content"].shots}
            minimum, maximum = self._duration_limits(provider_profile.profile)
            plan_ids: dict[str, str] = {}
            for production_shot in self.repository.list_production_shots(job.id):
                shot = shot_by_id.get(production_shot.shot_id)
                if shot is None:
                    raise ProductionQueueError(f"ProductionShot 不属于冻结 Shot Plan: {production_shot.shot_id}")
                brief = self.brief_compiler.compile(project_id, job.id, shot.id)
                duration_plan = self.provider_profiles.plan_duration(
                    float(shot.duration_seconds), minimum=minimum, maximum=maximum
                )
                references = self._shot_references(snapshot.reference_asset_versions, brief)
                plan = self.runtime_plans.create(
                    project_id,
                    production_job_id=job.id,
                    brief=brief,
                    provider_capability=CapabilityKind.VIDEO_GENERATIVE.value,
                    provider_id=preview.provider_id,
                    model_id=preview.model_id,
                    generation_mode=(
                        "image_to_video" if references else "text_to_video"
                    ),
                    provider_generation_duration=duration_plan.provider_duration_seconds,
                    target_creative_duration=duration_plan.target_creative_duration_seconds,
                    audio_strategy=str(provider_profile.profile.get("audio_strategy") or "EXTERNAL_TTS"),
                    provider_parameters={
                        key: value
                        for key, value in provider_profile.profile.items()
                        if key not in {"api_key", "authorization", "token", "secret"}
                    },
                    reference_version_ids=tuple(references.values()),
                    reference_roles={version_id: binding for binding, version_id in references.items()},
                    continuity_strategy=str(provider_profile.profile.get("continuity_strategy") or "REFERENCE_ONLY"),
                    authorization=frozen_authorization,
                    prompt_template_version=str(provider_profile.profile.get("prompt_template_version") or "v1"),
                )
                plan_ids[shot.id] = plan.id
        except (
            ProductionExecutionServiceError,
            ProviderProfileError,
            CapabilityUnavailable,
            RuntimeFoundationError,
        ) as exc:
            raise ProductionQueueError(str(exc)) from exc
        attempt_number = 1 + sum(1 for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id)
        now = _now()
        task = self.repository.create_provider_task(ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=None,
            capability=CapabilityKind.VIDEO_GENERATIVE.value,
            provider_id=preview.provider_id,
            model_id=preview.model_id,
            idempotency_key=f"production-job:{job.id}:attempt:{attempt_number}", state="QUEUED",
            request_summary={
                "production_job_id": job.id,
                "attempt_number": attempt_number,
                "runtime_plan_ids_by_shot": plan_ids,
                "provider_profile_id": provider_profile.id,
                "shot_count": preview.shot_count,
                **frozen_authorization,
            },
            created_at=now, updated_at=now,
        ))
        if job.status not in {ProductionJobStatus.QUEUED, ProductionJobStatus.RUNNING}:
            self.repository.update_production_job_status(job.id, ProductionJobStatus.QUEUED, updated_at=now)
        return task

    def cancel_job(self, project_id: str, production_job_id: str, reason: str = "user"):
        job = self.production_service.get_job(project_id, production_job_id)
        tasks = [task for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id]
        if tasks:
            task = tasks[-1]
            if task.state == "QUEUED":
                self.repository.update_provider_task(task.model_copy(update={"state": "CANCELLED", "metadata": dict(task.metadata) | {"cancel_reason": reason}, "updated_at": _now()}))
            elif task.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                self.repository.update_provider_task(task.model_copy(update={"state": "RECONCILIATION_REQUIRED", "metadata": dict(task.metadata) | {"cancel_requested": True, "cancel_reason": reason}, "updated_at": _now()}))
        if job.status not in {ProductionJobStatus.SUCCEEDED, ProductionJobStatus.FAILED, ProductionJobStatus.CANCELLED}:
            self.repository.update_production_job_status(job.id, ProductionJobStatus.CANCELLED, updated_at=_now())
        return self.production_service.get_job(project_id, job.id)

    def get_job_progress(self, project_id: str, production_job_id: str) -> dict[str, object]:
        # This is a pure persisted-state projection; no adapter is required.
        return ProductionOrchestrator(production_service=self.production_service).get_job_progress(project_id, production_job_id)

    @staticmethod
    def _duration_limits(profile: Mapping[str, object]) -> tuple[float, float]:
        raw = profile.get("supported_durations")
        values: list[float] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    values.append(value)
        if values:
            return min(values), max(values)
        try:
            minimum = float(profile.get("minimum_duration_seconds", 2.0))
            maximum = float(profile.get("maximum_duration_seconds", 15.0))
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("Provider duration profile 无效") from exc
        return minimum, maximum

    @staticmethod
    def _shot_references(reference_versions: Mapping[str, str], brief) -> dict[str, str]:
        character_ids = {
            str(item.get("id"))
            for item in brief.character_context
            if isinstance(item, Mapping) and item.get("id")
        }
        location_id = str(brief.location_context.get("id") or "")
        allowed = {f"CHARACTER:{item}" for item in character_ids}
        if location_id:
            allowed.add(f"LOCATION:{location_id}")
        allowed.add(f"SHOT:{brief.shot_id}")
        # Style/prop references are project-level creative constraints and are
        # relevant to every shot. Character/location references are selected
        # by the exact frozen GenerationBrief, never alphabetically.
        return {
            str(binding): str(version_id)
            for binding, version_id in reference_versions.items()
            if str(binding) in allowed
            or str(binding).startswith("STYLE:")
            or str(binding).startswith("PROP:")
        }


__all__ = ["ProductionAuthorizationPreview", "ProductionQueueError", "ProductionQueueService"]
