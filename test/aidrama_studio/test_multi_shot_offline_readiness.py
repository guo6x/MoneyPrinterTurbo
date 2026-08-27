from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    AspectRatio,
    Character,
    Location,
    ProductionJobStatus,
    ProductionReviewDecision,
    ProductionShotStatus,
    ReferenceAssetType,
    ReferenceBindingType,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
)
from aidrama_studio.services import (
    CapabilityRegistry,
    CurrentProductionStateService,
    FinalAssemblyRuntimeService,
    FinalAssemblyService,
    HeavyJobRunner,
    HeavyJobService,
    ProductionExecutionService,
    ProductionOrchestrator,
    ProductionQueueService,
    ProductionQCService,
    ProductionService,
    ProductionWorker,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
    RuntimeVideoProvider,
)
from aidrama_studio.services.adapters import MockProductionAdapter
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.video_fixtures import mp4_bytes


SHOT_DURATION_SECONDS = 5


class _OfflineSyntheticShotAdapter(MockProductionAdapter):
    """Fake only the paid provider while returning physical H.264 media."""

    name = "offline-synthetic-multi-shot"

    def __init__(self, shot_ids: list[str]) -> None:
        super().__init__()
        self.submitted_shots: list[str] = []
        self.payloads = {
            shot_id: mp4_bytes(
                source=(
                    "testsrc2=size=320x180:rate=24:"
                    f"duration={SHOT_DURATION_SECONDS},hue=h={index * 30}"
                )
            )
            for index, shot_id in enumerate(shot_ids)
        }

    def submit(self, snapshot):
        submission = super().submit(snapshot)
        shot_id = next(iter(snapshot.shot_parameters))
        self.submitted_shots.append(shot_id)
        self.succeed(
            submission.runtime_reference,
            artifacts=[
                {
                    "artifact_type": "video",
                    "filename": f"{shot_id}.mp4",
                    "content": self.payloads[shot_id],
                    "metadata": {
                        "mime_type": "video/mp4",
                        "duration_seconds": SHOT_DURATION_SECONDS,
                        "resolution": {"width": 320, "height": 180},
                        "codec": "h264",
                        "audio_stream": False,
                        "audio_required": False,
                        "black_frame_detected": False,
                        "static_frame_detected": False,
                        "synthetic_shot_id": shot_id,
                    },
                }
            ],
        )
        return submission


def _repository(tmp_path: Path, shot_count: int) -> tuple[ProjectRepository, object, object]:
    data_root = tmp_path / "aidrama-data"
    repository = ProjectRepository(
        DatabasePaths(
            data_root / "aidrama.db",
            data_root / "projects",
            data_root / "archived-projects",
        )
    )
    target_duration = shot_count * SHOT_DURATION_SECONDS
    project = ProjectService(repository).create(
        title=f"Offline {shot_count}-shot readiness",
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=target_duration,
        delivery_resolution_label="720p",
        target_fps=24,
        quality_mode="FINAL",
    )
    story = StoryBible(
        title="Synthetic multi-shot story",
        logline="A deterministic offline story used only for acceptance.",
        premise="Every shot is persisted and assembled in canonical order.",
        genre="Drama",
        tone="Restrained",
        world=World(era="Present", setting="Offline fixture"),
        characters=[Character(id="char_001", name="Driver")],
        locations=[Location(id="loc_001", name="Terminal")],
        story_beats=[
            StoryBeat(
                id=f"beat_{index:03d}",
                order=index,
                type=(
                    "OPENING"
                    if index == 1
                    else "ENDING"
                    if index == shot_count
                    else "DEVELOPMENT"
                ),
                summary=f"Synthetic beat {index}",
                characters=["char_001"],
                location_id="loc_001",
            )
            for index in range(1, shot_count + 1)
        ],
    )
    repository.create_story_revision(
        revision_id="story_001",
        project_id=project.id,
        version=1,
        status=StoryRevisionStatus.APPROVED,
        content=story,
        generation_input={"offline": True},
        created_at="2026-08-27T00:00:00+00:00",
        updated_at="2026-08-27T00:00:00+00:00",
    )
    script = StructuredScript(
        title="Synthetic multi-shot script",
        scenes=[
            Scene(
                id=f"scene_{index:03d}",
                order=index,
                title=f"Scene {index:02d}",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=SHOT_DURATION_SECONDS,
                beats=[
                    ScriptBeat(
                        id=f"script_beat_{index:03d}",
                        order=1,
                        type=ScriptBeatType.ACTION,
                        text=f"Synthetic action {index}",
                        estimated_duration_seconds=SHOT_DURATION_SECONDS,
                    )
                ],
            )
            for index in range(1, shot_count + 1)
        ],
    )
    repository.create_script_revision(
        revision_id="script_001",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_001",
        content=script,
        generation_input={"offline": True},
        created_at="2026-08-27T00:00:01+00:00",
        updated_at="2026-08-27T00:00:01+00:00",
    )
    plan = ShotPlan(
        title="Synthetic multi-shot plan",
        source_script_revision_id="script_001",
        shots=[
            Shot(
                id=f"shot_{index:03d}",
                order=index,
                scene_id=f"scene_{index:03d}",
                source_script_beat_ids=[f"script_beat_{index:03d}"],
                duration_seconds=SHOT_DURATION_SECONDS,
                subject=["char_001"],
                action=f"Synthetic action {index}",
                visual_intent=f"Distinct synthetic shot {index}",
            )
            for index in range(1, shot_count + 1)
        ],
    )
    repository.create_shot_revision(
        revision_id="shot_plan_001",
        project_id=project.id,
        version=1,
        status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_001",
        content=plan,
        generation_input={"offline": True},
        created_at="2026-08-27T00:00:02+00:00",
        updated_at="2026-08-27T00:00:02+00:00",
    )
    references = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(references)
    for asset_type, binding_type, binding_id, color in (
        (
            ReferenceAssetType.CHARACTER_REFERENCE,
            ReferenceBindingType.CHARACTER,
            "char_001",
            "red",
        ),
        (
            ReferenceAssetType.LOCATION_REFERENCE,
            ReferenceBindingType.LOCATION,
            "loc_001",
            "blue",
        ),
    ):
        asset = references.create_asset(project.id, asset_type)
        version = storage.import_image(
            project.id,
            asset.id,
            png_bytes(color=color),
            filename=f"{binding_id}.png",
            mime_type="image/png",
            metadata={"source_story_revision_id": "story_001", "offline": True},
        )
        references.activate_version(project.id, asset.id, version.id)
        references.bind_version(project.id, version.id, binding_type, binding_id)
    production = ProductionService(repository, reference_service=references)
    readiness = production.validate_job_readiness(project.id, "shot_plan_001")
    assert readiness["ready"] is True
    job = production.create_production_job(project.id, "shot_plan_001")
    return repository, project, job


