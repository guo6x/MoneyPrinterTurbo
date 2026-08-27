"""Durable contracts for the product-level AUTO Mode orchestrator."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AutoRunStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    WAITING_HUMAN = "WAITING_HUMAN"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"


class AutoStage(StrEnum):
    CREATIVE = "CREATIVE"
    STORY = "STORY"
    SCRIPT = "SCRIPT"
    SHOT_PLAN = "SHOT_PLAN"
    REFERENCES = "REFERENCES"
    PRODUCTION = "PRODUCTION"
    QC = "QC"
    REVIEW = "REVIEW"
    FINAL = "FINAL"
    COMPLETED = "COMPLETED"


class AutoAction(StrEnum):
    GENERATE_OR_CREATE_STORY = "GENERATE_OR_CREATE_STORY"
    GENERATE_SCRIPT = "GENERATE_SCRIPT"
    GENERATE_SHOT_PLAN = "GENERATE_SHOT_PLAN"
    GENERATE_REFERENCE_CANDIDATE = "GENERATE_REFERENCE_CANDIDATE"
    PREPARE_PRODUCTION = "PREPARE_PRODUCTION"
    CREATE_PRODUCTION_EXECUTION = "CREATE_PRODUCTION_EXECUTION"
    POLL_EXISTING_TASK = "POLL_EXISTING_TASK"
    RUN_TECHNICAL_QC = "RUN_TECHNICAL_QC"
    RUN_OPTIONAL_VISION_QC = "RUN_OPTIONAL_VISION_QC"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAID_AUTHORIZATION_REQUIRED = "PAID_AUTHORIZATION_REQUIRED"
    FINAL_ASSEMBLY = "FINAL_ASSEMBLY"
    NONE = "NONE"


class AutoDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    status: AutoRunStatus
    current_stage: AutoStage
    next_action: AutoAction
    why: str = Field(min_length=1, max_length=2000)
    blocking_reason: str | None = Field(default=None, max_length=2000)
    requires_human: bool = False
    requires_paid_authorization: bool = False
    requested_action: str | None = Field(default=None, max_length=160)
    resume_token: str | None = Field(default=None, max_length=160)
    completed_stages: tuple[AutoStage, ...] = ()
    input_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutoOrchestrationState(AutoDecision):
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)
    last_result: str = Field(default="PENDING", min_length=1, max_length=160)
    actor: str = Field(default="product-agent", min_length=1, max_length=160)
    state_version: int = Field(default=1, ge=1)


class AutoAgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    sequence_number: int = Field(ge=1)
    decision: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    input_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: str = Field(min_length=1, max_length=160)
    timestamp: str = Field(min_length=1, max_length=80)
    actor: str = Field(min_length=1, max_length=160)


class AutoPaidAuthorization(BaseModel):
    """One explicit, input-bound create budget; never contains credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    action: AutoAction
    resource_key: str = Field(min_length=1, max_length=240)
    input_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization: dict[str, Any] = Field(default_factory=dict)
    global_max: int = Field(ge=1)
    per_item_max: int = Field(default=1, ge=1)
    retry_limit: int = Field(default=0, ge=0)
    consumed_count: int = Field(default=0, ge=0)
    status: str = Field(default="ACTIVE", pattern=r"^(ACTIVE|CONSUMED|REVOKED)$")
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class AutoPaidAuthorizationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    action: AutoAction
    resource_key: str = Field(min_length=1, max_length=240)
    input_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_create_count: int = Field(ge=1)
    per_item_max: int = Field(default=1, ge=1)
    retry_limit: int = Field(default=0, ge=0)
    provider_label: str = Field(min_length=1, max_length=240)
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AutoAction",
    "AutoAgentEvent",
    "AutoDecision",
    "AutoOrchestrationState",
    "AutoPaidAuthorization",
    "AutoPaidAuthorizationPreview",
    "AutoRunStatus",
    "AutoStage",
]
