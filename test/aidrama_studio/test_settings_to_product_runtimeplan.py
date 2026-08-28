"""Blocking regression for Settings -> Production RuntimePlan wiring.

This module intentionally exercises the shipped settings persistence seam and
the shipped production queue.  It must not call ``create_from_selection``
directly as the assertion subject: the product path is required to cross that
boundary when a future production plan is created.

All provider work is an in-process fake.  The fake status is shaped like the
registered Wan manifest so the queue can resolve the exact selected identity
without touching a network or a paid provider.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import requests

from aidrama_studio.services import (
    BackgroundProductionRunner,
    ProductionQueueService,
    RuntimePlanService,
    ShotKeyframePolicy,
    ShotKeyframeService,
)
from aidrama_studio.services.adapters import MockProductionAdapter
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind as LegacyCapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    RuntimeVideoProvider,
)
from aidrama_studio.services.model_runtime import (
    CapabilityKind as UniversalCapabilityKind,
    default_manifest_registry,
)
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.services.provider_profiles import ProviderProfileService
from test.aidrama_studio.image_fixtures import png_bytes
from test.aidrama_studio.test_production_execution import (
    _ready_job,
    context as _execution_context,
)


@pytest.fixture(autouse=True)
def _offline_temp_data_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep this product-path regression strictly temporary and offline."""

    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    monkeypatch.setenv("AIDRAMA_TEST_NO_NETWORK", "1")
    monkeypatch.setenv("REAL_PROVIDER_CALLS", "0")
    monkeypatch.setenv("PAID_CALLS", "0")
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(tmp_path / "aidrama-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "REAL_PROVIDER_CALLS=0 / PAID_CALLS=0: network opened by regression test"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


class _CredentialPresence:
    """Credential boundary exposing presence only; no value is consumed."""

    def configured(self, key: str) -> bool:
        return key == "DASHSCOPE_API_KEY"


class _WanOfflineAdapter(MockProductionAdapter):
    """Fake adapter whose public identity exactly matches the Wan manifest."""

    model_id = "wan2.7-i2v-2026-04-25"
    requires_shot_first_frame = True

    def submit(self, snapshot: object):
        submission = super().submit(snapshot)
        # Finish the fake task immediately so the runner exercises the exact
        # frozen snapshot seam without polling or any remote side effect.
        self.succeed(submission.runtime_reference)
        return submission

    @property
    def status(self) -> CapabilityStatus:
        return CapabilityStatus(
            LegacyCapabilityKind.VIDEO_GENERATIVE,
            "WAN_VIDEO",
            True,
            "offline test runtime",
            {
                "model": self.model_id,
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "DASHSCOPE_CN",
                "endpoint_profile_id": "DASHSCOPE_CN_BEIJING_V1",
                "credential_reference": "DASHSCOPE_API_KEY",
                "configured": True,
                "authorization_required": False,
                "test_only": True,
            },
            configured=True,
            verified=True,
        )


def _settings_and_queue(tmp_path: Path):
    """Build the real product services against one temporary SQLite database."""

    repository, project = _execution_context.__wrapped__(tmp_path)
    job = _ready_job(repository, project)
    manifests = default_manifest_registry(include_placeholders=False)
    settings = SettingsModelService(
        repository,
        manifest_registry=manifests,
        credential_store=_CredentialPresence(),
    )

    # This is the product Settings action: persist an exact manifest token,
    # rather than invoking RuntimePlanService directly from the test.
    wan_manifest_id = next(
        option.manifest_id
        for option in settings.inventory(UniversalCapabilityKind.VIDEO)
        if option.model_id == "wan2.7-i2v-2026-04-25"
    )
    settings.save_selections(
        project_id=None,
        selections={UniversalCapabilityKind.VIDEO: wan_manifest_id},
    )

    adapter = _WanOfflineAdapter()
    registry = CapabilityRegistry(
        [RuntimeVideoProvider(adapter, provider_name="WAN_VIDEO")]
    )
    profiles = ProviderProfileService(
        repository,
        registry=registry,
        manifest_registry=manifests,
    )
    queue = ProductionQueueService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )
    queue.production_service.create_production_shots(project.id, job.id)
    brief = queue.generation_briefs.prepare_for_job(project.id, job.id)[0]
    snapshot = queue.execution_service.create_input_snapshot(project.id, job.id)
    shot_plan = repository.get_shot_revision(job.shot_plan_revision_id)
    assert shot_plan is not None
    shot = shot_plan["content"].shots[0]
    keyframes = ShotKeyframeService(repository)
    keyframe_brief = keyframes.briefs.compile(snapshot, shot.id, brief)
    selection = ShotKeyframePolicy.select(
        shot,
        project_id=project.id,
        user_source_artifact_id="offline-user-frame",
        user_approval_id="offline-human-approval",
    )
    keyframes.record_user_provided(
        project.id,
        job.id,
        keyframe_brief,
        selection,
        png_bytes(color="blue"),
        filename="shot-001-first-frame.png",
        mime_type="image/png",
    )
    return repository, project, job, settings, wan_manifest_id, queue, adapter


