from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

import pytest
import requests

from aidrama_studio.domain import (
    ProviderTask,
    ProductionInputSnapshot,
    ReferenceProvenance,
    ReferenceAssetType,
    ShotFirstFrame,
    ShotFirstFrameSourceType,
    ShotKeyframeReferenceRole,
    ShotKeyframeSelection,
    ShotKeyframeSelectionPolicy,
)
from aidrama_studio.services import (
    CapabilityRegistry,
    ProductionExecutionService,
    ProductionRuntimeResolutionError,
    ProductionRuntimeResolver,
    RuntimeVideoProvider,
)
from aidrama_studio.services.adapters import (
    RuntimeContentRejectedError,
    WanAdapterError,
    WanInputMapper,
    WanProductionAdapter,
    WanProviderConfig,
    WanVideoClient,
    WanTransientError,
    RuntimeSubmission,
)
from aidrama_studio.services.adapters.wan_video import (
    WanFirstFrameResolver,
    WanFirstFrameSelection,
)
from aidrama_studio.services.provider_result_download import validate_mp4_prefix
from aidrama_studio.services.shot_keyframe import ShotFirstFrameArtifactResolver
from aidrama_studio.services.streaming_artifact import StreamingArtifactSource


JPEG = b"\xff\xd8\xff\xe0" + b"fixture" * 8
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"video"


def _first_frame() -> ShotFirstFrame:
    selection = ShotKeyframeSelection(
        project_id="project-1",
        shot_id="shot-1",
        policy=ShotKeyframeSelectionPolicy.NEW_SCENE,
        source_type=ShotFirstFrameSourceType.GENERATED_KEYFRAME,
        reason="A distinct shot-specific keyframe is required.",
    )
    identity = ReferenceProvenance(
        role=ShotKeyframeReferenceRole.IDENTITY,
        asset_id="character-asset-1",
        asset_version_id="version-1",
        asset_type=ReferenceAssetType.CHARACTER_REFERENCE,
        sha256="1" * 64,
        binding_id="CHARACTER:hero",
        subject_id="hero",
        stable_description="Hero identity constraint",
    )
    return ShotFirstFrame(
        id="first-frame-1",
        project_id="project-1",
        shot_id="shot-1",
        shot_plan_revision_id="plan-1",
        generation_brief_id="brief-1",
        shot_keyframe_brief_id="keyframe-brief-1",
        shot_keyframe_brief_sha256="2" * 64,
        artifact_id="first-frame-artifact-1",
        execution_id="keyframe-execution-1",
        artifact_size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
        mime_type="image/jpeg",
        source_type=ShotFirstFrameSourceType.GENERATED_KEYFRAME,
        selection=selection,
        identity_reference_provenance=(identity,),
        created_at="2026-08-28T00:00:00+00:00",
    )


def _snapshot(
    *, include_first_frame: bool = True, **references: str
) -> ProductionInputSnapshot:
    frame = _first_frame()
    return ProductionInputSnapshot(
        project_id="project-1",
        story_revision_id="story-1",
        script_revision_id="script-1",
        shot_plan_revision_id="plan-1",
        generation_brief_id="brief-1",
        reference_asset_versions=references or {"CHARACTER:hero": "version-1"},
        shot_parameters={
            "shot-1": {
                "visual_intent": "A cinematic hero enters a warm room",
                "subject": ["hero"],
                "action": "walks forward",
                "shot_size": "MEDIUM",
                "camera_movement": "PUSH_IN",
                "duration_seconds": 5,
                "lighting": {"quality": "soft", "tone": "golden"},
            }
        },
        shot_first_frames=(frame,) if include_first_frame else (),
        first_frame_required_shot_ids=("shot-1",),
    )


class FakeResolver:
    def __init__(self, path: Path):
        frame = _first_frame()
        self.selection = WanFirstFrameSelection(
            first_frame_id=frame.id,
            artifact_id=frame.artifact_id,
            source_type=frame.source_type.value,
            path=path,
            mime_type="image/jpeg",
            sha256=frame.sha256,
            size_bytes=frame.artifact_size_bytes,
            shot_id=frame.shot_id,
            identity_reference_version_ids=("version-1",),
        )

    def resolve(self, snapshot):
        self.selection.validate_snapshot(snapshot)
        return self.selection


