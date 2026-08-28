"""Wave 1 AUTO integration shard.

These tests deliberately exercise AUTO through the persisted project/repository
state.  Provider calls stay behind a deterministic in-process adapter so the
test can prove the create-once boundary without making a live or paid call.
"""

from __future__ import annotations

from pathlib import Path

from aidrama_studio.domain import (
    AutoAction,
    AutoRunStatus,
    AutoStage,
    ReferenceBindingType,
)
from aidrama_studio.services import (
    AutoOrchestratorService,
    ProductionWorker,
    ProjectService,
    StoryService,
)
from aidrama_studio.services.adapters import MockProductionAdapter, RuntimeSubmission
from aidrama_studio.services.production_reliability import PaidBudgetService
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_auto_orchestrator import (
    _AutoServices,
    _FakeImageProvider,
    _FakeLLMGateway,
    _FakeVideoAdapter,
    _approve_reference,
    test_auto_fake_full_pipeline_resume_paid_gate_and_event_log as _full_auto_case,
    test_empty_project_and_human_gate_step_are_idempotent as _state_case,
    _paths,
)


class _UncertainCreateAdapter(_FakeVideoAdapter):
    """The POST outcome is unknown once, then the original task can be polled."""

    def __init__(self) -> None:
        super().__init__()
        self.submit_count = 0
        self.remote_reference = "remote-uncertain-1"
        self.poll_count = 0

    def submit(self, snapshot) -> RuntimeSubmission:
        self.submit_count += 1
        # Register the deterministic remote task in the mock transport before
        # dropping the response.  This models "provider accepted, client timed
        # out" while still allowing reconciliation to poll the same identity.
        accepted = MockProductionAdapter.submit(self, snapshot)
        self.remote_reference = accepted.runtime_reference
        raise TimeoutError(
            "POST https://provider.invalid/create?sig=secret Authorization: Bearer secret"
        )

    def get_status(self, runtime_reference: str) -> str:
        assert runtime_reference == self.remote_reference
        self.poll_count += 1
        return "SUCCEEDED"

    def get_result(self, runtime_reference: str):
        assert runtime_reference == self.remote_reference
        return {
            "artifacts": [
                {
                    "artifact_type": "video",
                    "filename": "shot-001.mp4",
                    "content": self.payload,
                    "metadata": {
                        "mime_type": "video/mp4",
                        "duration_seconds": 1,
                        "resolution": {"width": 320, "height": 180},
                        "codec": "h264",
                        "audio_stream": False,
                        "audio_required": False,
                    },
                }
            ]
        }


def _ready_one_shot(tmp_path: Path, adapter: _FakeVideoAdapter):
    repository = ProjectRepository(_paths(tmp_path / "data"))
    project = ProjectService(repository).create(
        "AUTO uncertain create",
        description="A courier restores a city's power.",
        target_duration_seconds=1,
    )
    gateway = _FakeLLMGateway()
    services = _AutoServices(
        repository, gateway, _FakeImageProvider(), adapter
    )

    story = services.story.generate_story_bible(
        project, brief=project.description, genre="Thriller", tone="Restrained"
    )
    services.story.approve_revision(story["id"])
    script = services.script.generate_script(project)
    services.script.approve_revision(script["id"])
    plan = services.shot.generate_shot_plan(project)
    services.shot.approve_revision(plan["id"])

    auto = services.orchestrator()
    for expected_type in (
        ReferenceBindingType.CHARACTER,
        ReferenceBindingType.LOCATION,
    ):
        decision = auto.resume(project.id)
        assert decision.status is AutoRunStatus.WAITING_HUMAN
        assert decision.current_stage is AutoStage.REFERENCES
        assert decision.metadata["binding_type"] == expected_type.value
        # The AUTO reference boundary is persisted and human-facing; use the
        # formal candidate/promotion/bind/lock service sequence for each ref.
        _approve_reference(services, project.id, decision)

    prepared = auto.resume(project.id)
    assert prepared.status is AutoRunStatus.WAITING_HUMAN
    assert prepared.next_action is AutoAction.PAID_AUTHORIZATION_REQUIRED
    preview = auto.preview_paid_authorization(project.id)
    auto.grant_paid_authorization(
        project.id,
        authorization_fingerprint=preview.authorization_fingerprint,
        global_max=preview.required_create_count,
        per_item_max=1,
        retry_limit=0,
    )
    return repository, project, services, auto