def test_offline_multi_shot_queue_freezes_one_runtime_plan_per_shot(
    tmp_path: Path,
) -> None:
    repository, project, job = _repository(tmp_path, 3)
    registry = CapabilityRegistry(
        [
            RuntimeVideoProvider(
                MockProductionAdapter(),
                provider_name="OFFLINE_PLAN_ONLY",
            )
        ]
    )
    queue = ProductionQueueService(repository, registry=registry)
    preview = queue.preview_authorization(project.id, job.id)
    authorization = {
        "approved": True,
        "provider_id": preview.provider_id,
        "model_id": preview.model_id,
        "max_paid_attempts": preview.max_paid_attempts,
        "estimated_provider_requests": preview.estimated_provider_requests,
        "deployment_region": preview.deployment_region,
        "endpoint_profile_id": preview.endpoint_profile_id,
        "endpoint_class": preview.endpoint_class,
        "reference_count": preview.reference_count,
        "authorization_fingerprint": preview.authorization_fingerprint,
    }

    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=authorization,
    )

    plan_ids = task.request_summary["runtime_plan_ids_by_shot"]
    assert list(plan_ids) == ["shot_001", "shot_002", "shot_003"]
    assert len(set(plan_ids.values())) == 3
    plans = [repository.get_runtime_plan(plan_id) for plan_id in plan_ids.values()]
    assert all(plan is not None for plan in plans)
    assert [plan.target_creative_duration for plan in plans] == [5, 5, 5]
    assert [plan.provider_generation_duration for plan in plans] == [5, 5, 5]
    assert all(plan.estimated_request_count == 1 for plan in plans)
    assert repository.list_production_executions(job.id) == []


