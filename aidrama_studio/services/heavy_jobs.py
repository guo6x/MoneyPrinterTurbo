"""Canonical durable queue for long-running AIDrama V1 operations."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from aidrama_studio.domain import (
    HeavyJob,
    HeavyJobEvent,
    HeavyJobStatus,
    HeavyJobType,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .final_assembly_runtime import FinalAssemblyRuntimeService
from .postproduction import PostProductionService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HeavyJobServiceError("HeavyJob input 必须是有限、可持久化的 JSON") from exc


def _snapshot_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class HeavyJobServiceError(RuntimeError):
    pass


class HeavyJobService:
    """Create immutable job inputs and expose explicit lifecycle operations."""

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def enqueue(
        self,
        job_type: HeavyJobType,
        project_id: str | None,
        input_snapshot: Mapping[str, Any],
        *,
        idempotency_key: str,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        snapshot = dict(input_snapshot)
        job = self._new_job(
            job_type,
            project_id,
            snapshot,
            idempotency_key=idempotency_key,
            retry_of_job_id=retry_of_job_id,
        )
        return self.repository.create_heavy_job(
            job, queued_event_id=uuid4().hex
        )

    def enqueue_final_assembly(
        self,
        project_id: str,
        assembly_id: str,
        *,
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        runtime = FinalAssemblyRuntimeService(self.repository)
        job_id = uuid4().hex
        attempt = runtime.build_pending_attempt(
            project_id, assembly_id, heavy_job_id=job_id
        )
        assembly = self.repository.get_final_assembly(assembly_id)
        if assembly is None or assembly.project_id != project_id:
            raise HeavyJobServiceError("FinalAssembly 不属于该项目")
        manifest = runtime.manifest_service.get_manifest(project_id, assembly_id)
        project_root = (self.repository.paths.projects / project_id).resolve()
        source_size = 0
        required_paths: list[str] = []
        for item in manifest.items:
            relative = self._project_relative(item.source_path)
            required_paths.append(relative)
            source = project_root / Path(*PurePosixPath(relative).parts)
            if source.is_file():
                source_size += source.stat().st_size
        snapshot = {
            "schema_version": 1,
            "assembly_id": assembly_id,
            "attempt_id": attempt.id,
            "adapter_name": attempt.adapter_name,
            "output_profile_id": assembly.output_profile_id,
            "output_profile_hash": assembly.output_profile_hash,
            "required_project_paths": required_paths,
            "estimated_required_bytes": max(
                256 * 1024 * 1024, source_size * 4
            ),
            "cancel_supported": False,
        }
        job = self._new_job(
            HeavyJobType.FINAL_ASSEMBLY_RENDER,
            project_id,
            snapshot,
            job_id=job_id,
            idempotency_key=(
                idempotency_key or f"final-assembly:{assembly_id}:initial"
            ),
            retry_of_job_id=retry_of_job_id,
        )
        return self.repository.create_heavy_job(
            job,
            queued_event_id=uuid4().hex,
            final_attempt=attempt,
        )

    def enqueue_post_render(
        self,
        project_id: str,
        plan_id: str,
        *,
        subtitle_track_id: str | None = None,
        music_track_id: str | None = None,
        voice_track_id: str | None = None,
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        service = PostProductionService(self.repository)
        job_id = uuid4().hex
        attempt = service.build_pending_attempt(
            project_id, plan_id, heavy_job_id=job_id
        )
        plan = self.repository.get_post_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise HeavyJobServiceError("PostProductionPlan 不属于该项目")
        source_attempt = (
            self.repository.get_final_assembly_render_attempt(
                attempt.source_final_assembly_render_attempt_id
            )
            if attempt.source_final_assembly_render_attempt_id
            else None
        )
        source_relative = (
            str(source_attempt.output_relative_path)
            if source_attempt is not None and source_attempt.output_relative_path
            else ""
        )
        required_paths = [self._project_relative(source_relative)] if source_relative else []
        source_size = 0
        if source_relative:
            source = (
                self.repository.paths.projects
                / project_id
                / Path(*PurePosixPath(source_relative.replace("\\", "/")).parts)
            )
            if source.is_file():
                source_size = source.stat().st_size
        frozen_inputs = self.post_input_fingerprints(
            project_id,
            plan_id,
            subtitle_track_id=subtitle_track_id,
            music_track_id=music_track_id,
            voice_track_id=voice_track_id,
        )
        for path in frozen_inputs.get("required_paths", []):
            if path not in required_paths:
                required_paths.append(path)
        snapshot = {
            "schema_version": 1,
            "plan_id": plan_id,
            "attempt_id": attempt.id,
            "source_final_assembly_id": attempt.source_final_assembly_id,
            "source_final_assembly_render_attempt_id": (
                attempt.source_final_assembly_render_attempt_id
            ),
            "subtitle_track_id": subtitle_track_id,
            "music_track_id": music_track_id,
            "voice_track_id": voice_track_id,
            "adapter_name": attempt.adapter_name,
            "post_input_fingerprints": frozen_inputs,
            "required_project_paths": required_paths,
            "estimated_required_bytes": max(
                256 * 1024 * 1024, source_size * 3
            ),
            "cancel_supported": False,
        }
        snapshot_hash = _snapshot_sha256(snapshot)
        job = self._new_job(
            HeavyJobType.POST_RENDER,
            project_id,
            snapshot,
            job_id=job_id,
            idempotency_key=(
                idempotency_key or f"post-render:{plan_id}:{snapshot_hash}"
            ),
            retry_of_job_id=retry_of_job_id,
        )
        return self.repository.create_heavy_job(
            job,
            queued_event_id=uuid4().hex,
            post_attempt=attempt,
        )

    def enqueue_final_media_export(
        self,
        project_id: str,
        *,
        source_relative_path: str,
        source_sha256: str,
        source_size_bytes: int,
        destination: Path,
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        relative = self._project_relative(source_relative_path)
        if len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise HeavyJobServiceError("export source SHA256 无效")
        if int(source_size_bytes) <= 0:
            raise HeavyJobServiceError("export source size 无效")
        destination = Path(destination).expanduser()
        if not destination.is_absolute() or destination.suffix.lower() != ".mp4":
            raise HeavyJobServiceError("export destination 必须是绝对 MP4 路径")
        snapshot = {
            "schema_version": 1,
            "source_relative_path": relative,
            "source_sha256": source_sha256,
            "source_size_bytes": int(source_size_bytes),
            "destination": str(destination),
            "estimated_required_bytes": int(source_size_bytes),
            "required_project_paths": [relative],
            "cancel_supported": True,
        }
        fingerprint = _snapshot_sha256(snapshot)
        return self.enqueue(
            HeavyJobType.FINAL_MEDIA_EXPORT,
            project_id,
            snapshot,
            idempotency_key=(
                idempotency_key or f"final-media-export:{fingerprint}"
            ),
            retry_of_job_id=retry_of_job_id,
        )

    def enqueue_project_export(
        self,
        project_id: str,
        *,
        destination: Path,
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        if self.repository.get_project(project_id) is None:
            raise HeavyJobServiceError("项目不存在")
        destination = Path(destination).expanduser()
        if not destination.is_absolute() or destination.suffix.lower() != ".aidrama":
            raise HeavyJobServiceError("项目导出目标必须是绝对 .aidrama 路径")
        snapshot = {
            "schema_version": 1,
            "destination": str(destination),
            "estimated_required_bytes": max(
                64 * 1024 * 1024,
                sum(
                    path.stat().st_size
                    for path in self.repository.project_directory(project_id).rglob("*")
                    if path.is_file() and not path.is_symlink()
                ),
            ),
            "cancel_supported": False,
        }
        fingerprint = _snapshot_sha256(snapshot)
        return self.enqueue(
            HeavyJobType.PROJECT_EXPORT,
            project_id,
            snapshot,
            idempotency_key=idempotency_key or f"project-export:{fingerprint}",
            retry_of_job_id=retry_of_job_id,
        )

    def enqueue_project_import(
        self,
        *,
        staged_archive_relative_path: str,
        archive_sha256: str,
        archive_size_bytes: int,
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        relative = self._project_relative(staged_archive_relative_path)
        if len(archive_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in archive_sha256
        ):
            raise HeavyJobServiceError("项目导入 archive SHA256 无效")
        if int(archive_size_bytes) <= 0:
            raise HeavyJobServiceError("项目导入 archive size 无效")
        snapshot = {
            "schema_version": 1,
            "staged_archive_relative_path": relative,
            "archive_sha256": archive_sha256,
            "archive_size_bytes": int(archive_size_bytes),
            "estimated_required_bytes": int(archive_size_bytes) * 3,
            "cancel_supported": False,
        }
        fingerprint = _snapshot_sha256(snapshot)
        return self.enqueue(
            HeavyJobType.PROJECT_IMPORT,
            None,
            snapshot,
            idempotency_key=idempotency_key or f"project-import:{fingerprint}",
            retry_of_job_id=retry_of_job_id,
        )

    def enqueue_tts(
        self,
        project_id: str,
        *,
        plan_id: str,
        script_revision_id: str,
        subtitle_track_id: str,
        voice_assignments: Mapping[str, str] | None = None,
        default_voice: str = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female",
        idempotency_key: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> HeavyJob:
        track = self.repository.get_post_subtitle_track(subtitle_track_id)
        if (
            track is None
            or track.project_id != project_id
            or track.plan_id != plan_id
            or track.source_script_revision_id != script_revision_id
        ):
            raise HeavyJobServiceError("SubtitleTrack 不属于指定项目/后期计划/剧本")
        snapshot = {
            "schema_version": 1,
            "plan_id": plan_id,
            "script_revision_id": script_revision_id,
            "subtitle_track_id": subtitle_track_id,
            "cues": [cue.model_dump(mode="json") for cue in track.cues],
            "voice_assignments": {
                str(key): str(value)
                for key, value in (voice_assignments or {}).items()
            },
            "default_voice": str(default_voice),
            "cancel_supported": False,
        }
        content_fingerprint = _snapshot_sha256(snapshot)
        snapshot["track_id"] = hashlib.sha256(
            f"{project_id}:{content_fingerprint}".encode("utf-8")
        ).hexdigest()[:32]
        fingerprint = _snapshot_sha256(snapshot)
        return self.enqueue(
            HeavyJobType.TTS,
            project_id,
            snapshot,
            idempotency_key=idempotency_key or f"tts:{subtitle_track_id}:{fingerprint}",
            retry_of_job_id=retry_of_job_id,
        )

    def retry(self, job_id: str) -> HeavyJob:
        original = self.get(job_id)
        if original.status not in {
            HeavyJobStatus.FAILED,
            HeavyJobStatus.CANCELLED,
            HeavyJobStatus.INTERRUPTED,
        }:
            raise HeavyJobServiceError("只有失败、取消或中断的 HeavyJob 可以重试")
        key = f"retry:{original.id}:{uuid4().hex}"
        snapshot = original.input_snapshot
        if original.job_type is HeavyJobType.FINAL_ASSEMBLY_RENDER:
            return self.enqueue_final_assembly(
                str(original.project_id),
                str(snapshot.get("assembly_id") or ""),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        if original.job_type is HeavyJobType.POST_RENDER:
            return self.enqueue_post_render(
                str(original.project_id),
                str(snapshot.get("plan_id") or ""),
                subtitle_track_id=self._optional_id(snapshot, "subtitle_track_id"),
                music_track_id=self._optional_id(snapshot, "music_track_id"),
                voice_track_id=self._optional_id(snapshot, "voice_track_id"),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        if original.job_type is HeavyJobType.PROJECT_EXPORT:
            return self.enqueue_project_export(
                str(original.project_id),
                destination=Path(str(snapshot.get("destination") or "")),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        if original.job_type is HeavyJobType.PROJECT_IMPORT:
            return self.enqueue_project_import(
                staged_archive_relative_path=str(
                    snapshot.get("staged_archive_relative_path") or ""
                ),
                archive_sha256=str(snapshot.get("archive_sha256") or ""),
                archive_size_bytes=int(snapshot.get("archive_size_bytes") or 0),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        if original.job_type is HeavyJobType.TTS:
            # Keep the original frozen track identity on retry so a crash
            # after DB commit can reconcile rather than synthesize twice.
            return self.enqueue(
                HeavyJobType.TTS,
                original.project_id,
                dict(snapshot),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        clone = dict(snapshot)
        return self.enqueue(
            original.job_type,
            original.project_id,
            clone,
            idempotency_key=key,
            retry_of_job_id=original.id,
        )

    def request_cancel(self, project_id: str | None, job_id: str) -> HeavyJob:
        job = self.get(job_id)
        if job.project_id != project_id:
            raise HeavyJobServiceError("HeavyJob 不属于该项目")
        if job.status is HeavyJobStatus.RUNNING and not bool(
            job.input_snapshot.get("cancel_supported")
        ):
            raise HeavyJobServiceError("当前本地 runtime 不支持安全中途取消")
        updated = self.repository.request_heavy_job_cancel(
            job.id, event_id=uuid4().hex, created_at=_now()
        )
        if updated.status is HeavyJobStatus.QUEUED:
            return self.repository.finish_heavy_job(
                job.id,
                status=HeavyJobStatus.CANCELLED,
                stage="CANCELLED",
                event_id=uuid4().hex,
                finished_at=_now(),
                safe_error="任务在开始前由用户取消。",
            )
        return updated

    def get(self, job_id: str) -> HeavyJob:
        job = self.repository.get_heavy_job(job_id)
        if job is None:
            raise HeavyJobServiceError("HeavyJob 不存在")
        return job

    def list_jobs(
        self,
        project_id: str,
        *,
        job_type: HeavyJobType | None = None,
    ) -> list[HeavyJob]:
        if self.repository.get_project(project_id) is None:
            raise HeavyJobServiceError("项目不存在")
        return self.repository.list_heavy_jobs(project_id, job_type=job_type)

    def list_project_imports(self) -> list[HeavyJob]:
        return [
            job
            for job in self.repository.list_heavy_jobs(include_unscoped=True)
            if job.project_id is None and job.job_type is HeavyJobType.PROJECT_IMPORT
        ]

    def list_events(self, project_id: str | None, job_id: str) -> list[HeavyJobEvent]:
        job = self.get(job_id)
        if job.project_id != project_id:
            raise HeavyJobServiceError("HeavyJob 不属于该项目")
        return self.repository.list_heavy_job_events(job_id)

    def recover_interrupted(self) -> list[HeavyJob]:
        running = self.repository.list_heavy_jobs(
            status=HeavyJobStatus.RUNNING, include_unscoped=True
        )
        if not running:
            return []
        return self.repository.recover_interrupted_heavy_jobs(
            finished_at=_now(),
            event_ids={job.id: uuid4().hex for job in running},
        )

    def resume_interrupted(self, job_id: str) -> HeavyJob:
        """Create one deterministic local recovery job from frozen inputs."""

        original = self.get(job_id)
        if original.status is not HeavyJobStatus.INTERRUPTED:
            raise HeavyJobServiceError(
                "只有 INTERRUPTED HeavyJob 可以进行 crash recovery"
            )
        key = f"crash-recovery:{original.id}"
        existing = self.repository.get_heavy_job_by_idempotency(
            original.project_id, key
        )
        if existing is not None:
            return existing
        snapshot = original.input_snapshot
        if original.job_type is HeavyJobType.FINAL_ASSEMBLY_RENDER:
            return self.enqueue_final_assembly(
                str(original.project_id),
                str(snapshot.get("assembly_id") or ""),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        if original.job_type is HeavyJobType.POST_RENDER:
            return self.enqueue_post_render(
                str(original.project_id),
                str(snapshot.get("plan_id") or ""),
                subtitle_track_id=self._optional_id(
                    snapshot, "subtitle_track_id"
                ),
                music_track_id=self._optional_id(snapshot, "music_track_id"),
                voice_track_id=self._optional_id(snapshot, "voice_track_id"),
                idempotency_key=key,
                retry_of_job_id=original.id,
            )
        raise HeavyJobServiceError(
            "该 HeavyJob 可能包含外部副作用，不能自动 crash retry"
        )

    @staticmethod
    def _new_job(
        job_type: HeavyJobType,
        project_id: str | None,
        snapshot: Mapping[str, Any],
        *,
        idempotency_key: str,
        retry_of_job_id: str | None,
        job_id: str | None = None,
    ) -> HeavyJob:
        key = str(idempotency_key).strip()
        if not key:
            raise HeavyJobServiceError("HeavyJob idempotency key 不能为空")
        clean_snapshot = dict(snapshot)
        return HeavyJob(
            id=job_id or uuid4().hex,
            job_type=job_type,
            project_id=project_id,
            idempotency_key=key,
            input_snapshot=clean_snapshot,
            input_sha256=_snapshot_sha256(clean_snapshot),
            retry_of_job_id=retry_of_job_id,
            created_at=_now(),
        )

    @staticmethod
    def _project_relative(value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or PureWindowsPath(value).drive
        ):
            raise HeavyJobServiceError("项目文件必须使用相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise HeavyJobServiceError("项目文件路径不能越过项目目录")
        return PurePosixPath(*parts).as_posix()

    @staticmethod
    def _optional_id(snapshot: Mapping[str, Any], key: str) -> str | None:
        value = snapshot.get(key)
        return str(value) if value else None

    def post_input_fingerprints(
        self,
        project_id: str,
        plan_id: str,
        *,
        subtitle_track_id: str | None,
        music_track_id: str | None,
        voice_track_id: str | None,
    ) -> dict[str, Any]:
        plan = self.repository.get_post_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise HeavyJobServiceError("PostProductionPlan 不属于该项目")
        result: dict[str, Any] = {
            "audio_mix_sha256": _snapshot_sha256(
                plan.audio_mix.model_dump(mode="json")
            ),
            "required_paths": [],
        }
        for key, item_id, getter in (
            ("subtitle", subtitle_track_id, self.repository.get_post_subtitle_track),
            ("music", music_track_id, self.repository.get_post_music_track),
            ("voice", voice_track_id, self.repository.get_post_voice_track),
        ):
            if item_id is None:
                result[f"{key}_sha256"] = None
                continue
            item = getter(item_id)
            if (
                item is None
                or item.project_id != project_id
                or getattr(item, "plan_id", None) != plan_id
            ):
                raise HeavyJobServiceError(f"{key} track 不属于该项目/后期计划")
            payload = item.model_dump(mode="json")
            result[f"{key}_sha256"] = _snapshot_sha256(payload)
            path = getattr(item, "path", None)
            if path:
                result["required_paths"].append(self._project_relative(path))
        return result


class LocalResourcePreflight:
    """Conservative, truthful checks before expensive local work starts."""

    MIN_HEADROOM_BYTES = 64 * 1024 * 1024

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def validate(self, job: HeavyJob) -> dict[str, object]:
        project_root: Path | None = None
        if job.project_id is not None:
            project_root = (self.repository.paths.projects / job.project_id).resolve()
            project_root.mkdir(parents=True, exist_ok=True)
            for raw in job.input_snapshot.get("required_project_paths", []):
                relative = HeavyJobService._project_relative(str(raw))
                target = (project_root / Path(*PurePosixPath(relative).parts)).resolve()
                if project_root not in target.parents or not target.is_file():
                    raise HeavyJobServiceError("HeavyJob 所需项目源文件不存在")
        raw_destination = job.input_snapshot.get("destination")
        if raw_destination:
            destination = Path(str(raw_destination)).expanduser()
            disk_root = destination.parent
            if not destination.is_absolute() or not disk_root.is_dir():
                raise HeavyJobServiceError("HeavyJob 输出目标目录不存在")
        elif job.job_type is HeavyJobType.PROJECT_IMPORT:
            disk_root = self.repository.paths.projects
        elif project_root is not None:
            disk_root = project_root
        else:
            disk_root = self.repository.paths.root
        estimated = int(job.input_snapshot.get("estimated_required_bytes") or 0)
        free = int(shutil.disk_usage(disk_root).free)
        required = max(0, estimated) + self.MIN_HEADROOM_BYTES
        if free < required:
            raise HeavyJobServiceError("可用磁盘空间不足，无法安全开始本地重任务")
        return {
            "disk_checked": True,
            "free_bytes": free,
            "estimated_required_bytes": estimated,
            "hardware_acceleration": (
                "NOT_USED"
                if job.job_type
                in {
                    HeavyJobType.FINAL_MEDIA_EXPORT,
                    HeavyJobType.PROJECT_EXPORT,
                    HeavyJobType.PROJECT_IMPORT,
                }
                else "NOT_ASSERTED"
            ),
        }


__all__ = [
    "HeavyJobService",
    "HeavyJobServiceError",
    "LocalResourcePreflight",
]
