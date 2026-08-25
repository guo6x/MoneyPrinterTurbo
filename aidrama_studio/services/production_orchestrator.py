"""Durable multi-shot production orchestration.

The orchestrator is deliberately thin: ``ProductionService`` owns shot and
attempt state, ``ProductionExecutionService`` owns execution events, the
``ProductionWorker`` owns the runtime adapter seam, and
``ProductionQCService`` owns deterministic quality gates.  No in-memory
cursor is required to resume a job; all decisions are reconstructed from
persisted ProductionShot/Attempt/Execution/QC facts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from aidrama_studio.domain import (
    ProductionAttemptStatus,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionJob,
    ProductionJobStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShot,
    ProductionShotStatus,
    ProductionInputSnapshot,
    RuntimePlan,
)
from aidrama_studio.services.adapters import ProductionRuntimeAdapter
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError
from .production_execution import ProductionExecutionService, ProductionExecutionServiceError
from .production_qc import ProductionQCService, ProductionQCServiceError
from .production_worker import ProductionWorker, ProductionWorkerError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionOrchestratorError(RuntimeError):
    """Raised when a durable multi-shot transition cannot be completed."""


class ProductionOrchestrator:
    """Execute one immutable runtime execution per ProductionShot in order."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        execution_service: ProductionExecutionService | None = None,
        qc_service: ProductionQCService | None = None,
        worker: ProductionWorker | None = None,
        adapter: ProductionRuntimeAdapter | None = None,
        runtime_adapter: ProductionRuntimeAdapter | None = None,
        adapter_resolver: Callable[[RuntimePlan], ProductionRuntimeAdapter] | None = None,
        runtime_plan_ids_by_shot: Mapping[str, str] | None = None,
    ) -> None:
        if production_service is not None:
            self.production_service = production_service
            self.repository = production_service.repository
        else:
            self.repository = repository or ProjectRepository()
            self.production_service = ProductionService(self.repository)
        if execution_service is None and worker is not None:
            execution_service = getattr(worker, "execution_service", None)
        self.execution_service = execution_service or ProductionExecutionService(
            self.repository, production_service=self.production_service
        )
        self.qc_service = qc_service or ProductionQCService(self.repository)
        self.worker = worker
        self.adapter = adapter or runtime_adapter
        self.adapter_resolver = adapter_resolver
        self.runtime_plan_ids_by_shot = dict(runtime_plan_ids_by_shot or {})

    def run_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        adapter: ProductionRuntimeAdapter | None = None,
        adapter_resolver: Callable[[RuntimePlan], ProductionRuntimeAdapter] | None = None,
        runtime_plan_ids_by_shot: Mapping[str, str] | None = None,
    ) -> ProductionJob:
        """Run/resume a job until a shot fails, is cancelled, or all pass."""
        job = self._get_job(project_id, production_job_id)
        if job.status in (ProductionJobStatus.FAILED, ProductionJobStatus.CANCELLED):
            return job
        if job.status is ProductionJobStatus.SUCCEEDED:
            persisted_shots = self._ordered_shots(job.id)
            if not persisted_shots or all(
                shot.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED)
                for shot in persisted_shots
            ):
                return job
        try:
            self.production_service.create_production_shots(project_id, job.id)
        except ProductionServiceError as exc:
            raise ProductionOrchestratorError(str(exc)) from exc

        runtime_adapter = adapter or self.adapter
        runtime_resolver = adapter_resolver or self.adapter_resolver
        plan_ids = dict(self.runtime_plan_ids_by_shot)
        plan_ids.update(dict(runtime_plan_ids_by_shot or {}))
        if runtime_adapter is None and self.worker is not None:
            runtime_adapter = getattr(self.worker, "adapter", None)
        if runtime_adapter is None and runtime_resolver is None:
            raise ProductionOrchestratorError("ProductionOrchestrator 需要一个 ProductionRuntimeAdapter")

        while True:
            job = self._get_job(project_id, production_job_id)
            if job.status in (ProductionJobStatus.FAILED, ProductionJobStatus.CANCELLED):
                return job
            shots = self._ordered_shots(job.id)
            if not shots:
                raise ProductionOrchestratorError("ProductionJob 没有 ProductionShot")
            if all(shot.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED) for shot in shots):
                return self._sync_job_status(job, shots)
            if any(shot.status is ProductionShotStatus.FAILED for shot in shots):
                return self._sync_job_status(job, shots)
            shot = next(
                shot for shot in shots
                if shot.status not in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED)
            )
            try:
                passed = self._run_shot(
                    project_id,
                    job,
                    shot,
                    runtime_adapter,
                    adapter_resolver=runtime_resolver,
                    runtime_plan_ids_by_shot=plan_ids,
                )
            except ProductionOrchestratorError:
                raise
            if not passed:
                return self._get_job(project_id, production_job_id)

    resume_job = run_job

    def cancel_job(self, project_id: str, production_job_id: str, reason: str = "user") -> ProductionJob:
        """Cancel active work while leaving completed and pending history intact."""
        job = self._get_job(project_id, production_job_id)
        if job.status is ProductionJobStatus.SUCCEEDED:
            return job
        if job.status in (ProductionJobStatus.CANCELLED, ProductionJobStatus.FAILED):
            return job
        shots = self._ordered_shots(job.id)
        executions = self.execution_service.list_executions(project_id, job.id)
        for execution in executions:
            if execution.status not in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING):
                continue
            try:
                cancel = getattr(self.worker, "cancel", None) if self.worker is not None else None
                if callable(cancel):
                    cancel(project_id, execution.id, reason)
                else:
                    self.execution_service.cancel_execution(project_id, execution.id, reason)
                durable = self.execution_service.get_execution(project_id, execution.id)
                if durable.status in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING):
                    self.execution_service.cancel_execution(
                        project_id, execution.id, reason, _notify_runtime=False
                    )
            except (ProductionExecutionServiceError, ProductionWorkerError) as exc:
                raise ProductionOrchestratorError(str(exc)) from exc
            shot = self._execution_shot(execution, shots)
            if shot is not None:
                attempt = self._attempt_for_shot(project_id, shot)
                if attempt is not None and attempt.status is ProductionAttemptStatus.STARTED:
                    try:
                        self.production_service.cancel_attempt(project_id, attempt.id, reason)
                    except ProductionServiceError as exc:
                        raise ProductionOrchestratorError(str(exc)) from exc
        current = self._get_job(project_id, production_job_id)
        if current.status is not ProductionJobStatus.CANCELLED:
            self.repository.update_production_job_status(current.id, ProductionJobStatus.CANCELLED, updated_at=_now())
        return self._get_job(project_id, production_job_id)

    def get_next_actionable_shot(self, project_id: str, production_job_id: str) -> ProductionShot | None:
        job = self._get_job(project_id, production_job_id)
        if job.status in (ProductionJobStatus.FAILED, ProductionJobStatus.CANCELLED, ProductionJobStatus.SUCCEEDED):
            return None
        shots = self._ordered_shots(job.id)
        if any(shot.status is ProductionShotStatus.FAILED for shot in shots):
            return None
        return next(
            (shot for shot in shots if shot.status not in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED)),
            None,
        )

    def get_job_progress(self, project_id: str, production_job_id: str) -> dict[str, object]:
        job = self._get_job(project_id, production_job_id)
        shots = self._ordered_shots(job.id)
        total = len(shots)
        completed = sum(shot.status is ProductionShotStatus.SUCCEEDED for shot in shots)
        failed = sum(shot.status is ProductionShotStatus.FAILED for shot in shots)
        pending = sum(
            shot.status not in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.FAILED, ProductionShotStatus.SKIPPED)
            for shot in shots
        )
        current = next(
            (shot.shot_id for shot in shots if shot.status not in (
                ProductionShotStatus.SUCCEEDED,
                ProductionShotStatus.FAILED,
                ProductionShotStatus.SKIPPED,
            )),
            None,
        )
        return {
            "job_id": job.id,
            "project_id": project_id,
            "total_shots": total,
            "completed_shots": completed,
            "failed_shots": failed,
            "pending_shots": pending,
            "current_shot_id": current,
            "percent_complete": round((completed / total) * 100, 2) if total else 0,
            "status": job.status,
        }

    progress = get_job_progress

    def _run_shot(
        self,
        project_id: str,
        job: ProductionJob,
        shot: ProductionShot,
        adapter: ProductionRuntimeAdapter | None,
        *,
        adapter_resolver: Callable[[RuntimePlan], ProductionRuntimeAdapter] | None = None,
        runtime_plan_ids_by_shot: Mapping[str, str] | None = None,
    ) -> bool:
        executions = self.execution_service.list_executions(project_id, job.id)
        execution = self._latest_execution_for_shot(executions, shot.shot_id)
        attempt = self._attempt_for_shot(project_id, shot)
        plan_id = (
            (runtime_plan_ids_by_shot or {}).get(shot.shot_id)
            or (execution.runtime_plan_id if execution is not None else None)
        )
        runtime_plan = self.repository.get_runtime_plan(plan_id) if plan_id else None
        if plan_id and (
            runtime_plan is None
            or runtime_plan.project_id != project_id
            or runtime_plan.production_job_id != job.id
        ):
            raise ProductionOrchestratorError("冻结 RuntimePlan 不属于当前 ProductionJob")
        shot_adapter = adapter_resolver(runtime_plan) if adapter_resolver is not None and runtime_plan is not None else adapter
        if shot_adapter is None:
            if adapter_resolver is not None:
                raise ProductionOrchestratorError(f"镜头 {shot.shot_id} 缺少冻结 RuntimePlan")
            raise ProductionOrchestratorError("ProductionOrchestrator 需要一个 ProductionRuntimeAdapter")

        # A terminal execution is immutable history.  If its shot is not
        # complete (runtime failure or QC rejection), the next run must create
        # a fresh execution/attempt pair rather than trying to mutate or reuse
        # the old one.
        if execution is not None and execution.status in {
            ProductionExecutionStatus.SUCCEEDED,
            ProductionExecutionStatus.FAILED,
            ProductionExecutionStatus.CANCELLED,
        } and shot.status not in {ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED}:
            execution = None

        if execution is None:
            snapshot = self._shot_snapshot(project_id, job, shot)
            try:
                execution, attempt = self.execution_service.enqueue_shot_execution_with_attempt(
                    project_id,
                    job.id,
                    snapshot,
                    worker_type=getattr(shot_adapter, "name", "runtime"),
                    runtime_plan_id=runtime_plan.id if runtime_plan is not None else None,
                    generation_brief_id=runtime_plan.generation_brief_id if runtime_plan is not None else None,
                )
            except (ProductionExecutionServiceError, ProductionServiceError) as exc:
                raise ProductionOrchestratorError(str(exc)) from exc

        if execution.status is ProductionExecutionStatus.QUEUED:
            result = self._worker_run(project_id, execution.id, shot_adapter)
        elif execution.status is ProductionExecutionStatus.RUNNING:
            result = self._worker_resume(project_id, execution.id, shot_adapter)
        else:
            result = execution

        if result.status is ProductionExecutionStatus.CANCELLED:
            self._cancel_shot(project_id, shot, attempt, "runtime cancelled")
            return False
        if result.status is ProductionExecutionStatus.FAILED:
            message = self._last_failure(project_id, result.id) or "runtime execution failed"
            self._fail_shot(project_id, shot, attempt, message)
            return False
        if result.status is not ProductionExecutionStatus.SUCCEEDED:
            return False

        passed, reason = self._qc_gate(project_id, result)
        if not passed:
            self._fail_shot(project_id, shot, attempt, reason)
            return False
        self._complete_shot(project_id, job, shot, attempt, result)
        return True

    def _shot_snapshot(self, project_id: str, job: ProductionJob, shot: ProductionShot) -> ProductionInputSnapshot:
        try:
            full = self.execution_service.create_input_snapshot(project_id, job.id)
        except ProductionExecutionServiceError as exc:
            raise ProductionOrchestratorError(str(exc)) from exc
        parameters = full.shot_parameters.get(shot.shot_id)
        if parameters is None:
            raise ProductionOrchestratorError(f"ShotPlan 中不存在 ProductionShot: {shot.shot_id}")
        return ProductionInputSnapshot(
            project_id=full.project_id,
            story_revision_id=full.story_revision_id,
            script_revision_id=full.script_revision_id,
            shot_plan_revision_id=full.shot_plan_revision_id,
            reference_asset_versions=full.reference_asset_versions,
            shot_parameters={shot.shot_id: parameters},
        )

    def _worker_run(self, project_id: str, execution_id: str, adapter: ProductionRuntimeAdapter) -> ProductionExecution:
        worker = self.worker or ProductionWorker(self.execution_service, adapter)
        try:
            return worker.run(project_id, execution_id, adapter=adapter)
        except Exception as exc:
            current = self.execution_service.get_execution(project_id, execution_id)
            if current.status not in (
                ProductionExecutionStatus.FAILED,
                ProductionExecutionStatus.CANCELLED,
                ProductionExecutionStatus.SUCCEEDED,
            ):
                try:
                    self.execution_service.fail_execution(project_id, execution_id, str(exc))
                except ProductionExecutionServiceError:
                    pass
            return self.execution_service.get_execution(project_id, execution_id)

    def _worker_resume(self, project_id: str, execution_id: str, adapter: ProductionRuntimeAdapter) -> ProductionExecution:
        worker = self.worker or ProductionWorker(self.execution_service, adapter)
        resume = getattr(worker, "resume", None)
        if not callable(resume):
            raise ProductionOrchestratorError("ProductionWorker 不支持恢复 RUNNING execution")
        try:
            return resume(project_id, execution_id, adapter=adapter)
        except Exception as exc:
            current = self.execution_service.get_execution(project_id, execution_id)
            if current.status not in (
                ProductionExecutionStatus.FAILED,
                ProductionExecutionStatus.CANCELLED,
                ProductionExecutionStatus.SUCCEEDED,
            ):
                self.execution_service.fail_execution(project_id, execution_id, str(exc))
            return self.execution_service.get_execution(project_id, execution_id)

    def _qc_gate(self, project_id: str, execution: ProductionExecution) -> tuple[bool, str]:
        try:
            artifacts = self.execution_service.list_artifacts(project_id, execution.id)
            if not artifacts:
                result = self.qc_service.run_qc(project_id, execution.id, None)
                return False, self._qc_reason(result)
            results = self.qc_service.list_results(project_id, execution.id)
            for artifact in artifacts:
                matching = [result for result in results if result.artifact_id == artifact.id]
                result = matching[-1] if matching else self.qc_service.run_qc(project_id, execution.id, artifact.id)
                results.append(result)
                result_status = getattr(result.status, "value", result.status)
                if str(result_status) != ProductionQCStatus.QC_PASS.value:
                    return False, self._qc_reason(result)
                reviews = self.qc_service.list_reviews(project_id, result.id)
                # Reviews are append-only; only the latest decision for the
                # current QC result is authoritative.  A later approval can
                # supersede an earlier rejection without deleting history.
                latest_review = reviews[-1] if reviews else None
                latest_decision = (
                    str(getattr(latest_review.decision, "value", latest_review.decision))
                    if latest_review is not None
                    else ""
                )
                if latest_decision == ProductionReviewDecision.REJECTED.value:
                    return False, "human review rejected QC result"
            return True, ""
        except ProductionQCServiceError as exc:
            return False, f"QC failed: {exc}"

    @staticmethod
    def _qc_reason(result) -> str:
        summary = result.summary_json or {}
        error = summary.get("error") if isinstance(summary, Mapping) else None
        if error:
            return f"QC failed: {error}"
        status = getattr(result.status, "value", result.status)
        return f"QC failed: {status}"

    def _complete_shot(
        self,
        project_id: str,
        job: ProductionJob,
        shot: ProductionShot,
        attempt,
        execution: ProductionExecution,
    ) -> None:
        if attempt is not None and attempt.status is ProductionAttemptStatus.STARTED:
            try:
                self.production_service.complete_attempt(
                    project_id,
                    attempt.id,
                    output_artifact_json={"execution_id": execution.id},
                )
            except ProductionServiceError as exc:
                raise ProductionOrchestratorError(str(exc)) from exc
        elif shot.status is not ProductionShotStatus.SUCCEEDED:
            self.repository.update_production_shot_status(shot.id, ProductionShotStatus.SUCCEEDED)
        self._sync_job_status(self._get_job(project_id, job.id), self._ordered_shots(job.id))

    def _fail_shot(self, project_id: str, shot: ProductionShot, attempt, reason: str) -> None:
        if attempt is not None and attempt.status is ProductionAttemptStatus.STARTED:
            try:
                self.production_service.fail_attempt(project_id, attempt.id, reason)
            except ProductionServiceError as exc:
                raise ProductionOrchestratorError(str(exc)) from exc
        else:
            self.repository.update_production_shot_status(shot.id, ProductionShotStatus.FAILED)
            job = self._get_job(project_id, self.repository.get_production_shot(shot.id).production_job_id)
            self.repository.update_production_job_status(job.id, ProductionJobStatus.FAILED, updated_at=_now())

    def _cancel_shot(self, project_id: str, shot: ProductionShot, attempt, reason: str) -> None:
        if attempt is not None and attempt.status is ProductionAttemptStatus.STARTED:
            try:
                self.production_service.cancel_attempt(project_id, attempt.id, reason)
            except ProductionServiceError as exc:
                raise ProductionOrchestratorError(str(exc)) from exc
        else:
            self.repository.update_production_shot_status(shot.id, ProductionShotStatus.SKIPPED)
            job = self._get_job(project_id, self.repository.get_production_shot(shot.id).production_job_id)
            self.repository.update_production_job_status(job.id, ProductionJobStatus.CANCELLED, updated_at=_now())

    def _attempt_for_shot(self, project_id: str, shot: ProductionShot):
        attempts = self.repository.list_production_attempts(shot.id)
        if not attempts:
            return None
        return attempts[-1]

    @staticmethod
    def _execution_shot(execution: ProductionExecution, shots: list[ProductionShot]) -> ProductionShot | None:
        if execution.input_snapshot is None or len(execution.input_snapshot.shot_parameters) != 1:
            return None
        shot_id = next(iter(execution.input_snapshot.shot_parameters))
        return next((shot for shot in shots if shot.shot_id == shot_id), None)

    def _latest_execution_for_shot(self, executions: list[ProductionExecution], shot_id: str) -> ProductionExecution | None:
        matches = [execution for execution in executions if self._execution_shot_id(execution) == shot_id]
        return matches[-1] if matches else None

    @staticmethod
    def _execution_shot_id(execution: ProductionExecution) -> str | None:
        if execution.input_snapshot is None or len(execution.input_snapshot.shot_parameters) != 1:
            return None
        return next(iter(execution.input_snapshot.shot_parameters))

    def _last_failure(self, project_id: str, execution_id: str) -> str | None:
        try:
            for event in reversed(self.execution_service.list_events(project_id, execution_id)):
                if event.payload_json.get("error"):
                    return str(event.payload_json["error"])
        except ProductionExecutionServiceError:
            return None
        return None

    def _get_job(self, project_id: str, production_job_id: str) -> ProductionJob:
        try:
            return self.production_service.get_job(project_id, production_job_id)
        except ProductionServiceError as exc:
            raise ProductionOrchestratorError(str(exc)) from exc

    def _ordered_shots(self, job_id: str) -> list[ProductionShot]:
        return sorted(
            self.repository.list_production_shots(job_id),
            key=lambda shot: (shot.order_index, shot.id),
        )

    def _sync_job_status(self, job: ProductionJob, shots: list[ProductionShot]) -> ProductionJob:
        if job.status in (ProductionJobStatus.CANCELLED, ProductionJobStatus.FAILED):
            return self._get_job(job.project_id, job.id)
        if shots and all(shot.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED) for shot in shots):
            status = ProductionJobStatus.SUCCEEDED
        elif any(shot.status is ProductionShotStatus.FAILED for shot in shots):
            status = ProductionJobStatus.FAILED
        else:
            status = ProductionJobStatus.RUNNING
        if job.status is not status:
            self.repository.update_production_job_status(job.id, status, updated_at=_now())
        return self._get_job(job.project_id, job.id)


__all__ = ["ProductionOrchestrator", "ProductionOrchestratorError"]
