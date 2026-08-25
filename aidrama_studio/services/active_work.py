"""One fail-closed definition of project-local work that may still write files."""

from __future__ import annotations

import sqlite3


TERMINAL_PROVIDER_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def project_has_active_work(
    connection: sqlite3.Connection, project_id: str
) -> bool:
    """Return whether canonical durable state can still mutate project storage.

    The query is table-aware so the predicate remains safe for upgrade and
    diagnostics scenarios. Provider state is an allowlist: a newly introduced
    or NULL state blocks destructive actions until its lifecycle is reviewed.
    """

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    checks = (
        (
            "provider_tasks",
            "project_id=? AND (state IS NULL OR state NOT IN "
            "('SUCCEEDED','FAILED','CANCELLED'))",
        ),
        (
            "production_jobs",
            "project_id=? AND status IN ('QUEUED','RUNNING')",
        ),
        (
            "production_executions",
            "production_job_id IN (SELECT id FROM production_jobs "
            "WHERE project_id=?) AND status IN ('QUEUED','RUNNING')",
        ),
        (
            "production_shots",
            "production_job_id IN (SELECT id FROM production_jobs "
            "WHERE project_id=?) AND status='RUNNING'",
        ),
        (
            "production_attempts",
            "production_shot_id IN (SELECT id FROM production_shots WHERE "
            "production_job_id IN (SELECT id FROM production_jobs WHERE "
            "project_id=?)) AND status='STARTED'",
        ),
        (
            "production_qc_results",
            "project_id=? AND status IN ('QC_PENDING','QC_RUNNING')",
        ),
        (
            "final_assembly_render_attempts",
            "final_assembly_id IN (SELECT id FROM final_assemblies "
            "WHERE project_id=?) AND status IN ('PENDING','RUNNING')",
        ),
        (
            "post_render_attempts",
            "project_id=? AND status IN ('PENDING','RUNNING')",
        ),
    )
    for table, predicate in checks:
        if table not in tables:
            continue
        if connection.execute(
            f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1',
            (project_id,),
        ).fetchone():
            return True
    return False


__all__ = ["TERMINAL_PROVIDER_STATES", "project_has_active_work"]
