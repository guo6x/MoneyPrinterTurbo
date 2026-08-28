from __future__ import annotations

import json

from aidrama_studio.domain import (
    PreLiveFirstFrameGate,
    ProductionReviewDecision,
    ShotFirstFrameSourceType,
)
from aidrama_studio.domain.continuity import ContinuityIssueType, ContinuitySourceKind
from aidrama_studio.services import (
    ContinuityEngine,
    CreativeIntakeService,
    CreativePipelineService,
    FinalAssemblyRuntimeService,
    FinalAssemblyService,
    GenerationBriefService,
    ProductionQCService,
    ScriptService,
    ShotService,
    ShotKeyframePolicy,
    ShotKeyframeService,
    StoryService,
)
from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter
from aidrama_studio.services.ai_capabilities import CapabilityRegistry
from aidrama_studio.services.llm_runtime import LLMInvocationGateway
from aidrama_studio.services.model_runtime import CapabilityKind, InMemoryManifestRegistry
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_continuity_engine import (
    _candidate,
    _facts,
    _request,
    _types,
    _umbrella,
    _wardrobe,
)
from test.aidrama_studio.test_auto_orchestrator import (
    _AutoServices,
    _FakeImageProvider as _AutoImageProvider,
    _FakeLLMGateway as _AutoLLMGateway,
    _FakeVideoAdapter as _AutoVideoAdapter,
)
from test.aidrama_studio.test_creative_pipeline import _FakeLLMProvider
from test.aidrama_studio.test_vision_universal_runtime import (
    FakeVisionSession,
    _wired_service,
)
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_shot_keyframe_continuity import (
    _FakeImageRuntime as _FakeKeyframeImageRuntime,
    _image_binding as _keyframe_image_binding,
)
from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1.test_21_reference_auto_reliability_regression import (
    _complete_canonical_references,
)
from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1.test_41_quality_core_regression import (
    _decode_probe,
)


