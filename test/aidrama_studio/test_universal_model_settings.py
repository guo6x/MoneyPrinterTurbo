from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from streamlit.testing.v1 import AppTest

from aidrama_studio.services import GenerationBriefService, ProjectService, RuntimePlanService
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind as LegacyCapabilityKind,
    default_capability_registry,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    DASHSCOPE_CN_ENDPOINT_PROFILE,
    DEEPSEEK_CN_ENDPOINT_PROFILE,
    ARK_CN_BEIJING_ENDPOINT_PROFILE,
    MainlandProviderRuntime,
    ModelResolutionError,
    build_mainland_manifests,
    default_manifest_registry,
)
from aidrama_studio.services.model_settings import (
    CAPABILITY_ORDER,
    SettingsModelService,
    validate_connection_value,
)
from aidrama_studio.services.provider_profiles import ProviderProfileService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_production_execution import _ready_job, context as _execution_context


@dataclass
class FakeCredentialStore:
    values: dict[str, str] = field(default_factory=dict)

    def configured(self, key: str) -> bool:
        return bool(self.values.get(key))

    def configured_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _repository(tmp_path) -> ProjectRepository:
    root = tmp_path / "aidrama-settings"
    return ProjectRepository(
        DatabasePaths(
            database=root / "aidrama.db",
            projects=root / "projects",
            archived_projects=root / "archived",
        )
    )


def _service(tmp_path, *, credentials: dict[str, str] | None = None) -> SettingsModelService:
    return SettingsModelService(
        _repository(tmp_path),
        credential_store=FakeCredentialStore(dict(credentials or {})),
    )


def _manifest_id(service: SettingsModelService, capability: CapabilityKind, model_id: str) -> str:
    return next(
        item.manifest_id
        for item in service.inventory(capability)
        if item.model_id == model_id
    )


def test_manifest_contract_hash_excludes_mutable_readiness_state():
    unavailable = {
        item.id: item
        for item in build_mainland_manifests(
            credential_presence={},
            create_authorized=False,
            artifact_sink_available=False,
        )
    }
    ready = {
        item.id: item
        for item in build_mainland_manifests(
            credential_presence={
                "DASHSCOPE_API_KEY": True,
                "DEEPSEEK_API_KEY": True,
                "ARK_API_KEY": True,
            },
            create_authorized=True,
            artifact_sink_available=True,
        )
    }

    assert unavailable.keys() == ready.keys()
    assert all(
        unavailable[manifest_id].manifest_hash
        == ready[manifest_id].manifest_hash
        for manifest_id in unavailable
    )
    assert any(
        unavailable[manifest_id].readiness
        != ready[manifest_id].readiness
        for manifest_id in unavailable
    )
    assert all(
        "readiness" not in manifest.contract_payload()
        for manifest in ready.values()
    )


def test_manifest_registry_is_the_only_settings_inventory_source(tmp_path):
    service = _service(tmp_path)
    inventory = service.inventory()

    assert {item.capability for item in inventory} == set(CAPABILITY_ORDER)
    assert {item.provider_id for item in inventory} == {
        "alibaba_model_studio",
        "deepseek",
        "volcengine_ark",
    }
    assert {item.model_id for item in inventory} == {
        "qwen-max",
        "qwen-image-3.0",
        "z-image-turbo",
        "wan2.7-i2v-2026-04-25",
        "qwen3-vl-flash",
        "qwen3-tts-flash",
        "deepseek-v4-pro",
        "doubao-seedance-2-5-260628",
    }
    assert all(item.registered and item.compatible for item in inventory)


def test_capability_filtering_does_not_trust_registry_filter(tmp_path):
    manifests = default_manifest_registry(include_placeholders=False).list()

    class UnfilteredRegistry:
        def list(self, capability=None):
            del capability
            return manifests

        def get(self, key):
            return next((item for item in manifests if item.id == key), None)

    service = SettingsModelService(
        _repository(tmp_path),
        manifest_registry=UnfilteredRegistry(),
        credential_store=FakeCredentialStore(),
    )
    assert {item.capability for item in service.inventory(CapabilityKind.VIDEO)} == {
        CapabilityKind.VIDEO
    }
    assert {item.model_id for item in service.inventory(CapabilityKind.VIDEO)} == {
        "wan2.7-i2v-2026-04-25",
        "doubao-seedance-2-5-260628",
    }


def test_credential_declarations_cover_alibaba_deepseek_ark_and_validate_endpoint(tmp_path):
    service = _service(tmp_path)
    declarations = {item["key"]: item for item in service.credential_requirements()}

    assert {"DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY"}.issubset(
        declarations
    )
    assert declarations["DASHSCOPE_API_KEY"]["secret"] is True
    assert declarations["DEEPSEEK_API_KEY"]["secret"] is True
    assert declarations["ARK_API_KEY"]["secret"] is True
    workspace = declarations["DASHSCOPE_WORKSPACE_BASE_URL"]
    assert workspace["secret"] is False
    assert validate_connection_value(
        workspace,
        "https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1",
    ) == "https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1"
    with pytest.raises(ValueError):
        validate_connection_value(workspace, "http://user:secret@example.test/api/v1")


