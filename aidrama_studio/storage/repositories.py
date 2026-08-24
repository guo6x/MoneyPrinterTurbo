from __future__ import annotations

import json
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath
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
from aidrama_studio.domain.final_assembly import (
    FinalAssembly,
    FinalAssemblyItem,
    FinalAssemblyStatus,
    FinalAssemblyRenderAttempt,
    FinalAssemblyRenderAttemptStatus,
)
from aidrama_studio.domain.post_production import (
    AudioMixConfig,
    MusicTrack,
    PostProductionPlan,
    PostRenderAttempt,
    PostRenderAttemptStatus,
    SubtitleCue,
    SubtitleTrack,
    VoiceTrack,
)
from aidrama_studio.domain.director import (
    DirectorDecision,
    DirectorDecisionEvent,
    DirectorDecisionStatus,
    DirectorGoal,
    DirectorGoalKind,
    DirectorGoalStatus,
    DirectorRecommendation,
    DirectorSession,
    DirectorSessionStatus,
)
from aidrama_studio.domain.runtime_foundation import AIInvocation, GenerationBrief, OutputProfile, RuntimePlan
from aidrama_studio.domain.creative_intake import ExtractionState, IntakeAnalysis, NormalizedCreativeBrief, SourceKind, SourcePackItem
from aidrama_studio.domain.reference_profile import ReferenceProfile, ReferenceProfileItem
from aidrama_studio.domain.runtime_operations import CapabilityProfile, ProviderTask, VisionAnalysisRecord, VisionFrameManifest

from .database import DatabasePaths, connect, initialize_database, transaction