class FakeWanClient:
    def __init__(self):
        self.payload = None
        self.status = "PENDING"
        self.downloaded_url = None

    def create_task(self, payload):
        self.payload = payload
        return "wan-task-1"

    def get_task(self, task_id):
        return {"output": {"task_id": task_id, "task_status": self.status, "video_url": "https://files.example/video.mp4"}}

    def stream_result(self, url):
        self.downloaded_url = url
        return StreamingArtifactSource(
            lambda sink: (validate_mp4_prefix(MP4), sink.write(MP4)),
            len(MP4) + 1,
        )


def test_wan_input_mapping_uses_exact_frozen_first_frame_and_structured_prompt(tmp_path):
    image = tmp_path / "shot-first-frame.jpg"
    image.write_bytes(JPEG)
    config = WanProviderConfig(api_key="test-key")
    snapshot = _snapshot()
    first_frame = FakeResolver(image).resolve(snapshot)
    payload, trace = WanInputMapper.map_snapshot(snapshot, config, first_frame)

    assert payload["model"] == "wan2.7-i2v-2026-04-25"
    assert payload["input"]["prompt"].startswith("A cinematic hero")
    media = payload["input"]["media"]
    assert media == [{"type": "first_frame", "url": media[0]["url"]}]
    encoded = media[0]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == JPEG
    assert trace["production_shot_id"] == "shot-1"
    assert trace["first_frame_id"] == "first-frame-1"
    assert trace["first_frame_artifact_id"] == "first-frame-artifact-1"
    assert trace["first_frame_source_type"] == "GENERATED_KEYFRAME"
    assert trace["first_frame_sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert trace["identity_reference_version_ids"] == ["version-1"]
    assert trace["provider"] == "alibaba_model_studio"
    assert trace["prompt_sha256"] == hashlib.sha256(
        payload["input"]["prompt"].encode("utf-8")
    ).hexdigest()
    assert len(trace["canonical_request_sha256"]) == 64
    assert "provider_references_actually_used" not in trace
    assert "reference_asset_version_id" not in trace
    assert "prompt" not in trace
    assert "api_key" not in trace


def test_wan_adapter_submit_status_and_result_download(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(
            api_key="test-key", allow_paid_live_tests=True
        ),
        first_frame_resolver=FakeResolver(image),
    )
    snapshot = _snapshot()
    assert adapter.validate(snapshot) is True
    submission = adapter.submit(snapshot)
    assert submission.runtime_reference == "wan-task-1"
    assert adapter.get_status("wan-task-1") == "QUEUED"
    client.status = "RUNNING"
    assert adapter.get_status("wan-task-1") == "RUNNING"
    client.status = "SUCCEEDED"
    result = adapter.get_result("wan-task-1")
    assert result["artifact_type"] == "wan-video"
    assert "content" not in result and "url" not in result
    sink = io.BytesIO()
    result["stream_source"].write_to(sink)
    assert sink.getvalue() == MP4
    assert result["metadata"]["provider_task_id"] == "wan-task-1"
    assert client.downloaded_url == "https://files.example/video.mp4"


def test_wan_adapter_rejects_missing_first_frame_and_unsupported_cancel(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(api_key="test-key"),
        first_frame_resolver=FakeResolver(image),
    )
    class RejectingResolver(FakeResolver):
        def resolve(self, snapshot):
            raise WanAdapterError("required frozen Shot First Frame is missing")

    missing_first_frame = _snapshot(
        include_first_frame=False,
        **{
            "CHARACTER:hero": "version-1",
            "LOCATION:room": "location-version-1",
        },
    )
    adapter.first_frame_resolver = RejectingResolver(image)
    assert adapter.validate(missing_first_frame) is False
    with pytest.raises(WanAdapterError, match="not supported"):
        adapter.cancel("wan-task-1")


def test_wan_adapter_rejects_malformed_video_result(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)

    class BadClient(FakeWanClient):
        def stream_result(self, url):
            invalid = b"x" * 32
            return StreamingArtifactSource(
                lambda sink: (validate_mp4_prefix(invalid), sink.write(invalid)),
                64,
            )

    adapter = WanProductionAdapter(
        BadClient(),
        config=WanProviderConfig(
            api_key="test-key", allow_paid_live_tests=True
        ),
        first_frame_resolver=FakeResolver(image),
    )
    adapter.submit(_snapshot())
    adapter.client.status = "SUCCEEDED"
    result = adapter.get_result("wan-task-1")
    with pytest.raises(Exception, match="MP4"):
        result["stream_source"].write_to(io.BytesIO())


def test_wan_status_mapping_matches_async_provider_states():
    for raw, expected in (
        ("PENDING", "QUEUED"),
        ({"output": {"task_status": "RUNNING"}}, "RUNNING"),
        ({"output": {"task_status": "SUCCEEDED"}}, "SUCCEEDED"),
        ({"output": {"task_status": "FAILED"}}, "FAILED"),
        ("CANCELED", "CANCELLED"),
        ("UNKNOWN", "FAILED"),
    ):
        assert WanProductionAdapter.map_status(raw) == expected


def test_wan_client_timeout_is_bounded_and_does_not_expose_secret():
    class TimeoutSession:
        def request(self, *args, **kwargs):
            raise requests.Timeout("secret-key must not be copied")

    client = WanVideoClient(WanProviderConfig(api_key="secret-key"), session=TimeoutSession())
    with pytest.raises(WanAdapterError, match="request failed") as error:
        client.get_task("wan-task-1")
    assert "secret-key" not in str(error.value)


def test_provider_trace_metadata_is_persistable_without_credentials():
    submission = RuntimeSubmission(
        "wan-task-1",
        {
            "provider": "alibaba_model_studio",
            "provider_task_id": "wan-task-1",
            "prompt": "walk forward",
            "first_frame_id": "first-frame-1",
            "first_frame_artifact_id": "first-frame-artifact-1",
            "first_frame_sha256": hashlib.sha256(JPEG).hexdigest(),
            "first_frame_source_type": "GENERATED_KEYFRAME",
            "api_key": "must-not-be-stored",
        },
    )
    metadata = ProductionExecutionService._submission_metadata(submission)
    assert metadata["provider_task_id"] == "wan-task-1"
    assert metadata["first_frame_artifact_id"] == "first-frame-artifact-1"
    assert metadata["first_frame_sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert "api_key" not in metadata
    assert "prompt" not in metadata


def test_wan_provider_failure_is_not_treated_as_a_result(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(
            api_key="test-key", allow_paid_live_tests=True
        ),
        first_frame_resolver=FakeResolver(image),
    )
    adapter.submit(_snapshot())
    client.status = "FAILED"
    assert adapter.get_status("wan-task-1") == "FAILED"
    with pytest.raises(WanAdapterError, match="before task succeeded"):
        adapter.get_result("wan-task-1")


def test_first_frame_resolver_never_falls_back_to_character_or_location_reference():
    references_only = _snapshot(
        include_first_frame=False,
        **{
            "CHARACTER:hero": "version-1",
            "LOCATION:room": "location-version-1",
        },
    )
    assert references_only.reference_asset_versions == {
        "CHARACTER:hero": "version-1",
        "LOCATION:room": "location-version-1",
    }
    resolver = WanFirstFrameResolver(ShotFirstFrameArtifactResolver(object()))
    with pytest.raises(
        WanAdapterError, match="Required frozen Shot First Frame is missing"
    ):
        resolver.resolve(references_only)


def test_wan_client_uses_async_header_without_exposing_key():
    class Response:
        status_code = 200

        def json(self):
            return {"output": {"task_id": "wan-task-1"}}

    class Session:
        def __init__(self):
            self.kwargs = None

        def request(self, *args, **kwargs):
            self.kwargs = kwargs
            return Response()

    session = Session()
    client = WanVideoClient(
        WanProviderConfig(api_key="secret-key", allow_paid_live_tests=True),
        session=session,
    )
    assert client.create_task({"model": "wan2.7-i2v-2026-04-25"}) == "wan-task-1"
    assert session.kwargs["headers"]["X-DashScope-Async"] == "enable"
    assert session.kwargs["headers"]["Authorization"] == "Bearer secret-key"


def test_wan_paid_create_gate_blocks_all_create_calls_but_allows_reconciliation(
    tmp_path,
):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    fake_client = FakeWanClient()
    adapter = WanProductionAdapter(
        fake_client,
        config=WanProviderConfig(
            api_key="test-key", allow_paid_live_tests=False
        ),
        first_frame_resolver=FakeResolver(image),
    )

    assert adapter.status.configured is True
    assert adapter.status.available is False
    with pytest.raises(WanAdapterError, match="AIDRAMA_ALLOW_PAID_LIVE_TESTS"):
        adapter.submit(_snapshot())
    assert fake_client.payload is None

    # Existing provider task IDs remain pollable without paid-create
    # authorization. No resubmission is attempted during reconciliation.
    fake_client.status = "RUNNING"
    assert adapter.get_status("existing-wan-task") == "RUNNING"
    assert fake_client.payload is None


def test_wan_runtime_resolution_retains_poll_only_reconciliation_boundary():
    adapter = WanProductionAdapter(
        FakeWanClient(),
        config=WanProviderConfig(
            api_key="test-key", allow_paid_live_tests=False
        ),
    )
    registry = CapabilityRegistry(
        [RuntimeVideoProvider(adapter, provider_name="WAN_VIDEO")]
    )
    task = ProviderTask(
        id="provider-task-1",
        project_id="project-1",
        execution_id="execution-1",
        capability="VIDEO_GENERATIVE",
        provider_id="WAN_VIDEO",
        model_id=adapter.config.model,
        idempotency_key="production:execution-1",
        provider_task_id="existing-wan-task",
        state="PROVIDER_RUNNING",
        request_summary={"approved": True},
        created_at="2026-08-26T00:00:00+00:00",
        updated_at="2026-08-26T00:00:00+00:00",
    )

    resolved = ProductionRuntimeResolver(
        registry=registry,
        repository=object(),
    ).resolve(task)

    assert isinstance(resolved, WanProductionAdapter)
    assert resolved.config.allow_paid_live_tests is False
    assert resolved.status.available is False

    # A task that has never received a provider task ID cannot use the
    # poll-only reconciliation exception.  Without this guard an unavailable
    # paid provider could be mistaken for a recoverable existing task.
    without_provider_reference = task.model_copy(update={"provider_task_id": None})
    with pytest.raises(ProductionRuntimeResolutionError, match="Provider 尚未就绪"):
        ProductionRuntimeResolver(
            registry=registry,
            repository=object(),
        ).resolve(without_provider_reference)

    forged_state = task.model_copy(update={"state": "PENDING_SUBMISSION"})
    with pytest.raises(ProductionRuntimeResolutionError, match="Provider 尚未就绪"):
        ProductionRuntimeResolver(
            registry=registry,
            repository=object(),
        ).resolve(forged_state)


def test_wan_resolver_preserves_injected_poll_transport_without_paid_post():
    class PollOnlyClient:
        def __init__(self):
            self.create_calls = 0
            self.get_calls = []

        def create_task(self, _payload):
            self.create_calls += 1
            raise AssertionError("poll-only reconciliation must not create a task")

        def get_task(self, task_id):
            self.get_calls.append(task_id)
            return {"output": {"task_status": "RUNNING"}}

    client = PollOnlyClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(api_key="test-key", allow_paid_live_tests=False),
    )
    registry = CapabilityRegistry(
        [RuntimeVideoProvider(adapter, provider_name="WAN_VIDEO")]
    )
    task = ProviderTask(
        id="provider-task-1",
        project_id="project-1",
        execution_id="execution-1",
        capability="VIDEO_GENERATIVE",
        provider_id="WAN_VIDEO",
        model_id=adapter.config.model,
        idempotency_key="production:execution-1",
        provider_task_id="existing-wan-task",
        state="PROVIDER_RUNNING",
        request_summary={"approved": True},
        created_at="2026-08-26T00:00:00+00:00",
        updated_at="2026-08-26T00:00:00+00:00",
    )

    resolved = ProductionRuntimeResolver(
        registry=registry,
        repository=object(),
    ).resolve(task)

    assert isinstance(resolved, WanProductionAdapter)
    assert resolved.client is client
    assert resolved.get_status("existing-wan-task") == "RUNNING"
    assert client.get_calls == ["existing-wan-task"]
    assert client.create_calls == 0


def test_wan_http_create_gate_is_environment_derived_and_polling_stays_enabled(
    monkeypatch,
):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "output": {
                    "task_id": "existing-wan-task",
                    "task_status": "RUNNING",
                }
            }

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, *args, **kwargs):
            self.calls.append((method, args, kwargs))
            return Response()

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)
    blocked = WanProviderConfig.from_environment()
    assert blocked.allow_paid_live_tests is False
    session = Session()
    client = WanVideoClient(blocked, session=session)
    with pytest.raises(WanAdapterError, match="AIDRAMA_ALLOW_PAID_LIVE_TESTS"):
        client.create_task({"model": blocked.model})
    assert session.calls == []

    response = client.get_task("existing-wan-task")
    assert response["output"]["task_status"] == "RUNNING"
    assert [call[0] for call in session.calls] == ["GET"]

    monkeypatch.setenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "1")
    assert WanProviderConfig.from_environment().allow_paid_live_tests is True


