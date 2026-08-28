"""Blocking Universal Runtime architecture regressions.

The tests in this module are deliberately product-boundary tests.  They use
temporary SQLite state and in-process transports/providers only; no test may
turn a credential-shaped sentinel into a real request.  A red assertion is
reported with the expected/actual semantic contract so it can be triaged as a
product defect without weakening the gate.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import pytest
import requests

from aidrama_studio.domain import ProviderTask
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind as LegacyCapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    RuntimeVideoProvider,
)
from aidrama_studio.services.adapters import (
    SeedanceProductionAdapter,
    SeedanceProviderConfig,
)
from aidrama_studio.services.model_runtime import (
    ARK_CN_BEIJING_ENDPOINT_PROFILE,
    AsyncTaskDriver,
    ArkSeedanceCodec,
    CapabilityKind,
    CapabilityRequest,
    ContentRef,
    InMemoryManifestRegistry,
    MainlandProviderRuntime,
    ModelResolver,
    ProtocolFamily,
    build_mainland_codecs,
    build_mainland_manifests,
    CodecError,
)
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.services.production_queue import ProductionQueueService
from aidrama_studio.services.production_runtime_resolver import (
    ProductionRuntimeResolver,
)
from aidrama_studio.services.provider_profiles import ProviderProfileService
from aidrama_studio.services.runtime_foundation import (
    GenerationBriefCompiler,
    RuntimePlanService,
)
from aidrama_studio.services.llm_runtime import LLMInvocationGateway
from aidrama_studio.services.creative_pipeline import CreativePipelineService
from aidrama_studio.services.project import ProjectService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_creative_pipeline import (
    _approved_intake,
    _plan,
    _script,
    _story,
)
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)


@pytest.fixture(autouse=True)
def _offline_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Make accidental network/paid-provider use an immediate test failure."""

    monkeypatch.setenv("AIDRAMA_TEST_NO_NETWORK", "1")
    monkeypatch.setenv("REAL_PROVIDER_CALLS", "0")
    monkeypatch.setenv("PAID_CALLS", "0")
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(tmp_path / "aidrama-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "REAL_PROVIDER_CALLS=1 / PAID_CALLS=1: network is forbidden in this regression shard"
        )

    # Patch the common connection entry points.  In-process fake sessions used
    # below do not inherit these methods and therefore remain usable.
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def _diagnostic(
    *,
    expected: object,
    actual: object,
    files: str,
    contract: str,
) -> str:
    return (
        f"EXPECTED={expected}\n"
        f"ACTUAL={actual}\n"
        f"LIKELY_PRODUCT_FILES={files}\n"
        f"SEMANTIC_CONTRACT={contract}"
    )


def _manifest_by_model(manifests: tuple[object, ...], model_id: str) -> object:
    return next(item for item in manifests if getattr(item, "model_id", None) == model_id)


def _static_manifest_payload(manifest: object) -> dict[str, object]:
    payload = dict(manifest.canonical_payload())
    payload.pop("readiness", None)
    return payload


def test_manifest_contract_hash_is_stable_across_mutable_readiness_changes():
    """Credential/sink/authorization readiness is mutable, not identity."""

    cold = _manifest_by_model(
        build_mainland_manifests(
            credential_presence={},
            create_authorized=False,
            artifact_sink_available=False,
        ),
        "wan2.7-i2v-2026-04-25",
    )
    ready = _manifest_by_model(
        build_mainland_manifests(
            credential_presence={"DASHSCOPE_API_KEY": True},
            create_authorized=True,
            artifact_sink_available=True,
        ),
        "wan2.7-i2v-2026-04-25",
    )

    assert cold.readiness != ready.readiness
    assert _static_manifest_payload(cold) == _static_manifest_payload(ready)
    assert cold.manifest_hash == ready.manifest_hash, _diagnostic(
        expected="same provider/model/protocol/codec/endpoint contract has one stable hash",
        actual={"cold": cold.manifest_hash, "ready": ready.manifest_hash},
        files="aidrama_studio/services/model_runtime/manifest.py; aidrama_studio/services/model_runtime/readiness.py",
        contract="mutable configured/runtime_available/create_authorized state must not participate in frozen contract identity",
    )