def test_global_and_project_manifest_selection_persistence(tmp_path):
    repository = _repository(tmp_path)
    project = ProjectService(repository).create(title="Manifest Settings")
    service = SettingsModelService(repository, credential_store=FakeCredentialStore())
    qwen = _manifest_id(service, CapabilityKind.LLM, "qwen-max")
    deepseek = _manifest_id(service, CapabilityKind.LLM, "deepseek-v4-pro")

    service.save_selections(project_id=None, selections={CapabilityKind.LLM: qwen})
    inherited = service.resolve(project.id, CapabilityKind.LLM)
    assert inherited.option.manifest_id == qwen
    assert inherited.source == "GLOBAL_SELECTION"
    assert inherited.inherited is True

    service.save_selections(
        project_id=project.id,
        selections={CapabilityKind.LLM: deepseek},
    )
    selected = service.resolve(project.id, CapabilityKind.LLM)
    assert selected.option.manifest_id == deepseek
    assert selected.source == "PROJECT_SELECTION"
    assert service.resolve(None, CapabilityKind.LLM).option.manifest_id == qwen


def test_incompatible_capability_manifest_cannot_be_selected(tmp_path):
    service = _service(tmp_path)
    wan = _manifest_id(service, CapabilityKind.VIDEO, "wan2.7-i2v-2026-04-25")

    with pytest.raises(ModelResolutionError, match="incompatible"):
        service.save_selections(
            project_id=None,
            selections={CapabilityKind.IMAGE: wan},
        )


def test_missing_credentials_are_distinct_from_runtime_and_authorization(tmp_path):
    service = _service(tmp_path)
    for option in service.inventory():
        assert option.configured is False
        assert option.verified is False
        assert option.runtime_available is True
        assert option.create_authorized is False
        assert option.authorization_required is True
        assert option.credential_ready is False


def test_settings_render_smoke_has_provider_and_model_selectors_without_network(tmp_path):
    data_root = (tmp_path / "app-test").as_posix()
    app = AppTest.from_string(
        f"""
from pathlib import Path
from aidrama_studio.pages import settings as page
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository

class Store:
    def configured(self, key): return False
    def configured_providers(self): return ()
    def set(self, key, value): raise AssertionError('save was not requested')
    def delete(self, key): raise AssertionError('delete was not requested')

root = Path(r'{data_root}')
paths = DatabasePaths(root / 'aidrama.db', root / 'projects', root / 'archived')
service = SettingsModelService(ProjectRepository(paths), credential_store=Store())
page._render_model_scheme((), selection_service=service)
page._render_credentials(paths, (), requirements=service.credential_requirements(), credential_store=Store())
"""
    ).run()

    assert not app.exception
    labels = {item.label for item in app.selectbox}
    for capability_label in ("文本生成", "参考图生成", "视频生成", "画面分析", "配音"):
        assert f"{capability_label} Provider" in labels
        assert f"{capability_label} 模型选择" in labels
    rendered = "\n".join(
        str(element.value)
        for collection in (app.markdown, app.caption, app.info, app.warning)
        for element in collection
    )
    assert "DeepSeek" in rendered
    assert "火山引擎" in rendered
    assert "secret-value" not in rendered


def test_secret_value_is_never_rendered_after_save(tmp_path):
    data_root = (tmp_path / "secret-app-test").as_posix()
    app = AppTest.from_string(
        f"""
from pathlib import Path
import streamlit as st
from aidrama_studio.pages import settings as page

class Store:
    def __init__(self): self.values = {{}}
    def configured(self, key): return bool(self.values.get(key))
    def configured_providers(self): return tuple(self.values)
    def set(self, key, value): self.values[key] = value
    def delete(self, key): self.values.pop(key, None)

store = st.session_state.setdefault('_fake-store', Store())
paths = type('Paths', (), {{'root': Path(r'{data_root}')}})()
page._render_credentials(paths, (), requirements=({{
    'key': 'DEEPSEEK_API_KEY', 'label': 'DeepSeek', 'secret': True,
}},), credential_store=store)
"""
    ).run()
    secret = "unit-test-secret-must-never-render"
    app.text_input[0].set_value(secret)
    next(button for button in app.button if button.label == "安全保存").click().run()

    assert not app.exception
    rendered = "\n".join(
        str(element.value)
        for collection in (app.markdown, app.caption, app.success, app.warning)
        for element in collection
    )
    assert "已配置" in rendered
    assert secret not in rendered


