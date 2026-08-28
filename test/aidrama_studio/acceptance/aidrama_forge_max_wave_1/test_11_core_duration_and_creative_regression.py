from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.services import (
    DurationPlanningService,
    GenerationBriefService,
    OutputProfileService,
    ProjectService,
    RuntimePlanService,
)
from aidrama_studio.services.creative_pipeline import CreativePipelineError
from aidrama_studio.services.model_runtime import CapabilityKind
from aidrama_studio.services.model_settings import CAPABILITY_ORDER, SettingsModelService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService
from test.aidrama_studio.test_creative_pipeline import (
    _FakeLLMProvider,
    _approved_intake,
    _pipeline,
    _plan,
    _script,
    _story,
)
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)
from test.aidrama_studio.test_universal_model_settings import (
    FakeCredentialStore,
    _manifest_id,
)


def test_creative_intake_to_approved_shot_plan_uses_universal_llm(
    tmp_path,
) -> None:
    provider = _FakeLLMProvider(
        [
            json.dumps(_story().model_dump(mode="json")),
            json.dumps(_script()),
            json.dumps(_plan()),
        ],
        provider_name="OFFLINE_UNIVERSAL_LLM",
        model="fake-llm-wave1-v1",
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)

    story = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": brief.id},
    )
    assert story["status"] is StoryRevisionStatus.DRAFT
    assert story["generation_input"]["normalized_brief_id"] == brief.id
    replay = pipeline.execute(
        project_id=project.id,
        operation="STORY_BIBLE_GENERATION",
        payload={"normalized_brief_id": brief.id},
    )
    assert replay["id"] == story["id"]
    assert provider.calls == 1

    approved_story = StoryService(repository).approve_revision(story["id"])
    script = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SCRIPT",
        payload={"source_story_revision_id": approved_story["id"]},
    )
    assert script["status"] is ScriptRevisionStatus.DRAFT
    assert script["source_story_revision_id"] == approved_story["id"]
    approved_script = ScriptService(repository).approve_revision(script["id"])

    shot_plan = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SHOT_PLAN",
        payload={"source_script_revision_id": approved_script["id"]},
    )
    assert shot_plan["status"] is ShotRevisionStatus.DRAFT
    assert shot_plan["source_script_revision_id"] == approved_script["id"]
    assert shot_plan["content"].source_script_revision_id == approved_script["id"]
    approved_plan = ShotService(repository).approve_revision(shot_plan["id"])
    assert approved_plan["status"] is ShotRevisionStatus.APPROVED

    operations = repository.list_creative_pipeline_operations(project.id)
    invocations = repository.list_ai_invocations(project.id)
    assert [item.operation for item in reversed(operations)] == [
        "GENERATE_STORY",
        "GENERATE_SCRIPT",
        "GENERATE_SHOT_PLAN",
    ]
    assert {item.provider_id for item in operations} == {"OFFLINE_UNIVERSAL_LLM"}
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"] * 3
    assert all(item.request_summary["llm_runtime"] == "UNIVERSAL" for item in invocations)
    assert all(item.request_summary["protocol"] == "REQUEST_RESPONSE" for item in invocations)
    assert provider.calls == 3


