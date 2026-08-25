from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from aidrama_studio.domain import (
    ProductionArtifact,
    ProductionAttempt,
    ProductionAttemptStatus,
    ProductionExecutionStatus,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
    ReferenceAssetType,
    ReferenceImageCandidateStatus,
)
from aidrama_studio.services import (
    FinalAssemblyService,
    FinalAssemblyServiceError,
    ProductionExecutionService,
    ProductionService,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetServiceError,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_final_assembly import _shots, _source
from test.aidrama_studio.test_production_execution import context as _execution_context


@pytest.fixture
def context(tmp_path: Path):
    return _execution_context.__wrapped__(tmp_path)


def _candidate(service, project, asset, *, color="red", parent=None):
    return service.record_image_candidate(
        project.id,
        asset.id,
        source_story_revision_id="story_001",
        provider_id="MOCK_IMAGE",
        model_id="deterministic-image-v1",
        endpoint_profile_id="runtime:IMAGE:MOCK_IMAGE:LOCAL",
        deployment_region="LOCAL",
        prompt=f"Hero portrait {color}",
        content=png_bytes(color=color),
        filename=f"hero-{color}.png",
        mime_type="image/png",
        request_parameters={"quality": "preview"},
        parent_candidate_id=parent,
    )


def test_image_candidate_is_durable_noncanonical_and_requires_separate_lock(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)

    candidate = _candidate(service, project, asset)

    assert candidate.status is ReferenceImageCandidateStatus.DRAFT
    assert service.list_versions(project.id, asset.id) == []
    assert service.get_current_version(project.id, asset.id) is None
    assert service.resolve_image_candidate_path(project.id, candidate.id).is_file()

    reloaded = ReferenceAssetService(ProjectRepository(repository.paths))
    assert reloaded.list_image_candidates(project.id, asset.id) == [candidate]

    version = reloaded.promote_image_candidate(project.id, candidate.id)
    promoted = reloaded.get_image_candidate(project.id, candidate.id)
    assert promoted.status is ReferenceImageCandidateStatus.PROMOTED
    assert promoted.promoted_version_id == version.id
    assert version.metadata["source_image_candidate_id"] == candidate.id
    assert reloaded.get_current_version(project.id, asset.id) is None

    reloaded.activate_version(project.id, asset.id, version.id)
    assert reloaded.get_current_version(project.id, asset.id).id == version.id


def test_candidate_rejection_regeneration_history_and_project_isolation(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    first = _candidate(service, project, asset, color="red")
    rejected = service.reject_image_candidate(
        project.id, first.id, notes="creative mismatch"
    )
    assert rejected.status is ReferenceImageCandidateStatus.REJECTED
    with pytest.raises(ReferenceAssetServiceError, match="DRAFT"):
        service.promote_image_candidate(project.id, first.id)

    second = _candidate(service, project, asset, color="blue", parent=first.id)
    assert second.parent_candidate_id == first.id
    assert [item.status for item in service.list_image_candidates(project.id, asset.id)] == [
        ReferenceImageCandidateStatus.REJECTED,
        ReferenceImageCandidateStatus.DRAFT,
    ]
    other = ProjectService(repository).create(title="Other")
    with pytest.raises(ReferenceAssetServiceError, match="不属于"):
        service.get_image_candidate(other.id, second.id)


def test_tampered_candidate_cannot_partially_promote(context):
    repository, project = context
    service = ReferenceAssetService(repository)
    asset = service.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    candidate = _candidate(service, project, asset)
    path = service.resolve_image_candidate_path(project.id, candidate.id)
    path.write_bytes(b"tampered")

    with pytest.raises(ReferenceAssetServiceError, match="大小|SHA-256"):
        service.promote_image_candidate(project.id, candidate.id)
    assert service.list_versions(project.id, asset.id) == []
    assert service.get_image_candidate(project.id, candidate.id).status is ReferenceImageCandidateStatus.DRAFT


def test_explicit_shot_source_selection_is_frozen_and_append_only(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    first = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="1",
        created_at="2025-01-01T00:00:01+00:00",
        review=ProductionReviewDecision.APPROVED,
    )
    second = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="2",
        created_at="2025-01-01T00:00:02+00:00",
        review=ProductionReviewDecision.APPROVED,
    )
    service = FinalAssemblyService(repository)
    decision = service.select_shot_source(
        project.id,
        job.id,
        shots[0].id,
        production_execution_id=first[0].id,
        production_artifact_id=first[1].id,
    )
    selected = service.select_qualified_source(project.id, job.id, shots[0].id)
    assert selected.production_execution_id == first[0].id
    assert selected.source_decision_id == decision.id

    assembly = service.create_assembly(project.id, job.id, freeze=True)
    frozen = service.get_manifest(project.id, assembly.id).items[0]
    assert frozen.source_decision_id == decision.id

    replacement = service.select_shot_source(
        project.id,
        job.id,
        shots[0].id,
        production_execution_id=second[0].id,
        production_artifact_id=second[1].id,
    )
    assert replacement.sequence_number == 2
    assert service.select_qualified_source(
        project.id, job.id, shots[0].id
    ).production_execution_id == second[0].id
    assert service.get_manifest(project.id, assembly.id).items[0] == frozen


def test_preview_requires_explicit_promotion_before_final_selection(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    preview = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="1",
        review=ProductionReviewDecision.APPROVED,
    )
    with repository.transaction() as connection:
        import json

        metadata = dict(preview[1].metadata_json)
        metadata["artifact_role"] = "PREVIEW"
        connection.execute(
            "UPDATE production_artifacts SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), preview[1].id),
        )
    service = FinalAssemblyService(repository)
    with pytest.raises(FinalAssemblyServiceError, match="Preview"):
        service.select_qualified_source(project.id, job.id, shots[0].id)
    with pytest.raises(FinalAssemblyServiceError, match="Preview"):
        service.select_shot_source(
            project.id,
            job.id,
            shots[0].id,
            production_execution_id=preview[0].id,
            production_artifact_id=preview[1].id,
        )

    decision = service.select_shot_source(
        project.id,
        job.id,
        shots[0].id,
        production_execution_id=preview[0].id,
        production_artifact_id=preview[1].id,
        promote_preview=True,
    )
    assert decision.selection_kind.value == "PREVIEW_PROMOTED"
    assert service.select_qualified_source(
        project.id, job.id, shots[0].id
    ).production_artifact_id == preview[1].id


def test_creative_rejection_requires_explicit_attempt_two_and_resolves_new_source(context):
    repository, project = context
    job, shots = _shots(repository, project, 1)
    first_execution, first_artifact, first_qc, rejected = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="1",
        review=ProductionReviewDecision.REJECTED,
    )
    assert rejected is not None
    repository.create_production_attempt(
        ProductionAttempt(
            id=uuid4().hex,
            production_shot_id=shots[0].id,
            attempt_number=1,
            status=ProductionAttemptStatus.SUCCEEDED,
            runtime_adapter="mock",
            input_snapshot_json=first_execution.input_snapshot.to_json_dict(),
            output_artifact_json={"artifact_id": first_artifact.id},
            created_at=first_execution.created_at,
        )
    )
    execution_service = ProductionExecutionService(repository)
    with pytest.raises(FinalAssemblyServiceError, match="rejected"):
        FinalAssemblyService(repository).select_qualified_source(
            project.id, job.id, shots[0].id
        )
    assert len(execution_service.list_executions(project.id, job.id)) == 1

    second_execution, second_attempt = execution_service.request_creative_regeneration(
        project.id,
        job.id,
        shots[0].id,
        rejected.id,
        first_execution.input_snapshot,
        worker_type="mock",
    )
    assert second_attempt.attempt_number == 2
    assert second_execution.creative_retry_of_execution_id == first_execution.id
    assert second_execution.creative_rejection_review_id == rejected.id
    assert repository.get_production_execution(first_execution.id).status is ProductionExecutionStatus.SUCCEEDED

    execution_service.start_execution(project.id, second_execution.id)
    relative_path = f"production/{second_execution.id}/shot_001.mp4"
    target = repository.paths.projects / project.id / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-two")
    second_artifact = execution_service.record_artifact(
        project.id,
        second_execution.id,
        "video",
        relative_path,
        {"mime_type": "video/mp4", "duration_seconds": 2, "shot_id": "shot_001"},
    )
    execution_service.complete_execution(project.id, second_execution.id)
    ProductionService(repository).complete_attempt(
        project.id,
        second_attempt.id,
        {"artifact_id": second_artifact.id},
    )
    second_qc = repository.create_production_qc_result(
        ProductionQCResult(
            id=uuid4().hex,
            project_id=project.id,
            execution_id=second_execution.id,
            artifact_id=second_artifact.id,
            status=ProductionQCStatus.QC_PASS,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    approved = repository.create_production_review(
        ProductionReview(
            id=uuid4().hex,
            project_id=project.id,
            qc_result_id=second_qc.id,
            decision=ProductionReviewDecision.APPROVED,
            reviewer="human",
            created_at="2026-01-01T00:00:01+00:00",
        )
    )
    source_service = FinalAssemblyService(repository)
    source_service.select_shot_source(
        project.id,
        job.id,
        shots[0].id,
        production_execution_id=second_execution.id,
        production_artifact_id=second_artifact.id,
    )
    current = source_service.select_qualified_source(project.id, job.id, shots[0].id)
    assert current.production_execution_id == second_execution.id
    assert current.review_id == approved.id
    assert repository.get_production_review(rejected.id).decision is ProductionReviewDecision.REJECTED
    assert len(repository.list_production_attempts(shots[0].id)) == 2
