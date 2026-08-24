from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import (
    ReferenceAsset, ReferenceAssetBinding, ReferenceAssetType, ReferenceAssetVersion,
    ReferenceBindingType,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ReferenceAssetServiceError(RuntimeError):
    pass


class ReferenceAssetService:
    def __init__(self, repository: ProjectRepository | None = None):
        self.repository = repository or ProjectRepository()

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None: raise ReferenceAssetServiceError(f"项目不存在: {project_id}")
        return project

    def create_asset(self, project_id: str, asset_type: ReferenceAssetType) -> ReferenceAsset:
        self._require_project(project_id)
        now = _now()
        return self.repository.create_reference_asset(ReferenceAsset(id=uuid4().hex, project_id=project_id, asset_type=asset_type, created_at=now, updated_at=now))

    def create_version(
        self, project_id: str, asset_id: str, *, filename: str, mime_type: str,
        size_bytes: int, sha256: str, storage_path: str, metadata: dict[str, object] | None = None,
        allow_duplicate_hash: bool = False,
    ) -> ReferenceAssetVersion:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None: raise ReferenceAssetServiceError(f"ReferenceAsset 不存在: {asset_id}")
        if asset.project_id != project_id: raise ReferenceAssetServiceError("asset 不属于该项目")
        if self.repository.find_reference_version_by_hash(project_id, sha256) and not allow_duplicate_hash:
            raise ReferenceAssetServiceError("该项目已存在相同 SHA-256 的资产")
        versions = self.repository.list_reference_asset_versions(asset_id)
        version = ReferenceAssetVersion(
            id=uuid4().hex, asset_id=asset_id, project_id=project_id,
            version_number=(versions[-1].version_number + 1 if versions else 1),
            filename=filename, mime_type=mime_type, size_bytes=size_bytes,
            sha256=sha256, storage_path=storage_path, metadata=metadata or {}, created_at=_now(),
        )
        return self.repository.create_reference_asset_version(version)

    def list_versions(self, project_id: str, asset_id: str) -> list[ReferenceAssetVersion]:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None or asset.project_id != project_id: raise ReferenceAssetServiceError("asset 不属于该项目")
        return self.repository.list_reference_asset_versions(asset_id)

    def get_current_version(self, project_id: str, asset_id: str) -> ReferenceAssetVersion | None:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None or asset.project_id != project_id: raise ReferenceAssetServiceError("asset 不属于该项目")
        if not asset.current_version_id: return None
        return self.repository.get_reference_asset_version(asset.current_version_id)

    def activate_version(self, project_id: str, asset_id: str, version_id: str) -> ReferenceAsset:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        version = self.repository.get_reference_asset_version(version_id)
        if asset is None or asset.project_id != project_id: raise ReferenceAssetServiceError("asset 不属于该项目")
        if version is None or version.asset_id != asset_id or version.project_id != project_id: raise ReferenceAssetServiceError("version 不属于该 asset")
        return self.repository.set_current_reference_version(asset_id, version_id, updated_at=_now())

    def _binding_target_exists(self, project_id: str, binding_type: ReferenceBindingType, binding_id: str) -> bool:
        if binding_type in (ReferenceBindingType.CHARACTER, ReferenceBindingType.LOCATION):
            revisions = self.repository.list_story_revisions(project_id)
            for revision in revisions:
                values = revision["content"].characters if binding_type is ReferenceBindingType.CHARACTER else revision["content"].locations
                if any(item.id == binding_id for item in values): return True
            return False
        for revision in self.repository.list_shot_revisions(project_id):
            if any(shot.id == binding_id for shot in revision["content"].shots): return True
        return False

    def bind_version(self, project_id: str, version_id: str, binding_type: ReferenceBindingType, binding_id: str) -> ReferenceAssetBinding:
        self._require_project(project_id)
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id: raise ReferenceAssetServiceError("version 不属于该项目")
        if not self._binding_target_exists(project_id, binding_type, binding_id): raise ReferenceAssetServiceError("binding target 不存在")
        return self.repository.create_reference_binding(ReferenceAssetBinding(id=uuid4().hex, project_id=project_id, asset_version_id=version_id, binding_type=binding_type, binding_id=binding_id, created_at=_now()))

    def list_bindings(self, project_id: str, version_id: str | None = None):
        self._require_project(project_id)
        return self.repository.list_reference_bindings(project_id, asset_version_id=version_id)
