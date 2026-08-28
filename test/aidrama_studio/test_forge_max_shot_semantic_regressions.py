"""P0 semantic regressions for the formal Shot First Frame architecture.

The original shard was authored against the pre-keyframe baseline and used
``CHARACTER_REFERENCE`` rows as literal ``SHOT:*`` media.  These tests retain
the seven authoritative semantic gates while exercising the integrated
ProductionArtifact-backed ``ShotKeyframeService`` from ``fe028541``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import (
    PreLiveFirstFrameGate,
    ProductionInputSnapshot,
    ShotFirstFrameSourceType,
)
from aidrama_studio.services import (
    ReferenceAssetService,
    ReferenceAssetStorageService,
    ShotFirstFrameArtifactResolver,
    ShotKeyframePolicy,
    ShotKeyframeService,
)
from aidrama_studio.services.adapters import WanAdapterError, WanFirstFrameResolver
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio import test_shot_keyframe_continuity as canonical


def test_character_reference_is_not_an_implicit_first_frame_without_shot_override(
    tmp_path: Path,
) -> None:
    """Identity references can make Production ready but cannot satisfy I2V."""

    context = canonical._build_context(tmp_path)
    service = ShotKeyframeService(context.repository)
    report = service.validate_pre_live(context.project.id, context.job.id)

    assert report.gate is PreLiveFirstFrameGate.BLOCKED
    assert report.missing_first_frame_shot_ids == tuple(
        shot.id for shot in context.shots
    )
    assert report.validated_first_frame_ids == ()

    shot = context.shots[0]
    missing = ProductionInputSnapshot(
        project_id=context.snapshot.project_id,
        story_revision_id=context.snapshot.story_revision_id,
        script_revision_id=context.snapshot.script_revision_id,
        shot_plan_revision_id=context.snapshot.shot_plan_revision_id,
        generation_brief_id=context.briefs[0].id,
        reference_asset_versions=context.snapshot.reference_asset_versions,
        shot_parameters={shot.id: context.snapshot.shot_parameters[shot.id]},
        first_frame_required_shot_ids=(shot.id,),
    )
    resolver = WanFirstFrameResolver(
        ShotFirstFrameArtifactResolver(context.repository)
    )
    with pytest.raises(WanAdapterError, match="Required frozen Shot First Frame is missing"):
        resolver.resolve(missing)


def test_three_shot_one_character_freezes_three_distinct_first_frame_artifacts(
    tmp_path: Path,
) -> None:
    """One identity source constrains three distinct literal compositions."""

    canonical.test_three_shot_distinct_keyframes_preserve_reference_identity_and_freeze(
        tmp_path
    )


def test_frozen_first_frame_does_not_follow_a_newer_reference_after_freeze(
    tmp_path: Path,
) -> None:
    """Changing current identity truth never rewrites frozen first frames."""

    context = canonical._build_context(tmp_path)
    service, _runtime, frames = canonical._generate_frames(
        context,
        (png_bytes(color="red"), png_bytes(color="green"), png_bytes(color="blue")),
    )
    frozen = service.freeze_snapshot(
        context.snapshot,
        frames,
        required_shot_ids=[shot.id for shot in context.shots],
    )
    before = tuple(
        frozen.first_frame_for_shot(shot.id) for shot in context.shots
    )

    references = ReferenceAssetService(context.repository)
    newer = ReferenceAssetStorageService(references).import_image(
        context.project.id,
        context.character_asset.id,
        png_bytes(color="yellow"),
        filename="newer-character-identity.png",
        mime_type="image/png",
        metadata={
            "source_story_revision_id": "story_revision_001",
            "stable_description": "A newer approved identity rendering",
        },
    )
    references.activate_version(
        context.project.id, context.character_asset.id, newer.id
    )

    round_trip = ProductionInputSnapshot.model_validate_json(
        frozen.model_dump_json()
    )
    assert tuple(
        round_trip.first_frame_for_shot(shot.id) for shot in context.shots
    ) == before
    assert {
        frame.identity_reference_provenance[0].asset_version_id
        for frame in before
    } == {context.character_version.id}
    assert newer.id not in {
        frame.identity_reference_provenance[0].asset_version_id
        for frame in before
    }
    reloaded = ShotKeyframeService(ProjectRepository(context.repository.paths))
    assert reloaded.selected_first_frames(context.project.id, context.job.id) == {
        frame.shot_id: frame for frame in frames
    }


def test_previous_shot_last_frame_is_optional_continuity_input_not_global_first_frame(
    tmp_path: Path,
) -> None:
    """Previous approved LAST frame reuse remains an explicit composition decision."""

    canonical.test_previous_approved_last_frame_is_exact_and_new_composition_is_generated(
        tmp_path
    )


def test_wan_consumes_exact_frozen_shot_first_frame_not_character_or_location_reference(
    tmp_path: Path,
) -> None:
    """Wan consumes only the exact resolved ProductionArtifact-backed frame."""

    canonical.test_wan_uses_exact_frozen_first_frame_and_never_falls_back_to_reference(
        tmp_path
    )


def test_duplicate_first_frame_sha_blocks_pre_live_readiness_without_explicit_override(
    tmp_path: Path,
) -> None:
    """Identical bytes without shot-level intent block before paid VIDEO work."""

    canonical.test_duplicate_character_image_as_literal_first_frame_blocks_pre_live(
        tmp_path
    )


def test_duplicate_first_frame_sha_with_explicit_intentional_override_is_allowed(
    tmp_path: Path,
) -> None:
    """A duplicate is allowed only through explicit per-shot human intent."""

    context = canonical._build_context(tmp_path)
    service = ShotKeyframeService(context.repository)
    briefs = canonical._compile_keyframe_briefs(context, service)
    shared = png_bytes(color="cyan")
    frames = []
    for index, (shot, brief) in enumerate(
        zip(context.shots, briefs, strict=True), start=1
    ):
        selection = ShotKeyframePolicy.select(
            shot,
            project_id=context.project.id,
            user_source_artifact_id=f"human-source-{index}",
            user_approval_id=f"explicit-same-composition-approval-{index}",
        )
        assert selection.source_type is ShotFirstFrameSourceType.USER_PROVIDED
        frames.append(
            service.record_user_provided(
                context.project.id,
                context.job.id,
                brief,
                selection,
                shared,
                filename=f"shot-{index}-approved-shared-frame.png",
                mime_type="image/png",
            )
        )

    assert len({frame.sha256 for frame in frames}) == 1
    report = service.validate_pre_live(context.project.id, context.job.id)
    assert report.gate is PreLiveFirstFrameGate.PASS
    assert report.unintended_duplicate_first_frame_count == 0
    assert len(report.duplicate_groups) == 1
    assert report.duplicate_groups[0].blocking is False
    assert report.duplicate_groups[0].explicit_creative_override_id is not None


__all__ = [name for name in globals() if name.startswith("test_")]
