from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aidrama_studio.domain import ProviderTask, ProductionInputSnapshot, RuntimePlan
from aidrama_studio.services.adapters.mainland_wan import (
    MainlandWanAdapterError,
    MainlandWanProductionAdapter,
)
from aidrama_studio.services.adapters.wan_video import WanReferenceSelection
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityResult,
    DriverStatus,
    DriverSubmission,
    RuntimeOutcome,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    build_mainland_manifests,
)
from aidrama_studio.storage.database import DatabasePaths
from test.aidrama_studio.image_fixtures import png_bytes


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"video"


class _CredentialStore:
    def __init__(
        self,
        *,
        secret: str = "unit-test-secret",
        workspace_base_url: str | None = None,
    ) -> None:
        self.read_count = 0
        self.presence_count = 0
        self.values = {"DASHSCOPE_API_KEY": secret}
        if workspace_base_url:
            self.values["DASHSCOPE_WORKSPACE_BASE_URL"] = workspace_base_url

    def configured_providers(self):
        self.presence_count += 1
        return tuple(self.values)

    def get(self, key):
        self.read_count += 1
        return self.values.get(key)


class _ReferenceResolver:
    def __init__(self, path: Path) -> None:
        self.selection = WanReferenceSelection(
            role="character",
            binding_key="CHARACTER:hero",
            version_id="version-1",
            path=path,
            mime_type="image/png",
        )

    def resolve(self, snapshot):
        assert snapshot.reference_asset_versions["CHARACTER:hero"] == "version-1"
        return self.selection


class _OfflineMainlandWanRuntime:
    instances = []
    submit_count = 0

    def __init__(
        self,
        *,
        credentials,
        create_authorized,
        artifact_sink,
        input_resolver=None,
        dashscope_workspace_base_url=None,
    ) -> None:
        assert set(credentials) == {"DASHSCOPE_API_KEY"}
        assert credentials["DASHSCOPE_API_KEY"]
        self.create_authorized = create_authorized
        self.artifact_sink = artifact_sink
        self.input_resolver = input_resolver
        self.dashscope_workspace_base_url = dashscope_workspace_base_url
        self.requests = []
        self.poll_references = []
        self.outcome = RuntimeOutcome.SUBMITTED
        self.manifest = next(
            item
            for item in build_mainland_manifests(
                credential_presence={"DASHSCOPE_API_KEY": True},
                create_authorized=True,
                artifact_sink_available=True,
            )
            if item.capability is CapabilityKind.VIDEO
        )
        self.__class__.instances.append(self)

    def primary_manifest(self, capability):
        assert CapabilityKind.coerce(capability) is CapabilityKind.VIDEO
        return self.manifest

    def submit(self, request, *, authorization):
        assert self.create_authorized is True
        assert authorization["approved"] is True
        self.__class__.submit_count += 1
        self.requests.append(request)
        return DriverSubmission(
            request_id=request.request_id,
            protocol_reference="wan-task-1",
            provider_task_id="wan-task-1",
        )

    def poll(self, manifest_id, reference, *, request=None):
        assert manifest_id == "mainland:alibaba:wan2.7-i2v-2026-04-25:v1"
        self.poll_references.append((reference, request.request_id if request else None))
        return DriverStatus(protocol_reference=reference, outcome=self.outcome)

    def fetch_result(self, manifest_id, reference, *, request):
        assert manifest_id == self.manifest.id
        assert reference == "wan-task-1"
        output = self.artifact_sink.persist_bytes(
            MP4,
            request_id=request.request_id,
            role="generated_video",
            mime_type="video/mp4",
            safe_metadata={"provider": "alibaba_model_studio"},
        )
        return CapabilityResult(
            request_id=request.request_id,
            protocol_reference=reference,
            provider_task_id=reference,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(output,),
        )


class _RecoveryRepository:
    def __init__(self, snapshot: ProductionInputSnapshot) -> None:
        self.snapshot = snapshot

    def get_production_execution(self, execution_id):
        assert execution_id == "execution-1"
        return SimpleNamespace(input_snapshot=self.snapshot)


def _paths(tmp_path: Path) -> DatabasePaths:
    return DatabasePaths(
        tmp_path / "db" / "aidrama.db",
        tmp_path / "db" / "projects",
        tmp_path / "db" / "archived",
    )


