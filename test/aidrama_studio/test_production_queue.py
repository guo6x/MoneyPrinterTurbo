from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aidrama_studio.domain import ProviderDeploymentRegion
from aidrama_studio.services import (
    BackgroundProductionRunner,
    CapabilityRegistry,
    ProductionQueueError,
    ProductionQueueService,
    ProductionExecutionService,
    ProductionService,
    ProductionWorker,
    ProductionRuntimeResolutionError,
    ProductionRuntimeResolver,
    ProductionRuntimeAdapter,
    RuntimeSubmission,
    RuntimeVideoProvider,
)
from aidrama_studio.services.ai_capabilities import CapabilityKind
from aidrama_studio.services.adapters import MockProductionAdapter
from aidrama_studio.services.streaming_artifact import StreamingArtifactSource
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
        "deployment_region": preview.deployment_region,
        "endpoint_profile_id": preview.endpoint_profile_id,
        "endpoint_class": preview.endpoint_class,
        "reference_count": preview.reference_count,
        "authorization_fingerprint": preview.authorization_fingerprint,
    }


def test_seedance_queue_accepts_only_the_official_4_to_30_duration_profile():
    profile = {
        "requires_explicit_selection": True,
        "minimum_duration_seconds": 4,
        "maximum_duration_seconds": 30,
        "supported_durations": list(range(4, 31)),
    }

    assert ProductionQueueService._duration_limits(
        profile, provider_id="SEEDANCE"
    ) == (4.0, 30.0)
    assert ProductionQueueService._allowed_durations(
        profile, provider_id="SEEDANCE"
    ) == tuple(float(value) for value in range(4, 31))


@pytest.mark.parametrize(
    "tampered",
    [
        {"requires_explicit_selection": False},
        {"minimum_duration_seconds": 2},
        {"maximum_duration_seconds": 15},
        {"supported_durations": list(range(2, 16))},
    ],
)
def test_seedance_queue_rejects_missing_or_tampered_duration_metadata(tampered):
    profile = {
        "requires_explicit_selection": True,
        "minimum_duration_seconds": 4,
        "maximum_duration_seconds": 30,
        "supported_durations": list(range(4, 31)),
    }
    profile.update(tampered)

    with pytest.raises(ProductionQueueError, match="Seedance"):
        ProductionQueueService._duration_limits(profile, provider_id="SEEDANCE")
    with pytest.raises(ProductionQueueError, match="Seedance"):
        ProductionQueueService._allowed_durations(profile, provider_id="SEEDANCE")


