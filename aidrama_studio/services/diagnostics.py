"""Project diagnostics and safe recovery operations.

The diagnostics layer is intentionally read-mostly.  It inspects durable
SQLite state and project-local files without deleting canonical media.  The
same service is used by startup reconciliation and the Settings page so that
there is one truthful recovery projection.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from aidrama_studio.services.ai_capabilities import CapabilityRegistry, default_capability_registry
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DiagnosticsError(RuntimeError):
    pass


class DiagnosticsService:
    """Produce redacted diagnostics and perform narrowly safe cleanup."""

    TEMP_MARKERS = (".tmp", ".in-progress", ".partial", ".download")

    def __init__(self, repository: ProjectRepository | None = None, *, registry: CapabilityRegistry | None = None, stale_after_seconds: int = 900) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry
        self.stale_after_seconds = max(60, int(stale_after_seconds))

    def scan(self, project_id: str | None = None) -> dict[str, Any]:
        project_ids = [project_id] if project_id else [item.id for item in self.repository.list_projects()]
        for item in project_ids:
            if self.repository.get_project(item) is None:
                raise DiagnosticsError("项目不存在")
        integrity = self._integrity_check()
        with self.repository.transaction() as connection:
            schema_version = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
        reports = [self._project_report(item) for item in project_ids]
        root = self.repository.paths.root
        usage = self._usage(root)
        free = shutil.disk_usage(root).free if root.exists() else None
        registry = self.registry or default_capability_registry()
        return {
            "generated_at": _now(),
            "schema_version": schema_version,
            "sqlite_integrity": integrity,
            "data_root": self._safe_relative_root(root),
            "disk": {"used_bytes": usage, "free_bytes": free},
            "projects": reports,
            "provider_readiness": registry.public_status(),
        }

    def export_report(self, destination: Path, *, project_id: str | None = None) -> Path:
        report = self.scan(project_id)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(target)
        return target

    def cleanup_safe_temporary_files(self, project_id: str | None = None) -> list[str]:
        """Delete only clearly temporary files, never canonical DB rows/media."""
        project_ids = [project_id] if project_id else [item.id for item in self.repository.list_projects()]
        removed: list[str] = []
        for item in project_ids:
            root = self._project_root(item)
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink() or not self._is_temporary(path):
                    continue
                try:
                    path.unlink()
                    removed.append(self._project_relative(root, path))
                except OSError:
                    continue
        return removed

    def reconcile_startup(self, project_id: str | None = None) -> dict[str, Any]:
        """Identify stale local work without pretending a remote task failed."""
        projects = [project_id] if project_id else [item.id for item in self.repository.list_projects()]
        stale_executions: list[str] = []
        reconciliation: list[str] = []
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        for item in projects:
            for job in self.repository.list_production_jobs(item):
                for execution in self.repository.list_production_executions(job.id):
                    if execution.status.value != "RUNNING":
                        continue
                    stamp = execution.started_at or execution.created_at
                    try:
                        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        parsed = datetime.min.replace(tzinfo=timezone.utc)
                    if parsed < cutoff:
                        stale_executions.append(execution.id)
            for task in self.repository.list_provider_tasks(item):
                if task.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    reconciliation.append(task.id)
        return {"stale_running_executions": stale_executions, "provider_tasks_requiring_reconciliation": reconciliation, "checked_at": _now()}

    def _project_report(self, project_id: str) -> dict[str, Any]:
        root = self._project_root(project_id)
        missing: list[str] = []
        mismatches: list[str] = []
        orphan_temporary: list[str] = []
        for job in self.repository.list_production_jobs(project_id):
            for execution in self.repository.list_production_executions(job.id):
                for artifact in self.repository.list_production_artifacts(execution.id):
                    path = self._resolve_relative(root, artifact.path)
                    if path is None or not path.is_file() or path.stat().st_size <= 0:
                        missing.append(artifact.id)
                        continue
                    expected = artifact.metadata_json.get("sha256")
                    if isinstance(expected, str) and expected and self._sha256(path) != expected:
                        mismatches.append(artifact.id)
        if root.exists():
            orphan_temporary = [self._project_relative(root, path) for path in root.rglob("*") if path.is_file() and not path.is_symlink() and self._is_temporary(path)]
        return {
            "project_id": project_id,
            "storage": self._safe_relative_root(root),
            "storage_bytes": self._usage(root),
            "missing_artifacts": missing,
            "hash_mismatches": mismatches,
            "orphan_temporary_files": orphan_temporary,
            "startup": self.reconcile_startup(project_id),
        }

    def _integrity_check(self) -> str:
        with sqlite3.connect(self.repository.paths.database) as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    @staticmethod
    def _usage(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())

    def _project_root(self, project_id: str) -> Path:
        if not project_id or self.repository.get_project(project_id) is None:
            raise DiagnosticsError("项目不存在")
        root = self.repository.project_directory(project_id).resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise DiagnosticsError("project storage path escapes configured root")
        return root

    @staticmethod
    def _resolve_relative(root: Path, relative: str) -> Path | None:
        if not isinstance(relative, str) or not relative.strip():
            return None
        normalized = relative.replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(normalized).drive:
            return None
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        candidate = (root / Path(*parts)).resolve()
        return candidate if root.resolve() in candidate.parents else None

    @classmethod
    def _project_relative(cls, root: Path, path: Path) -> str:
        return path.resolve().relative_to(root.resolve()).as_posix()

    @classmethod
    def _is_temporary(cls, path: Path) -> bool:
        name = path.name.lower()
        return any(marker in name for marker in cls.TEMP_MARKERS) or name.startswith(".") and name.endswith(".json")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _safe_relative_root(root: Path) -> str:
        # Diagnostics are operator-facing and should not leak a user's full
        # home path.  Keep only the final product directory name.
        return f"<AIDramaData>/{root.name}"


class DiskSpaceService:
    """Disk preflight and provenance-aware cleanup facade."""

    def __init__(self, repository: ProjectRepository | None = None, *, diagnostics: DiagnosticsService | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.diagnostics = diagnostics or DiagnosticsService(self.repository)

    def usage(self, project_id: str | None = None) -> dict[str, int | None]:
        root = self.repository.paths.projects / project_id if project_id else self.repository.paths.root
        root = root.resolve()
        usage = self.diagnostics._usage(root)
        free = shutil.disk_usage(root if root.exists() else self.repository.paths.root).free
        return {"used_bytes": usage, "free_bytes": free}

    def preflight(self, required_bytes: int, *, project_id: str | None = None, reserve_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
        if isinstance(required_bytes, bool) or int(required_bytes) < 0:
            raise DiagnosticsError("required_bytes 必须为非负整数")
        status = self.usage(project_id)
        available = int(status["free_bytes"] or 0)
        required = int(required_bytes) + max(0, int(reserve_bytes))
        return {"ready": available >= required, "required_bytes": required, "free_bytes": available, "reason": "可用空间不足" if available < required else "ready"}

    def cleanup_safe(self, project_id: str | None = None) -> list[str]:
        return self.diagnostics.cleanup_safe_temporary_files(project_id)


__all__ = ["DiagnosticsError", "DiagnosticsService", "DiskSpaceService"]