def _plan(*, approved: bool = True) -> RuntimePlan:
    return RuntimePlan(
        id="runtime-plan-1",
        project_id="project-1",
        production_job_id="job-1",
        execution_id="execution-1",
        provider_capability="VIDEO_GENERATIVE",
        provider_id="WAN_VIDEO",
        model_id="wan2.7-i2v-2026-04-25",
        endpoint_profile_id="DASHSCOPE_CN_BEIJING_V1",
        deployment_region="MAINLAND_CHINA",
        endpoint_class="DASHSCOPE_CN",
        credential_reference="DASHSCOPE_API_KEY",
        selection_source="PROJECT_PROFILE",
        transmitted_content_types=("PROMPT", "REFERENCE_IMAGE"),
        estimated_request_count=1,
        generation_mode="REFERENCE_I2V",
        native_generation_resolution="720P",
        native_generation_fps=24.0,
        delivery_width=720,
        delivery_height=1280,
        target_fps=24.0,
        delivery_strategy="NATIVE",
        quality_mode="STANDARD",
        provider_generation_duration=5,
        target_creative_duration=5,
        duration_strategy="EXACT",
        audio_strategy="EXTERNAL_TTS",
        provider_parameters={"provider_resolution": "720P"},
        reference_version_ids=("version-1",),
        reference_roles={"version-1": "first_frame"},
        continuity_strategy="REFERENCE_ONLY",
        generation_brief_hash="1" * 64,
        output_profile_hash="2" * 64,
        authorization={
            "approved": approved,
            "max_paid_attempts": 1,
            "authorization_fingerprint": "fingerprint-1",
        },
        prompt_template_version="mainland-wan-i2v-v1",
        plan_hash="3" * 64,
        created_at="2026-08-27T00:00:00+00:00",
    )


def _snapshot() -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        project_id="project-1",
        story_revision_id="story-1",
        script_revision_id="script-1",
        shot_plan_revision_id="shot-plan-1",
        runtime_plan_id="runtime-plan-1",
        runtime_plan_hash="3" * 64,
        reference_asset_versions={"CHARACTER:hero": "version-1"},
        shot_parameters={
            "shot-1": {
                "visual_intent": "Rainy bus terminal at night",
                "subject": ["hero"],
                "action": "looks through the wet windshield",
                "shot_size": "MEDIUM",
                "camera_movement": "STATIC",
                "duration_seconds": 5,
                "lighting": {"quality": "restrained", "tone": "moody"},
            }
        },
    )


def _adapter(
    tmp_path: Path,
    *,
    plan=None,
    provider_task=None,
    repository=None,
    credential_store=None,
):
    image = tmp_path / "reference.png"
    image.write_bytes(png_bytes())
    store = credential_store or _CredentialStore()
    adapter = MainlandWanProductionAdapter(
        repository=repository or object(),
        paths=_paths(tmp_path),
        credential_store=store,
        runtime_factory=_OfflineMainlandWanRuntime,
        runtime_plan=plan or _plan(),
        provider_task=provider_task,
        env={"AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1"},
    )
    adapter.reference_resolver = _ReferenceResolver(image)
    return adapter, store


def test_status_and_validation_do_not_read_credential_value(tmp_path):
    adapter, store = _adapter(tmp_path)

    assert adapter.status.configured is True
    assert adapter.status.available is True
    assert adapter.validate(_snapshot()) is True
    assert store.presence_count >= 2
    assert store.read_count == 0


def test_submit_uses_exact_mainland_manifest_one_reference_and_one_create(tmp_path):
    _OfflineMainlandWanRuntime.instances.clear()
    _OfflineMainlandWanRuntime.submit_count = 0
    adapter, store = _adapter(tmp_path)

    submission = adapter.submit(_snapshot())

    assert store.read_count == 1
    assert _OfflineMainlandWanRuntime.submit_count == 1
    assert submission.runtime_reference == "wan-task-1"
    assert submission.metadata["manifest_id"] == (
        "mainland:alibaba:wan2.7-i2v-2026-04-25:v1"
    )
    assert submission.metadata["endpoint_profile_id"] == "DASHSCOPE_CN_BEIJING_V1"
    assert submission.metadata["model"] == "wan2.7-i2v-2026-04-25"
    runtime = _OfflineMainlandWanRuntime.instances[-1]
    request = runtime.requests[0]
    assert request.manifest_id == submission.metadata["manifest_id"]
    assert request.model_id == "wan2.7-i2v-2026-04-25"
    assert len(request.inputs) == 1
    assert request.inputs[0].role == "first_frame"
    assert request.inputs[0].source_id == "version-1"
    assert dict(request.provider_parameters) == {
        "duration_seconds": 5,
        "resolution": "720P",
    }
    assert request.authorization_required is True
    assert request.create_authorized is True


