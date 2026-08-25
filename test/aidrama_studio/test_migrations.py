from __future__ import annotations

import sqlite3

from aidrama_studio.storage.migrations import MIGRATIONS, apply_migrations, _migration_015_reference_asset_repair_completion


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
        "production_jobs",
        "production_shots",
        "production_attempts",
        "production_executions",
        "production_events",
        "production_artifacts",
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
        "production_jobs",
        "production_shots",
        "production_attempts",
        "production_executions",
        "production_events",
        "production_artifacts",
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

    assert apply_migrations(connection) == 1
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
