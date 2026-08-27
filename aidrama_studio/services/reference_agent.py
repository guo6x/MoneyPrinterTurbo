"""Autonomous, human-gated reference planning for an approved creative chain.

The service observes the exact approved Story Bible -> Structured Script ->
Shot Plan chain, creates a single requirement per required subject, and uses
the existing Image/Reference services for the only mutating steps.  It never
locks an asset and never calls an Image provider without a bounded explicit
authorization.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import hashlib
import json

from aidrama_studio.domain import (
    ReferenceBindingType,
    ReferenceImageCandidateStatus,
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.domain.reference_agent import (
    GeneratedReferenceCandidate,
    ReferenceActionKind,
    ReferenceBrief,
    ReferenceCoverageStatus,
    ReferenceGenerationAction,
    ReferenceGenerationAuthorization,
    ReferenceReadiness,
    ReferenceRequirement,
    ReferenceSubjectType,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .image_runtime import ImageRuntimeService
from .production import ProductionService
from .reference_assets import ReferenceAssetService, ReferenceAssetServiceError


class ReferenceAgentError(RuntimeError):
    pass


class ReferenceAgentService:
    """Reference planning seam for the Assets page and a future AUTO controller."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        reference_assets: ReferenceAssetService | None = None,
        image_runtime: ImageRuntimeService | None = None,
        production_service: ProductionService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.reference_assets = reference_assets or ReferenceAssetService(self.repository)
        # Constructing the runtime may inspect installed provider adapters.
        # Observation is deliberately provider-free, so defer it until an
        # explicitly authorized create action actually needs the capability.
        self.image_runtime = image_runtime
        self.production_service = production_service or ProductionService(
            self.repository, reference_service=self.reference_assets
        )

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ReferenceAgentError(f"项目不存在: {project_id}")
        return project

    @staticmethod
    def _approved(revisions: Iterable[dict[str, object]], status):
        return next((item for item in revisions if item["status"] is status.APPROVED), None)

    def _approved_chain(self, project_id: str):
        story = self._approved(
            self.repository.list_story_revisions(project_id), StoryRevisionStatus
        )
        script = self._approved(
            self.repository.list_script_revisions(project_id), ScriptRevisionStatus
        )
        plan = self._approved(
            self.repository.list_shot_revisions(project_id), ShotRevisionStatus
        )
        blocked: list[str] = []
        if story is None:
            blocked.append("approved Story Bible is required")
        if script is None:
            blocked.append("approved Structured Script is required")
        if plan is None:
            blocked.append("approved Shot Plan is required")
        if story is not None and script is not None:
            if script["source_story_revision_id"] != story["id"]:
                blocked.append("Structured Script is outdated relative to the approved Story Bible")
        if script is not None and plan is not None:
            if plan["source_script_revision_id"] != script["id"]:
                blocked.append("Shot Plan is outdated relative to the approved Structured Script")
        revisions = {
            key: str(value["id"])
            for key, value in (("story", story), ("script", script), ("shot_plan", plan))
            if value is not None
        }
        return story, script, plan, tuple(blocked), revisions

    @staticmethod
    def _fingerprint(value: Mapping[str, object]) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _subject_identity(cls, story, subject_type: ReferenceSubjectType, subject_id: str) -> str:
        if subject_type is ReferenceSubjectType.CHARACTER:
            subject = next(item for item in story.characters if item.id == subject_id)
            payload = {
                "type": subject_type.value,
                "id": subject.id,
                "name": subject.name,
                "role": subject.role,
                "age_or_range": subject.age_or_range,
                "identity": subject.identity,
                "appearance": subject.appearance,
                "story_style": {
                    "genre": story.genre,
                    "tone": story.tone,
                    "world": story.world.model_dump(mode="json"),
                },
            }
        else:
            subject = next(item for item in story.locations if item.id == subject_id)
            payload = {
                "type": subject_type.value,
                "id": subject.id,
                "name": subject.name,
                "function": subject.function,
                "environment": subject.environment,
                "time_of_day": subject.time_of_day,
                "visual_style": subject.visual_style,
                "key_props": subject.key_props,
                "story_style": {
                    "genre": story.genre,
                    "tone": story.tone,
                    "world": story.world.model_dump(mode="json"),
                },
            }
        return cls._fingerprint(payload)

    @staticmethod
    def _action_id(project_id: str, requirement: ReferenceRequirement) -> str:
        payload = {
            "project_id": project_id,
            "subject_id": requirement.subject_id,
            "subject_type": requirement.subject_type.value,
            "source_revision_ids": requirement.source_revision_ids,
            "subject_identity": requirement.subject_identity,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _locked_version_for(self, project_id: str, binding_type, binding_id: str):
        for binding in self.reference_assets.list_bindings(project_id):
            if binding.binding_type is not binding_type or binding.binding_id != binding_id:
                continue
            version = self.repository.get_reference_asset_version(binding.asset_version_id)
            if version is None:
                continue
            asset = self.repository.get_reference_asset(version.asset_id)
            if asset is None or asset.current_version_id != version.id:
                continue
            try:
                if self.reference_assets.resolve_version_path(project_id, version.id).is_file():
                    return version
            except ReferenceAssetServiceError:
                continue
        return None

    def _draft_candidate_for(self, project_id: str, binding_type, binding_id: str):
        asset = self.reference_assets.find_workspace_asset(
            project_id, binding_type, binding_id
        )
        if asset is None:
            return None
        candidates = self.reference_assets.list_image_candidates(project_id, asset.id)
        return next(
            (item for item in reversed(candidates) if item.status is ReferenceImageCandidateStatus.DRAFT),
            None,
        )

    def _bound_version_for(self, project_id: str, binding_type, binding_id: str):
        for binding in self.reference_assets.list_bindings(project_id):
            if binding.binding_type is not binding_type or binding.binding_id != binding_id:
                continue
            version = self.repository.get_reference_asset_version(binding.asset_version_id)
            if version is None:
                continue
            asset = self.repository.get_reference_asset(version.asset_id)
            if asset is not None and asset.current_version_id != version.id:
                return version
        return None

    def _coverage_for(
        self,
        project_id: str,
        story_revision: dict[str, object],
        subject_type: ReferenceSubjectType,
        subject_id: str,
    ) -> tuple[ReferenceCoverageStatus, str | None, str | None, str | None]:
        binding_type = (
            ReferenceBindingType.CHARACTER
            if subject_type is ReferenceSubjectType.CHARACTER
            else ReferenceBindingType.LOCATION
        )
        if self.reference_assets.is_binding_ready(
            project_id, binding_type, subject_id, str(story_revision["id"])
        ):
            locked = self._locked_version_for(project_id, binding_type, subject_id)
            return ReferenceCoverageStatus.LOCKED, locked.id if locked else None, None, None

        candidate = self._draft_candidate_for(project_id, binding_type, subject_id)
        if candidate is not None:
            return (
                ReferenceCoverageStatus.WAITING_HUMAN,
                None,
                candidate.id,
                "candidate generated; explicit human approval is required",
            )

        bound = self._bound_version_for(project_id, binding_type, subject_id)
        if bound is not None:
            return (
                ReferenceCoverageStatus.BOUND,
                None,
                None,
                "candidate was approved and bound; explicit human lock is required",
            )

        locked = self._locked_version_for(project_id, binding_type, subject_id)
        if locked is None:
            return ReferenceCoverageStatus.MISSING, None, None, None

        source_id = locked.metadata.get("source_story_revision_id")
        source_revision = (
            self.repository.get_story_revision(source_id)
            if isinstance(source_id, str) and source_id
            else None
        )
        if source_revision is None:
            return (
                ReferenceCoverageStatus.STALE,
                locked.id,
                None,
                "REFERENCE_REVIEW_REQUIRED: locked reference provenance is incomplete",
            )
        try:
            old_identity = self._subject_identity(
                source_revision["content"], subject_type, subject_id
            )
        except StopIteration:
            return (
                ReferenceCoverageStatus.STALE,
                locked.id,
                None,
                "REFERENCE_REVIEW_REQUIRED: subject no longer exists in reference source revision",
            )
        new_identity = self._subject_identity(
            story_revision["content"], subject_type, subject_id
        )
        reason = (
            "REFERENCE_REVIEW_REQUIRED: material visual definition changed"
            if old_identity != new_identity
            else "REFERENCE_REVIEW_REQUIRED: locked reference is from an older Story revision"
        )
        return ReferenceCoverageStatus.STALE, locked.id, None, reason

    def _discover_requirements(
        self, project_id: str, story, script, plan, revision_ids: dict[str, str]
    ) -> tuple[ReferenceRequirement, ...]:
        scenes = {scene.id: scene for scene in script["content"].scenes}
        characters = {item.id: item for item in story["content"].characters}
        locations = {item.id: item for item in story["content"].locations}
        requirements: dict[tuple[ReferenceSubjectType, str], list[str]] = defaultdict(list)
        first_order: dict[tuple[ReferenceSubjectType, str], int] = {}
        for shot in sorted(plan["content"].shots, key=lambda item: (item.order, item.id)):
            scene = scenes.get(shot.scene_id)
            if scene is None:
                raise ReferenceAgentError(f"Shot {shot.id} references an unknown scene")
            for character_id in shot.subject:
                if character_id not in characters:
                    raise ReferenceAgentError(f"Shot {shot.id} references an unknown character: {character_id}")
                key = (ReferenceSubjectType.CHARACTER, character_id)
                requirements[key].append(shot.id)
                first_order.setdefault(key, shot.order)
            if scene.location_id not in locations:
                raise ReferenceAgentError(f"Shot {shot.id} references an unknown location: {scene.location_id}")
            key = (ReferenceSubjectType.LOCATION, scene.location_id)
            requirements[key].append(shot.id)
            first_order.setdefault(key, shot.order)

        result: list[ReferenceRequirement] = []
        for (subject_type, subject_id), shot_ids in requirements.items():
            subject = (
                characters[subject_id]
                if subject_type is ReferenceSubjectType.CHARACTER
                else locations[subject_id]
            )
            coverage, locked_version_id, candidate_id, stale_reason = self._coverage_for(
                project_id, story, subject_type, subject_id
            )
            priority = "HIGH" if first_order[(subject_type, subject_id)] <= 2 or len(shot_ids) >= 3 else "NORMAL"
            result.append(
                ReferenceRequirement(
                    subject_id=subject_id,
                    subject_type=subject_type,
                    canonical_name=subject.name,
                    required_by_shot_ids=tuple(shot_ids),
                    priority=priority,
                    source_revision_ids=dict(revision_ids),
                    subject_identity=self._subject_identity(
                        story["content"], subject_type, subject_id
                    ),
                    coverage_status=coverage,
                    locked_version_id=locked_version_id,
                    candidate_id=candidate_id,
                    stale_reason=stale_reason,
                )
            )
        return tuple(sorted(result, key=lambda item: (item.subject_type.value, item.subject_id)))

    def _actions(
        self, project_id: str, requirements: Iterable[ReferenceRequirement], blocked: tuple[str, ...]
    ) -> tuple[ReferenceGenerationAction, ...]:
        if blocked:
            return tuple(
                ReferenceGenerationAction(
                    id=hashlib.sha256(f"{project_id}:{reason}".encode("utf-8")).hexdigest(),
                    kind=ReferenceActionKind.BLOCKED_UPSTREAM,
                    requirement=ReferenceRequirement(
                        subject_id="upstream",
                        subject_type=ReferenceSubjectType.CHARACTER,
                        canonical_name="Upstream approved chain",
                        priority="BLOCKED",
                        subject_identity=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
                        coverage_status=ReferenceCoverageStatus.BLOCKED,
                    ),
                    reason=reason,
                )
                for reason in blocked
            )
        actions: list[ReferenceGenerationAction] = []
        for requirement in requirements:
            action_id = self._action_id(project_id, requirement)
            if requirement.coverage_status is ReferenceCoverageStatus.MISSING:
                actions.append(
                    ReferenceGenerationAction(
                        id=action_id,
                        kind=ReferenceActionKind.WAITING_PAID_AUTHORIZATION,
                        requirement=requirement,
                        reason="GENERATION_REQUIRED; bounded paid authorization is required before Image create",
                        affected_shot_ids=requirement.required_by_shot_ids,
                    )
                )
            elif requirement.coverage_status is ReferenceCoverageStatus.WAITING_HUMAN:
                actions.append(
                    ReferenceGenerationAction(
                        id=action_id,
                        kind=ReferenceActionKind.WAITING_HUMAN_REFERENCE_APPROVAL,
                        requirement=requirement,
                        reason="candidate is DRAFT; promote and bind only after human approval",
                        affected_shot_ids=requirement.required_by_shot_ids,
                        candidate_id=requirement.candidate_id,
                    )
                )
            elif requirement.coverage_status is ReferenceCoverageStatus.BOUND:
                actions.append(
                    ReferenceGenerationAction(
                        id=action_id,
                        kind=ReferenceActionKind.WAITING_HUMAN_REFERENCE_LOCK,
                        requirement=requirement,
                        reason="bound reference is not locked and does not satisfy production coverage",
                        affected_shot_ids=requirement.required_by_shot_ids,
                    )
                )
            elif requirement.coverage_status is ReferenceCoverageStatus.STALE:
                actions.append(
                    ReferenceGenerationAction(
                        id=action_id,
                        kind=ReferenceActionKind.REFERENCE_REVIEW_REQUIRED,
                        requirement=requirement,
                        reason=requirement.stale_reason or "REFERENCE_REVIEW_REQUIRED",
                        affected_shot_ids=requirement.required_by_shot_ids,
                    )
                )
        return tuple(actions)

    def observe_project_reference_state(self, project_id: str) -> ReferenceReadiness:
        """Observe exact approved inputs; this method is strictly read-only."""

        self._require_project(project_id)
        story, script, plan, blocked, revision_ids = self._approved_chain(project_id)
        requirements: tuple[ReferenceRequirement, ...] = ()
        if not blocked and story is not None and script is not None and plan is not None:
            try:
                requirements = self._discover_requirements(
                    project_id, story, script, plan, revision_ids
                )
            except ReferenceAgentError as exc:
                blocked = (str(exc),)
        actions = self._actions(project_id, requirements, blocked)
        covered = tuple(
            item for item in requirements if item.coverage_status is ReferenceCoverageStatus.LOCKED
        )
        missing = tuple(
            item for item in requirements if item.coverage_status is ReferenceCoverageStatus.MISSING
        )
        stale = tuple(
            item for item in requirements if item.coverage_status is ReferenceCoverageStatus.STALE
        )

        def coverage(kind: ReferenceSubjectType) -> str:
            selected = [item for item in requirements if item.subject_type is kind]
            locked = [item for item in selected if item.coverage_status is ReferenceCoverageStatus.LOCKED]
            return f"{len(locked)}/{len(selected)}"

        production_readiness: dict[str, object] = {}
        if not blocked and plan is not None:
            production_readiness = self.production_service.calculate_production_readiness(
                project_id, str(plan["id"])
            )
        return ReferenceReadiness(
            project_id=project_id,
            source_revision_ids=revision_ids,
            required=requirements,
            covered=covered,
            missing=missing,
            stale=stale,
            blocked=blocked,
            next_actions=actions,
            character_coverage=coverage(ReferenceSubjectType.CHARACTER),
            location_coverage=coverage(ReferenceSubjectType.LOCATION),
            production_reference_ready=bool(production_readiness.get("ready")),
            production_readiness=production_readiness,
        )

    # Stable convenience names for the AUTO controller seam.
    reference_readiness = observe_project_reference_state
    evaluate = observe_project_reference_state

    def next_reference_actions(self, project_id: str) -> tuple[ReferenceGenerationAction, ...]:
        return self.observe_project_reference_state(project_id).next_actions

    def determine_required_references(self, project_id: str) -> tuple[ReferenceRequirement, ...]:
        return self.observe_project_reference_state(project_id).required

    def identify_missing_references(self, project_id: str) -> tuple[ReferenceRequirement, ...]:
        return self.observe_project_reference_state(project_id).missing

    def plan_generation_actions(self, project_id: str) -> tuple[ReferenceGenerationAction, ...]:
        return self.next_reference_actions(project_id)

    def build_reference_brief(self, project_id: str, requirement: ReferenceRequirement) -> ReferenceBrief:
        project = self._require_project(project_id)
        story, script, plan, blocked, revision_ids = self._approved_chain(project_id)
        if blocked or story is None or script is None or plan is None:
            raise ReferenceAgentError("approved creative chain is required for a ReferenceBrief")
        if requirement.source_revision_ids != revision_ids:
            raise ReferenceAgentError("reference requirement revision context is stale; evaluate again")
        content = story["content"]
        style = " · ".join(
            item for item in (content.genre, content.tone, content.world.setting, content.world.era) if item
        )
        context = " · ".join(
            item for item in (content.title, content.logline, content.premise) if item
        )
        if requirement.subject_type is ReferenceSubjectType.CHARACTER:
            subject = next((item for item in content.characters if item.id == requirement.subject_id), None)
            if subject is None:
                raise ReferenceAgentError("character no longer exists in approved Story Bible")
            return ReferenceBrief(
                subject_id=subject.id,
                subject_type=requirement.subject_type,
                canonical_name=subject.name,
                canonical_identity=subject.identity or subject.role or subject.name,
                appearance=subject.appearance,
                wardrobe=subject.appearance,
                age_presentation=subject.age_or_range,
                story_context=context,
                visual_style=style,
                aspect_ratio=project.aspect_ratio.value,
                negative_constraints=("no text", "no watermark", "no unrelated identity"),
                source_revision_ids=revision_ids,
            )
        subject = next((item for item in content.locations if item.id == requirement.subject_id), None)
        if subject is None:
            raise ReferenceAgentError("location no longer exists in approved Story Bible")
        return ReferenceBrief(
            subject_id=subject.id,
            subject_type=requirement.subject_type,
            canonical_name=subject.name,
            canonical_identity=subject.function or subject.name,
            story_context=context,
            visual_style=" · ".join(item for item in (style, subject.visual_style) if item),
            aspect_ratio=project.aspect_ratio.value,
            time_weather=subject.time_of_day or "unspecified time; weather not specified",
            set_details=" · ".join(item for item in (subject.environment, ", ".join(subject.key_props)) if item),
            negative_constraints=("no text", "no watermark", "no unrelated location"),
            source_revision_ids=revision_ids,
        )

    @staticmethod
    def generation_authorization(
        project_id: str,
        action_ids: Iterable[str],
        *,
        max_creates: int,
        approved_by: str,
        approved: bool,
    ) -> ReferenceGenerationAuthorization:
        return ReferenceGenerationAuthorization(
            project_id=project_id,
            action_ids=tuple(action_ids),
            max_creates=max_creates,
            approved_by=approved_by,
            approved=approved,
        )

    def generate_candidates(
        self,
        project_id: str,
        action_ids: Iterable[str],
        *,
        authorization: ReferenceGenerationAuthorization | None,
        disclosure: Mapping[str, object] | None = None,
    ) -> tuple[GeneratedReferenceCandidate, ...]:
        """Generate only the exact, bounded, authorized missing requirements."""

        requested_ids = tuple(dict.fromkeys(str(item) for item in action_ids))
        if not requested_ids:
            return ()
        if authorization is None or authorization.approved is not True:
            raise ReferenceAgentError("WAITING_PAID_AUTHORIZATION: explicit bounded authorization is required")
        if authorization.project_id != project_id:
            raise ReferenceAgentError("authorization does not belong to this project")
        if len(requested_ids) > authorization.max_creates:
            raise ReferenceAgentError("authorization max_creates is lower than requested Image creates")
        if not set(requested_ids).issubset(set(authorization.action_ids)):
            raise ReferenceAgentError("authorization does not cover every requested generation action")
        readiness = self.observe_project_reference_state(project_id)
        actions = {item.id: item for item in readiness.next_actions}
        selected: list[ReferenceGenerationAction] = []
        for action_id in requested_ids:
            action = actions.get(action_id)
            if action is None or action.kind is not ReferenceActionKind.WAITING_PAID_AUTHORIZATION:
                raise ReferenceAgentError("generation action is no longer missing or is not authorized for create")
            selected.append(action)

        generated: list[GeneratedReferenceCandidate] = []
        for action in selected:
            requirement = action.requirement
            binding_type = (
                ReferenceBindingType.CHARACTER
                if requirement.subject_type is ReferenceSubjectType.CHARACTER
                else ReferenceBindingType.LOCATION
            )
            asset = self.reference_assets.ensure_workspace_asset(
                project_id, binding_type, requirement.subject_id
            )
            brief = self.build_reference_brief(project_id, requirement)
            runtime = self.image_runtime or ImageRuntimeService(self.repository)
            candidate = runtime.generate_and_record_candidate(
                project_id,
                asset.id,
                brief.render_prompt(),
                source_story_revision_id=requirement.source_revision_ids["story"],
                filename=f"reference-{requirement.subject_type.value.lower()}-{requirement.subject_id}.png",
                metadata={
                    "reference_action_id": action.id,
                    "reference_subject_type": requirement.subject_type.value,
                    "reference_subject_id": requirement.subject_id,
                    "reference_brief_revision_ids": dict(requirement.source_revision_ids),
                },
                disclosure=disclosure,
                actor="reference-agent",
                reference_assets=self.reference_assets,
            )
            generated.append(
                GeneratedReferenceCandidate(
                    action_id=action.id,
                    requirement=requirement,
                    candidate_id=candidate.id,
                )
            )
        return tuple(generated)

    def approve_candidate_and_bind(
        self,
        project_id: str,
        candidate_id: str,
        *,
        human_confirmed: bool,
        actor: str = "human",
        notes: str = "",
    ):
        """Perform the human-approved promotion/binding step, never a lock."""

        if human_confirmed is not True:
            raise ReferenceAgentError("WAITING_HUMAN_REFERENCE_APPROVAL")
        readiness = self.observe_project_reference_state(project_id)
        requirement = next(
            (item for item in readiness.required if item.candidate_id == candidate_id), None
        )
        if requirement is None:
            raise ReferenceAgentError("candidate is not an active Reference Agent requirement")
        version = self.reference_assets.promote_image_candidate(
            project_id, candidate_id, actor=actor, notes=notes
        )
        binding_type = (
            ReferenceBindingType.CHARACTER
            if requirement.subject_type is ReferenceSubjectType.CHARACTER
            else ReferenceBindingType.LOCATION
        )
        self.reference_assets.bind_version(
            project_id, version.id, binding_type, requirement.subject_id
        )
        return version

    def lock_bound_reference(
        self,
        project_id: str,
        version_id: str,
        *,
        human_confirmed: bool,
    ):
        """Lock a previously bound version only after a separate human gate."""

        if human_confirmed is not True:
            raise ReferenceAgentError("WAITING_HUMAN_REFERENCE_LOCK")
        bindings = self.reference_assets.list_bindings(project_id, version_id)
        if not bindings:
            raise ReferenceAgentError("version must be bound before it can be locked")
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id:
            raise ReferenceAgentError("reference version does not belong to this project")
        return self.reference_assets.activate_version(project_id, version.asset_id, version.id)


__all__ = ["ReferenceAgentError", "ReferenceAgentService"]
