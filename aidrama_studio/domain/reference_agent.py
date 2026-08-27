"""Provider-neutral projections for the autonomous reference agent.

These records describe reference needs and review gates.  They deliberately do
not introduce another persistent lifecycle beside ``ReferenceAsset``: the
existing candidate, promoted-version, binding, and current-version records
remain the source of truth for asset mutation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ReferenceSubjectType(StrEnum):
    CHARACTER = "CHARACTER"
    LOCATION = "LOCATION"


class ReferenceCoverageStatus(StrEnum):
    MISSING = "MISSING"
    WAITING_PAID_AUTHORIZATION = "WAITING_PAID_AUTHORIZATION"
    CANDIDATE_READY = "CANDIDATE_READY"
    WAITING_HUMAN = "WAITING_HUMAN"
    APPROVED = "APPROVED"
    BOUND = "BOUND"
    LOCKED = "LOCKED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"


class ReferenceActionKind(StrEnum):
    GENERATION_REQUIRED = "GENERATION_REQUIRED"
    WAITING_PAID_AUTHORIZATION = "WAITING_PAID_AUTHORIZATION"
    WAITING_HUMAN_REFERENCE_APPROVAL = "WAITING_HUMAN_REFERENCE_APPROVAL"
    WAITING_HUMAN_REFERENCE_LOCK = "WAITING_HUMAN_REFERENCE_LOCK"
    REFERENCE_REVIEW_REQUIRED = "REFERENCE_REVIEW_REQUIRED"
    BLOCKED_UPSTREAM = "BLOCKED_UPSTREAM"


class ReferenceRequirement(BaseModel):
    """One deduplicated reference subject required by one or more shots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str = Field(min_length=1, max_length=80)
    subject_type: ReferenceSubjectType
    canonical_name: str = Field(min_length=1, max_length=160)
    required_by_shot_ids: tuple[str, ...] = ()
    priority: str = Field(min_length=1, max_length=32)
    source_revision_ids: dict[str, str] = Field(default_factory=dict)
    subject_identity: str = Field(min_length=64, max_length=64)
    coverage_status: ReferenceCoverageStatus
    locked_version_id: str | None = Field(default=None, max_length=80)
    candidate_id: str | None = Field(default=None, max_length=80)
    stale_reason: str | None = Field(default=None, max_length=2000)


class ReferenceBrief(BaseModel):
    """A provider-neutral input for creating one reference candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str = Field(min_length=1, max_length=80)
    subject_type: ReferenceSubjectType
    canonical_name: str = Field(min_length=1, max_length=160)
    canonical_identity: str = ""
    appearance: str = ""
    wardrobe: str = ""
    age_presentation: str = ""
    story_context: str = ""
    visual_style: str = ""
    aspect_ratio: str = Field(min_length=1, max_length=32)
    time_weather: str = ""
    set_details: str = ""
    negative_constraints: tuple[str, ...] = ()
    source_revision_ids: dict[str, str] = Field(default_factory=dict)

    def render_prompt(self) -> str:
        """Render portable text without provider-specific prompt syntax."""

        parts = [
            "Provider-neutral reference image brief",
            f"Reference type: {self.subject_type.value}",
            f"Canonical subject: {self.canonical_name}",
            f"Aspect ratio: {self.aspect_ratio}",
        ]
        for label, value in (
            ("Identity", self.canonical_identity),
            ("Appearance", self.appearance),
            ("Wardrobe", self.wardrobe),
            ("Age / presentation", self.age_presentation),
            ("Story context", self.story_context),
            ("Visual style", self.visual_style),
            ("Time / weather", self.time_weather),
            ("Set details", self.set_details),
        ):
            if value.strip():
                parts.append(f"{label}: {value.strip()}")
        if self.negative_constraints:
            parts.append("Avoid: " + "; ".join(self.negative_constraints))
        return "\n".join(parts)


class ReferenceGenerationAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=16, max_length=64)
    kind: ReferenceActionKind
    requirement: ReferenceRequirement
    reason: str = Field(min_length=1, max_length=2000)
    affected_shot_ids: tuple[str, ...] = ()
    candidate_id: str | None = Field(default=None, max_length=80)


class ReferenceGenerationAuthorization(BaseModel):
    """A bounded, user-visible authorization for a finite create set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    action_ids: tuple[str, ...] = Field(min_length=1)
    max_creates: int = Field(ge=1, le=100)
    approved_by: str = Field(min_length=1, max_length=160)
    approved: bool = False


class GeneratedReferenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=16, max_length=64)
    requirement: ReferenceRequirement
    candidate_id: str = Field(min_length=1, max_length=80)


class ReferenceReadiness(BaseModel):
    """Stable seam for Reference UI, AUTO, and later Continuity consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    source_revision_ids: dict[str, str] = Field(default_factory=dict)
    required: tuple[ReferenceRequirement, ...] = ()
    covered: tuple[ReferenceRequirement, ...] = ()
    missing: tuple[ReferenceRequirement, ...] = ()
    stale: tuple[ReferenceRequirement, ...] = ()
    blocked: tuple[str, ...] = ()
    next_actions: tuple[ReferenceGenerationAction, ...] = ()
    character_coverage: str = "0/0"
    location_coverage: str = "0/0"
    production_reference_ready: bool = False
    production_readiness: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "GeneratedReferenceCandidate",
    "ReferenceActionKind",
    "ReferenceBrief",
    "ReferenceCoverageStatus",
    "ReferenceGenerationAction",
    "ReferenceGenerationAuthorization",
    "ReferenceReadiness",
    "ReferenceRequirement",
    "ReferenceSubjectType",
]
