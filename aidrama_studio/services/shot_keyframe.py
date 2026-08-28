"""Shot-specific keyframe planning, generation, persistence, and readiness.

Reference assets are immutable identity/style constraints.  This module owns
the separate image artifact that a VIDEO runtime may use as the literal first
frame.  Provider calls are reachable only through an explicitly supplied
Universal IMAGE runtime binding; validation and repair recommendations never
invoke IMAGE or VIDEO providers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol
from uuid import uuid4

from aidrama_studio.domain import (
    GenerationBrief,
    ProductionArtifact,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotSourceDecisionType,
    ProductionShotSourceSelectionKind,
    ReferenceAssetType,
    ReferenceBindingType,
    ProviderTask,
    RuntimePlan,
    ScriptRevisionStatus,
    Shot,
    ShotKeyframePlanningSnapshot,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.domain.shot_keyframe import (
    DuplicateFirstFrameGroup,
    PreLiveFirstFrameGate,
    PreLiveFirstFrameReport,
    PreviousApprovedArtifactProvenance,
    PreviousApprovedShotContext,
    ReferenceProvenance,
    ShotFirstFrame,
    ShotFirstFrameSourceType,
    ShotKeyframeBrief,
    ShotKeyframeLighting,
    ShotKeyframeLocation,
    ShotKeyframeReferenceRole,
    ShotKeyframeRepairAction,
    ShotKeyframeRepairRecommendation,
    ShotKeyframeRepairScope,
    ShotKeyframeSelection,
    ShotKeyframeSelectionPolicy,
    ShotKeyframeSubject,
    UserProvidedSourceProvenance,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    RuntimeOutcome,
)
from aidrama_studio.storage.reference_assets import validate_image_input
from aidrama_studio.storage.repositories import ProjectRepository

from .production_artifact_storage import (
    ProductionArtifactStorageError,
    ProductionArtifactStorageService,
)
from .reference_assets import ReferenceAssetService
from .runtime_foundation import RuntimePlanService
from .security import sanitize_error, sanitize_persistent_metadata


SHOT_FIRST_FRAME_ARTIFACT_TYPE = "SHOT_FIRST_FRAME"
SHOT_KEYFRAME_PROMPT_TEMPLATE_VERSION = "shot-keyframe-brief-v1"
MAX_FIRST_FRAME_BYTES = 20 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _stable_description(value: object, *, fallback: str) -> str:
    if isinstance(value, Mapping):
        preferred = (
            "stable_description",
            "identity_description",
            "location_description",
            "prop_description",
            "style_description",
            "description",
            "appearance",
            "visual_identity",
            "name",
        )
        parts = [
            str(value[key]).strip()
            for key in preferred
            if key in value and str(value[key] or "").strip()
        ]
        if parts:
            return sanitize_error("; ".join(dict.fromkeys(parts)), max_length=4000)
    elif isinstance(value, str) and value.strip():
        return sanitize_error(value, max_length=4000)
    return sanitize_error(fallback, max_length=4000)


class ShotKeyframeError(RuntimeError):
    """A sanitized, provider-neutral keyframe contract failure."""


class ShotKeyframeReadinessError(ShotKeyframeError):
    def __init__(self, report: PreLiveFirstFrameReport):
        self.report = report
        detail = "; ".join(report.blocking_reasons)
        super().__init__(f"PRE_LIVE_FIRST_FRAME_GATE=BLOCKED: {detail}")


class ShotKeyframeCreateUncertainError(ShotKeyframeError):
    """The provider may have accepted a paid create; never resubmit it."""


class ShotKeyframeProviderResultError(ShotKeyframeError):
    """A definitive provider response could not produce one valid image."""


class UniversalImageRuntime(Protocol):
    def submit(
        self,
        request: CapabilityRequest,
        *,
        authorization: object | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class UniversalImageBinding:
    """Exact Universal IMAGE manifest/runtime/output-reader binding."""

    runtime: UniversalImageRuntime = field(repr=False)
    manifest: object
    read_output: Callable[[ContentRef], bytes] = field(repr=False)


_RESERVED_IMAGE_PROVIDER_PARAMETERS = frozenset(
    {
        "codec_id",
        "credential_reference",
        "endpoint_profile_id",
        "manifest_hash",
        "manifest_id",
        "model_id",
        "provider_id",
        "runtime_plan_hash",
        "runtime_plan_id",
    }
)


def _validated_image_provider_parameters(
    binding: UniversalImageBinding,
    raw: Mapping[str, object] | None,
) -> dict[str, object]:
    parameters = dict(raw or {})
    safe = sanitize_persistent_metadata(parameters)
    if not isinstance(safe, dict) or safe != parameters:
        raise ShotKeyframeError(
            "Universal IMAGE provider parameters contain unsafe values"
        )
    reserved = sorted(_RESERVED_IMAGE_PROVIDER_PARAMETERS.intersection(parameters))
    if reserved:
        raise ShotKeyframeError(
            "Universal IMAGE provider parameters cannot override frozen identity: "
            + ", ".join(reserved)
        )
    resolution = parameters.get("resolution")
    if not isinstance(resolution, str):
        raise ShotKeyframeError(
            "Keyframe RuntimePlan requires an exact IMAGE resolution"
        )
    dimensions = resolution.split("*")
    if len(dimensions) != 2 or not all(item.isdecimal() for item in dimensions):
        raise ShotKeyframeError(
            "Keyframe RuntimePlan requires WIDTH*HEIGHT provider dimensions"
        )
    supported = tuple(
        str(item)
        for item in getattr(
            getattr(binding.manifest, "resolution", None), "supported", ()
        )
    )
    if supported and resolution not in supported:
        raise ShotKeyframeError(
            "Keyframe RuntimePlan IMAGE resolution is not supported by the exact manifest"
        )
    return parameters


@dataclass(frozen=True, slots=True)
class GeneratedKeyframeImage:
    content: bytes = field(repr=False)
    mime_type: str
    filename: str
    sha256: str
    size_bytes: int
    request_id: str
    request_sha256: str
    manifest_id: str
    manifest_hash: str
    provider_id: str
    model_id: str
    reference_conditioning_mode: str


@dataclass(frozen=True, slots=True)
class ResolvedShotFirstFrame:
    first_frame: ShotFirstFrame
    artifact: ProductionArtifact
    path: Path = field(repr=False)

    @property
    def size_bytes(self) -> int:
        return self.first_frame.artifact_size_bytes


class ShotKeyframePolicy:
    """Deterministic baseline; future Director reasoning can replace this seam."""

    @staticmethod
    def select(
        current: Shot,
        *,
        project_id: str,
        previous: Shot | None = None,
        continuous_action: bool = False,
        previous_reuse_authorization_id: str | None = None,
        user_source_artifact_id: str | None = None,
        user_approval_id: str | None = None,
        explicit_reference_version_id: str | None = None,
        reference_override_approval_id: str | None = None,
    ) -> ShotKeyframeSelection:
        if user_source_artifact_id is not None:
            return ShotKeyframeSelection(
                project_id=project_id,
                shot_id=current.id,
                policy=ShotKeyframeSelectionPolicy.USER_APPROVED_STARTING_IMAGE,
                source_type=ShotFirstFrameSourceType.USER_PROVIDED,
                reason="An explicit user-approved starting image was selected.",
                user_source_artifact_id=user_source_artifact_id,
                literal_reuse_authorization_id=user_approval_id,
            )
        if explicit_reference_version_id is not None:
            return ShotKeyframeSelection(
                project_id=project_id,
                shot_id=current.id,
                policy=ShotKeyframeSelectionPolicy.EXPLICIT_REFERENCE_OVERRIDE,
                source_type=ShotFirstFrameSourceType.EXPLICIT_REFERENCE_OVERRIDE,
                reason=(
                    "An exact reference version was explicitly approved as the literal "
                    "starting image; ordinary locked references never select this path."
                ),
                literal_reference_version_id=explicit_reference_version_id,
                literal_reuse_authorization_id=reference_override_approval_id,
            )
        if previous is None or previous.scene_id != current.scene_id:
            return ShotKeyframeSelection(
                project_id=project_id,
                shot_id=current.id,
                policy=ShotKeyframeSelectionPolicy.NEW_SCENE,
                source_type=ShotFirstFrameSourceType.GENERATED_KEYFRAME,
                reason="A new scene requires a shot-specific generated composition.",
            )

        compatible = (
            previous.shot_size is current.shot_size
            and previous.camera_angle is current.camera_angle
            and previous.composition.strip().casefold()
            == current.composition.strip().casefold()
        )
        if continuous_action and compatible:
            return ShotKeyframeSelection(
                project_id=project_id,
                shot_id=current.id,
                policy=(
                    ShotKeyframeSelectionPolicy.CONTINUOUS_ACTION_COMPATIBLE_COMPOSITION
                ),
                source_type=ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME,
                reason=(
                    "Continuous action and compatible composition explicitly select the "
                    "previous approved shot's final decoded frame."
                ),
                previous_shot_id=previous.id,
                literal_reuse_authorization_id=previous_reuse_authorization_id,
            )
        return ShotKeyframeSelection(
            project_id=project_id,
            shot_id=current.id,
            policy=ShotKeyframeSelectionPolicy.NEW_COMPOSITION,
            source_type=ShotFirstFrameSourceType.GENERATED_KEYFRAME,
            reason=(
                "The shot changes composition/camera or lacks an explicit continuous-action "
                "selection, so it receives a distinct generated keyframe."
            ),
        )


class ShotKeyframeBriefCompiler:
    """Compile one provider-neutral brief from approved frozen product truth."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def compile(
        self,
        snapshot: ProductionInputSnapshot | ShotKeyframePlanningSnapshot,
        shot_id: str,
        generation_brief: GenerationBrief,
        *,
        continuity_facts: object | None = None,
        previous_approved_shot_context: PreviousApprovedShotContext | None = None,
    ) -> ShotKeyframeBrief:
        if generation_brief.project_id != snapshot.project_id:
            raise ShotKeyframeError("GenerationBrief does not belong to the snapshot project")
        if generation_brief.shot_id != shot_id:
            raise ShotKeyframeError("GenerationBrief does not belong to the requested shot")
        if (
            isinstance(snapshot, ShotKeyframePlanningSnapshot)
            and generation_brief.production_job_id != snapshot.production_job_id
        ):
            raise ShotKeyframeError(
                "GenerationBrief does not belong to the keyframe planning job"
            )
        revision = self.repository.get_shot_revision(snapshot.shot_plan_revision_id)
        if (
            revision is None
            or revision["project_id"] != snapshot.project_id
            or revision["status"] is not ShotRevisionStatus.APPROVED
        ):
            raise ShotKeyframeError("ShotKeyframeBrief requires the exact approved Shot Plan")
        script = self.repository.get_script_revision(
            str(revision["source_script_revision_id"])
        )
        story = (
            self.repository.get_story_revision(
                str(script["source_story_revision_id"])
            )
            if script is not None
            else None
        )
        if (
            script is None
            or script["project_id"] != snapshot.project_id
            or script["status"] is not ScriptRevisionStatus.APPROVED
            or story is None
            or story["project_id"] != snapshot.project_id
            or story["status"] is not StoryRevisionStatus.APPROVED
            or snapshot.script_revision_id != script["id"]
            or snapshot.story_revision_id != story["id"]
            or revision["content"].source_script_revision_id != script["id"]
        ):
            raise ShotKeyframeError(
                "ShotKeyframeBrief approved Story/Script/Shot Plan provenance changed"
            )
        shot = next(
            (item for item in revision["content"].shots if item.id == shot_id), None
        )
        if shot is None or shot_id not in snapshot.shot_parameters:
            raise ShotKeyframeError("Shot is absent from the frozen Production input")

        references = self._reference_provenance(snapshot, shot, generation_brief)
        by_role = {
            role: tuple(item for item in references if item.role is role)
            for role in ShotKeyframeReferenceRole
        }
        character_context = {
            str(item.get("id") or ""): item
            for item in generation_brief.character_context
            if isinstance(item, Mapping)
        }
        subjects = tuple(
            ShotKeyframeSubject(
                subject_id=subject_id,
                stable_identity_description=_stable_description(
                    character_context.get(subject_id),
                    fallback=f"Character {subject_id} as defined by locked identity provenance",
                ),
            )
            for subject_id in shot.subject
        )
        location_id = str(generation_brief.location_context.get("id") or shot.scene_id)
        location = ShotKeyframeLocation(
            location_id=location_id,
            stable_description=_stable_description(
                generation_brief.location_context,
                fallback=f"Location {location_id} as defined by the approved shot context",
            ),
        )
        lighting = ShotKeyframeLighting(
            quality=shot.lighting.quality or "unspecified",
            direction=shot.lighting.direction,
            tone=shot.lighting.tone,
            notes=shot.lighting.notes,
        )
        payload = {
            "project_id": snapshot.project_id,
            "shot_id": shot.id,
            "shot_plan_revision_id": snapshot.shot_plan_revision_id,
            "generation_brief_id": generation_brief.id,
            "generation_brief_sha256": generation_brief.sha256,
            "shot_visual_intent": shot.visual_intent,
            "shot_size": shot.shot_size.value,
            "camera_angle": shot.camera_angle.value,
            "camera_movement": shot.camera_movement.value,
            "composition": shot.composition,
            "subjects": [item.model_dump(mode="json") for item in subjects],
            "action": shot.action or shot.visual_intent,
            "lighting": lighting.model_dump(mode="json"),
            "location": location.model_dump(mode="json"),
            "identity_reference_provenance": [
                item.model_dump(mode="json")
                for item in by_role[ShotKeyframeReferenceRole.IDENTITY]
            ],
            "location_reference_provenance": [
                item.model_dump(mode="json")
                for item in by_role[ShotKeyframeReferenceRole.LOCATION]
            ],
            "prop_reference_provenance": [
                item.model_dump(mode="json")
                for item in by_role[ShotKeyframeReferenceRole.PROP]
            ],
            "style_reference_provenance": [
                item.model_dump(mode="json")
                for item in by_role[ShotKeyframeReferenceRole.STYLE]
            ],
            "continuity_facts": (
                continuity_facts.model_dump(mode="json")
                if hasattr(continuity_facts, "model_dump")
                else None
            ),
            "continuity_constraints": list(generation_brief.continuity_constraints),
            "previous_approved_shot_context": (
                previous_approved_shot_context.model_dump(mode="json")
                if previous_approved_shot_context is not None
                else None
            ),
            "negative_constraints": list(generation_brief.negative_constraints),
        }
        digest = _canonical_sha256(payload)
        return ShotKeyframeBrief(
            id=f"skfb:{digest[:32]}",
            sha256=digest,
            continuity_facts=continuity_facts,
            previous_approved_shot_context=previous_approved_shot_context,
            **{
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "continuity_facts",
                    "previous_approved_shot_context",
                }
            },
        )

    def _reference_provenance(
        self,
        snapshot: ProductionInputSnapshot | ShotKeyframePlanningSnapshot,
        shot: Shot,
        generation_brief: GenerationBrief,
    ) -> tuple[ReferenceProvenance, ...]:
        location_id = str(generation_brief.location_context.get("id") or "")
        allowed = {f"CHARACTER:{item}" for item in shot.subject}
        if location_id:
            allowed.add(f"LOCATION:{location_id}")
        allowed.add(f"SHOT:{shot.id}")
        result: list[ReferenceProvenance] = []
        for binding_key, raw_version_id in snapshot.reference_asset_versions.items():
            binding = str(binding_key)
            if not (
                binding in allowed
                or binding.startswith("STYLE:")
                or binding.startswith("PROP:")
            ):
                continue
            version_id = str(raw_version_id)
            version = self.repository.get_reference_asset_version(version_id)
            asset = (
                self.repository.get_reference_asset(version.asset_id)
                if version is not None
                else None
            )
            if (
                version is None
                or asset is None
                or version.project_id != snapshot.project_id
                or asset.project_id != snapshot.project_id
            ):
                raise ShotKeyframeError("Locked reference provenance is invalid")
            # The snapshot pins an immutable version.  A later Reference Asset
            # activation must not rewrite or invalidate already-frozen
            # keyframe provenance by forcing a lookup of ``current_version``.
            role = {
                ReferenceAssetType.CHARACTER_REFERENCE: ShotKeyframeReferenceRole.IDENTITY,
                ReferenceAssetType.LOCATION_REFERENCE: ShotKeyframeReferenceRole.LOCATION,
                ReferenceAssetType.PROP_REFERENCE: ShotKeyframeReferenceRole.PROP,
                ReferenceAssetType.STYLE_REFERENCE: ShotKeyframeReferenceRole.STYLE,
            }[asset.asset_type]
            subject_id = binding.split(":", 1)[1] if ":" in binding else None
            fallback = f"Locked {role.value.lower()} reference {subject_id or version.id}"
            result.append(
                ReferenceProvenance(
                    role=role,
                    asset_id=asset.id,
                    asset_version_id=version.id,
                    asset_type=asset.asset_type,
                    sha256=version.sha256,
                    binding_id=binding,
                    subject_id=subject_id,
                    stable_description=_stable_description(
                        version.metadata, fallback=fallback
                    ),
                )
            )
        return tuple(result)


