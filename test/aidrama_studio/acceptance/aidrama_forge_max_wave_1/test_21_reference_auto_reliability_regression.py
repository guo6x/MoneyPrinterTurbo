from __future__ import annotations

import pytest

from aidrama_studio.domain import AutoAction, AutoRunStatus, AutoStage, ReferenceBindingType
from aidrama_studio.domain.reference_agent import ReferenceActionKind, ReferenceCoverageStatus
from aidrama_studio.services import (
    AutoOrchestratorService,
    ImageRuntimeService,
    ProductionExecutionService,
    ProductionService,
    ReferenceAgentError,
    ReferenceAgentService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
)
from aidrama_studio.services.production_worker import ProductionWorker
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_production_reliability_cost_guard import (
    _FakePaidAdapter,
    _FakeProviderBackend,
    _authorize,
)
from test.aidrama_studio.test_reference_agent import FakeImageProvider


def _lock_reference(
    repository: ProjectRepository,
    project_id: str,
    binding_type: ReferenceBindingType,
    subject_id: str,
    *,
    color: str,
    source_story_revision_id: str = "story_001",
) -> None:
    references = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(references)
    asset = references.ensure_workspace_asset(project_id, binding_type, subject_id)
    version = storage.import_image(
        project_id,
        asset.id,
        png_bytes(color=color),
        filename=f"{subject_id}.png",
        mime_type="image/png",
        metadata={"source_story_revision_id": source_story_revision_id},
    )
    references.bind_version(project_id, version.id, binding_type, subject_id)
    references.activate_version(project_id, asset.id, version.id)


def _agent(repository: ProjectRepository) -> tuple[ReferenceAgentService, FakeImageProvider]:
    provider = FakeImageProvider()
    return (
        ReferenceAgentService(
            repository,
            image_runtime=ImageRuntimeService(repository, provider=provider),
        ),
        provider,
    )


def _complete_canonical_references(
    repository: ProjectRepository,
    project_id: str,
) -> tuple[ReferenceAgentService, FakeImageProvider]:
    source_story_revision_id = repository.list_story_revisions(project_id)[0]["id"]
    _lock_reference(
        repository,
        project_id,
        ReferenceBindingType.CHARACTER,
        "character_lin",
        color="black",
        source_story_revision_id=source_story_revision_id,
    )
    _lock_reference(
        repository,
        project_id,
        ReferenceBindingType.LOCATION,
        "location_bookshop_exterior",
        color="blue",
        source_story_revision_id=source_story_revision_id,
    )
    agent, provider = _agent(repository)
    readiness = agent.evaluate(project_id)
    action_ids = [item.id for item in readiness.next_actions]
    authorization = agent.generation_authorization(
        project_id,
        action_ids,
        max_creates=2,
        approved_by="wave1-human",
        approved=True,
    )
    generated = agent.generate_candidates(
        project_id,
        action_ids,
        authorization=authorization,
    )
    for item in generated:
        version = agent.approve_candidate_and_bind(
            project_id,
            item.candidate_id,
            human_confirmed=True,
            actor="wave1-human",
        )
        agent.lock_bound_reference(
            project_id,
            version.id,
            human_confirmed=True,
        )
    return agent, provider


