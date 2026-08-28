"""Fail-closed Wave 1 product paths.

These tests intentionally stop at the first unsafe boundary.  They use the
same repository and service contracts as the product; only provider edges are
deterministic in-process fakes.  In particular, a failed/unknown provider
outcome is never converted into a new create request by the test itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AutoAction,
    AutoRunStatus,
    FinalAssemblyStatus,
    FinalAssemblyRenderAttemptStatus,
    HeavyJobStatus,
    HeavyJobType,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
    ReferenceBindingType,
    SubtitleCue,
    SubtitleTrack,
)
from aidrama_studio.domain.continuity import (
    ContinuityIssueType,
    ContinuitySourceKind,
)
from aidrama_studio.services import (
    AutoOrchestratorService,
    ContinuityEngine,
    FinalAssemblyService,
    FinalAssemblyServiceError,
    FinalAssemblyRuntimeService,
    HeavyJobRunner,
    HeavyJobService,
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionQCService,
    ProductionService,
    ProductionWorker,
    TTSRuntimeService,
)
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio import test_continuity_engine as continuity_fixtures
from test.aidrama_studio import test_final_assembly as final_fixtures
from test.aidrama_studio import test_postproduction_service as post_fixtures
from test.aidrama_studio import test_production_execution as execution_fixtures
from test.aidrama_studio import test_production_reliability_cost_guard as reliability
from test.aidrama_studio import test_vision_universal_runtime as vision_fixtures
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_reference_agent import FakeImageProvider
from test.aidrama_studio.test_tts_runtime import FakeTTS


def _assert_offline(
    provider_calls,
    *,
    video_create: int | None = None,
    unauthorized_create: int | None = None,
    duplicate_create: int | None = None,
    automatic_retry: int | None = None,
) -> None:
    """Assert the acceptance ledger and paid-create safety counters."""

    assert provider_calls.real_provider_calls == 0
    assert provider_calls.paid == 0
    if video_create is not None:
        assert provider_calls.video_create == video_create
    if unauthorized_create is not None:
        assert unauthorized_create == 0
    if duplicate_create is not None:
        assert duplicate_create == 0
    if automatic_retry is not None:
        assert automatic_retry == 0


def _lock_reference(
    repository: ProjectRepository,
    project_id: str,
    binding_type: ReferenceBindingType,
    subject_id: str,
    *,
    color: str,
    source_story_revision_id: str = "story_001",
) -> None:
    """Create, bind and activate one real project-local reference version."""

    from aidrama_studio.services import (
        ReferenceAssetService,
        ReferenceAssetStorageService,
    )

    references = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(references)
    asset = references.ensure_workspace_asset(project_id, binding_type, subject_id)
    version = storage.import_image(
        project_id,
        asset.id,
        png_bytes(color=color),
        filename=f"{subject_id}.png",
        mime_type="image/png",
        metadata={"source_story_revision_id": source_story_revision_id},
    )
    references.bind_version(project_id, version.id, binding_type, subject_id)
    references.activate_version(project_id, asset.id, version.id)


def _reference_agent(repository: ProjectRepository):
    """Return the real reference service with an in-process image edge."""

    from aidrama_studio.services import ImageRuntimeService, ReferenceAgentService

    provider = FakeImageProvider()
    return (
        ReferenceAgentService(
            repository,
            image_runtime=ImageRuntimeService(repository, provider=provider),
        ),
        provider,
    )


@dataclass
class _Availability:
    available: bool = False


class _AvailabilityGatedAdapter(reliability._FakePaidAdapter):
    """A paid adapter whose local readiness can be toggled by the test."""

    def __init__(self, backend, availability: _Availability) -> None:
        super().__init__(backend)
        self.availability = availability

    def validate(self, snapshot) -> bool:
        return self.availability.available


class _FailingTTS(FakeTTS):
    """Provider edge which fails one durable TTS task before commit."""

    provider_name = "FAIL_FAKE_TTS"

    def synthesize(self, text: str, **kwargs):
        self.calls += 1
        raise RuntimeError("Authorization: Bearer tts-provider-secret")


class _SixShotFinalAdapter(reliability._FakeFinalAdapter):
    """Deterministic media edge matching the frozen six-shot fixture.

    The production runtime still owns manifest loading, SHA checks, attempt
    persistence and final state transitions.  This adapter only supplies
    bounded local media metadata/bytes, and never contacts a provider.
    """

    def __init__(self) -> None:
        super().__init__()
        self._expected_duration = 6.0
        self._width = 1920
        self._height = 1080
        self._fps = 30.0

    def render(self, request, output_path: Path) -> None:
        profile = dict(request.output_profile or {})
        self._width = int(profile.get("delivery_width") or self._width)
        self._height = int(profile.get("delivery_height") or self._height)
        self._fps = float(profile.get("target_fps") or self._fps)
        self._expected_duration = float(request.expected_duration or self._expected_duration)
        super().render(request, output_path)

    def probe_output(self, output_path: Path) -> dict[str, object]:
        # Frozen source probes happen before render; the temporary output probe
        # happens after render.  Keep those two physical truths distinct.
        # Source files created by ``test_final_assembly._source`` live below
        # ``production/<execution-id>/...``.  The runtime's temporary output
        # is the only path carrying the in-progress marker, so use that
        # durable seam instead of relying on a fixture-only directory name.
        if not Path(output_path).name.endswith(".in-progress.mp4"):
            return {
                "video_stream": True,
                "audio_stream": False,
                "size_bytes": Path(output_path).stat().st_size,
                "width": 1280,
                "height": 720,
                "resolution": "1280x720",
                "fps": 24.0,
                "duration_seconds": 1.0,
            }
        return {
            "video_stream": True,
            "audio_stream": False,
            "size_bytes": Path(output_path).stat().st_size,
            "width": self._width,
            "height": self._height,
            "resolution": f"{self._width}x{self._height}",
            "fps": self._fps,
            "duration_seconds": self._expected_duration,
        }


def _seed_six_shot_frozen_assembly(
    repository: ProjectRepository,
    project,
) -> tuple[str, str, str, tuple[str, ...]]:
    """Build six accepted sources through the tracked final-assembly fixtures.

    ``_shots``/``_source`` exercise the real production repository contracts;
    only the media bytes are deterministic test inputs.  The manifest itself
    is then frozen by ``FinalAssemblyService`` so the interruption test can
    prove that recovery reuses the exact source identities.
    """

    job, shots = final_fixtures._shots(repository, project, 6)
    for index, shot in enumerate(shots, start=1):
        final_fixtures._source(
            repository,
            project,
            job,
            shot,
            suffix=str(index),
            duration_seconds=1.0,
        )
    assembly = FinalAssemblyService(repository).create_assembly(
        project.id,
        job.id,
        freeze=True,
    )
    manifest = FinalAssemblyService(repository).get_manifest(project.id, assembly.id)
    return (
        project.id,
        job.id,
        assembly.id,
        tuple(item.id for item in manifest.items),
    )


def test_neg_a_missing_human_approval_blocks_final_then_exact_approval_resumes(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-A: QC PASS is not final until the exact candidate is approved."""

    repository, project = execution_fixtures.context.__wrapped__(tmp_path)
    job, shots = final_fixtures._shots(repository, project, 1)
    execution, artifact, qc_result, review = final_fixtures._source(
        repository,
        project,
        job,
        shots[0],
        suffix="neg-a",
        review=None,
    )
    assert review is None
    final = FinalAssemblyService(repository)

    blocked = final.calculate_readiness(project.id, job.id)
    assert blocked.ready is False
    assert blocked.blocked_shots == 1
    assert "等待人工审片" in " ".join(blocked.blocked_reasons)
    auto_blocked = AutoOrchestratorService(repository).next_action(project.id)
    assert auto_blocked.status is AutoRunStatus.WAITING_HUMAN
    assert auto_blocked.current_stage.value == "REVIEW"
    assert auto_blocked.next_action is AutoAction.WAITING_HUMAN
    assert auto_blocked.requires_human is True
    assert auto_blocked.blocking_reason == "APPROVE_OR_REJECT_PRODUCTION_REVIEW"
    assert auto_blocked.requested_action == "APPROVE_OR_REJECT_PRODUCTION_REVIEW"
    with pytest.raises(FinalAssemblyServiceError, match="等待人工审片"):
        final.select_qualified_source(project.id, job.id, shots[0].id)

    # A cold repository must observe the same blocked truth, not a process
    # local review cache.
    cold = ProjectRepository(repository.paths)
    assert FinalAssemblyService(cold).calculate_readiness(
        project.id, job.id
    ).ready is False

    approved = repository.create_production_review(
        ProductionReview(
            id="neg-a-human-approval",
            project_id=project.id,
            qc_result_id=qc_result.id,
            decision=ProductionReviewDecision.APPROVED,
            reviewer="wave1-human",
            created_at="2099-01-01T00:00:00+00:00",
        )
    )
    selected = FinalAssemblyService(
        ProjectRepository(repository.paths)
    ).select_qualified_source(project.id, job.id, shots[0].id)
    assert selected.production_execution_id == execution.id
    assert selected.production_artifact_id == artifact.id
    assert selected.review_id == approved.id
    assert FinalAssemblyService(repository).calculate_readiness(
        project.id, job.id
    ).ready is True
    assert len(repository.list_production_reviews(project.id, qc_result.id)) == 1
    cold_auto = AutoOrchestratorService(
        ProjectRepository(repository.paths)
    ).next_action(project.id)
    assert cold_auto.status is not AutoRunStatus.WAITING_HUMAN
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=len(repository.list_provider_tasks(project.id)),
        duplicate_create=0,
        automatic_retry=0,
    )


