from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from streamlit.testing.v1 import AppTest

from aidrama_studio.domain import (
    Character,
    Location,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    World,
)
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
)
from aidrama_studio.services.creative_intake import CreativeIntakeService
from aidrama_studio.services.creative_pipeline import (
    CreativePipelineError,
    CreativePipelineService,
    ProductActivityAdapter,
)
from aidrama_studio.services.llm_runtime import LLMInvocationGateway
from aidrama_studio.services.project import ProjectService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


@dataclass
class _FakeLLMProvider:
    responses: list[object]
    provider_name: str
    model: str
    available: bool = True
    calls: int = field(default=0, init=False)
    capability: CapabilityKind = CapabilityKind.LLM

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            CapabilityKind.LLM,
            self.provider_name,
            self.available,
            "configured" if self.available else "unavailable",
            {
                "model": self.model,
                "configured": self.available,
                "deployment_region": "LOCAL",
                "endpoint_class": "FAKE_LOCAL_LLM",
                "endpoint_profile_id": f"runtime:LLM:{self.provider_name}:local",
                "verification_state": "NOT_VERIFIED",
            },
            configured=self.available,
        )

    def generate_json_text(self, _prompt: str) -> str:
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return str(value)


def _paths(tmp_path) -> DatabasePaths:
    return DatabasePaths(
        database=tmp_path / "temporary-aidrama" / "aidrama.db",
        projects=tmp_path / "temporary-aidrama" / "projects",
        archived_projects=tmp_path / "temporary-aidrama" / "archived",
    )


