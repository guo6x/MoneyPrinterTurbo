from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AspectRatio,
    FinalAssemblyStatus,
    HeavyJobStatus,
)
from aidrama_studio.services import (
    FinalAssemblyRuntimeService,
    FinalAssemblyRuntimeServiceError,
    FinalAssemblyService,
    HeavyJobRunner,
    HeavyJobService,
    ProductionService,
    ProjectService,
    ShotService,
)
from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter
from aidrama_studio.services.ffmpeg_runtime import (
    FFmpegEncoderConfigurationError,
    H264_ENCODER_ENV,
    VideoEncoderSelection,
)
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.database import connect
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)


def test_h264_output_codec_uses_configured_non_gpl_encoder(monkeypatch):
    monkeypatch.setenv(H264_ENCODER_ENV, "h264_mf")
    encoder = VideoEncoderSelection.resolve("H264")

    assert encoder.codec == "h264"
    assert encoder.implementation == "h264_mf"
    assert encoder.output_args() == ["-c:v", "h264_mf", "-pix_fmt", "yuv420p"]
    assert "libx264" not in encoder.output_args()


def test_configured_h264_encoder_unavailable_fails_without_fallback(monkeypatch):
    monkeypatch.setenv(H264_ENCODER_ENV, "h264_mf")
    encoder = VideoEncoderSelection.resolve("h264")
    completed = subprocess.CompletedProcess(
        args=["ffmpeg", "-encoders"],
        returncode=0,
        stdout=" V..... libx264             H.264 / AVC\n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(
        FFmpegEncoderConfigurationError,
        match="automatic fallback is disabled",
    ):
        encoder.require_available("ffmpeg")


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def _make_source_videos(repository, project, job, shots, *, ffmpeg: str):
    artifacts = []
    colors = ("red", "green", "blue")
    for index, (shot, color) in enumerate(zip(shots, colors), 1):
        execution, artifact, _qc, _review = _source(
            repository,
            project,
            job,
            shot,
            suffix=str(index),
            duration_seconds=0.6,
        )
        target = repository.paths.projects / project.id / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d=0.6",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(completed.stderr[-1000:])
        artifacts.append((execution, artifact))
    return artifacts


def _make_source_video(
    repository,
    project,
    job,
    shot,
    *,
    ffmpeg: str,
    duration_seconds: float,
    resolution: str,
    suffix: str,
    metadata_duration_seconds: float | None = None,
):
    execution, artifact, _qc, _review = _source(
        repository,
        project,
        job,
        shot,
        suffix=suffix,
        duration_seconds=(
            duration_seconds
            if metadata_duration_seconds is None
            else metadata_duration_seconds
        ),
    )
    target = repository.paths.projects / project.id / artifact.path
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=navy:s={resolution}:d={duration_seconds:g}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr[-1000:])
    return execution, artifact


