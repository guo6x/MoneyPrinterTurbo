from __future__ import annotations

import math

import pytest

from aidrama_studio.services.model_runtime import ManifestValidationError, ModelManifest


def _manifest(**kwargs: object) -> ModelManifest:
    values: dict[str, object] = {
        "id": "manifest-hardening",
        "provider_id": "fake",
        "model_id": "fake-model",
    }
    values.update(kwargs)
    return ModelManifest(**values)


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [
        ({"capability": "LLM"}, {"capabilities": ["IMAGE"]}),
        ({"protocol": "REQUEST_RESPONSE"}, {"protocol_family": "STREAM"}),
        ({"id": "canonical"}, {"manifest_id": "other"}),
        ({"display_name": "canonical"}, {"label": "other"}),
    ],
)
def test_conflicting_manifest_aliases_fail_closed(
    canonical: dict[str, object], alias: dict[str, object]
) -> None:
    with pytest.raises(ManifestValidationError):
        _manifest(**canonical, **alias)


def test_equal_aliases_round_trip_without_mutating_identity() -> None:
    manifest = _manifest(capability="IMAGE", protocol="STREAM", metadata={"safe": {"value": 1}})
    restored = ModelManifest.from_dict(manifest.to_dict())
    assert restored.manifest_hash == manifest.manifest_hash
    with pytest.raises(TypeError):
        restored.metadata["safe"]["value"] = 2  # type: ignore[index]


@pytest.mark.parametrize(
    "duration",
    [
        {"minimum": math.nan},
        {"maximum": math.inf},
        {"discrete_values": ["2"]},
        {"discrete_values": [10**1000]},
    ],
)
def test_duration_values_must_be_finite_numbers(duration: dict[str, object]) -> None:
    with pytest.raises(ManifestValidationError):
        _manifest(duration=duration)


@pytest.mark.parametrize(
    "metadata",
    [
        {"path": "/private/project/input.png"},
        {"storage_path": "relative/private/input.png"},
        {"url": "https://user:password@example.test/model"},
        {"url": "https://example.test/model?X-Amz-Signature=secret"},
        {"nested": {"value": math.nan}},
    ],
)
def test_manifest_metadata_cannot_carry_paths_or_secrets(metadata: dict[str, object]) -> None:
    with pytest.raises(ManifestValidationError):
        _manifest(metadata=metadata)


def test_reference_boolean_count_is_not_an_integer() -> None:
    with pytest.raises(ManifestValidationError):
        _manifest(reference={"max_count": True})


def test_nested_readiness_aliases_are_normalized_without_overriding_flags() -> None:
    manifest = _manifest(
        readiness={
            "verification_state": "VERIFIED",
            "paid_create_requires_authorization": True,
        }
    )
    assert manifest.verified is True
    assert manifest.authorization_required is True
    with pytest.raises(ManifestValidationError):
        _manifest(readiness={"verification_state": "VERIFIED", "verified": False})
