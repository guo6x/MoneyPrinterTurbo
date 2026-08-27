from __future__ import annotations

import sqlite3

import pytest

from aidrama_studio.domain import RuntimePlan
from aidrama_studio.storage.database import DatabasePaths, initialize_database
from aidrama_studio.storage.migrations import (
    MIGRATIONS,
    _migration_015_reference_asset_repair_completion,
    apply_migrations,
)
from aidrama_studio.storage.repositories import ProjectRepository


RUNTIME_PLAN_EXPECTED_COLUMNS = (
    "id",
    "project_id",
    "production_job_id",
    "execution_id",
    "output_profile_id",
    "generation_brief_id",
    "provider_capability",
    "provider_id",
    "model_id",
    "generation_mode",
    "resolution",
    "provider_generation_duration",
    "target_creative_duration",
    "audio_strategy",
    "provider_parameters_json",
    "reference_version_ids_json",
    "reference_roles_json",
    "continuity_strategy",
    "generation_brief_hash",
    "output_profile_hash",
    "authorization_json",
    "prompt_template_version",
    "plan_hash",
    "created_at",
    "endpoint_profile_id",
    "deployment_region",
    "endpoint_class",
    "credential_reference",
    "selection_source",
    "transmitted_content_types_json",
    "estimated_request_count",
    "native_generation_resolution",
    "native_generation_fps",
    "delivery_width",
    "delivery_height",
    "target_fps",
    "delivery_strategy",
    "quality_mode",
    "duration_strategy",
    "generation_override_sha256",
)

RUNTIME_PLAN_FORWARD_COLUMNS = RUNTIME_PLAN_EXPECTED_COLUMNS[24:]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_all_aidrama_migrations_apply_in_order_and_create_revision_tables() -> None:
    connection = sqlite3.connect(":memory:")
    versions = [version for version, _ in MIGRATIONS]
    assert versions == sorted(set(versions))
    assert apply_migrations(connection) == len(versions)
    assert [row[0] for row in connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )] == versions
    tables = _tables(connection)
    assert {
        "projects",
        "story_bible_revisions",
        "structured_script_revisions",
        "shot_plan_revisions",
        "reference_assets",
        "reference_asset_versions",
        "reference_asset_bindings",
        "reference_image_candidates",
        "reference_image_candidate_events",
        "production_jobs",
        "production_shots",
        "production_attempts",
        "production_executions",
        "production_events",
        "production_artifacts",
        "production_shot_source_decisions",
        "final_assemblies",
        "final_assembly_items",
        "final_assembly_render_attempts",
        "post_production_plans",
        "post_subtitle_tracks",
        "post_voice_tracks",
        "post_music_tracks",
        "post_render_attempts",
        "director_sessions",
        "director_goals",
        "director_decisions",
        "director_decision_events",
        "producer_recommendation_events",
        "provider_capability_profiles",
        "provider_tasks",
        "vision_frame_manifests",
        "vision_analysis_results",
        "continuity_snapshots",
        "continuity_issues",
        "continuity_repair_recommendations",
        "creative_pipeline_operations",
        "auto_orchestrator_runs",
        "auto_agent_events",
        "auto_paid_authorizations",
        "auto_paid_consumptions",
        "paid_budget_ledgers",
        "paid_create_reservations",
        "production_artifact_identities",
        "post_dialogue_plans",
        "post_voice_assignment_sets",
        "post_tts_tasks",
        "post_audio_timelines",
    } <= tables


