"""Output profiles, GenerationBrief compilation, RuntimePlan pinning and
non-secret AI invocation ledgers.

Provider adapters receive these immutable records instead of reconstructing
creative state from the live database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from aidrama_studio.domain import AIInvocation, GenerationBrief, OutputProfile, RuntimePlan
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class RuntimeFoundationError(RuntimeError):
    pass


class OutputProfileService:
    RESOLUTION_BY_RATIO = {"16:9": "1920x1080", "9:16": "1080x1920", "1:1": "1080x1080", "4:3": "1440x1080"}

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def create(self, project_id: str, *, aspect_ratio: str, target_duration_seconds: float, target_resolution: str | None = None, fps: float = 24.0, video_codec_target: str = "h264", audio_sample_rate: int = 48000, audio_channels: int = 2, profile_id: str | None = None) -> OutputProfile:
        if self.repository.get_project(project_id) is None:
            raise RuntimeFoundationError(f"项目不存在: {project_id}")
        profile = OutputProfile(
            id=profile_id or uuid4().hex,
            project_id=project_id,
            aspect_ratio=aspect_ratio,
            target_duration_seconds=target_duration_seconds,
            target_resolution=target_resolution or self.RESOLUTION_BY_RATIO.get(aspect_ratio, "1920x1080"),
            fps=fps,
            video_codec_target=video_codec_target,
            audio_sample_rate=audio_sample_rate,
            audio_channels=audio_channels,
            created_at=_now(),
        )
        return self.repository.create_output_profile(profile)

    def ensure_for_job(self, project_id: str, job_id: str) -> OutputProfile:
        job = self.repository.get_production_job(job_id)
        if job is None or job.project_id != project_id:
            raise RuntimeFoundationError("ProductionJob 不属于该项目")
        if job.output_profile_id:
            profile = self.repository.get_output_profile(job.output_profile_id)
            if profile is not None:
                return profile
        project = self.repository.get_project(project_id)
        if project is None:
            raise RuntimeFoundationError(f"项目不存在: {project_id}")
        profile = self.create(
            project_id,
            aspect_ratio=project.aspect_ratio.value,
            target_duration_seconds=float(project.target_duration_seconds),
        )
        self.repository.set_production_job_output_profile(job.id, profile.id)
        return profile


class GenerationBriefCompiler:
    """Compile one shot brief from the job's approved revision chain."""

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def compile(self, project_id: str, production_job_id: str, shot_id: str, *, brief_id: str | None = None) -> GenerationBrief:
        job = self.repository.get_production_job(production_job_id)
        if job is None or job.project_id != project_id:
            raise RuntimeFoundationError("ProductionJob 不属于该项目")
        plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if plan is None or plan["project_id"] != project_id:
            raise RuntimeFoundationError("Shot Plan revision 不属于该项目")
        shot = next((item for item in plan["content"].shots if item.id == shot_id), None)
        if shot is None:
            raise RuntimeFoundationError("Shot 不属于该 ProductionJob")
        script_revision = self.repository.get_script_revision(plan["source_script_revision_id"])
        story_revision = self.repository.get_story_revision(script_revision["source_story_revision_id"]) if script_revision else None
        if script_revision is None or story_revision is None:
            raise RuntimeFoundationError("GenerationBrief 缺少完整 Story/Script provenance")
        script = script_revision["content"]
        story = story_revision["content"]
        scene = next((item for item in script.scenes if item.id == shot.scene_id), None)
        if scene is None:
            raise RuntimeFoundationError("Shot scene 不存在")
        characters = [character for character in story.characters if character.id in set(shot.subject)]
        location = next((item for item in story.locations if item.id == scene.location_id), None)
        if location is None:
            raise RuntimeFoundationError("Shot location 不存在")
        raw = {
            "character_context": [character.model_dump(mode="json") for character in characters],
            "location_context": location.model_dump(mode="json"),
            "key_props": list(location.key_props),
            "style": {"genre": story.genre, "tone": story.tone, "visual_style": location.visual_style, "world": story.world.model_dump(mode="json")},
            "action": shot.action,
            "framing": shot.shot_size.value,
            "camera_movement": shot.camera_movement.value,
            "lens_intent": shot.lens.value,
            "lighting": shot.lighting.model_dump(mode="json"),
            "continuity_constraints": list(shot.risk_reasons) + ([shot.transition_hint] if shot.transition_hint else []),
            "dialogue_audio_intent": shot.dialogue_or_narration,
            "target_duration_seconds": float(shot.duration_seconds),
            "source_ids": [story_revision["id"], script_revision["id"], plan["id"], shot.id, *shot.source_script_beat_ids],
        }
        brief_hash = _hash(raw)
        # Queue retries and Streamlit reruns must not create competing copies
        # of the same frozen creative truth.  Reuse an identical persisted
        # brief; a changed upstream revision or shot produces a new hash and
        # therefore a new immutable record.
        for existing in reversed(self.repository.list_generation_briefs(project_id, production_job_id)):
            if existing.shot_id == shot.id and existing.sha256 == brief_hash:
                return existing
        brief = GenerationBrief(
            id=brief_id or uuid4().hex,
            project_id=project_id,
            production_job_id=production_job_id,
            shot_id=shot.id,
            sha256=brief_hash,
            created_at=_now(),
            **raw,
        )
        return self.repository.create_generation_brief(brief)


