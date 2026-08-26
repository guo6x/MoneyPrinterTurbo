from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from aidrama_studio.domain import (
    ReferenceAsset, ReferenceAssetBinding, ReferenceAssetType, ReferenceAssetVersion,
    ReferenceBindingType, ReferenceImageCandidate, ReferenceImageCandidateEvent,
    ReferenceImageCandidateEventType, ReferenceImageCandidateStatus,
    ShotRevisionStatus, StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.reference_assets import (
    image_sha256,
    reference_candidate_blob_path,
    store_immutable_blob,
    validate_image_input,
)
from .security import sanitize_persistent_metadata


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


_UNSAFE_IMAGE_PARAMETER_KEY = re.compile(
    r"(?:^|[_\-.])(?:api[_\-.]?key|access[_\-.]?token|authorization|bearer|"
    r"client[_\-.]?secret|credential(?:[_\-.]?reference)?|password|private[_\-.]?key|"
    r"refresh[_\-.]?token|secret|signature|signed[_\-.]?url|token|url)(?:$|[_\-.])",
    re.IGNORECASE,
)
_UNSAFE_IMAGE_PARAMETER_VALUE = re.compile(
    r"(?i)(?:bearer\s+|https?://|[a-z][a-z0-9+.-]*://|\bsk-[a-z0-9_-]{8,}\b|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret|signature|token)\s*[:=])"
)


def _contains_unsafe_image_parameter(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip()
            compact = re.sub(r"[^A-Za-z0-9_.-]", "", normalized)
            if _UNSAFE_IMAGE_PARAMETER_KEY.search(compact):
                return True
            if _contains_unsafe_image_parameter(child):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_unsafe_image_parameter(item) for item in value)
    if isinstance(value, str):
        return bool(_UNSAFE_IMAGE_PARAMETER_VALUE.search(value))
    return False


def _safe_image_request_parameters(value: object) -> dict[str, object]:
    """Validate provider parameters before they contribute to durable hashes.

    Candidate provenance is intentionally credential-free.  Sanitizing and
    then requiring an unchanged JSON-safe mapping rejects secret-bearing keys,
    signed URLs, and token-like values instead of persisting a value or a
    secret-derived request hash.  The provider adapter remains responsible for
    deciding which ordinary parameters are semantically valid.
    """

    if not isinstance(value, dict):
        # ``Mapping`` implementations are normalized by callers before this
        # helper; rejecting other objects keeps the persistence boundary
        # deterministic.
        raise ReferenceAssetServiceError(
            "image request parameters 必须是 JSON object"
        )
    if _contains_unsafe_image_parameter(value):
        raise ReferenceAssetServiceError(
            "image request parameters 不得包含 secret、URL 或 token"
        )
    safe = sanitize_persistent_metadata(value)
    if not isinstance(safe, dict) or safe != value:
        raise ReferenceAssetServiceError(
            "image request parameters 不得包含 secret、URL 或不可安全持久化值"
        )
    try:
        json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReferenceAssetServiceError(
            "image request parameters 必须可持久化"
        ) from exc
    return dict(safe)


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
        if project is None:
            raise ReferenceAssetServiceError(f"项目不存在: {project_id}")
        return project

    def create_asset(self, project_id: str, asset_type: ReferenceAssetType) -> ReferenceAsset:
        self._require_project(project_id)
        now = _now()
        return self.repository.create_reference_asset(ReferenceAsset(id=uuid4().hex, project_id=project_id, asset_type=asset_type, created_at=now, updated_at=now))

    @staticmethod
    def _workspace_asset_id(
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
    ) -> str:
        """Return the stable pre-promotion workspace identity for a subject."""

        return uuid5(
            NAMESPACE_URL,
            f"aidrama-reference-workspace:{project_id}:{binding_type.value}:{binding_id}",
        ).hex

    def find_workspace_asset(
        self,
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
    ) -> ReferenceAsset | None:
        """Find a bound asset or its durable pre-promotion workspace asset.

        A generated candidate cannot be bound to a subject until a human
        promotes it to a version.  The stable workspace identity keeps those
        Draft candidates discoverable across UI restarts without pretending a
        version or binding already exists.
        """

        self._require_project(project_id)
        bound = self.find_asset_for_binding(project_id, binding_type, binding_id)
        if bound is not None:
            return bound
        workspace_id = self._workspace_asset_id(
            project_id,
            binding_type,
            binding_id,
        )
        candidate = self.repository.get_reference_asset(workspace_id)
        if candidate is None:
            return None
        expected_type = {
            ReferenceBindingType.CHARACTER: ReferenceAssetType.CHARACTER_REFERENCE,
            ReferenceBindingType.LOCATION: ReferenceAssetType.LOCATION_REFERENCE,
        }.get(binding_type)
        if (
            expected_type is None
            or candidate.project_id != project_id
            or candidate.asset_type is not expected_type
        ):
            raise ReferenceAssetServiceError("Reference workspace asset provenance 无效")
        return candidate

    def ensure_workspace_asset(
        self,
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
    ) -> ReferenceAsset:
        """Create the subject's empty candidate workspace, never a version."""

        existing = self.find_workspace_asset(project_id, binding_type, binding_id)
        if existing is not None:
            return existing
        asset_type = {
            ReferenceBindingType.CHARACTER: ReferenceAssetType.CHARACTER_REFERENCE,
            ReferenceBindingType.LOCATION: ReferenceAssetType.LOCATION_REFERENCE,
        }.get(binding_type)
        if asset_type is None:
            raise ReferenceAssetServiceError("该 binding type 不支持 reference workspace")
        if not self._binding_target_exists(project_id, binding_type, binding_id):
            raise ReferenceAssetServiceError("Reference workspace target 不存在")
        now = _now()
        return self.repository.create_reference_asset(
            ReferenceAsset(
                id=self._workspace_asset_id(project_id, binding_type, binding_id),
                project_id=project_id,
                asset_type=asset_type,
                created_at=now,
                updated_at=now,
            )
        )

    def create_version(
        self, project_id: str, asset_id: str, *, filename: str, mime_type: str,
        size_bytes: int, sha256: str, storage_path: str, metadata: dict[str, object] | None = None,
        allow_duplicate_hash: bool = False,
    ) -> ReferenceAssetVersion:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None:
            raise ReferenceAssetServiceError(f"ReferenceAsset 不存在: {asset_id}")
        if asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
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
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        return self.repository.list_reference_asset_versions(asset_id)

    def get_current_version(self, project_id: str, asset_id: str) -> ReferenceAssetVersion | None:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        if not asset.current_version_id:
            return None
        return self.repository.get_reference_asset_version(asset.current_version_id)

    def activate_version(self, project_id: str, asset_id: str, version_id: str) -> ReferenceAsset:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        version = self.repository.get_reference_asset_version(version_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        if version is None or version.asset_id != asset_id or version.project_id != project_id:
            raise ReferenceAssetServiceError("version 不属于该 asset")
        return self.repository.set_current_reference_version(asset_id, version_id, updated_at=_now())

    def record_image_candidate(
        self,
        project_id: str,
        asset_id: str,
        *,
        source_story_revision_id: str,
        provider_id: str,
        model_id: str,
        endpoint_profile_id: str,
        deployment_region: str,
        prompt: str,
        content: bytes,
        filename: str,
        mime_type: str,
        request_parameters: dict[str, object] | None = None,
        parent_candidate_id: str | None = None,
        actor: str = "system",
    ) -> ReferenceImageCandidate:
        """Persist a generated image as a non-canonical DRAFT candidate.

        Recording never creates or locks a ``ReferenceAssetVersion``.  The
        provider request is represented by a stable, credential-free hash;
        only an explicit later promotion enters the version lifecycle.
        """
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        self._story_revision(project_id, source_story_revision_id)
        for label, value in (
            ("provider_id", provider_id),
            ("model_id", model_id),
            ("endpoint_profile_id", endpoint_profile_id),
            ("deployment_region", deployment_region),
            ("prompt", prompt),
            ("actor", actor),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ReferenceAssetServiceError(f"{label} 不能为空")
        safe_name, normalized_mime, suffix = validate_image_input(
            content, filename, mime_type
        )
        candidate_id = uuid4().hex
        digest = image_sha256(content)
        target, relative_path = reference_candidate_blob_path(
            self.repository.paths.projects,
            project_id,
            candidate_id,
            digest,
            suffix,
        )
        safe_request_parameters = _safe_image_request_parameters(
            dict(request_parameters or {})
        )
        request_truth = {
            "project_id": project_id,
            "asset_id": asset_id,
            "source_story_revision_id": source_story_revision_id,
            "provider_id": provider_id.strip(),
            "model_id": model_id.strip(),
            "endpoint_profile_id": endpoint_profile_id.strip(),
            "deployment_region": deployment_region.strip(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "parameters": safe_request_parameters,
        }
        try:
            request_json = json.dumps(
                request_truth, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ReferenceAssetServiceError("image request parameters 必须可持久化") from exc
        now = _now()
        candidate = ReferenceImageCandidate(
            id=candidate_id,
            project_id=project_id,
            asset_id=asset_id,
            source_story_revision_id=source_story_revision_id,
            provider_id=provider_id.strip(),
            model_id=model_id.strip(),
            endpoint_profile_id=endpoint_profile_id.strip(),
            deployment_region=deployment_region.strip(),
            prompt_text=prompt,
            prompt_sha256=request_truth["prompt_sha256"],
            request_sha256=hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
            filename=safe_name,
            mime_type=normalized_mime,
            size_bytes=len(bytes(content)),
            sha256=digest,
            storage_path=relative_path,
            status=ReferenceImageCandidateStatus.DRAFT,
            parent_candidate_id=parent_candidate_id,
            created_at=now,
        )
        event = ReferenceImageCandidateEvent(
            id=uuid4().hex,
            candidate_id=candidate.id,
            sequence_number=1,
            event_type=ReferenceImageCandidateEventType.CREATED,
            actor=actor.strip(),
            created_at=now,
        )
        store_immutable_blob(target, content)
        return self.repository.create_reference_image_candidate(candidate, event)

    def list_image_candidates(
        self, project_id: str, asset_id: str
    ) -> list[ReferenceImageCandidate]:
        self._require_project(project_id)
        asset = self.repository.get_reference_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise ReferenceAssetServiceError("asset 不属于该项目")
        return self.repository.list_reference_image_candidates(
            project_id, asset_id=asset_id
        )

    def get_image_candidate(
        self, project_id: str, candidate_id: str
    ) -> ReferenceImageCandidate:
        self._require_project(project_id)
        candidate = self.repository.get_reference_image_candidate(candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise ReferenceAssetServiceError("image candidate 不属于该项目")
        return candidate

    def reject_image_candidate(
        self,
        project_id: str,
        candidate_id: str,
        *,
        actor: str = "user",
        notes: str = "",
    ) -> ReferenceImageCandidate:
        candidate = self.get_image_candidate(project_id, candidate_id)
        events = self.repository.list_reference_image_candidate_events(candidate.id)
        event = ReferenceImageCandidateEvent(
            id=uuid4().hex,
            candidate_id=candidate.id,
            sequence_number=len(events) + 1,
            event_type=ReferenceImageCandidateEventType.REJECTED,
            actor=actor,
            notes=notes,
            created_at=_now(),
        )
        try:
            return self.repository.reject_reference_image_candidate(
                candidate.id, event
            )
        except (KeyError, ValueError) as exc:
            raise ReferenceAssetServiceError(str(exc)) from exc

    def promote_image_candidate(
        self,
        project_id: str,
        candidate_id: str,
        *,
        actor: str = "user",
        notes: str = "",
    ) -> ReferenceAssetVersion:
        """Promote a Draft candidate without locking it for production."""
        candidate = self.get_image_candidate(project_id, candidate_id)
        if candidate.status is not ReferenceImageCandidateStatus.DRAFT:
            raise ReferenceAssetServiceError("只有 DRAFT candidate 可以提升")
        path = self.resolve_image_candidate_path(project_id, candidate.id)
        if not path.is_file() or path.stat().st_size != candidate.size_bytes:
            raise ReferenceAssetServiceError("image candidate 文件不存在或大小不匹配")
        if image_sha256(path.read_bytes()) != candidate.sha256:
            raise ReferenceAssetServiceError("image candidate SHA-256 不匹配")
        if self.repository.find_reference_version_by_hash(project_id, candidate.sha256):
            raise ReferenceAssetServiceError("该项目已存在相同 SHA-256 的资产版本")
        versions = self.repository.list_reference_asset_versions(candidate.asset_id)
        now = _now()
        version = ReferenceAssetVersion(
            id=uuid4().hex,
            asset_id=candidate.asset_id,
            project_id=project_id,
            version_number=(versions[-1].version_number + 1 if versions else 1),
            filename=candidate.filename,
            mime_type=candidate.mime_type,
            size_bytes=candidate.size_bytes,
            sha256=candidate.sha256,
            storage_path=candidate.storage_path,
            metadata={
                "source_story_revision_id": candidate.source_story_revision_id,
                "source_image_candidate_id": candidate.id,
                "origin": "AI_GENERATED_CANDIDATE",
                "provider_id": candidate.provider_id,
                "model_id": candidate.model_id,
                "endpoint_profile_id": candidate.endpoint_profile_id,
                "deployment_region": candidate.deployment_region,
                "prompt_sha256": candidate.prompt_sha256,
                "request_sha256": candidate.request_sha256,
            },
            created_at=now,
        )
        events = self.repository.list_reference_image_candidate_events(candidate.id)
        event = ReferenceImageCandidateEvent(
            id=uuid4().hex,
            candidate_id=candidate.id,
            sequence_number=len(events) + 1,
            event_type=ReferenceImageCandidateEventType.PROMOTED,
            actor=actor,
            notes=notes,
            promoted_version_id=version.id,
            created_at=now,
        )
        try:
            _, stored_version = self.repository.promote_reference_image_candidate(
                candidate.id, version, event
            )
        except (KeyError, ValueError) as exc:
            raise ReferenceAssetServiceError(str(exc)) from exc
        return stored_version

    def resolve_image_candidate_path(
        self, project_id: str, candidate_id: str
    ) -> Path:
        candidate = self.get_image_candidate(project_id, candidate_id)
        project_root = (self.repository.paths.projects / project_id).resolve()
        image_path = (project_root / candidate.storage_path).resolve()
        if project_root not in image_path.parents:
            raise ReferenceAssetServiceError("image candidate 路径不属于该项目")
        return image_path

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
        if version is None or version.project_id != project_id:
            raise ReferenceAssetServiceError("version 不属于该项目")
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
