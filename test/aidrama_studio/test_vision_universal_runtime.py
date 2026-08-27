from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
from streamlit.testing.v1 import AppTest

from aidrama_studio.domain import ProductionReviewDecision, ProviderPreset
from aidrama_studio.services import (
    CapabilityKind,
    CapabilityRegistry,
    ProductionExecutionService,
    ProductionQCService,
    ProductionService,
    ProviderProfileService,
    UniversalVisionAnalysisProvider,
    UniversalVisionRuntimeError,
    VISION_ANALYSIS_METRICS,
    VisionAnalysisRequest,
    VisionFrameSamplingService,
    VisionMediaInput,
    VisionQCService,
    default_capability_registry,
    validate_vision_analysis_output,
)
from aidrama_studio.services.vision_qc import VisionQCError
from aidrama_studio.services.model_runtime import (
    CapabilityKind as RuntimeCapabilityKind,
    DASHSCOPE_CN_ENDPOINT_PROFILE,
    MainlandProviderRuntime,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    MAINLAND_PRIMARY_MANIFEST_IDS,
)
from test.aidrama_studio.test_seedance_video_adapter import (
    _frozen_seedance_context,
)


class FakeCredentialStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.get_calls: list[str] = []

    def get(self, provider_id: str) -> str | None:
        self.get_calls.append(provider_id)
        return self.values.get(provider_id)

    def configured_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.headers = {
            "Content-Type": "application/json",
            "X-Request-Id": "fake-vision-request",
        }
        self.content = b""
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeVisionSession:
    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.mode == "exception":
            raise RuntimeError(
                "api_key=sk-provider-secret raw_response=private-provider-body"
            )
        payload = kwargs["json"]
        content = payload["input"]["messages"][0]["content"]
        prompt = next(item["text"] for item in content if "text" in item)
        raw_snapshot = prompt.split(
            "FROZEN_VISION_INPUT_JSON_BEGIN\n", 1
        )[1].split("\nFROZEN_VISION_INPUT_JSON_END", 1)[0]
        snapshot = json.loads(raw_snapshot)
        structured: object
        if self.mode == "invalid":
            structured = "api_key=sk-provider-secret raw-provider-response"
        else:
            structured = _structured_output(snapshot)
        text = (
            structured
            if isinstance(structured, str)
            else json.dumps(structured, ensure_ascii=False)
        )
        return FakeResponse(
            {
                "output": {
                    "choices": [
                        {
                            "message": {"content": text},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        )


def _structured_output(snapshot: dict[str, object]) -> dict[str, object]:
    frames = snapshot["frames"]
    source = frames[0] if frames else snapshot["video"]
    evidence = {
        "source_id": source["source_id"],
        "observation": "The supplied frame matches the frozen intent.",
    }
    if "time_seconds" in source:
        evidence["time_seconds"] = source["time_seconds"]
    metrics = {
        name: {
            "score": 0.9,
            "severity": "PASS",
            "reason": f"{name} is consistent in the supplied evidence.",
            "evidence": [dict(evidence)],
        }
        for name in VISION_ANALYSIS_METRICS
    }
    references = [
        item["source_id"] for item in snapshot.get("references", [])
    ]
    return {
        "metrics": metrics,
        "reference_comparison": {
            "compared_reference_version_ids": references,
            "findings": [
                {
                    "reference_version_id": reference_id,
                    "severity": "PASS",
                    "reason": "The locked reference identity was compared.",
                }
                for reference_id in references
            ],
        },
        "summary": "Advisory Vision analysis completed.",
    }


def _provider(
    repository,
    session: FakeVisionSession,
    *,
    configured: bool = True,
    authorized: bool = True,
):
    store = FakeCredentialStore(
        {"DASHSCOPE_API_KEY": "fake-vision-credential"}
        if configured
        else {}
    )

    def runtime_factory(**options: object) -> MainlandProviderRuntime:
        return MainlandProviderRuntime(
            sessions={DASHSCOPE_CN_ENDPOINT_PROFILE: session},
            **options,
        )

    provider = UniversalVisionAnalysisProvider(
        repository,
        manifest_id=MAINLAND_PRIMARY_MANIFEST_IDS[
            RuntimeCapabilityKind.VISION
        ],
        credential_store=store,
        runtime_factory=runtime_factory,
        env={"AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1" if authorized else "0"},
    )
    return provider, store


def _vision_context(tmp_path: Path):
    repository, project, job, snapshot, plan, brief, ordered_bindings = (
        _frozen_seedance_context(tmp_path)
    )
    ProductionService(repository).create_production_shots(project.id, job.id)
    execution, _attempt = ProductionExecutionService(
        repository
    ).enqueue_shot_execution_with_attempt(
        project.id,
        job.id,
        snapshot,
        runtime_plan_id=plan.id,
        generation_brief_id=brief.id,
    )
    relative_path = f"production/{execution.id}/shot.mp4"
    target = repository.paths.projects / project.id / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=25:d=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    references = dict(snapshot.reference_asset_versions)
    artifact = ProductionExecutionService(repository).record_artifact(
        project.id,
        execution.id,
        "video",
        relative_path,
        {
            "mime_type": "video/mp4",
            "shot_id": "shot_001",
            "duration_seconds": 1.0,
            "resolution": "160x120",
            "codec": "h264",
            "audio_required": False,
            "reference_versions": references,
            "snapshot_references_available": references,
            "provider_references_actually_used": [
                {
                    "binding_key": binding,
                    "reference_asset_version_id": references[binding],
                }
                for binding in ordered_bindings
            ],
            "runtime_plan_id": plan.id,
            "runtime_plan_hash": plan.plan_hash,
        },
    )
    return (
        repository,
        project,
        execution,
        artifact,
        target,
        plan,
        brief,
        ordered_bindings,
    )


def _wired_service(repository, project, session, **provider_options):
    provider, store = _provider(
        repository,
        session,
        **provider_options,
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    inventory = profiles.inventory(project.id, CapabilityKind.VISION)
    assert len(inventory) == 1
    profile = inventory[0]
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.VISION: profile.endpoint_profile_id},
    )
    resolved = profiles.resolve(
        project.id,
        CapabilityKind.VISION,
        require_available=True,
    )
    if provider_options.get("configured", True) and provider_options.get(
        "authorized", True
    ):
        # Settings resolution may project the same manifest-backed endpoint
        # into a fresh CapabilityProfile instance; identity is the durable
        # endpoint/model contract, not transient created_at metadata.
        assert resolved.profile is not None
        assert resolved.profile.endpoint_profile_id == profile.endpoint_profile_id
        assert resolved.profile.model_id == profile.model_id
        assert profiles.provider_for_selection(resolved) is provider
    service = VisionQCService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )
    return service, provider, store, profile, resolved


def test_vision_request_snapshot_is_deeply_immutable_and_public() -> None:
    request = VisionAnalysisRequest(
        project_id="project-1",
        execution_id="execution-1",
        artifact_id="artifact-1",
        video=VisionMediaInput(
            "VIDEO_ARTIFACT",
            "artifact-1",
            Path("C:/transient/video.mp4"),
            "video/mp4",
            "a" * 64,
        ),
        frame_manifest_id="manifest-1",
        generation_brief_hash="b" * 64,
        creative_context={
            "generation_brief_id": "brief-1",
            "content": {"action": "walks into frame"},
        },
    )

    with pytest.raises(TypeError):
        request.creative_context["content"]["action"] = "mutated"
    public = request.public_dict()
    assert public["creative_context"]["content"]["action"] == "walks into frame"
    assert "C:/" not in repr(public)


def test_default_inventory_exposes_universal_vision_manifest_offline() -> None:
    registry = default_capability_registry(
        env={
            "DASHSCOPE_API_KEY": "fake-inventory-credential",
            "AIDRAMA_ALLOW_PAID_LIVE_TESTS": "0",
        }
    )
    providers = registry.list(CapabilityKind.VISION)
    universal = next(
        item
        for item in providers
        if getattr(item, "provider_name", "") == "alibaba_model_studio"
    )

    assert providers[0] is universal
    assert universal.status.configured is True
    assert universal.status.available is False
    assert universal.status.metadata["manifest_id"] == (
        MAINLAND_PRIMARY_MANIFEST_IDS[RuntimeCapabilityKind.VISION]
    )


def test_structured_output_rejects_unknown_frame_evidence() -> None:
    request = VisionAnalysisRequest(
        project_id="project-1",
        execution_id="execution-1",
        artifact_id="artifact-1",
        video=VisionMediaInput(
            "VIDEO_ARTIFACT",
            "artifact-1",
            Path("C:/transient/video.mp4"),
            "video/mp4",
            "a" * 64,
        ),
    )
    value = _structured_output(request.public_dict())
    value["metrics"][VISION_ANALYSIS_METRICS[0]]["evidence"][0][
        "source_id"
    ] = "invented-frame"

    with pytest.raises(
        UniversalVisionRuntimeError,
        match="unknown input",
    ):
        validate_vision_analysis_output(value, request)


def test_fake_universal_runtime_full_vision_qc_path(tmp_path: Path) -> None:
    (
        repository,
        project,
        execution,
        artifact,
        _target,
        plan,
        brief,
        ordered_bindings,
    ) = _vision_context(tmp_path)
    qc_service = ProductionQCService(repository)
    technical = qc_service.run_qc(project.id, execution.id, artifact.id)
    human = qc_service.create_review(
        project.id,
        technical.id,
        ProductionReviewDecision.APPROVED,
        reviewer="human-reviewer",
        notes="Human decision remains authoritative.",
    )
    execution_before = repository.get_production_execution(execution.id)
    session = FakeVisionSession()
    service, provider, store, profile, resolved = _wired_service(
        repository,
        project,
        session,
    )

    result = service.analyze(project.id, execution.id, artifact.id)

    assert result.status == "AI_ANALYSIS", result.reason
    assert service.blocks_final is False
    assert set(result.metrics) == set(VISION_ANALYSIS_METRICS)
    assert all(
        set(metric) == {"score", "severity", "reason", "evidence"}
        for metric in result.metrics.values()
    )
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].startswith("https://dashscope.aliyuncs.com/api/v1/")
    assert "127.0.0.1:8502" not in call["url"]
    payload = call["json"]
    assert payload["model"] == profile.model_id
    assert payload["parameters"]["response_format"]["type"] == "json_schema"
    content = payload["input"]["messages"][0]["content"]
    media_content = [item for item in content if "text" not in item]
    manifest = repository.get_vision_frame_manifest(result.frame_manifest_id)
    assert manifest is not None
    assert len(media_content) == (
        1 + manifest.frame_count + len(plan.reference_version_ids)
    )
    assert all(
        next(iter(item.values())).startswith("data:") for item in media_content
    )

    assert manifest.frame_count >= 3
    assert {item["role"] for item in manifest.samples} >= {
        "FIRST",
        "MIDDLE",
        "LAST",
    }
    record = repository.get_vision_analysis(result.analysis_id)
    assert record is not None
    assert record.reference_version_ids == plan.reference_version_ids
    assert record.input_provenance["generation_brief_hash"] == brief.sha256
    assert (
        record.input_provenance["creative_context"]["generation_brief_id"]
        == brief.id
    )
    assert (
        record.input_provenance["creative_context"]["content"]["action"]
        == brief.action
    )
    assert [item["role"] for item in record.input_provenance["references"]] == (
        ordered_bindings
    )
    runtime_selection = record.input_provenance["runtime_selection"]
    assert runtime_selection == provider.runtime_selection()
    assert runtime_selection["manifest_id"] == MAINLAND_PRIMARY_MANIFEST_IDS[
        RuntimeCapabilityKind.VISION
    ]
    assert runtime_selection["codec_id"] == "dashscope.qwen.vl.v1"
    assert runtime_selection["protocol"] == "REQUEST_RESPONSE"
    assert runtime_selection["credential_reference"] == "DASHSCOPE_API_KEY"
    assert runtime_selection["endpoint_profile_id"] == profile.endpoint_profile_id
    assert resolved.source == "PROJECT_DEFAULT"
    assert store.get_calls == ["DASHSCOPE_API_KEY"]

    invocations = repository.list_ai_invocations(project.id, execution.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    assert all(
        item.request_summary["runtime_selection"]["manifest_id"]
        == runtime_selection["manifest_id"]
        for item in invocations
    )
    assert service.latest(project.id, execution.id).analysis_id == result.analysis_id
    assert repository.get_production_qc_result(technical.id) == technical
    assert qc_service.list_reviews(project.id, technical.id) == [human]
    assert repository.get_production_execution(execution.id) == execution_before
    durable = repr(record.model_dump(mode="json")) + repr(
        [item.model_dump(mode="json") for item in invocations]
    )
    assert "fake-vision-credential" not in durable
    assert "data:video" not in durable
    assert "local-path" not in durable


@pytest.mark.parametrize(
    ("configured", "authorized"),
    [(False, True), (True, False)],
)
def test_unavailable_universal_vision_never_calls_transport(
    tmp_path: Path,
    configured: bool,
    authorized: bool,
) -> None:
    repository, project, execution, artifact, *_rest = _vision_context(tmp_path)
    session = FakeVisionSession()
    service, _provider_value, _store, _profile, resolved = _wired_service(
        repository,
        project,
        session,
        configured=configured,
        authorized=authorized,
    )

    result = service.analyze(project.id, execution.id, artifact.id)

    assert result.status == "NOT_RUN"
    assert not resolved.available
    assert session.calls == []
    assert repository.list_vision_frame_manifests(project.id, execution.id) == []
    assert repository.list_ai_invocations(project.id, execution.id) == []


@pytest.mark.parametrize("mode", ["invalid", "exception"])
def test_universal_vision_failures_are_persisted_and_sanitized(
    tmp_path: Path,
    mode: str,
) -> None:
    repository, project, execution, artifact, *_rest = _vision_context(tmp_path)
    qc_service = ProductionQCService(repository)
    technical = qc_service.run_qc(project.id, execution.id, artifact.id)
    human = qc_service.create_review(
        project.id,
        technical.id,
        ProductionReviewDecision.REJECTED,
        reviewer="human-reviewer",
        notes="Provider failure cannot change this decision.",
    )
    execution_before = repository.get_production_execution(execution.id)
    session = FakeVisionSession(mode)
    service, *_unused = _wired_service(repository, project, session)

    result = service.analyze(project.id, execution.id, artifact.id)

    assert result.status == "FAILED"
    assert "sk-provider-secret" not in result.reason
    assert "private-provider-body" not in result.reason
    record = repository.get_vision_analysis(result.analysis_id)
    invocations = repository.list_ai_invocations(project.id, execution.id)
    assert record.status == "FAILED"
    assert record.input_provenance["failure_reason"] == result.reason
    assert [item.status for item in invocations] == ["STARTED", "FAILED"]
    durable = repr(record.model_dump(mode="json")) + repr(
        [item.model_dump(mode="json") for item in invocations]
    )
    assert "sk-provider-secret" not in durable
    assert "private-provider-body" not in durable
    assert "raw-provider-response" not in durable
    assert repository.get_production_qc_result(technical.id) == technical
    assert qc_service.list_reviews(project.id, technical.id) == [human]
    assert repository.get_production_execution(execution.id) == execution_before


def test_frame_extraction_and_artifact_failures_do_not_call_provider(
    tmp_path: Path,
) -> None:
    repository, project, execution, artifact, target, *_rest = _vision_context(
        tmp_path
    )
    qc_service = ProductionQCService(repository)
    technical = qc_service.run_qc(project.id, execution.id, artifact.id)
    human = qc_service.create_review(
        project.id,
        technical.id,
        ProductionReviewDecision.APPROVED,
        reviewer="human-reviewer",
        notes="Input failures cannot change this decision.",
    )
    session = FakeVisionSession()
    service, provider, _store, _profile, _resolved = _wired_service(
        repository,
        project,
        session,
    )

    class FailingSampler(VisionFrameSamplingService):
        def sample(self, *args: object, **kwargs: object):
            raise VisionQCError("frame extraction failed")

    failed_sampling = VisionQCService(
        repository,
        provider=provider,
        sampler=FailingSampler(repository),
    ).analyze(project.id, execution.id, artifact.id)
    assert failed_sampling.status == "FAILED"
    assert session.calls == []

    target.unlink()
    missing_artifact = service.analyze(project.id, execution.id, artifact.id)
    assert missing_artifact.status == "FAILED"
    assert "不存在" in missing_artifact.reason
    assert session.calls == []
    assert repository.get_production_qc_result(technical.id) == technical
    assert qc_service.list_reviews(project.id, technical.id) == [human]


def test_review_apptest_projects_persisted_advisory_analysis(
    tmp_path: Path,
) -> None:
    repository, project, execution, artifact, *_rest = _vision_context(tmp_path)
    service, *_unused = _wired_service(
        repository,
        project,
        FakeVisionSession(),
    )
    result = service.analyze(project.id, execution.id, artifact.id)
    assert result.status == "AI_ANALYSIS", result.reason
    root = str(repository.paths.root).replace("\\", "\\\\")
    script = f"""
from pathlib import Path
from types import SimpleNamespace
from aidrama_studio.pages import review as page
from aidrama_studio.services import UnavailableVisionProvider, VisionQCService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository

root = Path(r"{root}")
paths = DatabasePaths(root / "aidrama.db", root / "projects", root / "archived_projects")
repository = ProjectRepository(paths)
service = VisionQCService(repository, provider=UnavailableVisionProvider())
page._render_vision_summary(
    service,
    SimpleNamespace(id="{project.id}"),
    SimpleNamespace(id="{execution.id}"),
    SimpleNamespace(id="{artifact.id}"),
)
"""

    app = AppTest.from_string(script).run(timeout=30)

    assert not app.exception
    captions = [item.value for item in app.caption]
    assert any("CHARACTER IDENTITY CONSISTENCY" in item for item in captions)
    assert any("PASS" in item for item in captions)
    assert any("不会替代技术检查或人工决定" in item for item in captions)
    assert not any(button.label == "运行 Vision QC" for button in app.button)
