"""Durable, non-blocking production runner boundary.

The Streamlit process only enqueues work. A caller may run this service in a
dedicated process/thread or a packaged desktop worker; all state needed for a
restart lives in SQLite and the existing ProductionWorker.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from aidrama_studio.domain import ProductionExecutionStatus, ProviderTask
from aidrama_studio.services.production_worker import ProductionWorker, ProductionWorkerError
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class BackgroundRunnerError(RuntimeError):
    pass


class SingleInstanceGuard:
    """Per-data-root lock. It is deliberately process-local and recoverable."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "runner.lock"
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise BackgroundRunnerError("该数据目录已有 Production runner 在运行") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class BackgroundProductionRunner:
    """Queue, execute and reconcile durable production work."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        worker_factory: Callable[[], ProductionWorker] | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.worker_factory = worker_factory or (lambda: ProductionWorker())
        self.guard = SingleInstanceGuard(self.repository.paths.root)

    def enqueue(self, project_id: str, execution_id: str) -> ProviderTask:
        execution = self.repository.get_production_execution(execution_id)
        if execution is None:
            raise BackgroundRunnerError("ProductionExecution 不存在")
        job = self.repository.get_production_job(execution.production_job_id)
        if job is None or job.project_id != project_id:
            raise BackgroundRunnerError("ProductionExecution 不属于该项目")
        if execution.status not in {ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING}:
            raise BackgroundRunnerError("只有 QUEUED/RUNNING execution 可以入后台队列")
        key = f"production:{execution.id}"
        existing = self.repository.get_provider_task_by_idempotency(project_id, key)
        if existing is not None:
            return existing
        now = _now()
        return self.repository.create_provider_task(
            ProviderTask(
                id=uuid4().hex, project_id=project_id, execution_id=execution.id,
                capability="VIDEO_GENERATIVE", provider_id="RUNTIME_BOUNDARY", model_id="PINNED",
                idempotency_key=key, state="QUEUED", request_summary={"execution_id": execution.id},
                created_at=now, updated_at=now,
            )
        )

    def run_once(self, project_id: str | None = None) -> list[ProviderTask]:
        """Process currently queued tasks without blocking a Streamlit request."""
        tasks = self.repository.list_provider_tasks(project_id or "", state="QUEUED") if project_id else self._all_queued()
        completed: list[ProviderTask] = []
        with self.guard:
            for task in tasks:
                if task.execution_id is None:
                    continue
                running = task.model_copy(update={"state": "RUNNING", "updated_at": _now()})
                self.repository.update_provider_task(running)
                try:
                    result = self.worker_factory().run(task.project_id, task.execution_id)
                    state = {
                        ProductionExecutionStatus.SUCCEEDED: "SUCCEEDED",
                        ProductionExecutionStatus.FAILED: "FAILED",
                        ProductionExecutionStatus.CANCELLED: "CANCELLED",
                    }.get(result.status, "RUNNING")
                    updated = running.model_copy(update={"state": state, "updated_at": _now()})
                except (ProductionWorkerError, Exception) as exc:
                    updated = running.model_copy(update={"state": "FAILED", "error_message": self._safe_error(exc), "updated_at": _now()})
                completed.append(self.repository.update_provider_task(updated))
        return completed

    def reconcile(self, project_id: str) -> list[ProviderTask]:
        """Mark durable tasks from already-terminal executions without rerun."""
        changed: list[ProviderTask] = []
        for task in self.repository.list_provider_tasks(project_id):
            if task.execution_id is None or task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                continue
            execution = self.repository.get_production_execution(task.execution_id)
            if execution is None:
                continue
            state = {ProductionExecutionStatus.SUCCEEDED: "SUCCEEDED", ProductionExecutionStatus.FAILED: "FAILED", ProductionExecutionStatus.CANCELLED: "CANCELLED"}.get(execution.status)
            if state:
                changed.append(self.repository.update_provider_task(task.model_copy(update={"state": state, "updated_at": _now()})))
        return changed

    def cancel(self, project_id: str, execution_id: str, *, reason: str = "user") -> ProviderTask:
        task = next((item for item in self.repository.list_provider_tasks(project_id) if item.execution_id == execution_id), None)
        if task is None:
            raise BackgroundRunnerError("后台任务不存在")
        if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        try:
            self.worker_factory().cancel(project_id, execution_id, reason)
        except Exception as exc:
            # Cancellation is truthful: retain a recoverable task and expose
            # the error instead of pretending a remote task stopped.
            return self.repository.update_provider_task(task.model_copy(update={"error_message": self._safe_error(exc), "updated_at": _now()}))
        return self.repository.update_provider_task(task.model_copy(update={"state": "CANCELLED", "updated_at": _now()}))

    def pause(self, project_id: str, execution_id: str) -> ProviderTask:
        task = next((item for item in self.repository.list_provider_tasks(project_id) if item.execution_id == execution_id), None)
        if task is None:
            raise BackgroundRunnerError("后台任务不存在")
        if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        return self.repository.update_provider_task(task.model_copy(update={"state": "PAUSED", "updated_at": _now()}))

    def _all_queued(self) -> list[ProviderTask]:
        result: list[ProviderTask] = []
        for project in self.repository.list_projects():
            result.extend(self.repository.list_provider_tasks(project.id, state="QUEUED"))
        return result

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc).replace("\\", "/")[:4000]


__all__ = ["BackgroundProductionRunner", "BackgroundRunnerError", "SingleInstanceGuard"]
