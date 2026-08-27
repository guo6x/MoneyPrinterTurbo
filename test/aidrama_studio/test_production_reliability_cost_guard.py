from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AspectRatio,
    FinalAssembly,
    FinalAssemblyItem,
    FinalAssemblyRenderAttemptStatus,
    FinalAssemblyStatus,
    HeavyJobType,
    ProductionArtifact,
    ProductionEvent,
    ProductionEventType,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionJob,
    ProductionJobStatus,
    ProductionShot,
    ProductionShotStatus,
    Project,
    ProjectStatus,
    ProviderTask,
)
from aidrama_studio.services.adapters import (
    ProductionRuntimeAdapter,
    RuntimeSubmission,
    RuntimeTransientError,
)
from aidrama_studio.services.adapters.final_assembly_runtime import (
    FinalAssemblyRuntimeAdapter,
)
from aidrama_studio.services.background_runner import BackgroundProductionRunner
from aidrama_studio.services.final_assembly_runtime import (
    FinalAssemblyRuntimeService,
)
from aidrama_studio.services.heavy_job_runner import HeavyJobRunner
from aidrama_studio.services.heavy_jobs import HeavyJobService
from aidrama_studio.services.production_artifact_storage import (
    ProductionArtifactStorageService,
)
from aidrama_studio.services.production_execution import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
)
from aidrama_studio.services.production_reliability import PaidBudgetService
from aidrama_studio.services.production_recovery import ProductionRecoveryService
from aidrama_studio.services.production_worker import (
    ProductionWorker,
    ProductionWorkerError,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _paths(tmp_path: Path) -> DatabasePaths:
    return DatabasePaths(
        database=tmp_path / "aidrama.db",
        projects=tmp_path / "projects",
        archived_projects=tmp_path / "archived-projects",
    )


def _seed_job(
    repository: ProjectRepository,
    *,
    project_id: str = "project-reliability",
    job_id: str = "job-reliability",
    shot_count: int = 1,
) -> tuple[str, str]:
    now = _now()
    repository.create_project(
        Project(
            id=project_id,
            title="Reliability Fixture",
            description="offline",
            status=ProjectStatus.PRODUCTION,
            aspect_ratio=AspectRatio.LANDSCAPE,
            target_duration_seconds=max(shot_count, 1),
            created_at=now,
            updated_at=now,
        )
    )
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO story_bible_revisions("
            "id,project_id,version,status,content_json,generation_input_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("story-1", project_id, 1, "APPROVED", "{}", None, now, now),
        )
        connection.execute(
            "INSERT INTO structured_script_revisions("
            "id,project_id,version,status,source_story_revision_id,content_json,"
            "generation_input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "script-1",
                project_id,
                1,
                "APPROVED",
                "story-1",
                "{}",
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO shot_plan_revisions("
            "id,project_id,version,status,source_script_revision_id,content_json,"
            "generation_input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "shot-plan-1",
                project_id,
                1,
                "APPROVED",
                "script-1",
                "{}",
                None,
                now,
                now,
            ),
        )
    repository.create_production_job(
        ProductionJob(
            id=job_id,
            project_id=project_id,
            shot_plan_revision_id="shot-plan-1",
            status=ProductionJobStatus.READY,
            created_at=now,
            updated_at=now,
        )
    )
    for index in range(shot_count):
        repository.create_production_shot(
            ProductionShot(
                id=f"production-shot-{index + 1:02d}",
                production_job_id=job_id,
                shot_id=f"shot-{index + 1:02d}",
                order_index=index,
                status=ProductionShotStatus.PENDING,
                created_at=now,
            )
        )
    return project_id, job_id


def _new_execution(
    repository: ProjectRepository,
    project_id: str,
    job_id: str,
    index: int,
) -> ProductionExecution:
    now = _now()
    execution = ProductionExecution(
        id=f"execution-{index:02d}",
        production_job_id=job_id,
        status=ProductionExecutionStatus.QUEUED,
        worker_type="fake-paid-provider",
        created_at=now,
        input_snapshot=ProductionInputSnapshot(
            project_id=project_id,
            story_revision_id="story-1",
            script_revision_id="script-1",
            shot_plan_revision_id="shot-plan-1",
            shot_parameters={
                f"shot-{index:02d}": {"order": index, "duration_seconds": 1}
            },
        ),
    )
    return repository.enqueue_production_execution_atomic(
        execution,
        job_status=ProductionJobStatus.QUEUED,
        event=ProductionEvent(
            id=f"queued-{index:02d}",
            execution_id=execution.id,
            event_type=ProductionEventType.QUEUED,
            payload_json={"shot_id": f"shot-{index:02d}", "attempt_id": f"attempt-{index:02d}"},
            created_at=now,
        ),
    )


def _authorize(
    repository: ProjectRepository,
    project_id: str,
    job_id: str,
    maximum: int,
) -> None:
    PaidBudgetService(repository).authorize_job(
        project_id,
        job_id,
        authorization_fingerprint="a" * 64,
        planned_creates=maximum,
        authorized_max=maximum,
    )


@dataclass
class _FakeProviderBackend:
    submit_calls: int = 0
    poll_calls: int = 0
    result_calls: int = 0
    poll_outcomes: list[object] = field(default_factory=list)
    download_failures: int = 0
    submit_error: Exception | None = None


class _FakePaidAdapter(ProductionRuntimeAdapter):
    name = "fake-paid-provider"
    provider_id = "fake-paid-provider"
    model_id = "fake-v1"
    requires_paid_budget = True
    poll_interval_seconds = 0.0

    def __init__(self, backend: _FakeProviderBackend) -> None:
        self.backend = backend

    def validate(self, snapshot) -> bool:
        return True

    def submit(self, snapshot) -> RuntimeSubmission:
        self.backend.submit_calls += 1
        if self.backend.submit_error is not None:
            raise self.backend.submit_error
        return RuntimeSubmission(
            f"remote-task-{self.backend.submit_calls}",
            metadata={
                "request_id": f"request-{self.backend.submit_calls}",
                "api_key": "must-not-persist",
                "signed_url": "https://provider.invalid/result?sig=secret",
            },
        )

    def get_status(self, runtime_reference: str) -> str:
        self.backend.poll_calls += 1
        if self.backend.poll_outcomes:
            outcome = self.backend.poll_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)
        return "SUCCEEDED"

    def get_result(self, runtime_reference: str):
        self.backend.result_calls += 1
        if self.backend.download_failures > 0:
            self.backend.download_failures -= 1
            raise TimeoutError(
                "download https://provider.invalid/result?sig=secret "
                "Authorization: Bearer raw-secret"
            )
        return {
            "artifacts": [
                {
                    "artifact_type": "video",
                    "filename": "shot.mp4",
                    "content": b"deterministic-fake-video",
                    "metadata": {
                        "mime_type": "video/mp4",
                        "signed_result_url": "https://provider.invalid/result?sig=secret",
                    },
                }
            ]
        }


