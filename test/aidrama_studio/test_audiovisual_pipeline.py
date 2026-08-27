from __future__ import annotations

import hashlib
import inspect
import re
import shutil
import socket
import subprocess
import wave
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AspectRatio,
    Character,
    FinalAssembly,
    FinalAssemblyRenderAttempt,
    FinalAssemblyRenderAttemptStatus,
    FinalAssemblyStatus,
    Location,
    ProductionJob,
    ProductionJobStatus,
    ProductionShot,
    ProductionShotStatus,
    Project,
    ProjectStatus,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    TTSTaskStatus,
    World,
)
from aidrama_studio.pages.postproduction import _postproduction_state_snapshot
from aidrama_studio.services import (
    AUDIO_DURATION_CONFLICT,
    AudiovisualPipelineError,
    AudiovisualPipelineService,
    FFmpegPostProductionAdapter,
    FakeTTSUniversalRuntime,
    PostProductionService,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    ProtocolFamily,
)
from aidrama_studio.services.model_runtime.mainland_runtime import (
    ContentAddressedArtifactSink,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


NOW = "2026-08-28T00:00:00.000000+00:00"


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


def _story() -> StoryBible:
    return StoryBible(
        title="Audiovisual fixture",
        logline="Three speakers cross a synthetic sixty second timeline.",
        premise="Offline delivery verification.",
        genre="Test",
        tone="Neutral",
        world=World(setting="Synthetic stage"),
        characters=[
            Character(id="speaker_a", name="Speaker A"),
            Character(id="speaker_b", name="Speaker B"),
        ],
        locations=[Location(id="stage", name="Stage")],
        story_beats=[
            StoryBeat(
                id=f"story_{index}",
                order=index,
                type="DEVELOPMENT",
                summary=f"Beat {index}",
                characters=["speaker_a", "speaker_b"],
                location_id="stage",
            )
            for index in range(1, 4)
        ],
    )


def _script() -> StructuredScript:
    return StructuredScript(
        title="Three-line audiovisual fixture",
        scenes=[
            Scene(
                id=f"scene_{index}",
                order=index,
                title=f"Scene {index}",
                location_id="stage",
                character_ids=["speaker_a", "speaker_b"],
                estimated_duration_seconds=20,
                beats=[
                    ScriptBeat(
                        id=f"beat_{index}",
                        order=1,
                        type=(
                            ScriptBeatType.NARRATION
                            if index == 2
                            else ScriptBeatType.DIALOGUE
                        ),
                        character_id=(
                            None
                            if index == 2
                            else ("speaker_a" if index == 1 else "speaker_b")
                        ),
                        text=(
                            "第一位角色开始说话。"
                            if index == 1
                            else "旁白确认画面继续向前。"
                            if index == 2
                            else "第二位角色完成最后一句。"
                        ),
                        estimated_duration_seconds=2,
                    )
                ],
                source_story_beat_ids=[f"story_{index}"],
            )
            for index in range(1, 4)
        ],
    )


def _shot_plan() -> ShotPlan:
    return ShotPlan(
        title="Three synthetic shots",
        source_script_revision_id="script-av",
        shots=[
            Shot(
                id=f"shot_{index}",
                order=index,
                scene_id=f"scene_{index}",
                source_script_beat_ids=[f"beat_{index}"],
                duration_seconds=20,
                subject=["speaker_a" if index == 1 else "speaker_b"],
                dialogue_or_narration=f"line {index}",
                visual_intent=f"Synthetic shot {index}",
            )
            for index in range(1, 4)
        ],
    )


def _synthetic_picture_final(root: Path, *, real_media: bool) -> Path:
    output = root / "final" / "assembly-av" / "picture-final.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not real_media:
        output.write_bytes(b"\x00\x00\x00\x18ftypisom-synthetic-picture-final")
        return output
    ffmpeg = _ffmpeg()
    shots: list[Path] = []
    for index, color in enumerate(("red", "green", "blue"), start=1):
        shot = root / "synthetic-shots" / f"shot-{index}.mp4"
        shot.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=24:d=20",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(shot),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr[-1000:]
        shots.append(shot)
    concat_list = root / "synthetic-shots" / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in shots), encoding="utf-8"
    )
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    return output


