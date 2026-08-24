from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import (
    ProductionAttempt,
    ProductionAttemptStatus,
    ProductionJob,
    ProductionJobStatus,
    ProductionShot,
    ProductionShotStatus,
    ReferenceBindingType,
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .reference_assets import ReferenceAssetService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionServiceError(RuntimeError):
    pass


class ProductionService:
    """Persistence and lifecycle boundary for future production runtimes."""

    def __init__(self, repository: ProjectRepository | None = None, *, reference_service: ReferenceAssetService | None = None):
        self.repository = repository or ProjectRepository()
        self.reference_service = reference_service or ReferenceAssetService(self.repository)

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProductionServiceError(f"项目不存在: {project_id}")
        return project

    def _get_job(self, project_id: str, job_id: str) -> ProductionJob:
        self._require_project(project_id)
        job = self.repository.get_production_job(job_id)
        if job is None or job.project_id != project_id:
            raise ProductionServiceError("ProductionJob 不属于该项目")
        return job

    def _get_shot(self, project_id: str, production_shot_id: str) -> tuple[ProductionJob, ProductionShot]:
        self._require_project(project_id)
        shot = self.repository.get_production_shot(production_shot_id)
        if shot is None:
            raise ProductionServiceError("ProductionShot 不存在")
        job = self.repository.get_production_job(shot.production_job_id)
        if job is None or job.project_id != project_id:
            raise ProductionServiceError("ProductionShot 不属于该项目")
        return job, shot

    def _approved_story(self, project_id: str):
        return next(
            (revision for revision in self.repository.list_story_revisions(project_id) if revision["status"] is StoryRevisionStatus.APPROVED),
            None,
        )

    def _approved_script(self, project_id: str):
        return next(
            (revision for revision in self.repository.list_script_revisions(project_id) if revision["status"] is ScriptRevisionStatus.APPROVED),
            None,
        )

    def _shot_plan(self, project_id: str, revision_id: str | None):
        if revision_id is None:
            return next(
                (revision for revision in self.repository.list_shot_revisions(project_id) if revision["status"] is ShotRevisionStatus.APPROVED),
                None,
            )
        revision = self.repository.get_shot_revision(revision_id)
        if revision is None or revision["project_id"] != project_id:
            raise ProductionServiceError("Shot Plan revision 不属于该项目")
        return revision

    def validate_job_readiness(self, project_id: str, shot_plan_revision_id: str | None = None) -> dict[str, object]:
        """Validate the canonical Story → Script → Approved Shot Plan chain and references."""
        self._require_project(project_id)
        if shot_plan_revision_id:
            referenced_job = self.repository.get_production_job(shot_plan_revision_id)
            if referenced_job is not None:
                if referenced_job.project_id != project_id:
                    raise ProductionServiceError("ProductionJob 不属于该项目")
                shot_plan_revision_id = referenced_job.shot_plan_revision_id
        blocked: list[str] = []
        story = self._approved_story(project_id)
        script = self._approved_script(project_id)
        plan = self._shot_plan(project_id, shot_plan_revision_id)

        if story is None:
            blocked.append("approved Story Bible is required")
        if script is None:
            blocked.append("approved Structured Script is required")
        if plan is None:
            blocked.append("approved Shot Plan is required")
        elif plan["status"] is not ShotRevisionStatus.APPROVED:
            blocked.append("Shot Plan revision is not APPROVED")

        required_characters: set[str] = set()
        required_locations: set[str] = set()
        shot_count = 0
        if plan is not None and plan["status"] is ShotRevisionStatus.APPROVED:
            shot_count = len(plan["content"].shots)
            if script is None:
                blocked.append("Shot Plan source Structured Script is unavailable")
            elif plan["source_script_revision_id"] != script["id"]:
                blocked.append("Shot Plan is outdated relative to the approved Structured Script")
            else:
                scenes = {scene.id: scene for scene in script["content"].scenes}
                for shot in plan["content"].shots:
                    scene = scenes.get(shot.scene_id)
                    if scene is None:
                        blocked.append(f"Shot {shot.id} references an unknown scene")
                        continue
                    required_characters.update(shot.subject)
                    required_locations.add(scene.location_id)

        if story is not None and script is not None:
            if script["source_story_revision_id"] != story["id"]:
                blocked.append("Structured Script is outdated relative to the approved Story Bible")
            story_characters = {character.id for character in story["content"].characters}
            story_locations = {location.id for location in story["content"].locations}
            missing_characters = sorted(required_characters - story_characters)
            missing_locations = sorted(required_locations - story_locations)
            blocked.extend(f"unknown required character: {item}" for item in missing_characters)
            blocked.extend(f"unknown required location: {item}" for item in missing_locations)
            if not missing_characters and not missing_locations:
                for character_id in sorted(required_characters):
                    if not self.reference_service.is_binding_ready(
                        project_id, ReferenceBindingType.CHARACTER, character_id, story["id"]
                    ):
                        blocked.append(f"missing locked character reference: {character_id}")
                for location_id in sorted(required_locations):
                    if not self.reference_service.is_binding_ready(
                        project_id, ReferenceBindingType.LOCATION, location_id, story["id"]
                    ):
                        blocked.append(f"missing locked location reference: {location_id}")

        return {
            "ready": not blocked,
            "blocked_reasons": blocked,
            "project_id": project_id,
            "shot_plan_revision_id": plan["id"] if plan is not None else shot_plan_revision_id,
            "required_characters": sorted(required_characters),
            "required_locations": sorted(required_locations),
            "shot_count": shot_count,
        }

    calculate_production_readiness = validate_job_readiness

    def create_production_job(self, project_id: str, shot_plan_revision_id: str | None = None) -> ProductionJob:
        self._require_project(project_id)
        plan = self._shot_plan(project_id, shot_plan_revision_id)
        if plan is None:
            raise ProductionServiceError("请先确认 APPROVED Shot Plan")
        if plan["status"] is not ShotRevisionStatus.APPROVED:
            raise ProductionServiceError("只有 APPROVED Shot Plan 可以创建 ProductionJob")
        readiness = self.validate_job_readiness(project_id, plan["id"])
        now = _now()
        job = ProductionJob(
            id=uuid4().hex,
            project_id=project_id,
            shot_plan_revision_id=plan["id"],
            status=ProductionJobStatus.READY if readiness["ready"] else ProductionJobStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_production_job(job)

    def list_jobs(self, project_id: str) -> list[ProductionJob]:
        self._require_project(project_id)
        return self.repository.list_production_jobs(project_id)

    def get_job(self, project_id: str, job_id: str) -> ProductionJob:
        return self._get_job(project_id, job_id)

    def create_production_shots(self, project_id: str, job_id: str) -> list[ProductionShot]:
        job = self._get_job(project_id, job_id)
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if plan is None or plan["status"] is not ShotRevisionStatus.APPROVED:
            raise ProductionServiceError("ProductionJob 必须引用 APPROVED Shot Plan")
        existing = {shot.shot_id: shot for shot in self.repository.list_production_shots(job.id)}
        created = list(existing.values())
        for index, shot in enumerate(plan["content"].shots, start=1):
            if shot.id in existing:
                continue
            production_shot = self.repository.create_production_shot(
                ProductionShot(
                    id=uuid4().hex,
                    production_job_id=job.id,
                    shot_id=shot.id,
                    order_index=index,
                    status=ProductionShotStatus.PENDING,
                    created_at=_now(),
                )
            )
            created.append(production_shot)
        return sorted(created, key=lambda item: (item.order_index, item.id))

    def start_attempt(
        self,
        project_id: str,
        production_shot_id: str,
        runtime_adapter: str,
        input_snapshot_json: dict[str, object] | None = None,
        runtime_reference: str | None = None,
    ) -> ProductionAttempt:
        job, shot = self._get_shot(project_id, production_shot_id)
        if not runtime_adapter.strip():
            raise ProductionServiceError("runtime_adapter 不能为空")
        if job.status in (ProductionJobStatus.SUCCEEDED, ProductionJobStatus.CANCELLED):
            raise ProductionServiceError("ProductionJob 已结束，不能启动新的 attempt")
        if shot.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED):
            raise ProductionServiceError("ProductionShot 已完成，不能重试")
        if job.status in (ProductionJobStatus.DRAFT, ProductionJobStatus.READY, ProductionJobStatus.FAILED):
            readiness = self.validate_job_readiness(project_id, job.shot_plan_revision_id)
            if not readiness["ready"]:
                raise ProductionServiceError("ProductionJob 尚未 READY: " + "; ".join(readiness["blocked_reasons"]))
            job = self.repository.update_production_job_status(job.id, ProductionJobStatus.READY, updated_at=_now())
        attempts = self.repository.list_production_attempts(shot.id)
        attempt = self.repository.create_production_attempt(
            ProductionAttempt(
                id=uuid4().hex,
                production_shot_id=shot.id,
                attempt_number=(attempts[-1].attempt_number + 1 if attempts else 1),
                status=ProductionAttemptStatus.STARTED,
                runtime_adapter=runtime_adapter,
                runtime_reference=runtime_reference,
                input_snapshot_json=input_snapshot_json or {},
                created_at=_now(),
            )
        )
        self.repository.update_production_shot_status(shot.id, ProductionShotStatus.RUNNING)
        self.repository.update_production_job_status(job.id, ProductionJobStatus.RUNNING, updated_at=_now())
        return attempt

    def _require_started_attempt(self, project_id: str, attempt_id: str) -> tuple[ProductionJob, ProductionShot, ProductionAttempt]:
        self._require_project(project_id)
        attempt = self.repository.get_production_attempt(attempt_id)
        if attempt is None:
            raise ProductionServiceError("ProductionAttempt 不存在")
        job, shot = self._get_shot(project_id, attempt.production_shot_id)
        if attempt.status is not ProductionAttemptStatus.STARTED:
            raise ProductionServiceError("ProductionAttempt 已经结束，历史记录不可覆盖")
        return job, shot, attempt

    def complete_attempt(
        self,
        project_id: str,
        attempt_id: str,
        output_artifact_json: dict[str, object] | None = None,
        runtime_reference: str | None = None,
    ) -> ProductionAttempt:
        job, shot, attempt = self._require_started_attempt(project_id, attempt_id)
        completed = self.repository.update_production_attempt(
            attempt.id,
            status=ProductionAttemptStatus.SUCCEEDED,
            runtime_reference=runtime_reference or attempt.runtime_reference,
            output_artifact_json=output_artifact_json,
        )
        self.repository.update_production_shot_status(shot.id, ProductionShotStatus.SUCCEEDED)
        all_shots = self.repository.list_production_shots(job.id)
        final_status = ProductionJobStatus.SUCCEEDED if all_shots and all(item.status in (ProductionShotStatus.SUCCEEDED, ProductionShotStatus.SKIPPED) for item in all_shots) else ProductionJobStatus.RUNNING
        self.repository.update_production_job_status(job.id, final_status, updated_at=_now())
        return completed

    def fail_attempt(self, project_id: str, attempt_id: str, error_message: str) -> ProductionAttempt:
        job, shot, attempt = self._require_started_attempt(project_id, attempt_id)
        failed = self.repository.update_production_attempt(
            attempt.id, status=ProductionAttemptStatus.FAILED, error_message=error_message,
        )
        self.repository.update_production_shot_status(shot.id, ProductionShotStatus.FAILED)
        self.repository.update_production_job_status(job.id, ProductionJobStatus.FAILED, updated_at=_now())
        return failed

    def get_job_status(self, project_id: str, job_id: str) -> dict[str, object]:
        job = self._get_job(project_id, job_id)
        shots = self.repository.list_production_shots(job.id)
        attempts = {shot.id: self.repository.list_production_attempts(shot.id) for shot in shots}
        return {"job": job, "shots": shots, "attempts": attempts, "status": job.status}
