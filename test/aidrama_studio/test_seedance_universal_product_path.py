from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aidrama_studio.services.ai_capabilities import CapabilityKind, default_capability_registry
from aidrama_studio.services.adapters import (
    MainlandSeedanceAdapterError,
    MainlandSeedanceProductionAdapter,
)
from aidrama_studio.services.adapters.wan_video import WanFirstFrameSelection
from aidrama_studio.services.model_runtime import (
    ArkSeedanceCodec,
    CapabilityKind as UniversalCapabilityKind,
    CapabilityRequest,
    CodecError,
    AsyncTaskDriver,
    DriverResponse,
    DurationSpec,
    DriverSubmission,
    ProtocolFamily,
    RuntimeOutcome,
    build_mainland_manifests,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    MAINLAND_COMPATIBILITY_MANIFEST_IDS,
)
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.services.runtime_foundation import RuntimePlanService
from aidrama_studio.services.production_runtime_resolver import ProductionRuntimeResolver
from aidrama_studio.domain import ProviderTask
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_mainland_wan_adapter import (
    _first_frame,
    _plan,
    _snapshot,
)
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch):
    monkeypatch.setenv("REAL_PROVIDER_CALLS", "0")
    monkeypatch.setenv("PAID_CALLS", "0")
    monkeypatch.setenv("AIDRAMA_TEST_NO_NETWORK", "1")


class _CredentialStore:
    def configured_providers(self):
        return ("ARK_API_KEY",)

    def get(self, key):
        return "offline-ark-key" if key == "ARK_API_KEY" else None


