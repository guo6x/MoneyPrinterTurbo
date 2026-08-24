from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import shutil
import subprocess

import pytest

from aidrama_studio.domain import (
    AudioMixConfig,
    ScriptBeat,
    ScriptBeatType,
    Scene,
    StructuredScript,
    SubtitleTrack,
    StoryRevisionStatus,
    ScriptRevisionStatus,
)
from aidrama_studio.services import PostProductionMediaAdapter, PostProductionService, PostProductionServiceError
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


class FakePostAdapter(PostProductionMediaAdapter):
    name = "fake-post"

    def render(self, request):
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"\x00\x00\x00\x18ftypisom-post-produced")
        return {"runtime": "fake", "video_stream": True}


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


def test_plan_audio_tracks_are_project_scoped_and_safe(context, tmp_path):
    repository, project, service, assembly = _plan(context)
    plan = service.create_plan(project.id, assembly.id)
    audio = repository.paths.projects / project.id / "music.wav"
    audio.write_bytes(b"RIFFfake")
    music = service.add_music_track(project.id, plan.id, "music.wav", gain=0.3, loop=True)
    assert music.path == "music.wav"
    with pytest.raises(PostProductionServiceError):
        service.add_music_track(project.id, plan.id, "../music.wav")
    other = service.repository.create_project(replace(service.repository.get_project(project.id), id="other-project", title="Other"))
    with pytest.raises(PostProductionServiceError):
        service.get_plan(other.id, plan.id)
    assert service.configure_audio_mix(project.id, plan.id, AudioMixConfig(music_gain=0.1)).audio_mix.music_gain == 0.1


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
        def render(self, request):
            raise RuntimeError("boom")

    service.media_adapter = Broken()
    plan = service.create_plan(project.id, assembly.id)
    with pytest.raises(PostProductionServiceError):
        service.render(project.id, plan.id)
    assert service.list_render_attempts(project.id, plan.id)[0].status.value == "FAILED"


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
