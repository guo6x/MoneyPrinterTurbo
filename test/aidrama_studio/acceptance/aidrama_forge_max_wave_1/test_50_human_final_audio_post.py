from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from aidrama_studio.domain import (
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionReviewDecision,
    VisionAnalysisRecord,
)
from aidrama_studio.services import (
    FinalAssemblyRuntimeService,
    FinalAssemblyRuntimeServiceError,
    FinalAssemblyService,
    FinalAssemblyServiceError,
    ProductionExecutionService,
    ProductionQCService,
)
from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter
from test.aidrama_studio.test_final_assembly import _shots
from test.aidrama_studio.test_production_execution import context as seed_context


def _real_source(
    repository,
    project,
    job,
    shot,
    *,
    index: int,
    ffmpeg: str,
    approved: bool,
):
    duration = 0.6
    execution = repository.create_production_execution(
        ProductionExecution(
            id=uuid4().hex,
            production_job_id=job.id,
            status=ProductionExecutionStatus.SUCCEEDED,
            worker_type="wave-1-real-media",
            created_at=f"2026-08-28T00:00:0{index}+00:00",
            input_snapshot=ProductionInputSnapshot(
                project_id=project.id,
                story_revision_id="story_001",
                script_revision_id="script_001",
                shot_plan_revision_id=job.shot_plan_revision_id,
                reference_asset_versions={},
                shot_parameters={shot.shot_id: {"duration_seconds": duration}},
            ),
        )
    )
    relative_path = f"production/{execution.id}/shot-{index}.mp4"
    target = repository.paths.projects / project.id / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    generated = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=24:duration={duration}",
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
    assert generated.returncode == 0, generated.stderr[-1500:]
    artifact = ProductionExecutionService(repository).record_artifact(
        project.id,
        execution.id,
        "video",
        relative_path,
        {
            "execution_id": execution.id,
            "mime_type": "video/mp4",
            "shot_id": shot.shot_id,
            "duration_seconds": duration,
            "resolution": "320x240",
            "codec": "h264",
            "audio_required": False,
            "reference_versions": {},
        },
    )
    qc_service = ProductionQCService(repository)
    qc = qc_service.run_qc(project.id, execution.id, artifact.id)
    assert qc.status.value == "QC_PASS"
    review = None
    if approved:
        review = qc_service.create_review(
            project.id,
            qc.id,
            ProductionReviewDecision.APPROVED,
            reviewer="wave-1-human",
            notes="Exact artifact approved for final assembly.",
        )
    return execution, artifact, qc, review, target


def _decode_with_ffmpeg(ffmpeg: str, target: Path) -> None:
    decoded = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(target), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr[-1500:]


