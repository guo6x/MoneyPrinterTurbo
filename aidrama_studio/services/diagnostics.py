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
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from aidrama_studio.services.ai_capabilities import CapabilityRegistry, default_capability_registry
from aidrama_studio.services.active_work import project_has_active_work
from aidrama_studio.services.security import sanitize_persistent_metadata
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DiagnosticsError(RuntimeError):
    pass


class DiagnosticsService:
    """Produce redacted diagnostics and perform narrowly safe cleanup."""

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
        foreign_keys = self._foreign_key_check()
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
            "sqlite_foreign_key_violations": foreign_keys,
            "data_root": self._safe_relative_root(root),
            "disk": {"used_bytes": usage, "free_bytes": free},
            "projects": reports,
            "provider_readiness": registry.public_status(),
            "ffmpeg_readiness": self._ffmpeg_readiness(),
        }

    def export_report(self, destination: Path, *, project_id: str | None = None) -> Path:
        report = sanitize_persistent_metadata(self.scan(project_id))
        if not isinstance(report, Mapping):
            raise DiagnosticsError("诊断报告清理失败")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(target)
        return target

    def cleanup_safe_temporary_files(self, project_id: str | None = None) -> list[str]:
        """Delete stale, unreferenced temporary files outside active work."""
        project_ids = [project_id] if project_id else [item.id for item in self.repository.list_projects()]
        removed: list[str] = []
        for item in project_ids:
            root = self._project_root(item)
            if not root.exists():
                continue
            cutoff = datetime.now(timezone.utc).timestamp() - self.stale_after_seconds
            candidates: list[tuple[Path, str]] = []
            for path in root.rglob("*"):
                try:
                    eligible = (
                        path.is_file()
                        and not path.is_symlink()
                        and self._is_temporary(path)
                        and path.stat().st_mtime < cutoff
                    )
                except OSError:
                    eligible = False
                if not eligible:
                    continue
                try:
                    relative = self._project_relative(root, path)
                except (OSError, ValueError):
                    continue
                candidates.append((path, relative))
            if not candidates:
                continue
            # BEGIN IMMEDIATE prevents a new durable task or media reference
            # racing into existence between the final recheck and unlink.
            with self.repository.transaction() as connection:
                if project_has_active_work(connection, item):
                    continue
                referenced = self._canonical_paths(item, connection=connection)
                for path, relative in candidates:
                    if relative in referenced:
                        continue
                    try:
                        path.unlink()
                        removed.append(relative)
                    except OSError:
                        continue
        return removed

    def reconcile_startup(self, project_id: str | None = None) -> dict[str, Any]:
        """Identify stale local work without pretending a remote task failed."""
        projects = [project_id] if project_id else [item.id for item in self.repository.list_projects()]
        stale_executions: list[str] = []
        reconciliation: list[str] = []
        provider_states: dict[str, int] = {}
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
                state = str(task.state).upper()
                provider_states[state] = provider_states.get(state, 0) + 1
                if state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    reconciliation.append(task.id)
        return {
            "stale_running_executions": stale_executions,
            "provider_tasks_requiring_reconciliation": reconciliation,
            "provider_task_states": provider_states,
            "checked_at": _now(),
        }

    def _project_report(self, project_id: str) -> dict[str, Any]:
        root = self._project_root(project_id)
        missing: list[str] = []
        mismatches: list[str] = []
        integrity_by_kind: dict[str, dict[str, list[str]]] = {}
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
        for kind, identifier, relative, expected in self._canonical_media(project_id):
            bucket = integrity_by_kind.setdefault(kind, {"missing": [], "hash_mismatches": []})
            path = self._resolve_relative(root, relative)
            try:
                valid_file = path is not None and path.is_file() and path.stat().st_size > 0
            except OSError:
                valid_file = False
            if not valid_file:
                bucket["missing"].append(identifier)
                continue
            if expected:
                try:
                    if self._sha256(path) != expected:
                        bucket["hash_mismatches"].append(identifier)
                except OSError:
                    bucket["missing"].append(identifier)
        if root.exists():
            referenced = self._canonical_paths(project_id)
            cutoff = datetime.now(timezone.utc).timestamp() - self.stale_after_seconds
            if not self._project_has_active_work(project_id):
                for path in root.rglob("*"):
                    try:
                        if (
                            not path.is_file()
                            or path.is_symlink()
                            or not self._is_temporary(path)
                            or path.stat().st_mtime >= cutoff
                        ):
                            continue
                        relative = self._project_relative(root, path)
                    except (OSError, ValueError):
                        continue
                    if relative not in referenced:
                        orphan_temporary.append(relative)
        return {
            "project_id": project_id,
            "storage": self._safe_relative_root(root),
            "storage_bytes": self._usage(root),
            "missing_artifacts": missing,
            "hash_mismatches": mismatches,
            "canonical_media_integrity": integrity_by_kind,
            "orphan_temporary_files": orphan_temporary,
            "startup": self.reconcile_startup(project_id),
        }

    def _integrity_check(self) -> str:
        with sqlite3.connect(self.repository.paths.database) as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def _foreign_key_check(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.repository.paths.database) as connection:
            return [
                {"table": str(row[0]), "rowid": int(row[1]), "parent": str(row[2]), "fk_index": int(row[3])}
                for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ]

    def _canonical_media(
        self,
        project_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[tuple[str, str, str, str | None]]:
        """Return durable path/hash references without inventing another truth."""
        specs = (
            ("source", "source_pack_items", "id", "storage_path", "sha256", "project_id=?"),
            ("reference", "reference_asset_versions", "id", "storage_path", "sha256", "project_id=?"),
            ("production", "production_artifacts", "id", "path", None, "execution_id IN (SELECT id FROM production_executions WHERE production_job_id IN (SELECT id FROM production_jobs WHERE project_id=?))"),
            ("final", "final_assembly_render_attempts", "id", "output_relative_path", None, "final_assembly_id IN (SELECT id FROM final_assemblies WHERE project_id=?) AND status='SUCCEEDED'"),
            ("post", "post_render_attempts", "id", "output_relative_path", None, "project_id=? AND status='SUCCEEDED'"),
            ("voice", "post_voice_tracks", "id", "path", None, "project_id=?"),
            ("music", "post_music_tracks", "id", "path", None, "project_id=?"),
        )
        if connection is None:
            with sqlite3.connect(self.repository.paths.database) as owned:
                owned.row_factory = sqlite3.Row
                return self._canonical_media_rows(owned, project_id, specs)
        return self._canonical_media_rows(connection, project_id, specs)

    @staticmethod
    def _canonical_media_rows(
        connection: sqlite3.Connection,
        project_id: str,
        specs: tuple[tuple[str, str, str, str, str | None, str], ...],
    ) -> list[tuple[str, str, str, str | None]]:
        result: list[tuple[str, str, str, str | None]] = []
        connection.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind, table, id_col, path_col, hash_col, where in specs:
            if table not in tables:
                continue
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if path_col not in columns:
                continue
            selected_hash = hash_col if hash_col in columns else "NULL"
            rows = connection.execute(
                f"SELECT {id_col},{path_col},{selected_hash} AS direct_sha,metadata_json FROM {table} WHERE {where}",
                (project_id,),
            ).fetchall()
            for row in rows:
                relative = row[path_col]
                if not isinstance(relative, str) or not relative.strip():
                    continue
                expected = str(row["direct_sha"] or "") or None
                if expected is None and "metadata_json" in row.keys():
                    try:
                        metadata = json.loads(row["metadata_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    value = metadata.get("sha256") if isinstance(metadata, Mapping) else None
                    expected = str(value) if isinstance(value, str) and value else None
                result.append((kind, str(row[id_col]), relative.replace("\\", "/"), expected))
        return result

    def _canonical_paths(
        self,
        project_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> set[str]:
        return {
            relative
            for _kind, _identifier, relative, _sha in self._canonical_media(
                project_id, connection=connection
            )
        }

    def _project_has_active_work(self, project_id: str) -> bool:
        with sqlite3.connect(self.repository.paths.database) as connection:
            return project_has_active_work(connection, project_id)

    @staticmethod
    def _ffmpeg_readiness() -> dict[str, Any]:
        try:
            from app.utils.utils import get_ffmpeg_binary

            binary = get_ffmpeg_binary()
            completed = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10, check=False)
            return {"ready": completed.returncode == 0, "runtime": "configured" if completed.returncode == 0 else "unavailable"}
        except Exception:
            return {"ready": False, "runtime": "unavailable"}

    @staticmethod
    def _usage(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())

    def _project_root(self, project_id: str) -> Path:
        if not project_id or self.repository.get_project(project_id) is None:
            raise DiagnosticsError("项目不存在")
        raw_root = self.repository.project_directory(project_id)
        if raw_root.is_symlink():
            raise DiagnosticsError("project storage root 不能是 symlink")
        root = raw_root.resolve()
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
        if not name.startswith("."):
            return False
        return (
            name.endswith(".tmp")
            or ".tmp." in name
            or ".in-progress." in name
            or name.endswith(".partial")
            or name.endswith(".download")
        )

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
