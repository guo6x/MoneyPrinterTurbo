from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from aidrama_studio.domain import (
    FinalAssemblyStatus,
    ProductionArtifact,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
    ProductionShot,
    ProductionShotStatus,
)
from aidrama_studio.services import FinalAssemblyService, FinalAssemblyServiceError
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def _shots(repository: ProjectRepository, project, count: int = 3):
    job = _ready_job(repository, project)
    existing = repository.list_production_shots(job.id)
    for index in range(len(existing) + 1, count + 1):
        repository.create_production_shot(
            ProductionShot(
                id=uuid4().hex,
                production_job_id=job.id,
                shot_id=f"shot_{index:03d}",
                order_index=index,
                status=ProductionShotStatus.PENDING,
                created_at=f"2025-01-01T00:00:0{index}+00:00",
            )
        )
    return job, sorted(repository.list_production_shots(job.id), key=lambda item: item.order_index)


def _source(repository, project, job, shot, *, suffix: str, execution_status=ProductionExecutionStatus.SUCCEEDED,
            qc_status=ProductionQCStatus.QC_PASS, create_file: bool = True, review: str | None = None,
            created_at: str | None = None, artifact_type: str = "video", path: str | None = None):
    execution_id = uuid4().hex
    execution = ProductionExecution(
        id=execution_id,
        production_job_id=job.id,
        status=execution_status,
        worker_type="test",
        created_at=created_at or f"2025-01-01T00:00:{suffix}+00:00",
        input_snapshot=ProductionInputSnapshot(
            project_id=project.id,
            story_revision_id="story_001",
            script_revision_id="script_001",
            shot_plan_revision_id=job.shot_plan_revision_id,
            reference_asset_versions={},
            shot_parameters={shot.shot_id: {"duration_seconds": 2}},
        ),
    )
    repository.create_production_execution(execution)
    relative_path = path or f"production/{execution.id}/{suffix}.mp4"
    target = repository.paths.projects / project.id / relative_path
    if create_file:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-bytes")
    artifact = repository.create_production_artifact(
        ProductionArtifact(
            id=uuid4().hex,
            execution_id=execution.id,
            artifact_type=artifact_type,
            path=relative_path,
            metadata_json={
                "mime_type": "video/mp4",
                "duration_seconds": 2,
                "resolution": "1280x720",
                "codec": "h264",
                "audio_stream": True,
            },
            created_at=execution.created_at or "2025-01-01T00:00:00+00:00",
        )
    )
    result = repository.create_production_qc_result(
        ProductionQCResult(
            id=uuid4().hex,
            project_id=project.id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            status=qc_status,
            created_at=execution.created_at or "2025-01-01T00:00:00+00:00",
        )
    )
    review_model = None
    if review is not None:
        review_model = repository.create_production_review(
            ProductionReview(
                id=uuid4().hex,
                project_id=project.id,
                qc_result_id=result.id,
                decision=review,
                reviewer="qa",
                created_at=execution.created_at or "2025-01-01T00:00:00+00:00",
            )
        )
    return execution, artifact, result, review_model


def test_freezes_ordered_three_shot_manifest_with_exact_provenance(context):
    repository, project = context
    job, shots = _shots(repository, project, 3)
    sources = [_source(repository, project, job, shot, suffix=str(index)) for index, shot in enumerate(shots, 1)]

    service = FinalAssemblyService(repository)
    readiness = service.calculate_readiness(project.id, job.id)
    assert readiness.ready
    assert readiness.total_shots == readiness.eligible_shots == 3
    assembly = service.create_assembly(project.id, job.id)
    frozen = service.freeze_manifest(project.id, assembly.id)
    manifest = service.get_manifest(project.id, assembly.id)

    assert frozen.status is FinalAssemblyStatus.READY
    assert [item.order_index for item in manifest.items] == [0, 1, 2]
    assert [item.production_shot_id for item in manifest.items] == [shot.id for shot in shots]
    assert [item.production_execution_id for item in manifest.items] == [item[0].id for item in sources]
    assert [item.production_artifact_id for item in manifest.items] == [item[1].id for item in sources]
    assert [item.qc_result_id for item in manifest.items] == [item[2].id for item in sources]


