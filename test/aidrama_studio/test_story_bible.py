from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from aidrama_studio.domain import (
    Character,
    Location,
    ProjectStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    World,
)
from aidrama_studio.services.story import StoryService, StoryServiceError, blank_story_bible
from aidrama_studio.services.story_parser import StoryBibleParseError, parse_story_bible
from aidrama_studio.storage.database import DatabasePaths, initialize_database
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.services.project import ProjectService


def valid_bible(title: str = "测试故事") -> StoryBible:
    return StoryBible(
        title=title,
        logline="一个人必须在最后一班车前做出选择。",
        premise="秘密和时间压力把两个陌生人拉到一起。",
        genre="悬疑",
        tone="克制",
        themes=["选择"],
        world=World(era="当代", setting="一座夜晚的城市", rules=["午夜后所有钟表会停走"], timeline_notes="一夜完成"),
        characters=[
            Character(id="char_001", name="林舟", role="主角", identity="夜班司机", motivation="找回真相"),
            Character(id="char_002", name="沈遥", role="来客", identity="神秘乘客", motivation="完成约定"),
        ],
        locations=[
            Location(id="loc_001", name="末班车", function="冲突发生地", environment="潮湿的车厢", time_of_day="深夜", visual_style="冷蓝色", key_props=["旧车票"]),
            Location(id="loc_002", name="终点站", function="揭示真相", environment="空旷站台", time_of_day="午夜", visual_style="白雾", key_props=[]),
        ],
        story_beats=[
            StoryBeat(id="beat_001", order=1, type="OPENING", summary="林舟接到最后一位乘客", characters=["char_001", "char_002"], location_id="loc_001", emotional_goal="不安"),
            StoryBeat(id="beat_002", order=2, type="TURNING_POINT", summary="乘客说出林舟忘记的名字", characters=["char_001", "char_002"], location_id="loc_001", emotional_goal="震动"),
            StoryBeat(id="beat_003", order=3, type="ENDING", summary="终点站留下唯一答案", characters=["char_001"], location_id="loc_002", emotional_goal="余韵"),
        ],
    )


@pytest.fixture
def paths(tmp_path: Path) -> DatabasePaths:
    return DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived_projects",
    )


@pytest.fixture
def project_service(paths: DatabasePaths) -> ProjectService:
    return ProjectService(ProjectRepository(paths))


@pytest.fixture
def story_service(paths: DatabasePaths) -> StoryService:
    return StoryService(ProjectRepository(paths))


def test_story_bible_domain_rejects_duplicate_ids_and_references():
    data = valid_bible().model_dump()
    data["characters"][1]["id"] = data["characters"][0]["id"]
    with pytest.raises(ValidationError, match="character IDs must be unique"):
        StoryBible.model_validate(data)

    data = valid_bible().model_dump()
    data["story_beats"][0]["characters"] = ["unknown"]
    with pytest.raises(ValidationError, match="unknown characters"):
        StoryBible.model_validate(data)

    data = valid_bible().model_dump()
    data["story_beats"][1]["location_id"] = "unknown"
    with pytest.raises(ValidationError, match="unknown location"):
        StoryBible.model_validate(data)

    data = valid_bible().model_dump()
    data["story_beats"][1]["order"] = data["story_beats"][0]["order"]
    with pytest.raises(ValidationError, match="story beat order"):
        StoryBible.model_validate(data)


def test_story_parser_accepts_json_fence_and_leading_prose():
    payload = json.dumps(valid_bible().model_dump(mode="json"), ensure_ascii=False)

    assert parse_story_bible(f"```json\n{payload}\n```").title == "测试故事"
    assert parse_story_bible(f"Here is the result:\n{payload}\nThanks").title == "测试故事"


def test_story_parser_rejects_malformed_and_invalid_schema():
    with pytest.raises(StoryBibleParseError, match="有效 JSON"):
        parse_story_bible("{not-json}")
    invalid = valid_bible().model_dump(mode="json")
    invalid["story_beats"] = []
    with pytest.raises(StoryBibleParseError, match="结构校验失败"):
        parse_story_bible(json.dumps(invalid))


