from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aidrama_studio.domain import (
    CameraAngle,
    CameraMovement,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    Shot,
    ShotPlan,
    ShotSize,
    StructuredScript,
)
from aidrama_studio.services.duration_planning import DurationPlanningService
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    MAINLAND_PRIMARY_MANIFEST_IDS,
    default_manifest_registry,
)
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.services.project import ProjectService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService, ShotServiceError
from aidrama_studio.services.shot_parser import ShotPlanParseError, parse_shot_plan
from aidrama_studio.services.shot_prompt import _shot_plan_schema, build_shot_prompt
from aidrama_studio.services.story import StoryService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_story_bible import valid_bible


SCRIPT_REVISION_ID = "c249c8b78c97436f8b39a0aee9b50479"


class _FakeCredentialStore:
    def get(self, key: str) -> str | None:
        return "test-only-dashscope-key" if key == "DASHSCOPE_API_KEY" else None

    def configured(self, key: str) -> bool:
        return self.get(key) is not None


class _ValidatorGateway:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls = 0
        self.prompt = ""

    def generate_validated_json(self, _project_id, prompt, *, validator, **_kwargs):
        self.calls += 1
        self.prompt = prompt
        return validator(json.dumps(self.payload))


def _script() -> StructuredScript:
    return StructuredScript(
        title="Contract script",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Last bus",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=60,
                source_story_beat_ids=["beat_001"],
                beats=[
                    ScriptBeat(
                        id="script_beat_001",
                        order=1,
                        type=ScriptBeatType.ACTION,
                        text="The driver starts the bus.",
                        estimated_duration_seconds=60,
                    )
                ],
            )
        ],
    )


def _canonical_payload() -> dict[str, object]:
    return {
        "title": "Canonical shot plan",
        "summary": "One exact shot",
        "source_script_revision_id": SCRIPT_REVISION_ID,
        "shots": [
            {
                "id": "shot_001",
                "order": 1,
                "scene_id": "scene_001",
                "source_script_beat_ids": ["script_beat_001"],
                "duration_seconds": 60,
                "shot_size": "WIDE",
                "camera_angle": "EYE_LEVEL",
                "camera_movement": "STATIC",
                "composition": "single subject centered in the bus aisle",
                "subject": ["char_001"],
                "action": "The driver starts the last bus.",
                "visual_intent": "Establish the final departure.",
            }
        ],
    }


