"""Cold-start coordination over the existing durable runners."""

from __future__ import annotations

from aidrama_studio.domain import ProductionExecutionStatus
from aidrama_studio.storage.repositories import ProjectRepository

from .background_runner import BackgroundProductionRunner
from .heavy_job_runner import HeavyJobRunner
from .production_queue import ProductionQueueService
from .production_reliability import PaidBudgetService


class ProductionRecoveryService:
    """Resume persisted work without introducing another queue authority."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        background_runner: BackgroundProductionRunner | None = None,
        heavy_job_runner: HeavyJobRunner | None = None,
        queue_service: ProductionQueueService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.background_runner = background_runner or BackgroundProductionRunner(
            self.repository
        )
        self.heavy_job_runner = heavy_job_runner or HeavyJobRunner(self.repository)
        self.queue_service = queue_service or ProductionQueueService(self.repository)
        self.paid_budgets = PaidBudgetService(self.repository)

    def resume_pending_work(
        self,
        project_id: str | None = None,
        *,
        max_heavy_jobs: int = 100,
    ) -> dict[str, object]:
        """Reconstruct work exclusively from SQLite and frozen artifacts."""

        projects = (
            [project_id]
            if project_id is not None
            else [project.id for project in self.repository.list_projects()]
        )
        uncertain = self.paid_budgets.reconcile_startup(project_id)
        prepared = self.queue_service.resume_preparing_tasks(project_id)
        boundary_tasks = []
        reconciled = []
        production_results = []
        for scoped_project_id in projects:
            for job in self.repository.list_production_jobs(scoped_project_id):
                for execution in self.repository.list_production_executions(job.id):
                    if execution.status in {
                        ProductionExecutionStatus.QUEUED,
                        ProductionExecutionStatus.RUNNING,
                    }:
                        boundary_tasks.append(
                            self.background_runner.enqueue(
                                scoped_project_id, execution.id
                            )
                        )
            reconciled.extend(
                self.background_runner.reconcile(scoped_project_id)
            )
            production_results.extend(
                self.background_runner.run_once(scoped_project_id)
            )
        heavy = self.heavy_job_runner.resume_pending_work(
            project_id, max_jobs=max_heavy_jobs
        )
        return {
            "uncertain_creates": uncertain,
            "prepared_actions": prepared,
            "boundary_tasks": boundary_tasks,
            "reconciled_tasks": reconciled,
            "production_results": production_results,
            "heavy_jobs": heavy,
        }


__all__ = ["ProductionRecoveryService"]