def _authorization(preview):
    return {
        "approved": True,
        "provider_id": preview.provider_id,
        "model_id": preview.model_id,
        "deployment_region": preview.deployment_region,
        "endpoint_profile_id": preview.endpoint_profile_id,
        "endpoint_class": preview.endpoint_class,
        "reference_count": preview.reference_count,
        "estimated_provider_requests": preview.estimated_provider_requests,
        "authorization_fingerprint": preview.authorization_fingerprint,
    }


def test_settings_selection_reaches_production_through_frozen_runtime_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A Settings choice must be consumed by the real Production product path.

    The persisted plan assertions prove the selected manifest is not lost.  The
    call-boundary assertion is the blocking part: the old committed product
    path bypasses ``create_from_selection`` and therefore fails this test even
    though its isolated helper tests pass.
    """

    (
        repository,
        project,
        job,
        settings,
        selected_manifest_id,
        queue,
        adapter,
    ) = _settings_and_queue(tmp_path)
    selected = settings.resolve(project.id, UniversalCapabilityKind.VIDEO)
    assert selected.option.manifest_id == selected_manifest_id

    selection_boundary_calls: list[dict[str, object]] = []
    original = RuntimePlanService.create_from_selection

    def traced_create_from_selection(self, *args, **kwargs):
        project_id = args[0] if args else kwargs.get("project_id")
        brief = kwargs.get("brief")
        selection_boundary_calls.append(
            {
                "project_id": project_id,
                "capability": kwargs.get("capability"),
                "shot_id": getattr(brief, "shot_id", None),
                "selection_service_supplied": kwargs.get("selection_service") is not None,
            }
        )
        return original(self, *args, **kwargs)

    # Observe the product seam; do not invoke it from the test itself.
    monkeypatch.setattr(
        RuntimePlanService,
        "create_from_selection",
        traced_create_from_selection,
    )

    preview = queue.preview_authorization(project.id, job.id)
    task = queue.enqueue_job(
        project.id,
        job.id,
        authorization=_authorization(preview),
    )
    plan_id = task.request_summary["runtime_plan_ids_by_shot"]["shot_001"]
    plan = repository.get_runtime_plan(plan_id)
    assert plan is not None

    # Traceability gate: Settings -> exact manifest -> frozen plan.  These
    # fields must remain identical even when the product path is refactored.
    assert plan.provider_id == selected.option.runtime_provider_id
    assert plan.model_id == selected.option.model_id
    assert plan.endpoint_profile_id == selected.option.runtime_endpoint_profile_id
    assert plan.endpoint_class == selected.option.runtime_endpoint_class
    assert plan.deployment_region == selected.option.deployment_region
    assert plan.provider_parameters["manifest_id"] == selected_manifest_id
    assert plan.provider_parameters["manifest_hash"] == selected.option.manifest_hash
    assert plan.provider_parameters["codec_id"] == selected.option.codec_id
    assert task.request_summary["provider_profile_id"] == selected_manifest_id

    # Continue through the shipped background execution boundary.  The fake
    # adapter receives the same frozen plan; it never opens a network socket.
    runner = BackgroundProductionRunner(
        repository,
        adapter_factory=lambda _task, _plan: adapter,
    )
    runner.run_once(project.id)
    submitted = adapter.submitted_snapshots
    assert len(submitted) == 1
    executed_snapshot = next(iter(submitted.values()))
    assert executed_snapshot.runtime_plan_id == plan.id
    assert executed_snapshot.runtime_plan_hash == plan.plan_hash
    assert executed_snapshot.to_json_dict()["runtime_plan_id"] == plan.id
    executions = [
        execution
        for execution in repository.list_production_executions(job.id)
        if execution.worker_type
        not in {"UNIVERSAL_IMAGE_SHOT_KEYFRAME", "SHOT_FIRST_FRAME_INGEST"}
    ]
    assert len(executions) == 1
    assert executions[0].runtime_plan_id == plan.id
    assert executions[0].input_snapshot.runtime_plan_id == plan.id
    assert executions[0].input_snapshot.runtime_plan_hash == plan.plan_hash

    # Blocking architecture gate.  ``ProductionQueueService`` on BASE_HEAD
    # bypasses this call and will produce the diagnostic below.
    assert len(selection_boundary_calls) == 1, (
        "EXPECTED: user Settings selection must flow through the shipped "
        "RuntimePlanService.create_from_selection boundary before Production "
        "freezes a plan.\n"
        f"ACTUAL: observed {len(selection_boundary_calls)} product call(s); "
        "the persisted plan was created without the selection boundary.\n"
        "LIKELY_PRODUCT_FILES: aidrama_studio/services/production_queue.py; "
        "aidrama_studio/services/runtime_foundation.py\n"
        "SEMANTIC_CONTRACT: Settings selection -> exact manifest -> frozen "
        "RuntimePlan -> exact runtime execution; no layer may silently "
        "substitute provider/model identity."
    )
    call = selection_boundary_calls[0]
    assert call["project_id"] == project.id
    assert str(getattr(call["capability"], "value", call["capability"])) in {
        UniversalCapabilityKind.VIDEO.value,
        LegacyCapabilityKind.VIDEO_GENERATIVE.value,
    }
    assert call["shot_id"] == "shot_001"


__all__ = ["test_settings_selection_reaches_production_through_frozen_runtime_plan"]
