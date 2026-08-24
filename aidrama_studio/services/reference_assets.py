from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aidrama_studio.domain import (
    ReferenceAsset, ReferenceAssetBinding, ReferenceAssetType, ReferenceAssetVersion,
    ReferenceBindingType, ShotRevisionStatus, StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ReferenceAssetServiceError(RuntimeError):
    pass


class ReferenceAssetService:
    # Canonical binding policy: entity-specific references are strict; a Shot
    # may use any explicitly supported reference class because a shot can
    # combine a character, location, style, or prop image.
    BINDING_ASSET_TYPES = {
        ReferenceBindingType.CHARACTER: {ReferenceAssetType.CHARACTER_REFERENCE},
        ReferenceBindingType.LOCATION: {ReferenceAssetType.LOCATION_REFERENCE},
        ReferenceBindingType.SHOT: {
            ReferenceAssetType.CHARACTER_REFERENCE,
            ReferenceAssetType.LOCATION_REFERENCE,
            ReferenceAssetType.STYLE_REFERENCE,
            ReferenceAssetType.PROP_REFERENCE,
        },
    }

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
        if metadata is not None and "source_story_revision_id" in metadata:
            source_id = metadata["source_story_revision_id"]
            if not isinstance(source_id, str) or not source_id:
                raise ReferenceAssetServiceError("version 缺少有效 source Story Bible revision")
            self._story_revision(project_id, source_id)
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
                if any(item.id == binding_id for item in values):
                    return True
            return False
        return any(
            any(shot.id == binding_id for shot in revision["content"].shots)
            for revision in self.repository.list_shot_revisions(project_id)
            if revision["status"] is ShotRevisionStatus.APPROVED
        )

    def _story_revision(self, project_id: str, revision_id: str):
        revision = self.repository.get_story_revision(revision_id)
        if revision is None or revision["project_id"] != project_id:
            raise ReferenceAssetServiceError("source Story Bible revision 不属于该项目")
        if revision["status"] is StoryRevisionStatus.DRAFT:
            raise ReferenceAssetServiceError("source Story Bible revision 尚未确认")
        return revision

    def _version_source_revision(self, project_id: str, version: ReferenceAssetVersion):
        source_id = version.metadata.get("source_story_revision_id")
        if not isinstance(source_id, str) or not source_id:
            raise ReferenceAssetServiceError("version 缺少有效 source Story Bible revision")
        return self._story_revision(project_id, source_id)

    @staticmethod
    def _story_target_exists(revision, binding_type: ReferenceBindingType, binding_id: str) -> bool:
        if binding_type is ReferenceBindingType.CHARACTER:
            return any(item.id == binding_id for item in revision["content"].characters)
        if binding_type is ReferenceBindingType.LOCATION:
            return any(item.id == binding_id for item in revision["content"].locations)
        return False

    def _binding_is_valid(self, binding: ReferenceAssetBinding, version: ReferenceAssetVersion) -> bool:
        if binding.project_id != version.project_id:
            return False
        asset = self.repository.get_reference_asset(version.asset_id)
        if asset is None or asset.project_id != binding.project_id:
            return False
        if asset.asset_type not in self.BINDING_ASSET_TYPES.get(binding.binding_type, set()):
            return False
        try:
            source_revision = self._version_source_revision(binding.project_id, version)
        except ReferenceAssetServiceError:
            return False
        if binding.binding_type in (ReferenceBindingType.CHARACTER, ReferenceBindingType.LOCATION):
            return self._story_target_exists(source_revision, binding.binding_type, binding.binding_id)
        return self._binding_target_exists(binding.project_id, ReferenceBindingType.SHOT, binding.binding_id)

    def bind_version(self, project_id: str, version_id: str, binding_type: ReferenceBindingType, binding_id: str) -> ReferenceAssetBinding:
        self._require_project(project_id)
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id: raise ReferenceAssetServiceError("version 不属于该项目")
        asset = self.repository.get_reference_asset(version.asset_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("version 的 asset 不属于该项目")
        source_revision = self._version_source_revision(project_id, version)
        if binding_type in (ReferenceBindingType.CHARACTER, ReferenceBindingType.LOCATION):
            if not self._story_target_exists(source_revision, binding_type, binding_id):
                raise ReferenceAssetServiceError("binding target 不存在于 source Story Bible revision")
        elif not self._binding_target_exists(project_id, binding_type, binding_id):
            raise ReferenceAssetServiceError("binding target 不存在于 APPROVED ShotPlan")
        if asset.asset_type not in self.BINDING_ASSET_TYPES.get(binding_type, set()):
            allowed = ", ".join(sorted(item.value for item in self.BINDING_ASSET_TYPES.get(binding_type, set())))
            raise ReferenceAssetServiceError(
                f"binding target/type 不兼容：{binding_type.value} binding 要求兼容 ReferenceAsset 类型: {allowed}"
            )
        return self.repository.create_reference_binding(ReferenceAssetBinding(id=uuid4().hex, project_id=project_id, asset_version_id=version_id, binding_type=binding_type, binding_id=binding_id, created_at=_now()))

    def list_bindings(self, project_id: str, version_id: str | None = None):
        self._require_project(project_id)
        return self.repository.list_reference_bindings(project_id, asset_version_id=version_id)

    def list_assets(self, project_id: str) -> list[ReferenceAsset]:
        self._require_project(project_id)
        return self.repository.list_reference_assets(project_id)

    def approved_story_revision(self, project_id: str):
        self._require_project(project_id)
        revisions = self.repository.list_story_revisions(project_id)
        return next((revision for revision in revisions if revision["status"].value == "APPROVED"), None)

    def find_asset_for_binding(self, project_id: str, binding_type: ReferenceBindingType, binding_id: str) -> ReferenceAsset | None:
        for asset in self.list_assets(project_id):
            for version in self.repository.list_reference_asset_versions(asset.id):
                if any(binding.binding_type is binding_type and binding.binding_id == binding_id for binding in self.repository.list_reference_bindings(project_id, asset_version_id=version.id)):
                    return asset
        return None

    def is_version_outdated(self, project_id: str, version_id: str) -> bool:
        self._require_project(project_id)
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id:
            raise ReferenceAssetServiceError("version 不属于该项目")
        approved = self.approved_story_revision(project_id)
        source_id = version.metadata.get("source_story_revision_id")
        return approved is None or source_id != approved["id"]

    def _version_file_exists(self, project_id: str, version_id: str) -> bool:
        try:
            return self.resolve_version_path(project_id, version_id).is_file()
        except ReferenceAssetServiceError:
            return False

    def calculate_readiness(self, project_id: str, story_revision_id: str | None = None) -> dict[str, dict[str, object]]:
        """Return binding-aware readiness for the approved Story Bible subjects."""
        self._require_project(project_id)
        story_revision = self._story_revision(project_id, story_revision_id) if story_revision_id else self.approved_story_revision(project_id)
        if story_revision is None:
            return {
                "characters": {"total": 0, "used": 0, "locked": 0, "missing": 0, "missing_names": []},
                "locations": {"total": 0, "used": 0, "locked": 0, "missing": 0, "missing_names": []},
            }
        bindings = self.repository.list_reference_bindings(project_id)

        def section(subjects, binding_type: ReferenceBindingType) -> dict[str, object]:
            used = 0
            locked = 0
            missing_names: list[str] = []
            for subject in subjects:
                candidates: list[tuple[ReferenceAssetBinding, ReferenceAssetVersion, ReferenceAsset]] = []
                for binding in bindings:
                    if binding.binding_type is not binding_type or binding.binding_id != subject.id:
                        continue
                    version = self.repository.get_reference_asset_version(binding.asset_version_id)
                    if version is None or not self._binding_is_valid(binding, version):
                        continue
                    asset = self.repository.get_reference_asset(version.asset_id)
                    if asset is not None:
                        candidates.append((binding, version, asset))
                if candidates:
                    used += 1
                current_locked = any(
                    asset.current_version_id == version.id
                    and version.metadata.get("source_story_revision_id") == story_revision["id"]
                    and self._version_file_exists(project_id, version.id)
                    for _, version, asset in candidates
                )
                if current_locked:
                    locked += 1
                else:
                    missing_names.append(subject.name)
            return {
                "total": len(subjects),
                "used": used,
                "locked": locked,
                "missing": len(subjects) - locked,
                "missing_names": missing_names,
            }

        story = story_revision["content"]
        return {
            "characters": section(story.characters, ReferenceBindingType.CHARACTER),
            "locations": section(story.locations, ReferenceBindingType.LOCATION),
        }

    readiness = calculate_readiness
    get_readiness = calculate_readiness

    def is_binding_ready(
        self,
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
        story_revision_id: str | None = None,
    ) -> bool:
        """Return whether a target has a locked, current, on-disk reference."""
        self._require_project(project_id)
        story_revision = story_revision_id and self._story_revision(project_id, story_revision_id)
        if story_revision is None:
            story_revision = self.approved_story_revision(project_id)
        if story_revision is None:
            return False
        for binding in self.repository.list_reference_bindings(project_id):
            if binding.binding_type is not binding_type or binding.binding_id != binding_id:
                continue
            version = self.repository.get_reference_asset_version(binding.asset_version_id)
            if version is None or not self._binding_is_valid(binding, version):
                continue
            asset = self.repository.get_reference_asset(version.asset_id)
            if (
                asset is not None
                and asset.current_version_id == version.id
                and version.metadata.get("source_story_revision_id") == story_revision["id"]
                and self._version_file_exists(project_id, version.id)
            ):
                return True
        return False

    reference_ready = is_binding_ready

    def create_draft_from_version(
        self,
        project_id: str,
        asset_id: str,
        version_id: str,
        *,
        source_story_revision_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ReferenceAssetVersion:
        """Create an immutable Draft record that reuses a locked version's blob."""
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        source = self.repository.get_reference_asset_version(version_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        if source is None or source.asset_id != asset_id or source.project_id != project_id:
            raise ReferenceAssetServiceError("version 不属于该 asset")
        if asset.current_version_id != version_id:
            raise ReferenceAssetServiceError("只能从 LOCKED version 创建 Draft")
        if not self._version_file_exists(project_id, version_id):
            raise ReferenceAssetServiceError("locked version 文件不存在")
        draft_metadata = dict(source.metadata)
        next_source_revision = source_story_revision_id or draft_metadata.get("source_story_revision_id")
        if not isinstance(next_source_revision, str) or not next_source_revision:
            raise ReferenceAssetServiceError("Draft 缺少有效 source Story Bible revision")
        self._story_revision(project_id, next_source_revision)
        draft_metadata["source_story_revision_id"] = next_source_revision
        draft_metadata["derived_from_version_id"] = source.id
        if metadata:
            draft_metadata.update(metadata)
        draft = self.create_version(
            project_id,
            asset_id,
            filename=source.filename,
            mime_type=source.mime_type,
            size_bytes=source.size_bytes,
            sha256=source.sha256,
            storage_path=source.storage_path,
            metadata=draft_metadata,
            allow_duplicate_hash=True,
        )
        for binding in self.repository.list_reference_bindings(project_id, asset_version_id=source.id):
            try:
                self.bind_version(project_id, draft.id, binding.binding_type, binding.binding_id)
            except ReferenceAssetServiceError:
                # A superseded Story Bible may no longer contain the old target;
                # keep the Draft available for explicit rebinding instead.
                continue
        return draft

    create_draft_from_locked = create_draft_from_version

    def resolve_version_path(self, project_id: str, version_id: str) -> Path:
        """Resolve an imported image inside the project's isolated storage root."""
        self._require_project(project_id)
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id:
            raise ReferenceAssetServiceError("version 不属于该项目")
        project_root = (self.repository.paths.projects / project_id).resolve()
        image_path = (project_root / version.storage_path).resolve()
        if project_root not in image_path.parents:
            raise ReferenceAssetServiceError("Reference image 路径不属于该项目")
        return image_path