def test_creative_structured_failure_is_bounded_and_preserves_approval(
    tmp_path,
) -> None:
    provider = _FakeLLMProvider(
        ["not-json", "still-not-json"],
        provider_name="OFFLINE_UNIVERSAL_LLM",
        model="fake-llm-wave1-invalid",
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    previous = StoryService(repository).create_blank_draft(project)
    StoryService(repository).approve_revision(previous["id"])

    with pytest.raises(CreativePipelineError, match="一次修复"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_STORY",
            payload={"normalized_brief_id": brief.id, "regenerate": True},
        )

    assert provider.calls == 2
    assert StoryService(repository).get_revision(previous["id"])["status"] is StoryRevisionStatus.APPROVED
    assert repository.list_creative_pipeline_operations(project.id)[0].status.value == "FAILED"


def test_settings_selection_drives_future_plan_and_preserves_frozen_plan(
    tmp_path,
    canonical_project: dict[str, object],
    assert_public_safe,
) -> None:
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    brief = GenerationBriefService(repository).prepare_for_job(project.id, job.id)[0]
    secret = canonical_project["security_canaries"]["api_key"]
    store = FakeCredentialStore(
        {
            "DASHSCOPE_API_KEY": secret,
            "DEEPSEEK_API_KEY": secret,
            "ARK_API_KEY": secret,
        }
    )
    settings = SettingsModelService(repository, credential_store=store)
    selected_models = {
        CapabilityKind.LLM: "qwen-max",
        CapabilityKind.IMAGE: "qwen-image-3.0",
        CapabilityKind.VIDEO: "wan2.7-i2v-2026-04-25",
        CapabilityKind.VISION: "qwen3-vl-flash",
        CapabilityKind.TTS: "qwen3-tts-flash",
    }
    first_selection = {
        capability: _manifest_id(settings, capability, model_id)
        for capability, model_id in selected_models.items()
    }
    settings.save_selections(project_id=project.id, selections=first_selection)

    resolutions = {
        capability: settings.resolve(project.id, capability)
        for capability in CAPABILITY_ORDER
    }
    assert {
        capability: resolution.option.model_id
        for capability, resolution in resolutions.items()
    } == selected_models
    assert all(item.source == "PROJECT_SELECTION" for item in resolutions.values())
    assert_public_safe(
        {
            capability.value: {
                key: value
                for key in (field.name for field in fields(resolution.option))
                if key != "manifest"
                for value in (getattr(resolution.option, key),)
            }
            for capability, resolution in resolutions.items()
        }
    )
    assert_public_safe(settings.credential_requirements())

    plans = RuntimePlanService(repository)
    first = plans.create_from_selection(
        project.id,
        production_job_id=job.id,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=settings,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )
    frozen_before = first.model_dump(mode="json")

    seedance = _manifest_id(
        settings,
        CapabilityKind.VIDEO,
        "doubao-seedance-2-5-260628",
    )
    settings.save_selections(
        project_id=project.id,
        selections={**first_selection, CapabilityKind.VIDEO: seedance},
    )
    second = plans.create_from_selection(
        project.id,
        production_job_id=job.id,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=settings,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )

    assert second.provider_id == "SEEDANCE"
    assert second.model_id == "doubao-seedance-2-5-260628"
    assert repository.get_runtime_plan(first.id).model_dump(mode="json") == frozen_before


def test_custom_duration_has_no_product_cap_and_selected_manifest_plans_bounded_batches(
    tmp_path,
    canonical_project: dict[str, object],
    provider_calls,
) -> None:
    repository, project = _execution_context.__wrapped__(tmp_path)
    secret = canonical_project["security_canaries"]["api_key"]
    settings = SettingsModelService(
        repository,
        credential_store=FakeCredentialStore({"DASHSCOPE_API_KEY": secret}),
    )
    wan = _manifest_id(
        settings,
        CapabilityKind.VIDEO,
        "wan2.7-i2v-2026-04-25",
    )
    settings.save_selections(
        project_id=project.id,
        selections={CapabilityKind.VIDEO: wan},
    )
    planner = DurationPlanningService(repository, settings_service=settings)

    real_demo = planner.plan(project.id, 60, max_batch_size=8)
    assert real_demo.provider_id == "alibaba_model_studio"
    assert real_demo.model_id == "wan2.7-i2v-2026-04-25"
    assert real_demo.planned_shot_count == 8
    assert real_demo.planned_shot_durations == (
        8.0,
        8.0,
        8.0,
        8.0,
        7.0,
        7.0,
        7.0,
        7.0,
    )
    assert real_demo.total_native_seconds == 60
    assert real_demo.expected_video_create_count == 8
    assert len(real_demo.batches) == 1

    hour = planner.plan(project.id, 3600, max_batch_size=8)
    assert hour.planned_shot_count == hour.expected_video_create_count == 450
    assert set(hour.planned_shot_durations) == {8.0}
    assert hour.total_native_seconds == 3600
    assert len(hour.batches) == 57
    assert max(item.expected_video_create_count for item in hour.batches) == 8
    assert sum(item.expected_video_create_count for item in hour.batches) == 450

    project_service = ProjectService(repository)
    unlimited = project_service.create(
        title="No product duration cap",
        target_duration_seconds=7200,
    )
    assert unlimited.target_duration_seconds == 7200
    assert (
        OutputProfileService(repository)
        .ensure_for_project(unlimited.id)
        .target_episode_duration_seconds
        == 7200
    )
    updated = project_service.update(
        unlimited.id,
        title=unlimited.title,
        description=unlimited.description,
        aspect_ratio=unlimited.aspect_ratio,
        target_duration_seconds=86400,
    )
    assert updated.target_duration_seconds == 86400
    assert (
        OutputProfileService(repository)
        .ensure_for_project(unlimited.id)
        .target_episode_duration_seconds
        == 86400
    )

    for relative in (
        "aidrama_studio/pages/dashboard.py",
        "aidrama_studio/pages/creative.py",
        "aidrama_studio/pages/settings.py",
        "aidrama_studio/pages/production.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "max_value=3600" not in source
        assert "max_value = 3600" not in source
    assert provider_calls.real_provider_calls == 0
    assert provider_calls.video_create == 0
    assert provider_calls.paid == 0


def test_production_orchestrator_respects_bounded_create_batches(tmp_path) -> None:
    from aidrama_studio.domain import ProductionJobStatus
    from test.aidrama_studio.test_production_orchestrator import (
        MultiShotAdapter,
        context as orchestrator_context,
        make_orchestrator,
    )

    repository, project, job = orchestrator_context.__wrapped__(tmp_path)
    adapter = MultiShotAdapter()
    orchestrator = make_orchestrator(repository, adapter)

    first = orchestrator.run_job(project.id, job.id, max_new_creates=1)
    assert first.status is not ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == ["shot_1"]
    assert len(repository.list_production_executions(job.id)) == 1

    second = orchestrator.run_job(project.id, job.id, max_new_creates=1)
    assert second.status is not ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == ["shot_1", "shot_2"]
    assert len(repository.list_production_executions(job.id)) == 2

    third = orchestrator.run_job(project.id, job.id, max_new_creates=1)
    assert third.status is ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == ["shot_1", "shot_2", "shot_3"]
    executions = repository.list_production_executions(job.id)
    assert len(executions) == 3
    assert len({item.id for item in executions}) == 3


def test_production_queue_freezes_local_batch_limit_without_expanding_budget(
    tmp_path,
) -> None:
    from test.aidrama_studio.test_production_queue import _authorization, _queue

    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    queue = _queue(repository)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )

    assert task.request_summary["max_paid_creates_per_tick"] == 8
    assert task.request_summary["estimated_provider_requests"] == 1
    projection = queue.budget_projection(project.id, job.id)
    assert projection.authorized_max == 1
    assert projection.planned_creates == 1


def test_queued_execution_consumes_batch_slot_before_next_paid_create(
    tmp_path,
) -> None:
    from aidrama_studio.domain import ProductionJobStatus
    from test.aidrama_studio.test_production_orchestrator import (
        MultiShotAdapter,
        context as orchestrator_context,
        make_orchestrator,
    )

    repository, project, job = orchestrator_context.__wrapped__(tmp_path)
    adapter = MultiShotAdapter()
    orchestrator = make_orchestrator(repository, adapter)
    shots = orchestrator.production_service.create_production_shots(
        project.id, job.id
    )
    snapshot = orchestrator._shot_snapshot(project.id, job, shots[0])
    execution, _attempt = (
        orchestrator.execution_service.enqueue_shot_execution_with_attempt(
            project.id,
            job.id,
            snapshot,
            worker_type=adapter.name,
        )
    )
    assert execution.status.value == "QUEUED"

    first = orchestrator.run_job(project.id, job.id, max_new_creates=1)
    assert first.status is not ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == ["shot_1"]
    assert len(repository.list_production_executions(job.id)) == 1

    second = orchestrator.run_job(project.id, job.id, max_new_creates=1)
    assert second.status is not ProductionJobStatus.SUCCEEDED
    assert adapter.submitted_shots == ["shot_1", "shot_2"]
    assert len(repository.list_production_executions(job.id)) == 2
