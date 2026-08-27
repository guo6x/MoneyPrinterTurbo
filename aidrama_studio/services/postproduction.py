"""Project-scoped post-production MVP.

The service consumes an existing successful FinalAssembly output.  It never
mutates the immutable assembly or its manifest: every post render receives a
new append-only attempt and a unique project-relative output path.
"""

from __future__ import annotations

import hashlib
import json
import math
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

    def probe_output(self, output_path: Path) -> dict[str, object]:
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
        source_probe = self.probe_output(request.source_path)
        source_duration = float(source_probe.get("duration_seconds", 0) or 0)
        if source_duration <= 0:
            raise PostProductionServiceError("FinalAssembly source 缺少有效时长")
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
            duration_filter = f"apad,atrim=duration={source_duration:.6f}"
            labels = [f"[{audio_source_index}:a:0]volume={request.audio_mix.source_gain},{duration_filter}[src]"]
            next_index = audio_source_index + 1
            if request.voice_path is not None:
                labels.append(f"[{next_index}:a:0]volume={request.audio_mix.voice_gain},{duration_filter}[voice]")
                audio_inputs.append("[voice]")
                next_index += 1
            if request.music_path is not None:
                gain = request.audio_mix.music_gain * (request.music_track.gain if request.music_track else 1.0)
                labels.append(f"[{next_index}:a:0]volume={gain},{duration_filter}[music]")
                audio_inputs.append("[music]")
            raw_mix = "".join(audio_inputs) + f"amix=inputs={len(audio_inputs)}:duration=longest:dropout_transition=2[mix_raw]"
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
            command += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", "-t", f"{source_duration:.6f}"]
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
        if plan_id is not None:
            cues = self._build_final_subtitle_cues(
                project_id,
                plan_id,
                script_revision_id,
                script,
                requested_shot_plan_revision_id=shot_plan_revision_id,
            )
            track = SubtitleTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, source_script_revision_id=script_revision_id, enabled=enabled, cues=cues, created_at=_now(), updated_at=_now())
            return self.repository.create_post_subtitle_track(track)
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

    def _build_final_subtitle_cues(
        self,
        project_id: str,
        plan_id: str,
        script_revision_id: str,
        script,
        *,
        requested_shot_plan_revision_id: str | None,
    ) -> list[SubtitleCue]:
        """Map script cues onto the exact frozen FinalAssembly timeline."""

        plan = self.get_plan(project_id, plan_id)
        assembly = self.repository.get_final_assembly(plan.source_final_assembly_id)
        if assembly is None or assembly.project_id != project_id:
            raise PostProductionServiceError("PostProductionPlan 的 FinalAssembly 不属于该项目")
        job = self.repository.get_production_job(assembly.production_job_id)
        if job is None or job.project_id != project_id:
            raise PostProductionServiceError("FinalAssembly 的 ProductionJob 不属于该项目")
        if (
            requested_shot_plan_revision_id is not None
            and requested_shot_plan_revision_id != job.shot_plan_revision_id
        ):
            raise PostProductionServiceError("Shot Plan revision 不属于当前 FinalAssembly chain")
        shot_revision = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if shot_revision is None or shot_revision["project_id"] != project_id:
            raise PostProductionServiceError("FinalAssembly 的 Shot Plan revision 不可用")
        if shot_revision["source_script_revision_id"] != script_revision_id:
            raise PostProductionServiceError("Structured Script revision 不属于当前 FinalAssembly chain")

        script_beats: dict[str, tuple[Any, Any]] = {}
        for scene in script.scenes:
            for beat in scene.beats:
                if beat.id in script_beats:
                    raise PostProductionServiceError(
                        f"Structured Script 包含跨 Scene 重复 Beat ID: {beat.id}"
                    )
                script_beats[beat.id] = (scene, beat)
        shots = {shot.id: shot for shot in shot_revision["content"].shots}
        beat_segments: dict[str, list[tuple[float, float, str]]] = {}
        scene_segments: dict[str, list[tuple[float, float, str]]] = {}
        manifest_items = sorted(
            self.repository.list_final_assembly_items(assembly.id),
            key=lambda item: (item.order_index, item.id),
        )
        if not manifest_items:
            raise PostProductionServiceError("FinalAssembly timeline map 为空")
        if not plan.source_final_assembly_render_attempt_id:
            raise PostProductionServiceError("PostProductionPlan 尚未冻结实际 FinalAssembly render timeline")
        render_attempt = self.repository.get_final_assembly_render_attempt(
            plan.source_final_assembly_render_attempt_id
        )
        if (
            render_attempt is None
            or render_attempt.final_assembly_id != assembly.id
            or render_attempt.status is not FinalAssemblyRenderAttemptStatus.SUCCEEDED
        ):
            raise PostProductionServiceError("PostProductionPlan 的 FinalAssembly render attempt 不可用")
        source_trace = render_attempt.metadata_json.get("source_items")
        if not isinstance(source_trace, list) or len(source_trace) != len(manifest_items):
            raise PostProductionServiceError("FinalAssembly render attempt 缺少实际 source timeline")
        final_duration = float(render_attempt.metadata_json.get("duration_seconds", 0) or 0)
        if final_duration <= 0:
            raise PostProductionServiceError("FinalAssembly render attempt 缺少实际成片时长")
        try:
            output = self.final_assembly_service.resolve_output_path(
                project_id,
                assembly.id,
                render_attempt.id,
            )
        except TypeError as exc:
            raise PostProductionServiceError("FinalAssembly resolver 不支持冻结的 render attempt") from exc
        expected_output_sha = str(render_attempt.metadata_json.get("sha256") or "")
        if (
            output is None
            or not Path(output).is_file()
            or not expected_output_sha
            or self._sha256(Path(output)) != expected_output_sha
        ):
            raise PostProductionServiceError("FinalAssembly render output SHA256 校验失败")
        previous_end = 0.0
        for item, trace in zip(manifest_items, source_trace):
            if not isinstance(trace, Mapping):
                raise PostProductionServiceError("FinalAssembly source timeline entry 无效")
            if any(
                str(trace.get(key) or "") != str(getattr(item, key))
                for key in (
                    "production_shot_id",
                    "production_execution_id",
                    "production_artifact_id",
                )
            ):
                raise PostProductionServiceError("FinalAssembly source timeline provenance 不匹配")
            try:
                start = float(trace.get("timeline_start_seconds"))
                end = float(trace.get("timeline_end_seconds"))
            except (TypeError, ValueError):
                raise PostProductionServiceError("FinalAssembly timeline map 无效")
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                raise PostProductionServiceError("FinalAssembly timeline map 无效")
            if abs(start - previous_end) > 0.01 or end > final_duration + 0.35:
                raise PostProductionServiceError("FinalAssembly source timeline 与实际成片时长不匹配")
            previous_end = end
            production_shot = self.repository.get_production_shot(item.production_shot_id)
            if production_shot is None or production_shot.production_job_id != job.id:
                raise PostProductionServiceError("FinalAssembly item 不属于当前 ProductionJob")
            shot = shots.get(production_shot.shot_id)
            if shot is None:
                raise PostProductionServiceError("FinalAssembly item 无法映射到 Shot Plan")
            interval = (start, end, shot.id)
            scene_segments.setdefault(shot.scene_id, []).append(interval)
            sourced = [
                script_beats[beat_id][1]
                for beat_id in shot.source_script_beat_ids
                if beat_id in script_beats
            ]
            if not sourced:
                continue
            weights = [
                float(beat.estimated_duration_seconds or self._text_duration(beat.text))
                for beat in sourced
            ]
            total_weight = sum(weights) or float(len(weights))
            cursor = start
            duration = end - start
            for index, (beat, weight) in enumerate(zip(sourced, weights)):
                segment_end = (
                    end
                    if index == len(sourced) - 1
                    else cursor + duration * weight / total_weight
                )
                beat_segments.setdefault(beat.id, []).append(
                    (cursor, segment_end, shot.id)
                )
                cursor = segment_end

        if abs(previous_end - final_duration) > max(0.05, final_duration * 0.01):
            raise PostProductionServiceError("FinalAssembly source timeline 未覆盖实际成片时长")

        cues: list[SubtitleCue] = []
        for scene in sorted(script.scenes, key=lambda item: item.order):
            ordered_beats = sorted(scene.beats, key=lambda item: item.order)
            weights = [
                float(beat.estimated_duration_seconds or self._text_duration(beat.text))
                for beat in ordered_beats
            ]
            total_weight = sum(weights) or float(len(weights) or 1)
            scene_intervals = sorted(scene_segments.get(scene.id, []))
            scene_start = scene_intervals[0][0] if scene_intervals else None
            scene_end = scene_intervals[-1][1] if scene_intervals else None
            has_explicit_subtitle = any(
                beat_segments.get(beat.id)
                for beat in ordered_beats
                if beat.type.value in {"DIALOGUE", "NARRATION", "INNER_MONOLOGUE"}
            )
            elapsed_weight = 0.0
            for beat, weight in zip(ordered_beats, weights):
                if beat.type.value not in {"DIALOGUE", "NARRATION", "INNER_MONOLOGUE"} or not beat.text.strip():
                    elapsed_weight += weight
                    continue
                explicit = beat_segments.get(beat.id, [])
                if explicit:
                    ordered_segments = sorted(explicit)
                    groups: list[list[tuple[float, float, str]]] = []
                    for segment in ordered_segments:
                        if not groups or segment[0] > groups[-1][-1][1] + 0.01:
                            groups.append([segment])
                        else:
                            groups[-1].append(segment)
                else:
                    if has_explicit_subtitle:
                        raise PostProductionServiceError(
                            f"subtitle beat {beat.id} 缺少当前 Shot Plan source trace"
                        )
                    if scene_start is None or scene_end is None or scene_end <= scene_start:
                        raise PostProductionServiceError(
                            f"subtitle beat {beat.id} 无法映射到 FinalAssembly timeline"
                        )
                    scene_duration = scene_end - scene_start
                    start = scene_start + scene_duration * elapsed_weight / total_weight
                    end = scene_start + scene_duration * (elapsed_weight + weight) / total_weight
                    midpoint = (start + end) / 2
                    shot_id = next(
                        (
                            item[2]
                            for item in scene_intervals
                            if item[0] <= midpoint <= item[1]
                        ),
                        scene_intervals[0][2],
                    )
                    groups = [[(start, end, shot_id)]]
                for group_index, group in enumerate(groups, start=1):
                    start = group[0][0]
                    end = group[-1][1]
                    cues.append(
                        SubtitleCue(
                            id=self._subtitle_cue_id(beat.id, group_index, len(groups)),
                            text=beat.text.strip(),
                            start_seconds=round(start, 6),
                            end_seconds=round(end, 6),
                            scene_id=scene.id,
                            shot_id=group[0][2],
                            beat_id=beat.id,
                        )
                    )
                elapsed_weight += weight
        ordered_cues = sorted(cues, key=lambda item: (item.start_seconds, item.id))
        for previous, current in zip(ordered_cues, ordered_cues[1:]):
            if current.start_seconds < previous.end_seconds - 0.01:
                raise PostProductionServiceError("Final subtitle timeline 包含重叠 cue")
        if ordered_cues and ordered_cues[-1].end_seconds > final_duration + 0.01:
            raise PostProductionServiceError("Final subtitle timeline 超出实际成片时长")
        return ordered_cues

    @staticmethod
    def _subtitle_cue_id(beat_id: str, index: int, total: int) -> str:
        suffix = f"-{index}" if total > 1 else ""
        candidate = f"cue-{beat_id}{suffix}"
        if len(candidate) <= 80:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
        return f"cue-{beat_id[:55]}-{digest}{suffix}"[:80]

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
        safe_metadata = dict(metadata or {})
        if relative:
            physical = self._resolve_project_relative(project_id, relative, must_exist=True)
            safe_metadata.update({"sha256": self._sha256(physical), "size_bytes": physical.stat().st_size})
        track = VoiceTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, path=relative, voice_assignments=dict(voice_assignments or {}), metadata_json=safe_metadata, created_at=_now())
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
        physical = self._resolve_project_relative(project_id, relative, must_exist=True)
        safe_metadata = dict(metadata or {})
        safe_metadata.update({"sha256": self._sha256(physical), "size_bytes": physical.stat().st_size})
        track = MusicTrack(id=track_id or uuid4().hex, project_id=project_id, plan_id=plan_id, path=relative, start_seconds=start_seconds, end_seconds=end_seconds, gain=gain, loop=loop, fade_in_seconds=fade_in_seconds, fade_out_seconds=fade_out_seconds, metadata_json=safe_metadata, created_at=_now())
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
    def build_pending_attempt(
        self,
        project_id: str,
        plan_id: str,
        *,
        heavy_job_id: str | None = None,
    ) -> PostRenderAttempt:
        plan = self._ensure_source_attempt_pinned(
            project_id, self.get_plan(project_id, plan_id)
        )
        attempt_number = max(
            (
                item.attempt_number
                for item in self.repository.list_post_render_attempts(
                    project_id, plan_id
                )
            ),
            default=0,
        ) + 1
        return PostRenderAttempt(
            id=uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            source_final_assembly_id=plan.source_final_assembly_id,
            source_final_assembly_render_attempt_id=plan.source_final_assembly_render_attempt_id,
            attempt_number=attempt_number,
            adapter_name=getattr(
                self.media_adapter, "name", self.media_adapter.__class__.__name__
            ),
            heavy_job_id=heavy_job_id,
            created_at=_now(),
        )

    def render(self, project_id: str, plan_id: str, *, subtitle_track_id: str | None = None, music_track_id: str | None = None, voice_track_id: str | None = None, prepared_attempt_id: str | None = None) -> PostRenderAttempt:
        plan = self.get_plan(project_id, plan_id)
        plan = self._ensure_source_attempt_pinned(project_id, plan)
        if prepared_attempt_id is None:
            attempt = self.repository.create_post_render_attempt(
                self.build_pending_attempt(project_id, plan_id)
            )
        else:
            attempt = self.repository.get_post_render_attempt(prepared_attempt_id)
            if (
                attempt is None
                or attempt.project_id != project_id
                or attempt.plan_id != plan_id
                or attempt.source_final_assembly_id != plan.source_final_assembly_id
                or attempt.source_final_assembly_render_attempt_id
                != plan.source_final_assembly_render_attempt_id
                or attempt.status is not PostRenderAttemptStatus.PENDING
            ):
                raise PostProductionServiceError(
                    "prepared PostRenderAttempt 不存在、已运行或 provenance 已变化"
                )
            adapter_name = getattr(
                self.media_adapter, "name", self.media_adapter.__class__.__name__
            )
            if attempt.adapter_name != adapter_name:
                raise PostProductionServiceError(
                    "prepared Post adapter 与当前 runtime 不一致"
                )
        temporary: Path | None = None
        try:
            source = self._resolve_final_source(project_id, plan)
            if source is None or not Path(source).is_file():
                raise PostProductionServiceError("FinalAssembly 成片输出不存在")
            source = Path(source).resolve()
            project_root = self._project_root(project_id)
            if project_root not in source.parents:
                raise PostProductionServiceError("FinalAssembly source 不属于该项目")
            source_sha256 = self._sha256(source)
            source_attempt = (
                self.repository.get_final_assembly_render_attempt(
                    plan.source_final_assembly_render_attempt_id
                )
                if plan.source_final_assembly_render_attempt_id
                else None
            )
            expected_source_sha = str(
                (source_attempt.metadata_json.get("sha256") if source_attempt else "")
                or ""
            )
            if plan.source_final_assembly_render_attempt_id and not expected_source_sha:
                raise PostProductionServiceError("FinalAssembly source 缺少 SHA256 provenance")
            if expected_source_sha and source_sha256 != expected_source_sha:
                raise PostProductionServiceError("FinalAssembly source SHA256 校验失败")
            try:
                source_probe = dict(self.media_adapter.probe_output(source))
            except Exception as exc:
                raise PostProductionServiceError(f"FinalAssembly source probe 失败: {exc}") from exc
            source_duration = float(source_probe.get("duration_seconds", 0) or 0)
            if not source_probe.get("video_stream") or source_duration <= 0:
                raise PostProductionServiceError("FinalAssembly source 缺少有效 video stream/duration")
            output, relative = self._choose_output(project_id, plan_id, attempt.id)
            temporary = output.with_name(f".{attempt.id}.in-progress.mp4")
            subtitle_track = self._subtitle_track(project_id, plan, subtitle_track_id)
            subtitle_path = (
                self._resolve_project_relative(
                    project_id,
                    self.export_srt(project_id, subtitle_track.id, plan_id=plan.id),
                    suffix=".srt",
                    must_exist=True,
                )
                if subtitle_track is not None
                else None
            )
            music = self._music_track(project_id, plan_id, music_track_id)
            voice = self._voice_track(project_id, plan_id, voice_track_id)
            voice_path = self._resolve_optional_path(project_id, voice.path if voice else None)
            music_path = self._resolve_optional_path(project_id, music.path if music else None)
            if voice_path is not None:
                expected_voice_sha = str(voice.metadata_json.get("sha256") or "")
                if not expected_voice_sha or self._sha256(voice_path) != expected_voice_sha:
                    raise PostProductionServiceError("VoiceTrack SHA256 校验失败")
                if voice.metadata_json.get("kind") == "TTS_TIMELINE":
                    source_track_id = str(
                        voice.metadata_json.get("source_subtitle_track_id") or ""
                    )
                    source_track = self.repository.get_post_subtitle_track(source_track_id)
                    if (
                        source_track is None
                        or source_track.project_id != project_id
                        or source_track.plan_id != plan_id
                    ):
                        raise PostProductionServiceError("VoiceTrack 的 SubtitleTrack provenance 无效")
                    expected_cues_sha = str(
                        voice.metadata_json.get("source_subtitle_cues_sha256") or ""
                    )
                    if (
                        not expected_cues_sha
                        or self._subtitle_cues_sha256(source_track) != expected_cues_sha
                    ):
                        raise PostProductionServiceError("VoiceTrack 的 SubtitleTrack 已发生变化")
                    if subtitle_track is not None and source_track.id != subtitle_track.id:
                        raise PostProductionServiceError("VoiceTrack 与本次 SubtitleTrack 不匹配")
                    source_audio_timeline_id = str(
                        voice.metadata_json.get("source_audio_timeline_id") or ""
                    )
                    if source_audio_timeline_id:
                        audio_timeline = self.repository.get_audio_timeline(
                            source_audio_timeline_id
                        )
                        expected_timeline_sha = str(
                            voice.metadata_json.get("source_audio_timeline_sha256")
                            or ""
                        )
                        if (
                            audio_timeline is None
                            or audio_timeline.project_id != project_id
                            or audio_timeline.plan_id != plan_id
                            or audio_timeline.timeline_sha256
                            != expected_timeline_sha
                            or audio_timeline.artifact_relative_path != voice.path
                            or audio_timeline.artifact_sha256 != expected_voice_sha
                        ):
                            raise PostProductionServiceError(
                                "VoiceTrack 的 AudioTimeline provenance 无效"
                            )
            if music_path is not None:
                expected_music_sha = str(music.metadata_json.get("sha256") or "")
                if not expected_music_sha or self._sha256(music_path) != expected_music_sha:
                    raise PostProductionServiceError("MusicTrack SHA256 校验失败")
            input_fingerprints = {
                "source_final_assembly_render_attempt_id": plan.source_final_assembly_render_attempt_id,
                "source_sha256": source_sha256,
                "source_duration_seconds": source_duration,
                "subtitle_track_id": subtitle_track.id if subtitle_track else None,
                "subtitle_sha256": self._sha256(subtitle_path) if subtitle_path else None,
                "voice_track_id": voice.id if voice else None,
                "voice_sha256": self._sha256(voice_path) if voice_path else None,
                "voice_source_subtitle_track_id": (
                    voice.metadata_json.get("source_subtitle_track_id") if voice else None
                ),
                "music_track_id": music.id if music else None,
                "music_sha256": self._sha256(music_path) if music_path else None,
                "audio_mix": plan.audio_mix.model_dump(mode="json"),
            }
            self._validate_supported_music_parameters(music)
            self.repository.update_post_render_attempt(attempt.id, status=PostRenderAttemptStatus.RUNNING, started_at=_now(), metadata_json={"project_id": project_id, "plan_id": plan_id, "source_final_assembly_id": plan.source_final_assembly_id, "input_fingerprints": input_fingerprints})
            render_result = self.media_adapter.render(
                PostRenderRequest(
                    source_path=source,
                    output_path=temporary,
                    subtitle_path=subtitle_path,
                    voice_path=voice_path,
                    music_path=music_path,
                    music_track=music,
                    audio_mix=plan.audio_mix,
                )
            )
            metadata = self._sanitize_metadata(dict(render_result), project_id)
            probe = self._validate_output(
                temporary,
                require_audio=bool(music or voice),
                expected_duration=source_duration,
            )
            metadata.update(
                {
                    "artifact_role": "DELIVERY_FINAL",
                    "relationship": "PICTURE_FINAL_TO_POSTPRODUCTION_TO_DELIVERY_FINAL",
                    "postproduction_plan_id": plan.id,
                    "size_bytes": temporary.stat().st_size,
                    "sha256": self._sha256(temporary),
                    "source_final_assembly_id": plan.source_final_assembly_id,
                }
            )
            metadata["input_fingerprints"] = input_fingerprints
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

    def render_prepared(
        self,
        project_id: str,
        plan_id: str,
        attempt_id: str,
        *,
        subtitle_track_id: str | None = None,
        music_track_id: str | None = None,
        voice_track_id: str | None = None,
    ) -> PostRenderAttempt:
        return self.render(
            project_id,
            plan_id,
            subtitle_track_id=subtitle_track_id,
            music_track_id=music_track_id,
            voice_track_id=voice_track_id,
            prepared_attempt_id=attempt_id,
        )

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
        output = self._resolve_project_relative(project_id, attempt.output_relative_path, suffix=".mp4", must_exist=True)
        if output is None:
            return None
        expected_sha256 = str(attempt.metadata_json.get("sha256") or "")
        if not expected_sha256 or self._sha256(output) != expected_sha256:
            return None
        return output

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

    def _subtitle_track(self, project_id: str, plan: PostProductionPlan, track_id: str | None) -> SubtitleTrack | None:
        if not plan.subtitle_enabled:
            return None
        track = self.repository.get_post_subtitle_track(track_id) if track_id else next(iter(reversed(self.repository.list_post_subtitle_tracks(project_id, plan.id))), None)
        if track is None:
            return None
        if track.project_id != project_id or track.plan_id != plan.id:
            raise PostProductionServiceError("SubtitleTrack 不属于该项目或计划")
        return track if track.enabled else None

    def _subtitle_path(self, project_id: str, plan: PostProductionPlan, track_id: str | None) -> Path | None:
        track = self._subtitle_track(project_id, plan, track_id)
        if track is None:
            return None
        relative = self.export_srt(project_id, track.id, plan_id=plan.id)
        return self._resolve_project_relative(project_id, relative, suffix=".srt", must_exist=True)

    @staticmethod
    def _subtitle_cues_sha256(track: SubtitleTrack) -> str:
        payload = [item.model_dump(mode="json") for item in track.cues]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

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

    def _validate_output(self, path: Path, *, require_audio: bool = False, expected_duration: float | None = None) -> dict[str, object]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise PostProductionServiceError("post output 为空")
        with path.open("rb") as handle:
            header = handle.read(128)
        if b"ftyp" not in header:
            raise PostProductionServiceError("post output 不是有效 MP4")
        probe_fn = getattr(self.media_adapter, "probe_output", None)
        if not callable(probe_fn):
            raise PostProductionServiceError("post media adapter 未提供真实输出 probe")
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
        actual_duration = float(probed.get("duration_seconds", 0) or 0)
        if expected_duration and abs(actual_duration - expected_duration) > max(0.35, expected_duration * 0.12):
            raise PostProductionServiceError(
                f"post output duration 不匹配（expected={expected_duration:.3f}, actual={actual_duration:.3f}）"
            )
        return probed

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
