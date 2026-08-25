"""Canonical TTS runtime and timeline provenance.

This service is deliberately independent from Streamlit.  It turns subtitle
cues into immutable project-local audio segments and a voice-track record.  A
provider can be unavailable without making the rest of a project unusable.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence
from uuid import uuid4

from aidrama_studio.domain import SubtitleCue, VoiceTrack
from aidrama_studio.services.ai_capabilities import CapabilityUnavailable, TTSProvider, TTSResult, default_capability_registry
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class TTSRuntimeError(RuntimeError):
    pass


class TTSRuntimeService:
    def __init__(self, repository: ProjectRepository | None = None, *, provider: TTSProvider | None = None, ffmpeg_binary: str | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.provider = provider or default_capability_registry().get("TTS")
        self.ffmpeg_binary = ffmpeg_binary

    def synthesize_track(
        self,
        project_id: str,
        plan_id: str,
        cues: Sequence[SubtitleCue | Mapping[str, Any]],
        *,
        script_revision_id: str,
        voice_assignments: Mapping[str, str] | None = None,
        default_voice: str = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female",
        track_id: str | None = None,
    ) -> VoiceTrack:
        self._require_plan(project_id, plan_id)
        if self.provider is None:
            raise TTSRuntimeError("没有可用 TTS provider")
        if not self.provider.status.available:
            raise TTSRuntimeError(self.provider.status.reason)
        normalized = [cue if isinstance(cue, SubtitleCue) else SubtitleCue.model_validate(cue) for cue in cues]
        if not normalized:
            raise TTSRuntimeError("TTS timeline 不能为空")
        assignments = {str(key): str(value) for key, value in (voice_assignments or {}).items()}
        root = self._project_root(project_id) / "post" / plan_id / "voice" / (track_id or uuid4().hex)
        root.mkdir(parents=True, exist_ok=False)
        segments: list[dict[str, Any]] = []
        try:
            for index, cue in enumerate(normalized):
                speaker = str(cue.beat_id or cue.shot_id or "narrator")
                voice = assignments.get(speaker, assignments.get("narrator", default_voice))
                result = self.provider.synthesize(cue.text, voice=voice)
                path = self._write_segment(root, index, result)
                segments.append({
                    "cue_id": cue.id,
                    "text": cue.text,
                    "start_seconds": cue.start_seconds,
                    "end_seconds": cue.end_seconds,
                    "scene_id": cue.scene_id,
                    "shot_id": cue.shot_id,
                    "beat_id": cue.beat_id,
                    "voice": voice,
                    "provider": result.provider,
                    "mime_type": result.mime_type,
                    "sha256": self._sha256(path),
                    "relative_path": path.relative_to(self._project_root(project_id)).as_posix(),
                })
            merged = self._merge_segments(root, segments)
            relative = merged.relative_to(self._project_root(project_id)).as_posix()
            metadata = {
                "kind": "TTS_TIMELINE",
                "script_revision_id": script_revision_id,
                "segments": segments,
                "provider": str(getattr(self.provider, "provider_name", result.provider)),
                "voice_assignments": assignments,
                "timeline_start_seconds": min(item["start_seconds"] for item in segments),
                "timeline_end_seconds": max(item["end_seconds"] for item in segments),
            }
            track = VoiceTrack(id=track_id or root.name, project_id=project_id, plan_id=plan_id, path=relative, voice_assignments=assignments, metadata_json=metadata, created_at=_now())
            return self.repository.create_post_voice_track(track)
        except Exception:
            # Segments are disposable until the DB row is committed.
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass
            try:
                root.rmdir()
            except OSError:
                pass
            raise

    def _require_plan(self, project_id: str, plan_id: str) -> None:
        plan = self.repository.get_post_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise TTSRuntimeError("PostProductionPlan 不属于该项目")

    def _project_root(self, project_id: str) -> Path:
        if self.repository.get_project(project_id) is None:
            raise TTSRuntimeError("项目不存在")
        root = self.repository.project_directory(project_id).resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise TTSRuntimeError("project storage path escapes configured root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _write_segment(root: Path, index: int, result: TTSResult) -> Path:
        if not isinstance(result.audio, (bytes, bytearray)) or not result.audio:
            raise TTSRuntimeError("TTS provider returned empty audio")
        suffix = ".mp3" if "mpeg" in result.mime_type or result.mime_type.endswith("mp3") else ".audio"
        target = root / f"segment-{index:05d}{suffix}"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(bytes(result.audio))
        if temporary.stat().st_size <= 0:
            temporary.unlink(missing_ok=True)
            raise TTSRuntimeError("TTS segment is empty")
        os.replace(temporary, target)
        return target

    def _merge_segments(self, root: Path, segments: list[dict[str, Any]]) -> Path:
        if len(segments) == 1:
            return root / Path(segments[0]["relative_path"]).name
        binary = self.ffmpeg_binary
        if not binary:
            from app.utils.utils import get_ffmpeg_binary
            binary = get_ffmpeg_binary()
        concat_file = root / "concat.txt"
        lines: list[str] = []
        for item in segments:
            segment_path = (root / Path(item["relative_path"]).name).as_posix().replace("'", "'\\''")
            lines.append(f"file '{segment_path}'")
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        target = root / "voice-timeline.mp3"
        command = [binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c:a", "libmp3lame", str(target)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSRuntimeError("FFmpeg TTS timeline 不可用") from exc
        if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            raise TTSRuntimeError("FFmpeg TTS timeline 合并失败")
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


__all__ = ["TTSRuntimeError", "TTSRuntimeService"]