class UniversalShotKeyframeImageService:
    """Translate ShotKeyframeBrief into one exact Universal IMAGE request."""

    @staticmethod
    def generate(
        brief: ShotKeyframeBrief,
        binding: UniversalImageBinding,
        *,
        provider_parameters: Mapping[str, object] | None = None,
        create_authorized: bool = False,
        request_id: str | None = None,
        execution_id: str | None = None,
        runtime_plan_id: str | None = None,
        runtime_plan_hash: str | None = None,
    ) -> GeneratedKeyframeImage:
        if create_authorized is not True:
            raise ShotKeyframeError(
                "Universal IMAGE create requires explicit authorization; no call was made"
            )
        plan_id = str(runtime_plan_id or "").strip()
        plan_hash = str(runtime_plan_hash or "").strip().lower()
        try:
            int(plan_hash, 16)
        except (TypeError, ValueError) as exc:
            raise ShotKeyframeError(
                "Universal IMAGE create requires an exact frozen RuntimePlan; no call was made"
            ) from exc
        if not plan_id or len(plan_hash) != 64:
            raise ShotKeyframeError(
                "Universal IMAGE create requires an exact frozen RuntimePlan; no call was made"
            )
        manifest = binding.manifest
        try:
            capability = CapabilityKind.coerce(getattr(manifest, "capability"))
        except Exception as exc:
            raise ShotKeyframeError("Universal IMAGE manifest is invalid") from exc
        if capability is not CapabilityKind.IMAGE:
            raise ShotKeyframeError("Shot keyframes require an IMAGE manifest")
        if getattr(manifest, "protocol", None) is None:
            raise ShotKeyframeError("Universal IMAGE manifest protocol is missing")
        parameters = _validated_image_provider_parameters(
            binding, provider_parameters
        )

        references = (
            brief.identity_reference_provenance
            + brief.location_reference_provenance
            + brief.prop_reference_provenance
            + brief.style_reference_provenance
        )
        supports_reference_images = bool(
            getattr(getattr(manifest, "reference", None), "images", False)
        )
        # The shipped IMAGE codec is text-only.  Preserve exact provenance and
        # stable descriptions in the contract/prompt, but do not pretend those
        # images were pixel-conditioning inputs.
        conditioning_mode = (
            "REFERENCE_IMAGE_CONDITIONING_AVAILABLE_BUT_NOT_BOUND_V1"
            if supports_reference_images
            else "TEXTUAL_PROVENANCE_ONLY"
        )
        prompt_payload = brief.model_dump(mode="json")
        prompt = (
            "Create the literal first frame for this exact approved shot. "
            "References below are identity/style constraints, not an existing first frame. "
            "Preserve the specified composition, camera, action, lighting, and stable "
            "descriptions. Do not copy a reference image as the whole composition.\n"
            f"SHOT_KEYFRAME_BRIEF_JSON={json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)}"
        )
        request = CapabilityRequest(
            request_id=request_id or uuid4().hex,
            project_id=brief.project_id,
            execution_id=execution_id,
            capability=CapabilityKind.IMAGE,
            protocol_family=getattr(manifest, "protocol"),
            provider_id=str(getattr(manifest, "provider_id")),
            model_id=str(getattr(manifest, "model_id")),
            manifest_id=str(getattr(manifest, "id")),
            manifest_hash=str(getattr(manifest, "manifest_hash")),
            codec_id=str(getattr(manifest, "codec_id")),
            runtime_plan_id=plan_id,
            runtime_plan_hash=plan_hash,
            inputs=(),
            prompt_or_text=prompt,
            structured_input={
                "contract": "SHOT_KEYFRAME_BRIEF_V1",
                "brief_id": brief.id,
                "brief_sha256": brief.sha256,
                "reference_conditioning_mode": conditioning_mode,
                "reference_provenance": [
                    {
                        "role": item.role.value,
                        "asset_version_id": item.asset_version_id,
                        "sha256": item.sha256,
                        "stable_description": item.stable_description,
                    }
                    for item in references
                ],
            },
            provider_parameters=parameters,
            create_authorized=True,
            authorization_required=bool(
                getattr(manifest, "authorization_required", True)
            ),
        )
        request_hash = _canonical_sha256(request.to_dict())
        try:
            raw = binding.runtime.submit(
                request,
                authorization={"approved": True, "create_authorized": True},
            )
        except Exception as exc:
            raise ShotKeyframeCreateUncertainError(
                "Universal IMAGE keyframe generation failed after one explicit create; "
                "no automatic retry was attempted"
            ) from exc
        if (
            not isinstance(raw, CapabilityResult)
            or raw.outcome is not RuntimeOutcome.SUCCEEDED
            or len(raw.outputs) != 1
        ):
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE returned no single completed artifact"
            )
        output = raw.outputs[0]
        if not output.mime_type.startswith("image/") or not output.sha256:
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE output identity is invalid"
            )
        try:
            content = bytes(binding.read_output(output))
        except Exception as exc:
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE output bytes are unavailable"
            ) from exc
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }.get(output.mime_type)
        if suffix is None:
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE output MIME is unsupported"
            )
        filename = f"shot-keyframe-{brief.shot_id}{suffix}"
        try:
            validate_image_input(content, filename, output.mime_type)
        except ValueError as exc:
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE output failed physical validation"
            ) from exc
        digest = _sha256_bytes(content)
        if digest != output.sha256 or (
            output.size_bytes is not None and output.size_bytes != len(content)
        ):
            raise ShotKeyframeProviderResultError(
                "Universal IMAGE output SHA/size changed"
            )
        return GeneratedKeyframeImage(
            content=content,
            mime_type=output.mime_type,
            filename=filename,
            sha256=digest,
            size_bytes=len(content),
            request_id=request.request_id,
            request_sha256=request_hash,
            manifest_id=request.manifest_id,
            manifest_hash=request.manifest_hash,
            provider_id=request.provider_id,
            model_id=request.model_id,
            reference_conditioning_mode=conditioning_mode,
        )


