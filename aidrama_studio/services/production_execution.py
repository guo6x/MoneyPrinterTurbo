"""Execution queue orchestration for production jobs.

This module deliberately stops at the queue/worker seam.  It records durable
execution state, immutable events, and artifact metadata, but never invokes a
renderer, FFmpeg, an AI provider, or the MoneyPrinterTurbo runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from uuid import uuid4

from aidrama_studio.domain import (
    ProductionArtifact,
    ProductionEvent,
    ProductionEventType,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionJobStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionExecutionServiceError(RuntimeError):
    """Raised when an execution operation violates its lifecycle boundary."""


class ProductionExecutionService:
    """Project-scoped lifecycle boundary for queued production executions."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
    ) -> None:
        if production_service is not None:
            self.production_service = production_service
            self.repository = production_service.repository
        else:
            self.repository = repository or ProjectRepository()
            self.production_service = ProductionService(self.repository)

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProductionExecutionServiceError(f"项目不存在: {project_id}")
        return project

    def _get_execution(self, project_id: str, execution_id: str):
        self._require_project(project_id)
        execution = self.repository.get_production_execution(execution_id)
        if execution is None:
            raise ProductionExecutionServiceError("ProductionExecution 不存在")
        job = self.repository.get_production_job(execution.production_job_id)
        if job is None or job.project_id != project_id:
            raise ProductionExecutionServiceError("ProductionExecution 不属于该项目")
        return execution, job

    def get_execution(self, project_id: str, execution_id: str) -> ProductionExecution:
        return self._get_execution(project_id, execution_id)[0]

    def list_executions(self, project_id: str, production_job_id: str) -> list[ProductionExecution]:
        self._require_project(project_id)
        job = self.repository.get_production_job(production_job_id)
        if job is None or job.project_id != project_id:
            raise ProductionExecutionServiceError("ProductionJob 不属于该项目")
        return self.repository.list_production_executions(production_job_id)

    def enqueue_job(
        self,
        project_id: str,
        production_job_id: str,
        worker_type: str = "placeholder",
    ) -> ProductionExecution:
        """Validate and enqueue a job, creating a new immutable execution."""
        self._require_project(project_id)
        if not isinstance(worker_type, str) or not worker_type.strip():
            raise ProductionExecutionServiceError("worker_type 不能为空")
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(
                project_id, job.shot_plan_revision_id
            )
        except ProductionServiceError as exc:
            raise ProductionExecutionServiceError(str(exc)) from exc
        if not readiness["ready"]:
            reasons = "; ".join(str(reason) for reason in readiness["blocked_reasons"])
            raise ProductionExecutionServiceError(f"ProductionJob 尚未 READY: {reasons}")

        existing = self.repository.list_production_executions(job.id)
        if any(item.status in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING) for item in existing):
            raise ProductionExecutionServiceError("该 ProductionJob 已有正在排队或运行的 execution")

        now = _now()
        execution = self.repository.create_production_execution(
            ProductionExecution(
                id=uuid4().hex,
                production_job_id=job.id,
                status=ProductionExecutionStatus.QUEUED,
                worker_type=worker_type.strip(),
                created_at=now,
            )
        )
        self.repository.update_production_job_status(job.id, ProductionJobStatus.QUEUED, updated_at=now)
        self._append_event(project_id, execution, ProductionEventType.QUEUED, {})
        return execution

    def start_execution(self, project_id: str, execution_id: str) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        self._require_status(execution, ProductionExecutionStatus.QUEUED, "只有 QUEUED execution 可以启动")
        now = _now()
        execution = self.repository.update_production_execution(
            execution.id,
            status=ProductionExecutionStatus.RUNNING,
            started_at=now,
        )
        self.repository.update_production_job_status(job.id, ProductionJobStatus.RUNNING, updated_at=now)
        self._append_event(project_id, execution, ProductionEventType.STARTED, {})
        return execution

    def append_event(
        self,
        project_id: str,
        execution_id: str,
        event_type: ProductionEventType | str,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionEvent:
        execution, _ = self._get_execution(project_id, execution_id)
        try:
            normalized = ProductionEventType(event_type)
        except (TypeError, ValueError) as exc:
            raise ProductionExecutionServiceError("未知的 ProductionEvent 类型") from exc
        return self._append_event(project_id, execution, normalized, payload_json or {})

    def _append_event(
        self,
        project_id: str,
        execution: ProductionExecution,
        event_type: ProductionEventType,
        payload_json: dict[str, object],
    ) -> ProductionEvent:
        allowed_status = {
            ProductionEventType.QUEUED: ProductionExecutionStatus.QUEUED,
            ProductionEventType.STARTED: ProductionExecutionStatus.RUNNING,
            ProductionEventType.PROGRESS: ProductionExecutionStatus.RUNNING,
            ProductionEventType.SHOT_COMPLETED: ProductionExecutionStatus.RUNNING,
            ProductionEventType.FAILED: ProductionExecutionStatus.FAILED,
            ProductionEventType.CANCELLED: ProductionExecutionStatus.CANCELLED,
            ProductionEventType.FINISHED: ProductionExecutionStatus.SUCCEEDED,
        }[event_type]
        if execution.status is not allowed_status:
            raise ProductionExecutionServiceError(
                f"{event_type.value} event 与 execution 状态 {execution.status.value} 不匹配"
            )
        current_events = self.repository.list_production_events(execution.id)
        if event_type in (ProductionEventType.QUEUED, ProductionEventType.STARTED, ProductionEventType.FAILED, ProductionEventType.CANCELLED, ProductionEventType.FINISHED) and any(
            event.event_type is event_type for event in current_events
        ):
            raise ProductionExecutionServiceError("execution event history 不可重复写入")
        return self.repository.create_production_event(
            ProductionEvent(
                id=uuid4().hex,
                execution_id=execution.id,
                event_type=event_type,
                payload_json=payload_json,
                created_at=_now(),
            )
        )

    def update_progress(
        self,
        project_id: str,
        execution_id: str,
        progress: int | float | None = None,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionEvent:
        execution, _ = self._get_execution(project_id, execution_id)
        self._require_status(execution, ProductionExecutionStatus.RUNNING, "只有 RUNNING execution 可以更新进度")
        payload = dict(payload_json or {})
        if progress is None:
            progress = payload.get("progress")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 100:
            raise ProductionExecutionServiceError("progress 必须在 0 到 100 之间")
        payload["progress"] = progress
        return self._append_event(project_id, execution, ProductionEventType.PROGRESS, payload)

    def cancel_execution(
        self,
        project_id: str,
        execution_id: str,
        reason: str | None = None,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        if execution.status not in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING):
            raise ProductionExecutionServiceError("只有 QUEUED/RUNNING execution 可以取消")
        now = _now()
        execution = self.repository.update_production_execution(
            execution.id, status=ProductionExecutionStatus.CANCELLED, finished_at=now
        )
        self.repository.update_production_job_status(job.id, ProductionJobStatus.CANCELLED, updated_at=now)
        self._append_event(project_id, execution, ProductionEventType.CANCELLED, {"reason": reason or ""})
        return execution

    def complete_execution(
        self,
        project_id: str,
        execution_id: str,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        self._require_status(execution, ProductionExecutionStatus.RUNNING, "只有 RUNNING execution 可以完成")
        now = _now()
        execution = self.repository.update_production_execution(
            execution.id, status=ProductionExecutionStatus.SUCCEEDED, finished_at=now
        )
        self.repository.update_production_job_status(job.id, ProductionJobStatus.SUCCEEDED, updated_at=now)
        self._append_event(project_id, execution, ProductionEventType.FINISHED, payload_json or {})
        return execution

    def fail_execution(
        self,
        project_id: str,
        execution_id: str,
        error_message: str | None = None,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        if execution.status not in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING):
            raise ProductionExecutionServiceError("只有 QUEUED/RUNNING execution 可以失败")
        now = _now()
        execution = self.repository.update_production_execution(
            execution.id, status=ProductionExecutionStatus.FAILED, finished_at=now
        )
        self.repository.update_production_job_status(job.id, ProductionJobStatus.FAILED, updated_at=now)
        payload = dict(payload_json or {})
        if error_message:
            payload["error"] = error_message
        self._append_event(project_id, execution, ProductionEventType.FAILED, payload)
        return execution

    def list_events(self, project_id: str, execution_id: str) -> list[ProductionEvent]:
        execution, _ = self._get_execution(project_id, execution_id)
        return self.repository.list_production_events(execution.id)

    def record_artifact(
        self,
        project_id: str,
        execution_id: str,
        artifact_type: str,
        path: str,
        metadata_json: dict[str, object] | None = None,
    ) -> ProductionArtifact:
        execution, _ = self._get_execution(project_id, execution_id)
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ProductionExecutionServiceError("artifact_type 不能为空")
        safe_path = self._validate_artifact_path(path)
        if any(item.path == safe_path for item in self.repository.list_production_artifacts(execution.id)):
            raise ProductionExecutionServiceError("artifact path 不可覆盖")
        return self.repository.create_production_artifact(
            ProductionArtifact(
                id=uuid4().hex,
                execution_id=execution.id,
                artifact_type=artifact_type.strip(),
                path=safe_path,
                metadata_json=metadata_json or {},
                created_at=_now(),
            )
        )

    def list_artifacts(self, project_id: str, execution_id: str) -> list[ProductionArtifact]:
        execution, _ = self._get_execution(project_id, execution_id)
        return self.repository.list_production_artifacts(execution.id)

    @staticmethod
    def _require_status(
        execution: ProductionExecution,
        expected: ProductionExecutionStatus,
        message: str,
    ) -> None:
        if execution.status is not expected:
            raise ProductionExecutionServiceError(message)

    @staticmethod
    def _validate_artifact_path(path: str) -> str:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ProductionExecutionServiceError("artifact path 无效")
        normalized = path.strip().replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(path).is_absolute() or PureWindowsPath(path).drive:
            raise ProductionExecutionServiceError("artifact path 必须是项目相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ProductionExecutionServiceError("artifact path 不能越过项目目录")
        return "/".join(parts)
