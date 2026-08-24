from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character,
    Location,
    ProjectStatus,
    ReferenceAssetType,
    ReferenceBindingType,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    World,
    ScriptRevisionStatus,
    StructuredScript,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
)
from aidrama_studio.services import ProjectService, ReferenceAssetService, ReferenceAssetServiceError
from aidrama_studio.services import ReferenceAssetStorageError, ReferenceAssetStorageService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import jpeg_bytes, png_bytes, webp_bytes


@pytest.fixture
def paths(tmp_path: Path) -> DatabasePaths:
    return DatabasePaths(tmp_path / "db" / "aidrama.db", tmp_path / "db" / "projects", tmp_path / "db" / "archived")


@pytest.fixture
def context(paths):
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Asset project")
    story = StoryBible(
        title="Story", logline="Logline", premise="Premise", genre="Drama", tone="Calm",
        world=World(era="Now"),
        characters=[Character(id="char_001", name="Hero")],
        locations=[Location(id="loc_001", name="Room")],
        story_beats=[StoryBeat(id="beat_001", order=1, type="OPENING", summary="Open", characters=["char_001"], location_id="loc_001"), StoryBeat(id="beat_002", order=2, type="DEVELOPMENT", summary="Middle", characters=["char_001"], location_id="loc_001"), StoryBeat(id="beat_003", order=3, type="ENDING", summary="End", characters=["char_001"], location_id="loc_001")],
    )
    now = "2026-01-01T00:00:00+00:00"
    repository.create_story_revision(revision_id="story_001", project_id=project.id, version=1, status=StoryRevisionStatus.APPROVED, content=story, generation_input=None, created_at=now, updated_at=now)
    return repository, project