def test_shared_endpoint_frozen_identity_never_resolves_the_first_model():
    """A frozen model ID remains exact even when endpoint/profile is shared."""

    manifests = build_mainland_manifests(
        credential_presence={"DASHSCOPE_API_KEY": True},
        create_authorized=True,
        artifact_sink_available=True,
    )
    # Deliberately put Qwen Image first: an endpoint-only resolver would pick
    # it instead of the explicitly frozen Z-Image model.
    qwen = _manifest_by_model(manifests, "qwen-image-3.0")
    z_image = _manifest_by_model(manifests, "z-image-turbo")
    registry = InMemoryManifestRegistry((qwen, z_image))
    resolver = ModelResolver(registry)

    frozen_selection = resolver.resolve(
        capability=CapabilityKind.IMAGE,
        manifest_id=z_image.id,
        provider_id=z_image.provider_id,
        endpoint_profile_id=z_image.endpoint_profile_id,
        deployment_region=z_image.deployment_region,
        require_available=True,
    )
    frozen = frozen_selection.freeze()
    assert frozen.model_id == "z-image-turbo"

    later = resolver.resolve(
        capability=CapabilityKind.IMAGE,
        frozen_identity=frozen,
        require_available=True,
    )
    assert later.model_id == "z-image-turbo", _diagnostic(
        expected="z-image-turbo",
        actual=later.model_id,
        files="aidrama_studio/services/model_runtime/resolver.py; aidrama_studio/services/model_settings.py",
        contract="provider + endpoint is not a model identity; every frozen model dimension must be matched exactly",
    )
    assert later.manifest_id == frozen.manifest_id
    assert later.provider_id == frozen.provider_id
    assert later.endpoint_profile_id == frozen.endpoint_profile_id


@pytest.mark.parametrize(
    ("model_id", "valid_duration", "invalid_duration", "minimum", "maximum"),
    [
        ("wan2.7-i2v-2026-04-25", 3, 20, 2.0, 15.0),
        ("doubao-seedance-2-5-260628", 20, 3, 4.0, 30.0),
    ],
)
def test_production_duration_validation_is_derived_from_selected_manifest(
    model_id: str,
    valid_duration: int,
    invalid_duration: int,
    minimum: float,
    maximum: float,
):
    """Generic production sees the selected manifest's duration contract."""

    manifests = build_mainland_manifests(
        credential_presence={"DASHSCOPE_API_KEY": True, "ARK_API_KEY": True},
        create_authorized=True,
        artifact_sink_available=True,
    )
    registry = InMemoryManifestRegistry(manifests)
    projected = ProviderProfileService._manifest_profiles(
        registry, LegacyCapabilityKind.VIDEO_GENERATIVE.value
    )
    profile = next(item for item in projected if item.model_id == model_id)
    manifest = _manifest_by_model(manifests, model_id)
    duration = manifest.duration
    expected_limits = (float(duration.minimum), float(duration.maximum))

    # Pass a neutral runtime identity intentionally.  If this assertion only
    # worked for a Seedance/Wan alias, the business layer would still own a
    # second provider truth instead of consuming the manifest projection.
    limits = ProductionQueueService._duration_limits(
        profile.profile, provider_id="UNIVERSAL_VIDEO_RUNTIME"
    )
    allowed = ProductionQueueService._allowed_durations(
        profile.profile, provider_id="UNIVERSAL_VIDEO_RUNTIME"
    )
    assert limits == expected_limits == (minimum, maximum)
    assert allowed
    assert min(allowed) == minimum
    assert max(allowed) == maximum

    # The selected native codec is also checked against the same manifest
    # identity.  This is the provider-neutral pre-live gate for both values.
    codecs = build_mainland_codecs()
    codec = codecs[manifest.codec_id]
    input_refs = (
        ContentRef(
            source_kind="TEST_ARTIFACT",
            source_id="https://media.example.test/frozen.png",
            role="first_frame",
            mime_type="image/png",
        ),
    ) if model_id.startswith("wan") else ()

    def request_for(duration_value: int) -> CapabilityRequest:
        return CapabilityRequest(
            request_id=f"duration-{model_id}-{duration_value}",
            project_id="duration-test",
            capability=CapabilityKind.VIDEO,
            protocol_family=manifest.protocol,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            codec_id=manifest.codec_id,
            inputs=input_refs,
            prompt_or_text="synthetic duration contract",
            provider_parameters={"duration_seconds": duration_value},
        )

    codec.validate(request_for(valid_duration), manifest)
    with pytest.raises(CodecError):
        codec.validate(request_for(invalid_duration), manifest)