def test_settings_open_and_selection_do_not_make_network_calls(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network call is forbidden")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    service = _service(tmp_path)
    selections = {
        capability: service.resolve(None, capability).option.manifest_id
        for capability in CAPABILITY_ORDER
    }
    service.save_selections(project_id=None, selections=selections)
    assert all(service.resolve(None, item).option.manifest_id for item in CAPABILITY_ORDER)


def test_qwen_and_deepseek_resolve_from_saved_settings(tmp_path):
    service = _service(tmp_path)
    for model_id, provider_id in (
        ("qwen-max", "alibaba_model_studio"),
        ("deepseek-v4-pro", "deepseek"),
    ):
        selected = _manifest_id(service, CapabilityKind.LLM, model_id)
        service.save_selections(
            project_id=None,
            selections={CapabilityKind.LLM: selected},
        )
        resolved = service.resolve(None, CapabilityKind.LLM)
        assert resolved.option.model_id == model_id
        assert resolved.option.provider_id == provider_id


def test_new_runtime_plan_follows_selection_and_existing_plan_stays_frozen(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    brief = GenerationBriefService(repository).prepare_for_job(project.id, job.id)[0]
    store = FakeCredentialStore(
        {"DASHSCOPE_API_KEY": "fake", "ARK_API_KEY": "fake"}
    )
    service = SettingsModelService(repository, credential_store=store)
    wan = _manifest_id(service, CapabilityKind.VIDEO, "wan2.7-i2v-2026-04-25")
    seedance = _manifest_id(
        service, CapabilityKind.VIDEO, "doubao-seedance-2-5-260628"
    )
    service.save_selections(project_id=None, selections={CapabilityKind.VIDEO: wan})

    plans = RuntimePlanService(repository)
    first = plans.create_from_selection(
        project.id,
        production_job_id=job.id,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=service,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )
    first_hash = first.plan_hash
    assert first.provider_id == "WAN_VIDEO"
    assert first.provider_parameters["manifest_id"] == wan

    service.save_selections(
        project_id=None,
        selections={CapabilityKind.VIDEO: seedance},
    )
    second = plans.create_from_selection(
        project.id,
        production_job_id=job.id,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=service,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )

    reloaded = repository.get_runtime_plan(first.id)
    assert second.provider_id == "SEEDANCE"
    assert second.provider_parameters["manifest_id"] == seedance
    assert reloaded.provider_id == "WAN_VIDEO"
    assert reloaded.model_id == "wan2.7-i2v-2026-04-25"
    assert reloaded.plan_hash == first_hash


def test_saved_video_manifest_is_consumed_by_existing_runtime_selection(tmp_path):
    repository = _repository(tmp_path)
    project = ProjectService(repository).create(title="Runtime Selection Bridge")
    service = SettingsModelService(
        repository,
        credential_store=FakeCredentialStore({"ARK_API_KEY": "fake"}),
    )
    seedance = _manifest_id(
        service, CapabilityKind.VIDEO, "doubao-seedance-2-5-260628"
    )
    service.save_selections(
        project_id=None,
        selections={CapabilityKind.VIDEO: seedance},
    )
    runtime_profiles = ProviderProfileService(
        repository,
        registry=default_capability_registry(
            env={
                "ARK_API_KEY": "fake",
                "AIDRAMA_ALLOW_PAID_LIVE_TESTS": "0",
            }
        ),
    )

    resolved = runtime_profiles.resolve(
        project.id, LegacyCapabilityKind.VIDEO_GENERATIVE
    )
    assert resolved.profile is not None
    assert resolved.profile.id == seedance
    assert resolved.profile.provider_id == "SEEDANCE"
    assert resolved.profile.profile["supported_durations"] == list(range(4, 31))
    assert resolved.source == "GLOBAL_DEFAULT"


def test_wan_seedance_switch_binds_only_injected_fake_transports(tmp_path):
    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.trust_env = False

        def request(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("binding resolution must not call transport")

    sessions = {
        DASHSCOPE_CN_ENDPOINT_PROFILE: FakeSession(),
        DEEPSEEK_CN_ENDPOINT_PROFILE: FakeSession(),
        ARK_CN_BEIJING_ENDPOINT_PROFILE: FakeSession(),
    }
    runtime = MainlandProviderRuntime(
        credentials={
            "DASHSCOPE_API_KEY": "fake-dashscope",
            "DEEPSEEK_API_KEY": "fake-deepseek",
            "ARK_API_KEY": "fake-ark",
        },
        artifact_sink=object(),
        sessions=sessions,
    )
    service = SettingsModelService(
        _repository(tmp_path),
        manifest_registry=runtime.manifest_registry,
        credential_store=FakeCredentialStore(
            {"DASHSCOPE_API_KEY": "fake", "ARK_API_KEY": "fake"}
        ),
    )
    for model_id in ("wan2.7-i2v-2026-04-25", "doubao-seedance-2-5-260628"):
        manifest_id = _manifest_id(service, CapabilityKind.VIDEO, model_id)
        service.save_selections(
            project_id=None,
            selections={CapabilityKind.VIDEO: manifest_id},
        )
        resolved = service.resolve(None, CapabilityKind.VIDEO)
        binding = runtime.binding_for(resolved.option.manifest_id)
        assert binding.manifest.model_id == model_id
    for model_id in ("qwen-max", "deepseek-v4-pro"):
        manifest_id = _manifest_id(service, CapabilityKind.LLM, model_id)
        service.save_selections(
            project_id=None,
            selections={CapabilityKind.LLM: manifest_id},
        )
        resolved = service.resolve(None, CapabilityKind.LLM)
        binding = runtime.binding_for(resolved.option.manifest_id)
        assert binding.manifest.model_id == model_id
    assert sum(item.calls for item in sessions.values()) == 0
