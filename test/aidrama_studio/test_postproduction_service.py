from __future__ import annotations

import hashlib
from pathlib import Path
from dataclasses import replace
import shutil
import subprocess

import pytest

from aidrama_studio.domain import (
    AudioMixConfig,
    FinalAssemblyRenderAttempt,
    FinalAssemblyRenderAttemptStatus,
    FinalAssemblyStatus,
    ProjectStatus,
    ScriptBeat,
    ScriptBeatType,
    Scene,
    StructuredScript,
    SubtitleTrack,
    StoryRevisionStatus,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
)
from aidrama_studio.services import (
    FinalAssemblyService,
    CurrentProductionStateService,
    PostProductionMediaAdapter,
    PostProductionService,
    PostProductionServiceError,
    ProductionService,
)
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


class FakePostAdapter(PostProductionMediaAdapter):
    name = "fake-post"

    def render(self, request):
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"\x00\x00\x00\x18ftypisom-post-produced")
        return {"runtime": "fake", "video_stream": True}

    def probe_output(self, output_path):
        return {
            "video_stream": True,
            "audio_stream": False,
            "duration_seconds": 2.0,
            "width": 320,
            "height": 240,
            "size_bytes": output_path.stat().st_size,
        }


def _plan(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(repository, project, job, shots[0], suffix="1")
    from aidrama_studio.services import FinalAssemblyService

    assembly = FinalAssemblyService(repository).create_assembly(project.id, job.id, freeze=True)
    service = PostProductionService(repository, media_adapter=FakePostAdapter(), final_assembly_service=type("Resolver", (), {"resolve_output_path": lambda self, p, a: repository.paths.projects / project.id / "base.mp4"})())
    (repository.paths.projects / project.id / "base.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom-source")
    return repository, project, service, assembly


def test_subtitle_extraction_is_ordered_and_exports_srt(context):
    repository, project = context
    script = StructuredScript(title="dialogue", scenes=[Scene(id="scene_1", order=1, title="Room", location_id="loc_001", estimated_duration_seconds=3, beats=[
        ScriptBeat(id="b1", order=1, type=ScriptBeatType.DIALOGUE, character_id="char_001", text="Hello", estimated_duration_seconds=1),
        ScriptBeat(id="b2", order=2, type=ScriptBeatType.NARRATION, text="World", estimated_duration_seconds=1),
    ])])
    repository.create_script_revision(revision_id="script_dialogue", project_id=project.id, version=2, status=ScriptRevisionStatus.DRAFT, source_story_revision_id="story_001", content=script, generation_input=None, created_at="now", updated_at="now")
    service = PostProductionService(repository)
    track = service.build_subtitle_timeline(project.id, "script_dialogue")
    assert [cue.text for cue in track.cues] == ["Hello", "World"]
    assert track.cues[0].start_seconds < track.cues[1].start_seconds
    assert "00:00:00,000 --> 00:00:01,000" in service.to_srt(track)


def test_final_subtitles_use_pinned_render_timeline_and_split_noncontiguous_beat(context):
    repository, project = context
    # Seed the already-approved reference coverage, then create an exact
    # three-shot chain whose repeated beat is intentionally non-contiguous.
    _ready_job(repository, project)
    script = StructuredScript(
        title="final timeline",
        scenes=[
            Scene(
                id="scene_final",
                order=1,
                title="Room",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=20,
                beats=[
                    ScriptBeat(id="beat_a", order=1, type=ScriptBeatType.DIALOGUE, character_id="char_001", text="甲", estimated_duration_seconds=10),
                    ScriptBeat(id="beat_b", order=2, type=ScriptBeatType.DIALOGUE, character_id="char_001", text="乙", estimated_duration_seconds=10),
                ],
            )
        ],
    )
    repository.create_script_revision(
        revision_id="script_final_timeline",
        project_id=project.id,
        version=2,
        status=ScriptRevisionStatus.DRAFT,
        source_story_revision_id="story_001",
        content=script,
        generation_input=None,
        created_at="later",
        updated_at="later",
    )
    repository.approve_script_revision(
        "script_final_timeline", updated_at="later-approved"
    )
    shot_plan = ShotPlan(
        title="timeline shots",
        source_script_revision_id="script_final_timeline",
        shots=[
            Shot(id="shot_a1", order=1, scene_id="scene_final", source_script_beat_ids=["beat_a"], duration_seconds=1, subject=["char_001"], visual_intent="A1"),
            Shot(id="shot_b", order=2, scene_id="scene_final", source_script_beat_ids=["beat_b"], duration_seconds=1, subject=["char_001"], visual_intent="B"),
            Shot(id="shot_a2", order=3, scene_id="scene_final", source_script_beat_ids=["beat_a"], duration_seconds=1, subject=["char_001"], visual_intent="A2"),
        ],
    )
    repository.create_shot_revision(
        revision_id="shot_final_timeline",
        project_id=project.id,
        version=2,
        status=ShotRevisionStatus.DRAFT,
        source_script_revision_id="script_final_timeline",
        content=shot_plan,
        generation_input=None,
        created_at="later",
        updated_at="later",
    )
    repository.approve_shot_revision(
        "shot_final_timeline", updated_at="later-approved"
    )
    production_service = ProductionService(repository)
    job = production_service.create_production_job(project.id, "shot_final_timeline")
    production_service.create_production_shots(project.id, job.id)
    production_shots = sorted(
        repository.list_production_shots(job.id), key=lambda item: item.order_index
    )
    for index, production_shot in enumerate(production_shots, start=1):
        _source(
            repository,
            project,
            job,
            production_shot,
            suffix=f"timeline-{index}",
        )
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    manifest = manifest_service.get_manifest(project.id, assembly.id)
    output = repository.paths.projects / project.id / "final" / assembly.id / "episode.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\x00\x00\x00\x18ftypisom-final-timeline")
    durations = (2.0, 3.0, 5.0)
    cursor = 0.0
    source_trace = []
    for item, duration in zip(manifest.items, durations):
        source_trace.append(
            {
                "order_index": item.order_index,
                "production_shot_id": item.production_shot_id,
                "production_execution_id": item.production_execution_id,
                "production_artifact_id": item.production_artifact_id,
                "source_duration_seconds": duration,
                "timeline_start_seconds": cursor,
                "timeline_end_seconds": cursor + duration,
            }
        )
        cursor += duration
    attempt = repository.create_final_assembly_render_attempt(
        FinalAssemblyRenderAttempt(
            id="final-timeline-attempt",
            final_assembly_id=assembly.id,
            attempt_number=1,
            status=FinalAssemblyRenderAttemptStatus.SUCCEEDED,
            adapter_name="test",
            output_relative_path=f"final/{assembly.id}/episode.mp4",
            metadata_json={
                "duration_seconds": cursor,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "source_items": source_trace,
            },
            created_at="now",
            finished_at="now",
        )
    )
    repository.update_final_assembly_status(
        assembly.id, FinalAssemblyStatus.SUCCEEDED, updated_at="now"
    )

    class Resolver:
        def latest_successful_attempt(self, project_id, assembly_id):
            return attempt

        def resolve_output_path(self, project_id, assembly_id, attempt_id=None):
            return output

    service = PostProductionService(
        repository,
        media_adapter=FakePostAdapter(),
        final_assembly_service=Resolver(),
    )
    plan = service.create_plan(project.id, assembly.id)
    track = service.build_subtitle_timeline(
        project.id, "script_final_timeline", plan_id=plan.id
    )

    assert [(cue.text, cue.start_seconds, cue.end_seconds, cue.shot_id) for cue in track.cues] == [
        ("甲", 0.0, 2.0, "shot_a1"),
        ("乙", 2.0, 5.0, "shot_b"),
        ("甲", 5.0, 10.0, "shot_a2"),
    ]
    assert [cue.beat_id for cue in track.cues] == ["beat_a", "beat_b", "beat_a"]
    with pytest.raises(PostProductionServiceError, match="当前 FinalAssembly chain"):
        service.build_subtitle_timeline(project.id, "script_001", plan_id=plan.id)

    duplicate_beat_script = script.model_copy(
        update={"scenes": [script.scenes[0], script.scenes[0].model_copy(update={"id": "scene_duplicate"})]}
    )
    with pytest.raises(PostProductionServiceError, match="重复 Beat ID"):
        service._build_final_subtitle_cues(
            project.id,
            plan.id,
            "script_final_timeline",
            duplicate_beat_script,
            requested_shot_plan_revision_id=None,
        )

    repository.update_final_assembly_render_attempt(
        attempt.id,
        status=FinalAssemblyRenderAttemptStatus.SUCCEEDED,
        metadata_json={**attempt.metadata_json, "duration_seconds": cursor + 1.0},
    )
    with pytest.raises(PostProductionServiceError, match="未覆盖实际成片时长"):
        service.build_subtitle_timeline(
            project.id, "script_final_timeline", plan_id=plan.id
        )


