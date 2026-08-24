from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character,
    Location,
    ReferenceAssetType,
    ReferenceBindingType,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Scene,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
    ProductionEventType,
    ProductionExecutionStatus,
)
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionService,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
    ProductionWorker,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes


@pytest.fixture
def context(tmp_path: Path):
    paths = DatabasePaths(tmp_path / "db" / "aidrama.db", tmp_path / "db" / "projects", tmp_path / "db" / "archived")
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Execution project")
    story = StoryBible(
        title="Story", logline="Logline", premise="Premise", genre="Drama", tone="Calm",
        world=World(era="Now"),
        characters=[Character(id="char_001", name="Hero")],
        locations=[Location(id="loc_001", name="Room")],
        story_beats=[
            StoryBeat(id="beat_001", order=1, type="OPENING", summary="Open", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_002", order=2, type="DEVELOPMENT", summary="Middle", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_003", order=3, type="ENDING", summary="End", characters=["char_001"], location_id="loc_001"),
        ],
    )
    repository.create_story_revision(
        revision_id="story_001", project_id=project.id, version=1, status=StoryRevisionStatus.APPROVED,
        content=story, generation_input=None, created_at="now", updated_at="now",
    )
    script = StructuredScript(
        title="Script",
        scenes=[Scene(
            id="scene_001", order=1, title="Room", location_id="loc_001", character_ids=["char_001"],
            estimated_duration_seconds=2,
            beats=[ScriptBeat(id="script_beat_001", order=1, type=ScriptBeatType.ACTION, text="Open")],
        )],
    )
    repository.create_script_revision(
        revision_id="script_001", project_id=project.id, version=1, status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_001", content=script, generation_input=None,
        created_at="now", updated_at="now",
    )
    plan = ShotPlan(
        title="Shot Plan", source_script_revision_id="script_001",
        shots=[Shot(id="shot_001", order=1, scene_id="scene_001", duration_seconds=2, subject=["char_001"], visual_intent="Open")],
    )
    repository.create_shot_revision(
        revision_id="shot_001", project_id=project.id, version=1, status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_001", content=plan, generation_input=None,
        created_at="now", updated_at="now",
    )
    return repository, project


def _ready_job(repository, project):
    reference_service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(reference_service)
    for asset_type, binding_type, binding_id, name in (
        (ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", "hero"),
        (ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", "room"),
    ):
        asset = reference_service.create_asset(project.id, asset_type)
        version = storage.import_image(
            project.id, asset.id, png_bytes(),
            filename=f"{name}.png", mime_type="image/png",
            metadata={"source_story_revision_id": "story_001"},
        )
        reference_service.activate_version(project.id, asset.id, version.id)
        reference_service.bind_version(project.id, version.id, binding_type, binding_id)
    return ProductionService(repository).create_production_job(project.id, "shot_001")


def test_enqueue_and_state_transition_with_immutable_ordered_events(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)

    execution = service.enqueue_job(project.id, job.id, worker_type="placeholder")
    assert execution.status is ProductionExecutionStatus.QUEUED
    assert service.list_events(project.id, execution.id)[0].event_type is ProductionEventType.QUEUED

    with pytest.raises(ProductionExecutionServiceError, match="RUNNING"):
        service.complete_execution(project.id, execution.id)
    running = service.start_execution(project.id, execution.id)
    assert running.status is ProductionExecutionStatus.RUNNING
    progress = service.update_progress(project.id, execution.id, 25, payload_json={"shot": "shot_001"})
    shot_completed = service.append_event(project.id, execution.id, ProductionEventType.SHOT_COMPLETED, {"shot": "shot_001"})
    assert progress.payload_json["progress"] == 25
    assert shot_completed.event_type is ProductionEventType.SHOT_COMPLETED

    finished = service.complete_execution(project.id, execution.id, payload_json={"summary": "future"})
    assert finished.status is ProductionExecutionStatus.SUCCEEDED
    assert [event.event_type for event in service.list_events(project.id, execution.id)] == [
        ProductionEventType.QUEUED, ProductionEventType.STARTED, ProductionEventType.PROGRESS,
        ProductionEventType.SHOT_COMPLETED, ProductionEventType.FINISHED,
    ]
    with pytest.raises(ProductionExecutionServiceError):
        service.start_execution(project.id, execution.id)
    with pytest.raises(ProductionExecutionServiceError):
        service.append_event(project.id, execution.id, ProductionEventType.PROGRESS, {"progress": 100})


def test_invalid_job_cannot_enqueue_and_retry_preserves_history(context):
    repository, project = context
    invalid_job = ProductionService(repository).create_production_job(project.id, "shot_001")
    service = ProductionExecutionService(repository)
    with pytest.raises(ProductionExecutionServiceError, match="READY"):
        service.enqueue_job(project.id, invalid_job.id)

    job = _ready_job(repository, project)
    first = service.enqueue_job(project.id, job.id)
    service.start_execution(project.id, first.id)
    failed = service.fail_execution(project.id, first.id, error_message="temporary")
    assert failed.status is ProductionExecutionStatus.FAILED
    second = service.enqueue_job(project.id, job.id)
    assert second.id != first.id and second.status is ProductionExecutionStatus.QUEUED
    assert [item.event_type for item in service.list_events(project.id, first.id)] == [
        ProductionEventType.QUEUED, ProductionEventType.STARTED, ProductionEventType.FAILED,
    ]
    assert len(service.list_executions(project.id, job.id)) == 2


def test_cancel_and_artifact_metadata_are_project_scoped_and_non_overwriting(context):
    repository, project = context
    job = _ready_job(repository, project)
    service = ProductionExecutionService(repository)
    execution = service.enqueue_job(project.id, job.id)
    cancelled = service.cancel_execution(project.id, execution.id, reason="user")
    assert cancelled.status is ProductionExecutionStatus.CANCELLED
    assert service.list_events(project.id, execution.id)[-1].event_type is ProductionEventType.CANCELLED

    artifact = service.record_artifact(project.id, execution.id, "manifest", "artifacts/manifest.json", {"version": 1})
    assert service.list_artifacts(project.id, execution.id)[0].metadata_json == {"version": 1}
    with pytest.raises(ProductionExecutionServiceError, match="覆盖"):
        service.record_artifact(project.id, execution.id, "manifest", "artifacts/manifest.json")
    with pytest.raises(ProductionExecutionServiceError, match="相对"):
        service.record_artifact(project.id, execution.id, "manifest", "C:/outside.json")
    with pytest.raises(ProductionExecutionServiceError, match="越过"):
        service.record_artifact(project.id, execution.id, "manifest", "../outside.json")

    other = ProjectService(repository).create(title="Other project")
    with pytest.raises(ProductionExecutionServiceError, match="不属于"):
        service.get_execution(other.id, execution.id)
    with pytest.raises(ProductionExecutionServiceError, match="不属于"):
        service.list_events(other.id, execution.id)


def test_worker_boundary_does_not_invoke_a_runtime():
    with pytest.raises(NotImplementedError):
        ProductionWorker().run(object())
