"""Offline canonical-service acceptance for the 雨夜来信 60-second fixture.

This test intentionally stops at the provider/media boundary.  Creative
revisions, references, production snapshots, QC, and the final assembly
manifest are all persisted and projected through the canonical services.  The
only media used below is deterministic local ``testsrc`` video; no runtime
adapter is submitted and no provider/paid request is made.
"""

from __future__ import annotations

import hashlib
import gc
import io
import json
import re
import shutil
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aidrama_studio.domain import (
    AspectRatio,
    ExtractionState,
    FinalAssemblyStatus,
    ProductionExecutionStatus,
    ProductionJobStatus,
    ProductionQCMetricStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotStatus,
    ProjectStatus,
    ReferenceBindingType,
    ScriptRevisionStatus,
    ShotPlan,
    ShotRevisionStatus,
    SourceKind,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    SubtitleCue,
    SubtitleTrack,
)
from aidrama_studio.domain.production_snapshot import ProductionInputSnapshot
from aidrama_studio.services import (
    CreativeIntakeService,
    CurrentProductionStateService,
    DependencyStatusService,
    FinalAssemblyService,
    GenerationBriefService,
    PostProductionService,
    ProductionExecutionService,
    ProductionQCService,
    ProductionQueueError,
    ProductionQueueService,
    ProductionService,
    ProjectService,
    ReferenceAssetService,
    ScriptService,
    ScriptServiceError,
    ShotService,
    ShotServiceError,
    StoryService,
    StoryServiceError,
)
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    TTSProvider,
)
from aidrama_studio.services.llm_runtime import LLMInvocationError
from aidrama_studio.services.provider_profiles import ProviderProfileService
from aidrama_studio.services.runtime_foundation import OutputProfileService
from aidrama_studio.services.tts_runtime import TTSRuntimeError, TTSRuntimeService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


FIXTURE_DIR = Path(__file__).parent
EXPECTED_SHOT_IDS = [f"shot_{index:02d}" for index in range(1, 13)]
EXPECTED_SHOT_DURATIONS = [5, 5, 4, 6, 5, 6, 5, 6, 4, 5, 5, 4]
TARGET_DURATION = 60.0
EPSILON = 1e-6


def _load_json(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_srt(path: Path) -> list[dict[str, object]]:
    timing = re.compile(
        r"^(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})"
        r"\s+-->\s+"
        r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
    )

    def timestamp(match: re.Match[str], prefix: str) -> float:
        return (
            int(match[f"{prefix}h"]) * 3600
            + int(match[f"{prefix}m"]) * 60
            + int(match[f"{prefix}s"])
            + int(match[f"{prefix}ms"]) / 1000
        )

    cues: list[dict[str, object]] = []
    blocks = [
        block
        for block in path.read_text(encoding="utf-8").strip().split("\n\n")
        if block.strip()
    ]
    for block in blocks:
        lines = block.splitlines()
        assert len(lines) >= 3
        match = timing.fullmatch(lines[1])
        assert match, f"invalid SRT timing: {lines[1]}"
        cues.append(
            {
                "index": int(lines[0]),
                "start": timestamp(match, "s"),
                "end": timestamp(match, "e"),
                "text": "\n".join(lines[2:]),
            }
        )
    return cues


def _reference_png(color: tuple[int, int, int]) -> bytes:
    """Create a policy-sized, deterministic local reference image."""

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1024, 576), color=color).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class _OfflineLLMGateway:
    """A fail-closed provider seam used to prove generation is not reached."""

    def __init__(self) -> None:
        self.readiness_calls = 0
        self.generation_calls = 0

    def readiness(self, _project_id: str) -> tuple[bool, str]:
        self.readiness_calls += 1
        return False, "offline acceptance harness: provider invocation forbidden"

    def generate_validated_json(self, *_args: object, **_kwargs: object) -> object:
        self.generation_calls += 1
        raise LLMInvocationError(
            "offline acceptance harness: provider invocation forbidden"
        )