def test_reference_agent_human_lock_advances_shared_auto_readiness(
    canonical_approved_project: dict[str, object],
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    _lock_reference(
        repository,
        project.id,
        ReferenceBindingType.CHARACTER,
        "character_lin",
        color="black",
    )
    _lock_reference(
        repository,
        project.id,
        ReferenceBindingType.LOCATION,
        "location_bookshop_exterior",
        color="blue",
    )
    agent, provider = _agent(repository)

    initial = agent.evaluate(project.id)
    assert len(initial.required) == 4
    assert {item.subject_id for item in initial.missing} == {
        "character_su",
        "location_bookshop_interior",
    }
    assert len({(item.subject_type, item.subject_id) for item in initial.required}) == 4
    assert all(
        item.kind is ReferenceActionKind.WAITING_PAID_AUTHORIZATION
        for item in initial.next_actions
    )
    with pytest.raises(ReferenceAgentError, match="WAITING_PAID_AUTHORIZATION"):
        agent.generate_candidates(
            project.id,
            [item.id for item in initial.next_actions],
            authorization=None,
        )
    assert provider.calls == []

    authorization = agent.generation_authorization(
        project.id,
        [item.id for item in initial.next_actions],
        max_creates=2,
        approved_by="wave1-human",
        approved=True,
    )
    generated = agent.generate_candidates(
        project.id,
        [item.id for item in initial.next_actions],
        authorization=authorization,
    )
    assert len(generated) == len(provider.calls) == 2
    waiting = agent.evaluate(project.id)
    assert {item.coverage_status for item in waiting.required} == {
        ReferenceCoverageStatus.LOCKED,
        ReferenceCoverageStatus.WAITING_HUMAN,
    }
    for item in generated:
        version = agent.approve_candidate_and_bind(
            project.id,
            item.candidate_id,
            human_confirmed=True,
            actor="wave1-human",
        )
        agent.lock_bound_reference(
            project.id,
            version.id,
            human_confirmed=True,
        )

    ready = agent.evaluate(project.id)
    assert ready.production_reference_ready is True
    assert ready.production_readiness["ready"] is True
    decision = AutoOrchestratorService(repository).next_action(project.id)
    assert decision.current_stage is AutoStage.PRODUCTION
    assert decision.next_action is AutoAction.PREPARE_PRODUCTION
    assert decision.completed_stages[-1] is AutoStage.REFERENCES


def test_auto_blocks_uncertain_create_then_resumes_original_task_only(
    canonical_approved_project: dict[str, object],
    database_paths,
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    _complete_canonical_references(repository, project.id)
    production = ProductionService(repository)
    job = production.create_production_job(project.id, "shot_plan_001")
    production.create_production_shots(project.id, job.id)
    execution_service = ProductionExecutionService(repository, production_service=production)
    execution = execution_service.enqueue_job(project.id, job.id, worker_type="wave1-fake")
    _authorize(repository, project.id, job.id, 1)
    backend = _FakeProviderBackend(submit_error=TimeoutError("unknown submit outcome"))
    adapter = _FakePaidAdapter(backend)

    still_queued = ProductionWorker(execution_service, adapter).run(
        project.id,
        execution.id,
    )
    assert still_queued.status.value == "QUEUED"
    task = next(
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id
    )
    assert task.state == "UNCERTAIN_CREATE"
    assert backend.submit_calls == 1

    cold_repository = ProjectRepository(database_paths)
    blocked = AutoOrchestratorService(cold_repository).next_action(project.id)
    assert blocked.status is AutoRunStatus.BLOCKED
    assert blocked.current_stage is AutoStage.PRODUCTION
    assert "UNCERTAIN_CREATE" in f"{blocked.why} {blocked.blocking_reason}"
    assert blocked.metadata["reconciliation_required"] is True
    assert backend.submit_calls == 1

    # An operator reconciles the original provider outcome by attaching the
    # recovered remote identity. The worker may now poll, but never submit.
    cold_repository.update_provider_submission_outcome(
        task.id,
        state="PROVIDER_ACCEPTED",
        provider_task_id="recovered-original-task",
        updated_at="2026-08-28T01:00:00+00:00",
        metadata={"reconciled": True},
    )
    cold_execution = ProductionExecutionService(cold_repository)
    cold_execution.start_execution(
        project.id,
        execution.id,
        {
            "adapter": adapter.name,
            "runtime_reference": "recovered-original-task",
            "provider_metadata": {"reconciled": True},
        },
    )
    reconciled = ProductionWorker(
        cold_execution,
        adapter,
        max_polls=1,
    ).resume(project.id, execution.id)
    assert reconciled.status.value == "SUCCEEDED"
    assert backend.submit_calls == 1
    assert len(cold_repository.list_provider_tasks(project.id)) == 1
    assert AutoOrchestratorService(cold_repository).next_action(project.id).status is not AutoRunStatus.BLOCKED