def test_draft_assembly_is_blocked_without_creating_render_attempt(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(repository, project, job, shots[0], suffix="1")
    assembly = FinalAssemblyService(repository).create_assembly(project.id, job.id)
    service = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="READY"):
        service.render(project.id, assembly.id)
    assert repository.list_final_assembly_render_attempts(assembly.id) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_real_three_shot_mp4_smoke_preserves_manifest_order_and_metadata(context):
    repository, project = context
    job, shots = _shots(repository, project, 3)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_videos(repository, project, job, shots, ffmpeg=ffmpeg)
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    original_manifest = manifest_service.get_manifest(project.id, assembly.id)
    runtime = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))

    attempt = runtime.render(project.id, assembly.id)

    assert attempt.status.value == "SUCCEEDED"
    assert attempt.output_relative_path == f"final/{assembly.id}/episode.mp4"
    assert attempt.metadata_json["video_stream"] is True
    assert attempt.metadata_json["size_bytes"] > 0
    assert len(attempt.metadata_json["sha256"]) == 64
    assert [item["order_index"] for item in attempt.metadata_json["source_items"]] == [1, 2, 3]
    assert repository.get_final_assembly(assembly.id).status is FinalAssemblyStatus.SUCCEEDED
    assert (repository.paths.projects / project.id / attempt.output_relative_path).is_file()
    reloaded_manifest = manifest_service.get_manifest(project.id, assembly.id)
    assert reloaded_manifest.items == original_manifest.items
    assert reloaded_manifest.id == original_manifest.id

    retry = runtime.retry(project.id, assembly.id)
    assert retry.status.value == "SUCCEEDED"
    assert retry.attempt_number == 2
    assert retry.output_relative_path != attempt.output_relative_path
    assert repository.get_final_assembly_render_attempt(attempt.id).output_relative_path == attempt.output_relative_path


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_duration_output_e2e_trims_physical_source_to_creative_target(context):
    repository, project = context
    ProjectService(repository).update(
        project.id,
        title=project.title,
        description=project.description,
        status=project.status,
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=2,
        delivery_resolution_label="1080p",
        target_fps=30,
        quality_mode="HIGH",
    )
    job, shots = _shots(repository, project, 1)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_video(
        repository,
        project,
        job,
        shots[0],
        ffmpeg=ffmpeg,
        duration_seconds=2.5,
        resolution="1920x1080",
        suffix="duration",
    )
    assembly_service = FinalAssemblyService(repository)
    assembly = assembly_service.create_assembly(project.id, job.id, freeze=True)
    manifest = assembly_service.get_manifest(project.id, assembly.id)

    assert manifest.items[0].source_duration_seconds == 2.5
    assert manifest.items[0].trimmed_duration_seconds == 2.0
    assert manifest.items[0].timeline_duration_seconds == 2.0
    assert manifest.items[0].duration_strategy == "TRIM_TO_CREATIVE"

    attempt = FinalAssemblyRuntimeService(
        repository,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id,
            ffmpeg_binary=ffmpeg,
        ),
    ).render(project.id, assembly.id)
    metadata = attempt.metadata_json

    assert metadata["duration_control"]["truth"] == "TARGET_MET"
    assert metadata["duration_control"]["target_duration_seconds"] == 2.0
    assert metadata["duration_control"]["planned_timeline_duration_seconds"] == 2.0
    assert metadata["duration_control"]["actual_duration_seconds"] == pytest.approx(2.0, abs=0.1)
    assert metadata["source_items"][0]["source_duration_seconds"] == pytest.approx(2.5, abs=0.1)
    assert metadata["source_items"][0]["planned_timeline_duration_seconds"] == 2.0
    assert metadata["source_items"][0]["actual_timeline_duration_seconds"] == pytest.approx(2.0, abs=0.1)


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_120_second_1080p_30fps_delivery_e2e_uses_real_timeline(context):
    repository, project = context
    ProjectService(repository).update(
        project.id,
        title=project.title,
        description=project.description,
        status=project.status,
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=120,
        delivery_resolution_label="1080p",
        target_fps=30,
        quality_mode="HIGH",
    )
    # Reuse the normal readiness setup, then create an approved 120-second
    # creative plan instead of mutating the historical approved fixture plan.
    _ready_job(repository, project)
    original = repository.get_shot_revision("shot_001")
    assert original is not None
    long_plan = original["content"].model_copy(
        update={
            "shots": [
                original["content"].shots[0].model_copy(
                    update={"duration_seconds": 120.0}
                )
            ]
        }
    )
    shot_service = ShotService(repository)
    duration_draft = shot_service.save_draft(
        project.id,
        long_plan,
        revision_id="shot_001",
        generation_input={"duration_e2e": True},
    )
    duration_plan = shot_service.approve_revision(duration_draft["id"])
    production = ProductionService(repository)
    job = production.create_production_job(project.id, duration_plan["id"])
    shots = production.create_production_shots(project.id, job.id)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_video(
        repository,
        project,
        job,
        shots[0],
        ffmpeg=ffmpeg,
        duration_seconds=121.0,
        resolution="320x180",
        suffix="120-second",
    )
    assembly_service = FinalAssemblyService(repository)
    assembly = assembly_service.create_assembly(project.id, job.id, freeze=True)
    manifest = assembly_service.get_manifest(project.id, assembly.id)
    assert manifest.items[0].source_duration_seconds == 121.0
    assert manifest.items[0].timeline_duration_seconds == 120.0
    assert manifest.items[0].duration_strategy == "TRIM_TO_CREATIVE"

    attempt = FinalAssemblyRuntimeService(
        repository,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id,
            ffmpeg_binary=ffmpeg,
        ),
    ).render(project.id, assembly.id)
    metadata = attempt.metadata_json
    assert metadata["duration_control"]["truth"] == "TARGET_MET"
    assert metadata["duration_control"]["target_duration_seconds"] == 120.0
    assert metadata["duration_seconds"] == pytest.approx(120.0, abs=0.12)
    assert metadata["resolution"] == "1920x1080"
    assert metadata["fps"] == pytest.approx(30.0, abs=0.2)


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_four_k_delivery_runs_through_durable_heavy_job_and_preserves_native_truth(context):
    repository, project = context
    ProjectService(repository).update(
        project.id,
        title=project.title,
        description=project.description,
        status=project.status,
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=2,
        delivery_resolution_label="4K",
        target_fps=30,
        quality_mode="FINAL",
    )
    job, shots = _shots(repository, project, 1)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_video(
        repository,
        project,
        job,
        shots[0],
        ffmpeg=ffmpeg,
        duration_seconds=2.0,
        resolution="1920x1080",
        suffix="4k",
    )
    assembly = FinalAssemblyService(repository).create_assembly(
        project.id, job.id, freeze=True
    )
    queued = HeavyJobService(repository).enqueue_final_assembly(
        project.id, assembly.id
    )

    assert queued.status is HeavyJobStatus.QUEUED
    assert repository.list_final_assembly_render_attempts(assembly.id)[0].status.value == "PENDING"

    completed = HeavyJobRunner(repository).run_once(project.id)

    assert completed is not None and completed.id == queued.id
    assert completed.status is HeavyJobStatus.SUCCEEDED
    attempt = repository.list_final_assembly_render_attempts(assembly.id)[0]
    assert attempt.status.value == "SUCCEEDED"
    metadata = attempt.metadata_json
    assert metadata["native_source_resolutions"] == ["1920x1080"]
    assert metadata["delivery_resolution"] == "3840x2160"
    assert metadata["resolution"] == "3840x2160"
    assert metadata["delivery_strategy"] == "DETERMINISTIC_UPSCALE"
    assert metadata["delivery_fps"] == 30.0
    assert metadata["fps"] == pytest.approx(30.0, abs=0.1)
    assert HeavyJobService(repository).list_events(project.id, queued.id)[-1].event_type.value == "SUCCEEDED"


