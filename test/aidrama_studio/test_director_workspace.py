from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from aidrama_studio.domain import (
    ProductionQCStatus,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionReviewDecision,
    VisionAnalysisRecord,
)
from aidrama_studio.pages.director_workspace import candidate_for_preview
from aidrama_studio.services import (
    DirectorWorkspaceProjectionService,
    FinalAssemblyService,
    build_timeline,
)
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import (
    context as _execution_context,
)


@pytest.fixture
def context(tmp_path: Path):
    return _execution_context.__wrapped__(tmp_path)


def test_timeline_uses_authored_order_duration_and_identifies_gaps():
    segments, gaps, total = build_timeline(
        [
            {"id": "shot-2", "order": 3, "duration_seconds": 3},
            {"id": "shot-1", "order": 1, "duration_seconds": 2},
        ],
        7,
    )

    assert [item.shot_id for item in segments] == ["shot-1", "shot-2"]
    assert [(item.start_seconds, item.end_seconds) for item in segments] == [
        (0, 2),
        (2, 5),
    ]
    assert total == 5
    assert gaps[0].kind == "ORDER_GAP"
    assert gaps[0].missing_orders == (2,)
    assert gaps[-1].kind == "TARGET_SHORTFALL"
    assert gaps[-1].duration_seconds == 2


def test_reference_projection_is_exact_locked_and_thumbnail_backed(context):
    repository, project = context
    _shots(repository, project, 1)

    projection = DirectorWorkspaceProjectionService(repository).project(project.id)
    references = projection.shots[0].references

    assert {(item.binding_kind, item.binding_id) for item in references} == {
        ("CHARACTER", "char_001"),
        ("LOCATION", "loc_001"),
    }
    assert all(item.locked for item in references)
    assert all(item.version_id and item.version_number == 1 for item in references)
    assert all(
        item.thumbnail_path and item.thumbnail_path.is_file() for item in references
    )


