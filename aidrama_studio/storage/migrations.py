from __future__ import annotations

import hashlib
import json
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
