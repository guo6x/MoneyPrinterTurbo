from __future__ import annotations

import socket
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AutoAction,
    AutoRunStatus,
    AutoStage,
    Character,
    Location,
    ProductionReviewDecision,
    ReferenceBindingType,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    Shot,
    ShotPlan,
    StoryBeat,
    StoryBible,
    StructuredScript,
    World,
)
from aidrama_studio.services import (
    AutoOrchestratorError,
    AutoOrchestratorService,
    BackgroundProductionRunner,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    DeterministicMockVisionProvider,
    FinalAssemblyService,
    ImageCandidate,
    ImageGenerationProvider,
    ImageRuntimeService,
    ProductionExecutionService,
    ProductionQCService,
    ProductionQueueService,
    ProductionService,
    ProductionWorker,
    ProjectService,
    ReferenceAssetService,
    RuntimeVideoProvider,
    ScriptService,
    ShotService,
    StoryService,
    VisionQCService,
)
from aidrama_studio.services.adapters import MockProductionAdapter
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.video_fixtures import mp4_bytes


def _paths(root: Path) -> DatabasePaths:
    return DatabasePaths(
        root / "aidrama.db",
        root / "projects",
        root / "archived-projects",
    )


def _story() -> StoryBible:
    return StoryBible(
        title="AUTO Test",
        logline="A courier races through a rain-soaked city.",
        premise="One delivery can prevent tomorrow's blackout.",
        genre="Thriller",
        tone="Restrained",
        world=World(era="Present day", setting="A rain-soaked city"),
        characters=[Character(id="char_001", name="Lin")],
        locations=[Location(id="loc_001", name="Control room")],
        story_beats=[
            StoryBeat(
                id="beat_001",
                order=1,
                type="OPENING",
                summary="Lin receives the final delivery.",
                characters=["char_001"],
                location_id="loc_001",
            ),
            StoryBeat(
                id="beat_002",
                order=2,
                type="TURNING_POINT",
                summary="The route is blocked.",
                characters=["char_001"],
                location_id="loc_001",
            ),
            StoryBeat(
                id="beat_003",
                order=3,
                type="ENDING",
                summary="The control room lights return.",
                characters=["char_001"],
                location_id="loc_001",
            ),
        ],
    )


def _script() -> StructuredScript:
    return StructuredScript(
        title="AUTO Test",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Control room",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=1,
                beats=[
                    ScriptBeat(
                        id="script_beat_001",
                        order=1,
                        type=ScriptBeatType.DIALOGUE,
                        character_id="char_001",
                        text="Power is back.",
                        estimated_duration_seconds=1,
                    )
                ],
            )
        ],
    )


def _shot_plan(script_revision_id: str) -> ShotPlan:
    return ShotPlan(
        title="AUTO Test shot plan",
        source_script_revision_id=script_revision_id,
        shots=[
            Shot(
                id="shot_001",
                order=1,
                scene_id="scene_001",
                source_script_beat_ids=["script_beat_001"],
                duration_seconds=1,
                subject=["char_001"],
                action="Lin watches the control panels relight.",
                dialogue_or_narration="Power is back.",
                visual_intent="A slow push toward the restored display.",
            )
        ],
    )


class _FakeLLMGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def readiness(self, _project_id: str):
        return True, "offline fake"

    def generate_validated_json(
        self,
        _project_id,
        _prompt,
        *,
        operation,
        input_source_ids=(),
        **_kwargs,
    ):
        self.calls.append(operation)
        if operation == "STORY_BIBLE_GENERATION":
            return _story()
        if operation == "STRUCTURED_SCRIPT_GENERATION":
            return _script()
        if operation == "SHOT_PLAN_GENERATION":
            return _shot_plan(str(tuple(input_source_ids)[0]))
        raise AssertionError(f"unexpected fake LLM operation: {operation}")