def test_duration_limits_do_not_branch_on_legacy_seedance_provider_alias():
    """A selected manifest, rather than a provider-name switch, owns limits."""

    manifests = build_mainland_manifests(
        credential_presence={"ARK_API_KEY": True},
        create_authorized=True,
        artifact_sink_available=True,
    )
    seedance = _manifest_by_model(manifests, "doubao-seedance-2-5-260628")
    projected = ProviderProfileService._manifest_profiles(
        InMemoryManifestRegistry((seedance,)),
        LegacyCapabilityKind.VIDEO_GENERATIVE.value,
    )
    profile = dict(projected[0].profile)
    # Deliberately alter only the registered contract projection.  A generic
    # production layer must consume these values even when the legacy provider
    # label happens to be ``SEEDANCE``; hard-coded 4..30 alias logic must fail.
    profile.update(
        {
            "minimum_duration_seconds": 5,
            "maximum_duration_seconds": 7,
            "supported_durations": [5, 6, 7],
            "requires_explicit_selection": True,
        }
    )
    expected = (5.0, 7.0)
    try:
        actual_limits = ProductionQueueService._duration_limits(
            profile, provider_id="SEEDANCE"
        )
        actual_allowed = ProductionQueueService._allowed_durations(
            profile, provider_id="SEEDANCE"
        )
    except Exception as exc:  # pragma: no cover - diagnostic path on BASE
        pytest.fail(
            _diagnostic(
                expected={"limits": expected, "allowed": (5.0, 6.0, 7.0)},
                actual=f"{type(exc).__name__}: {exc}",
                files="aidrama_studio/services/production_queue.py; aidrama_studio/services/provider_profiles.py",
                contract="duration validation is derived from the selected manifest, with no provider alias branch",
            )
        )
    assert actual_limits == expected and actual_allowed == (5.0, 6.0, 7.0), _diagnostic(
        expected={"limits": expected, "allowed": (5.0, 6.0, 7.0)},
        actual={"limits": actual_limits, "allowed": actual_allowed},
        files="aidrama_studio/services/production_queue.py; aidrama_studio/services/provider_profiles.py",
        contract="duration validation is derived from the selected manifest, with no provider alias branch",
    )


