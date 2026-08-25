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

from .final_assembly import (
    FinalAssemblyService,
    FinalAssemblyServiceError,
    _output_profile_hash,
)


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

    def build_pending_attempt(
        self,
        project_id: str,
        assembly_id: str,
        *,
        heavy_job_id: str | None = None,
    ) -> FinalAssemblyRenderAttempt:
        """Build, but do not persist, one append-only attempt identity."""
        self._load_renderable_manifest(project_id, assembly_id)
        adapter = self.adapter or MPTFinalAssemblyAdapter(
            project_root=self._project_root(project_id)
        )
        return FinalAssemblyRenderAttempt(
            id=uuid4().hex,
            final_assembly_id=assembly_id,
            attempt_number=self._next_attempt_number(assembly_id),
            status=FinalAssemblyRenderAttemptStatus.PENDING,
            adapter_name=getattr(adapter, "name", adapter.__class__.__name__),
            heavy_job_id=heavy_job_id,
            created_at=_now(),
        )

    def render(
        self,
        project_id: str,
        assembly_id: str,
        *,
        prepared_attempt_id: str | None = None,
    ):
        """Render a new attempt from exactly the stored manifest items."""
        manifest = self._load_renderable_manifest(project_id, assembly_id)
        adapter = self.adapter or MPTFinalAssemblyAdapter(
            project_root=self._project_root(project_id)
        )
        if prepared_attempt_id is None:
            attempt = self.repository.create_final_assembly_render_attempt(
                self.build_pending_attempt(project_id, assembly_id)
            )
        else:
            attempt = self.repository.get_final_assembly_render_attempt(
                prepared_attempt_id
            )
            if (
                attempt is None
                or attempt.final_assembly_id != assembly_id
                or attempt.status is not FinalAssemblyRenderAttemptStatus.PENDING
            ):
                raise FinalAssemblyRuntimeServiceError(
                    "prepared FinalAssembly attempt 不存在、已运行或不属于该成片版本"
                )
            adapter_name = getattr(adapter, "name", adapter.__class__.__name__)
            if attempt.adapter_name != adapter_name:
                raise FinalAssemblyRuntimeServiceError(
                    "prepared FinalAssembly adapter 与当前 runtime 不一致"
                )
        attempt_id = attempt.id
        temporary_path: Path | None = None
        try:
            source_paths = tuple(self._resolve_source_path(project_id, item.source_path) for item in manifest.items)
            source_hashes = tuple(self._sha256(path) for path in source_paths)
            for item, source_sha256 in zip(manifest.items, source_hashes):
                if item.source_sha256 and source_sha256 != item.source_sha256:
                    raise FinalAssemblyRuntimeServiceError(
                        "frozen FinalAssembly source SHA256 校验失败"
                    )
            assembly = self._assembly(project_id, assembly_id)
            profile = self._frozen_output_profile(project_id, assembly)
            profile_data = profile.model_dump(mode="json") if profile is not None else None
            request = FinalAssemblyRenderRequest.from_manifest(manifest, source_paths, output_profile=profile_data)
            # Validate all frozen paths before changing assembly state to
            # ASSEMBLING.  No latest production data is read here.
            validation = adapter.validate_sources(request)
            if validation is False:
                raise FinalAssemblyRuntimeError("frozen source validation failed")
            source_metadata = [adapter.probe_output(path) for path in source_paths]
            self._validate_frozen_source_metadata(manifest.items, source_metadata)
            expected_duration = sum(
                float(
                    item.timeline_duration_seconds
                    if item.timeline_duration_seconds is not None
                    else self._duration(metadata)
                )
                for item, metadata in zip(manifest.items, source_metadata)
            )
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
            self._validate_rendered_output(
                probed, expected_duration, output_profile=profile_data
            )
            actual_duration = self._duration(probed)
            source_trace = self._align_source_trace(
                source_trace,
                expected_duration=expected_duration,
                actual_duration=actual_duration,
            )
            probed["sha256"] = self._sha256(temporary_path)
            probed["assembly_id"] = assembly_id
            probed["render_attempt_id"] = attempt_id
            probed["expected_duration_seconds"] = expected_duration
            probed["source_items"] = source_trace
            target_duration = (
                float(profile.target_episode_duration_seconds)
                if profile is not None
                else None
            )
            target_tolerance = (
                max(0.35, target_duration * 0.01)
                if target_duration is not None
                else None
            )
            probed["target_episode_duration_seconds"] = target_duration
            probed["duration_control"] = {
                "strategy": "DETERMINISTIC_TRIM_HOLD_NO_SPEED_CHANGE",
                "planned_timeline_duration_seconds": expected_duration,
                "actual_duration_seconds": actual_duration,
                "target_duration_seconds": target_duration,
                "target_met": (
                    None
                    if target_duration is None
                    else bool(
                        target_tolerance is not None
                        and abs(actual_duration - target_duration)
                        <= target_tolerance
                    )
                ),
                "truth": (
                    "NO_TARGET_PROFILE"
                    if target_duration is None
                    else (
                        "TARGET_MET"
                        if target_tolerance is not None
                        and abs(actual_duration - target_duration)
                        <= target_tolerance
                        else "ACTUAL_DIFFERS_FROM_TARGET"
                    )
                ),
            }
            source_resolutions = [
                str(item.get("resolution") or "UNKNOWN")
                for item in source_metadata
            ]
            probed["native_source_resolutions"] = source_resolutions
            probed["native_source_fps"] = [
                item.get("fps") for item in source_metadata
            ]
            probed["requested_delivery_resolution"] = (
                f"{profile.delivery_width}x{profile.delivery_height}"
                if profile is not None
                else None
            )
            probed["requested_delivery_fps"] = (
                float(profile.target_fps) if profile is not None else None
            )
            probed["delivery_resolution"] = str(
                probed.get("resolution") or "UNKNOWN"
            )
            probed["delivery_fps"] = probed.get("fps")
            probed["delivery_strategy"] = self._delivery_strategy(
                source_metadata, profile
            )
            probed["source_delivery_transforms"] = self._source_delivery_transforms(
                source_metadata, profile
            )
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

    def render_prepared(
        self, project_id: str, assembly_id: str, attempt_id: str
    ) -> FinalAssemblyRenderAttempt:
        return self.render(
            project_id, assembly_id, prepared_attempt_id=attempt_id
        )

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

    def _frozen_output_profile(self, project_id: str, assembly):
        if assembly.output_profile_id is None:
            if assembly.output_profile_hash is not None:
                raise FinalAssemblyRuntimeServiceError(
                    "FinalAssembly OutputProfile identity 不完整"
                )
            return None
        profile = self.repository.get_output_profile(assembly.output_profile_id)
        if profile is None or profile.project_id != project_id:
            raise FinalAssemblyRuntimeServiceError(
                "FinalAssembly 冻结的 OutputProfile 不存在或项目不匹配"
            )
        actual_hash = _output_profile_hash(profile)
        if assembly.output_profile_hash and actual_hash != assembly.output_profile_hash:
            raise FinalAssemblyRuntimeServiceError(
                "FinalAssembly OutputProfile hash 与冻结身份不匹配"
            )
        return profile

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
            source_duration = FinalAssemblyRuntimeService._duration(metadata)
            duration = float(
                item.timeline_duration_seconds
                if item.timeline_duration_seconds is not None
                else source_duration
            )
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
                "source_duration_seconds": source_duration,
                "planned_timeline_duration_seconds": duration,
                "actual_timeline_duration_seconds": duration,
                "timeline_duration_seconds": duration,
                "trimmed_duration_seconds": item.trimmed_duration_seconds,
                "duration_strategy": item.duration_strategy or "NONE",
                "planned_timeline_start_seconds": round(start, 6),
                "planned_timeline_end_seconds": round(end, 6),
                "actual_timeline_start_seconds": round(start, 6),
                "actual_timeline_end_seconds": round(end, 6),
                "timeline_start_seconds": round(start, 6),
                "timeline_end_seconds": round(end, 6),
            })
        return trace

    @staticmethod
    def _delivery_strategy(source_metadata, profile) -> str:
        if profile is None:
            return "NATIVE_OR_LEGACY"
        delivery_width = int(profile.delivery_width)
        delivery_height = int(profile.delivery_height)
        transforms = []
        for item in source_metadata:
            width = item.get("width")
            height = item.get("height")
            if isinstance(width, int) and isinstance(height, int):
                if width < delivery_width or height < delivery_height:
                    transforms.append("UPSCALE")
                elif width > delivery_width or height > delivery_height:
                    transforms.append("DOWNSCALE")
                else:
                    transforms.append("NATIVE")
        if "UPSCALE" in transforms:
            return "DETERMINISTIC_UPSCALE"
        if "DOWNSCALE" in transforms:
            return "DETERMINISTIC_SCALE"
        return "NATIVE_OR_NORMALIZE"

    @staticmethod
    def _source_delivery_transforms(source_metadata, profile) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in source_metadata:
            width = item.get("width")
            height = item.get("height")
            if profile is None or not isinstance(width, int) or not isinstance(height, int):
                transform = "UNKNOWN_OR_LEGACY"
            elif width < int(profile.delivery_width) or height < int(profile.delivery_height):
                transform = "DETERMINISTIC_UPSCALE"
            elif width > int(profile.delivery_width) or height > int(profile.delivery_height):
                transform = "DETERMINISTIC_SCALE"
            else:
                transform = "NATIVE_OR_NORMALIZE"
            result.append(
                {
                    "native_resolution": (
                        f"{width}x{height}"
                        if isinstance(width, int) and isinstance(height, int)
                        else "UNKNOWN"
                    ),
                    "native_fps": item.get("fps"),
                    "transform": transform,
                }
            )
        return result

    @staticmethod
    def _align_source_trace(
        trace: list[dict[str, object]],
        *,
        expected_duration: float,
        actual_duration: float,
    ) -> list[dict[str, object]]:
        """Map frozen source intervals onto the probed final-media clock.

        Container/frame time bases can make a valid FFmpeg output a few
        frames shorter than the sum of probed inputs.  Source identities and
        original durations stay immutable; only final timeline coordinates
        are scaled so subtitles and later post-production consume the actual
        rendered clock rather than a conflicting theoretical duration.
        """

        if not trace or expected_duration <= 0 or actual_duration <= 0:
            return [dict(item) for item in trace]
        scale = actual_duration / expected_duration
        aligned: list[dict[str, object]] = []
        for index, item in enumerate(trace):
            value = dict(item)
            start = float(
                value.get(
                    "planned_timeline_start_seconds",
                    value.get("timeline_start_seconds", 0),
                )
                or 0
            )
            end = float(
                value.get(
                    "planned_timeline_end_seconds",
                    value.get("timeline_end_seconds", 0),
                )
                or 0
            )
            actual_start = round(start * scale, 6)
            actual_end = (
                round(actual_duration, 6)
                if index == len(trace) - 1
                else round(end * scale, 6)
            )
            value.setdefault("planned_timeline_start_seconds", round(start, 6))
            value.setdefault("planned_timeline_end_seconds", round(end, 6))
            value.setdefault(
                "planned_timeline_duration_seconds", round(end - start, 6)
            )
            value["actual_timeline_start_seconds"] = actual_start
            value["actual_timeline_end_seconds"] = actual_end
            value["actual_timeline_duration_seconds"] = round(
                actual_end - actual_start, 6
            )
            value["timeline_start_seconds"] = actual_start
            value["timeline_end_seconds"] = actual_end
            value["timeline_duration_seconds"] = value[
                "actual_timeline_duration_seconds"
            ]
            aligned.append(value)
        return aligned

    @staticmethod
    def _duration(metadata: dict[str, object]) -> float:
        value = metadata.get("duration_seconds", metadata.get("duration", 0))
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0

    @staticmethod
    def _validate_rendered_output(
        metadata: dict[str, object],
        expected_duration: float,
        *,
        output_profile: dict[str, object] | None = None,
    ) -> None:
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
        profile = dict(output_profile or {})
        expected_fps = float(profile.get("target_fps") or profile.get("fps") or 30)
        duration_tolerance = max(0.12, 2.0 / expected_fps)
        if expected_duration > 0 and abs(actual - expected_duration) > duration_tolerance:
            raise FinalAssemblyRuntimeServiceError(
                f"final output duration 不匹配（expected={expected_duration:.3f}, actual={actual:.3f}）"
            )
        if profile:
            expected_width = int(profile.get("delivery_width") or 0)
            expected_height = int(profile.get("delivery_height") or 0)
            if (
                int(metadata.get("width") or 0) != expected_width
                or int(metadata.get("height") or 0) != expected_height
            ):
                raise FinalAssemblyRuntimeServiceError(
                    "final output resolution 与冻结 OutputProfile 不匹配"
                )
            actual_fps = metadata.get("fps")
            if (
                not isinstance(actual_fps, (int, float))
                or isinstance(actual_fps, bool)
                or abs(float(actual_fps) - expected_fps)
                > max(0.2, expected_fps * 0.01)
            ):
                raise FinalAssemblyRuntimeServiceError(
                    "final output fps 与冻结 OutputProfile 不匹配"
                    f"（expected={expected_fps:g}, actual={actual_fps}）"
                )

    @staticmethod
    def _validate_frozen_source_metadata(items, metadata_items) -> None:
        for item, metadata in zip(items, metadata_items):
            frozen = item.source_duration_seconds
            actual = FinalAssemblyRuntimeService._duration(metadata)
            if frozen is None or frozen <= 0:
                continue
            tolerance = max(0.12, float(frozen) * 0.05)
            if actual <= 0 or abs(actual - float(frozen)) > tolerance:
                raise FinalAssemblyRuntimeServiceError(
                    "physical source duration 与冻结 artifact metadata 不匹配"
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
