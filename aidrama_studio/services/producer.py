"""AI Producer projections backed by canonical current production truth."""

from __future__ import annotations

from aidrama_studio.domain import (
    ProducerPolicy,
    ProducerRecommendation,
    ProductionProgress,
    ProductionShot,
    ProductionShotStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .current_state import CurrentProductionStateService
from .production import ProductionService


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
        state = self.current_state_service.derive(project_id, job.id)
        # A projection may be requested immediately after a job is created,
        # before the orchestration service has materialized ProductionShot
        # rows.  Build ephemeral rows for display only; reads must never write
        # them to SQLite.
        projected_shots = list(state.shots)
        if not projected_shots:
            plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
            if plan is not None:
                projected_shots = [
                    ProductionShot(
                        id=f"projection:{job.id}:{shot.id}",
                        production_job_id=job.id,
                        shot_id=shot.id,
                        order_index=index,
                        status=ProductionShotStatus.PENDING,
                        created_at=job.created_at,
                    )
                    for index, shot in enumerate(
                        sorted(plan["content"].shots, key=lambda item: (item.order, item.id)),
                        start=1,
                    )
                ]
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        high_risk = tuple(
            sorted(
                shot.id
                for shot in (plan["content"].shots if plan else [])
                if getattr(shot.risk_level, "value", shot.risk_level) == "HIGH" and not shot.risk_override
            )
        )
        completed = sum(shot.status is ProductionShotStatus.SUCCEEDED for shot in projected_shots)
        failed = sum(shot.status is ProductionShotStatus.FAILED for shot in projected_shots)
        pending = sum(shot.status is ProductionShotStatus.PENDING for shot in projected_shots)
        running = next((shot.shot_id for shot in projected_shots if shot.status is ProductionShotStatus.RUNNING), None)
        final_ready = bool(state.final_readiness and state.final_readiness.ready)
        return ProductionProgress(
            project_id=project_id,
            job_id=job.id,
            total_shots=len(projected_shots),
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

    def _qc_retry_count(self, project_id: str, job_id: str, target_id: str) -> int:
        """Count executed QC retries without recording a read observation.

        A QC result is an executed action; a Producer recommendation is only a
        projection.  The first result for an artifact is the initial check and
        subsequent persisted results are retries.  This deliberately scans the
        current job only, preserving a history-wide audit without consuming a
        budget merely by opening the Producer page.
        """
        counts: dict[str, int] = {}
        for execution in self.repository.list_production_executions(job_id):
            snapshot = execution.input_snapshot
            shot_ids = set(snapshot.shot_parameters) if snapshot else set()
            artifacts = self.repository.list_production_artifacts(execution.id)
            results = self.repository.list_production_qc_results(project_id, execution.id)
            for artifact in artifacts:
                metadata = artifact.metadata_json or {}
                artifact_shot = metadata.get("shot_id")
                if artifact_shot not in shot_ids and artifact_shot != target_id and target_id not in shot_ids:
                    continue
                total = sum(1 for result in results if result.artifact_id == artifact.id)
                if total:
                    counts[target_id] = counts.get(target_id, 0) + max(0, total - 1)
            if not artifacts and target_id in shot_ids:
                total = sum(1 for result in results if result.artifact_id is None)
                counts[target_id] = counts.get(target_id, 0) + max(0, total - 1)
        return counts.get(target_id, 0)

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
