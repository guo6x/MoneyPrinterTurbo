from __future__ import annotations

import pytest

from aidrama_studio.domain import AspectRatio, Project, ProjectStatus
from aidrama_studio.domain.continuity import (
    CharacterContinuityState,
    ContinuityFacts,
    ContinuityIssueType,
    ContinuityShotRequest,
    ContinuitySource,
    ContinuitySourceKind,
    ContinuityStateCandidate,
    LightingContinuityState,
    LocationContinuityState,
    NarrativeContinuityState,
    PropContinuityState,
    PropDisposition,
    RepairAction,
    RepairPolicyContext,
    ShotRelationship,
    WardrobeItemState,
)
from aidrama_studio.services.continuity import ContinuityEngine, ContinuityEngineError
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


NOW = "2026-08-28T00:00:00.000000+00:00"


@pytest.fixture
def repository(tmp_path) -> ProjectRepository:
    paths = DatabasePaths(
        database=tmp_path / "continuity.db",
        projects=tmp_path / "projects",
        archived_projects=tmp_path / "archives",
    )
    result = ProjectRepository(paths)
    result.create_project(
        Project(
            id="project-continuity",
            title="Continuity synthetic fixture",
            description="offline only",
            status=ProjectStatus.DRAFT,
            aspect_ratio=AspectRatio.LANDSCAPE,
            target_duration_seconds=30,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with result.transaction() as connection:
        connection.execute(
            "INSERT INTO story_bible_revisions("
            "id,project_id,version,status,content_json,generation_input_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "story-rev-1",
                "project-continuity",
                1,
                "APPROVED",
                "{}",
                None,
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO structured_script_revisions("
            "id,project_id,version,status,source_story_revision_id,content_json,"
            "generation_input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "script-rev-1",
                "project-continuity",
                1,
                "APPROVED",
                "story-rev-1",
                "{}",
                None,
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO shot_plan_revisions("
            "id,project_id,version,status,source_script_revision_id,content_json,"
            "generation_input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "shot-plan-rev-1",
                "project-continuity",
                1,
                "APPROVED",
                "script-rev-1",
                "{}",
                None,
                NOW,
                NOW,
            ),
        )
    return result


@pytest.fixture
def engine(repository: ProjectRepository) -> ContinuityEngine:
    ids = (f"continuity-{index:04d}" for index in range(1, 500))
    return ContinuityEngine(
        repository,
        clock=lambda: NOW,
        id_factory=lambda: next(ids),
    )


def _wardrobe(color: str) -> tuple[WardrobeItemState, ...]:
    return (
        WardrobeItemState(
            item_id="coat-main", garment_type="coat", color=color
        ),
    )


def _umbrella() -> tuple[PropContinuityState, ...]:
    return (
        PropContinuityState(
            prop_id="umbrella-red",
            identity_key="red umbrella",
            disposition=PropDisposition.HELD,
            holder_character_id="character-lin",
        ),
    )


def _facts(
    shot_id: str,
    *,
    identity: str | None = "lin",
    wardrobe: tuple[WardrobeItemState, ...] | None = None,
    props: tuple[PropContinuityState, ...] | None = None,
    location_id: str | None = "alley",
    lighting: str | None = "blue-night",
    action: str | None = "walks forward",
    previous_action: str | None = None,
    shot_intent: str | None = "continue pursuit",
    previous_shot_id: str | None = None,
    next_shot_id: str | None = None,
) -> ContinuityFacts:
    location = None
    if location_id is not None:
        location = LocationContinuityState(
            location_id=location_id,
            lighting=(
                LightingContinuityState(tone_key=lighting)
                if lighting is not None
                else None
            ),
        )
    return ContinuityFacts(
        characters=(
            CharacterContinuityState(
                character_id="character-lin",
                identity_key=identity,
                wardrobe=wardrobe,
                important_props=props,
            ),
        ),
        location=location,
        narrative=NarrativeContinuityState(
            current_action_key=action,
            previous_action_key=previous_action,
            carried_object_ids=(
                tuple(item.prop_id for item in props) if props is not None else None
            ),
            shot_intent_key=shot_intent,
        ),
        shot_relationship=ShotRelationship(
            previous_shot_id=previous_shot_id,
            current_shot_id=shot_id,
            next_shot_id=next_shot_id,
        ),
    )


def _candidate(
    facts: ContinuityFacts,
    kind: ContinuitySourceKind,
    source_id: str,
    *,
    confidence: float = 1.0,
) -> ContinuityStateCandidate:
    return ContinuityStateCandidate(
        source=ContinuitySource(
            kind=kind,
            source_id=source_id,
            revision_id="shot-plan-rev-1",
            locked=kind
            in {
                ContinuitySourceKind.HUMAN_LOCKED_STATE,
                ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
            },
            approved=kind is not ContinuitySourceKind.VISION_QC_OBSERVATION,
            confidence=confidence,
        ),
        facts=facts,
    )


def _request(
    shot_id: str,
    order: int,
    expected: tuple[ContinuityStateCandidate, ...],
    observations: tuple[ContinuityStateCandidate, ...],
    *,
    approved_for_continuity: bool = False,
) -> ContinuityShotRequest:
    return ContinuityShotRequest(
        project_id="project-continuity",
        script_revision_id="script-rev-1",
        shot_plan_revision_id="shot-plan-rev-1",
        shot_id=shot_id,
        sequence_order=order,
        expected=expected,
        observations=observations,
        approved_for_continuity=approved_for_continuity,
        approval_source_id=(
            f"human-approval-{shot_id}" if approved_for_continuity else None
        ),
    )


def _types(result) -> set[ContinuityIssueType]:
    return {item.issue_type for item in result.issues}


@pytest.mark.parametrize(
    ("expected", "observed", "issue_type"),
    [
        (
            _facts("shot-1", identity="lin"),
            _facts("shot-1", identity="wrong-person"),
            ContinuityIssueType.IDENTITY_DRIFT,
        ),
        (
            _facts("shot-1", wardrobe=_wardrobe("black")),
            _facts("shot-1", wardrobe=_wardrobe("white")),
            ContinuityIssueType.WARDROBE_DRIFT,
        ),
        (
            _facts("shot-1", props=_umbrella()),
            _facts("shot-1", props=()),
            ContinuityIssueType.PROP_DRIFT,
        ),
        (
            _facts("shot-1", location_id="alley"),
            _facts("shot-1", location_id="station"),
            ContinuityIssueType.LOCATION_DRIFT,
        ),
        (
            _facts("shot-1", action="opens door"),
            _facts("shot-1", action="runs away"),
            ContinuityIssueType.ACTION_DISCONTINUITY,
        ),
    ],
)
def test_detects_core_continuity_taxonomy(
    engine: ContinuityEngine,
    expected: ContinuityFacts,
    observed: ContinuityFacts,
    issue_type: ContinuityIssueType,
) -> None:
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    expected,
                    ContinuitySourceKind.FROZEN_PRODUCTION_INPUT,
                    "production-input-1",
                ),
            ),
            (
                _candidate(
                    observed,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-1",
                    confidence=0.93,
                ),
            ),
        ),
        persist=False,
    )

    assert issue_type in _types(result)
    issue = next(item for item in result.issues if item.issue_type is issue_type)
    assert issue.confidence == 0.93
    assert issue.evidence[0].expected_source_ids == ("production-input-1",)
    assert issue.evidence[0].observed_source_ids == ("fake-vision-1",)