def test_wan_retry_after_is_exposed_as_transient_poll_failure():
    class Response:
        status_code = 503
        headers = {"Retry-After": "9"}

        def json(self):
            return {"code": "ServiceUnavailable"}

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    client = WanVideoClient(
        WanProviderConfig(api_key="secret-key"), session=Session()
    )
    with pytest.raises(WanTransientError) as error:
        client.get_task("wan-task-1")
    assert error.value.retry_after_seconds == 9


def test_wan_explicit_content_policy_codes_are_safe_and_not_generic_failures():
    class Response:
        status_code = 400
        headers = {}

        def json(self):
            return {
                "code": "DataInspectionFailed",
                "message": "must-not-be-persisted: user prompt",
            }

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    client = WanVideoClient(
        WanProviderConfig(api_key="secret-key"), session=Session()
    )
    with pytest.raises(RuntimeContentRejectedError) as error:
        client.get_task("wan-task-1")
    assert error.value.failure_category == "CONTENT_REJECTED"
    assert error.value.policy_stage == "UNSPECIFIED"
    assert error.value.provider_code == "DataInspectionFailed"
    assert "user prompt" not in str(error.value)

    with pytest.raises(RuntimeContentRejectedError) as async_error:
        WanProductionAdapter.map_status(
            {
                "output": {
                    "task_status": "FAILED",
                    "code": "DataInspectionFailed",
                    "message": "must-not-leak",
                }
            }
        )
    assert async_error.value.failure_category == "CONTENT_REJECTED"
    assert "must-not-leak" not in str(async_error.value)


def test_wan_unknown_client_error_is_not_misclassified_as_content_rejection():
    class Response:
        status_code = 400
        headers = {}

        def json(self):
            return {"code": "InvalidParameter"}

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    client = WanVideoClient(
        WanProviderConfig(api_key="secret-key"), session=Session()
    )
    with pytest.raises(WanAdapterError) as error:
        client.get_task("wan-task-1")
    assert not isinstance(error.value, RuntimeContentRejectedError)

    assert WanProductionAdapter.map_status(
        {
            "output": {
                "task_status": "FAILED",
                "code": "InputDataInspectionFailed",
            }
        }
    ) == "FAILED"