def test_crash_after_create_restarts_with_same_remote_task(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend()

    started = ProductionExecutionService(repository).submit_execution(
        project_id, execution.id, _FakePaidAdapter(backend)
    )
    assert started.status is ProductionExecutionStatus.RUNNING
    assert backend.submit_calls == 1

    # Destroy every local service/repository and reopen the same SQLite file.
    fresh_repository = ProjectRepository(paths)
    background = BackgroundProductionRunner(
        fresh_repository,
        adapter_factory=lambda _task: _FakePaidAdapter(backend),
        worker_factory=lambda: ProductionWorker(
            ProductionExecutionService(fresh_repository),
            max_polls=1,
        ),
    )
    summary = ProductionRecoveryService(
        fresh_repository, background_runner=background
    ).resume_pending_work(project_id)
    result = fresh_repository.get_production_execution(execution.id)

    assert summary["production_results"]
    assert result is not None
    assert result.status is ProductionExecutionStatus.SUCCEEDED
    assert backend.submit_calls == 1
    task = [
        item
        for item in fresh_repository.list_provider_tasks(project_id)
        if item.execution_id == execution.id
    ][0]
    assert task.provider_task_id == "remote-task-1"
    assert "api_key" not in task.metadata
    assert "signed_url" not in task.metadata


def test_crash_after_remote_identity_persist_resumes_before_started_event(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend()

    class CrashAfterRemoteIdentity(ProductionExecutionService):
        def start_execution(self, *args, **kwargs):
            raise RuntimeError("simulated crash after remote identity persist")

    with pytest.raises(RuntimeError, match="simulated crash"):
        CrashAfterRemoteIdentity(repository).submit_execution(
            project_id, execution.id, _FakePaidAdapter(backend)
        )
    persisted = [
        item
        for item in repository.list_provider_tasks(project_id)
        if item.execution_id == execution.id
    ][0]
    assert persisted.provider_task_id == "remote-task-1"
    assert persisted.state == "PROVIDER_ACCEPTED"
    assert backend.submit_calls == 1

    fresh_repository = ProjectRepository(paths)
    background = BackgroundProductionRunner(
        fresh_repository,
        adapter_factory=lambda _task: _FakePaidAdapter(backend),
        worker_factory=lambda: ProductionWorker(
            ProductionExecutionService(fresh_repository),
            max_polls=1,
        ),
    )
    ProductionRecoveryService(
        fresh_repository, background_runner=background
    ).resume_pending_work(project_id)

    recovered = fresh_repository.get_production_execution(execution.id)
    assert recovered is not None
    assert recovered.status is ProductionExecutionStatus.SUCCEEDED
    assert backend.submit_calls == 1


def test_download_failure_restart_reuses_task_and_artifact_identity(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend(download_failures=1)

    first = ProductionWorker(
        ProductionExecutionService(repository),
        _FakePaidAdapter(backend),
        max_polls=1,
    ).run(project_id, execution.id)
    assert first.status is ProductionExecutionStatus.RUNNING
    assert backend.submit_calls == 1

    fresh_repository = ProjectRepository(paths)
    second = ProductionWorker(
        ProductionExecutionService(fresh_repository),
        _FakePaidAdapter(backend),
        max_polls=1,
    ).resume(project_id, execution.id)
    assert second.status is ProductionExecutionStatus.SUCCEEDED
    assert backend.submit_calls == 1
    artifacts = fresh_repository.list_production_artifacts(execution.id)
    assert len(artifacts) == 1
    assert "signed_result_url" not in artifacts[0].metadata_json


def test_poll_timeout_restart_polls_same_task_without_create(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend(
        poll_outcomes=[RuntimeTransientError("poll timeout")]
    )

    first = ProductionWorker(
        ProductionExecutionService(repository),
        _FakePaidAdapter(backend),
        max_polls=1,
    ).run(project_id, execution.id)
    assert first.status is ProductionExecutionStatus.RUNNING

    fresh_repository = ProjectRepository(paths)
    second = ProductionWorker(
        ProductionExecutionService(fresh_repository),
        _FakePaidAdapter(backend),
        max_polls=1,
    ).resume(project_id, execution.id)
    assert second.status is ProductionExecutionStatus.SUCCEEDED
    assert backend.submit_calls == 1


def test_uncertain_create_fails_closed_and_redacts_diagnostics(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend(
        submit_error=TimeoutError(
            "POST https://provider.invalid/create?sig=raw-signature "
            "Authorization: Bearer raw-secret"
        )
    )

    with pytest.raises(ProductionExecutionServiceError, match="UNCERTAIN_CREATE"):
        ProductionExecutionService(repository).submit_execution(
            project_id, execution.id, _FakePaidAdapter(backend)
        )
    with pytest.raises(ProductionExecutionServiceError, match="UNCERTAIN_CREATE"):
        ProductionExecutionService(ProjectRepository(paths)).submit_execution(
            project_id, execution.id, _FakePaidAdapter(backend)
        )
    with pytest.raises(ProductionWorkerError, match="UNCERTAIN_CREATE"):
        ProductionWorker(
            ProductionExecutionService(ProjectRepository(paths)),
            _FakePaidAdapter(backend),
        ).reconcile(project_id, execution.id)

    assert backend.submit_calls == 1
    fresh_repository = ProjectRepository(paths)
    task = [
        item
        for item in fresh_repository.list_provider_tasks(project_id)
        if item.execution_id == execution.id
    ][0]
    assert task.state == "UNCERTAIN_CREATE"
    assert "raw-secret" not in str(task.error_message)
    assert "raw-signature" not in str(task.error_message)
    assert "sig=" not in str(task.error_message)
    projection = PaidBudgetService(fresh_repository).projection(
        project_id, job_id
    )
    assert projection.uncertain_creates == 1
    assert projection.remaining_creates == 0


def test_authorized_eight_hard_blocks_ninth_before_transport(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(_paths(tmp_path))
    project_id, job_id = _seed_job(repository, shot_count=8)
    _authorize(repository, project_id, job_id, 8)
    backend = _FakeProviderBackend()
    service = ProductionExecutionService(repository)

    for index in range(1, 9):
        execution = _new_execution(repository, project_id, job_id, index)
        service.submit_execution(project_id, execution.id, _FakePaidAdapter(backend))
    ninth = _new_execution(repository, project_id, job_id, 9)
    with pytest.raises(ProductionExecutionServiceError, match="PAID_BUDGET_EXHAUSTED"):
        service.submit_execution(project_id, ninth.id, _FakePaidAdapter(backend))

    assert backend.submit_calls == 8
    projection = PaidBudgetService(repository).projection(project_id, job_id)
    assert projection.authorized_max == 8
    assert projection.consumed_creates == 8
    assert projection.reserved_creates == 0
    assert projection.uncertain_creates == 0
    assert projection.remaining_creates == 0
    execution_projection = PaidBudgetService(repository).projection(
        project_id, job_id, execution_id=ninth.id
    )
    assert execution_projection.remaining_creates == 0


def test_double_click_and_double_step_share_one_intent_and_create(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(_paths(tmp_path))
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    now = _now()
    intent = ProviderTask(
        id="ui-intent-first",
        project_id=project_id,
        capability="VIDEO_GENERATIVE",
        provider_id="fake-paid-provider",
        model_id="fake-v1",
        idempotency_key=f"production-job:{job_id}:authorization:{'a' * 64}",
        state="PREPARING",
        request_summary={"production_job_id": job_id},
        created_at=now,
        updated_at=now,
    )
    first, first_created = repository.get_or_create_provider_task(intent)
    second, second_created = repository.get_or_create_provider_task(
        intent.model_copy(update={"id": "ui-intent-double-click"})
    )
    assert first_created is True
    assert second_created is False
    assert first.id == second.id

    backend = _FakeProviderBackend()
    service = ProductionExecutionService(repository)
    service.submit_execution(project_id, execution.id, _FakePaidAdapter(backend))
    with pytest.raises(ProductionExecutionServiceError):
        service.submit_execution(project_id, execution.id, _FakePaidAdapter(backend))
    assert backend.submit_calls == 1


def test_repeated_provider_result_has_one_content_addressed_artifact(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(_paths(tmp_path))
    project_id, job_id = _seed_job(repository)
    execution = _new_execution(repository, project_id, job_id, 1)
    _authorize(repository, project_id, job_id, 1)
    backend = _FakeProviderBackend()
    execution_service = ProductionExecutionService(repository)
    execution_service.submit_execution(
        project_id, execution.id, _FakePaidAdapter(backend)
    )
    worker = ProductionWorker(
        execution_service,
        _FakePaidAdapter(backend),
        artifact_storage=ProductionArtifactStorageService(repository),
    )
    spec = {
        "artifact_type": "video",
        "filename": "same.mp4",
        "content": b"same-provider-result",
        "metadata": {"mime_type": "video/mp4"},
    }
    worker._persist_artifact(project_id, execution.id, spec, "remote-task-1")
    repeated = {
        **spec,
        "filename": "same-provider-bytes.webm",
        "metadata": {"mime_type": "video/webm"},
    }
    worker._persist_artifact(
        project_id, execution.id, repeated, "remote-task-1"
    )

    artifacts = repository.list_production_artifacts(execution.id)
    assert len(artifacts) == 1
    execution_root = repository.paths.projects / project_id / "production" / execution.id
    assert [path for path in execution_root.iterdir() if path.is_file()] == [
        execution_root / Path(artifacts[0].path).name
    ]


class _FakeFinalAdapter(FinalAssemblyRuntimeAdapter):
    name = "mpt-media-concat"

    def validate_sources(self, request):
        return [{"path": path.name} for path in request.source_paths]

    def render(self, request, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"one-deterministic-final-artifact")

    def probe_output(self, output_path: Path) -> dict[str, object]:
        is_final = ".in-progress.mp4" in output_path.name
        return {
            "video_stream": True,
            "audio_stream": False,
            "size_bytes": output_path.stat().st_size,
            "width": 1280,
            "height": 720,
            "resolution": "1280x720",
            "fps": 24.0,
            "duration_seconds": 12.0 if is_final else 1.0,
        }


def _seed_twelve_shot_assembly(
    repository: ProjectRepository,
) -> tuple[str, str, str, tuple[str, ...]]:
    project_id, job_id = _seed_job(repository, shot_count=12)
    now = _now()
    project_root = repository.paths.projects / project_id
    assembly = repository.create_final_assembly(
        FinalAssembly(
            id="assembly-12",
            project_id=project_id,
            production_job_id=job_id,
            status=FinalAssemblyStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
    )
    item_ids: list[str] = []
    for index in range(12):
        ordinal = index + 1
        execution = ProductionExecution(
            id=f"accepted-execution-{ordinal:02d}",
            production_job_id=job_id,
            status=ProductionExecutionStatus.SUCCEEDED,
            worker_type="synthetic",
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        repository.create_production_execution(execution)
        relative = f"accepted/shot-{ordinal:02d}.mp4"
        source = project_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"accepted-shot-{ordinal:02d}".encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        artifact = repository.create_production_artifact(
            ProductionArtifact(
                id=f"accepted-artifact-{ordinal:02d}",
                execution_id=execution.id,
                artifact_type="video",
                path=relative,
                metadata_json={
                    "sha256": digest,
                    "mime_type": "video/mp4",
                    "duration_seconds": 1.0,
                },
                created_at=now,
            )
        )
        qc_id = f"accepted-qc-{ordinal:02d}"
        with repository.transaction() as connection:
            connection.execute(
                "INSERT INTO production_qc_results("
                "id,project_id,execution_id,artifact_id,status,report_path,"
                "summary_json,started_at,finished_at,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    qc_id,
                    project_id,
                    execution.id,
                    artifact.id,
                    "QC_PASS",
                    None,
                    "{}",
                    now,
                    now,
                    now,
                ),
            )
        item_id = f"assembly-item-{ordinal:02d}"
        repository.create_final_assembly_item(
            FinalAssemblyItem(
                id=item_id,
                final_assembly_id=assembly.id,
                order_index=index,
                production_shot_id=f"production-shot-{ordinal:02d}",
                production_execution_id=execution.id,
                production_artifact_id=artifact.id,
                qc_result_id=qc_id,
                source_path=relative,
                source_sha256=digest,
                source_duration_seconds=1.0,
                timeline_start_seconds=float(index),
                timeline_end_seconds=float(ordinal),
                timeline_duration_seconds=1.0,
                duration_strategy="NONE",
                created_at=now,
            )
        )
        item_ids.append(item_id)
    repository.update_final_assembly_status(
        assembly.id, FinalAssemblyStatus.READY, updated_at=now
    )
    repository.update_production_job_status(
        job_id, ProductionJobStatus.SUCCEEDED, updated_at=now
    )
    return project_id, job_id, assembly.id, tuple(item_ids)


def test_final_assembly_crash_recovery_reuses_twelve_frozen_sources(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    repository = ProjectRepository(paths)
    project_id, _job_id, assembly_id, item_ids = _seed_twelve_shot_assembly(
        repository
    )
    heavy = HeavyJobService(repository).enqueue_final_assembly(
        project_id, assembly_id
    )
    claimed = repository.claim_next_heavy_job(
        started_at=_now(), event_id="heavy-started"
    )
    assert claimed is not None and claimed.id == heavy.id
    attempt_id = str(heavy.input_snapshot["attempt_id"])
    repository.update_final_assembly_render_attempt(
        attempt_id,
        status=FinalAssemblyRenderAttemptStatus.RUNNING,
        started_at=_now(),
    )
    repository.update_final_assembly_status(
        assembly_id, FinalAssemblyStatus.ASSEMBLING, updated_at=_now()
    )

    # Simulate a Windows restart: reopen DB and build fresh runner/services.
    fresh_repository = ProjectRepository(paths)

    def render_final(job, context):
        context.stage("RECOVERING_FROZEN_MANIFEST")
        snapshot = job.input_snapshot
        attempt = FinalAssemblyRuntimeService(
            fresh_repository, adapter=_FakeFinalAdapter()
        ).render_prepared(
            str(job.project_id),
            str(snapshot["assembly_id"]),
            str(snapshot["attempt_id"]),
        )
        return {
            "attempt_id": attempt.id,
            "output_relative_path": attempt.output_relative_path,
        }

    summary = HeavyJobRunner(
        fresh_repository,
        handlers={HeavyJobType.FINAL_ASSEMBLY_RENDER: render_final},
    ).resume_pending_work(project_id)

    assert len(summary["interrupted"]) == 1
    assert len(summary["recovered"]) == 1
    manifest = FinalAssemblyRuntimeService(
        fresh_repository, adapter=_FakeFinalAdapter()
    ).manifest_service.get_manifest(project_id, assembly_id)
    assert tuple(item.id for item in manifest.items) == item_ids
    attempts = fresh_repository.list_final_assembly_render_attempts(assembly_id)
    successful = [
        item
        for item in attempts
        if item.status is FinalAssemblyRenderAttemptStatus.SUCCEEDED
    ]
    assert len(successful) == 1
    assert not any(
        item.status
        in {
            FinalAssemblyRenderAttemptStatus.PENDING,
            FinalAssemblyRenderAttemptStatus.RUNNING,
        }
        for item in attempts
    )
    final_path = (
        fresh_repository.paths.projects
        / project_id
        / str(successful[0].output_relative_path)
    )
    assert final_path.is_file() and final_path.stat().st_size > 0
    assert fresh_repository.get_final_assembly(assembly_id).status is FinalAssemblyStatus.SUCCEEDED
    assert fresh_repository.list_provider_tasks(project_id) == []
