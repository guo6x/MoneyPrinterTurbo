from __future__ import annotations

import subprocess

from aidrama_studio.domain import ProductionQCStatus, ProductionReviewDecision
from aidrama_studio.domain.continuity import ContinuityIssueType, ContinuitySourceKind
from aidrama_studio.services import ContinuityEngine, ProductionQCService
from test.aidrama_studio import test_production_qc as qc_contracts
from test.aidrama_studio.test_continuity_engine import (
    _candidate,
    _facts,
    _request,
    _types,
    _umbrella,
    _wardrobe,
)
from test.aidrama_studio.test_production_execution import (
    context as _execution_context,
)
from test.aidrama_studio.test_vision_universal_runtime import (
    FakeVisionSession,
    _vision_context,
    _wired_service,
    test_universal_vision_failures_are_persisted_and_sanitized as _vision_failure_contract,
)


def _decode_probe(ffmpeg_path: str, path) -> str:
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    return completed.stderr


def test_real_media_technical_qc_accepts_h264_and_rejects_invalid_profiles(
    tmp_path,
    ffmpeg_path: str,
) -> None:
    valid_context = _execution_context.__wrapped__(tmp_path / "valid")
    repository, project, execution, artifact = qc_contracts._artifact_context(
        valid_context,
        source_filter="testsrc=size=1280x720:rate=24:d=10",
        duration_seconds=10,
        resolution="1280x720",
        codec="h264",
    )
    valid = ProductionQCService(repository).run_qc(
        project.id,
        execution.id,
        artifact.id,
    )
    assert valid.status is ProductionQCStatus.QC_PASS
    media = repository.paths.projects / project.id / artifact.path
    probe = _decode_probe(ffmpeg_path, media)
    assert "Video: h264" in probe
    assert "1280x720" in probe
    assert "Duration: 00:00:10." in probe

    truncated_context = _execution_context.__wrapped__(tmp_path / "truncated")
    repo, proj, exe, art = qc_contracts._artifact_context(truncated_context)
    target = repo.paths.projects / proj.id / art.path
    target.write_bytes(target.read_bytes()[:128])
    assert ProductionQCService(repo).run_qc(proj.id, exe.id, art.id).status is ProductionQCStatus.QC_FAILED

    audio_context = _execution_context.__wrapped__(tmp_path / "audio-only")
    repo, proj, exe, art = qc_contracts._artifact_context(audio_context)
    target = repo.paths.projects / proj.id / art.path
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-vn",
            "-c:a",
            "aac",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    assert ProductionQCService(repo).run_qc(proj.id, exe.id, art.id).status is ProductionQCStatus.QC_FAILED

    wrong_codec_context = _execution_context.__wrapped__(tmp_path / "wrong-codec")
    repo, proj, exe, art = qc_contracts._artifact_context(wrong_codec_context)
    target = repo.paths.projects / proj.id / art.path
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=25:d=1",
            "-an",
            "-c:v",
            "mpeg4",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    assert ProductionQCService(repo).run_qc(proj.id, exe.id, art.id).status is ProductionQCStatus.QC_FAILED


def test_real_frames_universal_vision_feed_persisted_continuity_recommendations(
    tmp_path,
    provider_calls,
) -> None:
    (
        repository,
        project,
        execution,
        artifact,
        target,
        _runtime_plan,
        _brief,
        _ordered_bindings,
    ) = _vision_context(tmp_path)
    technical = ProductionQCService(repository).run_qc(
        project.id,
        execution.id,
        artifact.id,
    )
    human = ProductionQCService(repository).create_review(
        project.id,
        technical.id,
        ProductionReviewDecision.APPROVED,
        reviewer="wave1-human",
    )
    session = FakeVisionSession()
    vision, _provider, _store, _profile, _resolved = _wired_service(
        repository,
        project,
        session,
    )

    analysis = vision.analyze(project.id, execution.id, artifact.id)

    assert target.is_file()
    assert analysis.status == "AI_ANALYSIS"
    assert vision.blocks_final is False
    assert len(session.calls) == 1
    manifest = repository.get_vision_frame_manifest(analysis.frame_manifest_id)
    assert manifest is not None and manifest.frame_count >= 3
    assert {sample["role"] for sample in manifest.samples} >= {
        "FIRST",
        "MIDDLE",
        "LAST",
    }
    assert repository.get_vision_analysis(analysis.analysis_id) is not None
    assert ProductionQCService(repository).list_reviews(project.id, technical.id) == [human]

    script_revision = repository.list_script_revisions(project.id)[-1]
    shot_revision = repository.list_shot_revisions(project.id)[-1]
    black_with_umbrella = _facts(
        "shot_01",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        next_shot_id="shot_02",
    )
    consistent = _facts(
        "shot_02",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        previous_shot_id="shot_01",
        next_shot_id="shot_03",
    )
    drifted = _facts(
        "shot_03",
        wardrobe=_wardrobe("white"),
        props=(),
        previous_shot_id="shot_02",
    )
    scope = {
        "project_id": project.id,
        "script_revision_id": script_revision["id"],
        "shot_plan_revision_id": shot_revision["id"],
    }
    requests = (
        _request(
            "shot_01",
            1,
            (
                _candidate(
                    black_with_umbrella,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-lock-shot-01",
                ),
            ),
            (
                _candidate(
                    black_with_umbrella,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    analysis.analysis_id,
                ),
            ),
            approved_for_continuity=True,
        ).model_copy(update=scope),
        _request(
            "shot_02",
            2,
            (),
            (
                _candidate(
                    consistent,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    analysis.analysis_id,
                ),
            ),
            approved_for_continuity=True,
        ).model_copy(update=scope),
        _request(
            "shot_03",
            3,
            (),
            (
                _candidate(
                    drifted,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    analysis.analysis_id,
                ),
            ),
        ).model_copy(update=scope),
    )
    results = ContinuityEngine(repository).evaluate_sequence(requests)

    assert [item.observed_snapshot.shot_id for item in results] == [
        "shot_01",
        "shot_02",
        "shot_03",
    ]
    assert {
        ContinuityIssueType.WARDROBE_DRIFT,
        ContinuityIssueType.PROP_DRIFT,
    } <= _types(results[-1])
    repairs = repository.list_continuity_repair_recommendations(
        project.id,
        shot_id="shot_03",
    )
    assert repairs
    assert all(item.requires_human_confirmation for item in repairs)
    assert any(item.requires_paid_create for item in repairs)
    assert provider_calls.paid == 0
    assert provider_calls.video_create == 0
    assert ProductionQCService(repository).list_reviews(project.id, technical.id) == [human]


def test_vision_failure_is_sanitized_and_preserves_human_truth(tmp_path) -> None:
    _vision_failure_contract(
        tmp_path,
        "exception",
    )
