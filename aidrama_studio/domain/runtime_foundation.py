"""Immutable provider-neutral production inputs and non-secret AI audit models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OutputProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    aspect_ratio: str = Field(min_length=1, max_length=32)
    target_duration_seconds: float = Field(gt=0, le=3600)
    target_resolution: str = Field(min_length=3, max_length=32)
    fps: float = Field(gt=0, le=240)
    video_codec_target: str = Field(min_length=1, max_length=80)
    audio_sample_rate: int = Field(gt=0, le=192000)
    audio_channels: int = Field(gt=0, le=16)
    created_at: str = Field(min_length=1, max_length=80)


class GenerationBrief(BaseModel):
    """Provider-neutral, shot-scoped brief compiled from frozen revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str | None = Field(default=None, max_length=80)
    shot_id: str = Field(min_length=1, max_length=80)
    character_context: tuple[dict[str, Any], ...] = ()
    location_context: dict[str, Any] = Field(default_factory=dict)
    key_props: tuple[str, ...] = ()
    style: dict[str, Any] = Field(default_factory=dict)
    action: str = ""
    framing: str = ""
    camera_movement: str = ""
    lens_intent: str = ""
    lighting: dict[str, Any] = Field(default_factory=dict)
    continuity_constraints: tuple[str, ...] = ()
    dialogue_audio_intent: str = ""
    target_duration_seconds: float = Field(gt=0)
    source_ids: tuple[str, ...] = ()
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)


class RuntimePlan(BaseModel):
    """Frozen paid-provider request plan; credentials never belong here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str | None = Field(default=None, max_length=80)
    execution_id: str | None = Field(default=None, max_length=80)
    output_profile_id: str | None = Field(default=None, max_length=80)
    generation_brief_id: str | None = Field(default=None, max_length=80)
    provider_capability: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=240)
    generation_mode: str = Field(min_length=1, max_length=80)
    resolution: str = Field(min_length=3, max_length=32)
    provider_generation_duration: float = Field(gt=0, le=3600)
    target_creative_duration: float = Field(gt=0, le=3600)
    audio_strategy: str = Field(min_length=1, max_length=80)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)
    reference_version_ids: tuple[str, ...] = ()
    reference_roles: dict[str, str] = Field(default_factory=dict)
    continuity_strategy: str = Field(min_length=1, max_length=120)
    generation_brief_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization: dict[str, Any] = Field(default_factory=dict)
    prompt_template_version: str = Field(min_length=1, max_length=120)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)


class AIInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str | None = Field(default=None, max_length=80)
    execution_id: str | None = Field(default=None, max_length=80)
    capability: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=240)
    input_source_ids: tuple[str, ...] = ()
    reference_version_ids: tuple[str, ...] = ()
    generation_brief_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_plan_id: str | None = Field(default=None, max_length=80)
    runtime_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_summary: dict[str, Any] = Field(default_factory=dict)
    provider_task_id: str | None = Field(default=None, max_length=240)
    status: str = Field(min_length=1, max_length=40)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    usage: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: float | None = Field(default=None, ge=0)
    actual_cost: float | None = Field(default=None, ge=0)
    output_artifact_ids: tuple[str, ...] = ()
    created_at: str = Field(min_length=1, max_length=80)


__all__ = ["AIInvocation", "GenerationBrief", "OutputProfile", "RuntimePlan"]
