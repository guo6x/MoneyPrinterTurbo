"""Canonical post-production models.

Post-production is deliberately a small, project-scoped layer around an
immutable :class:`FinalAssembly` output.  The models contain no renderer
logic; rendering and path validation live in ``services.postproduction``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PostRenderAttemptStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubtitleCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=5000)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    scene_id: str | None = Field(default=None, max_length=80)
    shot_id: str | None = Field(default=None, max_length=80)
    beat_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def end_after_start(self) -> "SubtitleCue":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("subtitle cue end must be after start")
        return self


class SubtitleTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str | None = Field(default=None, max_length=80)
    source_script_revision_id: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    cues: list[SubtitleCue] = Field(default_factory=list, max_length=5000)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def cues_are_ordered(self) -> "SubtitleTrack":
        if any(self.cues[i].start_seconds > self.cues[i + 1].start_seconds for i in range(len(self.cues) - 1)):
            raise ValueError("subtitle cues must be ordered by start time")
        return self


class VoiceTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    path: str | None = Field(default=None, max_length=1000)
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)


class MusicTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=1000)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    gain: float = Field(default=1.0, ge=0, le=4)
    loop: bool = False
    fade_in_seconds: float = Field(default=0, ge=0, le=60)
    fade_out_seconds: float = Field(default=0, ge=0, le=60)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def valid_range(self) -> "MusicTrack":
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("music end must be after start")
        return self


class AudioMixConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_gain: float = Field(default=1.0, ge=0, le=4)
    voice_gain: float = Field(default=1.0, ge=0, le=4)
    music_gain: float = Field(default=0.25, ge=0, le=4)
    normalize: bool = True


class PostProductionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    source_final_assembly_id: str = Field(min_length=1, max_length=80)
    # Frozen at plan creation (or at the first render for legacy plans that
    # predate migration 017).  A later FinalAssembly retry must never silently
    # replace the media input for this plan.
    source_final_assembly_render_attempt_id: str | None = Field(default=None, max_length=80)
    subtitle_enabled: bool = True
    audio_mix: AudioMixConfig = Field(default_factory=AudioMixConfig)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class PostRenderAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    source_final_assembly_id: str = Field(min_length=1, max_length=80)
    source_final_assembly_render_attempt_id: str | None = Field(default=None, max_length=80)
    attempt_number: int = Field(ge=1)
    status: PostRenderAttemptStatus = PostRenderAttemptStatus.PENDING
    adapter_name: str = Field(min_length=1, max_length=120)
    output_relative_path: str | None = Field(default=None, max_length=1000)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=4000)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)


__all__ = [
    "AudioMixConfig",
    "MusicTrack",
    "PostProductionPlan",
    "PostProductionProject",
    "PostRenderAttempt",
    "PostRenderAttemptStatus",
    "SubtitleCue",
    "SubtitleItem",
    "SubtitleTrack",
    "VoiceTrack",
    "BGMTrack",
]

# Friendly names used by UI/integration code while retaining one canonical
# persisted model.  These are aliases, not parallel domain truths.
PostProductionProject = PostProductionPlan
SubtitleItem = SubtitleCue
BGMTrack = MusicTrack
