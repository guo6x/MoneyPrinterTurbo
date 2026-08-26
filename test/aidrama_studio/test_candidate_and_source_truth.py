from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest

from aidrama_studio.domain import (
    ProductionAttempt,
    ProductionAttemptStatus,
    ProductionExecutionStatus,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
    ReferenceAssetType,
    ReferenceBindingType,
    ReferenceImageCandidateStatus,
)
from aidrama_studio.services import (
    CapabilityKind,
    CapabilityStatus,
    FinalAssemblyService,
    FinalAssemblyServiceError,
    ImageCandidate,
    ImageGenerationProvider,
    ImageRuntimeError,
    ImageRuntimeService,
    ProductionExecutionService,
    ProductionService,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetServiceError,
)
from aidrama_studio.services.providers.openai_image import (
    OpenAIImageProvider,
    OpenAIImageProviderConfig,
)
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


class _OpenAIImageResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _OfflineImageProvider(ImageGenerationProvider):
    provider_name = "OFFLINE_TEST_IMAGE"

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            CapabilityKind.IMAGE,
            self.provider_name,
            True,
            "configured",
            {
                "model": "offline-image-v1",
                "configured": True,
                "deployment_region": "LOCAL",
                "endpoint_class": "OFFLINE_TEST",
                "endpoint_profile_id": "runtime:IMAGE:OFFLINE_TEST_IMAGE:OFFLINE_TEST",
                "verification_state": "VERIFIED",
            },
            configured=True,
            verified=True,
        )

    def generate_candidate(self, prompt, *, project_id, metadata=None):
        self.calls.append(
            {
                "prompt": prompt,
                "project_id": project_id,
                "metadata": dict(metadata or {}),
            }
        )
        return ImageCandidate(
            project_id=project_id,
            provider=self.provider_name,
            prompt=prompt,
            content=self.content,
            mime_type="image/png",
            metadata={
                **dict(metadata or {}),
                "request_parameters": {"n": 1},
            },
        )


