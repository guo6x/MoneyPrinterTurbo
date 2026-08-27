"""Provider-neutral cross-shot continuity contracts.

The models in this module describe durable production truth.  They contain no
provider client and deliberately separate expected facts from observations so
an AI observation can never become approved truth by accident.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContinuitySourceKind(str, Enum):
    HUMAN_LOCKED_STATE = "HUMAN_LOCKED_STATE"
    LOCKED_REFERENCE_ASSET = "LOCKED_REFERENCE_ASSET"
    FROZEN_PRODUCTION_INPUT = "FROZEN_PRODUCTION_INPUT"
    APPROVED_SHOT_PLAN = "APPROVED_SHOT_PLAN"
    PREVIOUS_APPROVED_SHOT = "PREVIOUS_APPROVED_SHOT"
    APPROVED_STRUCTURED_SCRIPT = "APPROVED_STRUCTURED_SCRIPT"
    APPROVED_STORY_BIBLE = "APPROVED_STORY_BIBLE"
    GENERATION_BRIEF = "GENERATION_BRIEF"
    VISION_QC_OBSERVATION = "VISION_QC_OBSERVATION"


SOURCE_PRECEDENCE: dict[ContinuitySourceKind, int] = {
    ContinuitySourceKind.HUMAN_LOCKED_STATE: 900,
    ContinuitySourceKind.LOCKED_REFERENCE_ASSET: 800,
    ContinuitySourceKind.FROZEN_PRODUCTION_INPUT: 700,
    ContinuitySourceKind.APPROVED_SHOT_PLAN: 600,
    ContinuitySourceKind.PREVIOUS_APPROVED_SHOT: 550,
    ContinuitySourceKind.APPROVED_STRUCTURED_SCRIPT: 500,
    ContinuitySourceKind.APPROVED_STORY_BIBLE: 400,
    ContinuitySourceKind.GENERATION_BRIEF: 300,
    ContinuitySourceKind.VISION_QC_OBSERVATION: 100,
}


class ContinuitySnapshotKind(str, Enum):
    EXPECTED = "EXPECTED"
    OBSERVED = "OBSERVED"


class ContinuityTimeOfDay(str, Enum):
    DAWN = "DAWN"
    DAY = "DAY"
    DUSK = "DUSK"
    NIGHT = "NIGHT"
    UNSPECIFIED = "UNSPECIFIED"


class ContinuityWeather(str, Enum):
    CLEAR = "CLEAR"
    OVERCAST = "OVERCAST"
    RAIN = "RAIN"
    SNOW = "SNOW"
    FOG = "FOG"
    STORM = "STORM"
    INDOOR = "INDOOR"
    UNSPECIFIED = "UNSPECIFIED"


class PropDisposition(str, Enum):
    CARRIED = "CARRIED"
    HELD = "HELD"
    WORN = "WORN"
    PLACED = "PLACED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ContinuityIssueType(str, Enum):
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    WARDROBE_DRIFT = "WARDROBE_DRIFT"
    HAIR_DRIFT = "HAIR_DRIFT"
    LOCATION_DRIFT = "LOCATION_DRIFT"
    LIGHTING_DRIFT = "LIGHTING_DRIFT"
    WEATHER_DRIFT = "WEATHER_DRIFT"
    PROP_DRIFT = "PROP_DRIFT"
    ACTION_DISCONTINUITY = "ACTION_DISCONTINUITY"
    STATE_DISCONTINUITY = "STATE_DISCONTINUITY"
    SHOT_INTENT_MISMATCH = "SHOT_INTENT_MISMATCH"


class ContinuitySeverity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ContinuitySubjectType(str, Enum):
    CHARACTER = "CHARACTER"
    LOCATION = "LOCATION"
    PROP = "PROP"
    NARRATIVE = "NARRATIVE"
    SHOT = "SHOT"


class ContinuityRepairability(str, Enum):
    ACCEPTABLE = "ACCEPTABLE"
    LOCAL = "LOCAL"
    PROMPT = "PROMPT"
    REFERENCE_REBIND = "REFERENCE_REBIND"
    REGENERATE = "REGENERATE"
    REPLAN = "REPLAN"
    MODEL_ESCALATION = "MODEL_ESCALATION"
    HUMAN_DECISION = "HUMAN_DECISION"


class RepairAction(str, Enum):
    ACCEPT = "ACCEPT"
    LOCAL_REPAIR = "LOCAL_REPAIR"
    PROMPT_REPAIR = "PROMPT_REPAIR"
    REFERENCE_REBIND = "REFERENCE_REBIND"
    REGENERATE_SHOT = "REGENERATE_SHOT"
    REPLAN_SHOT = "REPLAN_SHOT"
    MODEL_ESCALATION = "MODEL_ESCALATION"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


class EstimatedRepairScope(str, Enum):
    NONE = "NONE"
    LOCAL_ARTIFACT = "LOCAL_ARTIFACT"
    SINGLE_SHOT = "SINGLE_SHOT"
    SHOT_SEQUENCE = "SHOT_SEQUENCE"
    SHOT_PLAN = "SHOT_PLAN"


class ContinuitySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ContinuitySourceKind
    source_id: str = Field(min_length=1, max_length=160)
    revision_id: str | None = Field(default=None, max_length=80)
    locked: bool = False
    approved: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_authority_markers(self) -> "ContinuitySource":
        locked_kinds = {
            ContinuitySourceKind.HUMAN_LOCKED_STATE,
            ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
        }
        approved_kinds = {
            ContinuitySourceKind.HUMAN_LOCKED_STATE,
            ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
            ContinuitySourceKind.APPROVED_SHOT_PLAN,
            ContinuitySourceKind.PREVIOUS_APPROVED_SHOT,
            ContinuitySourceKind.APPROVED_STRUCTURED_SCRIPT,
            ContinuitySourceKind.APPROVED_STORY_BIBLE,
        }
        if self.kind in locked_kinds and not self.locked:
            raise ValueError("locked continuity sources must declare locked=true")
        if self.kind in approved_kinds and not self.approved:
            raise ValueError("approved continuity sources must declare approved=true")
        if self.kind is ContinuitySourceKind.VISION_QC_OBSERVATION and (
            self.locked or self.approved
        ):
            raise ValueError("AI observations cannot declare locked or approved truth")
        return self

    @property
    def precedence(self) -> int:
        return SOURCE_PRECEDENCE[self.kind]


class WardrobeItemState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=80)
    garment_type: str = Field(min_length=1, max_length=80)
    color: str = Field(default="", max_length=80)
    detail_keys: tuple[str, ...] = Field(default=(), max_length=20)


class PropContinuityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prop_id: str = Field(min_length=1, max_length=80)
    identity_key: str = Field(min_length=1, max_length=160)
    disposition: PropDisposition = PropDisposition.UNKNOWN
    holder_character_id: str | None = Field(default=None, max_length=64)
    position_key: str | None = Field(default=None, max_length=160)


class CharacterContinuityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    character_id: str = Field(min_length=1, max_length=64)
    identity_key: str | None = Field(default=None, max_length=500)
    appearance_key: str | None = Field(default=None, max_length=1000)
    hair_key: str | None = Field(default=None, max_length=500)
    wardrobe: tuple[WardrobeItemState, ...] | None = None
    age_presentation_key: str | None = Field(default=None, max_length=160)
    important_props: tuple[PropContinuityState, ...] | None = None
    physical_state_keys: tuple[str, ...] | None = Field(default=None, max_length=30)
    position_state_key: str | None = Field(default=None, max_length=500)


class LightingContinuityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quality_key: str | None = Field(default=None, max_length=120)
    direction_key: str | None = Field(default=None, max_length=120)
    tone_key: str | None = Field(default=None, max_length=120)


class LocationContinuityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: str = Field(min_length=1, max_length=64)
    time_of_day: ContinuityTimeOfDay | None = None
    weather: ContinuityWeather | None = None
    lighting: LightingContinuityState | None = None
    spatial_cue_keys: tuple[str, ...] | None = Field(default=None, max_length=50)
    set_dressing: tuple[PropContinuityState, ...] | None = None


class NarrativeContinuityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_action_key: str | None = Field(default=None, max_length=500)
    previous_action_key: str | None = Field(default=None, max_length=500)
    required_next_state_key: str | None = Field(default=None, max_length=500)
    carried_object_ids: tuple[str, ...] | None = Field(default=None, max_length=30)
    injury_state_keys: tuple[str, ...] | None = Field(default=None, max_length=30)
    story_beat_id: str | None = Field(default=None, max_length=80)
    shot_intent_key: str | None = Field(default=None, max_length=500)


class ShotRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_shot_id: str | None = Field(default=None, max_length=80)
    current_shot_id: str = Field(min_length=1, max_length=80)
    next_shot_id: str | None = Field(default=None, max_length=80)


class ContinuityFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    characters: tuple[CharacterContinuityState, ...] = ()
    location: LocationContinuityState | None = None
    narrative: NarrativeContinuityState | None = None
    shot_relationship: ShotRelationship

    @model_validator(mode="after")
    def unique_characters(self) -> "ContinuityFacts":
        character_ids = [item.character_id for item in self.characters]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("continuity character IDs must be unique")
        return self


class ContinuityStateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ContinuitySource
    facts: ContinuityFacts


class ContinuityFieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1, max_length=500)
    source: ContinuitySource


class ContinuitySourceConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1, max_length=500)
    sources: tuple[ContinuitySource, ...] = Field(min_length=2, max_length=10)
    canonical_values: tuple[str, ...] = Field(min_length=2, max_length=10)


class ContinuityShotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=64)
    script_revision_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=80)
    sequence_order: int = Field(ge=1)
    expected: tuple[ContinuityStateCandidate, ...] = ()
    observations: tuple[ContinuityStateCandidate, ...] = Field(min_length=1)
    execution_id: str | None = Field(default=None, max_length=64)
    artifact_id: str | None = Field(default=None, max_length=64)
    reference_version_ids: tuple[str, ...] = ()
    analysis_source: str = Field(default="DETERMINISTIC_CONTINUITY_ENGINE_V1", max_length=160)
    carry_forward_previous_approved: bool = True
    approved_for_continuity: bool = False
    approval_source_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_scope(self) -> "ContinuityShotRequest":
        candidates = self.expected + self.observations
        for candidate in candidates:
            current = candidate.facts.shot_relationship.current_shot_id
            if current != self.shot_id:
                raise ValueError("continuity candidate current_shot_id does not match request")
        if self.approved_for_continuity and not self.approval_source_id:
            raise ValueError(
                "approved continuity observations require approval_source_id"
            )
        if not self.approved_for_continuity and self.approval_source_id is not None:
            raise ValueError(
                "approval_source_id requires approved_for_continuity=true"
            )
        return self


class ContinuitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    snapshot_version: int = Field(default=1, ge=1)
    kind: ContinuitySnapshotKind
    project_id: str = Field(min_length=1, max_length=64)
    script_revision_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=80)
    sequence_order: int = Field(ge=1)
    execution_id: str | None = Field(default=None, max_length=64)
    artifact_id: str | None = Field(default=None, max_length=64)
    reference_version_ids: tuple[str, ...] = ()
    facts: ContinuityFacts
    field_provenance: tuple[ContinuityFieldProvenance, ...] = ()
    source_conflicts: tuple[ContinuitySourceConflict, ...] = ()
    analysis_source: str = Field(min_length=1, max_length=160)
    created_at: str = Field(min_length=1, max_length=80)


class AffectedContinuitySubject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_type: ContinuitySubjectType
    subject_id: str = Field(min_length=1, max_length=160)


class ContinuityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1, max_length=500)
    expected_value: str = Field(max_length=4000)
    observed_value: str = Field(max_length=4000)
    expected_source_ids: tuple[str, ...] = ()
    observed_source_ids: tuple[str, ...] = ()


class ContinuityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    expected_snapshot_id: str = Field(min_length=1, max_length=64)
    observed_snapshot_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    script_revision_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=80)
    execution_id: str | None = Field(default=None, max_length=64)
    artifact_id: str | None = Field(default=None, max_length=64)
    reference_version_ids: tuple[str, ...] = ()
    analysis_source: str = Field(min_length=1, max_length=160)
    issue_type: ContinuityIssueType
    severity: ContinuitySeverity
    confidence: float = Field(ge=0.0, le=1.0)
    affected_subject: AffectedContinuitySubject
    evidence: tuple[ContinuityEvidence, ...] = Field(min_length=1)
    source: tuple[ContinuitySource, ...] = Field(min_length=1)
    repairability: ContinuityRepairability
    created_at: str = Field(min_length=1, max_length=80)


class RepairEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    code: str = Field(min_length=1, max_length=120)
    conditions: tuple[str, ...] = Field(default=(), max_length=20)


class RepairPolicyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_failed_repair_attempts: int = Field(default=0, ge=0)
    current_model_capability_sufficient: bool = True


class RepairRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    issue_ids: tuple[str, ...] = ()
    project_id: str = Field(min_length=1, max_length=64)
    script_revision_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=80)
    execution_id: str | None = Field(default=None, max_length=64)
    artifact_id: str | None = Field(default=None, max_length=64)
    reference_version_ids: tuple[str, ...] = ()
    analysis_source: str = Field(min_length=1, max_length=160)
    action: RepairAction
    eligibility: RepairEligibility
    rationale: str = Field(min_length=1, max_length=2000)
    requires_paid_create: bool
    estimated_scope: EstimatedRepairScope
    requires_human_confirmation: bool
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def paid_create_requires_confirmation(self) -> "RepairRecommendation":
        if self.requires_paid_create and not self.requires_human_confirmation:
            raise ValueError("paid repair recommendations require human confirmation")
        return self


class ContinuityEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_snapshot: ContinuitySnapshot
    observed_snapshot: ContinuitySnapshot
    issues: tuple[ContinuityIssue, ...]
    repair_recommendations: tuple[RepairRecommendation, ...]


class ContinuityWarningProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str
    issue_type: ContinuityIssueType
    severity: ContinuitySeverity
    shot_id: str
    subject_label: str
    recommended_action: RepairAction
    requires_human_confirmation: bool


class ContinuityUIProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    shot_id: str
    status: str = Field(pattern=r"^(PASS|WARNING|BLOCKED)$")
    warnings: tuple[ContinuityWarningProjection, ...]


__all__ = [
    "AffectedContinuitySubject",
    "CharacterContinuityState",
    "ContinuityEvaluationResult",
    "ContinuityEvidence",
    "ContinuityFacts",
    "ContinuityFieldProvenance",
    "ContinuityIssue",
    "ContinuityIssueType",
    "ContinuityRepairability",
    "ContinuitySeverity",
    "ContinuityShotRequest",
    "ContinuitySnapshot",
    "ContinuitySnapshotKind",
    "ContinuitySource",
    "ContinuitySourceConflict",
    "ContinuitySourceKind",
    "ContinuityStateCandidate",
    "ContinuitySubjectType",
    "ContinuityTimeOfDay",
    "ContinuityUIProjection",
    "ContinuityWarningProjection",
    "ContinuityWeather",
    "EstimatedRepairScope",
    "LightingContinuityState",
    "LocationContinuityState",
    "NarrativeContinuityState",
    "PropContinuityState",
    "PropDisposition",
    "RepairAction",
    "RepairEligibility",
    "RepairPolicyContext",
    "RepairRecommendation",
    "ShotRelationship",
    "SOURCE_PRECEDENCE",
    "WardrobeItemState",
]