@pytest.mark.parametrize(
    "execution_status,qc_status,review,create_file,reason",
    [
        (ProductionExecutionStatus.FAILED, ProductionQCStatus.QC_PASS, None, True, "SUCCEEDED"),
        (ProductionExecutionStatus.SUCCEEDED, ProductionQCStatus.QC_FAILED, None, True, "QC_PASS"),
        (ProductionExecutionStatus.SUCCEEDED, ProductionQCStatus.QC_PASS, ProductionReviewDecision.REJECTED, True, "rejected"),
        (ProductionExecutionStatus.SUCCEEDED, ProductionQCStatus.QC_PASS, None, False, "source 文件不存在"),
    ],
)
def test_unqualified_sources_block_readiness(context, execution_status, qc_status, review, create_file, reason):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(repository, project, job, shots[0], suffix="1", execution_status=execution_status,
            qc_status=qc_status, review=review, create_file=create_file)

    readiness = FinalAssemblyService(repository).calculate_readiness(project.id, job.id)
    assert not readiness.ready
    assert readiness.blocked_shots == 1
    assert reason.lower() in " ".join(readiness.blocked_reasons).lower()


def test_qc_pass_without_review_and_accepted_review_are_eligible(context):
    repository, project = context
    job, shots = _shots(repository, project, 2)
    first = _source(repository, project, job, shots[0], suffix="1")
    second = _source(repository, project, job, shots[1], suffix="2", review=ProductionReviewDecision.APPROVED)
    service = FinalAssemblyService(repository)

    assert service.select_qualified_source(project.id, job.id, shots[0].id).review_id is None
    selected = service.select_qualified_source(project.id, job.id, shots[1].id)
    assert selected.review_id == second[3].id
    assert service.calculate_readiness(project.id, job.id).ready


def test_multiple_attempts_choose_latest_qualified_and_accepted_review_overrides(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    old = _source(repository, project, job, shots[0], suffix="1", created_at="2025-01-01T00:00:01+00:00")
    newer = _source(repository, project, job, shots[0], suffix="2", created_at="2025-01-01T00:00:02+00:00")
    service = FinalAssemblyService(repository)
    assert service.select_qualified_source(project.id, job.id, shots[0].id).production_execution_id == newer[0].id

    accepted_old = _source(
        repository, project, job, shots[0], suffix="3", created_at="2025-01-01T00:00:03+00:00",
        review=ProductionReviewDecision.APPROVED,
    )
    latest = _source(repository, project, job, shots[0], suffix="4", created_at="2025-01-01T00:00:04+00:00")
    assert service.select_qualified_source(project.id, job.id, shots[0].id).production_execution_id == accepted_old[0].id
    assert latest[0].id != accepted_old[0].id


def test_ready_manifest_is_immutable_and_new_retry_requires_new_assembly(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    first = _source(repository, project, job, shots[0], suffix="1", created_at="2025-01-01T00:00:01+00:00")
    service = FinalAssemblyService(repository)
    assembly = service.create_assembly(project.id, job.id, freeze=True)
    original = service.get_manifest(project.id, assembly.id)

    assert service.freeze_manifest(project.id, assembly.id).status is FinalAssemblyStatus.READY
    newer = _source(repository, project, job, shots[0], suffix="2", created_at="2025-01-01T00:00:02+00:00")
    assert service.get_manifest(project.id, assembly.id).items == original.items
    replacement = service.create_assembly(project.id, job.id, freeze=True)
    assert replacement.id != assembly.id
    assert service.get_manifest(project.id, replacement.id).items[0].production_execution_id == newer[0].id
    assert service.get_manifest(project.id, assembly.id).items[0].production_execution_id == first[0].id


def test_absolute_path_and_cross_project_manifest_are_rejected(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    execution, artifact, result, _ = _source(repository, project, job, shots[0], suffix="1", path="C:/outside.mp4", create_file=False)
    service = FinalAssemblyService(repository)
    with pytest.raises(FinalAssemblyServiceError, match="相对路径"):
        service.select_qualified_source(project.id, job.id, shots[0].id)
    with pytest.raises(FinalAssemblyServiceError, match="项目"):
        service.get_manifest("other-project", "missing")


def test_manifest_survives_repository_reload_and_duplicate_order_is_rejected(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    _source(repository, project, job, shots[0], suffix="1")
    service = FinalAssemblyService(repository)
    assembly = service.create_assembly(project.id, job.id, freeze=True)
    item = service.get_manifest(project.id, assembly.id).items[0]
    reloaded = ProjectRepository(repository.paths)
    reloaded_service = FinalAssemblyService(reloaded)
    assert reloaded_service.get_manifest(project.id, assembly.id).items == [item]
    with pytest.raises(ValueError, match="READY"):
        reloaded.create_final_assembly_item(item)
