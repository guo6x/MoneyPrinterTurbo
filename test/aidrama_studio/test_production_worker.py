from __future__ import annotations

import pytest

from aidrama_studio.domain import ProductionEventType, ProductionExecutionStatus
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionRuntimeAdapter,
    ProductionWorker,
)
from aidrama_studio.services.adapters import MPTProductionAdapter, MockProductionAdapter, RuntimeSubmission
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