class RuntimePlanService:
    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.profiles = OutputProfileService(self.repository)

    def create(self, project_id: str, *, production_job_id: str | None, brief: GenerationBrief, provider_capability: str, provider_id: str, model_id: str, endpoint_profile_id: str | None = None, deployment_region: str = "UNSPECIFIED", endpoint_class: str = "UNSPECIFIED", credential_reference: str | None = None, selection_source: str = "LEGACY", transmitted_content_types: list[str] | tuple[str, ...] = (), estimated_request_count: int = 1, generation_mode: str = "text_to_video", resolution: str | None = None, provider_generation_duration: float | None = None, target_creative_duration: float | None = None, audio_strategy: str = "provider_or_post", provider_parameters: Mapping[str, Any] | None = None, reference_version_ids: list[str] | tuple[str, ...] = (), reference_roles: Mapping[str, str] | None = None, continuity_strategy: str = "shot-local", authorization: Mapping[str, Any] | None = None, prompt_template_version: str = "v1", plan_id: str | None = None) -> RuntimePlan:
        if brief.project_id != project_id or (production_job_id is not None and brief.production_job_id != production_job_id):
            raise RuntimeFoundationError("GenerationBrief provenance 不匹配")
        profile = self.profiles.ensure_for_job(project_id, production_job_id) if production_job_id else None
        profile_data = profile.model_dump(mode="json") if profile else {}
        profile_hash = _hash(profile_data)
        payload = {
            "provider_capability": provider_capability, "provider_id": provider_id, "model_id": model_id,
            "endpoint_profile_id": endpoint_profile_id,
            "deployment_region": deployment_region,
            "endpoint_class": endpoint_class,
            "credential_reference": credential_reference,
            "selection_source": selection_source,
            "transmitted_content_types": list(transmitted_content_types),
            "estimated_request_count": int(estimated_request_count),
            "generation_mode": generation_mode, "resolution": resolution or (profile.target_resolution if profile else "1920x1080"),
            "provider_generation_duration": provider_generation_duration or brief.target_duration_seconds,
            "target_creative_duration": target_creative_duration or brief.target_duration_seconds,
            "audio_strategy": audio_strategy, "provider_parameters": _redact(dict(provider_parameters or {})),
            "reference_version_ids": list(reference_version_ids), "reference_roles": dict(reference_roles or {}),
            "continuity_strategy": continuity_strategy, "generation_brief_hash": brief.sha256,
            "output_profile_hash": profile_hash, "authorization": _redact(dict(authorization or {})),
            "prompt_template_version": prompt_template_version,
        }
        plan_hash = _hash(payload)
        for existing in reversed(self.repository.list_runtime_plans(project_id)):
            if existing.plan_hash == plan_hash:
                return existing
        plan = RuntimePlan(
            id=plan_id or uuid4().hex, project_id=project_id, production_job_id=production_job_id,
            output_profile_id=profile.id if profile else None, generation_brief_id=brief.id,
            plan_hash=plan_hash, created_at=_now(), **payload,
        )
        return self.repository.create_runtime_plan(plan)


class AIInvocationService:
    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def record(self, project_id: str, *, capability: str, provider_id: str, model_id: str, status: str, production_job_id: str | None = None, execution_id: str | None = None, input_source_ids: list[str] | tuple[str, ...] = (), reference_version_ids: list[str] | tuple[str, ...] = (), generation_brief_hash: str | None = None, runtime_plan: RuntimePlan | None = None, request_summary: Mapping[str, Any] | None = None, provider_task_id: str | None = None, started_at: str | None = None, finished_at: str | None = None, usage: Mapping[str, Any] | None = None, estimated_cost: float | None = None, actual_cost: float | None = None, output_artifact_ids: list[str] | tuple[str, ...] = (), invocation_id: str | None = None) -> AIInvocation:
        from .security import sanitize_persistent_metadata

        if runtime_plan is not None and runtime_plan.project_id != project_id:
            raise RuntimeFoundationError("RuntimePlan 不属于该项目")
        safe_summary = sanitize_persistent_metadata(dict(request_summary or {}))
        safe_usage = sanitize_persistent_metadata(dict(usage or {}))
        invocation = AIInvocation(
            id=invocation_id or uuid4().hex, project_id=project_id, production_job_id=production_job_id, execution_id=execution_id,
            capability=capability, provider_id=provider_id, model_id=model_id, input_source_ids=tuple(input_source_ids),
            reference_version_ids=tuple(reference_version_ids), generation_brief_hash=generation_brief_hash,
            runtime_plan_id=runtime_plan.id if runtime_plan else None, runtime_plan_hash=runtime_plan.plan_hash if runtime_plan else None,
            request_summary=safe_summary if isinstance(safe_summary, dict) else {}, provider_task_id=provider_task_id, status=status,
            started_at=started_at, finished_at=finished_at, usage=safe_usage if isinstance(safe_usage, dict) else {}, estimated_cost=estimated_cost,
            actual_cost=actual_cost, output_artifact_ids=tuple(output_artifact_ids), created_at=_now(),
        )
        return self.repository.create_ai_invocation(invocation)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if str(key).lower()
            not in {
                "api_key", "apikey", "access_token", "refresh_token",
                "authorization", "token", "secret", "password",
                "client_secret", "private_key", "cookie", "set_cookie",
                "signed_url",
            }
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


__all__ = ["AIInvocationService", "GenerationBriefCompiler", "OutputProfileService", "RuntimeFoundationError", "RuntimePlanService"]