@pytest.mark.parametrize("shot_count", [3, 12])
def test_offline_multi_shot_production_to_real_final_assembly_and_cold_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shot_count: int,
) -> None:
    network_attempts: list[tuple[object, ...]] = []

    def deny_network(*args: object, **_kwargs: object) -> None:
        network_attempts.append(args)
        raise AssertionError("offline multi-shot acceptance attempted network I/O")

    monkeypatch.delenv("AIDRAMA_ALLOW_PAID", raising=False)
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)

    repository, project, job = _repository(tmp_path, shot_count)
    shot_ids = [f"shot_{index:03d}" for index in range(1, shot_count + 1)]
    adapter = _OfflineSyntheticShotAdapter(shot_ids)
    execution_service = ProductionExecutionService(repository)
    orchestrator = ProductionOrchestrator(
        repository,
        execution_service=execution_service,
        worker=ProductionWorker(execution_service, adapter, max_polls=3),
        adapter=adapter,
    )

    completed = orchestrator.run_job(project.id, job.id)

    assert completed.status is ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == shot_ids
    production_shots = repository.list_production_shots(job.id)
    assert [item.shot_id for item in production_shots] == shot_ids
    assert all(item.status is ProductionShotStatus.SUCCEEDED for item in production_shots)
    executions = repository.list_production_executions(job.id)
    assert len(executions) == shot_count
    assert [
        next(iter(item.input_snapshot.shot_parameters)) for item in executions
    ] == shot_ids

    qc_service = ProductionQCService(repository)
    artifact_hashes: list[str] = []
    for execution in executions:
        artifacts = execution_service.list_artifacts(project.id, execution.id)
        assert len(artifacts) == 1
        artifact_path = repository.paths.projects / project.id / artifacts[0].path
        artifact_hashes.append(hashlib.sha256(artifact_path.read_bytes()).hexdigest())
        qc_results = qc_service.list_results(project.id, execution.id)
        assert len(qc_results) == 1 and qc_results[0].status.value == "QC_PASS"
        qc_service.create_review(
            project.id,
            qc_results[0].id,
            ProductionReviewDecision.APPROVED,
            reviewer="offline-multi-shot-acceptance",
        )
    assert len(set(artifact_hashes)) == shot_count

    manifest_service = FinalAssemblyService(repository)
    final_readiness = manifest_service.calculate_readiness(project.id, job.id)
    assert final_readiness.ready is True
    assert final_readiness.eligible_shots == shot_count
    decisions = []
    for shot in production_shots:
        qualified = manifest_service.select_qualified_source(
            project.id,
            job.id,
            shot.id,
        )
        decisions.append(
            manifest_service.select_shot_source(
                project.id,
                job.id,
                shot.id,
                production_execution_id=qualified.production_execution_id,
                production_artifact_id=qualified.production_artifact_id,
                selected_by="offline-multi-shot-acceptance",
            )
        )
    assert len(decisions) == shot_count
    assert all(item.selection_kind.value == "FINAL_ACCEPTED" for item in decisions)
    sources = [
        manifest_service.select_qualified_source(project.id, job.id, shot.id)
        for shot in production_shots
    ]
    assert all(source.review_id for source in sources)
    assert [source.source_decision_id for source in sources] == [
        item.id for item in decisions
    ]
    assembly = manifest_service.create_assembly(project.id, job.id, freeze=True)
    manifest = manifest_service.get_manifest(project.id, assembly.id)
    assert [item.order_index for item in manifest.items] == list(
        range(1, shot_count + 1)
    )
    assert [
        repository.get_production_shot(item.production_shot_id).shot_id
        for item in manifest.items
    ] == shot_ids
    assert len({item.production_artifact_id for item in manifest.items}) == shot_count
    assert [item.source_decision_id for item in manifest.items] == [
        item.id for item in decisions
    ]

    heavy_job = HeavyJobService(repository).enqueue_final_assembly(
        project.id,
        assembly.id,
    )
    rendered_job = HeavyJobRunner(repository).run_once(project.id)
    assert rendered_job is not None and rendered_job.id == heavy_job.id
    assert rendered_job.status.value == "SUCCEEDED"
    attempts = FinalAssemblyRuntimeService(repository).list_attempts(
        project.id,
        assembly.id,
    )
    assert len(attempts) == 1 and attempts[0].status.value == "SUCCEEDED"
    final_attempt = attempts[0]
    target_duration = shot_count * SHOT_DURATION_SECONDS
    assert final_attempt.metadata_json["duration_seconds"] == pytest.approx(
        target_duration,
        abs=0.2,
    )
    assert final_attempt.metadata_json["video_stream"] is True
    assert str(final_attempt.metadata_json["codec"]).lower() in {"h264", "avc1"}
    assert [
        item["order_index"] for item in final_attempt.metadata_json["source_items"]
    ] == list(range(1, shot_count + 1))
    output = FinalAssemblyRuntimeService(repository).resolve_output_path(
        project.id,
        assembly.id,
        final_attempt.id,
    )
    assert output.is_file() and output.stat().st_size > 0
    assert hashlib.sha256(output.read_bytes()).hexdigest() == final_attempt.metadata_json[
        "sha256"
    ]

    cold_repository = ProjectRepository(repository.paths)
    cold_state = CurrentProductionStateService(cold_repository).derive(
        project.id,
        job.id,
    )
    assert cold_state.production_complete is True
    assert len(cold_state.qualified_sources) == shot_count
    cold_output = FinalAssemblyRuntimeService(cold_repository).resolve_output_path(
        project.id,
        assembly.id,
        final_attempt.id,
    )
    assert cold_output == output
    assert cold_output.is_file()
    fake_tasks = cold_repository.list_provider_tasks(project.id)
    assert len(fake_tasks) == shot_count
    assert {item.provider_id for item in fake_tasks} == {
        "offline-synthetic-multi-shot"
    }
    assert network_attempts == []

    evidence = {
        "project_id": project.id,
        "production_job_id": job.id,
        "assembly_id": assembly.id,
        "final_attempt_id": final_attempt.id,
        "shot_count": shot_count,
        "shot_ids": shot_ids,
        "duration_seconds": final_attempt.metadata_json["duration_seconds"],
        "resolution": final_attempt.metadata_json["resolution"],
        "codec": final_attempt.metadata_json["codec"],
        "sha256": final_attempt.metadata_json["sha256"],
        "data_root": str(repository.paths.root),
        "output": str(output),
        "fake_provider_tasks": shot_count,
        "live_provider_tasks": 0,
        "network_attempts": 0,
    }
    (tmp_path / "offline-multi-shot-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
