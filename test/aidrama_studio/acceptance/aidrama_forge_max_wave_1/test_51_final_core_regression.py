from __future__ import annotations

from uuid import uuid4

from aidrama_studio.domain import (
    ProductionReview,
    ProductionReviewDecision,
    VisionAnalysisRecord,
)
from aidrama_studio.services import FinalAssemblyService
from test.aidrama_studio.test_final_assembly import (
    _shots,
    _source,
)
from test.aidrama_studio.test_final_assembly_runtime import (
    test_real_three_shot_mp4_smoke_preserves_manifest_order_and_metadata as _real_final_contract,
)
from test.aidrama_studio.test_postproduction_service import (
    context as _post_context,
    test_real_post_subtitle_and_bgm_smoke_is_pinned_and_probe_valid as _real_post_contract,
)
from test.aidrama_studio.test_production_execution import (
    context as _execution_context,
)
from test.aidrama_studio.test_tts_runtime import (
    test_tts_track_persists_cue_and_voice_provenance as _fake_tts_contract,
)


def test_human_governance_and_final_readiness(tmp_path) -> None:
    repository, project = _execution_context.__wrapped__(tmp_path)
    job, shots = _shots(repository, project, 3)
    missing = _source(
        repository,
        project,
        job,
        shots[0],
        suffix="missing-human",
        review=None,
    )
    rejected = _source(
        repository,
        project,
        job,
        shots[1],
        suffix="rejected-human",
        review=ProductionReviewDecision.REJECTED,
    )
    approved = _source(
        repository,
        project,
        job,
        shots[2],
        suffix="approved-human",
        review=ProductionReviewDecision.APPROVED,
    )
    repository.create_vision_analysis(
        VisionAnalysisRecord(
            id=uuid4().hex,
            project_id=project.id,
            execution_id=missing[0].id,
            artifact_id=missing[1].id,
            provider_id="offline-vision",
            model_id="fake-vision-wave1-v1",
            status="PASS",
            metrics={"verdict": "PASS"},
            reference_comparison={"verdict": "PASS"},
            created_at="2026-08-28T00:00:00+00:00",
        )
    )
    service = FinalAssemblyService(repository)

    blocked = service.calculate_readiness(project.id, job.id)
    assert blocked.ready is False
    assert blocked.eligible_shots == 1
    assert blocked.blocked_shots == 2
    reasons = " ".join(blocked.blocked_reasons)
    assert "等待人工审片" in reasons
    assert "rejected" in reasons.lower()
    assert approved[3] is not None

    missing_approval = repository.create_production_review(
        ProductionReview(
            id=uuid4().hex,
            project_id=project.id,
            qc_result_id=missing[2].id,
            decision=ProductionReviewDecision.APPROVED,
            reviewer="wave1-human",
            created_at="2099-01-01T00:00:00+00:00",
        )
    )
    rejected_reapproval = repository.create_production_review(
        ProductionReview(
            id=uuid4().hex,
            project_id=project.id,
            qc_result_id=rejected[2].id,
            decision=ProductionReviewDecision.APPROVED,
            reviewer="wave1-human",
            created_at="2099-01-01T00:00:01+00:00",
        )
    )
    ready = service.calculate_readiness(project.id, job.id)
    assert ready.ready is True
    assert ready.eligible_shots == 3
    assert service.select_qualified_source(
        project.id,
        job.id,
        shots[0].id,
    ).review_id == missing_approval.id
    assert service.select_qualified_source(
        project.id,
        job.id,
        shots[1].id,
    ).review_id == rejected_reapproval.id

    assembly = service.create_assembly(project.id, job.id, freeze=True)
    manifest = service.get_manifest(project.id, assembly.id)
    assert [item.production_artifact_id for item in manifest.items] == [
        missing[1].id,
        rejected[1].id,
        approved[1].id,
    ]
    assert all(item.review_id for item in manifest.items)


def test_real_three_shot_final_assembly_is_ordered_playable_and_persisted(
    tmp_path,
) -> None:
    _real_final_contract(_execution_context.__wrapped__(tmp_path))


def test_existing_audio_post_regression_stays_fake_and_picture_final_is_preserved(
    tmp_path,
    provider_calls,
) -> None:
    _fake_tts_contract(tmp_path / "tts")
    _real_post_contract(_post_context.__wrapped__(tmp_path / "post"))
    assert provider_calls.real_provider_calls == 0
    assert provider_calls.paid == 0
