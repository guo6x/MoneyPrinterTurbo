from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .production_snapshot import ProductionInputSnapshot


class ProductionExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProductionEventType(str, Enum):
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    SHOT_COMPLETED = "SHOT_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    FINISHED = "FINISHED"


class ProductionExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    production_job_id: str = Field(min_length=1, max_length=64)
    status: ProductionExecutionStatus = ProductionExecutionStatus.QUEUED
    worker_type: str = Field(min_length=1, max_length=120)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    created_at: str | None = Field(default=None, max_length=80)
    input_snapshot: ProductionInputSnapshot | None = None


class ProductionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=64)
    event_type: ProductionEventType
    payload_json: dict[str, object] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)


class ProductionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=64)
    artifact_type: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)
    metadata_json: dict[str, object] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)