class _FakeImageProvider(ImageGenerationProvider):
    provider_name = "AUTO_FAKE_IMAGE"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            CapabilityKind.IMAGE,
            self.provider_name,
            True,
            "offline fake",
            {
                "model": "fake-image-v1",
                "configured": True,
                "deployment_region": "LOCAL",
                "endpoint_class": "OFFLINE_TEST",
                "endpoint_profile_id": "runtime:IMAGE:AUTO_FAKE_IMAGE:LOCAL",
                "verification_state": "VERIFIED",
            },
            configured=True,
            verified=True,
        )

    def generate_candidate(self, prompt, *, project_id, metadata=None):
        self.calls += 1
        return ImageCandidate(
            project_id=project_id,
            provider=self.provider_name,
            prompt=prompt,
            content=png_bytes(color="red" if self.calls == 1 else "blue"),
            mime_type="image/png",
            metadata={**dict(metadata or {}), "model": "fake-image-v1"},
        )


class _FakeVideoAdapter(MockProductionAdapter):
    name = "auto-fake-video"

    def __init__(self) -> None:
        super().__init__()
        self.payload = mp4_bytes(
            source="testsrc2=size=320x180:rate=24:duration=1",
            audio=False,
        )
        self.submit_count = 0

    def submit(self, snapshot):
        self.submit_count += 1
        submission = super().submit(snapshot)
        shot_id = next(iter(snapshot.shot_parameters))
        self.succeed(
            submission.runtime_reference,
            artifacts=[
                {
                    "artifact_type": "video",
                    "filename": f"{shot_id}.mp4",
                    "content": self.payload,
                    "metadata": {
                        "mime_type": "video/mp4",
                        "duration_seconds": 1,
                        "resolution": {"width": 320, "height": 180},
                        "codec": "h264",
                        "audio_stream": False,
                        "audio_required": False,
                        "black_frame_detected": False,
                        "static_frame_detected": False,
                    },
                }
            ],
        )
        return submission


class _AutoServices:
    def __init__(
        self,
        repository: ProjectRepository,
        gateway: _FakeLLMGateway,
        image_provider: _FakeImageProvider,
        video_adapter: _FakeVideoAdapter,
    ) -> None:
        self.repository = repository
        self.story = StoryService(repository, llm_gateway=gateway)
        self.script = ScriptService(repository, llm_gateway=gateway)
        self.shot = ShotService(repository, llm_gateway=gateway)
        self.references = ReferenceAssetService(repository)
        self.image = ImageRuntimeService(repository, provider=image_provider)
        self.production = ProductionService(
            repository, reference_service=self.references
        )
        registry = CapabilityRegistry(
            [RuntimeVideoProvider(video_adapter, provider_name="AUTO_FAKE_VIDEO")]
        )
        self.queue = ProductionQueueService(
            repository,
            production_service=self.production,
            registry=registry,
        )
        self.execution = ProductionExecutionService(
            repository, production_service=self.production
        )
        self.qc = ProductionQCService(repository)
        self.vision = VisionQCService(
            repository, provider=DeterministicMockVisionProvider()
        )
        self.background = BackgroundProductionRunner(
            repository,
            worker_factory=lambda: ProductionWorker(
                self.execution,
                video_adapter,
                poll_interval=0,
                max_polls=3,
            ),
            adapter_factory=lambda _task, _plan=None: video_adapter,
        )

    def orchestrator(self) -> AutoOrchestratorService:
        return AutoOrchestratorService(
            self.repository,
            story_service=self.story,
            script_service=self.script,
            shot_service=self.shot,
            reference_service=self.references,
            image_runtime=self.image,
            production_service=self.production,
            production_queue=self.queue,
            execution_service=self.execution,
            qc_service=self.qc,
            vision_service=self.vision,
            background_runner=self.background,
            actor="auto-fake-e2e",
            drive_background=True,
        )


