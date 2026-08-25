from __future__ import annotations

import sqlite3

from aidrama_studio.domain import AspectRatio, ProjectStatus
from aidrama_studio.services import ProductionService, ProjectService
from aidrama_studio.services.runtime_foundation import OutputProfileService
from aidrama_studio.storage.migrations import MIGRATIONS
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)
from test.aidrama_studio.test_production_queue import _authorization, _queue


def test_project_creation_freezes_user_delivery_intent(tmp_path):
    repository, _ = _execution_context.__wrapped__(tmp_path)
    project = ProjectService(repository).create(
        title="竖屏 4K 项目",
        aspect_ratio=AspectRatio.PORTRAIT,
        target_duration_seconds=120,
        delivery_resolution_label="4K",
        target_fps=30,
        quality_mode="HIGH",
    )

    profile = OutputProfileService(repository).current(project.id)

    assert profile is not None
    assert profile.version_number == 1
    assert profile.is_project_default is True
    assert profile.target_episode_duration_seconds == 120
    assert (profile.delivery_width, profile.delivery_height) == (2160, 3840)
    assert profile.delivery_resolution_label == "4K"
    assert profile.target_fps == 30
    assert profile.quality_mode == "HIGH"
    assert profile.model_dump()["target_episode_duration_seconds"] == 120
    assert "target_resolution" not in profile.model_dump()


def test_output_setting_edit_creates_new_version_without_rewriting_history(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    projects = ProjectService(repository)
    first = OutputProfileService(repository).current(project.id)

    projects.update(
        project.id,
        title=project.title,
        description=project.description,
        status=ProjectStatus.DRAFT,
        aspect_ratio=AspectRatio.PORTRAIT,
        target_duration_seconds=120,
        delivery_resolution_label="4K",
        target_fps=60,
        quality_mode="FINAL",
    )

    profiles = repository.list_output_profiles(project.id)
    current = repository.get_current_output_profile(project.id)
    assert [item.version_number for item in profiles] == [1, 2]
    assert repository.get_output_profile(first.id).is_project_default is False
    assert current.id != first.id
    assert current.target_resolution == "2160x3840"
    assert current.target_fps == 60
    assert current.quality_mode == "FINAL"
    assert repository.get_output_profile(first.id).target_resolution == "1080x1920"


def test_production_jobs_pin_exact_profile_versions(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    first_job = _ready_job(repository, project)
    first_profile = repository.get_output_profile(first_job.output_profile_id)

    ProjectService(repository).update(
        project.id,
        title=project.title,
        description=project.description,
        status=project.status,
        aspect_ratio=project.aspect_ratio,
        target_duration_seconds=project.target_duration_seconds,
        delivery_resolution_label="4K",
        target_fps=30,
        quality_mode="HIGH",
    )
    second_job = ProductionService(repository).create_production_job(
        project.id, "shot_001"
    )
    second_profile = repository.get_output_profile(second_job.output_profile_id)

    assert first_job.output_profile_id == first_profile.id
    assert repository.get_production_job(first_job.id).output_profile_id == first_profile.id
    assert second_job.output_profile_id != first_job.output_profile_id
    assert (first_profile.delivery_width, first_profile.delivery_height) == (1080, 1920)
    assert (second_profile.delivery_width, second_profile.delivery_height) == (2160, 3840)


def test_migration_026_records_canonical_columns_and_is_idempotent(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    before = repository.get_current_output_profile(project.id)

    # Re-opening the repository applies no duplicate migration or profile.
    reopened = type(repository)(repository.paths)
    after = reopened.get_current_output_profile(project.id)
    with sqlite3.connect(repository.paths.database) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        profile_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(output_profiles)")
        }
        runtime_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runtime_plans)")
        }

    assert versions == [version for version, _ in MIGRATIONS]
    assert versions[-1] == 26
    assert before == after
    assert {
        "version_number", "is_project_default", "delivery_width",
        "delivery_height", "delivery_resolution_label", "target_fps",
        "quality_mode",
    } <= profile_columns
    assert {
        "native_generation_resolution", "native_generation_fps",
        "delivery_width", "delivery_height", "target_fps",
        "delivery_strategy", "quality_mode", "duration_strategy",
    } <= runtime_columns


def test_paid_preview_and_runtime_plan_keep_native_and_delivery_truth_separate(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    _ready_job(repository, project)
    ProjectService(repository).update(
        project.id,
        title=project.title,
        description=project.description,
        status=project.status,
        aspect_ratio=project.aspect_ratio,
        target_duration_seconds=120,
        delivery_resolution_label="4K",
        target_fps=60,
        quality_mode="FINAL",
    )
    job = ProductionService(repository).create_production_job(project.id, "shot_001")
    queue = _queue(repository)

    preview = queue.preview_authorization(project.id, job.id)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(queue, project.id, job.id),
    )
    runtime_plan = repository.get_runtime_plan(
        task.request_summary["runtime_plan_ids_by_shot"]["shot_001"]
    )

    assert preview.target_episode_duration_seconds == 120
    assert preview.native_generation_resolution == "1080x1920"
    assert preview.delivery_resolution == "2160x3840"
    assert preview.native_generation_fps == 24
    assert preview.target_fps == 60
    assert preview.delivery_strategy == "DETERMINISTIC_UPSCALE"
    assert preview.quality_mode == "FINAL"
    assert runtime_plan.native_generation_resolution == "1080x1920"
    assert (runtime_plan.delivery_width, runtime_plan.delivery_height) == (2160, 3840)
    assert runtime_plan.delivery_strategy == "DETERMINISTIC_UPSCALE"
    assert runtime_plan.target_fps == 60
    assert runtime_plan.quality_mode == "FINAL"
