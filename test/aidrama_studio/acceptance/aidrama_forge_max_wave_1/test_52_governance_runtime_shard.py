"""Wave 1 exact-artifact Human Review and final-governance shard.

The source-selection service is the final authority: technical QC and Vision
are evidence, while the latest Human Review for the exact artifact determines
whether a source can enter the final manifest.
"""

from __future__ import annotations

from pathlib import Path

from aidrama_studio.domain import (
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
)
from test.aidrama_studio.test_final_assembly import (
    test_final_readiness_requires_human_approval_for_every_shot as _multi_shot_case,
    test_latest_rejected_review_blocks_an_older_approval_until_reapproved as _reversal_case,
    test_latest_review_decision_wins_over_historical_rejection as _latest_case,
    test_qc_pass_without_human_approval_is_blocked_but_approved_review_is_eligible as _human_gate_case,
    test_review_is_bound_to_the_exact_candidate_artifact as _identity_case,
    test_vision_status_never_substitutes_for_human_approval as _vision_case,
    test_unqualified_sources_block_readiness as _unqualified_case,
)
from test.aidrama_studio.test_production_execution import (
    context as _execution_context,
)


def _context(tmp_path: Path):
    return _execution_context.__wrapped__(tmp_path)


def test_human_review_matrix_blocks_unapproved_sources_and_allows_approved(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _unqualified_case(
        context,
        ProductionExecutionStatus.SUCCEEDED,
        ProductionQCStatus.QC_FAILED,
        None,
        True,
        "QC_PASS",
    )
    _human_gate_case(context)


def test_pending_review_is_not_an_approval(tmp_path: Path) -> None:
    _unqualified_case(
        _context(tmp_path),
        ProductionExecutionStatus.SUCCEEDED,
        ProductionQCStatus.QC_PASS,
        ProductionReviewDecision.PENDING,
        True,
        "等待人工审片",
    )


def test_latest_exact_artifact_review_semantics_support_reversal(tmp_path: Path) -> None:
    _latest_case(_context(tmp_path / "approved-after-reject"))
    _reversal_case(_context(tmp_path / "rejected-after-approval"))


def test_review_identity_and_vision_cannot_cross_candidate_boundary(
    tmp_path: Path,
) -> None:
    _identity_case(_context(tmp_path / "identity"))
    _vision_case(_context(tmp_path / "vision"), "AI_ANALYSIS")


def test_three_shot_final_readiness_requires_every_human_approval(
    tmp_path: Path,
) -> None:
    _multi_shot_case(_context(tmp_path), 3)
