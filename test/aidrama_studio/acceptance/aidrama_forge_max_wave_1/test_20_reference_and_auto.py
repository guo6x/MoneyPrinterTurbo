from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import ReferenceBindingType
from aidrama_studio.domain.reference_agent import ReferenceActionKind, ReferenceCoverageStatus
from aidrama_studio.services.reference_agent import ReferenceAgentError
from aidrama_studio.services.reference_assets import ReferenceAssetService

from test.aidrama_studio.test_reference_agent import (
    _agent,
    _lock_reference,
    context as _reference_context,
    test_material_revision_change_marks_locked_reference_for_review_without_mutation as _staleness_contract,
)
from test.aidrama_studio.test_auto_orchestrator import (
    test_empty_project_and_human_gate_step_are_idempotent as _auto_idempotency_contract,
)


@pytest.fixture
def reference_context(tmp_path: Path):
    return _reference_context.__wrapped__(tmp_path)


def test_reference_requirements_are_deduplicated_and_locked_subjects_reused(reference_context):
    repository, project, _story, _script, _plan = reference_context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    agent, provider = _agent(repository)

    readiness = agent.reference_readiness(project.id)
    assert len(readiness.required) == 5
    assert len([item for item in readiness.required if item.subject_type.value == "CHARACTER"]) == 2
    assert len([item for item in readiness.required if item.subject_type.value == "LOCATION"]) == 3
    assert readiness.character_coverage == "1/2"
    assert readiness.location_coverage == "1/3"
    assert {item.subject_id for item in readiness.missing} == {
        "character_b",
        "location_b",
        "location_c",
    }
    assert {item.subject_id for item in readiness.covered} == {
        "character_a",
        "location_a",
    }
    assert all(
        item.kind is ReferenceActionKind.WAITING_PAID_AUTHORIZATION
        for item in readiness.next_actions
    )
    assert len(provider.calls) == 0
    # Covered exact requirements produce no generation action.
    assert {item.requirement.subject_id for item in readiness.next_actions}.isdisjoint(
        {"character_a", "location_a"}
    )


def test_missing_reference_flow_requires_paid_and_human_gates(reference_context):
    repository, project, _story, _script, _plan = reference_context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    agent, provider = _agent(repository)
    initial = agent.evaluate(project.id)
    action_ids = [item.id for item in initial.next_actions]

    with pytest.raises(ReferenceAgentError, match="WAITING_PAID_AUTHORIZATION"):
        agent.generate_candidates(project.id, action_ids, authorization=None)
    assert len(provider.calls) == 0

    authorization = agent.generation_authorization(
        project.id,
        action_ids,
        max_creates=len(action_ids),
        approved_by="wave1-human",
        approved=True,
    )
    generated = agent.generate_candidates(
        project.id, action_ids, authorization=authorization
    )
    assert len(generated) == 3
    assert len(provider.calls) == 3
    references = ReferenceAssetService(repository)
    for item in generated:
        candidate = references.get_image_candidate(project.id, item.candidate_id)
        assert candidate.status.value == "DRAFT"
        assert references.resolve_image_candidate_path(project.id, candidate.id).is_file()

    waiting = agent.evaluate(project.id)
    assert {item.coverage_status for item in waiting.required if item.subject_id not in {"character_a", "location_a"}} == {
        ReferenceCoverageStatus.WAITING_HUMAN
    }
    assert all(
        item.kind is ReferenceActionKind.WAITING_HUMAN_REFERENCE_APPROVAL
        for item in waiting.next_actions
    )
    with pytest.raises(ReferenceAgentError, match="WAITING_HUMAN_REFERENCE_APPROVAL"):
        agent.approve_candidate_and_bind(
            project.id, generated[0].candidate_id, human_confirmed=False
        )

    for item in generated:
        version = agent.approve_candidate_and_bind(
            project.id, item.candidate_id, human_confirmed=True, actor="wave1-reviewer"
        )
        assert references.get_current_version(project.id, version.asset_id) is None
        with pytest.raises(ReferenceAgentError, match="WAITING_HUMAN_REFERENCE_LOCK"):
            agent.lock_bound_reference(project.id, version.id, human_confirmed=False)
        agent.lock_bound_reference(project.id, version.id, human_confirmed=True)

    final = agent.reference_readiness(project.id)
    assert final.character_coverage == "2/2"
    assert final.location_coverage == "3/3"
    assert final.production_reference_ready is True
    assert final.production_readiness["ready"] is True
    assert all(item.coverage_status is ReferenceCoverageStatus.LOCKED for item in final.required)


def test_reference_generation_is_not_repeated_for_already_covered_requirements(reference_context):
    repository, project, _story, _script, _plan = reference_context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    agent, provider = _agent(repository)
    initial = agent.evaluate(project.id)
    ids = [item.id for item in initial.next_actions]
    authorization = agent.generation_authorization(
        project.id, ids, max_creates=3, approved_by="human", approved=True
    )
    agent.generate_candidates(project.id, ids, authorization=authorization)
    with pytest.raises(ReferenceAgentError):
        agent.generate_candidates(project.id, ids, authorization=authorization)
    assert len(provider.calls) == 3


def test_material_upstream_change_marks_historical_locked_reference_stale(reference_context):
    # Reuse the product-level contract test with this shard's isolated context.
    _staleness_contract(reference_context)


def test_auto_step_reads_persisted_state_and_is_idempotent(tmp_path: Path):
    _auto_idempotency_contract(tmp_path)


def test_auto_uncertain_create_is_one_shot_and_reconciliation_reuses_identity(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_22_auto_runtime_shard as auto_runtime,
    )

    auto_runtime.test_auto_uncertain_create_is_one_shot_and_reconciliation_reuses_identity(
        tmp_path
    )


def test_auto_state_machine_uses_persisted_state_and_idempotent_human_boundary(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_22_auto_runtime_shard as auto_runtime,
    )

    auto_runtime.test_auto_state_machine_uses_persisted_state_and_idempotent_human_boundary(
        tmp_path
    )


def test_auto_resume_cold_reload_and_terminal_state_are_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_22_auto_runtime_shard as auto_runtime,
    )

    auto_runtime.test_auto_resume_cold_reload_and_terminal_state_are_idempotent(
        tmp_path,
        monkeypatch,
    )


def test_auto_failed_action_is_persisted_and_provider_failure_is_blocked(
    tmp_path: Path,
) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_22_auto_runtime_shard as auto_runtime,
    )

    auto_runtime.test_auto_failed_action_is_persisted_and_provider_failure_is_blocked(
        tmp_path
    )
