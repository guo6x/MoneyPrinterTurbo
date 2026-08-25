from __future__ import annotations

import io
from pathlib import Path
import wave

import pytest

from aidrama_studio.domain import SubtitleCue, SubtitleTrack
from aidrama_studio.services.ai_capabilities import CapabilityKind, CapabilityStatus, TTSProvider, TTSResult
from aidrama_studio.services import PostProductionService, TTSRuntimeService
from test.aidrama_studio.test_postproduction_service import _plan
from test.aidrama_studio.test_production_execution import context as _execution_context


class FakeTTS(TTSProvider):
    provider_name = "FAKE_TTS"

    def __init__(self):
        self.calls = 0

    @property
    def status(self):
        return CapabilityStatus(CapabilityKind.TTS, self.provider_name, True, "test")

    def synthesize(self, text: str, *, voice: str, language: str = "zh-CN", sample_rate: int = 48000):
        self.calls += 1
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"\x00\x00" * int(sample_rate * 0.25))
        return TTSResult(self.provider_name, buffer.getvalue(), "audio/wav", 0.25, {"voice": voice})


def test_tts_track_persists_cue_and_voice_provenance(tmp_path: Path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    repository, project, post_service, assembly = _plan((repository, project))
    plan = post_service.create_plan(project.id, assembly.id)
    cues = [
        SubtitleCue(id="cue-1", text="你好", start_seconds=1, end_seconds=1.5, shot_id="shot-1", beat_id="beat-1"),
        SubtitleCue(id="cue-2", text="世界", start_seconds=3, end_seconds=3.5, shot_id="shot-1", beat_id="beat-2"),
    ]
    subtitle_track = repository.create_post_subtitle_track(
        SubtitleTrack(
            id="subtitle-track",
            project_id=project.id,
            plan_id=plan.id,
            source_script_revision_id="script_001",
            cues=cues,
            created_at="now",
            updated_at="now",
        )
    )
    provider = FakeTTS()
    track = TTSRuntimeService(repository, provider=provider).synthesize_track(
        project.id,
        plan.id,
        cues,
        script_revision_id="script_001",
        voice_assignments={"beat-1": "voice-a"},
        track_id="tts-track",
    )
    assert track.path == "post/%s/voice/tts-track/voice-timeline.mp3" % plan.id
    assert track.metadata_json["kind"] == "TTS_TIMELINE"
    assert track.metadata_json["source_subtitle_track_id"] == subtitle_track.id
    assert track.metadata_json["segments"][0]["cue_id"] == "cue-1"
    assert track.metadata_json["segments"][0]["voice"] == "voice-a"
    assert track.metadata_json["timeline_start_seconds"] == 1
    assert track.metadata_json["timeline_end_seconds"] == 3.5
    assert abs(track.metadata_json["duration_seconds"] - 3.5) <= 0.15
    assert len(track.metadata_json["sha256"]) == 64
    assert provider.calls == 2
    assert (repository.paths.projects / project.id / track.path).is_file()


def test_tts_rejects_cues_not_from_plan_subtitle_track_before_provider_call(tmp_path: Path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    repository, project, post_service, assembly = _plan((repository, project))
    plan = post_service.create_plan(project.id, assembly.id)
    canonical = [SubtitleCue(id="cue-1", text="原文", start_seconds=0, end_seconds=1)]
    track = repository.create_post_subtitle_track(
        SubtitleTrack(
            id="subtitle-canonical",
            project_id=project.id,
            plan_id=plan.id,
            source_script_revision_id="script_001",
            cues=canonical,
            created_at="now",
            updated_at="now",
        )
    )
    provider = FakeTTS()

    with pytest.raises(Exception, match="冻结的 SubtitleTrack"):
        TTSRuntimeService(repository, provider=provider).synthesize_track(
            project.id,
            plan.id,
            [canonical[0].model_copy(update={"text": "被替换"})],
            script_revision_id="script_001",
            subtitle_track_id=track.id,
        )

    assert provider.calls == 0


def test_post_rejects_tts_voice_after_source_subtitle_changes(tmp_path: Path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    repository, project, post_service, assembly = _plan((repository, project))
    plan = post_service.create_plan(project.id, assembly.id)
    cues = [SubtitleCue(id="cue-1", text="原文", start_seconds=0, end_seconds=1)]
    subtitle_track = repository.create_post_subtitle_track(
        SubtitleTrack(
            id="subtitle-before-edit",
            project_id=project.id,
            plan_id=plan.id,
            source_script_revision_id="script_001",
            cues=cues,
            created_at="now",
            updated_at="now",
        )
    )
    voice = TTSRuntimeService(repository, provider=FakeTTS()).synthesize_track(
        project.id,
        plan.id,
        cues,
        script_revision_id="script_001",
        subtitle_track_id=subtitle_track.id,
        track_id="tts-before-edit",
    )
    post_service.update_subtitle_track(
        project.id,
        subtitle_track.id,
        cues=[cues[0].model_copy(update={"text": "新文"})],
    )

    with pytest.raises(Exception, match="SubtitleTrack 已发生变化"):
        post_service.render(
            project.id,
            plan.id,
            subtitle_track_id=subtitle_track.id,
            voice_track_id=voice.id,
        )
