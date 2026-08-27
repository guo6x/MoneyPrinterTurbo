from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character,
    Location,
    ReferenceAssetType,
    ReferenceBindingType,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Scene,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionJobStatus,
    ProductionShotStatus,
    ProductionReviewDecision,
)
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionOrchestrator,
    ProductionQCService,
    ProductionService,
    ProductionWorker,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
)
from aidrama_studio.services.adapters import MockProductionAdapter
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.video_fixtures import mp4_bytes


class MultiShotAdapter(MockProductionAdapter):
    def __init__(self, *, fail_shots: set[str] | None = None, bad_qc_shots: set[str] | None = None):
        super().__init__()
        self.fail_shots = fail_shots or set()
        self.bad_qc_shots = bad_qc_shots or set()
        self.submitted_shots: list[str] = []

    def submit(self, snapshot):
        submission = super().submit(snapshot)
        shot_id = next(iter(snapshot.shot_parameters))
        self.submitted_shots.append(shot_id)
        if shot_id in self.fail_shots:
            self.fail(submission.runtime_reference, "runtime failure")
        else:
            self.succeed(
                submission.runtime_reference,
                artifacts=[
                    {
                        "artifact_type": "video",
                        "content": mp4_bytes(),
                        "filename": f"{shot_id}.mp4",
                        "metadata": {
                            "mime_type": "video/mp4",
                            "duration_seconds": 2,
                            "resolution": {"width": 1280, "height": 720},
                            "codec": "h264",
                            "audio_stream": shot_id not in self.bad_qc_shots,
                            "audio_required": shot_id in self.bad_qc_shots,
                            "black_frame_detected": False,
                            "static_frame_detected": False,
                        },
                    }
                ],
            )
        return submission


@pytest.fixture
def context(tmp_path: Path):
    paths = DatabasePaths(tmp_path / "db" / "aidrama.db", tmp_path / "db" / "projects", tmp_path / "db" / "archived")
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Orchestrator project")
    story = StoryBible(
        title="Story", logline="Logline", premise="Premise", genre="Drama", tone="Calm",
        world=World(era="Now"),
        characters=[Character(id="char_001", name="Hero")],
        locations=[Location(id="loc_001", name="Room")],
        story_beats=[
            StoryBeat(id=f"beat_{i}", order=i, type="OPENING" if i == 1 else "ENDING" if i == 3 else "DEVELOPMENT", summary=f"Beat {i}", characters=["char_001"], location_id="loc_001")
            for i in range(1, 4)
        ],
    )
    repository.create_story_revision(
        revision_id="story_001", project_id=project.id, version=1, status=StoryRevisionStatus.APPROVED,
        content=story, generation_input=None, created_at="now", updated_at="now",
    )
    script = StructuredScript(
        title="Script",
        scenes=[
            Scene(
                id=f"scene_{i}", order=i, title=f"Room {i}", location_id="loc_001", character_ids=["char_001"],
                estimated_duration_seconds=2,
                beats=[ScriptBeat(id=f"script_beat_{i}", order=1, type=ScriptBeatType.ACTION, text=f"Action {i}")],
            )
            for i in range(1, 4)
        ],
    )
    repository.create_script_revision(
        revision_id="script_001", project_id=project.id, version=1, status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_001", content=script, generation_input=None,
        created_at="now", updated_at="now",
    )
    plan = ShotPlan(
        title="Shot Plan", source_script_revision_id="script_001",
        shots=[
            Shot(id=f"shot_{i}", order=i, scene_id=f"scene_{i}", duration_seconds=2, subject=["char_001"], visual_intent=f"Shot {i}")
            for i in range(1, 4)
        ],
    )
    repository.create_shot_revision(
        revision_id="shot_plan_001", project_id=project.id, version=1, status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_001", content=plan, generation_input=None,
        created_at="now", updated_at="now",
    )
    reference_service = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(reference_service)
    for asset_type, binding_type, binding_id, name in (
        (ReferenceAssetType.CHARACTER_REFERENCE, ReferenceBindingType.CHARACTER, "char_001", "hero"),
        (ReferenceAssetType.LOCATION_REFERENCE, ReferenceBindingType.LOCATION, "loc_001", "room"),
    ):
        asset = reference_service.create_asset(project.id, asset_type)
        version = storage.import_image(
            project.id, asset.id, png_bytes(), filename=f"{name}.png", mime_type="image/png",
            metadata={"source_story_revision_id": "story_001"},
        )
        reference_service.activate_version(project.id, asset.id, version.id)
        reference_service.bind_version(project.id, version.id, binding_type, binding_id)
    job = ProductionService(repository).create_production_job(project.id, "shot_plan_001")
    return repository, project, job


def make_orchestrator(repository, adapter):
    execution_service = ProductionExecutionService(repository)
    worker = ProductionWorker(execution_service, adapter, max_polls=3)
    return ProductionOrchestrator(repository, execution_service=execution_service, worker=worker, adapter=adapter)


def test_three_shots_run_in_canonical_order_with_separate_execution_and_snapshot(context):
    repository, project, job = context
    adapter = MultiShotAdapter()
    result = make_orchestrator(repository, adapter).run_job(project.id, job.id)
    assert result.status is ProductionJobStatus.SUCCEEDED
    shots = repository.list_production_shots(job.id)
    assert [shot.shot_id for shot in shots] == ["shot_1", "shot_2", "shot_3"]
    assert all(shot.status is ProductionShotStatus.SUCCEEDED for shot in shots)
    executions = repository.list_production_executions(job.id)
    assert len(executions) == 3
    assert [next(iter(item.input_snapshot.shot_parameters)) for item in executions] == ["shot_1", "shot_2", "shot_3"]
    assert len({item.id for item in executions}) == 3
    assert [next(iter(item.input_snapshot.reference_asset_versions.values())) for item in executions]


def test_per_shot_execution_completion_waits_for_aggregate_shot_completion(context):
    repository, project, job = context
    production = ProductionService(repository)
    shots = production.create_production_shots(project.id, job.id)
    execution_service = ProductionExecutionService(
        repository,
        production_service=production,
    )
    full_snapshot = execution_service.create_input_snapshot(project.id, job.id)

    for index, shot in enumerate(shots):
        snapshot_json = full_snapshot.to_json_dict()
        snapshot_json["shot_parameters"] = {
            shot.shot_id: snapshot_json["shot_parameters"][shot.shot_id]
        }
        shot_snapshot = ProductionInputSnapshot.model_validate(
            snapshot_json
        )
        execution, attempt = execution_service.enqueue_shot_execution_with_attempt(
            project.id,
            job.id,
            shot_snapshot,
            worker_type="offline-aggregate-regression",
        )
        execution_service.start_execution(project.id, execution.id)
        execution_service.complete_execution(project.id, execution.id)

        # Runtime completion is not aggregate Production completion.  The
        # matching attempt/QC path owns the canonical shot and job projection.
        assert production.get_job(project.id, job.id).status is ProductionJobStatus.RUNNING
        production.complete_attempt(
            project.id,
            attempt.id,
            output_artifact_json={"execution_id": execution.id},
        )
        expected = (
            ProductionJobStatus.SUCCEEDED
            if index == len(shots) - 1
            else ProductionJobStatus.RUNNING
        )
        assert production.get_job(project.id, job.id).status is expected


def test_runtime_failure_stops_later_shots_and_preserves_attempt(context):
    repository, project, job = context
    adapter = MultiShotAdapter(fail_shots={"shot_2"})
    result = make_orchestrator(repository, adapter).run_job(project.id, job.id)
    assert result.status is ProductionJobStatus.FAILED
    assert adapter.submitted_shots == ["shot_1", "shot_2"]
    shots = repository.list_production_shots(job.id)
    assert [shot.status for shot in shots] == [ProductionShotStatus.SUCCEEDED, ProductionShotStatus.FAILED, ProductionShotStatus.PENDING]
    assert len(repository.list_production_attempts(shots[1].id)) == 1


def test_qc_failure_stops_later_shots(context):
    repository, project, job = context
    adapter = MultiShotAdapter(bad_qc_shots={"shot_2"})
    result = make_orchestrator(repository, adapter).run_job(project.id, job.id)
    assert result.status is ProductionJobStatus.FAILED
    assert adapter.submitted_shots == ["shot_1", "shot_2"]
    assert repository.list_production_shots(job.id)[1].status is ProductionShotStatus.FAILED


