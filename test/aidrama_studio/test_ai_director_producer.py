from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import DirectorGoalKind, DirectorSessionStatus
from aidrama_studio.services import (
    CapabilityKind,
    CapabilityRegistry,
    DeterministicMockVisionProvider,
    ProducerService,
    RuntimeVideoProvider,
    UnavailableImageProvider,
    UnavailableVisionProvider,
    DirectorService,
    default_capability_registry,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import context as execution_context, _ready_job


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
