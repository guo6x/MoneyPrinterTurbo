"""Execution queue orchestration for production jobs.

This module deliberately stops at the queue/worker seam.  It records durable
execution state, immutable events, and artifact metadata, but never invokes a
renderer, FFmpeg, an AI provider, or the MoneyPrinterTurbo runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotStatus,
    ProductionInputSnapshot,
    ProviderTask,
    ReferenceBindingType,
)
from aidrama_studio.services.adapters import (
    ProductionRuntimeAdapter,
    RuntimeContentRejectedError,
    RuntimeEvent,
    RuntimeSubmission,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService, ProductionServiceError
from .active_work import TERMINAL_PROVIDER_STATES
from .provider_profiles import ProviderProfileService
from .security import sanitize_error, sanitize_persistent_metadata
from .production_reliability import (
    PaidBudgetError,
    PaidBudgetExhausted,
    PaidBudgetService,
)


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
        self.paid_budgets = PaidBudgetService(self.repository)

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
        _creative_retry_context: tuple[str, str] | None = None,
        _provider_rejection_context: str | None = None,
    ) -> tuple[ProductionExecution, object]:
        """Create the immutable execution and its first/retry attempt atomically."""
        self._require_project(project_id)
        if not isinstance(worker_type, str) or not worker_type.strip():
            raise ProductionExecutionServiceError("worker_type 不能为空")
        job = self.production_service.get_job(project_id, production_job_id)
        if job.status is ProductionJobStatus.CANCELLED or (
            job.status is ProductionJobStatus.SUCCEEDED
            and _creative_retry_context is None
            and _provider_rejection_context is None
        ):
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
        expected_plan_hash = runtime_plan.plan_hash if runtime_plan is not None else None
        for supplied, expected, label in (
            (input_snapshot.runtime_plan_id, runtime_plan_id, "RuntimePlan id"),
            (
                input_snapshot.generation_brief_id,
                generation_brief_id,
                "GenerationBrief id",
            ),
            (input_snapshot.runtime_plan_hash, expected_plan_hash, "RuntimePlan hash"),
        ):
            if supplied is not None and supplied != expected:
                raise ProductionExecutionServiceError(f"input snapshot 的 {label} 不匹配")
        frozen_snapshot = input_snapshot.model_copy(
            update={
                "runtime_plan_id": runtime_plan_id,
                "generation_brief_id": generation_brief_id,
                "runtime_plan_hash": expected_plan_hash,
            }
        )
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
            input_snapshot=frozen_snapshot,
            runtime_plan_id=runtime_plan_id,
            generation_brief_id=generation_brief_id,
            creative_retry_of_execution_id=(
                _creative_retry_context[0] if _creative_retry_context else None
            ),
            creative_rejection_review_id=(
                _creative_retry_context[1] if _creative_retry_context else None
            ),
        )
        attempt = ProductionAttempt(
            id=uuid4().hex,
            production_shot_id=shot.id,
            attempt_number=(attempts[-1].attempt_number + 1 if attempts else 1),
            status=ProductionAttemptStatus.STARTED,
            runtime_adapter=worker_type.strip(),
            input_snapshot_json=frozen_snapshot.to_json_dict(),
            created_at=now,
        )
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.QUEUED,
            payload_json={
                "shot_id": shot_id,
                "attempt_id": attempt.id,
                **(
                    {
                        "generation_intent": "CREATIVE_REGENERATION",
                        "creative_retry_of_execution_id": _creative_retry_context[0],
                        "creative_rejection_review_id": _creative_retry_context[1],
                    }
                    if _creative_retry_context
                    else (
                        {
                            "generation_intent": "PROVIDER_CONTENT_RETRY",
                            "provider_rejected_execution_id": _provider_rejection_context,
                        }
                        if _provider_rejection_context
                        else {"generation_intent": "INITIAL_OR_TECHNICAL_RETRY"}
                    )
                ),
            },
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

    def request_creative_regeneration(
        self,
        project_id: str,
        production_job_id: str,
        production_shot_id: str,
        rejected_review_id: str,
        input_snapshot: ProductionInputSnapshot,
        *,
        worker_type: str = "mpt",
        runtime_plan_id: str | None = None,
        generation_brief_id: str | None = None,
    ) -> tuple[ProductionExecution, ProductionAttempt]:
        """Explicitly create a new paid-capable attempt after creative reject.

        Technical success, QC, review, artifact and Shot provenance are checked
        before a new immutable Execution/Attempt pair is appended.  Merely
        writing a REJECTED review never submits another provider request.
        """
        self._require_project(project_id)
        job = self.production_service.get_job(project_id, production_job_id)
        shot = next(
            (
                item
                for item in self.repository.list_production_shots(job.id)
                if item.id == production_shot_id
                or item.shot_id == production_shot_id
            ),
            None,
        )
        if shot is None:
            raise ProductionExecutionServiceError(
                "ProductionShot 不属于该 ProductionJob"
            )
        review = self.repository.get_production_review(rejected_review_id)
        if (
            review is None
            or review.project_id != project_id
            or review.decision is not ProductionReviewDecision.REJECTED
        ):
            raise ProductionExecutionServiceError("必须引用该项目的 REJECTED review")
        reviews = self.repository.list_production_reviews(
            project_id, review.qc_result_id
        )
        if not reviews or reviews[-1].id != review.id:
            raise ProductionExecutionServiceError("该 rejection 已不是最新 human review")
        qc = self.repository.get_production_qc_result(review.qc_result_id)
        if (
            qc is None
            or qc.project_id != project_id
            or qc.status is not ProductionQCStatus.QC_PASS
            or qc.artifact_id is None
        ):
            raise ProductionExecutionServiceError(
                "creative regeneration 要求真实 QC_PASS artifact"
            )
        execution = self.repository.get_production_execution(qc.execution_id)
        artifact = self.repository.get_production_artifact(qc.artifact_id)
        if (
            execution is None
            or execution.production_job_id != job.id
            or execution.status is not ProductionExecutionStatus.SUCCEEDED
            or artifact is None
            or artifact.execution_id != execution.id
        ):
            raise ProductionExecutionServiceError(
                "rejected source 的 execution/artifact provenance 无效"
            )
        execution_shots = (
            execution.input_snapshot.shot_parameters
            if execution.input_snapshot is not None
            else {}
        )
        artifact_shot = str((artifact.metadata_json or {}).get("shot_id") or "")
        if shot.shot_id not in execution_shots and artifact_shot not in {
            shot.id,
            shot.shot_id,
        }:
            raise ProductionExecutionServiceError(
                "rejected source 不属于指定 ProductionShot"
            )
        if (
            input_snapshot.project_id != project_id
            or input_snapshot.shot_plan_revision_id != job.shot_plan_revision_id
            or tuple(input_snapshot.shot_parameters) != (shot.shot_id,)
        ):
            raise ProductionExecutionServiceError(
                "creative regeneration snapshot 不属于指定 Shot"
            )
        return self.enqueue_shot_execution_with_attempt(
            project_id,
            job.id,
            input_snapshot,
            worker_type=worker_type,
            runtime_plan_id=runtime_plan_id,
            generation_brief_id=generation_brief_id,
            _creative_retry_context=(execution.id, review.id),
        )

    def request_provider_content_retry(
        self,
        project_id: str,
        production_job_id: str,
        rejected_execution_id: str,
        input_snapshot: ProductionInputSnapshot,
        *,
        worker_type: str = "mpt",
        runtime_plan_id: str | None = None,
        generation_brief_id: str | None = None,
    ) -> tuple[ProductionExecution, ProductionAttempt]:
        """Explicitly append a new attempt after provider content rejection.

        This method only creates durable queued state.  It never rewrites the
        user's creative input and never invokes an adapter; a separate worker
        action remains required for any new paid submission.
        """

        rejected, job = self._get_execution(project_id, rejected_execution_id)
        if job.id != production_job_id:
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED execution 不属于该 ProductionJob"
            )
        if rejected.status is not ProductionExecutionStatus.FAILED:
            raise ProductionExecutionServiceError(
                "只有失败的 CONTENT_REJECTED execution 可以显式重试"
            )
        rejected_task = next(
            (
                item
                for item in reversed(self.repository.list_provider_tasks(project_id))
                if item.execution_id == rejected.id
                and item.state == "CONTENT_REJECTED"
            ),
            None,
        )
        if rejected_task is None:
            raise ProductionExecutionServiceError(
                "execution 没有明确的 provider CONTENT_REJECTED outcome"
            )
        if (
            not rejected.runtime_plan_id
            or not runtime_plan_id
            or runtime_plan_id == rejected.runtime_plan_id
        ):
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry 必须使用重新确认授权后的新 RuntimePlan"
            )
        previous_plan = self.repository.get_runtime_plan(rejected.runtime_plan_id)
        retry_plan = self.repository.get_runtime_plan(runtime_plan_id)
        if (
            previous_plan is None
            or retry_plan is None
            or previous_plan.project_id != project_id
            or retry_plan.project_id != project_id
            or previous_plan.production_job_id != job.id
            or retry_plan.production_job_id != job.id
        ):
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry RuntimePlan provenance 无效"
            )
        previous_authorization = dict(previous_plan.authorization)
        retry_authorization = dict(retry_plan.authorization)
        retry_authorization_id = str(
            retry_authorization.get("authorization_id") or ""
        ).strip()
        previous_authorization_id = str(
            previous_authorization.get("authorization_id") or ""
        ).strip()
        fingerprint = str(
            retry_authorization.get("authorization_fingerprint") or ""
        ).strip()
        if (
            retry_authorization.get("approved") is not True
            or not retry_authorization_id
            or retry_authorization_id == previous_authorization_id
            or not str(retry_authorization.get("authorized_at") or "").strip()
            or str(retry_authorization.get("disclosure_version") or "")
            != "regional-provider-v1"
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry 缺少新的付费授权/区域披露确认"
            )
        expected_authorization = {
            "provider_id": retry_plan.provider_id,
            "model_id": retry_plan.model_id,
            "deployment_region": retry_plan.deployment_region,
            "endpoint_profile_id": retry_plan.endpoint_profile_id,
            "endpoint_class": retry_plan.endpoint_class,
        }
        if any(
            retry_authorization.get(key) != expected
            for key, expected in expected_authorization.items()
        ) or tuple(retry_authorization.get("transmitted_content_types") or ()) != tuple(
            retry_plan.transmitted_content_types
        ):
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry 授权与冻结 Provider/region/content 不匹配"
            )
        rejected_shots = (
            rejected.input_snapshot.shot_parameters
            if rejected.input_snapshot is not None
            else {}
        )
        if len(rejected_shots) != 1:
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry 必须引用单一 shot execution"
            )
        rejected_shot_id = next(iter(rejected_shots))
        if (
            input_snapshot.project_id != project_id
            or input_snapshot.shot_plan_revision_id != job.shot_plan_revision_id
            or tuple(input_snapshot.shot_parameters) != (rejected_shot_id,)
        ):
            raise ProductionExecutionServiceError(
                "CONTENT_REJECTED retry snapshot provenance 不匹配"
            )
        return self.enqueue_shot_execution_with_attempt(
            project_id,
            job.id,
            input_snapshot,
            worker_type=worker_type,
            runtime_plan_id=runtime_plan_id,
            generation_brief_id=generation_brief_id,
            _provider_rejection_context=rejected.id,
        )

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
        task = self._provider_task_intent(
            project_id, execution, adapter, runtime_snapshot
        )
        # A durable provider identity is the recovery boundary. Any process
        # that observes an already-closed create gate without an identity must
        # fail closed. It is not allowed to infer that the POST did not happen.
        if task.state in {
            "SUBMITTING",
            "SUBMISSION_UNCERTAIN",
            "UNCERTAIN_CREATE",
            "RECONCILIATION_REQUIRED",
        } and not task.provider_task_id:
            if task.state == "SUBMITTING":
                task = self.paid_budgets.mark_uncertain(
                    task,
                    "provider create interrupted before identity persistence",
                )
            raise ProductionExecutionServiceError(
                "UNCERTAIN_CREATE: provider submission 必须先 reconciliation"
            )
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
                self._update_provider_task(
                    task,
                    state="FAILED",
                    error_message="runtime adapter 拒绝 input snapshot",
                )
                self.fail_execution(
                    project_id,
                    execution.id,
                    error_message="runtime adapter 拒绝 input snapshot",
                )
                raise ProductionExecutionServiceError(
                    "runtime adapter 拒绝 input snapshot"
                )
        except ProductionExecutionServiceError:
            raise
        except Exception as exc:
            # Validation happens before the create gate and therefore cannot
            # consume budget or create a remote task.
            self._update_provider_task(
                task, state="FAILED", error_message=self._safe_error(exc)
            )
            self.fail_execution(
                project_id, execution.id, error_message=self._safe_error(exc)
            )
            raise ProductionExecutionServiceError(
                f"runtime validation 失败: {type(exc).__name__}"
            ) from exc

        try:
            task, claimed, _reservation = self.paid_budgets.claim_create(
                task,
                execution,
                require_budget=self._requires_paid_budget(execution, adapter),
            )
        except PaidBudgetExhausted as exc:
            raise ProductionExecutionServiceError(str(exc)) from exc
        except PaidBudgetError as exc:
            raise ProductionExecutionServiceError(str(exc)) from exc
        if not claimed:
            if task.provider_task_id:
                return self.start_execution(
                    project_id,
                    execution.id,
                    {
                        "adapter": getattr(
                            adapter, "name", adapter.__class__.__name__
                        ),
                        "runtime_reference": task.provider_task_id,
                        "provider_metadata": dict(task.metadata),
                    },
                )
            raise ProductionExecutionServiceError(
                "UNCERTAIN_CREATE: create gate 已关闭，不得重复 submit"
            )

        runtime_reference: str | None = None
        try:
            submission = adapter.submit(runtime_snapshot)
            runtime_reference = self._submission_reference(submission)
            submission_metadata = self._submission_metadata(submission)
            task = self.paid_budgets.mark_accepted(
                task,
                provider_task_id=runtime_reference,
                metadata=submission_metadata,
            )
        except RuntimeContentRejectedError as exc:
            # An explicit provider response proves the create transport was
            # attempted. Count it, but never auto-create a replacement.
            self.paid_budgets.mark_consumed(task)
            self.mark_provider_content_rejected(project_id, execution.id, exc)
            raise ProductionExecutionServiceError(
                "runtime provider 明确拒绝了内容；需要用户编辑后显式创建新 attempt"
            ) from exc
        except Exception as exc:
            # Once submit() is entered, every unknown outcome is uncertain by
            # default. Adapters are not trusted to opt into fail-closed safety.
            try:
                self.paid_budgets.mark_uncertain(
                    task,
                    exc,
                    provider_task_id=runtime_reference,
                )
            except Exception:
                # If the second persistence attempt also fails, the durable
                # task remains SUBMITTING. Startup reconciliation converts it
                # to UNCERTAIN_CREATE before any worker can run again.
                pass
            raise ProductionExecutionServiceError(
                f"UNCERTAIN_CREATE: runtime submit outcome unknown ({type(exc).__name__})"
            ) from exc
        start_payload = {
            "adapter": getattr(adapter, "name", adapter.__class__.__name__),
            "runtime_reference": runtime_reference,
        }
        # Provider adapters may return non-secret request trace data (model,
        # prompt hash, exact frozen reference version, and provider task id). Keep
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
        queued_event = next(
            (
                item
                for item in self.repository.list_production_events(execution.id)
                if item.event_type is ProductionEventType.QUEUED
            ),
            None,
        )
        shot_id = next(iter(snapshot.shot_parameters), None)
        attempt_id = (
            queued_event.payload_json.get("attempt_id")
            if queued_event is not None
            else None
        )
        key_payload = {
            "project_id": project_id,
            "production_job_id": execution.production_job_id,
            "shot_id": shot_id,
            "execution_id": execution.id,
            "runtime_plan_id": plan_id,
            "runtime_plan_hash": plan_hash,
            "attempt_id": attempt_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "snapshot": snapshot.to_json_dict(),
        }
        key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        now = _now()
        task, _created = self.repository.get_or_create_provider_task(ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=execution.id,
            capability="VIDEO_GENERATIVE", provider_id=provider_id, model_id=model_id,
            idempotency_key=key, state="PENDING_SUBMISSION",
            request_summary={
                "project_id": project_id,
                "production_job_id": execution.production_job_id,
                "shot_id": shot_id,
                "execution_id": execution.id,
                "attempt_id": attempt_id,
                "snapshot_hash": hashlib.sha256(json.dumps(snapshot.to_json_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                "runtime_plan_id": execution.runtime_plan_id,
                "runtime_plan_hash": plan_hash or None,
            },
            created_at=now, updated_at=now,
        ))
        return task

    def _requires_paid_budget(
        self,
        execution: ProductionExecution,
        adapter: ProductionRuntimeAdapter,
    ) -> bool:
        if bool(getattr(adapter, "requires_paid_budget", False)):
            return True
        if not execution.runtime_plan_id:
            return False
        plan = self.repository.get_runtime_plan(execution.runtime_plan_id)
        if plan is None:
            return False
        return str(plan.deployment_region).upper() != "LOCAL"

    def _update_provider_task(self, task: ProviderTask, *, state: str | None = None, provider_task_id: str | None = None, metadata: Mapping[str, object] | None = None, submitted_at: str | None = None, error_message: str | None = None) -> ProviderTask:
        updated = task.model_copy(update={
            "state": state or task.state,
            "provider_task_id": provider_task_id if provider_task_id is not None else task.provider_task_id,
            "metadata": sanitize_persistent_metadata(
                dict(task.metadata) | dict(metadata or {})
            ),
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
        safe = sanitize_persistent_metadata(raw)
        if not isinstance(safe, Mapping):
            return {}
        result = dict(safe)
        # Raw creative prompts are provider inputs, not durable operational
        # metadata. Adapters publish hashes and frozen source IDs instead.
        result.pop("prompt", None)
        result.pop("raw_prompt", None)
        return result

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
        # A canonical per-shot execution is only one fact in the aggregate
        # ProductionJob lifecycle.  Its matching ProductionAttempt updates
        # the ProductionShot and derives the job status after QC; completing
        # the runtime execution itself must not terminalize the whole job.
        # Legacy whole-job executions are retained for compatibility and are
        # identified by their QUEUED event having no frozen shot identity.
        queued_event = next(
            (
                item
                for item in self.repository.list_production_events(execution.id)
                if item.event_type is ProductionEventType.QUEUED
            ),
            None,
        )
        is_per_shot_execution = bool(
            queued_event is not None and queued_event.payload_json.get("shot_id")
        )
        result = self.repository.transition_production_execution_atomic(
            execution.id,
            expected_status=ProductionExecutionStatus.RUNNING,
            status=ProductionExecutionStatus.SUCCEEDED,
            finished_at=now,
            job_status=(
                None
                if is_per_shot_execution
                else ProductionJobStatus.SUCCEEDED
            ),
            event=event,
        )
        self._sync_provider_task_terminal(project_id, execution.id, "SUCCEEDED")
        return result

    def _sync_provider_task_terminal(self, project_id: str, execution_id: str, state: str) -> None:
        for task in self.repository.list_provider_tasks(project_id):
            if (
                task.execution_id == execution_id
                and task.state not in TERMINAL_PROVIDER_STATES
            ):
                self._update_provider_task(task, state=state)

    def mark_provider_content_rejected(
        self,
        project_id: str,
        execution_id: str,
        error: RuntimeContentRejectedError,
    ) -> ProductionExecution:
        """Persist an explicit provider-policy outcome without raw response data."""

        execution, _ = self._get_execution(project_id, execution_id)
        if execution.status not in {
            ProductionExecutionStatus.QUEUED,
            ProductionExecutionStatus.RUNNING,
        }:
            raise ProductionExecutionServiceError(
                "只有 QUEUED/RUNNING execution 可以记录 CONTENT_REJECTED"
            )
        task = next(
            (
                item
                for item in reversed(self.repository.list_provider_tasks(project_id))
                if item.execution_id == execution_id
                and not (
                    item.provider_id == "RUNTIME_BOUNDARY"
                    and item.idempotency_key == f"production:{execution_id}"
                )
            ),
            None,
        )
        if task is None:
            raise ProductionExecutionServiceError(
                "execution 缺少 provider task，不能记录 CONTENT_REJECTED"
            )
        outcome = {
            "provider_outcome": "CONTENT_REJECTED",
            "failure_category": error.failure_category,
            "policy_stage": error.policy_stage,
            "automatic_retry_allowed": False,
        }
        if error.provider_code:
            outcome["provider_code"] = error.provider_code
        now = _now()
        updated_task = task.model_copy(
            update={
                "state": "CONTENT_REJECTED",
                "metadata": sanitize_persistent_metadata(
                    dict(task.metadata) | outcome
                ),
                "error_message": "provider content rejected",
                "updated_at": now,
            }
        )
        event = ProductionEvent(
            id=uuid4().hex,
            execution_id=execution.id,
            event_type=ProductionEventType.FAILED,
            payload_json={**outcome, "error": "provider content rejected"},
            created_at=now,
        )
        return self.repository.mark_provider_content_rejected_atomic(
            updated_task,
            expected_status=execution.status,
            event=event,
            finished_at=now,
        )

    def mark_provider_artifact_pending(
        self,
        project_id: str,
        execution_id: str,
        error: object,
    ) -> ProviderTask:
        """Keep a paid provider success resumable when only download failed."""

        execution, _ = self._get_execution(project_id, execution_id)
        self._require_status(
            execution,
            ProductionExecutionStatus.RUNNING,
            "只有 RUNNING execution 可以等待 artifact 下载",
        )
        task = self._execution_provider_task(project_id, execution_id)
        try:
            attempt_count = int(task.metadata.get("artifact_download_attempts", 0)) + 1
        except (TypeError, ValueError):
            attempt_count = 1
        delay_seconds = min(300, 10 * (2 ** min(attempt_count - 1, 5)))
        next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat(timespec="microseconds")
        state = (
            "PROVIDER_SUCCEEDED_ARTIFACT_PENDING"
            if attempt_count <= 5
            else "RECONCILIATION_REQUIRED"
        )
        return self._update_provider_task(
            task,
            state=state,
            metadata={
                "artifact_download_attempts": attempt_count,
                "artifact_next_retry_at": next_retry_at,
            },
            error_message=self._safe_error(error),
        )

    def mark_provider_polling_interrupted(
        self,
        project_id: str,
        execution_id: str,
        error: object,
        *,
        retry_after_seconds: float | None = None,
    ) -> ProviderTask:
        """Persist a recoverable GET/poll failure for the original task."""

        execution, _ = self._get_execution(project_id, execution_id)
        self._require_status(
            execution,
            ProductionExecutionStatus.RUNNING,
            "只有 RUNNING execution 可以中断 polling",
        )
        task = self._execution_provider_task(project_id, execution_id)
        try:
            failure_count = int(task.metadata.get("poll_failure_count", 0)) + 1
        except (TypeError, ValueError):
            failure_count = 1
        try:
            requested_delay = max(0.0, float(retry_after_seconds or 0.0))
        except (TypeError, ValueError):
            requested_delay = 0.0
        delay_seconds = min(
            600.0,
            max(requested_delay, float(10 * (2 ** min(failure_count - 1, 6)))),
        )
        next_poll_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat(timespec="microseconds")
        return self._update_provider_task(
            task,
            state=(
                "POLLING_INTERRUPTED"
                if failure_count <= 8
                else "RECONCILIATION_REQUIRED"
            ),
            metadata={
                "poll_failure_count": failure_count,
                "poll_next_retry_at": next_poll_at,
                "poll_retry_after_seconds": delay_seconds,
            },
            error_message=self._safe_error(error),
        )

    def mark_provider_reconciliation_required(
        self,
        project_id: str,
        execution_id: str,
        error: object,
    ) -> ProviderTask:
        execution, _ = self._get_execution(project_id, execution_id)
        self._require_status(
            execution,
            ProductionExecutionStatus.RUNNING,
            "只有 RUNNING execution 可以进入 reconciliation",
        )
        return self._update_provider_task(
            self._execution_provider_task(project_id, execution_id),
            state="RECONCILIATION_REQUIRED",
            error_message=self._safe_error(error),
        )

    def mark_provider_polling_active(
        self,
        project_id: str,
        execution_id: str,
        *,
        running: bool,
    ) -> ProviderTask:
        task = self._execution_provider_task(project_id, execution_id)
        metadata = dict(task.metadata)
        for key in (
            "poll_failure_count",
            "poll_next_retry_at",
            "poll_retry_after_seconds",
        ):
            metadata.pop(key, None)
        updated = task.model_copy(
            update={
                "state": "PROVIDER_RUNNING" if running else "PROVIDER_ACCEPTED",
                "metadata": metadata,
                "error_message": None,
                "updated_at": _now(),
            }
        )
        return self.repository.update_provider_task(updated)

    def _execution_provider_task(
        self, project_id: str, execution_id: str
    ) -> ProviderTask:
        task = next(
            (
                item
                for item in reversed(
                    self.repository.list_provider_tasks(project_id)
                )
                if item.execution_id == execution_id
                and not (
                    item.provider_id == "RUNTIME_BOUNDARY"
                    and item.idempotency_key == f"production:{execution_id}"
                )
                and item.provider_task_id
            ),
            None,
        )
        if task is None or not task.provider_task_id:
            raise ProductionExecutionServiceError(
                "execution 缺少可恢复的 provider task"
            )
        return task

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
        sanitized = sanitize_persistent_metadata(dict(metadata_json or {}))
        metadata = dict(sanitized) if isinstance(sanitized, Mapping) else {}
        digest = metadata.get("sha256")
        artifact = ProductionArtifact(
            id=uuid4().hex,
            execution_id=execution.id,
            artifact_type=artifact_type.strip(),
            path=safe_path,
            metadata_json=metadata,
            created_at=_now(),
        )
        if isinstance(digest, str) and digest.strip():
            try:
                return self.repository.create_production_artifact_idempotent(
                    artifact, sha256=digest
                )
            except ValueError as exc:
                raise ProductionExecutionServiceError(str(exc)) from exc
        if any(
            item.path == safe_path
            for item in self.repository.list_production_artifacts(execution.id)
        ):
            raise ProductionExecutionServiceError("artifact path 不可覆盖")
        return self.repository.create_production_artifact(artifact)

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