def _story() -> StoryBible:
    return StoryBible(
        title="末班车",
        logline="一个夜班司机在终点站面对被遗忘的选择。",
        premise="林舟必须在最后一班车离站前回答沈遥的秘密。",
        genre="悬疑",
        tone="克制",
        themes=["选择"],
        world=World(era="当代", setting="夜班公交", rules=[], timeline_notes="一夜"),
        characters=[Character(id="char_001", name="林舟", role="主角")],
        locations=[Location(id="loc_001", name="末班车", function="冲突")],
        story_beats=[
            StoryBeat(id="beat_001", order=1, type="OPENING", summary="林舟发车", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_002", order=2, type="TURNING_POINT", summary="乘客出现", characters=["char_001"], location_id="loc_001"),
            StoryBeat(id="beat_003", order=3, type="ENDING", summary="林舟做出选择", characters=["char_001"], location_id="loc_001"),
        ],
    )


def _script() -> dict[str, object]:
    return {
        "title": "末班车",
        "summary": "一夜的选择",
        "scenes": [
            {
                "id": "scene_001",
                "order": 1,
                "title": "末班车",
                "location_id": "loc_001",
                "character_ids": ["char_001"],
                "estimated_duration_seconds": 60,
                "source_story_beat_ids": ["beat_001", "beat_002", "beat_003"],
                "beats": [
                    {
                        "id": "script_beat_001",
                        "order": 1,
                        "type": "ACTION",
                        "text": "林舟发动末班车。",
                        "estimated_duration_seconds": 30,
                    },
                    {
                        "id": "script_beat_002",
                        "order": 2,
                        "type": "ACTION",
                        "text": "林舟在终点站停下。",
                        "estimated_duration_seconds": 30,
                    },
                ],
            }
        ],
    }


def _plan() -> dict[str, object]:
    payload = ShotPlan(
        title="末班车分镜",
        source_script_revision_id="product-injects-this-provenance",
        shots=[
            Shot(
                id=f"shot_{order:03d}",
                order=order,
                scene_id="scene_001",
                source_script_beat_ids=[
                    "script_beat_001" if order <= 4 else "script_beat_002"
                ],
                # Deliberately non-authoritative: ShotService replaces these
                # with the frozen manifest-derived 60-second plan.
                duration_seconds=1,
                subject=["char_001"],
                action=f"末班车动作 {order}",
                visual_intent=f"镜头视觉意图 {order}",
            )
            for order in range(1, 9)
        ],
    ).model_dump(mode="json")
    payload.pop("source_script_revision_id")
    return payload


def _pipeline(tmp_path, provider: _FakeLLMProvider):
    repository = ProjectRepository(_paths(tmp_path))
    project = ProjectService(repository).create(
        title="Fake Creative Pipeline", description="末班车的选择"
    )
    gateway = LLMInvocationGateway(
        repository, registry=CapabilityRegistry([provider])
    )
    return (
        repository,
        project,
        CreativePipelineService(
            repository,
            story_service=StoryService(repository, llm_gateway=gateway),
            script_service=ScriptService(repository, llm_gateway=gateway),
            shot_service=ShotService(repository, llm_gateway=gateway),
        ),
    )


def _approved_intake(repository: ProjectRepository, project) -> object:
    intake = CreativeIntakeService(repository)
    source = intake.source_pack.import_text(project.id, "末班车司机在终点站遇见未来的自己。")
    brief = intake.normalize(
        project.id,
        source_ids=[source.id],
        overrides={"genre": "悬疑", "tone": "克制"},
    )
    return intake.approve_brief(project.id, brief.id)


@pytest.mark.parametrize(
    ("provider_name", "model"),
    [("QWEN_FAKE", "qwen-fake-v1"), ("DEEPSEEK_FAKE", "deepseek-fake-v1")],
)
def test_fake_full_creative_pipeline_is_universal_and_versioned(
    tmp_path, provider_name, model
):
    provider = _FakeLLMProvider(
        [json.dumps(_story().model_dump(mode="json")), json.dumps(_script()), json.dumps(_plan())],
        provider_name=provider_name,
        model=model,
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)

    story = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": brief.id},
    )
    assert story["status"] is StoryRevisionStatus.DRAFT
    assert story["generation_input"]["normalized_brief_id"] == brief.id
    # Replaying the same product action returns its durable activity result.
    assert pipeline.execute(
        project_id=project.id,
        operation="STORY_BIBLE_GENERATION",
        payload={"normalized_brief_id": brief.id},
    )["id"] == story["id"]
    assert provider.calls == 1

    approved_story = StoryService(repository).approve_revision(story["id"])
    script = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SCRIPT",
        payload={"source_story_revision_id": approved_story["id"]},
    )
    assert script["status"] is ScriptRevisionStatus.DRAFT
    assert script["source_story_revision_id"] == approved_story["id"]
    approved_script = ScriptService(repository).approve_revision(script["id"])

    # A newer unapproved Script must not replace the exact approved input.
    ScriptService(repository).create_revision_from_approved(approved_script["id"])
    plan = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SHOT_PLAN",
        payload={"source_script_revision_id": approved_script["id"]},
    )
    assert plan["status"] is ShotRevisionStatus.DRAFT
    assert plan["source_script_revision_id"] == approved_script["id"]
    assert plan["content"].source_script_revision_id == approved_script["id"]
    assert plan["content"].total_duration_seconds == 60
    approved_plan = ShotService(repository).approve_revision(plan["id"])
    assert approved_plan["status"] is ShotRevisionStatus.APPROVED

    activities = repository.list_creative_pipeline_operations(project.id)
    assert len(activities) == 3
    assert {item.status.value for item in activities} == {"WAITING_HUMAN"}
    assert {item.provider_id for item in activities} == {provider_name}
    assert {item.model_id for item in activities} == {model}
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"] * 3
    assert all(item.request_summary["llm_runtime"] == "UNIVERSAL" for item in invocations)
    assert all(item.request_summary["protocol"] == "REQUEST_RESPONSE" for item in invocations)
    assert all(item.request_summary["provenance"]["input_hash"] for item in invocations)
    assert provider.calls == 3


@pytest.mark.parametrize("invalid", ["not-json", "{}"])
def test_invalid_or_schema_mismatched_output_stops_after_one_repair_and_preserves_prior_approved_revision(
    tmp_path, invalid
):
    provider = _FakeLLMProvider([invalid, invalid], "QWEN_FAKE", "qwen-fake-v1")
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    previous = StoryService(repository).create_blank_draft(project)
    StoryService(repository).approve_revision(previous["id"])

    with pytest.raises(CreativePipelineError, match="一次修复"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_STORY",
            payload={"normalized_brief_id": brief.id, "regenerate": True},
        )

    assert provider.calls == 2
    assert StoryService(repository).get_revision(previous["id"])["status"] is StoryRevisionStatus.APPROVED
    assert [item.status for item in repository.list_ai_invocations(project.id)] == [
        "STARTED", "FAILED", "STARTED", "FAILED"
    ]
    assert repository.list_creative_pipeline_operations(project.id)[0].status.value == "FAILED"


