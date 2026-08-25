from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from loguru import logger

from aidrama_studio.domain import (
    Character,
    Location,
    Project,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    World,
)
from aidrama_studio.services.llm_runtime import LLMInvocationError, LLMInvocationGateway
from aidrama_studio.services.story_parser import parse_story_bible
from aidrama_studio.services.story_prompt import (
    build_repair_prompt,
    build_story_bible_prompt,
)
from aidrama_studio.storage import ProjectRepository


class StoryServiceError(RuntimeError):
    """Safe error suitable for the Story page."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def blank_story_bible(project: Project) -> StoryBible:
    return StoryBible(
        title=project.title,
        logline="待填写的故事一句话梗概",
        premise="待填写的故事前提",
        genre="短剧",
        tone="克制",
        themes=[],
        world=World(era="当代", setting="", rules=[], timeline_notes=""),
        characters=[
            Character(
                id="char_001",
                name="主角",
                role="主角",
                identity="",
                personality="",
                appearance="",
                motivation="",
                relationship_notes="",
                speech_style="",
            )
        ],
        locations=[
            Location(
                id="loc_001",
                name="主要场景",
                function="故事发生的核心空间",
                environment="",
                time_of_day="",
                visual_style="",
                key_props=[],
            )
        ],
        story_beats=[
            StoryBeat(
                id="beat_001",
                order=1,
                type="OPENING",
                summary="建立人物与冲突起点",
                characters=["char_001"],
                location_id="loc_001",
                emotional_goal="引发好奇",
            ),
            StoryBeat(
                id="beat_002",
                order=2,
                type="TURNING_POINT",
                summary="冲突发生关键转折",
                characters=["char_001"],
                location_id="loc_001",
                emotional_goal="制造压力",
            ),
            StoryBeat(
                id="beat_003",
                order=3,
                type="ENDING",
                summary="给出一个清晰的情绪落点",
                characters=["char_001"],
                location_id="loc_001",
                emotional_goal="留下余韵",
            ),
        ],
    )


class StoryService:
    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        llm_gateway: LLMInvocationGateway | None = None,
    ):
        self.repository = repository or ProjectRepository()
        self._llm_gateway = llm_gateway or LLMInvocationGateway(self.repository)

    def llm_readiness(self, project_id: str) -> tuple[bool, str]:
        return self._llm_gateway.readiness(project_id)

    def get_latest_revision(self, project_id: str) -> dict[str, Any] | None:
        return self.repository.get_latest_story_revision(project_id)

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return self.repository.get_story_revision(revision_id)

    def list_revisions(self, project_id: str) -> list[dict[str, Any]]:
        return self.repository.list_story_revisions(project_id)

    def _next_version(self, project_id: str) -> int:
        latest = self.get_latest_revision(project_id)
        return latest["version"] + 1 if latest else 1

    def _create_revision(
        self,
        project_id: str,
        content: StoryBible,
        *,
        generation_input: dict[str, Any] | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        return self.repository.create_story_revision(
            revision_id=uuid4().hex,
            project_id=project_id,
            version=version or self._next_version(project_id),
            status=StoryRevisionStatus.DRAFT,
            content=content,
            generation_input=generation_input,
            created_at=now,
            updated_at=now,
        )

    def create_blank_draft(self, project: Project) -> dict[str, Any]:
        return self._create_revision(project.id, blank_story_bible(project))

    def create_revision_from_approved(
        self, revision_id: str, *, content: StoryBible | None = None
    ) -> dict[str, Any]:
        revision = self.get_revision(revision_id)
        if revision is None:
            raise KeyError("Story Bible revision 不存在")
        if revision["status"] is not StoryRevisionStatus.APPROVED:
            raise ValueError("只有 APPROVED revision 可以创建新的 DRAFT")
        return self._create_revision(
            revision["project_id"], content or revision["content"]
        )

    def save_draft(
        self,
        project_id: str,
        content: StoryBible,
        *,
        revision_id: str | None = None,
        generation_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content = StoryBible.model_validate(content.model_dump(mode="json"))
        current = self.get_revision(revision_id) if revision_id else None
        if current and current["status"] is StoryRevisionStatus.APPROVED:
            return self._create_revision(
                project_id, content, generation_input=generation_input
            )
        if current and current["status"] is StoryRevisionStatus.DRAFT:
            return self.repository.update_story_revision(
                revision_id,
                content=content,
                updated_at=_now(),
                generation_input=generation_input,
            )
        latest = self.get_latest_revision(project_id)
        if latest and latest["status"] is StoryRevisionStatus.DRAFT:
            return self.repository.update_story_revision(
                latest["id"],
                content=content,
                updated_at=_now(),
                generation_input=generation_input,
            )
        return self._create_revision(
            project_id, content, generation_input=generation_input
        )

    def approve_revision(self, revision_id: str) -> dict[str, Any]:
        revision = self.get_revision(revision_id)
        if revision is None:
            raise KeyError("Story Bible revision 不存在")
        StoryBible.model_validate(revision["content"].model_dump(mode="json"))
        return self.repository.approve_story_revision(revision_id, updated_at=_now())

    def delete_draft_if_safe(self, revision_id: str) -> bool:
        revision = self.get_revision(revision_id)
        if revision is None:
            return False
        if revision["status"] is not StoryRevisionStatus.DRAFT:
            raise ValueError("只有 DRAFT revision 可以删除")
        return self.repository.delete_story_revision(revision_id)

    def generate_story_bible(
        self,
        project: Project,
        *,
        brief: str,
        genre: str,
        tone: str,
        target_audience: str = "",
        creative_constraints: str = "",
    ) -> dict[str, Any]:
        if not brief.strip():
            raise StoryServiceError("请先填写核心创意或项目 Brief。")
        generation_input = {
            "brief": brief.strip(),
            "genre": genre.strip(),
            "tone": tone.strip(),
            "target_audience": target_audience.strip(),
            "creative_constraints": creative_constraints.strip(),
            "project_aspect_ratio": project.aspect_ratio.value,
            "project_target_duration_seconds": project.target_duration_seconds,
        }
        prompt = build_story_bible_prompt(
            project,
            brief=brief,
            genre=genre,
            tone=tone,
            target_audience=target_audience,
            creative_constraints=creative_constraints,
        )
        try:
            content = self._llm_gateway.generate_validated_json(
                project.id,
                prompt,
                operation="STORY_BIBLE_GENERATION",
                validator=parse_story_bible,
                repair_prompt_builder=lambda raw, exc: build_repair_prompt(
                    raw, str(exc)
                ),
            )
        except LLMInvocationError as exc:
            logger.warning(f"Story Bible generation failed: {exc}")
            raise StoryServiceError(str(exc)) from exc
        except Exception as exc:
            logger.exception("unexpected Story Bible generation failure")
            raise StoryServiceError("Story Bible 生成失败，请稍后重试。") from exc
        return self._create_revision(
            project.id, content, generation_input=generation_input
        )