def test_migrations_are_idempotent_and_do_not_duplicate_schema_records() -> None:
    connection = sqlite3.connect(":memory:")
    assert apply_migrations(connection) == len(MIGRATIONS)
    assert apply_migrations(connection) == 0
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)
    # The canonical project table and all revision tables survive repeated startup.
    for table in (
        "projects",
        "story_bible_revisions",
        "structured_script_revisions",
        "shot_plan_revisions",
        "reference_assets",
        "reference_asset_versions",
        "reference_asset_bindings",
        "reference_image_candidates",
        "reference_image_candidate_events",
        "production_jobs",
        "production_shots",
        "production_attempts",
        "production_executions",
        "production_events",
        "production_artifacts",
        "production_shot_source_decisions",
        "final_assemblies",
        "final_assembly_items",
        "final_assembly_render_attempts",
        "post_production_plans",
        "post_subtitle_tracks",
        "post_voice_tracks",
        "post_music_tracks",
        "post_render_attempts",
        "director_sessions",
        "director_goals",
        "director_decisions",
        "director_decision_events",
        "producer_recommendation_events",
        "provider_capability_profiles",
        "provider_tasks",
        "vision_frame_manifests",
        "vision_analysis_results",
        "continuity_snapshots",
        "continuity_issues",
        "continuity_repair_recommendations",
    ):
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()


def test_execution_snapshot_column_is_available_after_migration() -> None:
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(production_executions)")
    }
    assert "input_snapshot_json" in columns


def test_forward_migrations_add_director_events_and_post_source_pin() -> None:
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(post_production_plans)")}
    attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(post_render_attempts)")}
    assert "source_final_assembly_render_attempt_id" in plan_columns
    assert "source_final_assembly_render_attempt_id" in attempt_columns
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)


def test_migration_029_adds_candidate_and_current_shot_source_truth() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    prior = [(version, migration) for version, migration in MIGRATIONS if version < 29]
    for _, migration in prior:
        migration(connection)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-25") for version, _ in prior],
    )

    assert apply_migrations(connection) == len(MIGRATIONS) - len(prior)
    tables = _tables(connection)
    assert {
        "reference_image_candidates",
        "reference_image_candidate_events",
        "production_shot_source_decisions",
    } <= tables
    execution_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(production_executions)")
    }
    final_item_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(final_assembly_items)")
    }
    assert {
        "creative_retry_of_execution_id",
        "creative_rejection_review_id",
    } <= execution_columns
    assert "source_decision_id" in final_item_columns
    assert [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations WHERE version>=29 ORDER BY version"
        )
    ] == [version for version, _ in MIGRATIONS if version >= 29]
    assert apply_migrations(connection) == 0


def test_migration_030_adds_final_duration_control_in_order_and_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    assert 30 in {version for version, _ in MIGRATIONS}
    assert [version for version, _ in MIGRATIONS] == list(range(1, max(v for v, _ in MIGRATIONS) + 1))
    prior = [(version, migration) for version, migration in MIGRATIONS if version < 30]
    for _, migration in prior:
        migration(connection)
    connection.execute(
        "INSERT INTO final_assemblies("
        "id,project_id,production_job_id,status,created_at,updated_at) "
        "VALUES ('assembly-legacy','project-legacy','job-legacy','READY','now','now')"
    )
    connection.execute(
        "INSERT INTO final_assembly_items("
        "id,final_assembly_id,order_index,production_shot_id,"
        "production_execution_id,production_artifact_id,qc_result_id,review_id,"
        "source_path,created_at,source_duration_seconds) "
        "VALUES ('item-legacy','assembly-legacy',1,'shot-legacy','execution-legacy',"
        "'artifact-legacy','qc-legacy',NULL,'production/source.mp4','now',2.0)"
    )
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-25") for version, _ in prior],
    )
    before = {
        row[1]
        for row in connection.execute("PRAGMA table_info(final_assembly_items)")
    }
    assert "timeline_duration_seconds" not in before
    assert "duration_strategy" not in before

    assert apply_migrations(connection) == len(MIGRATIONS) - len(prior)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(final_assembly_items)")
    }
    assert {"timeline_duration_seconds", "duration_strategy"} <= columns
    legacy = connection.execute(
        "SELECT timeline_duration_seconds,duration_strategy "
        "FROM final_assembly_items WHERE id='item-legacy'"
    ).fetchone()
    assert legacy["timeline_duration_seconds"] is None
    assert legacy["duration_strategy"] is None
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE final_assembly_items SET timeline_duration_seconds=-1 "
            "WHERE id='item-legacy'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE final_assembly_items SET duration_strategy='INVALID' "
            "WHERE id='item-legacy'"
        )
    connection.execute(
        "UPDATE final_assembly_items SET timeline_duration_seconds=2.0,"
        "duration_strategy='SOURCE_SHORTFALL' WHERE id='item-legacy'"
    )
    assert [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations WHERE version>=30 ORDER BY version"
        )
    ] == [version for version, _ in MIGRATIONS if version >= 30]
    assert apply_migrations(connection) == 0
    assert {
        row[1]
        for row in connection.execute("PRAGMA table_info(final_assembly_items)")
    } == columns


