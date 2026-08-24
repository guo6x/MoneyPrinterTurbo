from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character, Location, ProductionExecutionStatus, ProductionInputSnapshot,
    ReferenceAssetType, ReferenceBindingType, Scene, ScriptBeat, ScriptBeatType,
    ScriptRevisionStatus, Shot, ShotPlan, ShotRevisionStatus, StoryBeat,
    StoryBible, StoryRevisionStatus, StructuredScript, World,
)
from aidrama_studio.services import ProductionExecutionService, ProjectService
from aidrama_studio.services.adapters import MPTProductionAdapter, MockProductionAdapter, ProductionRuntimeAdapter
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import _ready_job


@pytest.fixture
def ready_context(tmp_path: Path):
    paths = DatabasePaths(tmp_path / "db" / "aidrama.db", tmp_path / "db" / "projects", tmp_path / "db" / "archived")
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Adapter project")
    story = StoryBible(
        title="Story", logline="Logline", premise="Premise", genre="Drama", tone="Calm",
        world=World(era="Now"), characters=[Character(id="char_001", name="Hero")],
        locations=[Location(id="loc_001", name="Room")], story_beats=[
            StoryBeat(id="beat_001", order=1, type="OPENING", summary="Open", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_002", order=2, type="DEVELOPMENT", summary="Middle", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_003", order=3, type="ENDING", summary="End", characters=["char_001"], location_id="loc_001"),
        ],
    )
    repository.create_story_revision(revision_id="story_001", project_id=project.id, version=1, status=StoryRevisionStatus.APPROVED, content=story, generation_input=None, created_at="now", updated_at="now")
    script = StructuredScript(title="Script", scenes=[Scene(
        id="scene_001", order=1, title="Room", location_id="loc_001", character_ids=["char_001"], estimated_duration_seconds=2,
        beats=[ScriptBeat(id="script_beat_001", order=1, type=ScriptBeatType.ACTION, text="Open")],
    )])
    repository.create_script_revision(revision_id="script_001", project_id=project.id, version=1, status=ScriptRevisionStatus.APPROVED, source_story_revision_id="story_001", content=script, generation_input=None, created_at="now", updated_at="now")
    plan = ShotPlan(title="Shot Plan", source_script_revision_id="script_001", shots=[Shot(id="shot_001", order=1, scene_id="scene_001", duration_seconds=2, subject=["char_001"], visual_intent="Open")])
    repository.create_shot_revision(revision_id="shot_001", project_id=project.id, version=1, status=ShotRevisionStatus.APPROVED, source_script_revision_id="script_001", content=plan, generation_input=None, created_at="now", updated_at="now")
    return repository, project


def test_snapshot_is_deeply_immutable():
    snapshot = ProductionInputSnapshot(
        project_id="project",
        story_revision_id="story",
        script_revision_id="script",
        shot_plan_revision_id="plan",
        reference_asset_versions={"CHARACTER:hero": "version-1"},
        shot_parameters={"shot-1": {"duration_seconds": 2}},
    )
    with pytest.raises((TypeError, ValueError)):
        snapshot.project_id = "other"
    with pytest.raises(TypeError):
        snapshot.reference_asset_versions["CHARACTER:hero"] = "version-2"
    with pytest.raises(TypeError):
        snapshot.shot_parameters["shot-1"]["duration_seconds"] = 4
    assert snapshot.to_json_dict()["shot_parameters"] == {"shot-1": {"duration_seconds": 2}}


def test_adapter_interface_and_mpt_boundary_are_not_runtime_implementations():
    adapter = ProductionRuntimeAdapter()
    snapshot = ProductionInputSnapshot(
        project_id="project", story_revision_id="story", script_revision_id="script", shot_plan_revision_id="plan"
    )
    for method, args in ((adapter.validate, (snapshot,)), (adapter.submit, (snapshot,)), (adapter.cancel, ("ref",)), (adapter.get_status, ("ref",))):
        with pytest.raises(NotImplementedError):
            method(*args)
    mpt = MPTProductionAdapter()
    assert mpt.validate(snapshot) is False
    assert "aidrama_studio.domain" not in Path(mpt.__class__.__module__.replace(".", "/") + ".py").as_posix()


def test_mock_adapter_lifecycle_and_events():
    snapshot = ProductionInputSnapshot(
        project_id="project", story_revision_id="story", script_revision_id="script", shot_plan_revision_id="plan"
    )
    adapter = MockProductionAdapter()
    submission = adapter.submit(snapshot)
    adapter.progress(submission.runtime_reference, 20)
    adapter.shot_completed(submission.runtime_reference, "shot-1")
    adapter.succeed(submission.runtime_reference, artifacts=[{"artifact_type": "manifest", "path": "artifacts/manifest.json"}])
    events = adapter.drain_events(submission.runtime_reference)
    assert [event.event_type for event in events] == ["PROGRESS", "SHOT_COMPLETED", "FINISHED"]
    assert adapter.get_status(submission.runtime_reference) == "SUCCEEDED"


def test_runtime_adapter_submission_and_success_flow(ready_context):
    repository, project = ready_context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    stored = service.get_execution(project.id, execution.id)
    assert stored.input_snapshot is not None
    assert stored.input_snapshot.project_id == project.id
    adapter = MockProductionAdapter()
    running = service.submit_execution(project.id, execution.id, adapter)
    assert running.status is ProductionExecutionStatus.RUNNING
    reference = next(event.payload_json["runtime_reference"] for event in service.list_events(project.id, execution.id) if event.event_type.value == "STARTED")
    adapter.progress(reference, 50)
    adapter.succeed(reference, artifacts=[{"artifact_type": "manifest", "path": "artifacts/manifest.json", "metadata": {"mock": True}}])
    service.handle_runtime_events(project.id, execution.id)
    assert service.get_execution(project.id, execution.id).status is ProductionExecutionStatus.SUCCEEDED
    assert service.list_artifacts(project.id, execution.id)[0].metadata_json == {"mock": True}


def test_runtime_failure_and_cancel_flow(ready_context):
    repository, project = ready_context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    failed_execution = service.submit_execution(project.id, service.enqueue_job(project.id, job.id).id, MockProductionAdapter())
    failed_ref = next(event.payload_json["runtime_reference"] for event in service.list_events(project.id, failed_execution.id) if event.event_type.value == "STARTED")
    failed_adapter = service._adapters[failed_execution.id]
    failed_adapter.fail(failed_ref, "render unavailable")
    service.handle_runtime_events(project.id, failed_execution.id)
    assert service.get_execution(project.id, failed_execution.id).status is ProductionExecutionStatus.FAILED

    retry = service.enqueue_job(project.id, job.id)
    adapter = MockProductionAdapter()
    service.submit_execution(project.id, retry.id, adapter)
    cancelled = service.cancel_execution(project.id, retry.id, "user requested")
    assert cancelled.status is ProductionExecutionStatus.CANCELLED
