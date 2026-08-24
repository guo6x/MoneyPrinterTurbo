from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from loguru import logger

from aidrama_studio.domain import AspectRatio, Project, ProjectStatus
from aidrama_studio.domain.project import utc_now_iso
from aidrama_studio.storage import ProjectRepository


@dataclass(frozen=True, slots=True)
class DeleteProjectResult:
    deleted: bool
    archived_artifacts_to: Path | None = None


class ProjectService:
    def __init__(self, repository: ProjectRepository | None = None):
        self.repository = repository or ProjectRepository()

    def create(
        self,
        title: str,
        description: str = "",
        aspect_ratio: AspectRatio | str = AspectRatio.PORTRAIT,
        target_duration_seconds: int = 60,
        *,
        status: ProjectStatus | str = ProjectStatus.DRAFT,
    ) -> Project:
        now = utc_now_iso()
        project = Project(
            id=uuid4().hex,
            title=title.strip(),
            description=description.strip(),
            status=ProjectStatus(status),
            aspect_ratio=AspectRatio(aspect_ratio),
            target_duration_seconds=int(target_duration_seconds),
            created_at=now,
            updated_at=now,
        ).validate()
        directory = self.repository.project_directory(project.id)
        try:
            directory.mkdir(parents=True, exist_ok=False)
            self.repository.create_project(project)
        except Exception:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
            raise
        return project

    def create_demo(self) -> Project:
        return self.create(
            title="DEMO · 雾港来信",
            description="演示项目：用于体验 AIDrama Studio 的项目工作流。",
            aspect_ratio=AspectRatio.PORTRAIT,
            target_duration_seconds=60,
        )

    def get(self, project_id: str) -> Project | None:
        return self.repository.get_project(project_id)

    def list(self) -> list[Project]:
        return self.repository.list_projects()

    def update(
        self,
        project_id: str,
        *,
        title: str,
        description: str,
        status: ProjectStatus | str,
        aspect_ratio: AspectRatio | str,
        target_duration_seconds: int,
    ) -> Project:
        existing = self.get(project_id)
        if existing is None:
            raise KeyError("要更新的项目不存在")
        requested_status = ProjectStatus(status)
        if requested_status is ProjectStatus.COMPLETED:
            from .current_state import CurrentProductionStateService

            if CurrentProductionStateService(self.repository).workflow_stage(project_id) is not ProjectStatus.COMPLETED:
                raise ValueError("项目尚未完成当前 canonical production/post chain")
        updated = existing.with_updates(
            title=title.strip(),
            description=description.strip(),
            status=requested_status,
            aspect_ratio=AspectRatio(aspect_ratio),
            target_duration_seconds=int(target_duration_seconds),
        )
        return self.repository.update_project(updated)

    def delete(
        self, project_id: str, *, confirmed: bool = False
    ) -> DeleteProjectResult:
        if not confirmed:
            raise ValueError("删除项目需要明确确认")
        project = self.get(project_id)
        if project is None:
            return DeleteProjectResult(deleted=False)

        # Never delete a project while a durable provider/execution task could
        # still write into its storage. Cancellation/reconciliation must happen
        # first; deletion remains an explicit, recoverable archive operation.
        with self.repository.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM production_jobs WHERE project_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
                (project_id,),
            ).fetchone()
            active_task = connection.execute(
                "SELECT 1 FROM provider_tasks WHERE project_id=? AND state IN ('INTENT','QUEUED','RUNNING','PAUSED') LIMIT 1",
                (project_id,),
            ).fetchone()
        if active or active_task:
            raise ValueError("项目存在活动制作任务，取消或完成任务后才能删除")

        project_dir = self.repository.project_directory(project_id)
        archived_to = None
        if project_dir.exists():
            if any(project_dir.iterdir()):
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archived_to = (
                    self.repository.paths.archived_projects
                    / f"{project_id}-{timestamp}"
                )
                archived_to.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(project_dir), str(archived_to))
                logger.info(
                    f"archived non-empty project artifacts before deletion: {archived_to}"
                )
            else:
                project_dir.rmdir()

        try:
            deleted = self.repository.delete_project(project_id)
        except Exception:
            if archived_to and archived_to.exists() and not project_dir.exists():
                shutil.move(str(archived_to), str(project_dir))
            raise
        return DeleteProjectResult(deleted=deleted, archived_artifacts_to=archived_to)
