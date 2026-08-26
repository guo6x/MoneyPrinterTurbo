"""Canonical TTS runtime and timeline provenance.

This service is deliberately independent from Streamlit.  It turns subtitle
cues into immutable project-local audio segments and a voice-track record.  A
provider can be unavailable without making the rest of a project unusable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from aidrama_studio.domain import SubtitleCue, VoiceTrack
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityUnavailable,
    TTSProvider,
    TTSResult,
    default_capability_registry,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .provider_profiles import ProviderDisclosure, ProviderProfileError, ProviderProfileService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class TTSRuntimeError(RuntimeError):
    pass


TTS_LIVE_SMOKE_TEXT = "AIDrama Studio TTS live smoke."


class TTSRuntimeService:
    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        provider: TTSProvider | None = None,
        ffmpeg_binary: str | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry or (
            CapabilityRegistry([provider]) if provider is not None else default_capability_registry()
        )
        self.provider_profiles = provider_profiles or ProviderProfileService(
            self.repository, registry=self.registry
        )
        self.provider = provider or self.registry.get("TTS")
        self.ffmpeg_binary = ffmpeg_binary

    def synthesize_live_smoke(
        self,
        project_id: str,
        *,
        text: str = TTS_LIVE_SMOKE_TEXT,
        voice: str | None = None,
        language: str = "zh-CN",
        sample_rate: int = 48000,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
    ) -> TTSResult:
        """Run one explicitly authorized TTS submission with retries disabled.

        This acceptance path intentionally does not build a multi-cue track.
        The selected provider must expose its own bounded smoke implementation;
        otherwise the method fails before any provider submission.
        """

        if self.repository.get_project(project_id) is None:
            raise TTSRuntimeError("项目不存在")
        bounded_text = str(text or "").strip()
        if not bounded_text or len(bounded_text) > 200:
            raise TTSRuntimeError("TTS live smoke 文本必须为 1 到 200 个字符")
        try:
            resolved = self.provider_profiles.resolve(
                project_id,
                CapabilityKind.TTS,
                require_available=True,
            )
            selected_provider = self.provider_profiles.provider_for_selection(resolved)
            self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.TTS,
                disclosure,
                transmitted_content_types=("TEXT_TIMELINE",),
            )
        except (ProviderProfileError, CapabilityUnavailable) as exc:
            raise TTSRuntimeError(
                "TTS live smoke Provider disclosure/selection 不可用；不会调用 Provider"
            ) from exc
        if not isinstance(selected_provider, TTSProvider):
            raise TTSRuntimeError("选中的 TTS provider 无效")
        # The bounded path is an acceptance-only paid boundary.  A concrete
        # provider may expose the method without enforcing its own flag, so
        # the canonical runtime also requires an explicit status signal before
        # invoking it.  Local engines are the only exception because they do
        # not submit a remote/paid request.
        try:
            status_metadata = dict(
                getattr(selected_provider.status, "metadata", {}) or {}
            )
        except Exception as exc:
            raise TTSRuntimeError(
                "TTS live smoke readiness check failed；不会调用 Provider"
            ) from exc
        deployment_region = str(
            status_metadata.get("deployment_region") or "UNSPECIFIED"
        ).upper()
        if (
            deployment_region != "LOCAL"
            and status_metadata.get("live_authorized") is not True
        ):
            raise TTSRuntimeError(
                "TTS live smoke requires AIDRAMA_ALLOW_PAID_LIVE_TESTS=1"
            )
        selected_voice = str(
            voice or getattr(selected_provider, "voice", "")
        ).strip()
        if not selected_voice:
            raise TTSRuntimeError("TTS live smoke voice 未选择")
        try:
            result = selected_provider.synthesize_live_smoke(
                bounded_text,
                voice=selected_voice,
                language=language,
                sample_rate=sample_rate,
            )
        except Exception as exc:
            if isinstance(exc, TTSRuntimeError):
                raise
            raise TTSRuntimeError("TTS live smoke synthesis failed") from exc
        if not isinstance(result, TTSResult) or not isinstance(
            result.audio, (bytes, bytearray)
        ) or not result.audio:
            raise TTSRuntimeError("TTS live smoke returned no audio")
        return result

    def synthesize_track(
        self,
        project_id: str,
        plan_id: str,
        cues: Sequence[SubtitleCue | Mapping[str, Any]],
        *,
        script_revision_id: str,
        subtitle_track_id: str | None = None,
        voice_assignments: Mapping[str, str] | None = None,
        default_voice: str = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female",
        track_id: str | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
    ) -> VoiceTrack:
        plan = self._require_plan(project_id, plan_id)
        self._require_script_chain(project_id, plan, script_revision_id)
        normalized = [cue if isinstance(cue, SubtitleCue) else SubtitleCue.model_validate(cue) for cue in cues]
        if not normalized:
            raise TTSRuntimeError("TTS timeline 不能为空")
        subtitle_track = self._resolve_subtitle_track(
            project_id,
            plan_id,
            script_revision_id,
            normalized,
            subtitle_track_id,
        )
        try:
            resolved = self.provider_profiles.resolve(
                project_id, CapabilityKind.TTS, require_available=True
            )
            selected_provider = self.provider_profiles.provider_for_selection(resolved)
            safe_disclosure = self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.TTS,
                disclosure,
                transmitted_content_types=("TEXT_TIMELINE",),
            )
        except (ProviderProfileError, CapabilityUnavailable) as exc:
            raise TTSRuntimeError(
                "Provider disclosure/selection 不可用；不会调用 TTS Provider"
            ) from exc
        if not isinstance(selected_provider, TTSProvider):
            raise TTSRuntimeError("选中的 TTS provider 无效")
        # The concrete provider is selected from the exact profile; the
        # constructor-injected provider is not a fallback once a selection is
        # unavailable.
        provider = selected_provider
        if any(
            normalized[index].end_seconds > normalized[index + 1].start_seconds
            for index in range(len(normalized) - 1)
        ):
            raise TTSRuntimeError("TTS timeline cue 不得重叠")
        assignments = {str(key): str(value) for key, value in (voice_assignments or {}).items()}
        root = self._project_root(project_id) / "post" / plan_id / "voice" / (track_id or uuid4().hex)
        root.mkdir(parents=True, exist_ok=False)
        segments: list[dict[str, Any]] = []
        try:
            for index, cue in enumerate(normalized):
                speaker = str(cue.beat_id or cue.shot_id or "narrator")
                voice = assignments.get(speaker, assignments.get("narrator", default_voice))
                result = provider.synthesize(cue.text, voice=voice)
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
            timeline_start = min(item["start_seconds"] for item in segments)
            timeline_end = max(item["end_seconds"] for item in segments)
            merged_duration = self._probe_audio_duration(merged)
            if abs(merged_duration - timeline_end) > max(0.15, timeline_end * 0.05):
                raise TTSRuntimeError("TTS timeline duration 与 SubtitleTrack 不匹配")
            metadata = {
                "kind": "TTS_TIMELINE",
                "script_revision_id": script_revision_id,
                "source_subtitle_track_id": subtitle_track.id,
                "source_subtitle_cues_sha256": self._cues_sha256(subtitle_track.cues),
                "segments": segments,
                "provider": str(getattr(provider, "provider_name", result.provider)),
                "provider_disclosure": safe_disclosure,
                "voice_assignments": assignments,
                "timeline_start_seconds": timeline_start,
                "timeline_end_seconds": timeline_end,
                "sha256": self._sha256(merged),
                "size_bytes": merged.stat().st_size,
                "duration_seconds": merged_duration,
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

    def _require_plan(self, project_id: str, plan_id: str):
        plan = self.repository.get_post_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise TTSRuntimeError("PostProductionPlan 不属于该项目")
        return plan

    def _require_script_chain(self, project_id: str, plan, script_revision_id: str) -> None:
        assembly = self.repository.get_final_assembly(plan.source_final_assembly_id)
        job = self.repository.get_production_job(assembly.production_job_id) if assembly else None
        shot_revision = self.repository.get_shot_revision(job.shot_plan_revision_id) if job else None
        if (
            assembly is None
            or assembly.project_id != project_id
            or job is None
            or job.project_id != project_id
            or shot_revision is None
            or shot_revision["project_id"] != project_id
            or shot_revision["source_script_revision_id"] != script_revision_id
        ):
            raise TTSRuntimeError("Structured Script revision 不属于当前 PostProductionPlan chain")

    def _resolve_subtitle_track(
        self,
        project_id: str,
        plan_id: str,
        script_revision_id: str,
        cues: Sequence[SubtitleCue],
        subtitle_track_id: str | None,
    ):
        if subtitle_track_id is not None:
            candidates = [self.repository.get_post_subtitle_track(subtitle_track_id)]
        else:
            expected_sha = self._cues_sha256(cues)
            candidates = [
                item
                for item in self.repository.list_post_subtitle_tracks(project_id, plan_id)
                if item.source_script_revision_id == script_revision_id
                and self._cues_sha256(item.cues) == expected_sha
            ]
        if len(candidates) != 1:
            raise TTSRuntimeError("必须指定唯一且冻结的 SubtitleTrack")
        track = candidates[0]
        if (
            track is None
            or track.project_id != project_id
            or track.plan_id != plan_id
            or track.source_script_revision_id != script_revision_id
        ):
            raise TTSRuntimeError("SubtitleTrack 不属于当前 PostProductionPlan chain")
        if self._cues_sha256(track.cues) != self._cues_sha256(cues):
            raise TTSRuntimeError("TTS cues 必须来自冻结的 SubtitleTrack")
        return track

    @staticmethod
    def _cues_sha256(cues: Sequence[SubtitleCue]) -> str:
        payload = [item.model_dump(mode="json") for item in cues]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

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
        if "mpeg" in result.mime_type or result.mime_type.endswith("mp3"):
            suffix = ".mp3"
        elif "wav" in result.mime_type:
            suffix = ".wav"
        else:
            suffix = ".audio"
        target = root / f"segment-{index:05d}{suffix}"
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(bytes(result.audio))
        if temporary.stat().st_size <= 0:
            temporary.unlink(missing_ok=True)
            raise TTSRuntimeError("TTS segment is empty")
        os.replace(temporary, target)
        return target

    def _merge_segments(self, root: Path, segments: list[dict[str, Any]]) -> Path:
        binary = self.ffmpeg_binary
        if not binary:
            from app.utils.utils import get_ffmpeg_binary
            binary = get_ffmpeg_binary()
        command = [binary, "-hide_banner", "-loglevel", "error", "-y"]
        for item in segments:
            segment_path = root / Path(item["relative_path"]).name
            command += ["-i", str(segment_path)]
        timeline_end = max(float(item["end_seconds"]) for item in segments)
        command += [
            "-f", "lavfi", "-t", f"{timeline_end:.6f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        filters: list[str] = []
        inputs = ["[base]"]
        filters.append(
            f"[{len(segments)}:a:0]atrim=duration={timeline_end:.6f}[base]"
        )
        for index, item in enumerate(segments):
            cue_duration = float(item["end_seconds"]) - float(item["start_seconds"])
            delay_ms = max(0, round(float(item["start_seconds"]) * 1000))
            filters.append(
                f"[{index}:a:0]apad,atrim=duration={cue_duration:.6f},"
                f"adelay={delay_ms}:all=1[segment{index}]"
            )
            inputs.append(f"[segment{index}]")
        filters.append(
            "".join(inputs)
            + f"amix=inputs={len(inputs)}:duration=longest:normalize=0,"
            + f"atrim=duration={timeline_end:.6f}[timeline]"
        )
        target = root / "voice-timeline.mp3"
        command += [
            "-filter_complex", ";".join(filters),
            "-map", "[timeline]", "-c:a", "libmp3lame", str(target),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSRuntimeError("FFmpeg TTS timeline 不可用") from exc
        if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            raise TTSRuntimeError("FFmpeg TTS timeline 合并失败")
        return target

    def _probe_audio_duration(self, path: Path) -> float:
        binary = self.ffmpeg_binary
        if not binary:
            from app.utils.utils import get_ffmpeg_binary
            binary = get_ffmpeg_binary()
        try:
            result = subprocess.run(
                [binary, "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSRuntimeError("FFmpeg TTS timeline probe 不可用") from exc
        text = f"{result.stderr or ''}\n{result.stdout or ''}"
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match is None or "Audio:" not in text:
            raise TTSRuntimeError("TTS timeline 缺少有效 audio stream/duration")
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if duration <= 0:
            raise TTSRuntimeError("TTS timeline duration 无效")
        return duration

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


__all__ = ["TTS_LIVE_SMOKE_TEXT", "TTSRuntimeError", "TTSRuntimeService"]
