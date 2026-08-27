from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character,
    Location,
    ReferenceBindingType,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
)
from aidrama_studio.domain.reference_agent import (
    ReferenceActionKind,
    ReferenceCoverageStatus,
)
from aidrama_studio.services import (
    CapabilityKind,
    CapabilityStatus,
    ImageCandidate,
    ImageGenerationProvider,
    ImageRuntimeService,
    ProjectService,
    ReferenceAgentError,
    ReferenceAgentService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes


class FakeImageProvider(ImageGenerationProvider):
    """A local deterministic provider; tests never perform network I/O."""

    provider_name = "FAKE_AUTONOMOUS_REFERENCE_IMAGE"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            CapabilityKind.IMAGE,
            self.provider_name,
            True,
            "local fake image provider",
            {
                "model": "fake-reference-image-v1",
                "configured": True,
                "deployment_region": "LOCAL",
                "endpoint_class": "TEST",
                "endpoint_profile_id": "runtime:IMAGE:FAKE_AUTONOMOUS_REFERENCE_IMAGE:TEST",
                "verification_state": "VERIFIED",
            },
            configured=True,
            verified=True,
        )

    def generate_candidate(self, prompt, *, project_id, metadata=None):
        colors = ("purple", "orange", "black")
        color = colors[len(self.calls)]
        self.calls.append(
            {"project_id": project_id, "prompt": prompt, "metadata": dict(metadata or {})}
        )
        return ImageCandidate(
            project_id=project_id,
            provider=self.provider_name,
            prompt=prompt,
            content=png_bytes(color=color),
            mime_type="image/png",
            metadata={**dict(metadata or {}), "request_parameters": {"n": 1}},
        )