class ShotFirstFrameArtifactResolver:
    """Resolve only an exact frozen ShotFirstFrame artifact, never a Reference."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def resolve(self, snapshot: ProductionInputSnapshot) -> ResolvedShotFirstFrame:
        if len(snapshot.shot_parameters) != 1:
            raise ShotKeyframeError("VIDEO first-frame resolution requires exactly one shot")
        shot_id = next(iter(snapshot.shot_parameters))
        if shot_id not in snapshot.first_frame_required_shot_ids:
            raise ShotKeyframeError("Snapshot does not declare a required Shot First Frame")
        frame = snapshot.first_frame_for_shot(shot_id)
        if frame is None:
            raise ShotKeyframeError("Required frozen Shot First Frame is missing")
        return self.resolve_frame(
            snapshot.project_id,
            frame,
            expected_production_job_id=None,
        )

    def resolve_frame(
        self,
        project_id: str,
        frame: ShotFirstFrame,
        *,
        expected_production_job_id: str | None,
    ) -> ResolvedShotFirstFrame:
        if frame.project_id != project_id:
            raise ShotKeyframeError("Shot First Frame crosses project boundary")
        artifact = self.repository.get_production_artifact(frame.artifact_id)
        if (
            artifact is None
            or artifact.execution_id != frame.execution_id
            or artifact.artifact_type != SHOT_FIRST_FRAME_ARTIFACT_TYPE
        ):
            raise ShotKeyframeError("Frozen Shot First Frame artifact identity is invalid")
        execution = self.repository.get_production_execution(artifact.execution_id)
        job = (
            self.repository.get_production_job(execution.production_job_id)
            if execution is not None
            else None
        )
        if (
            execution is None
            or execution.status is not ProductionExecutionStatus.SUCCEEDED
            or job is None
            or job.project_id != project_id
            or job.shot_plan_revision_id != frame.shot_plan_revision_id
        ):
            raise ShotKeyframeError("Shot First Frame artifact provenance is invalid")
        if (
            expected_production_job_id is not None
            and job.id != expected_production_job_id
        ):
            raise ShotKeyframeError("Shot First Frame belongs to another ProductionJob")
        raw_contract = artifact.metadata_json.get("shot_first_frame")
        try:
            persisted = ShotFirstFrame.model_validate(raw_contract)
        except Exception as exc:
            raise ShotKeyframeError("Shot First Frame artifact contract is missing") from exc
        if persisted != frame:
            raise ShotKeyframeError("Frozen Shot First Frame contract changed")
        if (
            artifact.metadata_json.get("sha256") != frame.sha256
            or artifact.metadata_json.get("mime_type") != frame.mime_type
            or artifact.metadata_json.get("size_bytes") != frame.artifact_size_bytes
        ):
            raise ShotKeyframeError("Shot First Frame artifact metadata changed")
        path = self._artifact_path(project_id, artifact.path)
        if not path.is_file():
            raise ShotKeyframeError("Shot First Frame artifact file is unavailable")
        if path.stat().st_size != frame.artifact_size_bytes:
            raise ShotKeyframeError("Shot First Frame artifact size changed")
        try:
            content = path.read_bytes()
            validate_image_input(content, path.name, frame.mime_type)
        except (OSError, ValueError) as exc:
            raise ShotKeyframeError("Shot First Frame artifact image is invalid") from exc
        if _sha256_bytes(content) != frame.sha256:
            raise ShotKeyframeError("Shot First Frame artifact SHA-256 changed")
        return ResolvedShotFirstFrame(frame, artifact, path)

    def _artifact_path(self, project_id: str, relative_path: str) -> Path:
        normalized = str(relative_path).replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            relative.is_absolute()
            or PureWindowsPath(relative_path).drive
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ShotKeyframeError("Shot First Frame artifact path is unsafe")
        project_root = (self.repository.paths.projects / project_id).resolve()
        path = (project_root / Path(*relative.parts)).resolve()
        if project_root not in path.parents:
            raise ShotKeyframeError("Shot First Frame artifact path escapes project root")
        return path


class ShotKeyframeService:
    """Persist and freeze Shot First Frames using existing Production artifacts."""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        artifact_storage: ProductionArtifactStorageService | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_storage = artifact_storage or ProductionArtifactStorageService(
            repository
        )
        self.resolver = ShotFirstFrameArtifactResolver(repository)
        self.briefs = ShotKeyframeBriefCompiler(repository)
        self.image_runtime = UniversalShotKeyframeImageService()

    def build_planning_snapshot(
        self,
        project_id: str,
        production_job_id: str,
        *,
        shot_ids: Sequence[str] | None = None,
    ) -> ShotKeyframePlanningSnapshot:
        """Freeze approved creative truth without weakening VIDEO readiness.

        Locked References are included when they actually exist.  A text-only
        IMAGE keyframe smoke may therefore preserve an honestly empty
        Reference provenance, while the stricter Production snapshot remains
        the sole input accepted by VIDEO execution.
        """

        job = self.repository.get_production_job(production_job_id)
        revision = (
            self.repository.get_shot_revision(job.shot_plan_revision_id)
            if job is not None
            else None
        )
        script = (
            self.repository.get_script_revision(
                str(revision["source_script_revision_id"])
            )
            if revision is not None
            else None
        )
        story = (
            self.repository.get_story_revision(
                str(script["source_story_revision_id"])
            )
            if script is not None
            else None
        )
        if (
            job is None
            or job.project_id != project_id
            or revision is None
            or revision["project_id"] != project_id
            or revision["status"] is not ShotRevisionStatus.APPROVED
            or revision["content"].source_script_revision_id
            != revision["source_script_revision_id"]
            or script is None
            or script["project_id"] != project_id
            or script["status"] is not ScriptRevisionStatus.APPROVED
            or story is None
            or story["project_id"] != project_id
            or story["status"] is not StoryRevisionStatus.APPROVED
            or script["source_story_revision_id"] != story["id"]
        ):
            raise ShotKeyframeError(
                "Shot keyframe planning requires one exact approved Story/Script/Shot Plan chain"
            )

        ordered = tuple(sorted(revision["content"].shots, key=lambda item: item.order))
        requested = tuple(str(value) for value in shot_ids) if shot_ids is not None else tuple(
            shot.id for shot in ordered
        )
        if not requested or len(requested) != len(set(requested)):
            raise ShotKeyframeError("Shot keyframe planning shot IDs must be unique")
        by_id = {shot.id: shot for shot in ordered}
        if any(shot_id not in by_id for shot_id in requested):
            raise ShotKeyframeError("Shot keyframe planning requested an unknown shot")

        reference_service = ReferenceAssetService(self.repository)
        reference_versions: dict[str, str] = {}
        selected = tuple(by_id[shot_id] for shot_id in requested)
        scene_by_id = {scene.id: scene for scene in script["content"].scenes}
        targets: set[tuple[ReferenceBindingType, str]] = set()
        for shot in selected:
            targets.update(
                (ReferenceBindingType.CHARACTER, subject_id)
                for subject_id in shot.subject
            )
            scene = scene_by_id.get(shot.scene_id)
            if scene is None:
                raise ShotKeyframeError(
                    "Shot keyframe planning found a shot outside its approved Script"
                )
            targets.add((ReferenceBindingType.LOCATION, scene.location_id))
            targets.add((ReferenceBindingType.SHOT, shot.id))
        for binding_type, binding_id in sorted(
            targets, key=lambda item: (item[0].value, item[1])
        ):
            version_id = self._current_locked_reference_version(
                reference_service,
                project_id,
                binding_type,
                binding_id,
                story["id"],
            )
            if version_id is not None:
                reference_versions[f"{binding_type.value}:{binding_id}"] = version_id

        return ShotKeyframePlanningSnapshot(
            project_id=project_id,
            production_job_id=job.id,
            story_revision_id=story["id"],
            script_revision_id=script["id"],
            shot_plan_revision_id=revision["id"],
            reference_asset_versions=reference_versions,
            shot_parameters={
                shot.id: shot.model_dump(mode="json") for shot in selected
            },
        )

    def freeze_runtime_plan(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        binding: UniversalImageBinding,
        *,
        provider_parameters: Mapping[str, object],
        authorization: Mapping[str, object],
        selection_service: object | None = None,
    ) -> RuntimePlan:
        """Persist the canonical immutable plan before a paid IMAGE intent.

        RuntimePlan already has a durable repository seam.  Keyframes use that
        existing model directly and then copy its non-secret identity into the
        paid intent/ProviderTask audit records.  Credentials never enter either
        record.
        """

        parameters = _validated_image_provider_parameters(
            binding, provider_parameters
        )
        safe_authorization = sanitize_persistent_metadata(dict(authorization))
        if (
            not isinstance(safe_authorization, dict)
            or safe_authorization != dict(authorization)
            or safe_authorization.get("create_authorized") is not True
            or safe_authorization.get("per_item_max") != 1
            or safe_authorization.get("automatic_paid_retry") != 0
            or safe_authorization.get("shot_id") != brief.shot_id
        ):
            raise ShotKeyframeError(
                "Keyframe RuntimePlan requires one exact non-retrying paid authorization"
            )
        raw_resolution = str(parameters["resolution"])
        dimensions = raw_resolution.split("*")
        native_resolution = "x".join(dimensions)

        if selection_service is None:
            from .model_settings import SettingsModelService

            selection_service = SettingsModelService(self.repository)
        resolve_identity = getattr(selection_service, "runtime_plan_identity", None)
        if not callable(resolve_identity):
            raise ShotKeyframeError("Keyframe model selection service is invalid")
        try:
            selected_identity = dict(
                resolve_identity(project_id, CapabilityKind.IMAGE)
            )
        except Exception as exc:
            raise ShotKeyframeError(
                "Saved IMAGE selection cannot resolve into a RuntimePlan"
            ) from exc
        manifest = binding.manifest
        selected_manifest = dict(
            selected_identity.get("provider_parameters", {})
        )
        endpoint_profile_id = (
            str(getattr(manifest, "endpoint_profile_id", "") or "") or None
        )
        credential_reference = (
            str(getattr(manifest, "credential_reference", "") or "") or None
        )
        expected_selection = {
            "provider_capability": CapabilityKind.IMAGE.value,
            "provider_id": str(getattr(manifest, "provider_id", "")),
            "model_id": str(getattr(manifest, "model_id", "")),
            "endpoint_profile_id": endpoint_profile_id,
            "deployment_region": str(getattr(manifest, "deployment_region", "")),
            "endpoint_class": str(getattr(manifest, "endpoint_class", "")),
            "credential_reference": credential_reference,
        }
        if any(
            selected_identity.get(key) != value
            for key, value in expected_selection.items()
        ) or selected_manifest != {
            "manifest_id": str(getattr(manifest, "id", "")),
            "manifest_hash": str(getattr(manifest, "manifest_hash", "")),
            "codec_id": str(getattr(manifest, "codec_id", "")),
        }:
            raise ShotKeyframeError(
                "Saved IMAGE selection differs from the exact runtime binding"
            )

        references = (
            brief.identity_reference_provenance
            + brief.location_reference_provenance
            + brief.prop_reference_provenance
            + brief.style_reference_provenance
        )
        generation_brief = self.repository.get_generation_brief(
            brief.generation_brief_id
        )
        if (
            generation_brief is None
            or generation_brief.project_id != project_id
            or generation_brief.production_job_id != production_job_id
            or generation_brief.shot_id != brief.shot_id
            or generation_brief.sha256 != brief.generation_brief_sha256
        ):
            raise ShotKeyframeError(
                "Keyframe RuntimePlan GenerationBrief provenance is invalid"
            )
        try:
            plan = RuntimePlanService(self.repository).create_from_selection(
                project_id,
                brief=generation_brief,
                capability=CapabilityKind.IMAGE,
                selection_service=selection_service,
                production_job_id=production_job_id,
                transmitted_content_types=("text",),
                estimated_request_count=1,
                generation_mode="text_to_image",
                resolution=native_resolution,
                native_generation_fps=1.0,
                provider_generation_duration=float(
                    generation_brief.target_duration_seconds
                ),
                target_creative_duration=float(
                    generation_brief.target_duration_seconds
                ),
                duration_strategy="EXACT",
                audio_strategy="none",
                provider_parameters=parameters,
                reference_version_ids=tuple(
                    item.asset_version_id for item in references
                ),
                reference_roles={
                    item.asset_version_id: item.role.value for item in references
                },
                continuity_strategy="PRE_GENERATION_CONTINUITY",
                authorization=safe_authorization,
                prompt_template_version=SHOT_KEYFRAME_PROMPT_TEMPLATE_VERSION,
            )
        except Exception as exc:
            raise ShotKeyframeError(
                "Canonical keyframe RuntimePlan could not be frozen"
            ) from exc
        self._runtime_plan_evidence(
            brief,
            binding,
            plan,
            provider_parameters=parameters,
        )
        return plan

    def _runtime_plan_evidence(
        self,
        brief: ShotKeyframeBrief,
        binding: UniversalImageBinding,
        runtime_plan: RuntimePlan,
        *,
        provider_parameters: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(runtime_plan, RuntimePlan):
            raise ShotKeyframeError(
                "Keyframe RuntimePlan must be the canonical immutable domain object"
            )
        manifest = binding.manifest
        parameters = _validated_image_provider_parameters(
            binding, provider_parameters
        )
        generation_brief = self.repository.get_generation_brief(
            brief.generation_brief_id
        )
        if generation_brief is None:
            raise ShotKeyframeError("Keyframe RuntimePlan GenerationBrief is missing")
        expected_parameters = {
            **parameters,
            "manifest_id": str(getattr(manifest, "id", "")),
            "manifest_hash": str(getattr(manifest, "manifest_hash", "")),
            "codec_id": str(getattr(manifest, "codec_id", "")),
        }
        endpoint_profile_id = (
            str(getattr(manifest, "endpoint_profile_id", "") or "") or None
        )
        credential_reference = (
            str(getattr(manifest, "credential_reference", "") or "") or None
        )
        persisted = self.repository.get_runtime_plan(runtime_plan.id)
        if persisted is None or persisted != runtime_plan:
            raise ShotKeyframeError(
                "Keyframe RuntimePlan was not durably frozen before create"
            )
        try:
            canonical_plan_hash = RuntimePlanService.canonical_plan_hash(runtime_plan)
        except Exception as exc:
            raise ShotKeyframeError("Keyframe RuntimePlan hash is invalid") from exc
        if canonical_plan_hash != runtime_plan.plan_hash:
            raise ShotKeyframeError("Keyframe RuntimePlan hash is invalid")
        if (
            runtime_plan.project_id != brief.project_id
            or runtime_plan.production_job_id
            != generation_brief.production_job_id
            or runtime_plan.generation_brief_id != brief.generation_brief_id
            or runtime_plan.generation_brief_hash
            != brief.generation_brief_sha256
            or runtime_plan.provider_capability != CapabilityKind.IMAGE.value
            or runtime_plan.provider_id != str(getattr(manifest, "provider_id", ""))
            or runtime_plan.model_id != str(getattr(manifest, "model_id", ""))
            or runtime_plan.endpoint_profile_id
            != endpoint_profile_id
            or runtime_plan.deployment_region
            != str(getattr(manifest, "deployment_region", ""))
            or runtime_plan.endpoint_class
            != str(getattr(manifest, "endpoint_class", ""))
            or runtime_plan.credential_reference
            != credential_reference
            or runtime_plan.selection_source
            not in {"PROJECT_SELECTION", "GLOBAL_SELECTION"}
            or runtime_plan.provider_parameters != expected_parameters
            or runtime_plan.prompt_template_version
            != SHOT_KEYFRAME_PROMPT_TEMPLATE_VERSION
            or runtime_plan.authorization.get("create_authorized") is not True
            or runtime_plan.authorization.get("per_item_max") != 1
            or runtime_plan.authorization.get("automatic_paid_retry") != 0
            or runtime_plan.authorization.get("shot_id") != brief.shot_id
        ):
            raise ShotKeyframeError(
                "Keyframe RuntimePlan differs from the exact paid IMAGE intent"
            )
        evidence = {
            "runtime_plan_id": runtime_plan.id,
            "runtime_plan_hash": runtime_plan.plan_hash,
            "provider_capability": runtime_plan.provider_capability,
            "provider_id": runtime_plan.provider_id,
            "model_id": runtime_plan.model_id,
            "endpoint_profile_id": runtime_plan.endpoint_profile_id,
            "deployment_region": runtime_plan.deployment_region,
            "endpoint_class": runtime_plan.endpoint_class,
            "credential_reference": runtime_plan.credential_reference,
            "selection_source": runtime_plan.selection_source,
            "generation_brief_id": runtime_plan.generation_brief_id,
            "generation_brief_hash": runtime_plan.generation_brief_hash,
            "provider_parameters": dict(runtime_plan.provider_parameters),
            # ``authorization`` is intentionally a globally-sensitive metadata
            # key.  This value is the already-validated, non-secret paid-create
            # policy, so give the durable public evidence an explicit name that
            # cannot be confused with an HTTP Authorization header.
            "paid_create_authorization": dict(runtime_plan.authorization),
            "prompt_template_version": runtime_plan.prompt_template_version,
            "manifest_id": str(getattr(manifest, "id", "")),
            "manifest_hash": str(getattr(manifest, "manifest_hash", "")),
        }
        safe_evidence = sanitize_persistent_metadata(evidence)
        if not isinstance(safe_evidence, dict) or safe_evidence != evidence:
            raise ShotKeyframeError("Keyframe RuntimePlan evidence is unsafe")
        return evidence

    def paid_create_intent(
        self,
        brief: ShotKeyframeBrief,
        binding: UniversalImageBinding,
        *,
        runtime_plan: RuntimePlan,
        provider_parameters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return the safe exact intent that a paid grant must freeze."""

        manifest = binding.manifest
        try:
            capability = CapabilityKind.coerce(getattr(manifest, "capability"))
        except Exception as exc:
            raise ShotKeyframeError("Universal IMAGE manifest is invalid") from exc
        if capability is not CapabilityKind.IMAGE:
            raise ShotKeyframeError("Shot keyframes require an IMAGE manifest")
        manifest_id = str(getattr(manifest, "id", ""))
        manifest_hash = str(getattr(manifest, "manifest_hash", ""))
        provider_id = str(getattr(manifest, "provider_id", ""))
        model_id = str(getattr(manifest, "model_id", ""))
        if not all((manifest_id, manifest_hash, provider_id, model_id)):
            raise ShotKeyframeError("Universal IMAGE manifest identity is incomplete")
        parameters = _validated_image_provider_parameters(
            binding, provider_parameters
        )
        runtime_plan_evidence = self._runtime_plan_evidence(
            brief,
            binding,
            runtime_plan,
            provider_parameters=parameters,
        )
        create_once_identity = {
            "contract": "SHOT_KEYFRAME_CREATE_ONCE_V1",
            "project_id": brief.project_id,
            "shot_id": brief.shot_id,
            "shot_plan_revision_id": brief.shot_plan_revision_id,
            "generation_brief_id": brief.generation_brief_id,
            "shot_keyframe_brief_sha256": brief.sha256,
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "runtime_plan_id": runtime_plan.id,
            "runtime_plan_hash": runtime_plan.plan_hash,
        }
        payload = {
            **create_once_identity,
            "provider_id": provider_id,
            "model_id": model_id,
            "provider_parameters_sha256": _canonical_sha256(parameters),
            "runtime_plan_evidence": runtime_plan_evidence,
        }
        return {
            **payload,
            "create_once_identity_sha256": _canonical_sha256(
                create_once_identity
            ),
            "authorization_intent_id": _canonical_sha256(payload),
        }

    def authorize_paid_creates(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization_fingerprint: str,
        planned_creates: int,
        authorized_max: int,
        authorized_intents: Sequence[Mapping[str, object]],
    ) -> ProviderTask:
        """Freeze one bounded keyframe authorization window.

        Production's existing job ledger remains the authority for VIDEO
        creates.  Keyframes use append-only authorization windows so a later
        explicit grant for remaining frames never mutates or silently expands
        an earlier two-image smoke authorization.
        """

        job = self.repository.get_production_job(production_job_id)
        if job is None or job.project_id != project_id:
            raise ShotKeyframeError("Keyframe authorization job provenance is invalid")
        fingerprint = str(authorization_fingerprint).strip().lower()
        try:
            int(fingerprint, 16)
            planned = int(planned_creates)
            maximum = int(authorized_max)
        except (TypeError, ValueError) as exc:
            raise ShotKeyframeError("Keyframe paid authorization is invalid") from exc
        intents = tuple(dict(item) for item in authorized_intents)
        safe_intents = sanitize_persistent_metadata(list(intents))
        if (
            len(fingerprint) != 64
            or planned <= 0
            or planned != maximum
            or maximum != len(intents)
            or maximum > 10000
            or not isinstance(safe_intents, list)
            or safe_intents != list(intents)
            or any(
                item.get("contract") != "SHOT_KEYFRAME_CREATE_ONCE_V1"
                or set(item)
                != {
                    "contract",
                    "project_id",
                    "shot_id",
                    "shot_plan_revision_id",
                    "generation_brief_id",
                    "shot_keyframe_brief_sha256",
                    "manifest_id",
                    "manifest_hash",
                    "runtime_plan_id",
                    "runtime_plan_hash",
                    "provider_id",
                    "model_id",
                    "provider_parameters_sha256",
                    "runtime_plan_evidence",
                    "create_once_identity_sha256",
                    "authorization_intent_id",
                }
                or item.get("project_id") != project_id
                for item in intents
            )
            or len({str(item["shot_id"]) for item in intents}) != len(intents)
            or len(
                {str(item["authorization_intent_id"]) for item in intents}
            )
            != len(intents)
        ):
            raise ShotKeyframeError("Keyframe paid authorization is invalid")
        now = _now()
        idempotency_key = self._authorization_idempotency_key(
            production_job_id, fingerprint
        )
        task, _ = self.repository.get_or_create_provider_task(
            ProviderTask(
                id=f"skfa-{_sha256_bytes(idempotency_key.encode())[:32]}",
                project_id=project_id,
                capability=CapabilityKind.IMAGE.value,
                provider_id="AIDRAMA_AUTHORIZATION",
                model_id="SHOT_KEYFRAME_IMAGE_CREATE_V1",
                idempotency_key=idempotency_key,
                state="AUTHORIZED",
                request_summary={
                    "contract": "SHOT_KEYFRAME_PAID_AUTHORIZATION_V1",
                    "production_job_id": production_job_id,
                    "planned_creates": planned,
                    "authorized_max": maximum,
                    "per_item_max": 1,
                    "automatic_paid_retry": 0,
                    "authorized_intents": list(intents),
                },
                metadata={"authorization_fingerprint": fingerprint},
                created_at=now,
                updated_at=now,
            )
        )
        if (
            task.state != "AUTHORIZED"
            or task.execution_id is not None
            or task.request_summary
            != {
                "contract": "SHOT_KEYFRAME_PAID_AUTHORIZATION_V1",
                "production_job_id": production_job_id,
                "planned_creates": planned,
                "authorized_max": maximum,
                "per_item_max": 1,
                "automatic_paid_retry": 0,
                "authorized_intents": list(intents),
            }
            or task.metadata.get("authorization_fingerprint") != fingerprint
        ):
            raise ShotKeyframeError(
                "Keyframe paid authorization is frozen with different bounds"
            )
        return task

    @staticmethod
    def _authorization_idempotency_key(
        production_job_id: str, authorization_fingerprint: str
    ) -> str:
        return (
            "shot-keyframe-authorization:"
            f"{production_job_id}:{authorization_fingerprint}"
        )

    def generate_and_record(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
        binding: UniversalImageBinding,
        *,
        runtime_plan: RuntimePlan | None = None,
        provider_parameters: Mapping[str, object] | None = None,
        create_authorized: bool = False,
        authorization_fingerprint: str | None = None,
    ) -> ShotFirstFrame:
        if selection.source_type is not ShotFirstFrameSourceType.GENERATED_KEYFRAME:
            raise ShotKeyframeError("Generated keyframe path requires GENERATED_KEYFRAME")
        if create_authorized is not True:
            raise ShotKeyframeError(
                "Universal IMAGE create requires explicit authorization; no call was made"
            )
        if runtime_plan is None:
            raise ShotKeyframeError(
                "Canonical keyframe RuntimePlan must be frozen before create; no call was made"
            )
        fingerprint = str(authorization_fingerprint or "").strip().lower()
        authorization = self.repository.get_provider_task_by_idempotency(
            project_id,
            self._authorization_idempotency_key(
                production_job_id, fingerprint
            ),
        )
        if authorization is None or authorization.state != "AUTHORIZED":
            raise ShotKeyframeError(
                "PAID_BUDGET_MISSING: exact keyframe authorization was not frozen"
        )
        parameters = dict(provider_parameters or {})
        try:
            intent = self.paid_create_intent(
                brief,
                binding,
                runtime_plan=runtime_plan,
                provider_parameters=parameters,
            )
        except ShotKeyframeError as exc:
            raise ShotKeyframeError(
                "PAID_AUTHORIZATION_MISMATCH: keyframe RuntimePlan or intent changed"
            ) from exc
        authorized_intents = authorization.request_summary.get(
            "authorized_intents"
        )
        if not isinstance(authorized_intents, list) or intent not in authorized_intents:
            raise ShotKeyframeError(
                "PAID_AUTHORIZATION_MISMATCH: exact keyframe intent was not authorized"
            )
        manifest_id = str(intent["manifest_id"])
        manifest_hash = str(intent["manifest_hash"])
        provider_id = str(intent["provider_id"])
        model_id = str(intent["model_id"])

        job, _ = self._validate_record_scope(
            project_id, production_job_id, brief, selection
        )
        intent_digest = str(intent["create_once_identity_sha256"])
        idempotency_key = f"shot-keyframe:{intent_digest}"
        request_id = f"skfr-{intent_digest[:32]}"
        execution_id = f"skfx-{intent_digest[:32]}"
        now = _now()
        execution = self.repository.get_production_execution(execution_id)
        if execution is None:
            execution = self.repository.create_production_execution(
                ProductionExecution(
                    id=execution_id,
                    production_job_id=job.id,
                    status=ProductionExecutionStatus.QUEUED,
                    worker_type="UNIVERSAL_IMAGE_SHOT_KEYFRAME",
                    created_at=now,
                    runtime_plan_id=runtime_plan.id,
                    generation_brief_id=brief.generation_brief_id,
                )
            )
        elif (
            execution.production_job_id != job.id
            or execution.worker_type != "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
            or execution.runtime_plan_id != runtime_plan.id
            or execution.generation_brief_id != brief.generation_brief_id
        ):
            raise ShotKeyframeError("Keyframe execution intent identity conflicts")

        request_summary = {
            "contract": "SHOT_KEYFRAME_CREATE_ONCE_V1",
            "request_id": request_id,
            "shot_id": brief.shot_id,
            "shot_plan_revision_id": brief.shot_plan_revision_id,
            "generation_brief_id": brief.generation_brief_id,
            "shot_keyframe_brief_sha256": brief.sha256,
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "runtime_plan_id": runtime_plan.id,
            "runtime_plan_hash": runtime_plan.plan_hash,
            "runtime_plan_evidence": intent["runtime_plan_evidence"],
            "provider_parameters_sha256": intent[
                "provider_parameters_sha256"
            ],
            "authorization_intent_id": intent["authorization_intent_id"],
        }
        task, _ = self.repository.get_or_create_provider_task(
            ProviderTask(
                id=f"skft-{intent_digest[:32]}",
                project_id=project_id,
                execution_id=execution.id,
                capability=CapabilityKind.IMAGE.value,
                provider_id=provider_id,
                model_id=model_id,
                idempotency_key=idempotency_key,
                state="PENDING_SUBMISSION",
                request_summary=request_summary,
                metadata={
                    "automatic_paid_retry": 0,
                    "authorization_task_id": authorization.id,
                    "authorization_fingerprint": fingerprint,
                    "authorization_intent_id": intent[
                        "authorization_intent_id"
                    ],
                    "runtime_plan_id": runtime_plan.id,
                    "runtime_plan_hash": runtime_plan.plan_hash,
                },
                created_at=now,
                updated_at=now,
            )
        )
        if task.request_summary != request_summary:
            raise ShotKeyframeError(
                "Keyframe create intent provider parameters are already frozen"
            )
        recorded = self._recorded_frame_for_execution(
            project_id, execution, brief, runtime_plan
        )
        if recorded is not None:
            if task.state != "SUCCEEDED":
                task = self.repository.update_provider_submission_outcome(
                    task.id,
                    state="SUCCEEDED",
                    updated_at=_now(),
                    metadata=dict(task.metadata),
                )
            return recorded
        if task.state != "PENDING_SUBMISSION":
            raise ShotKeyframeError(
                f"{task.state}: keyframe create intent is closed; never resubmit"
            )
        try:
            task, claimed = self.repository.claim_bounded_provider_submission(
                task.id,
                authorization_task_id=authorization.id,
            )
        except ValueError as exc:
            raise ShotKeyframeError(str(exc)) from exc
        if not claimed:
            raise ShotKeyframeError(
                f"{task.state}: keyframe create intent is already claimed"
            )
        execution = self.repository.update_production_execution(
            execution.id,
            status=ProductionExecutionStatus.RUNNING,
            started_at=_now(),
        )
        try:
            generated = self.image_runtime.generate(
                brief,
                binding,
                provider_parameters=parameters,
                create_authorized=True,
                request_id=request_id,
                execution_id=execution.id,
                runtime_plan_id=runtime_plan.id,
                runtime_plan_hash=runtime_plan.plan_hash,
            )
        except ShotKeyframeCreateUncertainError as exc:
            self.repository.update_provider_submission_outcome(
                task.id,
                state="UNCERTAIN_CREATE",
                updated_at=_now(),
                error_message=sanitize_error(exc, max_length=1000),
            )
            raise ShotKeyframeError(
                "UNCERTAIN_CREATE: reconcile the original keyframe intent; never resubmit"
            ) from exc
        except Exception as exc:
            self.repository.update_provider_submission_outcome(
                task.id,
                state="FAILED",
                updated_at=_now(),
                error_message=sanitize_error(exc, max_length=1000),
            )
            self.repository.update_production_execution(
                execution.id,
                status=ProductionExecutionStatus.FAILED,
                finished_at=_now(),
            )
            raise

        received_metadata = {
            "automatic_paid_retry": 0,
            "authorization_task_id": authorization.id,
            "authorization_fingerprint": fingerprint,
            "authorization_intent_id": intent["authorization_intent_id"],
            "request_id": generated.request_id,
            "request_sha256": generated.request_sha256,
            "manifest_id": generated.manifest_id,
            "manifest_hash": generated.manifest_hash,
            "provider_id": generated.provider_id,
            "model_id": generated.model_id,
            "runtime_plan_id": runtime_plan.id,
            "runtime_plan_hash": runtime_plan.plan_hash,
            "result_sha256": generated.sha256,
            "result_size_bytes": generated.size_bytes,
        }
        task = self.repository.update_provider_submission_outcome(
            task.id,
            state="RESULT_RECEIVED",
            updated_at=_now(),
            metadata=received_metadata,
            submitted_at=_now(),
        )
        try:
            frame = self._record(
                project_id,
                production_job_id,
                brief,
                selection,
                generated.content,
                filename=generated.filename,
                mime_type=generated.mime_type,
                execution=execution,
                provider_provenance={
                    **received_metadata,
                    "provider_task_record_id": task.id,
                    "idempotency_key": idempotency_key,
                    "reference_conditioning_mode": generated.reference_conditioning_mode,
                },
            )
        except Exception as exc:
            self.repository.update_provider_submission_outcome(
                task.id,
                state="RESULT_PERSISTENCE_FAILED",
                updated_at=_now(),
                metadata=received_metadata,
                error_message=sanitize_error(exc, max_length=1000),
            )
            raise
        self.repository.update_provider_submission_outcome(
            task.id,
            state="SUCCEEDED",
            updated_at=_now(),
            metadata=received_metadata,
        )
        return frame

    @staticmethod
    def _current_locked_reference_version(
        reference_service: ReferenceAssetService,
        project_id: str,
        binding_type: ReferenceBindingType,
        binding_id: str,
        story_revision_id: str,
    ) -> str | None:
        if not reference_service.is_binding_ready(
            project_id,
            binding_type,
            binding_id,
            story_revision_id,
        ):
            return None
        for asset in reference_service.list_assets(project_id):
            if asset.current_version_id is None:
                continue
            version = reference_service.repository.get_reference_asset_version(
                asset.current_version_id
            )
            if version is None:
                continue
            if any(
                binding.binding_type is binding_type
                and binding.binding_id == binding_id
                and binding.asset_version_id == version.id
                for binding in reference_service.list_bindings(
                    project_id, version.id
                )
            ):
                return version.id
        return None

    def _validate_record_scope(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
    ):
        job = self.repository.get_production_job(production_job_id)
        generation_brief = self.repository.get_generation_brief(
            brief.generation_brief_id
        )
        if (
            job is None
            or job.project_id != project_id
            or job.shot_plan_revision_id != brief.shot_plan_revision_id
            or brief.project_id != project_id
            or selection.project_id != project_id
            or selection.shot_id != brief.shot_id
            or generation_brief is None
            or generation_brief.project_id != project_id
            or generation_brief.production_job_id != job.id
            or generation_brief.shot_id != brief.shot_id
            or generation_brief.sha256 != brief.generation_brief_sha256
        ):
            raise ShotKeyframeError(
                "Shot First Frame provenance does not match frozen job truth"
            )
        return job, generation_brief

    def _recorded_frame_for_execution(
        self,
        project_id: str,
        execution: ProductionExecution,
        brief: ShotKeyframeBrief,
        runtime_plan: RuntimePlan,
    ) -> ShotFirstFrame | None:
        artifacts = [
            artifact
            for artifact in self.repository.list_production_artifacts(execution.id)
            if artifact.artifact_type == SHOT_FIRST_FRAME_ARTIFACT_TYPE
        ]
        if not artifacts:
            return None
        if len(artifacts) != 1:
            raise ShotKeyframeError(
                "Keyframe execution has ambiguous persisted artifacts"
            )
        raw = artifacts[0].metadata_json.get("shot_first_frame")
        try:
            frame = ShotFirstFrame.model_validate(raw)
        except Exception as exc:
            raise ShotKeyframeError(
                "Keyframe execution persisted an invalid Shot First Frame"
            ) from exc
        if (
            frame.execution_id != execution.id
            or frame.artifact_id != artifacts[0].id
            or frame.shot_id != brief.shot_id
            or frame.shot_plan_revision_id != brief.shot_plan_revision_id
            or frame.generation_brief_id != brief.generation_brief_id
            or frame.shot_keyframe_brief_sha256 != brief.sha256
            or execution.runtime_plan_id != runtime_plan.id
        ):
            raise ShotKeyframeError(
                "Keyframe execution persisted different frozen provenance"
            )
        if execution.status is not ProductionExecutionStatus.SUCCEEDED:
            execution = self.repository.update_production_execution(
                execution.id,
                status=ProductionExecutionStatus.SUCCEEDED,
                finished_at=_now(),
            )
        self.resolver.resolve_frame(
            project_id,
            frame,
            expected_production_job_id=execution.production_job_id,
        )
        return frame

    def record_user_provided(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
    ) -> ShotFirstFrame:
        if selection.source_type is not ShotFirstFrameSourceType.USER_PROVIDED:
            raise ShotKeyframeError("User-provided path requires USER_PROVIDED selection")
        digest = _sha256_bytes(bytes(content))
        provenance = UserProvidedSourceProvenance(
            source_artifact_id=str(selection.user_source_artifact_id),
            source_sha256=digest,
            source_mime_type=mime_type,
            approval_source_id=str(selection.literal_reuse_authorization_id),
        )
        return self._record(
            project_id,
            production_job_id,
            brief,
            selection,
            content,
            filename=filename,
            mime_type=mime_type,
            user_provided_provenance=provenance,
        )

    def record_explicit_reference_override(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
    ) -> ShotFirstFrame:
        if selection.source_type is not ShotFirstFrameSourceType.EXPLICIT_REFERENCE_OVERRIDE:
            raise ShotKeyframeError("Reference override path requires explicit selection")
        version_id = str(selection.literal_reference_version_id)
        references = (
            brief.identity_reference_provenance
            + brief.location_reference_provenance
            + brief.prop_reference_provenance
            + brief.style_reference_provenance
        )
        reference = next(
            (item for item in references if item.asset_version_id == version_id), None
        )
        version = self.repository.get_reference_asset_version(version_id)
        if reference is None or version is None or version.project_id != project_id:
            raise ShotKeyframeError("Explicit reference override version is not frozen")
        path = self._reference_path(project_id, version.storage_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ShotKeyframeError("Explicit reference override file is unavailable") from exc
        if _sha256_bytes(content) != version.sha256:
            raise ShotKeyframeError("Explicit reference override SHA changed")
        return self._record(
            project_id,
            production_job_id,
            brief,
            selection,
            content,
            filename=version.filename,
            mime_type=version.mime_type,
            literal_reference_override_version_id=version.id,
        )

    def record_previous_shot_last_frame(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
        *,
        source_decision_id: str,
        ffmpeg_binary: str | Path | None = None,
    ) -> ShotFirstFrame:
        if selection.source_type is not ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME:
            raise ShotKeyframeError("Previous-frame path requires explicit continuity selection")
        decision = self.repository.get_production_shot_source_decision(
            source_decision_id
        )
        if (
            decision is None
            or decision.project_id != project_id
            or decision.production_job_id != production_job_id
            or decision.decision_type is not ProductionShotSourceDecisionType.SELECTED
            or decision.selection_kind
            is not ProductionShotSourceSelectionKind.FINAL_ACCEPTED
        ):
            raise ShotKeyframeError("Previous shot source is not exact FINAL_ACCEPTED truth")
        history = self.repository.list_production_shot_source_decisions(
            project_id,
            decision.production_shot_id,
        )
        if not history or history[-1].id != decision.id:
            raise ShotKeyframeError("Previous shot source decision is no longer selected")
        source_shot = self.repository.get_production_shot(decision.production_shot_id)
        if (
            source_shot is None
            or source_shot.production_job_id != production_job_id
            or source_shot.shot_id != selection.previous_shot_id
        ):
            raise ShotKeyframeError("Previous shot source does not match continuity policy")
        artifact = self.repository.get_production_artifact(
            decision.production_artifact_id
        )
        execution = self.repository.get_production_execution(
            decision.production_execution_id
        )
        qc = self.repository.get_production_qc_result(decision.qc_result_id)
        review = (
            self.repository.get_production_review(decision.review_id)
            if decision.review_id
            else None
        )
        if (
            artifact is None
            or execution is None
            or execution.production_job_id != production_job_id
            or artifact.execution_id != execution.id
            or qc is None
            or qc.execution_id != execution.id
            or qc.artifact_id != artifact.id
            or qc.status is not ProductionQCStatus.QC_PASS
            or review is None
            or review.qc_result_id != qc.id
            or review.decision is not ProductionReviewDecision.APPROVED
        ):
            raise ShotKeyframeError("Previous shot artifact lacks exact QC/human approval")
        source_path = self.resolver._artifact_path(project_id, artifact.path)
        if not source_path.is_file():
            raise ShotKeyframeError("Previous approved video artifact is unavailable")
        source_sha = _sha256_path(source_path)
        expected_sha = artifact.metadata_json.get("sha256")
        if not isinstance(expected_sha, str) or expected_sha != source_sha:
            raise ShotKeyframeError("Previous approved video artifact SHA changed")
        source_mime = str(artifact.metadata_json.get("mime_type") or "")
        if not source_mime.startswith("video/"):
            raise ShotKeyframeError("Previous approved artifact is not video")
        binary = self._ffmpeg_binary(ffmpeg_binary)
        self.repository.paths.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="shot-last-frame-", dir=self.repository.paths.root
        ) as directory:
            target = Path(directory) / "last-frame.png"
            command = [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-f",
                "image2",
                "-update",
                "1",
                "-c:v",
                "png",
                str(target),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if completed.returncode != 0 or not target.is_file():
                raise ShotKeyframeError(
                    "FFmpeg could not extract the exact previous shot last frame"
                )
            content = target.read_bytes()
        frame_sha = _sha256_bytes(content)
        provenance = PreviousApprovedArtifactProvenance(
            project_id=project_id,
            source_shot_id=source_shot.shot_id,
            source_execution_id=execution.id,
            source_artifact_id=artifact.id,
            source_artifact_sha256=source_sha,
            source_artifact_mime_type=source_mime,
            approval_source_id=decision.id,
            extracted_last_frame_sha256=frame_sha,
        )
        return self._record(
            project_id,
            production_job_id,
            brief,
            selection,
            content,
            filename=f"{brief.shot_id}-previous-last-frame.png",
            mime_type="image/png",
            previous_shot_provenance=provenance,
        )

    def freeze_snapshot(
        self,
        snapshot: ProductionInputSnapshot,
        frames: Sequence[ShotFirstFrame],
        *,
        required_shot_ids: Sequence[str],
    ) -> ProductionInputSnapshot:
        payload = snapshot.to_json_dict()
        payload["shot_first_frames"] = [
            frame.model_dump(mode="json") for frame in frames
        ]
        payload["first_frame_required_shot_ids"] = list(required_shot_ids)
        return ProductionInputSnapshot.model_validate(payload)

    def selected_first_frames(
        self, project_id: str, production_job_id: str
    ) -> dict[str, ShotFirstFrame]:
        job = self.repository.get_production_job(production_job_id)
        if job is None or job.project_id != project_id:
            raise ShotKeyframeError("ProductionJob does not belong to the project")
        selected: dict[str, ShotFirstFrame] = {}
        for execution in self.repository.list_production_executions(job.id):
            for artifact in self.repository.list_production_artifacts(execution.id):
                if artifact.artifact_type != SHOT_FIRST_FRAME_ARTIFACT_TYPE:
                    continue
                try:
                    frame = ShotFirstFrame.model_validate(
                        artifact.metadata_json.get("shot_first_frame")
                    )
                except Exception as exc:
                    raise ShotKeyframeError(
                        "Persisted Shot First Frame contract is invalid"
                    ) from exc
                if (
                    frame.project_id != project_id
                    or frame.shot_plan_revision_id != job.shot_plan_revision_id
                ):
                    raise ShotKeyframeError("Persisted Shot First Frame scope changed")
                current = selected.get(frame.shot_id)
                if current is None or (frame.created_at, frame.id) > (
                    current.created_at,
                    current.id,
                ):
                    selected[frame.shot_id] = frame
        return selected

    def validate_pre_live(
        self, project_id: str, production_job_id: str
    ) -> PreLiveFirstFrameReport:
        job = self.repository.get_production_job(production_job_id)
        revision = (
            self.repository.get_shot_revision(job.shot_plan_revision_id)
            if job is not None and job.project_id == project_id
            else None
        )
        if revision is None or revision["status"] is not ShotRevisionStatus.APPROVED:
            raise ShotKeyframeError("Pre-live validation requires an approved Shot Plan")
        planned = tuple(
            shot.id
            for shot in sorted(revision["content"].shots, key=lambda item: item.order)
        )
        try:
            selected = self.selected_first_frames(project_id, production_job_id)
        except ShotKeyframeError:
            selected = {}
        missing = tuple(shot_id for shot_id in planned if shot_id not in selected)
        invalid: list[str] = []
        valid: list[ShotFirstFrame] = []
        for shot_id in planned:
            frame = selected.get(shot_id)
            if frame is None:
                continue
            try:
                self.resolver.resolve_frame(
                    project_id,
                    frame,
                    expected_production_job_id=production_job_id,
                )
            except ShotKeyframeError:
                invalid.append(shot_id)
            else:
                valid.append(frame)
        order = {shot_id: index for index, shot_id in enumerate(planned)}
        by_sha: dict[str, list[ShotFirstFrame]] = {}
        for frame in valid:
            by_sha.setdefault(frame.sha256, []).append(frame)
        groups: list[DuplicateFirstFrameGroup] = []
        for digest, items in sorted(by_sha.items()):
            if len(items) < 2:
                continue
            ordered = sorted(items, key=lambda item: order[item.shot_id])
            unauthorized = [
                item
                for index, item in enumerate(ordered)
                if index > 0 and item.literal_reuse_authorization_id is None
            ]
            blocking = bool(unauthorized)
            override_id = None
            if not blocking:
                authorization_ids = [
                    item.literal_reuse_authorization_id
                    for item in ordered[1:]
                    if item.literal_reuse_authorization_id is not None
                ]
                override_id = f"duplicate-override:{_canonical_sha256(authorization_ids)[:24]}"
            groups.append(
                DuplicateFirstFrameGroup(
                    sha256=digest,
                    shot_ids=tuple(item.shot_id for item in ordered),
                    first_frame_ids=tuple(item.id for item in ordered),
                    artifact_ids=tuple(item.artifact_id for item in ordered),
                    explicit_creative_override_id=override_id,
                    blocking=blocking,
                )
            )
        blocking_groups = tuple(group for group in groups if group.blocking)
        reasons: list[str] = []
        if missing:
            reasons.append("missing first frame for shots: " + ", ".join(missing))
        if invalid:
            reasons.append("invalid first frame for shots: " + ", ".join(invalid))
        if blocking_groups:
            reasons.append(
                "unintended duplicate first-frame SHA groups: "
                + ", ".join(group.sha256 for group in blocking_groups)
            )
        gate = (
            PreLiveFirstFrameGate.BLOCKED
            if reasons
            else PreLiveFirstFrameGate.PASS
        )
        return PreLiveFirstFrameReport(
            project_id=project_id,
            shot_plan_revision_id=revision["id"],
            gate=gate,
            planned_shot_ids=planned,
            validated_first_frame_ids=tuple(item.id for item in valid),
            missing_first_frame_shot_ids=missing,
            invalid_first_frame_shot_ids=tuple(invalid),
            duplicate_groups=tuple(groups),
            unintended_duplicate_first_frame_count=len(blocking_groups),
            blocking_reasons=tuple(reasons),
        )

    def require_pre_live(
        self, project_id: str, production_job_id: str
    ) -> tuple[PreLiveFirstFrameReport, dict[str, ShotFirstFrame]]:
        report = self.validate_pre_live(project_id, production_job_id)
        if report.gate is PreLiveFirstFrameGate.BLOCKED:
            raise ShotKeyframeReadinessError(report)
        return report, self.selected_first_frames(project_id, production_job_id)

    @staticmethod
    def repair_recommendations(
        project_id: str, shot_ids: Sequence[str], reason: str
    ) -> tuple[ShotKeyframeRepairRecommendation, ...]:
        """Return decisions only; never call IMAGE/VIDEO or resubmit paid work."""

        identifiers = tuple(str(item) for item in shot_ids)
        return (
            ShotKeyframeRepairRecommendation(
                id=f"repair:{uuid4().hex}",
                project_id=project_id,
                shot_ids=identifiers,
                action=ShotKeyframeRepairAction.REGENERATE_KEYFRAME,
                reason=reason,
                estimated_scope=ShotKeyframeRepairScope.SINGLE_KEYFRAME,
                requires_paid_create=True,
            ),
            ShotKeyframeRepairRecommendation(
                id=f"repair:{uuid4().hex}",
                project_id=project_id,
                shot_ids=identifiers,
                action=ShotKeyframeRepairAction.REPLAN_SHOT,
                reason=reason,
                estimated_scope=ShotKeyframeRepairScope.SINGLE_SHOT,
                requires_paid_create=False,
            ),
            ShotKeyframeRepairRecommendation(
                id=f"repair:{uuid4().hex}",
                project_id=project_id,
                shot_ids=identifiers,
                action=ShotKeyframeRepairAction.HUMAN_DECISION,
                reason=reason,
                estimated_scope=ShotKeyframeRepairScope.SHOT_SEQUENCE,
                requires_paid_create=False,
            ),
        )

    def _record(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        previous_shot_provenance: PreviousApprovedArtifactProvenance | None = None,
        user_provided_provenance: UserProvidedSourceProvenance | None = None,
        literal_reference_override_version_id: str | None = None,
        provider_provenance: Mapping[str, object] | None = None,
        execution: ProductionExecution | None = None,
    ) -> ShotFirstFrame:
        job, _ = self._validate_record_scope(
            project_id, production_job_id, brief, selection
        )
        payload = bytes(content)
        if len(payload) > MAX_FIRST_FRAME_BYTES:
            raise ShotKeyframeError("Shot First Frame exceeds the image size limit")
        try:
            safe_name, normalized_mime, _ = validate_image_input(
                payload, filename, mime_type
            )
        except ValueError as exc:
            raise ShotKeyframeError("Shot First Frame image is invalid") from exc
        digest = _sha256_bytes(payload)
        now = _now()
        if execution is None:
            execution = ProductionExecution(
                id=uuid4().hex,
                production_job_id=job.id,
                status=ProductionExecutionStatus.SUCCEEDED,
                worker_type=(
                    "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
                    if selection.source_type
                    is ShotFirstFrameSourceType.GENERATED_KEYFRAME
                    else "SHOT_FIRST_FRAME_INGEST"
                ),
                started_at=now,
                finished_at=now,
                created_at=now,
                generation_brief_id=brief.generation_brief_id,
            )
            execution = self.repository.create_production_execution(execution)
        elif (
            execution.production_job_id != job.id
            or execution.worker_type != "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
            or execution.generation_brief_id != brief.generation_brief_id
            or execution.status
            not in {
                ProductionExecutionStatus.QUEUED,
                ProductionExecutionStatus.RUNNING,
            }
        ):
            raise ShotKeyframeError(
                "Precreated keyframe execution provenance is invalid"
            )
        try:
            relative_path, stored_metadata = self.artifact_storage.store(
                project_id,
                execution.id,
                SHOT_FIRST_FRAME_ARTIFACT_TYPE,
                payload,
                filename=safe_name,
                metadata={
                    "mime_type": normalized_mime,
                    "shot_id": brief.shot_id,
                    "shot_plan_revision_id": brief.shot_plan_revision_id,
                    "generation_brief_id": brief.generation_brief_id,
                },
            )
        except ProductionArtifactStorageError as exc:
            raise ShotKeyframeError("Shot First Frame artifact storage failed") from exc
        artifact_id = uuid4().hex
        frame = ShotFirstFrame(
            id=uuid4().hex,
            project_id=project_id,
            shot_id=brief.shot_id,
            shot_plan_revision_id=brief.shot_plan_revision_id,
            generation_brief_id=brief.generation_brief_id,
            shot_keyframe_brief_id=brief.id,
            shot_keyframe_brief_sha256=brief.sha256,
            artifact_id=artifact_id,
            execution_id=execution.id,
            artifact_size_bytes=len(payload),
            sha256=digest,
            mime_type=normalized_mime,
            source_type=selection.source_type,
            selection=selection,
            identity_reference_provenance=brief.identity_reference_provenance,
            location_reference_provenance=brief.location_reference_provenance,
            prop_reference_provenance=brief.prop_reference_provenance,
            style_reference_provenance=brief.style_reference_provenance,
            previous_shot_provenance=previous_shot_provenance,
            user_provided_provenance=user_provided_provenance,
            literal_reference_override_version_id=(
                literal_reference_override_version_id
            ),
            literal_reuse_authorization_id=(
                selection.literal_reuse_authorization_id
            ),
            created_at=now,
        )
        metadata = {
            **dict(stored_metadata),
            "mime_type": normalized_mime,
            "sha256": digest,
            "size_bytes": len(payload),
            "shot_first_frame": frame.model_dump(mode="json"),
            "shot_keyframe_brief": brief.model_dump(mode="json"),
            "provider_provenance": dict(provider_provenance or {}),
        }
        sanitized = sanitize_persistent_metadata(metadata)
        if not isinstance(sanitized, dict) or sanitized != metadata:
            self.artifact_storage.discard_unrecorded(
                project_id,
                execution.id,
                relative_path,
                expected_sha256=digest,
            )
            raise ShotKeyframeError("Shot First Frame metadata contains unsafe values")
        artifact = ProductionArtifact(
            id=artifact_id,
            execution_id=execution.id,
            artifact_type=SHOT_FIRST_FRAME_ARTIFACT_TYPE,
            path=relative_path,
            metadata_json=metadata,
            created_at=now,
        )
        try:
            stored = self.repository.create_production_artifact_idempotent(
                artifact, sha256=digest
            )
        except Exception as exc:
            self.artifact_storage.discard_unrecorded(
                project_id,
                execution.id,
                relative_path,
                expected_sha256=digest,
            )
            raise ShotKeyframeError("Shot First Frame artifact record failed") from exc
        if stored.id != artifact_id:
            raise ShotKeyframeError("Shot First Frame artifact identity was not immutable")
        if execution.status is not ProductionExecutionStatus.SUCCEEDED:
            self.repository.update_production_execution(
                execution.id,
                status=ProductionExecutionStatus.SUCCEEDED,
                finished_at=_now(),
            )
        return frame

    def _reference_path(self, project_id: str, storage_path: str) -> Path:
        normalized = str(storage_path).replace("\\", "/")
        relative = PurePosixPath(normalized)
        if relative.is_absolute() or ".." in relative.parts:
            raise ShotKeyframeError("Reference override path is unsafe")
        project_root = (self.repository.paths.projects / project_id).resolve()
        path = (project_root / Path(*relative.parts)).resolve()
        if project_root not in path.parents or not path.is_file():
            raise ShotKeyframeError("Reference override path is unavailable")
        return path

    @staticmethod
    def _ffmpeg_binary(explicit: str | Path | None) -> str:
        if explicit is not None:
            candidate = Path(explicit)
            if candidate.is_file():
                return str(candidate)
            raise ShotKeyframeError("Configured FFmpeg binary is unavailable")
        system = shutil.which("ffmpeg")
        if system:
            return system
        try:
            import imageio_ffmpeg

            candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception as exc:
            raise ShotKeyframeError("Real FFmpeg is unavailable") from exc
        if not candidate.is_file():
            raise ShotKeyframeError("Real FFmpeg is unavailable")
        return str(candidate)


__all__ = [
    "GeneratedKeyframeImage",
    "ResolvedShotFirstFrame",
    "SHOT_FIRST_FRAME_ARTIFACT_TYPE",
    "SHOT_KEYFRAME_PROMPT_TEMPLATE_VERSION",
    "ShotFirstFrameArtifactResolver",
    "ShotKeyframeBriefCompiler",
    "ShotKeyframeError",
    "ShotKeyframePolicy",
    "ShotKeyframeReadinessError",
    "ShotKeyframeService",
    "UniversalImageBinding",
    "UniversalShotKeyframeImageService",
]