def test_plan_audio_tracks_are_project_scoped_and_safe(context, tmp_path):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    audio = repository.paths.projects / project.id / "music.wav"
    audio.write_bytes(b"RIFFfake")
    music = service.add_music_track(project.id, plan.id, "music.wav", gain=0.3, loop=True)
    assert music.path == "music.wav"
    assert len(music.metadata_json["sha256"]) == 64
    with pytest.raises(PostProductionServiceError):
        service.add_music_track(project.id, plan.id, "../music.wav")
    other = service.repository.create_project(replace(service.repository.get_project(project.id), id="other-project", title="Other"))
    with pytest.raises(PostProductionServiceError):
        service.get_plan(other.id, plan.id)
    assert service.configure_audio_mix(project.id, plan.id, AudioMixConfig(music_gain=0.1)).audio_mix.music_gain == 0.1


def test_tampered_music_is_rejected_before_post_adapter_call(context):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    audio = repository.paths.projects / project.id / "music.wav"
    audio.write_bytes(b"RIFF-original")
    music = service.add_music_track(project.id, plan.id, "music.wav")
    audio.write_bytes(b"RIFF-tampered")

    with pytest.raises(PostProductionServiceError, match="MusicTrack SHA256"):
        service.render(project.id, plan.id, music_track_id=music.id)

    assert service.list_render_attempts(project.id, plan.id)[0].status.value == "FAILED"