class _FakeResponse:
    def __init__(self, payload: Mapping[str, object], status_code: int = 200):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.content = b"{}"
        self._payload = dict(payload)

    def json(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeSession:
    """No-network transport recording only safe request shape."""

    def __init__(self, payload: Mapping[str, object]):
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.payload = dict(payload)

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((method, url, dict(kwargs)))
        return _FakeResponse(self.payload)


class _NoopArtifactSink:
    def persist_bytes(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("result artifact persistence was not expected in create-only test")

    def persist_remote(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("result artifact persistence was not expected in create-only test")


class _DataUriResolver:
    def resolve(self, _reference: ContentRef) -> str:
        # A valid tiny PNG signature is sufficient for codec input mapping;
        # the bytes never leave this process.
        return "data:image/png;base64,iVBORw0KGgo="


def _seedance_request(manifest: object, *, duration: int = 20) -> CapabilityRequest:
    return CapabilityRequest(
        request_id="seedance-universal-request",
        project_id="seedance-universal-project",
        capability=CapabilityKind.VIDEO,
        protocol_family=ProtocolFamily.ASYNC_TASK,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
        inputs=(
            ContentRef(
                source_kind="REFERENCE_ASSET_VERSION",
                source_id="reference-version-1",
                role="first_frame",
                mime_type="image/png",
            ),
        ),
        prompt_or_text="synthetic Seedance action",
        provider_parameters={
            "duration_seconds": duration,
            "resolution": "720P",
            "ratio": "16:9",
        },
        create_authorized=True,
        authorization_required=True,
    )


def test_seedance_universal_fake_transport_uses_exact_manifest_codec_and_async_driver(
    tmp_path: Path,
):
    """The registered Seedance contract is manifest -> codec -> async driver."""

    session = _FakeSession({"id": "seedance-fake-task", "status": "queued"})
    runtime = MainlandProviderRuntime(
        credentials={"ARK_API_KEY": "opaque-test-credential"},
        create_authorized=True,
        artifact_sink=_NoopArtifactSink(),
        input_resolver=_DataUriResolver(),
        sessions={ARK_CN_BEIJING_ENDPOINT_PROFILE: session},
    )
    manifest = runtime.manifest_registry.get(
        "mainland:volcengine:doubao-seedance-2-5-260628:v1"
    )
    assert manifest is not None
    binding = runtime.binding_for(manifest.id)
    assert binding.manifest.id == manifest.id
    assert binding.manifest.model_id == "doubao-seedance-2-5-260628"
    assert binding.manifest.codec_id == "ark.seedance.v1"
    assert isinstance(binding.codec, ArkSeedanceCodec)
    assert isinstance(binding.driver, AsyncTaskDriver)

    submission = runtime.submit(
        _seedance_request(manifest),
        authorization={"approved": True},
    )
    assert submission.provider_task_id == "seedance-fake-task"
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/contents/generations/tasks")
    assert kwargs["json"]["model"] == "doubao-seedance-2-5-260628"
    assert kwargs["json"]["duration"] == 20


class _Presence:
    def __init__(self, *keys: str):
        self._keys = frozenset(keys)

    def configured(self, key: str) -> bool:
        return key in self._keys


def _seedance_product_plan(tmp_path: Path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    manifests = InMemoryManifestRegistry(
        build_mainland_manifests(
            credential_presence={"ARK_API_KEY": True},
            create_authorized=True,
            artifact_sink_available=True,
        )
    )
    settings = SettingsModelService(
        repository,
        manifest_registry=manifests,
        credential_store=_Presence("ARK_API_KEY"),
    )
    selected_id = next(
        item.manifest_id
        for item in settings.inventory(CapabilityKind.VIDEO)
        if item.model_id == "doubao-seedance-2-5-260628"
    )
    settings.save_selections(
        project_id=None,
        selections={CapabilityKind.VIDEO: selected_id},
    )
    brief = GenerationBriefCompiler(repository).compile(
        project.id, job.id, "shot_001"
    )
    plan = RuntimePlanService(repository).create_from_selection(
        project.id,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=settings,
        production_job_id=job.id,
        generation_mode="text_to_video",
        resolution="1280x720",
        provider_generation_duration=20,
        target_creative_duration=20,
        audio_strategy="NATIVE_PROVIDER_AUDIO",
        authorization={"approved": True},
    )
    return repository, project, job, settings, selected_id, plan, brief


def test_seedance_product_resolver_must_not_bypass_registered_universal_binding(
    tmp_path: Path,
):
    """Production's Seedance path must execute the same universal contract."""

    repository, project, job, settings, selected_id, plan, brief = _seedance_product_plan(
        tmp_path
    )
    assert settings.resolve(project.id, CapabilityKind.VIDEO).option.manifest_id == selected_id
    assert plan.provider_parameters["manifest_id"] == selected_id
    assert plan.provider_parameters["codec_id"] == "ark.seedance.v1"

    # Build the legacy registry boundary exactly as the shipped resolver does,
    # but with a fake client.  The assertion below requires the resolver to
    # expose the universal binding it selected; returning a second
    # provider-specific protocol adapter is an architecture regression.
    fake_client = _FakeSession({"id": "legacy-path-task", "status": "queued"})
    legacy_adapter = SeedanceProductionAdapter(
        SeedanceProviderConfig(
            api_key="opaque-test-credential",
            allow_paid_live_tests=False,
        ),
        client=fake_client,
        runtime_plan=plan,
        generation_brief=brief,
        output_profile=repository.get_output_profile(plan.output_profile_id),
    )
    registry = CapabilityRegistry(
        [RuntimeVideoProvider(legacy_adapter, provider_name="SEEDANCE")]
    )
    task = ProviderTask(
        id="seedance-product-task",
        project_id=project.id,
        capability=LegacyCapabilityKind.VIDEO_GENERATIVE.value,
        provider_id=plan.provider_id,
        model_id=plan.model_id,
        idempotency_key="seedance-product-idempotency",
        state="QUEUED",
        request_summary={"approved": True},
        created_at="now",
        updated_at="now",
    )
    resolved = ProductionRuntimeResolver(
        registry=registry,
        repository=repository,
    ).resolve(task, plan)

    # This structural contract is intentionally explicit so a future adapter
    # may choose its own wrapper shape while still publishing the exact
    # universal pieces needed for audit/recovery.
    binding = getattr(resolved, "universal_binding", None)
    if binding is None:
        runtime = getattr(resolved, "runtime", None) or getattr(resolved, "universal_runtime", None)
        binding = getattr(runtime, "binding", None) if runtime is not None else None
    actual = {
        "adapter_type": type(resolved).__name__,
        "manifest_id": getattr(getattr(binding, "manifest", None), "id", None),
        "codec_id": getattr(getattr(binding, "manifest", None), "codec_id", None)
        or getattr(getattr(binding, "codec", None), "codec_id", None),
        "driver_type": type(getattr(binding, "driver", None)).__name__
        if binding is not None
        else None,
    }
    bound_driver = getattr(binding, "driver", None) if binding is not None else None
    assert (
        binding is not None
        and actual["manifest_id"] == selected_id
        and actual["codec_id"] == "ark.seedance.v1"
        and isinstance(bound_driver, AsyncTaskDriver)
    ), _diagnostic(
        expected={
            "manifest_id": selected_id,
            "codec_id": "ark.seedance.v1",
            "driver": "AsyncTaskDriver",
        },
        actual=actual,
        files="aidrama_studio/services/production_runtime_resolver.py; aidrama_studio/services/adapters/seedance_video.py",
        contract="Settings -> frozen RuntimePlan -> one registered Universal manifest/codec/ASYNC_TASK driver; no provider-specific protocol truth",
    )


def test_qwen_effective_provider_uses_canonical_dashscope_credential_identity():
    """Qwen's product status resolves the manifest's one canonical key."""

    from aidrama_studio.services.ai_capabilities import default_capability_registry

    registry = default_capability_registry(
        env={
            "LLM_PROVIDER": "qwen",
            # Presence is intentionally opaque; the value is never read,
            # printed, persisted, or compared by this test.
            "DASHSCOPE_API_KEY": "opaque-test-credential",
        }
    )
    provider = next(
        item
        for item in registry.list(LegacyCapabilityKind.LLM)
        if getattr(item, "provider_name", "") == "MPT_LLM"
    )
    status = provider.status
    metadata = dict(status.metadata)
    assert status.configured is True, _diagnostic(
        expected="Qwen configured from DASHSCOPE_API_KEY presence",
        actual=status.configured,
        files="aidrama_studio/services/ai_capabilities.py; aidrama_studio/services/model_settings.py; app/models/llm_provider.py",
        contract="Alibaba Model Studio Qwen must have one canonical DASHSCOPE_API_KEY credential identity",
    )
    assert metadata.get("credential_reference") == "DASHSCOPE_API_KEY", _diagnostic(
        expected="DASHSCOPE_API_KEY",
        actual=metadata.get("credential_reference"),
        files="aidrama_studio/services/ai_capabilities.py; app/models/llm_provider.py",
        contract="legacy QWEN_API_KEY may be accepted only as a compatibility input, never as a second persisted credential identity",
    )
    assert "QWEN_API_KEY" not in metadata.values()


@dataclass
class _QwenCreativeProvider:
    responses: list[str]
    calls: int = field(default=0, init=False)
    capability: LegacyCapabilityKind = LegacyCapabilityKind.LLM
    provider_name: str = "alibaba_model_studio"

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            LegacyCapabilityKind.LLM,
            self.provider_name,
            True,
            "offline configured",
            {
                "model": "qwen-max",
                "configured": True,
                "runtime_available": True,
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "DASHSCOPE_CN",
                "endpoint_profile_id": "DASHSCOPE_CN_BEIJING_V1",
                "credential_reference": "DASHSCOPE_API_KEY",
                # This is the legacy upstream spelling.  The product path
                # must project it to the canonical Universal provider ID.
                "upstream_provider_id": "qwen",
                "verification_state": "NOT_VERIFIED",
                "authorization_required": False,
            },
            configured=True,
            runtime_available=True,
            authorization_required=False,
        )

    def generate_json_text(self, _prompt: str) -> str:
        self.calls += 1
        return self.responses.pop(0)

    def invoke_universal_text(
        self, _selection: object, prompt: str, *, project_id: str
    ) -> str:
        assert project_id
        return self.generate_json_text(prompt)


def test_creative_qwen_settings_use_one_canonical_universal_identity_for_all_stages(
    tmp_path: Path,
):
    """Story, Script and Shot Plan AI all consume Settings' Qwen manifest."""

    repository, project = ProjectRepository(
        DatabasePaths(
            database=tmp_path / "creative" / "aidrama.db",
            projects=tmp_path / "creative" / "projects",
            archived_projects=tmp_path / "creative" / "archived",
        )
    ), None
    project = ProjectService(repository).create(
        title="Canonical Qwen creative path", description="offline regression"
    )
    manifests = InMemoryManifestRegistry(
        build_mainland_manifests(
            credential_presence={"DASHSCOPE_API_KEY": True},
            create_authorized=True,
            artifact_sink_available=True,
        )
    )
    settings = SettingsModelService(
        repository,
        manifest_registry=manifests,
        credential_store=_Presence("DASHSCOPE_API_KEY"),
    )
    qwen_manifest_id = next(
        item.manifest_id
        for item in settings.inventory(CapabilityKind.LLM)
        if item.model_id == "qwen-max"
    )
    wan_manifest_id = next(
        item.manifest_id
        for item in settings.inventory(CapabilityKind.VIDEO)
        if item.model_id == "wan2.7-i2v-2026-04-25"
    )
    settings.save_selections(
        project_id=None,
        selections={
            CapabilityKind.LLM: qwen_manifest_id,
            CapabilityKind.VIDEO: wan_manifest_id,
        },
    )

    provider = _QwenCreativeProvider(
        [
            json.dumps(_story().model_dump(mode="json"), ensure_ascii=False),
            json.dumps(_script(), ensure_ascii=False),
            json.dumps(_plan(), ensure_ascii=False),
        ]
    )
    legacy_registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(
        repository,
        registry=legacy_registry,
        manifest_registry=manifests,
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=legacy_registry,
        provider_profiles=profiles,
    )
    pipeline = CreativePipelineService(
        repository,
        story_service=StoryService(repository, llm_gateway=gateway),
        script_service=ScriptService(repository, llm_gateway=gateway),
        shot_service=ShotService(repository, llm_gateway=gateway),
    )
    brief = _approved_intake(repository, project)

    story = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": brief.id},
    )
    approved_story = StoryService(repository).approve_revision(story["id"])
    script = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SCRIPT",
        payload={"source_story_revision_id": approved_story["id"]},
    )
    approved_script = ScriptService(repository).approve_revision(script["id"])
    shot_plan = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SHOT_PLAN",
        payload={"source_script_revision_id": approved_script["id"]},
    )
    assert shot_plan["id"]
    assert provider.calls == 3

    invocations = repository.list_ai_invocations(project.id)
    assert len(invocations) == 6  # STARTED + SUCCEEDED for each product action
    assert {item.model_id for item in invocations} == {"qwen-max"}
    assert {item.provider_id for item in invocations} == {"alibaba_model_studio"}, _diagnostic(
        expected="alibaba_model_studio",
        actual={item.provider_id for item in invocations},
        files="aidrama_studio/services/llm_runtime.py; aidrama_studio/services/model_runtime/llm.py; aidrama_studio/services/ai_capabilities.py",
        contract="all creative LLM operations use the selected Universal Mainland Qwen provider; no qwen/Moonshot fallback",
    )
    assert {
        item.request_summary.get("model_manifest_id") for item in invocations
    } == {qwen_manifest_id}, _diagnostic(
        expected=qwen_manifest_id,
        actual={item.request_summary.get("model_manifest_id") for item in invocations},
        files="aidrama_studio/services/llm_runtime.py; aidrama_studio/services/model_runtime/llm.py; aidrama_studio/services/model_settings.py",
        contract="Settings exact manifest -> frozen UniversalLLM selection -> Story/Script/Shot invocation; no compatibility-manifest or endpoint-only substitution",
    )
    assert all(
        item.request_summary.get("llm_runtime") == "UNIVERSAL"
        for item in invocations
    )


__all__ = [
    "test_manifest_contract_hash_is_stable_across_mutable_readiness_changes",
    "test_shared_endpoint_frozen_identity_never_resolves_the_first_model",
    "test_production_duration_validation_is_derived_from_selected_manifest",
    "test_duration_limits_do_not_branch_on_legacy_seedance_provider_alias",
    "test_seedance_universal_fake_transport_uses_exact_manifest_codec_and_async_driver",
    "test_seedance_product_resolver_must_not_bypass_registered_universal_binding",
    "test_qwen_effective_provider_uses_canonical_dashscope_credential_identity",
    "test_creative_qwen_settings_use_one_canonical_universal_identity_for_all_stages",
]
