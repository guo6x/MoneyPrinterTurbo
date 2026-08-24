from __future__ import annotations

import sqlite3

from aidrama_studio.storage.migrations import MIGRATIONS, apply_migrations


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