def test_three_shot_sequence_detects_wardrobe_and_prop_drift(
    engine: ContinuityEngine,
) -> None:
    black_with_umbrella = _facts(
        "shot-01",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        next_shot_id="shot-02",
    )
    shot_02 = _facts(
        "shot-02",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        previous_action="walks forward",
        previous_shot_id="shot-01",
        next_shot_id="shot-03",
    )
    shot_03_wrong = _facts(
        "shot-03",
        wardrobe=_wardrobe("white"),
        props=(),
        previous_shot_id="shot-02",
    )
    requests = (
        _request(
            "shot-03",
            3,
            (),
            (
                _candidate(
                    shot_03_wrong,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-03",
                ),
            ),
        ),
        _request(
            "shot-01",
            1,
            (
                _candidate(
                    black_with_umbrella,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-lock-01",
                ),
            ),
            (
                _candidate(
                    black_with_umbrella,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-01",
                ),
            ),
            approved_for_continuity=True,
        ),
        _request(
            "shot-02",
            2,
            (),
            (
                _candidate(
                    shot_02,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-02",
                ),
            ),
            approved_for_continuity=True,
        ),
    )

    results = engine.evaluate_sequence(requests, persist=False)

    assert [item.observed_snapshot.shot_id for item in results] == [
        "shot-01",
        "shot-02",
        "shot-03",
    ]
    assert not results[0].issues
    assert not results[1].issues
    assert {
        ContinuityIssueType.WARDROBE_DRIFT,
        ContinuityIssueType.PROP_DRIFT,
    } <= _types(results[2])


