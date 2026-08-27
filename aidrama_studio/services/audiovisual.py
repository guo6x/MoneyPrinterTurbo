"""Offline-first dialogue, TTS, audio-timeline, and subtitle delivery pipeline.

The production seam is provider-neutral: every synthesis request is a frozen
Universal Runtime ``CapabilityRequest`` selected through a ``ModelManifest``.
V1 intentionally ships only a deterministic local fake runtime so tests and
desktop previews cannot cross a network or create paid provider work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from aidrama_studio.domain import (
    AudioTimeline,
    AudioTimelineItem,
    DialogueLine,
    DialoguePlan,
    FinalAssemblyRenderAttemptStatus,
    ScriptBeatType,
    ScriptRevisionStatus,
    SubtitleCue,
    SubtitleTrack,
    TTSTask,
    TTSTaskStatus,
    VoiceAssignment,
    VoiceAssignmentSet,
    VoiceTrack,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    InMemoryManifestRegistry,
    ModelManifest,
    ModelResolver,
    ProtocolFamily,
    RuntimeOutcome,
    validate_request_against_manifest,
)
from aidrama_studio.services.model_runtime.mainland_runtime import (
    ContentAddressedArtifactSink,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .postproduction import PostProductionService, PostProductionServiceError


AUDIO_DURATION_CONFLICT = "AUDIO_DURATION_CONFLICT"
FAKE_TTS_MANIFEST_ID = "local:aidrama:fake-tts-wav:v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class AudiovisualPipelineError(RuntimeError):
    pass


def fake_tts_manifest() -> ModelManifest:
    """Return the immutable, local-only Universal Runtime TTS manifest."""

    return ModelManifest(
        id=FAKE_TTS_MANIFEST_ID,
        display_name="AIDrama deterministic fake TTS WAV",
        provider_id="aidrama_fake_tts",
        capability=CapabilityKind.TTS,
        protocol=ProtocolFamily.REQUEST_RESPONSE,
        model_id="fake-sine-tts-v1",
        deployment_region="LOCAL",
        endpoint_class="LOCAL_PROCESS",
        endpoint_profile_id="local-fake-tts",
        codec_id="aidrama.fake.tts.wav.v1",
        input_modalities=("text",),
        output_modalities=("audio",),
        supported_modes=("speech_synthesis",),
        authorization={
            "create_is_paid": False,
            "requires_create_authorization": False,
        },
        readiness={
            "configured": True,
            "verified": True,
            "runtime_available": True,
            "create_authorized": True,
            "authorization_required": False,
        },
        selection_policy={"priority": 1, "profile": "OFFLINE_TEST"},
        parameter_schema={
            "audio_format": ["wav"],
            "sample_rate_min": 8000,
            "sample_rate_max": 192000,
        },
        pricing={"status": "FREE_LOCAL", "unit": "NONE"},
        metadata={
            "offline_only": True,
            "synthetic_audio": True,
            "real_provider_calls": 0,
            "paid_calls": 0,
        },
    )


class FakeTTSUniversalRuntime:
    """Deterministic local runtime returning real, parseable PCM WAV bytes."""

    def __init__(self, manifest: ModelManifest | None = None) -> None:
        self.manifest = manifest or fake_tts_manifest()
        self.registry = InMemoryManifestRegistry((self.manifest,))
        self.resolver = ModelResolver(self.registry)
        self.invocation_count = 0

    def selection(self):
        return self.resolver.resolve(
            capability=CapabilityKind.TTS,
            manifest_id=self.manifest.id,
            protocol=ProtocolFamily.REQUEST_RESPONSE,
            require_available=True,
        )

    def invoke(
        self,
        request: CapabilityRequest,
        *,
        artifact_sink: ContentAddressedArtifactSink,
    ) -> CapabilityResult:
        selection = self.selection()
        validate_request_against_manifest(
            request,
            selection.manifest,
            codec_id=self.manifest.codec_id,
        )
        text = str(request.prompt_or_text or "").strip()
        voice_profile = str(request.provider_parameters.get("voice", "")).strip()
        language = str(request.provider_parameters.get("language", "")).strip()
        audio_format = str(request.provider_parameters.get("audio_format", "wav"))
        sample_rate = request.provider_parameters.get("sample_rate", 48000)
        if not text or not voice_profile or not language:
            raise AudiovisualPipelineError("fake TTS request text/voice/language 不能为空")
        if audio_format.lower() != "wav":
            raise AudiovisualPipelineError("fake TTS runtime only supports WAV")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            raise AudiovisualPipelineError("fake TTS sample_rate 必须是整数")
        if sample_rate < 8000 or sample_rate > 192000:
            raise AudiovisualPipelineError("fake TTS sample_rate 超出范围")
        wav_bytes, duration = self._wav_bytes(
            text,
            voice_profile=voice_profile,
            sample_rate=sample_rate,
        )
        reference = artifact_sink.persist_bytes(
            wav_bytes,
            request_id=request.request_id,
            role="synthesized_dialogue",
            mime_type="audio/wav",
            safe_metadata={
                "provider": self.manifest.provider_id,
                "manifest_id": self.manifest.id,
                "synthetic": True,
            },
        )
        self.invocation_count += 1
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(reference,),
            usage={"characters": len(text), "paid_calls": 0},
            safe_metadata={
                "duration_seconds": duration,
                "sample_rate": sample_rate,
                "channels": 1,
                "sample_width_bytes": 2,
                "synthetic": True,
                "real_provider_calls": 0,
            },
        )

    @staticmethod
    def _wav_bytes(
        text: str,
        *,
        voice_profile: str,
        sample_rate: int,
    ) -> tuple[bytes, float]:
        # Duration and pitch are stable across runs but intentionally do not
        # approximate a real person.  The artifact is test media, not speech.
        duration = round(max(0.65, min(5.0, 0.35 + len(text) * 0.065)), 3)
        seed = int(hashlib.sha256(voice_profile.encode("utf-8")).hexdigest()[:8], 16)
        frequency = 180 + seed % 260
        frames = max(1, round(duration * sample_rate))
        fade_frames = max(1, min(frames // 4, round(sample_rate * 0.012)))
        samples = array("h")
        for index in range(frames):
            envelope = min(1.0, index / fade_frames, (frames - index) / fade_frames)
            value = int(6800 * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
            samples.append(value)
        if sys.byteorder != "little":
            samples.byteswap()
        buffer = BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(samples.tobytes())
        return buffer.getvalue(), frames / sample_rate


@dataclass(frozen=True, slots=True)
class AudiovisualDeliveryInputs:
    dialogue_plan: DialoguePlan
    voice_assignment_set: VoiceAssignmentSet
    tts_tasks: tuple[TTSTask, ...]
    audio_timeline: AudioTimeline
    subtitle_track: SubtitleTrack
    voice_track: VoiceTrack


class AudiovisualPipelineService:
    """Build formal delivery inputs without calling a real TTS provider."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        tts_runtime: FakeTTSUniversalRuntime | None = None,
        postproduction_service: PostProductionService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.tts_runtime = tts_runtime or FakeTTSUniversalRuntime()
        self.postproduction = postproduction_service or PostProductionService(
            repository=self.repository
        )

    # Dialogue and voices ----------------------------------------------
    def build_dialogue_plan(
        self,
        project_id: str,
        plan_id: str,
        *,
        language: str = "zh-CN",
        dialogue_plan_id: str | None = None,
    ) -> DialoguePlan:
        chain = self._chain(project_id, plan_id)
        script_revision = chain["script_revision"]
        shot_revision = chain["shot_revision"]
        if script_revision["status"] is not ScriptRevisionStatus.APPROVED:
            raise AudiovisualPipelineError("Structured Script revision 必须已批准")
        beat_to_shots: dict[str, list[str]] = {}
        for shot in sorted(shot_revision["content"].shots, key=lambda item: item.order):
            for beat_id in shot.source_script_beat_ids:
                beat_to_shots.setdefault(beat_id, []).append(shot.id)

        lines: list[DialogueLine] = []
        spoken_types = {
            ScriptBeatType.DIALOGUE,
            ScriptBeatType.NARRATION,
            ScriptBeatType.INNER_MONOLOGUE,
        }
        for scene in sorted(script_revision["content"].scenes, key=lambda item: item.order):
            for beat in sorted(scene.beats, key=lambda item: item.order):
                if beat.type not in spoken_types or not beat.text.strip():
                    continue
                shot_ids = beat_to_shots.get(beat.id, [])
                if not shot_ids:
                    raise AudiovisualPipelineError(
                        f"dialogue beat {beat.id} 缺少 Shot association"
                    )
                raw_id = f"line-{beat.id}"
                line_id = self._bounded_id(raw_id)
                lines.append(
                    DialogueLine(
                        id=line_id,
                        speaker=beat.character_id or "narrator",
                        text=beat.text.strip(),
                        language=language,
                        shot_id=shot_ids[0],
                        order=len(lines) + 1,
                        scene_id=scene.id,
                        beat_id=beat.id,
                    )
                )
        if not lines:
            raise AudiovisualPipelineError("Structured Script 没有可用对白或旁白")
        versions = self.repository.list_dialogue_plans(project_id, plan_id)
        payload = [item.model_dump(mode="json") for item in lines]
        plan = DialoguePlan(
            id=dialogue_plan_id or uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            source_script_revision_id=script_revision["id"],
            source_shot_plan_revision_id=shot_revision["id"],
            version=max((item.version for item in versions), default=0) + 1,
            lines=lines,
            lines_sha256=_canonical_sha256(payload),
            created_at=_now(),
        )
        return self.repository.create_dialogue_plan(plan)

    def assign_voices(
        self,
        project_id: str,
        plan_id: str,
        dialogue_plan_id: str,
        *,
        overrides: Mapping[str, str] | None = None,
        assignment_set_id: str | None = None,
    ) -> VoiceAssignmentSet:
        dialogue_plan = self._dialogue_plan(project_id, plan_id, dialogue_plan_id)
        requested = {str(key): str(value).strip() for key, value in (overrides or {}).items()}
        speakers = sorted({line.speaker for line in dialogue_plan.lines})
        assignments = [
            VoiceAssignment(
                speaker=speaker,
                voice_profile=requested.get(speaker) or self._fake_voice_profile(speaker),
            )
            for speaker in speakers
        ]
        if any(not item.voice_profile for item in assignments):
            raise AudiovisualPipelineError("每个 speaker 都必须分配 voice_profile")
        versions = self.repository.list_voice_assignment_sets(project_id, plan_id)
        version = max(
            (
                item.version
                for item in versions
                if item.source_dialogue_plan_id == dialogue_plan.id
            ),
            default=0,
        ) + 1
        payload = [item.model_dump(mode="json") for item in assignments]
        assignment_set = VoiceAssignmentSet(
            id=assignment_set_id or uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            source_dialogue_plan_id=dialogue_plan.id,
            version=version,
            assignments=assignments,
            assignments_sha256=_canonical_sha256(payload),
            created_at=_now(),
        )
        return self.repository.create_voice_assignment_set(assignment_set)

    # Universal TTS ----------------------------------------------------
    def plan_tts_tasks(
        self,
        project_id: str,
        plan_id: str,
        dialogue_plan_id: str,
        assignment_set_id: str,
        *,
        sample_rate: int = 48000,
    ) -> list[TTSTask]:
        dialogue_plan = self._dialogue_plan(project_id, plan_id, dialogue_plan_id)
        assignment_set = self._assignment_set(
            project_id, plan_id, dialogue_plan.id, assignment_set_id
        )
        assignments = {item.speaker: item.voice_profile for item in assignment_set.assignments}
        selection = self.tts_runtime.selection()
        existing = self.repository.list_tts_tasks(project_id, plan_id)
        tasks: list[TTSTask] = []
        for line in dialogue_plan.lines:
            voice_profile = assignments.get(line.speaker)
            if not voice_profile:
                raise AudiovisualPipelineError(
                    f"speaker {line.speaker} 缺少 voice assignment"
                )
            version = max(
                (
                    item.version
                    for item in existing
                    if item.source_dialogue_plan_id == dialogue_plan.id
                    and item.dialogue_line_id == line.id
                ),
                default=0,
            ) + 1
            task_id = uuid4().hex
            request = self._request_for(
                task_id,
                project_id=project_id,
                line=line,
                voice_profile=voice_profile,
                sample_rate=sample_rate,
                manifest=selection.manifest,
            )
            request_payload = request.to_dict()
            now = _now()
            task = TTSTask(
                id=task_id,
                project_id=project_id,
                plan_id=plan_id,
                source_dialogue_plan_id=dialogue_plan.id,
                source_voice_assignment_set_id=assignment_set.id,
                source_script_revision_id=dialogue_plan.source_script_revision_id,
                dialogue_line_id=line.id,
                shot_id=line.shot_id,
                version=version,
                text=line.text,
                voice_profile=voice_profile,
                language=line.language,
                sample_rate=sample_rate,
                manifest_id=selection.manifest_id,
                manifest_hash=selection.manifest_hash,
                request_sha256=_canonical_sha256(request_payload),
                metadata_json={
                    "request": request_payload,
                    "capability": CapabilityKind.TTS.value,
                    "protocol_family": ProtocolFamily.REQUEST_RESPONSE.value,
                    "source_revision": dialogue_plan.source_script_revision_id,
                    "shot_id": line.shot_id,
                },
                created_at=now,
                updated_at=now,
            )
            tasks.append(self.repository.create_tts_task(task))
        return tasks

    def synthesize_tts_tasks(
        self,
        project_id: str,
        plan_id: str,
        tasks: Sequence[TTSTask] | None = None,
    ) -> list[TTSTask]:
        planned = list(tasks or self.repository.list_tts_tasks(project_id, plan_id))
        if not planned:
            raise AudiovisualPipelineError("没有可执行的 versioned TTS tasks")
        project_root = self._project_root(project_id)
        sink = ContentAddressedArtifactSink(
            project_root / "post" / plan_id / "tts" / "artifacts"
        )
        completed: list[TTSTask] = []
        for task in planned:
            if task.project_id != project_id or task.plan_id != plan_id:
                raise AudiovisualPipelineError("TTSTask 不属于该项目/plan")
            if task.status is not TTSTaskStatus.PLANNED:
                raise AudiovisualPipelineError("仅 PLANNED TTSTask 可以执行")
            request = self._request_for_task(task)
            if _canonical_sha256(request.to_dict()) != task.request_sha256:
                raise AudiovisualPipelineError("TTSTask frozen request hash 不匹配")
            try:
                result = self.tts_runtime.invoke(request, artifact_sink=sink)
                if not result.succeeded or len(result.outputs) != 1:
                    raise AudiovisualPipelineError("fake Universal TTS 未返回唯一 artifact")
                reference = result.outputs[0]
                output = sink.path_for(reference)
                duration = self._validate_wav(output, expected_sample_rate=task.sample_rate)
                relative = output.relative_to(project_root).as_posix()
                updated = task.model_copy(
                    update={
                        "status": TTSTaskStatus.SUCCEEDED,
                        "output_relative_path": relative,
                        "output_sha256": reference.sha256,
                        "output_size_bytes": reference.size_bytes,
                        "duration_seconds": duration,
                        "metadata_json": {
                            **task.metadata_json,
                            "runtime_result": result.to_dict(),
                            "artifact_source_id": reference.source_id,
                            "mime_type": reference.mime_type,
                            "synthetic": True,
                            "real_provider_calls": 0,
                            "paid_calls": 0,
                        },
                        "updated_at": _now(),
                    }
                )
                completed.append(self.repository.update_tts_task(updated))
            except Exception as exc:
                failed = task.model_copy(
                    update={
                        "status": TTSTaskStatus.FAILED,
                        "metadata_json": {
                            **task.metadata_json,
                            "safe_error": str(exc)[:1000],
                            "real_provider_calls": 0,
                            "paid_calls": 0,
                        },
                        "updated_at": _now(),
                    }
                )
                self.repository.update_tts_task(failed)
                raise
        return completed

    # Timeline, subtitles, and formal voice artifact -------------------
    def build_audio_timeline(
        self,
        project_id: str,
        plan_id: str,
        dialogue_plan_id: str,
        assignment_set_id: str,
        *,
        silence_gap_seconds: float = 0.2,
        timeline_id: str | None = None,
    ) -> AudioTimeline:
        if not math.isfinite(silence_gap_seconds) or silence_gap_seconds < 0:
            raise AudiovisualPipelineError("silence gap 必须是非负有限数")
        dialogue_plan = self._dialogue_plan(project_id, plan_id, dialogue_plan_id)
        assignment_set = self._assignment_set(
            project_id, plan_id, dialogue_plan.id, assignment_set_id
        )
        all_tasks = self.repository.list_tts_tasks(project_id, plan_id)
        latest: dict[str, TTSTask] = {}
        for task in all_tasks:
            if (
                task.source_dialogue_plan_id == dialogue_plan.id
                and task.source_voice_assignment_set_id == assignment_set.id
                and (
                    task.dialogue_line_id not in latest
                    or task.version > latest[task.dialogue_line_id].version
                )
            ):
                latest[task.dialogue_line_id] = task
        if set(latest) != {line.id for line in dialogue_plan.lines}:
            raise AudiovisualPipelineError("每个 dialogue line 必须有唯一 latest TTS task")
        if any(task.status is not TTSTaskStatus.SUCCEEDED for task in latest.values()):
            raise AudiovisualPipelineError("所有 latest TTS tasks 必须成功后才能构建 timeline")
        sample_rates = {task.sample_rate for task in latest.values()}
        if len(sample_rates) != 1:
            raise AudiovisualPipelineError("AudioTimeline 的 TTS sample_rate 必须一致")
        sample_rate = next(iter(sample_rates))
        items: list[AudioTimelineItem] = []
        cursor = 0.0
        for line in dialogue_plan.lines:
            task = latest[line.id]
            gap = 0.0 if not items else float(silence_gap_seconds)
            start = cursor + gap
            duration = float(task.duration_seconds or 0)
            item = AudioTimelineItem(
                dialogue_line_id=line.id,
                tts_task_id=task.id,
                speaker=line.speaker,
                text=line.text,
                shot_id=line.shot_id,
                order=line.order,
                start_seconds=round(start, 6),
                duration_seconds=round(duration, 6),
                silence_gap_seconds=round(gap, 6),
            )
            items.append(item)
            cursor = item.end_seconds
        picture_duration = float(self._chain(project_id, plan_id)["picture_duration"])
        if cursor > picture_duration + 0.01:
            raise AudiovisualPipelineError(
                f"{AUDIO_DURATION_CONFLICT}: audio={cursor:.3f}s picture={picture_duration:.3f}s"
            )
        artifact = self._render_timeline_wav(
            project_id,
            plan_id,
            items,
            latest,
            sample_rate=sample_rate,
            duration_seconds=picture_duration,
        )
        versions = self.repository.list_audio_timelines(project_id, plan_id)
        item_payload = [item.model_dump(mode="json") for item in items]
        timeline_payload = {
            "source_dialogue_plan_id": dialogue_plan.id,
            "source_voice_assignment_set_id": assignment_set.id,
            "source_script_revision_id": dialogue_plan.source_script_revision_id,
            "sample_rate": sample_rate,
            "items": item_payload,
            "content_end_seconds": round(cursor, 6),
            "duration_seconds": round(picture_duration, 6),
        }
        timeline = AudioTimeline(
            id=timeline_id or uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            source_dialogue_plan_id=dialogue_plan.id,
            source_voice_assignment_set_id=assignment_set.id,
            source_script_revision_id=dialogue_plan.source_script_revision_id,
            version=max((item.version for item in versions), default=0) + 1,
            sample_rate=sample_rate,
            items=items,
            content_end_seconds=round(cursor, 6),
            duration_seconds=round(picture_duration, 6),
            artifact_relative_path=artifact.relative_to(self._project_root(project_id)).as_posix(),
            artifact_sha256=_file_sha256(artifact),
            artifact_size_bytes=artifact.stat().st_size,
            timeline_sha256=_canonical_sha256(timeline_payload),
            created_at=_now(),
        )
        return self.repository.create_audio_timeline(timeline)

    def build_subtitles_from_audio_timeline(
        self,
        project_id: str,
        plan_id: str,
        timeline_id: str,
        *,
        track_id: str | None = None,
    ) -> SubtitleTrack:
        timeline = self._audio_timeline(project_id, plan_id, timeline_id)
        dialogue = self._dialogue_plan(
            project_id, plan_id, timeline.source_dialogue_plan_id
        )
        line_by_id = {line.id: line for line in dialogue.lines}
        cues: list[SubtitleCue] = []
        for item in timeline.items:
            line = line_by_id.get(item.dialogue_line_id)
            if line is None:
                raise AudiovisualPipelineError("AudioTimeline 引用了未知 DialogueLine")
            cues.append(
                SubtitleCue(
                    id=self._bounded_id(f"cue-{line.id}"),
                    text=line.text,
                    start_seconds=item.start_seconds,
                    end_seconds=round(item.end_seconds, 6),
                    scene_id=line.scene_id,
                    shot_id=item.shot_id,
                    beat_id=line.beat_id,
                )
            )
        now = _now()
        track = SubtitleTrack(
            id=track_id or uuid4().hex,
            project_id=project_id,
            plan_id=plan_id,
            source_script_revision_id=timeline.source_script_revision_id,
            enabled=True,
            cues=cues,
            created_at=now,
            updated_at=now,
        )
        persisted = self.repository.create_post_subtitle_track(track)
        self.assert_subtitle_timing_matches_audio(timeline, persisted)
        self.postproduction.export_srt(project_id, persisted.id, plan_id=plan_id)
        return persisted

    def create_voice_track(
        self,
        project_id: str,
        plan_id: str,
        timeline_id: str,
        subtitle_track_id: str,
        *,
        voice_track_id: str | None = None,
    ) -> VoiceTrack:
        timeline = self._audio_timeline(project_id, plan_id, timeline_id)
        subtitle = self.repository.get_post_subtitle_track(subtitle_track_id)
        if (
            subtitle is None
            or subtitle.project_id != project_id
            or subtitle.plan_id != plan_id
            or subtitle.source_script_revision_id != timeline.source_script_revision_id
        ):
            raise AudiovisualPipelineError("SubtitleTrack 不属于 AudioTimeline provenance")
        self.assert_subtitle_timing_matches_audio(timeline, subtitle)
        assignment_set = self._assignment_set(
            project_id,
            plan_id,
            timeline.source_dialogue_plan_id,
            timeline.source_voice_assignment_set_id,
        )
        task_ids = [item.tts_task_id for item in timeline.items]
        return self.postproduction.add_voice_track(
            project_id,
            plan_id,
            path=timeline.artifact_relative_path,
            voice_assignments={
                item.speaker: item.voice_profile for item in assignment_set.assignments
            },
            metadata={
                "kind": "TTS_TIMELINE",
                "source_audio_timeline_id": timeline.id,
                "source_audio_timeline_sha256": timeline.timeline_sha256,
                "source_dialogue_plan_id": timeline.source_dialogue_plan_id,
                "source_voice_assignment_set_id": timeline.source_voice_assignment_set_id,
                "source_subtitle_track_id": subtitle.id,
                "source_subtitle_cues_sha256": self.postproduction._subtitle_cues_sha256(subtitle),
                "source_tts_task_ids": task_ids,
                "manifest_id": self.tts_runtime.manifest.id,
                "manifest_hash": self.tts_runtime.manifest.manifest_hash,
                "sample_rate": timeline.sample_rate,
                "duration_seconds": timeline.duration_seconds,
                "content_end_seconds": timeline.content_end_seconds,
                "synthetic": True,
                "real_provider_calls": 0,
                "paid_calls": 0,
            },
            track_id=voice_track_id,
        )

    def run_fake_pipeline(
        self,
        project_id: str,
        plan_id: str,
        *,
        language: str = "zh-CN",
        voice_overrides: Mapping[str, str] | None = None,
        sample_rate: int = 48000,
        silence_gap_seconds: float = 0.2,
    ) -> AudiovisualDeliveryInputs:
        dialogue = self.build_dialogue_plan(
            project_id, plan_id, language=language
        )
        assignments = self.assign_voices(
            project_id,
            plan_id,
            dialogue.id,
            overrides=voice_overrides,
        )
        planned = self.plan_tts_tasks(
            project_id,
            plan_id,
            dialogue.id,
            assignments.id,
            sample_rate=sample_rate,
        )
        tasks = self.synthesize_tts_tasks(project_id, plan_id, planned)
        timeline = self.build_audio_timeline(
            project_id,
            plan_id,
            dialogue.id,
            assignments.id,
            silence_gap_seconds=silence_gap_seconds,
        )
        subtitles = self.build_subtitles_from_audio_timeline(
            project_id, plan_id, timeline.id
        )
        voice_track = self.create_voice_track(
            project_id, plan_id, timeline.id, subtitles.id
        )
        return AudiovisualDeliveryInputs(
            dialogue_plan=dialogue,
            voice_assignment_set=assignments,
            tts_tasks=tuple(tasks),
            audio_timeline=timeline,
            subtitle_track=subtitles,
            voice_track=voice_track,
        )

    # Read/UI helpers --------------------------------------------------
    def list_dialogue_plans(self, project_id: str, plan_id: str) -> list[DialoguePlan]:
        return self.repository.list_dialogue_plans(project_id, plan_id)

    def list_voice_assignment_sets(
        self, project_id: str, plan_id: str
    ) -> list[VoiceAssignmentSet]:
        return self.repository.list_voice_assignment_sets(project_id, plan_id)

    def list_tts_tasks(self, project_id: str, plan_id: str) -> list[TTSTask]:
        return self.repository.list_tts_tasks(project_id, plan_id)

    def list_audio_timelines(self, project_id: str, plan_id: str) -> list[AudioTimeline]:
        return self.repository.list_audio_timelines(project_id, plan_id)

    @staticmethod
    def assert_subtitle_timing_matches_audio(
        timeline: AudioTimeline, subtitle: SubtitleTrack
    ) -> None:
        if len(timeline.items) != len(subtitle.cues):
            raise AudiovisualPipelineError("Subtitle cue count 与 AudioTimeline 不匹配")
        for item, cue in zip(timeline.items, subtitle.cues):
            if (
                cue.text != item.text
                or cue.shot_id != item.shot_id
                or abs(cue.start_seconds - item.start_seconds) > 0.001
                or abs(cue.end_seconds - item.end_seconds) > 0.001
            ):
                raise AudiovisualPipelineError(
                    "SUBTITLE_TIMING_MISMATCH: subtitle 必须来自 AudioTimeline"
                )

    # Internal validation ---------------------------------------------
    def _chain(self, project_id: str, plan_id: str) -> dict[str, object]:
        try:
            plan = self.postproduction.get_plan(project_id, plan_id)
        except PostProductionServiceError as exc:
            raise AudiovisualPipelineError(str(exc)) from exc
        assembly = self.repository.get_final_assembly(plan.source_final_assembly_id)
        job = (
            self.repository.get_production_job(assembly.production_job_id)
            if assembly is not None
            else None
        )
        shot_revision = (
            self.repository.get_shot_revision(job.shot_plan_revision_id)
            if job is not None
            else None
        )
        script_revision = (
            self.repository.get_script_revision(shot_revision["source_script_revision_id"])
            if shot_revision is not None
            else None
        )
        attempt = (
            self.repository.get_final_assembly_render_attempt(
                plan.source_final_assembly_render_attempt_id
            )
            if plan.source_final_assembly_render_attempt_id
            else None
        )
        if (
            assembly is None
            or assembly.project_id != project_id
            or job is None
            or job.project_id != project_id
            or shot_revision is None
            or shot_revision["project_id"] != project_id
            or script_revision is None
            or script_revision["project_id"] != project_id
            or attempt is None
            or attempt.final_assembly_id != assembly.id
            or attempt.status is not FinalAssemblyRenderAttemptStatus.SUCCEEDED
        ):
            raise AudiovisualPipelineError("PostProduction provenance chain 不完整")
        picture_duration = float(attempt.metadata_json.get("duration_seconds", 0) or 0)
        if picture_duration <= 0 or not math.isfinite(picture_duration):
            raise AudiovisualPipelineError("Picture Final 缺少有效 duration provenance")
        output = self.postproduction._resolve_final_source(project_id, plan)
        expected_sha = str(attempt.metadata_json.get("sha256") or "")
        if (
            output is None
            or not Path(output).is_file()
            or not expected_sha
            or _file_sha256(Path(output)) != expected_sha
        ):
            raise AudiovisualPipelineError("Picture Final artifact SHA256 校验失败")
        return {
            "plan": plan,
            "assembly": assembly,
            "job": job,
            "shot_revision": shot_revision,
            "script_revision": script_revision,
            "final_attempt": attempt,
            "picture_duration": picture_duration,
            "picture_path": Path(output),
        }

    def _dialogue_plan(
        self, project_id: str, plan_id: str, dialogue_plan_id: str
    ) -> DialoguePlan:
        plan = self.repository.get_dialogue_plan(dialogue_plan_id)
        if plan is None or plan.project_id != project_id or plan.plan_id != plan_id:
            raise AudiovisualPipelineError("DialoguePlan 不属于该项目/plan")
        if _canonical_sha256(
            [item.model_dump(mode="json") for item in plan.lines]
        ) != plan.lines_sha256:
            raise AudiovisualPipelineError("DialoguePlan content hash 不匹配")
        return plan

    def _assignment_set(
        self,
        project_id: str,
        plan_id: str,
        dialogue_plan_id: str,
        assignment_set_id: str,
    ) -> VoiceAssignmentSet:
        value = self.repository.get_voice_assignment_set(assignment_set_id)
        if (
            value is None
            or value.project_id != project_id
            or value.plan_id != plan_id
            or value.source_dialogue_plan_id != dialogue_plan_id
        ):
            raise AudiovisualPipelineError("VoiceAssignmentSet 不属于 DialoguePlan")
        if _canonical_sha256(
            [item.model_dump(mode="json") for item in value.assignments]
        ) != value.assignments_sha256:
            raise AudiovisualPipelineError("VoiceAssignmentSet content hash 不匹配")
        return value

    def _audio_timeline(
        self, project_id: str, plan_id: str, timeline_id: str
    ) -> AudioTimeline:
        timeline = self.repository.get_audio_timeline(timeline_id)
        if (
            timeline is None
            or timeline.project_id != project_id
            or timeline.plan_id != plan_id
        ):
            raise AudiovisualPipelineError("AudioTimeline 不属于该项目/plan")
        path = self._project_root(project_id) / Path(timeline.artifact_relative_path)
        if (
            not path.is_file()
            or path.stat().st_size != timeline.artifact_size_bytes
            or _file_sha256(path) != timeline.artifact_sha256
        ):
            raise AudiovisualPipelineError("AudioTimeline artifact SHA256 校验失败")
        payload = {
            "source_dialogue_plan_id": timeline.source_dialogue_plan_id,
            "source_voice_assignment_set_id": timeline.source_voice_assignment_set_id,
            "source_script_revision_id": timeline.source_script_revision_id,
            "sample_rate": timeline.sample_rate,
            "items": [item.model_dump(mode="json") for item in timeline.items],
            "content_end_seconds": timeline.content_end_seconds,
            "duration_seconds": timeline.duration_seconds,
        }
        if _canonical_sha256(payload) != timeline.timeline_sha256:
            raise AudiovisualPipelineError("AudioTimeline content hash 不匹配")
        return timeline

    def _project_root(self, project_id: str) -> Path:
        project = self.repository.get_project(project_id)
        if project is None:
            raise AudiovisualPipelineError("项目不存在")
        root = self.repository.project_directory(project_id).resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise AudiovisualPipelineError("project storage path escapes configured root")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _request_for(
        self,
        request_id: str,
        *,
        project_id: str,
        line: DialogueLine,
        voice_profile: str,
        sample_rate: int,
        manifest: ModelManifest,
    ) -> CapabilityRequest:
        return CapabilityRequest(
            request_id=request_id,
            project_id=project_id,
            execution_id=request_id,
            capability=CapabilityKind.TTS,
            protocol_family=ProtocolFamily.REQUEST_RESPONSE,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            codec_id=manifest.codec_id,
            prompt_or_text=line.text,
            structured_input={
                "dialogue_line_id": line.id,
                "shot_id": line.shot_id,
                "speaker": line.speaker,
            },
            provider_parameters={
                "voice": voice_profile,
                "language": line.language,
                "sample_rate": sample_rate,
                "audio_format": "wav",
            },
            create_authorized=True,
            authorization_required=False,
        )

    def _request_for_task(self, task: TTSTask) -> CapabilityRequest:
        manifest = self.tts_runtime.manifest
        if task.manifest_id != manifest.id or task.manifest_hash != manifest.manifest_hash:
            raise AudiovisualPipelineError("TTSTask manifest identity 不匹配")
        return CapabilityRequest(
            request_id=task.id,
            project_id=task.project_id,
            execution_id=task.id,
            capability=CapabilityKind.TTS,
            protocol_family=ProtocolFamily.REQUEST_RESPONSE,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            codec_id=manifest.codec_id,
            prompt_or_text=task.text,
            structured_input={
                "dialogue_line_id": task.dialogue_line_id,
                "shot_id": task.shot_id,
                "speaker": self._dialogue_line(task).speaker,
            },
            provider_parameters={
                "voice": task.voice_profile,
                "language": task.language,
                "sample_rate": task.sample_rate,
                "audio_format": "wav",
            },
            create_authorized=True,
            authorization_required=False,
        )

    def _dialogue_line(self, task: TTSTask) -> DialogueLine:
        plan = self.repository.get_dialogue_plan(task.source_dialogue_plan_id)
        if plan is None:
            raise AudiovisualPipelineError("TTSTask DialoguePlan 不存在")
        line = next((item for item in plan.lines if item.id == task.dialogue_line_id), None)
        if line is None:
            raise AudiovisualPipelineError("TTSTask DialogueLine 不存在")
        return line

    def _render_timeline_wav(
        self,
        project_id: str,
        plan_id: str,
        items: Sequence[AudioTimelineItem],
        tasks: Mapping[str, TTSTask],
        *,
        sample_rate: int,
        duration_seconds: float,
    ) -> Path:
        project_root = self._project_root(project_id)
        total_frames = max(1, round(duration_seconds * sample_rate))
        output_samples = array("h", [0]) * total_frames
        for item in items:
            task = tasks[item.dialogue_line_id]
            relative = task.output_relative_path
            if not relative:
                raise AudiovisualPipelineError("TTSTask WAV artifact path 缺失")
            source = project_root / Path(relative)
            if not source.is_file() or _file_sha256(source) != task.output_sha256:
                raise AudiovisualPipelineError("TTSTask WAV artifact SHA256 校验失败")
            with wave.open(str(source), "rb") as handle:
                if (
                    handle.getnchannels() != 1
                    or handle.getsampwidth() != 2
                    or handle.getframerate() != sample_rate
                ):
                    raise AudiovisualPipelineError("TTSTask WAV format 不一致")
                raw = handle.readframes(handle.getnframes())
            segment = array("h")
            segment.frombytes(raw)
            if sys.byteorder != "little":
                segment.byteswap()
            offset = round(item.start_seconds * sample_rate)
            if offset + len(segment) > total_frames:
                raise AudiovisualPipelineError(
                    f"{AUDIO_DURATION_CONFLICT}: segment exceeds Picture Final"
                )
            output_samples[offset : offset + len(segment)] = segment
        if sys.byteorder != "little":
            output_samples.byteswap()
        destination_root = project_root / "post" / plan_id / "audio"
        destination_root.mkdir(parents=True, exist_ok=True)
        temporary = destination_root / f".timeline-{uuid4().hex}.tmp.wav"
        try:
            with wave.open(str(temporary), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(output_samples.tobytes())
            self._validate_wav(
                temporary,
                expected_sample_rate=sample_rate,
                expected_duration=duration_seconds,
            )
            digest = _file_sha256(temporary)
            target = destination_root / f"{digest}.wav"
            if target.exists():
                temporary.unlink()
            else:
                os.replace(temporary, target)
            return target
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _validate_wav(
        path: Path,
        *,
        expected_sample_rate: int,
        expected_duration: float | None = None,
    ) -> float:
        if not path.is_file() or path.stat().st_size <= 44:
            raise AudiovisualPipelineError("WAV artifact 为空或 header 无效")
        try:
            with wave.open(str(path), "rb") as handle:
                if handle.getcomptype() != "NONE":
                    raise AudiovisualPipelineError("WAV artifact 必须是 PCM")
                if handle.getframerate() != expected_sample_rate:
                    raise AudiovisualPipelineError("WAV artifact sample_rate 不匹配")
                if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                    raise AudiovisualPipelineError("WAV artifact format 必须是 mono PCM16")
                duration = handle.getnframes() / handle.getframerate()
        except (EOFError, wave.Error) as exc:
            raise AudiovisualPipelineError("WAV artifact 无法解析") from exc
        if duration <= 0:
            raise AudiovisualPipelineError("WAV artifact duration 无效")
        if expected_duration is not None and abs(duration - expected_duration) > 0.01:
            raise AudiovisualPipelineError("WAV artifact duration 不匹配")
        return duration

    @staticmethod
    def _bounded_id(value: str) -> str:
        if len(value) <= 80:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"{value[:63]}-{digest}"[:80]

    @staticmethod
    def _fake_voice_profile(speaker: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", speaker.casefold()).strip("-") or "speaker"
        digest = hashlib.sha256(speaker.encode("utf-8")).hexdigest()[:8]
        return f"fake-{slug[:72]}-{digest}-v1"[:120]


__all__ = [
    "AUDIO_DURATION_CONFLICT",
    "FAKE_TTS_MANIFEST_ID",
    "AudiovisualDeliveryInputs",
    "AudiovisualPipelineError",
    "AudiovisualPipelineService",
    "FakeTTSUniversalRuntime",
    "fake_tts_manifest",
]