def test_pipeline_rejects_draft_story_and_draft_script_even_when_they_are_newer(tmp_path):
    provider = _FakeLLMProvider(
        [json.dumps(_story().model_dump(mode="json"))], "QWEN_FAKE", "qwen-fake-v1"
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    draft_story = StoryService(repository).create_blank_draft(project)

    with pytest.raises(CreativePipelineError, match="APPROVED Story Bible"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_SCRIPT",
            payload={"source_story_revision_id": draft_story["id"]},
        )

    approved_story = StoryService(repository).approve_revision(
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_STORY",
            payload={"normalized_brief_id": brief.id},
        )["id"]
    )
    newer_draft_script = ScriptService(repository).create_manual_script(
        project, approved_story
    )
    with pytest.raises(CreativePipelineError, match="APPROVED Structured Script"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_SHOT_PLAN",
            payload={"source_script_revision_id": newer_draft_script["id"]},
        )

    assert provider.calls == 1
    assert repository.list_shot_revisions(project.id) == []


def test_pipeline_rejects_missing_approved_upstream_and_unavailable_provider_without_calls(tmp_path):
    provider = _FakeLLMProvider([], "UNAVAILABLE_FAKE", "none", available=False)
    repository, project, pipeline = _pipeline(tmp_path, provider)

    with pytest.raises(CreativePipelineError, match="Story Bible"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_SCRIPT",
            payload={"source_story_revision_id": "missing"},
        )
    with pytest.raises(CreativePipelineError):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_STORY",
            payload={"normalized_brief_id": _approved_intake(repository, project).id},
        )

    assert provider.calls == 0
    assert repository.list_story_revisions(project.id) == []


def test_product_activity_adapter_routes_creative_operations_and_preserves_other_fallbacks(tmp_path):
    provider = _FakeLLMProvider([json.dumps(_story().model_dump(mode="json"))], "QWEN_FAKE", "qwen-fake-v1")
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    adapter = ProductActivityAdapter(pipeline, fallback=lambda *_args: {"fallback": True})

    result = adapter(project.id, "STORY_BIBLE_GENERATION", {"normalized_brief_id": brief.id})
    assert result["id"]
    assert adapter(project.id, "REFERENCE_IMAGE_CANDIDATE", {}) == {"fallback": True}


def test_formal_story_script_and_shot_generation_ctas_render_in_apptest():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.domain import StoryRevisionStatus
from aidrama_studio.pages import director, story

project = SimpleNamespace(
    id='creative-ui-project', title='Creative UI', description='brief',
    target_duration_seconds=60, aspect_ratio=SimpleNamespace(value='16:9'),
)
brief = SimpleNamespace(
    id='approved-intake', status='APPROVED', title_candidate='Brief', premise='A durable intake',
    genre='Drama', tone='Calm', story_information={'audience': 'Adults'},
    source_ids=('source-1',), constraints=(), visual_direction={},
)

class FakeStory:
    repository = object()
    def llm_readiness(self, project_id): return True, 'ready'
    def list_revisions(self, project_id): return []

class FakeScript:
    def llm_readiness(self, project_id): return True, 'ready'
    def list_revisions(self, project_id): return []

class FakeShot:
    def llm_readiness(self, project_id): return True, 'ready'

story._latest_normalized_brief = lambda *_args: brief
story._render_brief(project, FakeStory())
story._render_script_editor(
    project,
    {'id': 'story-approved', 'status': StoryRevisionStatus.APPROVED},
    FakeScript(),
)
director._shot_plan_context = lambda _project: (
    {'id': 'script-approved', 'version': 1}, FakeShot(), [], None,
)
director._render_shot_plan(project)
"""
    ).run(timeout=30)
    assert not app.exception
    labels = {item.label for item in app.button}
    assert "生成 Story Bible 草稿" in labels
    assert "准备结构化剧本草稿" in labels
    assert "AI 生成分镜草稿" in labels
