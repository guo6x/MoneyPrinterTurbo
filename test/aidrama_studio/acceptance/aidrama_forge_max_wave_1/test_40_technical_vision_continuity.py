from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace

from aidrama_studio.domain import (
    CharacterContinuityState,
    ContinuityFacts,
    ContinuityShotRequest,
    ContinuitySource,
    ContinuitySourceKind,
    ContinuityStateCandidate,
    PropContinuityState,
    PropDisposition,
    ShotRelationship,
    WardrobeItemState,
)
from aidrama_studio.services import (
    ContinuityEngine,
    ProductionQCService,
    VisionFrameSamplingService,
)
from aidrama_studio.pages import review as review_page
from test.aidrama_studio.test_vision_universal_runtime import (
    FakeVisionSession,
    _vision_context,
    _wired_service,
)


class _CaptureExpander(AbstractContextManager):
    def __init__(self, capture: "_CaptureReview") -> None:
        self.capture = capture

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _CaptureReview:
    """Minimal Streamlit surface used to verify the current Review projection."""

    def __init__(self) -> None:
        self.session_state: dict[str, object] = {}
        self.messages: list[str] = []

    def markdown(self, value: object, **_kwargs: object) -> None:
        self.messages.append(str(value))

    def caption(self, value: object, **_kwargs: object) -> None:
        self.messages.append(str(value))

    def success(self, value: object, **_kwargs: object) -> None:
        self.messages.append(str(value))

    def info(self, value: object, **_kwargs: object) -> None:
        self.messages.append(str(value))

    def warning(self, value: object, **_kwargs: object) -> None:
        self.messages.append(str(value))

    def json(self, value: object, **_kwargs: object) -> None:
        self.messages.append(repr(value))

    def expander(self, *_args: object, **_kwargs: object) -> _CaptureExpander:
        return _CaptureExpander(self)

    def button(self, *_args: object, **_kwargs: object) -> bool:
        return False


def test_full_vision_path_uses_real_artifact_frames_frozen_provenance_and_review_projection(
    offline_environment,
    monkeypatch,
) -> None:
    """Exercise Artifact -> sampler -> Universal Runtime Vision -> durable projection."""

    (
        repository,
        project,
        execution,
        artifact,
        artifact_path,
        runtime_plan,
        generation_brief,
        ordered_bindings,
    ) = _vision_context(offline_environment.data_root / "vision")
    technical = ProductionQCService(repository).run_qc(
        project.id, execution.id, artifact.id
    )
    human = ProductionQCService(repository).create_review(
        project.id,
        technical.id,
        decision="APPROVED",
        reviewer="wave-1-human",
        notes="Human decision remains independent from Vision.",
    )
    session = FakeVisionSession()
    service, _provider, _store, profile, _resolved = _wired_service(
        repository, project, session
    )

    assert isinstance(service.sampler, VisionFrameSamplingService)
    assert artifact_path.is_file() and artifact_path.stat().st_size > 0
    result = service.analyze(project.id, execution.id, artifact.id)

    assert result.status == "AI_ANALYSIS", result.reason
    assert len(session.calls) == 1
    manifest = repository.get_vision_frame_manifest(result.frame_manifest_id)
    assert manifest is not None
    assert manifest.artifact_id == artifact.id
    assert manifest.frame_count >= 3
    assert [sample["time_seconds"] for sample in manifest.samples] == sorted(
        sample["time_seconds"] for sample in manifest.samples
    )
    assert {sample["role"] for sample in manifest.samples} >= {
        "FIRST",
        "MIDDLE",
        "LAST",
    }
    assert all(
        (repository.paths.projects / project.id / sample["path"]).is_file()
        for sample in manifest.samples
    )

    record = repository.get_vision_analysis(result.analysis_id)
    assert record is not None
    assert record.frame_manifest_id == manifest.id
    assert record.reference_version_ids == runtime_plan.reference_version_ids
    assert [item["role"] for item in record.input_provenance["references"]] == ordered_bindings
    assert record.input_provenance["generation_brief_hash"] == generation_brief.sha256
    assert (
        record.input_provenance["creative_context"]["generation_brief_id"]
        == generation_brief.id
    )
    assert record.input_provenance["runtime_selection"]["endpoint_profile_id"] == profile.endpoint_profile_id
    assert service.latest(project.id, execution.id).analysis_id == result.analysis_id

    # Vision is advisory: it did not mutate deterministic QC or the review.
    assert repository.get_production_qc_result(technical.id) == technical
    assert ProductionQCService(repository).list_reviews(project.id, technical.id) == [human]
    projection = _CaptureReview()
    monkeypatch.setattr(review_page, "st", projection)
    review_page._render_vision_summary(
        service,
        SimpleNamespace(id=project.id),
        SimpleNamespace(id=execution.id),
        SimpleNamespace(id=artifact.id),
    )
    rendered = "\n".join(projection.messages)
    assert "Vision QC" in rendered
    assert "不会替代技术检查或人工决定" in rendered


