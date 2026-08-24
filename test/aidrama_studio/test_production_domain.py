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
)
from aidrama_studio.services import (
    ProductionService,
    ProductionServiceError,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


@pytest.fixture
def context(tmp_path: Path):
    paths = DatabasePaths(tmp_path / "db" / "aidrama.db", tmp_path / "db" / "projects", tmp_path / "db" / "archived")
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Production project")
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
        scenes=[
            Scene(
                id="scene_001", order=1, title="Room", location_id="loc_001", character_ids=["char_001"],
                estimated_duration_seconds=2,
                beats=[ScriptBeat(id="script_beat_001", order=1, type=ScriptBeatType.ACTION, text="Open")],
            )
        ],
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


def _reference(repository, project, asset_type, binding_type, binding_id, *, source="story_001", payload=b"\x89PNG\r\n\x1a\nreference-IEND"):
    service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(service)
    asset = service.create_asset(project.id, asset_type)
    version = storage.import_image(
        project.id, asset.id, payload, filename=f"{binding_id}.png", mime_type="image/png",
        metadata={"source_story_revision_id": source},
    )
    service.activate_version(project.id, asset.id, version.id)
    service.bind_version(project.id, version.id, binding_type, binding_id)
    return asset, version


def test_production_job_creation_and_missing_reference_readiness(context):
    repository, project = context
    service = ProductionService(repository)

    job = service.create_production_job(project.id, "shot_001")
    assert job.status.value == "DRAFT"
    readiness = service.validate_job_readiness(project.id, "shot_001")
    assert readiness["ready"] is False
    assert any("character reference" in reason for reason in readiness["blocked_reasons"])
    assert any("location reference" in reason for reason in readiness["blocked_reasons"])

    _reference(repository, project, ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", payload=b"\x89PNG\r\n\x1a\nhero-IEND")
    _reference(repository, project, ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", payload=b"\x89PNG\r\n\x1a\nroom-IEND")
    ready = service.validate_job_readiness(project.id, "shot_001")
    assert ready["ready"] is True
    ready_job = service.create_production_job(project.id, "shot_001")
    assert ready_job.status.value == "READY"


def test_draft_shot_plan_and_invalid_project_are_rejected(context):
    repository, project = context
    plan = repository.get_shot_revision("shot_001")
    repository.create_shot_revision(
        revision_id="shot_draft", project_id=project.id, version=2, status=ShotRevisionStatus.DRAFT,
        source_script_revision_id="script_001", content=plan["content"], generation_input=None,
        created_at="now", updated_at="now",
    )
    service = ProductionService(repository)
    with pytest.raises(ProductionServiceError, match="APPROVED"):
        service.create_production_job(project.id, "shot_draft")
    with pytest.raises(ProductionServiceError, match="项目"):
        service.list_jobs("missing-project")


def test_outdated_reference_blocks_production_readiness(context):
    repository, project = context
    story = repository.get_story_revision("story_001")["content"]
    repository.create_story_revision(
        revision_id="story_old", project_id=project.id, version=2, status=StoryRevisionStatus.SUPERSEDED,
        content=story, generation_input=None, created_at="old", updated_at="old",
    )
    _reference(repository, project, ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", source="story_old", payload=b"\x89PNG\r\n\x1a\nold-hero-IEND")
    _reference(repository, project, ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", source="story_old", payload=b"\x89PNG\r\n\x1a\nold-room-IEND")
    readiness = ProductionService(repository).calculate_production_readiness(project.id, "shot_001")
    assert readiness["ready"] is False
    assert any("reference" in reason for reason in readiness["blocked_reasons"])


def test_production_shots_attempt_numbering_retry_and_immutable_history(context):
    repository, project = context
    _reference(repository, project, ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", payload=b"\x89PNG\r\n\x1a\nhero-attempt-IEND")
    _reference(repository, project, ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", payload=b"\x89PNG\r\n\x1a\nroom-attempt-IEND")
    service = ProductionService(repository)
    job = service.create_production_job(project.id, "shot_001")
    shots = service.create_production_shots(project.id, job.id)
    assert len(shots) == 1 and shots[0].shot_id == "shot_001"

    first = service.start_attempt(project.id, shots[0].id, runtime_adapter="future-adapter", input_snapshot_json={"shot": "shot_001"})
    failed = service.fail_attempt(project.id, first.id, error_message="temporary failure")
    assert failed.attempt_number == 1 and failed.status.value == "FAILED"
    second = service.start_attempt(project.id, shots[0].id, runtime_adapter="future-adapter", input_snapshot_json={"retry": 2})
    completed = service.complete_attempt(project.id, second.id, output_artifact_json={"artifact": "future"})
    assert second.attempt_number == 2 and completed.status.value == "SUCCEEDED"
    history = service.get_job_status(project.id, job.id)["attempts"][shots[0].id]
    assert [attempt.attempt_number for attempt in history] == [1, 2]
    assert history[0].status.value == "FAILED"
    with pytest.raises(ProductionServiceError, match="已经结束"):
        service.complete_attempt(project.id, first.id, output_artifact_json={"overwrite": True})


def test_production_shot_project_isolation(context):
    repository, project = context
    _reference(repository, project, ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", payload=b"\x89PNG\r\n\x1a\nhero-isolation-IEND")
    _reference(repository, project, ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", payload=b"\x89PNG\r\n\x1a\nroom-isolation-IEND")
    service = ProductionService(repository)
    job = service.create_production_job(project.id, "shot_001")
    shot = service.create_production_shots(project.id, job.id)[0]
    other = ProjectService(repository).create(title="Other")
    with pytest.raises(ProductionServiceError, match="不属于"):
        service.start_attempt(other.id, shot.id, runtime_adapter="future-adapter")