def test_missing_source_records_failure_and_leaves_no_canonical_output(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _execution, artifact, _qc, _review = _source(repository, project, job, shots[0], suffix="1")
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    (repository.paths.projects / project.id / artifact.path).unlink()
    service = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))
    with pytest.raises(FinalAssemblyRuntimeServiceError):
        service.render(project.id, assembly.id)
    attempts = service.list_attempts(project.id, assembly.id)
    assert len(attempts) == 1 and attempts[0].status.value == "FAILED"
    assert not (repository.paths.projects / project.id / "final" / assembly.id / "episode.mp4").exists()


def test_frozen_source_hash_mismatch_blocks_adapter_before_render(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _execution, artifact, _qc, _review = _source(
        repository, project, job, shots[0], suffix="hash"
    )
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    source = repository.paths.projects / project.id / artifact.path
    source.write_bytes(b"replaced-after-freeze")

    class NeverRender:
        name = "never-render"
        called = False

        def validate_sources(self, request):
            self.called = True
            return True

        def probe_output(self, path):
            self.called = True
            return {}

        def render(self, request, output_path):
            self.called = True

    adapter = NeverRender()
    service = FinalAssemblyRuntimeService(repository, adapter=adapter)
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="SHA256"):
        service.render(project.id, assembly.id)

    assert adapter.called is False
    assert service.list_attempts(project.id, assembly.id)[0].status.value == "FAILED"


@pytest.mark.skipif(shutil.which("ffmpeg") is None and not Path("D:/github/MoneyPrinterTurbo/.venv/Lib/site-packages/imageio_ffmpeg/binaries").exists(), reason="ffmpeg unavailable")
def test_frozen_source_duration_metadata_mismatch_is_fail_closed(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_video(
        repository,
        project,
        job,
        shots[0],
        ffmpeg=ffmpeg,
        duration_seconds=0.6,
        metadata_duration_seconds=2.0,
        resolution="320x240",
        suffix="duration-mismatch",
    )
    assembly = FinalAssemblyService(repository).create_assembly(
        project.id, job.id, freeze=True
    )
    service = FinalAssemblyRuntimeService(
        repository,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id,
            ffmpeg_binary=ffmpeg,
        ),
    )

    with pytest.raises(FinalAssemblyRuntimeServiceError, match="physical source duration"):
        service.render(project.id, assembly.id)

    assert service.list_attempts(project.id, assembly.id)[0].status.value == "FAILED"


def test_manifest_path_traversal_is_rejected_before_adapter_render(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _execution, artifact, _qc, _review = _source(repository, project, job, shots[0], suffix="1")
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    with connect(repository.paths.database) as connection:
        connection.execute(
            "UPDATE final_assembly_items SET source_path=? WHERE final_assembly_id=?",
            ("../outside.mp4", assembly.id),
        )
    service = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="越过|相对路径"):
        service.render(project.id, assembly.id)
    assert service.list_attempts(project.id, assembly.id)[0].status.value == "FAILED"