def test_final_assembly_renders_three_exact_approved_real_mp4_sources(
    offline_environment,
    ffmpeg_executable: str,
) -> None:
    repository, project = seed_context.__wrapped__(offline_environment.data_root / "final-positive")
    job, shots = _shots(repository, project, 3)
    sources = [
        _real_source(
            repository,
            project,
            job,
            shot,
            index=index,
            ffmpeg=ffmpeg_executable,
            approved=True,
        )
        for index, shot in enumerate(shots, start=1)
    ]
    manifest_service = FinalAssemblyService(repository)
    readiness = manifest_service.calculate_readiness(project.id, job.id)
    assert readiness.ready
    assert readiness.total_shots == readiness.eligible_shots == 3
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    manifest = manifest_service.get_manifest(project.id, assembly.id)
    assert [item.order_index for item in manifest.items] == [1, 2, 3]
    assert len({item.production_shot_id for item in manifest.items}) == 3
    assert len({item.production_artifact_id for item in manifest.items}) == 3
    assert [item.production_artifact_id for item in manifest.items] == [
        source[1].id for source in sources
    ]
    assert all(item.source_sha256 for item in manifest.items)

    runtime = FinalAssemblyRuntimeService(
        repository,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id
        ),
    )
    attempt = runtime.render(project.id, assembly.id)
    output = repository.paths.projects / project.id / attempt.output_relative_path
    assert attempt.status.value == "SUCCEEDED"
    assert output.is_file() and output.stat().st_size > 0
    assert attempt.metadata_json["video_stream"] is True
    assert attempt.metadata_json["codec"].lower() == "h264"
    assert attempt.metadata_json["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert [item["order_index"] for item in attempt.metadata_json["source_items"]] == [1, 2, 3]
    assert len(attempt.metadata_json["source_items"]) == 3
    assert attempt.metadata_json["duration_seconds"] > 1.0
    _decode_with_ffmpeg(ffmpeg_executable, output)


def test_final_readiness_blocks_a_real_candidate_without_human_approval(
    offline_environment,
    ffmpeg_executable: str,
) -> None:
    repository, project = seed_context.__wrapped__(offline_environment.data_root / "final-negative")
    job, shots = _shots(repository, project, 3)
    sources = [
        _real_source(
            repository,
            project,
            job,
            shot,
            index=index,
            ffmpeg=ffmpeg_executable,
            approved=index != 3,
        )
        for index, shot in enumerate(shots, start=1)
    ]
    missing_execution, missing_artifact, _missing_qc, _review, _path = sources[-1]
    repository.create_vision_analysis(
        VisionAnalysisRecord(
            id=uuid4().hex,
            project_id=project.id,
            execution_id=missing_execution.id,
            artifact_id=missing_artifact.id,
            provider_id="fake-universal-vision",
            model_id="fake-vision-v1",
            status="AI_ANALYSIS",
            metrics={"verdict": "PASS"},
            reference_comparison={"verdict": "PASS"},
            created_at="2026-08-28T00:00:10+00:00",
        )
    )
    manifest_service = FinalAssemblyService(repository)
    readiness = manifest_service.calculate_readiness(project.id, job.id)
    assert not readiness.ready
    assert readiness.blocked_shots == 1
    assert "等待人工审片" in " ".join(readiness.blocked_reasons)
    with pytest.raises(FinalAssemblyServiceError, match="人工审片"):
        manifest_service.select_qualified_source(project.id, job.id, shots[-1].id)
    assembly = manifest_service.create_assembly(project.id, job.id)
    with pytest.raises(FinalAssemblyServiceError):
        manifest_service.freeze_manifest(project.id, assembly.id)
    runtime = FinalAssemblyRuntimeService(repository)
    with pytest.raises(FinalAssemblyRuntimeServiceError, match="READY"):
        runtime.render(project.id, assembly.id)
    assert repository.list_final_assembly_render_attempts(assembly.id) == []


def test_human_governance_and_final_readiness(tmp_path) -> None:
    """Keep the authoritative FAST_GATE node backed by the full governance test."""

    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_51_final_core_regression as final_regression,
    )

    final_regression.test_human_governance_and_final_readiness(tmp_path)


def test_human_review_matrix_blocks_unapproved_sources_and_allows_approved(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_52_governance_runtime_shard as governance_runtime,
    )

    governance_runtime.test_human_review_matrix_blocks_unapproved_sources_and_allows_approved(
        tmp_path
    )


def test_pending_review_is_not_an_approval(tmp_path: Path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_52_governance_runtime_shard as governance_runtime,
    )

    governance_runtime.test_pending_review_is_not_an_approval(tmp_path)


def test_latest_exact_artifact_review_semantics_support_reversal(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_52_governance_runtime_shard as governance_runtime,
    )

    governance_runtime.test_latest_exact_artifact_review_semantics_support_reversal(
        tmp_path
    )


def test_review_identity_and_vision_cannot_cross_candidate_boundary(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_52_governance_runtime_shard as governance_runtime,
    )

    governance_runtime.test_review_identity_and_vision_cannot_cross_candidate_boundary(
        tmp_path
    )


def test_three_shot_final_readiness_requires_every_human_approval(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_52_governance_runtime_shard as governance_runtime,
    )

    governance_runtime.test_three_shot_final_readiness_requires_every_human_approval(
        tmp_path
    )
