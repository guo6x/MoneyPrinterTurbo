"""Project-scoped dependency projections for revision-aware UI.

Revision tables are append-only, but consumers should not infer dependency
state by comparing unrelated historical rows.  This service resolves the
current approved chain first and then reports only the downstream revisions
that actually point at an older source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from aidrama_studio.domain import (
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """A single source -> current revision edge suitable for UI rendering."""

    project_id: str
    entity_type: str
    revision_id: str | None
    revision_version: int | None
    source_entity_type: str | None
    source_revision_id: str | None
    source_revision_version: int | None
    current_revision_id: str | None
    current_revision_version: int | None
    outdated: bool
    affected_downstream: tuple[str, ...] = ()
    repair_action: str | None = None

    @property
    def source_to_current(self) -> str:
        """Human-readable revision mapping used consistently by pages."""

        source = (
            f"v{self.source_revision_version} ({self.source_revision_id})"
            if self.source_revision_id
            else "—"
        )
        current = (
            f"v{self.current_revision_version} ({self.current_revision_id})"
            if self.current_revision_id
            else "—"
        )
        return f"{source} → {current}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["affected_downstream"] = list(self.affected_downstream)
        value["source_to_current"] = self.source_to_current
        return value


class DependencyStatusService:
    """Resolve current approved revisions and their direct dependants."""

    def __init__(self, repository: ProjectRepository | None = None):
        self.repository = repository or ProjectRepository()

    @staticmethod
    def _approved(items: Iterable[dict[str, Any]], status_type: Any) -> dict[str, Any] | None:
        return next((item for item in items if item["status"] is status_type.APPROVED), None)

    def _require_project(self, project_id: str) -> None:
        if self.repository.get_project(project_id) is None:
            raise ValueError(f"项目不存在: {project_id}")

    def _current(self, project_id: str) -> dict[str, dict[str, Any] | None]:
        self._require_project(project_id)
        return {
            "story": self._approved(
                self.repository.list_story_revisions(project_id), StoryRevisionStatus
            ),
            "script": self._approved(
                self.repository.list_script_revisions(project_id), ScriptRevisionStatus
            ),
            "shot_plan": self._approved(
                self.repository.list_shot_revisions(project_id), ShotRevisionStatus
            ),
        }

    @staticmethod
    def _label(kind: str, revision: Any) -> str:
        if isinstance(revision, dict):
            version = revision.get("version")
            identity = revision.get("id")
        else:
            version = getattr(revision, "version", None)
            identity = getattr(revision, "id", None)
        suffix = f" v{version}" if version is not None else ""
        return f"{kind}{suffix}" + (f" ({identity})" if identity else "")

    def status_for_story(
        self, project_id: str, revision: dict[str, Any] | str | None = None
    ) -> DependencyStatus:
        current = self._current(project_id)
        if isinstance(revision, str):
            selected = self.repository.get_story_revision(revision)
            if selected is None:
                raise ValueError("Story Bible revision 不存在")
        else:
            selected = revision or current["story"]
        if selected is not None and selected["project_id"] != project_id:
            raise ValueError("Story Bible revision 不属于该项目")
        affected: list[str] = []
        if selected is not None:
            for script in self.repository.list_script_revisions(project_id):
                if script["source_story_revision_id"] == selected["id"] and script["id"] != (current["script"] or {}).get("id"):
                    affected.append(self._label("Structured Script", script))
        current_story = current["story"]
        return DependencyStatus(
            project_id=project_id,
            entity_type="STORY_BIBLE",
            revision_id=selected["id"] if selected else None,
            revision_version=selected["version"] if selected else None,
            source_entity_type=None,
            source_revision_id=None,
            source_revision_version=None,
            current_revision_id=current_story["id"] if current_story else None,
            current_revision_version=current_story["version"] if current_story else None,
            outdated=bool(selected and current_story and selected["id"] != current_story["id"]),
            affected_downstream=tuple(affected),
            repair_action="批准或创建最新 Story Bible revision" if selected and selected.get("status") is not StoryRevisionStatus.APPROVED else None,
        )

    def status_for_script(
        self, project_id: str, revision: dict[str, Any] | str | None = None
    ) -> DependencyStatus:
        current = self._current(project_id)
        if isinstance(revision, str):
            selected = self.repository.get_script_revision(revision)
            if selected is None:
                raise ValueError("Structured Script revision 不存在")
        else:
            selected = revision or current["script"]
        if selected is not None and selected["project_id"] != project_id:
            raise ValueError("Structured Script revision 不属于该项目")
        source_id = selected.get("source_story_revision_id") if selected else None
        source = self.repository.get_story_revision(source_id) if source_id else None
        current_story = current["story"]
        affected = tuple(
            self._label("Shot Plan", plan)
            for plan in self.repository.list_shot_revisions(project_id)
            if selected
            and plan["source_script_revision_id"] == selected["id"]
            and plan["id"] != (current["shot_plan"] or {}).get("id")
        )
        outdated = bool(selected and current_story and source_id != current_story["id"])
        return DependencyStatus(
            project_id=project_id,
            entity_type="STRUCTURED_SCRIPT",
            revision_id=selected["id"] if selected else None,
            revision_version=selected["version"] if selected else None,
            source_entity_type="STORY_BIBLE" if source_id else None,
            source_revision_id=source_id,
            source_revision_version=source["version"] if source else None,
            current_revision_id=current_story["id"] if current_story else None,
            current_revision_version=current_story["version"] if current_story else None,
            outdated=outdated,
            affected_downstream=affected,
            repair_action=(
                "从最新 APPROVED Story Bible 创建 Structured Script Draft"
                if outdated and current_story
                else None
            ),
        )

    def status_for_shot_plan(
        self, project_id: str, revision: dict[str, Any] | str | None = None
    ) -> DependencyStatus:
        current = self._current(project_id)
        if isinstance(revision, str):
            selected = self.repository.get_shot_revision(revision)
            if selected is None:
                raise ValueError("Shot Plan revision 不存在")
        else:
            selected = revision or current["shot_plan"]
        if selected is not None and selected["project_id"] != project_id:
            raise ValueError("Shot Plan revision 不属于该项目")
        source_id = selected.get("source_script_revision_id") if selected else None
        source = self.repository.get_script_revision(source_id) if source_id else None
        current_script = current["script"]
        affected = tuple(
            self._label("Production Job", job)
            for job in self.repository.list_production_jobs(project_id)
            if selected and job.shot_plan_revision_id == selected["id"]
        )
        outdated = bool(selected and current_script and source_id != current_script["id"])
        return DependencyStatus(
            project_id=project_id,
            entity_type="SHOT_PLAN",
            revision_id=selected["id"] if selected else None,
            revision_version=selected["version"] if selected else None,
            source_entity_type="STRUCTURED_SCRIPT" if source_id else None,
            source_revision_id=source_id,
            source_revision_version=source["version"] if source else None,
            current_revision_id=current_script["id"] if current_script else None,
            current_revision_version=current_script["version"] if current_script else None,
            outdated=outdated,
            affected_downstream=affected,
            repair_action=(
                "从最新 APPROVED Structured Script 创建 Shot Plan Draft"
                if outdated and current_script
                else None
            ),
        )

    def project(self, project_id: str) -> dict[str, Any]:
        """Return one stable projection for dashboards and page notices."""

        story = self.status_for_story(project_id)
        script = self.status_for_script(project_id)
        shot_plan = self.status_for_shot_plan(project_id)
        statuses = (story, script, shot_plan)
        return {
            "project_id": project_id,
            "current": {
                "story": story.current_revision_id,
                "script": script.current_revision_id,
                "shot_plan": shot_plan.current_revision_id,
            },
            "dependencies": [item.to_dict() for item in statuses],
            "outdated": [item.to_dict() for item in statuses if item.outdated],
        }

    # Friendly aliases for callers/tests that prefer explicit terminology.
    get_project_status = project
    get_status = project


__all__ = ["DependencyStatus", "DependencyStatusService"]
