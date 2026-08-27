from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.domain import ReferenceBindingType
from aidrama_studio.services.mainland_frontend_runtime import (
    MainlandFrontendRuntimeBridge,
    MainlandFrontendRuntimeError,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityResult,
    RuntimeOutcome,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    build_mainland_manifests,
)
from aidrama_studio.services.reference_assets import ReferenceAssetService
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_production_execution import context as _context


@pytest.fixture
def context(tmp_path: Path):
    return _context.__wrapped__(tmp_path)


class _CredentialStore:
    def __init__(
        self,
        secret: str | None = "unit-test-secret",
        workspace_base_url: str | None = None,
    ) -> None:
        self.values = {}
        if secret:
            self.values["DASHSCOPE_API_KEY"] = secret
        if workspace_base_url:
            self.values["DASHSCOPE_WORKSPACE_BASE_URL"] = workspace_base_url
        self.read_count = 0

    def configured_providers(self):
        return tuple(self.values)

    def get(self, key):
        self.read_count += 1
        return self.values.get(key)


class _OfflineMainlandRuntime:
    requests = []
    workspace_base_urls = []

    def __init__(
        self,
        *,
        credentials,
        create_authorized,
        artifact_sink,
        dashscope_workspace_base_url=None,
    ):
        assert set(credentials) == {"DASHSCOPE_API_KEY"}
        assert credentials["DASHSCOPE_API_KEY"]
        assert create_authorized is True
        self.sink = artifact_sink
        self.__class__.workspace_base_urls.append(dashscope_workspace_base_url)
        self.manifest = next(
            item
            for item in build_mainland_manifests(
                credential_presence={"DASHSCOPE_API_KEY": True},
                create_authorized=True,
                artifact_sink_available=True,
            )
            if item.capability is CapabilityKind.IMAGE
        )

    def primary_manifest(self, capability):
        assert CapabilityKind.coerce(capability) is CapabilityKind.IMAGE
        return self.manifest

    def submit(self, request, *, authorization):
        assert authorization == {"approved": True, "create_authorized": True}
        self.__class__.requests.append(request)
        output = self.sink.persist_bytes(
            png_bytes(),
            request_id=request.request_id,
            role="generated_image",
            mime_type="image/png",
            safe_metadata={"provider": "alibaba_model_studio"},
        )
        return CapabilityResult(
            request_id=request.request_id,
            outcome=RuntimeOutcome.SUCCEEDED,
            outputs=(output,),
        )


def _bridge(repository, store):
    return MainlandFrontendRuntimeBridge(
        repository,
        paths=repository.paths,
        credential_store=store,
        runtime_factory=_OfflineMainlandRuntime,
        env={"AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1"},
    )


def test_settings_projection_declares_dashscope_without_reading_secret(context):
    repository, _project = context
    store = _CredentialStore()
    bridge = _bridge(repository, store)

    requirements = bridge.credential_requirements()
    snapshot = bridge.capability_snapshot()

    assert requirements[0]["key"] == "DASHSCOPE_API_KEY"
    assert requirements[1]["key"] == "DASHSCOPE_WORKSPACE_BASE_URL"
    assert requirements[1]["secret"] is False
    assert snapshot["IMAGE"]["configured"] is True
    assert snapshot["VIDEO"]["runtime_available"] is True
    assert snapshot["IMAGE"]["create_authorized"] is False
    assert store.read_count == 0


def test_reference_image_action_requires_explicit_paid_authorization(context):
    repository, project = context
    store = _CredentialStore()
    bridge = _bridge(repository, store)

    with pytest.raises(MainlandFrontendRuntimeError, match="明确确认"):
        bridge.handle_activity(
            project.id,
            "REFERENCE_IMAGE_CANDIDATE",
            {
                "subject_id": "char_001",
                "binding_type": ReferenceBindingType.CHARACTER.value,
                "source_story_revision_id": "story_001",
                "prompt": "rainy bus terminal",
                "create_authorized": False,
            },
        )

    assert store.read_count == 0
    assert _OfflineMainlandRuntime.requests == []


