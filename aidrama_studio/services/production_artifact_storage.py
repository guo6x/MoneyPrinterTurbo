"""Project-isolated storage for production runtime artifacts.

Runtime adapters may return paths owned by the runtime process, or in-memory
bytes in tests.  This service copies those outputs into the AIDrama project
directory and only exposes a project-relative path to the database layer.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from uuid import uuid4

from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.reference_assets import sanitize_filename

from .security import sanitize_error
from .streaming_artifact import StreamingArtifactSource


class ProductionArtifactStorageError(RuntimeError):
    """Raised when an artifact cannot be safely persisted."""


class _BoundedSink:
    """Count writes and stop a provider before it can exhaust local disk."""

    def __init__(self, handle: BinaryIO, max_bytes: int) -> None:
        self.handle = handle
        self.max_bytes = int(max_bytes)
        self.bytes_written = 0

    def write(self, value: bytes | bytearray | memoryview) -> int:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ProductionArtifactStorageError("artifact stream 必须写入 bytes")
        size = len(value)
        if self.bytes_written + size > self.max_bytes:
            raise ProductionArtifactStorageError("artifact stream 超过允许大小")
        written = self.handle.write(value)
        self.bytes_written += int(written)
        return int(written)

    def flush(self) -> None:
        self.handle.flush()

    def fileno(self) -> int:
        return self.handle.fileno()


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
        ``content``/``data``/``path`` plus optional metadata. A physical source
        is mandatory: metadata-only pseudo artifacts are rejected.
        """
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ProductionArtifactStorageError("artifact_type 不能为空")
        execution_root = self.execution_root(project_id, execution_id)
        source, source_name, artifact_metadata = self._unpack(artifact, metadata)
        if source is None:
            raise ProductionArtifactStorageError("artifact 必须包含真实文件或 bytes 内容")
        merged_metadata = dict(artifact_metadata)
        merged_metadata.setdefault("artifact_type", artifact_type.strip())
        # Validate/sanitize metadata before the physical commit so a JSON
        # serialization failure cannot strand an otherwise valid final file.
        safe_metadata = self._plain_metadata(merged_metadata)
        safe_name = sanitize_filename(filename or source_name or artifact_type)
        suffix = Path(safe_name).suffix.lower()
        if not suffix or not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        # The physical object name is content addressed. A repeated download
        # after crash/reconciliation converges on the same path instead of
        # manufacturing a second UUID-named artifact.
        temporary = (
            execution_root
            / f".{self._safe_type(artifact_type)}.{uuid4().hex}.ingest.tmp"
        ).resolve()
        if execution_root.resolve() not in temporary.parents:
            raise ProductionArtifactStorageError(
                "production artifact path escapes execution directory"
            )
        target: Path | None = None
        target_name = ""
        try:
            self._copy_source(source, temporary)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise ProductionArtifactStorageError("artifact physical bytes 为空")
            size_bytes = temporary.stat().st_size
            digest = self._sha256(temporary)
            target_name = (
                f"{self._safe_type(artifact_type)}-sha256-{digest}{suffix}"
            )
            target = (execution_root / target_name).resolve()
            if execution_root.resolve() not in target.parents:
                raise ProductionArtifactStorageError(
                    "production artifact path escapes execution directory"
                )
            if target.exists():
                if not target.is_file() or self._sha256(target) != digest:
                    raise ProductionArtifactStorageError(
                        "content-addressed artifact identity 冲突"
                    )
            else:
                os.replace(temporary, target)
            self._sync_directory(target.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

        if target is None:
            raise ProductionArtifactStorageError("artifact target 未创建")
        relative = PurePosixPath("production", execution_id, target_name).as_posix()
        if target.exists():
            safe_metadata["size_bytes"] = size_bytes
            safe_metadata["sha256"] = digest
            safe_metadata.setdefault("physical_artifact", True)
        return relative, safe_metadata

    def discard_unrecorded(
        self,
        project_id: str,
        execution_id: str,
        relative_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> bool:
        """Compensate a DB insert failure without touching recorded media."""

        execution_root = self.execution_root(project_id, execution_id).resolve()
        relative = PurePosixPath(str(relative_path).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ProductionArtifactStorageError("artifact cleanup path 无效")
        candidate = (self._project_root(project_id) / Path(*relative.parts)).resolve()
        if execution_root not in candidate.parents:
            raise ProductionArtifactStorageError("artifact cleanup path escapes execution directory")
        if any(
            item.path.replace("\\", "/") == relative.as_posix()
            for item in self.repository.list_production_artifacts(execution_id)
        ):
            return False
        if not candidate.is_file():
            return False
        if expected_sha256 and self._sha256(candidate) != expected_sha256:
            return False
        candidate.unlink()
        return True

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
            stream_source = artifact.get("stream_source")
            stream_writer = artifact.get("stream_writer")
            if isinstance(stream_source, StreamingArtifactSource):
                source = stream_source
            elif callable(stream_writer):
                raw_limit = artifact.get("max_bytes")
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as exc:
                    raise ProductionArtifactStorageError("stream_writer 缺少有效 max_bytes") from exc
                source = StreamingArtifactSource(stream_writer, limit)
            elif artifact.get("content") is not None:
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
                    handle.write(source)
                elif isinstance(source, StreamingArtifactSource):
                    sink = _BoundedSink(handle, source.max_bytes)
                    source.write_to(sink)
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
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ProductionArtifactStorageError("artifact 不可覆盖") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Best-effort directory durability; Windows does not expose POSIX fsync."""

        if os.name == "nt":
            return
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _plain_metadata(cls, value: Mapping[str, object]) -> dict[str, object]:
        def plain(item: object, key: str | None = None) -> object:
            if isinstance(item, Mapping):
                safe: dict[str, object] = {}
                for child_key, child in item.items():
                    text_key = str(child_key)
                    lowered = text_key.lower()
                    if (
                        lowered in {"api_key", "apikey", "authorization", "token", "secret", "password"}
                        or "signed_url" in lowered
                        or lowered == "url"
                        or lowered.endswith("_url")
                    ):
                        continue
                    safe[text_key] = plain(child, text_key)
                return safe
            if isinstance(item, (list, tuple, set, frozenset)):
                return [plain(child, key) for child in item]
            if isinstance(item, Path):
                return item.name
            if isinstance(item, str) and key and "path" in key.lower():
                normalized = item.replace("\\", "/")
                if normalized.startswith("/") or PureWindowsPath(item).drive:
                    return Path(normalized).name
                return sanitize_error(
                    PurePosixPath(normalized).as_posix(), max_length=8000
                )
            if isinstance(item, str):
                return sanitize_error(item, max_length=8000)
            if isinstance(item, (int, float, bool)) or item is None:
                return item
            return str(item)

        result = plain(value)
        if not isinstance(result, dict):
            return {"metadata": result}
        # Ensure repository JSON serialization cannot fail on exotic adapter
        # metadata while avoiding storage of absolute filesystem paths.
        json.dumps(result, ensure_ascii=False, sort_keys=True)
        return result
