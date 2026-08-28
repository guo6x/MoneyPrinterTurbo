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
    Shot,
    ShotRevisionStatus,
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
        snapshot: ProductionInputSnapshot,
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
        revision = self.repository.get_shot_revision(snapshot.shot_plan_revision_id)
        if (
            revision is None
            or revision["project_id"] != snapshot.project_id
            or revision["status"] is not ShotRevisionStatus.APPROVED
        ):
            raise ShotKeyframeError("ShotKeyframeBrief requires the exact approved Shot Plan")
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
        snapshot: ProductionInputSnapshot,
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
    ) -> GeneratedKeyframeImage:
        if create_authorized is not True:
            raise ShotKeyframeError(
                "Universal IMAGE create requires explicit authorization; no call was made"
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
            request_id=uuid4().hex,
            project_id=brief.project_id,
            capability=CapabilityKind.IMAGE,
            protocol_family=getattr(manifest, "protocol"),
            provider_id=str(getattr(manifest, "provider_id")),
            model_id=str(getattr(manifest, "model_id")),
            manifest_id=str(getattr(manifest, "id")),
            manifest_hash=str(getattr(manifest, "manifest_hash")),
            codec_id=str(getattr(manifest, "codec_id")),
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
            provider_parameters=dict(provider_parameters or {}),
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
            raise ShotKeyframeError(
                "Universal IMAGE keyframe generation failed after one explicit create; "
                "no automatic retry was attempted"
            ) from exc
        if (
            not isinstance(raw, CapabilityResult)
            or raw.outcome is not RuntimeOutcome.SUCCEEDED
            or len(raw.outputs) != 1
        ):
            raise ShotKeyframeError("Universal IMAGE returned no single completed artifact")
        output = raw.outputs[0]
        if not output.mime_type.startswith("image/") or not output.sha256:
            raise ShotKeyframeError("Universal IMAGE output identity is invalid")
        try:
            content = bytes(binding.read_output(output))
        except Exception as exc:
            raise ShotKeyframeError("Universal IMAGE output bytes are unavailable") from exc
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }.get(output.mime_type)
        if suffix is None:
            raise ShotKeyframeError("Universal IMAGE output MIME is unsupported")
        filename = f"shot-keyframe-{brief.shot_id}{suffix}"
        try:
            validate_image_input(content, filename, output.mime_type)
        except ValueError as exc:
            raise ShotKeyframeError("Universal IMAGE output failed physical validation") from exc
        digest = _sha256_bytes(content)
        if digest != output.sha256 or (
            output.size_bytes is not None and output.size_bytes != len(content)
        ):
            raise ShotKeyframeError("Universal IMAGE output SHA/size changed")
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

    def generate_and_record(
        self,
        project_id: str,
        production_job_id: str,
        brief: ShotKeyframeBrief,
        selection: ShotKeyframeSelection,
        binding: UniversalImageBinding,
        *,
        provider_parameters: Mapping[str, object] | None = None,
        create_authorized: bool = False,
    ) -> ShotFirstFrame:
        if selection.source_type is not ShotFirstFrameSourceType.GENERATED_KEYFRAME:
            raise ShotKeyframeError("Generated keyframe path requires GENERATED_KEYFRAME")
        generated = self.image_runtime.generate(
            brief,
            binding,
            provider_parameters=provider_parameters,
            create_authorized=create_authorized,
        )
        return self._record(
            project_id,
            production_job_id,
            brief,
            selection,
            generated.content,
            filename=generated.filename,
            mime_type=generated.mime_type,
            provider_provenance={
                "request_id": generated.request_id,
                "request_sha256": generated.request_sha256,
                "manifest_id": generated.manifest_id,
                "manifest_hash": generated.manifest_hash,
                "provider_id": generated.provider_id,
                "model_id": generated.model_id,
                "reference_conditioning_mode": generated.reference_conditioning_mode,
            },
        )

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
    ) -> ShotFirstFrame:
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
            raise ShotKeyframeError("Shot First Frame provenance does not match frozen job truth")
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
        execution = ProductionExecution(
            id=uuid4().hex,
            production_job_id=job.id,
            status=ProductionExecutionStatus.SUCCEEDED,
            worker_type=(
                "UNIVERSAL_IMAGE_SHOT_KEYFRAME"
                if selection.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME
                else "SHOT_FIRST_FRAME_INGEST"
            ),
            started_at=now,
            finished_at=now,
            created_at=now,
            generation_brief_id=brief.generation_brief_id,
        )
        self.repository.create_production_execution(execution)
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
