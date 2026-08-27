"""Focused resolver/compatibility tests for the universal model runtime."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aidrama_studio.domain.runtime_operations import ProviderDeploymentRegion
from aidrama_studio.services.model_runtime.manifest import ModelManifest
from aidrama_studio.services.model_runtime.contracts import RuntimeContractError
from aidrama_studio.services.model_runtime.resolver import (
    CompatibilityManifest,
    FrozenIdentityError,
    InMemoryManifestRegistry,
    LegacyCapabilityBridge,
    ModelResolver,
    ModelResolutionError,
    ModelUnavailableError,
    RegionResolutionError,
    UnsupportedCapabilityError,
    UnsupportedProtocolError,
)


def _manifest(
    manifest_id: str,
    *,
    region: str,
    provider: str = "provider",
    model: str = "model-v1",
    protocol: str = "REQUEST_RESPONSE",
    configured: bool = True,
    runtime_available: bool = True,
    create_authorized: bool = False,
    authorization_required: bool = False,
) -> ModelManifest:
    return ModelManifest(
        id=manifest_id,
        display_name=manifest_id,
        provider_id=provider,
        capability="LLM",
        protocol=protocol,
        model_id=model,
        deployment_region=region,
        endpoint_class=f"{region}_PUBLIC",
        endpoint_profile_id=f"endpoint:{manifest_id}",
        codec_id="generic.json",
        readiness={
            "configured": configured,
            "runtime_available": runtime_available,
            "create_authorized": create_authorized,
            "authorization_required": authorization_required,
        },
        authorization={
            "requires_create_authorization": authorization_required,
        },
    )


def test_region_policy_is_fail_closed_and_never_crosses_to_another_region():
    mainland = _manifest("cn", region="MAINLAND_CHINA")
    international = _manifest("intl", region="INTERNATIONAL")
    resolver = ModelResolver(InMemoryManifestRegistry((mainland, international)))

    selected = resolver.resolve(capability="LLM", region_policy="MAINLAND")
    assert selected.manifest_id == "cn"
    assert selected.deployment_region == "MAINLAND_CHINA"

    with pytest.raises(RegionResolutionError, match="cross-region"):
        resolver.resolve(
            capability="LLM",
            manifest_id="intl",
            region_policy="MAINLAND",
        )


def test_frozen_identity_cannot_mutate_provider_model_or_region():
    manifest = _manifest("cn", region="MAINLAND_CHINA")
    resolver = ModelResolver(InMemoryManifestRegistry((manifest,)))
    selected = resolver.resolve(capability="LLM", manifest_id="cn")

    matching = selected.identity
    resolver.assert_frozen(selected, matching)

    mismatched = matching.as_dict()
    mismatched["deployment_region"] = "INTERNATIONAL"
    with pytest.raises(FrozenIdentityError):
        resolver.assert_frozen(selected, mismatched)


def test_configuration_and_runtime_availability_do_not_imply_create_authorization():
    manifest = _manifest(
        "paid",
        region="INTERNATIONAL",
        authorization_required=True,
        create_authorized=False,
    )
    resolver = ModelResolver(InMemoryManifestRegistry((manifest,)))

    selected = resolver.resolve(capability="LLM", require_available=True)
    assert selected.readiness.configured is True
    assert selected.readiness.runtime_available is True
    assert selected.readiness.create_authorized is False
    assert selected.readiness.authorization_required is True
    assert selected.readiness.ready is True
    assert selected.readiness.ready_for_create is False

    with pytest.raises(ModelUnavailableError):
        resolver.resolve(capability="LLM", availability="READY_FOR_CREATE")


def test_unsupported_capability_fails_closed_before_registry_selection():
    resolver = ModelResolver(InMemoryManifestRegistry((_manifest("m", region="CUSTOM"),)))

    with pytest.raises(UnsupportedCapabilityError):
        resolver.resolve(capability="AUDIO")


def test_unsupported_protocol_fails_closed_at_manifest_registration():
    with pytest.raises(RuntimeContractError):
        _manifest("bad-protocol", region="CUSTOM", protocol="WEBSOCKET")

    class MalformedManifest:
        id = "malformed"
        capability = "LLM"
        protocol = "WEBSOCKET"

    with pytest.raises(UnsupportedProtocolError):
        InMemoryManifestRegistry((MalformedManifest(),))


@dataclass
class _LegacyProvider:
    provider_name: str = "LEGACY_LLM"

    @property
    def status(self):
        return type(
            "Status",
            (),
            {
                "available": True,
                "configured": True,
                "verified": False,
                "reason": "configured",
                "metadata": {
                    "model": "legacy-model",
                    "deployment_region": "MAINLAND_CHINA",
                    "endpoint_class": "LEGACY_PUBLIC",
                    "endpoint_profile_id": "legacy-endpoint",
                    "credential_reference": "LEGACY_API_KEY",
                    "api_key": "credential-placeholder",
                },
            },
        )()


class _LegacyRegistry:
    def __init__(self):
        self.provider = _LegacyProvider()

    def all_status(self):
        return {"LLM": self.provider.status}

    def list(self, capability=None):
        return (self.provider,) if capability in (None, "LLM") else ()


def test_legacy_bridge_preserves_exact_provider_and_does_not_publish_secret_values():
    bridge = LegacyCapabilityBridge(_LegacyRegistry())
    manifests = bridge.manifests("LLM")
    assert len(manifests) == 1
    manifest = manifests[0]
    public = manifest.as_dict()
    rendered = repr(public)

    assert manifest.provider_id == "LEGACY_LLM"
    assert manifest.model_id == "legacy-model"
    assert bridge.provider_for(manifest).provider_name == "LEGACY_LLM"
    assert "credential-placeholder" not in rendered
    assert "api_key" not in rendered

    resolver = ModelResolver(bridge.manifest_registry(), legacy_bridge=bridge)
    selected = resolver.resolve(capability="LLM", manifest_id=manifest.manifest_id)
    assert resolver.provider_for(selected).provider_name == "LEGACY_LLM"

    serialized = manifest.serialize()
    assert serialized == manifest.canonical_json()
    assert serialized == manifest.to_json()
    assert manifest.validate() is manifest
    assert "credential-placeholder" not in serialized
    restored = CompatibilityManifest.from_json(serialized)
    assert restored.manifest_hash == manifest.manifest_hash


def test_mapping_registry_may_be_keyed_by_manifest_id_without_capability_cast():
    manifest = _manifest("manifest-key", region="MAINLAND_CHINA")
    resolver = ModelResolver({manifest.manifest_id: manifest})

    selected = resolver.resolve(capability="LLM", region_policy="MAINLAND")

    assert selected.manifest_id == manifest.manifest_id


def test_implicit_lookup_rejects_unknown_region_mixed_with_known_region():
    unknown = _manifest("unknown-region", region="UNSPECIFIED")
    international = _manifest("international", region="INTERNATIONAL")
    resolver = ModelResolver(InMemoryManifestRegistry((unknown, international)))

    with pytest.raises(RegionResolutionError, match="cross-region"):
        resolver.resolve(capability="LLM")


def test_explicit_model_selector_still_requires_region_when_model_spans_regions():
    mainland = _manifest("cn-model", region="MAINLAND_CHINA", model="shared-model")
    international = _manifest(
        "intl-model", region="INTERNATIONAL", model="shared-model"
    )
    resolver = ModelResolver(InMemoryManifestRegistry((mainland, international)))

    with pytest.raises(RegionResolutionError, match="cross-region"):
        resolver.resolve(capability="LLM", model_id="shared-model")

    selected = resolver.resolve(
        capability="LLM", model_id="shared-model", region_policy="INTERNATIONAL"
    )
    assert selected.manifest_id == "intl-model"


def test_region_policy_accepts_legacy_deployment_region_enum_values():
    mainland = _manifest("legacy-region", region="MAINLAND_CHINA")
    resolver = ModelResolver(InMemoryManifestRegistry((mainland,)))

    selected = resolver.resolve(
        capability="LLM", region_policy=ProviderDeploymentRegion.MAINLAND_CHINA
    )

    assert selected.manifest_id == "legacy-region"


def test_custom_policy_without_exact_region_cannot_choose_across_deployments():
    mainland = _manifest("custom-cn", region="MAINLAND_CHINA", model="shared-custom")
    international = _manifest(
        "custom-intl", region="INTERNATIONAL", model="shared-custom"
    )
    resolver = ModelResolver(InMemoryManifestRegistry((mainland, international)))

    with pytest.raises(RegionResolutionError, match="cross-region"):
        resolver.resolve(
            capability="LLM", model_id="shared-custom", region_policy="CUSTOM"
        )


def _compat_manifest(**overrides: object) -> CompatibilityManifest:
    values: dict[str, object] = {
        "manifest_id": "runtime:IMAGE:OPENAI:PUBLIC",
        "display_name": "OpenAI Image",
        "provider_id": "OPENAI",
        "capability": "IMAGE",
        "protocol": "REQUEST_RESPONSE",
        "model_id": "gpt-image-1",
        "deployment_region": "INTERNATIONAL",
        "endpoint_class": "PUBLIC",
        "endpoint_profile_id": "https://api.example.com/v1",
    }
    values.update(overrides)
    return CompatibilityManifest(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_id", "https://api.example.com/v1?X-Amz-Signature=secret"),
        ("display_name", "Bearer sk-live-secret"),
        ("provider_id", "C:\\Users\\alice\\provider"),
        ("model_id", "eyJhbGciOiJIUzI1NiJ9.payload.signature"),
        ("deployment_region", "/private/runtime/region"),
        ("endpoint_class", "https://user:password@example.test/v1"),
        ("endpoint_profile_id", "https://api.example.com/v1?sig=secret"),
        ("codec_id", "api_key=sk-live-secret"),
    ],
)
def test_compatibility_manifest_rejects_unsafe_identity_scalars(
    field: str, value: str
) -> None:
    with pytest.raises(ModelResolutionError):
        _compat_manifest(**{field: value})


def test_compatibility_manifest_allows_credential_free_endpoint_identity() -> None:
    manifest = _compat_manifest(endpoint_profile_id="https://api.example.com/v1")

    rendered = manifest.serialize()

    assert "https://api.example.com/v1" in rendered
    assert "secret" not in rendered.casefold()


class _UnsafeLegacyProvider:
    provider_name = "LEGACY_LLM"

    @property
    def status(self):
        return type(
            "Status",
            (),
            {
                "available": True,
                "configured": True,
                "verified": False,
                "reason": "configured",
                "metadata": {
                    "model": "legacy-model",
                    "deployment_region": "INTERNATIONAL",
                    "endpoint_class": "LEGACY_PUBLIC",
                    "endpoint_profile_id": "https://api.example.com/v1?sig=do-not-persist",
                },
            },
        )()


class _UnsafeLegacyRegistry:
    def __init__(self):
        self.provider = _UnsafeLegacyProvider()

    def all_status(self):
        return {"LLM": self.provider.status}

    def list(self, capability=None):
        return (self.provider,) if capability in (None, "LLM") else ()


def test_legacy_bridge_skips_provider_rows_with_unsafe_identity_metadata() -> None:
    bridge = LegacyCapabilityBridge(_UnsafeLegacyRegistry())

    assert bridge.manifests("LLM") == ()
    assert "do-not-persist" not in repr(bridge.manifests())


def test_runtime_status_from_another_provider_cannot_mark_manifest_ready():
    manifest = _manifest("status-identity", region="MAINLAND_CHINA", provider="provider-a")
    status = {
        "configured": True,
        "runtime_available": True,
        "metadata": {
            "provider_id": "provider-b",
            "model_id": manifest.model_id,
            "deployment_region": "MAINLAND_CHINA",
        },
    }
    resolver = ModelResolver(
        InMemoryManifestRegistry((manifest,)),
        readiness_resolver={manifest.manifest_id: status},
    )

    with pytest.raises(RegionResolutionError, match="provider"):
        resolver.resolve(capability="LLM", manifest_id=manifest.manifest_id)
