from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
import requests

from aidrama_studio.domain import (
    ProductionInputSnapshot,
    ReferenceAsset,
    ReferenceAssetType,
    ReferenceAssetVersion,
)
from aidrama_studio.services import ProductionExecutionService
from aidrama_studio.services.adapters import (
    WanAdapterError,
    WanInputMapper,
    WanProductionAdapter,
    WanProviderConfig,
    WanReferenceResolver,
    WanReferenceSelection,
    WanVideoClient,
    RuntimeSubmission,
)


JPEG = b"\xff\xd8\xff\xe0" + b"fixture" * 8
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"video"


def _snapshot(**references: str) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        project_id="project-1",
        story_revision_id="story-1",
        script_revision_id="script-1",
        shot_plan_revision_id="plan-1",
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
    )


class FakeResolver:
    def __init__(self, path: Path):
        self.selection = WanReferenceSelection(
            role="character",
            binding_key="CHARACTER:hero",
            version_id="version-1",
            path=path,
            mime_type="image/jpeg",
        )

    def resolve(self, snapshot):
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

    def download_result(self, url):
        self.downloaded_url = url
        return MP4


def test_wan_input_mapping_uses_exact_frozen_reference_and_structured_prompt(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    config = WanProviderConfig(api_key="test-key")
    payload, trace = WanInputMapper.map_snapshot(_snapshot(), config, FakeResolver(image))

    assert payload["model"] == "wan2.7-i2v-2026-04-25"
    assert payload["input"]["prompt"].startswith("A cinematic hero")
    media = payload["input"]["media"]
    assert media == [{"type": "first_frame", "url": media[0]["url"]}]
    encoded = media[0]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == JPEG
    assert trace["production_shot_id"] == "shot-1"
    assert trace["reference_asset_version_id"] == "version-1"
    assert trace["provider"] == "alibaba_model_studio"
    assert "api_key" not in trace


def test_wan_adapter_submit_status_and_result_download(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(api_key="test-key"),
        reference_resolver=FakeResolver(image),
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
    assert result["content"] == MP4
    assert result["metadata"]["provider_task_id"] == "wan-task-1"
    assert result["metadata"]["sha256"] == hashlib.sha256(MP4).hexdigest()
    assert client.downloaded_url == "https://files.example/video.mp4"


def test_wan_adapter_rejects_missing_reference_and_unsupported_cancel(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(api_key="test-key"),
        reference_resolver=FakeResolver(image),
    )
    class RejectingResolver(FakeResolver):
        def resolve(self, snapshot):
            raise WanAdapterError("no locked reference")

    no_reference = _snapshot(**{"STYLE:look": "style-version"})
    adapter.reference_resolver = RejectingResolver(image)
    assert adapter.validate(no_reference) is False
    with pytest.raises(WanAdapterError, match="not supported"):
        adapter.cancel("wan-task-1")


def test_wan_adapter_rejects_malformed_video_result(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)

    class BadClient(FakeWanClient):
        def download_result(self, url):
            return b"x" * 32

    adapter = WanProductionAdapter(
        BadClient(),
        config=WanProviderConfig(api_key="test-key"),
        reference_resolver=FakeResolver(image),
    )
    adapter.submit(_snapshot())
    adapter.client.status = "SUCCEEDED"
    with pytest.raises(WanAdapterError, match="MP4"):
        adapter.get_result("wan-task-1")


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
            "reference_asset_version_id": "version-1",
            "api_key": "must-not-be-stored",
        },
    )
    metadata = ProductionExecutionService._submission_metadata(submission)
    assert metadata["provider_task_id"] == "wan-task-1"
    assert "api_key" not in metadata


def test_wan_provider_failure_is_not_treated_as_a_result(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    client = FakeWanClient()
    adapter = WanProductionAdapter(
        client,
        config=WanProviderConfig(api_key="test-key"),
        reference_resolver=FakeResolver(image),
    )
    adapter.submit(_snapshot())
    client.status = "FAILED"
    assert adapter.get_status("wan-task-1") == "FAILED"
    with pytest.raises(WanAdapterError, match="before task succeeded"):
        adapter.get_result("wan-task-1")


class FakeReferenceRepository:
    def __init__(self, version, asset):
        self.version = version
        self.asset = asset

    def get_reference_asset_version(self, version_id):
        return self.version if version_id == self.version.id else None

    def get_reference_asset(self, asset_id):
        return self.asset if asset_id == self.asset.id else None


class FakeReferenceService:
    def __init__(self, repository, path):
        self.repository = repository
        self.path = path

    def resolve_version_path(self, project_id, version_id):
        return self.path


def test_reference_resolver_requires_locked_current_version_and_matching_story(tmp_path):
    image = tmp_path / "reference.jpg"
    image.write_bytes(JPEG)
    version = ReferenceAssetVersion(
        id="version-1",
        asset_id="asset-1",
        project_id="project-1",
        version_number=1,
        filename="reference.jpg",
        mime_type="image/jpeg",
        size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
        storage_path="assets/references/reference.jpg",
        metadata={"source_story_revision_id": "story-1"},
        created_at="2026-01-01T00:00:00+00:00",
    )
    asset = ReferenceAsset(
        id="asset-1",
        project_id="project-1",
        asset_type=ReferenceAssetType.CHARACTER_REFERENCE,
        current_version_id="version-1",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    service = FakeReferenceService(FakeReferenceRepository(version, asset), image)
    selection = WanReferenceResolver(service).resolve(_snapshot())
    assert selection.version_id == "version-1"
    assert selection.role == "character"

    stale_asset = asset.model_copy(update={"current_version_id": None})
    stale_service = FakeReferenceService(FakeReferenceRepository(version, stale_asset), image)
    with pytest.raises(WanAdapterError, match="locked current"):
        WanReferenceResolver(stale_service).resolve(_snapshot())


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
    client = WanVideoClient(WanProviderConfig(api_key="secret-key"), session=session)
    assert client.create_task({"model": "wan2.7-i2v-2026-04-25"}) == "wan-task-1"
    assert session.kwargs["headers"]["X-DashScope-Async"] == "enable"
    assert session.kwargs["headers"]["Authorization"] == "Bearer secret-key"