def test_asset_and_version_creation_numbering_and_current(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    digest = hashlib.sha256(b"image").hexdigest()
    first = service.create_version(project.id, asset.id, filename="hero.png", mime_type="image/png", size_bytes=5, sha256=digest, storage_path="assets/references/characters/char_001/v1.png")
    second = service.create_version(project.id, asset.id, filename="hero-v2.png", mime_type="image/png", size_bytes=5, sha256=hashlib.sha256(b"image2").hexdigest(), storage_path="assets/references/characters/char_001/v2.png")
    assert first.version_number == 1 and second.version_number == 2
    service.activate_version(project.id, asset.id, second.id)
    assert service.get_current_version(project.id, asset.id).id == second.id


def test_versions_are_immutable_and_duplicate_hash_is_rejected(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.LOCATION_REFERENCE)
    digest = hashlib.sha256(b"same").hexdigest()
    version = service.create_version(project.id, asset.id, filename="room.png", mime_type="image/png", size_bytes=4, sha256=digest, storage_path="assets/references/locations/loc_001/v1.png")
    version.filename = "mutated-in-memory.png"
    assert service.list_versions(project.id, asset.id)[0].filename == "room.png"
    other = service.create_asset(project.id, ReferenceAssetType.PROP_REFERENCE)
    with pytest.raises(ReferenceAssetServiceError, match="SHA-256"):
        service.create_version(project.id, other.id, filename="copy.png", mime_type="image/png", size_bytes=4, sha256=digest, storage_path="assets/references/props/v1.png")


def test_duplicate_version_number_is_rejected_by_database(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.STYLE_REFERENCE)
    digest = hashlib.sha256(b"style").hexdigest()
    version = service.create_version(project.id, asset.id, filename="style.png", mime_type="image/png", size_bytes=5, sha256=digest, storage_path="assets/references/styles/v1.png")
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_reference_asset_version(version)


def test_binding_validation_and_project_isolation(context, paths):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    version = service.create_version(project.id, asset.id, filename="hero.png", mime_type="image/png", size_bytes=1, sha256=hashlib.sha256(b"x").hexdigest(), storage_path="assets/references/characters/char_001/v1.png", metadata={"source_story_revision_id": "story_001"})
    binding = service.bind_version(project.id, version.id, ReferenceBindingType.CHARACTER, "char_001")
    assert binding.binding_id == "char_001"
    with pytest.raises(ReferenceAssetServiceError, match="target"):
        service.bind_version(project.id, version.id, ReferenceBindingType.CHARACTER, "missing")
    other = ProjectService(repository).create(title="Other")
    with pytest.raises(ReferenceAssetServiceError, match="不属于"):
        service.list_versions(other.id, asset.id)


def test_storage_path_must_be_relative():
    from aidrama_studio.domain import ReferenceAssetVersion
    values = dict(id="v", asset_id="a", project_id="p", version_number=1, filename="x", mime_type="image/png", size_bytes=1, sha256="a" * 64, created_at="now")
    with pytest.raises(ValueError): ReferenceAssetVersion(**values, storage_path="C:/outside.png")
    with pytest.raises(ValueError): ReferenceAssetVersion(**values, storage_path="assets/../outside.png")


@pytest.mark.parametrize(
    ("filename", "mime", "payload"),
    [
        ("hero.jpg", "image/jpeg", jpeg_bytes()),
        ("hero.png", "image/png", png_bytes()),
        ("hero.webp", "image/webp", webp_bytes()),
    ],
)
def test_controlled_import_validates_stores_and_hashes(context, filename, mime, payload):
    repository, project = context
    service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(service)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    version = storage.import_image(project.id, asset.id, payload, filename="../../CON." + filename.rsplit(".", 1)[-1], mime_type=mime)
    assert version.sha256 == hashlib.sha256(payload).hexdigest()
    assert version.storage_path.startswith("assets/references/")
    assert (repository.paths.projects / project.id / version.storage_path).read_bytes() == payload
    assert version.filename.startswith("CON") is False


def test_import_rejects_fake_extension_signature_and_oversize(context):
    repository, project = context
    service = ReferenceAssetService(repository); storage = ReferenceAssetStorageService(service)
    asset = service.create_asset(project.id, ReferenceAssetType.LOCATION_REFERENCE)
    with pytest.raises(ValueError): storage.import_image(project.id, asset.id, b"not png", filename="x.png", mime_type="image/png")
    with pytest.raises(ValueError): storage.import_image(project.id, asset.id, b"\xff\xd8\xffx\xff\xd9", filename="x.png", mime_type="image/png")
    with pytest.raises(ValueError, match="15 MB"): storage.import_image(project.id, asset.id, b"x" * (15 * 1024 * 1024 + 1), filename="x.jpg", mime_type="image/jpeg")


def test_import_deduplicates_blob_across_assets_and_rejects_same_asset_duplicate(context):
    repository, project = context
    service = ReferenceAssetService(repository); storage = ReferenceAssetStorageService(service)
    payload = png_bytes()
    first = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    second = service.create_asset(project.id, ReferenceAssetType.PROP_REFERENCE)
    one = storage.import_image(project.id, first.id, payload, filename="one.png", mime_type="image/png")
    two = storage.import_image(project.id, second.id, payload, filename="two.png", mime_type="image/png")
    assert one.storage_path == two.storage_path
    with pytest.raises(ReferenceAssetStorageError, match="相同 SHA-256"):
        storage.import_image(project.id, first.id, payload, filename="again.png", mime_type="image/png")


def test_import_project_isolation_and_immutable_blob(context):
    repository, project = context
    other = ProjectService(repository).create(title="Other")
    service = ReferenceAssetService(repository); storage = ReferenceAssetStorageService(service)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    payload = jpeg_bytes()
    version = storage.import_image(project.id, asset.id, payload, filename="hero.jpg", mime_type="image/jpeg")
    path = repository.paths.projects / project.id / version.storage_path
    before = path.read_bytes()
    with pytest.raises(ReferenceAssetStorageError, match="不属于"):
        storage.import_image(other.id, asset.id, payload, filename="hero.jpg", mime_type="image/jpeg")
    assert path.read_bytes() == before


def _create_approved_shot(repository, project):
    script = StructuredScript(
        title="Script",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Opening",
                location_id="loc_001",
                estimated_duration_seconds=2,
                beats=[ScriptBeat(id="script_beat_001", order=1, type=ScriptBeatType.ACTION, text="Open")],
            )
        ],
    )
    repository.create_script_revision(
        revision_id="script_001", project_id=project.id, version=1,
        status=ScriptRevisionStatus.APPROVED, source_story_revision_id="story_001",
        content=script, generation_input=None, created_at="now", updated_at="now",
    )
    plan = ShotPlan(
        title="Shots", source_script_revision_id="script_001",
        shots=[Shot(id="shot_001", order=1, scene_id="scene_001", duration_seconds=2, visual_intent="Open")],
    )
    return repository.create_shot_revision(
        revision_id="shot_plan_001", project_id=project.id, version=1,
        status=ShotRevisionStatus.APPROVED, source_script_revision_id="script_001",
        content=plan, generation_input=None, created_at="now", updated_at="now",
    )