def test_ui_queue_is_durable_idempotent_and_nonblocking(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    authorization = _authorization(queue, project.id, job.id)
    first = queue.run_job(project.id, job.id, authorization=authorization)
    double_click = queue.run_job(
        project.id, job.id, authorization=authorization
    )
    second = queue.run_job(project.id, job.id)
    assert first.id == double_click.id
    assert first.id == second.id
    assert first.state == "QUEUED"
    assert first.execution_id is None
    assert first.provider_id == "TEST_VIDEO"
    assert first.model_id == "runtime"
    assert first.request_summary["approved"] is True
    assert first.request_summary["estimated_provider_requests"] == 1
    assert set(first.request_summary["runtime_plan_ids_by_shot"]) == {"shot_001"}
    budget = queue.budget_projection(project.id, job.id)
    assert budget.planned_creates == 1
    assert budget.authorized_max == 1
    assert budget.remaining_creates == 1
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
    assert plan.endpoint_profile_id == task.request_summary["endpoint_profile_id"]
    assert plan.deployment_region == task.request_summary["deployment_region"]
    assert plan.endpoint_class == task.request_summary["endpoint_class"]
    assert plan.selection_source == "LEGACY_DEFAULT"
    assert plan.transmitted_content_types == ("TEXT", "REFERENCE_IMAGE")
    assert plan.estimated_request_count == 1
    assert plan.authorization["approved"] is True
    assert plan.authorization["authorization_id"]
    assert plan.generation_brief_hash == brief.sha256
    assert set(plan.reference_roles.values()) == {"CHARACTER:char_001", "LOCATION:loc_001"}
    assert "api_key" not in repr(plan)
    assert isinstance(ProductionRuntimeResolver(queue.registry).resolve(task, plan), MockProductionAdapter)
    with pytest.raises(ProductionRuntimeResolutionError, match="endpoint"):
        ProductionRuntimeResolver(queue.registry).resolve(
            task,
            plan.model_copy(update={"endpoint_profile_id": "different-endpoint"}),
        )


def test_provider_switch_changes_only_new_runtime_plans_and_stale_disclosure_is_rejected(tmp_path):
    from aidrama_studio.domain import ProviderPreset
    from aidrama_studio.services.provider_profiles import ProviderProfileService
    from test.aidrama_studio.test_provider_selection import _Provider

    repository, project = _execution_context.__wrapped__(tmp_path)
    first_job = _ready_job(repository, project)
    registry = CapabilityRegistry(
        [
            _Provider(
                CapabilityKind.VIDEO_GENERATIVE,
                "CN_VIDEO",
                ProviderDeploymentRegion.MAINLAND_CHINA,
                "CN_VIDEO_ENDPOINT",
            ),
            _Provider(
                CapabilityKind.VIDEO_GENERATIVE,
                "INTL_VIDEO",
                ProviderDeploymentRegion.INTERNATIONAL,
                "INTL_VIDEO_ENDPOINT",
            ),
        ]
    )
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(project_id=None, preset=ProviderPreset.MAINLAND)
    queue = ProductionQueueService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    first_preview = queue.preview_authorization(project.id, first_job.id)
    first_task = queue.enqueue_job(
        project.id,
        first_job.id,
        authorization={
            "approved": True,
            "provider_id": first_preview.provider_id,
            "model_id": first_preview.model_id,
            "deployment_region": first_preview.deployment_region,
            "endpoint_profile_id": first_preview.endpoint_profile_id,
            "endpoint_class": first_preview.endpoint_class,
            "reference_count": first_preview.reference_count,
            "max_paid_attempts": first_preview.max_paid_attempts,
            "estimated_provider_requests": first_preview.estimated_provider_requests,
            "authorization_fingerprint": first_preview.authorization_fingerprint,
        },
    )
    first_plan = repository.get_runtime_plan(
        first_task.request_summary["runtime_plan_ids_by_shot"]["shot_001"]
    )
    frozen_dump = first_plan.model_dump(mode="json")

    profiles.save_settings(project_id=None, preset=ProviderPreset.INTERNATIONAL)
    changed_preview = queue.preview_authorization(project.id, first_job.id)
    assert changed_preview.provider_id == "INTL_VIDEO"
    assert changed_preview.authorization_fingerprint != first_preview.authorization_fingerprint
    assert repository.get_runtime_plan(first_plan.id).model_dump(mode="json") == frozen_dump

    queue.cancel_job(project.id, first_job.id)
    with pytest.raises(ProductionQueueError, match="provider_id|fingerprint"):
        queue.enqueue_job(
            project.id,
            first_job.id,
            authorization={
                "approved": True,
                "provider_id": first_preview.provider_id,
                "model_id": first_preview.model_id,
                "estimated_provider_requests": first_preview.estimated_provider_requests,
                "authorization_fingerprint": first_preview.authorization_fingerprint,
            },
        )

    second_job = ProductionService(repository).create_production_job(project.id, "shot_001")
    second_preview = queue.preview_authorization(project.id, second_job.id)
    second_task = queue.enqueue_job(
        project.id,
        second_job.id,
        authorization={
            "approved": True,
            "provider_id": second_preview.provider_id,
            "model_id": second_preview.model_id,
            "deployment_region": second_preview.deployment_region,
            "endpoint_profile_id": second_preview.endpoint_profile_id,
            "endpoint_class": second_preview.endpoint_class,
            "reference_count": second_preview.reference_count,
            "estimated_provider_requests": second_preview.estimated_provider_requests,
            "authorization_fingerprint": second_preview.authorization_fingerprint,
        },
    )
    second_plan = repository.get_runtime_plan(
        second_task.request_summary["runtime_plan_ids_by_shot"]["shot_001"]
    )
    assert first_plan.provider_id == "CN_VIDEO"
    assert second_plan.provider_id == "INTL_VIDEO"
    assert second_plan.endpoint_profile_id == second_preview.endpoint_profile_id
    assert second_plan.plan_hash != first_plan.plan_hash


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


def test_background_runner_defers_artifact_redownload_and_never_resubmits(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )

    class FlakyResultAdapter(ProductionRuntimeAdapter):
        name = "flaky-result"
        model_id = "runtime"

        def __init__(self):
            self.submits = 0
            self.status_checks = 0
            self.downloads = 0

        def validate(self, snapshot):
            return True

        def submit(self, snapshot):
            self.submits += 1
            return RuntimeSubmission("paid-task-1")

        def get_status(self, runtime_reference):
            self.status_checks += 1
            return "SUCCEEDED"

        def get_result(self, runtime_reference):
            self.downloads += 1

            def writer(sink):
                if self.downloads == 1:
                    raise OSError("temporary CDN interruption")
                sink.write(b"not-a-real-video-but-a-physical-test-artifact")

            return {
                "stream_source": StreamingArtifactSource(writer, 1024),
                "filename": "shot.mp4",
                "artifact_type": "video",
            }

        def cancel(self, runtime_reference):
            return False

    adapter = FlakyResultAdapter()
    runner = BackgroundProductionRunner(
        repository,
        adapter_factory=lambda _task, _plan: adapter,
    )

    first = runner.run_once(project.id)

    assert first[0].state == "QUEUED"
    assert first[0].metadata["not_before"]
    assert adapter.submits == 1
    assert runner.run_once(project.id) == []

    queued = repository.get_provider_task(task.id)
    repository.update_provider_task(
        queued.model_copy(
            update={
                "metadata": dict(queued.metadata)
                | {
                    "not_before": (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat()
                }
            }
        )
    )
    second = runner.run_once(project.id)

    assert second[0].state == "FAILED"  # deterministic QC rejects fake media
    assert adapter.submits == 1
    assert adapter.status_checks == 1
    assert adapter.downloads == 2
    executions = repository.list_production_executions(job.id)
    assert len(executions) == 1
    assert executions[0].status.value == "SUCCEEDED"


def test_startup_direct_execution_resumes_original_provider_identity(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    class ColdResumeAdapter(ProductionRuntimeAdapter):
        name = "cold-resume"
        model_id = "runtime"

        def __init__(self):
            self.submits = 0
            self.status_checks = 0

        def validate(self, snapshot):
            return True

        def submit(self, snapshot):
            self.submits += 1
            return RuntimeSubmission("original-paid-task")

        def get_status(self, runtime_reference):
            self.status_checks += 1
            assert runtime_reference == "original-paid-task"
            return "SUCCEEDED"

        def get_result(self, runtime_reference):
            assert runtime_reference == "original-paid-task"
            return {"content": b"resumed-output", "filename": "shot.mp4"}

        def cancel(self, runtime_reference):
            return False

    adapter = ColdResumeAdapter()
    interrupted = ProductionWorker(
        service, adapter, should_stop=lambda: True
    ).run(project.id, execution.id)
    assert interrupted.status.value == "RUNNING"
    assert adapter.submits == 1

    runner = BackgroundProductionRunner(
        repository, adapter_factory=lambda _task, _plan: adapter
    )
    wrapper = runner.enqueue(project.id, execution.id)
    repository.update_provider_task(wrapper.model_copy(update={"state": "RUNNING"}))

    changed = runner.reconcile(project.id)
    assert [item.state for item in changed if item.id == wrapper.id] == ["QUEUED"]
    completed = runner.run_once(project.id)

    assert completed[0].state == "SUCCEEDED"
    assert adapter.submits == 1
    assert adapter.status_checks == 1


def test_submission_uncertain_without_identity_stays_manual_and_calls_no_adapter(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    class UncertainAdapter(ProductionRuntimeAdapter):
        name = "uncertain"
        model_id = "runtime"
        submission_uncertain_on_error = True

        def __init__(self):
            self.submits = 0

        def validate(self, snapshot):
            return True

        def submit(self, snapshot):
            self.submits += 1
            raise OSError("response lost after POST")

        def get_status(self, runtime_reference):
            raise AssertionError("unknown identity must not be queried")

        def cancel(self, runtime_reference):
            return False

    adapter = UncertainAdapter()
    current = ProductionWorker(service, adapter).run(project.id, execution.id)
    assert current.status.value == "QUEUED"
    assert adapter.submits == 1

    runner = BackgroundProductionRunner(
        repository,
        adapter_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("manual reconciliation must not resolve an adapter")
        ),
    )
    runner.enqueue(project.id, execution.id)
    completed = runner.run_once(project.id)

    assert completed[0].state == "RECONCILIATION_REQUIRED"
    assert adapter.submits == 1


def test_crash_after_submitting_state_never_reposts_paid_request(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    class CrashWindowAdapter(ProductionRuntimeAdapter):
        name = "crash-window"
        model_id = "runtime"

        def __init__(self):
            self.submits = 0
            self.crash = True

        def validate(self, snapshot):
            return True

        def submit(self, snapshot):
            self.submits += 1
            if self.crash:
                raise SystemExit("simulated process loss after provider POST")
            raise AssertionError("restart must not submit again")

        def get_status(self, runtime_reference):
            raise AssertionError("provider identity is unknown")

        def cancel(self, runtime_reference):
            return False

    adapter = CrashWindowAdapter()
    with pytest.raises(SystemExit):
        ProductionWorker(service, adapter).run(project.id, execution.id)
    task = next(
        item for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id
        and item.provider_id != "RUNTIME_BOUNDARY"
    )
    assert task.state == "SUBMITTING"

    adapter.crash = False
    current = ProductionWorker(
        ProductionExecutionService(repository), adapter
    ).run(project.id, execution.id)

    assert current.status.value == "QUEUED"
    assert adapter.submits == 1
    assert repository.get_provider_task(task.id).state == "UNCERTAIN_CREATE"

    runner = BackgroundProductionRunner(
        repository,
        adapter_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("manual reconciliation must not resolve an adapter")
        ),
    )
    runner.enqueue(project.id, execution.id)
    completed = runner.run_once(project.id)

    assert completed[0].state == "RECONCILIATION_REQUIRED"
    assert adapter.submits == 1


def test_direct_execution_artifact_pending_retries_download_without_status_or_submit(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    class ArtifactPendingAdapter(ProductionRuntimeAdapter):
        name = "artifact-pending"
        model_id = "runtime"

        def __init__(self):
            self.submits = 0
            self.status_checks = 0
            self.downloads = 0

        def validate(self, snapshot):
            return True

        def submit(self, snapshot):
            self.submits += 1
            return RuntimeSubmission("original-provider-task")

        def get_status(self, runtime_reference):
            self.status_checks += 1
            return "SUCCEEDED"

        def get_result(self, runtime_reference):
            self.downloads += 1
            if self.downloads == 1:
                raise OSError("temporary download failure")
            return {"content": b"recovered-output", "filename": "shot.mp4"}

        def cancel(self, runtime_reference):
            return False

    adapter = ArtifactPendingAdapter()
    interrupted = ProductionWorker(service, adapter).run(project.id, execution.id)
    assert interrupted.status.value == "RUNNING"
    child = next(
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id and item.provider_id != "RUNTIME_BOUNDARY"
    )
    assert child.state == "PROVIDER_SUCCEEDED_ARTIFACT_PENDING"

    runner = BackgroundProductionRunner(
        repository, adapter_factory=lambda _task, _plan: adapter
    )
    wrapper = runner.enqueue(project.id, execution.id)
    completed = runner.run_once(project.id)

    assert completed[0].id == wrapper.id
    assert completed[0].state == "SUCCEEDED"
    assert adapter.submits == 1
    assert adapter.status_checks == 1
    assert adapter.downloads == 2
