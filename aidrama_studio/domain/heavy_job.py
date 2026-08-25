"""Durable orchestration records for local and provider-adjacent heavy work."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HeavyJobType(StrEnum):
    PRODUCTION = "PRODUCTION"
    FINAL_ASSEMBLY_RENDER = "FINAL_ASSEMBLY_RENDER"
    POST_RENDER = "POST_RENDER"
    UPSCALE = "UPSCALE"
    TTS = "TTS"
    FINAL_MEDIA_EXPORT = "FINAL_MEDIA_EXPORT"
    PROJECT_EXPORT = "PROJECT_EXPORT"
    PROJECT_IMPORT = "PROJECT_IMPORT"


class HeavyJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class HeavyJobEventType(StrEnum):
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    STAGE = "STAGE"
    PROGRESS = "PROGRESS"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class HeavyJob(BaseModel):
    """Immutable input identity plus a mutable durable lifecycle projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    job_type: HeavyJobType
    project_id: str | None = Field(default=None, max_length=64)
    status: HeavyJobStatus = HeavyJobStatus.QUEUED
    stage: str = Field(default="QUEUED", min_length=1, max_length=120)
    progress: float | None = Field(default=None, ge=0, le=100)
    idempotency_key: str = Field(min_length=1, max_length=240)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_provenance: dict[str, Any] = Field(default_factory=dict)
    safe_error: str | None = Field(default=None, max_length=4000)
    cancel_requested: bool = False
    retry_of_job_id: str | None = Field(default=None, max_length=64)
    created_at: str = Field(min_length=1, max_length=80)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_scope(self) -> "HeavyJob":
        if self.project_id is None and self.job_type is not HeavyJobType.PROJECT_IMPORT:
            raise ValueError("只有 PROJECT_IMPORT 可以在导入前没有 project_id")
        return self


class HeavyJobEvent(BaseModel):
    """Append-only event. Sequence numbers are allocated transactionally."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    heavy_job_id: str = Field(min_length=1, max_length=64)
    sequence_number: int = Field(ge=1)
    event_type: HeavyJobEventType
    stage: str | None = Field(default=None, max_length=120)
    progress: float | None = Field(default=None, ge=0, le=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)


__all__ = [
    "HeavyJob",
    "HeavyJobEvent",
    "HeavyJobEventType",
    "HeavyJobStatus",
    "HeavyJobType",
]
