from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import pytest
from streamlit.testing.v1 import AppTest

from aidrama_studio.domain import GenerationBrief, ShotRevisionStatus, StoryRevisionStatus
from aidrama_studio.services import ProjectService, RuntimePlanService
from aidrama_studio.services.creative_pipeline import CreativePipelineError
from aidrama_studio.services.model_runtime import CapabilityKind
from aidrama_studio.services.model_settings import CAPABILITY_ORDER, SettingsModelService
from aidrama_studio.services.script import ScriptService
from aidrama_studio.services.shot import ShotService
from aidrama_studio.services.story import StoryService

from test.aidrama_studio.test_creative_pipeline import (
    _FakeLLMProvider,
    _approved_intake,
    _pipeline,
    _plan,
    _script,
    _story,
)


def test_full_ai_creative_chain_uses_universal_llm_and_exact_provenance(tmp_path):
    provider = _FakeLLMProvider(
        [
            json.dumps(_story().model_dump(mode="json")),
            json.dumps(_script()),
            json.dumps(_plan()),
        ],
        provider_name="OFFLINE_UNIVERSAL",
        model="fake-llm-wave1-v1",
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)

    story = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": brief.id},
    )
    assert story["status"] is StoryRevisionStatus.DRAFT
    with pytest.raises(CreativePipelineError, match="APPROVED Story Bible"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_SCRIPT",
            payload={"source_story_revision_id": story["id"]},
        )
    approved_story = StoryService(repository).approve_revision(story["id"])

    script = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SCRIPT",
        payload={"source_story_revision_id": approved_story["id"]},
    )
    assert script["source_story_revision_id"] == approved_story["id"]
    with pytest.raises(CreativePipelineError, match="APPROVED Structured Script"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_SHOT_PLAN",
            payload={"source_script_revision_id": script["id"]},
        )
    approved_script = ScriptService(repository).approve_revision(script["id"])

    plan = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_SHOT_PLAN",
        payload={"source_script_revision_id": approved_script["id"]},
    )
    assert plan["source_script_revision_id"] == approved_script["id"]
    assert plan["content"].source_script_revision_id == approved_script["id"]
    approved_plan = ShotService(repository).approve_revision(plan["id"])
    assert approved_plan["status"] is ShotRevisionStatus.APPROVED

    operations = repository.list_creative_pipeline_operations(project.id)
    assert len(operations) == 3
    assert all(item.status.value == "WAITING_HUMAN" for item in operations)
    invocations = repository.list_ai_invocations(project.id)
    assert provider.calls == 3
    assert all(item.request_summary["llm_runtime"] == "UNIVERSAL" for item in invocations)
    assert all(item.request_summary["provenance"]["input_revision_ids"] for item in invocations)
    by_operation = {item.operation: item for item in operations}
    assert by_operation["GENERATE_SCRIPT"].input_revision_ids == (approved_story["id"],)
    assert set(by_operation["GENERATE_SHOT_PLAN"].input_revision_ids) == {
        approved_script["id"],
        approved_story["id"],
    }


def test_creative_duplicate_and_regenerate_semantics_are_explicit(tmp_path):
    provider = _FakeLLMProvider(
        [json.dumps(_story().model_dump(mode="json")), json.dumps(_story().model_dump(mode="json"))],
        provider_name="OFFLINE_UNIVERSAL",
        model="fake-llm-wave1-v1",
    )
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    first = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={"normalized_brief_id": brief.id},
    )
    duplicate = pipeline.execute(
        project_id=project.id,
        operation="STORY_BIBLE_GENERATION",
        payload={"normalized_brief_id": brief.id},
    )
    assert duplicate["id"] == first["id"]
    assert len(repository.list_story_revisions(project.id)) == 1
    regenerated = pipeline.execute(
        project_id=project.id,
        operation="GENERATE_STORY",
        payload={
            "normalized_brief_id": brief.id,
            "regenerate": True,
            "operation_intent_id": "explicit-new-version",
        },
    )
    assert regenerated["id"] != first["id"]
    assert len(repository.list_story_revisions(project.id)) == 2
    assert provider.calls == 2


@pytest.mark.parametrize("invalid", ["not-json", "{}"])
def test_invalid_structured_output_is_bounded_and_preserves_approved_revision(tmp_path, invalid):
    provider = _FakeLLMProvider([invalid, invalid], "OFFLINE_UNIVERSAL", "fake-llm-wave1-v1")
    repository, project, pipeline = _pipeline(tmp_path, provider)
    brief = _approved_intake(repository, project)
    previous = StoryService(repository).create_blank_draft(project)
    StoryService(repository).approve_revision(previous["id"])

    with pytest.raises(CreativePipelineError, match="一次修复"):
        pipeline.execute(
            project_id=project.id,
            operation="GENERATE_STORY",
            payload={"normalized_brief_id": brief.id, "regenerate": True},
        )
    assert provider.calls == 2
    assert StoryService(repository).get_revision(previous["id"])["status"] is StoryRevisionStatus.APPROVED
    assert repository.list_creative_pipeline_operations(project.id)[0].status.value == "FAILED"


