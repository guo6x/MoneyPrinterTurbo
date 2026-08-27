from __future__ import annotations

import pytest

from aidrama_studio.services.model_runtime import (
    AsyncTaskDriver,
    CapabilityRequest,
    CreateAuthorizationError,
    DriverUnsupportedProtocolError,
    InMemoryManifestRegistry,
    JsonAsyncTaskCodec,
    ModelManifest,
    ModelResolutionError,
    ModelResolver,
    ResolverRequest,
    ProtocolDriverRegistry,
    RegionResolutionError,
    UnsupportedCapabilityError,
    UnsupportedProtocolError,
    JsonProviderCodec,
    DriverResponse,
    MalformedProviderResult,
    RuntimeOutcome,
)
from aidrama_studio.services.model_runtime.readiness import readiness_from_status
from aidrama_studio.services.model_runtime.contracts import RuntimeContractError


def _async_manifest(**kwargs: object) -> ModelManifest:
    protocol = kwargs.pop("protocol", "ASYNC_TASK")
    return ModelManifest(
        id="hardening:async:v1",
        provider_id="fake",
        model_id="fake-model",
        capability="VIDEO",
            protocol=protocol,
        codec_id="generic.json",
        readiness={"configured": True, "runtime_available": True, **kwargs.pop("readiness", {})},
        authorization=kwargs.pop("authorization", None),
        **kwargs,
    )


def _request(manifest: ModelManifest, **kwargs: object) -> CapabilityRequest:
    values = {
        "manifest_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        **kwargs,
    }
    return CapabilityRequest(
        request_id="hardening-request",
        capability=manifest.capability,
        protocol_family=manifest.protocol,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=values.pop("manifest_id"),
        manifest_hash=values.pop("manifest_hash"),
        codec_id=manifest.codec_id,
        **values,
    )


def test_protocol_registry_rejects_unknown_family_before_transport() -> None:
    registry = ProtocolDriverRegistry()
    with pytest.raises(DriverUnsupportedProtocolError):
        registry.get("NOT_A_PROTOCOL")


def test_request_authorization_overrides_manifest_default_only_when_explicit() -> None:
    class Transport:
        calls = 0

        def create(self, encoded):
            self.calls += 1
            return {"task_id": "remote-approved"}

    manifest = _async_manifest(
        authorization={"create_is_paid": True, "requires_create_authorization": True},
    )
    transport = Transport()
    driver = AsyncTaskDriver(transport)
    with pytest.raises(CreateAuthorizationError):
        driver.create(_request(manifest), JsonAsyncTaskCodec(), manifest)
    assert transport.calls == 0

    approved = _request(manifest, create_authorized=True)
    submission = driver.create(approved, JsonAsyncTaskCodec(), manifest)
    assert submission.remote_id == "remote-approved"
    assert transport.calls == 1


def test_no_manifest_required_authorization_defaults_to_deny() -> None:
    class Transport:
        calls = 0

        def create(self, encoded):
            self.calls += 1
            return {"task_id": "must-not-create"}

    manifest = _async_manifest()
    request = _request(
        manifest,
        manifest_id="",
        manifest_hash="",
        authorization_required=True,
    )
    transport = Transport()
    with pytest.raises(CreateAuthorizationError):
        AsyncTaskDriver(transport).create(request, JsonAsyncTaskCodec())
    assert transport.calls == 0


def test_optional_cancel_swallows_provider_sdk_failures() -> None:
    class Transport:
        def cancel(self, _reference, _request=None):
            raise RuntimeError("provider does not expose cancellation")

    assert AsyncTaskDriver(Transport()).cancel("remote", JsonAsyncTaskCodec()) is False


def test_explicit_runtime_unavailable_is_not_promoted_by_paid_marker() -> None:
    status = type(
        "Status",
        (),
        {
            "configured": True,
            "verified": False,
            "runtime_available": False,
            "available": False,
            "reason": "paid create authorization required",
            "metadata": {
                "authorization_required": True,
                "live_authorized": False,
            },
        },
    )()
    readiness = readiness_from_status(status)
    assert readiness.runtime_available is False
    assert readiness.ready is False


def test_readiness_accepts_serialized_mapping_snapshots() -> None:
    readiness = readiness_from_status(
        {
            "configured": True,
            "verified": False,
            "runtime_available": True,
            "authorization_required": True,
            "create_authorized": False,
        }
    )
    assert readiness.configured is True
    assert readiness.runtime_available is True
    assert readiness.ready_for_create is False


