from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import CreativeLock
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class CreativeControlError(RuntimeError):
    pass


class CreativeLockService:
    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def lock(
        self,
        project_id: str,
        entity_kind: str,
        stable_entity_id: str,
        field_path: str = "*",
        *,
        source_revision_id: str | None = None,
        reason: str = "",
    ) -> CreativeLock:
        if self.repository.get_project(project_id) is None:
            raise CreativeControlError(f"项目不存在: {project_id}")
        kind = str(entity_kind).strip().upper()
        entity_id = str(stable_entity_id).strip()
        path = str(field_path).strip()
        if not kind or not entity_id or not path:
            raise CreativeControlError("CreativeLock identity/path 不能为空")
        if source_revision_id is not None:
            getter = {
                "STORY": self.repository.get_story_revision,
                "SCRIPT": self.repository.get_script_revision,
                "SHOT": self.repository.get_shot_revision,
                "GENERATION_BRIEF": self.repository.get_generation_brief,
            }.get(kind)
            revision = getter(source_revision_id) if getter else None
            revision_project = (
                revision.get("project_id") if isinstance(revision, dict)
                else getattr(revision, "project_id", None)
            )
            if revision is None or revision_project != project_id:
                raise CreativeControlError("CreativeLock source revision provenance 不匹配")
        existing = next(
            (
                item for item in self.repository.list_creative_locks(
                    project_id, entity_kind=kind, stable_entity_id=entity_id, active_only=True
                )
                if item.field_path == path
            ),
            None,
        )
        if existing is not None:
            return existing
        return self.repository.create_creative_lock(
            CreativeLock(
                id=uuid4().hex, project_id=project_id, entity_kind=kind,
                stable_entity_id=entity_id, field_path=path,
                source_revision_id=source_revision_id, reason=str(reason).strip(),
                created_at=_now(),
            )
        )

    def release(self, project_id: str, lock_id: str) -> CreativeLock:
        lock = next(
            (item for item in self.repository.list_creative_locks(project_id) if item.id == lock_id),
            None,
        )
        if lock is None:
            raise CreativeControlError("CreativeLock 不属于该项目")
        return self.repository.release_creative_lock(lock.id, released_at=_now())

    def release_path(self, project_id: str, entity_kind: str, stable_entity_id: str, field_path: str = "*") -> tuple[CreativeLock, ...]:
        released = []
        for item in self.repository.list_creative_locks(
            project_id, entity_kind=entity_kind.upper(), stable_entity_id=stable_entity_id,
            active_only=True,
        ):
            if item.field_path == field_path:
                released.append(self.repository.release_creative_lock(item.id, released_at=_now()))
        return tuple(released)

    def active(self, project_id: str, *, entity_kind: str | None = None, stable_entity_id: str | None = None) -> tuple[CreativeLock, ...]:
        return tuple(
            self.repository.list_creative_locks(
                project_id, entity_kind=entity_kind.upper() if entity_kind else None,
                stable_entity_id=stable_entity_id, active_only=True,
            )
        )


__all__ = ["CreativeControlError", "CreativeLockService"]