def test_workspace_key_and_base_url_are_pinned_into_wan_runtime(tmp_path):
    _OfflineMainlandWanRuntime.instances.clear()
    workspace_base_url = (
        "https://ws-unit-test.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    store = _CredentialStore(
        secret="sk-ws-unit-test",
        workspace_base_url=workspace_base_url,
    )
    adapter, _store = _adapter(tmp_path, credential_store=store)

    adapter.submit(_snapshot())

    runtime = _OfflineMainlandWanRuntime.instances[-1]
    assert runtime.dashscope_workspace_base_url == workspace_base_url


def test_workspace_key_without_base_url_stops_before_wan_runtime(tmp_path):
    _OfflineMainlandWanRuntime.instances.clear()
    _OfflineMainlandWanRuntime.submit_count = 0
    adapter, _store = _adapter(
        tmp_path,
        credential_store=_CredentialStore(secret="sk-ws-unit-test"),
    )

    with pytest.raises(MainlandWanAdapterError, match="workspace Base URL"):
        adapter.submit(_snapshot())

    assert _OfflineMainlandWanRuntime.instances == []
    assert _OfflineMainlandWanRuntime.submit_count == 0


def test_submit_requires_explicit_authorization_and_never_creates_when_blocked(
    tmp_path,
):
    _OfflineMainlandWanRuntime.instances.clear()
    _OfflineMainlandWanRuntime.submit_count = 0
    adapter, store = _adapter(tmp_path, plan=_plan(approved=False))

    assert adapter.validate(_snapshot()) is False
    with pytest.raises(MainlandWanAdapterError, match="authorized create"):
        adapter.submit(_snapshot())

    assert store.read_count == 0
    assert _OfflineMainlandWanRuntime.submit_count == 0
    assert _OfflineMainlandWanRuntime.instances == []


def test_poll_and_result_are_content_addressed_without_provider_url(tmp_path):
    _OfflineMainlandWanRuntime.instances.clear()
    adapter, _store = _adapter(tmp_path)
    adapter.submit(_snapshot())
    runtime = _OfflineMainlandWanRuntime.instances[-1]

    assert adapter.get_status("wan-task-1") == "QUEUED"
    runtime.outcome = RuntimeOutcome.RUNNING
    assert adapter.get_status("wan-task-1") == "RUNNING"
    runtime.outcome = RuntimeOutcome.SUCCEEDED
    assert adapter.get_status("wan-task-1") == "SUCCEEDED"

    result = adapter.get_result("wan-task-1")
    path = Path(result["path"])
    assert path.read_bytes() == MP4
    assert path.name == f"{result['metadata']['sha256']}.mp4"
    assert result["metadata"]["content_addressed"] is True
    assert "url" not in str(result).casefold()
    assert "signature" not in str(result).casefold()


def test_cold_recovery_polls_existing_task_without_new_create(tmp_path):
    _OfflineMainlandWanRuntime.instances.clear()
    _OfflineMainlandWanRuntime.submit_count = 0
    snapshot = _snapshot()
    task = ProviderTask(
        id="provider-task-1",
        project_id="project-1",
        execution_id="execution-1",
        capability="VIDEO_GENERATIVE",
        provider_id="WAN_VIDEO",
        model_id="wan2.7-i2v-2026-04-25",
        idempotency_key="production:execution-1",
        provider_task_id="wan-task-1",
        state="PROVIDER_RUNNING",
        request_summary={"approved": True},
        metadata={"request_id": "request-fixed"},
        created_at="2026-08-27T00:00:00+00:00",
        updated_at="2026-08-27T00:00:00+00:00",
    )
    adapter, store = _adapter(
        tmp_path,
        provider_task=task,
        repository=_RecoveryRepository(snapshot),
    )
    adapter.env = {}

    assert adapter.status.available is False
    assert adapter.status.metadata["supports_poll_without_paid_create_authorization"]
    assert adapter.get_status("wan-task-1") == "QUEUED"

    runtime = _OfflineMainlandWanRuntime.instances[-1]
    assert runtime.create_authorized is False
    assert runtime.poll_references == [("wan-task-1", "request-fixed")]
    assert _OfflineMainlandWanRuntime.submit_count == 0
    assert store.read_count == 1
