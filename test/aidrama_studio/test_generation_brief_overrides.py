from __future__ import annotations

import pytest

from aidrama_studio.services import (
    CreativeLockService,
    GenerationBriefService,
    ProjectArchiveService,
    ProductionQueueError,
    RuntimeFoundationError,
    RuntimePlanService,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)
from test.aidrama_studio.test_production_queue import _authorization, _queue


def test_generation_brief_override_is_immutable_selected_and_runtime_pinned(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = GenerationBriefService(repository)

    base = service.prepare_for_job(project.id, job.id)[0]
    override = service.create_override(
        project.id,
        job.id,
        "shot_001",
        {
            "action": "女主停在门口，先深呼吸再推门",
            "composition": "三分法，人物位于右侧",
            "mood": "克制但紧张",
            "negative_constraints": "不要文字水印，不要改变服装",
            "target_duration_seconds": 2.5,
        },
        base_brief_id=base.id,
    )

    assert override.id != base.id
    assert override.origin == "HUMAN_OVERRIDE"
    assert override.parent_brief_id == base.id
    assert override.manual_override_sha256
    assert set(override.changed_fields) == {
        "action", "composition", "mood", "negative_constraints",
        "target_duration_seconds",
    }
    assert repository.get_generation_brief(base.id) == base
    assert service.current(project.id, job.id, "shot_001") == override

    plan = RuntimePlanService(repository).create(
        project.id,
        production_job_id=job.id,
        brief=override,
        provider_capability="VIDEO_GENERATIVE",
        provider_id="TEST_VIDEO",
        model_id="runtime",
        provider_generation_duration=4,
        target_creative_duration=2.5,
        duration_strategy="TRIM_TO_CREATIVE",
    )
    later = service.create_override(
        project.id,
        job.id,
        "shot_001",
        {"mood": "坚定"},
        base_brief_id=override.id,
    )

    reloaded_plan = repository.get_runtime_plan(plan.id)
    assert later.id != override.id
    assert reloaded_plan.generation_brief_id == override.id
    assert reloaded_plan.generation_brief_hash == override.sha256
    assert reloaded_plan.generation_override_sha256 == override.manual_override_sha256


def test_generation_override_rejects_identity_fields_and_cross_project_scope(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = GenerationBriefService(repository)
    base = service.prepare_for_job(project.id, job.id)[0]

    with pytest.raises(RuntimeFoundationError, match="不可编辑"):
        service.create_override(
            project.id, job.id, "shot_001", {"provider_id": "hidden"}
        )
    with pytest.raises(RuntimeFoundationError, match="provenance"):
        service.create_override(
            "another-project", job.id, "shot_001", {"action": "cross"},
            base_brief_id=base.id,
        )


def test_paid_authorization_fingerprint_invalidates_after_brief_edit(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    stale_authorization = _authorization(queue, project.id, job.id)
    current = queue.prepare_generation_briefs(project.id, job.id)[0]

    queue.save_generation_brief_override(
        project.id,
        job.id,
        current.shot_id,
        {"action": current.action + "，随后回头"},
        base_brief_id=current.id,
    )

    with pytest.raises(ProductionQueueError, match="fingerprint"):
        queue.enqueue_job(
            project.id, job.id, authorization=stale_authorization
        )
    fresh = queue.preview_authorization(project.id, job.id)
    assert fresh.authorization_fingerprint != stale_authorization["authorization_fingerprint"]
    assert fresh.generation_brief_hashes["shot_001"] == queue.prepare_generation_briefs(
        project.id, job.id
    )[0].sha256


def test_saved_generation_override_and_creative_lock_survive_archive_cold_restore(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path / "source")
    job = _ready_job(repository, project)
    service = GenerationBriefService(repository)
    base = service.prepare_for_job(project.id, job.id)[0]
    override = service.create_override(
        project.id, job.id, "shot_001", {"action": "冷恢复后仍保留"},
        base_brief_id=base.id,
    )
    CreativeLockService(repository).lock(
        project.id, "SHOT", "shot_001", "duration_seconds",
        source_revision_id="shot_001", reason="用户手工时长",
    )
    archive = ProjectArchiveService(repository).export_project(
        project.id, tmp_path / "human-editability.aidrama"
    )
    target = ProjectRepository(
        DatabasePaths(
            tmp_path / "target" / "db" / "aidrama.db",
            tmp_path / "target" / "projects",
            tmp_path / "target" / "archived",
        )
    )

    ProjectArchiveService(target).import_project(archive)

    restored = GenerationBriefService(target).current(project.id, job.id, "shot_001")
    assert restored.id == override.id
    assert restored.action == "冷恢复后仍保留"
    locks = CreativeLockService(target).active(
        project.id, entity_kind="SHOT", stable_entity_id="shot_001"
    )
    assert len(locks) == 1
    assert locks[0].field_path == "duration_seconds"
