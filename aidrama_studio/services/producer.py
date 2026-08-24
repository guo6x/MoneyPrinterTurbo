"""AI Producer control-plane projections with explicit retry budgets."""

from __future__ import annotations

from aidrama_studio.domain import ProducerPolicy, ProducerRecommendation, ProductionProgress, ProductionShotStatus, RiskLevel
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError


class ProducerServiceError(RuntimeError):
    pass


class ProducerService:
    """Derive operational recommendations without mutating creative truth.

    Provider execution remains behind ProductionWorker.  ``automatic_retry``
    is deliberately disabled by default and all recommendations carry a
    bounded budget so a producer run cannot spend indefinitely.
    """

    def __init__(self, repository: ProjectRepository | None = None, *, production_service: ProductionService | None = None, policy: ProducerPolicy | None = None):
        self.repository = repository or ProjectRepository()
        self.production_service = production_service or ProductionService(self.repository)
        self.policy = policy or ProducerPolicy()

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProducerServiceError(f"项目不存在: {project_id}")
        return project

    def progress(self, project_id: str, job_id: str | None = None) -> ProductionProgress:
        self._require_project(project_id)
        jobs = self.production_service.list_jobs(project_id)
        job = None
        if job_id is not None:
            job = self.repository.get_production_job(job_id)
            if job is None or job.project_id != project_id:
                raise ProducerServiceError("ProductionJob 不属于该项目")
        elif jobs:
            job = jobs[0]
        if job is None:
            return ProductionProgress(project_id, None)
        shots = self.repository.list_production_shots(job.id)
        if not shots:
            try:
                shots = self.production_service.create_production_shots(project_id, job.id)
            except ProductionServiceError:
                shots = []
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        high_risk = tuple(sorted(shot.id for shot in (plan["content"].shots if plan else []) if shot.risk_level is RiskLevel.HIGH and not shot.risk_override))
        completed = sum(shot.status is ProductionShotStatus.SUCCEEDED for shot in shots)
        failed = sum(shot.status is ProductionShotStatus.FAILED for shot in shots)
        pending = sum(shot.status is ProductionShotStatus.PENDING for shot in shots)
        running = next((shot.shot_id for shot in shots if shot.status is ProductionShotStatus.RUNNING), None)
        qc_failures = 0
        for execution in self.repository.list_production_executions(job.id):
            qc_failures += sum(result.status.value == "QC_FAILED" for result in self.repository.list_production_qc_results(project_id, execution.id))
        final_assembly_ready = bool(shots) and all(shot.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED) for shot in shots)
        return ProductionProgress(project_id=project_id, job_id=job.id, total_shots=len(shots), completed_shots=completed, pending_shots=pending, failed_shots=failed, blocked_shots=0, current_shot_id=running, high_risk_shots=high_risk, qc_failures=qc_failures, final_assembly_ready=final_assembly_ready, post_production_ready=False)

    def readiness(self, project_id: str, job_id: str | None = None) -> dict[str, object]:
        self._require_project(project_id)
        revision_id = None
        if job_id:
            job = self.repository.get_production_job(job_id)
            if job is None or job.project_id != project_id:
                raise ProducerServiceError("ProductionJob 不属于该项目")
            revision_id = job.shot_plan_revision_id
        return self.production_service.calculate_production_readiness(project_id, revision_id)

    def recommendations(self, project_id: str, job_id: str | None = None) -> list[ProducerRecommendation]:
        readiness = self.readiness(project_id, job_id)
        if not readiness.get("ready"):
            reason = "; ".join(str(item) for item in readiness.get("blocked_reasons", [])) or "生产准备未完成"
            return [ProducerRecommendation("STOP_AND_REVIEW", reason, requires_human_approval=True)]
        progress = self.progress(project_id, job_id)
        if progress.qc_failures:
            return [ProducerRecommendation("REVIEW_QC_FAILURE", "存在 QC 失败，需要人工决定重试", requires_human_approval=True, metadata={"max_qc_retry_recommendations": self.policy.max_qc_retry_recommendations})]
        if progress.failed_shots:
            failed_shot = next((shot for shot in self.repository.list_production_shots(progress.job_id or "") if shot.status is ProductionShotStatus.FAILED), None)
            if failed_shot:
                attempts = self.repository.list_production_attempts(failed_shot.id)
                if len(attempts) < self.policy.max_generation_attempts_per_shot:
                    return [ProducerRecommendation("RETRY_SHOT", "镜头执行失败，可在预算内重试", target_id=failed_shot.shot_id, requires_human_approval=True, metadata={"attempts_used": len(attempts), "attempt_budget": self.policy.max_generation_attempts_per_shot, "automatic_retry_enabled": self.policy.automatic_retry_enabled})]
                return [ProducerRecommendation("STOP_AND_REVIEW", "镜头已达到最大执行尝试次数", target_id=failed_shot.shot_id, requires_human_approval=True)]
        if progress.pending_shots or progress.current_shot_id:
            action = "RESUME_PRODUCTION" if progress.current_shot_id else "START_PRODUCTION"
            return [ProducerRecommendation(action, "生产队列仍有待执行镜头", requires_human_approval=True)]
        if progress.final_assembly_ready:
            return [ProducerRecommendation("CREATE_NEW_FINAL_ASSEMBLY", "所有镜头已有成功结果，可创建不可变成片清单", requires_human_approval=True)]
        return [ProducerRecommendation("START_PRODUCTION", "等待 Production Job 创建", requires_human_approval=True)]

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
