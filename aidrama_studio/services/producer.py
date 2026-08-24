"""AI Producer projections backed by canonical current production truth."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import ProducerPolicy, ProducerRecommendation, ProductionProgress, ProductionShotStatus
from aidrama_studio.storage.repositories import ProjectRepository

from .current_state import CurrentProductionStateService
from .production import ProductionService, ProductionServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProducerServiceError(RuntimeError):
    pass


class ProducerService:
    """Derive operational recommendations without mutating creative truth.

    Provider execution remains behind ProductionWorker.  Automatic retry is
    disabled by default.  QC retry recommendations are append-only durable
    observations, so repeatedly rendering the Producer page cannot recommend
    the same retry forever without consuming the configured budget.
    """

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        policy: ProducerPolicy | None = None,
        current_state_service: CurrentProductionStateService | None = None,
    ):
        self.repository = repository or ProjectRepository()
        self.production_service = production_service or ProductionService(self.repository)
        self.policy = policy or ProducerPolicy()
        self.current_state_service = current_state_service or CurrentProductionStateService(self.repository)

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProducerServiceError(f"项目不存在: {project_id}")
        return project

    def _select_job(self, project_id: str, job_id: str | None = None):
        try:
            return self.current_state_service.select_job(project_id, job_id)
        except ValueError as exc:
            raise ProducerServiceError(str(exc)) from exc

    def progress(self, project_id: str, job_id: str | None = None) -> ProductionProgress:
        self._require_project(project_id)
        job = self._select_job(project_id, job_id)
        if job is None:
            return ProductionProgress(project_id, None)
        shots = self.repository.list_production_shots(job.id)
        if not shots:
            try:
                shots = self.production_service.create_production_shots(project_id, job.id)
            except ProductionServiceError:
                shots = []
        state = self.current_state_service.derive(project_id, job.id)
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        high_risk = tuple(
            sorted(
                shot.id
                for shot in (plan["content"].shots if plan else [])
                if getattr(shot.risk_level, "value", shot.risk_level) == "HIGH" and not shot.risk_override
            )
        )
        completed = sum(shot.status is ProductionShotStatus.SUCCEEDED for shot in state.shots)
        failed = sum(shot.status is ProductionShotStatus.FAILED for shot in state.shots)
        pending = sum(shot.status is ProductionShotStatus.PENDING for shot in state.shots)
        running = next((shot.shot_id for shot in state.shots if shot.status is ProductionShotStatus.RUNNING), None)
        final_ready = bool(state.final_readiness and state.final_readiness.ready)
        return ProductionProgress(
            project_id=project_id,
            job_id=job.id,
            total_shots=len(state.shots),
            completed_shots=completed,
            pending_shots=pending,
            failed_shots=failed,
            blocked_shots=len(state.qc_blockers),
            current_shot_id=running,
            high_risk_shots=high_risk,
            qc_failures=len(state.qc_blockers),
            final_assembly_ready=final_ready,
            post_production_ready=state.post_production_ready,
        )

    def readiness(self, project_id: str, job_id: str | None = None) -> dict[str, object]:
        self._require_project(project_id)
        revision_id = None
        if job_id:
            job = self.repository.get_production_job(job_id)
            if job is None or job.project_id != project_id:
                raise ProducerServiceError("ProductionJob 不属于该项目")
            revision_id = job.shot_plan_revision_id
        return self.production_service.calculate_production_readiness(project_id, revision_id)

    def _record_qc_recommendation(self, project_id: str, job_id: str, target_id: str) -> int:
        self.repository.create_producer_recommendation_event(
            event_id=uuid4().hex,
            project_id=project_id,
            production_job_id=job_id,
            action="RETRY_QC",
            target_id=target_id,
            metadata={"automatic_retry_enabled": self.policy.automatic_retry_enabled},
            created_at=_now(),
        )
        return len(
            self.repository.list_producer_recommendation_events(
                project_id, production_job_id=job_id, action="RETRY_QC", target_id=target_id
            )
        )

    def recommendations(self, project_id: str, job_id: str | None = None) -> list[ProducerRecommendation]:
        readiness = self.readiness(project_id, job_id)
        if not readiness.get("ready"):
            reason = "; ".join(str(item) for item in readiness.get("blocked_reasons", [])) or "生产准备未完成"
            return [ProducerRecommendation("STOP_AND_REVIEW", reason, requires_human_approval=True)]
        state = self.current_state_service.derive(project_id, job_id)
        if state.job is None:
            return [ProducerRecommendation("START_PRODUCTION", "生产前置条件已满足，可创建 Production Job", requires_human_approval=True)]
        if state.qc_blockers:
            target = state.qc_blockers[0]
            used = len(self.repository.list_producer_recommendation_events(state.project_id, production_job_id=state.job.id, action="RETRY_QC", target_id=target))
            if used < self.policy.max_qc_retry_recommendations:
                used = self._record_qc_recommendation(project_id, state.job.id, target)
                return [ProducerRecommendation("RETRY_QC", "当前镜头 QC 未通过，可在预算内人工重试", target_id=target, requires_human_approval=True, metadata={"recommendations_used": used, "recommendation_budget": self.policy.max_qc_retry_recommendations, "automatic_retry_enabled": self.policy.automatic_retry_enabled})]
            return [ProducerRecommendation("STOP_AND_REVIEW", "该镜头已达到 QC 重试建议预算", target_id=target, requires_human_approval=True, metadata={"recommendations_used": used, "recommendation_budget": self.policy.max_qc_retry_recommendations})]
        failed_shot = next((shot for shot in state.shots if shot.status is ProductionShotStatus.FAILED), None)
        if failed_shot is not None:
            attempts = self.repository.list_production_attempts(failed_shot.id)
            if len(attempts) < self.policy.max_generation_attempts_per_shot:
                return [ProducerRecommendation("RETRY_SHOT", "镜头执行失败，可在预算内人工重试", target_id=failed_shot.shot_id, requires_human_approval=True, metadata={"attempts_used": len(attempts), "attempt_budget": self.policy.max_generation_attempts_per_shot, "automatic_retry_enabled": self.policy.automatic_retry_enabled})]
            return [ProducerRecommendation("STOP_AND_REVIEW", "镜头已达到最大执行尝试次数", target_id=failed_shot.shot_id, requires_human_approval=True)]
        if not state.production_complete:
            if any(shot.status is ProductionShotStatus.RUNNING for shot in state.shots):
                return [ProducerRecommendation("RESUME_PRODUCTION", "生产队列仍有正在执行的镜头", requires_human_approval=True)]
            return [ProducerRecommendation("START_PRODUCTION", "生产队列仍有待执行镜头", requires_human_approval=True)]
        if state.final_readiness and state.final_readiness.ready:
            existing = self.repository.list_final_assemblies(project_id, state.job.id)
            if any(item.status.value in {"READY", "SUCCEEDED"} for item in existing):
                return [ProducerRecommendation("START_POST_PRODUCTION", "当前 Final Assembly 已冻结，可进入后期流程", requires_human_approval=True)]
            return [ProducerRecommendation("CREATE_NEW_FINAL_ASSEMBLY", "所有镜头已有当前 qualified source，可创建不可变成片清单", requires_human_approval=True)]
        return [ProducerRecommendation("STOP_AND_REVIEW", "当前生产结果尚未满足 Final Assembly qualification", requires_human_approval=True)]

    def high_risk_shots(self, project_id: str, job_id: str | None = None) -> list[str]:
        return list(self.progress(project_id, job_id).high_risk_shots)

    def can_retry_shot(self, project_id: str, production_shot_id: str) -> bool:
        self._require_project(project_id)
        shot = self.repository.get_production_shot(production_shot_id)
        if shot is None:
            raise ProducerServiceError("ProductionShot 不存在")
        job = self.repository.get_production_job(shot.production_job_id)
        if job is None or job.project_id != project_id:
            raise ProducerServiceError("ProductionShot 不属于该项目")
        return len(self.repository.list_production_attempts(shot.id)) < self.policy.max_generation_attempts_per_shot


__all__ = ["ProducerService", "ProducerServiceError"]
