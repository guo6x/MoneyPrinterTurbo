from __future__ import annotations

from aidrama_studio.domain import ReferenceAssetVersion
from aidrama_studio.services.reference_assets import ReferenceAssetService, ReferenceAssetServiceError
from aidrama_studio.storage.reference_assets import image_sha256, reference_blob_path, store_immutable_blob, validate_image_input


class ReferenceAssetStorageError(ReferenceAssetServiceError):
    pass


class ReferenceAssetStorageService:
    """Controlled import pipeline for immutable reference image blobs."""

    def __init__(self, reference_service: ReferenceAssetService | None = None):
        self.reference_service = reference_service or ReferenceAssetService()
        self.repository = self.reference_service.repository

    def import_image(self, project_id: str, asset_id: str, data: bytes, *, filename: str, mime_type: str, metadata: dict[str, object] | None = None) -> ReferenceAssetVersion:
        self.reference_service._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None: raise ReferenceAssetStorageError(f"ReferenceAsset 不存在: {asset_id}")
        if asset.project_id != project_id: raise ReferenceAssetStorageError("asset 不属于该项目")
        safe_name, normalized_mime, suffix = validate_image_input(data, filename, mime_type)
        digest = image_sha256(data)
        existing = self.repository.find_reference_version_by_hash(project_id, digest)
        if existing is not None and existing.asset_id == asset_id:
            raise ReferenceAssetStorageError("该 asset 已存在相同 SHA-256 的图片")
        if existing is not None:
            relative_path = existing.storage_path
        else:
            _, relative_path = reference_blob_path(self.repository.paths.projects, project_id, asset_id, digest, suffix)
            target = self.repository.paths.projects / project_id / relative_path
            store_immutable_blob(target, data)
        try:
            return self.reference_service.create_version(
                project_id, asset_id, filename=safe_name, mime_type=normalized_mime,
                size_bytes=len(bytes(data)), sha256=digest, storage_path=relative_path,
                metadata=metadata, allow_duplicate_hash=existing is not None,
            )
        except Exception:
            # A newly written blob is intentionally retained: it is immutable and
            # may be referenced by a retry; no destructive rollback is attempted.
            raise
