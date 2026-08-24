from __future__ import annotations

import json

import pytest

from aidrama_studio.domain import ProductionQCStatus, ProductionReviewDecision
from aidrama_studio.services import ProductionExecutionService, ProductionQCService
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def _artifact_context(context, *, create_file: bool = True, **overrides):
    repository, project = context
    job = _ready_job(repository, project)
    execution_service = ProductionExecutionService(repository)
    execution = execution_service.enqueue_job(project.id, job.id)
    relative_path = f"production/{execution.id}/shot.mp4"
    target = repository.paths.projects / project.id / relative_path
    if create_file:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"valid-video")
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
        "audio_stream": True,
    }
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


def test_invalid_video_metadata_fails(context):
    repository, project, execution, artifact = _artifact_context(
        context,
        duration_seconds=0,
        resolution="not-a-resolution",
        codec="",
    )

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_FAILED
    failed_names = {metric.metric_name for metric in ProductionQCService(repository).list_metrics(project.id, result.id) if metric.status.value == "FAIL"}
    assert {"video_duration", "video_resolution", "video_codec"}.issubset(failed_names)


def test_black_frame_detection_fails(context):
    repository, project, execution, artifact = _artifact_context(context, black_frame_detected=True)

    result = ProductionQCService(repository).run_qc(project.id, execution.id, artifact.id)

    assert result.status is ProductionQCStatus.QC_FAILED
    metric = next(metric for metric in ProductionQCService(repository).list_metrics(project.id, result.id) if metric.metric_name == "black_frame")
    assert metric.status.value == "FAIL"


def test_missing_audio_stream_fails(context):
    repository, project, execution, artifact = _artifact_context(context, audio_stream=False)

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
    target.write_bytes(b"valid-video")
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
    with connect(repository.paths.database) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions[-1] == 9
        for table in ("production_qc_results", "production_qc_metrics", "production_reviews"):
            assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
