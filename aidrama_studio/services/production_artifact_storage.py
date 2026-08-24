"""Project-isolated storage for production runtime artifacts.

Runtime adapters may return paths owned by the runtime process, or in-memory
bytes in tests.  This service copies those outputs into the AIDrama project
directory and only exposes a project-relative path to the database layer.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.reference_assets import sanitize_filename


class ProductionArtifactStorageError(RuntimeError):
    """Raised when an artifact cannot be safely persisted."""


class ProductionArtifactStorageService:
    """Persist runtime outputs below ``projects/<project>/production/<execution>``."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        projects_root: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        configured_root = artifact_root if artifact_root is not None else projects_root
        self.projects_root = Path(configured_root) if configured_root is not None else self.repository.paths.projects

    def execution_root(self, project_id: str, execution_id: str) -> Path:
        self._require_execution(project_id, execution_id)
        project_root = self._project_root(project_id)
        execution_root = (project_root / "production" / execution_id).resolve()
        if project_root not in execution_root.parents:
            raise ProductionArtifactStorageError("production artifact path escapes project root")
        execution_root.mkdir(parents=True, exist_ok=True)
        return execution_root

    def store(
        self,
        project_id: str,
        execution_id: str,
        artifact_type: str,
        artifact: object = None,
        *,
        filename: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Copy one runtime artifact and return its project-relative path.

        ``artifact`` can be bytes, a filesystem path, or a mapping containing
        ``content``/``data``/``path`` plus optional metadata.  A missing source
        is allowed for metadata-only runtime results; in that case no fake
        media bytes are created, but the canonical relative artifact path is
        still persisted by the caller.
        """
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ProductionArtifactStorageError("artifact_type 不能为空")
        execution_root = self.execution_root(project_id, execution_id)
        source, source_name, artifact_metadata = self._unpack(artifact, metadata)
        safe_name = sanitize_filename(filename or source_name or artifact_type)
        if not Path(safe_name).suffix:
            safe_name = f"{safe_name}.bin"
        # UUID names make retries non-overwriting while retaining a useful,
        # sanitized suffix for operators inspecting the artifact directory.
        target_name = f"{self._safe_type(artifact_type)}-{uuid4().hex}-{safe_name}"
        target = (execution_root / target_name).resolve()
        if execution_root.resolve() not in target.parents:
            raise ProductionArtifactStorageError("production artifact path escapes execution directory")

        if source is not None:
            self._copy_source(source, target)

        relative = PurePosixPath("production", execution_id, target_name).as_posix()
        merged_metadata = dict(artifact_metadata)
        merged_metadata.setdefault("artifact_type", artifact_type.strip())
        if source is not None and target.exists():
            merged_metadata.setdefault("size_bytes", target.stat().st_size)
        return relative, self._plain_metadata(merged_metadata)

    def _project_root(self, project_id: str) -> Path:
        if not self._safe_component(project_id):
            raise ProductionArtifactStorageError("project_id 无效")
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProductionArtifactStorageError(f"项目不存在: {project_id}")
        root = (self.projects_root / project_id).resolve()
        configured_root = self.projects_root.resolve()
        if configured_root not in root.parents:
            raise ProductionArtifactStorageError("project storage path escapes configured root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _require_execution(self, project_id: str, execution_id: str):
        if not self._safe_component(execution_id):
            raise ProductionArtifactStorageError("execution_id 无效")
        execution = self.repository.get_production_execution(execution_id)
        if execution is None:
            raise ProductionArtifactStorageError("ProductionExecution 不存在")
        job = self.repository.get_production_job(execution.production_job_id)
        if job is None or job.project_id != project_id:
            raise ProductionArtifactStorageError("ProductionExecution 不属于该项目")
        return execution

    @staticmethod
    def _safe_component(value: str) -> bool:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            return False
        candidate = value.strip()
        return (
            candidate not in {".", ".."}
            and "/" not in candidate
            and "\\" not in candidate
            and not PureWindowsPath(candidate).drive
        )

    @staticmethod
    def _safe_type(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
        return cleaned[:80] or "artifact"

    @classmethod
    def _unpack(
        cls,
        artifact: object,
        metadata: Mapping[str, object] | None,
    ) -> tuple[object | None, str | None, dict[str, object]]:
        source = artifact
        source_name: str | None = None
        result_metadata = dict(metadata or {})
        if isinstance(artifact, Mapping):
            for value in (artifact.get("metadata"), artifact.get("metadata_json")):
                if isinstance(value, Mapping):
                    result_metadata.update(value)
            source_name = cls._as_name(artifact.get("filename") or artifact.get("name"))
            if artifact.get("content") is not None:
                source = artifact["content"]
            elif artifact.get("data") is not None:
                source = artifact["data"]
            elif artifact.get("bytes") is not None:
                source = artifact["bytes"]
            else:
                candidate = artifact.get("source_path") or artifact.get("path")
                if isinstance(candidate, (str, Path)) and Path(candidate).is_file():
                    source = candidate
                else:
                    source = None
                    if candidate:
                        source_name = source_name or cls._as_name(candidate)
                        result_metadata.setdefault("runtime_path", cls._as_name(candidate))
            if source_name is None and isinstance(source, (str, Path)):
                source_name = Path(str(source).replace("\\", "/")).name
        elif isinstance(artifact, Path):
            source_name = artifact.name
            source = artifact if artifact.is_file() else None
        elif isinstance(artifact, str):
            source_name = Path(artifact.replace("\\", "/")).name
            source = artifact if Path(artifact).is_file() else None
        return source, source_name, result_metadata

    @staticmethod
    def _as_name(value: object) -> str | None:
        if isinstance(value, Path):
            return value.name
        if isinstance(value, str) and value.strip():
            return Path(value.replace("\\", "/")).name
        return None

    @staticmethod
    def _copy_source(source: object, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                if isinstance(source, (bytes, bytearray, memoryview)):
                    handle.write(bytes(source))
                elif isinstance(source, str) and not Path(source).is_file():
                    handle.write(source.encode("utf-8"))
                elif isinstance(source, Path) or isinstance(source, str):
                    source_path = Path(source)
                    if not source_path.is_file():
                        raise ProductionArtifactStorageError("runtime artifact source 不存在")
                    with source_path.open("rb") as source_handle:
                        shutil.copyfileobj(source_handle, handle)
                else:
                    raise ProductionArtifactStorageError("runtime artifact source 类型无效")
        except FileExistsError as exc:
            raise ProductionArtifactStorageError("artifact 不可覆盖") from exc

    @classmethod
    def _plain_metadata(cls, value: Mapping[str, object]) -> dict[str, object]:
        def plain(item: object, key: str | None = None) -> object:
            if isinstance(item, Mapping):
                return {str(child_key): plain(child, str(child_key)) for child_key, child in item.items()}
            if isinstance(item, (list, tuple, set, frozenset)):
                return [plain(child, key) for child in item]
            if isinstance(item, Path):
                return item.name
            if isinstance(item, str) and key and "path" in key.lower():
                normalized = item.replace("\\", "/")
                if normalized.startswith("/") or PureWindowsPath(item).drive:
                    return Path(normalized).name
                return PurePosixPath(normalized).as_posix()
            if isinstance(item, (str, int, float, bool)) or item is None:
                return item
            return str(item)

        result = plain(value)
        if not isinstance(result, dict):
            return {"metadata": result}
        # Ensure repository JSON serialization cannot fail on exotic adapter
        # metadata while avoiding storage of absolute filesystem paths.
        json.dumps(result, ensure_ascii=False, sort_keys=True)
        return result