def test_retry_keeps_frozen_source_identity_and_survives_repository_reload(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    [(first_execution, first_artifact)] = _make_source_videos(repository, project, job, shots, ffmpeg=ffmpeg)
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    runtime = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))
    first_attempt = runtime.render(project.id, assembly.id)
    # A newer production retry is deliberately created after the manifest was
    # frozen.  Rendering the same assembly must not discover or use it.
    _new_execution, _new_artifact, _new_qc, _new_review = _source(
        repository, project, job, shots[0], suffix="new", created_at="2099-01-01T00:00:00+00:00"
    )
    second_attempt = runtime.retry(project.id, assembly.id)
    assert second_attempt.status.value == "SUCCEEDED"
    frozen_sources = second_attempt.metadata_json["source_items"]
    assert frozen_sources[0]["production_execution_id"] == first_execution.id
    assert frozen_sources[0]["production_artifact_id"] == first_artifact.id
    reloaded = ProjectRepository(repository.paths)
    assert reloaded.get_final_assembly_render_attempt(first_attempt.id).metadata_json["sha256"]
    assert len(FinalAssemblyRuntimeService(reloaded).list_attempts(project.id, assembly.id)) == 2


def test_successful_output_resolver_handles_missing_file_and_rejects_cross_project(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _make_source_videos(repository, project, job, shots, ffmpeg=ffmpeg)
    manifest_service = FinalAssemblyService(repository)
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    runtime = FinalAssemblyRuntimeService(repository, adapter=MPTFinalAssemblyAdapter(project_root=repository.paths.projects / project.id))
    attempt = runtime.render(project.id, assembly.id)
    assert runtime.resolve_output_path(project.id, assembly.id, attempt.id).is_file()
    output = repository.paths.projects / project.id / attempt.output_relative_path
    output.unlink()
    assert runtime.resolve_output_path(project.id, assembly.id, attempt.id) is None
    with pytest.raises(FinalAssemblyRuntimeServiceError):
        runtime.resolve_output_path("other-project", assembly.id, attempt.id)


def test_final_timeline_is_aligned_to_actual_container_duration():
    trace = [
        {
            "production_shot_id": "shot-1",
            "source_duration_seconds": 1.0,
            "timeline_start_seconds": 0.0,
            "timeline_end_seconds": 1.0,
        },
        {
            "production_shot_id": "shot-2",
            "source_duration_seconds": 1.0,
            "timeline_start_seconds": 1.0,
            "timeline_end_seconds": 2.0,
        },
    ]

    aligned = FinalAssemblyRuntimeService._align_source_trace(
        trace,
        expected_duration=2.0,
        actual_duration=1.92,
    )

    assert [item["timeline_start_seconds"] for item in aligned] == [0.0, 0.96]
    assert [item["timeline_end_seconds"] for item in aligned] == [0.96, 1.92]
    assert [item["planned_timeline_start_seconds"] for item in aligned] == [0.0, 1.0]
    assert [item["planned_timeline_end_seconds"] for item in aligned] == [1.0, 2.0]
    assert [item["actual_timeline_duration_seconds"] for item in aligned] == [0.96, 0.96]
    assert [item["timeline_duration_seconds"] for item in aligned] == [0.96, 0.96]
    assert [item["source_duration_seconds"] for item in aligned] == [1.0, 1.0]
    assert trace[1]["timeline_end_seconds"] == 2.0


def test_output_validation_requires_frozen_resolution_fps_and_strict_duration():
    profile = {
        "delivery_width": 3840,
        "delivery_height": 2160,
        "target_fps": 30,
    }
    valid = {
        "video_stream": True,
        "size_bytes": 1024,
        "width": 3840,
        "height": 2160,
        "fps": 30.0,
        "duration_seconds": 2.0,
    }
    FinalAssemblyRuntimeService._validate_rendered_output(
        valid, 2.0, output_profile=profile
    )
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="duration"):
        FinalAssemblyRuntimeService._validate_rendered_output(
            {**valid, "duration_seconds": 2.2}, 2.0, output_profile=profile
        )
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="resolution"):
        FinalAssemblyRuntimeService._validate_rendered_output(
            {**valid, "width": 1920, "height": 1080},
            2.0,
            output_profile=profile,
        )
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="fps"):
        FinalAssemblyRuntimeService._validate_rendered_output(
            {**valid, "fps": 24.0}, 2.0, output_profile=profile
        )


def test_mixed_native_sources_report_any_required_upscale():
    class Profile:
        delivery_width = 1920
        delivery_height = 1080

    sources = [
        {"width": 1920, "height": 1080, "fps": 30.0},
        {"width": 1280, "height": 720, "fps": 24.0},
    ]
    assert (
        FinalAssemblyRuntimeService._delivery_strategy(sources, Profile())
        == "DETERMINISTIC_UPSCALE"
    )
    assert [
        item["transform"]
        for item in FinalAssemblyRuntimeService._source_delivery_transforms(
            sources, Profile()
        )
    ] == ["NATIVE_OR_NORMALIZE", "DETERMINISTIC_UPSCALE"]