def test_post_render_history_retry_and_immutable_source(context):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    first = service.render(project.id, plan.id)
    second = service.retry(project.id, plan.id)
    assert first.status.value == "SUCCEEDED"
    assert second.attempt_number == 2
    assert first.output_relative_path != second.output_relative_path
    assert service.resolve_output_path(project.id, plan.id, first.id).is_file()
    assert repository.get_final_assembly(assembly.id).id == assembly.id
    assert len(service.list_render_attempts(project.id, plan.id)) == 2


def test_failed_post_render_is_recorded(context):
    repository, project, service, assembly = _plan(context)

    class Broken(PostProductionMediaAdapter):
        def probe_output(self, output_path):
            return FakePostAdapter().probe_output(output_path)

        def render(self, request):
            raise RuntimeError("boom")

    service.media_adapter = Broken()
    plan = service.create_plan(project.id, assembly.id)
    with pytest.raises(PostProductionServiceError):
        service.render(project.id, plan.id)
    assert service.list_render_attempts(project.id, plan.id)[0].status.value == "FAILED"


def test_adapter_without_real_probe_cannot_accept_ftyp_pseudo_output(context):
    repository, project, service, assembly = _plan(context)

    class NoProbe(PostProductionMediaAdapter):
        def render(self, request):
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"\x00\x00\x00\x18ftypisom-pseudo")
            return {}

    service.media_adapter = NoProbe()
    plan = service.create_plan(project.id, assembly.id)
    with pytest.raises(PostProductionServiceError, match="probe"):
        service.render(project.id, plan.id)
    assert service.list_render_attempts(project.id, plan.id)[0].status.value == "FAILED"


def test_successful_post_output_hash_is_rechecked_before_resolution(context):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    attempt = service.render(project.id, plan.id)
    output = repository.paths.projects / project.id / attempt.output_relative_path
    output.write_bytes(b"\x00\x00\x00\x18ftypisom-tampered")

    assert service.resolve_output_path(project.id, plan.id, attempt.id) is None


def test_deleted_post_output_is_not_resolved(context):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    attempt = service.render(project.id, plan.id)
    output = repository.paths.projects / project.id / attempt.output_relative_path
    output.unlink()

    assert service.resolve_output_path(project.id, plan.id, attempt.id) is None


