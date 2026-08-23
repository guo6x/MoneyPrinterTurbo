from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4
from aidrama_studio.domain import Project, ScriptRevisionStatus, Scene, ScriptBeat, ScriptBeatType, StructuredScript, InteriorExterior, TimeOfDay, StoryRevisionStatus
from aidrama_studio.services import ai
from aidrama_studio.services.script_parser import parse_structured_script, StructuredScriptParseError
from aidrama_studio.services.script_prompt import build_script_prompt, build_script_repair_prompt
from aidrama_studio.storage import ProjectRepository

def _now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds")

class ScriptServiceError(RuntimeError): pass

class ScriptService:
    def __init__(self, repository=None, *, text_generator: Callable[[str, Mapping[str, Any]], str] | None = None, config_snapshot_provider=None):
        self.repository = repository or ProjectRepository(); self._text_generator = text_generator or ai.generate_text; self._snapshot_provider = config_snapshot_provider or ai.snapshot_llm_config
    def get_revision(self, revision_id): return self.repository.get_script_revision(revision_id)
    def list_revisions(self, project_id): return self.repository.list_script_revisions(project_id)
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
    def generate_script(self, project: Project, *, dialogue_density="standard", narration="少量", pacing="standard"):
        story = next((x for x in self.repository.list_story_revisions(project.id) if x["status"] is StoryRevisionStatus.APPROVED), None)
        if not story: raise ScriptServiceError("请先确认 Story Bible")
        prompt = build_script_prompt(project, story["content"], dialogue_density=dialogue_density, narration=narration, pacing=pacing); snapshot = self._snapshot_provider()
        try:
            raw = self._text_generator(prompt, snapshot)
            try:
                content = parse_structured_script(raw); content.validate_against(story["content"])
            except (StructuredScriptParseError, ValueError) as first:
                repaired = self._text_generator(build_script_repair_prompt(getattr(first, "raw", raw), str(first)), snapshot)
                content = parse_structured_script(repaired); content.validate_against(story["content"])
        except ai.AIDramaAIError as exc:
            raise ScriptServiceError(str(exc)) from exc
        except Exception as exc:
            raise ScriptServiceError("结构化剧本生成失败，请稍后重试。") from exc
        return self._create(project.id, story["id"], content, {"dialogue_density":dialogue_density,"narration":narration,"pacing":pacing,"target_duration_seconds":project.target_duration_seconds,"aspect_ratio":project.aspect_ratio.value})