def test_explicit_rejected_human_review_blocks_progression(context):
    repository, project, job = context
    adapter = MultiShotAdapter()

    class RejectingQC(ProductionQCService):
        def run_qc(self, project_id, execution_id, artifact_id=None):
            result = super().run_qc(project_id, execution_id, artifact_id)
            self.create_review(project_id, result.id, ProductionReviewDecision.REJECTED, notes="needs review")
            return result

    execution_service = ProductionExecutionService(repository)
    worker = ProductionWorker(execution_service, adapter, max_polls=3)
    orchestrator = ProductionOrchestrator(
        repository,
        execution_service=execution_service,
        qc_service=RejectingQC(repository),
        worker=worker,
        adapter=adapter,
    )
    result = orchestrator.run_job(project.id, job.id)
    assert result.status is ProductionJobStatus.FAILED
    assert adapter.submitted_shots == ["shot_1"]
    assert repository.list_production_shots(job.id)[0].status is ProductionShotStatus.FAILED


def test_cold_resume_starts_first_unfinished_shot_and_is_idempotent(context):
    repository, project, job = context
    adapter = MultiShotAdapter()
    orchestrator = make_orchestrator(repository, adapter)
    orchestrator.production_service.create_production_shots(project.id, job.id)
    shots = repository.list_production_shots(job.id)
    for shot in shots[:2]:
        assert orchestrator._run_shot(project.id, job, shot, adapter)
    resumed_adapter = MultiShotAdapter()
    resumed = make_orchestrator(repository, resumed_adapter)
    result = resumed.resume_job(project.id, job.id)
    assert result.status is ProductionJobStatus.SUCCEEDED
    assert resumed_adapter.submitted_shots == ["shot_3"]
    before = len(repository.list_production_executions(job.id))
    resumed.resume_job(project.id, job.id)
    assert len(repository.list_production_executions(job.id)) == before


def test_failed_job_resume_does_not_implicitly_create_retry_history(context):
    repository, project, job = context
    adapter = MultiShotAdapter(fail_shots={"shot_2"})
    orchestrator = make_orchestrator(repository, adapter)
    orchestrator.run_job(project.id, job.id)
    executions_before = len(repository.list_production_executions(job.id))
    attempts_before = sum(len(repository.list_production_attempts(shot.id)) for shot in repository.list_production_shots(job.id))
    orchestrator.resume_job(project.id, job.id)
    assert len(repository.list_production_executions(job.id)) == executions_before
    assert sum(len(repository.list_production_attempts(shot.id)) for shot in repository.list_production_shots(job.id)) == attempts_before


def test_project_isolation_and_progress_are_derived_from_persisted_shots(context):
    repository, project, job = context
    orchestrator = make_orchestrator(repository, MultiShotAdapter())
    orchestrator.production_service.create_production_shots(project.id, job.id)
    progress = orchestrator.get_job_progress(project.id, job.id)
    assert progress["total_shots"] == 3
    assert progress["pending_shots"] == 3
    assert progress["percent_complete"] == 0
    other = ProjectService(repository).create(title="Other")
    with pytest.raises(Exception, match="不属于|项目不存在"):
        orchestrator.get_job_progress(other.id, job.id)


def test_cancellation_stops_current_and_future_shots(context):
    repository, project, job = context
    adapter = MultiShotAdapter()
    orchestrator = make_orchestrator(repository, adapter)
    orchestrator.production_service.create_production_shots(project.id, job.id)
    shots = repository.list_production_shots(job.id)
    snapshot = orchestrator._shot_snapshot(project.id, job, shots[0])
    execution = orchestrator.execution_service.enqueue_shot_execution(project.id, job.id, snapshot, "mock")
    orchestrator.production_service.start_attempt(project.id, shots[0].id, "mock", input_snapshot_json=snapshot.to_json_dict())
    cancelled = orchestrator.cancel_job(project.id, job.id, "user stopped")
    assert cancelled.status is ProductionJobStatus.CANCELLED
    assert repository.get_production_execution(execution.id).status is ProductionExecutionStatus.CANCELLED
    assert repository.list_production_shots(job.id)[0].status is ProductionShotStatus.SKIPPED
    assert repository.list_production_shots(job.id)[1].status is ProductionShotStatus.PENDING
