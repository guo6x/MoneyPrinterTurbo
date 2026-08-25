"""Execution queue orchestration for production jobs.

This module deliberately stops at the queue/worker seam.  It records durable
execution state, immutable events, and artifact metadata, but never invokes a
renderer, FFmpeg, an AI provider, or the MoneyPrinterTurbo runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from uuid import uuid4

from aidrama_studio.domain import (
    ProductionAttempt,
    ProductionAttemptStatus,
    ProductionArtifact,
    ProductionEvent,
    ProductionEventType,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionJobStatus,
    ProductionShotStatus,
    ProductionInputSnapshot,
    ProviderTask,
    ReferenceBindingType,
)
from aidrama_studio.services.adapters import ProductionRuntimeAdapter, RuntimeEvent, RuntimeSubmission
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError
from .provider_profiles import ProviderProfileService
from .security import sanitize_error


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
        self._adapters: dict[str, ProductionRuntimeAdapter] = {}
        self.provider_profiles = ProviderProfileService(self.repository)

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
        worker_type: str = "mpt",
    ) -> ProductionExecution:
        """Validate and enqueue a job, creating a new immutable execution."""
        self._require_project(project_id)
        if not isinstance(worker_type, str) or not worker_type.strip():
            raise ProductionExecutionServiceError("worker_type 不能为空")
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            snapshot = self.create_input_snapshot(project_id, job.id)
        except ProductionServiceError as exc:
            raise ProductionExecutionServiceError(str(exc)) from exc
        except ProductionExecutionServiceError:
            raise

        existing = self.repository.list_production_executions(job.id)
        if any(item.status in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING) for item in existing):
            raise ProductionExecutionServiceError("该 ProductionJob 已有正在排队或运行的 execution")

        now = _now()
        execution = ProductionExecution(
            id=uuid4().hex,
            production_job_id=job.id,
            status=ProductionExecutionStatus.QUEUED,
            worker_type=worker_type.strip(),
            created_at=now,
            input_snapshot=snapshot,
        )
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.QUEUED,
            payload_json={},
            created_at=now,
        )
        return self.repository.enqueue_production_execution_atomic(
            execution, job_status=ProductionJobStatus.QUEUED, event=event
        )

    def enqueue_shot_execution(
        self,
        project_id: str,
        production_job_id: str,
        input_snapshot: ProductionInputSnapshot,
        worker_type: str = "mpt",
    ) -> ProductionExecution:
        """Queue one immutable, project-scoped shot execution.

        ``enqueue_job`` predates the multi-shot orchestrator and captures a
        whole-plan snapshot.  The orchestrator uses this narrower seam so
        every ``ProductionShot`` has exactly one durable execution containing
        only its own frozen shot parameters.
        """
        self._require_project(project_id)
        if not isinstance(worker_type, str) or not worker_type.strip():
            raise ProductionExecutionServiceError("worker_type 不能为空")
        if input_snapshot.project_id != project_id:
            raise ProductionExecutionServiceError("input snapshot 不属于该项目")
        if len(input_snapshot.shot_parameters) != 1:
            raise ProductionExecutionServiceError("shot execution 必须包含且只包含一个 shot")
        job = self.production_service.get_job(project_id, production_job_id)
        if job.status in (ProductionJobStatus.SUCCEEDED, ProductionJobStatus.CANCELLED):
            raise ProductionExecutionServiceError("ProductionJob 已结束，不能启动新的 shot execution")
        if input_snapshot.shot_plan_revision_id != job.shot_plan_revision_id:
            raise ProductionExecutionServiceError("input snapshot 的 Shot Plan revision 不匹配")
        shot_id = next(iter(input_snapshot.shot_parameters))
        if not any(shot.shot_id == shot_id for shot in self.repository.list_production_shots(job.id)):
            raise ProductionExecutionServiceError("input snapshot 的 shot 不属于该 ProductionJob")
        for existing in self.repository.list_production_executions(job.id):
            existing_shots = existing.input_snapshot.shot_parameters if existing.input_snapshot else {}
            if shot_id in existing_shots and existing.status in (
                ProductionExecutionStatus.QUEUED,
                ProductionExecutionStatus.RUNNING,
            ):
                raise ProductionExecutionServiceError("该 shot 已有正在排队或运行的 execution")
        now = _now()
        execution = ProductionExecution(
            id=uuid4().hex,
            production_job_id=job.id,
            status=ProductionExecutionStatus.QUEUED,
            worker_type=worker_type.strip(),
            created_at=now,
            input_snapshot=input_snapshot,
        )
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.QUEUED,
            payload_json={"shot_id": shot_id},
            created_at=now,
        )
        return self.repository.enqueue_production_execution_atomic(
            execution, job_status=ProductionJobStatus.QUEUED, event=event
        )

    def enqueue_shot_execution_with_attempt(
        self,
        project_id: str,
        production_job_id: str,
        input_snapshot: ProductionInputSnapshot,
        *,
        worker_type: str = "mpt",
        runtime_plan_id: str | None = None,
        generation_brief_id: str | None = None,
    ) -> tuple[ProductionExecution, object]:
        """Create the immutable execution and its first/retry attempt atomically."""
        self._require_project(project_id)
        if not isinstance(worker_type, str) or not worker_type.strip():
            raise ProductionExecutionServiceError("worker_type 不能为空")
        job = self.production_service.get_job(project_id, production_job_id)
        if job.status in (ProductionJobStatus.SUCCEEDED, ProductionJobStatus.CANCELLED):
            raise ProductionExecutionServiceError("ProductionJob 已结束，不能启动新的 shot execution")
        if input_snapshot.project_id != project_id or input_snapshot.shot_plan_revision_id != job.shot_plan_revision_id:
            raise ProductionExecutionServiceError("input snapshot provenance 不匹配")
        if len(input_snapshot.shot_parameters) != 1:
            raise ProductionExecutionServiceError("shot execution 必须包含且只包含一个 shot")
        shot_id = next(iter(input_snapshot.shot_parameters))
        shots = self.repository.list_production_shots(job.id)
        shot = next((item for item in shots if item.shot_id == shot_id), None)
        if shot is None:
            raise ProductionExecutionServiceError("input snapshot 的 shot 不属于该 ProductionJob")
        runtime_plan = None
        generation_brief = None
        if runtime_plan_id is not None:
            runtime_plan = self.repository.get_runtime_plan(runtime_plan_id)
            if (
                runtime_plan is None
                or runtime_plan.project_id != project_id
                or runtime_plan.production_job_id != job.id
            ):
                raise ProductionExecutionServiceError("RuntimePlan 不属于该 ProductionJob")
            generation_brief_id = generation_brief_id or runtime_plan.generation_brief_id
        if generation_brief_id is not None:
            generation_brief = next(
                (
                    item
                    for item in self.repository.list_generation_briefs(project_id, job.id)
                    if item.id == generation_brief_id
                ),
                None,
            )
            if generation_brief is None or generation_brief.shot_id != shot_id:
                raise ProductionExecutionServiceError("GenerationBrief 不属于该 shot")
        if runtime_plan is not None:
            if runtime_plan.generation_brief_id != generation_brief_id:
                raise ProductionExecutionServiceError("RuntimePlan 与 GenerationBrief provenance 不匹配")
            if generation_brief is None or runtime_plan.generation_brief_hash != generation_brief.sha256:
                raise ProductionExecutionServiceError("RuntimePlan 的 GenerationBrief hash 无效")
            if tuple(runtime_plan.reference_version_ids) != tuple(
                version_id
                for version_id in input_snapshot.reference_asset_versions.values()
                if version_id in set(runtime_plan.reference_version_ids)
            ):
                # Every frozen plan reference must be present in the execution
                # snapshot. Extra snapshot references are allowed because the
                # plan records the exact provider subset actually selected.
                missing = set(runtime_plan.reference_version_ids) - set(input_snapshot.reference_asset_versions.values())
                if missing:
                    raise ProductionExecutionServiceError("RuntimePlan 引用了 snapshot 之外的 reference version")
        for existing in self.repository.list_production_executions(job.id):
            existing_shots = existing.input_snapshot.shot_parameters if existing.input_snapshot else {}
            if shot_id in existing_shots and existing.status in (
                ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING
            ):
                raise ProductionExecutionServiceError("该 shot 已有正在排队或运行的 execution")
        attempts = self.repository.list_production_attempts(shot.id)
        if any(item.status is ProductionAttemptStatus.STARTED for item in attempts):
            raise ProductionExecutionServiceError("该 ProductionShot 已有正在运行的 attempt")
        now = _now()
        execution = ProductionExecution(
            id=uuid4().hex,
            production_job_id=job.id,
            status=ProductionExecutionStatus.QUEUED,
            worker_type=worker_type.strip(),
            created_at=now,
            input_snapshot=input_snapshot,
            runtime_plan_id=runtime_plan_id,
            generation_brief_id=generation_brief_id,
        )
        attempt = ProductionAttempt(
            id=uuid4().hex,
            production_shot_id=shot.id,
            attempt_number=(attempts[-1].attempt_number + 1 if attempts else 1),
            status=ProductionAttemptStatus.STARTED,
            runtime_adapter=worker_type.strip(),
            input_snapshot_json=input_snapshot.to_json_dict(),
            created_at=now,
        )
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.QUEUED,
            payload_json={"shot_id": shot_id, "attempt_id": attempt.id},
            created_at=now,
        )
        created = self.repository.enqueue_production_execution_atomic(
            execution,
            job_status=ProductionJobStatus.QUEUED,
            event=event,
            attempt=attempt,
            shot_status=ProductionShotStatus.PENDING,
        )
        return created, attempt

    create_shot_execution = enqueue_shot_execution

    def start_execution(
        self,
        project_id: str,
        execution_id: str,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        self._require_status(execution, ProductionExecutionStatus.QUEUED, "只有 QUEUED execution 可以启动")
        if execution.input_snapshot is None:
            raise ProductionExecutionServiceError("execution 缺少 immutable input snapshot")
        now = _now()
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.STARTED,
            payload_json=payload_json or {},
            created_at=now,
        )
        result = self.repository.transition_production_execution_atomic(
            execution.id,
            expected_status=ProductionExecutionStatus.QUEUED,
            status=ProductionExecutionStatus.RUNNING,
            started_at=now,
            job_status=ProductionJobStatus.RUNNING,
            event=event,
        )
        return result

    def create_input_snapshot(self, project_id: str, production_job_id: str) -> ProductionInputSnapshot:
        """Capture the approved Story → Script → Shot Plan input graph once."""
        self._require_project(project_id)
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(project_id, job.shot_plan_revision_id)
        except ProductionServiceError as exc:
            raise ProductionExecutionServiceError(str(exc)) from exc
        if not readiness["ready"]:
            reasons = "; ".join(str(reason) for reason in readiness["blocked_reasons"])
            raise ProductionExecutionServiceError(f"ProductionJob 尚未 READY: {reasons}")

        stories = self.repository.list_story_revisions(project_id)
        scripts = self.repository.list_script_revisions(project_id)
        story = next((item for item in stories if item["status"].value == "APPROVED"), None)
        script = next((item for item in scripts if item["status"].value == "APPROVED"), None)
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if story is None or script is None or plan is None:
            raise ProductionExecutionServiceError("无法创建完整的 production input snapshot")

        reference_service = self.production_service.reference_service
        reference_versions: dict[str, str] = {}
        required_targets = (
            (ReferenceBindingType.CHARACTER, readiness["required_characters"]),
            (ReferenceBindingType.LOCATION, readiness["required_locations"]),
        )
        for binding_type, target_ids in required_targets:
            for target_id in target_ids:
                version_id = self._current_bound_version(reference_service, project_id, binding_type, target_id)
                if version_id is None:
                    raise ProductionExecutionServiceError(f"缺少 {binding_type.value} reference: {target_id}")
                reference_versions[f"{binding_type.value}:{target_id}"] = version_id
        for shot in plan["content"].shots:
            version_id = self._current_bound_version(reference_service, project_id, ReferenceBindingType.SHOT, shot.id)
            if version_id is not None:
                reference_versions[f"{ReferenceBindingType.SHOT.value}:{shot.id}"] = version_id

        return ProductionInputSnapshot(
            project_id=project_id,
            story_revision_id=story["id"],
            script_revision_id=script["id"],
            shot_plan_revision_id=plan["id"],
            reference_asset_versions=reference_versions,
            shot_parameters={shot.id: shot.model_dump(mode="json") for shot in plan["content"].shots},
        )

    build_input_snapshot = create_input_snapshot

    @staticmethod
    def _current_bound_version(reference_service, project_id: str, binding_type: ReferenceBindingType, binding_id: str) -> str | None:
        for asset in reference_service.list_assets(project_id):
            if asset.current_version_id is None:
                continue
            current = reference_service.repository.get_reference_asset_version(asset.current_version_id)
            if current is None:
                continue
            if any(
                binding.binding_type is binding_type
                and binding.binding_id == binding_id
                and binding.asset_version_id == current.id
                for binding in reference_service.list_bindings(project_id, current.id)
            ):
                return current.id
        return None

    def submit_execution(
        self,
        project_id: str,
        execution_id: str,
        adapter: ProductionRuntimeAdapter,
        *,
        input_snapshot: ProductionInputSnapshot | None = None,
    ) -> ProductionExecution:
        """Validate and submit an immutable snapshot to a runtime adapter."""
        execution, _ = self._get_execution(project_id, execution_id)
        if execution.status is not ProductionExecutionStatus.QUEUED:
            raise ProductionExecutionServiceError("只有 QUEUED execution 可以提交到 runtime")
        runtime_snapshot = input_snapshot or execution.input_snapshot
        if runtime_snapshot is None:
            raise ProductionExecutionServiceError("execution 缺少 immutable input snapshot")
        task = self._provider_task_intent(project_id, execution, adapter, runtime_snapshot)
        # A durable provider identity is the recovery boundary.  If the
        # process was restarted after a successful provider POST, never issue
        # another paid POST; resume polling the original task instead.
        if task.state in {"SUBMISSION_UNCERTAIN", "RECONCILIATION_REQUIRED"} and not task.provider_task_id:
            raise ProductionExecutionServiceError("provider submission 状态不确定，必须先 reconciliation")
        self._adapters[execution.id] = adapter
        if task.provider_task_id:
            runtime_reference = task.provider_task_id
            submission_metadata = dict(task.metadata)
            if execution.status is ProductionExecutionStatus.QUEUED:
                return self.start_execution(project_id, execution.id, {"adapter": getattr(adapter, "name", adapter.__class__.__name__), "runtime_reference": runtime_reference, "provider_metadata": submission_metadata})
            return execution
        try:
            accepted = adapter.validate(runtime_snapshot)
            if accepted is False:
                self._update_provider_task(task, state="FAILED", error_message="runtime adapter 拒绝 input snapshot")
                self.fail_execution(project_id, execution.id, error_message="runtime adapter 拒绝 input snapshot")
                raise ProductionExecutionServiceError("runtime adapter 拒绝 input snapshot")
            self._update_provider_task(task, state="SUBMITTING")
            submission = adapter.submit(runtime_snapshot)
            runtime_reference = self._submission_reference(submission)
            submission_metadata = self._submission_metadata(submission)
            task = self._update_provider_task(task, state="PROVIDER_ACCEPTED", provider_task_id=runtime_reference, metadata=submission_metadata, submitted_at=_now())
        except ProductionExecutionServiceError:
            raise
        except Exception as exc:
            # The adapter may have accepted the request before the transport
            # raised.  Persist uncertainty and require explicit reconciliation
            # instead of turning an unknown paid request into an auto-retry.
            if bool(getattr(adapter, "submission_uncertain_on_error", False)):
                self._update_provider_task(task, state="SUBMISSION_UNCERTAIN", error_message=self._safe_error(exc))
                raise ProductionExecutionServiceError(f"runtime submit 状态不确定: {type(exc).__name__}") from exc
            self._update_provider_task(task, state="FAILED", error_message=self._safe_error(exc))
            self.fail_execution(project_id, execution.id, error_message=self._safe_error(exc))
            raise ProductionExecutionServiceError(f"runtime submit 失败: {type(exc).__name__}") from exc
        start_payload = {
            "adapter": getattr(adapter, "name", adapter.__class__.__name__),
            "runtime_reference": runtime_reference,
        }
        # Provider adapters may return non-secret request trace data (model,
        # prompt, exact frozen reference version, and provider task id).  Keep
        # it in the immutable STARTED event so a worker restart and later QC
        # inspection retain the exact request without changing the snapshot
        # or persisting credentials.
        if submission_metadata:
            start_payload["provider_metadata"] = submission_metadata
        return self.start_execution(project_id, execution.id, start_payload)

    def _provider_task_intent(self, project_id: str, execution: ProductionExecution, adapter: ProductionRuntimeAdapter, snapshot: ProductionInputSnapshot) -> ProviderTask:
        provider_id = str(getattr(adapter, "provider_id", getattr(adapter, "name", adapter.__class__.__name__)))
        config = getattr(adapter, "config", None)
        model_id = str(getattr(config, "model", getattr(adapter, "model_id", "runtime")))
        plan_id = execution.runtime_plan_id or "snapshot"
        plan_hash = ""
        if execution.runtime_plan_id:
            plan = self.repository.get_runtime_plan(execution.runtime_plan_id)
            if plan is None or plan.project_id != project_id or plan.production_job_id != execution.production_job_id:
                raise ProductionExecutionServiceError("execution 的 RuntimePlan provenance 无效")
            if execution.generation_brief_id != plan.generation_brief_id:
                raise ProductionExecutionServiceError("execution 的 RuntimePlan/GenerationBrief 不匹配")
            if model_id != plan.model_id:
                raise ProductionExecutionServiceError("runtime adapter model 与冻结 RuntimePlan 不匹配")
            provider_id = plan.provider_id
            model_id = plan.model_id
            plan_hash = plan.plan_hash
        key_payload = {"project_id": project_id, "execution_id": execution.id, "provider_id": provider_id, "model_id": model_id, "plan_id": plan_id, "plan_hash": plan_hash, "snapshot": snapshot.to_json_dict()}
        key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        existing = self.repository.get_provider_task_by_idempotency(project_id, key)
        if existing is not None:
            return existing
        now = _now()
        return self.repository.create_provider_task(ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=execution.id,
            capability="VIDEO_GENERATIVE", provider_id=provider_id, model_id=model_id,
            idempotency_key=key, state="PENDING_SUBMISSION",
            request_summary={
                "execution_id": execution.id,
                "snapshot_hash": hashlib.sha256(json.dumps(snapshot.to_json_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                "runtime_plan_id": execution.runtime_plan_id,
                "runtime_plan_hash": plan_hash or None,
            },
            created_at=now, updated_at=now,
        ))

    def _update_provider_task(self, task: ProviderTask, *, state: str | None = None, provider_task_id: str | None = None, metadata: Mapping[str, object] | None = None, submitted_at: str | None = None, error_message: str | None = None) -> ProviderTask:
        updated = task.model_copy(update={
            "state": state or task.state,
            "provider_task_id": provider_task_id if provider_task_id is not None else task.provider_task_id,
            "metadata": dict(task.metadata) | dict(metadata or {}),
            "submitted_at": submitted_at if submitted_at is not None else task.submitted_at,
            "error_message": error_message,
            "updated_at": _now(),
        })
        return self.repository.update_provider_task(updated)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return sanitize_error(exc, max_length=1000)

    @staticmethod
    def _submission_reference(submission: RuntimeSubmission | str | Mapping[str, object]) -> str:
        if isinstance(submission, RuntimeSubmission):
            reference = submission.runtime_reference
        elif isinstance(submission, str):
            reference = submission
        elif isinstance(submission, Mapping):
            reference = submission.get("runtime_reference") or submission.get("reference")
        else:
            reference = getattr(submission, "runtime_reference", None) or getattr(submission, "reference", None)
        if not isinstance(reference, str) or not reference.strip():
            raise ProductionExecutionServiceError("runtime submit 未返回有效 reference")
        return reference.strip()

    @staticmethod
    def _submission_metadata(submission: RuntimeSubmission | str | Mapping[str, object]) -> dict[str, object]:
        """Extract JSON-safe, non-secret provider trace metadata.

        ``RuntimeSubmission.metadata`` is the adapter boundary for request
        traceability.  It is intentionally copied as a separate event field;
        the provider adapter owns the rule that secrets never enter it.
        Generic mappings are accepted for compatibility with older adapters.
        """
        if isinstance(submission, RuntimeSubmission):
            raw = submission.metadata
        elif isinstance(submission, Mapping):
            raw = submission.get("metadata") or submission.get("provider_metadata") or {}
        else:
            raw = getattr(submission, "metadata", {})
        if not isinstance(raw, Mapping):
            return {}
        return {str(key): value for key, value in raw.items() if str(key).lower() not in {"api_key", "apikey", "authorization", "token", "secret"}}

    def handle_runtime_event(
        self,
        project_id: str,
        execution_id: str,
        event: RuntimeEvent | Mapping[str, object],
    ):
        execution, _ = self._get_execution(project_id, execution_id)
        if isinstance(event, RuntimeEvent):
            event_type, payload = event.event_type, dict(event.payload)
        elif isinstance(event, Mapping):
            event_type = event.get("event_type") or event.get("type")
            raw_payload = event.get("payload_json") or event.get("payload") or {}
            payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        else:
            raise ProductionExecutionServiceError("runtime event 格式无效")
        if not isinstance(event_type, str):
            raise ProductionExecutionServiceError("runtime event 缺少 event_type")
        normalized = event_type.upper()
        if normalized == ProductionEventType.PROGRESS.value:
            return self.update_progress(project_id, execution.id, payload_json=payload)
        if normalized == ProductionEventType.SHOT_COMPLETED.value:
            return self.append_event(project_id, execution.id, ProductionEventType.SHOT_COMPLETED, payload)
        if normalized == ProductionEventType.FAILED.value:
            return self.fail_execution(project_id, execution.id, payload.get("error"), payload)
        if normalized == ProductionEventType.CANCELLED.value:
            return self.cancel_execution(project_id, execution.id, payload.get("reason"), _notify_runtime=False)
        if normalized == ProductionEventType.FINISHED.value:
            for artifact in payload.pop("artifacts", []) or []:
                if not isinstance(artifact, Mapping) or not artifact.get("path"):
                    raise ProductionExecutionServiceError("runtime artifact metadata 缺少 path")
                self.record_artifact(
                    project_id,
                    execution.id,
                    str(artifact.get("artifact_type") or artifact.get("type") or "runtime-artifact"),
                    str(artifact["path"]),
                    dict(artifact.get("metadata_json") or artifact.get("metadata") or {}),
                )
            return self.complete_execution(project_id, execution.id, payload)
        if normalized == ProductionEventType.STARTED.value and execution.status is ProductionExecutionStatus.QUEUED:
            return self.start_execution(project_id, execution.id, payload)
        raise ProductionExecutionServiceError(f"不支持的 runtime event: {event_type}")

    def handle_runtime_events(
        self,
        project_id: str,
        execution_id: str,
        adapter: ProductionRuntimeAdapter | None = None,
        events: Iterable[RuntimeEvent | Mapping[str, object]] | None = None,
    ) -> list[object]:
        execution, _ = self._get_execution(project_id, execution_id)
        runtime_adapter = adapter or self._adapters.get(execution.id)
        if events is None:
            if runtime_adapter is None or not hasattr(runtime_adapter, "drain_events"):
                raise ProductionExecutionServiceError("没有可读取 runtime events 的 adapter")
            runtime_reference = self._runtime_reference(execution.id)
            events = runtime_adapter.drain_events(runtime_reference)
        return [self.handle_runtime_event(project_id, execution.id, event) for event in events]

    def _runtime_reference(self, execution_id: str) -> str:
        for event in reversed(self.repository.list_production_events(execution_id)):
            if event.event_type is ProductionEventType.STARTED:
                reference = event.payload_json.get("runtime_reference")
                if isinstance(reference, str) and reference:
                    return reference
        raise ProductionExecutionServiceError("execution 缺少 runtime reference")

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
        *,
        _notify_runtime: bool = True,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        if execution.status not in (ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING):
            raise ProductionExecutionServiceError("只有 QUEUED/RUNNING execution 可以取消")
        if _notify_runtime and execution.id in self._adapters and execution.status is ProductionExecutionStatus.RUNNING:
            try:
                self._adapters[execution.id].cancel(self._runtime_reference(execution.id))
            except Exception as exc:
                raise ProductionExecutionServiceError(f"runtime cancel 失败: {exc}") from exc
        now = _now()
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.CANCELLED,
            payload_json={"reason": reason or ""},
            created_at=now,
        )
        result = self.repository.transition_production_execution_atomic(
            execution.id,
            expected_status=execution.status,
            status=ProductionExecutionStatus.CANCELLED,
            finished_at=now,
            job_status=ProductionJobStatus.CANCELLED,
            event=event,
        )
        self._sync_provider_task_terminal(project_id, execution.id, "CANCELLED")
        return result

    def complete_execution(
        self,
        project_id: str,
        execution_id: str,
        payload_json: dict[str, object] | None = None,
    ) -> ProductionExecution:
        execution, job = self._get_execution(project_id, execution_id)
        self._require_status(execution, ProductionExecutionStatus.RUNNING, "只有 RUNNING execution 可以完成")
        now = _now()
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.FINISHED,
            payload_json=payload_json or {},
            created_at=now,
        )
        result = self.repository.transition_production_execution_atomic(
            execution.id,
            expected_status=ProductionExecutionStatus.RUNNING,
            status=ProductionExecutionStatus.SUCCEEDED,
            finished_at=now,
            job_status=ProductionJobStatus.SUCCEEDED,
            event=event,
        )
        self._sync_provider_task_terminal(project_id, execution.id, "SUCCEEDED")
        return result

    def _sync_provider_task_terminal(self, project_id: str, execution_id: str, state: str) -> None:
        for task in self.repository.list_provider_tasks(project_id):
            if task.execution_id == execution_id and task.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                self._update_provider_task(task, state=state)

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
        payload = dict(payload_json or {})
        if error_message:
            payload["error"] = error_message
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.FAILED,
            payload_json=payload,
            created_at=now,
        )
        result = self.repository.transition_production_execution_atomic(
            execution.id,
            expected_status=execution.status,
            status=ProductionExecutionStatus.FAILED,
            finished_at=now,
            job_status=ProductionJobStatus.FAILED,
            event=event,
        )
        self._sync_provider_task_terminal(project_id, execution.id, "FAILED")
        return result

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