class _Runtime:
    instances: list["_Runtime"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.__class__.instances.append(self)

    def submit(self, request, *, authorization):
        assert authorization["approved"] is True
        self.requests.append(request)
        return DriverSubmission(
            request_id=request.request_id,
            protocol_reference="seedance-universal-task-1",
            provider_task_id="seedance-universal-task-1",
        )

    def poll(self, manifest_id, reference, *, request=None):
        del manifest_id, reference, request
        return type("Status", (), {"outcome": RuntimeOutcome.RUNNING})()


class _FrameResolver:
    def __init__(self, path: Path):
        frame = _first_frame()
        self.selection = WanFirstFrameSelection(
            first_frame_id=frame.id,
            artifact_id=frame.artifact_id,
            source_type=frame.source_type.value,
            path=path,
            mime_type=frame.mime_type,
            sha256=frame.sha256,
            size_bytes=frame.artifact_size_bytes,
            shot_id=frame.shot_id,
            identity_reference_version_ids=("version-1",),
        )

    def resolve(self, snapshot):
        self.selection.validate_snapshot(snapshot)
        return self.selection


def _seedance_plan():
    manifest = next(
        item
        for item in build_mainland_manifests()
        if item.id == MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"]
    )
    plan = _plan().model_copy(
        update={
            "provider_id": "SEEDANCE",
            "model_id": manifest.model_id,
            "endpoint_profile_id": "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING",
            "endpoint_class": "ARK_CN_BEIJING",
            "credential_reference": "ARK_API_KEY",
            "provider_parameters": {
                "manifest_id": manifest.id,
                "manifest_hash": manifest.manifest_hash,
                "codec_id": manifest.codec_id,
                "provider_resolution": "720p",
                "ratio": "16:9",
            },
            "reference_version_ids": (),
            "reference_roles": {},
            "authorization": {
                "approved": True,
                "max_paid_attempts": 1,
                "authorization_fingerprint": "fingerprint-1",
            },
        }
    )
    return plan, manifest


def _adapter(tmp_path: Path, *, approved: bool = True):
    snapshot = _snapshot()
    plan, manifest = _seedance_plan()
    if not approved:
        plan = plan.model_copy(
            update={"authorization": {"approved": False, "max_paid_attempts": 1}}
        )
    # The fixture's frozen frame hash is tied to its PNG bytes; use those exact
    # bytes at a temporary path so no Reference Asset can be promoted.
    path = tmp_path / "shot-first-frame.png"
    path.write_bytes(png_bytes())
    adapter = MainlandSeedanceProductionAdapter(
        repository=object(),
        paths=type("Paths", (), {"root": tmp_path})(),
        credential_store=_CredentialStore(),
        runtime_factory=_Runtime,
        runtime_plan=plan,
        provider_task=None,
        env={"AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1"},
        first_frame_resolver=_FrameResolver(path),
    )
    # The manifest is asserted here to make accidental endpoint-only routing
    # visible in this focused fixture.
    assert manifest.model_id == "doubao-seedance-2-5-260628"
    return adapter, snapshot, plan


def test_settings_selection_freezes_exact_seedance_manifest_and_model(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    brief = __import__(
        "aidrama_studio.services", fromlist=["GenerationBriefService"]
    ).GenerationBriefService(repository).prepare_for_job(project.id, job.id)[0]
    service = SettingsModelService(repository)
    option = next(
        item
        for item in service.inventory(UniversalCapabilityKind.VIDEO)
        if item.model_id == "doubao-seedance-2-5-260628"
    )
    service.save_selections(project_id=None, selections={UniversalCapabilityKind.VIDEO: option.manifest_id})
    plan = RuntimePlanService(repository).create_from_selection(
        project.id,
        production_job_id=job.id,
        brief=brief,
        capability=UniversalCapabilityKind.VIDEO,
        selection_service=service,
        resolution="1280x720",
        provider_generation_duration=30,
        target_creative_duration=30,
        authorization={"approved": True, "max_paid_attempts": 1},
    )
    assert plan.provider_id == "SEEDANCE"
    assert plan.model_id == "doubao-seedance-2-5-260628"
    assert plan.provider_parameters["manifest_id"] == option.manifest_id
    assert plan.provider_parameters["manifest_hash"] == option.manifest_hash
    assert plan.provider_parameters["codec_id"] == "ark.seedance.v1"


def test_universal_seedance_bridge_sends_exact_shot_first_frame_and_not_reference(tmp_path):
    _Runtime.instances.clear()
    adapter, snapshot, plan = _adapter(tmp_path)
    updated = snapshot.model_copy(
        update={
            "runtime_plan_id": plan.id,
            "runtime_plan_hash": plan.plan_hash,
        }
    )
    submission = adapter.submit(updated)
    request = _Runtime.instances[-1].requests[0]
    assert submission.runtime_reference == "seedance-universal-task-1"
    assert request.manifest_id == plan.provider_parameters["manifest_id"]
    assert request.model_id == "doubao-seedance-2-5-260628"
    assert request.inputs[0].role == "first_frame"
    assert request.inputs[0].source_kind == "SHOT_FIRST_FRAME_ARTIFACT"
    assert request.inputs[0].source_id == _first_frame().artifact_id
    assert request.inputs[0].sha256 == _first_frame().sha256
    assert len(request.inputs) == 1
    assert request.provider_parameters["duration_seconds"] == 5
    assert submission.metadata["first_frame_artifact_id"] == _first_frame().artifact_id


def test_seedance_manifest_duration_authority_and_auth_gate(tmp_path):
    adapter, snapshot, plan = _adapter(tmp_path)
    updated = snapshot.model_copy(
        update={"runtime_plan_id": plan.id, "runtime_plan_hash": plan.plan_hash}
    )
    assert adapter.validate(updated) is True
    assert plan.provider_generation_duration == 5
    adapter.runtime_plan = plan.model_copy(update={"provider_generation_duration": 31})
    assert adapter.validate(updated) is False

    _Runtime.instances.clear()
    blocked, blocked_snapshot, blocked_plan = _adapter(tmp_path, approved=False)
    blocked_snapshot = blocked_snapshot.model_copy(
        update={
            "runtime_plan_id": blocked_plan.id,
            "runtime_plan_hash": blocked_plan.plan_hash,
        }
    )
    assert blocked.validate(blocked_snapshot) is False
    with pytest.raises(MainlandSeedanceAdapterError, match="authorized create"):
        blocked.submit(blocked_snapshot)
    assert _Runtime.instances == []


def test_ark_codec_reads_duration_bounds_from_selected_manifest():
    manifest = next(
        item
        for item in build_mainland_manifests()
        if item.id == MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"]
    )
    constrained = replace(manifest, duration=DurationSpec(minimum=6, maximum=12))
    request = CapabilityRequest(
        request_id="duration-authority",
        capability=UniversalCapabilityKind.VIDEO,
        protocol_family=ProtocolFamily.ASYNC_TASK,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=constrained.manifest_hash,
        codec_id=manifest.codec_id,
        prompt_or_text="shot",
        provider_parameters={"duration_seconds": 12},
    )
    ArkSeedanceCodec().validate(request, constrained)
    with pytest.raises(CodecError, match="6 to 12"):
        ArkSeedanceCodec().validate(
            replace(request, provider_parameters={"duration_seconds": 5}),
            constrained,
        )


def test_seedance_reconciliation_never_creates_a_second_task():
    manifest = next(
        item
        for item in build_mainland_manifests()
        if item.id == MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"]
    )

    class Transport:
        def __init__(self):
            self.create_count = 0

        def create(self, _request, _context=None):
            self.create_count += 1
            return DriverResponse(
                {"output": {"task_id": "uncertain-task-1"}}, status_code=200
            )

        def poll(self, _reference, _context=None):
            return DriverResponse(
                {"output": {"task_id": "uncertain-task-1", "status": "RUNNING"}},
                status_code=200,
            )

    request = CapabilityRequest(
        request_id="uncertain-create",
        capability=UniversalCapabilityKind.VIDEO,
        protocol_family=ProtocolFamily.ASYNC_TASK,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
        prompt_or_text="shot",
        provider_parameters={"duration_seconds": 4},
    )
    transport = Transport()
    driver = AsyncTaskDriver(transport, manifest=manifest)
    codec = ArkSeedanceCodec()
    first = driver.submit(request, codec, manifest, authorization={"approved": True})
    assert first.protocol_reference == "uncertain-task-1"
    reconciled = driver.submit(
        request,
        codec,
        manifest,
        existing_reference="uncertain-task-1",
    )
    assert reconciled.outcome is RuntimeOutcome.RUNNING
    assert transport.create_count == 1


def test_default_video_registry_exposes_seedance_as_universal_bridge(tmp_path):
    registry = default_capability_registry(
        env={"ARK_API_KEY": "offline", "AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1"}
    )
    provider = next(
        item
        for item in registry.list(CapabilityKind.VIDEO_GENERATIVE)
        if item.provider_name == "SEEDANCE"
    )
    assert isinstance(provider.adapter, MainlandSeedanceProductionAdapter)
    assert provider.status.metadata["model"] == "doubao-seedance-2-5-260628"
    assert provider.status.metadata["endpoint_profile_id"] == (
        "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING"
    )


def test_production_runtime_resolver_binds_seedance_plan_without_wan_fallback(tmp_path):
    registry = default_capability_registry(
        env={"ARK_API_KEY": "offline", "AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1"}
    )
    plan, _manifest = _seedance_plan()
    frame = _first_frame()
    plan = plan.model_copy(
        update={
            "authorization": {
                "approved": True,
                "max_paid_attempts": 1,
                "shot_first_frame": {
                    "first_frame_id": frame.id,
                    "artifact_id": frame.artifact_id,
                    "sha256": frame.sha256,
                    "source_type": frame.source_type.value,
                },
            }
        }
    )
    task = ProviderTask(
        id="task-1",
        project_id="project-1",
        execution_id="execution-1",
        capability="VIDEO_GENERATIVE",
        provider_id="SEEDANCE",
        model_id="doubao-seedance-2-5-260628",
        idempotency_key="production:execution-1",
        state="QUEUED",
        request_summary={"approved": True},
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )
    adapter = ProductionRuntimeResolver(registry=registry, repository=object()).resolve(
        task, plan
    )
    assert isinstance(adapter, MainlandSeedanceProductionAdapter)
    assert adapter.model_id == "doubao-seedance-2-5-260628"