class _OfflineTTSProvider(TTSProvider):
    """Unavailable canonical TTS boundary with a zero-call counter."""

    provider_name = "OFFLINE_ACCEPTANCE_TTS"

    def __init__(self) -> None:
        self.synthesis_calls = 0

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            CapabilityKind.TTS,
            self.provider_name,
            False,
            "offline acceptance harness: TTS provider forbidden",
            {
                "model": "offline-boundary",
                "deployment_region": "LOCAL",
                "endpoint_class": "OFFLINE_ACCEPTANCE",
                "endpoint_profile_id": (
                    "runtime:TTS:OFFLINE_ACCEPTANCE_TTS:OFFLINE_ACCEPTANCE"
                ),
            },
            configured=False,
        )

    def synthesize(
        self,
        _text: str,
        *,
        voice: str,
        language: str = "zh-CN",
        sample_rate: int = 48000,
    ) -> object:
        self.synthesis_calls += 1
        raise AssertionError(
            "offline acceptance harness reached TTS provider synthesis"
        )


def test_canonical_service_path_ingests_fixture_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingest and project every safe canonical layer without provider I/O."""

    started = time.perf_counter()
    data_parent = Path(tempfile.mkdtemp(prefix="aidrama-offline-canonical-"))
    data_root = data_parent / "aidrama-data"
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(data_root))
    # Rollback journaling keeps cleanup deterministic on Windows, while still
    # exercising the same canonical SQLite schema and repository APIs.
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")

    network_attempts: list[tuple[object, ...]] = []

    def deny_network(*args: object, **_kwargs: object) -> None:
        network_attempts.append(args)
        raise AssertionError("offline acceptance harness attempted network I/O")

    # SQLite, Pillow, and local FFmpeg do not need sockets.  If a provider or
    # remote preflight is accidentally reached, fail at the first connect.
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    provider_submit_attempts: list[tuple[object, ...]] = []

    def deny_provider_submit(*args: object, **_kwargs: object) -> None:
        provider_submit_attempts.append(args)
        raise AssertionError("offline acceptance harness reached provider submit")

    monkeypatch.setattr(
        ProductionExecutionService, "submit_execution", deny_provider_submit
    )

    try:
        paths = DatabasePaths(
            database=data_root / "aidrama.db",
            projects=data_root / "projects",
            archived_projects=data_root / "archived_projects",
        )
        repository = ProjectRepository(paths)
        assert repository.paths.root == data_root.resolve()

        creative = _load_json("creative_brief.json")
        story_data = _load_json("story_bible.json")
        script_data = _load_json("structured_script.json")
        shot_data = _load_json("shot_plan.json")
        reference_data = _load_json("reference_requirements.json")
        tts_data = _load_json("tts_cues.json")
        assembly_data = _load_json("final_assembly.json")
        profile_data = _load_json("output_profile.json")

        # Project and output profile are created by the real ProjectService.
        project = ProjectService(repository).create(
            title=creative["title"],
            description=creative["logline"],
            aspect_ratio=AspectRatio(creative["aspect_ratio"]),
            target_duration_seconds=creative["target_duration_seconds"],
            delivery_resolution_label=creative["delivery"]["label"],
            target_fps=creative["fps"],
            quality_mode="FINAL",
        )
        project_directory = repository.project_directory(project.id).resolve()
        assert project_directory.is_dir()
        assert project_directory.parent == paths.projects.resolve()

        profile = repository.get_current_output_profile(project.id)
        assert profile is not None
        assert (profile.delivery_width, profile.delivery_height) == (
            creative["delivery"]["width"],
            creative["delivery"]["height"],
        )
        assert profile.target_fps == creative["fps"]
        assert profile.target_video_codec == "h264"
        assert profile.target_audio_sample_rate == 48000
        assert profile.target_audio_channels == 2
        assert OutputProfileService.dimensions_for(
            creative["native_generation"]["label"], creative["aspect_ratio"]
        ) == (creative["native_generation"]["width"], creative["native_generation"]["height"])
        assert profile_data["native_generation"] == {
            "width": creative["native_generation"]["width"],
            "height": creative["native_generation"]["height"],
            "fps": creative["fps"],
            "aspect_ratio": creative["aspect_ratio"],
        }
        assert profile_data["delivery"]["width"] == profile.delivery_width
        assert profile_data["delivery"]["height"] == profile.delivery_height
        assert profile_data["delivery"]["fps"] == profile.target_fps

        # Creative/source intake is durable and local, including deterministic
        # normalization and source-path integrity checks.
        intake = CreativeIntakeService(repository)
        source_text = json.dumps(creative, ensure_ascii=False, sort_keys=True)
        source = intake.source_pack.import_text(
            project.id,
            source_text,
            filename="creative_brief.txt",
            metadata={"fixture_id": creative["fixture_id"]},
        )
        assert source.extraction_state is ExtractionState.EXTRACTED
        assert source.extracted_text == source_text
        assert intake.source_pack.resolve_path(project.id, source.id).is_file()
        analysis = intake.analyzer.analyze(project.id, source.id)
        assert analysis.project_id == project.id and analysis.source_id == source.id
        assert analysis.classifications
        normalized = intake.normalize(
            project.id,
            source_ids=(source.id,),
            overrides={
                "title_candidate": creative["title"],
                "premise": creative["logline"],
                "genre": creative["genre"],
                "tone": creative["tone"],
                "themes": tuple(creative["creative_pillars"]),
                "constraints": tuple(creative["hard_constraints"]),
            },
        )
        normalized_reload = repository.get_normalized_creative_brief(normalized.id)
        assert normalized_reload is not None
        assert normalized_reload.source_ids == (source.id,)
        assert normalized_reload.title_candidate == creative["title"]

        # Every AI-dependent service is given the same fail-closed gateway.
        # Calling its generation entry points must stop before any provider or
        # invocation ledger is touched.
        offline_gateway = _OfflineLLMGateway()
        story_service = StoryService(repository, llm_gateway=offline_gateway)
        script_service = ScriptService(repository, llm_gateway=offline_gateway)
        shot_service = ShotService(repository, llm_gateway=offline_gateway)
        assert story_service.llm_readiness(project.id)[0] is False
        assert script_service.llm_readiness(project.id)[0] is False
        assert shot_service.llm_readiness(project.id)[0] is False
        assert offline_gateway.readiness_calls == 3
        with pytest.raises(StoryServiceError, match="provider invocation forbidden"):
            story_service.generate_story_bible(
                project,
                brief=creative["logline"],
                genre=creative["genre"],
                tone=creative["tone"],
                source_ids=(source.id,),
                normalized_brief_id=normalized.id,
            )

        # Story Bible: validate, persist as a Draft, approve, and cold-load.
        story = StoryBible.model_validate(story_data)
        story_revision = story_service.save_draft(
            project.id,
            story,
            generation_input={"fixture_id": creative["fixture_id"], "offline": True},
        )
        assert story_revision["status"] is StoryRevisionStatus.DRAFT
        story_revision = story_service.approve_revision(story_revision["id"])
        assert story_revision["status"] is StoryRevisionStatus.APPROVED
        story_loaded = repository.get_story_revision(story_revision["id"])
        assert story_loaded is not None
        assert isinstance(story_loaded["content"], StoryBible)
        assert len(story_loaded["content"].characters) == 2
        assert len(story_loaded["content"].locations) == 2

        # Structured Script: use the canonical story-pinned revision creator.
        script = StructuredScript.model_validate(script_data)
        script.validate_against(story)
        assert script.total_estimated_duration_seconds == pytest.approx(TARGET_DURATION)
        with pytest.raises(ScriptServiceError, match="provider invocation forbidden"):
            script_service.generate_script(project)
        script_revision = script_service.create_revision_from_story(
            project.id, story_revision["id"], script
        )
        script_revision = script_service.approve_revision(script_revision["id"])
        assert script_revision["status"] is ScriptRevisionStatus.APPROVED
        script_loaded = repository.get_script_revision(script_revision["id"])
        assert script_loaded is not None
        assert script_loaded["content"].total_estimated_duration_seconds == pytest.approx(TARGET_DURATION)

        # Shot Plan: create a canonical Draft first, then replace its content
        # with the fixture while pinning the real persisted script revision.
        shot_payload = dict(shot_data)
        shot_payload["source_script_revision_id"] = script_revision["id"]
        shot_plan = ShotPlan.model_validate(shot_payload)
        shot_plan.validate_against(script, story)
        shot_seed = shot_service.create_plan(project.id, script_revision["id"])
        shot_revision = shot_service.save_draft(
            project.id, shot_plan, revision_id=shot_seed["id"]
        )
        shot_revision = shot_service.approve_revision(shot_revision["id"])
        assert shot_revision["status"] is ShotRevisionStatus.APPROVED
        assert offline_gateway.generation_calls == 2
        with pytest.raises(ShotServiceError, match="provider invocation forbidden"):
            shot_service.generate_shot_plan(project)
        assert offline_gateway.generation_calls == 3
        shot_loaded = repository.get_shot_revision(shot_revision["id"])
        assert shot_loaded is not None
        loaded_plan = shot_loaded["content"]
        assert loaded_plan.source_script_revision_id == script_revision["id"]
        assert [shot.id for shot in loaded_plan.shots] == EXPECTED_SHOT_IDS
        assert [shot.duration_seconds for shot in loaded_plan.shots] == EXPECTED_SHOT_DURATIONS
        assert all(shot.status.value == "LOCKED" for shot in loaded_plan.shots)
        assert sum(shot.duration_seconds for shot in loaded_plan.shots) == TARGET_DURATION
        assert loaded_plan.total_duration_seconds == pytest.approx(TARGET_DURATION)

        dependency = DependencyStatusService(repository).project(project.id)
        dependency_by_type = {
            item["entity_type"]: item for item in dependency["dependencies"]
        }
        assert set(dependency_by_type) == {
            "STORY_BIBLE",
            "STRUCTURED_SCRIPT",
            "SHOT_PLAN",
        }
        assert dependency_by_type["STORY_BIBLE"]["revision_id"] == story_revision["id"]
        assert dependency_by_type["STORY_BIBLE"]["current_revision_id"] == story_revision["id"]
        assert dependency_by_type["STRUCTURED_SCRIPT"]["revision_id"] == script_revision["id"]
        assert dependency_by_type["STRUCTURED_SCRIPT"]["source_revision_id"] == story_revision["id"]
        assert dependency_by_type["STRUCTURED_SCRIPT"]["current_revision_id"] == story_revision["id"]
        assert dependency_by_type["SHOT_PLAN"]["revision_id"] == shot_revision["id"]
        assert dependency_by_type["SHOT_PLAN"]["source_revision_id"] == script_revision["id"]
        assert dependency_by_type["SHOT_PLAN"]["current_revision_id"] == script_revision["id"]
        assert dependency["outdated"] == []
        assert CurrentProductionStateService(repository).workflow_stage(project.id) is ProjectStatus.PREPRODUCTION

        # Promote four policy-sized local images through the canonical Source
        # Pack -> ReferenceAsset version/binding pipeline.
        reference_service = ReferenceAssetService(repository)
        reference_specs = [
            ("CHARACTER", "lin_xia", (185, 95, 95)),
            ("CHARACTER", "lin_father", (95, 125, 185)),
            ("LOCATION", "rain_old_street", (75, 100, 120)),
            ("LOCATION", "old_house_interior", (175, 125, 65)),
        ]
        for binding_type, binding_id, color in reference_specs:
            image = _reference_png(color)
            image_source = intake.source_pack.import_bytes(
                project.id,
                f"{binding_id}.png",
                image,
                mime_type="image/png",
                source_kind=SourceKind.IMAGE,
                metadata={"fixture_binding_id": binding_id},
            )
            promoted = intake.promote_image_reference(
                project.id,
                image_source.id,
                source_story_revision_id=story_revision["id"],
                binding_type=binding_type,
                binding_id=binding_id,
                lock=True,
            )
            assert promoted["asset"].current_version_id == promoted["version"].id
            assert promoted["binding"].binding_id == binding_id

        readiness = reference_service.calculate_readiness(project.id, story_revision["id"])
        assert readiness["characters"]["total"] == 2
        assert readiness["characters"]["used"] == 2
        assert readiness["characters"]["locked"] == 2
        assert readiness["characters"]["missing"] == 0
        assert readiness["locations"]["total"] == 2
        assert readiness["locations"]["used"] == 2
        assert readiness["locations"]["locked"] == 2
        assert readiness["locations"]["missing"] == 0
        required_character_ids = {
            item["binding_id"] for item in reference_data["characters"] if item["required"]
        }
        required_location_ids = {
            item["binding_id"] for item in reference_data["locations"] if item["required"]
        }
        assert required_character_ids == {"lin_xia", "lin_father"}
        assert required_location_ids == {"rain_old_street", "old_house_interior"}
        for binding_type, binding_id in (
            [(ReferenceBindingType.CHARACTER, item) for item in sorted(required_character_ids)]
            + [(ReferenceBindingType.LOCATION, item) for item in sorted(required_location_ids)]
        ):
            assert reference_service.is_binding_ready(
                project.id, binding_type, binding_id, story_revision["id"]
            )

        # Production precondition and provider-neutral projection.
        production = ProductionService(repository, reference_service=reference_service)
        production_readiness = production.validate_job_readiness(
            project.id, shot_revision["id"]
        )
        assert production_readiness["ready"] is True
        assert production_readiness["shot_count"] == 12
        assert production_readiness["required_characters"] == sorted(required_character_ids)
        assert production_readiness["required_locations"] == sorted(required_location_ids)
        assert production_readiness["character_reference_coverage"] == "2/2"
        assert production_readiness["location_reference_coverage"] == "2/2"
        job = production.create_production_job(project.id, shot_revision["id"])
        assert job.status is ProductionJobStatus.READY
        production_shots = production.create_production_shots(project.id, job.id)
        assert [item.shot_id for item in production_shots] == EXPECTED_SHOT_IDS
        assert [item.order_index for item in production_shots] == list(range(1, 13))

        # The paid-capable queue has an explicit authorization gate.  Calling
        # it without approval must stop before provider-profile resolution or
        # creation of any provider task.
        queue_service = ProductionQueueService(
            repository, production_service=production
        )
        with pytest.raises(ProductionQueueError, match="必须明确批准"):
            queue_service.enqueue_job(project.id, job.id)
        assert repository.list_provider_tasks(project.id) == []

        briefs = GenerationBriefService(repository).prepare_for_job(project.id, job.id)
        assert len(briefs) == 12
        brief_by_shot = {brief.shot_id: brief for brief in briefs}
        assert set(brief_by_shot) == set(EXPECTED_SHOT_IDS)

        execution_service = ProductionExecutionService(
            repository, production_service=production
        )
        full_snapshot = execution_service.create_input_snapshot(project.id, job.id)
        full_json = full_snapshot.to_json_dict()
        assert len(full_snapshot.shot_parameters) == 12
        assert set(full_snapshot.reference_asset_versions) == {
            "CHARACTER:lin_xia",
            "CHARACTER:lin_father",
            "LOCATION:rain_old_street",
            "LOCATION:old_house_interior",
        }

        # Queue every shot before completing any one.  The current canonical
        # complete_execution() implementation marks the whole job SUCCEEDED
        # for one finished execution, so later queue creation must happen up
        # front; complete_attempt() repairs the aggregate state for each
        # non-final shot.  This is retained as source-blocker evidence rather
        # than changed in the production layer here.
        queued: list[tuple[Any, Any, Any]] = []
        for production_shot in production_shots:
            shot_id = production_shot.shot_id
            one_json = dict(full_json)
            one_json["shot_parameters"] = {
                shot_id: full_json["shot_parameters"][shot_id]
            }
            one_json["generation_brief_id"] = brief_by_shot[shot_id].id
            one_snapshot = ProductionInputSnapshot.model_validate(one_json)
            execution, attempt = execution_service.enqueue_shot_execution_with_attempt(
                project.id,
                job.id,
                one_snapshot,
                worker_type="offline-fixture",
                generation_brief_id=brief_by_shot[shot_id].id,
            )
            assert execution.status is ProductionExecutionStatus.QUEUED
            queued.append((production_shot, execution, attempt))
        assert repository.list_provider_tasks(project.id) == []
        assert repository.list_ai_invocations(project.id) == []

        # Deterministic local clips are generated at the native 720p/24fps
        # profile.  Real QC probes their physical bytes, so each source keeps
        # the exact creative duration without trusting artifact metadata.
        from test.aidrama_studio.video_fixtures import mp4_bytes

        media_by_duration = {
            duration: mp4_bytes(source=f"testsrc=size=1280x720:rate=24:d={duration}")
            for duration in sorted(set(EXPECTED_SHOT_DURATIONS))
        }
        sha_by_duration = {
            duration: hashlib.sha256(payload).hexdigest()
            for duration, payload in media_by_duration.items()
        }
        qc_service = ProductionQCService(repository)
        qc_results = []
        reviews = []
        for production_shot, execution, attempt in queued:
            execution_service.start_execution(
                project.id,
                execution.id,
                {"offline_fixture": True, "shot_id": production_shot.shot_id},
            )
            relative_path = (
                f"production/{job.id}/renders/{production_shot.shot_id}.mp4"
            )
            creative_duration = int(
                full_snapshot.shot_parameters[production_shot.shot_id][
                    "duration_seconds"
                ]
            )
            local_media = media_by_duration[creative_duration]
            media_path = project_directory / Path(*relative_path.split("/"))
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(local_media)
            artifact = execution_service.record_artifact(
                project.id,
                execution.id,
                "video/mp4",
                relative_path,
                {
                    "mime_type": "video/mp4",
                    "artifact_role": "FINAL",
                    "execution_id": execution.id,
                    "shot_id": production_shot.shot_id,
                    "reference_versions": dict(full_snapshot.reference_asset_versions),
                    "duration_seconds": creative_duration,
                    "resolution": "1280x720",
                    "codec": "h264",
                    "sha256": sha_by_duration[creative_duration],
                    "audio_required": False,
                },
            )
            execution_service.complete_execution(
                project.id, execution.id, {"offline_fixture": True}
            )
            production.complete_attempt(
                project.id,
                attempt.id,
                {"artifact_id": artifact.id, "path": relative_path},
            )
            qc_result = qc_service.run_qc(project.id, execution.id, artifact.id)
            assert qc_result.status is ProductionQCStatus.QC_PASS
            metrics = qc_service.list_metrics(project.id, qc_result.id)
            assert any(
                metric.metric_name == "video_duration"
                and metric.status is ProductionQCMetricStatus.PASS
                for metric in metrics
            )
            assert any(
                metric.metric_name == "traceability"
                and metric.status is ProductionQCMetricStatus.PASS
                for metric in metrics
            )
            metric_by_name = {metric.metric_name: metric for metric in metrics}
            assert metric_by_name["video_resolution"].status is ProductionQCMetricStatus.PASS
            assert metric_by_name["video_resolution"].value_json["resolution"] == "1280x720"
            assert metric_by_name["video_codec"].status is ProductionQCMetricStatus.PASS
            assert metric_by_name["video_codec"].value_json["codec"] == "h264"
            review = qc_service.create_review(
                project.id,
                qc_result.id,
                ProductionReviewDecision.APPROVED,
                reviewer="offline-acceptance-harness",
                notes="deterministic local media; provider boundary not crossed",
            )
            qc_results.append(qc_result)
            reviews.append(review)

        assert len(qc_results) == len(reviews) == 12
        assert all(item.status is ProductionQCStatus.QC_PASS for item in qc_results)
        assert all(item.decision is ProductionReviewDecision.APPROVED for item in reviews)
        assert all(
            item.status is ProductionShotStatus.SUCCEEDED
            for item in repository.list_production_shots(job.id)
        )
        assert production.get_job(project.id, job.id).status is ProductionJobStatus.SUCCEEDED

        # Final Assembly is metadata-only here: select the real qualified
        # sources, freeze the ordered manifest, and do not invoke a renderer.
        final_assembly_service = FinalAssemblyService(repository)
        final_readiness = final_assembly_service.calculate_readiness(project.id, job.id)
        assert final_readiness.total_shots == 12
        assert final_readiness.eligible_shots == 12
        assert final_readiness.blocked_shots == 0
        assert final_readiness.ready
        assembly = final_assembly_service.create_assembly(
            project.id, job.id, freeze=True
        )
        assert assembly.status is FinalAssemblyStatus.READY
        manifest = final_assembly_service.get_manifest(project.id, assembly.id)
        items = sorted(manifest.items, key=lambda item: item.order_index)
        assert len(items) == 12
        manifest_shot_ids = [
            repository.get_production_shot(item.production_shot_id).shot_id
            for item in items
        ]
        assert assembly_data["expected_order"] == EXPECTED_SHOT_IDS
        assert manifest_shot_ids == assembly_data["expected_order"]
        assert [item.order_index for item in items] == list(range(1, 13))
        timeline = [
            (float(item.timeline_start_seconds), float(item.timeline_end_seconds))
            for item in items
        ]
        assert timeline[0][0] == pytest.approx(0.0, abs=EPSILON)
        for previous, current in zip(timeline, timeline[1:]):
            assert current[0] == pytest.approx(previous[1], abs=EPSILON)
        assert timeline[-1][1] == pytest.approx(TARGET_DURATION, abs=EPSILON)
        fixture_assembly_durations = [
            float(item["trimmed_duration_seconds"]) for item in assembly_data["items"]
        ]
        assert fixture_assembly_durations == EXPECTED_SHOT_DURATIONS
        assert [
            pytest.approx(float(item.timeline_duration_seconds), abs=EPSILON)
            for item in items
        ] == [pytest.approx(value, abs=EPSILON) for value in fixture_assembly_durations]
        assert sum(float(item.timeline_duration_seconds) for item in items) == pytest.approx(TARGET_DURATION, abs=EPSILON)
        assert [
            float(item.source_duration_seconds) for item in items
        ] == [pytest.approx(value, abs=EPSILON) for value in EXPECTED_SHOT_DURATIONS]

        # Current state and output-profile projections must resolve the same
        # canonical chain after QC/review and manifest freezing.
        current_state = CurrentProductionStateService(repository).derive(project.id, job.id)
        assert current_state.production_complete is True
        assert len(current_state.qualified_sources) == 12
        assert current_state.final_readiness is not None and current_state.final_readiness.ready
        assert CurrentProductionStateService(repository).workflow_stage(project.id) in {
            ProjectStatus.REVIEW,
            ProjectStatus.POSTPRODUCTION,
        }
        pinned_profile = repository.get_output_profile(job.output_profile_id)
        assert pinned_profile is not None
        assert pinned_profile.delivery_width == 1920
        assert pinned_profile.delivery_height == 1080
        assert pinned_profile.target_fps == 24
        assert pinned_profile.target_video_codec == "h264"
        assert pinned_profile.target_audio_sample_rate == 48000
        assert pinned_profile.target_audio_channels == 2
        assert assembly.output_profile_id == pinned_profile.id

        # Canonical script -> subtitle projection (unsaved, because exact SRT
        # timings are a fixture contract) plus a domain-validated fixture track.
        post_service = PostProductionService(repository)
        canonical_track = post_service.build_subtitle_timeline(
            project.id,
            script_revision["id"],
            shot_plan_revision_id=shot_revision["id"],
        )
        # The canonical script contains one additional non-TTS inner
        # monologue (beat_03_05); the fixture's seven-cue delivery contract
        # intentionally omits it.  The canonical projection should therefore
        # retain all eight script dialogue/monologue cues, while the fixture
        # track below validates the seven delivery cues exactly.
        assert len(canonical_track.cues) == 8
        assert all(cue.shot_id in EXPECTED_SHOT_IDS for cue in canonical_track.cues)
        canonical_cues_by_text = {cue.text: cue for cue in canonical_track.cues}
        subtitle_data = _parse_srt(FIXTURE_DIR / "subtitle_track.srt")
        assert len(subtitle_data) == 7
        assert all(
            0 <= float(cue["start"]) < float(cue["end"]) <= TARGET_DURATION
            for cue in subtitle_data
        )
        assert all(
            float(left["end"]) <= float(right["start"])
            for left, right in zip(subtitle_data, subtitle_data[1:])
        )
        tts_cues = tts_data["cues"]
        assert len(tts_cues) == 7
        shot_by_id = {shot.id: shot for shot in loaded_plan.shots}
        fixture_subtitle_track = SubtitleTrack(
            id="offline-fixture-subtitles",
            project_id=project.id,
            source_script_revision_id=script_revision["id"],
            cues=[
                SubtitleCue(
                    id=f"subtitle-{index}",
                    text=str(cue["text"]),
                    start_seconds=float(cue["start"]),
                    end_seconds=float(cue["end"]),
                    shot_id=str(tts_cues[index - 1]["shot_id"]),
                )
                for index, cue in enumerate(subtitle_data, start=1)
            ],
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        assert len(fixture_subtitle_track.cues) == 7
        tts_intervals: list[tuple[float, float]] = []
        for index, (subtitle, tts) in enumerate(
            zip(fixture_subtitle_track.cues, tts_cues), start=1
        ):
            assert subtitle.text == tts["text"]
            assert tts["expected_subtitle_index"] == index
            assert tts["speaker_id"] in {"lin_xia", "lin_father"}
            assert tts["voice_profile_id"] in tts_data["voice_profiles"]
            canonical_cue = canonical_cues_by_text.get(str(tts["text"]))
            assert canonical_cue is not None
            assert canonical_cue.shot_id == tts["shot_id"]
            assert subtitle.shot_id == tts["shot_id"]
            assert subtitle.start_seconds == pytest.approx(
                float(tts["start_seconds"]), abs=EPSILON
            )
            assert subtitle.end_seconds == pytest.approx(
                float(tts["end_seconds"]), abs=EPSILON
            )
            shot = shot_by_id[tts["shot_id"]]
            shot_start = sum(
                item.duration_seconds for item in loaded_plan.shots if item.order < shot.order
            )
            assert shot_start <= float(tts["start_seconds"]) < float(tts["end_seconds"]) <= shot_start + shot.duration_seconds
            tts_intervals.append((float(tts["start_seconds"]), float(tts["end_seconds"])))
        assert all(
            left[1] <= right[0] for left, right in zip(tts_intervals, tts_intervals[1:])
        )

        # Persist the seven-cue fixture projection through the canonical post
        # repository so a cold reload proves the cues were not only re-read in
        # memory.  A plan is metadata-only here; no final-media render is run.
        post_plan = post_service.create_plan(project.id, assembly.id)
        persisted_track = repository.create_post_subtitle_track(
            fixture_subtitle_track.model_copy(update={"plan_id": post_plan.id})
        )
        assert len(persisted_track.cues) == 7

        # The real TTS service validates the persisted post-plan/script/track
        # chain, then fails at its unavailable provider selection boundary.
        # Its provider method must never be reached in this offline harness.
        offline_tts = _OfflineTTSProvider()
        tts_registry = CapabilityRegistry([offline_tts])
        tts_profiles = ProviderProfileService(repository, registry=tts_registry)
        tts_runtime = TTSRuntimeService(
            repository,
            provider=offline_tts,
            registry=tts_registry,
            provider_profiles=tts_profiles,
        )
        with pytest.raises(TTSRuntimeError, match="不会调用 TTS Provider"):
            tts_runtime.synthesize_track(
                project.id,
                post_plan.id,
                persisted_track.cues,
                script_revision_id=script_revision["id"],
                subtitle_track_id=persisted_track.id,
            )
        assert offline_tts.synthesis_calls == 0

        # A cold repository reopen must project the same approved chain and
        # manifest from the isolated database, not from in-memory JSON.
        cold_repository = ProjectRepository(paths)
        assert cold_repository.get_project(project.id) is not None
        cold_manifest = FinalAssemblyService(cold_repository).get_manifest(project.id, assembly.id)
        assert [
            cold_repository.get_production_shot(item.production_shot_id).shot_id
            for item in sorted(cold_manifest.items, key=lambda item: item.order_index)
        ] == EXPECTED_SHOT_IDS
        assert cold_repository.list_ai_invocations(project.id) == []
        assert cold_repository.list_provider_tasks(project.id) == []
        assert cold_repository.list_runtime_plans(project.id) == []
        assert cold_repository.list_heavy_jobs(project.id) == []
        cold_track = cold_repository.get_post_subtitle_track(persisted_track.id)
        assert cold_track is not None and len(cold_track.cues) == 7
        assert [cue.text for cue in cold_track.cues] == [
            str(item["text"]) for item in tts_cues
        ]
        assert provider_submit_attempts == []
        assert network_attempts == []
        assert time.perf_counter() - started < 300
    finally:
        # Close/reopen operations above use short-lived repository connections;
        # removing the entire temporary root proves no user DB was touched.
        for _ in range(20):
            gc.collect()
            shutil.rmtree(data_parent, ignore_errors=True)
            if not data_parent.exists():
                break
            time.sleep(0.05)

    assert not data_parent.exists()
