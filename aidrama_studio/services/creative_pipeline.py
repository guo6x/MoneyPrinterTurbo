"""Formal upstream Story -> Script -> Shot Plan product activity pipeline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from aidrama_studio.domain import (
    CreativePipelineOperation,
    CreativePipelineOperationStatus,
    CreativePipelineStage,
    ScriptRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService
from aidrama_studio.services.security import sanitize_error
from aidrama_studio.storage import ProjectRepository


class CreativePipelineError(RuntimeError):
    """Safe error returned by the product activity adapter."""


_OPERATION_ALIASES = {
    "GENERATE_STORY": "GENERATE_STORY",
    "STORY_BIBLE_GENERATION": "GENERATE_STORY",
    "GENERATE_SCRIPT": "GENERATE_SCRIPT",
    "STRUCTURED_SCRIPT_GENERATION": "GENERATE_SCRIPT",
    "GENERATE_SHOT_PLAN": "GENERATE_SHOT_PLAN",
    "SHOT_PLAN_GENERATION": "GENERATE_SHOT_PLAN",
}

_OPERATION_DETAILS = {
    "GENERATE_STORY": (
        CreativePipelineStage.STORY_GENERATION,
        "aidrama-story-bible-v1",
    ),
    "GENERATE_SCRIPT": (
        CreativePipelineStage.SCRIPT_GENERATION,
        "aidrama-structured-script-v1",
    ),
    "GENERATE_SHOT_PLAN": (
        CreativePipelineStage.SHOT_PLAN_GENERATION,
        "aidrama-shot-plan-v1",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CreativePipelineService:
    """Execute explicit product actions while preserving approval/version gates."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        story_service: StoryService | None = None,
        script_service: ScriptService | None = None,
        shot_service: ShotService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.story_service = story_service or StoryService(self.repository)
        self.script_service = script_service or ScriptService(self.repository)
        self.shot_service = shot_service or ShotService(self.repository)

    def execute(
        self,
        *,
        project_id: str,
        operation: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        project = self.repository.get_project(project_id)
        if project is None:
            raise CreativePipelineError("项目不存在")
        normalized_operation = _OPERATION_ALIASES.get(str(operation or "").strip().upper())
        if normalized_operation is None:
            raise CreativePipelineError("当前产品活动类型尚未连接")
        values = dict(payload or {})
        stage, prompt_template_version = _OPERATION_DETAILS[normalized_operation]
        inputs, service_kwargs = self._resolve_inputs(
            project_id, normalized_operation, values
        )

        regenerate = values.get("regenerate") is True
        intent = {
            "operation": normalized_operation,
            "input_revision_ids": list(inputs),
            "project_target_duration_seconds": project.target_duration_seconds,
            "project_aspect_ratio": project.aspect_ratio.value,
        }
        if regenerate:
            # A new version is permitted only by an explicit regenerate action.
            intent["regeneration_intent_id"] = str(
                values.get("operation_intent_id") or uuid4().hex
            )
        input_hash = _hash(intent)
        existing = self.repository.get_creative_pipeline_operation_by_input(
            project_id, normalized_operation, input_hash
        )
        if existing is not None:
            return self._idempotent_result(existing, normalized_operation)

        now = _now()
        activity = CreativePipelineOperation(
            id=uuid4().hex,
            project_id=project_id,
            operation=normalized_operation,
            stage=stage,
            status=CreativePipelineOperationStatus.RUNNING,
            input_hash=input_hash,
            input_revision_ids=inputs,
            prompt_template_version=prompt_template_version,
            created_at=now,
            started_at=now,
            updated_at=now,
        )
        try:
            activity = self.repository.create_creative_pipeline_operation(activity)
        except sqlite3.IntegrityError:
            raced = self.repository.get_creative_pipeline_operation_by_input(
                project_id, normalized_operation, input_hash
            )
            if raced is not None:
                return self._idempotent_result(raced, normalized_operation)
            raise

        provenance = {
            "creative_pipeline_operation_id": activity.id,
            "input_hash": input_hash,
            "input_revision_ids": list(inputs),
            "prompt_template_version": prompt_template_version,
        }
        try:
            revision = self._run_generation(
                normalized_operation,
                project,
                service_kwargs,
                provenance,
            )
        except Exception as exc:
            failure = sanitize_error(exc, max_length=1000) or "Creative AI generation failed"
            self.repository.finish_creative_pipeline_operation(
                activity.id,
                status=CreativePipelineOperationStatus.FAILED,
                updated_at=_now(),
                failure_reason=failure,
            )
            raise CreativePipelineError(failure) from exc

        provider_id, model_id = self._invocation_identity(project_id, activity.id)
        completed = self.repository.finish_creative_pipeline_operation(
            activity.id,
            status=CreativePipelineOperationStatus.WAITING_HUMAN,
            updated_at=_now(),
            output_revision_id=str(revision["id"]),
            provider_id=provider_id,
            model_id=model_id,
        )
        return self._result(revision, completed)

    def _resolve_inputs(
        self,
        project_id: str,
        operation: str,
        payload: Mapping[str, object],
    ) -> tuple[tuple[str, ...], dict[str, object]]:
        if operation == "GENERATE_STORY":
            brief_id = str(payload.get("normalized_brief_id") or "").strip()
            brief = (
                self.repository.get_normalized_creative_brief(brief_id)
                if brief_id
                else next(
                    (
                        item
                        for item in reversed(
                            self.repository.list_normalized_creative_briefs(project_id)
                        )
                        if item.status == "APPROVED"
                    ),
                    None,
                )
            )
            if brief is None or brief.project_id != project_id:
                raise CreativePipelineError("Story AI 缺少当前项目已确认的 Creative Intake")
            if brief.status != "APPROVED":
                raise CreativePipelineError("Story AI 必须使用 APPROVED Creative Intake")
            if not brief.premise.strip() or not brief.source_ids:
                raise CreativePipelineError("已确认 Creative Intake 缺少故事输入或来源")
            return (
                (brief.id, *tuple(brief.source_ids)),
                {
                    "brief": brief.premise,
                    "genre": brief.genre,
                    "tone": brief.tone,
                    "target_audience": str(brief.story_information.get("audience") or ""),
                    "creative_constraints": "\n".join(brief.constraints),
                    "source_ids": brief.source_ids,
                    "normalized_brief_id": brief.id,
                },
            )

        if operation == "GENERATE_SCRIPT":
            story_id = str(payload.get("source_story_revision_id") or "").strip()
            story = self.repository.get_story_revision(story_id) if story_id else None
            if story is None or story["project_id"] != project_id:
                raise CreativePipelineError("Script AI 缺少指定 Story Bible revision")
            if story["status"] is not StoryRevisionStatus.APPROVED:
                raise CreativePipelineError("Script AI 必须使用 APPROVED Story Bible")
            return (
                (story["id"],),
                {
                    "source_story_revision_id": story["id"],
                    "dialogue_density": str(payload.get("dialogue_density") or "standard"),
                    "narration": str(payload.get("narration") or "少量"),
                    "pacing": str(payload.get("pacing") or "standard"),
                },
            )

        script_id = str(payload.get("source_script_revision_id") or "").strip()
        script = self.repository.get_script_revision(script_id) if script_id else None
        if script is None or script["project_id"] != project_id:
            raise CreativePipelineError("Shot Planner AI 缺少指定 Structured Script revision")
        if script["status"] is not ScriptRevisionStatus.APPROVED:
            raise CreativePipelineError("Shot Planner AI 必须使用 APPROVED Structured Script")
        story = self.repository.get_story_revision(script["source_story_revision_id"])
        if story is None or story["project_id"] != project_id:
            raise CreativePipelineError("Shot Planner AI 缺少 Script 的 Story provenance")
        return (
            (script["id"], story["id"]),
            {"source_script_revision_id": script["id"]},
        )

    def _run_generation(
        self,
        operation: str,
        project,
        kwargs: Mapping[str, object],
        provenance: Mapping[str, object],
    ) -> dict[str, object]:
        if operation == "GENERATE_STORY":
            return self.story_service.generate_story_bible(
                project,
                generation_provenance=provenance,
                **kwargs,
            )
        if operation == "GENERATE_SCRIPT":
            return self.script_service.generate_script(
                project,
                generation_provenance=provenance,
                **kwargs,
            )
        return self.shot_service.generate_shot_plan(
            project,
            generation_provenance=provenance,
            **kwargs,
        )

    def _idempotent_result(
        self, activity: CreativePipelineOperation, operation: str
    ) -> dict[str, object]:
        if activity.status is CreativePipelineOperationStatus.WAITING_HUMAN:
            if not activity.output_revision_id:
                raise CreativePipelineError("已完成活动缺少输出 revision；不会伪造成功")
            getter = {
                "GENERATE_STORY": self.repository.get_story_revision,
                "GENERATE_SCRIPT": self.repository.get_script_revision,
                "GENERATE_SHOT_PLAN": self.repository.get_shot_revision,
            }[operation]
            revision = getter(activity.output_revision_id)
            if revision is None or revision["project_id"] != activity.project_id:
                raise CreativePipelineError("活动输出 revision 不可用；不会重新提交 AI")
            return self._result(revision, activity)
        if activity.status is CreativePipelineOperationStatus.RUNNING:
            raise CreativePipelineError("相同创作活动正在执行；不会重复提交")
        raise CreativePipelineError("相同创作活动曾失败；请显式重新生成新版本")

    def _invocation_identity(self, project_id: str, activity_id: str) -> tuple[str | None, str | None]:
        records = []
        for record in self.repository.list_ai_invocations(project_id):
            provenance = record.request_summary.get("provenance", {})
            if isinstance(provenance, Mapping) and provenance.get("creative_pipeline_operation_id") == activity_id:
                records.append(record)
        if not records:
            return None, None
        terminal = next((item for item in reversed(records) if item.status == "SUCCEEDED"), records[-1])
        return terminal.provider_id, terminal.model_id

    @staticmethod
    def _result(
        revision: Mapping[str, object], activity: CreativePipelineOperation
    ) -> dict[str, object]:
        result = dict(revision)
        result["pipeline_operation_id"] = activity.id
        result["pipeline_status"] = activity.status.value
        return result


class ProductActivityAdapter:
    """Route formal creative operations before optional downstream adapters."""

    def __init__(
        self,
        creative_pipeline: CreativePipelineService,
        *,
        fallback: object | None = None,
    ) -> None:
        self.creative_pipeline = creative_pipeline
        self.fallback = fallback

    def __call__(
        self, project_id: str, operation: str, payload: Mapping[str, object]
    ) -> object:
        if str(operation or "").strip().upper() in _OPERATION_ALIASES:
            return self.creative_pipeline.execute(
                project_id=project_id,
                operation=operation,
                payload=payload,
            )
        if callable(self.fallback):
            return self.fallback(project_id, operation, payload)
        raise CreativePipelineError("当前产品活动类型尚未连接")


__all__ = [
    "CreativePipelineError",
    "CreativePipelineService",
    "ProductActivityAdapter",
]
