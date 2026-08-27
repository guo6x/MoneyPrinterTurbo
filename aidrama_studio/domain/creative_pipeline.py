"""Durable product activity for the upstream AI creative pipeline."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CreativePipelineStage(str, Enum):
    CREATIVE_INTAKE = "CREATIVE_INTAKE"
    STORY_GENERATION = "STORY_GENERATION"
    STORY_REVIEW = "STORY_REVIEW"
    SCRIPT_GENERATION = "SCRIPT_GENERATION"
    SCRIPT_REVIEW = "SCRIPT_REVIEW"
    SHOT_PLAN_GENERATION = "SHOT_PLAN_GENERATION"
    SHOT_PLAN_REVIEW = "SHOT_PLAN_REVIEW"
    REFERENCE_PREPARATION = "REFERENCE_PREPARATION"


class CreativePipelineOperationStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    FAILED = "FAILED"


class CreativePipelineOperation(BaseModel):
    """Append-only generation intent and its durable human-review handoff."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=80)
    stage: CreativePipelineStage
    status: CreativePipelineOperationStatus
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_revision_ids: tuple[str, ...] = ()
    output_revision_id: str | None = Field(default=None, max_length=80)
    provider_id: str | None = Field(default=None, max_length=160)
    model_id: str | None = Field(default=None, max_length=240)
    prompt_template_version: str = Field(min_length=1, max_length=160)
    failure_reason: str | None = Field(default=None, max_length=1000)
    created_at: str = Field(min_length=1, max_length=80)
    started_at: str = Field(min_length=1, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


__all__ = [
    "CreativePipelineOperation",
    "CreativePipelineOperationStatus",
    "CreativePipelineStage",
]
