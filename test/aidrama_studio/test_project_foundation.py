from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aidrama_studio.domain import AspectRatio, ProjectStatus
from aidrama_studio.services import ProjectService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService
from aidrama_studio.storage.database import (
    DatabasePaths,
    initialize_database,
    migrate_legacy_data,
)
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
    assert expected_versions == sorted(set(expected_versions))
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
    assert [row[0] for row in after] == [version for version, _ in MIGRATIONS]
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
        aspect_ratio=AspectRatio.LANDSCAPE,
        target_duration_seconds=120,
    )

    assert service.get(project.id) == updated
    assert updated.title == "After"
    assert updated.status is ProjectStatus.DRAFT
    assert updated.aspect_ratio is AspectRatio.LANDSCAPE


@pytest.mark.parametrize(
    "requested_status",
    [ProjectStatus.STORY, ProjectStatus.PREPRODUCTION, ProjectStatus.PRODUCTION,
     ProjectStatus.REVIEW, ProjectStatus.POSTPRODUCTION, ProjectStatus.COMPLETED],
)
def test_workflow_stage_cannot_be_manually_mutated(
    service: ProjectService, requested_status: ProjectStatus
):
    project = service.create(title="Canonical stage")

    with pytest.raises(ValueError, match="canonical production state"):
        service.update(
            project.id,
            title=project.title,
            description=project.description,
            status=requested_status,
            aspect_ratio=project.aspect_ratio,
            target_duration_seconds=project.target_duration_seconds,
        )

    assert service.get(project.id).status is ProjectStatus.DRAFT


def test_legacy_migration_copies_real_database_revisions_and_files(tmp_path: Path):
    legacy_root = tmp_path / "legacy"
    target_root = tmp_path / "target"
    legacy = DatabasePaths(
        legacy_root / "aidrama.db",
        legacy_root / "projects",
        legacy_root / "archived_projects",
    )
    target = DatabasePaths(
        target_root / "aidrama.db",
        target_root / "projects",
        target_root / "archived_projects",
    )

    legacy_repository = ProjectRepository(legacy)
    project = ProjectService(legacy_repository).create(
        title="Filesystem legacy", description="preserve me"
    )
    story = StoryService(legacy_repository)
    story_revision = story.approve_revision(story.create_blank_draft(project)["id"])
    script = ScriptService(legacy_repository)
    script_revision = script.approve_revision(
        script.create_manual_script(project, story_revision)["id"]
    )
    shot = ShotService(legacy_repository)
    shot_revision = shot.approve_revision(
        shot.create_manual_shot_plan(project, script_revision)["id"]
    )
    physical_file = legacy_repository.project_directory(project.id) / "evidence.txt"
    physical_file.write_text("legacy bytes", encoding="utf-8")

    assert migrate_legacy_data(target, legacy=legacy) is True
    with sqlite3.connect(target.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated_repository = ProjectRepository(target)
    assert migrated_repository.get_project(project.id).description == "preserve me"
    assert migrated_repository.get_story_revision(story_revision["id"])["content"].title
    assert migrated_repository.get_script_revision(script_revision["id"])["id"] == script_revision["id"]
    assert migrated_repository.get_shot_revision(shot_revision["id"])["id"] == shot_revision["id"]
    assert (target.projects / project.id / "evidence.txt").read_text(encoding="utf-8") == "legacy bytes"
    assert physical_file.read_text(encoding="utf-8") == "legacy bytes"
    assert migrate_legacy_data(target, legacy=legacy) is False


def test_legacy_migration_quarantines_partial_target_and_retries(
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy"
    target_root = tmp_path / "target"
    legacy = DatabasePaths(legacy_root / "aidrama.db", legacy_root / "projects", legacy_root / "archived_projects")
    target = DatabasePaths(target_root / "aidrama.db", target_root / "projects", target_root / "archived_projects")
    legacy_repository = ProjectRepository(legacy)
    project = ProjectService(legacy_repository).create(title="Retry legacy")
    (legacy_repository.project_directory(project.id) / "asset.txt").write_text("asset", encoding="utf-8")
    target_root.mkdir(parents=True)
    sqlite3.connect(target.database).close()
    target.projects.mkdir(parents=True)
    (target.projects / "partial.txt").write_text("partial", encoding="utf-8")

    assert migrate_legacy_data(target, legacy=legacy) is True
    assert ProjectRepository(target).get_project(project.id) is not None
    assert any((target_root / "backups").glob("legacy-*/partial-aidrama.db"))
    assert any((target_root / "backups").glob("legacy-*/partial-projects"))


def test_legacy_migration_failure_leaves_legacy_untouched_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import aidrama_studio.storage.database as database_module

    legacy_root = tmp_path / "legacy"
    target_root = tmp_path / "target"
    legacy = DatabasePaths(legacy_root / "aidrama.db", legacy_root / "projects", legacy_root / "archived_projects")
    target = DatabasePaths(target_root / "aidrama.db", target_root / "projects", target_root / "archived_projects")
    legacy_repository = ProjectRepository(legacy)
    project = ProjectService(legacy_repository).create(title="Failure recovery")
    source_file = legacy_repository.project_directory(project.id) / "asset.txt"
    source_file.write_text("must survive", encoding="utf-8")
    original_copytree = database_module.shutil.copytree

    def fail_copytree(*args, **kwargs):
        raise OSError("simulated project copy failure")

    monkeypatch.setattr(database_module.shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="simulated project copy failure"):
        migrate_legacy_data(target, legacy=legacy)
    assert source_file.read_text(encoding="utf-8") == "must survive"
    assert not target.database.exists()
    monkeypatch.setattr(database_module.shutil, "copytree", original_copytree)
    assert migrate_legacy_data(target, legacy=legacy) is True
    assert ProjectRepository(target).get_project(project.id) is not None


def test_legacy_migration_does_not_overwrite_populated_target(tmp_path: Path):
    legacy_root = tmp_path / "legacy"
    target_root = tmp_path / "target"
    legacy = DatabasePaths(legacy_root / "aidrama.db", legacy_root / "projects", legacy_root / "archived_projects")
    target = DatabasePaths(target_root / "aidrama.db", target_root / "projects", target_root / "archived_projects")
    ProjectService(ProjectRepository(legacy)).create(title="Legacy")
    target_project = ProjectService(ProjectRepository(target)).create(title="Keep target")

    assert migrate_legacy_data(target, legacy=legacy) is False
    assert ProjectRepository(target).get_project(target_project.id).title == "Keep target"


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