def _approve_reference(service: _AutoServices, project_id: str, decision) -> None:
    binding_type = ReferenceBindingType(str(decision.metadata["binding_type"]))
    binding_id = str(decision.metadata["binding_id"])
    asset = service.references.find_workspace_asset(
        project_id, binding_type, binding_id
    )
    assert asset is not None
    candidate = service.references.list_image_candidates(project_id, asset.id)[-1]
    version = service.references.promote_image_candidate(project_id, candidate.id)
    service.references.bind_version(
        project_id, version.id, binding_type, binding_id
    )
    service.references.activate_version(project_id, asset.id, version.id)


def test_empty_project_and_human_gate_step_are_idempotent(tmp_path: Path) -> None:
    repository = ProjectRepository(_paths(tmp_path / "data"))
    project = ProjectService(repository).create("AUTO empty")
    gateway = _FakeLLMGateway()
    story = StoryService(repository, llm_gateway=gateway)
    service = AutoOrchestratorService(
        repository,
        story_service=story,
        actor="auto-test",
    )

    decision = service.next_action(project.id)
    assert decision.status is AutoRunStatus.IDLE
    assert decision.current_stage is AutoStage.STORY
    assert decision.next_action is AutoAction.GENERATE_OR_CREATE_STORY

    first = service.step(project.id)
    assert first.status is AutoRunStatus.WAITING_HUMAN
    assert first.requested_action == "APPROVE_STORY"
    assert first.completed_stages == ()
    assert first.resume_token
    assert len(repository.list_story_revisions(project.id)) == 1

    second = service.step(project.id)
    assert second.status is AutoRunStatus.WAITING_HUMAN
    assert second.resume_token == first.resume_token
    assert len(repository.list_story_revisions(project.id)) == 1
    assert [event.result for event in service.list_events(project.id)][:2] == [
        "ACTION_STARTED",
        "STORY_REVISION_CREATED",
    ]
    with pytest.raises(AutoOrchestratorError, match="resume token"):
        service.resume(project.id, resume_token="stale-token")
    cancelled = service.cancel(project.id, reason="test_cancel")
    assert cancelled.status is AutoRunStatus.CANCELLED
    assert service.next_action(project.id).status is AutoRunStatus.CANCELLED


def test_qc_pending_and_optional_vision_use_formal_services(tmp_path: Path) -> None:
    repository = ProjectRepository(_paths(tmp_path / "data"))
    project = ProjectService(repository).create(
        "AUTO QC planner",
        description="A courier restores a city's power.",
        target_duration_seconds=1,
    )
    gateway = _FakeLLMGateway()
    image_provider = _FakeImageProvider()
    video_adapter = _FakeVideoAdapter()
    services = _AutoServices(repository, gateway, image_provider, video_adapter)

    story = services.story.generate_story_bible(
        project,
        brief=project.description,
        genre="Thriller",
        tone="Restrained",
    )
    services.story.approve_revision(story["id"])
    script = services.script.generate_script(project)
    services.script.approve_revision(script["id"])
    plan = services.shot.generate_shot_plan(project)
    services.shot.approve_revision(plan["id"])

    for binding_type, binding_id in (
        (ReferenceBindingType.CHARACTER, "char_001"),
        (ReferenceBindingType.LOCATION, "loc_001"),
    ):
        asset = services.references.ensure_workspace_asset(
            project.id, binding_type, binding_id
        )
        candidate = services.image.generate_and_record_candidate(
            project.id,
            asset.id,
            f"Reference for {binding_id}",
            source_story_revision_id=story["id"],
            filename=f"{binding_id}.png",
            reference_assets=services.references,
        )
        version = services.references.promote_image_candidate(
            project.id, candidate.id
        )
        services.references.bind_version(
            project.id, version.id, binding_type, binding_id
        )
        services.references.activate_version(project.id, asset.id, version.id)

    job = services.production.create_production_job(project.id, plan["id"])
    services.production.create_production_shots(project.id, job.id)
    execution = services.execution.enqueue_job(
        project.id, job.id, worker_type="auto-fake-video"
    )
    completed = ProductionWorker(
        services.execution, video_adapter, max_polls=3
    ).run(project.id, execution.id)
    assert completed.status.value == "SUCCEEDED"
    assert services.qc.list_results(project.id, execution.id) == []

    auto = services.orchestrator()
    decision = auto.next_action(project.id)
    assert decision.current_stage is AutoStage.QC
    assert decision.next_action is AutoAction.RUN_TECHNICAL_QC

    state = auto.step(project.id)
    assert state.status is AutoRunStatus.IDLE
    assert state.next_action is AutoAction.RUN_OPTIONAL_VISION_QC
    assert len(services.qc.list_results(project.id, execution.id)) == 1

    state = auto.step(project.id)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.current_stage is AutoStage.REVIEW
    assert state.requested_action == "APPROVE_OR_REJECT_PRODUCTION_REVIEW"
    assert len(repository.list_vision_analyses(project.id, execution.id)) == 1


