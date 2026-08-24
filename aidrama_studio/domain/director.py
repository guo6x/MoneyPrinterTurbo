"""Durable, structured AI Director records.

The Director is an advisory control plane.  These records intentionally do
not mirror Story, Shot or Production state; they only preserve the bounded
goal and the decisions made while inspecting canonical state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DirectorGoalKind(str, Enum):
    COMPLETE_STORY = "COMPLETE_STORY"
    COMPLETE_SCRIPT = "COMPLETE_SCRIPT"
    COMPLETE_SHOT_PLAN = "COMPLETE_SHOT_PLAN"
    COMPLETE_REFERENCES = "COMPLETE_REFERENCES"
    MAKE_PRODUCTION_READY = "MAKE_PRODUCTION_READY"
    COMPLETE_PRODUCTION = "COMPLETE_PRODUCTION"
    RESOLVE_QC_BLOCKER = "RESOLVE_QC_BLOCKER"
    MAKE_FINAL_ASSEMBLY_READY = "MAKE_FINAL_ASSEMBLY_READY"
    COMPLETE_POST_PRODUCTION = "COMPLETE_POST_PRODUCTION"


class DirectorSessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"


class DirectorGoalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class DirectorDecisionStatus(str, Enum):
    RECOMMENDED = "RECOMMENDED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class DirectorRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=120)
    target_id: str | None = Field(default=None, max_length=160)
    reason: str = Field(default="", max_length=4000)
    requires_human_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class DirectorSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    status: DirectorSessionStatus = DirectorSessionStatus.ACTIVE
    current_goal: DirectorGoalKind
    blocking_reason: str = ""
    pending_recommendation: DirectorRecommendation | None = None
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class DirectorGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    goal: DirectorGoalKind
    status: DirectorGoalStatus = DirectorGoalStatus.ACTIVE
    max_steps: int = Field(default=1, ge=1, le=100)
    completed_steps: int = Field(default=0, ge=0)
    created_at: str = Field(min_length=1, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)


class DirectorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    goal_id: str = Field(min_length=1, max_length=64)
    status: DirectorDecisionStatus = DirectorDecisionStatus.RECOMMENDED
    project_state: str = Field(default="UNKNOWN", max_length=80)
    recommendation: DirectorRecommendation
    state_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)


class DirectorDecisionEvent(BaseModel):
    """Append-only lifecycle transition for one immutable recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    decision_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    from_status: DirectorDecisionStatus
    to_status: DirectorDecisionStatus
    event_type: str = Field(min_length=1, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)
