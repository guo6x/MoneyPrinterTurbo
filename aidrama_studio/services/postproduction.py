"""Project-scoped post-production MVP.

The service consumes an existing successful FinalAssembly output.  It never
mutates the immutable assembly or its manifest: every post render receives a
new append-only attempt and a unique project-relative output path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from uuid import uuid4

from aidrama_studio.domain import (
    AudioMixConfig,
    MusicTrack,
    PostProductionPlan,
    PostRenderAttempt,
    PostRenderAttemptStatus,
    FinalAssemblyRenderAttemptStatus,
    SubtitleCue,
    SubtitleTrack,
    VoiceTrack,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class PostProductionServiceError(RuntimeError):
    """Raised when post-production data or project boundaries are invalid."""


@dataclass(frozen=True, slots=True)
class PostRenderRequest:
    source_path: Path
    output_path: Path
    subtitle_path: Path | None = None
    voice_path: Path | None = None
    music_path: Path | None = None
    music_track: MusicTrack | None = None
    audio_mix: AudioMixConfig = field(default_factory=AudioMixConfig)


class PostProductionMediaAdapter:
    """Renderer seam.  Tests may inject a deterministic local adapter."""

    name = "post-media-adapter"

    def render(self, request: PostRenderRequest) -> dict[str, object]:
        raise NotImplementedError


class FFmpegPostProductionAdapter(PostProductionMediaAdapter):
    """Small FFmpeg adapter using the existing MPT binary resolver."""

    name = "ffmpeg-post-production"

    def __init__(self, *, ffmpeg_binary: str | None = None, timeout_seconds: int = 900) -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds

    def render(self, request: PostRenderRequest) -> dict[str, object]:
        if not request.source_path.is_file() or request.source_path.stat().st_size <= 0:
            raise PostProductionServiceError("FinalAssembly source 文件不存在或为空")
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [self.ffmpeg_binary or self._resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(request.source_path)]
        extra_inputs: list[Path] = []
        has_source_audio = self._has_audio_stream(request.source_path) if (request.voice_path is not None or request.music_path is not None) else False
        audio_source_index = 0
        if (request.voice_path is not None or request.music_path is not None) and not has_source_audio:
            # A FinalAssembly may legitimately be silent.  Mix against a
            # generated silent source rather than rejecting a BGM-only plan.
            command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            audio_source_index = 1
        if request.voice_path is not None:
            command += ["-i", str(request.voice_path)]
            extra_inputs.append(request.voice_path)
        if request.music_path is not None:
            command += ["-stream_loop", "-1" if request.music_track and request.music_track.loop else "0", "-i", str(request.music_path)]
            extra_inputs.append(request.music_path)

        filters: list[str] = []
        video_map = "0:v:0"
        if request.subtitle_path is not None:
            # FFmpeg's subtitles filter needs a POSIX-looking escaped path.
            subtitle = str(request.subtitle_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            filters.append(f"[0:v:0]subtitles='{subtitle}'[vout]")
            video_map = "[vout]"
        audio_map = "0:a:0?"
        if request.voice_path is not None or request.music_path is not None:
            audio_inputs = ["[src]"]
            labels = [f"[{audio_source_index}:a:0]volume={request.audio_mix.source_gain}[src]"]
            next_index = audio_source_index + 1
            if request.voice_path is not None:
                labels.append(f"[{next_index}:a:0]volume={request.audio_mix.voice_gain}[voice]")
                audio_inputs.append("[voice]")
                next_index += 1
            if request.music_path is not None:
                gain = request.audio_mix.music_gain * (request.music_track.gain if request.music_track else 1.0)
                labels.append(f"[{next_index}:a:0]volume={gain}[music]")
                audio_inputs.append("[music]")
            raw_mix = "".join(audio_inputs) + f"amix=inputs={len(audio_inputs)}:duration=first:dropout_transition=2[mix_raw]"
            labels.append(raw_mix)
            if request.audio_mix.normalize:
                # ``normalize`` is a real FFmpeg operation, not a persisted
                # no-op.  Keep it after amix so all source/voice/music inputs
                # receive the same loudness treatment.
                labels.append("[mix_raw]loudnorm=I=-16:TP=-1.5:LRA=11[mix]")
            else:
                labels.append("[mix_raw]anull[mix]")
            filters.extend(labels)
            audio_map = "[mix]"
        if filters:
            command += ["-filter_complex", ";".join(filters)]
            command += ["-map", video_map, "-map", audio_map]
            command += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", "-shortest"]
        else:
            command += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
        command.append(str(request.output_path))
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PostProductionServiceError(f"post render FFmpeg 执行失败: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()[-1000:]
            raise PostProductionServiceError(f"post render FFmpeg 失败: {detail}")
        if not request.output_path.is_file() or request.output_path.stat().st_size <= 0:
            raise PostProductionServiceError("post render 输出为空")
        return {"size_bytes": request.output_path.stat().st_size, "runtime": "ffmpeg", "inputs": len(extra_inputs) + 1}

    def probe_output(self, output_path: Path) -> dict[str, object]:
        """Reuse the existing media probe seam for post-output validation."""
        from aidrama_studio.services.adapters.final_assembly_runtime import MPTFinalAssemblyAdapter

        return MPTFinalAssemblyAdapter(ffmpeg_binary=self.ffmpeg_binary).probe_output(Path(output_path))

    @staticmethod
    def _resolve_ffmpeg() -> str:
        from app.utils.utils import get_ffmpeg_binary

        return get_ffmpeg_binary()

    def _has_audio_stream(self, source: Path) -> bool:
        """Probe only stream presence; decoding is left to the render call."""
        try:
            result = subprocess.run(
                [self.ffmpeg_binary or self._resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:a:0", "-t", "0.01", "-f", "null", "-"],
                capture_output=True, text=True, timeout=min(self.timeout_seconds, 30), check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


class PostProductionService:
    """Create tracks, export SRT, import BGM, and render post output."""

    SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

    def __init__(self, repository: ProjectRepository | None = None, *, media_adapter: PostProductionMediaAdapter | None = None, final_assembly_service: Any | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.media_adapter = media_adapter or FFmpegPostProductionAdapter()
        if final_assembly_service is None:
            from .final_assembly_runtime import FinalAssemblyRuntimeService

            final_assembly_service = FinalAssemblyRuntimeService(repository=self.repository)
        self.final_assembly_service = final_assembly_service

    # Plans -------------------------------------------------------------
    def create_plan(self, project_id: str, source_final_assembly_id: str, *, plan_id: str | None = None, subtitle_enabled: bool = True, audio_mix: AudioMixConfig | None = None) -> PostProductionPlan:
        self._require_project(project_id)
        if not isinstance(source_final_assembly_id, str) and hasattr(source_final_assembly_id, "id"):
            source_final_assembly_id = str(source_final_assembly_id.id)
        assembly = self.repository.get_final_assembly(source_final_assembly_id)
        if assembly is None or assembly.project_id != project_id:
            raise PostProductionServiceError("FinalAssembly 不属于该项目")
        pinned_attempt_id: str | None = None
        latest_attempt = getattr(self.final_assembly_service, "latest_successful_attempt", None)
        if callable(latest_attempt):
            attempt = latest_attempt(project_id, source_final_assembly_id)
            if attempt is not None:
                pinned_attempt_id = attempt.id
        now = _now()
        return self.repository.create_post_plan(PostProductionPlan(id=plan_id or uuid4().hex, project_id=project_id, source_final_assembly_id=source_final_assembly_id, source_final_assembly_render_attempt_id=pinned_attempt_id, subtitle_enabled=subtitle_enabled, audio_mix=audio_mix or AudioMixConfig(), created_at=now, updated_at=now))

    create_post_plan = create_plan
    create_post_production = create_plan

    def get_plan(self, project_id: str, plan_id: str) -> PostProductionPlan:
        self._require_project(project_id)
        plan = self.repository.get_post_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise PostProductionServiceError("PostProductionPlan 不属于该项目")
        return plan

    def list_plans(self, project_id: str) -> list[PostProductionPlan]:
        self._require_project(project_id)
        return self.repository.list_post_plans(project_id)

    def update_plan(self, project_id: str, plan_id: str, *, subtitle_enabled: bool | None = None, audio_mix: AudioMixConfig | None = None) -> PostProductionPlan:
        current = self.get_plan(project_id, plan_id)
        return self.repository.update_post_plan(current.model_copy(update={"subtitle_enabled": current.subtitle_enabled if subtitle_enabled is None else subtitle_enabled, "audio_mix": audio_mix or current.audio_mix, "updated_at": _now()}))

    # Subtitle timeline -------------------------------------------------
    def build_subtitle_timeline(self, project_id: str, script_revision_id: str, *, plan_id: str | None = None, track_id: str | None = None, enabled: bool = True, shot_plan_revision_id: str | None = None) -> SubtitleTrack:
        self._require_project(project_id)
        revision = self.repository.get_script_revision(script_revision_id)
        if revision is None or revision["project_id"] != project_id:
            raise PostProductionServiceError("Structured Script revision 不属于该项目")
        script = revision["content"]
        beat_to_shot: dict[str, str] = {}
        if shot_plan_revision_id is not None:
            shot_revision = self.repository.get_shot_revision(shot_plan_revision_id)
            if shot_revision is None or shot_revision["project_id"] != project_id:
                raise PostProductionServiceError("Shot Plan revision 不属于该项目")
            for shot in shot_revision["content"].shots:
                for beat_id in shot.source_script_beat_ids:
                    beat_to_shot[beat_id] = shot.id
        cues: list[SubtitleCue] = []
        timeline = 0.0
        for scene in sorted(script.scenes, key=lambda item: item.order):
            scene_start = timeline
            for beat in sorted(scene.beats, key=lambda item: item.order):
                duration = float(beat.estimated_duration_seconds or self._text_duration(beat.text))
                start, end = timeline, timeline + duration
                if beat.type.value in {"DIALOGUE", "NARRATION", "INNER_MONOLOGUE"} and beat.text.strip():
                    cues.append(SubtitleCue(id=f"cue-{beat.id}", text=beat.text.strip(), start_seconds=start, end_seconds=end, scene_id=scene.id, shot_id=beat_to_shot.get(beat.id), beat_id=beat.id))
                timeline = end
            if timeline - scene_start < scene.estimated_duration_seconds:
                timeline = scene_start + float(scene.estimated_duration_seconds)
        track = SubtitleTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, source_script_revision_id=script_revision_id, enabled=enabled, cues=cues, created_at=_now(), updated_at=_now())
        return self.repository.create_post_subtitle_track(track) if plan_id is not None else track

    extract_subtitle_timeline = build_subtitle_timeline
    generate_subtitles = build_subtitle_timeline
    build_subtitle_track = build_subtitle_timeline

    @staticmethod
    def subtitle_to_srt(track: SubtitleTrack) -> str:
        lines: list[str] = []
        for index, cue in enumerate(sorted(track.cues, key=lambda item: (item.start_seconds, item.id)), start=1):
            lines.extend([str(index), f"{PostProductionService._srt_time(cue.start_seconds)} --> {PostProductionService._srt_time(cue.end_seconds)}", cue.text.strip(), ""])
        return "\n".join(lines)

    to_srt = subtitle_to_srt

    def export_srt(self, project_id: str, track_id: str, *, plan_id: str | None = None) -> str:
        self._require_project(project_id)
        if not isinstance(track_id, str) and hasattr(track_id, "id"):
            track_id = str(track_id.id)
        track = self.repository.get_post_subtitle_track(track_id)
        if track is None or track.project_id != project_id or (plan_id is not None and track.plan_id != plan_id):
            raise PostProductionServiceError("SubtitleTrack 不属于该项目")
        target_root = self._project_root(project_id) / "post" / (plan_id or track.plan_id or "draft") / "subtitles"
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / f"{track.id}.srt"
        temporary = target.with_name(f".{track.id}.tmp")
        temporary.write_text(self.subtitle_to_srt(track), encoding="utf-8")
        os.replace(temporary, target)
        return self._relative_to_project(project_id, target)

    def update_subtitle_track(self, project_id: str, track_id: str, *, cues: list[SubtitleCue] | None = None, enabled: bool | None = None) -> SubtitleTrack:
        """Edit subtitle presentation while retaining script revision provenance."""
        self._require_project(project_id)
        track = self.repository.get_post_subtitle_track(track_id)
        if track is None or track.project_id != project_id:
            raise PostProductionServiceError("SubtitleTrack 不属于该项目")
        parsed = track.cues if cues is None else [cue if isinstance(cue, SubtitleCue) else SubtitleCue.model_validate(cue) for cue in cues]
        updated = track.model_copy(update={
            "cues": parsed,
            "enabled": track.enabled if enabled is None else bool(enabled),
            "updated_at": _now(),
        })
        return self.repository.update_post_subtitle_track(updated)

    def list_subtitle_tracks(self, project_id: str, plan_id: str | None = None) -> list[SubtitleTrack]:
        self._require_project(project_id)
        if plan_id is not None:
            self.get_plan(project_id, plan_id)
        return self.repository.list_post_subtitle_tracks(project_id, plan_id)

    export_subtitles = export_srt

    # Audio -------------------------------------------------------------
    def add_voice_track(self, project_id: str, plan_id: str, *, path: str | None = None, voice_assignments: Mapping[str, str] | None = None, metadata: Mapping[str, Any] | None = None, track_id: str | None = None) -> VoiceTrack:
        self.get_plan(project_id, plan_id)
        relative = self._validate_optional_audio_path(project_id, path)
        track = VoiceTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, path=relative, voice_assignments=dict(voice_assignments or {}), metadata_json=dict(metadata or {}), created_at=_now())
        return self.repository.create_post_voice_track(track)

    def list_voice_tracks(self, project_id: str, plan_id: str) -> list[VoiceTrack]:
        self.get_plan(project_id, plan_id)
        return self.repository.list_post_voice_tracks(project_id, plan_id)

    def import_bgm(self, project_id: str, plan_id: str, source_path: str | Path, *, filename: str | None = None) -> MusicTrack:
        self.get_plan(project_id, plan_id)
        source = Path(source_path).expanduser()
        if not source.is_file() or source.suffix.lower() not in self.SUPPORTED_AUDIO_EXTENSIONS:
            raise PostProductionServiceError("BGM 必须是存在且受支持的本地音频文件")
        root = self._project_root(project_id)
        destination_dir = root / "post" / plan_id / "audio"
        destination_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_filename(filename or source.name)
        target = destination_dir / f"{uuid4().hex[:16]}-{safe_name}"
        shutil.copyfile(source, target)
        return self.add_music_track(project_id, plan_id, self._relative_to_project(project_id, target))

    def import_bgm_bytes(self, project_id: str, plan_id: str, data: bytes, *, filename: str) -> MusicTrack:
        """Import an uploaded BGM buffer into project-isolated storage.

        The UI never passes an arbitrary filesystem path.  The generated
        destination is validated and the source bytes are written atomically.
        """
        self.get_plan(project_id, plan_id)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise PostProductionServiceError("BGM 文件为空")
        safe_name = self._safe_filename(filename)
        if Path(safe_name).suffix.lower() not in self.SUPPORTED_AUDIO_EXTENSIONS:
            raise PostProductionServiceError("BGM 格式不受支持")
        root = self._project_root(project_id)
        target = root / "post" / plan_id / "audio" / f"{uuid4().hex[:16]}-{safe_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_bytes(bytes(data))
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self.add_music_track(project_id, plan_id, self._relative_to_project(project_id, target))

    def add_music_track(self, project_id: str, plan_id: str, path: str, *, start_seconds: float = 0, end_seconds: float | None = None, gain: float = 1.0, loop: bool = False, fade_in_seconds: float = 0, fade_out_seconds: float = 0, metadata: Mapping[str, Any] | None = None, track_id: str | None = None) -> MusicTrack:
        self.get_plan(project_id, plan_id)
        relative = self._validate_audio_path(project_id, path)
        track = MusicTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, path=relative, start_seconds=start_seconds, end_seconds=end_seconds, gain=gain, loop=loop, fade_in_seconds=fade_in_seconds, fade_out_seconds=fade_out_seconds, metadata_json=dict(metadata or {}), created_at=_now())
        return self.repository.create_post_music_track(track)

    def list_music_tracks(self, project_id: str, plan_id: str) -> list[MusicTrack]:
        self.get_plan(project_id, plan_id)
        return self.repository.list_post_music_tracks(project_id, plan_id)

    def add_bgm(self, project_id: str, plan_id: str, path: str, **kwargs: Any) -> MusicTrack:
        """Compatibility helper: absolute user-selected paths are imported."""
        candidate = Path(path).expanduser() if isinstance(path, str) else Path("")
        if candidate.is_absolute():
            return self.import_bgm(project_id, plan_id, candidate, filename=kwargs.pop("filename", None))
        return self.add_music_track(project_id, plan_id, path, **kwargs)

    def configure_audio_mix(self, project_id: str, plan_id: str, config: AudioMixConfig) -> PostProductionPlan:
        return self.update_plan(project_id, plan_id, audio_mix=config)

    # Rendering ---------------------------------------------------------
    def render(self, project_id: str, plan_id: str, *, subtitle_track_id: str | None = None, music_track_id: str | None = None, voice_track_id: str | None = None) -> PostRenderAttempt:
        plan = self.get_plan(project_id, plan_id)
        plan = self._ensure_source_attempt_pinned(project_id, plan)
        attempt_number = max((item.attempt_number for item in self.repository.list_post_render_attempts(project_id, plan_id)), default=0) + 1
        attempt = self.repository.create_post_render_attempt(PostRenderAttempt(id=uuid4().hex, project_id=project_id, plan_id=plan_id, source_final_assembly_id=plan.source_final_assembly_id, source_final_assembly_render_attempt_id=plan.source_final_assembly_render_attempt_id, attempt_number=attempt_number, adapter_name=getattr(self.media_adapter, "name", self.media_adapter.__class__.__name__), created_at=_now()))
        temporary: Path | None = None
        try:
            source = self._resolve_final_source(project_id, plan)
            if source is None or not Path(source).is_file():
                raise PostProductionServiceError("FinalAssembly 成片输出不存在")
            source = Path(source).resolve()
            project_root = self._project_root(project_id)
            if project_root not in source.parents:
                raise PostProductionServiceError("FinalAssembly source 不属于该项目")
            output, relative = self._choose_output(project_id, plan_id, attempt.id)
            temporary = output.with_name(f".{attempt.id}.in-progress.mp4")
            subtitle_path = self._subtitle_path(project_id, plan, subtitle_track_id)
            music = self._music_track(project_id, plan_id, music_track_id)
            voice = self._voice_track(project_id, plan_id, voice_track_id)
            self._validate_supported_music_parameters(music)
            self.repository.update_post_render_attempt(attempt.id, status=PostRenderAttemptStatus.RUNNING, started_at=_now(), metadata_json={"project_id": project_id, "plan_id": plan_id, "source_final_assembly_id": plan.source_final_assembly_id})
            render_result = self.media_adapter.render(
                PostRenderRequest(
                    source_path=source,
                    output_path=temporary,
                    subtitle_path=subtitle_path,
                    voice_path=self._resolve_optional_path(project_id, voice.path if voice else None),
                    music_path=self._resolve_optional_path(project_id, music.path if music else None),
                    music_track=music,
                    audio_mix=plan.audio_mix,
                )
            )
            metadata = self._sanitize_metadata(dict(render_result), project_id)
            probe = self._validate_output(temporary, require_audio=bool(music or voice))
            metadata.update({"size_bytes": temporary.stat().st_size, "sha256": self._sha256(temporary), "source_final_assembly_id": plan.source_final_assembly_id})
            if plan.source_final_assembly_render_attempt_id:
                metadata["source_final_assembly_render_attempt_id"] = plan.source_final_assembly_render_attempt_id
            if probe:
                metadata["probe"] = probe
            os.rename(temporary, output)
            temporary = None
            return self.repository.update_post_render_attempt(attempt.id, status=PostRenderAttemptStatus.SUCCEEDED, output_relative_path=relative, metadata_json=metadata, finished_at=_now())
        except Exception as exc:
            if temporary is not None and temporary.exists():
                temporary.unlink()
            message = str(exc).replace("\\", "/").replace(project_id, "<project>")[:4000]
            self.repository.update_post_render_attempt(attempt.id, status=PostRenderAttemptStatus.FAILED, error_message=message, finished_at=_now())
            raise PostProductionServiceError(message) from exc

    render_post = render
    render_final_post = render
    render_post_production = render
    start_post_render = render
    run = render

    def retry(self, project_id: str, plan_id: str, **kwargs: Any) -> PostRenderAttempt:
        return self.render(project_id, plan_id, **kwargs)

    def list_render_attempts(self, project_id: str, plan_id: str) -> list[PostRenderAttempt]:
        self.get_plan(project_id, plan_id)
        return self.repository.list_post_render_attempts(project_id, plan_id)

    def latest_successful_attempt(self, project_id: str, plan_id: str) -> PostRenderAttempt | None:
        attempts = self.list_render_attempts(project_id, plan_id)
        return next((item for item in reversed(attempts) if item.status is PostRenderAttemptStatus.SUCCEEDED), None)

    def resolve_output_path(self, project_id: str, plan_id: str, attempt_id: str | None = None) -> Path | None:
        attempts = self.list_render_attempts(project_id, plan_id)
        attempt = self.repository.get_post_render_attempt(attempt_id) if attempt_id else next((item for item in reversed(attempts) if item.status is PostRenderAttemptStatus.SUCCEEDED), None)
        if attempt is not None and (attempt.project_id != project_id or attempt.plan_id != plan_id):
            raise PostProductionServiceError("PostRenderAttempt 不属于该项目或计划")
        if attempt is None or attempt.status is not PostRenderAttemptStatus.SUCCEEDED or not attempt.output_relative_path:
            return None
        return self._resolve_project_relative(project_id, attempt.output_relative_path, suffix=".mp4", must_exist=True)

    def _ensure_source_attempt_pinned(self, project_id: str, plan: PostProductionPlan) -> PostProductionPlan:
        if plan.source_final_assembly_render_attempt_id:
            attempt = self.repository.get_final_assembly_render_attempt(plan.source_final_assembly_render_attempt_id)
            if attempt is None or attempt.final_assembly_id != plan.source_final_assembly_id or attempt.status is not FinalAssemblyRenderAttemptStatus.SUCCEEDED:
                raise PostProductionServiceError("PostProductionPlan 的 source render attempt 不再可用")
            return plan
        latest = getattr(self.final_assembly_service, "latest_successful_attempt", None)
        candidate = latest(project_id, plan.source_final_assembly_id) if callable(latest) else None
        if candidate is None:
            # Compatibility for a pre-017 draft plan whose injected resolver
            # already owns a source path.  Once a real FinalAssembly attempt
            # exists, the plan is pinned before rendering and cannot drift.
            return plan
        return self.repository.pin_post_plan_source_attempt(project_id, plan.id, candidate.id)

    def _resolve_final_source(self, project_id: str, plan: PostProductionPlan) -> Path | None:
        resolver = self.final_assembly_service.resolve_output_path
        if plan.source_final_assembly_render_attempt_id:
            try:
                return resolver(project_id, plan.source_final_assembly_id, plan.source_final_assembly_render_attempt_id)
            except TypeError as exc:
                raise PostProductionServiceError("FinalAssembly resolver 不支持冻结的 render attempt") from exc
        return resolver(project_id, plan.source_final_assembly_id)

    # Validation helpers ------------------------------------------------
    def _require_project(self, project_id: str) -> None:
        if self.repository.get_project(project_id) is None:
            raise PostProductionServiceError(f"项目不存在: {project_id}")

    def _project_root(self, project_id: str) -> Path:
        self._require_project(project_id)
        root = (self.repository.paths.projects / project_id).resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise PostProductionServiceError("project storage path escapes configured root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve_project_relative(self, project_id: str, relative: str, *, suffix: str | None = None, must_exist: bool = False) -> Path:
        root = self._project_root(project_id)
        if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
            raise PostProductionServiceError("path 无效")
        normalized = relative.strip().replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(relative).drive:
            raise PostProductionServiceError("path 必须是项目相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise PostProductionServiceError("path 不能越过项目目录")
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise PostProductionServiceError("path 不属于该项目")
        if suffix and target.suffix.lower() != suffix.lower():
            raise PostProductionServiceError("path 扩展名不受支持")
        if must_exist and (not target.is_file() or target.stat().st_size <= 0):
            return None  # type: ignore[return-value]
        return target

    def _validate_audio_path(self, project_id: str, path: str) -> str:
        normalized = path.strip().replace("\\", "/") if isinstance(path, str) else ""
        target = self._resolve_project_relative(project_id, normalized)
        if target.suffix.lower() not in self.SUPPORTED_AUDIO_EXTENSIONS or not target.is_file() or target.stat().st_size <= 0:
            raise PostProductionServiceError("BGM/voice path 不存在或格式不受支持")
        return PurePosixPath(normalized).as_posix()

    def _validate_optional_audio_path(self, project_id: str, path: str | None) -> str | None:
        return self._validate_audio_path(project_id, path) if path else None

    def _resolve_optional_path(self, project_id: str, path: str | None) -> Path | None:
        return self._resolve_project_relative(project_id, path) if path else None

    def _subtitle_path(self, project_id: str, plan: PostProductionPlan, track_id: str | None) -> Path | None:
        if not plan.subtitle_enabled:
            return None
        track = self.repository.get_post_subtitle_track(track_id) if track_id else next(iter(reversed(self.repository.list_post_subtitle_tracks(project_id, plan.id))), None)
        if track is None or not track.enabled:
            return None
        relative = self.export_srt(project_id, track.id, plan_id=plan.id)
        return self._resolve_project_relative(project_id, relative, suffix=".srt", must_exist=True)

    def _music_track(self, project_id: str, plan_id: str, track_id: str | None) -> MusicTrack | None:
        track = self.repository.get_post_music_track(track_id) if track_id else next(iter(reversed(self.repository.list_post_music_tracks(project_id, plan_id))), None)
        if track is not None and (track.project_id != project_id or track.plan_id != plan_id):
            raise PostProductionServiceError("MusicTrack 不属于该项目")
        return track

    def _voice_track(self, project_id: str, plan_id: str, track_id: str | None) -> VoiceTrack | None:
        track = self.repository.get_post_voice_track(track_id) if track_id else next(iter(reversed(self.repository.list_post_voice_tracks(project_id, plan_id))), None)
        if track is not None and (track.project_id != project_id or track.plan_id != plan_id):
            raise PostProductionServiceError("VoiceTrack 不属于该项目")
        return track

    def _choose_output(self, project_id: str, plan_id: str, attempt_id: str) -> tuple[Path, str]:
        root = self._project_root(project_id)
        if not self._safe_component(plan_id) or not self._safe_component(attempt_id):
            raise PostProductionServiceError("post identity 无效")
        output = root / "post" / plan_id / "attempts" / attempt_id / "final.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        return output, PurePosixPath("post", plan_id, "attempts", attempt_id, "final.mp4").as_posix()

    def _validate_output(self, path: Path, *, require_audio: bool = False) -> dict[str, object]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise PostProductionServiceError("post output 为空")
        with path.open("rb") as handle:
            header = handle.read(128)
        if b"ftyp" not in header:
            raise PostProductionServiceError("post output 不是有效 MP4")
        probe_fn = getattr(self.media_adapter, "probe_output", None)
        if callable(probe_fn):
            try:
                probed = dict(probe_fn(path))
            except Exception as exc:
                raise PostProductionServiceError(f"post output probe 失败: {exc}") from exc
            if not probed.get("video_stream") or float(probed.get("duration_seconds", 0) or 0) <= 0:
                raise PostProductionServiceError("post output 缺少有效 video stream/duration")
            if not isinstance(probed.get("width"), int) or not isinstance(probed.get("height"), int) or probed["width"] <= 0 or probed["height"] <= 0:
                raise PostProductionServiceError("post output dimensions 无效")
            if require_audio and not probed.get("audio_stream"):
                raise PostProductionServiceError("post output 缺少预期 audio stream")
            return probed
        return {}

    @staticmethod
    def _validate_supported_music_parameters(music: MusicTrack | None) -> None:
        if music is None:
            return
        unsupported = []
        if music.start_seconds != 0:
            unsupported.append("start_seconds")
        if music.end_seconds is not None:
            unsupported.append("end_seconds")
        if music.fade_in_seconds != 0:
            unsupported.append("fade_in_seconds")
        if music.fade_out_seconds != 0:
            unsupported.append("fade_out_seconds")
        if unsupported:
            raise PostProductionServiceError("当前 FFmpeg seam 尚未应用 MusicTrack 参数: " + ", ".join(unsupported))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _sanitize_metadata(value: Any, project_id: str) -> Any:
        """Prevent absolute local paths from entering durable metadata."""
        if isinstance(value, Mapping):
            return {str(key): PostProductionService._sanitize_metadata(item, project_id) for key, item in value.items()}
        if isinstance(value, list):
            return [PostProductionService._sanitize_metadata(item, project_id) for item in value]
        if isinstance(value, str):
            normalized = value.replace("\\", "/")
            if project_id in normalized and (":" in normalized[:4] or normalized.startswith("/")):
                return "<project>"
            if PureWindowsPath(value).drive or normalized.startswith("/"):
                return "<local-path>"
        return value

    @staticmethod
    def _text_duration(text: str) -> float:
        return max(0.8, min(6.0, len(text.strip()) * 0.12))

    @staticmethod
    def _srt_time(seconds: float) -> str:
        millis = max(0, round(seconds * 1000))
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        secs, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    @staticmethod
    def _safe_component(value: str) -> bool:
        return bool(value and value not in {".", ".."} and "/" not in value and "\\" not in value and not PureWindowsPath(value).drive)

    @staticmethod
    def _safe_filename(value: str) -> str:
        text = Path(value).name.strip().replace("\x00", "")
        if not text or text in {".", ".."}:
            raise PostProductionServiceError("文件名无效")
        return "".join(character if character.isalnum() or character in "._-" else "_" for character in text)

    def _relative_to_project(self, project_id: str, path: Path) -> str:
        root = self._project_root(project_id)
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise PostProductionServiceError("path 不属于该项目") from exc


__all__ = [
    "FFmpegPostProductionAdapter",
    "PostProductionMediaAdapter",
    "PostProductionService",
    "PostProductionServiceError",
    "PostRenderService",
    "PostRenderRequest",
]

PostRenderService = PostProductionService
