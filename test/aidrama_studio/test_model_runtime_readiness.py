"""Focused tests for the universal-runtime readiness compatibility seam.

These tests deliberately exercise the new, provider-neutral readiness helper
without constructing a provider or making a network call.  The legacy
``CapabilityStatus.available`` value must not collapse configuration,
runtime reachability, verification, and paid-create authorization into one
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from aidrama_studio.services.model_runtime.readiness import (
    ModelReadiness,
    readiness_from_manifest,
    readiness_from_status,
)
from aidrama_studio.services.ai_capabilities import CapabilityKind, CapabilityStatus


@dataclass
class _Status:
    configured: bool | None = None
    available: bool = True
    verified: bool = False
    reason: str = "runtime reachable"
    metadata: dict[str, object] | None = None


def test_configured_runtime_available_is_independent_from_paid_create_authorization():
    status = _Status(
        configured=True,
        available=True,
        verified=False,
        metadata={
            "requires_create_authorization": True,
            # Absence of an explicit approval must remain unauthorized.
            "configured": True,
        },
    )

    readiness = readiness_from_status(status)

    assert isinstance(readiness, ModelReadiness)
    assert readiness.configured is True
    assert readiness.verified is False
    assert readiness.runtime_available is True
    assert readiness.authorization_required is True
    assert readiness.create_authorized is False
    assert readiness.ready is True
    assert readiness.ready_for_create is False
    assert readiness.authorization_pending is True
    assert readiness.as_dict()["available"] is True


def test_explicit_create_authorization_is_the_only_paid_create_gate():
    status = _Status(
        configured=True,
        available=True,
        metadata={"requires_create_authorization": True},
    )

    denied = readiness_from_status(status, authorization={"create_authorized": False})
    approved = readiness_from_status(status, authorization={"create_authorized": True})

    assert denied.ready is True
    assert denied.ready_for_create is False
    assert approved.ready is True
    assert approved.ready_for_create is True


def test_plain_mapping_status_preserves_top_level_and_nested_readiness_flags():
    readiness = readiness_from_status(
        {
            "configured": True,
            "verified": False,
            "runtime_available": True,
            "authorization_required": True,
            "metadata": {"reason": "configured"},
        }
    )
    assert readiness.configured is True
    assert readiness.runtime_available is True
    assert readiness.authorization_required is True
    assert readiness.create_authorized is False
    assert readiness.ready_for_create is False


def test_readiness_manifest_projection_preserves_independent_flags_and_redacts_nested_secrets():
    manifest = SimpleNamespace(
        authorization={"requires_create_authorization": True},
        readiness={
            "configured": True,
            "verified": False,
            "runtime_available": True,
            "api_key": "credential-placeholder",
            "nested": {
                "Authorization": "AUTH_PLACEHOLDER",
                "signed_url": "https://example.test/result?token=URL_PLACEHOLDER",
                "model": "safe-model",
            },
        },
    )

    readiness = readiness_from_manifest(manifest)
    public = readiness.as_dict()
    rendered = repr(public)

    assert public["configured"] is True
    assert public["verified"] is False
    assert public["runtime_available"] is True
    assert public["authorization_required"] is True
    assert public["create_authorized"] is False
    assert public["ready_for_create"] is False
    assert "credential-placeholder" not in rendered
    assert "AUTH_PLACEHOLDER" not in rendered
    assert "URL_PLACEHOLDER" not in rendered
    assert public["metadata"]["nested"]["model"] == "safe-model"


def test_legacy_capability_status_malformed_authorization_requirement_fails_closed():
    status = CapabilityStatus(
        CapabilityKind.VIDEO_GENERATIVE,
        "LEGACY",
        True,
        "configured",
        {"authorization_required": "false"},
        configured=True,
        runtime_available=True,
    )

    assert status.configured is True
    assert status.runtime_available is True
    assert status.authorization_required is True
    assert status.create_authorized is False


def test_explicit_no_authorization_requirement_is_not_replaced_by_reason_text():
    status = CapabilityStatus(
        CapabilityKind.LLM,
        "EXPLICIT_FREE",
        True,
        "paid wording from an unrelated diagnostic",
        {"live_authorized": False},
        configured=True,
        runtime_available=True,
        authorization_required=False,
    )

    assert status.authorization_required is False
    assert status.create_authorized is True


def test_object_level_authorization_requirement_survives_readiness_bridge():
    status = CapabilityStatus(
        CapabilityKind.VIDEO_GENERATIVE,
        "PAID_OBJECT_STATUS",
        False,
        "paid live authorization is required",
        {"configured": True, "live_authorized": False},
        configured=True,
        authorization_required=True,
    )

    readiness = readiness_from_status(status)

    assert readiness.configured is True
    assert readiness.runtime_available is True
    assert readiness.authorization_required is True
    assert readiness.create_authorized is False
    assert readiness.ready_for_create is False
