from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aidrama_studio.domain import (
    Character,
    ReferenceAsset,
    ReferenceAssetType,
    ReferenceAssetVersion,
    ReferenceBindingType,
)
from aidrama_studio.pages import assets


@dataclass(frozen=True)
class _Project:
    id: str


class _FakeRepository:
    class _Paths:
        projects = Path(".")

    paths = _Paths()


class _FakeService:
    def __init__(self, asset=None, current=None):
        self.asset = asset
        self.current = current
        self.calls: list[tuple] = []
        self.repository = _FakeRepository()

    def find_asset_for_binding(self, project_id, binding_type, binding_id):
        self.calls.append(("find", project_id, binding_type, binding_id))
        return self.asset

    def get_current_version(self, project_id, asset_id):
        self.calls.append(("current", project_id, asset_id))
        return self.current

    def create_asset(self, project_id, asset_type):
        self.calls.append(("create", project_id, asset_type))
        self.asset = ReferenceAsset(
            id="asset-created",
            project_id=project_id,
            asset_type=asset_type,
            created_at="now",
            updated_at="now",
        )
        return self.asset

    def bind_version(self, project_id, version_id, binding_type, binding_id):
        self.calls.append(("bind", project_id, version_id, binding_type, binding_id))


class _FakeStorage:
    def __init__(self, version):
        self.version = version
        self.calls: list[tuple] = []

    def import_image(self, project_id, asset_id, data, **kwargs):
        self.calls.append((project_id, asset_id, data, kwargs))
        return self.version


@dataclass(frozen=True)
class _Upload:
    name: str
    type: str
    payload: bytes

    def getvalue(self) -> bytes:
        return self.payload


def _version(*, version_id="v1", source_revision="story-v1"):
    return ReferenceAssetVersion(
        id=version_id,
        asset_id="asset-1",
        project_id="project-1",
        version_number=1,
        filename="hero.png",
        mime_type="image/png",
        size_bytes=8,
        sha256="a" * 64,
        storage_path="assets/references/asset-1/aaaaaaaa.png",
        metadata={"source_story_revision_id": source_revision},
        created_at="now",
    )


def test_page_loads_and_exposes_required_center_sections():
    assert callable(assets.render)
    source = Path(assets.__file__).read_text(encoding="utf-8")
    for label in ("Reference Readiness", "Characters", "Locations", "Styles", "Props", "Version history"):
        assert label in source


def test_readiness_dashboard_is_project_isolated_and_counts_locked_assets():
    project = _Project("project-1")
    subject = Character(id="char-1", name="Hero")
    locked_asset = ReferenceAsset(
        id="asset-1", project_id=project.id, asset_type=ReferenceAssetType.CHARACTER_REFERENCE,
        current_version_id="v1", created_at="now", updated_at="now",
    )
    service = _FakeService(locked_asset, _version())

    locked, missing = assets._readiness(
        service, project, [subject], ReferenceBindingType.CHARACTER, "story-v1"
    )

    assert (locked, missing) == (1, [])
    assert service.calls[0] == ("find", project.id, ReferenceBindingType.CHARACTER, subject.id)
    assert all(call[1] == project.id for call in service.calls)


def test_lock_and_outdated_status_are_distinct():
    locked_asset = ReferenceAsset(
        id="asset-1", project_id="project-1", asset_type=ReferenceAssetType.CHARACTER_REFERENCE,
        current_version_id="v1", created_at="now", updated_at="now",
    )
    assert assets._version_status(locked_asset, _version(), "story-v1") == "LOCKED"
    assert assets._version_status(locked_asset, _version(source_revision="story-v2"), "story-v1") == "REFERENCE OUTDATED"
    assert assets._version_status(locked_asset, _version(version_id="v2"), "story-v1") == "DRAFT"


def test_upload_flow_invokes_storage_then_binds_version():
    project = _Project("project-1")
    subject = Character(id="char-1", name="Hero")
    service = _FakeService()
    imported_version = _version()
    storage = _FakeStorage(imported_version)
    upload = _Upload("hero.png", "image/png", b"png-bytes")

    imported = assets._import_uploads(
        service, storage, project, subject, ReferenceBindingType.CHARACTER, "story-v1", [upload]
    )

    assert imported == 1
    assert storage.calls[0][0:3] == (project.id, "asset-created", upload.payload)
    assert storage.calls[0][3]["metadata"] == {
        "source_story_revision_id": "story-v1",
        "subject_id": subject.id,
    }
    assert service.calls[-1] == ("bind", project.id, imported_version.id, ReferenceBindingType.CHARACTER, subject.id)
