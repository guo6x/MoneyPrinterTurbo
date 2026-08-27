"""Deterministic, provider-neutral continuity evaluation and repair policy."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from aidrama_studio.domain.continuity import (
    AffectedContinuitySubject,
    CharacterContinuityState,
    ContinuityEvaluationResult,
    ContinuityEvidence,
    ContinuityFacts,
    ContinuityFieldProvenance,
    ContinuityIssue,
    ContinuityIssueType,
    ContinuityRepairability,
    ContinuitySeverity,
    ContinuityShotRequest,
    ContinuitySnapshot,
    ContinuitySnapshotKind,
    ContinuitySource,
    ContinuitySourceConflict,
    ContinuitySourceKind,
    ContinuityStateCandidate,
    ContinuitySubjectType,
    ContinuityUIProjection,
    ContinuityWarningProjection,
    EstimatedRepairScope,
    LightingContinuityState,
    LocationContinuityState,
    NarrativeContinuityState,
    RepairAction,
    RepairEligibility,
    RepairPolicyContext,
    RepairRecommendation,
    SOURCE_PRECEDENCE,
    ShotRelationship,
)
from aidrama_studio.storage.repositories import ProjectRepository


SEVERITY_RANK = {
    ContinuitySeverity.INFO: 0,
    ContinuitySeverity.LOW: 1,
    ContinuitySeverity.MEDIUM: 2,
    ContinuitySeverity.HIGH: 3,
    ContinuitySeverity.CRITICAL: 4,
}

ISSUE_POLICY = {
    ContinuityIssueType.IDENTITY_DRIFT: (
        ContinuitySeverity.CRITICAL,
        ContinuityRepairability.REFERENCE_REBIND,
    ),
    ContinuityIssueType.WARDROBE_DRIFT: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REGENERATE,
    ),
    ContinuityIssueType.HAIR_DRIFT: (
        ContinuitySeverity.MEDIUM,
        ContinuityRepairability.PROMPT,
    ),
    ContinuityIssueType.LOCATION_DRIFT: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REGENERATE,
    ),
    ContinuityIssueType.LIGHTING_DRIFT: (
        ContinuitySeverity.LOW,
        ContinuityRepairability.LOCAL,
    ),
    ContinuityIssueType.WEATHER_DRIFT: (
        ContinuitySeverity.MEDIUM,
        ContinuityRepairability.PROMPT,
    ),
    ContinuityIssueType.PROP_DRIFT: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REGENERATE,
    ),
    ContinuityIssueType.ACTION_DISCONTINUITY: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REGENERATE,
    ),
    ContinuityIssueType.STATE_DISCONTINUITY: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REGENERATE,
    ),
    ContinuityIssueType.SHOT_INTENT_MISMATCH: (
        ContinuitySeverity.HIGH,
        ContinuityRepairability.REPLAN,
    ),
}


class ContinuityEngineError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_value(value: Any) -> str:
    value = _json_ready(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _normalized(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        items = [_normalized(item) for item in value]
        return sorted(items, key=_json_value)
    if isinstance(value, str):
        return " ".join(value.strip().casefold().split())
    return value


def _same(expected: Any, observed: Any) -> bool:
    return _normalized(expected) == _normalized(observed)


class _FactMerger:
    def __init__(self, current_shot_id: str):
        self.current_shot_id = current_shot_id
        self.values: dict[str, Any] = {}
        self.sources: dict[str, ContinuitySource] = {}
        self.conflicts: list[ContinuitySourceConflict] = []

    def assign(self, path: str, value: Any, source: ContinuitySource) -> None:
        if value is None:
            return
        previous_source = self.sources.get(path)
        if previous_source is not None:
            previous_value = self.values[path]
            if (
                previous_source.precedence == source.precedence
                and not _same(previous_value, value)
            ):
                self.conflicts.append(
                    ContinuitySourceConflict(
                        field_path=path,
                        sources=(previous_source, source),
                        canonical_values=(
                            _json_value(previous_value),
                            _json_value(value),
                        ),
                    )
                )
        self.values[path] = value
        self.sources[path] = source

    def add(self, candidate: ContinuityStateCandidate) -> None:
        source = candidate.source
        facts = candidate.facts
        for character in facts.characters:
            prefix = f"characters.{character.character_id}"
            for field in (
                "identity_key",
                "appearance_key",
                "hair_key",
                "wardrobe",
                "age_presentation_key",
                "important_props",
                "physical_state_keys",
                "position_state_key",
            ):
                self.assign(f"{prefix}.{field}", getattr(character, field), source)
        if facts.location is not None:
            location = facts.location
            self.assign("location.location_id", location.location_id, source)
            self.assign("location.time_of_day", location.time_of_day, source)
            self.assign("location.weather", location.weather, source)
            self.assign("location.spatial_cue_keys", location.spatial_cue_keys, source)
            self.assign("location.set_dressing", location.set_dressing, source)
            if location.lighting is not None:
                for field in ("quality_key", "direction_key", "tone_key"):
                    self.assign(
                        f"location.lighting.{field}",
                        getattr(location.lighting, field),
                        source,
                    )
        if facts.narrative is not None:
            for field in (
                "current_action_key",
                "previous_action_key",
                "required_next_state_key",
                "carried_object_ids",
                "injury_state_keys",
                "story_beat_id",
                "shot_intent_key",
            ):
                self.assign(
                    f"narrative.{field}", getattr(facts.narrative, field), source
                )
        relationship = facts.shot_relationship
        self.assign(
            "shot_relationship.previous_shot_id",
            relationship.previous_shot_id,
            source,
        )
        self.assign(
            "shot_relationship.current_shot_id",
            relationship.current_shot_id,
            source,
        )
        self.assign(
            "shot_relationship.next_shot_id", relationship.next_shot_id, source
        )

    def build(self) -> tuple[
        ContinuityFacts,
        tuple[ContinuityFieldProvenance, ...],
        tuple[ContinuitySourceConflict, ...],
    ]:
        characters: list[CharacterContinuityState] = []
        character_ids = sorted(
            {
                path.split(".")[1]
                for path in self.values
                if path.startswith("characters.")
            }
        )
        for character_id in character_ids:
            prefix = f"characters.{character_id}."
            fields = {
                path.removeprefix(prefix): value
                for path, value in self.values.items()
                if path.startswith(prefix)
            }
            characters.append(
                CharacterContinuityState(character_id=character_id, **fields)
            )

        lighting_values = {
            path.removeprefix("location.lighting."): value
            for path, value in self.values.items()
            if path.startswith("location.lighting.")
        }
        location_values = {
            path.removeprefix("location."): value
            for path, value in self.values.items()
            if path.startswith("location.") and not path.startswith("location.lighting.")
        }
        if lighting_values:
            location_values["lighting"] = LightingContinuityState(**lighting_values)
        location = (
            LocationContinuityState(**location_values)
            if "location_id" in location_values
            else None
        )

        narrative_values = {
            path.removeprefix("narrative."): value
            for path, value in self.values.items()
            if path.startswith("narrative.")
        }
        narrative = (
            NarrativeContinuityState(**narrative_values)
            if narrative_values
            else None
        )
        relationship_values = {
            path.removeprefix("shot_relationship."): value
            for path, value in self.values.items()
            if path.startswith("shot_relationship.")
        }
        relationship_values.setdefault("current_shot_id", self.current_shot_id)
        relationship = ShotRelationship(**relationship_values)
        facts = ContinuityFacts(
            characters=tuple(characters),
            location=location,
            narrative=narrative,
            shot_relationship=relationship,
        )
        provenance = tuple(
            ContinuityFieldProvenance(field_path=path, source=self.sources[path])
            for path in sorted(self.sources)
        )
        unique_conflicts = {
            (
                item.field_path,
                tuple(source.source_id for source in item.sources),
                item.canonical_values,
            ): item
            for item in self.conflicts
        }
        return facts, provenance, tuple(unique_conflicts.values())


class ContinuityEngine:
    """Stable facade for deterministic shot and sequence evaluation."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository or ProjectRepository()
        self.clock = clock
        self.id_factory = id_factory or (lambda: uuid4().hex)

    @staticmethod
    def source_precedence() -> tuple[ContinuitySourceKind, ...]:
        return tuple(
            sorted(
                ContinuitySourceKind,
                key=SOURCE_PRECEDENCE.__getitem__,
                reverse=True,
            )
        )

    def _snapshot(
        self,
        request: ContinuityShotRequest,
        kind: ContinuitySnapshotKind,
        candidates: Iterable[ContinuityStateCandidate],
        created_at: str,
    ) -> ContinuitySnapshot:
        ordered = sorted(
            enumerate(candidates),
            key=lambda item: (item[1].source.precedence, item[0]),
        )
        merger = _FactMerger(request.shot_id)
        for _, candidate in ordered:
            merger.add(candidate)
        facts, provenance, conflicts = merger.build()
        return ContinuitySnapshot(
            id=self.id_factory(),
            kind=kind,
            project_id=request.project_id,
            script_revision_id=request.script_revision_id,
            shot_plan_revision_id=request.shot_plan_revision_id,
            shot_id=request.shot_id,
            sequence_order=request.sequence_order,
            execution_id=request.execution_id,
            artifact_id=request.artifact_id,
            reference_version_ids=request.reference_version_ids,
            facts=facts,
            field_provenance=provenance,
            source_conflicts=conflicts,
            analysis_source=request.analysis_source,
            created_at=created_at,
        )

    @staticmethod
    def _provenance(snapshot: ContinuitySnapshot) -> dict[str, ContinuitySource]:
        return {item.field_path: item.source for item in snapshot.field_provenance}

    def _make_issue(
        self,
        *,
        expected: ContinuitySnapshot,
        observed: ContinuitySnapshot,
        issue_type: ContinuityIssueType,
        subject_type: ContinuitySubjectType,
        subject_id: str,
        path: str,
        expected_value: Any,
        observed_value: Any,
        created_at: str,
        repairability: ContinuityRepairability | None = None,
        confidence: float | None = None,
        source_override: tuple[ContinuitySource, ...] | None = None,
    ) -> ContinuityIssue:
        expected_sources = self._provenance(expected)
        observed_sources = self._provenance(observed)
        selected_sources = source_override or tuple(
            {
                source.source_id: source
                for source in (
                    expected_sources.get(path),
                    observed_sources.get(path),
                )
                if source is not None
            }.values()
        )
        if not selected_sources:
            selected_sources = (
                ContinuitySource(
                    kind=ContinuitySourceKind.VISION_QC_OBSERVATION,
                    source_id=observed.analysis_source,
                    confidence=confidence if confidence is not None else 1.0,
                ),
            )
        observed_source = observed_sources.get(path)
        issue_confidence = confidence
        if issue_confidence is None:
            issue_confidence = observed_source.confidence if observed_source else 1.0
        severity, default_repairability = ISSUE_POLICY[issue_type]
        return ContinuityIssue(
            id=self.id_factory(),
            expected_snapshot_id=expected.id,
            observed_snapshot_id=observed.id,
            project_id=observed.project_id,
            script_revision_id=observed.script_revision_id,
            shot_plan_revision_id=observed.shot_plan_revision_id,
            shot_id=observed.shot_id,
            execution_id=observed.execution_id,
            artifact_id=observed.artifact_id,
            reference_version_ids=observed.reference_version_ids,
            analysis_source=observed.analysis_source,
            issue_type=issue_type,
            severity=severity,
            confidence=issue_confidence,
            affected_subject=AffectedContinuitySubject(
                subject_type=subject_type, subject_id=subject_id
            ),
            evidence=(
                ContinuityEvidence(
                    field_path=path,
                    expected_value=_json_value(expected_value),
                    observed_value=_json_value(observed_value),
                    expected_source_ids=(
                        (expected_sources[path].source_id,)
                        if path in expected_sources
                        else ()
                    ),
                    observed_source_ids=(
                        (observed_sources[path].source_id,)
                        if path in observed_sources
                        else ()
                    ),
                ),
            ),
            source=selected_sources,
            repairability=repairability or default_repairability,
            created_at=created_at,
        )

    def _compare(
        self,
        expected: ContinuitySnapshot,
        observed: ContinuitySnapshot,
        created_at: str,
    ) -> tuple[ContinuityIssue, ...]:
        issues: list[ContinuityIssue] = []

        def compare_field(
            issue_type: ContinuityIssueType,
            subject_type: ContinuitySubjectType,
            subject_id: str,
            path: str,
            expected_value: Any,
            observed_value: Any,
        ) -> None:
            if expected_value is None or _same(expected_value, observed_value):
                return
            issues.append(
                self._make_issue(
                    expected=expected,
                    observed=observed,
                    issue_type=issue_type,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    path=path,
                    expected_value=expected_value,
                    observed_value=observed_value,
                    created_at=created_at,
                )
            )

        observed_characters = {
            item.character_id: item for item in observed.facts.characters
        }
        for character in expected.facts.characters:
            observed_character = observed_characters.get(character.character_id)
            values = (
                observed_character
                if observed_character is not None
                else CharacterContinuityState(character_id=character.character_id)
            )
            prefix = f"characters.{character.character_id}"
            fields = (
                (ContinuityIssueType.IDENTITY_DRIFT, "identity_key"),
                (ContinuityIssueType.IDENTITY_DRIFT, "appearance_key"),
                (ContinuityIssueType.HAIR_DRIFT, "hair_key"),
                (ContinuityIssueType.WARDROBE_DRIFT, "wardrobe"),
                (ContinuityIssueType.IDENTITY_DRIFT, "age_presentation_key"),
                (ContinuityIssueType.PROP_DRIFT, "important_props"),
                (ContinuityIssueType.STATE_DISCONTINUITY, "physical_state_keys"),
                (ContinuityIssueType.STATE_DISCONTINUITY, "position_state_key"),
            )
            for issue_type, field in fields:
                compare_field(
                    issue_type,
                    ContinuitySubjectType.CHARACTER,
                    character.character_id,
                    f"{prefix}.{field}",
                    getattr(character, field),
                    getattr(values, field),
                )

        expected_location = expected.facts.location
        observed_location = observed.facts.location
        if expected_location is not None:
            location_id = expected_location.location_id
            compare_field(
                ContinuityIssueType.LOCATION_DRIFT,
                ContinuitySubjectType.LOCATION,
                location_id,
                "location.location_id",
                expected_location.location_id,
                observed_location.location_id if observed_location else None,
            )
            for issue_type, field in (
                (ContinuityIssueType.WEATHER_DRIFT, "weather"),
                (ContinuityIssueType.STATE_DISCONTINUITY, "time_of_day"),
                (ContinuityIssueType.STATE_DISCONTINUITY, "spatial_cue_keys"),
                (ContinuityIssueType.PROP_DRIFT, "set_dressing"),
            ):
                compare_field(
                    issue_type,
                    ContinuitySubjectType.LOCATION,
                    location_id,
                    f"location.{field}",
                    getattr(expected_location, field),
                    getattr(observed_location, field) if observed_location else None,
                )
            if expected_location.lighting is not None:
                for field in ("quality_key", "direction_key", "tone_key"):
                    compare_field(
                        ContinuityIssueType.LIGHTING_DRIFT,
                        ContinuitySubjectType.LOCATION,
                        location_id,
                        f"location.lighting.{field}",
                        getattr(expected_location.lighting, field),
                        (
                            getattr(observed_location.lighting, field)
                            if observed_location and observed_location.lighting
                            else None
                        ),
                    )

        expected_narrative = expected.facts.narrative
        observed_narrative = observed.facts.narrative
        if expected_narrative is not None:
            narrative_fields = (
                (ContinuityIssueType.ACTION_DISCONTINUITY, "current_action_key"),
                (ContinuityIssueType.ACTION_DISCONTINUITY, "previous_action_key"),
                (
                    ContinuityIssueType.STATE_DISCONTINUITY,
                    "required_next_state_key",
                ),
                (ContinuityIssueType.PROP_DRIFT, "carried_object_ids"),
                (ContinuityIssueType.STATE_DISCONTINUITY, "injury_state_keys"),
                (ContinuityIssueType.SHOT_INTENT_MISMATCH, "story_beat_id"),
                (ContinuityIssueType.SHOT_INTENT_MISMATCH, "shot_intent_key"),
            )
            for issue_type, field in narrative_fields:
                compare_field(
                    issue_type,
                    ContinuitySubjectType.NARRATIVE,
                    expected.shot_id,
                    f"narrative.{field}",
                    getattr(expected_narrative, field),
                    (
                        getattr(observed_narrative, field)
                        if observed_narrative
                        else None
                    ),
                )

        for field in ("previous_shot_id", "next_shot_id"):
            compare_field(
                ContinuityIssueType.STATE_DISCONTINUITY,
                ContinuitySubjectType.SHOT,
                expected.shot_id,
                f"shot_relationship.{field}",
                getattr(expected.facts.shot_relationship, field),
                getattr(observed.facts.shot_relationship, field),
            )

        for conflict in expected.source_conflicts + observed.source_conflicts:
            provenance = self._provenance(observed) | self._provenance(expected)
            source = provenance.get(conflict.field_path)
            issues.append(
                self._make_issue(
                    expected=expected,
                    observed=observed,
                    issue_type=ContinuityIssueType.STATE_DISCONTINUITY,
                    subject_type=ContinuitySubjectType.SHOT,
                    subject_id=observed.shot_id,
                    path=conflict.field_path,
                    expected_value=conflict.canonical_values[0],
                    observed_value=conflict.canonical_values[1],
                    created_at=created_at,
                    repairability=ContinuityRepairability.HUMAN_DECISION,
                    confidence=source.confidence if source else 0.5,
                    source_override=conflict.sources,
                )
            )

        return tuple(
            sorted(
                issues,
                key=lambda issue: (
                    -SEVERITY_RANK[issue.severity],
                    issue.issue_type.value,
                    issue.affected_subject.subject_id,
                ),
            )
        )

    def recommend_repairs(
        self,
        issues: Iterable[ContinuityIssue],
        *,
        snapshot: ContinuitySnapshot,
        context: RepairPolicyContext | None = None,
    ) -> tuple[RepairRecommendation, ...]:
        """Recommend only; this method has no provider execution capability."""
        context = context or RepairPolicyContext()
        ranked = sorted(
            issues,
            key=lambda issue: (
                -SEVERITY_RANK[issue.severity],
                issue.issue_type.value,
                issue.id,
            ),
        )
        if not ranked:
            return (
                RepairRecommendation(
                    id=self.id_factory(),
                    issue_ids=(),
                    project_id=snapshot.project_id,
                    script_revision_id=snapshot.script_revision_id,
                    shot_plan_revision_id=snapshot.shot_plan_revision_id,
                    shot_id=snapshot.shot_id,
                    execution_id=snapshot.execution_id,
                    artifact_id=snapshot.artifact_id,
                    reference_version_ids=snapshot.reference_version_ids,
                    analysis_source=snapshot.analysis_source,
                    action=RepairAction.ACCEPT,
                    eligibility=RepairEligibility(
                        eligible=True,
                        code="NO_CONTINUITY_ISSUES",
                        conditions=("deterministic continuity evaluation passed",),
                    ),
                    rationale="No continuity drift was detected.",
                    requires_paid_create=False,
                    estimated_scope=EstimatedRepairScope.NONE,
                    requires_human_confirmation=False,
                    created_at=snapshot.created_at,
                ),
            )

        recommendations: list[RepairRecommendation] = []
        for issue in ranked:
            action, eligibility, paid, scope, confirmation = self._repair_action(
                issue, context
            )
            recommendations.append(
                RepairRecommendation(
                    id=self.id_factory(),
                    issue_ids=(issue.id,),
                    project_id=issue.project_id,
                    script_revision_id=issue.script_revision_id,
                    shot_plan_revision_id=issue.shot_plan_revision_id,
                    shot_id=issue.shot_id,
                    execution_id=issue.execution_id,
                    artifact_id=issue.artifact_id,
                    reference_version_ids=issue.reference_version_ids,
                    analysis_source=issue.analysis_source,
                    action=action,
                    eligibility=eligibility,
                    rationale=(
                        f"{issue.issue_type.value} at {issue.severity.value} severity "
                        f"maps to {action.value} under deterministic repair policy v1."
                    ),
                    requires_paid_create=paid,
                    estimated_scope=scope,
                    requires_human_confirmation=confirmation,
                    created_at=issue.created_at,
                )
            )
        return tuple(recommendations)

    @staticmethod
    def _repair_action(
        issue: ContinuityIssue, context: RepairPolicyContext
    ) -> tuple[
        RepairAction,
        RepairEligibility,
        bool,
        EstimatedRepairScope,
        bool,
    ]:
        if (
            issue.repairability is ContinuityRepairability.HUMAN_DECISION
            or issue.confidence < 0.55
        ):
            return (
                RepairAction.HUMAN_DECISION_REQUIRED,
                RepairEligibility(
                    eligible=True,
                    code="AMBIGUOUS_OR_CONFLICTING_EVIDENCE",
                    conditions=("resolve authoritative truth before any create",),
                ),
                False,
                EstimatedRepairScope.SINGLE_SHOT,
                True,
            )
        if (
            context.prior_failed_repair_attempts >= 2
            and not context.current_model_capability_sufficient
            and SEVERITY_RANK[issue.severity] >= SEVERITY_RANK[ContinuitySeverity.HIGH]
        ):
            return (
                RepairAction.MODEL_ESCALATION,
                RepairEligibility(
                    eligible=True,
                    code="REPEATED_FAILURE_AND_MODEL_INSUFFICIENT",
                    conditions=(
                        "at least two prior repairs failed",
                        "current model capability is insufficient",
                    ),
                ),
                True,
                EstimatedRepairScope.SINGLE_SHOT,
                True,
            )
        if issue.repairability is ContinuityRepairability.REFERENCE_REBIND:
            return (
                RepairAction.REFERENCE_REBIND,
                RepairEligibility(
                    eligible=True,
                    code="LOCKED_IDENTITY_REFERENCE_AVAILABLE",
                    conditions=("rebind approved reference before a new create",),
                ),
                True,
                EstimatedRepairScope.SINGLE_SHOT,
                True,
            )
        if issue.repairability is ContinuityRepairability.REPLAN:
            return (
                RepairAction.REPLAN_SHOT,
                RepairEligibility(
                    eligible=True,
                    code="SHOT_INTENT_REQUIRES_PLAN_CHANGE",
                    conditions=("approve a new shot-plan revision",),
                ),
                False,
                EstimatedRepairScope.SHOT_PLAN,
                True,
            )
        if issue.repairability is ContinuityRepairability.LOCAL:
            return (
                RepairAction.LOCAL_REPAIR,
                RepairEligibility(
                    eligible=True,
                    code="BOUNDED_POST_PROCESS_REPAIR",
                    conditions=("repair must not change narrative or identity",),
                ),
                False,
                EstimatedRepairScope.LOCAL_ARTIFACT,
                False,
            )
        if issue.repairability is ContinuityRepairability.PROMPT:
            return (
                RepairAction.PROMPT_REPAIR,
                RepairEligibility(
                    eligible=True,
                    code="PROMPT_CONSTRAINT_CAN_ADDRESS_DRIFT",
                    conditions=("freeze a revised GenerationBrief",),
                ),
                True,
                EstimatedRepairScope.SINGLE_SHOT,
                True,
            )
        return (
            RepairAction.REGENERATE_SHOT,
            RepairEligibility(
                eligible=True,
                code="MATERIAL_VISUAL_OR_STATE_DRIFT",
                conditions=("retain approved truth and create a new shot candidate",),
            ),
            True,
            EstimatedRepairScope.SINGLE_SHOT,
            True,
        )

    def evaluate_shot(
        self,
        request: ContinuityShotRequest,
        *,
        policy_context: RepairPolicyContext | None = None,
        persist: bool = True,
    ) -> ContinuityEvaluationResult:
        if not request.expected:
            raise ContinuityEngineError(
                "evaluate_shot requires authoritative expected continuity truth"
            )
        created_at = self.clock()
        expected = self._snapshot(
            request, ContinuitySnapshotKind.EXPECTED, request.expected, created_at
        )
        observed = self._snapshot(
            request,
            ContinuitySnapshotKind.OBSERVED,
            request.observations,
            created_at,
        )
        issues = self._compare(expected, observed, created_at)
        repairs = self.recommend_repairs(
            issues, snapshot=observed, context=policy_context
        )
        result = ContinuityEvaluationResult(
            expected_snapshot=expected,
            observed_snapshot=observed,
            issues=issues,
            repair_recommendations=repairs,
        )
        if persist:
            self.repository.create_continuity_evaluation(result)
        return result

    @staticmethod
    def _carry_forward_candidate(
        previous: ContinuitySnapshot,
        current: ContinuityShotRequest,
        approval_source_id: str,
    ) -> ContinuityStateCandidate:
        previous_narrative = previous.facts.narrative
        narrative = None
        if previous_narrative is not None:
            narrative = NarrativeContinuityState(
                previous_action_key=previous_narrative.current_action_key,
                carried_object_ids=previous_narrative.carried_object_ids,
                injury_state_keys=previous_narrative.injury_state_keys,
            )
        relationship = current.observations[0].facts.shot_relationship
        facts = ContinuityFacts(
            characters=previous.facts.characters,
            location=previous.facts.location,
            narrative=narrative,
            shot_relationship=relationship,
        )
        return ContinuityStateCandidate(
            source=ContinuitySource(
                kind=ContinuitySourceKind.PREVIOUS_APPROVED_SHOT,
                source_id=approval_source_id,
                revision_id=previous.id,
                approved=True,
                confidence=1.0,
            ),
            facts=facts,
        )

    def evaluate_sequence(
        self,
        requests: Iterable[ContinuityShotRequest],
        *,
        policy_context: RepairPolicyContext | None = None,
        persist: bool = True,
    ) -> tuple[ContinuityEvaluationResult, ...]:
        ordered = sorted(requests, key=lambda item: item.sequence_order)
        if not ordered:
            return ()
        orders = [item.sequence_order for item in ordered]
        shot_ids = [item.shot_id for item in ordered]
        if len(orders) != len(set(orders)) or len(shot_ids) != len(set(shot_ids)):
            raise ContinuityEngineError("sequence order and shot IDs must be unique")
        scope = {
            (item.project_id, item.script_revision_id, item.shot_plan_revision_id)
            for item in ordered
        }
        if len(scope) != 1:
            raise ContinuityEngineError("sequence requests must share revision scope")

        results: list[ContinuityEvaluationResult] = []
        previous_request: ContinuityShotRequest | None = None
        for request in ordered:
            effective = request
            if results and request.carry_forward_previous_approved:
                if previous_request and previous_request.approved_for_continuity:
                    assert previous_request.approval_source_id is not None
                    carry = self._carry_forward_candidate(
                        results[-1].observed_snapshot,
                        request,
                        previous_request.approval_source_id,
                    )
                    effective = request.model_copy(
                        update={"expected": (carry,) + request.expected}
                    )
            results.append(
                self.evaluate_shot(
                    effective,
                    policy_context=policy_context,
                    persist=persist,
                )
            )
            previous_request = request
        return tuple(results)

    @staticmethod
    def project_for_ui(result: ContinuityEvaluationResult) -> ContinuityUIProjection:
        repair_by_issue = {
            issue_id: recommendation
            for recommendation in result.repair_recommendations
            for issue_id in recommendation.issue_ids
        }
        warnings = tuple(
            ContinuityWarningProjection(
                issue_id=issue.id,
                issue_type=issue.issue_type,
                severity=issue.severity,
                shot_id=issue.shot_id,
                subject_label=(
                    f"{issue.affected_subject.subject_type.value}:"
                    f"{issue.affected_subject.subject_id}"
                ),
                recommended_action=repair_by_issue[issue.id].action,
                requires_human_confirmation=repair_by_issue[
                    issue.id
                ].requires_human_confirmation,
            )
            for issue in result.issues
        )
        blocked = any(
            SEVERITY_RANK[item.severity] >= SEVERITY_RANK[ContinuitySeverity.HIGH]
            for item in result.issues
        )
        status = "BLOCKED" if blocked else ("WARNING" if warnings else "PASS")
        return ContinuityUIProjection(
            project_id=result.observed_snapshot.project_id,
            shot_id=result.observed_snapshot.shot_id,
            status=status,
            warnings=warnings,
        )


__all__ = ["ContinuityEngine", "ContinuityEngineError"]