def test_neg_b_missing_locked_reference_blocks_production_and_auto_then_resumes(
    canonical_approved_project,
    provider_calls,
) -> None:
    """NEG-B: a candidate/unbound reference cannot make Production ready."""

    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    _lock_reference(
        repository,
        project.id,
        ReferenceBindingType.CHARACTER,
        "character_lin",
        color="black",
    )
    _lock_reference(
        repository,
        project.id,
        ReferenceBindingType.LOCATION,
        "location_bookshop_exterior",
        color="blue",
    )
    production = ProductionService(repository)

    blocked = production.validate_job_readiness(project.id, "shot_plan_001")
    assert blocked["ready"] is False
    assert "character_su" in blocked["missing_character_references"]
    assert "location_bookshop_interior" in blocked["missing_location_references"]

    auto = AutoOrchestratorService(repository).next_action(project.id)
    assert auto.current_stage.value == "REFERENCES"
    assert auto.status is AutoRunStatus.IDLE
    assert auto.next_action is AutoAction.GENERATE_REFERENCE_CANDIDATE
    assert auto.blocking_reason is None
    assert "reference" in f"{auto.why} {auto.blocking_reason}".lower()

    chain_before = (
        repository.get_story_revision("story_001")["id"],
        repository.get_script_revision("script_001")["id"],
        repository.get_shot_revision("shot_plan_001")["id"],
    )

    agent, image_provider = _reference_agent(repository)
    evaluation = agent.evaluate(project.id)
    action_ids = [item.id for item in evaluation.next_actions]
    assert {item.subject_id for item in evaluation.missing} == {
        "character_su",
        "location_bookshop_interior",
    }
    authorization = agent.generation_authorization(
        project.id,
        action_ids,
        max_creates=len(action_ids),
        approved_by="wave1-human",
        approved=True,
    )
    candidates = agent.generate_candidates(
        project.id,
        action_ids,
        authorization=authorization,
    )
    for candidate in candidates:
        version = agent.approve_candidate_and_bind(
            project.id,
            candidate.candidate_id,
            human_confirmed=True,
            actor="wave1-human",
        )
        agent.lock_bound_reference(project.id, version.id, human_confirmed=True)

    assert len(candidates) == len(image_provider.calls) == 2
    cold_repository = ProjectRepository(repository.paths)
    resumed = ProductionService(cold_repository).validate_job_readiness(
        project.id, "shot_plan_001"
    )
    assert resumed["ready"] is True
    resumed_auto = AutoOrchestratorService(cold_repository).next_action(project.id)
    assert resumed_auto.current_stage.value == "PRODUCTION"
    assert resumed_auto.next_action is AutoAction.PREPARE_PRODUCTION
    assert (
        cold_repository.get_story_revision("story_001")["id"],
        cold_repository.get_script_revision("script_001")["id"],
        cold_repository.get_shot_revision("shot_plan_001")["id"],
    ) == chain_before
    # Image candidates are local fakes; they are not paid video creates.
    provider_calls.image = len(image_provider.calls)
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=len(repository.list_provider_tasks(project.id)),
        duplicate_create=0,
        automatic_retry=0,
    )