def test_shot_prompt_projects_domain_schema_and_forbids_script_shape():
    project = SimpleNamespace(
        target_duration_seconds=60,
        aspect_ratio=SimpleNamespace(value="9:16"),
    )
    prompt = build_shot_prompt(
        project,
        _script(),
        valid_bible(),
        source_script_revision_id=SCRIPT_REVISION_ID,
    )

    assert "Generate a SHOT PLAN" in prompt
    assert "DO NOT return a top-level scenes field" in prompt
    assert "shots is the only shot collection" in prompt
    assert SCRIPT_REVISION_ID in prompt
    for field in (
        "id",
        "order",
        "scene_id",
        "source_script_beat_ids",
        "duration_seconds",
        "shot_size",
        "camera_angle",
        "camera_movement",
        "composition",
        "subject",
        "action",
        "visual_intent",
    ):
        assert field in prompt

    schema = json.loads(_shot_plan_schema(SCRIPT_REVISION_ID))
    assert schema["required"] == ["title", "source_script_revision_id", "shots"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["source_script_revision_id"]["const"] == (
        SCRIPT_REVISION_ID
    )
    shot_schema = schema["$defs"]["Shot"]
    assert shot_schema["additionalProperties"] is False
    assert set(shot_schema["properties"]) == set(
        ShotPlan.model_json_schema()["$defs"]["Shot"]["properties"]
    )
    assert schema["$defs"]["ShotSize"]["enum"] == [item.value for item in ShotSize]
    assert schema["$defs"]["CameraAngle"]["enum"] == [
        item.value for item in CameraAngle
    ]
    assert schema["$defs"]["CameraMovement"]["enum"] == [
        item.value for item in CameraMovement
    ]


def test_shot_plan_parser_binds_exact_product_provenance_and_stays_strict():
    script_shaped = {
        "title": "Not a shot plan",
        "scenes": [{"id": "scene_001", "beats": []}],
    }
    with pytest.raises(ShotPlanParseError, match="Shot Plan"):
        parse_shot_plan(
            json.dumps(script_shaped),
            expected_source_script_revision_id=SCRIPT_REVISION_ID,
        )

    canonical = _canonical_payload()
    exact = parse_shot_plan(
        json.dumps(canonical),
        expected_source_script_revision_id=SCRIPT_REVISION_ID,
    )
    assert exact.source_script_revision_id == SCRIPT_REVISION_ID
    assert exact.shots[0].action
    assert exact.shots[0].visual_intent

    missing = dict(canonical)
    missing.pop("source_script_revision_id")
    injected = parse_shot_plan(
        json.dumps(missing),
        expected_source_script_revision_id=SCRIPT_REVISION_ID,
    )
    assert injected.source_script_revision_id == SCRIPT_REVISION_ID

    conflicting = dict(canonical)
    conflicting["source_script_revision_id"] = "different-script-revision"
    with pytest.raises(ShotPlanParseError, match="conflicts with authoritative"):
        parse_shot_plan(
            json.dumps(conflicting),
            expected_source_script_revision_id=SCRIPT_REVISION_ID,
        )


def test_manifest_duration_plan_overrides_only_generated_execution_timing(tmp_path):
    paths = DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(
        title="Duration authority",
        target_duration_seconds=60,
    )
    story_service = StoryService(repository)
    story = story_service.approve_revision(
        story_service.save_draft(project.id, valid_bible())["id"]
    )
    script_service = ScriptService(repository)
    script = script_service.approve_revision(
        script_service.create_manual_script(project, story)["id"]
    )
    settings = SettingsModelService(
        repository,
        manifest_registry=default_manifest_registry(include_placeholders=False),
        credential_store=_FakeCredentialStore(),
    )
    settings.save_selections(
        project_id=project.id,
        selections={
            CapabilityKind.VIDEO: MAINLAND_PRIMARY_MANIFEST_IDS[
                CapabilityKind.VIDEO
            ]
        },
    )
    duration_planner = DurationPlanningService(
        repository,
        settings_service=settings,
    )
    duration_plan = duration_planner.plan(project.id, 60)
    assert duration_plan.provider_id == "alibaba_model_studio"
    assert duration_plan.model_id == "wan2.7-i2v-2026-04-25"
    assert duration_plan.planned_shot_durations == (
        8.0,
        8.0,
        8.0,
        8.0,
        7.0,
        7.0,
        7.0,
        7.0,
    )

    semantic_proposal = ShotPlan(
        title="Eight semantic shots",
        source_script_revision_id=script["id"],
        shots=[
            Shot(
                id=f"shot_{order:03d}",
                order=order,
                scene_id="scene_001",
                source_script_beat_ids=["beat_001"],
                duration_seconds=3.25,
                composition=f"composition {order}",
                subject=["char_001"],
                action=f"semantic action {order}",
                visual_intent=f"visual intent {order}",
                risk_level="MEDIUM",
                risk_reasons=["KEY_STORY_BEAT"],
            )
            for order in range(1, 9)
        ],
    )
    semantic_before = semantic_proposal.model_dump(mode="json")
    gateway = _ValidatorGateway(semantic_before)
    revision = ShotService(
        repository,
        llm_gateway=gateway,
        duration_planner=duration_planner,
    ).generate_shot_plan(
        project,
        source_script_revision_id=script["id"],
    )

    assert gateway.calls == 1
    assert "product, not the model, owns provider execution timing" in gateway.prompt
    assert revision["source_script_revision_id"] == script["id"]
    assert revision["content"].source_script_revision_id == script["id"]
    assert tuple(shot.duration_seconds for shot in revision["content"].shots) == (
        duration_plan.planned_shot_durations
    )
    assert revision["content"].total_duration_seconds == 60

    semantic_after = revision["content"].model_dump(mode="json")
    for before, after in zip(
        semantic_before["shots"], semantic_after["shots"], strict=True
    ):
        before_without_duration = dict(before)
        after_without_duration = dict(after)
        before_without_duration.pop("duration_seconds")
        after_without_duration.pop("duration_seconds")
        assert after_without_duration == before_without_duration
    assert revision["generation_input"]["duration_provider_id"] == (
        duration_plan.provider_id
    )
    assert revision["generation_input"]["duration_model_id"] == duration_plan.model_id
    assert revision["generation_input"]["planned_shot_count"] == 8
    assert revision["generation_input"]["planned_shot_durations"] == [
        8.0,
        8.0,
        8.0,
        8.0,
        7.0,
        7.0,
        7.0,
        7.0,
    ]

    with pytest.raises(ShotServiceError, match="expected 8, got 7"):
        ShotService.apply_authoritative_duration_plan(
            semantic_proposal.model_copy(
                update={"shots": semantic_proposal.shots[:7]},
                deep=True,
            ),
            duration_plan,
        )
