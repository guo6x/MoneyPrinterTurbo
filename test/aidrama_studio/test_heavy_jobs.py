from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    FinalAssemblyRenderAttemptStatus,
    FinalAssemblyStatus,
    HeavyJobStatus,
    HeavyJobType,
)
from aidrama_studio.services import (
    FinalAssemblyService,
    HeavyJobRunner,
    HeavyJobService,
    HeavyJobServiceError,
    LargeMediaExportError,
    LargeMediaExportService,
)
from aidrama_studio.services.active_work import project_has_active_work
from aidrama_studio.storage.database import DatabasePaths, connect
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import context as _execution_context
from test.aidrama_studio.test_final_assembly import _shots, _source


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def test_migration_028_has_ordered_tables_constraints_and_immutable_events(context):
    repository, project = context
    with connect(repository.paths.database) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert version == 28
    assert {"heavy_jobs", "heavy_job_events"} <= tables

    job = HeavyJobService(repository).enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"source": "frozen", "cancel_supported": True},
        idempotency_key="migration-constraints",
    )
    with connect(repository.paths.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE heavy_jobs SET input_snapshot_json='{}' WHERE id=?",
                (job.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE heavy_job_events SET stage='tampered' WHERE heavy_job_id=?",
                (job.id,),
            )


def test_enqueue_is_durable_idempotent_and_project_scoped(context):
    repository, project = context
    service = HeavyJobService(repository)
    snapshot = {"source": "asset.mp4", "cancel_supported": True}
    first = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        snapshot,
        idempotency_key="same-command",
    )
    second = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        snapshot,
        idempotency_key="same-command",
    )
    assert second.id == first.id
    assert second.input_sha256 == first.input_sha256
    assert second.input_snapshot == snapshot
    assert [event.event_type.value for event in service.list_events(project.id, first.id)] == [
        "QUEUED"
    ]
    with pytest.raises(ValueError, match="不同输入"):
        service.enqueue(
            HeavyJobType.UPSCALE,
            project.id,
            {"source": "different.mp4"},
            idempotency_key="same-command",
        )
    with pytest.raises(HeavyJobServiceError, match="不属于"):
        service.list_events("another-project", first.id)


def test_runner_claims_once_records_truthful_progress_and_append_only_history(context):
    repository, project = context
    service = HeavyJobService(repository)
    job = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"cancel_supported": True},
        idempotency_key="runner-success",
    )
    calls: list[str] = []

    def handler(claimed, context):
        calls.append(claimed.id)
        context.stage("WAITING_FOR_RUNTIME")
        context.stage("ENCODING", current=2, total=8, unit="frames")
        return {"artifact": "delivery/output.mp4"}

    runner = HeavyJobRunner(repository, handlers={HeavyJobType.UPSCALE: handler})
    result = runner.run_once(project.id)
    assert result is not None and result.status is HeavyJobStatus.SUCCEEDED
    assert result.progress == 100
    assert calls == [job.id]
    assert runner.run_once(project.id) is None
    events = service.list_events(project.id, job.id)
    assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type.value for event in events] == [
        "QUEUED",
        "STARTED",
        "STAGE",
        "STAGE",
        "PROGRESS",
        "SUCCEEDED",
    ]
    waiting = next(event for event in events if event.stage == "WAITING_FOR_RUNTIME")
    encoding = next(event for event in events if event.stage == "ENCODING")
    assert waiting.progress is None
    assert encoding.progress == 25
    assert encoding.payload == {
        "progress_current": 2,
        "progress_total": 8,
        "progress_unit": "frames",
    }


