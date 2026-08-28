"""Wave 1 production/reliability shard.

The production reliability module already owns focused service tests.  This
acceptance slice binds those scenarios to the Wave 1 matrix names so the
integration runner cannot silently omit a recovery or cost-guard gate.
"""

from __future__ import annotations

from pathlib import Path

from test.aidrama_studio.test_production_reliability_cost_guard import (
    test_authorized_eight_hard_blocks_ninth_before_transport as _budget_case,
    test_crash_after_create_restarts_with_same_remote_task as _restart_case,
    test_download_failure_restart_reuses_task_and_artifact_identity as _artifact_case,
    test_double_click_and_double_step_share_one_intent_and_create as _double_click_case,
    test_poll_timeout_restart_polls_same_task_without_create as _poll_timeout_case,
)


def test_process_restart_recovery_reuses_persisted_task(tmp_path: Path) -> None:
    """A cold repository reload polls the original task, never submits again."""

    _restart_case(tmp_path)


def test_double_click_idempotency_has_one_create_intent(tmp_path: Path) -> None:
    """Duplicate CTA/worker entry points converge on one durable intent."""

    _double_click_case(tmp_path)


def test_poll_timeout_recovery_keeps_task_identity(tmp_path: Path) -> None:
    """A timeout is resumable polling, not permission to resubmit."""

    _poll_timeout_case(tmp_path)


def test_artifact_failure_reconciles_without_duplicate_artifact(tmp_path: Path) -> None:
    """A download/write interruption repairs the existing provider result."""

    _artifact_case(tmp_path)


def test_paid_budget_ledger_blocks_ninth_create_before_transport(
    tmp_path: Path,
) -> None:
    """Eight authorized creates exhaust the ledger; the ninth never reaches transport."""

    _budget_case(tmp_path)
