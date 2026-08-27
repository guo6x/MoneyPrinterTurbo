"""Canonical post-production models.

Post-production is deliberately a small, project-scoped layer around an
immutable :class:`FinalAssembly` output.  The models contain no renderer
logic; rendering and path validation live in ``services.postproduction``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PostRenderAttemptStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TTSTaskStatus(str, Enum):
    PLANNED = "PLANNED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DialogueLine(BaseModel):
    """One ordered spoken line extracted from an approved Structured Script."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    speaker: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=5000)
    language: str = Field(min_length=2, max_length=32)
    shot_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    scene_id: str = Field(min_length=1, max_length=80)
    beat_id: str = Field(min_length=1, max_length=80)


class DialoguePlan(BaseModel):
    """Immutable, versioned dialogue truth for one post-production plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    source_script_revision_id: str = Field(min_length=1, max_length=80)
    source_shot_plan_revision_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    lines: list[DialogueLine] = Field(min_length=1, max_length=5000)
    lines_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def lines_are_unique_and_ordered(self) -> "DialoguePlan":
        if len({item.id for item in self.lines}) != len(self.lines):
            raise ValueError("dialogue line IDs must be unique")
        if [item.order for item in self.lines] != list(range(1, len(self.lines) + 1)):
            raise ValueError("dialogue lines must have contiguous order")
        return self


class VoiceAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(min_length=1, max_length=80)
    voice_profile: str = Field(min_length=1, max_length=120)


class VoiceAssignmentSet(BaseModel):
    """Versioned speaker-to-voice mapping; never inferred at render time."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    source_dialogue_plan_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    assignments: list[VoiceAssignment] = Field(min_length=1, max_length=500)
    assignments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def speakers_are_unique(self) -> "VoiceAssignmentSet":
        if len({item.speaker for item in self.assignments}) != len(self.assignments):
            raise ValueError("voice assignment speakers must be unique")
        return self


class TTSTask(BaseModel):
    """A versioned Universal Runtime request and its formal WAV artifact."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    source_dialogue_plan_id: str = Field(min_length=1, max_length=80)
    source_voice_assignment_set_id: str = Field(min_length=1, max_length=80)
    source_script_revision_id: str = Field(min_length=1, max_length=80)
    dialogue_line_id: str = Field(min_length=1, max_length=80)
    shot_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=5000)
    voice_profile: str = Field(min_length=1, max_length=120)
    language: str = Field(min_length=2, max_length=32)
    sample_rate: int = Field(ge=8000, le=192000)
    manifest_id: str = Field(min_length=1, max_length=200)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: TTSTaskStatus = TTSTaskStatus.PLANNED
    output_relative_path: str | None = Field(default=None, max_length=1000)
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_size_bytes: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def successful_task_has_artifact(self) -> "TTSTask":
        if self.status is TTSTaskStatus.SUCCEEDED and not all(
            (
                self.output_relative_path,
                self.output_sha256,
                self.output_size_bytes,
                self.duration_seconds,
            )
        ):
            raise ValueError("successful TTS task must identify its WAV artifact")
        return self


class AudioTimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialogue_line_id: str = Field(min_length=1, max_length=80)
    tts_task_id: str = Field(min_length=1, max_length=80)
    speaker: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=5000)
    shot_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    silence_gap_seconds: float = Field(ge=0)

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


class AudioTimeline(BaseModel):
    """Single timing truth shared by the WAV track and generated subtitles."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=80)
    source_dialogue_plan_id: str = Field(min_length=1, max_length=80)
    source_voice_assignment_set_id: str = Field(min_length=1, max_length=80)
    source_script_revision_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    sample_rate: int = Field(ge=8000, le=192000)
    items: list[AudioTimelineItem] = Field(min_length=1, max_length=5000)
    content_end_seconds: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    artifact_relative_path: str = Field(min_length=1, max_length=1000)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(gt=0)
    timeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def items_are_ordered_and_fit(self) -> "AudioTimeline":
        previous_end = 0.0
        for expected_order, item in enumerate(self.items, start=1):
            if item.order != expected_order or item.start_seconds < previous_end - 0.001:
                raise ValueError("audio timeline items must be ordered and non-overlapping")
            previous_end = item.end_seconds
        if abs(previous_end - self.content_end_seconds) > 0.01:
            raise ValueError("audio timeline content end does not match its items")
        if self.content_end_seconds > self.duration_seconds + 0.01:
            raise ValueError("audio timeline content exceeds track duration")
        return self


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
    heavy_job_id: str | None = Field(default=None, max_length=64)
    output_relative_path: str | None = Field(default=None, max_length=1000)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=4000)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)


__all__ = [
    "AudioMixConfig",
    "AudioTimeline",
    "AudioTimelineItem",
    "DialogueLine",
    "DialoguePlan",
    "MusicTrack",
    "PostProductionPlan",
    "PostProductionProject",
    "PostRenderAttempt",
    "PostRenderAttemptStatus",
    "SubtitleCue",
    "SubtitleItem",
    "SubtitleTrack",
    "TTSTask",
    "TTSTaskStatus",
    "VoiceAssignment",
    "VoiceAssignmentSet",
    "VoiceTrack",
    "BGMTrack",
]

# Friendly names used by UI/integration code while retaining one canonical
# persisted model.  These are aliases, not parallel domain truths.
PostProductionProject = PostProductionPlan
SubtitleItem = SubtitleCue
BGMTrack = MusicTrack
