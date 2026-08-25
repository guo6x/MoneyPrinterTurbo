from __future__ import annotations

import pytest

from aidrama_studio.domain import ProductionEventType, ProductionExecutionStatus
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionRuntimeAdapter,
    ProductionWorker,
    RuntimeReconciliationRequired,
    RuntimeTransientError,
)
from aidrama_studio.services.adapters import MPTProductionAdapter, MockProductionAdapter, RuntimeSubmission
from aidrama_studio.services.streaming_artifact import StreamingArtifactSource
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


class ImmediateMockAdapter(MockProductionAdapter):
    def submit(self, snapshot):
        submission = super().submit(snapshot)
        assert len(snapshot.shot_parameters) == 1
        self.progress(submission.runtime_reference, 50)
        self.shot_completed(submission.runtime_reference, next(iter(snapshot.shot_parameters)))
        self.succeed(
            submission.runtime_reference,
            artifacts=[
                {
                    "artifact_type": "video",
                    "content": b"one-shot-output",
                    "filename": "shot.mp4",
                    "metadata": {"runtime": "mock"},
                }
            ],
        )
        return submission


class ImmediateFailureAdapter(MockProductionAdapter):
    def submit(self, snapshot):
        submission = super().submit(snapshot)
        self.fail(submission.runtime_reference, "runtime failed")
        return submission


class RaisingAdapter(ProductionRuntimeAdapter):
    name = "raising"

    def validate(self, snapshot):
        return True

    def submit(self, snapshot):
        raise RuntimeError("adapter unavailable")

    def cancel(self, runtime_reference):
        return True

    def get_status(self, runtime_reference):
        return "FAILED"


class MPTResultRuntime:
    def __init__(self):
        self.payload = None
        self.status = "waiting"

    def validate(self, payload):
        return True

    def submit(self, payload):
        self.payload = payload
        self.status = "completed"
        return RuntimeSubmission("mpt-shot-1")

    def get_status(self, reference):
        return self.status

    def get_result(self, reference):
        return {
            "artifacts": [
                {"type": "video", "content": b"mpt-output", "filename": "shot.mp4"}
            ]
        }

    def cancel(self, reference):
        self.status = "cancelled"
        return True


class ResumableStreamingAdapter(ProductionRuntimeAdapter):
    name = "streaming-provider"

    def __init__(self):
        self.submit_count = 0
        self.status_count = 0
        self.download_count = 0

    def validate(self, snapshot):
        return True

    def submit(self, snapshot):
        self.submit_count += 1
        return RuntimeSubmission("paid-task-1")

    def get_status(self, runtime_reference):
        self.status_count += 1
        return "SUCCEEDED"

    def get_result(self, runtime_reference):
        self.download_count += 1

        def writer(sink):
            if self.download_count == 1:
                sink.write(b"partial")
                raise OSError("download interrupted")
            sink.write(b"streamed-final-output")

        return {
            "artifact_type": "video",
            "filename": "shot.mp4",
            "stream_source": StreamingArtifactSource(writer, 1024),
        }

    def cancel(self, runtime_reference):
        return False


class TransientPollingAdapter(ProductionRuntimeAdapter):
    name = "transient-provider"

    def __init__(self, *, always_transient=False, reconciliation=False):
        self.submit_count = 0
        self.status_count = 0
        self.always_transient = always_transient
        self.reconciliation = reconciliation

    def validate(self, snapshot):
        return True

    def submit(self, snapshot):
        self.submit_count += 1
        return RuntimeSubmission("paid-poll-task-1")

    def get_status(self, runtime_reference):
        self.status_count += 1
        if self.reconciliation:
            raise RuntimeReconciliationRequired("unknown provider status")
        if self.always_transient or self.status_count == 1:
            raise RuntimeTransientError(
                "provider rate limited", retry_after_seconds=0
            )
        return "SUCCEEDED"

    def get_result(self, runtime_reference):
        return {"content": b"poll-recovered", "filename": "shot.mp4"}

    def cancel(self, runtime_reference):
        return False