def _context(
    tmp_path: Path,
    *,
    picture_duration: float = 60.0,
    real_media: bool = False,
) -> tuple[ProjectRepository, Project, PostProductionService, object, Path]:
    paths = DatabasePaths(
        database=tmp_path / "aidrama.db",
        projects=tmp_path / "projects",
        archived_projects=tmp_path / "archived",
    )
    repository = ProjectRepository(paths)
    project = repository.create_project(
        Project(
            id="project-av",
            title="Audiovisual delivery",
            description="offline fixture",
            status=ProjectStatus.POSTPRODUCTION,
            aspect_ratio=AspectRatio.LANDSCAPE,
            target_duration_seconds=60,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    repository.create_story_revision(
        revision_id="story-av",
        project_id=project.id,
        version=1,
        status=StoryRevisionStatus.APPROVED,
        content=_story(),
        generation_input=None,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_script_revision(
        revision_id="script-av",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story-av",
        content=_script(),
        generation_input=None,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_shot_revision(
        revision_id="shots-av",
        project_id=project.id,
        version=1,
        status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script-av",
        content=_shot_plan(),
        generation_input=None,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_production_job(
        ProductionJob(
            id="job-av",
            project_id=project.id,
            shot_plan_revision_id="shots-av",
            status=ProductionJobStatus.SUCCEEDED,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for index in range(1, 4):
        repository.create_production_shot(
            ProductionShot(
                id=f"production-shot-{index}",
                production_job_id="job-av",
                shot_id=f"shot_{index}",
                order_index=index,
                status=ProductionShotStatus.SUCCEEDED,
                created_at=NOW,
            )
        )
    repository.create_final_assembly(
        FinalAssembly(
            id="assembly-av",
            project_id=project.id,
            production_job_id="job-av",
            status=FinalAssemblyStatus.SUCCEEDED,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    picture = _synthetic_picture_final(
        repository.project_directory(project.id), real_media=real_media
    )
    repository.create_final_assembly_render_attempt(
        FinalAssemblyRenderAttempt(
            id="picture-final-attempt",
            final_assembly_id="assembly-av",
            attempt_number=1,
            status=FinalAssemblyRenderAttemptStatus.SUCCEEDED,
            adapter_name="synthetic-three-shot-concat",
            output_relative_path=picture.relative_to(
                repository.project_directory(project.id)
            ).as_posix(),
            metadata_json={
                "artifact_role": "PICTURE_FINAL",
                "shot_count": 3,
                "duration_seconds": picture_duration,
                "sha256": hashlib.sha256(picture.read_bytes()).hexdigest(),
                "size_bytes": picture.stat().st_size,
            },
            created_at=NOW,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    postproduction = PostProductionService(
        repository,
        media_adapter=(
            FFmpegPostProductionAdapter(ffmpeg_binary=_ffmpeg())
            if real_media
            else None
        ),
    )
    plan = postproduction.create_plan(project.id, "assembly-av")
    return repository, project, postproduction, plan, picture


def test_fake_universal_runtime_returns_content_addressed_parseable_wav(tmp_path):
    runtime = FakeTTSUniversalRuntime()
    manifest = runtime.manifest
    request = CapabilityRequest(
        request_id="fake-runtime-request",
        project_id="project-av",
        capability=CapabilityKind.TTS,
        protocol_family=ProtocolFamily.REQUEST_RESPONSE,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
        prompt_or_text="可解析的离线测试音频",
        structured_input={"shot_id": "shot_1"},
        provider_parameters={
            "voice": "fake-speaker-a-v1",
            "language": "zh-CN",
            "sample_rate": 48000,
            "audio_format": "wav",
        },
    )
    sink = ContentAddressedArtifactSink(tmp_path / "artifacts")
    result = runtime.invoke(request, artifact_sink=sink)

    assert result.succeeded
    assert result.safe_metadata["real_provider_calls"] == 0
    artifact = result.outputs[0]
    path = sink.path_for(artifact)
    assert path.name == f"{artifact.sha256}.wav"
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == 48000
        assert handle.getnchannels() == 1
        assert handle.getnframes() > 0


def test_dialogue_voice_tts_audio_subtitle_pipeline_is_versioned_and_persistent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")

    def fail_network(*_args, **_kwargs):
        raise AssertionError("network access is forbidden in fake TTS acceptance")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    repository, project, postproduction, plan, _picture = _context(tmp_path)
    runtime = FakeTTSUniversalRuntime()
    service = AudiovisualPipelineService(
        repository,
        tts_runtime=runtime,
        postproduction_service=postproduction,
    )

    result = service.run_fake_pipeline(project.id, plan.id)

    assert [line.speaker for line in result.dialogue_plan.lines] == [
        "speaker_a",
        "narrator",
        "speaker_b",
    ]
    assert [line.shot_id for line in result.dialogue_plan.lines] == [
        "shot_1",
        "shot_2",
        "shot_3",
    ]
    assert result.dialogue_plan.version == 1
    assert {item.speaker for item in result.voice_assignment_set.assignments} == {
        "speaker_a",
        "speaker_b",
        "narrator",
    }
    assert all(item.voice_profile.startswith("fake-") for item in result.voice_assignment_set.assignments)
    assert len(result.tts_tasks) == 3
    assert all(item.status is TTSTaskStatus.SUCCEEDED for item in result.tts_tasks)
    assert all(item.metadata_json["real_provider_calls"] == 0 for item in result.tts_tasks)
    assert all(item.output_relative_path and item.output_sha256 for item in result.tts_tasks)
    assert runtime.invocation_count == 3
    assert result.audio_timeline.duration_seconds == pytest.approx(60.0)
    assert result.audio_timeline.content_end_seconds < 60.0
    assert [item.silence_gap_seconds for item in result.audio_timeline.items] == [
        0.0,
        0.2,
        0.2,
    ]
    service.assert_subtitle_timing_matches_audio(
        result.audio_timeline, result.subtitle_track
    )
    srt_relative = postproduction.export_srt(
        project.id, result.subtitle_track.id, plan_id=plan.id
    )
    srt_path = repository.project_directory(project.id) / srt_relative
    assert "00:00:00,000 -->" in srt_path.read_text(encoding="utf-8")
    voice_path = repository.project_directory(project.id) / result.voice_track.path
    assert voice_path.name == f"{result.audio_timeline.artifact_sha256}.wav"
    with wave.open(str(voice_path), "rb") as handle:
        assert handle.getnframes() / handle.getframerate() == pytest.approx(60.0)

    cold = ProjectRepository(repository.paths)
    assert cold.get_dialogue_plan(result.dialogue_plan.id) == result.dialogue_plan
    assert cold.get_voice_assignment_set(result.voice_assignment_set.id) == result.voice_assignment_set
    assert cold.get_audio_timeline(result.audio_timeline.id) == result.audio_timeline
    assert [item.status for item in cold.list_tts_tasks(project.id, plan.id)] == [
        TTSTaskStatus.SUCCEEDED,
        TTSTaskStatus.SUCCEEDED,
        TTSTaskStatus.SUCCEEDED,
    ]
    assert cold.list_provider_tasks(project.id) == []
    assert cold.list_ai_invocations(project.id) == []


def test_audio_duration_conflict_is_reported_without_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    repository, project, postproduction, plan, _picture = _context(
        tmp_path, picture_duration=1.0
    )
    service = AudiovisualPipelineService(
        repository, postproduction_service=postproduction
    )
    dialogue = service.build_dialogue_plan(project.id, plan.id)
    assignments = service.assign_voices(project.id, plan.id, dialogue.id)
    planned = service.plan_tts_tasks(
        project.id, plan.id, dialogue.id, assignments.id
    )
    service.synthesize_tts_tasks(project.id, plan.id, planned)

    with pytest.raises(AudiovisualPipelineError, match=AUDIO_DURATION_CONFLICT):
        service.build_audio_timeline(
            project.id, plan.id, dialogue.id, assignments.id
        )
    assert service.list_audio_timelines(project.id, plan.id) == []


def test_postproduction_ui_state_does_not_mark_partial_tts_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    repository, project, postproduction, plan, _picture = _context(tmp_path)
    pipeline = AudiovisualPipelineService(
        repository, postproduction_service=postproduction
    )
    dialogue = pipeline.build_dialogue_plan(project.id, plan.id)
    assignments = pipeline.assign_voices(project.id, plan.id, dialogue.id)
    planned = pipeline.plan_tts_tasks(
        project.id, plan.id, dialogue.id, assignments.id
    )
    pipeline.synthesize_tts_tasks(project.id, plan.id, planned[:1])

    assert _postproduction_state_snapshot(
        postproduction, pipeline, project.id, plan.id
    ) == {
        "Voice state": "NOT READY",
        "Audio state": "NOT READY",
        "Subtitle state": "NOT READY",
        "Delivery artifact": "NOT RENDERED",
    }


def test_three_shot_sixty_second_audiovisual_mux_and_ui_playback_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    repository, project, postproduction, plan, picture = _context(
        tmp_path, real_media=True
    )
    pipeline = AudiovisualPipelineService(
        repository, postproduction_service=postproduction
    )
    source_probe = postproduction.media_adapter.probe_output(picture)
    assert source_probe["video_stream"] is True
    assert source_probe["audio_stream"] is False
    assert source_probe["duration_seconds"] == pytest.approx(60.0, abs=0.15)

    inputs = pipeline.run_fake_pipeline(project.id, plan.id)
    attempt = postproduction.render(
        project.id,
        plan.id,
        subtitle_track_id=inputs.subtitle_track.id,
        voice_track_id=inputs.voice_track.id,
    )
    output = postproduction.resolve_output_path(project.id, plan.id, attempt.id)
    assert output is not None and output.stat().st_size > 0
    probe = postproduction.media_adapter.probe_output(output)
    assert probe["video_stream"] is True
    assert probe["audio_stream"] is True
    assert probe["duration_seconds"] == pytest.approx(60.0, abs=0.35)
    assert probe["codec"] in {"h264", "avc1"}
    codec_probe = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    codec_description = f"{codec_probe.stderr}\n{codec_probe.stdout}"
    assert re.search(r"Video:\s*(?:h264|avc1)\b", codec_description)
    assert re.search(r"Audio:\s*aac\b", codec_description)
    assert attempt.metadata_json["artifact_role"] == "DELIVERY_FINAL"
    assert attempt.metadata_json["source_final_assembly_id"] == "assembly-av"
    assert attempt.metadata_json["source_final_assembly_render_attempt_id"] == "picture-final-attempt"
    assert attempt.metadata_json["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert not Path(attempt.output_relative_path).is_absolute()

    state = _postproduction_state_snapshot(
        postproduction, pipeline, project.id, plan.id
    )
    assert state == {
        "Voice state": "READY · 3 lines",
        "Audio state": "READY",
        "Subtitle state": "READY",
        "Delivery artifact": "READY · v1",
    }
    from aidrama_studio.pages import postproduction as page

    workspace_source = inspect.getsource(page._render_post_workspace)
    assert "voice_track_id=" in workspace_source
    assert "st.video" in workspace_source
