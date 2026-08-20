from __future__ import annotations

from pathlib import Path

from aidrama_studio.domain import AspectRatio, Project, ProjectStatus

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
