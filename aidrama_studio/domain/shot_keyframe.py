"""Immutable provider-neutral shot keyframe and first-frame contracts.

Reference assets constrain identity and style.  A :class:`ShotFirstFrame` is a
separate, exact image artifact that defines the literal visual start of one
shot.  Provider adapters must never infer the latter from the former.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .continuity import ContinuityFacts
from .reference_asset import ReferenceAssetType
from .shot import CameraAngle, CameraMovement, ShotSize


Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageMimeType = Annotated[
    str,
    Field(pattern=r"^image/[a-z0-9][a-z0-9.+-]*$", max_length=100),
]
VideoMimeType = Annotated[
    str,
    Field(pattern=r"^video/[a-z0-9][a-z0-9.+-]*$", max_length=100),
]

_UNSAFE_TEXT_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE),
    re.compile(r"(?:^|\s)[A-Za-z]:[\\/]"),
    re.compile(r"(?:^|\s)\\\\"),
    re.compile(r"(?:^|\s)/(?:[A-Za-z0-9._-]+/)+"),
    re.compile(r"\b(?:authorization|api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bX-Amz-(?:Signature|Credential)\s*=", re.IGNORECASE),
)


def _reject_unsafe_text(value: object) -> object:
    """Reject accidental URLs, private absolute paths, and credential values."""

    if isinstance(value, BaseModel):
        return _reject_unsafe_text(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_unsafe_text(item)
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_unsafe_text(item)
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("/", "~/", "~\\")) or any(
            pattern.search(text) for pattern in _UNSAFE_TEXT_PATTERNS
        ):
            raise ValueError(
                "shot keyframe contracts cannot contain URLs, absolute paths, "
                "signed URLs, or plaintext credentials"
            )
    return value


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def reject_unsafe_contract_text(cls, value: object) -> object:
        return _reject_unsafe_text(value)


class ShotFirstFrameSourceType(str, Enum):
    GENERATED_KEYFRAME = "GENERATED_KEYFRAME"
    PREVIOUS_SHOT_LAST_FRAME = "PREVIOUS_SHOT_LAST_FRAME"
    USER_PROVIDED = "USER_PROVIDED"
    EXPLICIT_REFERENCE_OVERRIDE = "EXPLICIT_REFERENCE_OVERRIDE"


class ShotKeyframeReferenceRole(str, Enum):
    IDENTITY = "IDENTITY"
    LOCATION = "LOCATION"
    PROP = "PROP"
    STYLE = "STYLE"


class ShotKeyframeSelectionPolicy(str, Enum):
    NEW_SCENE = "NEW_SCENE"
    NEW_COMPOSITION = "NEW_COMPOSITION"
    CONTINUOUS_ACTION_COMPATIBLE_COMPOSITION = (
        "CONTINUOUS_ACTION_COMPATIBLE_COMPOSITION"
    )
    USER_APPROVED_STARTING_IMAGE = "USER_APPROVED_STARTING_IMAGE"
    EXPLICIT_REFERENCE_OVERRIDE = "EXPLICIT_REFERENCE_OVERRIDE"


class PreLiveFirstFrameGate(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class ShotKeyframeRepairAction(str, Enum):
    REGENERATE_KEYFRAME = "REGENERATE_KEYFRAME"
    REPLAN_SHOT = "REPLAN_SHOT"
    HUMAN_DECISION = "HUMAN_DECISION"


class ShotKeyframeRepairScope(str, Enum):
    SINGLE_KEYFRAME = "SINGLE_KEYFRAME"
    SINGLE_SHOT = "SINGLE_SHOT"
    SHOT_SEQUENCE = "SHOT_SEQUENCE"
    SHOT_PLAN = "SHOT_PLAN"


class ReferenceProvenance(_FrozenContract):
    """A locked reference constraint, never an implicit literal first frame."""

    role: ShotKeyframeReferenceRole
    asset_id: Identifier
    asset_version_id: Identifier
    asset_type: ReferenceAssetType
    sha256: Sha256
    binding_id: Identifier | None = None
    subject_id: Identifier | None = None
    stable_description: str = Field(min_length=1, max_length=4000)
    locked: Literal[True] = True

    @model_validator(mode="after")
    def role_matches_reference_type(self) -> "ReferenceProvenance":
        expected = {
            ShotKeyframeReferenceRole.IDENTITY: ReferenceAssetType.CHARACTER_REFERENCE,
            ShotKeyframeReferenceRole.LOCATION: ReferenceAssetType.LOCATION_REFERENCE,
            ShotKeyframeReferenceRole.PROP: ReferenceAssetType.PROP_REFERENCE,
            ShotKeyframeReferenceRole.STYLE: ReferenceAssetType.STYLE_REFERENCE,
        }
        if self.asset_type is not expected[self.role]:
            raise ValueError("reference role does not match historical asset type")
        return self


class PreviousApprovedArtifactProvenance(_FrozenContract):
    """Exact approved video artifact from which a last frame was extracted."""

    project_id: Identifier
    source_shot_id: Identifier
    source_execution_id: Identifier
    source_artifact_id: Identifier
    source_artifact_sha256: Sha256
    source_artifact_mime_type: VideoMimeType
    approval_source_id: Identifier
    extracted_last_frame_sha256: Sha256
    approved: Literal[True] = True


class UserProvidedSourceProvenance(_FrozenContract):
    """Exact user-approved image ingested as a first-frame source."""

    source_artifact_id: Identifier
    source_sha256: Sha256
    source_mime_type: ImageMimeType
    approval_source_id: Identifier


class ShotKeyframeSubject(_FrozenContract):
    subject_id: Identifier
    stable_identity_description: str = Field(min_length=1, max_length=4000)
    pose_or_state: str = Field(default="", max_length=2000)


class ShotKeyframeLighting(_FrozenContract):
    quality: str = Field(min_length=1, max_length=500)
    direction: str = Field(default="", max_length=500)
    tone: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)


class ShotKeyframeLocation(_FrozenContract):
    location_id: Identifier
    stable_description: str = Field(min_length=1, max_length=4000)
    spatial_cues: tuple[str, ...] = Field(default=(), max_length=50)


class PreviousApprovedShotContext(_FrozenContract):
    shot_id: Identifier
    approval_source_id: Identifier
    visual_summary: str = Field(min_length=1, max_length=4000)
    shot_size: ShotSize
    camera_angle: CameraAngle
    camera_movement: CameraMovement
    composition: str = Field(min_length=1, max_length=2000)
    action: str = Field(default="", max_length=2000)
    artifact_provenance: PreviousApprovedArtifactProvenance | None = None

    @model_validator(mode="after")
    def artifact_matches_context(self) -> "PreviousApprovedShotContext":
        if (
            self.artifact_provenance is not None
            and self.artifact_provenance.source_shot_id != self.shot_id
        ):
            raise ValueError("previous-shot artifact does not match context shot")
        return self


class ShotKeyframeBrief(_FrozenContract):
    """Provider-neutral IMAGE request intent compiled from approved truth."""

    id: Identifier
    project_id: Identifier
    shot_id: Identifier
    shot_plan_revision_id: Identifier
    generation_brief_id: Identifier
    generation_brief_sha256: Sha256
    shot_visual_intent: str = Field(min_length=1, max_length=4000)
    shot_size: ShotSize
    camera_angle: CameraAngle
    camera_movement: CameraMovement
    composition: str = Field(min_length=1, max_length=2000)
    subjects: tuple[ShotKeyframeSubject, ...] = Field(default=(), max_length=100)
    action: str = Field(min_length=1, max_length=4000)
    lighting: ShotKeyframeLighting
    location: ShotKeyframeLocation
    identity_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    location_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    prop_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    style_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    continuity_facts: ContinuityFacts | None = None
    continuity_constraints: tuple[str, ...] = Field(default=(), max_length=100)
    previous_approved_shot_context: PreviousApprovedShotContext | None = None
    negative_constraints: tuple[str, ...] = Field(default=(), max_length=100)
    sha256: Sha256

    @model_validator(mode="after")
    def validate_brief_scope_and_provenance(self) -> "ShotKeyframeBrief":
        subject_ids = [item.subject_id for item in self.subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("shot keyframe subject IDs must be unique")

        buckets = (
            (ShotKeyframeReferenceRole.IDENTITY, self.identity_reference_provenance),
            (ShotKeyframeReferenceRole.LOCATION, self.location_reference_provenance),
            (ShotKeyframeReferenceRole.PROP, self.prop_reference_provenance),
            (ShotKeyframeReferenceRole.STYLE, self.style_reference_provenance),
        )
        version_ids: list[str] = []
        for expected_role, references in buckets:
            if any(item.role is not expected_role for item in references):
                raise ValueError("reference provenance is stored in the wrong role bucket")
            version_ids.extend(item.asset_version_id for item in references)
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("reference asset versions must be unique across a keyframe brief")

        if self.continuity_facts is not None:
            current_shot_id = self.continuity_facts.shot_relationship.current_shot_id
            if current_shot_id != self.shot_id:
                raise ValueError("continuity facts do not match keyframe brief shot")
        if (
            self.previous_approved_shot_context is not None
            and self.previous_approved_shot_context.shot_id == self.shot_id
        ):
            raise ValueError("a shot cannot use itself as previous approved context")
        return self


class ShotKeyframeSelection(_FrozenContract):
    """Deterministic, auditable result of first-frame planning policy."""

    project_id: Identifier
    shot_id: Identifier
    policy: ShotKeyframeSelectionPolicy
    source_type: ShotFirstFrameSourceType
    reason: str = Field(min_length=1, max_length=2000)
    previous_shot_id: Identifier | None = None
    user_source_artifact_id: Identifier | None = None
    literal_reference_version_id: Identifier | None = None
    literal_reuse_authorization_id: Identifier | None = None
    deterministic: Literal[True] = True

    @model_validator(mode="after")
    def validate_source_selection(self) -> "ShotKeyframeSelection":
        expected_source = {
            ShotKeyframeSelectionPolicy.NEW_SCENE: ShotFirstFrameSourceType.GENERATED_KEYFRAME,
            ShotKeyframeSelectionPolicy.NEW_COMPOSITION: ShotFirstFrameSourceType.GENERATED_KEYFRAME,
            ShotKeyframeSelectionPolicy.CONTINUOUS_ACTION_COMPATIBLE_COMPOSITION: ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME,
            ShotKeyframeSelectionPolicy.USER_APPROVED_STARTING_IMAGE: ShotFirstFrameSourceType.USER_PROVIDED,
            ShotKeyframeSelectionPolicy.EXPLICIT_REFERENCE_OVERRIDE: ShotFirstFrameSourceType.EXPLICIT_REFERENCE_OVERRIDE,
        }
        if self.source_type is not expected_source[self.policy]:
            raise ValueError("selection policy and first-frame source type disagree")

        literal_fields = (
            self.previous_shot_id,
            self.user_source_artifact_id,
            self.literal_reference_version_id,
            self.literal_reuse_authorization_id,
        )
        if self.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME:
            if any(value is not None for value in literal_fields):
                raise ValueError(
                    "generated keyframes cannot use references or other artifacts "
                    "as literal first frames"
                )
        elif self.source_type is ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME:
            if not self.previous_shot_id or not self.literal_reuse_authorization_id:
                raise ValueError("previous-last-frame selection requires shot and authorization")
            if self.user_source_artifact_id or self.literal_reference_version_id:
                raise ValueError("previous-last-frame selection has conflicting literal source")
        elif self.source_type is ShotFirstFrameSourceType.USER_PROVIDED:
            if not self.user_source_artifact_id or not self.literal_reuse_authorization_id:
                raise ValueError("user-provided selection requires artifact and authorization")
            if self.previous_shot_id or self.literal_reference_version_id:
                raise ValueError("user-provided selection has conflicting literal source")
        else:
            if not self.literal_reference_version_id or not self.literal_reuse_authorization_id:
                raise ValueError("reference override requires exact version and authorization")
            if self.previous_shot_id or self.user_source_artifact_id:
                raise ValueError("reference override has conflicting literal source")
        return self

    @property
    def requires_image_generation(self) -> bool:
        return self.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME


class ShotFirstFrame(_FrozenContract):
    """Exact frozen image artifact supplied to VIDEO as its literal first frame."""

    id: Identifier
    project_id: Identifier
    shot_id: Identifier
    shot_plan_revision_id: Identifier
    generation_brief_id: Identifier
    shot_keyframe_brief_id: Identifier
    shot_keyframe_brief_sha256: Sha256
    artifact_id: Identifier
    artifact_type: Literal["SHOT_FIRST_FRAME"] = "SHOT_FIRST_FRAME"
    execution_id: Identifier
    artifact_size_bytes: int = Field(gt=0)
    sha256: Sha256
    mime_type: ImageMimeType
    source_type: ShotFirstFrameSourceType
    selection: ShotKeyframeSelection
    identity_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    location_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    prop_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    style_reference_provenance: tuple[ReferenceProvenance, ...] = ()
    previous_shot_provenance: PreviousApprovedArtifactProvenance | None = None
    user_provided_provenance: UserProvidedSourceProvenance | None = None
    literal_reference_override_version_id: Identifier | None = None
    literal_reuse_authorization_id: Identifier | None = None
    created_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_frozen_literal_source(self) -> "ShotFirstFrame":
        if self.selection.project_id != self.project_id or self.selection.shot_id != self.shot_id:
            raise ValueError("first-frame selection does not match artifact scope")
        if self.selection.source_type is not self.source_type:
            raise ValueError("first-frame source does not match frozen selection")
        if self.selection.literal_reuse_authorization_id != self.literal_reuse_authorization_id:
            raise ValueError("literal reuse authorization does not match frozen selection")

        references = (
            self.identity_reference_provenance
            + self.location_reference_provenance
            + self.prop_reference_provenance
            + self.style_reference_provenance
        )
        version_ids = [item.asset_version_id for item in references]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("first-frame reference provenance versions must be unique")

        if self.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME:
            if any(
                value is not None
                for value in (
                    self.previous_shot_provenance,
                    self.user_provided_provenance,
                    self.literal_reference_override_version_id,
                    self.literal_reuse_authorization_id,
                )
            ):
                raise ValueError("generated keyframe cannot declare a literal reused source")
        elif self.source_type is ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME:
            provenance = self.previous_shot_provenance
            if provenance is None or self.literal_reuse_authorization_id is None:
                raise ValueError("previous-last-frame source requires approved provenance")
            if provenance.project_id != self.project_id:
                raise ValueError("previous-last-frame provenance crosses project boundary")
            if provenance.source_shot_id != self.selection.previous_shot_id:
                raise ValueError("previous-last-frame provenance does not match selection")
            if provenance.extracted_last_frame_sha256 != self.sha256:
                raise ValueError("first-frame SHA does not match extracted last frame")
            if self.user_provided_provenance or self.literal_reference_override_version_id:
                raise ValueError("previous-last-frame source has conflicting provenance")
        elif self.source_type is ShotFirstFrameSourceType.USER_PROVIDED:
            provenance = self.user_provided_provenance
            if provenance is None or self.literal_reuse_authorization_id is None:
                raise ValueError("user-provided first frame requires approved provenance")
            if provenance.source_artifact_id != self.selection.user_source_artifact_id:
                raise ValueError("user-provided artifact does not match selection")
            if provenance.source_sha256 != self.sha256:
                raise ValueError("user-provided source SHA does not match first frame")
            if self.previous_shot_provenance or self.literal_reference_override_version_id:
                raise ValueError("user-provided source has conflicting provenance")
        else:
            version_id = self.literal_reference_override_version_id
            if version_id is None or self.literal_reuse_authorization_id is None:
                raise ValueError("explicit reference override requires exact authorized version")
            if version_id != self.selection.literal_reference_version_id:
                raise ValueError("literal reference override does not match selection")
            if version_id not in version_ids:
                raise ValueError("literal reference override lacks reference provenance")
            if self.previous_shot_provenance or self.user_provided_provenance:
                raise ValueError("explicit reference override has conflicting provenance")
        return self


class DuplicateFirstFrameGroup(_FrozenContract):
    sha256: Sha256
    shot_ids: tuple[Identifier, ...] = Field(min_length=2)
    first_frame_ids: tuple[Identifier, ...] = Field(min_length=2)
    artifact_ids: tuple[Identifier, ...] = Field(min_length=2)
    explicit_creative_override_id: Identifier | None = None
    blocking: bool

    @model_validator(mode="after")
    def validate_duplicate_group(self) -> "DuplicateFirstFrameGroup":
        lengths = {len(self.shot_ids), len(self.first_frame_ids), len(self.artifact_ids)}
        if len(lengths) != 1:
            raise ValueError("duplicate group member lists must align")
        if len(set(self.shot_ids)) != len(self.shot_ids):
            raise ValueError("duplicate group shot IDs must be unique")
        if self.blocking != (self.explicit_creative_override_id is None):
            raise ValueError("duplicate first frames block unless explicitly overridden")
        return self


class PreLiveFirstFrameReport(_FrozenContract):
    project_id: Identifier
    shot_plan_revision_id: Identifier
    gate: PreLiveFirstFrameGate
    planned_shot_ids: tuple[Identifier, ...]
    validated_first_frame_ids: tuple[Identifier, ...]
    missing_first_frame_shot_ids: tuple[Identifier, ...] = ()
    invalid_first_frame_shot_ids: tuple[Identifier, ...] = ()
    duplicate_groups: tuple[DuplicateFirstFrameGroup, ...] = ()
    unintended_duplicate_first_frame_count: int = Field(ge=0)
    blocking_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_gate(self) -> "PreLiveFirstFrameReport":
        blocking_duplicates = sum(group.blocking for group in self.duplicate_groups)
        if self.unintended_duplicate_first_frame_count != blocking_duplicates:
            raise ValueError("unintended duplicate count must equal blocking duplicate groups")
        has_blocker = bool(
            blocking_duplicates
            or self.missing_first_frame_shot_ids
            or self.invalid_first_frame_shot_ids
        )
        if self.gate is PreLiveFirstFrameGate.PASS and has_blocker:
            raise ValueError("pre-live first-frame gate cannot pass with blockers")
        if self.gate is PreLiveFirstFrameGate.BLOCKED and not has_blocker:
            raise ValueError("blocked pre-live first-frame gate requires a blocker")
        if has_blocker and not self.blocking_reasons:
            raise ValueError("blocked pre-live first-frame report requires reasons")
        return self


class ShotKeyframeRepairRecommendation(_FrozenContract):
    id: Identifier
    project_id: Identifier
    shot_ids: tuple[Identifier, ...] = Field(min_length=1)
    action: ShotKeyframeRepairAction
    reason: str = Field(min_length=1, max_length=2000)
    estimated_scope: ShotKeyframeRepairScope
    requires_paid_create: bool
    requires_human_confirmation: Literal[True] = True
    auto_execute: Literal[False] = False


__all__ = [
    "DuplicateFirstFrameGroup",
    "PreLiveFirstFrameGate",
    "PreLiveFirstFrameReport",
    "PreviousApprovedArtifactProvenance",
    "PreviousApprovedShotContext",
    "ReferenceProvenance",
    "ShotFirstFrame",
    "ShotFirstFrameSourceType",
    "ShotKeyframeBrief",
    "ShotKeyframeLighting",
    "ShotKeyframeLocation",
    "ShotKeyframeReferenceRole",
    "ShotKeyframeRepairAction",
    "ShotKeyframeRepairRecommendation",
    "ShotKeyframeRepairScope",
    "ShotKeyframeSelection",
    "ShotKeyframeSelectionPolicy",
    "ShotKeyframeSubject",
    "UserProvidedSourceProvenance",
]
