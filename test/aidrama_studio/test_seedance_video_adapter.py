from __future__ import annotations

import base64
import io

import pytest

from aidrama_studio.domain import ProductionInputSnapshot
from aidrama_studio.services import (
    GenerationBriefCompiler,
    ProductionExecutionService,
    ReferenceAssetService,
    RuntimePlanService,
)
from aidrama_studio.services.adapters.seedance_video import (
    DEFAULT_SEEDANCE_MODEL,
    SEEDANCE_TASK_PATH,
    SeedanceAdapterError,
    SeedanceProductionAdapter,
    SeedanceProviderConfig,
)
from aidrama_studio.services.streaming_artifact import StreamingArtifactSource
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class Client:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.task = {"id": "seedance-task-1", "status": "waiting"}

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"id": "seedance-task-1", "status": "waiting"})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return Response(self.task)


class FakeDownloader:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def source(self, url, **_kwargs):
        self.urls.append(url)
        return StreamingArtifactSource(lambda sink: sink.write(self.payload), 1024)


def _frozen_seedance_context(tmp_path, *, provider_parameters=None):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    full = ProductionExecutionService(repository).create_input_snapshot(project.id, job.id)
    brief = GenerationBriefCompiler(repository).compile(project.id, job.id, "shot_001")
    references = dict(full.reference_asset_versions)
    ordered_bindings = ["LOCATION:loc_001", "CHARACTER:char_001"]
    ordered_versions = [references[item] for item in ordered_bindings]
    plan = RuntimePlanService(repository).create(
        project.id,
        production_job_id=job.id,
        brief=brief,
        provider_capability="VIDEO_GENERATIVE",
        provider_id="SEEDANCE",
        model_id=DEFAULT_SEEDANCE_MODEL,
        provider_generation_duration=5,
        target_creative_duration=2,
        audio_strategy="NATIVE_PROVIDER_AUDIO",
        provider_parameters={
            "provider_resolution": "720p",
            "ratio": "16:9",
            "watermark": False,
            "camera_fixed": True,
            "unknown_internal_parameter": "must-not-pass-through",
            **dict(provider_parameters or {}),
        },
        reference_version_ids=ordered_versions,
        reference_roles=dict(zip(ordered_versions, ordered_bindings)),
        continuity_strategy="PREVIOUS_LAST_FRAME",
        authorization={"approved": True, "authorization_id": "auth-1"},
        prompt_template_version="seedance-v1",
    )
    shot_parameters = {"shot_001": full.shot_parameters["shot_001"]}
    snapshot = ProductionInputSnapshot(
        project_id=project.id,
        story_revision_id=full.story_revision_id,
        script_revision_id=full.script_revision_id,
        shot_plan_revision_id=full.shot_plan_revision_id,
        runtime_plan_id=plan.id,
        generation_brief_id=brief.id,
        runtime_plan_hash=plan.plan_hash,
        reference_asset_versions=full.reference_asset_versions,
        shot_parameters=shot_parameters,
    )
    return repository, project, job, snapshot, plan, brief, ordered_bindings


def _adapter(repository, plan, brief, *, client=None, downloader=None, image_downloader=None):
    return SeedanceProductionAdapter(
        SeedanceProviderConfig(
            api_key="test-key",
            allow_paid_live_tests=True,
            result_hosts=("cdn.provider.example",),
        ),
        client=client or Client(),
        runtime_plan=plan,
        generation_brief=brief,
        output_profile=repository.get_output_profile(plan.output_profile_id),
        reference_service=ReferenceAssetService(repository),
        downloader=downloader,
        image_downloader=image_downloader,
    )


