"""Portable, self-validating project export/import primitives.

The archive is deliberately an allowlisted project snapshot, not a database
dump. Global settings and credential stores are never selected, while normal
creative text is preserved byte-for-byte inside the JSON manifest.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable
from uuid import uuid4

from aidrama_studio.branding import BRAND
from aidrama_studio.services.security import sanitize_error, sanitize_persistent_metadata
from aidrama_studio.services.active_work import project_has_active_work
from aidrama_studio.storage.database import DatabasePaths, backup_database
from aidrama_studio.storage.repositories import ProjectRepository


class ProjectArchiveError(RuntimeError):
    pass


class ProjectArchiveService:
    """Export and restore one complete, project-owned graph."""

    FORMAT = "AIDRAMA_PROJECT_ARCHIVE"
    ARCHIVE_VERSION = 2
    MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
    MAX_MANIFEST_BYTES = 64 * 1024 * 1024
    MAX_ENTRIES = 100_000

    # Import order is also the ownership-discovery order. This is intentionally
    # explicit: a newly added table is excluded until its ownership is reviewed.
    TABLE_ALLOWLIST = (
        "projects",
        "story_bible_revisions",
        "structured_script_revisions",
        "shot_plan_revisions",
        "creative_locks",
        "reference_assets",
        "reference_asset_versions",
        "reference_image_candidates",
        "reference_image_candidate_events",
        "reference_asset_bindings",
        "output_profiles",
        "production_jobs",
        "production_shots",
        "production_attempts",
        "production_executions",
        "production_events",
        "production_artifacts",
        "generation_briefs",
        "generation_brief_selections",
        "runtime_plans",
        "ai_invocations",
        "production_qc_results",
        "production_qc_metrics",
        "production_reviews",
        "production_shot_source_decisions",
        "final_assemblies",
        "final_assembly_items",
        "final_assembly_render_attempts",
        "post_production_plans",
        "post_subtitle_tracks",
        "post_voice_tracks",
        "post_music_tracks",
        "post_render_attempts",
        "director_sessions",
        "director_goals",
        "director_decisions",
        "director_decision_events",
        "producer_recommendation_events",
        "source_pack_items",
        "normalized_creative_briefs",
        "intake_analyses",
        "reference_profiles",
        "reference_profile_items",
        "provider_capability_profiles",
        "provider_tasks",
        "vision_frame_manifests",
        "vision_analysis_results",
        "provider_selection_settings",
        "heavy_jobs",
        "heavy_job_events",
        "auto_orchestrator_runs",
        "auto_agent_events",
        "auto_paid_authorizations",
        "auto_paid_consumptions",
    )

    # Rows with NULL project_id (global provider defaults/settings) are
    # excluded by construction.
    DIRECT_PROJECT_TABLES = frozenset(
        {
            "story_bible_revisions",
            "structured_script_revisions",
            "shot_plan_revisions",
            "creative_locks",
            "reference_assets",
            "reference_asset_versions",
            "reference_image_candidates",
            "reference_asset_bindings",
            "output_profiles",
            "production_jobs",
            "generation_briefs",
            "generation_brief_selections",
            "runtime_plans",
            "ai_invocations",
            "production_qc_results",
            "production_reviews",
            "production_shot_source_decisions",
            "final_assemblies",
            "post_production_plans",
            "post_subtitle_tracks",
            "post_voice_tracks",
            "post_music_tracks",
            "post_render_attempts",
            "director_sessions",
            "director_goals",
            "director_decisions",
            "director_decision_events",
            "producer_recommendation_events",
            "source_pack_items",
            "normalized_creative_briefs",
            "intake_analyses",
            "reference_profiles",
            "provider_capability_profiles",
            "provider_tasks",
            "vision_frame_manifests",
            "vision_analysis_results",
            "provider_selection_settings",
            "heavy_jobs",
            "auto_orchestrator_runs",
            "auto_agent_events",
            "auto_paid_authorizations",
        }
    )

    # Child tables that intentionally omit project_id. Each child must be
    # reachable through this exact owner edge.
    CHILD_OWNERS = {
        "production_shots": ("production_job_id", "production_jobs"),
        "production_attempts": ("production_shot_id", "production_shots"),
        "production_executions": ("production_job_id", "production_jobs"),
        "production_events": ("execution_id", "production_executions"),
        "production_artifacts": ("execution_id", "production_executions"),
        "production_qc_metrics": ("result_id", "production_qc_results"),
        "final_assembly_items": ("final_assembly_id", "final_assemblies"),
        "final_assembly_render_attempts": ("final_assembly_id", "final_assemblies"),
        "reference_profile_items": ("profile_id", "reference_profiles"),
        "reference_image_candidate_events": (
            "candidate_id",
            "reference_image_candidates",
        ),
        "heavy_job_events": ("heavy_job_id", "heavy_jobs"),
        "auto_paid_consumptions": (
            "authorization_id",
            "auto_paid_authorizations",
        ),
    }

    # ALTER TABLE additions without SQLite FK clauses are still project graph
    # edges and must not resolve to another project's existing row on import.
    SOFT_FOREIGN_KEYS = {
        ("reference_assets", "current_version_id"): ("reference_asset_versions", "id"),
        ("production_jobs", "output_profile_id"): ("output_profiles", "id"),
        ("production_executions", "runtime_plan_id"): ("runtime_plans", "id"),
        ("production_executions", "generation_brief_id"): ("generation_briefs", "id"),
        ("final_assemblies", "output_profile_id"): ("output_profiles", "id"),
        ("post_production_plans", "source_final_assembly_render_attempt_id"): (
            "final_assembly_render_attempts",
            "id",
        ),
        ("post_render_attempts", "source_final_assembly_render_attempt_id"): (
            "final_assembly_render_attempts",
            "id",
        ),
    }

    MANIFEST_KEYS = frozenset(
        {
            "format",
            "version",
            "product_version",
            "project_id",
            "schema_version",
            "created_at",
            "tables",
            "files",
            "content_sha256",
        }
    )

    # These columns are operational provenance rather than authored creative
    # text. Sanitize their structured contents at the export boundary as a
    # defense in depth even though their writers already apply redaction.
    SANITIZED_JSON_COLUMNS = {
        "provider_tasks": frozenset({"request_summary_json", "metadata_json"}),
        "ai_invocations": frozenset({"request_summary_json", "usage_json"}),
        "production_events": frozenset({"payload_json"}),
        "production_attempts": frozenset({"input_snapshot_json", "output_artifact_json"}),
        "production_executions": frozenset({"input_snapshot_json"}),
        "production_artifacts": frozenset({"metadata_json"}),
        "production_qc_results": frozenset({"summary_json"}),
        "production_qc_metrics": frozenset({"value_json"}),
        "final_assembly_render_attempts": frozenset({"metadata_json"}),
        "post_voice_tracks": frozenset({"metadata_json"}),
        "post_music_tracks": frozenset({"metadata_json"}),
        "post_render_attempts": frozenset({"metadata_json"}),
        "vision_frame_manifests": frozenset({"samples_json"}),
        "vision_analysis_results": frozenset(
            {"metrics_json", "reference_comparison_json", "input_provenance_json"}
        ),
        "source_pack_items": frozenset({"metadata_json"}),
        "reference_asset_versions": frozenset({"metadata_json"}),
        "runtime_plans": frozenset({"provider_parameters_json", "authorization_json"}),
        "heavy_jobs": frozenset(
            {"input_snapshot_json", "output_provenance_json"}
        ),
        "heavy_job_events": frozenset({"payload_json"}),
        "auto_orchestrator_runs": frozenset({"metadata_json"}),
        "auto_paid_authorizations": frozenset({"authorization_json"}),
    }
    SANITIZED_TEXT_COLUMNS = {
        "provider_tasks": frozenset({"error_message"}),
        "production_attempts": frozenset({"error_message"}),
        "final_assembly_render_attempts": frozenset({"error_message"}),
        "post_render_attempts": frozenset({"error_message"}),
        "heavy_jobs": frozenset({"safe_error"}),
    }

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def backup_database(self, destination: Path) -> Path:
        return backup_database(self.repository.paths.database, Path(destination))

    def export_project(
        self,
        project_id: str,
        destination: Path,
        *,
        excluding_heavy_job_id: str | None = None,
    ) -> Path:
        if self.repository.get_project(project_id) is None:
            raise ProjectArchiveError("项目不存在")
        target = Path(destination)
        if target.exists():
            raise ProjectArchiveError("项目归档目标已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        self.repository.paths.archived_projects.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=f".export-{project_id}-",
                dir=self.repository.paths.archived_projects,
            ) as snapshot_directory:
                snapshot_root = Path(snapshot_directory) / "files"
                snapshot_root.mkdir(parents=True, exist_ok=False)
                # BEGIN IMMEDIATE prevents a concurrent DB transition from
                # straddling the row/file snapshot. Existing active work is
                # rejected; the background PROJECT_EXPORT job may exclude
                # only its own durable row.
                with self.repository.transaction() as connection:
                    if project_has_active_work(
                        connection,
                        project_id,
                        excluding_heavy_job_id=excluding_heavy_job_id,
                    ):
                        raise ProjectArchiveError(
                            "项目仍有活动任务；暂不能创建一致归档快照"
                        )
                    rows = self._project_rows(project_id, connection=connection)
                    files = self._snapshot_project_files(
                        project_id, snapshot_root
                    )
                    schema_version = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
                        ).fetchone()[0]
                    )
                manifest: dict[str, Any] = {
                    "format": self.FORMAT,
                    "version": self.ARCHIVE_VERSION,
                    "product_version": BRAND.version,
                    "project_id": project_id,
                    "schema_version": schema_version,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "tables": rows,
                    "files": files,
                }
                manifest["content_sha256"] = self._content_sha256(manifest)
                with zipfile.ZipFile(
                    temporary,
                    "x",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                    )
                    for item in files:
                        source = snapshot_root / Path(*item["path"].split("/"))
                        archive.write(source, arcname=f"files/{item['path']}")
            # Validate the temporary package through the same isolated import
            # path before it becomes the caller-visible backup.
            self.verify_importable(temporary)
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
        try:
            archive = zipfile.ZipFile(source)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ProjectArchiveError("项目归档 ZIP 无效") from exc
        with archive:
            members = self._validate_archive(archive)
            manifest = self._load_manifest(archive)
            self._validate_manifest(manifest, members)
            old_id = str(manifest["project_id"])
            new_id = project_id or old_id
            if not self._safe_component(new_id):
                raise ProjectArchiveError("项目 ID 无效")
            if self.repository.get_project(new_id) is not None:
                raise ProjectArchiveError("项目 ID 已存在；请指定新的 project_id")
            files = manifest["files"]
            raw_root = self.repository.paths.projects / new_id
            root = raw_root.resolve()
            configured = self.repository.paths.projects.resolve()
            if configured not in root.parents:
                raise ProjectArchiveError("项目导入路径越界")
            staging = (
                self.repository.paths.projects
                / f".{new_id}-{uuid4().hex}.importing"
            ).resolve()
            if raw_root.exists() or raw_root.is_symlink() or staging.exists():
                raise ProjectArchiveError("项目导入目标已存在")
            staging.mkdir(parents=True, exist_ok=False)

            def finalize_files() -> None:
                if raw_root.exists() or raw_root.is_symlink():
                    raise ProjectArchiveError("项目导入目标在恢复期间被占用")
                os.replace(staging, raw_root)

            try:
                self._restore_files(archive, files, staging, members)
                self._restore_rows(
                    manifest["tables"],
                    old_id,
                    new_id,
                    finalize_files=finalize_files,
                )
            except Exception:
                cleanup_errors = []
                for candidate in (staging, raw_root):
                    try:
                        self._remove_tree(candidate)
                    except OSError as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                if cleanup_errors:
                    raise ProjectArchiveError(
                        "项目导入失败且残留 staging；需要通过诊断中心清理"
                    ) from cleanup_errors[0]
                raise
            return new_id

    def verify_importable(self, archive_path: Path) -> None:
        """Prove a backup can be restored through the public safe import path."""

        self.repository.paths.archived_projects.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".archive-verify-", dir=self.repository.paths.archived_projects
        ) as directory:
            root = Path(directory)
            verification_repository = ProjectRepository(
                DatabasePaths(
                    root / "db" / "aidrama.db",
                    root / "projects",
                    root / "archived",
                )
            )
            verification_service = ProjectArchiveService(verification_repository)
            imported = verification_service.import_project(archive_path)
            if verification_repository.get_project(imported) is None:
                raise ProjectArchiveError("项目归档恢复自校验失败")
            # sqlite3.Connection's context manager commits but does not close.
            # Repository helpers are short-lived, yet on Windows finalizers can
            # lag just long enough to keep the verification database locked.
            del verification_service
            del verification_repository
            gc.collect()

    def _project_rows(
        self,
        project_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if connection is None:
            with self.repository.transaction() as owned_connection:
                return self._project_rows(
                    project_id, connection=owned_connection
                )
        else:
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = set(self.TABLE_ALLOWLIST) - available
            if missing:
                raise ProjectArchiveError(
                    f"当前 schema 缺少项目归档表: {', '.join(sorted(missing))}"
                )
            result: dict[str, list[dict[str, Any]]] = {}
            ids: dict[str, set[str]] = {}
            for table in self.TABLE_ALLOWLIST:
                if table == "projects":
                    selected = connection.execute(
                        "SELECT * FROM projects WHERE id=? ORDER BY rowid", (project_id,)
                    ).fetchall()
                elif table == "heavy_jobs":
                    # Delivery-copy/export jobs can contain a user-selected
                    # absolute destination and do not affect creative/runtime
                    # provenance. Keep final/post/TTS history, but never put
                    # private destination paths or the export job itself into
                    # a portable archive.
                    selected = connection.execute(
                        "SELECT * FROM heavy_jobs WHERE project_id=? AND job_type "
                        "NOT IN ('FINAL_MEDIA_EXPORT','PROJECT_EXPORT','PROJECT_IMPORT') "
                        "ORDER BY rowid",
                        (project_id,),
                    ).fetchall()
                elif table in self.DIRECT_PROJECT_TABLES:
                    selected = connection.execute(
                        f'SELECT * FROM "{table}" WHERE project_id=? ORDER BY rowid',
                        (project_id,),
                    ).fetchall()
                else:
                    owner_column, parent_table = self.CHILD_OWNERS[table]
                    parent_ids = ids[parent_table]
                    if not parent_ids:
                        selected = []
                    else:
                        placeholders = ",".join("?" for _ in parent_ids)
                        selected = connection.execute(
                            f'SELECT * FROM "{table}" WHERE "{owner_column}" '
                            f"IN ({placeholders}) ORDER BY rowid",
                            tuple(sorted(parent_ids)),
                        ).fetchall()
                rows = [self._export_row(table, row) for row in selected]
                result[table] = rows
                ids[table] = {
                    str(row["id"]) for row in rows if row.get("id") is not None
                }
            if len(result["projects"]) != 1:
                raise ProjectArchiveError("项目归档必须包含且仅包含一个 project row")
            return result

    def _snapshot_project_files(
        self, project_id: str, snapshot_root: Path
    ) -> list[dict[str, Any]]:
        """Copy each live file once; ZIP/hash later consume only the snapshot."""
        root = self._project_root(project_id)
        result: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or path.is_symlink() or self._transient_file(path):
                continue
            resolved = path.resolve(strict=True)
            if root not in resolved.parents or self._has_symlink_component(root, path):
                raise ProjectArchiveError("项目目录包含不安全的 symlink 文件路径")
            relative = self._safe_relative(path.relative_to(root).as_posix())
            target = snapshot_root / Path(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            digest = hashlib.sha256()
            copied = 0
            try:
                with path.open("rb") as source, target.open("xb") as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        destination.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
            except OSError as exc:
                raise ProjectArchiveError("项目文件快照读取失败") from exc
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or copied != before.st_size
            ):
                raise ProjectArchiveError("项目文件在归档快照期间发生变化")
            result.append(
                {
                    "path": relative,
                    "size_bytes": copied,
                    "sha256": digest.hexdigest(),
                }
            )
        return result

    def _project_files(self, project_id: str) -> list[dict[str, Any]]:
        root = self._project_root(project_id)
        result: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if (
                not path.is_file()
                or path.is_symlink()
                or self._transient_file(path)
            ):
                continue
            resolved = path.resolve(strict=True)
            if root not in resolved.parents or self._has_symlink_component(root, path):
                raise ProjectArchiveError("项目目录包含不安全的 symlink 文件路径")
            relative = self._safe_relative(path.relative_to(root).as_posix())
            result.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": self._sha256(path),
                }
            )
        return result

    @staticmethod
    def _transient_file(path: Path) -> bool:
        name = path.name.casefold()
        return (
            name.endswith((".tmp", ".partial", ".in-progress"))
            or ".in-progress." in name
            or ".partial." in name
            or name.startswith(".in-progress")
            or "credentials" in name
        )

    def _restore_files(
        self,
        archive: zipfile.ZipFile,
        files: list[object],
        root: Path,
        members: dict[str, zipfile.ZipInfo],
    ) -> None:
        for item in files:
            relative = self._safe_relative(str(item["path"]))
            member = f"files/{relative}"
            info = members[member]
            if int(item["size_bytes"]) != info.file_size:
                raise ProjectArchiveError("归档文件清单与 ZIP size 不一致")
            target = (root / Path(*relative.split("/"))).resolve()
            if root not in target.parents:
                raise ProjectArchiveError("归档文件路径越界")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                with archive.open(info, "r") as source, temporary.open("xb") as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
                    dest.flush()
                    os.fsync(dest.fileno())
                if (
                    int(item["size_bytes"]) != temporary.stat().st_size
                    or str(item["sha256"]) != self._sha256(temporary)
                ):
                    raise ProjectArchiveError("归档文件 hash/size 校验失败")
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _restore_rows(
        self,
        tables: dict[str, object],
        old_id: str,
        new_id: str,
        *,
        finalize_files: Callable[[], None] | None = None,
    ) -> None:
        with self.repository.transaction() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            for table in self.TABLE_ALLOWLIST:
                raw_rows = tables[table]
                columns = [
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                for raw in raw_rows:
                    values = [
                        self._import_value(table, column, raw[column], old_id, new_id)
                        for column in columns
                    ]
                    try:
                        connection.execute(
                            f'INSERT INTO "{table}" ({",".join(columns)}) '
                            f'VALUES ({",".join("?" for _ in columns)})',
                            tuple(values),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ProjectArchiveError(
                            f"项目归档包含 ID collision 或无效关系: {table}"
                        ) from exc
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                first = violations[0]
                raise ProjectArchiveError(
                    f"项目归档 foreign_key_check 失败: {first[0]}"
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM projects WHERE id=?", (new_id,)
            ).fetchone()[0]
            if count != 1:
                raise ProjectArchiveError("项目归档 project row 恢复不精确")
            if finalize_files is not None:
                finalize_files()

    def _validate_archive(
        self, archive: zipfile.ZipFile
    ) -> dict[str, zipfile.ZipInfo]:
        infos = archive.infolist()
        if len(infos) > self.MAX_ENTRIES:
            raise ProjectArchiveError("归档条目过多")
        total = 0
        members: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        for info in infos:
            raw = info.filename
            if "\\" in raw or "\x00" in raw:
                raise ProjectArchiveError("归档包含非规范路径")
            try:
                relative = self._safe_relative(raw)
            except ProjectArchiveError as exc:
                raise ProjectArchiveError("归档包含路径穿越") from exc
            if relative != raw:
                raise ProjectArchiveError("归档包含非规范路径")
            if relative in members or relative.casefold() in folded:
                raise ProjectArchiveError("归档包含重复 member/path")
            if info.flag_bits & 0x1:
                raise ProjectArchiveError("归档包含加密 member")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ProjectArchiveError("归档包含符号链接")
            total += info.file_size
            if total > self.MAX_ARCHIVE_BYTES:
                raise ProjectArchiveError("归档解压大小超过限制")
            members[relative] = info
            folded.add(relative.casefold())
        if "manifest.json" not in members:
            raise ProjectArchiveError("项目归档缺少 manifest")
        return members

    def _load_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        def reject_duplicate_keys(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ProjectArchiveError("项目归档 JSON 包含重复 key")
                value[key] = item
            return value

        info = archive.getinfo("manifest.json")
        if info.file_size > self.MAX_MANIFEST_BYTES:
            raise ProjectArchiveError("项目归档 manifest 超过大小限制")
        try:
            value = json.loads(
                archive.read("manifest.json").decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except ProjectArchiveError:
            raise
        except Exception as exc:
            raise ProjectArchiveError("项目归档 manifest 无效") from exc
        if not isinstance(value, dict):
            raise ProjectArchiveError("项目归档 manifest 无效")
        return value

    def _validate_manifest(
        self, manifest: dict[str, Any], members: dict[str, zipfile.ZipInfo]
    ) -> None:
        if set(manifest) != self.MANIFEST_KEYS:
            raise ProjectArchiveError("项目归档 manifest 字段不受支持")
        if manifest["format"] != self.FORMAT:
            raise ProjectArchiveError("项目归档格式不受支持")
        if manifest["version"] != self.ARCHIVE_VERSION:
            raise ProjectArchiveError("项目归档版本不受支持")
        product_version = manifest["product_version"]
        if not isinstance(product_version, str) or not re.fullmatch(
            r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", product_version
        ):
            raise ProjectArchiveError("项目归档 product version 无效")
        # V1 is the first public product release. Preserve import compatibility
        # with verified 0.x preview archives while still rejecting unknown
        # future major formats.
        archive_major = product_version.split(".", 1)[0]
        current_major = BRAND.version.split(".", 1)[0]
        compatible_majors = {current_major}
        if current_major == "1":
            compatible_majors.add("0")
        if archive_major not in compatible_majors:
            raise ProjectArchiveError("项目归档 product major version 不兼容")
        if manifest["schema_version"] != self._schema_version():
            raise ProjectArchiveError("项目归档 schema version 不兼容")
        old_id = manifest["project_id"]
        if not isinstance(old_id, str) or not self._safe_component(old_id):
            raise ProjectArchiveError("项目归档 project_id 无效")
        if not isinstance(manifest["created_at"], str) or not manifest["created_at"]:
            raise ProjectArchiveError("项目归档 created_at 无效")
        expected_hash = manifest["content_sha256"]
        if not self._valid_sha256(expected_hash):
            raise ProjectArchiveError("项目归档 content hash 无效")
        if self._content_sha256(manifest) != expected_hash:
            raise ProjectArchiveError("项目归档 content hash 校验失败")

        files = manifest["files"]
        if not isinstance(files, list):
            raise ProjectArchiveError("项目归档文件清单无效")
        expected_members = {"manifest.json"}
        seen_paths: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "size_bytes",
                "sha256",
            }:
                raise ProjectArchiveError("项目归档文件清单无效")
            relative = self._safe_relative(str(item["path"]))
            if relative.casefold() in seen_paths:
                raise ProjectArchiveError("项目归档文件清单包含重复 path")
            if (
                not isinstance(item["size_bytes"], int)
                or isinstance(item["size_bytes"], bool)
                or item["size_bytes"] < 0
                or not self._valid_sha256(item["sha256"])
            ):
                raise ProjectArchiveError("项目归档文件 hash/size 无效")
            expected_members.add(f"files/{relative}")
            seen_paths.add(relative.casefold())
        if set(members) != expected_members:
            raise ProjectArchiveError("项目归档包含缺失或额外文件")

        tables = manifest["tables"]
        if not isinstance(tables, dict) or set(tables) != set(self.TABLE_ALLOWLIST):
            raise ProjectArchiveError("项目归档 table allowlist 不匹配")
        with self.repository.transaction() as connection:
            self._validate_table_rows(connection, tables, old_id)

    def _validate_table_rows(
        self, connection: sqlite3.Connection, tables: dict[str, Any], project_id: str
    ) -> None:
        project_rows = tables.get("projects")
        if (
            not isinstance(project_rows, list)
            or len(project_rows) != 1
            or not isinstance(project_rows[0], dict)
            or project_rows[0].get("id") != project_id
        ):
            raise ProjectArchiveError("项目归档必须包含且仅包含精确 project row")

        value_sets: dict[tuple[str, str], set[Any]] = {}
        for table in self.TABLE_ALLOWLIST:
            rows = tables.get(table)
            if not isinstance(rows, list):
                raise ProjectArchiveError(f"项目归档 table rows 无效: {table}")
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            if not columns:
                raise ProjectArchiveError(f"当前 schema 缺少 table: {table}")
            seen_ids: set[Any] = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ProjectArchiveError(f"项目归档 row schema 不匹配: {table}")
                if "id" in row:
                    if row["id"] in seen_ids:
                        raise ProjectArchiveError(f"项目归档包含重复 row id: {table}")
                    seen_ids.add(row["id"])
                if table in self.DIRECT_PROJECT_TABLES and row["project_id"] != project_id:
                    raise ProjectArchiveError(f"项目归档包含跨项目 row: {table}")
            for column in columns:
                value_sets[(table, column)] = {row[column] for row in rows}

        for table, (owner_column, parent_table) in self.CHILD_OWNERS.items():
            parent_ids = value_sets[(parent_table, "id")]
            if any(row[owner_column] not in parent_ids for row in tables[table]):
                raise ProjectArchiveError(f"项目归档 child ownership 无效: {table}")

        for table in self.TABLE_ALLOWLIST:
            for foreign_key in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall():
                referenced_table = str(foreign_key[2])
                source_column = str(foreign_key[3])
                target_column = str(foreign_key[4])
                if referenced_table not in self.TABLE_ALLOWLIST:
                    raise ProjectArchiveError(
                        f"项目归档 table FK 不在 allowlist: {table}.{source_column}"
                    )
                allowed = value_sets[(referenced_table, target_column)]
                for row in tables[table]:
                    value = row[source_column]
                    if value is not None and value not in allowed:
                        raise ProjectArchiveError(
                            f"项目归档 FK graph 不闭合: {table}.{source_column}"
                        )
        for (table, source_column), (target_table, target_column) in self.SOFT_FOREIGN_KEYS.items():
            allowed = value_sets[(target_table, target_column)]
            for row in tables[table]:
                value = row.get(source_column)
                if value is not None and value not in allowed:
                    raise ProjectArchiveError(
                        f"项目归档 soft FK graph 不闭合: {table}.{source_column}"
                    )

    def _schema_version(self) -> int:
        with self.repository.transaction() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
                ).fetchone()[0]
            )

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
        if path.exists() or path.is_symlink():
            raise OSError("project import staging cleanup incomplete")

    def _project_root(self, project_id: str) -> Path:
        if self.repository.get_project(project_id) is None:
            raise ProjectArchiveError("项目不存在")
        raw_root = self.repository.project_directory(project_id)
        if raw_root.is_symlink():
            raise ProjectArchiveError("项目目录不能是 symlink")
        root = raw_root.resolve()
        configured = self.repository.paths.projects.resolve()
        if configured not in root.parents:
            raise ProjectArchiveError("项目目录越过 configured storage root")
        return root

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    @classmethod
    def _export_row(cls, table: str, row: sqlite3.Row) -> dict[str, Any]:
        result = cls._row_dict(row)
        for column in cls.SANITIZED_JSON_COLUMNS.get(table, frozenset()):
            raw = result.get(column)
            if raw is None:
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectArchiveError(
                    f"项目 operational JSON 无效: {table}.{column}"
                ) from exc
            if sanitize_persistent_metadata(parsed) != parsed:
                raise ProjectArchiveError(
                    f"项目 operational JSON 含不可导出的敏感信息: {table}.{column}"
                )
        for column in cls.SANITIZED_TEXT_COLUMNS.get(table, frozenset()):
            raw = result.get(column)
            if raw is not None and sanitize_error(raw, max_length=4000) != raw:
                raise ProjectArchiveError(
                    f"项目 operational text 含不可导出的敏感信息: {table}.{column}"
                )
        return result

    @staticmethod
    def _has_symlink_component(root: Path, path: Path) -> bool:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def _import_value(
        table: str, column: str, value: Any, old_id: str, new_id: str
    ) -> Any:
        # Rewrite only identity columns. Creative JSON/text that happens to
        # contain the old project id remains untouched.
        if old_id != new_id and value == old_id and (
            (table == "projects" and column == "id") or column == "project_id"
        ):
            return new_id
        return value

    @classmethod
    def _safe_component(cls, value: str) -> bool:
        return bool(
            value
            and value not in {".", ".."}
            and "/" not in value
            and "\\" not in value
            and "\x00" not in value
            and not PureWindowsPath(value).drive
            and cls._safe_windows_component(value)
        )

    @classmethod
    def _safe_relative(cls, value: str) -> str:
        if "\x00" in value or "\\" in value:
            raise ProjectArchiveError("归档相对路径无效")
        normalized = value.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if (
            not normalized
            or normalized.startswith("/")
            or PureWindowsPath(normalized).drive
            or any(part in {"", ".", ".."} for part in parts)
            or any(not cls._safe_windows_component(part) for part in parts)
        ):
            raise ProjectArchiveError("归档相对路径无效")
        return PurePosixPath(normalized).as_posix()

    @staticmethod
    def _safe_windows_component(value: str) -> bool:
        if (
            not value
            or value[-1] in {" ", "."}
            or any(ord(character) < 32 for character in value)
            or any(character in '<>:"/\\|?*' for character in value)
        ):
            return False
        stem = value.split(".", 1)[0].upper()
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        return stem not in reserved

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _valid_sha256(value: object) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    @staticmethod
    def _content_sha256(value: dict[str, Any]) -> str:
        payload = {key: item for key, item in value.items() if key != "content_sha256"}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["ProjectArchiveError", "ProjectArchiveService"]
