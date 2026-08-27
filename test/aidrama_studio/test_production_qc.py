from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from aidrama_studio.domain import ProductionQCStatus, ProductionReviewDecision
from aidrama_studio.services import ProductionExecutionService, ProductionQCService
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def _artifact_context(
    context,
    *,
    create_file: bool = True,
    source_filter: str | None = None,
    provider_reference_subset: bool = False,
    **overrides,
):
    repository, project = context
    job = _ready_job(repository, project)
    execution_service = ProductionExecutionService(repository)
    execution = execution_service.enqueue_job(project.id, job.id)
    relative_path = f"production/{execution.id}/shot.mp4"
    target = repository.paths.projects / project.id / relative_path
    if create_file:
        target.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        source = source_filter or (
            "color=c=black:s=160x120:d=1"
            if overrides.get("black_frame_detected")
            else "testsrc=size=160x120:rate=25:d=1"
        )
        completed = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", source, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            pytest.fail(completed.stderr[-1000:])
    metadata = {
        "execution_id": execution.id,
        "shot_id": "shot_001",
        "reference_versions": dict(execution.input_snapshot.reference_asset_versions),
        "mime_type": "video/mp4",
        "duration_seconds": 2.0,
        "resolution": "1920x1080",
        "codec": "h264",
        "black_frame_detected": False,
        "static_frame_detected": False,
        "audio_required": False,
    }
    if provider_reference_subset:
        binding_key, version_id = next(iter(execution.input_snapshot.reference_asset_versions.items()))
        metadata.update(
            {
                "reference_versions": {binding_key: version_id},
                "snapshot_references_available": dict(
                    execution.input_snapshot.reference_asset_versions
                ),
                "provider_references_actually_used": [
                    {
                        "binding_key": binding_key,
                        "reference_asset_version_id": version_id,
                    }
                ],
            }
        )
    metadata.update(overrides)
    artifact = execution_service.record_artifact(project.id, execution.id, "video", relative_path, metadata)
    return repository, project, execution, artifact


def test_valid_artifact_passes_and_persists_report_and_metrics(context):
    repository, project, execution, artifact = _artifact_context(context)
    service = ProductionQCService(repository)

    result = service.run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_PASS
    assert result.report_path == f"production/{execution.id}/qc/qc_report.json"
    report_path = repository.paths.projects / project.id / result.report_path
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["execution_id"] == execution.id
    assert report["status"] == "QC_PASS"
    metrics = service.list_metrics(project.id, result.id)
    assert {metric.metric_name for metric in metrics} >= {
        "artifact_exists", "artifact_size", "supported_media_type", "video_duration",
        "video_resolution", "video_codec", "black_frame", "static_frame", "audio_stream", "traceability",
    }
    assert all(metric.status.value in {"PASS", "SKIPPED"} for metric in metrics)


def test_missing_artifact_fails_without_crashing_and_writes_report(context):
    repository, project, execution, artifact = _artifact_context(context, create_file=False)
    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_FAILED
    assert result.report_path is not None
    assert (repository.paths.projects / project.id / result.report_path).is_file()
    metrics = ProductionQCService(repository).list_metrics(project.id, result.id)
    assert next(metric for metric in metrics if metric.metric_name == "artifact_exists").status.value == "FAIL"


def test_invalid_video_metadata_does_not_override_real_probe(context):
    repository, project, execution, artifact = _artifact_context(
        context,
        duration_seconds=0,
        resolution="not-a-resolution",
        codec="",
    )

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_PASS
    failed_names = {metric.metric_name for metric in ProductionQCService(repository).list_metrics(project.id, result.id) if metric.status.value == "FAIL"}
    assert not ({"video_duration", "video_resolution", "video_codec"} & failed_names)


def test_black_frame_detection_fails(context):
    repository, project, execution, artifact = _artifact_context(context, black_frame_detected=True)

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_FAILED
    metric = next(metric for metric in ProductionQCService(repository).list_metrics(project.id, result.id) if metric.metric_name == "black_frame")
    assert metric.status.value == "FAIL"


def test_dark_cinematic_scene_is_not_misclassified_as_black(context):
    repository, project, execution, artifact = _artifact_context(
        context,
        source_filter="color=c=0x303030:s=160x120:d=1",
    )

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    metric = next(
        metric
        for metric in ProductionQCService(repository).list_metrics(project.id, result.id)
        if metric.metric_name == "black_frame"
    )
    assert metric.status.value == "PASS"


def test_traceability_accepts_full_snapshot_with_provider_reference_subset(context):
    repository, project, execution, artifact = _artifact_context(
        context,
        provider_reference_subset=True,
    )

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_PASS
    metric = next(
        metric
        for metric in ProductionQCService(repository).list_metrics(project.id, result.id)
        if metric.metric_name == "traceability"
    )
    assert metric.status.value == "PASS"


def test_missing_audio_stream_fails_when_shot_requires_audio(context):
    repository, project, execution, artifact = _artifact_context(context, audio_required=True)

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_FAILED
    metric = next(metric for metric in ProductionQCService(repository).list_metrics(project.id, result.id) if metric.metric_name == "audio_stream")
    assert metric.status.value == "FAIL"


def test_qc_retry_creates_history_and_review_is_project_scoped(context):
    repository, project, execution, artifact = _artifact_context(context, create_file=False)
    service = ProductionQCService(repository)
    first = service.run_qc(project.id, execution.id, artifact.id)
    target = repository.paths.projects / project.id / artifact.path
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    completed = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:d=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    second = service.retry_qc(project.id, execution.id, artifact.id)

    assert first.id != second.id
    assert first.status is ProductionQCStatus.QC_FAILED
    assert second.status is ProductionQCStatus.QC_PASS
    assert first.report_path != second.report_path
    assert len(service.list_results(project.id, execution.id)) == 2
    review = service.create_review(project.id, second.id, ProductionReviewDecision.APPROVED, reviewer="qa")
    assert review.decision is ProductionReviewDecision.APPROVED
    assert len(service.list_reviews(project.id, second.id)) == 1


def test_qc_migration_is_applied_and_idempotent(context):
    repository, project = context
    from aidrama_studio.storage.database import connect
    from aidrama_studio.storage.migrations import MIGRATIONS, apply_migrations
    with connect(repository.paths.database) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        expected = [version for version, _ in MIGRATIONS]
        assert versions == expected
        assert apply_migrations(connection) == 0
        versions_after = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions_after == expected
        for table in ("production_qc_results", "production_qc_metrics", "production_reviews"):
            assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