def test_migration_033_preserves_continuity_schema_and_accepts_recorded_version() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    assert apply_migrations(connection) == len(MIGRATIONS)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == max(version for version, _ in MIGRATIONS)
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'continuity_%' ORDER BY name"
        )
    } == {
        "continuity_snapshots",
        "continuity_issues",
        "continuity_repair_recommendations",
    }
    assert apply_migrations(connection) == 0


def _insert_legacy_runtime_plan(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO projects("
        "id,title,description,status,aspect_ratio,target_duration_seconds,"
        "created_at,updated_at) VALUES "
        "('project-runtime-plan','Legacy','','PREPRODUCTION','9:16',5,'now','now')"
    )
    connection.execute(
        "INSERT INTO runtime_plans("
        "id,project_id,provider_capability,provider_id,model_id,generation_mode,"
        "resolution,provider_generation_duration,target_creative_duration,"
        "audio_strategy,provider_parameters_json,reference_version_ids_json,"
        "reference_roles_json,continuity_strategy,generation_brief_hash,"
        "output_profile_hash,authorization_json,prompt_template_version,"
        "plan_hash,created_at) VALUES ("
        "'legacy-plan','project-runtime-plan','VIDEO_GENERATIVE','WAN_VIDEO',"
        "'wan-model','REFERENCE_I2V','720x1280',5,5,'EXTERNAL_TTS','{}','[]',"
        "'{}','REFERENCE_ONLY',?,?,?,?,?,'now')",
        ("1" * 64, "2" * 64, "{}", "wan-v1", "3" * 64),
    )


def _database_recorded_through_030(connection: sqlite3.Connection) -> None:
    for _, migration in MIGRATIONS:
        if _ == 31:
            break
        migration(connection)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-25") for version in range(1, 31)],
    )


def test_migration_031_repairs_recorded_legacy_duration_column_and_preserves_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _database_recorded_through_030(connection)
    _insert_legacy_runtime_plan(connection)
    before = connection.execute(
        "SELECT id,provider_id,generation_brief_hash,plan_hash FROM runtime_plans"
    ).fetchall()
    connection.execute("ALTER TABLE runtime_plans DROP COLUMN duration_strategy")

    assert apply_migrations(connection) == len(MIGRATIONS) - 30
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(runtime_plans)")
    }
    assert columns == set(RUNTIME_PLAN_EXPECTED_COLUMNS)
    assert connection.execute(
        "SELECT duration_strategy FROM runtime_plans WHERE id='legacy-plan'"
    ).fetchone()[0] == "EXACT"
    after = connection.execute(
        "SELECT id,provider_id,generation_brief_hash,plan_hash FROM runtime_plans"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert apply_migrations(connection) == 0


def test_migration_031_repairs_complete_runtime_plan_forward_contract() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _database_recorded_through_030(connection)
    _insert_legacy_runtime_plan(connection)
    connection.execute("DROP INDEX idx_runtime_plans_endpoint")
    for column in reversed(RUNTIME_PLAN_FORWARD_COLUMNS):
        connection.execute(f"ALTER TABLE runtime_plans DROP COLUMN {column}")

    assert apply_migrations(connection) == len(MIGRATIONS) - 30
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(runtime_plans)")
    }
    assert columns == set(RUNTIME_PLAN_EXPECTED_COLUMNS)
    legacy = connection.execute(
        "SELECT id,provider_id,generation_brief_hash,plan_hash,"
        "endpoint_profile_id,deployment_region,selection_source,"
        "native_generation_resolution,duration_strategy "
        "FROM runtime_plans WHERE id='legacy-plan'"
    ).fetchone()
    assert tuple(legacy) == (
        "legacy-plan",
        "WAN_VIDEO",
        "1" * 64,
        "3" * 64,
        None,
        "UNSPECIFIED",
        "LEGACY",
        "1920x1080",
        "EXACT",
    )
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_runtime_plans_endpoint'"
    ).fetchone()


