"""Durable product-agent orchestration over the existing AIDrama services.

This module decides *which* formal service should act next.  Story, Script,
Shot, Reference, Production, QC, Review, and Final Assembly remain owned by
their existing services; AUTO Mode never implements a parallel workflow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aidrama_studio.domain import (
    AutoAction,
    AutoAgentEvent,
    AutoDecision,
    AutoOrchestrationState,
    AutoPaidAuthorization,
    AutoPaidAuthorizationPreview,
    AutoRunStatus,
    AutoStage,
    FinalAssemblyStatus,
    HeavyJobStatus,
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ReferenceBindingType,
    ReferenceImageCandidateStatus,
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .background_runner import BackgroundProductionRunner
from .current_state import CurrentProductionStateService
from .final_assembly import FinalAssemblyService
from .heavy_job_runner import HeavyJobRunner
from .heavy_jobs import HeavyJobService
from .image_runtime import ImageRuntimeService
from .production import ProductionService
from .production_execution import ProductionExecutionService
from .production_qc import ProductionQCService
from .production_queue import ProductionAuthorizationPreview, ProductionQueueService
from .reference_assets import ReferenceAssetService
from .script import ScriptService
from .security import sanitize_error, sanitize_persistent_metadata
from .shot import ShotService
from .shot_keyframe import ShotKeyframeError
from .story import StoryService
from .vision_qc import VisionQCService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class AutoOrchestratorError(RuntimeError):
    pass


class AutoOrchestratorService:
    """Plan and execute one idempotent AUTO transition at a time."""

    ACTIVE_PROVIDER_STATES = frozenset(
        {
            "QUEUED",
            "RUNNING",
            "PAUSED",
            "SUBMITTING",
            "PROVIDER_ACCEPTED",
            "PROVIDER_RUNNING",
            "POLLING_INTERRUPTED",
            "PROVIDER_SUCCEEDED_ARTIFACT_PENDING",
            "RECONCILIATION_REQUIRED",
            "SUBMISSION_UNCERTAIN",
            "UNCERTAIN_CREATE",
        }
    )
    ACTIVE_HEAVY_STATES = frozenset(
        {
            HeavyJobStatus.QUEUED.value,
            HeavyJobStatus.RUNNING.value,
            HeavyJobStatus.INTERRUPTED.value,
        }
    )

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        story_service: StoryService | None = None,
        script_service: ScriptService | None = None,
        shot_service: ShotService | None = None,
        reference_service: ReferenceAssetService | None = None,
        image_runtime: ImageRuntimeService | None = None,
        production_service: ProductionService | None = None,
        production_queue: ProductionQueueService | None = None,
        execution_service: ProductionExecutionService | None = None,
        qc_service: ProductionQCService | None = None,
        vision_service: VisionQCService | None = None,
        final_service: FinalAssemblyService | None = None,
        heavy_jobs: HeavyJobService | None = None,
        background_runner: BackgroundProductionRunner | None = None,
        heavy_runner: HeavyJobRunner | None = None,
        current_state_service: CurrentProductionStateService | None = None,
        actor: str = "product-agent",
        drive_background: bool = False,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.story_service = story_service or StoryService(self.repository)
        self.script_service = script_service or ScriptService(self.repository)
        self.shot_service = shot_service or ShotService(self.repository)
        self.reference_service = reference_service or ReferenceAssetService(
            self.repository
        )
        self.image_runtime = image_runtime or ImageRuntimeService(self.repository)
        self.production_service = production_service or ProductionService(
            self.repository, reference_service=self.reference_service
        )
        self.production_queue = production_queue or ProductionQueueService(
            self.repository, production_service=self.production_service
        )
        self.execution_service = execution_service or ProductionExecutionService(
            self.repository, production_service=self.production_service
        )
        self.qc_service = qc_service or ProductionQCService(self.repository)
        self.vision_service = vision_service or VisionQCService(self.repository)
        self.final_service = final_service or FinalAssemblyService(self.repository)
        self.heavy_jobs = heavy_jobs or HeavyJobService(self.repository)
        self.background_runner = background_runner or BackgroundProductionRunner(
            self.repository
        )
        self.heavy_runner = heavy_runner or HeavyJobRunner(self.repository)
        self.current_state_service = (
            current_state_service
            or CurrentProductionStateService(
                self.repository, final_assembly_service=self.final_service
            )
        )
        self.actor = actor
        self.drive_background = drive_background

    def next_action(self, project_id: str) -> AutoDecision:
        """Return the next formal action from persisted product facts only."""

        decision = self._plan_raw(project_id)
        return self._apply_paid_gate(decision)

    def keyframe_readiness(self, project_id: str) -> dict[str, object]:
        """Return a safe UI projection of canonical Shot First Frame truth.

        The Workbench never derives readiness from references or AUTO labels.
        It receives the formal pre-live gate from the same service used by the
        Production queue, while artifact IDs, hashes and private paths remain
        outside the creator-facing projection.
        """

        self._require_project(project_id)
        jobs = self.repository.list_production_jobs(project_id)
        if not jobs:
            return {
                "gate": "PENDING",
                "planned_shot_count": 0,
                "validated_first_frame_count": 0,
                "missing_first_frame_count": 0,
                "invalid_first_frame_count": 0,
                "unintended_duplicate_first_frame_count": 0,
            }
        job = jobs[-1]
        try:
            report = self.production_queue.shot_keyframes.validate_pre_live(
                project_id, job.id
            )
        except ShotKeyframeError as exc:
            return {
                "gate": "BLOCKED",
                "planned_shot_count": 0,
                "validated_first_frame_count": 0,
                "missing_first_frame_count": 0,
                "invalid_first_frame_count": 0,
                "unintended_duplicate_first_frame_count": 0,
                "reason": sanitize_error(exc),
            }
        return {
            "gate": report.gate.value,
            "planned_shot_count": len(report.planned_shot_ids),
            "validated_first_frame_count": len(
                report.validated_first_frame_ids
            ),
            "missing_first_frame_count": len(
                report.missing_first_frame_shot_ids
            ),
            "invalid_first_frame_count": len(
                report.invalid_first_frame_shot_ids
            ),
            "unintended_duplicate_first_frame_count": (
                report.unintended_duplicate_first_frame_count
            ),
        }

    def get_state(self, project_id: str) -> AutoOrchestrationState | None:
        self._require_project(project_id)
        return self.repository.get_auto_orchestration_state(project_id)

    def list_events(self, project_id: str) -> list[AutoAgentEvent]:
        self._require_project(project_id)
        return self.repository.list_auto_agent_events(project_id)

    def step(self, project_id: str) -> AutoOrchestrationState:
        """Execute at most one formal action and persist the resulting decision."""

        decision = self.next_action(project_id)
        if decision.status in {
            AutoRunStatus.WAITING_HUMAN,
            AutoRunStatus.BLOCKED,
            AutoRunStatus.FAILED,
            AutoRunStatus.SUCCEEDED,
            AutoRunStatus.CANCELLED,
        }:
            result = (
                "AWAITING_HUMAN"
                if decision.status is AutoRunStatus.WAITING_HUMAN
                else decision.status.value
            )
            return self._record(decision, result=result, event_action=decision.next_action)

        if (
            decision.status is AutoRunStatus.WAITING_PROVIDER
            and not self.drive_background
        ):
            return self._record(
                decision,
                result="WAITING_FOR_BACKGROUND_RUNNER",
                event_action=decision.next_action,
            )

        try:
            running = decision.model_copy(update={"status": AutoRunStatus.RUNNING})
            self._record(
                running,
                result="ACTION_STARTED",
                event_action=decision.next_action,
            )
            result = self._execute(decision)
            after = self.next_action(project_id)
            return self._record(after, result=result, event_action=decision.next_action)
        except Exception as exc:
            safe = sanitize_error(exc, max_length=1200)
            failed = decision.model_copy(
                update={
                    "status": AutoRunStatus.FAILED,
                    "why": "正式服务未完成当前 AUTO action。",
                    "blocking_reason": safe,
                    "requires_human": True,
                    "requested_action": "INSPECT_FAILURE_AND_RESUME",
                }
            )
            return self._record(
                failed,
                result=f"FAILED:{type(exc).__name__}",
                event_action=decision.next_action,
            )

    def run_until_boundary(
        self, project_id: str, *, max_steps: int = 50
    ) -> AutoOrchestrationState:
        if isinstance(max_steps, bool) or not 1 <= int(max_steps) <= 200:
            raise AutoOrchestratorError("max_steps must be between 1 and 200")
        state: AutoOrchestrationState | None = None
        for _ in range(int(max_steps)):
            state = self.step(project_id)
            if state.status in {
                AutoRunStatus.WAITING_PROVIDER,
                AutoRunStatus.WAITING_HUMAN,
                AutoRunStatus.BLOCKED,
                AutoRunStatus.FAILED,
                AutoRunStatus.SUCCEEDED,
                AutoRunStatus.CANCELLED,
            }:
                return state
        decision = self.next_action(project_id).model_copy(
            update={
                "status": AutoRunStatus.BLOCKED,
                "why": "AUTO Mode 达到本次有界 step 上限。",
                "blocking_reason": "MAX_STEPS_REACHED",
                "requires_human": True,
                "requested_action": "RESUME_AUTO_MODE",
            }
        )
        return self._record(
            decision,
            result="MAX_STEPS_REACHED",
            event_action=decision.next_action,
        )

    def resume(
        self, project_id: str, *, resume_token: str | None = None
    ) -> AutoOrchestrationState:
        """Reconstruct from SQLite and continue without session-local truth."""

        current = self.repository.get_auto_orchestration_state(project_id)
        if (
            resume_token is not None
            and current is not None
            and current.resume_token is not None
            and resume_token != current.resume_token
        ):
            raise AutoOrchestratorError("resume token does not match current gate")
        return self.run_until_boundary(project_id)

    def cancel(self, project_id: str, *, reason: str = "user") -> AutoOrchestrationState:
        decision = self.next_action(project_id)
        job = self.current_state_service.select_job(project_id)
        if job is not None:
            tasks = self._job_provider_tasks(project_id, job.id)
            if any(task.state in self.ACTIVE_PROVIDER_STATES for task in tasks):
                self.production_queue.cancel_job(project_id, job.id, reason)
        cancelled = decision.model_copy(
            update={
                "status": AutoRunStatus.CANCELLED,
                "next_action": AutoAction.NONE,
                "why": "用户已取消 AUTO Mode。",
                "blocking_reason": reason[:1000],
                "requires_human": False,
                "requires_paid_authorization": False,
                "requested_action": None,
            }
        )
        return self._record(
            cancelled, result="CANCELLED", event_action=AutoAction.NONE
        )

    def preview_paid_authorization(
        self, project_id: str
    ) -> AutoPaidAuthorizationPreview:
        decision = self._plan_raw(project_id)
        if not self._requires_paid_authorization(decision):
            raise AutoOrchestratorError("current AUTO action does not require paid authorization")
        resource_key = self._resource_key(decision)
        if decision.next_action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            preview = self.production_queue.preview_authorization(
                project_id,
                resource_key,
                max_paid_attempts=1,
            )
            return AutoPaidAuthorizationPreview(
                project_id=project_id,
                action=decision.next_action,
                resource_key=resource_key,
                input_state_hash=decision.input_state_hash,
                authorization_fingerprint=preview.authorization_fingerprint,
                required_create_count=preview.estimated_provider_requests,
                per_item_max=1,
                retry_limit=0,
                provider_label=f"{preview.provider_id} / {preview.model_id}",
                details={
                    "shot_count": preview.shot_count,
                    "estimated_provider_requests": preview.estimated_provider_requests,
                    "deployment_region": preview.deployment_region,
                    "max_paid_attempts": 1,
                },
            )
        status = self._provider_status(decision.next_action)
        provider = str(getattr(status, "provider", "configured provider"))
        fingerprint = _hash(
            {
                "project_id": project_id,
                "action": decision.next_action.value,
                "resource_key": resource_key,
                "input_state_hash": decision.input_state_hash,
                "provider": provider,
                "retry_limit": 0,
                "max_creates": 1,
            }
        )
        return AutoPaidAuthorizationPreview(
            project_id=project_id,
            action=decision.next_action,
            resource_key=resource_key,
            input_state_hash=decision.input_state_hash,
            authorization_fingerprint=fingerprint,
            required_create_count=1,
            per_item_max=1,
            retry_limit=0,
            provider_label=provider,
            details={"max_creates": 1, "retry_limit": 0},
        )

    def grant_paid_authorization(
        self,
        project_id: str,
        *,
        authorization_fingerprint: str,
        global_max: int,
        per_item_max: int = 1,
        retry_limit: int = 0,
    ) -> AutoPaidAuthorization:
        """Persist an explicit, exact-input budget without executing a create."""

        preview = self.preview_paid_authorization(project_id)
        if authorization_fingerprint != preview.authorization_fingerprint:
            raise AutoOrchestratorError(
                "paid authorization fingerprint is stale; preview again"
            )
        if int(global_max) != preview.required_create_count:
            raise AutoOrchestratorError(
                "global max must equal the currently previewed create count"
            )
        if int(per_item_max) != 1 or int(retry_limit) != 0:
            raise AutoOrchestratorError(
                "AUTO V1 requires per-item max=1 and retry=0"
            )
        formal: dict[str, Any] = {}
        if preview.action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            queue_preview = self.production_queue.preview_authorization(
                project_id,
                preview.resource_key,
                max_paid_attempts=1,
            )
            formal = self._queue_authorization(queue_preview)
        now = _now()
        authorization = AutoPaidAuthorization(
            id=uuid4().hex,
            project_id=project_id,
            action=preview.action,
            resource_key=preview.resource_key,
            input_state_hash=preview.input_state_hash,
            authorization_fingerprint=preview.authorization_fingerprint,
            authorization=formal,
            global_max=preview.required_create_count,
            per_item_max=1,
            retry_limit=0,
            consumed_count=0,
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        stored = self.repository.create_auto_paid_authorization(authorization)
        if (
            stored.status != "ACTIVE"
            or stored.authorization_fingerprint != preview.authorization_fingerprint
        ):
            raise AutoOrchestratorError(
                "paid authorization is already consumed or cannot be refreshed"
            )
        return stored

    def _plan_raw(self, project_id: str) -> AutoDecision:
        self._require_project(project_id)
        snapshot = self._state_snapshot(project_id)
        state_hash = _hash(snapshot)
        persisted = self.repository.get_auto_orchestration_state(project_id)
        if persisted is not None and persisted.status is AutoRunStatus.CANCELLED:
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.CANCELLED,
                AutoStage.CREATIVE,
                AutoAction.NONE,
                "AUTO Mode 已取消。",
                blocking_reason=persisted.blocking_reason,
            )

        story = self.story_service.get_latest_revision(project_id)
        if story is None:
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.STORY,
                AutoAction.GENERATE_OR_CREATE_STORY,
                "项目还没有 Story revision。",
            )
        if story["status"] is not StoryRevisionStatus.APPROVED:
            return self._human_decision(
                project_id,
                state_hash,
                AutoStage.STORY,
                "Story draft 已生成，正式 Story gate 尚未批准。",
                "APPROVE_STORY",
                {"revision_id": story["id"]},
                completed=(),
            )

        script = self.script_service.get_latest_revision(project_id)
        if script is None or self.script_service.is_outdated(script):
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.SCRIPT,
                AutoAction.GENERATE_SCRIPT,
                "当前 approved Story 没有对应的 current Script。",
                completed=(AutoStage.STORY,),
            )
        if script["status"] is not ScriptRevisionStatus.APPROVED:
            return self._human_decision(
                project_id,
                state_hash,
                AutoStage.SCRIPT,
                "Script draft 已生成，正式 Script gate 尚未批准。",
                "APPROVE_SCRIPT",
                {"revision_id": script["id"]},
                completed=(AutoStage.STORY,),
            )

        plan = self.shot_service.get_latest_revision(project_id)
        if plan is None or self.shot_service.is_outdated(plan):
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.SHOT_PLAN,
                AutoAction.GENERATE_SHOT_PLAN,
                "当前 approved Script 没有对应的 current Shot Plan。",
                completed=(AutoStage.STORY, AutoStage.SCRIPT),
            )
        if plan["status"] is not ShotRevisionStatus.APPROVED:
            return self._human_decision(
                project_id,
                state_hash,
                AutoStage.SHOT_PLAN,
                "Shot Plan draft 已生成，Production 只接受 approved revision。",
                "APPROVE_SHOT_PLAN",
                {"revision_id": plan["id"]},
                completed=(AutoStage.STORY, AutoStage.SCRIPT),
            )

        readiness = self.production_service.validate_job_readiness(
            project_id, plan["id"]
        )
        missing = [
            (ReferenceBindingType.CHARACTER, target)
            for target in readiness["missing_character_references"]
        ] + [
            (ReferenceBindingType.LOCATION, target)
            for target in readiness["missing_location_references"]
        ]
        if missing:
            binding_type, binding_id = missing[0]
            asset = self.reference_service.find_workspace_asset(
                project_id, binding_type, binding_id
            )
            candidates = (
                self.reference_service.list_image_candidates(project_id, asset.id)
                if asset is not None
                else []
            )
            versions = (
                self.reference_service.list_versions(project_id, asset.id)
                if asset is not None
                else []
            )
            candidate = next(
                (
                    item
                    for item in reversed(candidates)
                    if item.status
                    in {
                        ReferenceImageCandidateStatus.DRAFT,
                        ReferenceImageCandidateStatus.PROMOTED,
                    }
                ),
                None,
            )
            metadata = {
                "binding_type": binding_type.value,
                "binding_id": binding_id,
                "source_story_revision_id": story["id"],
            }
            if candidate is not None:
                metadata["candidate_id"] = candidate.id
                metadata["asset_id"] = candidate.asset_id
                if candidate.promoted_version_id:
                    metadata["version_id"] = candidate.promoted_version_id
                requested_action = (
                    "PROMOTE_BIND_AND_LOCK_REFERENCE"
                    if candidate.status is ReferenceImageCandidateStatus.DRAFT
                    else "BIND_AND_LOCK_REFERENCE"
                )
                return self._human_decision(
                    project_id,
                    state_hash,
                    AutoStage.REFERENCES,
                    "Reference candidate 已生成；提升、绑定和锁定必须由人确认。",
                    requested_action,
                    metadata,
                    completed=(
                        AutoStage.STORY,
                        AutoStage.SCRIPT,
                        AutoStage.SHOT_PLAN,
                    ),
                )
            if versions:
                metadata["asset_id"] = str(asset.id)
                metadata["version_id"] = versions[-1].id
                return self._human_decision(
                    project_id,
                    state_hash,
                    AutoStage.REFERENCES,
                    "Reference version 已存在；绑定和锁定仍需要人工确认。",
                    "BIND_AND_LOCK_REFERENCE",
                    metadata,
                    completed=(
                        AutoStage.STORY,
                        AutoStage.SCRIPT,
                        AutoStage.SHOT_PLAN,
                    ),
                )
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.REFERENCES,
                AutoAction.GENERATE_REFERENCE_CANDIDATE,
                "Production readiness 显示 Reference coverage 不足。",
                completed=(
                    AutoStage.STORY,
                    AutoStage.SCRIPT,
                    AutoStage.SHOT_PLAN,
                ),
                metadata=metadata,
            )

        completed = (
            AutoStage.STORY,
            AutoStage.SCRIPT,
            AutoStage.SHOT_PLAN,
            AutoStage.REFERENCES,
        )
        job = self.current_state_service.select_job(project_id)
        if job is None or job.shot_plan_revision_id != plan["id"]:
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.PRODUCTION,
                AutoAction.PREPARE_PRODUCTION,
                "Approved inputs 已 READY，但还没有 current ProductionJob。",
                completed=completed,
                metadata={"shot_plan_revision_id": plan["id"]},
            )

        task_decision = self._provider_task_decision(
            project_id, job.id, state_hash, completed
        )
        if task_decision is not None:
            return task_decision

        shots = self.repository.list_production_shots(job.id)
        if not shots:
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.IDLE,
                AutoStage.PRODUCTION,
                AutoAction.PREPARE_PRODUCTION,
                "ProductionJob 尚未创建 formal ProductionShot records。",
                completed=completed,
                metadata={"production_job_id": job.id},
            )

        executions = self.repository.list_production_executions(job.id)
        for shot in sorted(shots, key=lambda item: (item.order_index, item.id)):
            execution = self._latest_execution_for_shot(executions, shot.shot_id)
            if execution is None:
                return self._decision(
                    project_id,
                    state_hash,
                    AutoRunStatus.IDLE,
                    AutoStage.PRODUCTION,
                    AutoAction.CREATE_PRODUCTION_EXECUTION,
                    "存在尚未生成 source 的 ProductionShot。",
                    completed=completed,
                    metadata={
                        "production_job_id": job.id,
                        "production_shot_id": shot.id,
                        "shot_id": shot.shot_id,
                    },
                )
            if execution.status in {
                ProductionExecutionStatus.QUEUED,
                ProductionExecutionStatus.RUNNING,
            }:
                return self._waiting_provider(
                    project_id,
                    state_hash,
                    "Provider execution 尚未 terminal；只允许轮询现有 task。",
                    completed,
                    {
                        "task_kind": "PROVIDER",
                        "production_job_id": job.id,
                        "execution_id": execution.id,
                    },
                )
            if execution.status in {
                ProductionExecutionStatus.FAILED,
                ProductionExecutionStatus.CANCELLED,
            }:
                return self._blocked(
                    project_id,
                    state_hash,
                    AutoStage.PRODUCTION,
                    "Provider execution 未成功，AUTO retry=0。",
                    f"EXECUTION_{execution.status.value}",
                    completed,
                    {"execution_id": execution.id},
                )

            artifacts = self.execution_service.list_artifacts(
                project_id, execution.id
            )
            if not artifacts:
                return self._blocked(
                    project_id,
                    state_hash,
                    AutoStage.PRODUCTION,
                    "Succeeded execution 没有物理 artifact。",
                    "MISSING_PRODUCTION_ARTIFACT",
                    completed,
                    {"execution_id": execution.id},
                )
            artifact = artifacts[-1]
            results = [
                item
                for item in self.qc_service.list_results(project_id, execution.id)
                if item.artifact_id == artifact.id
            ]
            if not results:
                return self._decision(
                    project_id,
                    state_hash,
                    AutoRunStatus.IDLE,
                    AutoStage.QC,
                    AutoAction.RUN_TECHNICAL_QC,
                    "Production artifact 尚无 technical QC result。",
                    completed=completed + (AutoStage.PRODUCTION,),
                    metadata={
                        "execution_id": execution.id,
                        "artifact_id": artifact.id,
                    },
                )
            qc = results[-1]
            if qc.status in {
                ProductionQCStatus.QC_PENDING,
                ProductionQCStatus.QC_RUNNING,
            }:
                return self._waiting_provider(
                    project_id,
                    state_hash,
                    "Technical QC 正在运行。",
                    completed + (AutoStage.PRODUCTION,),
                    {"task_kind": "QC", "qc_result_id": qc.id},
                    stage=AutoStage.QC,
                )
            if qc.status is ProductionQCStatus.QC_FAILED:
                return self._blocked(
                    project_id,
                    state_hash,
                    AutoStage.QC,
                    "Technical QC 未通过，AUTO retry=0。",
                    "TECHNICAL_QC_FAILED",
                    completed + (AutoStage.PRODUCTION,),
                    {"qc_result_id": qc.id},
                )
            if self._vision_configured() and not self.repository.list_vision_analyses(
                project_id, execution.id
            ):
                return self._decision(
                    project_id,
                    state_hash,
                    AutoRunStatus.IDLE,
                    AutoStage.QC,
                    AutoAction.RUN_OPTIONAL_VISION_QC,
                    "Vision 已配置，且当前 execution 尚未运行 optional Vision QC。",
                    completed=completed + (AutoStage.PRODUCTION,),
                    metadata={
                        "execution_id": execution.id,
                        "artifact_id": artifact.id,
                    },
                )
            reviews = self.qc_service.list_reviews(project_id, qc.id)
            latest_review = reviews[-1] if reviews else None
            if latest_review is None:
                return self._human_decision(
                    project_id,
                    state_hash,
                    AutoStage.REVIEW,
                    "Technical QC 已通过；Human Review 是不可跨越的正式 gate。",
                    "APPROVE_OR_REJECT_PRODUCTION_REVIEW",
                    {
                        "qc_result_id": qc.id,
                        "execution_id": execution.id,
                        "artifact_id": artifact.id,
                        "shot_id": shot.shot_id,
                    },
                    completed=completed
                    + (AutoStage.PRODUCTION, AutoStage.QC),
                )
            if latest_review.decision is not ProductionReviewDecision.APPROVED:
                return self._blocked(
                    project_id,
                    state_hash,
                    AutoStage.REVIEW,
                    "Human Review 没有批准 current source。",
                    f"HUMAN_REVIEW_{latest_review.decision.value}",
                    completed + (AutoStage.PRODUCTION, AutoStage.QC),
                    {"review_id": latest_review.id},
                )

        final = self._final_decision(project_id, job.id, state_hash, completed)
        if final is not None:
            return final
        return self._decision(
            project_id,
            state_hash,
            AutoRunStatus.IDLE,
            AutoStage.FINAL,
            AutoAction.FINAL_ASSEMBLY,
            "所有 current sources 已通过 QC 与 Human Review。",
            completed=completed
            + (AutoStage.PRODUCTION, AutoStage.QC, AutoStage.REVIEW),
            metadata={"production_job_id": job.id},
        )

    def _final_decision(
        self,
        project_id: str,
        job_id: str,
        state_hash: str,
        completed: tuple[AutoStage, ...],
    ) -> AutoDecision | None:
        assemblies = self.final_service.list_assemblies(project_id, job_id)
        if not assemblies:
            return None
        assembly = assemblies[-1]
        if assembly.status is FinalAssemblyStatus.SUCCEEDED:
            return self._decision(
                project_id,
                state_hash,
                AutoRunStatus.SUCCEEDED,
                AutoStage.COMPLETED,
                AutoAction.NONE,
                "Final Assembly 已成功。",
                completed=completed
                + (
                    AutoStage.PRODUCTION,
                    AutoStage.QC,
                    AutoStage.REVIEW,
                    AutoStage.FINAL,
                    AutoStage.COMPLETED,
                ),
                metadata={"final_assembly_id": assembly.id},
            )
        jobs = self._assembly_heavy_jobs(project_id, assembly.id)
        active = next(
            (
                item
                for item in reversed(jobs)
                if item.status.value in self.ACTIVE_HEAVY_STATES
            ),
            None,
        )
        if active is not None:
            return self._waiting_provider(
                project_id,
                state_hash,
                "Final Assembly heavy job 尚未 terminal。",
                completed + (AutoStage.PRODUCTION, AutoStage.QC, AutoStage.REVIEW),
                {
                    "task_kind": "HEAVY",
                    "heavy_job_id": active.id,
                    "final_assembly_id": assembly.id,
                },
                stage=AutoStage.FINAL,
            )
        terminal_failure = next(
            (
                item
                for item in reversed(jobs)
                if item.status
                in {HeavyJobStatus.FAILED, HeavyJobStatus.CANCELLED}
            ),
            None,
        )
        if terminal_failure is not None:
            return self._blocked(
                project_id,
                state_hash,
                AutoStage.FINAL,
                "Final Assembly heavy job 未成功，AUTO retry=0。",
                f"FINAL_{terminal_failure.status.value}",
                completed + (AutoStage.PRODUCTION, AutoStage.QC, AutoStage.REVIEW),
                {"heavy_job_id": terminal_failure.id},
            )
        return self._decision(
            project_id,
            state_hash,
            AutoRunStatus.IDLE,
            AutoStage.FINAL,
            AutoAction.FINAL_ASSEMBLY,
            "Final Assembly manifest 已存在，尚未完成正式 render。",
            completed=completed
            + (AutoStage.PRODUCTION, AutoStage.QC, AutoStage.REVIEW),
            metadata={
                "production_job_id": job_id,
                "final_assembly_id": assembly.id,
            },
        )

    def _provider_task_decision(
        self,
        project_id: str,
        job_id: str,
        state_hash: str,
        completed: tuple[AutoStage, ...],
    ) -> AutoDecision | None:
        tasks = self._job_provider_tasks(project_id, job_id)
        uncertain = next(
            (
                task
                for task in reversed(tasks)
                if task.state == "UNCERTAIN_CREATE"
            ),
            None,
        )
        if uncertain is not None:
            return self._blocked(
                project_id,
                state_hash,
                AutoStage.PRODUCTION,
                "UNCERTAIN_CREATE: provider create 结果未知；禁止 retry 或创建第二个 paid task。",
                "UNCERTAIN_CREATE_RECONCILIATION_REQUIRED",
                completed,
                {
                    "provider_task_record_id": uncertain.id,
                    "production_job_id": job_id,
                    "reconciliation_required": True,
                },
            )
        active = next(
            (
                task
                for task in reversed(tasks)
                if task.state in self.ACTIVE_PROVIDER_STATES
            ),
            None,
        )
        if active is not None:
            return self._waiting_provider(
                project_id,
                state_hash,
                "Production provider task 已存在；只轮询，不创建第二个 paid task。",
                completed,
                {
                    "task_kind": "PROVIDER",
                    "provider_task_record_id": active.id,
                    "production_job_id": job_id,
                },
            )
        failed = next(
            (
                task
                for task in reversed(tasks)
                if task.state in {"FAILED", "CANCELLED"}
            ),
            None,
        )
        job = self.repository.get_production_job(job_id)
        if failed is not None and job is not None and job.status.value != "SUCCEEDED":
            return self._blocked(
                project_id,
                state_hash,
                AutoStage.PRODUCTION,
                "Production provider task 未成功，AUTO retry=0。",
                f"PROVIDER_TASK_{failed.state}",
                completed,
                {"provider_task_record_id": failed.id},
            )
        return None

    def _apply_paid_gate(self, decision: AutoDecision) -> AutoDecision:
        if not self._requires_paid_authorization(decision):
            return decision
        resource_key = self._resource_key(decision)
        authorization = self.repository.find_auto_paid_authorization(
            decision.project_id,
            decision.next_action,
            resource_key,
            decision.input_state_hash,
            include_inactive=True,
        )
        preview = self.preview_paid_authorization(decision.project_id)
        if authorization is not None and authorization.status != "ACTIVE":
            return self._blocked(
                decision.project_id,
                decision.input_state_hash,
                decision.current_stage,
                "当前精确输入的付费预算已消费；AUTO V1 不允许自动重试。",
                "PAID_AUTHORIZATION_BUDGET_EXHAUSTED",
                decision.completed_stages,
                decision.metadata,
            )
        if (
            authorization is not None
            and authorization.authorization_fingerprint
            != preview.authorization_fingerprint
        ):
            authorization = None
        if authorization is not None:
            required = preview.required_create_count
            remaining = authorization.global_max - authorization.consumed_count
            if (
                authorization.per_item_max == 1
                and authorization.retry_limit == 0
                and remaining >= required
            ):
                return decision
            return self._blocked(
                decision.project_id,
                decision.input_state_hash,
                decision.current_stage,
                "已授权预算不足或不满足 V1 retry=0 边界。",
                "PAID_AUTHORIZATION_BUDGET_EXHAUSTED",
                decision.completed_stages,
                decision.metadata,
            )
        metadata = dict(decision.metadata)
        metadata.update(
            {
                "authorized_action": decision.next_action.value,
                "resource_key": resource_key,
                "retry_limit": 0,
                "per_item_max": 1,
            }
        )
        return decision.model_copy(
            update={
                "status": AutoRunStatus.WAITING_HUMAN,
                "next_action": AutoAction.PAID_AUTHORIZATION_REQUIRED,
                "why": "当前 create 需要显式、输入绑定且有上限的付费授权。",
                "blocking_reason": "PAID_AUTHORIZATION_REQUIRED",
                "requires_human": True,
                "requires_paid_authorization": True,
                "requested_action": "AUTHORIZE_BOUNDED_PAID_CREATE",
                "metadata": metadata,
            }
        )

    def _execute(self, decision: AutoDecision) -> str:
        project = self._require_project(decision.project_id)
        action = decision.next_action
        paid = None
        if self._requires_paid_authorization(decision):
            paid = self.repository.find_auto_paid_authorization(
                decision.project_id,
                action,
                self._resource_key(decision),
                decision.input_state_hash,
            )
            if paid is None or paid.status != "ACTIVE":
                raise AutoOrchestratorError("PAID_AUTHORIZATION_REQUIRED")

        if action is AutoAction.GENERATE_OR_CREATE_STORY:
            normalized = self.repository.list_normalized_creative_briefs(
                decision.project_id
            )
            brief = normalized[-1] if normalized else None
            revision = self.story_service.generate_story_bible(
                project,
                brief=str(
                    getattr(brief, "premise", "")
                    or project.description
                    or project.title
                ),
                genre=str(getattr(brief, "genre", "") or "Drama"),
                tone=str(getattr(brief, "tone", "") or "Cinematic"),
                creative_constraints="; ".join(
                    str(item) for item in getattr(brief, "constraints", ())
                ),
                source_ids=tuple(getattr(brief, "source_ids", ())),
                normalized_brief_id=getattr(brief, "id", None),
            )
            self._consume_generic(paid, f"story:{revision['id']}")
            return "STORY_REVISION_CREATED"
        if action is AutoAction.GENERATE_SCRIPT:
            revision = self.script_service.generate_script(project)
            self._consume_generic(paid, f"script:{revision['id']}")
            return "SCRIPT_REVISION_CREATED"
        if action is AutoAction.GENERATE_SHOT_PLAN:
            revision = self.shot_service.generate_shot_plan(project)
            self._consume_generic(paid, f"shot-plan:{revision['id']}")
            return "SHOT_PLAN_REVISION_CREATED"
        if action is AutoAction.GENERATE_REFERENCE_CANDIDATE:
            binding_type = ReferenceBindingType(decision.metadata["binding_type"])
            binding_id = str(decision.metadata["binding_id"])
            asset = self.reference_service.ensure_workspace_asset(
                decision.project_id, binding_type, binding_id
            )
            prompt = self._reference_prompt(
                decision.project_id, binding_type, binding_id
            )
            candidate = self.image_runtime.generate_and_record_candidate(
                decision.project_id,
                asset.id,
                prompt,
                source_story_revision_id=str(
                    decision.metadata["source_story_revision_id"]
                ),
                filename=f"{binding_type.value.lower()}-{binding_id}.png",
                actor=self.actor,
                reference_assets=self.reference_service,
            )
            self._consume_generic(paid, f"reference-candidate:{candidate.id}")
            return "REFERENCE_CANDIDATE_CREATED"
        if action is AutoAction.PREPARE_PRODUCTION:
            job = self.current_state_service.select_job(decision.project_id)
            plan_id = str(decision.metadata.get("shot_plan_revision_id") or "")
            if job is None or job.shot_plan_revision_id != plan_id:
                job = self.production_service.create_production_job(
                    decision.project_id, plan_id or None
                )
            self.production_service.create_production_shots(
                decision.project_id, job.id
            )
            self.production_queue.prepare_generation_briefs(
                decision.project_id, job.id
            )
            return "PRODUCTION_PREPARED"
        if action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            if paid is None:
                raise AutoOrchestratorError("PAID_AUTHORIZATION_REQUIRED")
            task = self.production_queue.enqueue_job(
                decision.project_id,
                self._resource_key(decision),
                authorization=paid.authorization,
            )
            self.repository.consume_auto_paid_authorization(
                paid.id,
                f"provider-task:{task.id}",
                int(task.request_summary.get("estimated_provider_requests") or 1),
                consumption_id=uuid4().hex,
                created_at=_now(),
            )
            return "PRODUCTION_TASK_QUEUED"
        if action is AutoAction.POLL_EXISTING_TASK:
            kind = str(decision.metadata.get("task_kind") or "")
            if kind == "HEAVY":
                self.heavy_runner.reconcile()
                heavy_job_id = str(decision.metadata.get("heavy_job_id") or "")
                heavy_job = (
                    self.repository.get_heavy_job(heavy_job_id)
                    if heavy_job_id
                    else None
                )
                if (
                    heavy_job is not None
                    and heavy_job.status is HeavyJobStatus.INTERRUPTED
                ):
                    self.heavy_jobs.retry(heavy_job.id)
                self.heavy_runner.run_once(decision.project_id)
                return "HEAVY_JOB_POLLED"
            if kind == "PROVIDER":
                self.background_runner.reconcile(decision.project_id)
                self.background_runner.run_once(decision.project_id)
                return "PROVIDER_TASK_POLLED"
            return "PERSISTED_TASK_STILL_RUNNING"
        if action is AutoAction.RUN_TECHNICAL_QC:
            result = self.qc_service.run_qc(
                decision.project_id,
                str(decision.metadata["execution_id"]),
                str(decision.metadata["artifact_id"]),
            )
            return f"TECHNICAL_QC_{result.status.value}"
        if action is AutoAction.RUN_OPTIONAL_VISION_QC:
            result = self.vision_service.analyze(
                decision.project_id,
                str(decision.metadata["execution_id"]),
                str(decision.metadata["artifact_id"]),
            )
            self._consume_generic(
                paid, f"vision-analysis:{getattr(result, 'analysis_id', 'recorded')}"
            )
            return f"VISION_QC_{result.status}"
        if action is AutoAction.FINAL_ASSEMBLY:
            job_id = str(decision.metadata["production_job_id"])
            assemblies = self.final_service.list_assemblies(
                decision.project_id, job_id
            )
            assembly = assemblies[-1] if assemblies else None
            if assembly is None:
                assembly = self.final_service.create_assembly(
                    decision.project_id, job_id, freeze=True
                )
            elif assembly.status is FinalAssemblyStatus.DRAFT:
                assembly = self.final_service.freeze_manifest(
                    decision.project_id, assembly.id
                )
            if assembly.status is FinalAssemblyStatus.READY:
                existing = self._assembly_heavy_jobs(
                    decision.project_id, assembly.id
                )
                if not existing:
                    self.heavy_jobs.enqueue_final_assembly(
                        decision.project_id,
                        assembly.id,
                        idempotency_key=f"auto-final-assembly:{assembly.id}",
                    )
            return "FINAL_ASSEMBLY_QUEUED"
        raise AutoOrchestratorError(f"unsupported AUTO action: {action.value}")

    def _record(
        self,
        decision: AutoDecision,
        *,
        result: str,
        event_action: AutoAction,
    ) -> AutoOrchestrationState:
        now = _now()
        previous = self.repository.get_auto_orchestration_state(decision.project_id)
        resume_token = None
        if decision.requires_human:
            if (
                previous is not None
                and previous.input_state_hash == decision.input_state_hash
                and previous.requested_action == decision.requested_action
                and previous.resume_token
            ):
                resume_token = previous.resume_token
            else:
                resume_token = uuid4().hex
        safe_metadata = sanitize_persistent_metadata(decision.metadata)
        metadata = safe_metadata if isinstance(safe_metadata, dict) else {}
        state = AutoOrchestrationState(
            **decision.model_dump(exclude={"metadata", "resume_token"}),
            metadata=metadata,
            resume_token=resume_token,
            created_at=previous.created_at if previous else now,
            updated_at=now,
            last_result=result[:160],
            actor=self.actor,
            state_version=(previous.state_version + 1 if previous else 1),
        )
        event = AutoAgentEvent(
            id=uuid4().hex,
            project_id=decision.project_id,
            sequence_number=1,
            decision=decision.next_action.value,
            action=event_action.value,
            reason=decision.why,
            input_state_hash=decision.input_state_hash,
            result=result[:160],
            timestamp=now,
            actor=self.actor,
        )
        stored, _ = self.repository.record_auto_transition(state, event)
        return stored

    def _state_snapshot(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        stories = self.repository.list_story_revisions(project_id)
        scripts = self.repository.list_script_revisions(project_id)
        plans = self.repository.list_shot_revisions(project_id)
        references = self.repository.list_reference_assets(project_id)
        reference_bindings = self.repository.list_reference_bindings(project_id)
        jobs = self.repository.list_production_jobs(project_id)
        job_facts: list[dict[str, Any]] = []
        for job in jobs:
            executions = self.repository.list_production_executions(job.id)
            production_shots = self.repository.list_production_shots(job.id)
            generation_briefs = self.repository.list_generation_briefs(
                project_id, job.id
            )
            job_facts.append(
                {
                    "id": job.id,
                    "plan": job.shot_plan_revision_id,
                    "output_profile_id": job.output_profile_id,
                    "status": job.status.value,
                    "shots": [
                        {"id": item.id, "shot_id": item.shot_id, "status": item.status.value}
                        for item in production_shots
                    ],
                    "generation_briefs": [
                        {
                            "id": item.id,
                            "shot_id": item.shot_id,
                            "sha256": item.sha256,
                        }
                        for item in generation_briefs
                    ],
                    "selected_generation_briefs": [
                        {
                            "shot_id": item.shot_id,
                            "brief_id": selected.id if selected is not None else None,
                            "sha256": selected.sha256 if selected is not None else None,
                        }
                        for item in production_shots
                        for selected in (
                            self.repository.get_selected_generation_brief(
                                project_id, job.id, item.shot_id
                            ),
                        )
                    ],
                    "executions": [
                        {
                            "id": item.id,
                            "status": item.status.value,
                            "shots": sorted(
                                (item.input_snapshot.shot_parameters if item.input_snapshot else {}).keys()
                            ),
                            "artifacts": [
                                artifact.id
                                for artifact in self.repository.list_production_artifacts(item.id)
                            ],
                            "qc": [
                                {
                                    "id": result.id,
                                    "artifact": result.artifact_id,
                                    "status": result.status.value,
                                    "reviews": [
                                        review.decision.value
                                        for review in self.repository.list_production_reviews(
                                            project_id, result.id
                                        )
                                    ],
                                }
                                for result in self.repository.list_production_qc_results(
                                    project_id, item.id
                                )
                            ],
                            "vision": [
                                record.status
                                for record in self.repository.list_vision_analyses(
                                    project_id, item.id
                                )
                            ],
                        }
                        for item in executions
                    ],
                    "provider_tasks": [
                        {"id": task.id, "state": task.state}
                        for task in self._job_provider_tasks(project_id, job.id)
                    ],
                    "assemblies": [
                        {
                            "id": assembly.id,
                            "status": assembly.status.value,
                            "heavy": [
                                {"id": heavy.id, "status": heavy.status.value}
                                for heavy in self._assembly_heavy_jobs(
                                    project_id, assembly.id
                                )
                            ],
                        }
                        for assembly in self.repository.list_final_assemblies(
                            project_id, job.id
                        )
                    ],
                }
            )
        return {
            "project_id": project_id,
            "project": {
                "title": project.title,
                "description": project.description,
                "aspect_ratio": project.aspect_ratio.value,
                "target_duration_seconds": project.target_duration_seconds,
            },
            "stories": [
                {"id": item["id"], "status": item["status"].value}
                for item in stories
            ],
            "scripts": [
                {
                    "id": item["id"],
                    "source": item["source_story_revision_id"],
                    "status": item["status"].value,
                }
                for item in scripts
            ],
            "shot_plans": [
                {
                    "id": item["id"],
                    "source": item["source_script_revision_id"],
                    "status": item["status"].value,
                }
                for item in plans
            ],
            "references": [
                {
                    "id": item.id,
                    "current": item.current_version_id,
                    "versions": [
                        version.id
                        for version in self.repository.list_reference_asset_versions(
                            item.id
                        )
                    ],
                    "candidates": [
                        {"id": candidate.id, "status": candidate.status.value}
                        for candidate in self.reference_service.list_image_candidates(
                            project_id, item.id
                        )
                    ],
                }
                for item in references
            ],
            "reference_bindings": [
                {
                    "version_id": item.asset_version_id,
                    "binding_type": item.binding_type.value,
                    "binding_id": item.binding_id,
                }
                for item in reference_bindings
            ],
            "jobs": job_facts,
        }

    def _decision(
        self,
        project_id: str,
        state_hash: str,
        status: AutoRunStatus,
        stage: AutoStage,
        action: AutoAction,
        why: str,
        *,
        blocking_reason: str | None = None,
        requires_human: bool = False,
        requested_action: str | None = None,
        completed: tuple[AutoStage, ...] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> AutoDecision:
        return AutoDecision(
            project_id=project_id,
            status=status,
            current_stage=stage,
            next_action=action,
            why=why,
            blocking_reason=blocking_reason,
            requires_human=requires_human,
            requires_paid_authorization=False,
            requested_action=requested_action,
            completed_stages=tuple(dict.fromkeys(completed)),
            input_state_hash=state_hash,
            metadata=dict(metadata or {}),
        )

    def _human_decision(
        self,
        project_id: str,
        state_hash: str,
        stage: AutoStage,
        why: str,
        requested_action: str,
        metadata: Mapping[str, Any],
        *,
        completed: tuple[AutoStage, ...],
    ) -> AutoDecision:
        return self._decision(
            project_id,
            state_hash,
            AutoRunStatus.WAITING_HUMAN,
            stage,
            AutoAction.WAITING_HUMAN,
            why,
            blocking_reason=requested_action,
            requires_human=True,
            requested_action=requested_action,
            completed=completed,
            metadata=metadata,
        )

    def _waiting_provider(
        self,
        project_id: str,
        state_hash: str,
        why: str,
        completed: tuple[AutoStage, ...],
        metadata: Mapping[str, Any],
        *,
        stage: AutoStage = AutoStage.PRODUCTION,
    ) -> AutoDecision:
        return self._decision(
            project_id,
            state_hash,
            AutoRunStatus.WAITING_PROVIDER,
            stage,
            AutoAction.POLL_EXISTING_TASK,
            why,
            completed=completed,
            metadata=metadata,
        )

    def _blocked(
        self,
        project_id: str,
        state_hash: str,
        stage: AutoStage,
        why: str,
        reason: str,
        completed: tuple[AutoStage, ...],
        metadata: Mapping[str, Any],
    ) -> AutoDecision:
        return self._decision(
            project_id,
            state_hash,
            AutoRunStatus.BLOCKED,
            stage,
            AutoAction.NONE,
            why,
            blocking_reason=reason,
            requires_human=True,
            requested_action="INSPECT_BLOCKER",
            completed=completed,
            metadata=metadata,
        )

    def _requires_paid_authorization(self, decision: AutoDecision) -> bool:
        if decision.next_action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            return True
        status = self._provider_status(decision.next_action)
        if status is None:
            return False
        metadata = dict(getattr(status, "metadata", {}) or {})
        return bool(
            getattr(status, "authorization_required", False) is True
            or metadata.get("create_is_paid") is True
        )

    def _provider_status(self, action: AutoAction):
        provider = None
        if action in {
            AutoAction.GENERATE_OR_CREATE_STORY,
            AutoAction.GENERATE_SCRIPT,
            AutoAction.GENERATE_SHOT_PLAN,
        }:
            gateway = getattr(self.story_service, "_llm_gateway", None)
            registry = getattr(gateway, "registry", None)
            if registry is not None:
                try:
                    provider = registry.get("LLM")
                except Exception:
                    provider = None
        elif action is AutoAction.GENERATE_REFERENCE_CANDIDATE:
            provider = getattr(self.image_runtime, "provider", None)
        elif action is AutoAction.RUN_OPTIONAL_VISION_QC:
            provider = getattr(self.vision_service, "provider", None)
        if provider is None:
            return None
        try:
            return provider.status
        except Exception:
            return None

    def _vision_configured(self) -> bool:
        status = self._provider_status(AutoAction.RUN_OPTIONAL_VISION_QC)
        return bool(
            status is not None
            and getattr(status, "configured", False) is True
            and getattr(status, "available", False) is True
        )

    def _required_create_count(self, decision: AutoDecision) -> int:
        if decision.next_action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            try:
                return self.production_queue.preview_authorization(
                    decision.project_id, self._resource_key(decision), max_paid_attempts=1
                ).estimated_provider_requests
            except Exception:
                shots = self.repository.list_production_shots(
                    self._resource_key(decision)
                )
                return max(1, len(shots))
        return 1

    @staticmethod
    def _resource_key(decision: AutoDecision) -> str:
        if decision.next_action is AutoAction.CREATE_PRODUCTION_EXECUTION:
            value = decision.metadata.get("production_job_id")
        else:
            value = (
                decision.metadata.get("binding_id")
                or decision.metadata.get("execution_id")
                or decision.next_action.value
            )
        text = str(value or "").strip()
        if not text:
            raise AutoOrchestratorError("AUTO paid action has no resource key")
        return text

    @staticmethod
    def _queue_authorization(
        preview: ProductionAuthorizationPreview,
    ) -> dict[str, Any]:
        return {
            "approved": True,
            "provider_id": preview.provider_id,
            "model_id": preview.model_id,
            "max_paid_attempts": 1,
            "estimated_provider_requests": preview.estimated_provider_requests,
            "deployment_region": preview.deployment_region,
            "endpoint_profile_id": preview.endpoint_profile_id,
            "endpoint_class": preview.endpoint_class,
            "reference_count": preview.reference_count,
            "target_episode_duration_seconds": preview.target_episode_duration_seconds,
            "native_generation_resolution": preview.native_generation_resolution,
            "native_generation_fps": preview.native_generation_fps,
            "delivery_resolution": preview.delivery_resolution,
            "target_fps": preview.target_fps,
            "delivery_strategy": preview.delivery_strategy,
            "quality_mode": preview.quality_mode,
            "authorization_fingerprint": preview.authorization_fingerprint,
        }

    def _consume_generic(
        self, authorization: AutoPaidAuthorization | None, operation_key: str
    ) -> None:
        if authorization is None:
            return
        self.repository.consume_auto_paid_authorization(
            authorization.id,
            operation_key,
            1,
            consumption_id=uuid4().hex,
            created_at=_now(),
        )

    def _job_provider_tasks(self, project_id: str, job_id: str):
        execution_ids = {
            item.id for item in self.repository.list_production_executions(job_id)
        }
        return [
            task
            for task in self.repository.list_provider_tasks(project_id)
            if (
                task.execution_id in execution_ids
                or task.execution_id is None
                and task.request_summary.get("production_job_id") == job_id
            )
        ]

    def _assembly_heavy_jobs(self, project_id: str, assembly_id: str):
        return [
            job
            for job in self.repository.list_heavy_jobs(project_id)
            if job.input_snapshot.get("assembly_id") == assembly_id
        ]

    @staticmethod
    def _latest_execution_for_shot(executions, shot_id: str):
        matches = [
            execution
            for execution in executions
            if execution.input_snapshot is not None
            and shot_id in execution.input_snapshot.shot_parameters
        ]
        return matches[-1] if matches else None

    def _reference_prompt(
        self,
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
    ) -> str:
        story = self.story_service.get_latest_revision(project_id)
        if story is None:
            raise AutoOrchestratorError("approved Story is required for references")
        subjects = (
            story["content"].characters
            if binding_type is ReferenceBindingType.CHARACTER
            else story["content"].locations
        )
        subject = next((item for item in subjects if item.id == binding_id), None)
        if subject is None:
            raise AutoOrchestratorError("reference target is absent from Story")
        description = str(
            getattr(subject, "description", "")
            or getattr(subject, "visual_description", "")
            or getattr(subject, "name", binding_id)
        )
        return (
            f"Create a production reference image for {binding_type.value.lower()} "
            f"{getattr(subject, 'name', binding_id)}. {description}"
        )

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise AutoOrchestratorError(f"project does not exist: {project_id}")
        return project


__all__ = ["AutoOrchestratorError", "AutoOrchestratorService"]