@pytest.fixture
def context(tmp_path: Path):
    paths = DatabasePaths(
        tmp_path / "db" / "aidrama.db",
        tmp_path / "db" / "projects",
        tmp_path / "db" / "archived",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Autonomous reference project")
    story = StoryBible(
        title="Rain Signal",
        logline="Two people follow a signal through a rain-soaked city.",
        premise="A precise visual story about trust and a mysterious signal.",
        genre="Science fiction drama",
        tone="Nocturnal and intimate",
        world=World(era="Near future", setting="Rain-soaked coastal city"),
        characters=[
            Character(
                id="character_a",
                name="Ari",
                role="signal analyst",
                age_or_range="late twenties",
                identity="Ari, the analytical lead",
                appearance="short black hair, amber raincoat, waterproof satchel",
            ),
            Character(
                id="character_b",
                name="Bo",
                role="courier",
                age_or_range="early thirties",
                identity="Bo, the guarded courier",
                appearance="silver undercut, navy utility jacket, red scarf",
            ),
        ],
        locations=[
            Location(
                id="location_a",
                name="Signal Lab",
                environment="compact laboratory with wet windows and monitors",
                time_of_day="NIGHT",
                visual_style="cool cyan practical lights",
                key_props=["signal console"],
            ),
            Location(
                id="location_b",
                name="Transit Platform",
                environment="empty elevated rail platform in heavy rain",
                time_of_day="NIGHT",
                visual_style="sodium vapor reflections",
                key_props=["arrival board"],
            ),
            Location(
                id="location_c",
                name="Harbor Steps",
                environment="stone harbor steps looking toward dark water",
                time_of_day="DAWN",
                visual_style="misty blue horizon",
                key_props=["rusted handrail"],
            ),
        ],
        story_beats=[
            StoryBeat(id="story_beat_01", order=1, type="OPENING", summary="Signal arrives", characters=["character_a"], location_id="location_a"),
            StoryBeat(id="story_beat_02", order=2, type="DEVELOPMENT", summary="Courier appears", characters=["character_a", "character_b"], location_id="location_b"),
            StoryBeat(id="story_beat_03", order=3, type="ENDING", summary="Signal resolved", characters=["character_b"], location_id="location_c"),
        ],
    )
    script = StructuredScript(
        title="Rain Signal script",
        scenes=[
            Scene(
                id="scene_a", order=1, title="Lab", location_id="location_a",
                character_ids=["character_a"], estimated_duration_seconds=12,
                beats=[ScriptBeat(id="script_beat_a", order=1, type=ScriptBeatType.ACTION, text="Ari reads the signal")],
            ),
            Scene(
                id="scene_b", order=2, title="Platform", location_id="location_b",
                character_ids=["character_a", "character_b"], estimated_duration_seconds=16,
                beats=[ScriptBeat(id="script_beat_b", order=1, type=ScriptBeatType.ACTION, text="Bo arrives")],
            ),
            Scene(
                id="scene_c", order=3, title="Harbor", location_id="location_c",
                character_ids=["character_b"], estimated_duration_seconds=20,
                beats=[ScriptBeat(id="script_beat_c", order=1, type=ScriptBeatType.ACTION, text="Bo leaves")],
            ),
        ],
    )
    plan = ShotPlan(
        title="Rain Signal shots",
        source_script_revision_id="script_001",
        shots=[
            Shot(id="shot_01", order=1, scene_id="scene_a", source_script_beat_ids=["script_beat_a"], duration_seconds=4, subject=["character_a"], visual_intent="Ari receives the signal"),
            Shot(id="shot_02", order=2, scene_id="scene_a", source_script_beat_ids=["script_beat_a"], duration_seconds=4, subject=["character_a"], visual_intent="Signal monitor reflection"),
            Shot(id="shot_03", order=3, scene_id="scene_b", source_script_beat_ids=["script_beat_b"], duration_seconds=4, subject=["character_b"], visual_intent="Bo arrives"),
            Shot(id="shot_04", order=4, scene_id="scene_b", source_script_beat_ids=["script_beat_b"], duration_seconds=4, subject=["character_a", "character_b"], visual_intent="Ari and Bo meet"),
            Shot(id="shot_05", order=5, scene_id="scene_c", source_script_beat_ids=["script_beat_c"], duration_seconds=4, subject=["character_b"], visual_intent="Bo descends the harbor steps"),
            Shot(id="shot_06", order=6, scene_id="scene_c", source_script_beat_ids=["script_beat_c"], duration_seconds=4, subject=["character_b"], visual_intent="Bo sees the water"),
            Shot(id="shot_07", order=7, scene_id="scene_a", source_script_beat_ids=["script_beat_a"], duration_seconds=4, subject=["character_a"], visual_intent="Ari decides to leave"),
            Shot(id="shot_08", order=8, scene_id="scene_c", source_script_beat_ids=["script_beat_c"], duration_seconds=4, subject=["character_b"], visual_intent="Bo resolves the signal"),
        ],
    )
    now = "2026-08-28T00:00:00+00:00"
    repository.create_story_revision(
        revision_id="story_001", project_id=project.id, version=1,
        status=StoryRevisionStatus.APPROVED, content=story, generation_input=None,
        created_at=now, updated_at=now,
    )
    repository.create_script_revision(
        revision_id="script_001", project_id=project.id, version=1,
        status=ScriptRevisionStatus.APPROVED, source_story_revision_id="story_001",
        content=script, generation_input=None, created_at=now, updated_at=now,
    )
    repository.create_shot_revision(
        revision_id="shot_plan_001", project_id=project.id, version=1,
        status=ShotRevisionStatus.APPROVED, source_script_revision_id="script_001",
        content=plan, generation_input=None, created_at=now, updated_at=now,
    )
    return repository, project, story, script, plan


def _lock_reference(repository, project, binding_type, subject_id: str, color: str):
    references = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(references)
    asset = references.ensure_workspace_asset(project.id, binding_type, subject_id)
    version = storage.import_image(
        project.id,
        asset.id,
        png_bytes(color=color),
        filename=f"{subject_id}.png",
        mime_type="image/png",
        metadata={"source_story_revision_id": "story_001"},
    )
    references.bind_version(project.id, version.id, binding_type, subject_id)
    references.activate_version(project.id, asset.id, version.id)
    return version


def _agent(repository):
    provider = FakeImageProvider()
    runtime = ImageRuntimeService(repository, provider=provider)
    return ReferenceAgentService(repository, image_runtime=runtime), provider


def test_reference_agent_discovers_deduplicates_reuses_and_requires_paid_authorization(context):
    repository, project, _, _, _ = context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    agent, provider = _agent(repository)

    readiness = agent.reference_readiness(project.id)

    assert len(readiness.required) == 5
    assert len([item for item in readiness.required if item.subject_type.value == "CHARACTER"]) == 2
    assert len([item for item in readiness.required if item.subject_type.value == "LOCATION"]) == 3
    assert readiness.character_coverage == "1/2"
    assert readiness.location_coverage == "1/3"
    assert {item.subject_id for item in readiness.missing} == {
        "character_b", "location_b", "location_c",
    }
    character_b = next(item for item in readiness.required if item.subject_id == "character_b")
    assert character_b.required_by_shot_ids == ("shot_03", "shot_04", "shot_05", "shot_06", "shot_08")
    assert character_b.source_revision_ids == {
        "story": "story_001", "script": "script_001", "shot_plan": "shot_plan_001",
    }
    location_c = next(item for item in readiness.required if item.subject_id == "location_c")
    assert location_c.required_by_shot_ids == ("shot_05", "shot_06", "shot_08")
    assert all(
        item.kind is ReferenceActionKind.WAITING_PAID_AUTHORIZATION
        for item in readiness.next_actions
    )
    assert len(provider.calls) == 0

    brief = agent.build_reference_brief(project.id, character_b)
    assert "Qwen" not in brief.render_prompt()
    assert "amber raincoat" not in brief.render_prompt()
    assert "navy utility jacket" in brief.render_prompt()
    assert "Aspect ratio" in brief.render_prompt()
    location_brief = agent.build_reference_brief(project.id, location_c)
    assert "Time / weather" in location_brief.render_prompt()
    assert "Harbor Steps" in location_brief.render_prompt()
    with pytest.raises(ReferenceAgentError, match="WAITING_PAID_AUTHORIZATION"):
        agent.generate_candidates(
            project.id, [item.id for item in readiness.next_actions], authorization=None
        )
    assert len(provider.calls) == 0


def test_fake_autonomous_reference_e2e_human_promotion_binding_lock_and_production_ready(context):
    repository, project, _, _, _ = context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    agent, provider = _agent(repository)
    initial = agent.evaluate(project.id)
    action_ids = [item.id for item in initial.next_actions]
    authorization = agent.generation_authorization(
        project.id,
        action_ids,
        max_creates=3,
        approved_by="test human",
        approved=True,
    )

    generated = agent.generate_candidates(
        project.id, action_ids, authorization=authorization
    )

    assert len(generated) == 3
    assert len(provider.calls) == 3
    references = ReferenceAssetService(repository)
    for item in generated:
        candidate = references.get_image_candidate(project.id, item.candidate_id)
        assert candidate.status.value == "DRAFT"
        assert references.resolve_image_candidate_path(project.id, candidate.id).is_file()
        reloaded = ReferenceAssetService(ProjectRepository(repository.paths))
        assert reloaded.get_image_candidate(project.id, candidate.id).id == candidate.id
        asset = references.find_workspace_asset(
            project.id,
            ReferenceBindingType[item.requirement.subject_type.value],
            item.requirement.subject_id,
        )
        assert references.list_versions(project.id, asset.id) == []

    waiting = agent.evaluate(project.id)
    assert len(waiting.missing) == 0
    assert {item.coverage_status for item in waiting.required if item.subject_id != "character_a" and item.subject_id != "location_a"} == {ReferenceCoverageStatus.WAITING_HUMAN}
    assert all(item.kind is ReferenceActionKind.WAITING_HUMAN_REFERENCE_APPROVAL for item in waiting.next_actions)
    with pytest.raises(ReferenceAgentError, match="WAITING_HUMAN_REFERENCE_APPROVAL"):
        agent.approve_candidate_and_bind(project.id, generated[0].candidate_id, human_confirmed=False)

    versions = []
    for item in generated:
        version = agent.approve_candidate_and_bind(
            project.id, item.candidate_id, human_confirmed=True, actor="reviewer"
        )
        versions.append(version)
        assert references.get_current_version(project.id, version.asset_id) is None
        with pytest.raises(ReferenceAgentError, match="WAITING_HUMAN_REFERENCE_LOCK"):
            agent.lock_bound_reference(project.id, version.id, human_confirmed=False)
        agent.lock_bound_reference(project.id, version.id, human_confirmed=True)

    final = agent.reference_readiness(project.id)
    assert final.character_coverage == "2/2"
    assert final.location_coverage == "3/3"
    assert final.production_reference_ready is True
    assert final.production_readiness["ready"] is True
    assert not final.next_actions
    assert all(item.coverage_status is ReferenceCoverageStatus.LOCKED for item in final.required)


def test_material_revision_change_marks_locked_reference_for_review_without_mutation(context):
    repository, project, story, script, plan = context
    _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_a", "red")
    old_character_b = _lock_reference(repository, project, ReferenceBindingType.CHARACTER, "character_b", "purple")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_a", "blue")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_b", "green")
    _lock_reference(repository, project, ReferenceBindingType.LOCATION, "location_c", "yellow")
    agent, provider = _agent(repository)
    assert agent.evaluate(project.id).production_reference_ready is True

    story_payload = story.model_dump(mode="json")
    story_payload["characters"][1]["appearance"] = "long white coat, formal black gloves, no red scarf"
    changed_story = StoryBible.model_validate(story_payload)
    now = "2026-08-28T01:00:00+00:00"
    repository.create_story_revision(
        revision_id="story_002", project_id=project.id, version=2,
        status=StoryRevisionStatus.DRAFT, content=changed_story, generation_input=None,
        created_at=now, updated_at=now,
    )
    changed_story_revision = repository.approve_story_revision(
        "story_002", updated_at=now
    )
    repository.create_script_revision(
        revision_id="script_002", project_id=project.id, version=2,
        status=ScriptRevisionStatus.DRAFT, source_story_revision_id=changed_story_revision["id"],
        content=script, generation_input=None, created_at=now, updated_at=now,
    )
    changed_script_revision = repository.approve_script_revision(
        "script_002", updated_at=now
    )
    changed_plan = ShotPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "source_script_revision_id": changed_script_revision["id"],
        }
    )
    repository.create_shot_revision(
        revision_id="shot_plan_002", project_id=project.id, version=2,
        status=ShotRevisionStatus.DRAFT, source_script_revision_id=changed_script_revision["id"],
        content=changed_plan, generation_input=None, created_at=now, updated_at=now,
    )
    repository.approve_shot_revision("shot_plan_002", updated_at=now)

    changed = agent.evaluate(project.id)

    character_b = next(item for item in changed.stale if item.subject_id == "character_b")
    assert "material visual definition changed" in character_b.stale_reason
    review = next(item for item in changed.next_actions if item.requirement.subject_id == "character_b")
    assert review.kind is ReferenceActionKind.REFERENCE_REVIEW_REQUIRED
    assert review.affected_shot_ids == ("shot_03", "shot_04", "shot_05", "shot_06", "shot_08")
    assert ReferenceAssetService(repository).get_current_version(
        project.id, old_character_b.asset_id
    ).id == old_character_b.id
    assert len(provider.calls) == 0


def test_reference_agent_blocks_an_outdated_approved_script_shot_chain(context):
    repository, project, _, script, _ = context
    now = "2026-08-28T02:00:00+00:00"
    repository.create_script_revision(
        revision_id="script_002", project_id=project.id, version=2,
        status=ScriptRevisionStatus.DRAFT, source_story_revision_id="story_001",
        content=script, generation_input=None, created_at=now, updated_at=now,
    )
    repository.approve_script_revision("script_002", updated_at=now)
    agent, provider = _agent(repository)

    readiness = agent.reference_readiness(project.id)

    assert readiness.required == ()
    assert "Shot Plan is outdated relative to the approved Structured Script" in readiness.blocked
    assert all(item.kind is ReferenceActionKind.BLOCKED_UPSTREAM for item in readiness.next_actions)
    assert len(provider.calls) == 0