def _database_recorded_through_031(connection: sqlite3.Connection) -> None:
    for version, migration in MIGRATIONS:
        if version == 32:
            break
        migration(connection)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-27") for version in range(1, 32)],
    )


def test_migration_032_repairs_recorded_legacy_source_decision_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _database_recorded_through_031(connection)
    connection.execute(
        "INSERT INTO production_shot_source_decisions("
        "id,project_id,production_job_id,production_shot_id,sequence_number,"
        "decision_type,selection_kind,production_execution_id,"
        "production_artifact_id,qc_result_id,selected_by,notes,created_at) "
        "VALUES ('decision-legacy','project-legacy','job-legacy','shot-legacy',1,"
        "'SELECTED','FINAL_ACCEPTED','execution-legacy','artifact-legacy',"
        "'qc-legacy','user','accepted before schema repair','now')"
    )
    connection.execute(
        "ALTER TABLE production_shot_source_decisions DROP COLUMN selection_kind"
    )

    before = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(production_shot_source_decisions)"
        )
    }
    assert "selection_kind" not in before

    assert apply_migrations(connection) == len(MIGRATIONS) - 31
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(production_shot_source_decisions)"
        )
    }
    assert "selection_kind" in columns
    preserved = connection.execute(
        "SELECT id,production_artifact_id,selection_kind "
        "FROM production_shot_source_decisions WHERE id='decision-legacy'"
    ).fetchone()
    assert tuple(preserved) == (
        "decision-legacy",
        "artifact-legacy",
        "FINAL_ACCEPTED",
    )
    assert apply_migrations(connection) == 0


def test_fresh_initialize_is_idempotent_and_runtime_plan_insert_contract_works(
    tmp_path,
) -> None:
    paths = DatabasePaths(
        tmp_path / "db" / "aidrama.db",
        tmp_path / "db" / "projects",
        tmp_path / "db" / "archived_projects",
    )
    initialize_database(paths)
    initialize_database(paths)
    with sqlite3.connect(paths.database) as connection:
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(runtime_plans)")
        )
        assert columns == RUNTIME_PLAN_EXPECTED_COLUMNS
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == len(MIGRATIONS)
        connection.execute(
            "INSERT INTO projects("
            "id,title,description,status,aspect_ratio,target_duration_seconds,"
            "created_at,updated_at) VALUES "
            "('project-runtime-plan','Fresh','','PREPRODUCTION','9:16',5,'now','now')"
        )

    repository = ProjectRepository(paths)
    plan = RuntimePlan(
        id="runtime-plan-insert",
        project_id="project-runtime-plan",
        provider_capability="VIDEO_GENERATIVE",
        provider_id="WAN_VIDEO",
        model_id="wan-model",
        endpoint_profile_id="DASHSCOPE_CN_BEIJING_V1",
        deployment_region="MAINLAND_CHINA",
        endpoint_class="DASHSCOPE_CN",
        credential_reference="DASHSCOPE_API_KEY",
        selection_source="PROJECT_PROFILE",
        transmitted_content_types=("PROMPT", "REFERENCE_IMAGE"),
        estimated_request_count=1,
        generation_mode="REFERENCE_I2V",
        native_generation_resolution="720x1280",
        native_generation_fps=24,
        delivery_width=720,
        delivery_height=1280,
        target_fps=24,
        delivery_strategy="NATIVE",
        quality_mode="STANDARD",
        provider_generation_duration=5,
        target_creative_duration=5,
        duration_strategy="EXACT",
        audio_strategy="EXTERNAL_TTS",
        provider_parameters={"provider_resolution": "720P"},
        reference_version_ids=("version-1",),
        reference_roles={"version-1": "first_frame"},
        continuity_strategy="REFERENCE_ONLY",
        generation_brief_hash="1" * 64,
        output_profile_hash="2" * 64,
        authorization={"approved": True, "max_paid_attempts": 1},
        prompt_template_version="mainland-wan-i2v-v1",
        plan_hash="3" * 64,
        created_at="2026-08-27T00:00:00+00:00",
    )

    assert repository.create_runtime_plan(plan) == plan
    assert repository.get_runtime_plan(plan.id) == plan


