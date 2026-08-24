from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ProductionJobStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProductionShotStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ProductionAttemptStatus(str, Enum):
    STARTED = "STARTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"


class ProductionJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    status: ProductionJobStatus = ProductionJobStatus.DRAFT
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class ProductionShot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    production_job_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=80)
    order_index: int = Field(ge=0)
    status: ProductionShotStatus = ProductionShotStatus.PENDING
    created_at: str = Field(min_length=1, max_length=80)


class ProductionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    production_shot_id: str = Field(min_length=1, max_length=64)
    attempt_number: int = Field(ge=1)
    status: ProductionAttemptStatus = ProductionAttemptStatus.STARTED
    runtime_adapter: str = Field(min_length=1, max_length=120)
    runtime_reference: str | None = Field(default=None, max_length=500)
    input_snapshot_json: dict[str, object] = Field(default_factory=dict)
    output_artifact_json: dict[str, object] | None = None
    error_message: str | None = Field(default=None, max_length=4000)
    created_at: str = Field(min_length=1, max_length=80)
