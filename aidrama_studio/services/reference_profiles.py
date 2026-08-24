from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import ReferenceBindingType, ReferenceProfile, ReferenceProfileItem
from aidrama_studio.storage.repositories import ProjectRepository

from .reference_assets import ReferenceAssetService, ReferenceAssetServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ReferenceProfileServiceError(ReferenceAssetServiceError):
    pass


class ReferenceProfileService:
    """Ordered profile of immutable ReferenceAssetVersion identities."""

    def __init__(self, repository: ProjectRepository | None = None, *, reference_service: ReferenceAssetService | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.reference_service = reference_service or ReferenceAssetService(self.repository)

    def get_or_create(self, project_id: str, binding_type: str, binding_id: str) -> ReferenceProfile:
        if self.repository.get_project(project_id) is None:
            raise ReferenceProfileServiceError(f"项目不存在: {project_id}")
        profile = self.repository.get_reference_profile_for_binding(project_id, binding_type, binding_id)
        if profile is not None:
            return profile
        now = _now()
        return self.repository.create_reference_profile(ReferenceProfile(id=uuid4().hex, project_id=project_id, binding_type=binding_type, binding_id=binding_id, created_at=now, updated_at=now))

    def add_version(self, project_id: str, binding_type: str, binding_id: str, version_id: str, role: str, *, order_index: int | None = None) -> ReferenceProfileItem:
        try:
            normalized_type = ReferenceBindingType(binding_type).value
            enum_type = ReferenceBindingType(binding_type)
        except ValueError as exc:
            raise ReferenceProfileServiceError("binding_type 无效") from exc
        profile = self.get_or_create(project_id, normalized_type, binding_id)
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id:
            raise ReferenceProfileServiceError("ReferenceAssetVersion 不属于该项目")
        bindings = self.repository.list_reference_bindings(project_id, asset_version_id=version_id)
        if not any(binding.binding_type is enum_type and binding.binding_id == binding_id for binding in bindings):
            self.reference_service.bind_version(project_id, version_id, enum_type, binding_id)
        items = self.repository.list_reference_profile_items(profile.id)
        index = order_index if order_index is not None else (items[-1].order_index + 1 if items else 0)
        return self.repository.create_reference_profile_item(ReferenceProfileItem(id=uuid4().hex, profile_id=profile.id, version_id=version_id, role=role.strip() or "ADDITIONAL", order_index=index, created_at=_now()))

    def list_versions(self, project_id: str, binding_type: str, binding_id: str) -> list[ReferenceProfileItem]:
        profile = self.repository.get_reference_profile_for_binding(project_id, binding_type, binding_id)
        if profile is None:
            return []
        return self.repository.list_reference_profile_items(profile.id)

    def classify_candidate(self, *, source_kind: str, prompt: str = "") -> str:
        text = f"{source_kind} {prompt}".lower()
        if "character" in text or "角色" in text:
            return "CHARACTER_REFERENCE_CANDIDATE"
        if "location" in text or "场景" in text:
            return "LOCATION_REFERENCE_CANDIDATE"
        if "style" in text or "风格" in text:
            return "STYLE_REFERENCE_CANDIDATE"
        if "prop" in text or "道具" in text:
            return "PROP_REFERENCE_CANDIDATE"
        return "GENERAL_INSPIRATION"


__all__ = ["ReferenceProfileService", "ReferenceProfileServiceError"]
