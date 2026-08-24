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
)
from aidrama_studio.services import ProjectService, ReferenceAssetService, ReferenceAssetServiceError
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


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
    version = service.create_version(project.id, asset.id, filename="hero.png", mime_type="image/png", size_bytes=1, sha256=hashlib.sha256(b"x").hexdigest(), storage_path="assets/references/characters/char_001/v1.png")
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