def test_character_location_and_approved_shot_bindings(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    digest = hashlib.sha256(b"binding").hexdigest()

    character_asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    character_version = service.create_version(
        project.id, character_asset.id, filename="hero.png", mime_type="image/png", size_bytes=7,
        sha256=digest, storage_path="assets/references/hero.png",
        metadata={"source_story_revision_id": "story_001"},
    )
    character_binding = service.bind_version(project.id, character_version.id, ReferenceBindingType.CHARACTER, "char_001")

    location_asset = service.create_asset(project.id, ReferenceAssetType.LOCATION_REFERENCE)
    location_version = service.create_version(
        project.id, location_asset.id, filename="room.png", mime_type="image/png", size_bytes=7,
        sha256=hashlib.sha256(b"location-binding").hexdigest(), storage_path="assets/references/room.png",
        metadata={"source_story_revision_id": "story_001"},
    )
    location_binding = service.bind_version(project.id, location_version.id, ReferenceBindingType.LOCATION, "loc_001")

    _create_approved_shot(repository, project)
    shot_asset = service.create_asset(project.id, ReferenceAssetType.PROP_REFERENCE)
    shot_version = service.create_version(
        project.id, shot_asset.id, filename="shot.png", mime_type="image/png", size_bytes=7,
        sha256=hashlib.sha256(b"shot-binding").hexdigest(), storage_path="assets/references/shot.png",
        metadata={"source_story_revision_id": "story_001"},
    )
    shot_binding = service.bind_version(project.id, shot_version.id, ReferenceBindingType.SHOT, "shot_001")

    assert character_binding.binding_type is ReferenceBindingType.CHARACTER
    assert location_binding.binding_type is ReferenceBindingType.LOCATION
    assert shot_binding.binding_type is ReferenceBindingType.SHOT


def test_binding_rejects_unknown_and_cross_project_targets(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    version = service.create_version(
        project.id, asset.id, filename="hero.png", mime_type="image/png", size_bytes=7,
        sha256=hashlib.sha256(b"strict-binding").hexdigest(), storage_path="assets/references/strict.png",
        metadata={"source_story_revision_id": "story_001"},
    )
    with pytest.raises(ReferenceAssetServiceError, match="不存在"):
        service.bind_version(project.id, version.id, ReferenceBindingType.LOCATION, "missing-location")
    with pytest.raises(ReferenceAssetServiceError, match="不存在"):
        service.bind_version(project.id, version.id, ReferenceBindingType.SHOT, "missing-shot")
    other = ProjectService(repository).create(title="Other")
    with pytest.raises(ReferenceAssetServiceError, match="不属于"):
        service.bind_version(other.id, version.id, ReferenceBindingType.CHARACTER, "char_001")
    with pytest.raises(ReferenceAssetServiceError, match="source"):
        service.create_version(
            project.id, asset.id, filename="invalid.png", mime_type="image/png", size_bytes=7,
            sha256=hashlib.sha256(b"invalid-source").hexdigest(), storage_path="assets/references/invalid.png",
            metadata={"source_story_revision_id": "missing-story"},
        )
    missing_source = service.create_version(
        project.id, asset.id, filename="missing-source.png", mime_type="image/png", size_bytes=7,
        sha256=hashlib.sha256(b"missing-source").hexdigest(), storage_path="assets/references/missing-source.png",
    )
    with pytest.raises(ReferenceAssetServiceError, match="source"):
        service.bind_version(project.id, missing_source.id, ReferenceBindingType.CHARACTER, "char_001")


def test_readiness_requires_valid_binding_current_source_and_existing_image(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(service)
    payload = png_bytes()

    character_asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    character_version = storage.import_image(
        project.id, character_asset.id, payload, filename="hero.png", mime_type="image/png",
        metadata={"source_story_revision_id": "story_001"},
    )
    service.activate_version(project.id, character_asset.id, character_version.id)
    service.bind_version(project.id, character_version.id, ReferenceBindingType.CHARACTER, "char_001")

    location_asset = service.create_asset(project.id, ReferenceAssetType.LOCATION_REFERENCE)
    location_version = storage.import_image(
        project.id, location_asset.id, png_bytes(color="blue"), filename="room.png", mime_type="image/png",
        metadata={"source_story_revision_id": "story_001"},
    )
    service.bind_version(project.id, location_version.id, ReferenceBindingType.LOCATION, "loc_001")

    readiness = service.calculate_readiness(project.id, "story_001")
    assert readiness["characters"] == {"total": 1, "used": 1, "locked": 1, "missing": 0, "missing_names": []}
    assert readiness["locations"] == {"total": 1, "used": 1, "locked": 0, "missing": 1, "missing_names": ["Room"]}

    (repository.paths.projects / project.id / character_version.storage_path).unlink()
    assert service.calculate_readiness(project.id, "story_001")["characters"]["locked"] == 0


def test_outdated_version_is_detected_and_can_be_cloned_as_draft(context):
    repository, project = context
    old_story = repository.get_story_revision("story_001")["content"]
    repository.create_story_revision(
        revision_id="story_old", project_id=project.id, version=2,
        status=StoryRevisionStatus.SUPERSEDED, content=old_story,
        generation_input=None, created_at="old", updated_at="old",
    )
    service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(service)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    version = storage.import_image(
        project.id, asset.id, png_bytes(color="green"), filename="old.png", mime_type="image/png",
        metadata={"source_story_revision_id": "story_old"},
    )
    service.activate_version(project.id, asset.id, version.id)
    service.bind_version(project.id, version.id, ReferenceBindingType.CHARACTER, "char_001")

    assert service.is_version_outdated(project.id, version.id) is True
    assert service.calculate_readiness(project.id)["characters"]["missing"] == 1

    draft = service.create_draft_from_version(project.id, asset.id, version.id, source_story_revision_id="story_001")
    assert draft.version_number == 2
    assert draft.metadata["derived_from_version_id"] == version.id
    assert any(binding.asset_version_id == draft.id for binding in service.list_bindings(project.id, draft.id))
    assert service.get_current_version(project.id, asset.id).id == version.id