@dataclass
class _FakeCredentialStore:
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


def _manifest(service: SettingsModelService, capability: CapabilityKind, model_id: str):
    return next(item for item in service.inventory(capability) if item.model_id == model_id)


def test_settings_persist_all_registered_capability_selections(tmp_path, repository):
    project = ProjectService(repository).create(title="Universal settings")
    service = SettingsModelService(repository, credential_store=_FakeCredentialStore())
    requested = {
        CapabilityKind.LLM: "qwen-max",
        CapabilityKind.IMAGE: "qwen-image-3.0",
        CapabilityKind.VIDEO: "wan2.7-i2v-2026-04-25",
        CapabilityKind.VISION: "qwen3-vl-flash",
        CapabilityKind.TTS: "qwen3-tts-flash",
    }
    selections = {kind: _manifest(service, kind, model).manifest_id for kind, model in requested.items()}
    service.save_selections(project_id=project.id, selections=selections)
    for capability, model_id in requested.items():
        resolved = service.resolve(project.id, capability)
        assert resolved.option.model_id == model_id
        assert resolved.option.manifest_id == selections[capability]

    deepseek = _manifest(service, CapabilityKind.LLM, "deepseek-v4-pro")
    service.save_selections(project_id=project.id, selections={CapabilityKind.LLM: deepseek.manifest_id})
    assert service.resolve(project.id, CapabilityKind.LLM).option.model_id == "deepseek-v4-pro"
    assert {item.capability for item in service.inventory()} == set(CAPABILITY_ORDER)


def test_frozen_runtime_plan_is_immutable_after_settings_change(tmp_path, repository):
    project = ProjectService(repository).create(title="Runtime freeze")
    service = SettingsModelService(repository, credential_store=_FakeCredentialStore())
    wan = _manifest(service, CapabilityKind.VIDEO, "wan2.7-i2v-2026-04-25")
    seedance = _manifest(service, CapabilityKind.VIDEO, "doubao-seedance-2-5-260628")
    service.save_selections(project_id=project.id, selections={CapabilityKind.VIDEO: wan.manifest_id})
    brief = GenerationBrief(
        id="brief-wave1",
        project_id=project.id,
        production_job_id=None,
        shot_id="shot-1",
        target_duration_seconds=4,
        sha256=hashlib.sha256(b"brief").hexdigest(),
        created_at="2026-08-28T00:00:00+00:00",
    )
    repository.create_generation_brief(brief)
    plans = RuntimePlanService(repository)
    first = plans.create_from_selection(
        project.id,
        production_job_id=None,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=service,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )
    frozen = first.model_dump(mode="json")

    service.save_selections(project_id=project.id, selections={CapabilityKind.VIDEO: seedance.manifest_id})
    second = plans.create_from_selection(
        project.id,
        production_job_id=None,
        brief=brief,
        capability=CapabilityKind.VIDEO,
        selection_service=service,
        resolution="1280x720",
        provider_generation_duration=4,
        target_creative_duration=4,
    )
    assert second.model_id == "doubao-seedance-2-5-260628"
    assert repository.get_runtime_plan(first.id).model_dump(mode="json") == frozen
    assert repository.get_runtime_plan(first.id).model_id == "wan2.7-i2v-2026-04-25"


def test_secret_value_is_absent_from_public_settings_projection(tmp_path):
    secret = "sk-wave1-canary-not-a-secret"
    service = SettingsModelService(
        credential_store=_FakeCredentialStore({"DASHSCOPE_API_KEY": secret})
    )
    public = [
        {
            "capability": item.capability.value,
            "provider": item.provider_id,
            "model": item.model_id,
            "configured": item.configured,
            "credential_reference": item.credential_reference,
        }
        for item in service.inventory()
    ]
    assert secret not in json.dumps(public, ensure_ascii=False)
    assert secret not in json.dumps(service.credential_requirements(), ensure_ascii=False)


def test_secret_value_is_not_rendered_by_settings_ui(tmp_path):
    data_root = (tmp_path / "settings-ui").as_posix()
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

store = st.session_state.setdefault("_wave1-store", Store())
paths = type("Paths", (), {{"root": Path(r"{data_root}")}})()
page._render_credentials(
    paths,
    (),
    requirements=({{"key": "DASHSCOPE_API_KEY", "label": "DashScope", "secret": True}},),
    credential_store=store,
)
"""
    ).run()
    secret = "sk-wave1-canary-not-a-secret"
    app.text_input[0].set_value(secret)
    next(button for button in app.button if button.label == "安全保存").click().run()
    rendered = "\n".join(
        str(element.value)
        for collection in (app.markdown, app.caption, app.success, app.warning)
        for element in collection
    )
    assert not app.exception
    assert "已配置" in rendered
    assert secret not in rendered