class ProjectRepository:
    def __init__(self, paths: DatabasePaths | None = None):
        self.paths = initialize_database(paths)

    def transaction(self):
        """Return an explicit transaction scoped to this repository database."""
        return transaction(self.paths.database)

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
            version = connection.execute(
                "SELECT project_id,asset_id FROM reference_asset_versions WHERE id=?",
                (binding.asset_version_id,),
            ).fetchone()
            if version is None: raise KeyError(f"ReferenceAssetVersion 不存在: {binding.asset_version_id}")
            if version["project_id"] != binding.project_id: raise ValueError("version 不属于该项目")
            asset = connection.execute(
                "SELECT project_id,asset_type FROM reference_assets WHERE id=?",
                (version["asset_id"],),
            ).fetchone()
            if asset is None or asset["project_id"] != binding.project_id:
                raise ValueError("asset 不属于该项目")
            allowed = {
                ReferenceBindingType.CHARACTER: {ReferenceAssetType.CHARACTER_REFERENCE.value},
                ReferenceBindingType.LOCATION: {ReferenceAssetType.LOCATION_REFERENCE.value},
                ReferenceBindingType.SHOT: {
                    ReferenceAssetType.CHARACTER_REFERENCE.value,
                    ReferenceAssetType.LOCATION_REFERENCE.value,
                    ReferenceAssetType.STYLE_REFERENCE.value,
                    ReferenceAssetType.PROP_REFERENCE.value,
                },
            }
            if asset["asset_type"] not in allowed.get(binding.binding_type, set()):
                raise ValueError("ReferenceAsset 类型与 binding 类型不兼容")
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
            output_profile_id=row["output_profile_id"] if "output_profile_id" in row.keys() else None,
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
                "INSERT INTO production_jobs(id,project_id,shot_plan_revision_id,output_profile_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.project_id,
                    job.shot_plan_revision_id,
                    job.output_profile_id,
                    job.status.value,
                    job.created_at,
                    job.updated_at,
                ),
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

    def set_production_job_output_profile(self, job_id: str, profile_id: str) -> ProductionJob:
        with connect(self.paths.database) as connection:
            profile = connection.execute("SELECT project_id FROM output_profiles WHERE id=?", (profile_id,)).fetchone()
            job = connection.execute("SELECT project_id FROM production_jobs WHERE id=?", (job_id,)).fetchone()
            if profile is None or job is None or profile["project_id"] != job["project_id"]:
                raise ValueError("OutputProfile 不属于该 ProductionJob 项目")
            cursor = connection.execute("UPDATE production_jobs SET output_profile_id=? WHERE id=?", (profile_id, job_id))
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
            runtime_plan_id=row["runtime_plan_id"] if "runtime_plan_id" in row.keys() else None,
            generation_brief_id=row["generation_brief_id"] if "generation_brief_id" in row.keys() else None,
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
                "INSERT INTO production_executions(id,production_job_id,status,worker_type,started_at,finished_at,created_at,input_snapshot_json,runtime_plan_id,generation_brief_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (execution.id, execution.production_job_id, execution.status.value, execution.worker_type, execution.started_at, execution.finished_at, execution.created_at,
                  json.dumps(execution.input_snapshot.to_json_dict(), ensure_ascii=False, sort_keys=True) if execution.input_snapshot else None,
                  execution.runtime_plan_id, execution.generation_brief_id),
            )
        return self.get_production_execution(execution.id)

    def enqueue_production_execution_atomic(
        self,
        execution: ProductionExecution,
        *,
        job_status: ProductionJobStatus,
        event: ProductionEvent,
        attempt: ProductionAttempt | None = None,
        shot_status: ProductionShotStatus | None = None,
    ) -> ProductionExecution:
        """Create an execution, optional matching attempt, job projection and
        initial event as one durable unit.

        The optional attempt is used by the multi-shot orchestrator.  It closes
        the historical window in which an execution could be visible without
        its matching ProductionAttempt after a process crash.
        """
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT project_id FROM production_jobs WHERE id=?",
                (execution.production_job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"ProductionJob 不存在: {execution.production_job_id}")
            connection.execute(
                "INSERT INTO production_executions("
                "id,production_job_id,status,worker_type,started_at,finished_at,created_at,input_snapshot_json,"
                "runtime_plan_id,generation_brief_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    execution.id,
                    execution.production_job_id,
                    execution.status.value,
                    execution.worker_type,
                    execution.started_at,
                    execution.finished_at,
                    execution.created_at,
                    json.dumps(
                        execution.input_snapshot.to_json_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if execution.input_snapshot
                    else None,
                    execution.runtime_plan_id,
                    execution.generation_brief_id,
                ),
            )
            if attempt is not None:
                shot = connection.execute(
                    "SELECT production_job_id FROM production_shots WHERE id=?",
                    (attempt.production_shot_id,),
                ).fetchone()
                if shot is None or shot["production_job_id"] != execution.production_job_id:
                    raise ValueError("ProductionAttempt 不属于该 ProductionJob")
                connection.execute(
                    "INSERT INTO production_attempts("
                    "id,production_shot_id,attempt_number,status,runtime_adapter,runtime_reference,"
                    "input_snapshot_json,output_artifact_json,error_message,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt.id,
                        attempt.production_shot_id,
                        attempt.attempt_number,
                        attempt.status.value,
                        attempt.runtime_adapter,
                        attempt.runtime_reference,
                        json.dumps(attempt.input_snapshot_json, ensure_ascii=False, sort_keys=True),
                        json.dumps(attempt.output_artifact_json, ensure_ascii=False, sort_keys=True)
                        if attempt.output_artifact_json is not None
                        else None,
                        attempt.error_message,
                        attempt.created_at,
                    ),
                )
                if shot_status is not None:
                    connection.execute(
                        "UPDATE production_shots SET status=? WHERE id=?",
                        (shot_status.value, attempt.production_shot_id),
                    )
            connection.execute(
                "UPDATE production_jobs SET status=?,updated_at=? WHERE id=?",
                (job_status.value, event.created_at, execution.production_job_id),
            )
            connection.execute(
                "INSERT INTO production_events(id,execution_id,event_type,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    event.id,
                    event.execution_id,
                    event.event_type.value,
                    json.dumps(event.payload_json, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
        return self.get_production_execution(execution.id)

    def transition_production_execution_atomic(
        self,
        execution_id: str,
        *,
        expected_status: ProductionExecutionStatus,
        status: ProductionExecutionStatus,
        started_at: str | None = None,
        finished_at: str | None = None,
        job_status: ProductionJobStatus | None,
        event: ProductionEvent,
    ) -> ProductionExecution:
        """Update execution/job projections and append the matching event atomically."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT production_job_id,status FROM production_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"ProductionExecution 不存在: {execution_id}")
            if row["status"] != expected_status.value:
                raise ValueError(
                    f"ProductionExecution 状态已改变: expected {expected_status.value}, got {row['status']}"
                )
            cursor = connection.execute(
                "UPDATE production_executions SET status=?,started_at=COALESCE(?,started_at),"
                "finished_at=COALESCE(?,finished_at) WHERE id=? AND status=?",
                (status.value, started_at, finished_at, execution_id, expected_status.value),
            )
            if cursor.rowcount != 1:
                raise ValueError("ProductionExecution transition 失败")
            if job_status is not None:
                connection.execute(
                    "UPDATE production_jobs SET status=?,updated_at=? WHERE id=?",
                    (job_status.value, event.created_at, row["production_job_id"]),
                )
            connection.execute(
                "INSERT INTO production_events(id,execution_id,event_type,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    event.id,
                    execution_id,
                    event.event_type.value,
                    json.dumps(event.payload_json, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
        return self.get_production_execution(execution_id)

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

    @staticmethod
    def _director_recommendation(value: dict[str, object] | None) -> DirectorRecommendation | None:
        return DirectorRecommendation.model_validate(value) if value else None

    @classmethod
    def _director_session_from_row(cls, row) -> DirectorSession:
        return DirectorSession(
            id=row["id"], project_id=row["project_id"], status=DirectorSessionStatus(row["status"]),
            current_goal=DirectorGoalKind(row["current_goal"]), blocking_reason=row["blocking_reason"] or "",
            pending_recommendation=cls._director_recommendation(
                json.loads(row["pending_recommendation_json"]) if row["pending_recommendation_json"] else None
            ), created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _director_goal_from_row(row) -> DirectorGoal:
        return DirectorGoal(
            id=row["id"], session_id=row["session_id"], project_id=row["project_id"],
            goal=DirectorGoalKind(row["goal"]), status=DirectorGoalStatus(row["status"]),
            max_steps=row["max_steps"], completed_steps=row["completed_steps"],
            created_at=row["created_at"], finished_at=row["finished_at"],
        )

    def _director_decision_from_row(self, row) -> DirectorDecision:
        status = DirectorDecisionStatus(row["status"])
        # The recommendation row is immutable.  Its effective status is the
        # latest append-only lifecycle event, if any.
        try:
            event = self._connection_event_row(row["id"])
        except sqlite3.OperationalError:
            event = None
        if event is not None:
            status = DirectorDecisionStatus(event["to_status"])
        return DirectorDecision(
            id=row["id"], session_id=row["session_id"], project_id=row["project_id"], goal_id=row["goal_id"],
            status=status, project_state=row["project_state"],
            recommendation=DirectorRecommendation.model_validate(json.loads(row["recommendation_json"])),
            state_snapshot=json.loads(row["state_snapshot_json"]), created_at=row["created_at"],
        )

    def _connection_event_row(self, decision_id: str):
        with connect(self.paths.database) as connection:
            return connection.execute(
                "SELECT to_status FROM director_decision_events WHERE decision_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (decision_id,),
            ).fetchone()

    def create_director_session(self, session: DirectorSession) -> DirectorSession:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, session.project_id):
                raise KeyError(f"项目不存在: {session.project_id}")
            connection.execute(
                "INSERT INTO director_sessions(id,project_id,status,current_goal,blocking_reason,pending_recommendation_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (session.id, session.project_id, session.status.value, session.current_goal.value,
                 session.blocking_reason, json.dumps(session.pending_recommendation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                 if session.pending_recommendation else None, session.created_at, session.updated_at),
            )
        return self.get_director_session(session.id)

    def get_director_session(self, session_id: str) -> DirectorSession | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM director_sessions WHERE id=?", (session_id,)).fetchone()
        return self._director_session_from_row(row) if row else None

    def list_director_sessions(self, project_id: str) -> list[DirectorSession]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM director_sessions WHERE project_id=? ORDER BY updated_at DESC,id", (project_id,)).fetchall()
        return [self._director_session_from_row(row) for row in rows]

    def update_director_session(self, session: DirectorSession) -> DirectorSession:
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                "UPDATE director_sessions SET status=?,current_goal=?,blocking_reason=?,pending_recommendation_json=?,updated_at=? WHERE id=? AND project_id=?",
                (session.status.value, session.current_goal.value, session.blocking_reason,
                 json.dumps(session.pending_recommendation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                 if session.pending_recommendation else None, session.updated_at, session.id, session.project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"DirectorSession 不存在: {session.id}")
        return self.get_director_session(session.id)

    def transition_director(
        self,
        *,
        decision: DirectorDecision,
        event: DirectorDecisionEvent,
        goal: DirectorGoal,
        session: DirectorSession,
    ) -> DirectorDecision:
        """Atomically append a Director event and update goal/session state.

        Director lifecycle history is append-only, while the goal and session
        are projections of the latest transition.  Keeping all four writes in
        one SQLite transaction prevents a crash or fault injection from
        exposing an APPROVED decision with an old session (or vice versa).
        """
        with self.transaction() as connection:
            decision_row = connection.execute(
                "SELECT session_id, project_id, goal_id, status FROM director_decisions WHERE id=?",
                (decision.id,),
            ).fetchone()
            session_row = connection.execute(
                "SELECT project_id FROM director_sessions WHERE id=?", (session.id,)
            ).fetchone()
            goal_row = connection.execute(
                "SELECT project_id, session_id FROM director_goals WHERE id=?", (goal.id,)
            ).fetchone()
            if (
                decision_row is None
                or decision_row["session_id"] != session.id
                or decision_row["project_id"] != decision.project_id
                or decision_row["goal_id"] != goal.id
                or session_row is None
                or session_row["project_id"] != decision.project_id
                or goal_row is None
                or goal_row["project_id"] != decision.project_id
                or goal_row["session_id"] != session.id
            ):
                raise ValueError("Director transition provenance 不属于同一 project/session/goal")
            latest = connection.execute(
                "SELECT to_status FROM director_decision_events "
                "WHERE decision_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (decision.id,),
            ).fetchone()
            current = latest["to_status"] if latest is not None else decision_row["status"]
            if current != event.from_status.value:
                raise ValueError(
                    f"Director transition 无效: expected {current}, got {event.from_status.value}"
                )
            connection.execute(
                "INSERT INTO director_decision_events("
                "id,decision_id,session_id,project_id,from_status,to_status,event_type,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.decision_id,
                    event.session_id,
                    event.project_id,
                    event.from_status.value,
                    event.to_status.value,
                    event.event_type,
                    json.dumps(event.metadata, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
            connection.execute(
                "UPDATE director_decisions SET status=? WHERE id=?",
                (event.to_status.value, decision.id),
            )
            goal_cursor = connection.execute(
                "UPDATE director_goals SET status=?,max_steps=?,completed_steps=?,finished_at=? "
                "WHERE id=? AND project_id=? AND session_id=?",
                (
                    goal.status.value,
                    goal.max_steps,
                    goal.completed_steps,
                    goal.finished_at,
                    goal.id,
                    goal.project_id,
                    session.id,
                ),
            )
            if goal_cursor.rowcount != 1:
                raise KeyError(f"DirectorGoal 不存在: {goal.id}")
            session_cursor = connection.execute(
                "UPDATE director_sessions SET status=?,current_goal=?,blocking_reason=?,"
                "pending_recommendation_json=?,updated_at=? WHERE id=? AND project_id=?",
                (
                    session.status.value,
                    session.current_goal.value,
                    session.blocking_reason,
                    json.dumps(
                        session.pending_recommendation.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if session.pending_recommendation
                    else None,
                    session.updated_at,
                    session.id,
                    session.project_id,
                ),
            )
            if session_cursor.rowcount != 1:
                raise KeyError(f"DirectorSession 不存在: {session.id}")
        return self.get_director_decision(decision.id)

    def create_director_goal(self, goal: DirectorGoal) -> DirectorGoal:
        with connect(self.paths.database) as connection:
            session = connection.execute("SELECT project_id FROM director_sessions WHERE id=?", (goal.session_id,)).fetchone()
            if session is None or session["project_id"] != goal.project_id:
                raise ValueError("DirectorGoal session 不属于该项目")
            connection.execute(
                "INSERT INTO director_goals(id,session_id,project_id,goal,status,max_steps,completed_steps,created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (goal.id, goal.session_id, goal.project_id, goal.goal.value, goal.status.value, goal.max_steps,
                 goal.completed_steps, goal.created_at, goal.finished_at),
            )
        return self.get_director_goal(goal.id)

    def get_director_goal(self, goal_id: str) -> DirectorGoal | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM director_goals WHERE id=?", (goal_id,)).fetchone()
        return self._director_goal_from_row(row) if row else None

    def list_director_goals(self, session_id: str) -> list[DirectorGoal]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM director_goals WHERE session_id=? ORDER BY created_at,id", (session_id,)).fetchall()
        return [self._director_goal_from_row(row) for row in rows]

    def update_director_goal(self, goal: DirectorGoal) -> DirectorGoal:
        with connect(self.paths.database) as connection:
            cursor = connection.execute(
                "UPDATE director_goals SET status=?,max_steps=?,completed_steps=?,finished_at=? WHERE id=? AND project_id=?",
                (goal.status.value, goal.max_steps, goal.completed_steps, goal.finished_at, goal.id, goal.project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"DirectorGoal 不存在: {goal.id}")
        return self.get_director_goal(goal.id)

    def create_director_decision(self, decision: DirectorDecision) -> DirectorDecision:
        with connect(self.paths.database) as connection:
            session = connection.execute("SELECT project_id FROM director_sessions WHERE id=?", (decision.session_id,)).fetchone()
            goal = connection.execute("SELECT project_id,session_id FROM director_goals WHERE id=?", (decision.goal_id,)).fetchone()
            if session is None or goal is None or session["project_id"] != decision.project_id or goal["project_id"] != decision.project_id or goal["session_id"] != decision.session_id:
                raise ValueError("DirectorDecision provenance 不属于该项目/session")
            connection.execute(
                "INSERT INTO director_decisions(id,session_id,project_id,goal_id,status,project_state,recommendation_json,state_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (decision.id, decision.session_id, decision.project_id, decision.goal_id, decision.status.value,
                 decision.project_state, json.dumps(decision.recommendation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                 json.dumps(decision.state_snapshot, ensure_ascii=False, sort_keys=True), decision.created_at),
            )
        return self.get_director_decision(decision.id)

    def create_director_decision_event(self, event: DirectorDecisionEvent) -> DirectorDecisionEvent:
        """Append one validated lifecycle transition; never edits history."""
        with connect(self.paths.database) as connection:
            decision = connection.execute(
                "SELECT session_id,project_id,status FROM director_decisions WHERE id=?", (event.decision_id,)
            ).fetchone()
            session = connection.execute(
                "SELECT project_id FROM director_sessions WHERE id=?", (event.session_id,)
            ).fetchone()
            if decision is None or decision["session_id"] != event.session_id or decision["project_id"] != event.project_id:
                raise ValueError("DirectorDecisionEvent decision provenance 不属于该 session/project")
            if session is None or session["project_id"] != event.project_id:
                raise ValueError("DirectorDecisionEvent session 不属于该项目")
            latest = connection.execute(
                "SELECT to_status FROM director_decision_events WHERE decision_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (event.decision_id,),
            ).fetchone()
            current = latest["to_status"] if latest is not None else decision["status"]
            if current != event.from_status.value:
                raise ValueError(f"DirectorDecisionEvent transition 无效: expected {current}, got {event.from_status.value}")
            connection.execute(
                "INSERT INTO director_decision_events(id,decision_id,session_id,project_id,from_status,to_status,event_type,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (event.id, event.decision_id, event.session_id, event.project_id, event.from_status.value, event.to_status.value, event.event_type, json.dumps(event.metadata, ensure_ascii=False, sort_keys=True), event.created_at),
            )
        return event

    def list_director_decision_events(self, project_id: str, decision_id: str | None = None) -> list[DirectorDecisionEvent]:
        query = "SELECT * FROM director_decision_events WHERE project_id=?"
        params: list[object] = [project_id]
        if decision_id is not None:
            query += " AND decision_id=?"
            params.append(decision_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [DirectorDecisionEvent(id=row["id"], decision_id=row["decision_id"], session_id=row["session_id"], project_id=row["project_id"], from_status=DirectorDecisionStatus(row["from_status"]), to_status=DirectorDecisionStatus(row["to_status"]), event_type=row["event_type"], metadata=json.loads(row["metadata_json"]), created_at=row["created_at"]) for row in rows]

    def get_director_decision(self, decision_id: str) -> DirectorDecision | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM director_decisions WHERE id=?", (decision_id,)).fetchone()
        return self._director_decision_from_row(row) if row else None

    def list_director_decisions(self, project_id: str, session_id: str | None = None) -> list[DirectorDecision]:
        query = "SELECT * FROM director_decisions WHERE project_id=?"
        args: list[str] = [project_id]
        if session_id is not None:
            query += " AND session_id=?"; args.append(session_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._director_decision_from_row(row) for row in rows]

    def create_producer_recommendation_event(
        self,
        *,
        event_id: str,
        project_id: str,
        production_job_id: str | None,
        action: str,
        target_id: str | None,
        metadata: dict[str, object] | None,
        created_at: str,
    ) -> dict[str, object]:
        """Append one durable Producer recommendation observation."""
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, project_id):
                raise KeyError(f"项目不存在: {project_id}")
            if production_job_id is not None:
                job = connection.execute("SELECT project_id FROM production_jobs WHERE id=?", (production_job_id,)).fetchone()
                if job is None or job["project_id"] != project_id:
                    raise ValueError("ProductionJob 不属于该项目")
            connection.execute(
                "INSERT INTO producer_recommendation_events(id,project_id,production_job_id,action,target_id,metadata_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (event_id, project_id, production_job_id, action, target_id, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), created_at),
            )
        return self.get_producer_recommendation_event(event_id)

    def get_producer_recommendation_event(self, event_id: str) -> dict[str, object] | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM producer_recommendation_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "project_id": row["project_id"], "production_job_id": row["production_job_id"], "action": row["action"], "target_id": row["target_id"], "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"]}

    def list_producer_recommendation_events(
        self,
        project_id: str,
        *,
        production_job_id: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, object]]:
        query = "SELECT * FROM producer_recommendation_events WHERE project_id=?"
        params: list[object] = [project_id]
        if production_job_id is not None:
            query += " AND production_job_id=?"; params.append(production_job_id)
        if action is not None:
            query += " AND action=?"; params.append(action)
        if target_id is not None:
            query += " AND target_id=?"; params.append(target_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [{"id": row["id"], "project_id": row["project_id"], "production_job_id": row["production_job_id"], "action": row["action"], "target_id": row["target_id"], "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"]} for row in rows]

    @staticmethod
    def _final_assembly_from_row(row) -> FinalAssembly:
        return FinalAssembly(
            id=row["id"],
            project_id=row["project_id"],
            production_job_id=row["production_job_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _final_assembly_item_from_row(row) -> FinalAssemblyItem:
        return FinalAssemblyItem(
            id=row["id"],
            final_assembly_id=row["final_assembly_id"],
            order_index=row["order_index"],
            production_shot_id=row["production_shot_id"],
            production_execution_id=row["production_execution_id"],
            production_artifact_id=row["production_artifact_id"],
            qc_result_id=row["qc_result_id"],
            review_id=row["review_id"],
            source_path=row["source_path"],
            created_at=row["created_at"],
        )

    def create_final_assembly(self, assembly: FinalAssembly) -> FinalAssembly:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, assembly.project_id):
                raise KeyError(f"项目不存在: {assembly.project_id}")
            job = connection.execute(
                "SELECT project_id FROM production_jobs WHERE id=?", (assembly.production_job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"ProductionJob 不存在: {assembly.production_job_id}")
            if job["project_id"] != assembly.project_id:
                raise ValueError("ProductionJob 不属于该项目")
            connection.execute(
                "INSERT INTO final_assemblies(id,project_id,production_job_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (
                    assembly.id,
                    assembly.project_id,
                    assembly.production_job_id,
                    assembly.status.value,
                    assembly.created_at,
                    assembly.updated_at,
                ),
            )
        return self.get_final_assembly(assembly.id)

    def get_final_assembly(self, assembly_id: str) -> FinalAssembly | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM final_assemblies WHERE id=?", (assembly_id,)).fetchone()
        return self._final_assembly_from_row(row) if row else None

    def list_final_assemblies(self, project_id: str, production_job_id: str | None = None) -> list[FinalAssembly]:
        query = "SELECT * FROM final_assemblies WHERE project_id=?"
        params: list[object] = [project_id]
        if production_job_id is not None:
            query += " AND production_job_id=?"
            params.append(production_job_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._final_assembly_from_row(row) for row in rows]

    def freeze_final_assembly_atomic(
        self,
        assembly_id: str,
        items: list[FinalAssemblyItem],
        *,
        updated_at: str,
    ) -> FinalAssembly:
        """Insert a complete immutable manifest and mark it READY atomically."""
        with self.transaction() as connection:
            assembly = connection.execute(
                "SELECT project_id,status FROM final_assemblies WHERE id=?", (assembly_id,)
            ).fetchone()
            if assembly is None:
                raise KeyError(f"FinalAssembly 不存在: {assembly_id}")
            if assembly["status"] != FinalAssemblyStatus.DRAFT.value:
                raise ValueError("只有 DRAFT FinalAssembly 可以 freeze")
            existing = connection.execute(
                "SELECT 1 FROM final_assembly_items WHERE final_assembly_id=? LIMIT 1",
                (assembly_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError("DRAFT FinalAssembly 已包含 item，不能重新构建")
            for item in items:
                if item.final_assembly_id != assembly_id:
                    raise ValueError("FinalAssemblyItem provenance 无效")
                connection.execute(
                    "INSERT INTO final_assembly_items("
                    "id,final_assembly_id,order_index,production_shot_id,production_execution_id,"
                    "production_artifact_id,qc_result_id,review_id,source_path,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.id,
                        item.final_assembly_id,
                        item.order_index,
                        item.production_shot_id,
                        item.production_execution_id,
                        item.production_artifact_id,
                        item.qc_result_id,
                        item.review_id,
                        item.source_path,
                        item.created_at,
                    ),
                )
            connection.execute(
                "UPDATE final_assemblies SET status=?,updated_at=? WHERE id=? AND status=?",
                (FinalAssemblyStatus.READY.value, updated_at, assembly_id, FinalAssemblyStatus.DRAFT.value),
            )
        return self.get_final_assembly(assembly_id)

    def update_final_assembly_status(
        self,
        assembly_id: str,
        status: FinalAssemblyStatus,
        *,
        updated_at: str,
    ) -> FinalAssembly:
        current = self.get_final_assembly(assembly_id)
        if current is None:
            raise KeyError(f"FinalAssembly 不存在: {assembly_id}")
        # READY item rows stay immutable, while the aggregate lifecycle may
        # advance through ASSEMBLING/SUCCEEDED/FAILED/CANCELLED.  Once frozen,
        # an assembly can never be reverted to DRAFT; a retry may re-enter
        # ASSEMBLING without changing any item row.
        if current.status is FinalAssemblyStatus.DRAFT and status not in {
            FinalAssemblyStatus.DRAFT,
            FinalAssemblyStatus.READY,
        }:
            raise ValueError("DRAFT FinalAssembly 必须先 freeze")
        if current.status is not FinalAssemblyStatus.DRAFT and status is FinalAssemblyStatus.DRAFT:
            raise ValueError("已 freeze 的 FinalAssembly 不可回退到 DRAFT")
        with connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE final_assemblies SET status=?,updated_at=? WHERE id=?",
                (status.value, updated_at, assembly_id),
            )
        return self.get_final_assembly(assembly_id)

    @staticmethod
    def _final_assembly_render_attempt_from_row(row) -> FinalAssemblyRenderAttempt:
        return FinalAssemblyRenderAttempt(
            id=row["id"],
            final_assembly_id=row["final_assembly_id"],
            attempt_number=row["attempt_number"],
            status=row["status"],
            adapter_name=row["adapter_name"],
            output_relative_path=row["output_relative_path"],
            metadata_json=json.loads(row["metadata_json"]),
            error_message=row["error_message"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
        )

    def create_final_assembly_render_attempt(self, attempt: FinalAssemblyRenderAttempt) -> FinalAssemblyRenderAttempt:
        with connect(self.paths.database) as connection:
            assembly = connection.execute(
                "SELECT project_id FROM final_assemblies WHERE id=?", (attempt.final_assembly_id,)
            ).fetchone()
            if assembly is None:
                raise KeyError(f"FinalAssembly 不存在: {attempt.final_assembly_id}")
            connection.execute(
                """
                INSERT INTO final_assembly_render_attempts(
                    id,final_assembly_id,attempt_number,status,adapter_name,
                    output_relative_path,metadata_json,error_message,started_at,
                    finished_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.id,
                    attempt.final_assembly_id,
                    attempt.attempt_number,
                    attempt.status.value,
                    attempt.adapter_name,
                    attempt.output_relative_path,
                    json.dumps(attempt.metadata_json, ensure_ascii=False, sort_keys=True),
                    attempt.error_message,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.created_at,
                ),
            )
        return self.get_final_assembly_render_attempt(attempt.id)

    def get_final_assembly_render_attempt(self, attempt_id: str) -> FinalAssemblyRenderAttempt | None:
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT * FROM final_assembly_render_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return self._final_assembly_render_attempt_from_row(row) if row else None

    def list_final_assembly_render_attempts(self, assembly_id: str) -> list[FinalAssemblyRenderAttempt]:
        with connect(self.paths.database) as connection:
            rows = connection.execute(
                "SELECT * FROM final_assembly_render_attempts WHERE final_assembly_id=? "
                "ORDER BY attempt_number,id", (assembly_id,)
            ).fetchall()
        return [self._final_assembly_render_attempt_from_row(row) for row in rows]

    def update_final_assembly_render_attempt(
        self,
        attempt_id: str,
        *,
        status: FinalAssemblyRenderAttemptStatus,
        output_relative_path: str | None = None,
        metadata_json: dict[str, object] | None = None,
        error_message: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> FinalAssemblyRenderAttempt:
        current = self.get_final_assembly_render_attempt(attempt_id)
        if current is None:
            raise KeyError(f"FinalAssemblyRenderAttempt 不存在: {attempt_id}")
        with connect(self.paths.database) as connection:
            connection.execute(
                """
                UPDATE final_assembly_render_attempts
                SET status=?, output_relative_path=COALESCE(?,output_relative_path),
                    metadata_json=?, error_message=COALESCE(?,error_message),
                    started_at=COALESCE(?,started_at), finished_at=COALESCE(?,finished_at)
                WHERE id=?
                """,
                (
                    status.value,
                    output_relative_path,
                    json.dumps(metadata_json if metadata_json is not None else current.metadata_json,
                               ensure_ascii=False, sort_keys=True),
                    error_message,
                    started_at,
                    finished_at,
                    attempt_id,
                ),
            )
        return self.get_final_assembly_render_attempt(attempt_id)

    def create_final_assembly_item(self, item: FinalAssemblyItem) -> FinalAssemblyItem:
        """Insert one item after validating every provenance edge.

        The service performs physical-file checks; this repository enforces
        durable project/job/execution/artifact/QC/review identity checks so
        direct callers cannot create a cross-project manifest row.
        """
        with connect(self.paths.database) as connection:
            assembly = connection.execute(
                "SELECT project_id,production_job_id,status FROM final_assemblies WHERE id=?",
                (item.final_assembly_id,),
            ).fetchone()
            if assembly is None:
                raise KeyError(f"FinalAssembly 不存在: {item.final_assembly_id}")
            if assembly["status"] != FinalAssemblyStatus.DRAFT.value:
                raise ValueError("READY 或非 DRAFT FinalAssembly 不可添加 item")

            shot = connection.execute(
                """
                SELECT ps.id
                FROM production_shots ps
                JOIN production_jobs pj ON pj.id=ps.production_job_id
                WHERE ps.id=? AND ps.production_job_id=? AND pj.project_id=?
                """,
                (item.production_shot_id, assembly["production_job_id"], assembly["project_id"]),
            ).fetchone()
            if shot is None:
                raise ValueError("ProductionShot 不属于该 FinalAssembly 项目或 ProductionJob")

            execution = connection.execute(
                """
                SELECT pe.id
                FROM production_executions pe
                JOIN production_jobs pj ON pj.id=pe.production_job_id
                WHERE pe.id=? AND pe.production_job_id=? AND pj.project_id=?
                """,
                (item.production_execution_id, assembly["production_job_id"], assembly["project_id"]),
            ).fetchone()
            if execution is None:
                raise ValueError("ProductionExecution 不属于该 FinalAssembly 项目或 ProductionJob")

            artifact = connection.execute(
                "SELECT execution_id,path FROM production_artifacts WHERE id=?", (item.production_artifact_id,)
            ).fetchone()
            if artifact is None or artifact["execution_id"] != item.production_execution_id:
                raise ValueError("ProductionArtifact 不属于该 ProductionExecution")
            if artifact["path"] != item.source_path:
                raise ValueError("FinalAssembly source_path 必须与 ProductionArtifact path 一致")

            qc = connection.execute(
                "SELECT project_id,execution_id,artifact_id FROM production_qc_results WHERE id=?",
                (item.qc_result_id,),
            ).fetchone()
            if (
                qc is None
                or qc["project_id"] != assembly["project_id"]
                or qc["execution_id"] != item.production_execution_id
                or qc["artifact_id"] != item.production_artifact_id
            ):
                raise ValueError("ProductionQCResult 不属于该 artifact/project")

            if item.review_id is not None:
                review = connection.execute(
                    "SELECT project_id,qc_result_id FROM production_reviews WHERE id=?", (item.review_id,)
                ).fetchone()
                if (
                    review is None
                    or review["project_id"] != assembly["project_id"]
                    or review["qc_result_id"] != item.qc_result_id
                ):
                    raise ValueError("ProductionReview 不属于该 QCResult/project")

            normalized = item.source_path.replace("\\", "/")
            if (
                not normalized
                or normalized.startswith("/")
                or PureWindowsPath(item.source_path).drive
                or any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts)
            ):
                raise ValueError("FinalAssembly source_path 必须是项目相对路径")
            connection.execute(
                """
                INSERT INTO final_assembly_items(
                    id,final_assembly_id,order_index,production_shot_id,
                    production_execution_id,production_artifact_id,qc_result_id,
                    review_id,source_path,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item.id,
                    item.final_assembly_id,
                    item.order_index,
                    item.production_shot_id,
                    item.production_execution_id,
                    item.production_artifact_id,
                    item.qc_result_id,
                    item.review_id,
                    normalized,
                    item.created_at,
                ),
            )
        return self.get_final_assembly_item(item.id)

    def get_final_assembly_item(self, item_id: str) -> FinalAssemblyItem | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM final_assembly_items WHERE id=?", (item_id,)).fetchone()
        return self._final_assembly_item_from_row(row) if row else None

    def list_final_assembly_items(self, assembly_id: str) -> list[FinalAssemblyItem]:
        with connect(self.paths.database) as connection:
            rows = connection.execute(
                "SELECT * FROM final_assembly_items WHERE final_assembly_id=? ORDER BY order_index,id",
                (assembly_id,),
            ).fetchall()
        return [self._final_assembly_item_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Post-production persistence.  These methods intentionally expose
    # models only; media/path policy belongs to PostProductionService.
    @staticmethod
    def _post_plan_from_row(row) -> PostProductionPlan:
        return PostProductionPlan(
            id=row["id"], project_id=row["project_id"],
            source_final_assembly_id=row["source_final_assembly_id"],
            source_final_assembly_render_attempt_id=row["source_final_assembly_render_attempt_id"] if "source_final_assembly_render_attempt_id" in row.keys() else None,
            subtitle_enabled=bool(row["subtitle_enabled"]),
            audio_mix=AudioMixConfig.model_validate(json.loads(row["audio_mix_json"])),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _post_subtitle_from_row(row) -> SubtitleTrack:
        return SubtitleTrack(
            id=row["id"], project_id=row["project_id"], plan_id=row["plan_id"],
            source_script_revision_id=row["source_script_revision_id"],
            enabled=bool(row["enabled"]),
            cues=[SubtitleCue.model_validate(item) for item in json.loads(row["cues_json"])],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _post_voice_from_row(row) -> VoiceTrack:
        return VoiceTrack(
            id=row["id"], project_id=row["project_id"], plan_id=row["plan_id"], path=row["path"],
            voice_assignments=json.loads(row["voice_assignments_json"]),
            metadata_json=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )

    @staticmethod
    def _post_music_from_row(row) -> MusicTrack:
        return MusicTrack(
            id=row["id"], project_id=row["project_id"], plan_id=row["plan_id"], path=row["path"],
            start_seconds=row["start_seconds"], end_seconds=row["end_seconds"], gain=row["gain"],
            loop=bool(row["loop"]), fade_in_seconds=row["fade_in_seconds"],
            fade_out_seconds=row["fade_out_seconds"], metadata_json=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _post_attempt_from_row(row) -> PostRenderAttempt:
        return PostRenderAttempt(
            id=row["id"], project_id=row["project_id"], plan_id=row["plan_id"],
            source_final_assembly_id=row["source_final_assembly_id"],
            source_final_assembly_render_attempt_id=row["source_final_assembly_render_attempt_id"] if "source_final_assembly_render_attempt_id" in row.keys() else None,
            attempt_number=row["attempt_number"],
            status=PostRenderAttemptStatus(row["status"]), adapter_name=row["adapter_name"],
            output_relative_path=row["output_relative_path"], metadata_json=json.loads(row["metadata_json"]),
            error_message=row["error_message"], started_at=row["started_at"], finished_at=row["finished_at"],
            created_at=row["created_at"],
        )

    def create_post_plan(self, plan: PostProductionPlan) -> PostProductionPlan:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, plan.project_id):
                raise KeyError(f"项目不存在: {plan.project_id}")
            assembly = connection.execute(
                "SELECT project_id FROM final_assemblies WHERE id=?", (plan.source_final_assembly_id,)
            ).fetchone()
            if assembly is None:
                raise KeyError("FinalAssembly 不存在")
            if assembly["project_id"] != plan.project_id:
                raise ValueError("FinalAssembly 不属于该项目")
            connection.execute(
                "INSERT INTO post_production_plans(id,project_id,source_final_assembly_id,source_final_assembly_render_attempt_id,subtitle_enabled,audio_mix_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (plan.id, plan.project_id, plan.source_final_assembly_id, plan.source_final_assembly_render_attempt_id, int(plan.subtitle_enabled),
                 json.dumps(plan.audio_mix.model_dump(mode="json"), ensure_ascii=False, sort_keys=True), plan.created_at, plan.updated_at),
            )
        return self.get_post_plan(plan.id)

    def get_post_plan(self, plan_id: str) -> PostProductionPlan | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM post_production_plans WHERE id=?", (plan_id,)).fetchone()
        return self._post_plan_from_row(row) if row else None

    def list_post_plans(self, project_id: str) -> list[PostProductionPlan]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM post_production_plans WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._post_plan_from_row(row) for row in rows]

    def update_post_plan(self, plan: PostProductionPlan) -> PostProductionPlan:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT project_id FROM post_production_plans WHERE id=?", (plan.id,)).fetchone()
            if row is None:
                raise KeyError("PostProductionPlan 不存在")
            if row["project_id"] != plan.project_id:
                raise ValueError("PostProductionPlan 不属于该项目")
            connection.execute(
                "UPDATE post_production_plans SET subtitle_enabled=?,audio_mix_json=?,updated_at=? WHERE id=?",
                (int(plan.subtitle_enabled), json.dumps(plan.audio_mix.model_dump(mode="json"), ensure_ascii=False, sort_keys=True), plan.updated_at, plan.id),
            )
        return self.get_post_plan(plan.id)

    def pin_post_plan_source_attempt(self, project_id: str, plan_id: str, attempt_id: str) -> PostProductionPlan:
        """Freeze a plan to one successful FinalAssembly render attempt.

        The write is monotonic: a plan may be pinned once, and a later render
        attempt can only be selected by creating a new plan explicitly.
        """
        with connect(self.paths.database) as connection:
            plan = connection.execute(
                "SELECT project_id,source_final_assembly_id,source_final_assembly_render_attempt_id FROM post_production_plans WHERE id=?",
                (plan_id,),
            ).fetchone()
            if plan is None or plan["project_id"] != project_id:
                raise ValueError("PostProductionPlan 不属于该项目")
            attempt = connection.execute(
                "SELECT id,final_assembly_id,status FROM final_assembly_render_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["final_assembly_id"] != plan["source_final_assembly_id"]:
                raise ValueError("FinalAssemblyRenderAttempt 不属于该 PostProductionPlan")
            if attempt["status"] != "SUCCEEDED":
                raise ValueError("PostProductionPlan 只能绑定成功的 FinalAssemblyRenderAttempt")
            existing = plan["source_final_assembly_render_attempt_id"]
            if existing is not None and existing != attempt_id:
                raise ValueError("PostProductionPlan 的 source render attempt 已冻结")
            connection.execute(
                "UPDATE post_production_plans SET source_final_assembly_render_attempt_id=?,updated_at=? WHERE id=?",
                (attempt_id, connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0], plan_id),
            )
        return self.get_post_plan(plan_id)

    def create_post_subtitle_track(self, track: SubtitleTrack) -> SubtitleTrack:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, track.project_id):
                raise KeyError(f"项目不存在: {track.project_id}")
            revision = connection.execute("SELECT project_id FROM structured_script_revisions WHERE id=?", (track.source_script_revision_id,)).fetchone()
            if revision is None or revision["project_id"] != track.project_id:
                raise ValueError("Structured Script revision 不属于该项目")
            if track.plan_id is not None:
                plan = connection.execute("SELECT project_id FROM post_production_plans WHERE id=?", (track.plan_id,)).fetchone()
                if plan is None or plan["project_id"] != track.project_id:
                    raise ValueError("PostProductionPlan 不属于该项目")
            connection.execute(
                "INSERT INTO post_subtitle_tracks(id,project_id,plan_id,source_script_revision_id,enabled,cues_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (track.id, track.project_id, track.plan_id, track.source_script_revision_id, int(track.enabled),
                 json.dumps([cue.model_dump(mode="json") for cue in track.cues], ensure_ascii=False, sort_keys=True), track.created_at, track.updated_at),
            )
        return self.get_post_subtitle_track(track.id)

    def get_post_subtitle_track(self, track_id: str) -> SubtitleTrack | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM post_subtitle_tracks WHERE id=?", (track_id,)).fetchone()
        return self._post_subtitle_from_row(row) if row else None

    def list_post_subtitle_tracks(self, project_id: str, plan_id: str | None = None) -> list[SubtitleTrack]:
        query = "SELECT * FROM post_subtitle_tracks WHERE project_id=?"
        params: list[object] = [project_id]
        if plan_id is not None:
            query += " AND plan_id=?"
            params.append(plan_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._post_subtitle_from_row(row) for row in rows]

    def update_post_subtitle_track(self, track: SubtitleTrack) -> SubtitleTrack:
        """Update the editable subtitle projection without changing its source script."""
        with connect(self.paths.database) as connection:
            row = connection.execute(
                "SELECT project_id, source_script_revision_id, plan_id FROM post_subtitle_tracks WHERE id=?",
                (track.id,),
            ).fetchone()
            if row is None:
                raise KeyError("SubtitleTrack 不存在")
            if row["project_id"] != track.project_id or row["plan_id"] != track.plan_id:
                raise ValueError("SubtitleTrack 不属于该项目或计划")
            if row["source_script_revision_id"] != track.source_script_revision_id:
                raise ValueError("SubtitleTrack 的来源剧本不可变更")
            connection.execute(
                "UPDATE post_subtitle_tracks SET enabled=?, cues_json=?, updated_at=? WHERE id=?",
                (
                    int(track.enabled),
                    json.dumps([cue.model_dump(mode="json") for cue in track.cues], ensure_ascii=False, sort_keys=True),
                    track.updated_at,
                    track.id,
                ),
            )
        return self.get_post_subtitle_track(track.id)

    def create_post_voice_track(self, track: VoiceTrack) -> VoiceTrack:
        with connect(self.paths.database) as connection:
            plan = connection.execute("SELECT project_id FROM post_production_plans WHERE id=?", (track.plan_id,)).fetchone()
            if plan is None or plan["project_id"] != track.project_id:
                raise ValueError("PostProductionPlan 不属于该项目")
            connection.execute(
                "INSERT INTO post_voice_tracks(id,project_id,plan_id,path,voice_assignments_json,metadata_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (track.id, track.project_id, track.plan_id, track.path, json.dumps(track.voice_assignments, ensure_ascii=False, sort_keys=True), json.dumps(track.metadata_json, ensure_ascii=False, sort_keys=True), track.created_at),
            )
        return self.get_post_voice_track(track.id)

    def get_post_voice_track(self, track_id: str) -> VoiceTrack | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM post_voice_tracks WHERE id=?", (track_id,)).fetchone()
        return self._post_voice_from_row(row) if row else None

    def list_post_voice_tracks(self, project_id: str, plan_id: str) -> list[VoiceTrack]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM post_voice_tracks WHERE project_id=? AND plan_id=? ORDER BY created_at,id", (project_id, plan_id)).fetchall()
        return [self._post_voice_from_row(row) for row in rows]

    def create_post_music_track(self, track: MusicTrack) -> MusicTrack:
        with connect(self.paths.database) as connection:
            plan = connection.execute("SELECT project_id FROM post_production_plans WHERE id=?", (track.plan_id,)).fetchone()
            if plan is None or plan["project_id"] != track.project_id:
                raise ValueError("PostProductionPlan 不属于该项目")
            connection.execute(
                "INSERT INTO post_music_tracks(id,project_id,plan_id,path,start_seconds,end_seconds,gain,loop,fade_in_seconds,fade_out_seconds,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (track.id, track.project_id, track.plan_id, track.path, track.start_seconds, track.end_seconds, track.gain, int(track.loop), track.fade_in_seconds, track.fade_out_seconds, json.dumps(track.metadata_json, ensure_ascii=False, sort_keys=True), track.created_at),
            )
        return self.get_post_music_track(track.id)

    def get_post_music_track(self, track_id: str) -> MusicTrack | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM post_music_tracks WHERE id=?", (track_id,)).fetchone()
        return self._post_music_from_row(row) if row else None

    def list_post_music_tracks(self, project_id: str, plan_id: str) -> list[MusicTrack]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM post_music_tracks WHERE project_id=? AND plan_id=? ORDER BY created_at,id", (project_id, plan_id)).fetchall()
        return [self._post_music_from_row(row) for row in rows]

    def create_post_render_attempt(self, attempt: PostRenderAttempt) -> PostRenderAttempt:
        with connect(self.paths.database) as connection:
            plan = connection.execute("SELECT project_id,source_final_assembly_id,source_final_assembly_render_attempt_id FROM post_production_plans WHERE id=?", (attempt.plan_id,)).fetchone()
            if plan is None or plan["project_id"] != attempt.project_id or plan["source_final_assembly_id"] != attempt.source_final_assembly_id or plan["source_final_assembly_render_attempt_id"] != attempt.source_final_assembly_render_attempt_id:
                raise ValueError("PostRenderAttempt provenance 不属于该项目/plan")
            connection.execute(
                "INSERT INTO post_render_attempts(id,project_id,plan_id,source_final_assembly_id,source_final_assembly_render_attempt_id,attempt_number,status,adapter_name,output_relative_path,metadata_json,error_message,started_at,finished_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt.id, attempt.project_id, attempt.plan_id, attempt.source_final_assembly_id, attempt.source_final_assembly_render_attempt_id, attempt.attempt_number, attempt.status.value, attempt.adapter_name, attempt.output_relative_path, json.dumps(attempt.metadata_json, ensure_ascii=False, sort_keys=True), attempt.error_message, attempt.started_at, attempt.finished_at, attempt.created_at),
            )
        return self.get_post_render_attempt(attempt.id)

    def get_post_render_attempt(self, attempt_id: str) -> PostRenderAttempt | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM post_render_attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._post_attempt_from_row(row) if row else None

    def list_post_render_attempts(self, project_id: str, plan_id: str) -> list[PostRenderAttempt]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM post_render_attempts WHERE project_id=? AND plan_id=? ORDER BY attempt_number,id", (project_id, plan_id)).fetchall()
        return [self._post_attempt_from_row(row) for row in rows]

    def update_post_render_attempt(self, attempt_id: str, *, status: PostRenderAttemptStatus, output_relative_path: str | None = None, metadata_json: dict[str, object] | None = None, error_message: str | None = None, started_at: str | None = None, finished_at: str | None = None) -> PostRenderAttempt:
        current = self.get_post_render_attempt(attempt_id)
        if current is None:
            raise KeyError("PostRenderAttempt 不存在")
        with connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE post_render_attempts SET status=?,output_relative_path=COALESCE(?,output_relative_path),metadata_json=?,error_message=COALESCE(?,error_message),started_at=COALESCE(?,started_at),finished_at=COALESCE(?,finished_at) WHERE id=?",
                (status.value, output_relative_path, json.dumps(metadata_json if metadata_json is not None else current.metadata_json, ensure_ascii=False, sort_keys=True), error_message, started_at, finished_at, attempt_id),
            )
        return self.get_post_render_attempt(attempt_id)

    # Runtime foundation -------------------------------------------------
    @staticmethod
    def _output_profile_from_row(row) -> OutputProfile:
        return OutputProfile(
            id=row["id"], project_id=row["project_id"], aspect_ratio=row["aspect_ratio"],
            target_duration_seconds=row["target_duration_seconds"], target_resolution=row["target_resolution"],
            fps=row["fps"], video_codec_target=row["video_codec_target"],
            audio_sample_rate=row["audio_sample_rate"], audio_channels=row["audio_channels"],
            created_at=row["created_at"],
        )

    def create_output_profile(self, profile: OutputProfile) -> OutputProfile:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, profile.project_id):
                raise KeyError(f"项目不存在: {profile.project_id}")
            connection.execute(
                "INSERT INTO output_profiles(id,project_id,aspect_ratio,target_duration_seconds,target_resolution,fps,"
                "video_codec_target,audio_sample_rate,audio_channels,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (profile.id, profile.project_id, profile.aspect_ratio, profile.target_duration_seconds,
                 profile.target_resolution, profile.fps, profile.video_codec_target, profile.audio_sample_rate,
                 profile.audio_channels, profile.created_at),
            )
        return self.get_output_profile(profile.id)

    def get_output_profile(self, profile_id: str) -> OutputProfile | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM output_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._output_profile_from_row(row) if row else None

    def list_output_profiles(self, project_id: str) -> list[OutputProfile]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM output_profiles WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._output_profile_from_row(row) for row in rows]

    @staticmethod
    def _generation_brief_from_row(row) -> GenerationBrief:
        content = json.loads(row["content_json"])
        return GenerationBrief.model_validate(content | {
            "id": row["id"], "project_id": row["project_id"],
            "production_job_id": row["production_job_id"], "shot_id": row["shot_id"],
            "sha256": row["sha256"], "created_at": row["created_at"],
        })

    def create_generation_brief(self, brief: GenerationBrief) -> GenerationBrief:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, brief.project_id):
                raise KeyError(f"项目不存在: {brief.project_id}")
            connection.execute(
                "INSERT INTO generation_briefs(id,project_id,production_job_id,shot_id,content_json,sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                (brief.id, brief.project_id, brief.production_job_id, brief.shot_id,
                 json.dumps(brief.model_dump(mode="json", exclude={"id","project_id","production_job_id","shot_id","sha256","created_at"}), ensure_ascii=False, sort_keys=True),
                 brief.sha256, brief.created_at),
            )
        return self.get_generation_brief(brief.id)

    def get_generation_brief(self, brief_id: str) -> GenerationBrief | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM generation_briefs WHERE id=?", (brief_id,)).fetchone()
        return self._generation_brief_from_row(row) if row else None

    def list_generation_briefs(self, project_id: str, production_job_id: str | None = None) -> list[GenerationBrief]:
        query = "SELECT * FROM generation_briefs WHERE project_id=?"; args: list[object] = [project_id]
        if production_job_id is not None:
            query += " AND production_job_id=?"; args.append(production_job_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._generation_brief_from_row(row) for row in rows]

    @staticmethod
    def _runtime_plan_from_row(row) -> RuntimePlan:
        return RuntimePlan(
            id=row["id"], project_id=row["project_id"], production_job_id=row["production_job_id"],
            execution_id=row["execution_id"], output_profile_id=row["output_profile_id"],
            generation_brief_id=row["generation_brief_id"], provider_capability=row["provider_capability"],
            provider_id=row["provider_id"], model_id=row["model_id"], generation_mode=row["generation_mode"],
            resolution=row["resolution"], provider_generation_duration=row["provider_generation_duration"],
            target_creative_duration=row["target_creative_duration"], audio_strategy=row["audio_strategy"],
            provider_parameters=json.loads(row["provider_parameters_json"]),
            reference_version_ids=tuple(json.loads(row["reference_version_ids_json"])),
            reference_roles=json.loads(row["reference_roles_json"]),
            continuity_strategy=row["continuity_strategy"], generation_brief_hash=row["generation_brief_hash"],
            output_profile_hash=row["output_profile_hash"], authorization=json.loads(row["authorization_json"]),
            prompt_template_version=row["prompt_template_version"], plan_hash=row["plan_hash"], created_at=row["created_at"],
        )

    def create_runtime_plan(self, plan: RuntimePlan) -> RuntimePlan:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, plan.project_id):
                raise KeyError(f"项目不存在: {plan.project_id}")
            connection.execute(
                "INSERT INTO runtime_plans(id,project_id,production_job_id,execution_id,output_profile_id,generation_brief_id,"
                "provider_capability,provider_id,model_id,generation_mode,resolution,provider_generation_duration,target_creative_duration,"
                "audio_strategy,provider_parameters_json,reference_version_ids_json,reference_roles_json,continuity_strategy,"
                "generation_brief_hash,output_profile_hash,authorization_json,prompt_template_version,plan_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan.id, plan.project_id, plan.production_job_id, plan.execution_id, plan.output_profile_id,
                 plan.generation_brief_id, plan.provider_capability, plan.provider_id, plan.model_id,
                 plan.generation_mode, plan.resolution, plan.provider_generation_duration, plan.target_creative_duration,
                 plan.audio_strategy, json.dumps(plan.provider_parameters, ensure_ascii=False, sort_keys=True),
                 json.dumps(list(plan.reference_version_ids), ensure_ascii=False), json.dumps(plan.reference_roles, ensure_ascii=False, sort_keys=True),
                 plan.continuity_strategy, plan.generation_brief_hash, plan.output_profile_hash,
                 json.dumps(plan.authorization, ensure_ascii=False, sort_keys=True), plan.prompt_template_version,
                 plan.plan_hash, plan.created_at),
            )
        return self.get_runtime_plan(plan.id)

    def get_runtime_plan(self, plan_id: str) -> RuntimePlan | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM runtime_plans WHERE id=?", (plan_id,)).fetchone()
        return self._runtime_plan_from_row(row) if row else None

    def list_runtime_plans(self, project_id: str, execution_id: str | None = None) -> list[RuntimePlan]:
        query = "SELECT * FROM runtime_plans WHERE project_id=?"; args: list[object] = [project_id]
        if execution_id is not None:
            query += " AND execution_id=?"; args.append(execution_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._runtime_plan_from_row(row) for row in rows]

    @staticmethod
    def _ai_invocation_from_row(row) -> AIInvocation:
        return AIInvocation(
            id=row["id"], project_id=row["project_id"], production_job_id=row["production_job_id"], execution_id=row["execution_id"],
            capability=row["capability"], provider_id=row["provider_id"], model_id=row["model_id"],
            input_source_ids=tuple(json.loads(row["input_source_ids_json"])), reference_version_ids=tuple(json.loads(row["reference_version_ids_json"])),
            generation_brief_hash=row["generation_brief_hash"], runtime_plan_id=row["runtime_plan_id"], runtime_plan_hash=row["runtime_plan_hash"],
            request_summary=json.loads(row["request_summary_json"]), provider_task_id=row["provider_task_id"], status=row["status"],
            started_at=row["started_at"], finished_at=row["finished_at"], usage=json.loads(row["usage_json"]), estimated_cost=row["estimated_cost"], actual_cost=row["actual_cost"],
            output_artifact_ids=tuple(json.loads(row["output_artifact_ids_json"])), created_at=row["created_at"],
        )

    def create_ai_invocation(self, invocation: AIInvocation) -> AIInvocation:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, invocation.project_id):
                raise KeyError(f"项目不存在: {invocation.project_id}")
            connection.execute(
                "INSERT INTO ai_invocations(id,project_id,production_job_id,execution_id,capability,provider_id,model_id,"
                "input_source_ids_json,reference_version_ids_json,generation_brief_hash,runtime_plan_id,runtime_plan_hash,"
                "request_summary_json,provider_task_id,status,started_at,finished_at,usage_json,estimated_cost,actual_cost,output_artifact_ids_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (invocation.id, invocation.project_id, invocation.production_job_id, invocation.execution_id, invocation.capability,
                 invocation.provider_id, invocation.model_id, json.dumps(list(invocation.input_source_ids)), json.dumps(list(invocation.reference_version_ids)),
                 invocation.generation_brief_hash, invocation.runtime_plan_id, invocation.runtime_plan_hash,
                 json.dumps(invocation.request_summary, ensure_ascii=False, sort_keys=True), invocation.provider_task_id, invocation.status,
                 invocation.started_at, invocation.finished_at, json.dumps(invocation.usage, ensure_ascii=False, sort_keys=True),
                 invocation.estimated_cost, invocation.actual_cost, json.dumps(list(invocation.output_artifact_ids)), invocation.created_at),
            )
        return self.get_ai_invocation(invocation.id)

    def get_ai_invocation(self, invocation_id: str) -> AIInvocation | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)).fetchone()
        return self._ai_invocation_from_row(row) if row else None

    def list_ai_invocations(self, project_id: str, execution_id: str | None = None) -> list[AIInvocation]:
        query = "SELECT * FROM ai_invocations WHERE project_id=?"; args: list[object] = [project_id]
        if execution_id is not None:
            query += " AND execution_id=?"; args.append(execution_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._ai_invocation_from_row(row) for row in rows]

    # Creative intake / Source Pack -------------------------------------
    @staticmethod
    def _source_pack_from_row(row) -> SourcePackItem:
        return SourcePackItem(
            id=row["id"], project_id=row["project_id"], source_kind=row["source_kind"],
            display_filename=row["display_filename"], mime_type=row["mime_type"], size_bytes=row["size_bytes"],
            sha256=row["sha256"], storage_path=row["storage_path"], version_of_id=row["version_of_id"],
            extraction_state=row["extraction_state"], extracted_text=row["extracted_text"],
            metadata=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )

    def create_source_pack_item(self, item: SourcePackItem) -> SourcePackItem:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, item.project_id):
                raise KeyError(f"项目不存在: {item.project_id}")
            if item.version_of_id is not None:
                parent = connection.execute("SELECT project_id FROM source_pack_items WHERE id=?", (item.version_of_id,)).fetchone()
                if parent is None or parent["project_id"] != item.project_id:
                    raise ValueError("SourcePack version parent 不属于该项目")
            connection.execute(
                "INSERT INTO source_pack_items(id,project_id,source_kind,display_filename,mime_type,size_bytes,sha256,storage_path,version_of_id,extraction_state,extracted_text,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.id, item.project_id, item.source_kind.value, item.display_filename, item.mime_type, item.size_bytes, item.sha256, item.storage_path, item.version_of_id, item.extraction_state.value, item.extracted_text, json.dumps(item.metadata, ensure_ascii=False, sort_keys=True), item.created_at),
            )
        return self.get_source_pack_item(item.id)

    def get_source_pack_item(self, item_id: str) -> SourcePackItem | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM source_pack_items WHERE id=?", (item_id,)).fetchone()
        return self._source_pack_from_row(row) if row else None

    def list_source_pack_items(self, project_id: str) -> list[SourcePackItem]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM source_pack_items WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._source_pack_from_row(row) for row in rows]

    def find_source_pack_by_hash(self, project_id: str, sha256: str) -> SourcePackItem | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM source_pack_items WHERE project_id=? AND sha256=? ORDER BY created_at,id LIMIT 1", (project_id, sha256)).fetchone()
        return self._source_pack_from_row(row) if row else None

    def update_source_pack_extraction(self, item_id: str, *, state: ExtractionState, extracted_text: str | None, metadata: dict[str, object] | None = None) -> SourcePackItem:
        current = self.get_source_pack_item(item_id)
        if current is None:
            raise KeyError(f"SourcePackItem 不存在: {item_id}")
        with connect(self.paths.database) as connection:
            connection.execute("UPDATE source_pack_items SET extraction_state=?,extracted_text=?,metadata_json=? WHERE id=?", (state.value, extracted_text, json.dumps(metadata if metadata is not None else current.metadata, ensure_ascii=False, sort_keys=True), item_id))
        return self.get_source_pack_item(item_id)

    @staticmethod
    def _normalized_brief_from_row(row) -> NormalizedCreativeBrief:
        content = json.loads(row["content_json"])
        return NormalizedCreativeBrief.model_validate(content | {"id": row["id"], "project_id": row["project_id"], "status": row["status"], "source_ids": tuple(json.loads(row["source_ids_json"])), "created_at": row["created_at"], "updated_at": row["updated_at"]})

    def create_normalized_creative_brief(self, brief: NormalizedCreativeBrief) -> NormalizedCreativeBrief:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, brief.project_id):
                raise KeyError(f"项目不存在: {brief.project_id}")
            connection.execute("INSERT INTO normalized_creative_briefs(id,project_id,status,content_json,source_ids_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (brief.id, brief.project_id, brief.status, json.dumps(brief.model_dump(mode="json", exclude={"id","project_id","status","source_ids","created_at","updated_at"}), ensure_ascii=False, sort_keys=True), json.dumps(list(brief.source_ids), ensure_ascii=False), brief.created_at, brief.updated_at))
        return self.get_normalized_creative_brief(brief.id)

    def get_normalized_creative_brief(self, brief_id: str) -> NormalizedCreativeBrief | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM normalized_creative_briefs WHERE id=?", (brief_id,)).fetchone()
        return self._normalized_brief_from_row(row) if row else None

    def list_normalized_creative_briefs(self, project_id: str) -> list[NormalizedCreativeBrief]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM normalized_creative_briefs WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._normalized_brief_from_row(row) for row in rows]

    @staticmethod
    def _intake_analysis_from_row(row) -> IntakeAnalysis:
        return IntakeAnalysis(id=row["id"], project_id=row["project_id"], source_id=row["source_id"], classifications=tuple(json.loads(row["classifications_json"])), confidence=row["confidence"], warnings=tuple(json.loads(row["warnings_json"])), created_at=row["created_at"])

    def create_intake_analysis(self, analysis: IntakeAnalysis) -> IntakeAnalysis:
        with connect(self.paths.database) as connection:
            source = connection.execute("SELECT project_id FROM source_pack_items WHERE id=?", (analysis.source_id,)).fetchone()
            if source is None or source["project_id"] != analysis.project_id:
                raise ValueError("IntakeAnalysis source 不属于该项目")
            connection.execute("INSERT INTO intake_analyses(id,project_id,source_id,classifications_json,confidence,warnings_json,created_at) VALUES (?,?,?,?,?,?,?)", (analysis.id, analysis.project_id, analysis.source_id, json.dumps(list(analysis.classifications), ensure_ascii=False), analysis.confidence, json.dumps(list(analysis.warnings), ensure_ascii=False), analysis.created_at))
        return self.get_intake_analysis(analysis.id)

    def get_intake_analysis(self, analysis_id: str) -> IntakeAnalysis | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM intake_analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._intake_analysis_from_row(row) if row else None

    def list_intake_analyses(self, project_id: str) -> list[IntakeAnalysis]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM intake_analyses WHERE project_id=? ORDER BY created_at,id", (project_id,)).fetchall()
        return [self._intake_analysis_from_row(row) for row in rows]

    # Ordered multi-reference profiles ----------------------------------
    @staticmethod
    def _reference_profile_from_row(row) -> ReferenceProfile:
        return ReferenceProfile(id=row["id"], project_id=row["project_id"], binding_type=row["binding_type"], binding_id=row["binding_id"], created_at=row["created_at"], updated_at=row["updated_at"])

    @staticmethod
    def _reference_profile_item_from_row(row) -> ReferenceProfileItem:
        return ReferenceProfileItem(id=row["id"], profile_id=row["profile_id"], version_id=row["version_id"], role=row["role"], order_index=row["order_index"], created_at=row["created_at"])

    def create_reference_profile(self, profile: ReferenceProfile) -> ReferenceProfile:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, profile.project_id):
                raise KeyError(f"项目不存在: {profile.project_id}")
            connection.execute("INSERT INTO reference_profiles(id,project_id,binding_type,binding_id,created_at,updated_at) VALUES (?,?,?,?,?,?)", (profile.id, profile.project_id, profile.binding_type, profile.binding_id, profile.created_at, profile.updated_at))
        return self.get_reference_profile(profile.id)

    def get_reference_profile(self, profile_id: str) -> ReferenceProfile | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._reference_profile_from_row(row) if row else None

    def get_reference_profile_for_binding(self, project_id: str, binding_type: str, binding_id: str) -> ReferenceProfile | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_profiles WHERE project_id=? AND binding_type=? AND binding_id=?", (project_id, binding_type, binding_id)).fetchone()
        return self._reference_profile_from_row(row) if row else None

    def list_reference_profile_items(self, profile_id: str) -> list[ReferenceProfileItem]:
        with connect(self.paths.database) as connection:
            rows = connection.execute("SELECT * FROM reference_profile_items WHERE profile_id=? ORDER BY order_index,id", (profile_id,)).fetchall()
        return [self._reference_profile_item_from_row(row) for row in rows]

    def create_reference_profile_item(self, item: ReferenceProfileItem) -> ReferenceProfileItem:
        with connect(self.paths.database) as connection:
            profile = connection.execute("SELECT project_id FROM reference_profiles WHERE id=?", (item.profile_id,)).fetchone()
            version = connection.execute("SELECT project_id FROM reference_asset_versions WHERE id=?", (item.version_id,)).fetchone()
            if profile is None or version is None or profile["project_id"] != version["project_id"]:
                raise ValueError("ReferenceProfileItem project provenance 无效")
            connection.execute("INSERT INTO reference_profile_items(id,profile_id,version_id,role,order_index,created_at) VALUES (?,?,?,?,?,?)", (item.id, item.profile_id, item.version_id, item.role, item.order_index, item.created_at))
        return self.get_reference_profile_item(item.id)

    def get_reference_profile_item(self, item_id: str) -> ReferenceProfileItem | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM reference_profile_items WHERE id=?", (item_id,)).fetchone()
        return self._reference_profile_item_from_row(row) if row else None

    # Provider/runtime operation records --------------------------------
    @staticmethod
    def _capability_profile_from_row(row) -> CapabilityProfile:
        return CapabilityProfile(
            id=row["id"], project_id=row["project_id"], capability=row["capability"],
            provider_id=row["provider_id"], model_id=row["model_id"],
            profile=json.loads(row["profile_json"]), enabled=bool(row["enabled"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_capability_profile(self, profile: CapabilityProfile) -> CapabilityProfile:
        with connect(self.paths.database) as connection:
            if profile.project_id is not None and not self._project_exists(connection, profile.project_id):
                raise KeyError(f"项目不存在: {profile.project_id}")
            connection.execute(
                "INSERT INTO provider_capability_profiles(id,project_id,capability,provider_id,model_id,profile_json,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (profile.id, profile.project_id, profile.capability, profile.provider_id, profile.model_id,
                 json.dumps(profile.profile, ensure_ascii=False, sort_keys=True), int(profile.enabled), profile.created_at, profile.updated_at),
            )
        return self.get_capability_profile(profile.id)

    def get_capability_profile(self, profile_id: str) -> CapabilityProfile | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM provider_capability_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._capability_profile_from_row(row) if row else None

    def list_capability_profiles(self, project_id: str | None = None, capability: str | None = None) -> list[CapabilityProfile]:
        query = "SELECT * FROM provider_capability_profiles WHERE (project_id IS NULL OR project_id=?)"
        args: list[object] = [project_id]
        if capability is not None:
            query += " AND capability=?"; args.append(capability)
        query += " ORDER BY enabled DESC, updated_at DESC, id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._capability_profile_from_row(row) for row in rows]

    def update_capability_profile(self, profile: CapabilityProfile) -> CapabilityProfile:
        current = self.get_capability_profile(profile.id)
        if current is None:
            raise KeyError(f"CapabilityProfile 不存在: {profile.id}")
        with connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE provider_capability_profiles SET profile_json=?,enabled=?,updated_at=? WHERE id=?",
                (json.dumps(profile.profile, ensure_ascii=False, sort_keys=True), int(profile.enabled), profile.updated_at, profile.id),
            )
        return self.get_capability_profile(profile.id)

    @staticmethod
    def _provider_task_from_row(row) -> ProviderTask:
        return ProviderTask(
            id=row["id"], project_id=row["project_id"], execution_id=row["execution_id"],
            capability=row["capability"], provider_id=row["provider_id"], model_id=row["model_id"],
            idempotency_key=row["idempotency_key"], provider_task_id=row["provider_task_id"],
            state=row["state"], request_summary=json.loads(row["request_summary_json"]),
            metadata=json.loads(row["metadata_json"]), submitted_at=row["submitted_at"],
            last_polled_at=row["last_polled_at"], next_poll_at=row["next_poll_at"],
            error_message=row["error_message"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_provider_task(self, task: ProviderTask) -> ProviderTask:
        with connect(self.paths.database) as connection:
            if not self._project_exists(connection, task.project_id):
                raise KeyError(f"项目不存在: {task.project_id}")
            if task.execution_id is not None:
                execution = connection.execute(
                    "SELECT j.project_id FROM production_executions e JOIN production_jobs j ON j.id=e.production_job_id WHERE e.id=?",
                    (task.execution_id,),
                ).fetchone()
                if execution is None or execution["project_id"] != task.project_id:
                    raise ValueError("ProviderTask execution 不属于该项目")
            connection.execute(
                "INSERT INTO provider_tasks(id,project_id,execution_id,capability,provider_id,model_id,idempotency_key,provider_task_id,state,request_summary_json,metadata_json,submitted_at,last_polled_at,next_poll_at,error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, task.project_id, task.execution_id, task.capability, task.provider_id, task.model_id,
                 task.idempotency_key, task.provider_task_id, task.state,
                 json.dumps(task.request_summary, ensure_ascii=False, sort_keys=True),
                 json.dumps(task.metadata, ensure_ascii=False, sort_keys=True), task.submitted_at,
                 task.last_polled_at, task.next_poll_at, task.error_message, task.created_at, task.updated_at),
            )
        return self.get_provider_task(task.id)

    def get_provider_task(self, task_id: str) -> ProviderTask | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM provider_tasks WHERE id=?", (task_id,)).fetchone()
        return self._provider_task_from_row(row) if row else None

    def get_provider_task_by_idempotency(self, project_id: str, key: str) -> ProviderTask | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM provider_tasks WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
        return self._provider_task_from_row(row) if row else None

    def list_provider_tasks(self, project_id: str, *, state: str | None = None) -> list[ProviderTask]:
        query = "SELECT * FROM provider_tasks WHERE project_id=?"; args: list[object] = [project_id]
        if state is not None:
            query += " AND state=?"; args.append(state)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._provider_task_from_row(row) for row in rows]

    def update_provider_task(self, task: ProviderTask) -> ProviderTask:
        if self.get_provider_task(task.id) is None:
            raise KeyError(f"ProviderTask 不存在: {task.id}")
        with connect(self.paths.database) as connection:
            connection.execute(
                "UPDATE provider_tasks SET provider_task_id=?,state=?,request_summary_json=?,metadata_json=?,submitted_at=?,last_polled_at=?,next_poll_at=?,error_message=?,updated_at=? WHERE id=?",
                (task.provider_task_id, task.state, json.dumps(task.request_summary, ensure_ascii=False, sort_keys=True),
                 json.dumps(task.metadata, ensure_ascii=False, sort_keys=True), task.submitted_at, task.last_polled_at,
                 task.next_poll_at, task.error_message, task.updated_at, task.id),
            )
        return self.get_provider_task(task.id)

    @staticmethod
    def _vision_frame_manifest_from_row(row) -> VisionFrameManifest:
        return VisionFrameManifest(
            id=row["id"], project_id=row["project_id"], execution_id=row["execution_id"],
            artifact_id=row["artifact_id"], frame_count=row["frame_count"],
            samples=tuple(json.loads(row["samples_json"])), sha256=row["sha256"], created_at=row["created_at"],
        )

    def create_vision_frame_manifest(self, manifest: VisionFrameManifest) -> VisionFrameManifest:
        with connect(self.paths.database) as connection:
            execution = connection.execute(
                "SELECT j.project_id FROM production_executions e JOIN production_jobs j ON j.id=e.production_job_id WHERE e.id=?",
                (manifest.execution_id,),
            ).fetchone()
            if execution is None or execution["project_id"] != manifest.project_id:
                raise ValueError("VisionFrameManifest execution 不属于该项目")
            connection.execute(
                "INSERT INTO vision_frame_manifests(id,project_id,execution_id,artifact_id,frame_count,samples_json,sha256,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (manifest.id, manifest.project_id, manifest.execution_id, manifest.artifact_id, manifest.frame_count,
                 json.dumps(list(manifest.samples), ensure_ascii=False, sort_keys=True), manifest.sha256, manifest.created_at),
            )
        return self.get_vision_frame_manifest(manifest.id)

    def get_vision_frame_manifest(self, manifest_id: str) -> VisionFrameManifest | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM vision_frame_manifests WHERE id=?", (manifest_id,)).fetchone()
        return self._vision_frame_manifest_from_row(row) if row else None

    def list_vision_frame_manifests(self, project_id: str, execution_id: str | None = None) -> list[VisionFrameManifest]:
        query = "SELECT * FROM vision_frame_manifests WHERE project_id=?"; args: list[object] = [project_id]
        if execution_id is not None:
            query += " AND execution_id=?"; args.append(execution_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._vision_frame_manifest_from_row(row) for row in rows]

    @staticmethod
    def _vision_analysis_from_row(row) -> VisionAnalysisRecord:
        return VisionAnalysisRecord(
            id=row["id"], project_id=row["project_id"], execution_id=row["execution_id"], artifact_id=row["artifact_id"],
            frame_manifest_id=row["frame_manifest_id"], provider_id=row["provider_id"], model_id=row["model_id"],
            status=row["status"], metrics=json.loads(row["metrics_json"]),
            reference_comparison=json.loads(row["reference_comparison_json"]), created_at=row["created_at"],
        )

    def create_vision_analysis(self, analysis: VisionAnalysisRecord) -> VisionAnalysisRecord:
        with connect(self.paths.database) as connection:
            execution = connection.execute(
                "SELECT j.project_id FROM production_executions e JOIN production_jobs j ON j.id=e.production_job_id WHERE e.id=?",
                (analysis.execution_id,),
            ).fetchone()
            if execution is None or execution["project_id"] != analysis.project_id:
                raise ValueError("VisionAnalysis execution 不属于该项目")
            connection.execute(
                "INSERT INTO vision_analysis_results(id,project_id,execution_id,artifact_id,frame_manifest_id,provider_id,model_id,status,metrics_json,reference_comparison_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (analysis.id, analysis.project_id, analysis.execution_id, analysis.artifact_id, analysis.frame_manifest_id,
                 analysis.provider_id, analysis.model_id, analysis.status,
                 json.dumps(analysis.metrics, ensure_ascii=False, sort_keys=True),
                 json.dumps(analysis.reference_comparison, ensure_ascii=False, sort_keys=True), analysis.created_at),
            )
        return self.get_vision_analysis(analysis.id)

    def get_vision_analysis(self, analysis_id: str) -> VisionAnalysisRecord | None:
        with connect(self.paths.database) as connection:
            row = connection.execute("SELECT * FROM vision_analysis_results WHERE id=?", (analysis_id,)).fetchone()
        return self._vision_analysis_from_row(row) if row else None

    def list_vision_analyses(self, project_id: str, execution_id: str | None = None) -> list[VisionAnalysisRecord]:
        query = "SELECT * FROM vision_analysis_results WHERE project_id=?"; args: list[object] = [project_id]
        if execution_id is not None:
            query += " AND execution_id=?"; args.append(execution_id)
        query += " ORDER BY created_at,id"
        with connect(self.paths.database) as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return [self._vision_analysis_from_row(row) for row in rows]