def test_desktop_shutdown_pauses_polling_and_cold_resume_does_not_resubmit(context):
    repository, project = context
    job = _ready_job(repository, project)
    execution_service = ProductionExecutionService(repository)
    execution = execution_service.enqueue_job(project.id, job.id, worker_type="mock")
    adapter = MockProductionAdapter()

    interrupted = ProductionWorker(
        execution_service,
        adapter,
        should_stop=lambda: True,
    ).run(project.id, execution.id)

    assert interrupted.status is ProductionExecutionStatus.RUNNING
    assert len(adapter.submitted_snapshots) == 1
    runtime_reference = next(iter(adapter.submitted_snapshots))
    adapter.succeed(runtime_reference)
    resumed = ProductionWorker(execution_service, adapter).resume(project.id, execution.id)
    assert resumed.status is ProductionExecutionStatus.SUCCEEDED
    assert len(adapter.submitted_snapshots) == 1
    assert len(
        [item for item in repository.list_provider_tasks(project.id) if item.execution_id == execution.id]
    ) == 1


def test_worker_success_polls_mpt_adapter_and_persists_one_shot_artifact(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    runtime = MPTResultRuntime()
    worker = ProductionWorker(service, MPTProductionAdapter(runtime))
    execution = service.enqueue_job(project.id, job.id)

    result = worker.run(project.id, execution.id)

    assert result.status is ProductionExecutionStatus.SUCCEEDED
    assert len(runtime.payload["shots"]) == 1
    assert len(service.get_execution(project.id, execution.id).input_snapshot.shot_parameters) == 1
    events = service.list_events(project.id, execution.id)
    assert [event.event_type for event in events] == [
        ProductionEventType.QUEUED,
        ProductionEventType.STARTED,
        ProductionEventType.FINISHED,
    ]
    artifacts = service.list_artifacts(project.id, execution.id)
    assert len(artifacts) == 1
    assert artifacts[0].path.startswith(f"production/{execution.id}/")
    assert (repository.paths.projects / project.id / artifacts[0].path).read_bytes() == b"mpt-output"


def test_worker_accepts_persisted_execution_object(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    result = ProductionWorker(service, MPTProductionAdapter(MPTResultRuntime())).run(execution)

    assert result.status is ProductionExecutionStatus.SUCCEEDED


def test_worker_success_writes_progress_and_shot_events_in_order(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    result = ProductionWorker(service, ImmediateMockAdapter()).run(project.id, execution.id)

    assert result.status is ProductionExecutionStatus.SUCCEEDED
    assert [event.event_type for event in service.list_events(project.id, execution.id)] == [
        ProductionEventType.QUEUED,
        ProductionEventType.STARTED,
        ProductionEventType.PROGRESS,
        ProductionEventType.SHOT_COMPLETED,
        ProductionEventType.FINISHED,
    ]


def test_worker_failure_is_durable_and_retry_creates_new_execution(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    first = service.enqueue_job(project.id, job.id)

    failed = ProductionWorker(service, ImmediateFailureAdapter()).run(project.id, first.id)

    assert failed.status is ProductionExecutionStatus.FAILED
    assert service.list_events(project.id, first.id)[-1].event_type is ProductionEventType.FAILED
    second = service.enqueue_job(project.id, job.id)
    assert second.id != first.id
    assert len(service.list_executions(project.id, job.id)) == 2


def test_worker_adapter_error_marks_execution_failed(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)

    result = ProductionWorker(service, RaisingAdapter()).run(project.id, execution.id)

    assert result.status is ProductionExecutionStatus.FAILED
    assert service.list_events(project.id, execution.id)[-1].payload_json["error"]


def test_provider_task_intent_is_durable_and_submission_is_not_repeated(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    adapter = ImmediateMockAdapter()
    execution = service.enqueue_job(project.id, job.id)
    result = ProductionWorker(service, adapter).run(project.id, execution.id)
    assert result.status is ProductionExecutionStatus.SUCCEEDED
    tasks = repository.list_provider_tasks(project.id)
    assert len(tasks) == 1
    assert tasks[0].execution_id == execution.id
    assert tasks[0].state == "SUCCEEDED"
    assert tasks[0].provider_task_id
    # A second submit against the terminal execution is rejected before any
    # adapter call; the durable task remains the sole provider identity.
    with pytest.raises(Exception):
        service.submit_execution(project.id, execution.id, adapter)


def test_worker_cancel_notifies_adapter_and_persists_cancelled_event(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    adapter = MockProductionAdapter()
    execution = service.enqueue_job(project.id, job.id)
    service.submit_execution(project.id, execution.id, adapter)

    cancelled = ProductionWorker(service, adapter).cancel(project.id, execution.id, "user")

    assert cancelled.status is ProductionExecutionStatus.CANCELLED
    assert service.list_events(project.id, execution.id)[-1].event_type is ProductionEventType.CANCELLED


def test_worker_requires_explicit_project_scoped_identity():
    with pytest.raises(NotImplementedError):
        ProductionWorker().run(object())


def test_provider_success_download_can_resume_without_paid_resubmission(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    adapter = ResumableStreamingAdapter()
    worker = ProductionWorker(service, adapter)

    interrupted = worker.run(project.id, execution.id)

    assert interrupted.status is ProductionExecutionStatus.RUNNING
    assert adapter.submit_count == 1
    assert adapter.download_count == 1
    task = next(
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id
    )
    assert task.state == "PROVIDER_SUCCEEDED_ARTIFACT_PENDING"
    assert service.list_artifacts(project.id, execution.id) == []
    execution_root = repository.paths.projects / project.id / "production" / execution.id
    assert list(execution_root.iterdir()) == []

    completed = worker.resume(project.id, execution.id)

    assert completed.status is ProductionExecutionStatus.SUCCEEDED
    assert adapter.submit_count == 1
    assert adapter.status_count == 1
    assert adapter.download_count == 2
    artifact = service.list_artifacts(project.id, execution.id)[0]
    assert (repository.paths.projects / project.id / artifact.path).read_bytes() == b"streamed-final-output"


def test_artifact_db_failure_compensates_finalized_file(context, monkeypatch):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    adapter = ResumableStreamingAdapter()
    adapter.download_count = 1  # make the first attempted download succeed

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("injected DB failure")

    monkeypatch.setattr(service, "record_artifact", fail_record)
    result = ProductionWorker(service, adapter).run(project.id, execution.id)

    assert result.status is ProductionExecutionStatus.RUNNING
    assert service.list_artifacts(project.id, execution.id) == []
    execution_root = repository.paths.projects / project.id / "production" / execution.id
    assert list(execution_root.iterdir()) == []


def test_transient_poll_failure_recovers_without_failing_or_resubmitting(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    adapter = TransientPollingAdapter()

    result = ProductionWorker(service, adapter, max_polls=3).run(
        project.id, execution.id
    )

    assert result.status is ProductionExecutionStatus.SUCCEEDED
    assert adapter.submit_count == 1
    assert adapter.status_count == 2
    assert all(
        event.event_type is not ProductionEventType.FAILED
        for event in service.list_events(project.id, execution.id)
    )


def test_polling_window_interrupts_and_cold_resume_reuses_provider_task(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    adapter = TransientPollingAdapter(always_transient=True)
    worker = ProductionWorker(service, adapter, max_polls=1)

    interrupted = worker.run(project.id, execution.id)

    assert interrupted.status is ProductionExecutionStatus.RUNNING
    task = next(
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id
    )
    assert task.state == "POLLING_INTERRUPTED"
    assert task.metadata["poll_failure_count"] == 1
    assert adapter.submit_count == 1

    adapter.always_transient = False
    adapter.status_count = 1
    completed = ProductionWorker(service, adapter, max_polls=1).resume(
        project.id, execution.id
    )
    assert completed.status is ProductionExecutionStatus.SUCCEEDED
    assert adapter.submit_count == 1


def test_unknown_poll_state_requires_reconciliation_without_false_failure(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    adapter = TransientPollingAdapter(reconciliation=True)

    result = ProductionWorker(service, adapter, max_polls=1).run(
        project.id, execution.id
    )

    assert result.status is ProductionExecutionStatus.RUNNING
    task = next(
        item
        for item in repository.list_provider_tasks(project.id)
        if item.execution_id == execution.id
    )
    assert task.state == "RECONCILIATION_REQUIRED"
    assert adapter.submit_count == 1
    assert all(
        event.event_type is not ProductionEventType.FAILED
        for event in service.list_events(project.id, execution.id)
    )