def test_upgrade_from_023_adds_append_only_vision_provenance_and_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    prior = [(version, migration) for version, migration in MIGRATIONS if version < 24]
    for version, migration in prior:
        migration(connection)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-25") for version, _ in prior],
    )
    before = {
        row[1]
        for row in connection.execute("PRAGMA table_info(vision_analysis_results)")
    }
    assert "reference_version_ids_json" not in before

    assert apply_migrations(connection) == len(MIGRATIONS) - len(prior)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(vision_analysis_results)")
    }
    assert {
        "reference_version_ids_json",
        "prompt_template_sha256",
        "input_provenance_json",
        "provider_interaction_id",
    } <= columns
    assert [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ] == [version for version, _ in MIGRATIONS]
    assert apply_migrations(connection) == 0


def test_migration_025_adds_regional_provider_selection_and_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    prior = [(version, migration) for version, migration in MIGRATIONS if version < 25]
    for _, migration in prior:
        migration(connection)
    connection.execute(
        "INSERT INTO provider_capability_profiles("
        "id,project_id,capability,provider_id,model_id,profile_json,enabled,created_at,updated_at"
        ") VALUES ('legacy-profile',NULL,'VISION','legacy','legacy-model','{}',1,'now','now')"
    )
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-08-25") for version, _ in prior],
    )

    # Starting immediately before 025 applies every later migration in order;
    # this remains correct as append-only schema versions are added.
    assert apply_migrations(connection) == len(MIGRATIONS) - len(prior)
    profile_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(provider_capability_profiles)")
    }
    runtime_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(runtime_plans)")
    }
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "endpoint_profile_id",
        "deployment_region",
        "endpoint_class",
        "credential_reference",
        "verification_state",
        "verified_at",
        "selection_priority",
    } <= profile_columns
    assert {
        "endpoint_profile_id",
        "deployment_region",
        "endpoint_class",
        "credential_reference",
        "selection_source",
        "transmitted_content_types_json",
        "estimated_request_count",
    } <= runtime_columns
    assert "provider_selection_settings" in tables
    assert [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations WHERE version>=25 ORDER BY version"
        )
    ] == [version for version, _ in MIGRATIONS if version >= 25]
    legacy = connection.execute(
        "SELECT endpoint_profile_id,deployment_region,endpoint_class FROM provider_capability_profiles WHERE id='legacy-profile'"
    ).fetchone()
    assert tuple(legacy) == ("LEGACY", "UNSPECIFIED", "UNSPECIFIED")
    assert apply_migrations(connection) == 0


def _legacy_reference_schema(connection: sqlite3.Connection) -> None:
    """Build the pre-canonical reference tables used by migration 015."""
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, status TEXT NOT NULL, aspect_ratio TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("INSERT INTO projects VALUES ('p1','Legacy',NULL,'DRAFT','16:9','2026-01-01','2026-01-01')")
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.executemany("INSERT INTO schema_migrations VALUES (?,?)", [(version, "2026-01-01") for version in range(1, 15)])
    connection.execute("CREATE TABLE reference_asset_sets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, version INTEGER NOT NULL, source_story_revision_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE reference_asset_images (id TEXT PRIMARY KEY, asset_set_id TEXT NOT NULL, sha256 TEXT, relative_path TEXT, original_filename TEXT, media_type TEXT, size_bytes INTEGER, created_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE reference_assets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_type TEXT NOT NULL, current_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE reference_asset_versions (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, project_id TEXT NOT NULL, version_number INTEGER NOT NULL, filename TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_path TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(asset_id, version_number))")
    connection.execute("CREATE TABLE reference_asset_bindings (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_version_id TEXT NOT NULL, binding_type TEXT NOT NULL, binding_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_id, asset_version_id, binding_type, binding_id))")


