"""Orchestration for real, manifest-driven Final Assembly rendering."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from aidrama_studio.domain import (
    FinalAssemblyRenderAttempt,
    FinalAssemblyRenderAttemptStatus,
    FinalAssemblyStatus,
)
from aidrama_studio.services.adapters.final_assembly_runtime import (
    FinalAssemblyRenderRequest,
    FinalAssemblyRuntimeAdapter,
    FinalAssemblyRuntimeError,
    MPTFinalAssemblyAdapter,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .final_assembly import FinalAssemblyService, FinalAssemblyServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class FinalAssemblyRuntimeServiceError(RuntimeError):
    """Raised when final rendering violates the immutable manifest boundary."""


class FinalAssemblyRuntimeService:
    """Render one immutable READY manifest and persist every attempt."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        manifest_service: FinalAssemblyService | None = None,
        adapter: FinalAssemblyRuntimeAdapter | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.manifest_service = manifest_service or FinalAssemblyService(self.repository)
        self.adapter = adapter

    def render(self, project_id: str, assembly_id: str):
        """Render a new attempt from exactly the stored manifest items."""
        manifest = self._load_renderable_manifest(project_id, assembly_id)
        adapter = self.adapter or MPTFinalAssemblyAdapter(
            project_root=self._project_root(project_id)
        )
        attempt_number = self._next_attempt_number(assembly_id)
        attempt_id = uuid4().hex
        created_at = _now()
        attempt = self.repository.create_final_assembly_render_attempt(
            FinalAssemblyRenderAttempt(
                id=attempt_id,
                final_assembly_id=assembly_id,
                attempt_number=attempt_number,
                status=FinalAssemblyRenderAttemptStatus.PENDING,
                adapter_name=getattr(adapter, "name", adapter.__class__.__name__),
                created_at=created_at,
            )
        )
        temporary_path: Path | None = None
        try:
            source_paths = tuple(self._resolve_source_path(project_id, item.source_path) for item in manifest.items)
            source_hashes = tuple(self._sha256(path) for path in source_paths)
            for item, source_sha256 in zip(manifest.items, source_hashes):
                if item.source_sha256 and source_sha256 != item.source_sha256:
                    raise FinalAssemblyRuntimeServiceError(
                        "frozen FinalAssembly source SHA256 校验失败"
                    )
            profile = self.repository.get_output_profile(self._assembly(project_id, assembly_id).output_profile_id) if self._assembly(project_id, assembly_id).output_profile_id else None
            profile_data = profile.model_dump(mode="json") if profile is not None else None
            request = FinalAssemblyRenderRequest.from_manifest(manifest, source_paths, output_profile=profile_data)
            # Validate all frozen paths before changing assembly state to
            # ASSEMBLING.  No latest production data is read here.
            validation = adapter.validate_sources(request)
            if validation is False:
                raise FinalAssemblyRuntimeError("frozen source validation failed")
            source_metadata = [adapter.probe_output(path) for path in source_paths]
            expected_duration = sum(self._duration(meta) for meta in source_metadata)
            source_trace = self._source_trace(
                manifest,
                source_metadata=source_metadata,
                source_hashes=source_hashes,
            )
            request = FinalAssemblyRenderRequest.from_manifest(
                manifest, source_paths, expected_duration=expected_duration, output_profile=profile_data
            )
            output_path, output_relative_path = self._choose_output_path(project_id, assembly_id, attempt_id)
            temporary_path = output_path.with_name(f".{attempt_id}.in-progress.mp4")
            if temporary_path.exists():
                temporary_path.unlink()
            self.repository.update_final_assembly_render_attempt(
                attempt.id,
                status=FinalAssemblyRenderAttemptStatus.RUNNING,
                started_at=_now(),
                metadata_json={
                    "assembly_id": assembly_id,
                    "project_id": project_id,
                    "source_items": source_trace,
                    "expected_duration_seconds": expected_duration,
                },
            )
            self.repository.update_final_assembly_status(
                assembly_id, FinalAssemblyStatus.ASSEMBLING, updated_at=_now()
            )
            adapter.render(request, temporary_path)
            probed = dict(adapter.probe_output(temporary_path))
            self._validate_rendered_output(probed, expected_duration)
            probed["sha256"] = self._sha256(temporary_path)
            probed["assembly_id"] = assembly_id
            probed["render_attempt_id"] = attempt_id
            probed["source_items"] = source_trace
            self._atomic_finalize(temporary_path, output_path, project_id)
            temporary_path = None
            finished_at = _now()
            result = self.repository.update_final_assembly_render_attempt(
                attempt.id,
                status=FinalAssemblyRenderAttemptStatus.SUCCEEDED,
                output_relative_path=output_relative_path,
                metadata_json=probed,
                finished_at=finished_at,
            )
            self.repository.update_final_assembly_status(
                assembly_id, FinalAssemblyStatus.SUCCEEDED, updated_at=finished_at
            )
            return result
        except Exception as exc:
            if temporary_path is not None:
                self._remove_temporary(temporary_path)
            error = self._sanitize_error(exc, project_id)
            finished_at = _now()
            try:
                self.repository.update_final_assembly_render_attempt(
                    attempt.id,
                    status=FinalAssemblyRenderAttemptStatus.FAILED,
                    error_message=error,
                    finished_at=finished_at,
                )
                self.repository.update_final_assembly_status(
                    assembly_id, FinalAssemblyStatus.FAILED, updated_at=finished_at
                )
            except Exception:
                # Preserve the original render failure.  The attempt was
                # already inserted, so a later audit can inspect its state.
                pass
            raise FinalAssemblyRuntimeServiceError(error) from exc

    render_assembly = render
    render_final_assembly = render
    run = render

    def retry(self, project_id: str, assembly_id: str):
        """Retry using the same stored READY manifest items, never latest sources."""
        return self.render(project_id, assembly_id)

    retry_render = retry

    def list_attempts(self, project_id: str, assembly_id: str) -> list[FinalAssemblyRenderAttempt]:
        self._assembly(project_id, assembly_id)
        return self.repository.list_final_assembly_render_attempts(assembly_id)

    list_render_attempts = list_attempts

    def latest_successful_attempt(
        self, project_id: str, assembly_id: str
    ) -> FinalAssemblyRenderAttempt | None:
        """Return the newest successful attempt, scoped to the project."""
        attempts = self.list_attempts(project_id, assembly_id)
        successful = [
            attempt
            for attempt in attempts
            if attempt.status is FinalAssemblyRenderAttemptStatus.SUCCEEDED
        ]
        return successful[-1] if successful else None

    def resolve_output_path(
        self,
        project_id: str,
        assembly_id: str,
        attempt_id: str | None = None,
    ) -> Path | None:
        """Resolve a persisted successful output without exposing raw paths.

        The page receives this safe, project-scoped path only for Streamlit's
        preview/download APIs.  The database still stores only a relative
        path, and a missing file returns ``None`` rather than being presented
        as a valid completed video.
        """
        self._assembly(project_id, assembly_id)
        if attempt_id is None:
            attempt = self.latest_successful_attempt(project_id, assembly_id)
        else:
            attempt = self.get_attempt(project_id, attempt_id)
            if attempt.final_assembly_id != assembly_id:
                raise FinalAssemblyRuntimeServiceError("render attempt 不属于该成片版本")
            if attempt.status is not FinalAssemblyRenderAttemptStatus.SUCCEEDED:
                return None
        if attempt is None or not attempt.output_relative_path:
            return None
        relative = attempt.output_relative_path.strip().replace("\\", "/")
        if not relative or relative.startswith("/") or PureWindowsPath(relative).drive:
            raise FinalAssemblyRuntimeServiceError("final output path 必须是项目相对路径")
        parts = PurePosixPath(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise FinalAssemblyRuntimeServiceError("final output path 不能越过项目目录")
        root = self._project_root(project_id)
        target = (root / Path(*parts)).resolve()
        if root not in target.parents or target.suffix.lower() != ".mp4":
            raise FinalAssemblyRuntimeServiceError("final output path 不属于该项目")
        if not target.is_file() or target.stat().st_size <= 0:
            return None
        return target

    def get_attempt(self, project_id: str, attempt_id: str) -> FinalAssemblyRenderAttempt:
        attempt = self.repository.get_final_assembly_render_attempt(attempt_id)
        if attempt is None:
            raise FinalAssemblyRuntimeServiceError("FinalAssemblyRenderAttempt 不存在")
        self._assembly(project_id, attempt.final_assembly_id)
        return attempt

    def cancel(self, project_id: str, assembly_id: str, attempt_id: str | None = None):
        """Cancellation is intentionally not faked for the synchronous seam."""
        self._assembly(project_id, assembly_id)
        raise FinalAssemblyRuntimeServiceError(
            "selected final assembly media runtime does not support safe cancellation"
        )

    def _load_renderable_manifest(self, project_id: str, assembly_id: str):
        try:
            manifest = self.manifest_service.get_manifest(project_id, assembly_id)
        except FinalAssemblyServiceError as exc:
            raise FinalAssemblyRuntimeServiceError(str(exc)) from exc
        if manifest.status is FinalAssemblyStatus.DRAFT:
            raise FinalAssemblyRuntimeServiceError("只有 READY manifest 可以 render")
        if not manifest.items:
            raise FinalAssemblyRuntimeServiceError("FinalAssembly manifest 没有 frozen items")
        orders = [item.order_index for item in manifest.items]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            raise FinalAssemblyRuntimeServiceError("manifest canonical order 无效")
        return manifest

    def _assembly(self, project_id: str, assembly_id: str):
        try:
            return self.manifest_service._get_assembly(project_id, assembly_id)
        except FinalAssemblyServiceError as exc:
            raise FinalAssemblyRuntimeServiceError(str(exc)) from exc

    def _project_root(self, project_id: str) -> Path:
        self._assembly_project(project_id)
        if not self._safe_component(project_id):
            raise FinalAssemblyRuntimeServiceError("project_id 无效")
        root = (self.repository.paths.projects / project_id).resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise FinalAssemblyRuntimeServiceError("project storage path escapes configured root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _assembly_project(self, project_id: str):
        if self.repository.get_project(project_id) is None:
            raise FinalAssemblyRuntimeServiceError(f"项目不存在: {project_id}")

    def _resolve_source_path(self, project_id: str, relative_path: str) -> Path:
        root = self._project_root(project_id)
        if not isinstance(relative_path, str) or not relative_path.strip() or "\x00" in relative_path:
            raise FinalAssemblyRuntimeServiceError("source path 无效")
        normalized = relative_path.strip().replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(relative_path).drive:
            raise FinalAssemblyRuntimeServiceError("source path 必须是项目相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise FinalAssemblyRuntimeServiceError("source path 不能越过项目目录")
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise FinalAssemblyRuntimeServiceError("source path 不属于该项目")
        if not target.exists() or not target.is_file() or target.stat().st_size <= 0:
            raise FinalAssemblyRuntimeServiceError("source 文件不存在或为空")
        return target

    def _choose_output_path(self, project_id: str, assembly_id: str, attempt_id: str) -> tuple[Path, str]:
        root = self._project_root(project_id)
        if not self._safe_component(assembly_id):
            raise FinalAssemblyRuntimeServiceError("assembly_id 无效")
        assembly_root = (root / "final" / assembly_id).resolve()
        if root not in assembly_root.parents:
            raise FinalAssemblyRuntimeServiceError("final output path escapes project")
        assembly_root.mkdir(parents=True, exist_ok=True)
        canonical = assembly_root / "episode.mp4"
        if not canonical.exists():
            return canonical, PurePosixPath("final", assembly_id, "episode.mp4").as_posix()
        attempt_root = (assembly_root / "attempts" / attempt_id).resolve()
        if assembly_root not in attempt_root.parents:
            raise FinalAssemblyRuntimeServiceError("attempt output path escapes assembly")
        attempt_root.mkdir(parents=True, exist_ok=True)
        return attempt_root / "episode.mp4", PurePosixPath("final", assembly_id, "attempts", attempt_id, "episode.mp4").as_posix()

    @staticmethod
    def _source_trace(
        manifest,
        *,
        source_metadata: list[dict[str, object]] | None = None,
        source_hashes: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        timeline = 0.0
        trace: list[dict[str, object]] = []
        metadata_items = source_metadata or [{} for _ in manifest.items]
        hashes = source_hashes or tuple(item.source_sha256 or "" for item in manifest.items)
        for item, metadata, source_sha256 in zip(manifest.items, metadata_items, hashes):
            duration = FinalAssemblyRuntimeService._duration(metadata)
            start = timeline
            end = start + duration
            timeline = end
            trace.append({
                "order_index": item.order_index,
                "production_shot_id": item.production_shot_id,
                "production_execution_id": item.production_execution_id,
                "production_artifact_id": item.production_artifact_id,
                "qc_result_id": item.qc_result_id,
                "review_id": item.review_id,
                "source_relative_path": item.source_path.replace("\\", "/"),
                "source_sha256": source_sha256 or None,
                "source_duration_seconds": duration,
                "timeline_start_seconds": round(start, 6),
                "timeline_end_seconds": round(end, 6),
            })
        return trace

    @staticmethod
    def _duration(metadata: dict[str, object]) -> float:
        value = metadata.get("duration_seconds", metadata.get("duration", 0))
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0

    @staticmethod
    def _validate_rendered_output(metadata: dict[str, object], expected_duration: float) -> None:
        if not metadata.get("video_stream"):
            raise FinalAssemblyRuntimeServiceError("final output 缺少 video stream")
        if not isinstance(metadata.get("size_bytes"), int) or int(metadata["size_bytes"]) <= 0:
            raise FinalAssemblyRuntimeServiceError("final output 为空")
        if (
            not isinstance(metadata.get("width"), int)
            or not isinstance(metadata.get("height"), int)
            or int(metadata["width"]) <= 0
            or int(metadata["height"]) <= 0
        ):
            raise FinalAssemblyRuntimeServiceError("final output dimensions 无效")
        actual = FinalAssemblyRuntimeService._duration(metadata)
        if actual <= 0:
            raise FinalAssemblyRuntimeServiceError("final output duration 无效")
        if expected_duration > 0 and abs(actual - expected_duration) > max(0.35, expected_duration * 0.12):
            raise FinalAssemblyRuntimeServiceError(
                f"final output duration 不匹配（expected={expected_duration:.3f}, actual={actual:.3f}）"
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _atomic_finalize(temporary: Path, target: Path, project_id: str) -> None:
        if not temporary.exists() or temporary.stat().st_size <= 0:
            raise FinalAssemblyRuntimeServiceError("temporary render output 不存在")
        try:
            os.rename(temporary, target)
        except FileExistsError as exc:
            raise FinalAssemblyRuntimeServiceError("成功 final output 不可覆盖") from exc

    @staticmethod
    def _remove_temporary(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _sanitize_error(exc: Exception, project_id: str) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        text = text.replace("\\", "/")
        return text.replace(project_id, "<project>")[:4000]

    def _next_attempt_number(self, assembly_id: str) -> int:
        attempts = self.repository.list_final_assembly_render_attempts(assembly_id)
        return max((item.attempt_number for item in attempts), default=0) + 1

    @staticmethod
    def _safe_component(value: str) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and value.strip() not in {".", ".."}
            and "/" not in value
            and "\\" not in value
            and not PureWindowsPath(value).drive
        )


FinalAssemblyRenderService = FinalAssemblyRuntimeService

__all__ = ["FinalAssemblyRuntimeService", "FinalAssemblyRenderService", "FinalAssemblyRuntimeServiceError"]