@pytest.mark.parametrize("tampered_stage", ("final", "post"))
def test_current_state_rejects_tampered_final_or_post_output(context, tampered_stage):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(repository, project, job, shots[0], suffix="current-state")
    assembly = FinalAssemblyService(repository).create_assembly(
        project.id, job.id, freeze=True
    )
    final_relative = f"final/{assembly.id}/episode.mp4"
    final_output = repository.paths.projects / project.id / final_relative
    final_output.parent.mkdir(parents=True, exist_ok=True)
    final_output.write_bytes(b"original-final-output")
    final_attempt = repository.create_final_assembly_render_attempt(
        FinalAssemblyRenderAttempt(
            id="current-state-final-attempt",
            final_assembly_id=assembly.id,
            attempt_number=1,
            status=FinalAssemblyRenderAttemptStatus.SUCCEEDED,
            adapter_name="test",
            output_relative_path=final_relative,
            metadata_json={
                "sha256": hashlib.sha256(final_output.read_bytes()).hexdigest()
            },
            created_at="now",
            finished_at="now",
        )
    )
    repository.update_final_assembly_status(
        assembly.id, FinalAssemblyStatus.SUCCEEDED, updated_at="now"
    )

    class Resolver:
        def latest_successful_attempt(self, project_id, assembly_id):
            return final_attempt

        def resolve_output_path(self, project_id, assembly_id, attempt_id=None):
            return final_output

    post_service = PostProductionService(
        repository,
        media_adapter=FakePostAdapter(),
        final_assembly_service=Resolver(),
    )
    plan = post_service.create_plan(project.id, assembly.id)
    post_attempt = post_service.render(project.id, plan.id)
    assert post_attempt.output_relative_path is not None

    current_state = CurrentProductionStateService(repository)
    assert current_state.derive(project.id).post_production_ready is True
    assert current_state.workflow_stage(project.id) is ProjectStatus.COMPLETED

    tampered_output = (
        final_output
        if tampered_stage == "final"
        else repository.paths.projects / project.id / post_attempt.output_relative_path
    )
    tampered_output.write_bytes(b"tampered-but-still-nonempty")

    assert current_state.derive(project.id).post_production_ready is False
    assert current_state.workflow_stage(project.id) is not ProjectStatus.COMPLETED


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_real_post_render_produces_project_scoped_mp4(context):
    repository, project, service, assembly = _plan(context)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = repository.paths.projects / project.id / "base.mp4"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=0.4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
    from aidrama_studio.services import FFmpegPostProductionAdapter
    service.media_adapter = FFmpegPostProductionAdapter(ffmpeg_binary=ffmpeg)
    plan = service.create_plan(project.id, assembly.id)
    attempt = service.render(project.id, plan.id)
    assert attempt.status.value == "SUCCEEDED"
    output = service.resolve_output_path(project.id, plan.id, attempt.id)
    assert output is not None and output.is_file() and output.stat().st_size > 0
    assert attempt.output_relative_path and not Path(attempt.output_relative_path).is_absolute()


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_real_post_short_audio_does_not_truncate_final_video(context):
    repository, project, service, assembly = _plan(context)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = repository.paths.projects / project.id / "base.mp4"
    generated = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=purple:s=320x240:d=2.0",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=0.4",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr[-1000:]
    music_source = repository.paths.projects / project.id / "short-music.wav"
    music_result = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:a", "pcm_s16le", str(music_source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert music_result.returncode == 0, music_result.stderr[-1000:]
    from aidrama_studio.services import FFmpegPostProductionAdapter

    service.media_adapter = FFmpegPostProductionAdapter(ffmpeg_binary=ffmpeg)
    plan = service.create_plan(project.id, assembly.id)
    music = service.import_bgm(project.id, plan.id, music_source)
    attempt = service.render(project.id, plan.id, music_track_id=music.id)

    assert attempt.status.value == "SUCCEEDED"
    assert attempt.metadata_json["probe"]["duration_seconds"] >= 1.7
    assert attempt.metadata_json["input_fingerprints"]["music_sha256"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_real_post_subtitle_and_bgm_smoke_is_pinned_and_probe_valid(context):
    """Exercise the real subtitle+BGM FFmpeg path, not a fake media adapter."""
    repository, project = context
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    job, shots = _shots(repository, project, 1)
    _execution, artifact, _qc, _review = _source(repository, project, job, shots[0], suffix="post-smoke")
    source = repository.paths.projects / project.id / artifact.path
    source.parent.mkdir(parents=True, exist_ok=True)
    generated = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1.2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        capture_output=True, text=True, check=False,
    )
    assert generated.returncode == 0, generated.stderr[-1000:]
    from aidrama_studio.services import FinalAssemblyRuntimeService, FinalAssemblyService, PostProductionService
    from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter

    assembly = FinalAssemblyService(repository).create_assembly(project.id, job.id, freeze=True)
    runtime = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id, ffmpeg_binary=ffmpeg))
    final_attempt = runtime.render(project.id, assembly.id)
    service = PostProductionService(repository, final_assembly_service=runtime)
    plan = service.create_plan(project.id, assembly.id)
    assert plan.source_final_assembly_render_attempt_id == final_attempt.id
    track = service.build_subtitle_timeline(project.id, "script_001", plan_id=plan.id)
    bgm_source = repository.paths.projects / project.id / "source-bgm.wav"
    bgm = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-c:a", "pcm_s16le", str(bgm_source)],
        capture_output=True, text=True, check=False,
    )
    assert bgm.returncode == 0, bgm.stderr[-1000:]
    music = service.import_bgm(project.id, plan.id, bgm_source)
    attempt = service.render(project.id, plan.id, subtitle_track_id=track.id, music_track_id=music.id)
    assert attempt.status.value == "SUCCEEDED"
    assert attempt.output_relative_path and not Path(attempt.output_relative_path).is_absolute()
    assert attempt.metadata_json["source_final_assembly_render_attempt_id"] == final_attempt.id
    assert attempt.metadata_json["sha256"] and len(attempt.metadata_json["sha256"]) == 64
    assert attempt.metadata_json["probe"]["video_stream"] is True
    assert attempt.metadata_json["probe"]["audio_stream"] is True
    assert (repository.paths.projects / project.id / attempt.output_relative_path).is_file()