def test_reference_image_action_requires_process_live_gate(context):
    repository, project = context
    store = _CredentialStore()
    bridge = MainlandFrontendRuntimeBridge(
        repository,
        paths=repository.paths,
        credential_store=store,
        runtime_factory=_OfflineMainlandRuntime,
        env={},
    )
    _OfflineMainlandRuntime.requests.clear()

    with pytest.raises(MainlandFrontendRuntimeError, match="受控付费"):
        bridge.handle_activity(
            project.id,
            "REFERENCE_IMAGE_CANDIDATE",
            {
                "subject_id": "char_001",
                "binding_type": ReferenceBindingType.CHARACTER.value,
                "source_story_revision_id": "story_001",
                "prompt": "rainy bus terminal",
                "create_authorized": True,
            },
        )

    assert store.read_count == 0
    assert _OfflineMainlandRuntime.requests == []


def test_reference_image_action_uses_mainland_contract_and_records_draft(context):
    repository, project = context
    store = _CredentialStore()
    bridge = _bridge(repository, store)
    _OfflineMainlandRuntime.requests.clear()

    candidate = bridge.handle_activity(
        project.id,
        "REFERENCE_IMAGE_CANDIDATE",
        {
            "subject_id": "char_001",
            "binding_type": ReferenceBindingType.CHARACTER.value,
            "source_story_revision_id": "story_001",
            "prompt": "rainy bus terminal",
            "create_authorized": True,
        },
    )

    assert store.read_count == 1
    assert len(_OfflineMainlandRuntime.requests) == 1
    request = _OfflineMainlandRuntime.requests[0]
    assert request.manifest_id == "mainland:alibaba:z-image-turbo:v1"
    assert dict(request.provider_parameters) == {
        "resolution": "720*1280",
        "prompt_extend": False,
    }
    assert "n" not in request.provider_parameters
    assert candidate.model_id == "z-image-turbo"
    assert candidate.sha256
    service = ReferenceAssetService(repository)
    asset = service.find_workspace_asset(
        project.id,
        ReferenceBindingType.CHARACTER,
        "char_001",
    )
    assert asset is not None
    assert asset.current_version_id is None
    assert service.resolve_image_candidate_path(project.id, candidate.id).is_file()


def test_workspace_key_requires_workspace_base_url_before_runtime(context):
    repository, project = context
    store = _CredentialStore(secret="sk-ws-unit-test")
    bridge = _bridge(repository, store)
    _OfflineMainlandRuntime.requests.clear()

    with pytest.raises(MainlandFrontendRuntimeError, match="业务空间 Base URL"):
        bridge.handle_activity(
            project.id,
            "REFERENCE_IMAGE_CANDIDATE",
            {
                "subject_id": "char_001",
                "binding_type": ReferenceBindingType.CHARACTER.value,
                "source_story_revision_id": "story_001",
                "prompt": "rainy bus terminal",
                "create_authorized": True,
            },
        )

    assert _OfflineMainlandRuntime.requests == []


def test_workspace_base_url_is_pinned_into_frontend_runtime(context):
    repository, project = context
    workspace_base_url = (
        "https://ws-unit-test.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    store = _CredentialStore(
        secret="sk-ws-unit-test",
        workspace_base_url=workspace_base_url,
    )
    bridge = _bridge(repository, store)
    _OfflineMainlandRuntime.requests.clear()
    _OfflineMainlandRuntime.workspace_base_urls.clear()

    bridge.handle_activity(
        project.id,
        "REFERENCE_IMAGE_CANDIDATE",
        {
            "subject_id": "char_001",
            "binding_type": ReferenceBindingType.CHARACTER.value,
            "source_story_revision_id": "story_001",
            "prompt": "rainy bus terminal",
            "create_authorized": True,
        },
    )

    assert _OfflineMainlandRuntime.workspace_base_urls == [workspace_base_url]
    assert len(_OfflineMainlandRuntime.requests) == 1
