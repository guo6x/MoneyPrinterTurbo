"""Immutable provider-neutral production inputs and non-secret AI audit models."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OutputProfile(BaseModel):
    """Versioned project delivery truth pinned by every production job.

    The legacy attribute names remain read-only compatibility projections;
    serialization and hashes use the unambiguous V1 delivery names.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    version_number: int = Field(default=1, ge=1)
    is_project_default: bool = True
    aspect_ratio: str = Field(min_length=1, max_length=32)
    target_episode_duration_seconds: float = Field(gt=0, le=3600)
    delivery_width: int = Field(ge=16, le=16384)
    delivery_height: int = Field(ge=16, le=16384)
    delivery_resolution_label: str = Field(min_length=1, max_length=32)
    target_fps: float = Field(gt=0, le=240)
    target_video_codec: str = Field(min_length=1, max_length=80)
    target_audio_sample_rate: int = Field(gt=0, le=192000)
    target_audio_channels: int = Field(gt=0, le=16)
    quality_mode: str = Field(default="STANDARD", pattern=r"^(PREVIEW|STANDARD|HIGH|FINAL)$")
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        aliases = {
            "target_duration_seconds": "target_episode_duration_seconds",
            "fps": "target_fps",
            "video_codec_target": "target_video_codec",
            "audio_sample_rate": "target_audio_sample_rate",
            "audio_channels": "target_audio_channels",
        }
        for legacy, canonical in aliases.items():
            if legacy in data:
                data.setdefault(canonical, data[legacy])
                data.pop(legacy, None)
        legacy_resolution = str(data.pop("target_resolution", "") or "")
        match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", legacy_resolution.lower())
        if match:
            data.setdefault("delivery_width", int(match.group(1)))
            data.setdefault("delivery_height", int(match.group(2)))
        data.setdefault("delivery_width", 1920)
        data.setdefault("delivery_height", 1080)
        data.setdefault(
            "delivery_resolution_label",
            _resolution_label(int(data["delivery_width"]), int(data["delivery_height"])),
        )
        return data

    @property
    def target_duration_seconds(self) -> float:
        return self.target_episode_duration_seconds

    @property
    def target_resolution(self) -> str:
        return f"{self.delivery_width}x{self.delivery_height}"

    @property
    def fps(self) -> float:
        return self.target_fps

    @property
    def video_codec_target(self) -> str:
        return self.target_video_codec

    @property
    def audio_sample_rate(self) -> int:
        return self.target_audio_sample_rate

    @property
    def audio_channels(self) -> int:
        return self.target_audio_channels


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
    composition: str = ""
    camera_movement: str = ""
    lens_intent: str = ""
    lighting: dict[str, Any] = Field(default_factory=dict)
    mood: str = ""
    continuity_constraints: tuple[str, ...] = ()
    negative_constraints: tuple[str, ...] = ()
    dialogue_audio_intent: str = ""
    target_duration_seconds: float = Field(gt=0)
    source_ids: tuple[str, ...] = ()
    origin: str = Field(
        default="AI_COMPILED",
        pattern=r"^(AI_COMPILED|HUMAN_OVERRIDE|AI_REGENERATED)$",
    )
    parent_brief_id: str | None = Field(default=None, max_length=80)
    override_patch: dict[str, Any] = Field(default_factory=dict)
    changed_fields: tuple[str, ...] = ()
    manual_override_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    endpoint_profile_id: str | None = Field(default=None, max_length=160)
    deployment_region: str = Field(default="UNSPECIFIED", min_length=1, max_length=40)
    endpoint_class: str = Field(default="UNSPECIFIED", min_length=1, max_length=160)
    credential_reference: str | None = Field(default=None, max_length=160)
    selection_source: str = Field(default="LEGACY", min_length=1, max_length=80)
    transmitted_content_types: tuple[str, ...] = ()
    estimated_request_count: int = Field(default=1, ge=1, le=10000)
    generation_mode: str = Field(min_length=1, max_length=80)
    native_generation_resolution: str = Field(min_length=3, max_length=32)
    native_generation_fps: float = Field(default=24.0, gt=0, le=240)
    delivery_width: int = Field(default=1920, ge=16, le=16384)
    delivery_height: int = Field(default=1080, ge=16, le=16384)
    target_fps: float = Field(default=24.0, gt=0, le=240)
    delivery_strategy: str = Field(
        default="NATIVE",
        pattern=r"^(NATIVE|DETERMINISTIC_SCALE|DETERMINISTIC_UPSCALE)$",
    )
    quality_mode: str = Field(default="STANDARD", pattern=r"^(PREVIEW|STANDARD|HIGH|FINAL)$")
    provider_generation_duration: float = Field(gt=0, le=3600)
    target_creative_duration: float = Field(gt=0, le=3600)
    duration_strategy: str = Field(
        default="EXACT",
        pattern=r"^(EXACT|TRIM_TO_CREATIVE|HOLD_OR_PAD|CHUNK_AND_CONTINUE)$",
    )
    audio_strategy: str = Field(min_length=1, max_length=80)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)
    reference_version_ids: tuple[str, ...] = ()
    reference_roles: dict[str, str] = Field(default_factory=dict)
    continuity_strategy: str = Field(min_length=1, max_length=120)
    generation_brief_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_override_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    output_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization: dict[str, Any] = Field(default_factory=dict)
    prompt_template_version: str = Field(min_length=1, max_length=120)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_resolution(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "resolution" in data:
            data.setdefault("native_generation_resolution", data["resolution"])
            data.pop("resolution", None)
        return data

    @property
    def resolution(self) -> str:
        """Compatibility projection: provider-native request resolution."""
        return self.native_generation_resolution


def _resolution_label(width: int, height: int) -> str:
    long_edge, short_edge = max(width, height), min(width, height)
    if long_edge >= 3840 or short_edge >= 2160:
        return "4K"
    if long_edge >= 2560 or short_edge >= 1440:
        return "1440p"
    if long_edge >= 1920 or short_edge >= 1080:
        return "1080p"
    if long_edge >= 1280 or short_edge >= 720:
        return "720p"
    return f"{width}x{height}"


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