def test_failure_is_safe_and_retry_creates_new_job_with_same_frozen_input(context):
    repository, project = context
    service = HeavyJobService(repository)
    original = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"source": "same", "cancel_supported": False},
        idempotency_key="runner-failure",
    )

    def fail(_job, _context):
        raise RuntimeError(r"D:\private\movie.mp4 API_KEY=secret-value")

    failed = HeavyJobRunner(
        repository, handlers={HeavyJobType.UPSCALE: fail}
    ).run_once(project.id)
    assert failed is not None and failed.status is HeavyJobStatus.FAILED
    assert "secret-value" not in (failed.safe_error or "")
    assert "D:\\private" not in (failed.safe_error or "")

    retried = service.retry(original.id)
    assert retried.id != original.id
    assert retried.retry_of_job_id == original.id
    assert retried.input_snapshot == original.input_snapshot
    assert retried.input_sha256 == original.input_sha256
    assert retried.status is HeavyJobStatus.QUEUED


def test_cold_recovery_marks_running_interrupted_and_active_work_is_fail_closed(context):
    repository, project = context
    service = HeavyJobService(repository)
    job = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"cancel_supported": False},
        idempotency_key="cold-recovery",
    )
    claimed = repository.claim_next_heavy_job(
        started_at="2026-08-25T00:00:00+00:00",
        event_id="started-event",
        project_id=project.id,
    )
    assert claimed is not None and claimed.id == job.id
    with connect(repository.paths.database) as connection:
        assert project_has_active_work(connection, project.id) is True
    recovered = service.recover_interrupted()
    assert [item.id for item in recovered] == [job.id]
    assert recovered[0].status is HeavyJobStatus.INTERRUPTED
    assert recovered[0].finished_at
    assert service.list_events(project.id, job.id)[-1].event_type.value == "INTERRUPTED"
    with connect(repository.paths.database) as connection:
        assert project_has_active_work(connection, project.id) is False


def test_final_render_enqueue_binds_attempt_and_recovery_closes_child_state(context):
    repository, project = context
    production_job, shots = _shots(repository, project, 1)
    _source(repository, project, production_job, shots[0], suffix="1")
    assembly = FinalAssemblyService(repository).create_assembly(
        project.id, production_job.id, freeze=True
    )
    service = HeavyJobService(repository)
    job = service.enqueue_final_assembly(project.id, assembly.id)
    attempts = repository.list_final_assembly_render_attempts(assembly.id)
    assert len(attempts) == 1
    assert attempts[0].heavy_job_id == job.id
    assert attempts[0].status is FinalAssemblyRenderAttemptStatus.PENDING
    repository.claim_next_heavy_job(
        started_at="2026-08-25T00:00:00+00:00",
        event_id="final-started",
        project_id=project.id,
    )
    repository.update_final_assembly_render_attempt(
        attempts[0].id,
        status=FinalAssemblyRenderAttemptStatus.RUNNING,
        started_at="2026-08-25T00:00:01+00:00",
    )
    repository.update_final_assembly_status(
        assembly.id,
        FinalAssemblyStatus.ASSEMBLING,
        updated_at="2026-08-25T00:00:01+00:00",
    )
    recovered = service.recover_interrupted()[0]
    assert recovered.status is HeavyJobStatus.INTERRUPTED
    interrupted_attempt = repository.get_final_assembly_render_attempt(attempts[0].id)
    assert interrupted_attempt.status is FinalAssemblyRenderAttemptStatus.FAILED
    assert repository.get_final_assembly(assembly.id).status is FinalAssemblyStatus.FAILED
    retry = service.retry(job.id)
    retry_attempts = repository.list_final_assembly_render_attempts(assembly.id)
    assert retry.retry_of_job_id == job.id
    assert len(retry_attempts) == 2
    assert retry_attempts[-1].heavy_job_id == retry.id
    assert retry_attempts[-1].status is FinalAssemblyRenderAttemptStatus.PENDING


def test_queued_cancel_is_real_and_running_unsupported_cancel_is_rejected(context):
    repository, project = context
    service = HeavyJobService(repository)
    queued = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"cancel_supported": True},
        idempotency_key="cancel-before-start",
    )
    cancelled = service.request_cancel(project.id, queued.id)
    assert cancelled.status is HeavyJobStatus.CANCELLED
    assert HeavyJobRunner(repository, handlers={}).run_once(project.id) is None

    running = service.enqueue(
        HeavyJobType.UPSCALE,
        project.id,
        {"cancel_supported": False},
        idempotency_key="cannot-cancel-runtime",
    )
    repository.claim_next_heavy_job(
        started_at="2026-08-25T00:00:00+00:00",
        event_id="running-event",
        project_id=project.id,
    )
    with pytest.raises(HeavyJobServiceError, match="不支持安全"):
        service.request_cancel(project.id, running.id)