def test_gpt_image_request_body_matches_documented_contract(monkeypatch):
    captured: list[object] = []
    image = png_bytes()

    def fake_urlopen(request, *, timeout):
        captured.append((request, timeout))
        return _OpenAIImageResponse(
            {"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    monkeypatch.setattr(
        "aidrama_studio.services.providers.openai_image.urllib.request.urlopen",
        fake_urlopen,
    )
    provider = OpenAIImageProvider(
        OpenAIImageProviderConfig(
            api_key="unit-test-credential",
            model="gpt-image-2",
            timeout_seconds=17,
            allow_paid_live_tests=True,
        )
    )

    candidate = provider.generate_candidate(
        "一位身穿蓝色风衣的侦探",
        project_id="project-1",
    )

    assert len(captured) == 1
    request, timeout = captured[0]
    assert request.full_url == "https://api.openai.com/v1/images/generations"
    assert timeout == 17
    assert json.loads(request.data.decode("utf-8")) == {
        "model": "gpt-image-2",
        "prompt": "一位身穿蓝色风衣的侦探",
        "n": 1,
    }
    assert candidate.content == image
    assert candidate.metadata["model"] == "gpt-image-2"
    assert candidate.metadata["request_parameters"] == {"n": 1}


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


def test_generation_records_durable_draft_but_never_promotes_or_locks(context):
    repository, project = context
    references = ReferenceAssetService(repository)
    asset = references.ensure_workspace_asset(
        project.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )
    provider = _OfflineImageProvider(png_bytes())
    runtime = ImageRuntimeService(repository, provider=provider)

    candidate = runtime.generate_and_record_candidate(
        project.id,
        asset.id,
        "Hero portrait",
        source_story_revision_id="story_001",
        filename="hero-generated.png",
        reference_assets=references,
    )

    assert len(provider.calls) == 1
    assert candidate.status is ReferenceImageCandidateStatus.DRAFT
    assert candidate.provider_id == provider.provider_name
    assert candidate.model_id == "offline-image-v1"
    assert references.resolve_image_candidate_path(
        project.id, candidate.id
    ).read_bytes() == png_bytes()
    assert references.list_versions(project.id, asset.id) == []
    assert references.get_current_version(project.id, asset.id) is None

    reloaded = ReferenceAssetService(ProjectRepository(repository.paths))
    assert reloaded.find_workspace_asset(
        project.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    ).id == asset.id
    assert reloaded.list_image_candidates(project.id, asset.id) == [candidate]

    version = reloaded.promote_image_candidate(project.id, candidate.id)
    assert reloaded.get_current_version(project.id, asset.id) is None
    reloaded.bind_version(
        project.id,
        version.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )
    assert reloaded.get_current_version(project.id, asset.id) is None
    reloaded.activate_version(project.id, asset.id, version.id)
    assert reloaded.get_current_version(project.id, asset.id).id == version.id


def test_physical_image_validation_happens_before_candidate_recording(context):
    repository, project = context
    references = ReferenceAssetService(repository)
    asset = references.ensure_workspace_asset(
        project.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )
    provider = _OfflineImageProvider(b"not-a-physical-image")
    runtime = ImageRuntimeService(repository, provider=provider)

    with pytest.raises(ImageRuntimeError, match="物理图片验证"):
        runtime.generate_and_record_candidate(
            project.id,
            asset.id,
            "Hero portrait",
            source_story_revision_id="story_001",
            filename="hero-generated.png",
            reference_assets=references,
        )

    assert len(provider.calls) == 1
    assert references.list_image_candidates(project.id, asset.id) == []
    assert references.list_versions(project.id, asset.id) == []
    assert references.get_current_version(project.id, asset.id) is None


def test_image_candidate_rejects_forged_provider_disclosure(context):
    repository, project = context
    references = ReferenceAssetService(repository)
    asset = references.ensure_workspace_asset(
        project.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )

    class ForgingProvider(_OfflineImageProvider):
        def generate_candidate(self, prompt, *, project_id, metadata=None):
            candidate = super().generate_candidate(
                prompt, project_id=project_id, metadata=metadata
            )
            forged = dict(candidate.metadata)
            disclosure = dict(forged["provider_disclosure"])
            disclosure["model_id"] = "forged-model"
            forged["provider_disclosure"] = disclosure
            return candidate.__class__(
                project_id=candidate.project_id,
                provider=candidate.provider,
                prompt=candidate.prompt,
                content=candidate.content,
                mime_type=candidate.mime_type,
                metadata=forged,
            )

    runtime = ImageRuntimeService(
        repository,
        provider=ForgingProvider(png_bytes()),
    )
    with pytest.raises(ImageRuntimeError, match="provenance"):
        runtime.generate_and_record_candidate(
            project.id,
            asset.id,
            "Hero portrait",
            source_story_revision_id="story_001",
            filename="hero-generated.png",
            reference_assets=references,
        )
    assert references.list_image_candidates(project.id, asset.id) == []


def test_image_candidate_rejects_secret_bearing_request_parameters(context):
    repository, project = context
    references = ReferenceAssetService(repository)
    asset = references.create_asset(project.id, ReferenceAssetType.CHARACTER_REFERENCE)
    with pytest.raises(ReferenceAssetServiceError, match="secret"):
        references.record_image_candidate(
            project.id,
            asset.id,
            source_story_revision_id="story_001",
            provider_id="MOCK_IMAGE",
            model_id="deterministic-image-v1",
            endpoint_profile_id="runtime:IMAGE:MOCK_IMAGE:LOCAL",
            deployment_region="LOCAL",
            prompt="Hero portrait",
            content=png_bytes(),
            filename="hero-secret.png",
            mime_type="image/png",
            request_parameters={"api_key": "secret-value"},
        )
    assert references.list_image_candidates(project.id, asset.id) == []


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
