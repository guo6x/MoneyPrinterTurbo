from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aidrama_studio.domain.script import (
    ScriptBeatType,
    ScriptRevisionStatus,
    StructuredScript,
    TimeOfDay,
)
from aidrama_studio.services.project import ProjectService
from aidrama_studio.services.script_prompt import build_script_prompt
from aidrama_studio.services.story import StoryService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_story_bible import valid_bible


def _payload(
    time_of_day: str,
    *,
    beat_type: str = "ACTION",
) -> dict[str, object]:
    return {
        "title": "Alias normalization",
        "summary": "Canonical enum persistence",
        "scenes": [
            {
                "id": "scene_001",
                "order": 1,
                "title": "末班车",
                "location_id": "loc_001",
                "interior_exterior": "INT",
                "time_of_day": time_of_day,
                "character_ids": ["char_001"],
                "estimated_duration_seconds": 8,
                "beats": [
                    {
                        "id": "script_beat_001",
                        "order": 1,
                        "type": beat_type,
                        "character_id": "char_001",
                        "text": "站务员抬头看向末班车。",
                        "estimated_duration_seconds": 8,
                    }
                ],
                "source_story_beat_ids": ["beat_001"],
            }
        ],
    }


def test_exact_chinese_time_of_day_aliases_persist_as_canonical_enum(tmp_path):
    expected = {
        "深夜": TimeOfDay.NIGHT,
        "夜晚": TimeOfDay.NIGHT,
        "夜间": TimeOfDay.NIGHT,
        "晚上": TimeOfDay.NIGHT,
        "清晨": TimeOfDay.DAWN,
        "黎明": TimeOfDay.DAWN,
        "白天": TimeOfDay.DAY,
        "日间": TimeOfDay.DAY,
        "黄昏": TimeOfDay.DUSK,
        "傍晚": TimeOfDay.DUSK,
        "未指定": TimeOfDay.UNSPECIFIED,
        "不确定": TimeOfDay.UNSPECIFIED,
    }
    for alias, canonical in expected.items():
        assert StructuredScript.model_validate(_payload(alias)).scenes[0].time_of_day is canonical

    with pytest.raises(ValidationError):
        StructuredScript.model_validate(_payload("午夜之后"))

    paths = DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Time alias persistence")
    story_service = StoryService(repository)
    story = story_service.save_draft(project.id, valid_bible())
    story = story_service.approve_revision(story["id"])
    script = StructuredScript.model_validate(_payload("深夜"))
    script.validate_against(story["content"])
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    saved = repository.create_script_revision(
        revision_id="script_alias_001",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.DRAFT,
        source_story_revision_id=story["id"],
        content=script,
        generation_input={"normalization": "EXACT_ALIAS"},
        created_at=now,
        updated_at=now,
    )

    cold = ProjectRepository(paths).get_script_revision(saved["id"])
    assert cold is not None
    assert cold["content"].scenes[0].time_of_day is TimeOfDay.NIGHT
    assert cold["content"].model_dump(mode="json")["scenes"][0]["time_of_day"] == "NIGHT"


def test_script_generation_contract_separates_story_and_script_beat_types(tmp_path):
    paths = DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived",
    )
    project = ProjectService(ProjectRepository(paths)).create(title="Script contract")
    story = valid_bible()

    prompt = build_script_prompt(project, story)
    assert (
        'Every scenes[*].beats[*].type MUST be exactly one of '
        '["ACTION", "DIALOGUE", "NARRATION", "INNER_MONOLOGUE", "TRANSITION"]'
        in prompt
    )
    assert "StoryBeat.type is a narrative-role input" in prompt
    assert "NEVER copy those values into a ScriptBeat.type" in prompt

    for invalid_story_beat_type in ("OPENING", "DEVELOPMENT"):
        with pytest.raises(ValidationError):
            StructuredScript.model_validate(
                _payload("NIGHT", beat_type=invalid_story_beat_type)
            )

    for valid_script_beat_type in ScriptBeatType:
        script = StructuredScript.model_validate(
            _payload("NIGHT", beat_type=valid_script_beat_type.value)
        )
        script.validate_against(story)
        assert script.scenes[0].beats[0].type is valid_script_beat_type