def test_multi_issue_ranking_and_repair_recommendations(
    engine: ContinuityEngine,
) -> None:
    expected = _facts(
        "shot-1", identity="lin", wardrobe=_wardrobe("black"), props=_umbrella()
    )
    observed = _facts(
        "shot-1", identity="other", wardrobe=_wardrobe("white"), props=()
    )
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    expected,
                    ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
                    "reference-v7",
                ),
            ),
            (
                _candidate(
                    observed,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
            ),
        ),
        persist=False,
    )

    assert result.issues[0].issue_type is ContinuityIssueType.IDENTITY_DRIFT
    identity_repair = next(
        item
        for item in result.repair_recommendations
        if item.issue_ids == (result.issues[0].id,)
    )
    assert identity_repair.action is RepairAction.REFERENCE_REBIND
    assert identity_repair.requires_paid_create is True
    assert identity_repair.requires_human_confirmation is True
    assert all(item.eligibility.eligible for item in result.repair_recommendations)


def test_human_observation_override_precedes_fake_vision(
    engine: ContinuityEngine,
) -> None:
    expected = _facts("shot-1", wardrobe=_wardrobe("black"))
    ai_wrong = _facts("shot-1", wardrobe=_wardrobe("white"))
    human_corrected = _facts("shot-1", wardrobe=_wardrobe("black"))
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    expected,
                    ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
                    "reference-v1",
                ),
            ),
            (
                _candidate(
                    ai_wrong,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
                _candidate(
                    human_corrected,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-correction",
                ),
            ),
        ),
        persist=False,
    )

    assert not result.issues
    provenance = {
        item.field_path: item.source.source_id
        for item in result.observed_snapshot.field_provenance
    }
    assert provenance["characters.character-lin.wardrobe"] == "human-correction"


def test_ai_candidate_cannot_override_locked_expected_truth(
    engine: ContinuityEngine,
) -> None:
    locked = _facts("shot-1", identity="lin")
    ai_claim = _facts("shot-1", identity="wrong-person")
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    locked,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "locked-truth",
                ),
                _candidate(
                    ai_claim,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "ai-claim",
                ),
            ),
            (
                _candidate(
                    locked,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
            ),
        ),
        persist=False,
    )

    assert not result.issues
    expected_character = result.expected_snapshot.facts.characters[0]
    assert expected_character.identity_key == "lin"