def test_auto_uncertain_create_is_one_shot_and_reconciliation_reuses_identity(
    tmp_path: Path,
) -> None:
    adapter = _UncertainCreateAdapter()
    repository, project, services, auto = _ready_one_shot(tmp_path, adapter)

    queued = auto.step(project.id)
    assert queued.status is AutoRunStatus.WAITING_PROVIDER
    assert queued.current_stage is AutoStage.PRODUCTION

    # AUTO drives the persisted parent task.  The child execution enters the
    # uncertain boundary after exactly one provider submit.
    uncertain = auto.resume(project.id)
    assert uncertain.status in {
        AutoRunStatus.WAITING_PROVIDER,
        AutoRunStatus.BLOCKED,
    }
    assert adapter.submit_count == 1
    child_tasks = [
        task
        for task in repository.list_provider_tasks(project.id)
        if task.execution_id is not None
    ]
    assert len(child_tasks) == 1
    child = child_tasks[0]
    assert child.state == "UNCERTAIN_CREATE"
    production_job_id = str(child.request_summary["production_job_id"])
    assert len(repository.list_paid_create_reservations(production_job_id)) == 1
    projection = PaidBudgetService(repository).projection(
        project.id, production_job_id
    )
    assert projection.uncertain_creates == 1
    assert projection.remaining_creates == 0
    assert "secret" not in (child.error_message or "")

    # A repeated AUTO step/resume is a read/poll boundary, never a replacement
    # create.  The original execution and intent remain the only records.
    auto.resume(project.id)
    assert adapter.submit_count == 1
    assert len(
        [
            task
            for task in repository.list_provider_tasks(project.id)
            if task.execution_id is not None
        ]
    ) == 1

    # Reconciliation supplies the original remote identity, then the worker
    # polls/downloads that task.  No second submit is possible or permitted.
    services.execution.paid_budgets.mark_accepted(
        child,
        provider_task_id=adapter.remote_reference,
        metadata={"remote_task": adapter.remote_reference},
    )
    execution = repository.get_production_execution(child.execution_id)
    assert execution is not None
    repaired = ProductionWorker(
        services.execution,
        adapter,
        poll_interval=0,
        max_polls=2,
    ).reconcile(project.id, execution.id)
    assert repaired.status.value == "SUCCEEDED", [
        (event.event_type.value, event.payload_json)
        for event in repository.list_production_events(execution.id)
    ]
    assert adapter.submit_count == 1
    assert adapter.poll_count >= 1
    assert len(repository.list_production_artifacts(execution.id)) == 1

    # A cold AUTO service resumes from the same persisted execution/task and
    # cannot turn reconciliation into another paid create.
    fresh = ProjectRepository(_paths(tmp_path / "data"))
    fresh_auto = _AutoServices(
        fresh, _FakeLLMGateway(), _FakeImageProvider(), adapter
    ).orchestrator()
    fresh_auto.resume(project.id)
    assert adapter.submit_count == 1


def test_auto_state_machine_uses_persisted_state_and_idempotent_human_boundary(
    tmp_path: Path,
) -> None:
    _state_case(tmp_path)


def test_auto_resume_cold_reload_and_terminal_state_are_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _full_auto_case(tmp_path, monkeypatch)


def test_auto_failed_action_is_persisted_and_provider_failure_is_blocked(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(_paths(tmp_path / "failed"))
    project = ProjectService(repository).create("AUTO failed state")

    class FailingGateway:
        def readiness(self, _project_id):
            return True, "offline fake"

        def generate_validated_json(self, *_args, **_kwargs):
            raise RuntimeError("synthetic generation failure")

    failed = AutoOrchestratorService(
        repository,
        story_service=StoryService(
            repository, llm_gateway=FailingGateway()
        ),
        actor="auto-failed-test",
    ).step(project.id)
    assert failed.status is AutoRunStatus.FAILED
    assert failed.requires_human is True
    assert failed.blocking_reason

    adapter = _FakeVideoAdapter()
    repository, project, _services, auto = _ready_one_shot(
        tmp_path / "blocked", adapter
    )
    auto.step(project.id)
    parent = next(
        task
        for task in repository.list_provider_tasks(project.id)
        if task.execution_id is None
    )
    repository.update_provider_task(
        parent.model_copy(update={"state": "FAILED"})
    )
    blocked = auto.next_action(project.id)
    assert blocked.status is AutoRunStatus.BLOCKED
    assert blocked.blocking_reason == "PROVIDER_TASK_FAILED"
