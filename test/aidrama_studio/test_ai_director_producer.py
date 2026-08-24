from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from aidrama_studio.domain import DirectorDecisionStatus, DirectorGoalKind, DirectorGoalStatus, DirectorSessionStatus, ProductionJobStatus, ProductionQCStatus, ProductionReviewDecision, ProductionShotStatus
from aidrama_studio.services import (
    CapabilityKind,
    CapabilityRegistry,
    DeterministicMockVisionProvider,
    ProducerService,
    DirectorServiceError,
    RuntimeVideoProvider,
    UnavailableImageProvider,
    UnavailableVisionProvider,
    DirectorService,
    default_capability_registry,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import context as execution_context, _ready_job
from test.aidrama_studio.test_final_assembly import _source


@pytest.fixture
def context(tmp_path: Path):
    return execution_context.__wrapped__(tmp_path)


def test_capability_registry_exposes_safe_readiness_and_test_vision_only():
    registry = CapabilityRegistry([UnavailableImageProvider(), UnavailableVisionProvider(), DeterministicMockVisionProvider()])
    status = registry.public_status()
    assert status["IMAGE"]["available"] is False
    assert status["VISION"]["available"] is True
    assert "api_key" not in str(status).lower()
    analysis = registry.get(CapabilityKind.VISION).analyze(artifact_path="shot.mp4")
    assert analysis.analysis_kind == "AI_ANALYSIS"


def test_director_recommendation_is_structured_and_cold_resumable(context):
    repository, project = context
    service = DirectorService(repository)
    session = service.start_session(project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=2)
    decision = service.run(project.id, session.id)
    assert decision.recommendation.action in {"APPROVE_STORY_BIBLE", "LOCK_CHARACTER_REFERENCE", "LOCK_LOCATION_REFERENCE"}
    assert decision.recommendation.requires_human_approval is True
    restored = DirectorService(ProjectRepository(repository.paths)).reconstruct(project.id, session.id)
    assert restored["last_decision"].id == decision.id
    assert restored["session"].status is DirectorSessionStatus.BLOCKED
    assert len(restored["decisions"]) == 1


def test_director_projects_are_isolated(context):
    repository, project = context
    other = ProjectRepository(repository.paths)
    with pytest.raises(Exception):
        DirectorService(repository).get_session("missing-project", "missing-session")


def test_producer_retry_budget_is_explicit_and_never_auto(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProducerService(repository)
    progress = service.progress(project.id, job.id)
    assert progress.total_shots == 1
    recommendation = service.recommendations(project.id, job.id)
    assert recommendation[0].action == "START_PRODUCTION"
    assert recommendation[0].requires_human_approval is True
    assert service.policy.automatic_retry_enabled is False
    assert service.policy.max_generation_attempts_per_shot == 3


def test_default_capability_registry_keeps_generative_and_stock_video_separate(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    registry = default_capability_registry()
    status = registry.public_status()
    assert status["VIDEO_GENERATIVE"]["provider"] == "WAN_VIDEO"
    assert status["VIDEO_GENERATIVE"]["available"] is False
    assert status["VIDEO_STOCK"]["provider"] == "MPT_STOCK"
    assert status["VIDEO_STOCK"]["available"] is True


def test_director_approval_rejection_resume_and_append_only_history(context):
    repository, project = context
    service = DirectorService(repository)
    session = service.start_session(project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=2)
    first = service.run(project.id, session.id)
    assert service.get_session(project.id, session.id).status is DirectorSessionStatus.BLOCKED
    approved = service.approve_decision(project.id, first.id)
    assert approved.status is DirectorDecisionStatus.APPROVED
    assert service.get_session(project.id, session.id).status is DirectorSessionStatus.ACTIVE
    cold = DirectorService(ProjectRepository(repository.paths))
    assert cold.repository.get_director_decision(first.id).status is DirectorDecisionStatus.APPROVED
    second = cold.resume(project.id, session.id)
    assert second.id != first.id
    assert cold.repository.get_director_decision(first.id).recommendation == first.recommendation
    assert len(cold.list_decision_events(project.id, first.id)) == 1
    rejected = cold.reject_decision(project.id, second.id)
    assert rejected.status is DirectorDecisionStatus.REJECTED
    assert cold.get_session(project.id, session.id).status is DirectorSessionStatus.ACTIVE
    assert cold.list_decision_events(project.id, second.id)[0].metadata["reason"] == ""


def test_director_cross_project_transition_is_rejected(context):
    repository, project = context
    service = DirectorService(repository)
    first = service.run(project.id, service.start_session(project.id).id)
    other = ProjectRepository(repository.paths).create_project(
        replace(project, id="other-director-project", title="Other")
    )
    with pytest.raises(Exception):
        service.approve_decision(other.id, first.id)


def test_director_block_gate_and_max_steps_require_explicit_resume_segment(context):
    repository, project = context
    service = DirectorService(repository)
    session = service.start_session(project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=1)
    first = service.run(project.id, session.id)
    with pytest.raises(DirectorServiceError, match="等待人工处理"):
        service.resume(project.id, session.id)
    service.approve_decision(project.id, first.id)
    with pytest.raises(DirectorServiceError, match="max_steps"):
        service.run(project.id, session.id)
    second = service.resume(project.id, session.id)
    assert second.id != first.id


@pytest.mark.parametrize(
    ("goal", "satisfied"),
    [
        (DirectorGoalKind.COMPLETE_STORY, True),
        (DirectorGoalKind.COMPLETE_SCRIPT, True),
        (DirectorGoalKind.COMPLETE_SHOT_PLAN, True),
        (DirectorGoalKind.COMPLETE_REFERENCES, False),
        (DirectorGoalKind.MAKE_PRODUCTION_READY, False),
    ],
)
def test_director_goal_kinds_have_distinct_completion_semantics(context, goal, satisfied):
    repository, project = context
    service = DirectorService(repository)
    session = service.start_session(project.id, goal, max_steps=1)
    decision = service.run(project.id, session.id)
    if satisfied:
        assert decision.recommendation.action == "GOAL_COMPLETE"
        assert service.get_session(project.id, session.id).status is DirectorSessionStatus.COMPLETED
        assert service.repository.list_director_goals(session.id)[-1].status is DirectorGoalStatus.COMPLETED
    else:
        assert decision.recommendation.action in {"LOCK_CHARACTER_REFERENCE", "LOCK_LOCATION_REFERENCE"}
        assert service.get_session(project.id, session.id).status is DirectorSessionStatus.BLOCKED


def test_producer_uses_current_qualified_retry_not_historical_qc_failure(context):
    repository, project = context
    job = _ready_job(repository, project)
    from aidrama_studio.services import ProductionService
    ProductionService(repository).create_production_shots(project.id, job.id)
    shot = repository.list_production_shots(job.id)[0]
    _source(repository, project, job, shot, suffix="old", qc_status=ProductionQCStatus.QC_FAILED, created_at="2025-01-01T00:00:01+00:00")
    newer = _source(repository, project, job, shot, suffix="new", qc_status=ProductionQCStatus.QC_PASS, review=ProductionReviewDecision.APPROVED, created_at="2025-01-01T00:00:02+00:00")
    repository.update_production_shot_status(shot.id, ProductionShotStatus.SUCCEEDED)
    repository.update_production_job_status(job.id, ProductionJobStatus.SUCCEEDED, updated_at="2025-01-01T00:00:03+00:00")
    recommendation = ProducerService(repository).recommendations(project.id, job.id)[0]
    assert recommendation.action == "CREATE_NEW_FINAL_ASSEMBLY"
    assert newer[0].id != ""


def test_producer_qc_retry_recommendation_budget_is_durable(context):
    repository, project = context
    job = _ready_job(repository, project)
    from aidrama_studio.services import ProductionService
    ProductionService(repository).create_production_shots(project.id, job.id)
    shot = repository.list_production_shots(job.id)[0]
    _source(repository, project, job, shot, suffix="qc-fail", qc_status=ProductionQCStatus.QC_FAILED)
    repository.update_production_shot_status(shot.id, ProductionShotStatus.SUCCEEDED)
    repository.update_production_job_status(job.id, ProductionJobStatus.SUCCEEDED, updated_at="2026-01-01T00:00:00+00:00")
    service = ProducerService(repository)
    assert service.recommendations(project.id, job.id)[0].action == "RETRY_QC"
    assert service.recommendations(project.id, job.id)[0].action == "RETRY_QC"
    exhausted = service.recommendations(project.id, job.id)[0]
    assert exhausted.action == "STOP_AND_REVIEW"
    assert len(repository.list_producer_recommendation_events(project.id, production_job_id=job.id, action="RETRY_QC")) == 2


@pytest.mark.parametrize(
    ("goal", "expected_action", "expected_status"),
    [
        (DirectorGoalKind.COMPLETE_PRODUCTION, "RESUME_PRODUCTION", DirectorSessionStatus.BLOCKED),
        (DirectorGoalKind.RESOLVE_QC_BLOCKER, "GOAL_COMPLETE", DirectorSessionStatus.COMPLETED),
        (DirectorGoalKind.MAKE_FINAL_ASSEMBLY_READY, "RESUME_PRODUCTION", DirectorSessionStatus.BLOCKED),
        (DirectorGoalKind.COMPLETE_POST_PRODUCTION, "MAKE_FINAL_ASSEMBLY_READY", DirectorSessionStatus.BLOCKED),
    ],
)
def test_director_production_qc_final_and_post_goals_have_distinct_semantics(context, goal, expected_action, expected_status):
    repository, project = context
    job = _ready_job(repository, project)
    from aidrama_studio.services import ProductionService

    ProductionService(repository).create_production_shots(project.id, job.id)
    service = DirectorService(repository)
    session = service.start_session(project.id, goal, max_steps=1)
    decision = service.run(project.id, session.id)
    assert decision.recommendation.action == expected_action
    assert service.get_session(project.id, session.id).status is expected_status


def test_pending_current_shots_do_not_recommend_final_assembly(context):
    repository, project = context
    job = _ready_job(repository, project)
    from aidrama_studio.services import ProductionService

    ProductionService(repository).create_production_shots(project.id, job.id)
    producer = ProducerService(repository)
    assert producer.recommendations(project.id, job.id)[0].action == "START_PRODUCTION"
    director = DirectorService(repository)
    session = director.start_session(project.id, DirectorGoalKind.MAKE_FINAL_ASSEMBLY_READY, max_steps=1)
    decision = director.run(project.id, session.id)
    assert decision.recommendation.action == "RESUME_PRODUCTION"