def test_neg_c_unavailable_video_runtime_fails_before_create_then_one_authorized_create(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-C: unavailable runtime is FAILED, never fake success."""

    repository = ProjectRepository(reliability._paths(tmp_path))
    project_id, job_id = reliability._seed_job(repository, shot_count=2)
    backend = reliability._FakeProviderBackend()
    availability = _Availability()
    adapter = _AvailabilityGatedAdapter(backend, availability)
    first = reliability._new_execution(repository, project_id, job_id, 1)
    reliability._authorize(repository, project_id, job_id, 1)

    unavailable = ProductionWorker(
        ProductionExecutionService(repository), adapter, max_polls=1
    ).run(project_id, first.id)
    assert unavailable.status.value == "FAILED"
    assert backend.submit_calls == 0
    unauthorized_create_count = backend.submit_calls
    failed_task = next(
        task
        for task in repository.list_provider_tasks(project_id)
        if task.execution_id == first.id
    )
    assert failed_task.state == "FAILED"
    assert "runtime adapter" in (failed_task.error_message or "")
    assert failed_task.provider_task_id is None
    execution_ids_before_resume = {
        item.id for item in repository.list_production_executions(job_id)
    }

    # Resumption is explicit: the failed execution is not silently retried.
    availability.available = True
    second = reliability._new_execution(repository, project_id, job_id, 2)
    resumed = ProductionWorker(
        ProductionExecutionService(repository), adapter, max_polls=1
    ).run(project_id, second.id)
    assert resumed.status.value == "SUCCEEDED"
    assert backend.submit_calls == 1
    tasks = repository.list_provider_tasks(project_id)
    assert len(tasks) == 2
    successful_tasks = [item for item in tasks if item.execution_id == second.id]
    assert len(successful_tasks) == 1
    assert successful_tasks[0].provider_task_id == "remote-task-1"
    assert repository.get_production_execution(first.id).status.value == "FAILED"
    execution_ids_after_resume = {
        item.id for item in repository.list_production_executions(job_id)
    }
    # The only new execution is the one explicitly created by this test;
    # worker failure does not synthesize an automatic retry.
    assert execution_ids_after_resume - execution_ids_before_resume == {second.id}
    automatic_retry_count = len(
        execution_ids_after_resume - execution_ids_before_resume - {second.id}
    )
    duplicate_create_count = backend.submit_calls - len(successful_tasks)
    provider_calls.video_create = backend.submit_calls
    _assert_offline(
        provider_calls,
        video_create=1,
        unauthorized_create=unauthorized_create_count,
        duplicate_create=duplicate_create_count,
        automatic_retry=automatic_retry_count,
    )
    # One provider identity and one transport call prove DUPLICATE_CREATE=0;
    # the explicit execution-set assertion above proves AUTOMATIC_RETRY=0.
    assert len({item.provider_task_id for item in successful_tasks}) == 1


def test_neg_d_missing_paid_authorization_stops_before_transport_then_same_intent_submits_once(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-D: the paid gate is checked before entering fake transport."""

    repository = ProjectRepository(reliability._paths(tmp_path))
    project_id, job_id = reliability._seed_job(repository)
    execution = reliability._new_execution(repository, project_id, job_id, 1)
    backend = reliability._FakeProviderBackend()
    adapter = reliability._FakePaidAdapter(backend)
    service = ProductionExecutionService(repository)

    with pytest.raises(
        ProductionExecutionServiceError,
        match="PAID_BUDGET_MISSING: provider create blocked before transport",
    ):
        service.submit_execution(project_id, execution.id, adapter)
    assert backend.submit_calls == 0
    unauthorized_create_count = backend.submit_calls
    assert repository.get_production_execution(execution.id).status.value == "QUEUED"
    task = next(
        item
        for item in repository.list_provider_tasks(project_id)
        if item.execution_id == execution.id
    )
    assert task.provider_task_id is None
    assert task.state == "PENDING_SUBMISSION"
    projection = reliability.PaidBudgetService(repository).projection(
        project_id, job_id, execution_id=execution.id
    )
    assert projection.authorized_max == 0
    assert projection.remaining_creates == 0
    cold_before_authorization = ProjectRepository(repository.paths)
    cold_task = next(
        item
        for item in cold_before_authorization.list_provider_tasks(project_id)
        if item.execution_id == execution.id
    )
    assert cold_task.state == "PENDING_SUBMISSION"
    assert cold_task.provider_task_id is None

    reliability._authorize(repository, project_id, job_id, 1)
    started = ProductionExecutionService(
        cold_before_authorization
    ).submit_execution(project_id, execution.id, adapter)
    assert started.status.value == "RUNNING"
    assert backend.submit_calls == 1
    submitted = cold_before_authorization.list_provider_tasks(project_id)
    assert len(submitted) == 1
    assert submitted[0].provider_task_id == "remote-task-1"
    provider_calls.video_create = backend.submit_calls
    duplicate_create_count = backend.submit_calls - len(submitted)
    automatic_retry_count = len(submitted) - 1
    _assert_offline(
        provider_calls,
        video_create=1,
        unauthorized_create=unauthorized_create_count,
        duplicate_create=duplicate_create_count,
        automatic_retry=automatic_retry_count,
    )
    # The same persisted intent transitions once from PENDING_SUBMISSION to
    # PROVIDER_ACCEPTED; no second task or implicit retry is created.
    assert len({item.id for item in submitted}) == 1
    assert len({item.provider_task_id for item in submitted}) == 1


def test_neg_e_vision_failure_is_persisted_advisory_and_retry_does_not_create_video(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-E: Vision errors are sanitized/advisory; QC/Human truth survives."""

    (
        repository,
        project,
        execution,
        artifact,
        _target,
        _plan,
        _brief,
        _bindings,
    ) = vision_fixtures._vision_context(tmp_path)
    # Seed non-empty technical and human truth so the advisory Vision failure
    # must demonstrably preserve existing decisions, not merely preserve an
    # empty fixture.
    qc_service = ProductionQCService(repository)
    qc = qc_service.run_qc(project.id, execution.id, artifact.id)
    # Depending on the shared fixture's physical traceability metadata, the
    # real Technical QC service may pass or fail; either is formal, persisted
    # technical truth.  The negative path must not overwrite or upgrade it.
    assert qc.status in {
        ProductionQCStatus.QC_PASS,
        ProductionQCStatus.QC_FAILED,
    }
    human = qc_service.create_review(
        project.id,
        qc.id,
        ProductionReviewDecision.REJECTED,
        reviewer="wave1-human",
    )
    technical_before = tuple(
        repository.list_production_qc_results(project.id, execution.id)
    )
    human_before = tuple(repository.list_production_reviews(project.id))
    executions_before = tuple(repository.list_production_executions(execution.production_job_id))
    tasks_before = tuple(repository.list_provider_tasks(project.id))

    failed_session = vision_fixtures.FakeVisionSession("exception")
    failed_service, *_unused = vision_fixtures._wired_service(
        repository, project, failed_session
    )
    failed = failed_service.analyze(project.id, execution.id, artifact.id)
    assert failed.status == "FAILED"
    assert failed.analysis_id is not None
    assert "sk-provider-secret" not in failed.reason
    records = repository.list_vision_analyses(project.id, execution.id)
    assert records and records[-1].status == "FAILED"
    assert "sk-provider-secret" not in repr(records[-1])
    assert tuple(repository.list_production_qc_results(project.id, execution.id)) == technical_before
    assert tuple(repository.list_production_reviews(project.id)) == human_before
    assert tuple(repository.list_production_executions(execution.production_job_id)) == executions_before
    assert tuple(repository.list_provider_tasks(project.id)) == tasks_before
    cold_failed = ProjectRepository(repository.paths)
    cold_records = cold_failed.list_vision_analyses(project.id, execution.id)
    assert cold_records and cold_records[-1].status == "FAILED"
    assert cold_failed.list_production_reviews(project.id, qc.id) == [human]

    retry_session = vision_fixtures.FakeVisionSession("valid")
    retry_service, *_unused = vision_fixtures._wired_service(
        cold_failed, project, retry_session
    )
    retried = retry_service.analyze(project.id, execution.id, artifact.id)
    assert retried.status == "AI_ANALYSIS"
    assert len(cold_failed.list_vision_analyses(project.id, execution.id)) == 2
    assert tuple(cold_failed.list_production_qc_results(project.id, execution.id)) == technical_before
    assert cold_failed.list_production_reviews(project.id, qc.id) == [human]
    assert tuple(cold_failed.list_production_executions(execution.production_job_id)) == executions_before
    assert tuple(cold_failed.list_provider_tasks(project.id)) == tasks_before
    assert len(failed_session.calls) == 1
    assert len(retry_session.calls) == 1
    provider_calls.vision = len(failed_session.calls) + len(retry_session.calls)
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=len(cold_failed.list_provider_tasks(project.id)),
        duplicate_create=0,
        automatic_retry=0,
    )


def test_neg_f_severe_continuity_is_blocked_and_only_recommends_human_repair(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-F: severe continuity drift only recommends human repair."""

    repository = continuity_fixtures.repository.__wrapped__(tmp_path)
    engine = continuity_fixtures.engine.__wrapped__(repository)
    expected = continuity_fixtures._facts(
        "shot-03",
        identity="lin",
        wardrobe=continuity_fixtures._wardrobe("black"),
        props=continuity_fixtures._umbrella(),
    )
    drifted = continuity_fixtures._facts(
        "shot-03",
        identity="lin",
        wardrobe=continuity_fixtures._wardrobe("white"),
        props=(),
    )
    before_executions = tuple(repository.list_production_executions("project-continuity"))
    before_tasks = tuple(repository.list_provider_tasks("project-continuity"))
    result = engine.evaluate_shot(
        continuity_fixtures._request(
            "shot-03",
            3,
            (
                continuity_fixtures._candidate(
                    expected,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-lock-shot-03",
                ),
            ),
            (
                continuity_fixtures._candidate(
                    drifted,
                    ContinuitySourceKind.VISION_QC_OBSERVATION,
                    "fake-vision-shot-03",
                ),
            ),
        ),
        persist=True,
    )
    issue_types = {item.issue_type for item in result.issues}
    assert {
        ContinuityIssueType.WARDROBE_DRIFT,
        ContinuityIssueType.PROP_DRIFT,
    } <= issue_types
    severity_by_type = {item.issue_type: item.severity.value for item in result.issues}
    assert severity_by_type[ContinuityIssueType.WARDROBE_DRIFT] == "HIGH"
    assert severity_by_type[ContinuityIssueType.PROP_DRIFT] == "HIGH"
    projection = ContinuityEngine.project_for_ui(result)
    assert projection.status == "BLOCKED"
    assert projection.warnings
    assert projection.shot_id == "shot-03"
    assert all(item.requires_human_confirmation for item in result.repair_recommendations)
    assert any(item.requires_paid_create for item in result.repair_recommendations)
    persisted_issues = repository.list_continuity_issues("project-continuity")
    persisted_recommendations = repository.list_continuity_repair_recommendations(
        "project-continuity"
    )
    assert persisted_issues
    assert persisted_recommendations
    assert {item.shot_id for item in persisted_issues} == {"shot-03"}
    assert {item.shot_id for item in persisted_recommendations} == {"shot-03"}
    assert tuple(repository.list_production_executions("project-continuity")) == before_executions
    assert tuple(repository.list_provider_tasks("project-continuity")) == before_tasks

    corrected = engine.evaluate_shot(
        continuity_fixtures._request(
            "shot-03",
            3,
            (
                continuity_fixtures._candidate(
                    expected,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-lock-shot-03",
                ),
            ),
            (
                continuity_fixtures._candidate(
                    expected,
                    ContinuitySourceKind.HUMAN_LOCKED_STATE,
                    "human-correction-shot-03",
                ),
            ),
            approved_for_continuity=True,
        ),
        persist=True,
    )
    assert corrected.issues == ()
    assert ContinuityEngine.project_for_ui(corrected).status == "PASS"
    provider_calls.video_create = 0
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=len(repository.list_provider_tasks("project-continuity")),
        duplicate_create=0,
        automatic_retry=0,
    )


def test_neg_g_tts_failure_preserves_picture_final_and_delivery_is_not_ready(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-G: one failed TTS task cannot publish a partial Delivery Final.

    The first task is allowed to complete and becomes durable VoiceTrack
    truth.  A separate task then fails at the provider edge; retrying that
    failed task keeps the completed track and its identity intact.
    """

    repository, project = execution_fixtures.context.__wrapped__(tmp_path)
    repository, project, post, assembly = post_fixtures._plan((repository, project))
    plan = post.create_plan(project.id, assembly.id)
    cues = [
        SubtitleCue(
            id="neg-g-cue-1",
            text="第一句",
            start_seconds=0,
            end_seconds=0.5,
            shot_id="shot_001",
            beat_id="beat-1",
        ),
        SubtitleCue(
            id="neg-g-cue-2",
            text="第二句",
            start_seconds=0.5,
            end_seconds=1.0,
            shot_id="shot_001",
            beat_id="beat-2",
        ),
    ]

    subtitle_first = repository.create_post_subtitle_track(
        SubtitleTrack(
            id="neg-g-subtitle-1",
            project_id=project.id,
            plan_id=plan.id,
            source_script_revision_id="script_001",
            cues=[cues[0]],
            created_at="now",
            updated_at="now",
        )
    )
    subtitle_failed = repository.create_post_subtitle_track(
        SubtitleTrack(
            id="neg-g-subtitle-2",
            project_id=project.id,
            plan_id=plan.id,
            source_script_revision_id="script_001",
            cues=[cues[1]],
            created_at="now",
            updated_at="now",
        )
    )
    picture_before = repository.get_final_assembly(assembly.id)
    manifest_before = tuple(repository.list_final_assembly_items(assembly.id))
    attempts_before = tuple(post.list_render_attempts(project.id, plan.id))

    heavy_service = HeavyJobService(repository)
    completed_job = heavy_service.enqueue_tts(
        project.id,
        plan_id=plan.id,
        script_revision_id="script_001",
        subtitle_track_id=subtitle_first.id,
        idempotency_key="neg-g-tts-completed",
    )
    failed_job = heavy_service.enqueue_tts(
        project.id,
        plan_id=plan.id,
        script_revision_id="script_001",
        subtitle_track_id=subtitle_failed.id,
        idempotency_key="neg-g-tts-failure",
    )

    def tts_handler(provider):
        def handle(job, context):
            context.stage("SYNTHESIZING_TTS")
            snapshot = job.input_snapshot
            track = TTSRuntimeService(
                repository, provider=provider
            ).synthesize_track(
                project.id,
                str(snapshot["plan_id"]),
                list(snapshot["cues"]),
                script_revision_id=str(snapshot["script_revision_id"]),
                subtitle_track_id=str(snapshot["subtitle_track_id"]),
                voice_assignments=dict(snapshot.get("voice_assignments") or {}),
                default_voice=str(snapshot.get("default_voice") or ""),
                track_id=str(snapshot["track_id"]),
            )
            return {"voice_track_id": track.id}

        return handle

    completed = HeavyJobRunner(
        repository,
        handlers={HeavyJobType.TTS: tts_handler(FakeTTS())},
    ).run_once(project.id)
    assert completed is not None
    assert completed.id == completed_job.id
    assert completed.status is HeavyJobStatus.SUCCEEDED
    completed_track_id = str(completed_job.input_snapshot["track_id"])
    completed_track = repository.get_post_voice_track(completed_track_id)
    assert completed_track is not None
    completed_track_before = completed_track.model_copy(deep=True)

    failing_provider = _FailingTTS()
    failed = HeavyJobRunner(
        repository,
        handlers={HeavyJobType.TTS: tts_handler(failing_provider)},
    ).run_once(project.id)
    assert failed is not None
    assert failed.id == failed_job.id
    assert failed.status is HeavyJobStatus.FAILED
    # HeavyJob is the durable failure boundary and stores only a sanitized
    # error, while the disposable segment directory is removed.
    assert "tts-provider-secret" not in (failed.safe_error or "")
    assert repository.list_post_voice_tracks(project.id, plan.id) == [completed_track]
    assert repository.get_post_voice_track(completed_track_id) == completed_track_before
    assert repository.get_final_assembly(assembly.id) == picture_before
    assert tuple(repository.list_final_assembly_items(assembly.id)) == manifest_before
    assert tuple(post.list_render_attempts(project.id, plan.id)) == attempts_before
    assert post.resolve_output_path(project.id, plan.id) is None
    current = ProjectRepository(repository.paths)
    assert not current.list_post_render_attempts(project.id, plan.id)
    assert current.get_post_voice_track(completed_track_id) == completed_track_before
    # CurrentProductionState derives Delivery Final from a successful, pinned
    # post render; a TTS exception leaves that formal projection false.
    from aidrama_studio.services import CurrentProductionStateService

    assert CurrentProductionStateService(repository).derive(
        project.id
    ).post_production_ready is False
    failed_track_id = str(failed_job.input_snapshot["track_id"])
    voice_root = (
        repository.paths.projects
        / project.id
        / "post"
        / plan.id
        / "voice"
        / failed_track_id
    )
    assert not voice_root.exists()

    retry_provider = FakeTTS()
    retry_job = HeavyJobService(current).retry(failed.id)
    assert retry_job.retry_of_job_id == failed.id
    assert retry_job.input_snapshot["track_id"] == failed_job.input_snapshot["track_id"]

    retried_job = HeavyJobRunner(
        current,
        handlers={HeavyJobType.TTS: tts_handler(retry_provider)},
    ).run_once(project.id)
    assert retried_job is not None and retried_job.id == retry_job.id
    assert retried_job.status is HeavyJobStatus.SUCCEEDED
    tracks = current.list_post_voice_tracks(project.id, plan.id)
    assert len(tracks) == 2
    assert {track.id for track in tracks} == {
        completed_track_id,
        failed_track_id,
    }
    assert current.get_post_voice_track(completed_track_id) == completed_track_before
    assert current.get_post_voice_track(failed_track_id) is not None
    assert current.get_final_assembly(assembly.id) == picture_before
    assert tuple(current.list_final_assembly_items(assembly.id)) == manifest_before
    assert len(current.list_post_render_attempts(project.id, plan.id)) == len(
        attempts_before
    )
    retry_jobs = [
        item
        for item in current.list_heavy_jobs(project.id)
        if item.retry_of_job_id is not None
    ]
    assert [item.id for item in retry_jobs] == [retry_job.id]
    provider_calls.tts = failing_provider.calls + retry_provider.calls
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=len(current.list_provider_tasks(project.id)),
        duplicate_create=0,
        automatic_retry=0,
    )


def test_neg_h_final_assembly_interruption_resumes_frozen_sources_without_video_create(
    tmp_path: Path,
    provider_calls,
) -> None:
    """NEG-H: restart resumes frozen sources without another production create."""

    repository, project = execution_fixtures.context.__wrapped__(tmp_path)
    paths = repository.paths
    project_id, _job_id, assembly_id, item_ids = _seed_six_shot_frozen_assembly(
        repository,
        project,
    )
    provider_tasks_before = tuple(repository.list_provider_tasks(project_id))
    production_executions_before = tuple(
        repository.list_production_executions(_job_id)
    )
    frozen_before = tuple(
        (
            item.id,
            item.order_index,
            item.production_execution_id,
            item.production_artifact_id,
            item.source_sha256,
        )
        for item in repository.list_final_assembly_items(assembly_id)
    )
    heavy = HeavyJobService(repository).enqueue_final_assembly(
        project_id, assembly_id
    )
    claimed = repository.claim_next_heavy_job(
        started_at=reliability._now(), event_id="heavy-started"
    )
    assert claimed is not None and claimed.id == heavy.id
    assert claimed.status is HeavyJobStatus.RUNNING
    attempt_id = str(heavy.input_snapshot["attempt_id"])
    repository.update_final_assembly_render_attempt(
        attempt_id,
        status=FinalAssemblyRenderAttemptStatus.RUNNING,
        started_at=reliability._now(),
    )
    repository.update_final_assembly_status(
        assembly_id,
        FinalAssemblyStatus.ASSEMBLING,
        updated_at=reliability._now(),
    )
    assert repository.get_heavy_job(heavy.id).status.value == "RUNNING"
    assert repository.get_final_assembly_render_attempt(attempt_id).status is FinalAssemblyRenderAttemptStatus.RUNNING

    cold_repository = ProjectRepository(paths)
    assert tuple(
        (
            item.id,
            item.order_index,
            item.production_execution_id,
            item.production_artifact_id,
            item.source_sha256,
        )
        for item in cold_repository.list_final_assembly_items(assembly_id)
    ) == frozen_before

    def render_final(job, context):
        context.stage("RECOVERING_FROZEN_MANIFEST")
        snapshot = job.input_snapshot
        attempt = FinalAssemblyRuntimeService(
            cold_repository,
            adapter=_SixShotFinalAdapter(),
        ).render_prepared(
            str(job.project_id),
            str(snapshot["assembly_id"]),
            str(snapshot["attempt_id"]),
        )
        return {"attempt_id": attempt.id, "output_relative_path": attempt.output_relative_path}

    summary = HeavyJobRunner(
        cold_repository,
        handlers={HeavyJobType.FINAL_ASSEMBLY_RENDER: render_final},
    ).resume_pending_work(project_id)
    assert len(summary["interrupted"]) == 1
    assert summary["interrupted"][0].status is HeavyJobStatus.INTERRUPTED
    assert len(summary["recovered"]) == 1
    assert len(summary["completed"]) == 1
    # Crash recovery intentionally creates one local recovery HeavyJob.  Its
    # retry_of_job_id and frozen attempt identity tie it to the interrupted
    # job; a new production/provider create is not implied.
    recovered_job = summary["recovered"][0]
    assert recovered_job.retry_of_job_id == heavy.id
    completed_job = summary["completed"][0]
    assert completed_job.id == recovered_job.id
    assert completed_job.status is HeavyJobStatus.SUCCEEDED
    assert recovered_job.input_snapshot["assembly_id"] == assembly_id
    assert recovered_job.input_snapshot["required_project_paths"] == heavy.input_snapshot[
        "required_project_paths"
    ]
    manifest = reliability.FinalAssemblyRuntimeService(
        cold_repository, adapter=_SixShotFinalAdapter()
    ).manifest_service.get_manifest(project_id, assembly_id)
    assert tuple(item.id for item in manifest.items) == item_ids
    assert len(manifest.items) == 6
    assert [item.order_index for item in manifest.items] == [1, 2, 3, 4, 5, 6]
    frozen_after = tuple(
        (
            item.id,
            item.order_index,
            item.production_execution_id,
            item.production_artifact_id,
            item.source_sha256,
        )
        for item in cold_repository.list_final_assembly_items(assembly_id)
    )
    assert frozen_after == frozen_before
    attempts = cold_repository.list_final_assembly_render_attempts(assembly_id)
    successful_attempts = [
        item
        for item in attempts
        if item.status is FinalAssemblyRenderAttemptStatus.SUCCEEDED
    ]
    assert len(successful_attempts) == 1
    successful_trace = successful_attempts[0].metadata_json.get("source_items")
    assert isinstance(successful_trace, list)
    assert [
        (
            int(item["order_index"]),
            str(item["production_artifact_id"]),
            str(item["source_sha256"]),
        )
        for item in successful_trace
    ] == [
        (item.order_index, item.production_artifact_id, str(item.source_sha256))
        for item in manifest.items
    ]
    assert len(attempts) == 2
    assert sum(
        item.status is FinalAssemblyRenderAttemptStatus.FAILED for item in attempts
    ) == 1
    assert cold_repository.get_final_assembly(assembly_id).status.value == "SUCCEEDED"
    provider_tasks_after = tuple(cold_repository.list_provider_tasks(project_id))
    assert provider_tasks_after == provider_tasks_before
    assert tuple(
        cold_repository.list_production_executions(_job_id)
    ) == production_executions_before
    provider_calls.video_create = 0
    # The only retry is the explicit local HeavyJob recovery.  No production
    # execution/provider intent was created, so both paid safety counters stay
    # at zero (DUPLICATE_CREATE=0 and AUTOMATIC_RETRY=0).
    duplicate_create = len(provider_tasks_after) - len(provider_tasks_before)
    assert duplicate_create == 0
    automatic_retry_jobs = [
        job
        for job in cold_repository.list_heavy_jobs(project_id)
        if job.retry_of_job_id is not None and job.id != recovered_job.id
    ]
    automatic_retry = len(automatic_retry_jobs)
    _assert_offline(
        provider_calls,
        video_create=0,
        unauthorized_create=duplicate_create,
        duplicate_create=duplicate_create,
        automatic_retry=automatic_retry,
    )
