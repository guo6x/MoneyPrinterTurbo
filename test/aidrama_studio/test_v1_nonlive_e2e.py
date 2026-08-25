from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from aidrama_studio.domain import (
    Character,
    Location,
    ProductionReviewDecision,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    Shot,
    ShotPlan,
    StoryBeat,
    StoryBible,
    StructuredScript,
    World,
)
from aidrama_studio.services import (
    CreativeIntakeService,
    FFmpegPostProductionAdapter,
    FinalAssemblyRuntimeService,
    FinalAssemblyService,
    PostProductionService,
    ProductionExecutionService,
    ProductionQCService,
    ProductionService,
    ProductionWorker,
    ProjectArchiveService,
    ProjectService,
    ScriptService,
    ShotService,
    StoryService,
)
from aidrama_studio.services.adapters import MPTFinalAssemblyAdapter, MockProductionAdapter
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.image_fixtures import jpeg_bytes, png_bytes
from test.aidrama_studio.video_fixtures import mp4_bytes


def _paths(root: Path) -> DatabasePaths:
    return DatabasePaths(
        root / "aidrama.db",
        root / "projects",
        root / "archived",
    )


def _docx_bytes(text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="urn:test"><w:t>{text}</w:t></w:document>',
        )
    return output.getvalue()


def _story() -> StoryBible:
    return StoryBible(
        title="Last Bus",
        logline="A driver meets a passenger from tomorrow.",
        premise="The final route reveals a forgotten choice.",
        genre="Mystery",
        tone="Restrained",
        world=World(era="Present day", setting="A rain-soaked city"),
        characters=[Character(id="char_001", name="Lin")],
        locations=[Location(id="loc_001", name="Last bus")],
        story_beats=[
            StoryBeat(
                id="beat_001",
                order=1,
                type="OPENING",
                summary="The last passenger boards.",
                characters=["char_001"],
                location_id="loc_001",
            ),
            StoryBeat(
                id="beat_002",
                order=2,
                type="TURNING_POINT",
                summary="The passenger knows tomorrow's accident.",
                characters=["char_001"],
                location_id="loc_001",
            ),
            StoryBeat(
                id="beat_003",
                order=3,
                type="ENDING",
                summary="The driver chooses another road.",
                characters=["char_001"],
                location_id="loc_001",
            ),
        ],
    )


def _script() -> StructuredScript:
    return StructuredScript(
        title="Last Bus",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="Inside the bus",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=1,
                beats=[
                    ScriptBeat(
                        id="script_beat_001",
                        order=1,
                        type=ScriptBeatType.DIALOGUE,
                        character_id="char_001",
                        text="This is the last stop.",
                        estimated_duration_seconds=1,
                    )
                ],
            )
        ],
    )


def _shot_plan() -> ShotPlan:
    return ShotPlan(
        title="Last Bus shot plan",
        source_script_revision_id="placeholder-replaced-by-service-context",
        shots=[
            Shot(
                id="shot_001",
                order=1,
                scene_id="scene_001",
                source_script_beat_ids=["script_beat_001"],
                duration_seconds=1,
                subject=["char_001"],
                action="Lin watches the empty road.",
                dialogue_or_narration="This is the last stop.",
                visual_intent="A slow push toward the driver.",
            )
        ],
    )


class _CanonicalFakeLLMGateway:
    """Only the external model boundary is fake; all persistence is real."""

    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def readiness(self, _project_id: str):
        return True, "configured test boundary"

    def generate_validated_json(self, _project_id, _prompt, *, operation, input_source_ids=(), **_kwargs):
        self.calls.append((operation, tuple(input_source_ids)))
        if operation == "STORY_BIBLE_GENERATION":
            return _story()
        if operation == "STRUCTURED_SCRIPT_GENERATION":
            return _script()
        if operation == "SHOT_PLAN_GENERATION":
            return _shot_plan()
        raise AssertionError(f"unexpected operation: {operation}")


class _RealLocalShotAdapter(MockProductionAdapter):
    """Return a real local MP4 while retaining a mock external provider."""

    name = "nonlive-real-local-media"

    def __init__(self, payload: bytes):
        super().__init__()
        self.payload = payload
        self.submit_count = 0

    def submit(self, snapshot):
        self.submit_count += 1
        submission = super().submit(snapshot)
        shot_id = next(iter(snapshot.shot_parameters))
        self.progress(submission.runtime_reference, 50)
        self.shot_completed(submission.runtime_reference, shot_id)
        self.succeed(
            submission.runtime_reference,
            artifacts=[
                {
                    "artifact_type": "video",
                    "filename": "shot.mp4",
                    "content": self.payload,
                    "metadata": {
                        "mime_type": "video/mp4",
                        "audio_required": False,
                        "runtime": self.name,
                    },
                }
            ],
        )
        return submission


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _ffmpeg_available() -> bool:
    try:
        return Path(_ffmpeg()).is_file()
    except (ImportError, OSError):
        return False


@pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="existing local ffmpeg runtime unavailable",
)
def test_v1_nonlive_mixed_intake_to_portable_final_mp4_and_cold_resume(tmp_path: Path):
    source_root = tmp_path / "source-data"
    repository = ProjectRepository(_paths(source_root))
    project = ProjectService(repository).create(
        "V1 non-live journey",
        description="One-line idea plus an existing planning pack",
    )

    # Creative Intake: one-line text + multiple documents + multiple images.
    intake = CreativeIntakeService(repository)
    idea = intake.source_pack.import_text(
        project.id,
        "A night-bus driver meets a passenger from tomorrow.",
    )
    outline = intake.source_pack.import_bytes(
        project.id,
        "outline.md",
        b"# Outline\nCharacter, scene, dialogue and shot notes.",
        mime_type="text/markdown",
    )
    screenplay = intake.source_pack.import_bytes(
        project.id,
        "screenplay.docx",
        _docx_bytes("Scene dialogue and final shot"),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    hero_image = intake.source_pack.import_bytes(
        project.id,
        "hero.png",
        png_bytes(color="red"),
        mime_type="image/png",
    )
    location_image = intake.source_pack.import_bytes(
        project.id,
        "bus.jpg",
        jpeg_bytes(color="blue"),
        mime_type="image/jpeg",
    )
    source_ids = tuple(
        item.id for item in (idea, outline, screenplay, hero_image, location_image)
    )
    normalized = intake.normalize(project.id, source_ids=source_ids)

    # The canonical LLM gateway is the only mocked planning boundary.
    gateway = _CanonicalFakeLLMGateway()
    story_service = StoryService(repository, llm_gateway=gateway)
    story = story_service.generate_story_bible(
        project,
        brief=normalized.premise,
        genre="Mystery",
        tone="Restrained",
        source_ids=source_ids,
        normalized_brief_id=normalized.id,
    )
    story = story_service.approve_revision(story["id"])
    script_service = ScriptService(repository, llm_gateway=gateway)
    script = script_service.approve_revision(
        script_service.generate_script(project)["id"]
    )
    shot_service = ShotService(repository, llm_gateway=gateway)
    generated_plan = shot_service.generate_shot_plan(project)
    # The fake boundary cannot know the persisted script UUID; canonical
    # validation uses the service's source revision, so normalize the model's
    # informational field before approval.
    generated_plan["content"].source_script_revision_id = script["id"]
    generated_plan = shot_service.save_draft(
        project.id,
        generated_plan["content"],
        revision_id=generated_plan["id"],
        generation_input=generated_plan["generation_input"],
    )
    shot_revision = shot_service.approve_revision(generated_plan["id"])

    assert [item[0] for item in gateway.calls] == [
        "STORY_BIBLE_GENERATION",
        "STRUCTURED_SCRIPT_GENERATION",
        "SHOT_PLAN_GENERATION",
    ]
    assert gateway.calls[0][1] == source_ids
    assert story["generation_input"]["normalized_brief_id"] == normalized.id

    # Promote both source images through the atomic reference boundary.
    character = intake.promote_image_reference(
        project.id,
        hero_image.id,
        source_story_revision_id=story["id"],
        binding_type="CHARACTER",
        binding_id="char_001",
        lock=True,
    )
    location = intake.promote_image_reference(
        project.id,
        location_image.id,
        source_story_revision_id=story["id"],
        binding_type="LOCATION",
        binding_id="loc_001",
        lock=True,
    )
    assert character["version"].metadata["source_pack_item_id"] == hero_image.id
    assert location["version"].metadata["source_pack_item_id"] == location_image.id

    # Real local MP4 production through a mock external runtime boundary.
    production = ProductionService(repository)
    assert production.validate_job_readiness(project.id, shot_revision["id"])["ready"]
    job = production.create_production_job(project.id, shot_revision["id"])
    production_shots = production.create_production_shots(project.id, job.id)
    assert len(production_shots) == 1
    execution_service = ProductionExecutionService(repository)
    execution = execution_service.enqueue_job(project.id, job.id, worker_type="nonlive")
    adapter = _RealLocalShotAdapter(
        mp4_bytes(source="testsrc=size=160x120:rate=25:d=2", audio=False)
    )
    execution = ProductionWorker(execution_service, adapter).run(project.id, execution.id)
    artifacts = execution_service.list_artifacts(project.id, execution.id)
    assert execution.status.value == "SUCCEEDED"
    assert adapter.submit_count == 1
    assert len(artifacts) == 1

    # Deterministic physical-media QC and append-only human approval.
    qc_service = ProductionQCService(repository)
    qc = qc_service.run_qc(project.id, execution.id, artifacts[0].id)
    assert qc.status.value == "QC_PASS"
    review = qc_service.create_review(
        project.id,
        qc.id,
        ProductionReviewDecision.APPROVED,
        reviewer="nonlive-e2e",
    )
    assert review.decision is ProductionReviewDecision.APPROVED

    # Immutable FinalAssembly plus real FFmpeg subtitle/BGM post render.
    ffmpeg = _ffmpeg()
    assembly_service = FinalAssemblyService(repository)
    assembly = assembly_service.create_assembly(project.id, job.id, freeze=True)
    assembly_runtime = FinalAssemblyRuntimeService(
        repository,
        manifest_service=assembly_service,
        adapter=MPTFinalAssemblyAdapter(
            project_root=repository.paths.projects / project.id,
            ffmpeg_binary=ffmpeg,
        ),
    )
    final_attempt = assembly_runtime.render(project.id, assembly.id)
    assert final_attempt.status.value == "SUCCEEDED"

    post = PostProductionService(
        repository,
        media_adapter=FFmpegPostProductionAdapter(ffmpeg_binary=ffmpeg),
        final_assembly_service=assembly_runtime,
    )
    post_plan = post.create_plan(project.id, assembly.id)
    subtitle = post.build_subtitle_timeline(
        project.id,
        script["id"],
        plan_id=post_plan.id,
        shot_plan_revision_id=shot_revision["id"],
    )
    bgm_source = repository.paths.projects / project.id / "source-bgm.wav"
    bgm_result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.2",
            "-c:a",
            "pcm_s16le",
            str(bgm_source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bgm_result.returncode == 0, bgm_result.stderr[-1000:]
    music = post.import_bgm(project.id, post_plan.id, bgm_source)
    post_attempt = post.render(
        project.id,
        post_plan.id,
        subtitle_track_id=subtitle.id,
        music_track_id=music.id,
    )
    final_output = post.resolve_output_path(project.id, post_plan.id, post_attempt.id)
    assert final_output is not None and final_output.stat().st_size > 0
    final_sha = hashlib.sha256(final_output.read_bytes()).hexdigest()
    assert post_attempt.metadata_json["sha256"] == final_sha
    assert post_attempt.metadata_json["probe"]["audio_stream"] is True

    # Cold reload reconstructs durable state without another provider submit.
    reloaded = ProjectRepository(repository.paths)
    assert reloaded.get_project(project.id) is not None
    assert len(reloaded.list_source_pack_items(project.id)) == len(source_ids)
    assert len(reloaded.list_production_executions(job.id)) == 1
    assert len(reloaded.list_production_qc_results(project.id, execution.id)) == 1
    assert adapter.submit_count == 1

    # Export into a clean data directory, restore, and verify identities/files.
    archive = ProjectArchiveService(reloaded).export_project(
        project.id,
        tmp_path / "completed-project.aidrama",
    )
    restored_repository = ProjectRepository(_paths(tmp_path / "restored-data"))
    restored_id = ProjectArchiveService(restored_repository).import_project(archive)
    assert restored_id == project.id
    assert len(restored_repository.list_source_pack_items(project.id)) == len(source_ids)
    assert restored_repository.get_normalized_creative_brief(normalized.id) is not None
    assert restored_repository.get_story_revision(story["id"])["status"].value == "APPROVED"
    assert restored_repository.get_script_revision(script["id"])["status"].value == "APPROVED"
    assert restored_repository.get_shot_revision(shot_revision["id"])["status"].value == "APPROVED"
    assert len(restored_repository.list_reference_assets(project.id)) == 2
    assert restored_repository.get_production_execution(execution.id).status.value == "SUCCEEDED"
    assert restored_repository.get_production_qc_result(qc.id).status.value == "QC_PASS"
    assert restored_repository.get_final_assembly(assembly.id).status.value == "SUCCEEDED"
    restored_post = restored_repository.get_post_render_attempt(post_attempt.id)
    restored_output = (
        restored_repository.paths.projects
        / project.id
        / restored_post.output_relative_path
    )
    assert restored_output.is_file()
    assert hashlib.sha256(restored_output.read_bytes()).hexdigest() == final_sha
