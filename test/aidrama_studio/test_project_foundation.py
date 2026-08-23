from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aidrama_studio.domain import AspectRatio, ProjectStatus
from aidrama_studio.services import ProjectService
from aidrama_studio.storage.database import DatabasePaths, initialize_database
from aidrama_studio.storage.migrations import MIGRATIONS
from aidrama_studio.storage.repositories import ProjectRepository


@pytest.fixture
def paths(tmp_path: Path) -> DatabasePaths:
    return DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived_projects",
    )


@pytest.fixture
def service(paths: DatabasePaths) -> ProjectService:
    return ProjectService(ProjectRepository(paths))


def test_all_migrations_are_applied_in_order_and_recorded(paths: DatabasePaths):
    initialize_database(paths)

    with sqlite3.connect(paths.database) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        migration_rows = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    expected_versions = [version for version, _ in MIGRATIONS]
    assert expected_versions == [1, 2, 3, 4]
    assert versions == [(version,) for version in expected_versions]
    assert [row[0] for row in migration_rows] == expected_versions
    assert all(row[1] for row in migration_rows)
    assert {"projects", "story_bible_revisions", "structured_script_revisions", "shot_plan_revisions"} <= tables


def test_migrations_are_idempotent(paths: DatabasePaths):
    initialize_database(paths)
    with sqlite3.connect(paths.database) as connection:
        before = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    initialize_database(paths)

    with sqlite3.connect(paths.database) as connection:
        after = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert after == before
    assert [row[0] for row in after] == [1, 2, 3, 4]
    assert {"projects", "story_bible_revisions", "structured_script_revisions", "shot_plan_revisions"} <= tables


def test_project_model_validation_rejects_invalid_values(service: ProjectService):
    with pytest.raises(ValueError, match="项目名称不能为空"):
        service.create(title="   ")
    with pytest.raises(ValueError, match="目标时长"):
        service.create(title="Invalid", target_duration_seconds=0)


def test_create_get_and_project_directory(service: ProjectService):
    project = service.create(
        title="雾港来信",
        description="一封跨越十年的来信。",
        aspect_ratio=AspectRatio.PORTRAIT,
        target_duration_seconds=90,
    )

    loaded = service.get(project.id)

    assert loaded == project
    assert service.repository.project_directory(project.id).is_dir()


def test_list_projects_is_most_recent_first(service: ProjectService):
    first = service.create(title="First")
    second = service.create(title="Second")

    first = service.update(
        first.id,
        title=first.title,
        description=first.description,
        status=ProjectStatus.STORY,
        aspect_ratio=first.aspect_ratio,
        target_duration_seconds=first.target_duration_seconds,
    )

    listed = service.list()

    assert {item.id for item in listed} == {first.id, second.id}
    assert listed[0].updated_at >= listed[1].updated_at


def test_update_project_persists_fields(service: ProjectService):
    project = service.create(title="Before")

    updated = service.update(
        project.id,
        title="After",
        description="Updated description",
        status=ProjectStatus.PRODUCTION,
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=120,
    )

    assert service.get(project.id) == updated
    assert updated.title == "After"
    assert updated.status is ProjectStatus.PRODUCTION
    assert updated.aspect_ratio is AspectRatio.LANDSCAPE


def test_delete_requires_explicit_confirmation(service: ProjectService):
    project = service.create(title="Protected")

    with pytest.raises(ValueError, match="明确确认"):
        service.delete(project.id)

    assert service.get(project.id) is not None


def test_delete_removes_empty_artifact_directory(service: ProjectService):
    project = service.create(title="Empty artifacts")
    project_dir = service.repository.project_directory(project.id)

    result = service.delete(project.id, confirmed=True)

    assert result.deleted is True
    assert result.archived_artifacts_to is None
    assert service.get(project.id) is None
    assert not project_dir.exists()


def test_delete_archives_non_empty_artifacts(service: ProjectService):
    project = service.create(title="Keep artifacts")
    project_dir = service.repository.project_directory(project.id)
    (project_dir / "user-material.txt").write_text("keep me", encoding="utf-8")

    result = service.delete(project.id, confirmed=True)

    assert result.deleted is True
    assert result.archived_artifacts_to is not None
    assert (result.archived_artifacts_to / "user-material.txt").read_text(
        encoding="utf-8"
    ) == "keep me"
    assert service.get(project.id) is None
    assert not project_dir.exists()


def test_demo_project_is_explicitly_labeled(service: ProjectService):
    demo = service.create_demo()

    assert demo.title.startswith("DEMO ·")
