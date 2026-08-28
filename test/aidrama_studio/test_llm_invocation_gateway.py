from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

import pytest

from aidrama_studio.domain import (
    Scene,
    ScriptBeat,
    ScriptBeatType,
    Shot,
    ShotPlan,
    StructuredScript,
)
from aidrama_studio.domain.runtime_operations import ProviderPreset
from aidrama_studio.services import (
    ProjectService,
    ScriptService,
    ShotService,
    StoryService,
)
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    MainlandUniversalLLMProvider,
    MPTLLMProvider,
    default_capability_registry,
)
from aidrama_studio.services.llm_runtime import (
    LLM_LIVE_SMOKE_PROMPT,
    LLMInvocationError,
    LLMInvocationGateway,
)
from aidrama_studio.services.provider_profiles import ProviderProfileService
from aidrama_studio.services.model_runtime import (
    DASHSCOPE_CN_ENDPOINT_PROFILE,
    MAINLAND_PRIMARY_MANIFEST_IDS,
    CapabilityKind as RuntimeCapabilityKind,
    default_manifest_registry,
)
from aidrama_studio.services.runtime_foundation import AIInvocationService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_story_bible import valid_bible


@dataclass
class _LLMProvider:
    responses: list[object]
    provider_name: str = "TEST_LLM"
    model: str = "test-llm-v1"
    deployment_region: str = "MAINLAND_CHINA"
    endpoint_class: str = "TEST_CN_LLM"
    endpoint_profile_id: str = "runtime:LLM:TEST_LLM:TEST_CN_LLM"
    upstream_provider_id: str | None = None
    live_authorized: bool | None = None
    on_call: Callable[[int], None] | None = None
    capability: CapabilityKind = CapabilityKind.LLM

    def __post_init__(self):
        self.call_count = 0

    @property
    def status(self) -> CapabilityStatus:
        metadata = {
            "model": self.model,
            "configured": True,
            "deployment_region": self.deployment_region,
            "endpoint_class": self.endpoint_class,
            "endpoint_profile_id": self.endpoint_profile_id,
            "upstream_provider_id": self.upstream_provider_id,
            "verification_state": "NOT_VERIFIED",
        }
        if self.live_authorized is not None:
            metadata["live_authorized"] = self.live_authorized
        return CapabilityStatus(
            CapabilityKind.LLM,
            self.provider_name,
            True,
            "configured",
            metadata,
            configured=True,
        )

    def generate_json_text(self, prompt: str) -> str:
        self.call_count += 1
        if self.on_call is not None:
            self.on_call(self.call_count)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return str(response)


def _context(tmp_path):
    paths = DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="LLM Ledger")
    return repository, project


class _FakeQwenResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    content = b""

    def __init__(self, text: str):
        self.text = text

    def json(self):
        return {
            "output": {
                "choices": [
                    {
                        "message": {"content": self.text},
                        "finish_reason": "stop",
                    }
                ]
            },
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }


class _FakeQwenSession:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeQwenResponse(self.responses.pop(0))


def _native_qwen_gateway(repository, project, session, *, secret="fake-dashscope"):
    provider = MainlandUniversalLLMProvider(
        credentials={"DASHSCOPE_API_KEY": secret},
        env={},
        sessions={DASHSCOPE_CN_ENDPOINT_PROFILE: session},
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(
        repository,
        registry=registry,
        manifest_registry=default_manifest_registry(include_placeholders=False),
    )
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={
            CapabilityKind.LLM: MAINLAND_PRIMARY_MANIFEST_IDS[
                RuntimeCapabilityKind.LLM
            ]
        },
    )
    return LLMInvocationGateway(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )


def test_explicit_qwen_selection_uses_exact_native_manifest_codec_and_driver(
    tmp_path, monkeypatch
):
    repository, project = _context(tmp_path)
    session = _FakeQwenSession(['{"ok": true}'])
    gateway = _native_qwen_gateway(repository, project, session)
    monkeypatch.setattr(
        "app.services.llm._generate_response",
        lambda **_: (_ for _ in ()).throw(AssertionError("legacy LLM called")),
    )

    result = gateway.generate_json_text(
        project.id,
        "canonical qwen prompt",
        operation="NATIVE_QWEN_TEST",
    )

    assert json.loads(result) == {"ok": True}
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith(
        "/api/v1/services/aigc/text-generation/generation"
    )
    assert call["json"]["model"] == "qwen-max"
    assert call["json"]["input"]["messages"] == [
        {"role": "user", "content": "canonical qwen prompt"}
    ]
    assert call["headers"]["Authorization"] == "Bearer fake-dashscope"
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    assert {item.provider_id for item in invocations} == {
        "alibaba_model_studio"
    }
    assert {item.model_id for item in invocations} == {"qwen-max"}
    for item in invocations:
        summary = item.request_summary
        assert summary["llm_runtime"] == "UNIVERSAL"
        assert summary["model_manifest_id"] == MAINLAND_PRIMARY_MANIFEST_IDS[
            RuntimeCapabilityKind.LLM
        ]
        assert summary["protocol"] == "REQUEST_RESPONSE"
        assert summary["credential_reference"] == "DASHSCOPE_API_KEY"
    durable = repr(invocations)
    assert "fake-dashscope" not in durable
    assert "QWEN_API_KEY" not in durable
    assert "moonshot" not in durable.casefold()


def test_qwen_legacy_secret_alias_keeps_one_canonical_public_identity():
    provider = MainlandUniversalLLMProvider(
        credentials={"QWEN_API_KEY": "legacy-in-memory-only"},
        env={},
    )

    assert provider.status.configured is True
    assert provider.status.metadata["credential_reference"] == "DASHSCOPE_API_KEY"
    assert "legacy-in-memory-only" not in repr(provider.status.public_dict())


def test_default_qwen_registry_reads_workspace_url_from_canonical_store(
    monkeypatch,
):
    workspace_url = "https://workspace.example.invalid/api/v1"

    class _CredentialStore:
        def __init__(self, _root):
            self.values = {
                "DASHSCOPE_API_KEY": "credential-key-placeholder",
                "DASHSCOPE_WORKSPACE_BASE_URL": workspace_url,
            }

        def get(self, key):
            return self.values.get(key)

        def configured(self, key):
            return bool(self.get(key))

        def configured_providers(self):
            return tuple(sorted(self.values))

    monkeypatch.setattr(
        "aidrama_studio.services.credentials.WindowsCredentialStore",
        _CredentialStore,
    )

    registry = default_capability_registry()
    provider = next(
        item
        for item in registry.list(CapabilityKind.LLM)
        if item.provider_name == "alibaba_model_studio"
    )

    assert provider._workspace_base_url == workspace_url
    public_status = repr(provider.status.public_dict())
    assert workspace_url not in public_status
    assert "credential-key-placeholder" not in public_status


def test_gateway_records_append_only_started_and_succeeded_without_prompt(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(['{"ok": true}'])
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )
    prompt = "PRIVATE-CREATIVE-PROMPT-DO-NOT-PERSIST"

    result = gateway.generate_json_text(
        project.id,
        prompt,
        operation="TEST_OPERATION",
        input_source_ids=("story-revision-1",),
        correlation_id="correlation-1",
    )

    assert json.loads(result) == {"ok": True}
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    assert {item.provider_id for item in invocations} == {"TEST_LLM"}
    assert {item.model_id for item in invocations} == {"test-llm-v1"}
    assert {item.input_source_ids for item in invocations} == {
        ("story-revision-1",)
    }
    assert all(item.request_summary["operation"] == "TEST_OPERATION" for item in invocations)
    assert all(item.request_summary["prompt_sha256"] for item in invocations)
    assert prompt not in repr(invocations)


def test_gateway_records_sanitized_failure_without_secret_or_signed_url(tmp_path):
    repository, project = _context(tmp_path)
    secret = "sk-secret-value-1234567890"
    provider = _LLMProvider(
        [
            RuntimeError(
                f"api_key={secret} url=https://example.test/result?Signature=leak"
            )
        ]
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    with pytest.raises(LLMInvocationError):
        gateway.generate_json_text(
            project.id,
            "safe prompt",
            operation="TEST_FAILURE",
            correlation_id="correlation-2",
        )

    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "FAILED"]
    with sqlite3.connect(repository.paths.database) as connection:
        durable = " ".join(
            str(value)
            for row in connection.execute(
                "SELECT request_summary_json FROM ai_invocations"
            )
            for value in row
        )
    assert secret not in durable
    assert "Signature=leak" not in durable
    assert "<redacted>" in durable