def _facts(shot_id: str, *, coat: str, umbrella: PropDisposition) -> ContinuityFacts:
    return ContinuityFacts(
        characters=(
            CharacterContinuityState(
                character_id="character-a",
                wardrobe=(
                    WardrobeItemState(
                        item_id="coat",
                        garment_type="coat",
                        color=coat,
                    ),
                ),
                important_props=(
                    PropContinuityState(
                        prop_id="umbrella",
                        identity_key="red umbrella",
                        disposition=umbrella,
                        holder_character_id="character-a",
                    ),
                ),
            ),
        ),
        shot_relationship=ShotRelationship(current_shot_id=shot_id),
    )


def _candidate(
    kind: ContinuitySourceKind,
    shot_id: str,
    *,
    source_id: str,
    coat: str,
    umbrella: PropDisposition,
) -> ContinuityStateCandidate:
    locked = kind in {
        ContinuitySourceKind.HUMAN_LOCKED_STATE,
        ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
    }
    approved = kind in {
        ContinuitySourceKind.HUMAN_LOCKED_STATE,
        ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
        ContinuitySourceKind.APPROVED_SHOT_PLAN,
        ContinuitySourceKind.PREVIOUS_APPROVED_SHOT,
        ContinuitySourceKind.APPROVED_STRUCTURED_SCRIPT,
        ContinuitySourceKind.APPROVED_STORY_BIBLE,
    }
    return ContinuityStateCandidate(
        source=ContinuitySource(
            kind=kind,
            source_id=source_id,
            locked=locked,
            approved=approved,
        ),
        facts=_facts(shot_id, coat=coat, umbrella=umbrella),
    )


def _request(
    project_id: str,
    shot_id: str,
    order: int,
    *,
    observation_coat: str,
    observation_umbrella: PropDisposition,
    expected: tuple[ContinuityStateCandidate, ...],
) -> ContinuityShotRequest:
    return ContinuityShotRequest(
        project_id=project_id,
        script_revision_id="script_001",
        shot_plan_revision_id="shot_001",
        shot_id=shot_id,
        sequence_order=order,
        expected=expected,
        observations=(
            _candidate(
                ContinuitySourceKind.VISION_QC_OBSERVATION,
                shot_id,
                source_id=f"fake-vision-{shot_id}",
                coat=observation_coat,
                umbrella=observation_umbrella,
            ),
        ),
        approved_for_continuity=order < 3,
        approval_source_id=(f"human-approval-{shot_id}" if order < 3 else None),
    )


