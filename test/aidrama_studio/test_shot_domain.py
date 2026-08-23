from __future__ import annotations

import pytest

from aidrama_studio.domain.script import (
    Scene,
    ScriptBeat,
    ScriptBeatType,
    StructuredScript,
)
from aidrama_studio.domain.shot import (
    RiskLevel,
    Shot,
    ShotPlan,
    ShotSize,
)


def _script() -> StructuredScript:
    return StructuredScript(
        title="Test script",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Opening",
                location_id="loc_001",
                estimated_duration_seconds=5,
                beats=[
                    ScriptBeat(
                        id="beat_001",
                        order=1,
                        type=ScriptBeatType.ACTION,
                        text="A door opens",
                    )
                ],
            )
        ],
    )


def _shot(**kwargs) -> Shot:
    values = dict(
        id="shot_001",
        order=1,
        scene_id="scene_001",
        duration_seconds=2,
        visual_intent="Establish the room",
        shot_size=ShotSize.WIDE,
    )
    values.update(kwargs)
    return Shot(**values)


def test_shot_plan_enforces_unique_ids_orders_and_duration() -> None:
    plan = ShotPlan(
        title="Plan",
        source_script_revision_id="script-v1",
        shots=[_shot(source_script_beat_ids=["beat_001"]), _shot(id="shot_002", order=2, duration_seconds=3)],
    )
    assert plan.total_duration_seconds == 5
    with pytest.raises(ValueError, match="shot IDs"):
        ShotPlan(title="Plan", source_script_revision_id="script-v1", shots=[_shot(), _shot(id="shot_001", order=2)])
    with pytest.raises(ValueError, match="shot orders"):
        ShotPlan(title="Plan", source_script_revision_id="script-v1", shots=[_shot(), _shot(id="shot_002", order=1)])


def test_shot_plan_validates_scene_and_beat_traceability() -> None:
    plan = ShotPlan(title="Plan", source_script_revision_id="script-v1", shots=[_shot(source_script_beat_ids=["beat_001"])])
    assert plan.validate_against(_script()) is plan
    with pytest.raises(ValueError, match="unknown scene"):
        plan.model_copy(update={"shots": [_shot(scene_id="missing_scene")]}).validate_against(_script())
    with pytest.raises(ValueError, match="beat"):
        plan.model_copy(update={"shots": [_shot(source_script_beat_ids=["missing_beat"])]}).validate_against(_script())


def test_medium_and_high_risk_shots_require_reasons() -> None:
    with pytest.raises(ValueError, match="risk_reasons"):
        _shot(risk_level=RiskLevel.HIGH)
    shot = _shot(risk_level=RiskLevel.MEDIUM, risk_reasons=["Crowded blocking"])
    assert shot.risk_level is RiskLevel.MEDIUM
