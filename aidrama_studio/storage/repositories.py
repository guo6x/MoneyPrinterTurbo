from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aidrama_studio.domain import (
    AspectRatio,
    Project,
    ProjectStatus,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
)
from aidrama_studio.domain.script import ScriptRevisionStatus
from aidrama_studio.domain.shot import ShotPlan, ShotRevisionStatus
from aidrama_studio.domain.reference_asset import ReferenceAsset, ReferenceAssetBinding, ReferenceAssetType, ReferenceAssetVersion, ReferenceBindingType
from aidrama_studio.domain.production import ProductionAttempt, ProductionAttemptStatus, ProductionJob, ProductionJobStatus, ProductionShot, ProductionShotStatus
from aidrama_studio.domain.production_execution import ProductionArtifact, ProductionEvent, ProductionEventType, ProductionExecution, ProductionExecutionStatus
from aidrama_studio.domain.production_snapshot import ProductionInputSnapshot
from aidrama_studio.domain.production_qc import ProductionQCMetric, ProductionQCResult, ProductionReview

from .database import DatabasePaths, connect, initialize_database


class ProjectRepository:
    def __init__(self, paths: DatabasePaths | None = None):
        self.paths = initialize_database(paths)

    @staticmethod
    def _from_row(row) -> Project:
        return Project(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            status=ProjectStatus(row["status"]),
            aspect_ratio=AspectRatio(row["aspect_ratio"]),
            target_duration_seconds=row["target_duration_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_project(self, project: Project) -> Project:
        project.validate()
        with connect(self.paths.database) as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, title, description, status, aspect_ratio,
                    target_duration_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.title,
                    project.description,
                    project.status.value,
                    project.aspect_ratio.value,
                    project.target_duration_seconds,
                    project.created_at,
                    project.updated_at,
                ),
            )
        return project

    def get_project(self, project_id: str) -> Project | None:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        with connect(self.paths.database) as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def update_project(self, project: Project) -> Project:
        project.validate()
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                """
                UPDATE projects
                SET title = ?, description = ?, status = ?, aspect_ratio = ?,
                    target_duration_seconds = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    project.title,
                    project.description,
                    project.status.value,
                    project.aspect_ratio.value,
                    project.target_duration_seconds,
                    project.updated_at,
                    project.id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"项目不存在: {project.id}")
        return project

    def delete_project(self, project_id: str) -> bool:
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
        return cursor.rowcount == 1

    def project_directory(self, project_id: str) -> Path:
        return self.paths.projects / project_id

    def _project_exists(self, connection, project_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _revision_from_row(row) -> dict[str, Any]:
        content = json.loads(row["content_json"])
        generation_input = (
            json.loads(row["generation_input_json"])
            if row["generation_input_json"]
            else None
        )
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "version": row["version"],
            "status": StoryRevisionStatus(row["status"]),
            "content": StoryBible.model_validate(content),
            "generation_input": generation_input,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_story_revision(
        self,
        *,
        revision_id: str,
        project_id: str,
        version: int,
        status: StoryRevisionStatus,
        content: StoryBible,
        generation_input: dict[str, Any] | None,
        created_at: str,
        updated_at: str,
    ) -> dict[str, Any]:
        content_json = json.dumps(
            content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )
        input_json = (
            json.dumps(generation_input, ensure_ascii=False, sort_keys=True)
            if generation_input is not None
            else None
        )
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, project_id):
                raise KeyError(f"项目不存在: {project_id}")
            connection.execute(
                """
                INSERT INTO story_bible_revisions(
                    id, project_id, version, status, content_json,
                    generation_input_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    project_id,
                    version,
                    status.value,
                    content_json,
                    input_json,
                    created_at,
                    updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(row)

    def update_story_revision(
        self,
        revision_id: str,
        *,
        content: StoryBible,
        updated_at: str,
        generation_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content_json = json.dumps(
            content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )
        input_json = (
            json.dumps(generation_input, ensure_ascii=False, sort_keys=True)
            if generation_input is not None
            else None
        )
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Story Bible revision 不存在: {revision_id}")
            if row["status"] != StoryRevisionStatus.DRAFT.value:
                raise ValueError("只有 DRAFT revision 可以直接保存")
            connection.execute(
                """
                UPDATE story_bible_revisions
                SET content_json = ?, generation_input_json = COALESCE(?, generation_input_json),
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    content_json,
                    input_json,
                    updated_at,
                    revision_id,
                    StoryRevisionStatus.DRAFT.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(updated)

    def get_story_revision(self, revision_id: str) -> dict[str, Any] | None:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def get_latest_story_revision(self, project_id: str) -> dict[str, Any] | None:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                """
                SELECT * FROM story_bible_revisions
                WHERE project_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def list_story_revisions(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.paths.database) as connection:
            rows = connection.execute(
                """
                SELECT * FROM story_bible_revisions
                WHERE project_id = ?
                ORDER BY version DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def approve_story_revision(
        self, revision_id: str, *, updated_at: str
    ) -> dict[str, Any]:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Story Bible revision 不存在: {revision_id}")
            if row["status"] == StoryRevisionStatus.SUPERSEDED.value:
                raise ValueError("已被替代的 revision 不能批准")
            connection.execute(
                """
                UPDATE story_bible_revisions
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND status = ? AND id <> ?
                """,
                (
                    StoryRevisionStatus.SUPERSEDED.value,
                    updated_at,
                    row["project_id"],
                    StoryRevisionStatus.APPROVED.value,
                    revision_id,
                ),
            )
            connection.execute(
                """
                UPDATE story_bible_revisions
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (StoryRevisionStatus.APPROVED.value, updated_at, revision_id),
            )
            project = connection.execute(
                "SELECT status FROM projects WHERE id = ?", (row["project_id"],)
            ).fetchone()
            if project and project["status"] == ProjectStatus.DRAFT.value:
                connection.execute(
                    "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                    (ProjectStatus.STORY.value, updated_at, row["project_id"]),
                )
            approved = connection.execute(
                "SELECT * FROM story_bible_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(approved)

    def delete_story_revision(self, revision_id: str) -> bool:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT status FROM story_bible_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] != StoryRevisionStatus.DRAFT.value:
                raise ValueError("只有 DRAFT revision 可以删除")
            cursor = connection.execute(
                "DELETE FROM story_bible_revisions WHERE id = ?", (revision_id,)
            )
        return cursor.rowcount == 1

    @staticmethod
    def _script_revision_from_row(row) -> dict[str, Any]:
        return {
            "id": row["id"], "project_id": row["project_id"], "version": row["version"],
            "status": ScriptRevisionStatus(row["status"]),
            "source_story_revision_id": row["source_story_revision_id"],
            "content": StructuredScript.model_validate(json.loads(row["content_json"])),
            "generation_input": json.loads(row["generation_input_json"]) if row["generation_input_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def create_script_revision(self, *, revision_id: str, project_id: str, version: int,
                               status: ScriptRevisionStatus, source_story_revision_id: str,
                               content: StructuredScript, generation_input: dict[str, Any] | None,
                               created_at: str, updated_at: str) -> dict[str, Any]:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, project_id):
                raise KeyError(f"项目不存在: {project_id}")
            connection.execute("""INSERT INTO structured_script_revisions
                (id, project_id, version, status, source_story_revision_id, content_json,
                 generation_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (revision_id, project_id, version, status.value, source_story_revision_id,
                 json.dumps(content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                 json.dumps(generation_input, ensure_ascii=False, sort_keys=True) if generation_input is not None else None,
                 created_at, updated_at))
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
        return self._script_revision_from_row(row)

    def get_script_revision(self, revision_id: str) -> dict[str, Any] | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
        return self._script_revision_from_row(row) if row else None

    def list_script_revisions(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM structured_script_revisions WHERE project_id = ? ORDER BY version DESC", (project_id,)).fetchall()
        return [self._script_revision_from_row(row) for row in rows]

    def update_script_revision(self, revision_id: str, *, content: StructuredScript, updated_at: str,
                               generation_input: dict[str, Any] | None = None) -> dict[str, Any]:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
            if row is None: raise KeyError(f"Structured Script revision 不存在: {revision_id}")
            if row["status"] != ScriptRevisionStatus.DRAFT.value: raise ValueError("只有 DRAFT revision 可以直接保存")
            connection.execute("UPDATE structured_script_revisions SET content_json=?, generation_input_json=COALESCE(?, generation_input_json), updated_at=? WHERE id=?",
                (json.dumps(content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                 json.dumps(generation_input, ensure_ascii=False, sort_keys=True) if generation_input is not None else None, updated_at, revision_id))
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
        return self._script_revision_from_row(row)

    def approve_script_revision(self, revision_id: str, *, updated_at: str) -> dict[str, Any]:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
            if row is None: raise KeyError("Structured Script revision 不存在")
            if row["status"] == ScriptRevisionStatus.SUPERSEDED.value: raise ValueError("已被替代的 revision 不能批准")
            connection.execute("UPDATE structured_script_revisions SET status=?, updated_at=? WHERE project_id=? AND status=? AND id<>?",
                (ScriptRevisionStatus.SUPERSEDED.value, updated_at, row["project_id"], ScriptRevisionStatus.APPROVED.value, revision_id))
            connection.execute("UPDATE structured_script_revisions SET status=?, updated_at=? WHERE id=?", (ScriptRevisionStatus.APPROVED.value, updated_at, revision_id))
            project = connection.execute("SELECT status FROM projects WHERE id=?", (row["project_id"],)).fetchone()
            if project and project["status"] in (ProjectStatus.DRAFT.value, ProjectStatus.STORY.value):
                connection.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (ProjectStatus.PREPRODUCTION.value, updated_at, row["project_id"]))
            row = connection.execute("SELECT * FROM structured_script_revisions WHERE id = ?", (revision_id,)).fetchone()
        return self._script_revision_from_row(row)

    @staticmethod
    def _shot_revision_from_row(row) -> dict[str, Any]:
        return {"id":row["id"],"project_id":row["project_id"],"version":row["version"],"status":ShotRevisionStatus(row["status"]),"source_script_revision_id":row["source_script_revision_id"],"content":ShotPlan.model_validate(json.loads(row["content_json"])),"generation_input":json.loads(row["generation_input_json"]) if row["generation_input_json"] else None,"created_at":row["created_at"],"updated_at":row["updated_at"]}
    def create_shot_revision(self, *, revision_id, project_id, version, status, source_script_revision_id, content, generation_input, created_at, updated_at):
        with connect(self.paths.database) as c:
            if not self._project_exists(c, project_id): raise KeyError(f"项目不存在: {project_id}")
            c.execute("INSERT INTO shot_plan_revisions(id,project_id,version,status,source_script_revision_id,content_json,generation_input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",(revision_id,project_id,version,status.value,source_script_revision_id,json.dumps(content.model_dump(mode="json"),ensure_ascii=False,sort_keys=True),json.dumps(generation_input,ensure_ascii=False,sort_keys=True) if generation_input is not None else None,created_at,updated_at))
            row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
        return self._shot_revision_from_row(row)
    def get_shot_revision(self, revision_id):
        with connect(self.paths.database) as c: row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
        return self._shot_revision_from_row(row) if row else None
    def list_shot_revisions(self, project_id):
        with connect(self.paths.database) as c: rows=c.execute("SELECT * FROM shot_plan_revisions WHERE project_id=? ORDER BY version DESC",(project_id,)).fetchall()
        return [self._shot_revision_from_row(x) for x in rows]
    def update_shot_revision(self, revision_id, *, content, updated_at, generation_input=None):
        with connect(self.paths.database) as c:
            row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
            if row is None: raise KeyError("Shot Plan revision 不存在")
            if row["status"] != ShotRevisionStatus.DRAFT.value: raise ValueError("只有 DRAFT revision 可以直接保存")
            c.execute("UPDATE shot_plan_revisions SET content_json=?,generation_input_json=COALESCE(?,generation_input_json),updated_at=? WHERE id=?",(json.dumps(content.model_dump(mode="json"),ensure_ascii=False,sort_keys=True),json.dumps(generation_input,ensure_ascii=False,sort_keys=True) if generation_input is not None else None,updated_at,revision_id)); row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
        return self._shot_revision_from_row(row)
    def approve_shot_revision(self, revision_id, *, updated_at):
        with connect(self.paths.database) as c:
            row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
            if row is None: raise KeyError("Shot Plan revision 不存在")
            if row["status"] == ShotRevisionStatus.SUPERSEDED.value: raise ValueError("已被替代的 revision 不能批准")
            c.execute("UPDATE shot_plan_revisions SET status=?,updated_at=? WHERE project_id=? AND status=? AND id<>?",(ShotRevisionStatus.SUPERSEDED.value,updated_at,row["project_id"],ShotRevisionStatus.APPROVED.value,revision_id)); c.execute("UPDATE shot_plan_revisions SET status=?,updated_at=? WHERE id=?",(ShotRevisionStatus.APPROVED.value,updated_at,revision_id)); row=c.execute("SELECT * FROM shot_plan_revisions WHERE id=?",(revision_id,)).fetchone()
        return self._shot_revision_from_row(row)

    @staticmethod
    def _reference_asset_from_row(row) -> ReferenceAsset:
        return ReferenceAsset(
            id=row["id"], project_id=row["project_id"], asset_type=row["asset_type"],
            current_version_id=row["current_version_id"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _reference_version_from_row(row) -> ReferenceAssetVersion:
        return ReferenceAssetVersion(
            id=row["id"], asset_id=row["asset_id"], project_id=row["project_id"],
            version_number=row["version_number"], filename=row["filename"],
            mime_type=row["mime_type"], size_bytes=row["size_bytes"], sha256=row["sha256"],
            storage_path=row["storage_path"], metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _reference_binding_from_row(row) -> ReferenceAssetBinding:
        return ReferenceAssetBinding(
            id=row["id"], project_id=row["project_id"], asset_version_id=row["asset_version_id"],
            binding_type=row["binding_type"], binding_id=row["binding_id"],
            created_at=row["created_at"],
        )

    def create_reference_asset(self, asset: ReferenceAsset) -> ReferenceAsset:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, asset.project_id):
                raise KeyError(f"项目不存在: {asset.project_id}")
            connection.execute(
                "INSERT INTO reference_assets(id,project_id,asset_type,current_version_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (asset.id, asset.project_id, asset.asset_type.value, asset.current_version_id, asset.created_at, asset.updated_at),
            )
        return self.get_reference_asset(asset.id)

    def get_reference_asset(self, asset_id: str) -> ReferenceAsset | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_assets WHERE id=?", (asset_id,)).fetchone()
        return self._reference_asset_from_row(row) if row else None

    def list_reference_assets(self, project_id: str) -> list[ReferenceAsset]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM reference_assets WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._reference_asset_from_row(row) for row in rows]

    def create_reference_asset_version(self, version: ReferenceAssetVersion) -> ReferenceAssetVersion:
        with connect(self.paths.database) as connection:
            asset = connection.execute("SELECT project_id FROM reference_assets WHERE id=?", (version.asset_id,)).fetchone()
            if asset is None: raise KeyError(f"ReferenceAsset 不存在: {version.asset_id}")
            if asset["project_id"] != version.project_id: raise ValueError("asset 不属于该项目")
            connection.execute(
                "INSERT INTO reference_asset_versions(id,asset_id,project_id,version_number,filename,mime_type,size_bytes,sha256,storage_path,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (version.id, version.asset_id, version.project_id, version.version_number, version.filename, version.mime_type, version.size_bytes, version.sha256, version.storage_path, json.dumps(version.metadata, ensure_ascii=False, sort_keys=True), version.created_at),
            )
        return self.get_reference_asset_version(version.id)

    def get_reference_asset_version(self, version_id: str) -> ReferenceAssetVersion | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_asset_versions WHERE id=?", (version_id,)).fetchone()
        return self._reference_version_from_row(row) if row else None

    def list_reference_asset_versions(self, asset_id: str) -> list[ReferenceAssetVersion]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM reference_asset_versions WHERE asset_id=? ORDER BY version_number", (asset_id,)).fetchall()
        return [self._reference_version_from_row(row) for row in rows]

    def find_reference_version_by_hash(self, project_id: str, sha256: str) -> ReferenceAssetVersion | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_asset_versions WHERE project_id=? AND sha256=? LIMIT 1", (project_id, sha256)).fetchone()
        return self._reference_version_from_row(row) if row else None

    def set_current_reference_version(self, asset_id: str, version_id: str, *, updated_at: str) -> ReferenceAsset:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT asset_id FROM reference_asset_versions WHERE id=?", (version_id,)).fetchone()
            if row is None or row["asset_id"] != asset_id: raise ValueError("version 不属于该 asset")
            connection.execute("UPDATE reference_assets SET current_version_id=?,updated_at=? WHERE id=?", (version_id, updated_at, asset_id))
        asset = self.get_reference_asset(asset_id)
        if asset is None: raise KeyError(f"ReferenceAsset 不存在: {asset_id}")
        return asset

    def create_reference_binding(self, binding: ReferenceAssetBinding) -> ReferenceAssetBinding:
        with connect(self.paths.database) as connection:
            version = connection.execute("SELECT project_id FROM reference_asset_versions WHERE id=?", (binding.asset_version_id,)).fetchone()
            if version is None: raise KeyError(f"ReferenceAssetVersion 不存在: {binding.asset_version_id}")
            if version["project_id"] != binding.project_id: raise ValueError("version 不属于该项目")
            connection.execute(
                "INSERT INTO reference_asset_bindings(id,project_id,asset_version_id,binding_type,binding_id,created_at) VALUES (?,?,?,?,?,?)",
                (binding.id, binding.project_id, binding.asset_version_id, binding.binding_type.value, binding.binding_id, binding.created_at),
            )
        return self.get_reference_binding(binding.id)

    def get_reference_binding(self, binding_id: str) -> ReferenceAssetBinding | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_asset_bindings WHERE id=?", (binding_id,)).fetchone()
        return self._reference_binding_from_row(row) if row else None

    def list_reference_bindings(self, project_id: str, *, asset_version_id: str | None = None) -> list[ReferenceAssetBinding]:
        query = "SELECT * FROM reference_asset_bindings WHERE project_id=?"
        args: list[str] = [project_id]
        if asset_version_id:
            query += " AND asset_version_id=?"; args.append(asset_version_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection: rows = connection.execute(query, args).fetchall()
        return [self._reference_binding_from_row(row) for row in rows]

    @staticmethod
    def _production_job_from_row(row) -> ProductionJob:
        return ProductionJob(
            id=row["id"], project_id=row["project_id"], shot_plan_revision_id=row["shot_plan_revision_id"],
            status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _production_shot_from_row(row) -> ProductionShot:
        return ProductionShot(
            id=row["id"], production_job_id=row["production_job_id"], shot_id=row["shot_id"],
            order_index=row["order_index"], status=row["status"], created_at=row["created_at"],
        )

    @staticmethod
    def _production_attempt_from_row(row) -> ProductionAttempt:
        return ProductionAttempt(
            id=row["id"], production_shot_id=row["production_shot_id"], attempt_number=row["attempt_number"],
            status=row["status"], runtime_adapter=row["runtime_adapter"], runtime_reference=row["runtime_reference"],
            input_snapshot_json=json.loads(row["input_snapshot_json"]),
            output_artifact_json=json.loads(row["output_artifact_json"]) if row["output_artifact_json"] else None,
            error_message=row["error_message"], created_at=row["created_at"],
        )

    def create_production_job(self, job: ProductionJob) -> ProductionJob:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, job.project_id):
                raise KeyError(f"项目不存在: {job.project_id}")
            plan = connection.execute("SELECT project_id FROM shot_plan_revisions WHERE id=?", (job.shot_plan_revision_id,)).fetchone()
            if plan is None:
                raise KeyError(f"Shot Plan revision 不存在: {job.shot_plan_revision_id}")
            if plan["project_id"] != job.project_id:
                raise ValueError("Shot Plan revision 不属于该项目")
            connection.execute(
                "INSERT INTO production_jobs(id,project_id,shot_plan_revision_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (job.id, job.project_id, job.shot_plan_revision_id, job.status.value, job.created_at, job.updated_at),
            )
        return self.get_production_job(job.id)

    def get_production_job(self, job_id: str) -> ProductionJob | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
        return self._production_job_from_row(row) if row else None

    def list_production_jobs(self, project_id: str) -> list[ProductionJob]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_jobs WHERE project_id=? ORDER BY created_at DESC,id", (project_id,)).fetchall()
        return [self._production_job_from_row(row) for row in rows]

    def update_production_job_status(self, job_id: str, status: ProductionJobStatus, *, updated_at: str) -> ProductionJob:
        with connect(self.paths.database) as connection:
            cursor = connection.execute("UPDATE production_jobs SET status=?,updated_at=? WHERE id=?", (status.value, updated_at, job_id))
            if cursor.rowcount != 1:
                raise KeyError(f"ProductionJob 不存在: {job_id}")
        return self.get_production_job(job_id)

    def create_production_shot(self, shot: ProductionShot) -> ProductionShot:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_jobs WHERE id=?", (shot.production_job_id,)).fetchone() is None:
                raise KeyError(f"ProductionJob 不存在: {shot.production_job_id}")
            connection.execute(
                "INSERT INTO production_shots(id,production_job_id,shot_id,order_index,status,created_at) VALUES (?,?,?,?,?,?)",
                (shot.id, shot.production_job_id, shot.shot_id, shot.order_index, shot.status.value, shot.created_at),
            )
        return self.get_production_shot(shot.id)

    def get_production_shot(self, shot_id: str) -> ProductionShot | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_shots WHERE id=?", (shot_id,)).fetchone()
        return self._production_shot_from_row(row) if row else None

    def list_production_shots(self, job_id: str) -> list[ProductionShot]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_shots WHERE production_job_id=? ORDER BY order_index,id", (job_id,)).fetchall()
        return [self._production_shot_from_row(row) for row in rows]

    def update_production_shot_status(self, shot_id: str, status: ProductionShotStatus) -> ProductionShot:
        with connect(self.paths.database) as connection:
            cursor = connection.execute("UPDATE production_shots SET status=? WHERE id=?", (status.value, shot_id))
            if cursor.rowcount != 1:
                raise KeyError(f"ProductionShot 不存在: {shot_id}")
        return self.get_production_shot(shot_id)

    def create_production_attempt(self, attempt: ProductionAttempt) -> ProductionAttempt:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_shots WHERE id=?", (attempt.production_shot_id,)).fetchone() is None:
                raise KeyError(f"ProductionShot 不存在: {attempt.production_shot_id}")
            connection.execute(
                "INSERT INTO production_attempts(id,production_shot_id,attempt_number,status,runtime_adapter,runtime_reference,input_snapshot_json,output_artifact_json,error_message,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.id, attempt.production_shot_id, attempt.attempt_number, attempt.status.value,
                    attempt.runtime_adapter, attempt.runtime_reference,
                    json.dumps(attempt.input_snapshot_json, ensure_ascii=False, sort_keys=True),
                    json.dumps(attempt.output_artifact_json, ensure_ascii=False, sort_keys=True) if attempt.output_artifact_json is not None else None,
                    attempt.error_message, attempt.created_at,
                ),
            )
        return self.get_production_attempt(attempt.id)

    def get_production_attempt(self, attempt_id: str) -> ProductionAttempt | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._production_attempt_from_row(row) if row else None

    def list_production_attempts(self, production_shot_id: str) -> list[ProductionAttempt]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_attempts WHERE production_shot_id=? ORDER BY attempt_number", (production_shot_id,)).fetchall()
        return [self._production_attempt_from_row(row) for row in rows]

    def update_production_attempt(
        self,
        attempt_id: str,
        *,
        status: ProductionAttemptStatus,
        runtime_reference: str | None = None,
        output_artifact_json: dict[str, object] | None = None,
        error_message: str | None = None,
    ) -> ProductionAttempt:
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                "UPDATE production_attempts SET status=?,runtime_reference=?,output_artifact_json=?,error_message=? WHERE id=?",
                (
                    status.value, runtime_reference,
                    json.dumps(output_artifact_json, ensure_ascii=False, sort_keys=True) if output_artifact_json is not None else None,
                    error_message, attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"ProductionAttempt 不存在: {attempt_id}")
        return self.get_production_attempt(attempt_id)

    @staticmethod
    def _production_execution_from_row(row) -> ProductionExecution:
        return ProductionExecution(
            id=row["id"], production_job_id=row["production_job_id"], status=row["status"],
            worker_type=row["worker_type"], started_at=row["started_at"], finished_at=row["finished_at"],
            created_at=row["created_at"],
            input_snapshot=ProductionInputSnapshot.model_validate(json.loads(row["input_snapshot_json"]))
            if row["input_snapshot_json"] else None,
        )

    @staticmethod
    def _production_event_from_row(row) -> ProductionEvent:
        return ProductionEvent(
            id=row["id"], execution_id=row["execution_id"], event_type=row["event_type"],
            payload_json=json.loads(row["payload_json"]), created_at=row["created_at"],
        )

    @staticmethod
    def _production_artifact_from_row(row) -> ProductionArtifact:
        return ProductionArtifact(
            id=row["id"], execution_id=row["execution_id"], artifact_type=row["artifact_type"],
            path=row["path"], metadata_json=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )

    def create_production_execution(self, execution: ProductionExecution) -> ProductionExecution:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_jobs WHERE id=?", (execution.production_job_id,)).fetchone() is None:
                raise KeyError(f"ProductionJob 不存在: {execution.production_job_id}")
            connection.execute(
                "INSERT INTO production_executions(id,production_job_id,status,worker_type,started_at,finished_at,created_at,input_snapshot_json) VALUES (?,?,?,?,?,?,?,?)",
                (execution.id, execution.production_job_id, execution.status.value, execution.worker_type, execution.started_at, execution.finished_at, execution.created_at,
                 json.dumps(execution.input_snapshot.to_json_dict(), ensure_ascii=False, sort_keys=True) if execution.input_snapshot else None),
            )
        return self.get_production_execution(execution.id)

    def get_production_execution(self, execution_id: str) -> ProductionExecution | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_executions WHERE id=?", (execution_id,)).fetchone()
        return self._production_execution_from_row(row) if row else None

    def list_production_executions(self, job_id: str) -> list[ProductionExecution]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_executions WHERE production_job_id=? ORDER BY created_at,rowid", (job_id,)).fetchall()
        return [self._production_execution_from_row(row) for row in rows]

    def update_production_execution(
        self,
        execution_id: str,
        *,
        status: ProductionExecutionStatus,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ProductionExecution:
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                "UPDATE production_executions SET status=?,started_at=COALESCE(?,started_at),finished_at=COALESCE(?,finished_at) WHERE id=?",
                (status.value, started_at, finished_at, execution_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"ProductionExecution 不存在: {execution_id}")
        return self.get_production_execution(execution_id)

    def create_production_event(self, event: ProductionEvent) -> ProductionEvent:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_executions WHERE id=?", (event.execution_id,)).fetchone() is None:
                raise KeyError(f"ProductionExecution 不存在: {event.execution_id}")
            connection.execute(
                "INSERT INTO production_events(id,execution_id,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
                (event.id, event.execution_id, event.event_type.value, json.dumps(event.payload_json, ensure_ascii=False, sort_keys=True), event.created_at),
            )
        return self.get_production_event(event.id)

    def get_production_event(self, event_id: str) -> ProductionEvent | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_events WHERE id=?", (event_id,)).fetchone()
        return self._production_event_from_row(row) if row else None

    def list_production_events(self, execution_id: str) -> list[ProductionEvent]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_events WHERE execution_id=? ORDER BY created_at,rowid", (execution_id,)).fetchall()
        return [self._production_event_from_row(row) for row in rows]

    def create_production_artifact(self, artifact: ProductionArtifact) -> ProductionArtifact:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_executions WHERE id=?", (artifact.execution_id,)).fetchone() is None:
                raise KeyError(f"ProductionExecution 不存在: {artifact.execution_id}")
            connection.execute(
                "INSERT INTO production_artifacts(id,execution_id,artifact_type,path,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
                (artifact.id, artifact.execution_id, artifact.artifact_type, artifact.path, json.dumps(artifact.metadata_json, ensure_ascii=False, sort_keys=True), artifact.created_at),
            )
        return self.get_production_artifact(artifact.id)

    def get_production_artifact(self, artifact_id: str) -> ProductionArtifact | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._production_artifact_from_row(row) if row else None

    def list_production_artifacts(self, execution_id: str) -> list[ProductionArtifact]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_artifacts WHERE execution_id=? ORDER BY created_at,rowid", (execution_id,)).fetchall()
        return [self._production_artifact_from_row(row) for row in rows]

    @staticmethod
    def _production_qc_result_from_row(row) -> ProductionQCResult:
        return ProductionQCResult(
            id=row["id"], project_id=row["project_id"], execution_id=row["execution_id"],
            artifact_id=row["artifact_id"], status=row["status"], report_path=row["report_path"],
            summary_json=json.loads(row["summary_json"]), started_at=row["started_at"],
            finished_at=row["finished_at"], created_at=row["created_at"],
        )

    @staticmethod
    def _production_qc_metric_from_row(row) -> ProductionQCMetric:
        return ProductionQCMetric(
            id=row["id"], result_id=row["result_id"], metric_name=row["metric_name"],
            category=row["category"], status=row["status"], value_json=json.loads(row["value_json"]),
            message=row["message"], created_at=row["created_at"],
        )

    @staticmethod
    def _production_review_from_row(row) -> ProductionReview:
        return ProductionReview(
            id=row["id"], project_id=row["project_id"], qc_result_id=row["qc_result_id"],
            decision=row["decision"], reviewer=row["reviewer"], notes=row["notes"],
            created_at=row["created_at"],
        )

    def create_production_qc_result(self, result: ProductionQCResult) -> ProductionQCResult:
        with connect(self.paths.database) as connection:
            execution = connection.execute(
                "SELECT pj.project_id FROM production_executions pe JOIN production_jobs pj ON pj.id=pe.production_job_id WHERE pe.id=?",
                (result.execution_id,),
            ).fetchone()
            if execution is None or execution["project_id"] != result.project_id:
                raise KeyError("ProductionQCResult execution 不属于该项目")
            if result.artifact_id is not None:
                artifact = connection.execute(
                    "SELECT execution_id FROM production_artifacts WHERE id=?", (result.artifact_id,)
                ).fetchone()
                if artifact is None or artifact["execution_id"] != result.execution_id:
                    raise KeyError("ProductionQCResult artifact 不属于该 execution")
            connection.execute(
                "INSERT INTO production_qc_results(id,project_id,execution_id,artifact_id,status,report_path,summary_json,started_at,finished_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    result.id, result.project_id, result.execution_id, result.artifact_id, result.status.value,
                    result.report_path, json.dumps(result.summary_json, ensure_ascii=False, sort_keys=True),
                    result.started_at, result.finished_at, result.created_at,
                ),
            )
        return self.get_production_qc_result(result.id)

    def get_production_qc_result(self, result_id: str) -> ProductionQCResult | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_qc_results WHERE id=?", (result_id,)).fetchone()
        return self._production_qc_result_from_row(row) if row else None

    def list_production_qc_results(self, project_id: str, execution_id: str | None = None) -> list[ProductionQCResult]:
        query = "SELECT * FROM production_qc_results WHERE project_id=?"
        params: list[object] = [project_id]
        if execution_id is not None:
            query += " AND execution_id=?"
            params.append(execution_id)
        query += " ORDER BY created_at,rowid"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._production_qc_result_from_row(row) for row in rows]

    def update_production_qc_result(
        self,
        result_id: str,
        *,
        status,
        report_path: str | None = None,
        summary_json: dict[str, object] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ProductionQCResult:
        current = self.get_production_qc_result(result_id)
        if current is None:
            raise KeyError(f"ProductionQCResult 不存在: {result_id}")
        with connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE production_qc_results SET status=?,report_path=COALESCE(?,report_path),summary_json=?,started_at=COALESCE(?,started_at),finished_at=COALESCE(?,finished_at) WHERE id=?",
                (
                    status.value, report_path,
                    json.dumps(summary_json if summary_json is not None else current.summary_json, ensure_ascii=False, sort_keys=True),
                    started_at, finished_at, result_id,
                ),
            )
        return self.get_production_qc_result(result_id)

    def create_production_qc_metric(self, metric: ProductionQCMetric) -> ProductionQCMetric:
        with connect(self.paths.database) as connection:
            if connection.execute("SELECT 1 FROM production_qc_results WHERE id=?", (metric.result_id,)).fetchone() is None:
                raise KeyError(f"ProductionQCResult 不存在: {metric.result_id}")
            connection.execute(
                "INSERT INTO production_qc_metrics(id,result_id,metric_name,category,status,value_json,message,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    metric.id, metric.result_id, metric.metric_name, metric.category, metric.status.value,
                    json.dumps(metric.value_json, ensure_ascii=False, sort_keys=True), metric.message, metric.created_at,
                ),
            )
        return self.get_production_qc_metric(metric.id)

    def get_production_qc_metric(self, metric_id: str) -> ProductionQCMetric | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_qc_metrics WHERE id=?", (metric_id,)).fetchone()
        return self._production_qc_metric_from_row(row) if row else None

    def list_production_qc_metrics(self, result_id: str) -> list[ProductionQCMetric]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM production_qc_metrics WHERE result_id=? ORDER BY created_at,rowid", (result_id,)).fetchall()
        return [self._production_qc_metric_from_row(row) for row in rows]

    def create_production_review(self, review: ProductionReview) -> ProductionReview:
        with connect(self.paths.database) as connection:
            result = connection.execute(
                "SELECT project_id FROM production_qc_results WHERE id=?", (review.qc_result_id,)
            ).fetchone()
            if result is None or result["project_id"] != review.project_id:
                raise KeyError("ProductionReview QC result 不属于该项目")
            connection.execute(
                "INSERT INTO production_reviews(id,project_id,qc_result_id,decision,reviewer,notes,created_at) VALUES (?,?,?,?,?,?,?)",
                (review.id, review.project_id, review.qc_result_id, review.decision.value, review.reviewer, review.notes, review.created_at),
            )
        return self.get_production_review(review.id)

    def get_production_review(self, review_id: str) -> ProductionReview | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM production_reviews WHERE id=?", (review_id,)).fetchone()
        return self._production_review_from_row(row) if row else None

    def list_production_reviews(self, project_id: str, qc_result_id: str | None = None) -> list[ProductionReview]:
        query = "SELECT * FROM production_reviews WHERE project_id=?"
        params: list[object] = [project_id]
        if qc_result_id is not None:
            query += " AND qc_result_id=?"
            params.append(qc_result_id)
        query += " ORDER BY created_at,rowid"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._production_review_from_row(row) for row in rows]
