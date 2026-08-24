"""Single-shot production execution worker.

The worker owns orchestration, not rendering. It submits one immutable shot
snapshot to the injected runtime adapter, drains runtime events, persists
returned artifacts, and closes the durable execution through
``ProductionExecutionService``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from aidrama_studio.domain import (
    ProductionEventType,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
)
from aidrama_studio.services.adapters import ProductionRuntimeAdapter, RuntimeEvent

from .production_artifact_storage import (
    ProductionArtifactStorageService,
)
from .production_execution import ProductionExecutionService, ProductionExecutionServiceError


class ProductionWorkerError(RuntimeError):
    """Raised for invalid worker setup or an unrecoverable orchestration error."""


class ProductionWorker:
    """Execute exactly one shot from a queued ProductionExecution."""

    worker_type = "mpt"

    def __init__(
        self,
        execution_service: ProductionExecutionService | None = None,
        adapter: ProductionRuntimeAdapter | None = None,
        *,
        runtime_adapter: ProductionRuntimeAdapter | None = None,
        artifact_storage: ProductionArtifactStorageService | None = None,
        artifact_root=None,
        poll_interval: float = 0.0,
        max_polls: int = 100,
    ) -> None:
        self.execution_service = execution_service or ProductionExecutionService()
        self.adapter = adapter or runtime_adapter
        self.poll_interval = max(0.0, float(poll_interval))
        self.max_polls = max(1, int(max_polls))
        self.artifact_storage = artifact_storage or ProductionArtifactStorageService(
            self.execution_service.repository,
            projects_root=artifact_root,
        )

    def run(
        self,
        project_id_or_execution: str | ProductionExecution | object,
        execution_id: str | None = None,
        *,
        adapter: ProductionRuntimeAdapter | None = None,
    ) -> ProductionExecution:
        """Run a queued execution and return its terminal or current state.

        ``run(project_id, execution_id)`` is the canonical form. Passing an
        execution object is intentionally not implicit: workers must always be
        project-scoped before touching runtime output paths.
        """
        project_id, resolved_execution_id = self._resolve_identity(project_id_or_execution, execution_id)
        runtime_adapter = adapter or self.adapter
        if runtime_adapter is None:
            raise ProductionWorkerError("ProductionWorker 需要一个 ProductionRuntimeAdapter")
        try:
            execution = self.execution_service.get_execution(project_id, resolved_execution_id)
        except ProductionExecutionServiceError as exc:
            raise ProductionWorkerError(str(exc)) from exc
        if execution.status is not ProductionExecutionStatus.QUEUED:
            raise ProductionWorkerError("只有 QUEUED execution 可以由 worker 启动")
        if execution.input_snapshot is None:
            return self._fail(project_id, execution, "execution 缺少 immutable input snapshot")

        shot_snapshot = self._single_shot_snapshot(execution.input_snapshot)
        try:
            started = self.execution_service.submit_execution(
                project_id,
                execution.id,
                runtime_adapter,
                input_snapshot=shot_snapshot,
            )
        except Exception as exc:
            # submit_execution durably records FAILED for adapter errors. A
            # failed execution is returned so a queue caller can inspect it.
            current = self.execution_service.get_execution(project_id, execution.id)
            if current.status is ProductionExecutionStatus.FAILED:
                return current
            return self._fail(project_id, current, f"adapter submit failed: {exc}")

        runtime_reference = self._runtime_reference(project_id, started.id)
        return self._poll(project_id, started, runtime_adapter, runtime_reference)

    execute = run

    def resume(
        self,
        project_id: str,
        execution_id: str,
        *,
        adapter: ProductionRuntimeAdapter | None = None,
    ) -> ProductionExecution:
        """Resume polling a persisted RUNNING execution.

        A process restart must not submit a second runtime job.  The runtime
        reference is recovered from the immutable STARTED event and the
        injected adapter remains the only runtime boundary.
        """
        runtime_adapter = adapter or self.adapter
        if runtime_adapter is None:
            raise ProductionWorkerError("ProductionWorker 需要一个 ProductionRuntimeAdapter")
        try:
            execution = self.execution_service.get_execution(project_id, execution_id)
        except ProductionExecutionServiceError as exc:
            raise ProductionWorkerError(str(exc)) from exc
        if execution.status is not ProductionExecutionStatus.RUNNING:
            raise ProductionWorkerError("只有 RUNNING execution 可以恢复")
        try:
            runtime_reference = self._runtime_reference(project_id, execution.id)
            return self._poll(project_id, execution, runtime_adapter, runtime_reference)
        except Exception as exc:
            current = self.execution_service.get_execution(project_id, execution.id)
            if current.status in self._terminal_statuses():
                return current
            return self._fail(project_id, current, f"worker resume failed: {exc}")

    def cancel(self, project_id: str, execution_id: str, reason: str | None = None) -> ProductionExecution:
        """Cancel a queued/running execution through the durable service."""
        try:
            return self.execution_service.cancel_execution(project_id, execution_id, reason)
        except ProductionExecutionServiceError as exc:
            raise ProductionWorkerError(str(exc)) from exc

    def _poll(
        self,
        project_id: str,
        execution: ProductionExecution,
        adapter: ProductionRuntimeAdapter,
        runtime_reference: str,
    ) -> ProductionExecution:
        for _ in range(self.max_polls):
            try:
                self._drain_events(project_id, execution.id, adapter, runtime_reference)
                execution = self.execution_service.get_execution(project_id, execution.id)
                if execution.status in self._terminal_statuses():
                    return execution

                raw_status = adapter.get_status(runtime_reference)
                status = self._normalize_status(adapter, raw_status)
                execution = self.execution_service.get_execution(project_id, execution.id)
                if status == ProductionExecutionStatus.FAILED:
                    return self._fail(project_id, execution, "runtime reported FAILED")
                if status == ProductionExecutionStatus.CANCELLED:
                    return self.execution_service.cancel_execution(
                        project_id, execution.id, "runtime reported CANCELLED", _notify_runtime=False
                    )
                if status == ProductionExecutionStatus.SUCCEEDED:
                    self._persist_result_artifacts(project_id, execution.id, adapter, runtime_reference)
                    return self.execution_service.complete_execution(
                        project_id, execution.id, {"runtime_reference": runtime_reference}
                    )
            except Exception as exc:
                execution = self.execution_service.get_execution(project_id, execution.id)
                if execution.status in self._terminal_statuses():
                    return execution
                return self._fail(project_id, execution, f"worker execution failed: {exc}")
            if self.poll_interval:
                time.sleep(self.poll_interval)

        execution = self.execution_service.get_execution(project_id, execution.id)
        if execution.status in self._terminal_statuses():
            return execution
        return self._fail(project_id, execution, "runtime polling timed out")

    def _drain_events(
        self,
        project_id: str,
        execution_id: str,
        adapter: ProductionRuntimeAdapter,
        runtime_reference: str,
    ) -> None:
        drain = getattr(adapter, "drain_events", None)
        if not callable(drain):
            return
        events = drain(runtime_reference)
        if events is None:
            return
        for event in events:
            event_type, payload = self._event_parts(event)
            if event_type == ProductionEventType.FINISHED.value:
                artifacts = self._artifact_specs(payload.get("artifacts"))
                for spec in artifacts:
                    self._persist_artifact(project_id, execution_id, spec, runtime_reference)
                finished_payload = dict(payload)
                finished_payload.pop("artifacts", None)
                self.execution_service.complete_execution(project_id, execution_id, finished_payload)
            elif event_type == ProductionEventType.FAILED.value:
                self.execution_service.fail_execution(
                    project_id, execution_id, payload.get("error"), payload
                )
            elif event_type == ProductionEventType.CANCELLED.value:
                self.execution_service.cancel_execution(
                    project_id, execution_id, payload.get("reason"), _notify_runtime=False
                )
            elif event_type == ProductionEventType.STARTED.value:
                current = self.execution_service.get_execution(project_id, execution_id)
                if current.status is ProductionExecutionStatus.QUEUED:
                    self.execution_service.start_execution(project_id, execution_id, payload)
            elif event_type == ProductionEventType.PROGRESS.value:
                self.execution_service.update_progress(
                    project_id, execution_id, payload_json=payload
                )
            elif event_type == ProductionEventType.SHOT_COMPLETED.value:
                self.execution_service.append_event(
                    project_id, execution_id, ProductionEventType.SHOT_COMPLETED, payload
                )
            else:
                raise ProductionWorkerError(f"不支持的 runtime event: {event_type}")

    def _persist_result_artifacts(
        self,
        project_id: str,
        execution_id: str,
        adapter: ProductionRuntimeAdapter,
        runtime_reference: str,
    ) -> None:
        result = None
        for method_name in ("get_artifacts", "get_result"):
            method = getattr(adapter, method_name, None)
            if callable(method):
                result = method(runtime_reference)
                if result is not None:
                    break
        for spec in self._artifact_specs(result):
            self._persist_artifact(project_id, execution_id, spec, runtime_reference)

    def _persist_artifact(
        self,
        project_id: str,
        execution_id: str,
        spec: Mapping[str, object] | object,
        runtime_reference: str,
    ) -> None:
        if isinstance(spec, Mapping):
            artifact_type = str(spec.get("artifact_type") or spec.get("type") or "runtime-artifact")
            metadata = dict(spec.get("metadata_json") or spec.get("metadata") or {})
            metadata.setdefault("runtime_reference", runtime_reference)
            filename = spec.get("filename")
            source = spec
        else:
            artifact_type = "runtime-artifact"
            metadata = {"runtime_reference": runtime_reference}
            filename = None
            source = spec
        execution = self.execution_service.get_execution(project_id, execution_id)
        metadata.setdefault("execution_id", execution_id)
        if execution.input_snapshot is not None:
            shot_ids = list(execution.input_snapshot.shot_parameters)
            if shot_ids:
                metadata.setdefault("shot_id", shot_ids[0])
            metadata.setdefault("reference_versions", dict(execution.input_snapshot.reference_asset_versions))
        relative_path, stored_metadata = self.artifact_storage.store(
            project_id,
            execution_id,
            artifact_type,
            source,
            filename=str(filename) if filename else None,
            metadata=metadata,
        )
        self.execution_service.record_artifact(
            project_id,
            execution_id,
            artifact_type,
            relative_path,
            stored_metadata,
        )

    @staticmethod
    def _artifact_specs(value: object) -> list[Mapping[str, object] | object]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            if isinstance(value.get("artifacts"), (list, tuple)):
                return list(value["artifacts"])
            if any(key in value for key in ("path", "source_path", "content", "data", "bytes", "filename")):
                return [value]
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    @staticmethod
    def _event_parts(event: RuntimeEvent | Mapping[str, object] | object) -> tuple[str, dict[str, object]]:
        if isinstance(event, RuntimeEvent):
            return event.event_type.upper(), dict(event.payload)
        if isinstance(event, Mapping):
            event_type = event.get("event_type") or event.get("type")
            payload = event.get("payload_json") or event.get("payload") or {}
            return str(event_type or "").upper(), dict(payload) if isinstance(payload, Mapping) else {}
        event_type = getattr(event, "event_type", "")
        payload = getattr(event, "payload", {})
        return str(event_type).upper(), dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _normalize_status(adapter: ProductionRuntimeAdapter, raw_status: object) -> ProductionExecutionStatus:
        value = raw_status
        mapper = getattr(adapter, "map_status", None)
        if callable(mapper):
            try:
                value = mapper(raw_status)
            except Exception:
                value = raw_status
        if isinstance(value, Mapping):
            value = value.get("status") or value.get("state")
        if hasattr(value, "value"):
            value = value.value
        try:
            return ProductionExecutionStatus(str(value).strip().upper())
        except ValueError as exc:
            raise ProductionWorkerError(f"unknown runtime status: {raw_status}") from exc

    @staticmethod
    def _terminal_statuses() -> set[ProductionExecutionStatus]:
        return {
            ProductionExecutionStatus.SUCCEEDED,
            ProductionExecutionStatus.FAILED,
            ProductionExecutionStatus.CANCELLED,
        }

    def _runtime_reference(self, project_id: str, execution_id: str) -> str:
        events = self.execution_service.list_events(project_id, execution_id)
        for event in reversed(events):
            reference = event.payload_json.get("runtime_reference")
            if isinstance(reference, str) and reference.strip():
                return reference.strip()
        raise ProductionWorkerError("execution 缺少 runtime reference")

    def _fail(self, project_id: str, execution: ProductionExecution, message: str) -> ProductionExecution:
        try:
            return self.execution_service.fail_execution(project_id, execution.id, message)
        except ProductionExecutionServiceError as exc:
            raise ProductionWorkerError(str(exc)) from exc

    @staticmethod
    def _single_shot_snapshot(snapshot: ProductionInputSnapshot) -> ProductionInputSnapshot:
        if not snapshot.shot_parameters:
            raise ProductionWorkerError("production input snapshot 不包含 shot")
        candidates = list(snapshot.shot_parameters.items())

        def order(item: tuple[str, object]) -> tuple[float, str]:
            shot_id, parameters = item
            value = parameters.get("order") if isinstance(parameters, Mapping) else None
            try:
                return float(value), shot_id
            except (TypeError, ValueError):
                return float("inf"), shot_id

        shot_id, parameters = sorted(candidates, key=order)[0]
        return ProductionInputSnapshot(
            project_id=snapshot.project_id,
            story_revision_id=snapshot.story_revision_id,
            script_revision_id=snapshot.script_revision_id,
            shot_plan_revision_id=snapshot.shot_plan_revision_id,
            reference_asset_versions=snapshot.reference_asset_versions,
            shot_parameters={shot_id: parameters},
        )

    def _resolve_identity(
        self,
        project_id_or_execution: str | ProductionExecution | object,
        execution_id: str | None,
    ) -> tuple[str, str]:
        if execution_id is not None:
            if not isinstance(project_id_or_execution, str) or not project_id_or_execution.strip():
                raise ProductionWorkerError("project_id 无效")
            return project_id_or_execution, execution_id
        if isinstance(project_id_or_execution, ProductionExecution):
            job = self.execution_service.repository.get_production_job(project_id_or_execution.production_job_id)
            if job is None:
                raise ProductionWorkerError("ProductionExecution 所属的 ProductionJob 不存在")
            return job.project_id, project_id_or_execution.id
        # Preserve the pre-Task007 interface's explicit seam failure for an
        # unconfigured object instead of accidentally executing arbitrary data.
        raise NotImplementedError("ProductionWorker.run 需要 project_id 和 execution_id")