def test_story_default_canonical_path_keeps_single_bounded_repair_and_ledgers_both_calls(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(
        [
            "not json",
            json.dumps(valid_bible().model_dump(mode="json"), ensure_ascii=False),
        ]
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )
    service = StoryService(repository, llm_gateway=gateway)

    revision = service.generate_story_bible(
        project,
        brief="一个关于选择的故事",
        genre="悬疑",
        tone="克制",
    )

    assert revision["content"].title
    assert provider.responses == []
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == [
        "STARTED",
        "FAILED",
        "STARTED",
        "SUCCEEDED",
    ]
    assert [item.request_summary["attempt_kind"] for item in invocations] == [
        "PRIMARY",
        "PRIMARY",
        "REPAIR",
        "REPAIR",
    ]
    assert len({item.request_summary["correlation_id"] for item in invocations}) == 1
    assert all(
        item.request_summary["operation"] == "STORY_BIBLE_GENERATION"
        for item in invocations
    )
    assert invocations[1].request_summary["error_code"] == "OUTPUT_INVALID"


def test_gateway_readiness_uses_canonical_selected_endpoint(tmp_path):
    repository, project = _context(tmp_path)
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([_LLMProvider([])]),
    )

    ready, detail = gateway.readiness(project.id)

    assert ready is True
    assert "TEST_LLM" in detail
    assert "MAINLAND_CHINA" in detail


def test_live_smoke_uses_exact_prompt_and_at_most_one_provider_call(tmp_path):
    repository, project = _context(tmp_path)
    seen_prompts = []

    class CapturingProvider(_LLMProvider):
        def generate_json_text(self, prompt: str) -> str:
            seen_prompts.append(prompt)
            return super().generate_json_text(prompt)

    provider = CapturingProvider(["OK"], live_authorized=True)
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    assert gateway.run_live_smoke(project.id, correlation_id="llm-smoke") == "OK"
    assert provider.call_count == 1
    assert LLM_LIVE_SMOKE_PROMPT == "Reply with exactly: OK"
    assert seen_prompts == ["Reply with exactly: OK"]
    ledger = repository.list_ai_invocations(project.id)
    assert [item.status for item in ledger] == ["STARTED", "SUCCEEDED"]
    assert {item.request_summary["operation"] for item in ledger} == {
        "LLM_LIVE_SMOKE"
    }
    assert {item.request_summary["attempt_kind"] for item in ledger} == {"PRIMARY"}


