"""Desktop-owned dispatcher for the canonical durable HeavyJob queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aidrama_studio.domain import HeavyJob, HeavyJobStatus, HeavyJobType
from aidrama_studio.storage.repositories import ProjectRepository

from .background_runner import SingleInstanceGuard
from .final_assembly_runtime import FinalAssemblyRuntimeService
from .heavy_jobs import HeavyJobService, LocalResourcePreflight, _now
from .large_media_export import (
    LargeMediaExportCancelled,
    LargeMediaExportService,
)
from .postproduction import PostProductionService
from .project_archive import ProjectArchiveService
from .security import sanitize_error, sanitize_persistent_metadata
from .tts_runtime import TTSRuntimeService


class HeavyJobRunnerError(RuntimeError):
    pass


class HeavyJobCancelled(HeavyJobRunnerError):
    pass


@dataclass(frozen=True)
class HeavyJobContext:
    repository: ProjectRepository
    job_id: str

    def stage(
        self,
        name: str,
        *,
        current: float | None = None,
        total: float | None = None,
        unit: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        progress: float | None = None
        event_payload = dict(payload or {})
        if current is not None or total is not None:
            if current is None or total is None or total <= 0 or current < 0 or current > total:
                raise HeavyJobRunnerError("可测进度必须提供有效 current/total")
            progress = min(100.0, max(0.0, float(current) / float(total) * 100.0))
            event_payload.update(
                {
                    "progress_current": current,
                    "progress_total": total,
                    "progress_unit": unit or "items",
                }
            )
        self.repository.update_heavy_job_progress(
            self.job_id,
            stage=name,
            progress=progress,
            event_id=uuid4().hex,
            created_at=_now(),
            payload=event_payload,
        )

    def cancel_requested(self) -> bool:
        job = self.repository.get_heavy_job(self.job_id)
        return bool(job and job.cancel_requested)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested():
            raise HeavyJobCancelled("任务已安全取消")


HeavyJobHandler = Callable[[HeavyJob, HeavyJobContext], Mapping[str, Any] | None]


class HeavyJobRunner:
    """Claim one job at a time; every recoverable fact lives in SQLite."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        handlers: Mapping[HeavyJobType, HeavyJobHandler] | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.service = HeavyJobService(self.repository)
        self.handlers = dict(handlers or self.default_handlers(self.repository))
        self.preflight = LocalResourcePreflight(self.repository)
        self.guard = SingleInstanceGuard(
            self.repository.paths.root, "heavy-job-runner.lock"
        )

    @staticmethod
    def default_handlers(
        repository: ProjectRepository,
    ) -> dict[HeavyJobType, HeavyJobHandler]:
        def final_assembly(job: HeavyJob, context: HeavyJobContext):
            context.stage("VALIDATING_MANIFEST")
            snapshot = job.input_snapshot
            attempt = FinalAssemblyRuntimeService(repository).render_prepared(
                str(job.project_id),
                str(snapshot.get("assembly_id") or ""),
                str(snapshot.get("attempt_id") or ""),
            )
            return {
                "attempt_id": attempt.id,
                "assembly_id": attempt.final_assembly_id,
                "output_relative_path": attempt.output_relative_path,
                "metadata": attempt.metadata_json,
            }

        def post_render(job: HeavyJob, context: HeavyJobContext):
            context.stage("VALIDATING_POST_INPUTS")
            snapshot = job.input_snapshot
            expected_inputs = dict(
                snapshot.get("post_input_fingerprints") or {}
            )
            current_inputs = HeavyJobService(repository).post_input_fingerprints(
                str(job.project_id),
                str(snapshot.get("plan_id") or ""),
                subtitle_track_id=_optional(snapshot, "subtitle_track_id"),
                music_track_id=_optional(snapshot, "music_track_id"),
                voice_track_id=_optional(snapshot, "voice_track_id"),
            )
            if current_inputs != expected_inputs:
                raise HeavyJobRunnerError(
                    "Post inputs 在 enqueue 后发生变化；请创建新的后台任务"
                )
            attempt = PostProductionService(repository).render_prepared(
                str(job.project_id),
                str(snapshot.get("plan_id") or ""),
                str(snapshot.get("attempt_id") or ""),
                subtitle_track_id=_optional(snapshot, "subtitle_track_id"),
                music_track_id=_optional(snapshot, "music_track_id"),
                voice_track_id=_optional(snapshot, "voice_track_id"),
            )
            return {
                "attempt_id": attempt.id,
                "plan_id": attempt.plan_id,
                "output_relative_path": attempt.output_relative_path,
                "metadata": attempt.metadata_json,
            }

        def media_export(job: HeavyJob, context: HeavyJobContext):
            context.stage("COPYING", current=0, total=float(job.input_snapshot["source_size_bytes"]), unit="bytes")

            def on_progress(current: int, total: int) -> None:
                context.stage(
                    "COPYING", current=float(current), total=float(total), unit="bytes"
                )

            try:
                return LargeMediaExportService(repository).copy(
                    str(job.project_id),
                    source_relative_path=str(job.input_snapshot["source_relative_path"]),
                    source_sha256=str(job.input_snapshot["source_sha256"]),
                    source_size_bytes=int(job.input_snapshot["source_size_bytes"]),
                    destination=Path(str(job.input_snapshot["destination"])),
                    operation_id=job.id,
                    progress=on_progress,
                    cancelled=context.cancel_requested,
                )
            except LargeMediaExportCancelled as exc:
                raise HeavyJobCancelled(str(exc)) from exc

        def project_export(job: HeavyJob, context: HeavyJobContext):
            context.stage("SNAPSHOTTING_PROJECT")
            destination = Path(str(job.input_snapshot.get("destination") or ""))
            archive = ProjectArchiveService(repository).export_project(
                str(job.project_id),
                destination,
                excluding_heavy_job_id=job.id,
            )
            context.stage("VERIFYING_ARCHIVE")
            return {
                "archive_name": archive.name,
                "size_bytes": archive.stat().st_size,
                "sha256": _sha256_file(archive),
            }

        def project_import(job: HeavyJob, context: HeavyJobContext):
            context.stage("VERIFYING_ARCHIVE")
            relative = str(
                job.input_snapshot.get("staged_archive_relative_path") or ""
            ).replace("\\", "/")
            parts = tuple(part for part in relative.split("/") if part)
            if not parts or any(part in {".", ".."} for part in parts):
                raise HeavyJobRunnerError("项目导入 staging path 无效")
            root = repository.paths.archived_projects.resolve()
            source = (root / Path(*parts)).resolve()
            if root not in source.parents or not source.is_file():
                raise HeavyJobRunnerError("项目导入 staging archive 不存在")
            if source.stat().st_size != int(job.input_snapshot["archive_size_bytes"]):
                raise HeavyJobRunnerError("项目导入 archive size 与冻结输入不一致")
            if _sha256_file(source) != str(job.input_snapshot["archive_sha256"]):
                raise HeavyJobRunnerError("项目导入 archive SHA256 与冻结输入不一致")
            context.stage("RESTORING_PROJECT")
            imported = ProjectArchiveService(repository).import_project(source)
            return {
                "imported_project_id": imported,
                "archive_name": source.name,
            }

        def tts(job: HeavyJob, context: HeavyJobContext):
            snapshot = job.input_snapshot
            track_id = str(snapshot.get("track_id") or "")
            existing = repository.get_post_voice_track(track_id) if track_id else None
            if existing is not None:
                if existing.project_id != job.project_id or existing.plan_id != snapshot.get("plan_id"):
                    raise HeavyJobRunnerError("TTS track identity 与 HeavyJob project/plan 冲突")
                return {
                    "voice_track_id": existing.id,
                    "path": existing.path,
                    "metadata": existing.metadata_json,
                    "reconciled_existing": True,
                }
            context.stage("SYNTHESIZING_TTS")
            track = TTSRuntimeService(repository).synthesize_track(
                str(job.project_id),
                str(snapshot.get("plan_id") or ""),
                list(snapshot.get("cues") or []),
                script_revision_id=str(snapshot.get("script_revision_id") or ""),
                subtitle_track_id=str(snapshot.get("subtitle_track_id") or ""),
                voice_assignments=dict(snapshot.get("voice_assignments") or {}),
                default_voice=str(snapshot.get("default_voice") or ""),
                track_id=track_id,
            )
            return {
                "voice_track_id": track.id,
                "path": track.path,
                "metadata": track.metadata_json,
            }

        return {
            HeavyJobType.FINAL_ASSEMBLY_RENDER: final_assembly,
            HeavyJobType.POST_RENDER: post_render,
            HeavyJobType.FINAL_MEDIA_EXPORT: media_export,
            HeavyJobType.PROJECT_EXPORT: project_export,
            HeavyJobType.PROJECT_IMPORT: project_import,
            HeavyJobType.TTS: tts,
        }

    def reconcile(self) -> list[HeavyJob]:
        return self.service.recover_interrupted()

    def run_once(self, project_id: str | None = None) -> HeavyJob | None:
        with self.guard:
            job = self.repository.claim_next_heavy_job(
                started_at=_now(), event_id=uuid4().hex, project_id=project_id
            )
            if job is None:
                return None
            context = HeavyJobContext(self.repository, job.id)
            try:
                if job.cancel_requested:
                    raise HeavyJobCancelled("任务已在开始前取消")
                preflight = self.preflight.validate(job)
                context.stage("PREFLIGHT_COMPLETE", payload=preflight)
                handler = self.handlers.get(job.job_type)
                if handler is None:
                    raise HeavyJobRunnerError(
                        f"HeavyJob handler 未注册: {job.job_type.value}"
                    )
                result = handler(job, context) or {}
                context.raise_if_cancelled()
                safe = sanitize_persistent_metadata(dict(result))
                if not isinstance(safe, dict):
                    safe = {}
                return self.repository.finish_heavy_job(
                    job.id,
                    status=HeavyJobStatus.SUCCEEDED,
                    stage="FINISHED",
                    event_id=uuid4().hex,
                    finished_at=_now(),
                    output_provenance=safe,
                )
            except HeavyJobCancelled as exc:
                return self.repository.finish_heavy_job(
                    job.id,
                    status=HeavyJobStatus.CANCELLED,
                    stage="CANCELLED",
                    event_id=uuid4().hex,
                    finished_at=_now(),
                    safe_error=sanitize_error(exc, max_length=4000),
                )
            except Exception as exc:
                return self.repository.finish_heavy_job(
                    job.id,
                    status=HeavyJobStatus.FAILED,
                    stage="FAILED",
                    event_id=uuid4().hex,
                    finished_at=_now(),
                    safe_error=sanitize_error(exc, max_length=4000),
                )


def _optional(snapshot: Mapping[str, Any], key: str) -> str | None:
    value = snapshot.get(key)
    return str(value) if value else None


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "HeavyJobCancelled",
    "HeavyJobContext",
    "HeavyJobHandler",
    "HeavyJobRunner",
    "HeavyJobRunnerError",
]
