from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from aidrama_studio.domain import SubtitleCue, SubtitleTrack
from aidrama_studio.services.ai_capabilities import (
    CapabilityRegistry,
    CapabilityKind,
    CapabilityStatus,
    CapabilityUnavailable,
    MPTTTSProvider,
    TTSProvider,
    TTSResult,
)
from aidrama_studio.services import TTSRuntimeService
from aidrama_studio.services import TTSRuntimeError
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


class BoundedFakeTTS(FakeTTS):
    provider_name = "BOUNDED_FAKE_TTS"

    def __init__(self):
        super().__init__()
        self.live_calls = 0

    @property
    def status(self):
        return CapabilityStatus(
            CapabilityKind.TTS,
            self.provider_name,
            True,
            "configured",
            {
                "model": "bounded-tts-v1",
                "deployment_region": "INTERNATIONAL",
                "endpoint_class": "BOUNDED_TEST_TTS",
                "endpoint_profile_id": (
                    "runtime:TTS:BOUNDED_FAKE_TTS:BOUNDED_TEST_TTS"
                ),
                "configured": True,
                "live_authorized": True,
                "verification_state": "NOT_VERIFIED",
            },
            configured=True,
        )

    def synthesize_live_smoke(
        self,
        text: str,
        *,
        voice: str,
        language: str = "zh-CN",
        sample_rate: int = 48000,
    ):
        self.live_calls += 1
        return TTSResult(
            self.provider_name,
            b"one-bounded-smoke",
            "audio/mpeg",
            0.25,
            {"voice": voice},
        )


AZURE_V2_VOICE = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female"


def _azure_provider(**overrides):
    options = {
        "enabled": True,
        "voice": AZURE_V2_VOICE,
        "azure_config": {
            "speech_key": "in-memory-test-key",
            "speech_region": "eastasia",
        },
        "azure_speech_available": True,
        "allow_paid_live_tests": True,
        "env": {},
    }
    options.update(overrides)
    return MPTTTSProvider(**options)


def test_azure_tts_readiness_requires_key_region_voice_runtime_and_authorization():
    missing_key = _azure_provider(
        azure_config={"speech_key": "", "speech_region": "eastasia"}
    ).status
    missing_region = _azure_provider(
        azure_config={"speech_key": "in-memory-test-key", "speech_region": ""}
    ).status
    missing_runtime = _azure_provider(azure_speech_available=False).status
    unauthorized = _azure_provider(allow_paid_live_tests=False).status
    ready = _azure_provider().status

    assert not missing_key.available and not missing_key.configured
    assert "credential" in missing_key.reason
    assert not missing_region.available and not missing_region.configured
    assert "region" in missing_region.reason
    assert not missing_runtime.available and not missing_runtime.configured
    assert "runtime" in missing_runtime.reason
    assert unauthorized.configured and unauthorized.available
    assert unauthorized.metadata["live_authorized"] is False
    assert ready.available and ready.configured
    assert ready.metadata["upstream_provider_id"] == "AZURE_SPEECH"
    assert ready.metadata["deployment_region"] == "INTERNATIONAL"
    assert ready.metadata["endpoint_class"] == "AZURE_SPEECH_PUBLIC"
    assert ready.metadata["credential_reference"] == "AZURE_SPEECH_KEY"
    assert ready.metadata["region_configured"] is True
    assert "in-memory-test-key" not in repr(ready)


def test_mpt_tts_live_smoke_passes_single_attempt_and_unauthorized_calls_zero(
    monkeypatch,
):
    calls = []

    def fake_tts(text, voice, rate, path, volume, **kwargs):
        calls.append(
            {
                "text": text,
                "voice": voice,
                "rate": rate,
                "volume": volume,
                **kwargs,
            }
        )
        Path(path).write_bytes(b"bounded-audio")
        return SimpleNamespace(audio_duration_seconds=0.5)

    monkeypatch.setattr("app.services.voice.tts", fake_tts)
    result = _azure_provider().synthesize_live_smoke(
        "smoke",
        voice=AZURE_V2_VOICE,
    )

    assert result.audio == b"bounded-audio"
    assert calls == [
        {
            "text": "smoke",
            "voice": AZURE_V2_VOICE,
            "rate": 1.0,
            "volume": 1.0,
            "max_attempts": 1,
        }
    ]

    calls.clear()
    with pytest.raises(CapabilityUnavailable, match="AIDRAMA_ALLOW_PAID"):
        _azure_provider(allow_paid_live_tests=False).synthesize_live_smoke(
            "smoke",
            voice=AZURE_V2_VOICE,
        )
    assert calls == []


def test_tts_runtime_live_smoke_freezes_profile_disclosure_and_calls_once(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    provider = BoundedFakeTTS()
    registry = CapabilityRegistry([provider])
    from aidrama_studio.services.provider_profiles import ProviderProfileService

    profiles = ProviderProfileService(repository, registry=registry)
    disclosure = profiles.create_disclosure(
        project.id,
        CapabilityKind.TTS,
        transmitted_content_types=("TEXT_TIMELINE",),
    )
    original_require = profiles.require_disclosure
    profiles.require_disclosure = Mock(wraps=original_require)
    runtime = TTSRuntimeService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    result = runtime.synthesize_live_smoke(
        project.id,
        text="offline bounded smoke",
        voice="voice-a",
        disclosure=disclosure,
    )

    assert result.audio == b"one-bounded-smoke"
    assert provider.live_calls == 1
    assert provider.calls == 0
    profiles.require_disclosure.assert_called_once()
    assert profiles.require_disclosure.call_args.kwargs[
        "transmitted_content_types"
    ] == ("TEXT_TIMELINE",)


def test_tts_live_smoke_fails_closed_when_remote_authorization_is_missing(
    tmp_path,
):
    repository, project = _execution_context.__wrapped__(tmp_path)

    class UnauthorisedTTS(BoundedFakeTTS):
        @property
        def status(self):
            status = super().status
            metadata = dict(status.metadata)
            metadata.pop("live_authorized", None)
            return CapabilityStatus(
                status.capability,
                status.provider,
                status.available,
                status.reason,
                metadata,
                configured=status.configured,
            )

    provider = UnauthorisedTTS()
    registry = CapabilityRegistry([provider])
    from aidrama_studio.services.provider_profiles import ProviderProfileService

    profiles = ProviderProfileService(repository, registry=registry)
    disclosure = profiles.create_disclosure(
        project.id,
        CapabilityKind.TTS,
        transmitted_content_types=("TEXT_TIMELINE",),
    )
    runtime = TTSRuntimeService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    with pytest.raises(TTSRuntimeError, match="AIDRAMA_ALLOW_PAID_LIVE_TESTS"):
        # The provider's bounded method must never be reached.  The public
        # runtime error wraps the gate, while the assertion below proves zero
        # paid submissions.
        runtime.synthesize_live_smoke(
            project.id,
            text="offline bounded smoke",
            voice="voice-a",
            disclosure=disclosure,
        )

    assert provider.live_calls == 0


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
