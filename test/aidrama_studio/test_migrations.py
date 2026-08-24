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
