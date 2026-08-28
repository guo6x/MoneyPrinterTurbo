from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidrama_studio.domain import (
    CameraAngle,
    CameraMovement,
    Character,
    Location,
    PreLiveFirstFrameGate,
    ProductionArtifact,
    ProductionExecution,
    ProductionExecutionStatus,
    ProductionInputSnapshot,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
    ReferenceAssetType,
    ReferenceBindingType,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotFirstFrame,
    ShotFirstFrameSourceType,
    ShotKeyframeSelectionPolicy,
    ShotPlan,
    ShotRevisionStatus,
    ShotSize,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
)
from aidrama_studio.services import (
    FinalAssemblyService,
    GenerationBriefService,
    ProductionExecutionService,
    ProductionService,
    ProjectService,
    ReferenceAssetService,
    ReferenceAssetStorageService,
)
from aidrama_studio.services.adapters.wan_video import (
    WanAdapterError,
    WanFirstFrameResolver,
    WanInputMapper,
    WanProviderConfig,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentRef,
    RuntimeOutcome,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    build_mainland_manifests,
)
from aidrama_studio.services.shot_keyframe import (
    ShotFirstFrameArtifactResolver,
    ShotKeyframeError,
    ShotKeyframePolicy,
    ShotKeyframeService,
    UniversalImageBinding,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.video_fixtures import mp4_bytes


_NOW = "2026-08-28T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _offline_temp_data_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Every test is offline and makes even a default repository temporary."""

    isolated_default = tmp_path / "isolated-default-data"
    forbidden_real_default = tmp_path / "must-not-use-localappdata"
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(isolated_default))
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    monkeypatch.setenv("LOCALAPPDATA", str(forbidden_real_default))
    for variable in (
        "AIDRAMA_ALLOW_PAID",
        "AIDRAMA_ALLOW_PAID_LIVE_TESTS",
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "ARK_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shot-keyframe tests attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    yield
    assert not forbidden_real_default.exists()


@dataclass(frozen=True)
class _Context:
    repository: ProjectRepository
    project: object
    job: object
    shots: tuple[Shot, ...]
    snapshot: ProductionInputSnapshot
    briefs: tuple[object, ...]
    character_asset: object
    character_version: object
    location_asset: object
    location_version: object


def _build_context(
    tmp_path: Path,
    *,
    specifications: Sequence[
        tuple[ShotSize, CameraAngle, CameraMovement, str, str]
    ]
    | None = None,
) -> _Context:
    specs = tuple(
        specifications
        or (
            (
                ShotSize.WIDE,
                CameraAngle.EYE_LEVEL,
                CameraMovement.STATIC,
                "wide establishing composition",
                "The hero crosses the entire terminal",
            ),
            (
                ShotSize.MEDIUM,
                CameraAngle.OTHER,
                CameraMovement.TRACK,
                "medium side composition",
                "The hero continues along the platform",
            ),
            (
                ShotSize.CLOSE_UP,
                CameraAngle.EYE_LEVEL,
                CameraMovement.PUSH_IN,
                "close-up facial composition",
                "The hero notices the final signal",
            ),
        )
    )
    data_root = tmp_path / "explicit-test-data"
    paths = DatabasePaths(
        data_root / "aidrama.db",
        data_root / "projects",
        data_root / "archived-projects",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(
        title="Shot keyframe continuity fixture",
        target_duration_seconds=15,
        delivery_resolution_label="720p",
        target_fps=24,
        quality_mode="FINAL",
    )
    story = StoryBible(
        title="Continuity story",
        logline="One hero crosses one terminal through three compositions.",
        premise="Identity remains locked while each shot owns its composition.",
        genre="Drama",
        tone="Restrained",
        world=World(era="Present", setting="A quiet rail terminal"),
        characters=[
            Character(
                id="char_001",
                name="Hero",
                identity="same locked hero",
                appearance="short dark hair and a blue coat",
            )
        ],
        locations=[
            Location(
                id="loc_001",
                name="Terminal",
                environment="long concrete platform",
                visual_style="cool dawn light",
            )
        ],
        story_beats=[
            StoryBeat(
                id=f"beat_{index:03d}",
                order=index,
                type=("OPENING", "DEVELOPMENT", "ENDING")[index - 1],
                summary=f"Beat {index}",
                characters=["char_001"],
                location_id="loc_001",
            )
            for index in range(1, 4)
        ],
    )
    repository.create_story_revision(
        revision_id="story_revision_001",
        project_id=project.id,
        version=1,
        status=StoryRevisionStatus.APPROVED,
        content=story,
        generation_input={"fixture": "offline"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    script = StructuredScript(
        title="Continuity script",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Terminal crossing",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=15,
                beats=[
                    ScriptBeat(
                        id=f"script_beat_{index:03d}",
                        order=index,
                        type=ScriptBeatType.ACTION,
                        text=specs[index - 1][4],
                        estimated_duration_seconds=5,
                    )
                    for index in range(1, 4)
                ],
            )
        ],
    )
    repository.create_script_revision(
        revision_id="script_revision_001",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_revision_001",
        content=script,
        generation_input={"fixture": "offline"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    shots = tuple(
        Shot(
            id=f"shot_{index:03d}",
            order=index,
            scene_id="scene_001",
            source_script_beat_ids=[f"script_beat_{index:03d}"],
            duration_seconds=5,
            shot_size=spec[0],
            camera_angle=spec[1],
            camera_movement=spec[2],
            composition=spec[3],
            subject=["char_001"],
            action=spec[4],
            visual_intent=f"Literal shot-specific visual intent {index}",
        )
        for index, spec in enumerate(specs, start=1)
    )
    repository.create_shot_revision(
        revision_id="shot_plan_revision_001",
        project_id=project.id,
        version=1,
        status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_revision_001",
        content=ShotPlan(
            title="Three shot continuity plan",
            source_script_revision_id="script_revision_001",
            shots=list(shots),
        ),
        generation_input={"fixture": "offline"},
        created_at=_NOW,
        updated_at=_NOW,
    )

    references = ReferenceAssetService(repository)
    storage = ReferenceAssetStorageService(references)
    character_asset = references.create_asset(
        project.id, ReferenceAssetType.CHARACTER_REFERENCE
    )
    character_version = storage.import_image(
        project.id,
        character_asset.id,
        png_bytes(color="red"),
        filename="locked-character.png",
        mime_type="image/png",
        metadata={
            "source_story_revision_id": "story_revision_001",
            "stable_description": "Hero with short dark hair and a blue coat",
        },
    )
    references.activate_version(project.id, character_asset.id, character_version.id)
    references.bind_version(
        project.id,
        character_version.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )
    location_asset = references.create_asset(
        project.id, ReferenceAssetType.LOCATION_REFERENCE
    )
    location_version = storage.import_image(
        project.id,
        location_asset.id,
        png_bytes(color="white"),
        filename="locked-location.png",
        mime_type="image/png",
        metadata={
            "source_story_revision_id": "story_revision_001",
            "stable_description": "Long concrete platform in cool dawn light",
        },
    )
    references.activate_version(project.id, location_asset.id, location_version.id)
    references.bind_version(
        project.id,
        location_version.id,
        ReferenceBindingType.LOCATION,
        "loc_001",
    )

    production = ProductionService(repository, reference_service=references)
    assert production.validate_job_readiness(
        project.id, "shot_plan_revision_001"
    )["ready"]
    job = production.create_production_job(project.id, "shot_plan_revision_001")
    production.create_production_shots(project.id, job.id)
    briefs = GenerationBriefService(repository).prepare_for_job(project.id, job.id)
    snapshot = ProductionExecutionService(
        repository, production_service=production
    ).create_input_snapshot(project.id, job.id)
    return _Context(
        repository=repository,
        project=project,
        job=job,
        shots=shots,
        snapshot=snapshot,
        briefs=briefs,
        character_asset=character_asset,
        character_version=character_version,
        location_asset=location_asset,
        location_version=location_version,
    )


class _FakeImageRuntime:
    """A deterministic Universal IMAGE transport with no provider boundary."""

    def __init__(self, outputs: Sequence[bytes]) -> None:
        self._outputs = tuple(bytes(item) for item in outputs)
        self._content_by_id: dict[str, bytes] = {}
        self.requests: list[CapabilityRequest] = []
        self.real_provider_calls = 0
        self.paid_calls = 0

    def submit(
        self,
        request: CapabilityRequest,
        *,
        authorization: object | None = None,
    ) -> CapabilityResult:
        assert request.capability is CapabilityKind.IMAGE
        assert request.create_authorized is True
        assert authorization == {"approved": True, "create_authorized": True}
        index = len(self.requests)
        if index >= len(self._outputs):
            raise AssertionError("fake IMAGE output queue exhausted")
        self.requests.append(request)
        content = self._outputs[index]
        source_id = f"fake-keyframe-{index + 1}"
        self._content_by_id[source_id] = content
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(
                ContentRef(
                    source_kind="FAKE_IMAGE_TRANSPORT",
                    source_id=source_id,
                    role="SHOT_FIRST_FRAME",
                    mime_type="image/png",
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                ),
            ),
            safe_metadata={"fixture": "offline"},
        )

    def read_output(self, output: ContentRef) -> bytes:
        return self._content_by_id[output.source_id]


def _image_binding(runtime: _FakeImageRuntime) -> UniversalImageBinding:
    manifest = next(
        item
        for item in build_mainland_manifests(
            credential_presence={},
            create_authorized=False,
            artifact_sink_available=True,
        )
        if item.capability is CapabilityKind.IMAGE
    )
    return UniversalImageBinding(
        runtime=runtime,
        manifest=manifest,
        read_output=runtime.read_output,
    )


def _compile_keyframe_briefs(
    context: _Context, service: ShotKeyframeService
) -> tuple[object, ...]:
    brief_by_shot = {brief.shot_id: brief for brief in context.briefs}
    return tuple(
        service.briefs.compile(
            context.snapshot,
            shot.id,
            brief_by_shot[shot.id],
        )
        for shot in context.shots
    )


def _generate_frames(
    context: _Context, outputs: Sequence[bytes]
) -> tuple[ShotKeyframeService, _FakeImageRuntime, tuple[ShotFirstFrame, ...]]:
    service = ShotKeyframeService(context.repository)
    runtime = _FakeImageRuntime(outputs)
    binding = _image_binding(runtime)
    briefs = _compile_keyframe_briefs(context, service)
    frames: list[ShotFirstFrame] = []
    previous: Shot | None = None
    for shot, brief in zip(context.shots, briefs, strict=True):
        selection = ShotKeyframePolicy.select(
            shot,
            project_id=context.project.id,
            previous=previous,
            continuous_action=False,
        )
        assert selection.source_type is ShotFirstFrameSourceType.GENERATED_KEYFRAME
        frames.append(
            service.generate_and_record(
                context.project.id,
                context.job.id,
                brief,
                selection,
                binding,
                create_authorized=True,
            )
        )
        previous = shot
    return service, runtime, tuple(frames)


def _single_shot_snapshot(
    context: _Context, frame: ShotFirstFrame
) -> ProductionInputSnapshot:
    parameters = context.snapshot.shot_parameters[frame.shot_id]
    return ProductionInputSnapshot(
        project_id=context.snapshot.project_id,
        story_revision_id=context.snapshot.story_revision_id,
        script_revision_id=context.snapshot.script_revision_id,
        shot_plan_revision_id=context.snapshot.shot_plan_revision_id,
        generation_brief_id=frame.generation_brief_id,
        reference_asset_versions=context.snapshot.reference_asset_versions,
        shot_parameters={frame.shot_id: parameters},
        shot_first_frames=(frame,),
        first_frame_required_shot_ids=(frame.shot_id,),
    )


def test_three_shot_distinct_keyframes_preserve_reference_identity_and_freeze(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    character_before = context.repository.get_reference_asset(
        context.character_asset.id
    )
    version_before = context.repository.get_reference_asset_version(
        context.character_version.id
    )
    service, runtime, frames = _generate_frames(
        context,
        (
            png_bytes(color="red"),
            png_bytes(color="green"),
            png_bytes(color="blue"),
        ),
    )

    assert runtime.real_provider_calls == 0
    assert runtime.paid_calls == 0
    assert len(runtime.requests) == 3
    assert all(
        request.structured_input["contract"] == "SHOT_KEYFRAME_BRIEF_V1"
        for request in runtime.requests
    )
    assert all(request.inputs == () for request in runtime.requests)
    assert all(
        request.structured_input["reference_conditioning_mode"]
        == "TEXTUAL_PROVENANCE_ONLY"
        for request in runtime.requests
    )
    assert [frame.source_type for frame in frames] == [
        ShotFirstFrameSourceType.GENERATED_KEYFRAME
    ] * 3
    assert len({frame.sha256 for frame in frames}) == 3
    assert {
        frame.identity_reference_provenance[0].asset_version_id for frame in frames
    } == {context.character_version.id}
    assert all(
        frame.identity_reference_provenance[0].asset_type
        is ReferenceAssetType.CHARACTER_REFERENCE
        for frame in frames
    )
    assert all(
        frame.location_reference_provenance[0].asset_type
        is ReferenceAssetType.LOCATION_REFERENCE
        for frame in frames
    )
    report = service.validate_pre_live(context.project.id, context.job.id)
    assert report.gate is PreLiveFirstFrameGate.PASS
    assert report.unintended_duplicate_first_frame_count == 0

    frozen = service.freeze_snapshot(
        context.snapshot,
        frames,
        required_shot_ids=[shot.id for shot in context.shots],
    )
    round_trip = ProductionInputSnapshot.model_validate_json(
        frozen.model_dump_json()
    )
    assert round_trip == frozen
    assert tuple(
        round_trip.first_frame_for_shot(shot.id).sha256
        for shot in context.shots
    ) == tuple(frame.sha256 for frame in frames)
    with pytest.raises(ValidationError):
        frames[0].sha256 = "0" * 64

    reloaded = ProjectRepository(context.repository.paths)
    assert ShotKeyframeService(reloaded).selected_first_frames(
        context.project.id, context.job.id
    ) == {frame.shot_id: frame for frame in frames}
    assert context.repository.get_reference_asset(
        context.character_asset.id
    ) == character_before
    assert context.repository.get_reference_asset_version(
        context.character_version.id
    ) == version_before


def test_duplicate_character_image_as_literal_first_frame_blocks_pre_live(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    character_path = (
        context.repository.paths.projects
        / context.project.id
        / context.character_version.storage_path
    )
    historical_reference_bytes = character_path.read_bytes()
    service, runtime, frames = _generate_frames(
        context, (historical_reference_bytes,) * 3
    )

    assert runtime.real_provider_calls == runtime.paid_calls == 0
    assert len({frame.sha256 for frame in frames}) == 1
    report = service.validate_pre_live(context.project.id, context.job.id)
    assert report.gate is PreLiveFirstFrameGate.BLOCKED
    assert report.unintended_duplicate_first_frame_count >= 1
    assert len(report.duplicate_groups) == 1
    assert report.duplicate_groups[0].blocking is True
    assert report.duplicate_groups[0].shot_ids == tuple(
        shot.id for shot in context.shots
    )
    assert context.repository.get_reference_asset_version(
        context.character_version.id
    ).sha256 == hashlib.sha256(historical_reference_bytes).hexdigest()


def test_previous_approved_last_frame_is_exact_and_new_composition_is_generated(
    tmp_path: Path,
) -> None:
    compatible = (
        (
            ShotSize.MEDIUM,
            CameraAngle.EYE_LEVEL,
            CameraMovement.TRACK,
            "continuous medium tracking composition",
            "The hero starts crossing",
        ),
        (
            ShotSize.MEDIUM,
            CameraAngle.EYE_LEVEL,
            CameraMovement.TRACK,
            "continuous medium tracking composition",
            "The hero continues crossing",
        ),
        (
            ShotSize.CLOSE_UP,
            CameraAngle.LOW_ANGLE,
            CameraMovement.PUSH_IN,
            "new close-up low-angle composition",
            "The hero sees the signal",
        ),
    )
    context = _build_context(tmp_path, specifications=compatible)
    service = ShotKeyframeService(context.repository)
    keyframe_briefs = _compile_keyframe_briefs(context, service)
    brief_by_shot = {
        brief.shot_id: brief for brief in keyframe_briefs
    }

    production_shots = {
        item.shot_id: item
        for item in context.repository.list_production_shots(context.job.id)
    }
    source_bytes = mp4_bytes(
        source="testsrc2=size=160x120:rate=12:duration=1,hue=h=t*90"
    )
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    source_execution = ProductionExecution(
        id=uuid4().hex,
        production_job_id=context.job.id,
        status=ProductionExecutionStatus.SUCCEEDED,
        worker_type="OFFLINE_SYNTHETIC_VIDEO",
        started_at=_NOW,
        finished_at=_NOW,
        created_at=_NOW,
        input_snapshot=ProductionInputSnapshot(
            project_id=context.snapshot.project_id,
            story_revision_id=context.snapshot.story_revision_id,
            script_revision_id=context.snapshot.script_revision_id,
            shot_plan_revision_id=context.snapshot.shot_plan_revision_id,
            generation_brief_id=context.briefs[0].id,
            reference_asset_versions=context.snapshot.reference_asset_versions,
            shot_parameters={
                context.shots[0].id: context.snapshot.shot_parameters[
                    context.shots[0].id
                ]
            },
        ),
        generation_brief_id=context.briefs[0].id,
    )
    context.repository.create_production_execution(source_execution)
    relative_path = f"production/{source_execution.id}/shot-001.mp4"
    target = (
        context.repository.paths.projects / context.project.id / relative_path
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source_bytes)
    artifact = context.repository.create_production_artifact(
        ProductionArtifact(
            id=uuid4().hex,
            execution_id=source_execution.id,
            artifact_type="video",
            path=relative_path,
            metadata_json={
                "mime_type": "video/mp4",
                "sha256": source_sha,
                "size_bytes": len(source_bytes),
                "duration_seconds": 1.0,
                "resolution": {"width": 160, "height": 120},
                "codec": "h264",
                "audio_stream": False,
            },
            created_at=_NOW,
        )
    )
    qc = context.repository.create_production_qc_result(
        ProductionQCResult(
            id=uuid4().hex,
            project_id=context.project.id,
            execution_id=source_execution.id,
            artifact_id=artifact.id,
            status=ProductionQCStatus.QC_PASS,
            created_at=_NOW,
        )
    )
    review = context.repository.create_production_review(
        ProductionReview(
            id=uuid4().hex,
            project_id=context.project.id,
            qc_result_id=qc.id,
            decision=ProductionReviewDecision.APPROVED,
            reviewer="offline-human-fixture",
            created_at=_NOW,
        )
    )
    decision = FinalAssemblyService(context.repository).select_shot_source(
        context.project.id,
        context.job.id,
        production_shots[context.shots[0].id].id,
        production_execution_id=source_execution.id,
        production_artifact_id=artifact.id,
        selected_by="offline-human-fixture",
    )
    assert decision.review_id == review.id

    shot_two_selection = ShotKeyframePolicy.select(
        context.shots[1],
        project_id=context.project.id,
        previous=context.shots[0],
        continuous_action=True,
        previous_reuse_authorization_id="continuity-approval-001",
    )
    assert (
        shot_two_selection.policy
        is ShotKeyframeSelectionPolicy.CONTINUOUS_ACTION_COMPATIBLE_COMPOSITION
    )
    ffmpeg_binary = Path(__import__("imageio_ffmpeg").get_ffmpeg_exe())
    frame_two = service.record_previous_shot_last_frame(
        context.project.id,
        context.job.id,
        brief_by_shot[context.shots[1].id],
        shot_two_selection,
        source_decision_id=decision.id,
        ffmpeg_binary=ffmpeg_binary,
    )
    independent_frame = tmp_path / "independent-last-frame.png"
    completed = subprocess.run(
        [
            str(ffmpeg_binary),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(target),
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
            str(independent_frame),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    expected_last_frame_sha = hashlib.sha256(
        independent_frame.read_bytes()
    ).hexdigest()
    assert frame_two.source_type is ShotFirstFrameSourceType.PREVIOUS_SHOT_LAST_FRAME
    assert frame_two.sha256 == expected_last_frame_sha
    assert frame_two.previous_shot_provenance is not None
    assert frame_two.previous_shot_provenance.source_artifact_id == artifact.id
    assert frame_two.previous_shot_provenance.source_execution_id == source_execution.id
    assert frame_two.previous_shot_provenance.approval_source_id == decision.id
    assert frame_two.previous_shot_provenance.source_artifact_sha256 == source_sha

    shot_three_selection = ShotKeyframePolicy.select(
        context.shots[2],
        project_id=context.project.id,
        previous=context.shots[1],
        continuous_action=True,
        previous_reuse_authorization_id="should-not-be-used",
    )
    assert shot_three_selection.policy is ShotKeyframeSelectionPolicy.NEW_COMPOSITION
    assert (
        shot_three_selection.source_type
        is ShotFirstFrameSourceType.GENERATED_KEYFRAME
    )
    assert shot_three_selection.previous_shot_id is None
    assert shot_three_selection.literal_reuse_authorization_id is None


def test_wan_uses_exact_frozen_first_frame_and_never_falls_back_to_reference(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    service, _runtime, frames = _generate_frames(
        context,
        (png_bytes(color="blue"), png_bytes(color="green"), png_bytes(color="red")),
    )
    frame = frames[0]
    snapshot = _single_shot_snapshot(context, frame)
    resolver = WanFirstFrameResolver(
        ShotFirstFrameArtifactResolver(context.repository)
    )
    exact_first_frame = resolver.resolve(snapshot)
    payload, metadata = WanInputMapper.map_snapshot(
        snapshot, WanProviderConfig(), exact_first_frame
    )
    data_uri = payload["input"]["media"][0]["url"]
    encoded = data_uri.split(",", 1)[1]
    sent_bytes = base64.b64decode(encoded)
    assert hashlib.sha256(sent_bytes).hexdigest() == frame.sha256
    assert metadata["first_frame_sha256"] == frame.sha256
    assert metadata["first_frame_source_type"] == frame.source_type.value
    assert metadata["first_frame_artifact_id"] == frame.artifact_id
    assert metadata["first_frame_id"] == frame.id
    assert metadata["identity_reference_version_ids"] == [
        context.character_version.id
    ]
    serialized_metadata = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    assert str(context.repository.paths.root) not in serialized_metadata
    assert "DASHSCOPE_API_KEY" not in serialized_metadata
    assert "Authorization" not in serialized_metadata
    assert "X-Amz-Signature" not in serialized_metadata
    assert "https://" not in serialized_metadata
    assert "reference_asset_version_id" not in metadata

    missing = ProductionInputSnapshot(
        project_id=snapshot.project_id,
        story_revision_id=snapshot.story_revision_id,
        script_revision_id=snapshot.script_revision_id,
        shot_plan_revision_id=snapshot.shot_plan_revision_id,
        generation_brief_id=frame.generation_brief_id,
        reference_asset_versions=snapshot.reference_asset_versions,
        shot_parameters=snapshot.shot_parameters,
        first_frame_required_shot_ids=(frame.shot_id,),
    )
    with pytest.raises(WanAdapterError, match="Required frozen Shot First Frame is missing"):
        resolver.resolve(missing)

    assert service.resolver.resolve(snapshot).first_frame == frame


def test_contract_rejects_secret_url_and_absolute_path_and_repair_never_executes(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    service, runtime, frames = _generate_frames(
        context,
        (png_bytes(color="red"), png_bytes(color="green"), png_bytes(color="blue")),
    )
    persisted = context.repository.get_production_artifact(frames[0].artifact_id)
    assert persisted is not None
    persisted_json = json.dumps(
        persisted.metadata_json, ensure_ascii=False, sort_keys=True
    )
    assert str(context.repository.paths.root) not in persisted_json
    assert "https://" not in persisted_json
    assert "Bearer " not in persisted_json
    assert "api_key" not in persisted_json.casefold()

    identity = frames[0].identity_reference_provenance[0]
    unsafe = identity.model_dump(mode="json")
    unsafe["stable_description"] = "https://private.example/frame.png?X-Amz-Signature=secret"
    with pytest.raises(
        ValidationError,
        match="cannot contain URLs, absolute paths, signed URLs, or plaintext credentials",
    ):
        type(identity).model_validate(unsafe)
    unsafe["stable_description"] = str(tmp_path / "private" / "frame.png")
    with pytest.raises(ValidationError, match="cannot contain URLs"):
        type(identity).model_validate(unsafe)
    unsafe["stable_description"] = "api_key=plaintext-secret"
    with pytest.raises(ValidationError, match="plaintext credentials"):
        type(identity).model_validate(unsafe)

    calls_before = len(runtime.requests)
    recommendations = service.repair_recommendations(
        context.project.id,
        [context.shots[0].id],
        "keyframe continuity failed",
    )
    assert len(runtime.requests) == calls_before
    assert all(item.auto_execute is False for item in recommendations)
    assert all(item.requires_human_confirmation is True for item in recommendations)
    assert {item.action.value for item in recommendations} == {
        "REGENERATE_KEYFRAME",
        "REPLAN_SHOT",
        "HUMAN_DECISION",
    }
    assert runtime.real_provider_calls == runtime.paid_calls == 0


def test_default_repository_is_forced_to_temp_data_and_survives_cold_reload(
    tmp_path: Path,
) -> None:
    expected_root = (tmp_path / "isolated-default-data").resolve()
    repository = ProjectRepository()
    assert repository.paths.root == expected_root
    assert repository.paths.database.is_file()
    assert str(repository.paths.database).startswith(str(tmp_path.resolve()))
    reopened = ProjectRepository()
    assert reopened.paths == repository.paths
    assert reopened.list_projects() == []


def test_universal_image_create_requires_explicit_authorization_without_call(
    tmp_path: Path,
) -> None:
    context = _build_context(tmp_path)
    service = ShotKeyframeService(context.repository)
    brief = _compile_keyframe_briefs(context, service)[0]
    selection = ShotKeyframePolicy.select(
        context.shots[0], project_id=context.project.id
    )
    runtime = _FakeImageRuntime((png_bytes(color="red"),))
    with pytest.raises(
        ShotKeyframeError,
        match="explicit authorization; no call was made",
    ):
        service.generate_and_record(
            context.project.id,
            context.job.id,
            brief,
            selection,
            _image_binding(runtime),
            create_authorized=False,
        )
    assert runtime.requests == []
    assert runtime.real_provider_calls == runtime.paid_calls == 0
