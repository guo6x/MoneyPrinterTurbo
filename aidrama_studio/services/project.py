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

from .active_work import project_has_active_work


@dataclass(frozen=True, slots=True)
class DeleteProjectResult:
    deleted: bool
    archived_artifacts_to: Path | None = None
    recovery_archive_to: Path | None = None


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
        delivery_resolution_label: str = "1080p",
        target_fps: float = 30.0,
        quality_mode: str = "STANDARD",
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
            # The project-level OutputProfile is the canonical versioned
            # delivery intent. Project duration/aspect remain synchronized
            # compatibility projections used by creative planning.
            from .runtime_foundation import OutputProfileService

            OutputProfileService(self.repository).create(
                project.id,
                aspect_ratio=project.aspect_ratio.value,
                target_episode_duration_seconds=float(project.target_duration_seconds),
                delivery_resolution_label=delivery_resolution_label,
                target_fps=target_fps,
                quality_mode=quality_mode,
            )
        except Exception:
            with self.repository.transaction() as connection:
                connection.execute("DELETE FROM projects WHERE id=?", (project.id,))
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
        delivery_resolution_label: str | None = None,
        target_fps: float | None = None,
        quality_mode: str | None = None,
    ) -> Project:
        existing = self.get(project_id)
        if existing is None:
            raise KeyError("要更新的项目不存在")
        requested_status = ProjectStatus(status)
        if requested_status is ProjectStatus.COMPLETED:
            from .current_state import CurrentProductionStateService

            if CurrentProductionStateService(self.repository).workflow_stage(project_id) is not ProjectStatus.COMPLETED:
                raise ValueError("项目尚未完成当前 canonical production/post chain")
        requested_aspect = AspectRatio(aspect_ratio)
        requested_duration = int(target_duration_seconds)
        updated = existing.with_updates(
            title=title.strip(),
            description=description.strip(),
            status=requested_status,
            aspect_ratio=requested_aspect,
            target_duration_seconds=requested_duration,
        )
        saved = self.repository.update_project(updated)
        from .runtime_foundation import OutputProfileService

        profiles = OutputProfileService(self.repository)
        current = profiles.ensure_for_project(project_id)
        next_resolution = delivery_resolution_label or current.delivery_resolution_label
        next_fps = target_fps if target_fps is not None else current.target_fps
        next_quality = quality_mode or current.quality_mode
        if (
            requested_aspect.value != current.aspect_ratio
            or float(requested_duration) != current.target_episode_duration_seconds
            or next_resolution != current.delivery_resolution_label
            or float(next_fps) != current.target_fps
            or next_quality != current.quality_mode
        ):
            profiles.create(
                project_id,
                aspect_ratio=requested_aspect.value,
                target_episode_duration_seconds=float(requested_duration),
                delivery_resolution_label=next_resolution,
                target_fps=float(next_fps),
                target_video_codec=current.target_video_codec,
                target_audio_sample_rate=current.target_audio_sample_rate,
                target_audio_channels=current.target_audio_channels,
                quality_mode=next_quality,
            )
        return saved

    def delete(
        self, project_id: str, *, confirmed: bool = False
    ) -> DeleteProjectResult:
        if not confirmed:
            raise ValueError("删除项目需要明确确认")
        project = self.get(project_id)
        if project is None:
            return DeleteProjectResult(deleted=False)

        # First check avoids doing an expensive backup for an obviously active
        # project. The same fail-closed predicate is rechecked while holding the
        # deletion transaction, after the backup has been proven importable.
        with self.repository.transaction() as connection:
            self._assert_deletable(connection, project_id)

        from .project_archive import ProjectArchiveService

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = (
            self.repository.paths.archived_projects
            / f"{project_id}-{timestamp}-{uuid4().hex[:8]}.aidrama"
        )
        archive_service = ProjectArchiveService(self.repository)
        archive_service.export_project(project_id, archive_path)

        # Move the directory out of the live project namespace while the DB
        # write lock is held. A failed transaction restores it. Preserve the
        # established browsable artifact-directory result for non-empty projects
        # in addition to the canonical recovery package.
        project_dir = self.repository.project_directory(project_id)
        staging = (
            self.repository.paths.archived_projects
            / f".{project_id}-{uuid4().hex}.pending-delete"
        )
        archived_artifacts_to = None
        staged = False
        keep_staging = False
        try:
            with self.repository.transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if exists is None:
                    raise ValueError("项目在删除前已不存在")
                self._assert_deletable(connection, project_id)
                if project_dir.exists():
                    if any(project_dir.iterdir()):
                        archived_artifacts_to = (
                            self.repository.paths.archived_projects
                            / f"{project_id}-{timestamp}-{uuid4().hex[:8]}"
                        )
                        staging = archived_artifacts_to
                        keep_staging = True
                    staging.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(project_dir), str(staging))
                    staged = True
                cursor = connection.execute(
                    "DELETE FROM projects WHERE id=?", (project_id,)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("项目删除事务未删除精确 project row")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("项目删除后 foreign_key_check 失败")
        except Exception:
            if staged and staging.exists() and not project_dir.exists():
                shutil.move(str(staging), str(project_dir))
            raise
        if staging.exists() and not keep_staging:
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                logger.warning(f"failed to remove post-delete staging directory: {exc}")
        logger.info(f"created verified project recovery archive: {archive_path}")
        return DeleteProjectResult(
            deleted=True,
            archived_artifacts_to=archived_artifacts_to,
            recovery_archive_to=archive_path,
        )

    @staticmethod
    def _assert_deletable(connection, project_id: str) -> None:
        if project_has_active_work(connection, project_id):
            raise ValueError("项目存在活动制作任务，取消或完成任务后才能删除")
