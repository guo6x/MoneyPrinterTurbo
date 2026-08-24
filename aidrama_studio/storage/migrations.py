from __future__ import annotations

import sqlite3
from collections.abc import Callable


Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def _migration_001_projects(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            aspect_ratio TEXT NOT NULL,
            target_duration_seconds INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX idx_projects_updated_at ON projects(updated_at DESC)"
    )
    connection.execute("CREATE INDEX idx_projects_status ON projects(status)")


def _migration_002_story_bible_revisions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE story_bible_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            content_json TEXT NOT NULL,
            generation_input_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, version),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_story_revisions_project ON story_bible_revisions(project_id, version DESC)")
    connection.execute("CREATE INDEX idx_story_revisions_status ON story_bible_revisions(project_id, status)")
    connection.execute("CREATE UNIQUE INDEX idx_story_one_approved ON story_bible_revisions(project_id) WHERE status = 'APPROVED'")


def _migration_003_structured_script_revisions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE structured_script_revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            source_story_revision_id TEXT NOT NULL,
            content_json TEXT NOT NULL,
            generation_input_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, version),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(source_story_revision_id) REFERENCES story_bible_revisions(id)
        )
        """
    )
    connection.execute("CREATE INDEX idx_script_revisions_project ON structured_script_revisions(project_id, version DESC)")
    connection.execute("CREATE INDEX idx_script_revisions_status ON structured_script_revisions(project_id, status)")
    connection.execute("CREATE UNIQUE INDEX idx_script_one_approved ON structured_script_revisions(project_id) WHERE status = 'APPROVED'")

def _migration_004_shot_plan_revisions(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE shot_plan_revisions (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL,
        status TEXT NOT NULL, source_script_revision_id TEXT NOT NULL,
        content_json TEXT NOT NULL, generation_input_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id, version),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY(source_script_revision_id) REFERENCES structured_script_revisions(id)
    )""")
    connection.execute("CREATE INDEX idx_shot_revisions_project ON shot_plan_revisions(project_id, version DESC)")
    connection.execute("CREATE INDEX idx_shot_revisions_status ON shot_plan_revisions(project_id, status)")
    connection.execute("CREATE UNIQUE INDEX idx_shot_one_approved ON shot_plan_revisions(project_id) WHERE status='APPROVED'")


def _migration_005_reference_assets(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE reference_assets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            current_version_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE reference_asset_versions (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(asset_id, version_number),
            FOREIGN KEY(asset_id) REFERENCES reference_assets(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE reference_asset_bindings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            asset_version_id TEXT NOT NULL,
            binding_type TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, asset_version_id, binding_type, binding_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(asset_version_id) REFERENCES reference_asset_versions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_reference_assets_project ON reference_assets(project_id, asset_type)")
    connection.execute("CREATE INDEX idx_reference_versions_asset ON reference_asset_versions(asset_id, version_number DESC)")
    connection.execute("CREATE INDEX idx_reference_versions_hash ON reference_asset_versions(project_id, sha256)")
    connection.execute("CREATE INDEX idx_reference_bindings_target ON reference_asset_bindings(project_id, binding_type, binding_id)")


def _migration_006_production_domain(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE production_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            shot_plan_revision_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('DRAFT', 'READY', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(shot_plan_revision_id) REFERENCES shot_plan_revisions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_shots (
            id TEXT PRIMARY KEY,
            production_job_id TEXT NOT NULL,
            shot_id TEXT NOT NULL,
            order_index INTEGER NOT NULL CHECK (order_index >= 0),
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
            created_at TEXT NOT NULL,
            UNIQUE(production_job_id, shot_id),
            UNIQUE(production_job_id, order_index),
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_attempts (
            id TEXT PRIMARY KEY,
            production_shot_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('STARTED', 'FAILED', 'SUCCEEDED', 'CANCELLED')),
            runtime_adapter TEXT NOT NULL,
            runtime_reference TEXT,
            input_snapshot_json TEXT NOT NULL,
            output_artifact_json TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(production_shot_id, attempt_number),
            FOREIGN KEY(production_shot_id) REFERENCES production_shots(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_production_jobs_project ON production_jobs(project_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_production_jobs_status ON production_jobs(project_id, status)")
    connection.execute("CREATE INDEX idx_production_shots_job ON production_shots(production_job_id, order_index)")
    connection.execute("CREATE INDEX idx_production_attempts_shot ON production_attempts(production_shot_id, attempt_number)")


MIGRATIONS: tuple[Migration, ...] = (
    (1, _migration_001_projects),
    (2, _migration_002_story_bible_revisions),
    (3, _migration_003_structured_script_revisions),
    (4, _migration_004_shot_plan_revisions),
    (5, _migration_005_reference_assets),
    (6, _migration_006_production_domain),
)


def apply_migrations(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    count = 0
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        with connection:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (version,),
            )
        count += 1
    return count