def test_auto_fake_full_pipeline_resume_paid_gate_and_event_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_attempts: list[tuple[object, ...]] = []

    def deny_network(*args: object, **_kwargs: object) -> None:
        network_attempts.append(args)
        raise AssertionError("AUTO fake pipeline attempted network I/O")

    monkeypatch.delenv("AIDRAMA_ALLOW_PAID", raising=False)
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)

    paths = _paths(tmp_path / "data")
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(
        "AUTO fake full pipeline",
        description="A courier restores a city's power.",
        target_duration_seconds=1,
    )
    gateway = _FakeLLMGateway()
    image_provider = _FakeImageProvider()
    video_adapter = _FakeVideoAdapter()
    services = _AutoServices(repository, gateway, image_provider, video_adapter)
    auto = services.orchestrator()

    state = auto.run_until_boundary(project.id)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.current_stage is AutoStage.STORY
    story = services.story.get_latest_revision(project.id)
    services.story.approve_revision(story["id"])

    # Recreate both repository and orchestrator to prove cold SQLite resume.
    cold_repository = ProjectRepository(paths)
    services = _AutoServices(
        cold_repository, gateway, image_provider, video_adapter
    )
    auto = services.orchestrator()
    state = auto.resume(project.id, resume_token=state.resume_token)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.current_stage is AutoStage.SCRIPT
    assert state.completed_stages == (AutoStage.STORY,)
    script = services.script.get_latest_revision(project.id)
    services.script.approve_revision(script["id"])

    state = auto.resume(project.id)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.current_stage is AutoStage.SHOT_PLAN
    assert state.completed_stages == (AutoStage.STORY, AutoStage.SCRIPT)
    shot_plan = services.shot.get_latest_revision(project.id)
    services.shot.approve_revision(shot_plan["id"])

    for expected_type in (
        ReferenceBindingType.CHARACTER,
        ReferenceBindingType.LOCATION,
    ):
        state = auto.resume(project.id)
        assert state.status is AutoRunStatus.WAITING_HUMAN, state.blocking_reason
        assert state.current_stage is AutoStage.REFERENCES
        assert state.requested_action == "PROMOTE_BIND_AND_LOCK_REFERENCE"
        assert state.metadata["binding_type"] == expected_type.value
        if expected_type is ReferenceBindingType.CHARACTER:
            asset = services.references.find_workspace_asset(
                project.id, expected_type, str(state.metadata["binding_id"])
            )
            candidate = services.references.list_image_candidates(
                project.id, asset.id
            )[-1]
            version = services.references.promote_image_candidate(
                project.id, candidate.id
            )
            partial = auto.next_action(project.id)
            assert partial.status is AutoRunStatus.WAITING_HUMAN
            assert partial.requested_action == "BIND_AND_LOCK_REFERENCE"
            assert partial.metadata["version_id"] == version.id
            assert len(
                services.references.list_image_candidates(project.id, asset.id)
            ) == 1
            services.references.bind_version(
                project.id,
                version.id,
                expected_type,
                str(state.metadata["binding_id"]),
            )
            services.references.activate_version(project.id, asset.id, version.id)
        else:
            _approve_reference(services, project.id, state)

    state = auto.resume(project.id)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.next_action is AutoAction.PAID_AUTHORIZATION_REQUIRED
    assert state.requires_paid_authorization is True
    assert cold_repository.list_provider_tasks(project.id) == []

    preview = auto.preview_paid_authorization(project.id)
    assert preview.required_create_count == 1
    assert preview.per_item_max == 1
    assert preview.retry_limit == 0
    with pytest.raises(AutoOrchestratorError, match="global max"):
        auto.grant_paid_authorization(
            project.id,
            authorization_fingerprint=preview.authorization_fingerprint,
            global_max=preview.required_create_count + 1,
        )
    with pytest.raises(AutoOrchestratorError, match="per-item max=1"):
        auto.grant_paid_authorization(
            project.id,
            authorization_fingerprint=preview.authorization_fingerprint,
            global_max=preview.required_create_count,
            per_item_max=2,
        )
    authorization = auto.grant_paid_authorization(
        project.id,
        authorization_fingerprint=preview.authorization_fingerprint,
        global_max=preview.required_create_count,
        per_item_max=1,
        retry_limit=0,
    )
    assert cold_repository.list_provider_tasks(project.id) == []

    # An unused authorization whose exact provider/input fingerprint drifts
    # must fail closed and require a fresh explicit confirmation.
    with cold_repository.transaction() as connection:
        connection.execute(
            "UPDATE auto_paid_authorizations SET authorization_fingerprint=? "
            "WHERE id=?",
            ("0" * 64, authorization.id),
        )
    stale = auto.next_action(project.id)
    assert stale.next_action is AutoAction.PAID_AUTHORIZATION_REQUIRED
    refreshed_preview = auto.preview_paid_authorization(project.id)
    authorization = auto.grant_paid_authorization(
        project.id,
        authorization_fingerprint=refreshed_preview.authorization_fingerprint,
        global_max=refreshed_preview.required_create_count,
        per_item_max=1,
        retry_limit=0,
    )
    assert authorization.authorization_fingerprint == refreshed_preview.authorization_fingerprint

    state = auto.step(project.id)
    assert state.status is AutoRunStatus.WAITING_PROVIDER
    parent_tasks = [
        item
        for item in cold_repository.list_provider_tasks(project.id)
        if item.execution_id is None
    ]
    assert len(parent_tasks) == 1

    # Another cold service polls the existing task; it cannot create a second one.
    cold_repository = ProjectRepository(paths)
    services = _AutoServices(
        cold_repository, gateway, image_provider, video_adapter
    )
    auto = services.orchestrator()
    state = auto.resume(project.id)
    assert state.status is AutoRunStatus.WAITING_HUMAN
    assert state.current_stage is AutoStage.REVIEW
    assert state.requested_action == "APPROVE_OR_REJECT_PRODUCTION_REVIEW"
    assert len(cold_repository.list_production_executions(parent_tasks[0].request_summary["production_job_id"])) == 1
    assert len(
        [
            item
            for item in cold_repository.list_provider_tasks(project.id)
            if item.execution_id is None
        ]
    ) == 1
    assert video_adapter.submit_count == 1
    assert len(cold_repository.list_vision_analyses(project.id)) == 1

    # Existing Final compatibility can build from QC_PASS. AUTO must still
    # enforce its product-level human Review gate before honoring that record.
    job_id = str(parent_tasks[0].request_summary["production_job_id"])
    premature = FinalAssemblyService(cold_repository).create_assembly(
        project.id, job_id, freeze=True
    )
    before_review = auto.next_action(project.id)
    assert before_review.current_stage is AutoStage.REVIEW
    assert before_review.requested_action == "APPROVE_OR_REJECT_PRODUCTION_REVIEW"

    qc_result = cold_repository.get_production_qc_result(
        str(state.metadata["qc_result_id"])
    )
    services.qc.create_review(
        project.id,
        qc_result.id,
        ProductionReviewDecision.APPROVED,
        reviewer="auto-fake-human",
    )
    decision = auto.next_action(project.id)
    assert decision.current_stage is AutoStage.FINAL
    assert decision.next_action is AutoAction.FINAL_ASSEMBLY

    state = auto.resume(project.id)
    assert state.status is AutoRunStatus.WAITING_PROVIDER
    assert state.current_stage is AutoStage.FINAL
    assert len(FinalAssemblyService(cold_repository).list_assemblies(project.id)) == 1
    assert FinalAssemblyService(cold_repository).list_assemblies(project.id)[0].id == premature.id

    # Simulate process death after the durable local HeavyJob was claimed.
    claimed = cold_repository.claim_next_heavy_job(
        project_id=project.id,
        started_at="2026-08-28T00:00:00+00:00",
        event_id="auto-fake-heavy-claimed",
    )
    assert claimed is not None and claimed.status.value == "RUNNING"

    # Final heavy work also resumes from persisted state and synthetic media.
    final_repository = ProjectRepository(paths)
    final_services = _AutoServices(
        final_repository, gateway, image_provider, video_adapter
    )
    final_auto = final_services.orchestrator()
    state = final_auto.resume(project.id)
    assert state.status is AutoRunStatus.SUCCEEDED
    assert state.current_stage is AutoStage.COMPLETED
    assemblies = FinalAssemblyService(final_repository).list_assemblies(project.id)
    assert len(assemblies) == 1
    heavy_jobs = final_repository.list_heavy_jobs(project.id)
    assert [item.status.value for item in heavy_jobs] == [
        "INTERRUPTED",
        "SUCCEEDED",
    ]
    assert heavy_jobs[-1].retry_of_job_id == heavy_jobs[0].id

    # Terminal and human-boundary step calls are idempotent for business records.
    again = final_auto.step(project.id)
    assert again.status is AutoRunStatus.SUCCEEDED
    assert len(FinalAssemblyService(final_repository).list_assemblies(project.id)) == 1
    assert len(final_repository.list_story_revisions(project.id)) == 1
    assert len(final_repository.list_script_revisions(project.id)) == 1
    assert len(final_repository.list_shot_revisions(project.id)) == 1

    events = final_auto.list_events(project.id)
    assert events
    assert [item.sequence_number for item in events] == list(
        range(1, len(events) + 1)
    )
    assert all(len(item.input_state_hash) == 64 for item in events)
    assert all(item.actor == "auto-fake-e2e" for item in events)
    serialized_events = " ".join(item.model_dump_json() for item in events).lower()
    assert "api_key" not in serialized_events
    assert "raw provider" not in serialized_events

    with final_repository.transaction() as connection:
        paid = connection.execute(
            "SELECT consumed_count,status FROM auto_paid_authorizations WHERE id=?",
            (authorization.id,),
        ).fetchone()
    assert tuple(paid) == (1, "CONSUMED")
    assert network_attempts == []


def test_auto_page_renders_minimal_durable_state_surface() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.domain import AutoAction, AutoDecision, AutoRunStatus, AutoStage
from aidrama_studio.pages import auto

decision = AutoDecision(
    project_id="project-auto-ui",
    status=AutoRunStatus.IDLE,
    current_stage=AutoStage.STORY,
    next_action=AutoAction.GENERATE_OR_CREATE_STORY,
    why="项目还没有 Story revision。",
    input_state_hash="a" * 64,
)

class Service:
    def get_state(self, _project_id):
        return None
    def next_action(self, _project_id):
        return decision
    def list_events(self, _project_id):
        return []

auto.current_project_or_stop = lambda: SimpleNamespace(id="project-auto-ui", title="AUTO UI")
auto.render_project_context = lambda *_args, **_kwargs: None
auto.AutoOrchestratorService = lambda **_kwargs: Service()
auto.render()
"""
    ).run()

    assert not app.exception
    assert any("自动制作" in item.value for item in app.markdown)
    assert any(item.label == "当前阶段" for item in app.metric)
    assert any(item.label == "下一步" for item in app.metric)
    assert any(item.label == "开始 / 继续自动制作" for item in app.button)