def test_migration_002_creates_revision_table(paths: DatabasePaths):
    initialize_database(paths)
    with sqlite3.connect(paths.database) as connection:
        versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        table = connection.execute("SELECT name FROM sqlite_master WHERE name = 'story_bible_revisions'").fetchone()
        script_table = connection.execute("SELECT name FROM sqlite_master WHERE name = 'structured_script_revisions'").fetchone()
    assert versions == [(1,), (2,), (3,), (4,)]
    assert table == ("story_bible_revisions",)
    assert script_table == ("structured_script_revisions",)
    shot_table = connection.execute("SELECT name FROM sqlite_master WHERE name = 'shot_plan_revisions'").fetchone()
    assert shot_table == ("shot_plan_revisions",)


def test_manual_blank_draft_and_revision_persistence(project_service: ProjectService, story_service: StoryService):
    project = project_service.create(title="Manual Bible")
    revision = story_service.create_blank_draft(project)

    assert revision["version"] == 1
    assert revision["status"] is StoryRevisionStatus.DRAFT
    assert len(revision["content"].characters) == 1
    assert len(revision["content"].story_beats) == 3
    assert story_service.get_latest_revision(project.id)["id"] == revision["id"]


def test_generation_repair_uses_same_snapshot_and_persists_only_valid_content(project_service: ProjectService, paths: DatabasePaths):
    project = project_service.create(title="Generated Bible", description="Brief")
    responses = ["not json", json.dumps(valid_bible().model_dump(mode="json"), ensure_ascii=False)]
    snapshots = []

    def generate(_prompt, snapshot):
        snapshots.append(snapshot)
        return responses.pop(0)

    service = StoryService(
        ProjectRepository(paths),
        text_generator=generate,
        config_snapshot_provider=lambda: {"llm_provider": "mock", "api_key": "secret"},
    )
    revision = service.generate_story_bible(project, brief="一个关于选择的故事", genre="悬疑", tone="克制")

    assert revision["version"] == 1
    assert len(snapshots) == 2
    assert snapshots[0] is snapshots[1]
    stored = service.get_latest_revision(project.id)
    assert "secret" not in json.dumps(stored, default=str)
    assert stored["generation_input"]["brief"] == "一个关于选择的故事"


def test_repair_failure_preserves_previous_revision(project_service: ProjectService, paths: DatabasePaths):
    project = project_service.create(title="Preserve Bible")
    service = StoryService(ProjectRepository(paths))
    previous = service.create_blank_draft(project)
    calls = []

    def always_bad(prompt, snapshot):
        calls.append(prompt)
        return "still not json"

    failing = StoryService(
        ProjectRepository(paths),
        text_generator=always_bad,
        config_snapshot_provider=lambda: {},
    )
    with pytest.raises(StoryServiceError):
        failing.generate_story_bible(project, brief="bad", genre="悬疑", tone="紧张")
    assert len(calls) == 2
    assert service.get_latest_revision(project.id)["id"] == previous["id"]


def test_revision_increment_approval_supersede_and_approved_edit(project_service: ProjectService, story_service: StoryService):
    project = project_service.create(title="Revision Bible")
    first = story_service.create_blank_draft(project)
    approved = story_service.approve_revision(first["id"])
    assert approved["status"] is StoryRevisionStatus.APPROVED
    assert project_service.get(project.id).status is ProjectStatus.STORY

    second = story_service.create_revision_from_approved(first["id"])
    assert second["version"] == 2
    edited = second["content"].model_copy(update={"title": "Edited Bible"})
    saved = story_service.save_draft(project.id, edited, revision_id=approved["id"])
    assert saved["version"] == 3
    assert saved["status"] is StoryRevisionStatus.DRAFT
    approved_again = story_service.approve_revision(saved["id"])
    assert approved_again["status"] is StoryRevisionStatus.APPROVED
    revisions = story_service.list_revisions(project.id)
    assert sum(item["status"] is StoryRevisionStatus.APPROVED for item in revisions) == 1
    assert sum(item["status"] is StoryRevisionStatus.SUPERSEDED for item in revisions) == 1


def test_project_isolation(project_service: ProjectService, story_service: StoryService):
    first = project_service.create(title="First")
    second = project_service.create(title="Second")
    story_service.create_blank_draft(first)
    assert story_service.list_revisions(second.id) == []
