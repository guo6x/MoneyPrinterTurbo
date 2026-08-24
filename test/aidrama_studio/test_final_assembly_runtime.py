from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aidrama_studio.domain import FinalAssemblyStatus
from aidrama_studio.services import (
    FinalAssemblyRuntimeService,
    FinalAssemblyRuntimeServiceError,
    FinalAssemblyService,
)
from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.database import connect
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def _make_source_videos(repository, project, job, shots, *, ffmpeg: str):
    artifacts = []
    colors = ("red", "green", "blue")
    for index, (shot, color) in enumerate(zip(shots, colors), 1):
        execution, artifact, _qc, _review = _source(repository, project, job, shot, suffix=str(index))
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
