from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.services.dependency_status import DependencyStatusService
from aidrama_studio.services.project import ProjectService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.story import StoryService
from aidrama_studio.services.drafts import draft_is_dirty, draft_state
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.domain import StoryRevisionStatus, ScriptRevisionStatus


@pytest.fixture
def paths(tmp_path: Path) -> DatabasePaths:
    root = tmp_path / "aidrama"
    return DatabasePaths(root / "aidrama.db", root / "projects", root / "archived_projects")


@pytest.fixture
def services(paths: DatabasePaths):
    repository = ProjectRepository(paths)
    return (
        ProjectService(repository),
        StoryService(repository),
        ScriptService(repository),
        DependencyStatusService(repository),
    )


def test_dependency_projection_reports_source_current_and_affected_downstream(services):
    project_service, story_service, script_service, dependency_service = services
    project = project_service.create(title="Dependency project")

    old_story = story_service.create_blank_draft(project)
    approved_old_story = story_service.approve_revision(old_story["id"])
    old_script = script_service.create_manual_script(project, approved_old_story)
    script_service.approve_revision(old_script["id"])

    new_story = story_service.create_revision_from_approved(approved_old_story["id"])
    approved_new_story = story_service.approve_revision(new_story["id"])
    status = dependency_service.status_for_script(project.id, old_script["id"])

    assert status.outdated is True
    assert status.source_revision_id == approved_old_story["id"]
    assert status.current_revision_id == approved_new_story["id"]
    assert "v1" in status.source_to_current and "v2" in status.source_to_current
    assert status.repair_action
    assert dependency_service.project(project.id)["outdated"]


def test_dependency_projection_does_not_cross_project(paths):
    repository = ProjectRepository(paths)
    project_service = ProjectService(repository)
    story_service = StoryService(repository)
    dependency_service = DependencyStatusService(repository)
    first = project_service.create(title="first")
    second = project_service.create(title="second")
    revision = story_service.create_blank_draft(first)

    with pytest.raises(ValueError, match="不属于该项目"):
        dependency_service.status_for_story(second.id, revision)


def test_durable_draft_recovery_and_dirty_state(services):
    project_service, story_service, script_service, _ = services
    project = project_service.create(title="Draft project")
    revision = story_service.create_blank_draft(project)
    assert story_service.get_latest_draft(project.id)["id"] == revision["id"]
    recovered = story_service.recover_draft(project.id)
    assert recovered["id"] == revision["id"]
    assert not draft_is_dirty(revision, revision["content"])
    changed = revision["content"].model_copy(update={"title": "Edited"})
    assert draft_is_dirty(revision, changed)
    state = draft_state(revision, changed)
    assert state.dirty is True and state.recovery_available is True

    saved = story_service.save_draft(project.id, changed, revision_id=revision["id"])
    # A fresh service instance simulates a cold restart; canonical DB state is
    # the recovery source, not Streamlit session state.
    fresh_story_service = StoryService(story_service.repository)
    assert fresh_story_service.recover_draft(project.id)["content"].title == "Edited"
    assert saved["updated_at"]


def test_outdated_script_can_be_repaired_without_mutating_history(services):
    project_service, story_service, script_service, dependency_service = services
    project = project_service.create(title="Repair project")
    first = story_service.approve_revision(story_service.create_blank_draft(project)["id"])
    script = script_service.create_manual_script(project, first)
    script_service.approve_revision(script["id"])
    current_story = story_service.approve_revision(
        story_service.create_revision_from_approved(first["id"])["id"]
    )
    status = dependency_service.status_for_script(project.id, script["id"])
    repaired = script_service.create_revision_from_story(
        project.id, current_story["id"], content=script["content"]
    )
    assert status.outdated is True
    assert repaired["status"] is ScriptRevisionStatus.DRAFT
    assert repaired["source_story_revision_id"] == current_story["id"]
    assert script_service.get_revision(script["id"])["status"] is ScriptRevisionStatus.APPROVED
