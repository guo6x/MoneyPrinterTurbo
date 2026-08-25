from __future__ import annotations

import pytest

from aidrama_studio.services import (
    BackgroundProductionRunner,
    CapabilityRegistry,
    ProductionQueueError,
    ProductionQueueService,
    ProductionRuntimeResolver,
    RuntimeVideoProvider,
)
from aidrama_studio.services.adapters import MockProductionAdapter
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


def _queue(repository):
    registry = CapabilityRegistry(
        [RuntimeVideoProvider(MockProductionAdapter(), provider_name="TEST_VIDEO")]
    )
    return ProductionQueueService(repository, registry=registry)


def _authorization(queue, project_id, job_id):
    preview = queue.preview_authorization(project_id, job_id)
    return {
        "approved": True,
        "provider_id": preview.provider_id,
        "model_id": preview.model_id,
        "max_paid_attempts": preview.max_paid_attempts,
        "estimated_provider_requests": preview.estimated_provider_requests,
    }


def test_ui_queue_is_durable_idempotent_and_nonblocking(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    authorization = _authorization(queue, project.id, job.id)
    first = queue.run_job(project.id, job.id, authorization=authorization)
    second = queue.run_job(project.id, job.id)
    assert first.id == second.id
    assert first.state == "QUEUED"
    assert first.execution_id is None
    assert first.provider_id == "TEST_VIDEO"
    assert first.model_id == "runtime"
    assert first.request_summary["approved"] is True
    assert first.request_summary["estimated_provider_requests"] == 1
    assert set(first.request_summary["runtime_plan_ids_by_shot"]) == {"shot_001"}
    assert repository.get_production_job(job.id).status.value == "QUEUED"


def test_cancelled_queued_job_never_reaches_runner(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )
    queue.cancel_job(project.id, job.id)
    assert repository.get_provider_task(task.id).state == "CANCELLED"
    assert repository.get_production_job(job.id).status.value == "CANCELLED"


def test_queue_rejects_missing_or_mismatched_paid_authorization(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    preview = queue.preview_authorization(project.id, job.id)

    with pytest.raises(ProductionQueueError, match="明确批准"):
        queue.enqueue_job(project.id, job.id)
    with pytest.raises(ProductionQueueError, match="model_id"):
        queue.enqueue_job(
            project.id,
            job.id,
            authorization={
                "approved": True,
                "provider_id": preview.provider_id,
                "model_id": "different-model",
                "estimated_provider_requests": preview.estimated_provider_requests,
            },
        )


def test_runtime_plan_freezes_exact_provider_model_references_and_authorization(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )
    plan_id = task.request_summary["runtime_plan_ids_by_shot"]["shot_001"]
    plan = repository.get_runtime_plan(plan_id)
    brief = next(
        item
        for item in repository.list_generation_briefs(project.id, job.id)
        if item.id == plan.generation_brief_id
    )

    assert plan.provider_id == "TEST_VIDEO"
    assert plan.model_id == "runtime"
    assert plan.authorization["approved"] is True
    assert plan.authorization["authorization_id"]
    assert plan.generation_brief_hash == brief.sha256
    assert set(plan.reference_roles.values()) == {"CHARACTER:char_001", "LOCATION:loc_001"}
    assert "api_key" not in repr(plan)
    assert isinstance(ProductionRuntimeResolver(queue.registry).resolve(task, plan), MockProductionAdapter)


def test_background_runner_resolves_each_execution_from_frozen_runtime_plan(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )

    class ImmediateSuccessAdapter(MockProductionAdapter):
        model_id = "runtime"

        def submit(self, snapshot):
            submission = super().submit(snapshot)
            self._statuses[submission.runtime_reference] = "SUCCEEDED"
            return submission

    resolved = []

    def resolve_adapter(provider_task, runtime_plan):
        resolved.append((provider_task.id, runtime_plan.id, runtime_plan.model_id))
        return ImmediateSuccessAdapter()

    completed = BackgroundProductionRunner(
        repository,
        adapter_factory=resolve_adapter,
    ).run_once(project.id)

    assert resolved == [
        (
            task.id,
            task.request_summary["runtime_plan_ids_by_shot"]["shot_001"],
            "runtime",
        )
    ]
    executions = repository.list_production_executions(job.id)
    assert len(executions) == 1
    assert executions[0].runtime_plan_id == resolved[0][1]
    assert executions[0].generation_brief_id
    assert executions[0].input_snapshot.runtime_plan_id == executions[0].runtime_plan_id
    assert (
        executions[0].input_snapshot.generation_brief_id
        == executions[0].generation_brief_id
    )
    assert executions[0].input_snapshot.runtime_plan_hash == repository.get_runtime_plan(
        executions[0].runtime_plan_id
    ).plan_hash
    shot = repository.list_production_shots(job.id)[0]
    attempt = repository.list_production_attempts(shot.id)[0]
    assert attempt.input_snapshot_json["runtime_plan_id"] == executions[0].runtime_plan_id
    # The provider execution itself succeeded. With no real media result the
    # deterministic QC gate truthfully fails the job rather than inventing an
    # artifact, which is outside this queue/resolver test.
    assert executions[0].status.value == "SUCCEEDED"
    assert completed[0].state == "FAILED"
    child = next(item for item in repository.list_provider_tasks(project.id) if item.execution_id == executions[0].id)
    assert child.provider_id == "TEST_VIDEO"
    assert child.model_id == "runtime"
    assert child.request_summary["runtime_plan_id"] == executions[0].runtime_plan_id


def test_startup_reconciliation_requeues_local_job_without_new_paid_intent(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )
    repository.update_provider_task(task.model_copy(update={"state": "RUNNING"}))
    runner = BackgroundProductionRunner(
        repository,
        adapter_factory=lambda _task, _plan: MockProductionAdapter(),
    )

    changed = runner.reconcile(project.id)

    assert [item.id for item in changed] == [task.id]
    assert changed[0].state == "QUEUED"
    job_tasks = [
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id is None
    ]
    assert [item.id for item in job_tasks] == [task.id]
