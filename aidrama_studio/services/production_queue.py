"""Non-blocking UI facade for durable production jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import ProductionJobStatus, ProviderTask
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError
from .production_orchestrator import ProductionOrchestrator, ProductionOrchestratorError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionQueueError(RuntimeError):
    pass


class ProductionQueueService:
    """Queue/cancel/read facade; it never performs provider work inline."""

    def __init__(self, repository: ProjectRepository | None = None, *, production_service: ProductionService | None = None) -> None:
        self.production_service = production_service or ProductionService(repository or ProjectRepository())
        self.repository = self.production_service.repository

    def run_job(self, project_id: str, production_job_id: str):
        return self.enqueue_job(project_id, production_job_id)

    resume_job = run_job

    def enqueue_job(self, project_id: str, production_job_id: str) -> ProviderTask:
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
        attempt_number = 1 + sum(1 for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id)
        now = _now()
        task = self.repository.create_provider_task(ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=None,
            capability="VIDEO_GENERATIVE", provider_id="CONFIGURED_RUNTIME", model_id="PINNED_RUNTIME_PLAN",
            idempotency_key=f"production-job:{job.id}:attempt:{attempt_number}", state="QUEUED",
            request_summary={"production_job_id": job.id, "attempt_number": attempt_number},
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


__all__ = ["ProductionQueueError", "ProductionQueueService"]
