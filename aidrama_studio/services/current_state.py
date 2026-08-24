"""One canonical, project-scoped projection of current production truth.

Historical executions, QC failures, and reviews remain queryable, but this
module selects the latest relevant ProductionJob and asks FinalAssemblyService
for qualified sources.  Consumers must use this projection instead of
aggregating append-only failures across every historical attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aidrama_studio.domain import ProductionJob, ProductionShot, ProductionShotStatus
from aidrama_studio.storage.repositories import ProjectRepository

from .final_assembly import FinalAssemblyService, FinalAssemblyServiceError


@dataclass(frozen=True, slots=True)
class CurrentProductionState:
    project_id: str
    job: ProductionJob | None
    shots: tuple[ProductionShot, ...] = ()
    qualified_sources: dict[str, Any] = field(default_factory=dict)
    qc_blockers: tuple[str, ...] = ()
    historical_qc_failures: int = 0
    production_complete: bool = False
    final_readiness: Any | None = None
    post_production_ready: bool = False

    @property
    def current_job_id(self) -> str | None:
        return self.job.id if self.job else None


class CurrentProductionStateService:
    """Derive current state deterministically from canonical repositories."""

    def __init__(self, repository: ProjectRepository | None = None, *, final_assembly_service: FinalAssemblyService | None = None):
        self.repository = repository or ProjectRepository()
        self.final_assembly_service = final_assembly_service or FinalAssemblyService(self.repository)

    def select_job(self, project_id: str, job_id: str | None = None) -> ProductionJob | None:
        jobs = self.repository.list_production_jobs(project_id)
        if job_id is not None:
            selected = self.repository.get_production_job(job_id)
            if selected is None or selected.project_id != project_id:
                raise ValueError("ProductionJob 不属于该项目")
            return selected
        if not jobs:
            return None
        active = [job for job in jobs if job.status.value != "CANCELLED"]
        # Newest relevant job wins; created_at is canonical and id is a stable
        # tie-breaker.  Older failed attempts never poison a newer job.
        return max(active or jobs, key=lambda item: (item.created_at, item.id))

    def derive(self, project_id: str, job_id: str | None = None) -> CurrentProductionState:
        job = self.select_job(project_id, job_id)
        if job is None:
            return CurrentProductionState(project_id=project_id, job=None)
        shots = tuple(sorted(self.repository.list_production_shots(job.id), key=lambda item: (item.order_index, item.id)))
        qualified: dict[str, Any] = {}
        blockers: list[str] = []
        historical_failures = 0
        for execution in self.repository.list_production_executions(job.id):
            historical_failures += sum(
                1 for result in self.repository.list_production_qc_results(project_id, execution.id)
                if result.status.value == "QC_FAILED"
            )
        for shot in shots:
            try:
                source = self.final_assembly_service.select_qualified_source(project_id, job.id, shot.id)
            except (FinalAssemblyServiceError, ValueError):
                source = None
            if source is not None:
                qualified[shot.id] = source
            elif shot.status in {ProductionShotStatus.SUCCEEDED, ProductionShotStatus.FAILED}:
                blockers.append(shot.shot_id)
        production_complete = bool(shots) and all(
            shot.status in {ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED} for shot in shots
        ) and len(qualified) == len(shots)
        final_readiness = None
        try:
            final_readiness = self.final_assembly_service.calculate_readiness(project_id, job.id)
        except FinalAssemblyServiceError:
            pass
        post_ready = self._post_ready(project_id, job.id)
        return CurrentProductionState(
            project_id=project_id,
            job=job,
            shots=shots,
            qualified_sources=qualified,
            qc_blockers=tuple(blockers),
            historical_qc_failures=historical_failures,
            production_complete=production_complete,
            final_readiness=final_readiness,
            post_production_ready=post_ready,
        )

    def _post_ready(self, project_id: str, current_job_id: str) -> bool:
        """Require post output to belong to the current production chain.

        A successful post render from an older ProductionJob is historical
        evidence only; it must never make a newer job appear ready.
        """
        assemblies = self.repository.list_final_assemblies(project_id, current_job_id)
        if not assemblies:
            return False
        assembly = max(assemblies, key=lambda item: (item.created_at, item.id))
        if assembly.status.value not in {"READY", "SUCCEEDED"}:
            return False
        render_attempts = self.repository.list_final_assembly_render_attempts(assembly.id)
        successful_sources = [
            item
            for item in render_attempts
            if item.status.value == "SUCCEEDED" and item.output_relative_path
        ]
        if not successful_sources:
            return False
        root = (self.repository.paths.projects / project_id).resolve()
        for source_attempt in reversed(successful_sources):
            source_path = self._safe_project_file(root, source_attempt.output_relative_path)
            if source_path is None or not source_path.is_file() or source_path.stat().st_size <= 0:
                continue
            for plan in self.repository.list_post_plans(project_id):
                if plan.source_final_assembly_id != assembly.id:
                    continue
                # V1 requires an immutable pin to the exact successful
                # FinalAssembly render attempt; legacy unpinned plans are not
                # allowed to satisfy current-chain readiness.
                if plan.source_final_assembly_render_attempt_id != source_attempt.id:
                    continue
                for attempt in reversed(self.repository.list_post_render_attempts(project_id, plan.id)):
                    if (
                        attempt.status.value != "SUCCEEDED"
                        or not attempt.output_relative_path
                        or attempt.source_final_assembly_render_attempt_id != source_attempt.id
                    ):
                        continue
                    output = self._safe_project_file(root, attempt.output_relative_path)
                    if output is not None and output.is_file() and output.stat().st_size > 0:
                        return True
        return False

    @staticmethod
    def _safe_project_file(root: Path, relative: str | None) -> Path | None:
        if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
            return None
        normalized = relative.strip().replace("\\", "/")
        if normalized.startswith("/"):
            return None
        parts = normalized.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            return None
        return target


__all__ = ["CurrentProductionState", "CurrentProductionStateService"]
