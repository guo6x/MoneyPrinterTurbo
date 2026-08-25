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
from aidrama_studio.services.adapters.production_adapter import ProductionRuntimeAdapter
from aidrama_studio.services.production_worker import ProductionWorker, ProductionWorkerError
from aidrama_studio.services.production_orchestrator import ProductionOrchestrator
from aidrama_studio.services.production import ProductionService
from aidrama_studio.services.production_execution import ProductionExecutionService
from aidrama_studio.services.production_runtime_resolver import ProductionRuntimeResolver
from aidrama_studio.services.security import sanitize_error
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class BackgroundRunnerError(RuntimeError):
    pass


class SingleInstanceGuard:
    """Per-data-root lock. It is deliberately process-local and recoverable."""

    def __init__(self, root: Path, lock_name: str = "runner.lock") -> None:
        if not lock_name or Path(lock_name).name != lock_name:
            raise ValueError("lock_name 必须是安全文件名")
        self.path = Path(root) / lock_name
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
        adapter_factory: Callable[..., ProductionRuntimeAdapter] | None = None,
        runtime_resolver: ProductionRuntimeResolver | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.worker_factory = worker_factory or (
            lambda: ProductionWorker(
                ProductionExecutionService(self.repository),
                poll_interval=5.0,
                max_polls=360,
            )
        )
        self.adapter_factory = adapter_factory
        self.runtime_resolver = runtime_resolver or ProductionRuntimeResolver()
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
                if task.state in {"SUBMISSION_UNCERTAIN", "RECONCILIATION_REQUIRED"}:
                    # Never automatically retry an uncertain paid side effect.
                    completed.append(task)
                    continue
                running = task.model_copy(update={"state": "RUNNING", "updated_at": _now()})
                self.repository.update_provider_task(running)
                try:
                    worker = self.worker_factory()
                    if task.execution_id is None:
                        job_id = str(task.request_summary.get("production_job_id") or "")
                        if not job_id:
                            raise BackgroundRunnerError("后台 job task 缺少 ProductionJob identity")
                        raw_plan_ids = task.request_summary.get("runtime_plan_ids_by_shot")
                        if not isinstance(raw_plan_ids, dict) or not raw_plan_ids:
                            raise BackgroundRunnerError("后台 job task 缺少冻结 RuntimePlan map")
                        plan_ids = {
                            str(shot_id): str(plan_id)
                            for shot_id, plan_id in raw_plan_ids.items()
                            if str(shot_id) and str(plan_id)
                        }
                        if not plan_ids:
                            raise BackgroundRunnerError("后台 job task 的 RuntimePlan map 无效")
                        production = ProductionService(self.repository)
                        orchestrator = ProductionOrchestrator(
                            production_service=production,
                            worker=worker,
                            adapter_resolver=lambda plan: self._resolve_adapter(task, plan),
                            runtime_plan_ids_by_shot=plan_ids,
                        )
                        result = orchestrator.run_job(
                            task.project_id,
                            job_id,
                            adapter_resolver=lambda plan: self._resolve_adapter(task, plan),
                            runtime_plan_ids_by_shot=plan_ids,
                        )
                        state = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED", "CANCELLED": "CANCELLED"}.get(result.status.value, "RUNNING")
                    else:
                        execution = self.repository.get_production_execution(task.execution_id)
                        if execution is None:
                            raise BackgroundRunnerError("后台 execution task 不存在")
                        plan = self.repository.get_runtime_plan(execution.runtime_plan_id) if execution.runtime_plan_id else None
                        adapter = self._resolve_adapter(task, plan)
                        result = worker.run(task.project_id, task.execution_id, adapter=adapter)
                        state = {
                            ProductionExecutionStatus.SUCCEEDED: "SUCCEEDED",
                            ProductionExecutionStatus.FAILED: "FAILED",
                            ProductionExecutionStatus.CANCELLED: "CANCELLED",
                        }.get(result.status, "RUNNING")
                    updated = running.model_copy(update={"state": state, "updated_at": _now()})
                except Exception as exc:
                    updated = running.model_copy(update={"state": "FAILED", "error_message": self._safe_error(exc), "updated_at": _now()})
                completed.append(self.repository.update_provider_task(updated))
        return completed

    def reconcile(self, project_id: str) -> list[ProviderTask]:
        """Mark durable tasks from already-terminal executions without rerun."""
        changed: list[ProviderTask] = []
        for task in self.repository.list_provider_tasks(project_id):
            if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                continue
            if task.execution_id is None:
                job_id = str(task.request_summary.get("production_job_id") or "")
                job = self.repository.get_production_job(job_id) if job_id else None
                if job is None or job.project_id != project_id:
                    continue
                terminal = {
                    "SUCCEEDED": "SUCCEEDED",
                    "FAILED": "FAILED",
                    "CANCELLED": "CANCELLED",
                }.get(job.status.value)
                if terminal:
                    changed.append(
                        self.repository.update_provider_task(
                            task.model_copy(update={"state": terminal, "updated_at": _now()})
                        )
                    )
                    continue
                executions = self.repository.list_production_executions(job.id)
                child_tasks = [
                    child
                    for child in self.repository.list_provider_tasks(project_id)
                    if child.execution_id in {item.id for item in executions}
                ]
                if any(
                    child.state in {"SUBMISSION_UNCERTAIN", "RECONCILIATION_REQUIRED"}
                    for child in child_tasks
                ):
                    state = "RECONCILIATION_REQUIRED"
                elif task.state == "RUNNING":
                    # The previous desktop process stopped while local work
                    # was active. Requeue the local owner; the orchestrator
                    # resumes an existing RUNNING execution/provider task and
                    # does not issue another paid submission.
                    state = "QUEUED"
                else:
                    continue
                changed.append(
                    self.repository.update_provider_task(
                        task.model_copy(update={"state": state, "updated_at": _now()})
                    )
                )
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

    def _resolve_adapter(self, task: ProviderTask, runtime_plan=None) -> ProductionRuntimeAdapter:
        if self.adapter_factory is None:
            return self.runtime_resolver.resolve(task, runtime_plan)
        # The original one-argument injection seam remains supported for
        # deterministic tests.  New factories receive the frozen RuntimePlan
        # as a second argument and can prove exact model/config restoration.
        try:
            import inspect

            parameters = inspect.signature(self.adapter_factory).parameters
            if len(parameters) >= 2:
                return self.adapter_factory(task, runtime_plan)
        except (TypeError, ValueError):
            pass
        return self.adapter_factory(task)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return sanitize_error(exc, max_length=4000)


__all__ = ["BackgroundProductionRunner", "BackgroundRunnerError", "SingleInstanceGuard"]
