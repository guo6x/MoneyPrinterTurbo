"""Portable project export/import and backup primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from aidrama_studio.storage.database import backup_database
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.services.security import sanitize_error


class ProjectArchiveError(RuntimeError):
    pass


class ProjectArchiveService:
    MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
    MAX_ENTRIES = 100_000

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def backup_database(self, destination: Path) -> Path:
        return backup_database(self.repository.paths.database, Path(destination))

    def export_project(self, project_id: str, destination: Path) -> Path:
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProjectArchiveError("项目不存在")
        rows = self._project_rows(project_id)
        files = self._project_files(project_id)
        manifest = {
            "format": "AIDRAMA_PROJECT_ARCHIVE",
            "version": 1,
            "project_id": project_id,
            "schema_version": self._schema_version(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tables": rows,
            "files": files,
        }
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", json.dumps(self._redact(manifest), ensure_ascii=False, sort_keys=True, indent=2))
                root = self._project_root(project_id)
                for item in files:
                    source = root / Path(*item["path"].split("/"))
                    info = zipfile.ZipInfo(f"files/{item['path']}")
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.write(source, arcname=f"files/{item['path']}")
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def import_project(self, archive_path: Path, *, project_id: str | None = None) -> str:
        source = Path(archive_path)
        if not source.is_file():
            raise ProjectArchiveError("项目归档不存在")
        if source.stat().st_size > self.MAX_ARCHIVE_BYTES:
            raise ProjectArchiveError("项目归档超过大小限制")
        with zipfile.ZipFile(source) as archive:
            self._validate_archive(archive)
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                raise ProjectArchiveError("项目归档 manifest 无效") from exc
            if manifest.get("format") != "AIDRAMA_PROJECT_ARCHIVE" or not isinstance(manifest.get("tables"), dict):
                raise ProjectArchiveError("项目归档格式不受支持")
            if int(manifest.get("version", 0) or 0) != 1:
                raise ProjectArchiveError("项目归档版本不受支持")
            archive_schema = int(manifest.get("schema_version", 0) or 0)
            if archive_schema > self._schema_version():
                raise ProjectArchiveError("项目归档 schema 高于当前应用支持版本")
            old_id = str(manifest.get("project_id") or "")
            new_id = project_id or old_id
            if not self._safe_component(new_id):
                raise ProjectArchiveError("项目 ID 无效")
            if self.repository.get_project(new_id) is not None:
                raise ProjectArchiveError("项目 ID 已存在；请指定新的 project_id")
            files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
            root = (self.repository.paths.projects / new_id).resolve()
            configured = self.repository.paths.projects.resolve()
            if configured not in root.parents:
                raise ProjectArchiveError("项目导入路径越界")
            root.mkdir(parents=True, exist_ok=False)
            try:
                self._restore_files(archive, files, root)
                self._restore_rows(manifest["tables"], old_id, new_id)
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
            return new_id

    def _project_rows(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.repository.transaction() as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'")]
            result: dict[str, list[dict[str, Any]]] = {}
            ids: dict[str, set[str]] = {}
            for table in tables:
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                if table == "projects":
                    rows = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchall()
                    result[table] = [self._row_dict(row) for row in rows]
                    ids[table] = {str(row["id"]) for row in rows}
                    continue
                if "project_id" in columns:
                    rows = connection.execute(f'SELECT * FROM "{table}" WHERE project_id=?', (project_id,)).fetchall()
                    result[table] = [self._row_dict(row) for row in rows]
                    for key in ("id",):
                        if key in columns:
                            ids[table] = {str(row[key]) for row in rows}
            # Include child rows whose tables intentionally omit project_id.
            relations = {
                "production_shots": ("production_job_id", "production_jobs"),
                "production_attempts": ("production_shot_id", "production_shots"),
                "production_events": ("execution_id", "production_executions"),
                "production_artifacts": ("execution_id", "production_executions"),
                "production_qc_metrics": ("result_id", "production_qc_results"),
                "final_assembly_items": ("final_assembly_id", "final_assemblies"),
                "final_assembly_render_attempts": ("final_assembly_id", "final_assemblies"),
                "reference_profile_items": ("profile_id", "reference_profiles"),
            }
            for table, (foreign_key, parent) in relations.items():
                if table not in tables or parent not in ids:
                    continue
                parent_ids = ids[parent]
                if not parent_ids:
                    result[table] = []
                    continue
                placeholders = ",".join("?" for _ in parent_ids)
                rows = connection.execute(f'SELECT * FROM "{table}" WHERE "{foreign_key}" IN ({placeholders})', tuple(parent_ids)).fetchall()
                result[table] = [self._row_dict(row) for row in rows]
            return result

    def _project_files(self, project_id: str) -> list[dict[str, Any]]:
        root = self._project_root(project_id)
        result: list[dict[str, Any]] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.name.endswith(".tmp") or "credentials" in path.name.lower():
                continue
            relative = path.relative_to(root).as_posix()
            result.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": self._sha256(path)})
        return result

    def _restore_files(self, archive: zipfile.ZipFile, files: list[object], root: Path) -> None:
        for item in files:
            if not isinstance(item, dict):
                raise ProjectArchiveError("归档文件清单无效")
            relative = self._safe_relative(str(item.get("path") or ""))
            member = f"files/{relative}"
            if member not in archive.namelist():
                raise ProjectArchiveError("归档缺少文件")
            target = (root / Path(*relative.split("/"))).resolve()
            if root not in target.parents:
                raise ProjectArchiveError("归档文件路径越界")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                with archive.open(member, "r") as source, temporary.open("xb") as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
                    dest.flush(); os.fsync(dest.fileno())
                if int(item.get("size_bytes", -1)) != temporary.stat().st_size or str(item.get("sha256")) != self._sha256(temporary):
                    raise ProjectArchiveError("归档文件 hash/size 校验失败")
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _restore_rows(self, tables: dict[str, object], old_id: str, new_id: str) -> None:
        if old_id == new_id:
            rewrite = lambda value: value
        else:
            rewrite = lambda value: new_id if value == old_id else value
        order = ["projects", "story_bible_revisions", "structured_script_revisions", "shot_plan_revisions", "reference_assets", "reference_asset_versions", "reference_asset_bindings", "production_jobs", "production_shots", "production_attempts", "production_executions", "production_events", "production_artifacts", "production_qc_results", "production_qc_metrics", "production_reviews", "final_assemblies", "final_assembly_items", "final_assembly_render_attempts", "post_production_plans", "post_subtitle_tracks", "post_voice_tracks", "post_music_tracks", "post_render_attempts", "director_sessions", "director_goals", "director_decisions", "director_decision_events", "output_profiles", "generation_briefs", "runtime_plans", "ai_invocations", "source_pack_items", "normalized_creative_briefs", "intake_analyses", "reference_profiles", "reference_profile_items", "provider_capability_profiles", "provider_tasks", "vision_frame_manifests", "vision_analysis_results"]
        rank = {table: index for index, table in enumerate(order)}
        with self.repository.transaction() as connection:
            for table, raw_rows in sorted(tables.items(), key=lambda item: rank.get(item[0], 999)):
                if not isinstance(raw_rows, list):
                    continue
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                if not columns:
                    continue
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        raise ProjectArchiveError("归档行格式无效")
                    values = [self._rewrite_value(raw.get(column), rewrite) for column in columns]
                    try:
                        connection.execute(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', tuple(values))
                    except sqlite3.IntegrityError as exc:
                        raise ProjectArchiveError(f"项目归档包含 ID collision 或无效关系: {table}") from exc

    def _validate_archive(self, archive: zipfile.ZipFile) -> None:
        if len(archive.infolist()) > self.MAX_ENTRIES:
            raise ProjectArchiveError("归档条目过多")
        total = 0
        for info in archive.infolist():
            relative = info.filename.replace("\\", "/")
            if relative.startswith("/") or PureWindowsPath(relative).drive or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts):
                raise ProjectArchiveError("归档包含路径穿越")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ProjectArchiveError("归档包含符号链接")
            total += info.file_size
            if total > self.MAX_ARCHIVE_BYTES:
                raise ProjectArchiveError("归档解压大小超过限制")

    def _schema_version(self) -> int:
        with self.repository.transaction() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])

    def _project_root(self, project_id: str) -> Path:
        if self.repository.get_project(project_id) is None:
            raise ProjectArchiveError("项目不存在")
        return self.repository.project_directory(project_id).resolve()

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _rewrite_value(value: Any, rewrite) -> Any:
        if isinstance(value, str):
            return rewrite(value)
        if isinstance(value, bytes):
            return value
        return value

    @staticmethod
    def _safe_component(value: str) -> bool:
        return bool(value and value not in {".", ".."} and "/" not in value and "\\" not in value and not PureWindowsPath(value).drive)

    @classmethod
    def _safe_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if not normalized or normalized.startswith("/") or PureWindowsPath(normalized).drive or any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts):
            raise ProjectArchiveError("归档相对路径无效")
        return PurePosixPath(normalized).as_posix()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._redact(item) for key, item in value.items() if str(key).lower() not in {"api_key", "token", "authorization", "secret", "signed_url", "password"}}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            return sanitize_error(value, max_length=max(2000, len(value)))
        return value


__all__ = ["ProjectArchiveError", "ProjectArchiveService"]
