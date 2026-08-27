from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4
from aidrama_studio.domain import Project, ScriptRevisionStatus, Scene, ScriptBeat, ScriptBeatType, StructuredScript, InteriorExterior, TimeOfDay, StoryRevisionStatus
from aidrama_studio.services.llm_runtime import LLMInvocationError, LLMInvocationGateway
from aidrama_studio.services.script_parser import parse_structured_script
from aidrama_studio.services.script_prompt import build_script_prompt, build_script_repair_prompt
from aidrama_studio.storage import ProjectRepository
from .drafts import draft_state

def _now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds")

class ScriptServiceError(RuntimeError): pass

class ScriptService:
    def __init__(self, repository=None, *, llm_gateway: LLMInvocationGateway | None = None):
        self.repository = repository or ProjectRepository(); self._llm_gateway = llm_gateway or LLMInvocationGateway(self.repository)
    def llm_readiness(self, project_id):
        return self._llm_gateway.readiness(project_id)
    def get_revision(self, revision_id): return self.repository.get_script_revision(revision_id)
    def list_revisions(self, project_id): return self.repository.list_script_revisions(project_id)
    def get_latest_draft(self, project_id):
        """Return the newest durable Draft for cold-restart recovery."""
        return next((item for item in self.list_revisions(project_id) if item["status"] is ScriptRevisionStatus.DRAFT), None)
    def recover_draft(self, project_id, revision_id=None):
        revision = self.get_revision(revision_id) if revision_id else self.get_latest_draft(project_id)
        if revision is None: return None
        if revision["project_id"] != project_id: raise ValueError("Structured Script Draft 不属于该项目")
        if revision["status"] is not ScriptRevisionStatus.DRAFT: raise ValueError("只有 DRAFT revision 可以恢复")
        return revision
    @staticmethod
    def draft_state(revision, working): return draft_state(revision, working)
    def get_latest_revision(self, project_id):
        items = self.list_revisions(project_id); return items[0] if items else None
    def get_approved_revision(self, project_id):
        return next((x for x in self.list_revisions(project_id) if x["status"] is ScriptRevisionStatus.APPROVED), None)
    def _next_version(self, project_id):
        latest = self.get_latest_revision(project_id); return latest["version"] + 1 if latest else 1
    def _create(self, project_id, source_story_revision_id, content, generation_input=None):
        now = _now(); return self.repository.create_script_revision(revision_id=uuid4().hex, project_id=project_id, version=self._next_version(project_id), status=ScriptRevisionStatus.DRAFT, source_story_revision_id=source_story_revision_id, content=content, generation_input=generation_input, created_at=now, updated_at=now)
    def create_manual_script(self, project: Project, story_revision: dict[str, Any] | None = None) -> dict[str, Any]:
        if story_revision is None:
            story_revision = next((x for x in self.repository.list_story_revisions(project.id) if x["status"] is StoryRevisionStatus.APPROVED), None)
        if not story_revision or story_revision["status"] is not StoryRevisionStatus.APPROVED:
            raise ScriptServiceError("请先确认 Story Bible")
        story = story_revision["content"]; location = story.locations[0]; character = story.characters[0]
        content = StructuredScript(title=story.title, summary="", scenes=[Scene(id="scene_001", order=1, title=location.name, location_id=location.id, character_ids=[character.id], estimated_duration_seconds=float(project.target_duration_seconds), beats=[ScriptBeat(id="beat_001", order=1, type=ScriptBeatType.ACTION, text="（待填写）", estimated_duration_seconds=float(project.target_duration_seconds))])])
        content.validate_against(story); return self._create(project.id, story_revision["id"], content)
    def save_draft(self, project_id, content, *, revision_id=None, generation_input=None):
        revision = self.get_revision(revision_id) if revision_id else None
        source_id = revision["source_story_revision_id"] if revision else None
        if source_id:
            source = self.repository.get_story_revision(source_id)
            if source:
                content.validate_against(source["content"])
        if revision and revision["status"] is ScriptRevisionStatus.DRAFT: return self.repository.update_script_revision(revision_id, content=content, updated_at=_now(), generation_input=generation_input)
        if revision and revision["status"] is ScriptRevisionStatus.APPROVED: return self._create(project_id, revision["source_story_revision_id"], content, generation_input)
        latest = self.get_latest_revision(project_id)
        if latest and latest["status"] is ScriptRevisionStatus.DRAFT:
            source = self.repository.get_story_revision(latest["source_story_revision_id"])
            if source: content.validate_against(source["content"])
            return self.repository.update_script_revision(latest["id"], content=content, updated_at=_now(), generation_input=generation_input)
        raise ValueError("新建剧本必须提供 source story revision")
    def create_revision_from_approved(self, revision_id):
        rev = self.get_revision(revision_id)
        if not rev or rev["status"] is not ScriptRevisionStatus.APPROVED: raise ValueError("只有 APPROVED revision 可以创建新的 DRAFT")
        return self._create(rev["project_id"], rev["source_story_revision_id"], rev["content"])

    def create_revision_from_story(self, project_id: str, story_revision_id: str, content: StructuredScript | None = None):
        """Create a new Draft pinned to the current Story revision.

        This is the repair entry for an outdated script.  It preserves human
        content when it still validates against the new Story; otherwise the
        caller receives a validation error instead of silently dropping edits.
        """
        project = self.repository.get_project(project_id)
        story = self.repository.get_story_revision(story_revision_id)
        if project is None or story is None or story["project_id"] != project_id:
            raise ValueError("Story Bible revision 不属于该项目")
        if story["status"] is not StoryRevisionStatus.APPROVED:
            raise ValueError("修复必须基于 APPROVED Story Bible")
        if content is None:
            return self.create_manual_script(project, story)
        normalized = StructuredScript.model_validate(content.model_dump(mode="json"))
        normalized.validate_against(story["content"])
        return self._create(project_id, story_revision_id, normalized)
    def is_outdated(self, revision_or_id):
        rev = self.get_revision(revision_or_id) if isinstance(revision_or_id, str) else revision_or_id
        if not rev: return False
        stories = self.repository.list_story_revisions(rev["project_id"]); current = next((x for x in stories if x["status"] is StoryRevisionStatus.APPROVED), None)
        return current is not None and rev["source_story_revision_id"] != current["id"]
    def approve_revision(self, revision_id):
        rev = self.get_revision(revision_id)
        if not rev: raise KeyError("Structured Script revision 不存在")
        if self.is_outdated(rev): raise ValueError("当前剧本基于旧版 Story Bible，需重新同步后才能批准")
        rev["content"].validate_against(self.repository.get_story_revision(rev["source_story_revision_id"])["content"])
        return self.repository.approve_script_revision(revision_id, updated_at=_now())

    def duration_status(self, revision_or_id, target_duration_seconds: int) -> dict[str, float | bool]:
        revision = self.get_revision(revision_or_id) if isinstance(revision_or_id, str) else revision_or_id
        total = revision["content"].total_estimated_duration_seconds
        lower, upper = target_duration_seconds * 0.85, target_duration_seconds * 1.15
        return {"total": total, "target": float(target_duration_seconds), "within_tolerance": lower <= total <= upper}
    def generate_script(
        self,
        project: Project,
        *,
        dialogue_density="standard",
        narration="少量",
        pacing="standard",
        source_story_revision_id: str | None = None,
        generation_provenance: Mapping[str, object] | None = None,
    ):
        story = (
            self.repository.get_story_revision(source_story_revision_id)
            if source_story_revision_id
            else next(
                (
                    x
                    for x in self.repository.list_story_revisions(project.id)
                    if x["status"] is StoryRevisionStatus.APPROVED
                ),
                None,
            )
        )
        if story is not None and story["project_id"] != project.id:
            raise ScriptServiceError("Story Bible revision 不属于该项目")
        if not story: raise ScriptServiceError("请先确认 Story Bible")
        if story["status"] is not StoryRevisionStatus.APPROVED:
            raise ScriptServiceError("Script AI 必须使用 APPROVED Story Bible")
        prompt = build_script_prompt(project, story["content"], dialogue_density=dialogue_density, narration=narration, pacing=pacing)
        def validate(raw):
            content = parse_structured_script(raw); content.validate_against(story["content"]); return content
        try:
            content = self._llm_gateway.generate_validated_json(project.id, prompt, operation="STRUCTURED_SCRIPT_GENERATION", validator=validate, repair_prompt_builder=lambda raw, exc: build_script_repair_prompt(raw, str(exc)), input_source_ids=(story["id"],), provenance=generation_provenance)
        except LLMInvocationError as exc:
            raise ScriptServiceError(str(exc)) from exc
        except Exception as exc:
            raise ScriptServiceError("结构化剧本生成失败，请稍后重试。") from exc
        return self._create(project.id, story["id"], content, {"dialogue_density":dialogue_density,"narration":narration,"pacing":pacing,"target_duration_seconds":project.target_duration_seconds,"aspect_ratio":project.aspect_ratio.value} | dict(generation_provenance or {}))
