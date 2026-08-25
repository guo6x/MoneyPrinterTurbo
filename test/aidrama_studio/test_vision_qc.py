from __future__ import annotations

import shutil
import subprocess

import pytest

from aidrama_studio.services import DeterministicMockVisionProvider, ProductionExecutionService, ProductionService, UnavailableVisionProvider, VisionQCService
from test.aidrama_studio.test_production_qc import _artifact_context
from test.aidrama_studio.test_production_execution import context as _execution_context
from test.aidrama_studio.test_seedance_video_adapter import _frozen_seedance_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def test_unconfigured_vision_qc_is_truthfully_not_run(context):
    repository, project, execution, artifact = _artifact_context(context)
    result = VisionQCService(repository, provider=UnavailableVisionProvider()).analyze(project.id, execution.id, artifact.id)
    assert result.status == "NOT_RUN"
    assert result.analysis_kind == "AI_ANALYSIS"


def test_mock_vision_qc_is_structured_and_project_scoped(context):
    repository, project, execution, artifact = _artifact_context(context)
    result = VisionQCService(repository, provider=DeterministicMockVisionProvider({"CHARACTER_CONSISTENCY": {"score": 1.0}})).analyze(project.id, execution.id, artifact.id)
    assert result.status == "AI_ANALYSIS"
    assert result.metrics["CHARACTER_CONSISTENCY"]["score"] == 1.0
    assert result.frame_manifest_id
    manifest = repository.get_vision_frame_manifest(result.frame_manifest_id)
    assert manifest is not None
    assert {item["role"] for item in manifest.samples} >= {"FIRST", "MIDDLE", "LAST"}
    assert all(
        (repository.paths.projects / project.id / item["path"]).is_file()
        for item in manifest.samples
    )
    record = repository.get_vision_analysis(result.analysis_id)
    assert record is not None
    assert record.reference_version_ids
    assert record.frame_manifest_id == manifest.id
    assert "path" not in str(record.input_provenance).lower()
    invocations = repository.list_ai_invocations(project.id, execution.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    with pytest.raises(Exception):
        VisionQCService(repository).analyze("other-project", execution.id, artifact.id)


def test_vision_analysis_history_and_frame_files_are_append_only(context):
    repository, project, execution, artifact = _artifact_context(context)
    service = VisionQCService(repository, provider=DeterministicMockVisionProvider())

    first = service.analyze(project.id, execution.id, artifact.id)
    first_manifest = repository.get_vision_frame_manifest(first.frame_manifest_id)
    first_paths = [
        repository.paths.projects / project.id / item["path"]
        for item in first_manifest.samples
    ]
    first_hashes = [item["sha256"] for item in first_manifest.samples]
    second = service.analyze(project.id, execution.id, artifact.id)

    assert first.analysis_id != second.analysis_id
    assert first.frame_manifest_id != second.frame_manifest_id
    assert len(repository.list_vision_analyses(project.id, execution.id)) == 2
    assert len(repository.list_vision_frame_manifests(project.id, execution.id)) == 2
    assert all(path.is_file() for path in first_paths)
    assert first_hashes == [service._sha256(path) for path in first_paths]


def test_vision_uses_exact_runtime_plan_reference_order_and_generation_brief(tmp_path):
    repository, project, job, snapshot, plan, brief, ordered_bindings = (
        _frozen_seedance_context(tmp_path)
    )
    ProductionService(repository).create_production_shots(project.id, job.id)
    execution, _attempt = ProductionExecutionService(
        repository
    ).enqueue_shot_execution_with_attempt(
        project.id,
        job.id,
        snapshot,
        runtime_plan_id=plan.id,
        generation_brief_id=brief.id,
    )
    relative_path = f"production/{execution.id}/shot.mp4"
    target = repository.paths.projects / project.id / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=25:d=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    artifact = ProductionExecutionService(repository).record_artifact(
        project.id,
        execution.id,
        "video",
        relative_path,
        {"mime_type": "video/mp4", "shot_id": "shot_001"},
    )

    class CapturingVisionProvider(DeterministicMockVisionProvider):
        def __init__(self):
            super().__init__()
            self.request = None

        def analyze(self, *, request):
            self.request = request
            return super().analyze(request=request)

    provider = CapturingVisionProvider()
    result = VisionQCService(repository, provider=provider).analyze(
        project.id, execution.id, artifact.id
    )

    assert result.status == "AI_ANALYSIS"
    assert provider.request.reference_version_ids == plan.reference_version_ids
    assert [item.role for item in provider.request.references] == ordered_bindings
    assert provider.request.generation_brief_hash == brief.sha256
    assert provider.request.creative_context["content"]["action"] == brief.action
    record = repository.get_vision_analysis(result.analysis_id)
    assert record.reference_version_ids == plan.reference_version_ids
    ledger = repository.list_ai_invocations(project.id, execution.id)
    assert all(item.runtime_plan_id == plan.id for item in ledger)
    assert all(item.runtime_plan_hash == plan.plan_hash for item in ledger)