def test_large_media_export_is_chunked_unicode_safe_and_preserves_canonical(context, tmp_path):
    repository, project = context
    payload = (b"0123456789abcdef" * 200_000) + b"tail"
    relative = "final/Chinese Project (final)/episode.mp4"
    source = repository.paths.projects / project.id / Path(relative)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    destination = tmp_path / "交付 文件 (最终).mp4"
    progress: list[tuple[int, int]] = []
    service = LargeMediaExportService(repository)
    result = service.copy(
        project.id,
        source_relative_path=relative,
        source_sha256=digest,
        source_size_bytes=len(payload),
        destination=destination,
        operation_id="unicode-copy",
        progress=lambda current, total: progress.append((current, total)),
    )
    assert destination.read_bytes() == payload
    assert source.read_bytes() == payload
    assert result["sha256"] == digest
    assert result["canonical_source_preserved"] is True
    assert len(progress) >= 2
    assert progress[-1] == (len(payload), len(payload))
    with pytest.raises(LargeMediaExportError, match="不会覆盖"):
        service.copy(
            project.id,
            source_relative_path=relative,
            source_sha256=digest,
            source_size_bytes=len(payload),
            destination=destination,
            operation_id="no-overwrite",
        )


def test_cancelled_large_media_export_removes_partial_and_never_creates_delivery(context, tmp_path):
    repository, project = context
    payload = b"video" * 1000
    relative = "final/cancelled.mp4"
    source = repository.paths.projects / project.id / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    destination = tmp_path / "cancelled.mp4"
    with pytest.raises(LargeMediaExportError, match="取消"):
        LargeMediaExportService(repository).copy(
            project.id,
            source_relative_path=relative,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_size_bytes=len(payload),
            destination=destination,
            operation_id="cancelled",
            cancelled=lambda: True,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_project_export_and_import_run_through_same_durable_runner(tmp_path):
    source_repository, source_project = _execution_context.__wrapped__(
        tmp_path / "source"
    )
    source_file = source_repository.paths.projects / source_project.id / "素材 (中文).txt"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("一致快照", encoding="utf-8")
    archive = tmp_path / "交付 backups" / "项目 (V1).aidrama"
    archive.parent.mkdir(parents=True)
    source_service = HeavyJobService(source_repository)
    export_job = source_service.enqueue_project_export(
        source_project.id, destination=archive
    )
    exported = HeavyJobRunner(source_repository).run_once(source_project.id)
    assert exported is not None and exported.id == export_job.id
    assert exported.status is HeavyJobStatus.SUCCEEDED
    assert archive.is_file()

    target_root = tmp_path / "target"
    target_repository = ProjectRepository(
        DatabasePaths(
            target_root / "db" / "aidrama.db",
            target_root / "projects",
            target_root / "archived",
        )
    )
    staging_root = target_repository.paths.archived_projects / "import-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / archive.name
    shutil.copyfile(archive, staged)
    payload_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    target_service = HeavyJobService(target_repository)
    import_job = target_service.enqueue_project_import(
        staged_archive_relative_path=staged.relative_to(
            target_repository.paths.archived_projects
        ).as_posix(),
        archive_sha256=payload_hash,
        archive_size_bytes=staged.stat().st_size,
    )
    imported = HeavyJobRunner(target_repository).run_once()
    assert imported is not None and imported.id == import_job.id
    assert imported.status is HeavyJobStatus.SUCCEEDED, imported.safe_error
    restored_id = str(imported.output_provenance["imported_project_id"])
    assert restored_id == source_project.id
    assert target_repository.get_project(restored_id) is not None
    assert (
        target_repository.paths.projects / restored_id / "素材 (中文).txt"
    ).read_text(encoding="utf-8") == "一致快照"
