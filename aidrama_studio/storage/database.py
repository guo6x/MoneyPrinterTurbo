from __future__ import annotations

import sqlite3
import os
import shutil
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class DatabasePaths:
    database: Path
    projects: Path
    archived_projects: Path

    @property
    def root(self) -> Path:
        return self.database.parent


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_default_paths() -> DatabasePaths:
    override = os.environ.get("AIDRAMA_DATA_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        # Windows user data belongs outside Program Files and the source or
        # PyInstaller bundle.  Keep the repository-local path only as an
        # explicit development override (AIDRAMA_DATA_DIR).
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            root = (Path(local_app_data) / "AIDramaStudio").resolve()
        else:
            root = (Path.home() / ".local" / "share" / "AIDramaStudio").resolve()
    return DatabasePaths(
        database=root / "aidrama.db",
        projects=root / "projects",
        archived_projects=root / "archived_projects",
    )


def get_legacy_paths() -> DatabasePaths:
    root = repository_root() / "storage" / "aidrama"
    return DatabasePaths(
        database=root / "aidrama.db",
        projects=root / "projects",
        archived_projects=root / "archived_projects",
    )


def backup_database(source: Path, destination: Path) -> Path:
    """Create a consistent SQLite backup using SQLite's backup API."""
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    return destination


def migrate_legacy_data(
    target: DatabasePaths | None = None,
    *,
    legacy: DatabasePaths | None = None,
) -> bool:
    """Safely copy the repository-local data directory to the V1 data root.

    The old directory is never deleted.  A backup is created before copying,
    the SQLite file is copied with the SQLite backup API, and the destination
    is verified before the caller starts applying forward migrations.
    """
    target = target or get_default_paths()
    legacy = legacy or get_legacy_paths()
    if target.root == legacy.root or not legacy.database.exists():
        return False
    target.root.mkdir(parents=True, exist_ok=True)

    def _has_schema_data(database: Path) -> bool:
        """Return whether an existing target looks like a real installation."""
        if not database.is_file() or database.stat().st_size == 0:
            return False
        connection = None
        try:
            connection = sqlite3.connect(database)
            schema = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if schema is None:
                return False
            applied = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            projects = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
            ).fetchone()
            project_count = (
                connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                if projects is not None
                else 0
            )
            # A fully initialized database, or one containing user data,
            # is a legitimate target and must never be overwritten.  A
            # fully initialized empty database accompanied by project
            # files is instead the characteristic partial-migration state
            # and is quarantined so retry remains possible.
            from .migrations import MIGRATIONS

            has_project_files = target.projects.exists() and any(target.projects.iterdir())
            return project_count > 0 or (
                applied >= len(MIGRATIONS) and not has_project_files
            )
        except (OSError, sqlite3.DatabaseError):
            # A corrupt/partial destination is quarantined below so a retry
            # can recover from the untouched legacy installation.
            return False
        finally:
            if connection is not None:
                connection.close()

    # Never silently adopt a destination left by an interrupted migration.
    # Preserve it under the backup namespace, while refusing to overwrite a
    # populated/fully initialized target installation.
    if target.database.exists() and _has_schema_data(target.database):
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = target.root / "backups" / f"legacy-{stamp}-{uuid4().hex[:8]}"
    backup_root.mkdir(parents=True, exist_ok=True)
    quarantine = (
        (target.database, backup_root / "partial-aidrama.db"),
        (
            target.database.with_name(f"{target.database.name}-wal"),
            backup_root / "partial-aidrama.db-wal",
        ),
        (
            target.database.with_name(f"{target.database.name}-shm"),
            backup_root / "partial-aidrama.db-shm",
        ),
        (target.projects, backup_root / "partial-projects"),
        (target.archived_projects, backup_root / "partial-archived-projects"),
    )
    for source, destination in quarantine:
        if not source.exists():
            continue
        if source.is_dir() and not any(source.iterdir()):
            source.rmdir()
            continue
        shutil.move(str(source), str(destination))

    staging_root = target.root / f".legacy-migration-{uuid4().hex}"
    staging_root.mkdir(parents=True, exist_ok=False)
    staged_database = staging_root / "aidrama.db"
    staged_projects = staging_root / "projects"
    staged_archived_projects = staging_root / "archived_projects"
    installed: list[tuple[Path, Path]] = []
    try:
        # Keep an auditable backup and build the actual target database via
        # SQLite backup semantics.  The old implementation only populated
        # the audit copy, then integrity-checked a newly-created empty target.
        backup_database(legacy.database, backup_root / "aidrama.db")
        backup_database(legacy.database, staged_database)
        check = sqlite3.connect(staged_database)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"legacy database verification failed: {integrity}")
        finally:
            check.close()

        if legacy.projects.exists():
            shutil.copytree(legacy.projects, staged_projects)
        if legacy.archived_projects.exists():
            shutil.copytree(legacy.archived_projects, staged_archived_projects)

        # Install the verified files only after every source has been copied.
        # Database is installed last, so a failed file copy cannot expose a
        # partially migrated installation as active.
        for staged, destination in (
            (staged_projects, target.projects),
            (staged_archived_projects, target.archived_projects),
            (staged_database, target.database),
        ):
            if not staged.exists():
                continue
            os.replace(staged, destination)
            installed.append((destination, staged))

        check = sqlite3.connect(target.database)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"legacy database verification failed: {integrity}")
        finally:
            check.close()
        return True
    except Exception:
        # Roll back any installed destination paths. The untouched legacy
        # installation remains the source of truth and can be retried.
        for destination, staged in reversed(installed):
            if destination.exists() and not staged.exists():
                os.replace(destination, staged)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


class UnsupportedDatabaseSchemaError(RuntimeError):
    """Raised when an older binary sees a database from a newer binary."""


def initialize_database(paths: DatabasePaths | None = None) -> DatabasePaths:
    explicit = paths is not None
    resolved = paths or get_default_paths()
    if not explicit:
        migrate_legacy_data(resolved)
    resolved.database.parent.mkdir(parents=True, exist_ok=True)
    resolved.projects.mkdir(parents=True, exist_ok=True)
    resolved.archived_projects.mkdir(parents=True, exist_ok=True)
    if resolved.database.exists() and resolved.database.stat().st_size > 0:
        # Back up only when this startup will mutate an already-existing
        # schema.  Fresh databases need no pre-migration copy.
        with sqlite3.connect(resolved.database) as probe:
            has_schema = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            pending = False
            if has_schema:
                from .migrations import MIGRATIONS

                current = probe.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
                pending = current < max(version for version, _ in MIGRATIONS)
        if pending:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_database(resolved.database, resolved.root / "backups" / f"pre-migration-{stamp}.db")
    with connect(resolved.database) as connection:
        apply_migrations(connection)
    return resolved


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    # WAL is selected deliberately for file-backed user databases so the
    # desktop reader and background runner do not serialize ordinary reads.
    # In-memory test databases retain SQLite's default journal mode.
    if str(database_path) != ":memory:" and os.environ.get("AIDRAMA_SQLITE_WAL", "1") not in {"0", "false", "no"}:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


@contextmanager
def transaction(database_path: Path):
    """Open one explicit SQLite transaction for a durable domain transition.

    Repository methods intentionally use short-lived connections for ordinary
    reads and writes.  Cross-table state transitions must use this helper so
    an injected failure cannot leave an execution, event, goal, or manifest
    half committed.
    """
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()
