from __future__ import annotations

from aidrama_studio.domain import ProductionJobStatus, ProductionReviewDecision, ProductionShotStatus
from aidrama_studio.services import (
    FinalAssemblyService,
    ProductionExecutionService,
    ProductionOrchestrator,
    ProductionQCService,
    ProductionService,
    ProductionWorker,
)
from test.aidrama_studio import test_production_reliability_cost_guard as reliability
from test.aidrama_studio import test_production_worker as worker_contracts
from test.aidrama_studio.test_production_orchestrator import MultiShotAdapter
from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1.test_21_reference_auto_reliability_regression import (
    _complete_canonical_references,
)


def test_six_shot_fake_async_production_persists_governed_candidates(
    canonical_approved_project: dict[str, object],
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    _complete_canonical_references(repository, project.id)
    production = ProductionService(repository)
    job = production.create_production_job(project.id, "shot_plan_001")
    adapter = MultiShotAdapter()
    execution_service = ProductionExecutionService(
        repository,
        production_service=production,
    )
    orchestrator = ProductionOrchestrator(
        repository,
        production_service=production,
        execution_service=execution_service,
        qc_service=ProductionQCService(repository),
        worker=ProductionWorker(execution_service, adapter, max_polls=3),
        adapter=adapter,
    )

    completed = orchestrator.run_job(project.id, job.id)

    assert completed.status is ProductionJobStatus.SUCCEEDED
    shots = repository.list_production_shots(job.id)
    assert [shot.shot_id for shot in shots] == [f"shot_{index:02d}" for index in range(1, 7)]
    assert all(shot.status is ProductionShotStatus.SUCCEEDED for shot in shots)
    assert adapter.submitted_shots == [shot.shot_id for shot in shots]
    executions = repository.list_production_executions(job.id)
    assert len(executions) == 6
    assert [next(iter(item.input_snapshot.shot_parameters)) for item in executions] == [
        shot.shot_id for shot in shots
    ]
    tasks = repository.list_provider_tasks(project.id)
    assert len(tasks) == 6
    assert len({task.idempotency_key for task in tasks}) == 6
    artifacts = [
        repository.list_production_artifacts(execution.id)[0]
        for execution in executions
    ]
    assert all(
        len(str(artifact.metadata_json.get("sha256", ""))) == 64
        for artifact in artifacts
    )

    qc = ProductionQCService(repository)
    qc_results = repository.list_production_qc_results(project.id)
    assert len(qc_results) == 6
    assert all(result.status.value == "QC_PASS" for result in qc_results)
    for result in qc_results:
        qc.create_review(
            project.id,
            result.id,
            ProductionReviewDecision.APPROVED,
            reviewer="wave1-human",
        )

    final = FinalAssemblyService(repository)
    for shot, execution, artifact in zip(shots, executions, artifacts, strict=True):
        final.select_shot_source(
            project.id,
            job.id,
            shot.id,
            production_execution_id=execution.id,
            production_artifact_id=artifact.id,
            selected_by="wave1-human",
        )
    assembly = final.create_assembly(
        project.id,
        job.id,
        freeze=True,
    )
    assembly_items = repository.list_final_assembly_items(assembly.id)
    assert [item.production_shot_id for item in assembly_items] == [shot.id for shot in shots]
    decisions = [
        repository.list_production_shot_source_decisions(project.id, shot.id)[-1]
        for shot in shots
    ]
    assert all(item.selection_kind.value == "FINAL_ACCEPTED" for item in decisions)
    assert [item.production_artifact_id for item in decisions] == [
        artifact.id for artifact in artifacts
    ]


def test_reliability_restart_timeout_double_cta_and_artifact_dedup(tmp_path) -> None:
    reliability.test_crash_after_create_restarts_with_same_remote_task(
        tmp_path / "restart"
    )
    reliability.test_poll_timeout_restart_polls_same_task_without_create(
        tmp_path / "poll-timeout"
    )
    reliability.test_download_failure_restart_reuses_task_and_artifact_identity(
        tmp_path / "download-resume"
    )
    reliability.test_double_click_and_double_step_share_one_intent_and_create(
        tmp_path / "double-cta"
    )
    reliability.test_repeated_provider_result_has_one_content_addressed_artifact(
        tmp_path / "artifact-dedupe"
    )


def test_artifact_persistence_failure_compensates_and_remains_resumable(
    tmp_path,
    monkeypatch,
) -> None:
    context = worker_contracts.context.__wrapped__(tmp_path)
    worker_contracts.test_artifact_db_failure_compensates_finalized_file(
        context,
        monkeypatch,
    )


def test_process_restart_recovery_reuses_persisted_task(tmp_path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_32_reliability_runtime_shard as reliability_runtime,
    )

    reliability_runtime.test_process_restart_recovery_reuses_persisted_task(tmp_path)


def test_double_click_idempotency_has_one_create_intent(tmp_path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_32_reliability_runtime_shard as reliability_runtime,
    )

    reliability_runtime.test_double_click_idempotency_has_one_create_intent(tmp_path)


def test_poll_timeout_recovery_keeps_task_identity(tmp_path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_32_reliability_runtime_shard as reliability_runtime,
    )

    reliability_runtime.test_poll_timeout_recovery_keeps_task_identity(tmp_path)


def test_artifact_failure_reconciles_without_duplicate_artifact(tmp_path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_32_reliability_runtime_shard as reliability_runtime,
    )

    reliability_runtime.test_artifact_failure_reconciles_without_duplicate_artifact(
        tmp_path
    )


def test_paid_budget_ledger_blocks_ninth_create_before_transport(tmp_path) -> None:
    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_32_reliability_runtime_shard as reliability_runtime,
    )

    reliability_runtime.test_paid_budget_ledger_blocks_ninth_create_before_transport(
        tmp_path
    )
