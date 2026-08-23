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
    assert [version for version, _ in MIGRATIONS] == [1, 2, 3, 4]
    assert apply_migrations(connection) == 4
    assert [row[0] for row in connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )] == [1, 2, 3, 4]
    tables = _tables(connection)
    assert {
        "projects",
        "story_bible_revisions",
        "structured_script_revisions",
        "shot_plan_revisions",
    } <= tables


def test_migrations_are_idempotent_and_do_not_duplicate_schema_records() -> None:
    connection = sqlite3.connect(":memory:")
    assert apply_migrations(connection) == 4
    assert apply_migrations(connection) == 0
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4
    # The canonical project table and all revision tables survive repeated startup.
    for table in (
        "projects",
        "story_bible_revisions",
        "structured_script_revisions",
        "shot_plan_revisions",
    ):
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()