def test_model_resolver_orders_informational_pricing_without_crossing_region() -> None:
    first = ModelManifest(
        id="pricing-low",
        provider_id="p1",
        model_id="m1",
        capability="LLM",
        protocol="REQUEST_RESPONSE",
        deployment_region="MAINLAND",
        pricing={"cost": "low"},
        readiness={"configured": True, "runtime_available": True},
    )
    second = ModelManifest(
        id="pricing-high",
        provider_id="p2",
        model_id="m2",
        capability="LLM",
        protocol="REQUEST_RESPONSE",
        deployment_region="MAINLAND_CHINA",
        pricing={"cost": "high"},
        readiness={"configured": True, "runtime_available": True},
    )
    resolver = ModelResolver(InMemoryManifestRegistry((first, second)))
    selected = resolver.resolve(
        capability="LLM", region_policy="MAINLAND", cost_preference="high"
    )
    assert selected.manifest_id == "pricing-high"

    with pytest.raises(RegionResolutionError):
        resolver.resolve(capability="LLM", region_policy="INTERNATIONAL")


def test_malformed_result_boolean_fields_fail_closed() -> None:
    manifest = _async_manifest(protocol="REQUEST_RESPONSE")
    request = CapabilityRequest(
        request_id="malformed-bool",
        capability=manifest.capability,
        protocol_family=manifest.protocol,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
    )
    with pytest.raises(MalformedProviderResult):
        JsonProviderCodec().decode_response(
            DriverResponse(
                {"outcome": RuntimeOutcome.SUCCEEDED.value, "retryable": "false"}
            ),
            request,
        )


def test_conflicting_request_selector_aliases_fail_closed() -> None:
    with pytest.raises(ModelResolutionError, match="conflicting model"):
        ResolverRequest(capability="LLM", model_id="model-a", explicit_model="model-b")

    manifest = ModelManifest(
        id="alias-override",
        provider_id="fake",
        model_id="model-a",
        capability="LLM",
        protocol="REQUEST_RESPONSE",
        deployment_region="MAINLAND_CHINA",
        readiness={"configured": True, "runtime_available": True},
    )
    resolver = ModelResolver(InMemoryManifestRegistry((manifest,)))
    request = ResolverRequest(capability="LLM", model_id="model-a")
    with pytest.raises(ModelResolutionError, match="conflicting model"):
        resolver.resolve(request, explicit_model="model-b")


def test_multi_capability_duck_manifest_resolves_requested_capability() -> None:
    class MultiCapabilityManifest:
        id = "multi-capability"
        provider_id = "fake"
        model_id = "multi-model"
        capabilities = ("LLM", "IMAGE")
        protocol = "REQUEST_RESPONSE"
        deployment_region = "MAINLAND_CHINA"
        endpoint_class = "PUBLIC"
        endpoint_profile_id = "multi-profile"
        readiness = {"configured": True, "runtime_available": True}

    manifest = MultiCapabilityManifest()
    resolver = ModelResolver(InMemoryManifestRegistry((manifest,)))
    selected = resolver.resolve(capability="IMAGE", region_policy="MAINLAND")
    assert selected.capability.value == "IMAGE"


def test_request_protocol_alias_conflict_fails_closed() -> None:
    alias_only = CapabilityRequest(
        request_id="protocol-alias-only",
        capability="LLM",
        protocol="STREAM",
    )
    assert alias_only.protocol_family.value == "STREAM"

    with pytest.raises(RuntimeContractError, match="protocol and protocol_family conflict"):
        CapabilityRequest(
            request_id="protocol-conflict",
            capability="LLM",
            protocol_family="REQUEST_RESPONSE",
            protocol="STREAM",
        )


def test_duck_manifest_identity_and_protocol_alias_conflicts_fail_closed() -> None:
    class ConflictingManifest:
        id = "canonical-id"
        manifest_id = "alias-id"
        capability = "LLM"
        capabilities = ("LLM",)
        protocol = "REQUEST_RESPONSE"
        protocol_family = "STREAM"

    with pytest.raises(ModelResolutionError, match="identity aliases conflict"):
        ModelResolver(InMemoryManifestRegistry((ConflictingManifest(),)))

    class ConflictingCapabilityManifest:
        id = "capability-conflict"
        capability = "LLM"
        capabilities = ("IMAGE",)
        protocol = "REQUEST_RESPONSE"

    with pytest.raises(UnsupportedCapabilityError, match="capability.*conflict"):
        ModelResolver(InMemoryManifestRegistry((ConflictingCapabilityManifest(),)))

    class ConflictingProtocolManifest:
        id = "protocol-conflict"
        capability = "LLM"
        protocol = "REQUEST_RESPONSE"
        protocol_family = "STREAM"

    with pytest.raises(UnsupportedProtocolError, match="protocol.*aliases conflict"):
        ModelResolver(InMemoryManifestRegistry((ConflictingProtocolManifest(),)))