def test_seedance_official_contract_uses_typed_content_and_frozen_reference_order(tmp_path):
    repository, _project, _job, snapshot, plan, brief, ordered_bindings = (
        _frozen_seedance_context(tmp_path)
    )
    client = Client()
    adapter = _adapter(repository, plan, brief, client=client)

    submission = adapter.submit(snapshot)

    assert submission.runtime_reference == "seedance-task-1"
    url, request = client.posts[0]
    assert url == "https://ark.cn-beijing.volces.com/api/v3" + SEEDANCE_TASK_PATH
    assert request["headers"] == {"Authorization": "Bearer test-key"}
    payload = request["json"]
    assert payload["model"] == "doubao-seedance-2-5-260628"
    assert payload["content"][0]["type"] == "text"
    assert brief.action in payload["content"][0]["text"]
    assert [item["type"] for item in payload["content"][1:]] == [
        "image_url",
        "image_url",
    ]
    assert [item["role"] for item in payload["content"][1:]] == [
        "reference_image",
        "reference_image",
    ]
    assert payload["resolution"] == "720p"
    assert payload["ratio"] == "16:9"
    assert payload["duration"] == 5
    assert payload["generate_audio"] is True
    assert payload["camera_fixed"] is True
    assert payload["return_last_frame"] is True
    assert "unknown_internal_parameter" not in payload
    assert "metadata" not in payload

    actual = submission.metadata["provider_references_actually_used"]
    assert [item["binding_key"] for item in actual] == ordered_bindings
    assert [item["order"] for item in actual] == [1, 2]
    assert submission.metadata["snapshot_references_available"] == dict(
        snapshot.reference_asset_versions
    )
    assert "data:image" not in repr(submission.metadata)
    first_data_uri = payload["content"][1]["image_url"]["url"]
    assert base64.b64decode(first_data_uri.split(",", 1)[1])


def test_seedance_frames_replaces_duration_and_unknown_parameters_do_not_leak(tmp_path):
    repository, _project, _job, snapshot, plan, brief, _bindings = (
        _frozen_seedance_context(tmp_path, provider_parameters={"frames": 121})
    )
    client = Client()
    adapter = _adapter(repository, plan, brief, client=client)
    adapter.submit(snapshot)
    payload = client.posts[0][1]["json"]
    assert payload["frames"] == 121
    assert "duration" not in payload


def test_seedance_rejects_provenance_drift_and_missing_live_authorization_before_post(tmp_path):
    repository, _project, _job, snapshot, plan, brief, _bindings = (
        _frozen_seedance_context(tmp_path)
    )
    client = Client()
    adapter = _adapter(repository, plan, brief, client=client)
    drifted = snapshot.model_copy(update={"runtime_plan_hash": "0" * 64})
    assert adapter.validate(drifted) is False
    with pytest.raises(SeedanceAdapterError, match="provenance"):
        adapter.submit(drifted)
    assert client.posts == []

    blocked = SeedanceProductionAdapter(
        SeedanceProviderConfig(api_key="test-key", allow_paid_live_tests=False),
        client=client,
        runtime_plan=plan,
        generation_brief=brief,
        reference_service=ReferenceAssetService(repository),
    )
    with pytest.raises(SeedanceAdapterError, match="显式付费授权"):
        blocked.submit(snapshot)
    assert client.posts == []


def test_seedance_result_is_streamed_without_persisting_signed_urls(tmp_path):
    repository, _project, _job, snapshot, plan, brief, _bindings = (
        _frozen_seedance_context(tmp_path)
    )
    client = Client()
    video = FakeDownloader(b"video-bytes")
    image = FakeDownloader(b"image-bytes")
    adapter = _adapter(
        repository,
        plan,
        brief,
        client=client,
        downloader=video,
        image_downloader=image,
    )
    adapter.submit(snapshot)
    client.task = {
        "id": "seedance-task-1",
        "status": "succeeded",
        "content": {
            "video_url": "https://cdn.provider.example/video.mp4?Signature=secret",
            "last_frame_url": "https://cdn.provider.example/frame.png?Token=secret",
        },
    }

    result = adapter.get_result("seedance-task-1")

    assert video.urls and image.urls
    assert "Signature" not in repr(result)
    assert "Token" not in repr(result)
    assert len(result["artifacts"]) == 2
    sinks = []
    for artifact in result["artifacts"]:
        sink = io.BytesIO()
        artifact["stream_source"].write_to(sink)
        sinks.append(sink.getvalue())
        assert "url" not in repr(artifact["metadata"]).lower()
    assert sinks == [b"video-bytes", b"image-bytes"]


def test_seedance_status_result_shape_and_cancel_are_truthful(tmp_path):
    repository, _project, _job, _snapshot, plan, brief, _bindings = (
        _frozen_seedance_context(tmp_path)
    )
    adapter = _adapter(repository, plan, brief)
    for raw, expected in (
        ("waiting", "QUEUED"),
        ("processing", "RUNNING"),
        ("succeeded", "SUCCEEDED"),
        ("failed", "FAILED"),
        ("cancelled", "CANCELLED"),
    ):
        assert adapter.map_status(raw) == expected
    with pytest.raises(SeedanceAdapterError, match="cancel"):
        adapter.cancel("seedance-task-1")

    adapter._client.task = {"status": "succeeded", "content": {}}
    with pytest.raises(SeedanceAdapterError, match="video_url"):
        adapter.get_result("seedance-task-1")