def test_current_eight_feature_offline_product_path_cold_reloads_and_plays(
    canonical_approved_project: dict[str, object],
    database_paths,
    ffmpeg_path: str,
    provider_calls,
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    canonical_story = canonical_approved_project["story"]
    canonical_script = canonical_approved_project["script"]
    canonical_plan = canonical_approved_project["shot_plan"]

    intake = CreativeIntakeService(repository)
    source = intake.source_pack.import_text(project.id, project.description)
    brief = intake.normalize(
        project.id,
        source_ids=[source.id],
        overrides={"genre": "Drama", "tone": "Rainy and restrained"},
    )
    approved_brief = intake.approve_brief(project.id, brief.id)
    llm = _FakeLLMProvider(
        [
            json.dumps(canonical_story.model_dump(mode="json")),
            json.dumps(canonical_script.model_dump(mode="json")),
            json.dumps(canonical_plan.model_dump(mode="json")),
        ],
        provider_name="OFFLINE_UNIVERSAL_LLM",
        model="fake-llm-wave1-v1",
    )
    gateway = LLMInvocationGateway(repository, registry=CapabilityRegistry([llm]))
    creative = CreativePipelineService(
        repository,
        story_service=StoryService(repository, llm_gateway=gateway),
        script_service=ScriptService(repository, llm_gateway=gateway),
        shot_service=ShotService(repository, llm_gateway=gateway),
    )
    story = creative.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": approved_brief.id, "regenerate": True},
    )
    approved_story = StoryService(repository).approve_revision(story["id"])
    script = creative.execute(
        project_id=project.id,
        operation="GENERATE_SCRIPT",
        payload={"source_story_revision_id": approved_story["id"]},
    )
    approved_script = ScriptService(repository).approve_revision(script["id"])
    plan = creative.execute(
        project_id=project.id,
        operation="GENERATE_SHOT_PLAN",
        payload={"source_script_revision_id": approved_script["id"]},
    )
    approved_plan = ShotService(repository).approve_revision(plan["id"])
    assert len(approved_plan["content"].shots) == 6
    assert llm.calls == 3
    assert all(
        item.request_summary["llm_runtime"] == "UNIVERSAL"
        for item in repository.list_ai_invocations(project.id)
    )

    reference_agent, image_provider = _complete_canonical_references(
        repository,
        project.id,
    )
    assert reference_agent.evaluate(project.id).production_reference_ready is True
    assert len(image_provider.calls) == 2

    auto_video = _AutoVideoAdapter()
    auto_services = _AutoServices(
        repository,
        _AutoLLMGateway(),
        _AutoImageProvider(),
        auto_video,
    )
    auto = auto_services.orchestrator()
    assert auto.next_action(project.id).next_action.value == "PREPARE_PRODUCTION"
    prepared = auto.step(project.id)
    assert prepared.current_stage.value == "PRODUCTION"
    auto_results = [event.result for event in auto.list_events(project.id)]
    assert any(
        event.result == "PRODUCTION_PREPARED"
        for event in auto.list_events(project.id)
    ), (prepared, auto_results)
    job = repository.list_production_jobs(project.id)[0]
    assert job.shot_plan_revision_id == approved_plan["id"]

    # Pre-generation visual continuity is a distinct product layer. Compile
    # each keyframe from the exact approved snapshot/GenerationBrief, execute
    # it through the Universal IMAGE seam, and persist the result as a real
    # ProductionExecution + ProductionArtifact before VIDEO authorization.
    keyframes = ShotKeyframeService(repository)
    keyframe_authorization_fingerprint = "a" * 64
    input_snapshot = auto_services.execution.create_input_snapshot(
        project.id, job.id
    )
    generation_briefs = GenerationBriefService(repository).prepare_for_job(
        project.id, job.id
    )
    generation_briefs_by_shot = {
        brief.shot_id: brief for brief in generation_briefs
    }
    keyframe_runtime = _FakeKeyframeImageRuntime(
        tuple(
            png_bytes(color=color)
            for color in ("red", "blue", "green", "yellow", "purple", "orange")
        )
    )
    keyframe_binding = _keyframe_image_binding(keyframe_runtime)
    keyframe_parameters = {"resolution": "1024*1024"}
    keyframe_settings = SettingsModelService(
        repository,
        manifest_registry=InMemoryManifestRegistry(
            (keyframe_binding.manifest,)
        ),
    )
    keyframe_settings.save_selections(
        project_id=project.id,
        selections={CapabilityKind.IMAGE: keyframe_binding.manifest.id},
    )
    keyframe_briefs_by_shot = {
        shot.id: keyframes.briefs.compile(
            input_snapshot,
            shot.id,
            generation_briefs_by_shot[shot.id],
        )
        for shot in approved_plan["content"].shots
    }
    keyframe_runtime_plans = {
        shot.id: keyframes.freeze_runtime_plan(
            project.id,
            job.id,
            keyframe_briefs_by_shot[shot.id],
            keyframe_binding,
            provider_parameters=keyframe_parameters,
            authorization={
                "contract": "OFFLINE_SHOT_KEYFRAME_RUNTIMEPLAN_V1",
                "create_authorized": True,
                "shot_id": shot.id,
                "per_item_max": 1,
                "automatic_paid_retry": 0,
            },
            selection_service=keyframe_settings,
        )
        for shot in approved_plan["content"].shots
    }
    keyframes.authorize_paid_creates(
        project.id,
        job.id,
        authorization_fingerprint=keyframe_authorization_fingerprint,
        planned_creates=len(approved_plan["content"].shots),
        authorized_max=len(approved_plan["content"].shots),
        authorized_intents=[
            keyframes.paid_create_intent(
                keyframe_briefs_by_shot[shot.id],
                keyframe_binding,
                runtime_plan=keyframe_runtime_plans[shot.id],
                provider_parameters=keyframe_parameters,
            )
            for shot in approved_plan["content"].shots
        ],
    )
    previous_shot = None
    for shot in approved_plan["content"].shots:
        generation_brief = generation_briefs_by_shot[shot.id]
        keyframe_brief = keyframe_briefs_by_shot[shot.id]
        selection = ShotKeyframePolicy.select(
            shot,
            project_id=project.id,
            previous=previous_shot,
            continuous_action=False,
        )
        assert selection.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME
        frozen = keyframes.generate_and_record(
            project.id,
            job.id,
            keyframe_brief,
            selection,
            keyframe_binding,
            runtime_plan=keyframe_runtime_plans[shot.id],
            provider_parameters=keyframe_parameters,
            create_authorized=True,
            authorization_fingerprint=keyframe_authorization_fingerprint,
        )
        assert frozen.shot_id == shot.id
        assert frozen.generation_brief_id == generation_brief.id
        previous_shot = shot
    pre_live, frozen_frames = keyframes.require_pre_live(project.id, job.id)
    assert pre_live.gate is PreLiveFirstFrameGate.PASS
    assert pre_live.unintended_duplicate_first_frame_count == 0
    assert set(frozen_frames) == {
        shot.id for shot in approved_plan["content"].shots
    }
    assert keyframe_runtime.real_provider_calls == 0
    assert keyframe_runtime.paid_calls == 0

    paid_gate = auto.next_action(project.id)
    assert paid_gate.next_action.value == "PAID_AUTHORIZATION_REQUIRED"
    preview = auto.preview_paid_authorization(project.id)
    assert preview.required_create_count == 6
    auto.grant_paid_authorization(
        project.id,
        authorization_fingerprint=preview.authorization_fingerprint,
        global_max=preview.required_create_count,
        per_item_max=1,
        retry_limit=0,
    )
    queued = auto.step(project.id)
    assert queued.status.value == "WAITING_PROVIDER"
    review_gate = auto.resume(project.id)
    assert review_gate.status.value == "WAITING_HUMAN"
    assert review_gate.current_stage.value == "REVIEW"
    assert auto_video.submit_count == 6

    executions = [
        execution
        for execution in repository.list_production_executions(job.id)
        if execution.worker_type != "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
    ]
    assert len(executions) == 6
    first_artifact = repository.list_production_artifacts(executions[0].id)[0]
    vision_session = FakeVisionSession()
    vision, *_vision_dependencies = _wired_service(
        repository,
        project,
        vision_session,
    )
    analysis = vision.analyze(project.id, executions[0].id, first_artifact.id)
    assert analysis.status == "AI_ANALYSIS", analysis.reason
    assert len(vision_session.calls) == 1

    scope = {
        "project_id": project.id,
        "script_revision_id": approved_script["id"],
        "shot_plan_revision_id": approved_plan["id"],
    }
    baseline = _facts(
        "shot_01",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        next_shot_id="shot_02",
    )
    consistent = _facts(
        "shot_02",
        wardrobe=_wardrobe("black"),
        props=_umbrella(),
        previous_shot_id="shot_01",
        next_shot_id="shot_03",
    )
    drift = _facts(
        "shot_03",
        wardrobe=_wardrobe("white"),
        props=(),
        previous_shot_id="shot_02",
    )
    continuity = ContinuityEngine(repository).evaluate_sequence(
        (
            _request(
                "shot_01",
                1,
                (
                    _candidate(
                        baseline,
                        ContinuitySourceKind.HUMAN_LOCKED_STATE,
                        "human-lock-shot-01",
                    ),
                ),
                (
                    _candidate(
                        baseline,
                        ContinuitySourceKind.VISION_QC_OBSERVATION,
                        analysis.analysis_id,
                    ),
                ),
                approved_for_continuity=True,
            ).model_copy(update=scope),
            _request(
                "shot_02",
                2,
                (),
                (
                    _candidate(
                        consistent,
                        ContinuitySourceKind.VISION_QC_OBSERVATION,
                        analysis.analysis_id,
                    ),
                ),
                approved_for_continuity=True,
            ).model_copy(update=scope),
            _request(
                "shot_03",
                3,
                (),
                (
                    _candidate(
                        drift,
                        ContinuitySourceKind.VISION_QC_OBSERVATION,
                        analysis.analysis_id,
                    ),
                ),
            ).model_copy(update=scope),
        )
    )
    assert {
        ContinuityIssueType.WARDROBE_DRIFT,
        ContinuityIssueType.PROP_DRIFT,
    } <= _types(continuity[-1])

    qc = ProductionQCService(repository)
    qc_results = repository.list_production_qc_results(project.id)
    assert len(qc_results) == 6
    for result in qc_results:
        qc.create_review(
            project.id,
            result.id,
            ProductionReviewDecision.APPROVED,
            reviewer="wave1-human",
        )
    final = FinalAssemblyService(repository)
    assembly = final.create_assembly(project.id, job.id, freeze=True)
    manifest = final.get_manifest(project.id, assembly.id)
    assert [item.order_index for item in manifest.items] == list(range(1, 7))

    final_runtime = FinalAssemblyRuntimeService(
        repository,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id,
            ffmpeg_binary=ffmpeg_path,
        ),
    )
    rendered = final_runtime.render(project.id, assembly.id)
    output = repository.paths.projects / project.id / rendered.output_relative_path
    assert rendered.status.value == "SUCCEEDED"
    assert output.is_file()
    assert "Video: h264" in _decode_probe(ffmpeg_path, output)

    cold = ProjectRepository(database_paths)
    assert cold.get_project(project.id) is not None
    assert len(cold.list_creative_pipeline_operations(project.id)) == 3
    cold_executions = cold.list_production_executions(job.id)
    assert len(
        [
            execution
            for execution in cold_executions
            if execution.worker_type == "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
        ]
    ) == 6
    assert len(
        [
            execution
            for execution in cold_executions
            if execution.worker_type != "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
        ]
    ) == 6
    cold_keyframes = ShotKeyframeService(cold)
    cold_pre_live, cold_frames = cold_keyframes.require_pre_live(
        project.id, job.id
    )
    assert cold_pre_live.gate is PreLiveFirstFrameGate.PASS
    assert set(cold_frames) == {
        shot.id for shot in approved_plan["content"].shots
    }
    assert len(cold.list_continuity_issues(project.id, shot_id="shot_03")) >= 2
    assert cold.get_final_assembly_render_attempt(rendered.id).status.value == "SUCCEEDED"
    provider_calls.llm = llm.calls
    provider_calls.image = len(image_provider.calls) + len(keyframe_runtime.requests)
    provider_calls.video_create = auto_video.submit_count
    provider_calls.vision = len(vision_session.calls)
    assert provider_calls.real_provider_calls == 0
    assert provider_calls.paid == 0
