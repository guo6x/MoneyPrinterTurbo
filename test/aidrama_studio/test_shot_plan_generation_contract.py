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
    ShotPlan,
    ShotSize,
    StructuredScript,
)
from aidrama_studio.services.shot_parser import ShotPlanParseError, parse_shot_plan
from aidrama_studio.services.shot_prompt import _shot_plan_schema, build_shot_prompt
from test.aidrama_studio.test_story_bible import valid_bible


SCRIPT_REVISION_ID = "c249c8b78c97436f8b39a0aee9b50479"


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