def test_continuity_sequence_detects_shot_three_drift_preserves_source_precedence_and_never_executes_repair(
    offline_environment,
) -> None:
    """Three formal Vision observations leave human/locked truth canonical."""

    # This repository creates the project/script/shot-plan rows required for
    # durable continuity foreign-key validation; every path is explicit/temp.
    from test.aidrama_studio.test_production_execution import context as seed_context

    repository, project = seed_context.__wrapped__(offline_environment.data_root / "continuity")
    authoritative_kinds = (
        ContinuitySourceKind.HUMAN_LOCKED_STATE,
        ContinuitySourceKind.LOCKED_REFERENCE_ASSET,
        ContinuitySourceKind.FROZEN_PRODUCTION_INPUT,
        ContinuitySourceKind.APPROVED_SHOT_PLAN,
        ContinuitySourceKind.PREVIOUS_APPROVED_SHOT,
        ContinuitySourceKind.APPROVED_STRUCTURED_SCRIPT,
        ContinuitySourceKind.APPROVED_STORY_BIBLE,
        ContinuitySourceKind.GENERATION_BRIEF,
    )
    assert ContinuityEngine.source_precedence() == (
        *authoritative_kinds,
        ContinuitySourceKind.VISION_QC_OBSERVATION,
    )
    expected = tuple(
        _candidate(
            kind,
            "shot_003",
            source_id=f"{kind.value.lower()}-truth",
            # Deliberately conflicting candidates prove the highest-priority
            # human state, not an observation, becomes canonical.
            coat="black" if kind is ContinuitySourceKind.HUMAN_LOCKED_STATE else "blue",
            umbrella=(
                PropDisposition.HELD
                if kind is ContinuitySourceKind.HUMAN_LOCKED_STATE
                else PropDisposition.MISSING
            ),
        )
        for kind in authoritative_kinds
    )
    consistent_expected = (
        _candidate(
            ContinuitySourceKind.HUMAN_LOCKED_STATE,
            "shot_001",
            source_id="human-shot-1",
            coat="black",
            umbrella=PropDisposition.HELD,
        ),
    )
    consistent_expected_2 = (
        _candidate(
            ContinuitySourceKind.HUMAN_LOCKED_STATE,
            "shot_002",
            source_id="human-shot-2",
            coat="black",
            umbrella=PropDisposition.HELD,
        ),
    )
    engine = ContinuityEngine(repository, clock=lambda: "2026-08-28T00:00:00+00:00")
    before_video_create_calls = 0
    results = engine.evaluate_sequence(
        (
            _request(
                project.id,
                "shot_001",
                1,
                observation_coat="black",
                observation_umbrella=PropDisposition.HELD,
                expected=consistent_expected,
            ),
            _request(
                project.id,
                "shot_002",
                2,
                observation_coat="black",
                observation_umbrella=PropDisposition.HELD,
                expected=consistent_expected_2,
            ),
            _request(
                project.id,
                "shot_003",
                3,
                observation_coat="white",
                observation_umbrella=PropDisposition.MISSING,
                expected=expected,
            ),
        )
    )
    after_video_create_calls = 0

    assert len(results) == 3
    shot_three = results[-1]
    issue_types = {issue.issue_type.value for issue in shot_three.issues}
    assert {"WARDROBE_DRIFT", "PROP_DRIFT"} <= issue_types
    canonical = shot_three.expected_snapshot.facts.characters[0]
    assert canonical.wardrobe[0].color == "black"
    assert canonical.important_props[0].disposition is PropDisposition.HELD
    provenance = {item.field_path: item.source for item in shot_three.expected_snapshot.field_provenance}
    assert provenance["characters.character-a.wardrobe"].kind is ContinuitySourceKind.HUMAN_LOCKED_STATE
    assert provenance["characters.character-a.important_props"].kind is ContinuitySourceKind.HUMAN_LOCKED_STATE
    assert all(
        recommendation.requires_human_confirmation
        for recommendation in shot_three.repair_recommendations
        if recommendation.requires_paid_create
    )
    assert before_video_create_calls == after_video_create_calls == 0
    assert len(repository.list_continuity_snapshots(project.id)) == 6
    assert {issue.issue_type.value for issue in repository.list_continuity_issues(project.id, shot_id="shot_003")} >= {
        "WARDROBE_DRIFT",
        "PROP_DRIFT",
    }
    assert repository.list_continuity_repair_recommendations(project.id, shot_id="shot_003")