def test_source_precedence_is_explicit_and_missing_truth_fails_closed(
    engine: ContinuityEngine,
) -> None:
    precedence = engine.source_precedence()
    assert precedence[0] is ContinuitySourceKind.HUMAN_LOCKED_STATE
    assert precedence[-1] is ContinuitySourceKind.VISION_QC_OBSERVATION
    request = _request(
        "shot-1",
        1,
        (),
        (
            _candidate(
                _facts("shot-1"),
                ContinuitySourceKind.VISION_QC_OBSERVATION,
                "fake-vision",
            ),
        ),
    )

    with pytest.raises(ContinuityEngineError, match="authoritative expected"):
        engine.evaluate_shot(request, persist=False)


def test_sequence_does_not_promote_unapproved_ai_observation_to_truth(
    engine: ContinuityEngine,
) -> None:
    first = _facts("shot-01", wardrobe=_wardrobe("black"))
    second = _facts("shot-02", wardrobe=_wardrobe("black"))
    requests = (
        _request(
            "shot-01",
            1,
            (
                _candidate(
                    first,
                    ContinuitySourceKind.APPROVED_SHOT_PLAN,
                    "shot-plan-rev-1",
                ),
            ),
            (
                _candidate(
                    first,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-01",
                ),
            ),
        ),
        _request(
            "shot-02",
            2,
            (),
            (
                _candidate(
                    second,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-02",
                ),
            ),
        ),
    )

    with pytest.raises(ContinuityEngineError, match="authoritative expected"):
        engine.evaluate_sequence(requests, persist=False)


def test_paid_repair_and_model_escalation_always_require_confirmation(
    engine: ContinuityEngine,
) -> None:
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    _facts("shot-1", props=_umbrella()),
                    ContinuitySourceKind.FROZEN_PRODUCTION_INPUT,
                    "frozen-input",
                ),
            ),
            (
                _candidate(
                    _facts("shot-1", props=()),
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
            ),
        ),
        policy_context=RepairPolicyContext(
            prior_failed_repair_attempts=2,
            current_model_capability_sufficient=False,
        ),
        persist=False,
    )

    paid = [item for item in result.repair_recommendations if item.requires_paid_create]
    assert paid
    assert all(item.requires_human_confirmation for item in paid)
    assert any(item.action is RepairAction.MODEL_ESCALATION for item in paid)


def test_persists_snapshot_issue_and_repair_with_revision_provenance(
    engine: ContinuityEngine, repository: ProjectRepository
) -> None:
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    _facts("shot-1", location_id="alley"),
                    ContinuitySourceKind.APPROVED_SHOT_PLAN,
                    "shot-plan-rev-1",
                ),
            ),
            (
                _candidate(
                    _facts("shot-1", location_id="station"),
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
            ),
        )
    )

    snapshots = repository.list_continuity_snapshots(
        "project-continuity", shot_id="shot-1"
    )
    issues = repository.list_continuity_issues(
        "project-continuity", shot_id="shot-1"
    )
    repairs = repository.list_continuity_repair_recommendations(
        "project-continuity", shot_id="shot-1"
    )
    assert snapshots == [result.expected_snapshot, result.observed_snapshot]
    assert issues == list(result.issues)
    assert repairs == list(result.repair_recommendations)
    assert all(item.script_revision_id == "script-rev-1" for item in snapshots)
    assert all(item.shot_plan_revision_id == "shot-plan-rev-1" for item in snapshots)


def test_read_only_ui_projection_exposes_ranked_warning(
    engine: ContinuityEngine,
) -> None:
    result = engine.evaluate_shot(
        _request(
            "shot-1",
            1,
            (
                _candidate(
                    _facts("shot-1", lighting="blue-night"),
                    ContinuitySourceKind.APPROVED_SHOT_PLAN,
                    "shot-plan-rev-1",
                ),
            ),
            (
                _candidate(
                    _facts("shot-1", lighting="warm-day"),
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision",
                ),
            ),
        ),
        persist=False,
    )

    projection = engine.project_for_ui(result)

    assert projection.status == "WARNING"
    assert projection.warnings[0].issue_type is ContinuityIssueType.LIGHTING_DRIFT
    assert projection.warnings[0].recommended_action is RepairAction.LOCAL_REPAIR