def test_qc_review_and_finished_state_are_formal_projections(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(
        repository,
        project,
        job,
        shots[0],
        suffix="accepted",
        review=ProductionReviewDecision.APPROVED,
    )

    projection = DirectorWorkspaceProjectionService(repository).project(project.id)
    shot = projection.shots[0]

    assert shot.qc_status == "QC_PASS"
    assert shot.review_status == "APPROVED"
    assert shot.final_source_status == "ACCEPTED"
    assert shot.workspace_state == "ACCEPTED"
    assert projection.state == "FINISHED"


def test_blocked_state_comes_from_failed_technical_qc(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(
        repository,
        project,
        job,
        shots[0],
        suffix="failed-qc",
        qc_status=ProductionQCStatus.QC_FAILED,
    )

    projection = DirectorWorkspaceProjectionService(repository).project(project.id)

    assert projection.shots[0].workspace_state == "BLOCKED"
    assert projection.shots[0].qc_status == "QC_FAILED"
    assert projection.state == "BLOCKED"


def test_running_execution_projects_generating_without_fabricated_progress(context):
    repository, project = context
    job, _shots_for_job = _shots(repository, project, 1)
    repository.create_production_execution(
        ProductionExecution(
            id="execution-running",
            production_job_id=job.id,
            status=ProductionExecutionStatus.RUNNING,
            worker_type="offline-fixture",
            created_at="2026-01-01T00:00:00+00:00",
            input_snapshot=ProductionInputSnapshot(
                project_id=project.id,
                story_revision_id="story_001",
                script_revision_id="script_001",
                shot_plan_revision_id=job.shot_plan_revision_id,
                reference_asset_versions={},
                shot_parameters={"shot_001": {"duration_seconds": 2}},
            ),
        )
    )

    shot = DirectorWorkspaceProjectionService(repository).project(project.id).shots[0]

    assert shot.workspace_state == "GENERATING"
    assert shot.candidates == ()


def test_artifact_preview_priority_follows_explicit_source_decision(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    first = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="first",
        review=ProductionReviewDecision.APPROVED,
        created_at="2026-01-01T00:00:01+00:00",
    )
    second = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="second",
        review=ProductionReviewDecision.APPROVED,
        created_at="2026-01-01T00:00:02+00:00",
    )
    FinalAssemblyService(repository).select_shot_source(
        project.id,
        job.id,
        shots[0].id,
        production_execution_id=first[0].id,
        production_artifact_id=first[1].id,
    )

    shot = DirectorWorkspaceProjectionService(repository).project(project.id).shots[0]
    default = candidate_for_preview(shot)
    comparison = candidate_for_preview(shot, f"{second[0].id}:{second[1].id}")

    assert len(shot.candidates) == 2
    assert default is not None and default.artifact_id == first[1].id
    assert default.is_selected_source and default.source_decision_id
    assert shot.preview_candidate_id == default.candidate_id
    assert comparison is not None and comparison.artifact_id == second[1].id
    assert not comparison.is_selected_source


def test_vision_and_optional_continuity_adapters_remain_advisory(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    execution, artifact, _qc, _review = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="vision",
        review=ProductionReviewDecision.APPROVED,
    )
    repository.create_vision_analysis(
        VisionAnalysisRecord(
            id=uuid4().hex,
            project_id=project.id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            provider_id="offline-vision",
            model_id="fixture-v1",
            status="AI_ANALYSIS",
            metrics={"identity": {"status": "PASS"}},
            created_at="2026-01-01T00:00:03+00:00",
        )
    )
    calls: list[dict[str, object]] = []

    def continuity(**kwargs):
        calls.append(kwargs)
        return {"status": "WARNING", "warnings": ["screen direction"]}

    projection = DirectorWorkspaceProjectionService(
        repository, continuity_adapter=continuity
    ).project(project.id)
    shot = projection.shots[0]

    assert shot.candidates[0].vision_status == "AI_ANALYSIS"
    assert shot.candidates[0].vision_metrics["identity"]["status"] == "PASS"
    assert shot.continuity.available
    assert shot.continuity.status == "WARNING"
    assert shot.continuity.warnings == ("screen direction",)
    assert calls[0]["shot_id"] == "shot_001"


def _app_source(state: str = "ACTIVE", shot_state: str = "READY") -> str:
    final = "ACCEPTED" if shot_state == "ACCEPTED" else "NONE"
    return f'''
from types import SimpleNamespace
from aidrama_studio.pages.director_workspace import render_projection
from aidrama_studio.services.director_workspace import (
    DirectorWorkspaceProjection, TimelineSegment, WorkspaceBeat,
    WorkspaceContinuity, WorkspaceShot,
)

project = SimpleNamespace(id="project-app", title="Workspace")
shots = (
    WorkspaceShot(
        shot_id="shot-1", production_shot_id=None, number=1, order=1,
        duration_seconds=2, timeline_start_seconds=0, timeline_end_seconds=2,
        scene_id="scene-1", scene_label="Scene One", character_ids=("hero",),
        character_labels=("Hero",), beat_ids=("beat-1",),
        production_status="READY", qc_status="NOT_STARTED",
        review_status="NOT_STARTED", final_source_status="{final}",
        workspace_state="{shot_state}", preview_candidate_id=None,
        references=(), candidates=(),
        continuity=WorkspaceContinuity(False, "NOT_AVAILABLE"),
    ),
    WorkspaceShot(
        shot_id="shot-2", production_shot_id=None, number=2, order=2,
        duration_seconds=3, timeline_start_seconds=2, timeline_end_seconds=5,
        scene_id="scene-1", scene_label="Scene One", character_ids=("hero",),
        character_labels=("Hero",), beat_ids=("beat-2",),
        production_status="READY", qc_status="NOT_STARTED",
        review_status="NOT_STARTED", final_source_status="{final}",
        workspace_state="{shot_state}", preview_candidate_id=None,
        references=(), candidates=(),
        continuity=WorkspaceContinuity(False, "NOT_AVAILABLE"),
    ),
)
projection = DirectorWorkspaceProjection(
    project_id=project.id, script_revision_id="script-1",
    shot_plan_revision_id="plan-1", production_job_id=None,
    state="{state}", shots=shots,
    beats=(
        WorkspaceBeat("beat-1", 1, "scene-1", "Scene One", "First beat", "ACTION", ("shot-1", "shot-2"), "SCENE_FALLBACK"),
        WorkspaceBeat("beat-2", 2, "scene-1", "Scene One", "Second beat", "ACTION", ("shot-2",), "EXACT"),
    ),
    timeline=(
        TimelineSegment("shot-1", 1, 0, 2, 2),
        TimelineSegment("shot-2", 2, 2, 5, 3),
    ),
    total_duration_seconds=5, target_duration_seconds=5,
)
render_projection(project, projection)
'''


def test_app_script_shot_and_timeline_selection_stay_synchronized():
    app = AppTest.from_string(_app_source()).run(timeout=30)
    assert not app.exception

    next(item for item in app.button if item.label == "Second beat").click().run()
    assert app.session_state["director-selected-shot-project-app"] == "shot-2"
    assert app.session_state["director-selected-beat-project-app"] == "beat-2"

    next(item for item in app.button if item.label == "01 · 2s").click().run()
    assert app.session_state["director-selected-shot-project-app"] == "shot-1"
    assert app.session_state["director-selected-beat-project-app"] == "beat-1"

    next(item for item in app.button if item.label == "02 · 2s").click().run()
    assert app.session_state["director-selected-shot-project-app"] == "shot-2"
    assert app.session_state["director-selected-beat-project-app"] == "beat-2"


def test_app_empty_state_is_explicit():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages.director_workspace import render_projection
from aidrama_studio.services.director_workspace import DirectorWorkspaceProjection
render_projection(SimpleNamespace(id="empty"), DirectorWorkspaceProjection(
    project_id="empty", script_revision_id=None, shot_plan_revision_id=None,
    production_job_id=None, state="EMPTY"))
"""
    ).run(timeout=30)

    assert not app.exception
    assert any("尚未建立" in item.value for item in app.markdown)


@pytest.mark.parametrize(
    ("state", "shot_state", "widget", "copy"),
    [
        ("BLOCKED", "BLOCKED", "warning", "BLOCKED 镜头"),
        ("FINISHED", "ACCEPTED", "success", "所有镜头"),
    ],
)
def test_app_blocked_and_finished_states_are_distinct(
    state: str, shot_state: str, widget: str, copy: str
):
    app = AppTest.from_string(_app_source(state, shot_state)).run(timeout=30)

    assert not app.exception
    values = getattr(app, widget)
    assert any(copy in item.value for item in values)


def test_workspace_mode_foundation_defaults_to_director():
    app = AppTest.from_string(
        """
from aidrama_studio.pages.director_workspace import render_mode_selector
render_mode_selector("project-mode")
"""
    ).run(timeout=30)

    assert not app.exception
    assert app.segmented_control[0].value == "DIRECTOR"


def test_projection_without_continuity_engine_reports_not_available(context):
    repository, project = context
    _shots(repository, project, 1)

    projection = DirectorWorkspaceProjectionService(repository).project(project.id)

    assert projection.continuity_available is False
    assert projection.shots[0].continuity.status == "NOT_AVAILABLE"


def test_shot_grid_exposes_all_required_formal_status_fields():
    app = AppTest.from_string(_app_source()).run(timeout=30)

    assert not app.exception
    copy = "\n".join(str(item.value) for item in app.markdown)
    for field in (
        "Production · READY",
        "QC · NOT_STARTED",
        "Review · NOT_STARTED",
        "Final · NONE",
    ):
        assert field in copy
    assert "Characters · Hero" in "\n".join(str(item.value) for item in app.caption)


def test_candidate_projection_remains_read_only_by_construction():
    assert not hasattr(DirectorWorkspaceProjectionService, "select_shot_source")
    assert not hasattr(DirectorWorkspaceProjectionService, "update_candidate")
