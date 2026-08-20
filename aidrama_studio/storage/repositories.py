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
