from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable


Migration = tuple[int, Callable[[sqlite3.Connection], None]]


class UnsupportedDatabaseSchemaError(RuntimeError):
    """An older application must not mutate a newer database schema."""


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


def _migration_007_production_execution_queue(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE production_executions (
            id TEXT PRIMARY KEY,
            production_job_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
            worker_type TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_events (
            id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('QUEUED', 'STARTED', 'PROGRESS', 'SHOT_COMPLETED', 'FAILED', 'CANCELLED', 'FINISHED')),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_artifacts (
            id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            path TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_production_executions_job ON production_executions(production_job_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_production_executions_status ON production_executions(status, created_at)")
    connection.execute("CREATE INDEX idx_production_events_execution ON production_events(execution_id, created_at, id)")
    connection.execute("CREATE INDEX idx_production_artifacts_execution ON production_artifacts(execution_id, created_at, id)")


def _migration_008_production_input_snapshots(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE production_executions ADD COLUMN input_snapshot_json TEXT")


def _migration_009_production_qc(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE production_qc_results (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            artifact_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('QC_PENDING', 'QC_RUNNING', 'QC_PASS', 'QC_FAILED')),
            report_path TEXT,
            summary_json TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE CASCADE,
            FOREIGN KEY(artifact_id) REFERENCES production_artifacts(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_qc_metrics (
            id TEXT PRIMARY KEY,
            result_id TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'SKIPPED')),
            value_json TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(result_id) REFERENCES production_qc_results(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE production_reviews (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            qc_result_id TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('PENDING', 'APPROVED', 'REJECTED')),
            reviewer TEXT NOT NULL,
            notes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(qc_result_id) REFERENCES production_qc_results(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_production_qc_results_execution ON production_qc_results(execution_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_production_qc_results_status ON production_qc_results(project_id, status, created_at DESC)")
    connection.execute("CREATE INDEX idx_production_qc_metrics_result ON production_qc_metrics(result_id, created_at, id)")
    connection.execute("CREATE INDEX idx_production_reviews_result ON production_reviews(qc_result_id, created_at DESC)")


def _migration_010_final_assembly_manifest(connection: sqlite3.Connection) -> None:
    """Persist only the immutable identities selected for final assembly."""
    connection.execute(
        """
        CREATE TABLE final_assemblies (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            production_job_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('DRAFT', 'READY', 'ASSEMBLING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE final_assembly_items (
            id TEXT PRIMARY KEY,
            final_assembly_id TEXT NOT NULL,
            order_index INTEGER NOT NULL CHECK (order_index >= 0),
            production_shot_id TEXT NOT NULL,
            production_execution_id TEXT NOT NULL,
            production_artifact_id TEXT NOT NULL,
            qc_result_id TEXT NOT NULL,
            review_id TEXT,
            source_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(final_assembly_id, order_index),
            UNIQUE(final_assembly_id, production_shot_id),
            FOREIGN KEY(final_assembly_id) REFERENCES final_assemblies(id) ON DELETE CASCADE,
            FOREIGN KEY(production_shot_id) REFERENCES production_shots(id) ON DELETE RESTRICT,
            FOREIGN KEY(production_execution_id) REFERENCES production_executions(id) ON DELETE RESTRICT,
            FOREIGN KEY(production_artifact_id) REFERENCES production_artifacts(id) ON DELETE RESTRICT,
            FOREIGN KEY(qc_result_id) REFERENCES production_qc_results(id) ON DELETE RESTRICT,
            FOREIGN KEY(review_id) REFERENCES production_reviews(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX idx_final_assemblies_project ON final_assemblies(project_id, created_at DESC, id)")
    connection.execute("CREATE INDEX idx_final_assemblies_job ON final_assemblies(production_job_id, created_at DESC, id)")
    connection.execute("CREATE INDEX idx_final_assembly_items_assembly ON final_assembly_items(final_assembly_id, order_index, id)")
    connection.execute("CREATE INDEX idx_final_assembly_items_sources ON final_assembly_items(production_shot_id, production_execution_id)")


def _migration_011_final_assembly_render_attempts(connection: sqlite3.Connection) -> None:
    """Keep fallible render history separate from the immutable manifest."""
    connection.execute(
        """
        CREATE TABLE final_assembly_render_attempts (
            id TEXT PRIMARY KEY,
            final_assembly_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
            adapter_name TEXT NOT NULL,
            output_relative_path TEXT,
            metadata_json TEXT NOT NULL,
            error_message TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(final_assembly_id, attempt_number),
            FOREIGN KEY(final_assembly_id) REFERENCES final_assemblies(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX idx_final_assembly_render_attempts_assembly "
        "ON final_assembly_render_attempts(final_assembly_id, attempt_number)"
    )


def _migration_012_post_production(connection: sqlite3.Connection) -> None:
    """Persist project-scoped post plans, tracks, and append-only renders."""
    connection.execute(
        """
        CREATE TABLE post_production_plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            source_final_assembly_id TEXT NOT NULL,
            subtitle_enabled INTEGER NOT NULL DEFAULT 1,
            audio_mix_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(source_final_assembly_id) REFERENCES final_assemblies(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE post_subtitle_tracks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_id TEXT,
            source_script_revision_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            cues_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(plan_id) REFERENCES post_production_plans(id) ON DELETE SET NULL,
            FOREIGN KEY(source_script_revision_id) REFERENCES structured_script_revisions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE post_voice_tracks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            path TEXT,
            voice_assignments_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(plan_id) REFERENCES post_production_plans(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE post_music_tracks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            path TEXT NOT NULL,
            start_seconds REAL NOT NULL DEFAULT 0,
            end_seconds REAL,
            gain REAL NOT NULL DEFAULT 1.0,
            loop INTEGER NOT NULL DEFAULT 0,
            fade_in_seconds REAL NOT NULL DEFAULT 0,
            fade_out_seconds REAL NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(plan_id) REFERENCES post_production_plans(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE post_render_attempts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            source_final_assembly_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
            adapter_name TEXT NOT NULL,
            output_relative_path TEXT,
            metadata_json TEXT NOT NULL,
            error_message TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(plan_id, attempt_number),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(plan_id) REFERENCES post_production_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(source_final_assembly_id) REFERENCES final_assemblies(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute("CREATE INDEX idx_post_plans_project ON post_production_plans(project_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_post_subtitles_plan ON post_subtitle_tracks(plan_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_post_voice_plan ON post_voice_tracks(plan_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_post_music_plan ON post_music_tracks(plan_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_post_attempts_plan ON post_render_attempts(plan_id, attempt_number)")


def _migration_013_director_state(connection: sqlite3.Connection) -> None:
    """Persist bounded Director goals and append-only decisions.

    This migration deliberately stores only Director control-plane state.  It
    references a project but does not duplicate Story/Script/Shot/Production
    truth; every run reconstructs its state from those canonical services.
    """
    connection.execute(
        """
        CREATE TABLE director_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'BLOCKED', 'COMPLETED', 'PAUSED')),
            current_goal TEXT NOT NULL,
            blocking_reason TEXT NOT NULL DEFAULT '',
            pending_recommendation_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE director_goals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED', 'BLOCKED', 'CANCELLED')),
            max_steps INTEGER NOT NULL CHECK (max_steps >= 1),
            completed_steps INTEGER NOT NULL DEFAULT 0 CHECK (completed_steps >= 0),
            created_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY(session_id) REFERENCES director_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE director_decisions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            goal_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('RECOMMENDED', 'APPROVED', 'COMPLETED', 'REJECTED')),
            project_state TEXT NOT NULL,
            recommendation_json TEXT NOT NULL,
            state_snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES director_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(goal_id) REFERENCES director_goals(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_director_sessions_project ON director_sessions(project_id, updated_at DESC)")
    connection.execute("CREATE INDEX idx_director_goals_session ON director_goals(session_id, created_at DESC)")
    connection.execute("CREATE INDEX idx_director_decisions_session ON director_decisions(session_id, created_at, id)")


def _migration_014_reference_asset_schema_repair(connection: sqlite3.Connection) -> None:
    """Repair databases created by the pre-005 reference-set prototype.

    Early development builds recorded migration 005 while using
    ``reference_asset_sets``/``reference_asset_images``.  A release candidate
    must not fail when that durable database is reopened, so the canonical
    asset tables are created idempotently and the legacy rows are projected
    into them.  Fresh databases simply execute the CREATE IF NOT EXISTS path.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reference_assets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_type TEXT NOT NULL,
            current_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reference_asset_versions (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, project_id TEXT NOT NULL,
            version_number INTEGER NOT NULL, filename TEXT NOT NULL, mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(asset_id, version_number),
            FOREIGN KEY(asset_id) REFERENCES reference_assets(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reference_asset_bindings (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_version_id TEXT NOT NULL,
            binding_type TEXT NOT NULL, binding_id TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(project_id, asset_version_id, binding_type, binding_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(asset_version_id) REFERENCES reference_asset_versions(id) ON DELETE CASCADE
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_assets_project ON reference_assets(project_id, asset_type)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_versions_asset ON reference_asset_versions(asset_id, version_number DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_versions_hash ON reference_asset_versions(project_id, sha256)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_bindings_target ON reference_asset_bindings(project_id, binding_type, binding_id)")

    legacy_sets = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reference_asset_sets'").fetchone()
    legacy_images = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reference_asset_images'").fetchone()
    if not legacy_sets:
        return
    rows = connection.execute("SELECT * FROM reference_asset_sets ORDER BY project_id, subject_type, subject_id, version, id").fetchall()
    grouped: dict[tuple[str, str, str], str] = {}
    for row in rows:
        key = (row["project_id"], row["subject_type"], row["subject_id"])
        asset_id = grouped.setdefault(key, "legacy-" + hashlib.sha256("|".join(key).encode()).hexdigest()[:32])
        asset_type = {
            "CHARACTER": "CHARACTER_REFERENCE", "LOCATION": "LOCATION_REFERENCE",
            "STYLE": "STYLE_REFERENCE", "PROP": "PROP_REFERENCE",
        }.get(str(row["subject_type"]).upper(), "PROP_REFERENCE")
        connection.execute(
            "INSERT OR IGNORE INTO reference_assets(id,project_id,asset_type,created_at,updated_at) VALUES (?,?,?,?,?)",
            (asset_id, row["project_id"], asset_type, row["created_at"], row["updated_at"]),
        )
        image = None
        if legacy_images:
            image = connection.execute("SELECT * FROM reference_asset_images WHERE asset_set_id=? ORDER BY id LIMIT 1", (row["id"],)).fetchone()
        if image is None:
            continue
        raw_hash = str(image["sha256"] or "").lower()
        sha256 = raw_hash if len(raw_hash) == 64 and all(char in "0123456789abcdef" for char in raw_hash) else hashlib.sha256(("legacy:" + str(image["id"])).encode()).hexdigest()
        storage_path = str(image["relative_path"] or "").replace("\\", "/").lstrip("/")
        if not storage_path or any(part in {"", ".", ".."} for part in storage_path.split("/")) or "://" in storage_path:
            storage_path = "legacy/reference/" + str(image["id"]) + ".bin"
        metadata = {
            "source_story_revision_id": row["source_story_revision_id"],
            "legacy_subject_type": row["subject_type"],
            "legacy_subject_id": row["subject_id"],
            "legacy_status": row["status"],
        }
        connection.execute(
            "INSERT OR IGNORE INTO reference_asset_versions(id,asset_id,project_id,version_number,filename,mime_type,size_bytes,sha256,storage_path,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(image["id"]), asset_id, row["project_id"], int(row["version"]), str(image["original_filename"] or "reference"), str(image["media_type"] or "application/octet-stream"), max(1, int(image["size_bytes"] or 1)), sha256, storage_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True), image["created_at"]),
        )
        if str(row["status"]).upper() == "LOCKED":
            connection.execute("UPDATE reference_assets SET current_version_id=?, updated_at=? WHERE id=?", (str(image["id"]), row["updated_at"], asset_id))
        binding_type = {"CHARACTER": "CHARACTER", "LOCATION": "LOCATION", "SHOT": "SHOT"}.get(str(row["subject_type"]).upper())
        if binding_type:
            binding_id = "legacy-binding-" + hashlib.sha256((asset_id + str(image["id"])).encode()).hexdigest()[:24]
            connection.execute("INSERT OR IGNORE INTO reference_asset_bindings(id,project_id,asset_version_id,binding_type,binding_id,created_at) VALUES (?,?,?,?,?,?)", (binding_id, row["project_id"], str(image["id"]), binding_type, row["subject_id"], image["created_at"]))


def _migration_015_reference_asset_repair_completion(connection: sqlite3.Connection) -> None:
    """Complete legacy reference repair without rewriting migration 014.

    Early reference-set databases can contain more than one image for a set.
    Migration 014 intentionally repaired the schema conservatively, but its
    first release projected only one image.  This forward migration walks all
    legacy rows and appends every recoverable image to the canonical immutable
    version history.  It is safe on fresh databases, databases already marked
    through 014, and repeated initialization because all identities and
    bindings are deterministic/``INSERT OR IGNORE``.
    """
    legacy_sets = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reference_asset_sets'"
    ).fetchone()
    if not legacy_sets:
        return
    legacy_images = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reference_asset_images'"
    ).fetchone()
    if not legacy_images:
        return

    set_rows = connection.execute(
        "SELECT * FROM reference_asset_sets ORDER BY project_id, subject_type, subject_id, version, id"
    ).fetchall()
    image_columns = {row[1] for row in connection.execute("PRAGMA table_info(reference_asset_images)")}
    set_column = next((name for name in ("asset_set_id", "reference_asset_set_id", "set_id") if name in image_columns), None)
    if set_column is None:
        return
    image_rows = connection.execute(
        f"SELECT * FROM reference_asset_images ORDER BY {set_column}, id"
    ).fetchall()
    images_by_set: dict[str, list[sqlite3.Row]] = {}
    for image in image_rows:
        images_by_set.setdefault(str(image[set_column]), []).append(image)

    def value(row: sqlite3.Row, name: str, default: object = None) -> object:
        return row[name] if name in row.keys() else default

    def safe_sha(image: sqlite3.Row, project_id: str) -> str:
        raw = str(value(image, "sha256", "") or "").lower()
        if len(raw) == 64 and all(char in "0123456789abcdef" for char in raw):
            return raw
        return hashlib.sha256(("legacy:" + project_id + ":" + str(value(image, "id", ""))).encode()).hexdigest()

    def safe_path(image: sqlite3.Row, version_id: str) -> str:
        raw = str(value(image, "relative_path", "") or "").replace("\\", "/").strip()
        parts = raw.split("/")
        if not raw or raw.startswith("/") or "://" in raw or any(part in {"", ".", ".."} for part in parts):
            return "legacy/reference/" + version_id + ".bin"
        return "/".join(parts)

    grouped: dict[tuple[str, str, str], str] = {}
    locked_candidates: dict[str, tuple[int, str, str]] = {}
    for set_row in set_rows:
        project_id = str(set_row["project_id"])
        subject_type = str(set_row["subject_type"])
        subject_id = str(set_row["subject_id"])
        key = (project_id, subject_type, subject_id)
        asset_id = grouped.setdefault(
            key, "legacy-" + hashlib.sha256("|".join(key).encode()).hexdigest()[:32]
        )
        asset_type = {
            "CHARACTER": "CHARACTER_REFERENCE", "LOCATION": "LOCATION_REFERENCE",
            "STYLE": "STYLE_REFERENCE", "PROP": "PROP_REFERENCE",
        }.get(subject_type.upper(), "PROP_REFERENCE")
        created_at = str(value(set_row, "created_at", "1970-01-01T00:00:00Z"))
        updated_at = str(value(set_row, "updated_at", created_at))
        connection.execute(
            "INSERT OR IGNORE INTO reference_assets(id,project_id,asset_type,created_at,updated_at) VALUES (?,?,?,?,?)",
            (asset_id, project_id, asset_type, created_at, updated_at),
        )
        existing_max = connection.execute(
            "SELECT COALESCE(MAX(version_number),0) FROM reference_asset_versions WHERE asset_id=?", (asset_id,)
        ).fetchone()[0]
        legacy_version = max(1, int(value(set_row, "version", 1) or 1))
        set_images = images_by_set.get(str(set_row["id"]), [])
        for image_index, image in enumerate(set_images):
            image_id = str(value(image, "id", ""))
            raw_existing = connection.execute(
                "SELECT id,asset_id,version_number FROM reference_asset_versions WHERE id=?", (image_id,)
            ).fetchone()
            version_id = image_id if raw_existing is not None and raw_existing[1] == asset_id else (
                "legacy-v2-" + hashlib.sha256((asset_id + "|" + image_id).encode()).hexdigest()[:32]
            )
            existing = connection.execute(
                "SELECT version_number FROM reference_asset_versions WHERE id=?", (version_id,)
            ).fetchone()
            if existing is None:
                existing_max += 1
                version_number = max(legacy_version + image_index, existing_max)
                metadata = {
                    "source_story_revision_id": value(set_row, "source_story_revision_id"),
                    "legacy_subject_type": subject_type,
                    "legacy_subject_id": subject_id,
                    "legacy_set_id": str(set_row["id"]),
                    "legacy_version": legacy_version,
                    "legacy_status": str(value(set_row, "status", "DRAFT")),
                    "legacy_image_id": image_id,
                }
                connection.execute(
                    "INSERT OR IGNORE INTO reference_asset_versions(id,asset_id,project_id,version_number,filename,mime_type,size_bytes,sha256,storage_path,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, asset_id, project_id, version_number,
                        str(value(image, "original_filename", "reference") or "reference"),
                        str(value(image, "media_type", "application/octet-stream") or "application/octet-stream"),
                        max(0, int(value(image, "size_bytes", 0) or 0)), safe_sha(image, project_id),
                        safe_path(image, version_id), json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        str(value(image, "created_at", created_at)),
                    ),
                )
            else:
                version_number = int(existing[0])
            binding_type = {"CHARACTER": "CHARACTER", "LOCATION": "LOCATION", "SHOT": "SHOT"}.get(subject_type.upper())
            if binding_type:
                binding_id = "legacy-binding-v2-" + hashlib.sha256((asset_id + "|" + version_id + "|" + subject_id).encode()).hexdigest()[:24]
                connection.execute(
                    "INSERT OR IGNORE INTO reference_asset_bindings(id,project_id,asset_version_id,binding_type,binding_id,created_at) VALUES (?,?,?,?,?,?)",
                    (binding_id, project_id, version_id, binding_type, subject_id, str(value(image, "created_at", created_at))),
                )
            if str(value(set_row, "status", "DRAFT")).upper() == "LOCKED":
                candidate = (legacy_version, str(set_row["id"]), version_id)
                current = locked_candidates.get(asset_id)
                if current is None or candidate[:2] >= current[:2]:
                    locked_candidates[asset_id] = candidate
    for asset_id, candidate in locked_candidates.items():
        connection.execute("UPDATE reference_assets SET current_version_id=?, updated_at=updated_at WHERE id=?", (candidate[2], asset_id))


def _migration_016_director_decision_events(connection: sqlite3.Connection) -> None:
    """Persist append-only Director decision lifecycle transitions."""
    connection.execute(
        """
        CREATE TABLE director_decision_events (
            id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            from_status TEXT NOT NULL CHECK (from_status IN ('RECOMMENDED', 'APPROVED', 'COMPLETED', 'REJECTED')),
            to_status TEXT NOT NULL CHECK (to_status IN ('RECOMMENDED', 'APPROVED', 'COMPLETED', 'REJECTED')),
            event_type TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(decision_id) REFERENCES director_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES director_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_director_decision_events_decision ON director_decision_events(decision_id, created_at, id)")
    connection.execute("CREATE INDEX idx_director_decision_events_project ON director_decision_events(project_id, created_at, id)")


def _migration_017_post_source_render_attempt(connection: sqlite3.Connection) -> None:
    """Pin the exact FinalAssembly render attempt used by post production."""
    plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(post_production_plans)")}
    if "source_final_assembly_render_attempt_id" not in plan_columns:
        connection.execute("ALTER TABLE post_production_plans ADD COLUMN source_final_assembly_render_attempt_id TEXT")
    attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(post_render_attempts)")}
    if "source_final_assembly_render_attempt_id" not in attempt_columns:
        connection.execute("ALTER TABLE post_render_attempts ADD COLUMN source_final_assembly_render_attempt_id TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_post_plans_source_attempt ON post_production_plans(source_final_assembly_render_attempt_id)")


def _migration_018_producer_recommendation_events(connection: sqlite3.Connection) -> None:
    """Persist Producer retry recommendations so read projections are bounded."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS producer_recommendation_events (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            production_job_id TEXT,
            action TEXT NOT NULL,
            target_id TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_producer_recommendation_events_scope ON producer_recommendation_events(project_id, production_job_id, action, created_at, id)")


def _migration_019_runtime_foundation(connection: sqlite3.Connection) -> None:
    """Pin output/runtime inputs and keep a non-secret AI invocation ledger."""
    job_columns = {row[1] for row in connection.execute("PRAGMA table_info(production_jobs)")}
    if "output_profile_id" not in job_columns:
        connection.execute("ALTER TABLE production_jobs ADD COLUMN output_profile_id TEXT")
    execution_columns = {row[1] for row in connection.execute("PRAGMA table_info(production_executions)")}
    if "runtime_plan_id" not in execution_columns:
        connection.execute("ALTER TABLE production_executions ADD COLUMN runtime_plan_id TEXT")
    if "generation_brief_id" not in execution_columns:
        connection.execute("ALTER TABLE production_executions ADD COLUMN generation_brief_id TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS output_profiles (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            aspect_ratio TEXT NOT NULL,
            target_duration_seconds REAL NOT NULL,
            target_resolution TEXT NOT NULL,
            fps REAL NOT NULL,
            video_codec_target TEXT NOT NULL,
            audio_sample_rate INTEGER NOT NULL,
            audio_channels INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_output_profiles_project ON output_profiles(project_id, created_at DESC, id)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_briefs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            production_job_id TEXT,
            shot_id TEXT NOT NULL,
            content_json TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_generation_briefs_scope ON generation_briefs(project_id, production_job_id, shot_id, created_at DESC)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            production_job_id TEXT,
            execution_id TEXT,
            output_profile_id TEXT,
            generation_brief_id TEXT,
            provider_capability TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            generation_mode TEXT NOT NULL,
            resolution TEXT NOT NULL,
            provider_generation_duration REAL NOT NULL,
            target_creative_duration REAL NOT NULL,
            audio_strategy TEXT NOT NULL,
            provider_parameters_json TEXT NOT NULL,
            reference_version_ids_json TEXT NOT NULL,
            reference_roles_json TEXT NOT NULL,
            continuity_strategy TEXT NOT NULL,
            generation_brief_hash TEXT NOT NULL,
            output_profile_hash TEXT NOT NULL,
            authorization_json TEXT NOT NULL,
            prompt_template_version TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE SET NULL,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE SET NULL,
            FOREIGN KEY(output_profile_id) REFERENCES output_profiles(id) ON DELETE SET NULL,
            FOREIGN KEY(generation_brief_id) REFERENCES generation_briefs(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_plans_hash ON runtime_plans(project_id, plan_hash)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_runtime_plans_execution ON runtime_plans(execution_id, created_at DESC)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_invocations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            production_job_id TEXT,
            execution_id TEXT,
            capability TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            input_source_ids_json TEXT NOT NULL,
            reference_version_ids_json TEXT NOT NULL,
            generation_brief_hash TEXT,
            runtime_plan_id TEXT,
            runtime_plan_hash TEXT,
            request_summary_json TEXT NOT NULL,
            provider_task_id TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            usage_json TEXT NOT NULL,
            estimated_cost REAL,
            actual_cost REAL,
            output_artifact_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(production_job_id) REFERENCES production_jobs(id) ON DELETE SET NULL,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE SET NULL,
            FOREIGN KEY(runtime_plan_id) REFERENCES runtime_plans(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_invocations_scope ON ai_invocations(project_id, created_at DESC, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_invocations_provider_task ON ai_invocations(provider_id, provider_task_id)")


def _migration_020_creative_intake(connection: sqlite3.Connection) -> None:
    """Project-scoped immutable source pack and normalized intake drafts."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_pack_items (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            display_filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            version_of_id TEXT,
            extraction_state TEXT NOT NULL,
            extracted_text TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(version_of_id) REFERENCES source_pack_items(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_source_pack_project ON source_pack_items(project_id, created_at, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_source_pack_hash ON source_pack_items(project_id, sha256)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS normalized_creative_briefs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('DRAFT','APPROVED','SUPERSEDED')),
            content_json TEXT NOT NULL,
            source_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_normalized_briefs_project ON normalized_creative_briefs(project_id, created_at DESC, id)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS intake_analyses (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            classifications_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            warnings_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(source_id) REFERENCES source_pack_items(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_intake_analyses_project ON intake_analyses(project_id, created_at, id)")


def _migration_021_reference_profiles(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_profiles (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            binding_type TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, binding_type, binding_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_profile_items (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            role TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(profile_id) REFERENCES reference_profiles(id) ON DELETE CASCADE,
            FOREIGN KEY(version_id) REFERENCES reference_asset_versions(id) ON DELETE RESTRICT,
            UNIQUE(profile_id, order_index),
            UNIQUE(profile_id, version_id, role)
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_profiles_scope ON reference_profiles(project_id, binding_type, binding_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reference_profile_items_order ON reference_profile_items(profile_id, order_index)")


def _migration_022_runtime_operations(connection: sqlite3.Connection) -> None:
    """Durable provider profiles, task idempotency and local vision evidence."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_capability_profiles (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            capability TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_provider_profiles_scope ON provider_capability_profiles(project_id, capability, enabled, updated_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            execution_id TEXT,
            capability TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            provider_task_id TEXT,
            state TEXT NOT NULL,
            request_summary_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            submitted_at TEXT,
            last_polled_at TEXT,
            next_poll_at TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, idempotency_key),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_provider_tasks_state ON provider_tasks(project_id, state, next_poll_at, updated_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_provider_tasks_provider_id ON provider_tasks(provider_id, provider_task_id)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vision_frame_manifests (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            artifact_id TEXT,
            frame_count INTEGER NOT NULL,
            samples_json TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE CASCADE,
            FOREIGN KEY(artifact_id) REFERENCES production_artifacts(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_vision_frame_manifests_scope ON vision_frame_manifests(project_id, execution_id, created_at DESC)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vision_analysis_results (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            artifact_id TEXT,
            frame_manifest_id TEXT,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            reference_comparison_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(execution_id) REFERENCES production_executions(id) ON DELETE CASCADE,
            FOREIGN KEY(artifact_id) REFERENCES production_artifacts(id) ON DELETE SET NULL,
            FOREIGN KEY(frame_manifest_id) REFERENCES vision_frame_manifests(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_vision_analysis_scope ON vision_analysis_results(project_id, execution_id, created_at DESC)")


def _migration_023_final_assembly_provenance(connection: sqlite3.Connection) -> None:
    """Freeze source hashes and deterministic timeline positions."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(final_assembly_items)")}
    additions = (
        ("source_sha256", "TEXT"),
        ("source_duration_seconds", "REAL"),
        ("timeline_start_seconds", "REAL"),
        ("timeline_end_seconds", "REAL"),
        ("trimmed_duration_seconds", "REAL"),
    )
    for name, kind in additions:
        if name not in columns:
            connection.execute(f"ALTER TABLE final_assembly_items ADD COLUMN {name} {kind}")
    assembly_columns = {row[1] for row in connection.execute("PRAGMA table_info(final_assemblies)")}
    if "output_profile_id" not in assembly_columns:
        connection.execute("ALTER TABLE final_assemblies ADD COLUMN output_profile_id TEXT")
    if "output_profile_hash" not in assembly_columns:
        connection.execute("ALTER TABLE final_assemblies ADD COLUMN output_profile_hash TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_final_assembly_items_timeline ON final_assembly_items(final_assembly_id, order_index)")


MIGRATIONS: tuple[Migration, ...] = (
    (1, _migration_001_projects),
    (2, _migration_002_story_bible_revisions),
    (3, _migration_003_structured_script_revisions),
    (4, _migration_004_shot_plan_revisions),
    (5, _migration_005_reference_assets),
    (6, _migration_006_production_domain),
    (7, _migration_007_production_execution_queue),
    (8, _migration_008_production_input_snapshots),
    (9, _migration_009_production_qc),
    (10, _migration_010_final_assembly_manifest),
    (11, _migration_011_final_assembly_render_attempts),
    (12, _migration_012_post_production),
    (13, _migration_013_director_state),
    (14, _migration_014_reference_asset_schema_repair),
    (15, _migration_015_reference_asset_repair_completion),
    (16, _migration_016_director_decision_events),
    (17, _migration_017_post_source_render_attempt),
    (18, _migration_018_producer_recommendation_events),
    (19, _migration_019_runtime_foundation),
    (20, _migration_020_creative_intake),
    (21, _migration_021_reference_profiles),
    (22, _migration_022_runtime_operations),
    (23, _migration_023_final_assembly_provenance),
)


def apply_migrations(connection: sqlite3.Connection) -> int:
    # The application connector uses sqlite3.Row, while migration unit tests
    # and older callers may pass a bare sqlite3.connect() tuple-row handle.
    # Legacy repair needs named columns; normalize the connection once without
    # changing tuple-style positional access (sqlite3.Row supports both).
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
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
    supported_max = max(version for version, _ in MIGRATIONS)
    unsupported = sorted(version for version in applied if version > supported_max)
    if unsupported:
        raise UnsupportedDatabaseSchemaError(
            f"数据库 schema version {unsupported[-1]} 高于当前应用支持的 {supported_max}"
        )
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