def test_live_smoke_requires_explicit_remote_authorization_before_provider_call(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(["OK"], live_authorized=False)
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    with pytest.raises(LLMInvocationError, match="AIDRAMA_ALLOW_PAID_LIVE_TESTS"):
        gateway.run_live_smoke(project.id)

    assert provider.call_count == 0
    assert repository.list_ai_invocations(project.id) == []


def test_live_smoke_invalid_reply_does_not_enter_repair_loop(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(["Almost OK", "OK"], live_authorized=True)
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    with pytest.raises(LLMInvocationError, match="exactly OK"):
        gateway.run_live_smoke(project.id)

    assert provider.call_count == 1
    assert provider.responses == ["OK"]
    assert [
        item.status for item in repository.list_ai_invocations(project.id)
    ] == ["STARTED", "FAILED"]


def test_local_live_smoke_may_omit_paid_authorization(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(
        ["OK"],
        deployment_region="LOCAL",
        live_authorized=None,
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    assert gateway.run_live_smoke(project.id) == "OK"
    assert provider.call_count == 1


def test_remote_live_smoke_without_authorization_fails_closed(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(["OK"], live_authorized=None)
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    with pytest.raises(LLMInvocationError, match="AIDRAMA_ALLOW_PAID_LIVE_TESTS"):
        gateway.run_live_smoke(project.id)

    assert provider.call_count == 0
    assert repository.list_ai_invocations(project.id) == []


def test_structured_operation_freezes_provider_across_single_repair(tmp_path):
    repository, project = _context(tmp_path)
    provider_a = _LLMProvider(
        ["not-json", '{"ok": true}'],
        provider_name="A_LLM",
        endpoint_profile_id="runtime:LLM:A_LLM:cn",
    )
    provider_b = _LLMProvider(
        ['{"ok": true}'],
        provider_name="B_LLM",
        model="b-v1",
        deployment_region="INTERNATIONAL",
        endpoint_class="TEST_GLOBAL_LLM",
        endpoint_profile_id="runtime:LLM:B_LLM:global",
    )
    registry = CapabilityRegistry([provider_a, provider_b])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.LLM: provider_a.endpoint_profile_id},
    )

    def switch_after_primary(call_number):
        if call_number == 1:
            profiles.save_settings(
                project_id=project.id,
                preset=ProviderPreset.CUSTOM,
                selections={CapabilityKind.LLM: provider_b.endpoint_profile_id},
            )

    provider_a.on_call = switch_after_primary
    gateway = LLMInvocationGateway(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    value = gateway.generate_validated_json(
        project.id,
        "primary",
        operation="FROZEN_OPERATION",
        validator=json.loads,
        repair_prompt_builder=lambda raw, exc: f"repair {raw}",
    )

    assert value == {"ok": True}
    assert provider_a.call_count == 2
    assert provider_b.call_count == 0
    first_operation = repository.list_ai_invocations(project.id)
    assert {item.provider_id for item in first_operation} == {"A_LLM"}
    assert [item.status for item in first_operation] == [
        "STARTED", "FAILED", "STARTED", "SUCCEEDED"
    ]

    gateway.generate_json_text(project.id, "next", operation="NEXT_OPERATION")
    assert provider_b.call_count == 1


def test_invalid_repair_stops_after_two_calls_and_creates_no_revision(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(["bad-primary", "bad-repair"])
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )

    with pytest.raises(Exception, match="一次修复"):
        StoryService(repository, llm_gateway=gateway).generate_story_bible(
            project,
            brief="不会保存",
            genre="悬疑",
            tone="克制",
        )

    assert provider.call_count == 2
    assert repository.list_story_revisions(project.id) == []
    assert [item.status for item in repository.list_ai_invocations(project.id)] == [
        "STARTED", "FAILED", "STARTED", "FAILED"
    ]


def _approved_story(repository, project, gateway):
    service = StoryService(repository, llm_gateway=gateway)
    draft = service.save_draft(project.id, valid_bible())
    return service.approve_revision(draft["id"])


def _valid_script():
    return StructuredScript(
        title="结构化剧本",
        summary="测试",
        scenes=[
            Scene(
                id="scene_001",
                order=1,
                title="末班车",
                location_id="loc_001",
                character_ids=["char_001"],
                estimated_duration_seconds=5,
                source_story_beat_ids=["beat_001"],
                beats=[
                    ScriptBeat(
                        id="script_beat_001",
                        order=1,
                        type=ScriptBeatType.ACTION,
                        text="林舟看向车门。",
                        estimated_duration_seconds=5,
                    )
                ],
            )
        ],
    )


def test_script_canonical_generation_records_approved_story_source(tmp_path):
    repository, project = _context(tmp_path)
    provider = _LLMProvider(
        [json.dumps(_valid_script().model_dump(mode="json"), ensure_ascii=False)],
        upstream_provider_id="deepseek",
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([provider]),
    )
    story = _approved_story(repository, project, gateway)

    revision = ScriptService(repository, llm_gateway=gateway).generate_script(project)

    assert revision["source_story_revision_id"] == story["id"]
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    assert {item.provider_id for item in invocations} == {"deepseek"}
    assert {item.model_id for item in invocations} == {"test-llm-v1"}
    assert {item.input_source_ids for item in invocations} == {(story["id"],)}
    assert all(
        item.request_summary["boundary_provider_id"] == "TEST_LLM"
        for item in invocations
    )


def test_shot_canonical_generation_records_script_and_story_sources(tmp_path):
    repository, project = _context(tmp_path)
    plan_provider = _LLMProvider([])
    gateway = LLMInvocationGateway(
        repository,
        registry=CapabilityRegistry([plan_provider]),
    )
    story = _approved_story(repository, project, gateway)
    script_service = ScriptService(repository, llm_gateway=gateway)
    script_draft = script_service.create_manual_script(project, story)
    script = script_service.approve_revision(script_draft["id"])
    plan = ShotPlan(
        title="镜头计划",
        source_script_revision_id=script["id"],
        shots=[
            Shot(
                id="shot_001",
                order=1,
                scene_id="scene_001",
                source_script_beat_ids=["beat_001"],
                duration_seconds=5,
                subject=["char_001"],
                action="主角等待",
                visual_intent="建立场景",
            )
        ],
    )
    plan_provider.responses.append(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
    )

    revision = ShotService(repository, llm_gateway=gateway).generate_shot_plan(project)

    assert revision["source_script_revision_id"] == script["id"]
    invocations = repository.list_ai_invocations(project.id)
    assert [item.status for item in invocations] == ["STARTED", "SUCCEEDED"]
    assert {item.input_source_ids for item in invocations} == {
        (script["id"], story["id"])
    }
    assert all(
        item.request_summary["operation"] == "SHOT_PLAN_GENERATION"
        for item in invocations
    )


def test_mainland_selection_without_provider_fails_closed_before_remote_call(tmp_path):
    repository, project = _context(tmp_path)
    international = _LLMProvider(
        ['{"ok": true}'],
        provider_name="INTL_LLM",
        deployment_region="INTERNATIONAL",
        endpoint_class="TEST_GLOBAL_LLM",
        endpoint_profile_id="runtime:LLM:INTL_LLM:global",
    )
    registry = CapabilityRegistry([international])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.MAINLAND,
    )
    gateway = LLMInvocationGateway(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    with pytest.raises(LLMInvocationError, match="MAINLAND"):
        gateway.generate_json_text(project.id, "prompt", operation="FAIL_CLOSED")

    assert international.call_count == 0
    assert repository.list_ai_invocations(project.id) == []


def test_mpt_llm_status_uses_frozen_effective_model_and_moonshot_endpoint():
    china = MPTLLMProvider(
        {
            "llm_provider": "moonshot",
            "moonshot_api_key": "in-memory-only",
            "moonshot_model_name": "kimi-k3",
            "moonshot_base_url": "https://api.moonshot.cn/v1",
        }
    ).status
    global_status = MPTLLMProvider(
        {
            "llm_provider": "moonshot",
            "moonshot_api_key": "in-memory-only",
            "moonshot_model_name": "kimi-k3",
            "moonshot_base_url": "https://api.moonshot.ai/v1",
        }
    ).status
    custom = MPTLLMProvider(
        {
            "llm_provider": "moonshot",
            "moonshot_api_key": "in-memory-only",
            "moonshot_model_name": "kimi-k3",
            "moonshot_base_url": "https://proxy.example.test/v1",
        }
    ).status
    deprecated = MPTLLMProvider(
        {
            "llm_provider": "pollinations",
            "pollinations_api_key": "in-memory-only",
            "pollinations_model_name": "default",
            "pollinations_base_url": "https://text.pollinations.ai/openai",
        }
    ).status

    assert china.available and global_status.available and custom.available
    assert china.metadata["deployment_region"] == "MAINLAND_CHINA"
    assert global_status.metadata["deployment_region"] == "INTERNATIONAL"
    assert china.metadata["endpoint_profile_id"].endswith(":china")
    assert global_status.metadata["endpoint_profile_id"].endswith(":global")
    assert custom.metadata["deployment_region"] == "UNSPECIFIED"
    assert custom.metadata["endpoint_profile_id"].endswith(":unspecified")
    assert deprecated.metadata["model"] == "openai-fast"
    assert deprecated.metadata["endpoint_profile_id"].endswith(":default")


def test_invocation_ledger_boundary_removes_secrets_urls_and_absolute_paths(tmp_path):
    repository, project = _context(tmp_path)
    secret = "sk-ledger-secret-1234567890"
    AIInvocationService(repository).record(
        project.id,
        capability="LLM",
        provider_id="provider",
        model_id="model",
        status="FAILED",
        request_summary={
            "api-key": secret,
            "access_token": "token-value",
            "signed_result_url": "https://example.test/a?Signature=leak",
            "error": f"failed at C:\\Users\\private\\asset.png Authorization=Bearer {secret}",
            "credential_reference": "MOONSHOT_API_KEY",
        },
    )

    with sqlite3.connect(repository.paths.database) as connection:
        durable = connection.execute(
            "SELECT request_summary_json FROM ai_invocations"
        ).fetchone()[0]
    assert secret not in durable
    assert "token-value" not in durable
    assert "Signature=leak" not in durable
    assert "C:\\Users\\private" not in durable
    assert "MOONSHOT_API_KEY" in durable