def test_migration_015_preserves_every_legacy_image_and_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _legacy_reference_schema(connection)
    connection.executemany(
        "INSERT INTO reference_asset_sets VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("set-zero", "p1", "CHARACTER", "char-1", 1, "story-1", "DRAFT", "2026-01-01", "2026-01-01"),
            ("set-locked", "p1", "CHARACTER", "char-1", 2, "story-1", "LOCKED", "2026-01-02", "2026-01-02"),
            ("set-location", "p1", "LOCATION", "loc-1", 1, "story-1", "DRAFT", "2026-01-01", "2026-01-01"),
        ],
    )
    connection.executemany(
        "INSERT INTO reference_asset_images VALUES (?,?,?,?,?,?,?,?)",
        [
            ("img-a", "set-locked", "a" * 64, "references/a.png", "a.png", "image/png", 10, "2026-01-02"),
            ("img-b", "set-locked", "bad-hash", "../escape.png", "b.png", "image/png", 20, "2026-01-02"),
            ("img-c", "set-location", "c" * 64, "references/c.png", "c.png", "image/png", 30, "2026-01-01"),
        ],
    )
    _migration_015_reference_asset_repair_completion(connection)
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_versions").fetchone()[0] == 3
    paths = [row[0] for row in connection.execute("SELECT storage_path FROM reference_asset_versions ORDER BY id")]
    assert any("legacy/reference/legacy-v2-" in path for path in paths)
    current = connection.execute("SELECT current_version_id FROM reference_assets WHERE project_id='p1' AND asset_type='CHARACTER_REFERENCE'").fetchone()[0]
    assert current.startswith("legacy-v2-") or current in {"img-a", "img-b"}
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_bindings").fetchone()[0] == 3
    before = connection.execute("SELECT COUNT(*) FROM reference_asset_versions").fetchone()[0]
    _migration_015_reference_asset_repair_completion(connection)
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_versions").fetchone()[0] == before


def test_upgrade_from_database_already_marked_014_runs_forward_repair() -> None:
    """A real 014 database receives 015+ without rewriting applied history."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for version, migration in MIGRATIONS[:14]:
        migration(connection)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?,?)",
        [(version, "2026-01-01") for version in range(1, 15)],
    )
    connection.execute(
        "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)",
        ("p014", "Legacy 014", None, "DRAFT", "16:9", 60, "2026-01-01", "2026-01-01"),
    )
    connection.execute(
        "CREATE TABLE reference_asset_sets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, version INTEGER NOT NULL, source_story_revision_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE reference_asset_images (id TEXT PRIMARY KEY, asset_set_id TEXT NOT NULL, sha256 TEXT, relative_path TEXT, original_filename TEXT, media_type TEXT, size_bytes INTEGER, created_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO reference_asset_sets VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("s1", "p014", "CHARACTER", "char-1", 1, None, "DRAFT", "2026-01-01", "2026-01-01"),
            ("s2", "p014", "CHARACTER", "char-1", 2, None, "LOCKED", "2026-01-02", "2026-01-02"),
        ],
    )
    connection.executemany(
        "INSERT INTO reference_asset_images VALUES (?,?,?,?,?,?,?,?)",
        [
            ("i1", "s2", "1" * 64, "references/one.png", "one.png", "image/png", 10, "2026-01-02"),
            ("i2", "s2", "2" * 64, "references/two.png", "two.png", "image/png", 11, "2026-01-02"),
        ],
    )

    assert apply_migrations(connection) == len(MIGRATIONS) - 14
    assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [
        version for version, _ in MIGRATIONS
    ]
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_versions").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_bindings").fetchone()[0] == 2
    assert connection.execute("SELECT current_version_id FROM reference_assets WHERE project_id='p014'").fetchone()[0] is not None
    assert apply_migrations(connection) == 0
    assert connection.execute("SELECT COUNT(*) FROM reference_asset_versions").fetchone()[0] == 2
