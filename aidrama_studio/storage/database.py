from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class DatabasePaths:
    database: Path
    projects: Path
    archived_projects: Path


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_default_paths() -> DatabasePaths:
    root = repository_root() / "storage" / "aidrama"
    return DatabasePaths(
        database=root / "aidrama.db",
        projects=root / "projects",
        archived_projects=root / "archived_projects",
    )


def initialize_database(paths: DatabasePaths | None = None) -> DatabasePaths:
    resolved = paths or get_default_paths()
    resolved.database.parent.mkdir(parents=True, exist_ok=True)
    resolved.projects.mkdir(parents=True, exist_ok=True)
    resolved.archived_projects.mkdir(parents=True, exist_ok=True)
    with connect(resolved.database) as connection:
        apply_migrations(connection)
    return resolved


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection
