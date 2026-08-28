from __future__ import annotations

import socket
import urllib.request
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from aidrama_studio.domain import (
    CameraAngle,
    CameraMovement,
    Character,
    Location,
    ProductionExecutionStatus,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotFirstFrameSourceType,
    ShotKeyframePlanningSnapshot,
    ShotPlan,
    ShotRevisionStatus,
    ShotSize,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
)
from aidrama_studio.services import (
    GenerationBriefService,
    ProductionService,
    ProjectService,
)
from aidrama_studio.services.production_queue import (
    ProductionQueueError,
    ProductionQueueService,
)
from aidrama_studio.services.reference_assets import ReferenceAssetService
from aidrama_studio.services.shot_keyframe import (
    ShotKeyframeError,
    ShotKeyframePolicy,
    ShotKeyframeService,
    UniversalImageBinding,
    UniversalShotKeyframeImageService,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_shot_keyframe_continuity import (
    _FakeImageRuntime,
    _build_context,
    _compile_keyframe_briefs,
    _freeze_runtime_plans,
    _image_binding,
)


_NOW = "2026-08-28T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _offline_temp_database_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    isolated_default = tmp_path / "isolated-default-data"
    forbidden_default = tmp_path / "default-localappdata-must-remain-empty"
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(isolated_default))
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    monkeypatch.setenv("LOCALAPPDATA", str(forbidden_default))
    for variable in (
        "AIDRAMA_ALLOW_PAID",
        "AIDRAMA_ALLOW_PAID_LIVE_TESTS",
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "ARK_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("REAL_PROVIDER_OR_NETWORK_CALL_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(requests.sessions.Session, "request", deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", deny_network)
    yield
    assert not forbidden_default.exists()


class _UncertainImageRuntime:
    def __init__(self) -> None:
        self.submit_calls = 0

    def submit(self, request, *, authorization=None):
        assert request.create_authorized is True
        assert authorization == {"approved": True, "create_authorized": True}
        self.submit_calls += 1
        raise TimeoutError(
            "POST https://provider.invalid/create Authorization: Bearer secret"
        )

    def read_output(self, _output):
        raise AssertionError("uncertain transport cannot have output bytes")


class _SubmitMustNotRun:
    def __init__(self) -> None:
        self.submit_calls = 0

    def submit(self, _request, *, authorization=None):
        self.submit_calls += 1
        raise AssertionError(
            f"pre-created successful keyframe was resubmitted: {authorization!r}"
        )

    def read_output(self, _output):
        raise AssertionError("pre-created successful keyframe must use its artifact")


class _SimulatedProcessLoss(BaseException):
    pass


class _InterruptedThenForbiddenRuntime:
    """Leave the first durable claim SUBMITTING; a second call is a test failure."""

    def __init__(self) -> None:
        self.submit_calls = 0

    def submit(self, request, *, authorization=None):
        assert request.create_authorized is True
        assert authorization == {"approved": True, "create_authorized": True}
        self.submit_calls += 1
        if self.submit_calls == 1:
            raise _SimulatedProcessLoss("process exited after durable claim")
        raise AssertionError("SECOND_SHOT_TRANSPORT_CALLED_AFTER_SUBMITTING")

    def read_output(self, _output):
        raise AssertionError("interrupted create has no output bytes")


class _RuntimePlanProbeRuntime:
    """Assert durable plan/task/execution identity at the transport boundary."""

    def __init__(self, repository: ProjectRepository, content: bytes) -> None:
        self.repository = repository
        self.delegate = _FakeImageRuntime((content,))
        self.observed: dict[str, object] | None = None

    @property
    def requests(self):
        return self.delegate.requests

    def submit(self, request, *, authorization=None):
        plan = self.repository.get_runtime_plan(str(request.runtime_plan_id))
        execution = self.repository.get_production_execution(str(request.execution_id))
        tasks = [
            task
            for task in self.repository.list_provider_tasks(request.project_id)
            if task.execution_id == request.execution_id
        ]
        assert plan is not None
        assert execution is not None
        assert execution.runtime_plan_id == plan.id
        assert len(tasks) == 1
        assert tasks[0].state == "SUBMITTING"
        self.observed = {
            "plan": plan,
            "execution": execution,
            "task": tasks[0],
            "request": request,
        }
        return self.delegate.submit(request, authorization=authorization)

    def read_output(self, output):
        return self.delegate.read_output(output)


class _PreLiveKeyframeSeam:
    def __init__(self, report, frame) -> None:
        self.report = report
        self.frame = frame
        self.freeze_calls = 0

    def require_pre_live(self, _project_id: str, _production_job_id: str):
        return self.report, {self.frame.shot_id: self.frame}

    def freeze_snapshot(self, *_args, **_kwargs):
        self.freeze_calls += 1
        raise AssertionError("mismatched Reference provenance must not be frozen")


def _paths(tmp_path: Path, name: str) -> DatabasePaths:
    root = tmp_path / name
    return DatabasePaths(
        database=root / "aidrama.db",
        projects=root / "projects",
        archived_projects=root / "archived-projects",
    )


def _approved_chain_without_references(tmp_path: Path):
    repository = ProjectRepository(_paths(tmp_path, "planning-no-references"))
    project = ProjectService(repository).create(
        title="Keyframe planning without references",
        target_duration_seconds=10,
        delivery_resolution_label="720p",
        target_fps=24,
        quality_mode="FINAL",
    )
    story = StoryBible(
        title="Exact approved story",
        logline="A traveler crosses an empty platform.",
        premise="The same traveler advances through two shots.",
        genre="Drama",
        tone="Restrained",
        world=World(era="Present", setting="Empty platform"),
        characters=[
            Character(
                id="char_001",
                name="Traveler",
                identity="one exact approved identity",
                appearance="short dark hair and a navy coat",
            )
        ],
        locations=[
            Location(
                id="loc_001",
                name="Platform",
                environment="empty concrete platform",
                visual_style="cool dawn light",
            )
        ],
        story_beats=[
            StoryBeat(
                id=f"story_beat_{index:03d}",
                order=index,
                type=("OPENING", "DEVELOPMENT", "ENDING")[index - 1],
                summary=f"Approved beat {index}",
                characters=["char_001"],
                location_id="loc_001",
            )
            for index in range(1, 4)
        ],
    )
    repository.create_story_revision(
        revision_id="story_revision_exact",
        project_id=project.id,
        version=1,
        status=StoryRevisionStatus.APPROVED,
        content=story,
        generation_input={"fixture": "offline-no-refs"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    script = StructuredScript(
        title="Exact approved script",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Platform crossing",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=10,
                beats=[
                    ScriptBeat(
                        id=f"script_beat_{index:03d}",
                        order=index,
                        type=ScriptBeatType.ACTION,
                        text=f"Traveler action {index}",
                        estimated_duration_seconds=5,
                    )
                    for index in range(1, 3)
                ],
                source_story_beat_ids=[
                    "story_beat_001",
                    "story_beat_002",
                    "story_beat_003",
                ],
            )
        ],
    )
    repository.create_script_revision(
        revision_id="script_revision_exact",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_revision_exact",
        content=script,
        generation_input={"fixture": "offline-no-refs"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    shots = tuple(
        Shot(
            id=f"shot_{index:03d}",
            order=index,
            scene_id="scene_001",
            source_script_beat_ids=[f"script_beat_{index:03d}"],
            duration_seconds=5,
            shot_size=(ShotSize.WIDE if index == 1 else ShotSize.MEDIUM),
            camera_angle=CameraAngle.EYE_LEVEL,
            camera_movement=(
                CameraMovement.STATIC if index == 1 else CameraMovement.TRACK
            ),
            composition=f"Exact approved composition {index}",
            subject=["char_001"],
            action=f"Traveler action {index}",
            visual_intent=f"Exact approved visual intent {index}",
        )
        for index in range(1, 3)
    )
    repository.create_shot_revision(
        revision_id="shot_plan_revision_exact",
        project_id=project.id,
        version=1,
        status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_revision_exact",
        content=ShotPlan(
            title="Exact approved two-shot plan",
            source_script_revision_id="script_revision_exact",
            shots=list(shots),
        ),
        generation_input={"fixture": "offline-no-refs"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    production = ProductionService(repository)
    job = production.create_production_job(
        project.id, "shot_plan_revision_exact"
    )
    return repository, project, job, shots


def _prepared_paid_case(tmp_path: Path):
    context = _build_context(tmp_path)
    service = ShotKeyframeService(context.repository)
    briefs = _compile_keyframe_briefs(context, service)
    selections = tuple(
        ShotKeyframePolicy.select(shot, project_id=context.project.id)
        for shot in context.shots
    )
    assert all(
        item.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME
        for item in selections
    )
    return context, service, briefs, selections


def _authorize(
    service: ShotKeyframeService,
    context,
    fingerprint: str,
    binding,
    briefs,
    *,
    provider_parameters: Mapping[str, object] | None = None,
):
    parameters = dict(provider_parameters or {"resolution": "1024*1024"})
    runtime_plans = _freeze_runtime_plans(
        context,
        service,
        binding,
        briefs,
        provider_parameters=parameters,
    )
    intents = [
        service.paid_create_intent(
            brief,
            binding,
            runtime_plan=runtime_plan,
            provider_parameters=parameters,
        )
        for brief, runtime_plan in zip(briefs, runtime_plans, strict=True)
    ]
    count = len(intents)
    authorization = service.authorize_paid_creates(
        context.project.id,
        context.job.id,
        authorization_fingerprint=fingerprint,
        planned_creates=count,
        authorized_max=count,
        authorized_intents=intents,
    )
    assert authorization.state == "AUTHORIZED"
    assert authorization.execution_id is None
    assert authorization.request_summary == {
        "contract": "SHOT_KEYFRAME_PAID_AUTHORIZATION_V1",
        "production_job_id": context.job.id,
        "planned_creates": count,
        "authorized_max": count,
        "per_item_max": 1,
        "automatic_paid_retry": 0,
        "authorized_intents": intents,
    }
    assert authorization.metadata["authorization_fingerprint"] == fingerprint
    return authorization


def _assert_no_video_budget_rows(repository: ProjectRepository, job_id: str) -> None:
    assert repository.get_paid_budget_ledger(job_id) is None
    assert repository.list_paid_create_reservations(job_id) == []


def _generate(
    service: ShotKeyframeService,
    context,
    brief,
    selection,
    binding,
    fingerprint: str,
    *,
    provider_parameters: Mapping[str, object] | None = None,
):
    parameters = dict(provider_parameters or {"resolution": "1024*1024"})
    plans = [
        plan
        for plan in context.repository.list_runtime_plans(context.project.id)
        if plan.generation_brief_id == brief.generation_brief_id
    ]
    if not plans:
        plans = list(
            _freeze_runtime_plans(
                context,
                service,
                binding,
                (brief,),
                provider_parameters=parameters,
            )
        )
    assert len(plans) == 1
    return service.generate_and_record(
        context.project.id,
        context.job.id,
        brief,
        selection,
        binding,
        runtime_plan=plans[0],
        provider_parameters=parameters,
        create_authorized=True,
        authorization_fingerprint=fingerprint,
    )


def test_planning_snapshot_freezes_exact_approved_chain_with_zero_references(
    tmp_path: Path,
) -> None:
    repository, project, job, shots = _approved_chain_without_references(tmp_path)
    assert ReferenceAssetService(repository).list_assets(project.id) == []

    snapshot = ShotKeyframeService(repository).build_planning_snapshot(
        project.id,
        job.id,
        shot_ids=[shots[1].id, shots[0].id],
    )

    assert snapshot.purpose == "SHOT_KEYFRAME_PLANNING"
    assert snapshot.project_id == project.id
    assert snapshot.production_job_id == job.id
    assert snapshot.story_revision_id == "story_revision_exact"
    assert snapshot.script_revision_id == "script_revision_exact"
    assert snapshot.shot_plan_revision_id == "shot_plan_revision_exact"
    assert dict(snapshot.reference_asset_versions) == {}
    assert tuple(snapshot.shot_parameters) == (shots[1].id, shots[0].id)
    serialized = snapshot.model_dump(mode="json")
    assert serialized["shot_parameters"][shots[0].id] == shots[0].model_dump(
        mode="json"
    )
    round_trip = ShotKeyframePlanningSnapshot.model_validate_json(
        snapshot.model_dump_json()
    )
    assert round_trip == snapshot
    frozen_shot = snapshot.shot_parameters[shots[0].id]
    assert isinstance(frozen_shot, Mapping)
    with pytest.raises(TypeError):
        frozen_shot["action"] = "mutated"  # type: ignore[index]


def test_keyframe_runtime_plan_is_durable_and_identical_at_transport(
    tmp_path: Path,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "9" * 64
    runtime = _RuntimePlanProbeRuntime(
        context.repository, png_bytes(color="navy")
    )
    binding = _image_binding(runtime)
    parameters = {"resolution": "1024*1024"}
    plans = _freeze_runtime_plans(
        context,
        service,
        binding,
        briefs[:1],
        provider_parameters=parameters,
    )
    plan = plans[0]
    intent = service.paid_create_intent(
        briefs[0],
        binding,
        runtime_plan=plan,
        provider_parameters=parameters,
    )
    authorization = service.authorize_paid_creates(
        context.project.id,
        context.job.id,
        authorization_fingerprint=fingerprint,
        planned_creates=1,
        authorized_max=1,
        authorized_intents=[intent],
    )

    frame = service.generate_and_record(
        context.project.id,
        context.job.id,
        briefs[0],
        selections[0],
        binding,
        runtime_plan=plan,
        provider_parameters=parameters,
        create_authorized=True,
        authorization_fingerprint=fingerprint,
    )

    assert runtime.observed is not None
    observed_plan = runtime.observed["plan"]
    observed_task = runtime.observed["task"]
    request = runtime.observed["request"]
    assert observed_plan == plan == context.repository.get_runtime_plan(plan.id)
    assert request.runtime_plan_id == plan.id
    assert request.runtime_plan_hash == plan.plan_hash
    assert request.provider_id == plan.provider_id
    assert request.model_id == plan.model_id
    assert request.manifest_id == plan.provider_parameters["manifest_id"]
    assert request.manifest_hash == plan.provider_parameters["manifest_hash"]
    assert observed_task.request_summary["runtime_plan_id"] == plan.id
    assert observed_task.request_summary["runtime_plan_hash"] == plan.plan_hash
    assert observed_task.request_summary["runtime_plan_evidence"] == intent[
        "runtime_plan_evidence"
    ]
    assert authorization.request_summary["authorized_intents"] == [intent]
    final_task = context.repository.get_provider_task(observed_task.id)
    assert final_task.state == "SUCCEEDED"
    assert final_task.metadata["authorization_intent_id"] == intent[
        "authorization_intent_id"
    ]
    assert final_task.request_summary["runtime_plan_evidence"] == intent[
        "runtime_plan_evidence"
    ]
    assert frame.execution_id == request.execution_id


def test_planless_or_noncanonical_image_runtime_fails_before_transport(
    tmp_path: Path,
) -> None:
    context, service, briefs, _selections = _prepared_paid_case(tmp_path)
    runtime = _FakeImageRuntime((png_bytes(color="navy"),))
    binding = _image_binding(runtime)

    with pytest.raises(ShotKeyframeError, match="exact frozen RuntimePlan"):
        UniversalShotKeyframeImageService.generate(
            briefs[0],
            binding,
            provider_parameters={"resolution": "1024*1024"},
            create_authorized=True,
        )
    with pytest.raises(ShotKeyframeError, match="canonical immutable domain object"):
        service.paid_create_intent(
            briefs[0],
            binding,
            runtime_plan={"id": "not-a-runtime-plan"},  # type: ignore[arg-type]
            provider_parameters={"resolution": "1024*1024"},
        )
    assert runtime.requests == []


def test_forged_plan_hash_reserved_identity_and_unsupported_resolution_fail_closed(
    tmp_path: Path,
) -> None:
    context, service, briefs, _selections = _prepared_paid_case(tmp_path)
    runtime = _FakeImageRuntime((png_bytes(color="navy"),))
    binding = _image_binding(runtime)
    plan = _freeze_runtime_plans(
        context,
        service,
        binding,
        briefs[:1],
        provider_parameters={"resolution": "1024*1024"},
    )[0]
    forged = plan.model_copy(
        update={"id": "forged-runtime-plan", "plan_hash": "f" * 64}
    )
    context.repository.create_runtime_plan(forged)

    with pytest.raises(ShotKeyframeError, match="RuntimePlan hash is invalid"):
        service.paid_create_intent(
            briefs[0],
            binding,
            runtime_plan=forged,
            provider_parameters={"resolution": "1024*1024"},
        )
    with pytest.raises(ShotKeyframeError, match="cannot override frozen identity"):
        service.paid_create_intent(
            briefs[0],
            binding,
            runtime_plan=plan,
            provider_parameters={
                "resolution": "1024*1024",
                "manifest_id": binding.manifest.id,
            },
        )
    with pytest.raises(ShotKeyframeError, match="not supported by the exact manifest"):
        service.paid_create_intent(
            briefs[0],
            binding,
            runtime_plan=plan,
            provider_parameters={"resolution": "2048*2048"},
        )
    assert runtime.requests == []
    assert context.repository.list_production_executions(context.job.id) == []


def test_budget_two_allows_two_distinct_shots_once_and_deduplicates_double_calls(
    tmp_path: Path,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "a" * 64
    runtime = _FakeImageRuntime(
        (png_bytes(color="navy"), png_bytes(color="green"))
    )
    binding = _image_binding(runtime)
    authorization = _authorize(
        service, context, fingerprint, binding, briefs[:2]
    )

    first = _generate(
        service, context, briefs[0], selections[0], binding, fingerprint
    )
    first_again = _generate(
        service, context, briefs[0], selections[0], binding, fingerprint
    )
    second = _generate(
        service, context, briefs[1], selections[1], binding, fingerprint
    )
    second_again = _generate(
        service, context, briefs[1], selections[1], binding, fingerprint
    )

    assert first_again == first
    assert second_again == second
    assert first.shot_id != second.shot_id
    assert len(runtime.requests) == 2
    assert runtime.real_provider_calls == runtime.paid_calls == 0
    executions = context.repository.list_production_executions(context.job.id)
    all_tasks = context.repository.list_provider_tasks(context.project.id)
    tasks = [task for task in all_tasks if task.execution_id is not None]
    assert len(executions) == len(tasks) == 2
    assert [task for task in all_tasks if task.id == authorization.id] == [
        authorization
    ]
    assert {task.request_summary["shot_id"] for task in tasks} == {
        context.shots[0].id,
        context.shots[1].id,
    }
    assert all(task.state == "SUCCEEDED" for task in tasks)
    assert all(
        task.metadata["authorization_task_id"] == authorization.id
        for task in tasks
    )
    _assert_no_video_budget_rows(context.repository, context.job.id)


def test_transport_exception_is_uncertain_and_same_intent_never_resubmits(
    tmp_path: Path,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "b" * 64
    runtime = _UncertainImageRuntime()
    binding = _image_binding(runtime)
    authorization = _authorize(
        service, context, fingerprint, binding, briefs[:2]
    )

    with pytest.raises(ShotKeyframeError, match="UNCERTAIN_CREATE"):
        _generate(
            service, context, briefs[0], selections[0], binding, fingerprint
        )
    with pytest.raises(ShotKeyframeError, match="UNCERTAIN_CREATE"):
        _generate(
            ShotKeyframeService(ProjectRepository(context.repository.paths)),
            context,
            briefs[0],
            selections[0],
            binding,
            fingerprint,
        )
    with pytest.raises(ShotKeyframeError, match="UNCERTAIN_CREATE"):
        _generate(
            ShotKeyframeService(ProjectRepository(context.repository.paths)),
            context,
            briefs[1],
            selections[1],
            binding,
            fingerprint,
        )

    assert runtime.submit_calls == 1
    fresh = ProjectRepository(context.repository.paths)
    tasks = [
        task
        for task in fresh.list_provider_tasks(context.project.id)
        if task.execution_id is not None
    ]
    assert len(tasks) == 2
    by_shot = {task.request_summary["shot_id"]: task for task in tasks}
    uncertain = by_shot[context.shots[0].id]
    blocked = by_shot[context.shots[1].id]
    assert uncertain.state == "UNCERTAIN_CREATE"
    assert blocked.state == "PENDING_SUBMISSION"
    assert uncertain.metadata["authorization_task_id"] == authorization.id
    assert blocked.metadata["authorization_task_id"] == authorization.id
    assert fresh.list_production_artifacts(uncertain.execution_id) == []
    assert fresh.list_production_artifacts(blocked.execution_id) == []
    _assert_no_video_budget_rows(fresh, context.job.id)


def test_durable_submitting_intent_blocks_second_shot_before_transport(
    tmp_path: Path,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "e" * 64
    runtime = _InterruptedThenForbiddenRuntime()
    binding = _image_binding(runtime)
    authorization = _authorize(
        service, context, fingerprint, binding, briefs[:2]
    )

    with pytest.raises(_SimulatedProcessLoss):
        _generate(
            service, context, briefs[0], selections[0], binding, fingerprint
        )
    first_tasks = [
        task
        for task in context.repository.list_provider_tasks(context.project.id)
        if task.execution_id is not None
    ]
    assert len(first_tasks) == 1
    assert first_tasks[0].state == "SUBMITTING"
    assert first_tasks[0].metadata["authorization_task_id"] == authorization.id

    with pytest.raises(ShotKeyframeError, match="UNCERTAIN_CREATE"):
        _generate(
            ShotKeyframeService(ProjectRepository(context.repository.paths)),
            context,
            briefs[1],
            selections[1],
            binding,
            fingerprint,
        )

    assert runtime.submit_calls == 1
    fresh = ProjectRepository(context.repository.paths)
    by_shot = {
        task.request_summary["shot_id"]: task
        for task in fresh.list_provider_tasks(context.project.id)
        if task.execution_id is not None
    }
    assert by_shot[context.shots[0].id].state == "SUBMITTING"
    assert by_shot[context.shots[1].id].state == "PENDING_SUBMISSION"
    _assert_no_video_budget_rows(fresh, context.job.id)


@pytest.mark.parametrize(
    "mutation",
    ("provider_parameters", "model_id", "manifest"),
)
def test_authorized_intent_change_fails_closed_before_transport(
    tmp_path: Path,
    mutation: str,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "f" * 64
    runtime = _FakeImageRuntime((png_bytes(color="navy"),))
    authorized_binding = _image_binding(runtime)
    _authorize(
        service, context, fingerprint, authorized_binding, briefs[:1]
    )
    actual_binding = authorized_binding
    provider_parameters = None
    if mutation == "provider_parameters":
        provider_parameters = {"seed": 71}
    else:
        changed_field = "model_id" if mutation == "model_id" else "display_name"
        changed_value = (
            "changed-model-after-authorization"
            if mutation == "model_id"
            else f"{authorized_binding.manifest.display_name} changed"
        )
        changed_manifest = replace(
            authorized_binding.manifest, **{changed_field: changed_value}
        )
        actual_binding = UniversalImageBinding(
            runtime=runtime,
            manifest=changed_manifest,
            read_output=runtime.read_output,
        )

    with pytest.raises(ShotKeyframeError, match="PAID_AUTHORIZATION_MISMATCH"):
        _generate(
            service,
            context,
            briefs[0],
            selections[0],
            actual_binding,
            fingerprint,
            provider_parameters=provider_parameters,
        )

    assert runtime.requests == []
    assert context.repository.list_production_executions(context.job.id) == []
    assert [
        task
        for task in context.repository.list_provider_tasks(context.project.id)
        if task.execution_id is not None
    ] == []
    _assert_no_video_budget_rows(context.repository, context.job.id)


def test_missing_budget_blocks_before_intent_or_transport(tmp_path: Path) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    runtime = _FakeImageRuntime((png_bytes(color="navy"),))
    binding = _image_binding(runtime)

    with pytest.raises(ShotKeyframeError, match="PAID_BUDGET_MISSING"):
        _generate(
            service,
            context,
            briefs[0],
            selections[0],
            binding,
            "c" * 64,
        )

    assert runtime.requests == []
    assert context.repository.list_provider_tasks(context.project.id) == []
    assert context.repository.list_production_executions(context.job.id) == []
    _assert_no_video_budget_rows(context.repository, context.job.id)


def test_planning_snapshot_job_a_rejects_generation_brief_from_job_b(
    tmp_path: Path,
) -> None:
    context, service, _briefs, _selections = _prepared_paid_case(tmp_path)
    references = ReferenceAssetService(context.repository)
    production = ProductionService(
        context.repository, reference_service=references
    )
    job_b = production.create_production_job(
        context.project.id, context.job.shot_plan_revision_id
    )
    production.create_production_shots(context.project.id, job_b.id)
    briefs_b = GenerationBriefService(context.repository).prepare_for_job(
        context.project.id, job_b.id
    )
    brief_b = next(
        item for item in briefs_b if item.shot_id == context.shots[0].id
    )
    snapshot_a = service.build_planning_snapshot(
        context.project.id,
        context.job.id,
        shot_ids=[context.shots[0].id],
    )

    assert snapshot_a.production_job_id == context.job.id
    assert brief_b.production_job_id == job_b.id
    with pytest.raises(
        ShotKeyframeError,
        match="GenerationBrief does not belong to the keyframe planning job",
    ):
        service.briefs.compile(snapshot_a, context.shots[0].id, brief_b)


def test_zero_reference_frame_is_blocked_after_formal_snapshot_locks_references(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    brief = next(
        item for item in context.briefs if item.shot_id == context.shots[0].id
    )
    expected_references = ProductionQueueService._shot_references(
        context.snapshot.reference_asset_versions,
        brief,
    )
    assert expected_references
    zero_reference_frame = SimpleNamespace(
        id="zero-reference-frame",
        artifact_id="zero-reference-artifact",
        sha256="0" * 64,
        source_type=ShotFirstFrameSourceType.GENERATED_KEYFRAME,
        shot_id=brief.shot_id,
        generation_brief_id=brief.id,
        identity_reference_provenance=(),
        location_reference_provenance=(),
        prop_reference_provenance=(),
        style_reference_provenance=(),
    )
    report = SimpleNamespace(planned_shot_ids=(brief.shot_id,))
    seam = _PreLiveKeyframeSeam(report, zero_reference_frame)
    queue = object.__new__(ProductionQueueService)
    queue.shot_keyframes = seam

    with pytest.raises(
        ProductionQueueError,
        match="Shot First Frame Reference provenance",
    ):
        queue._freeze_pre_live_snapshot(
            context.project.id,
            context.job.id,
            context.snapshot,
            generation_briefs=(brief,),
        )

    assert seam.freeze_calls == 0


def test_cold_success_reuses_precreated_execution_task_and_consumed_window_slot(
    tmp_path: Path,
) -> None:
    context, service, briefs, selections = _prepared_paid_case(tmp_path)
    fingerprint = "d" * 64
    first_runtime = _FakeImageRuntime((png_bytes(color="navy"),))
    first_binding = _image_binding(first_runtime)
    authorization = _authorize(
        service, context, fingerprint, first_binding, briefs[:1]
    )
    first = _generate(
        service,
        context,
        briefs[0],
        selections[0],
        first_binding,
        fingerprint,
    )

    cold = ProjectRepository(context.repository.paths)
    executions_before = tuple(cold.list_production_executions(context.job.id))
    all_tasks_before = tuple(cold.list_provider_tasks(context.project.id))
    tasks_before = tuple(
        task for task in all_tasks_before if task.execution_id is not None
    )
    artifacts_before = tuple(
        cold.list_production_artifacts(executions_before[0].id)
    )
    assert len(executions_before) == len(tasks_before) == len(artifacts_before) == 1
    assert [task for task in all_tasks_before if task.id == authorization.id] == [
        authorization
    ]
    assert executions_before[0].status is ProductionExecutionStatus.SUCCEEDED
    assert tasks_before[0].state == "SUCCEEDED"
    assert tasks_before[0].metadata["authorization_task_id"] == authorization.id
    _assert_no_video_budget_rows(cold, context.job.id)

    blocked_runtime = _SubmitMustNotRun()
    reused = _generate(
        ShotKeyframeService(cold),
        context,
        briefs[0],
        selections[0],
        _image_binding(blocked_runtime),
        fingerprint,
    )

    assert reused == first
    assert blocked_runtime.submit_calls == 0
    assert tuple(cold.list_production_executions(context.job.id)) == executions_before
    assert tuple(cold.list_provider_tasks(context.project.id)) == all_tasks_before
    _assert_no_video_budget_rows(cold, context.job.id)
    assert tuple(cold.list_production_artifacts(executions_before[0].id)) == artifacts_before
