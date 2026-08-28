from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from aidrama_studio.domain import ProviderDeploymentRegion, ProviderPreset
from aidrama_studio.services import ProjectService
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
    CapabilityUnavailable,
)
from aidrama_studio.services.provider_profiles import (
    ProviderProfileError,
    ProviderProfileService,
    ProviderSelectionState,
)
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


@dataclass
class _Provider:
    capability: CapabilityKind
    provider_name: str
    region: ProviderDeploymentRegion
    endpoint: str
    available: bool = True
    configured: bool = True
    model: str = "model-v1"

    @property
    def status(self) -> CapabilityStatus:
        metadata = {
            "model": self.model,
            "deployment_region": self.region.value,
            "endpoint_class": self.endpoint,
            "endpoint_profile_id": (
                f"runtime:{self.capability.value}:{self.provider_name}:{self.endpoint}"
            ),
            "configured": self.configured,
            "verification_state": "NOT_VERIFIED",
        }
        if self.capability is CapabilityKind.VIDEO_GENERATIVE:
            metadata.update(
                {
                    "minimum_duration_seconds": 2,
                    "maximum_duration_seconds": 15,
                    "supported_durations": list(range(2, 16)),
                    "native_generation_resolution": "720p",
                    "native_generation_fps": 24,
                }
            )
        return CapabilityStatus(
            self.capability,
            self.provider_name,
            self.available,
            "configured" if self.configured else "credential unavailable",
            metadata,
            configured=self.configured,
            verified=False,
        )


def _context(tmp_path):
    paths = DatabasePaths(
        database=tmp_path / "aidrama" / "aidrama.db",
        projects=tmp_path / "aidrama" / "projects",
        archived_projects=tmp_path / "aidrama" / "archived",
    )
    repository = ProjectRepository(paths)
    project = ProjectService(repository).create(title="Provider Selection")
    return repository, project


def _registry(*, mainland_available: bool = True) -> CapabilityRegistry:
    providers = []
    for capability in ProviderProfileService.PRODUCT_CAPABILITIES:
        providers.extend(
            (
                _Provider(
                    capability,
                    f"CN_{capability.value}",
                    ProviderDeploymentRegion.MAINLAND_CHINA,
                    f"CN_{capability.value}_ENDPOINT",
                    available=mainland_available,
                    configured=True,
                ),
                _Provider(
                    capability,
                    f"INTL_{capability.value}",
                    ProviderDeploymentRegion.INTERNATIONAL,
                    f"INTL_{capability.value}_ENDPOINT",
                ),
            )
        )
    return CapabilityRegistry(providers)


def test_mainland_and_international_presets_change_all_capability_resolution(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(repository, registry=_registry())

    service.save_settings(project_id=None, preset=ProviderPreset.MAINLAND)
    mainland = {
        capability.value: service.resolve(project.id, capability)
        for capability in service.PRODUCT_CAPABILITIES
    }
    assert all(item.profile.provider_id.startswith("CN_") for item in mainland.values())
    assert all(item.source == "GLOBAL_DEFAULT" for item in mainland.values())

    service.save_settings(project_id=None, preset=ProviderPreset.INTERNATIONAL)
    international = {
        capability.value: service.resolve(project.id, capability)
        for capability in service.PRODUCT_CAPABILITIES
    }
    assert all(item.profile.provider_id.startswith("INTL_") for item in international.values())
    assert all(
        international[key].profile.endpoint_profile_id
        != mainland[key].profile.endpoint_profile_id
        for key in international
    )


def test_custom_mixed_configuration_is_independent_per_capability(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(repository, registry=_registry())
    selections = {}
    expected = {}
    for index, capability in enumerate(service.PRODUCT_CAPABILITIES):
        profiles = service.inventory(project.id, capability)
        wanted_region = (
            ProviderDeploymentRegion.MAINLAND_CHINA
            if index % 2 == 0
            else ProviderDeploymentRegion.INTERNATIONAL
        )
        selected = next(item for item in profiles if item.deployment_region is wanted_region)
        selections[capability.value] = selected.id
        expected[capability.value] = selected.provider_id

    service.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections=selections,
    )

    resolved = service.public_selection(project.id)
    assert {item["capability"]: item["provider_id"] for item in resolved} == expected
    assert {item["source"] for item in resolved} == {"PROJECT_DEFAULT"}


def test_missing_or_failed_mainland_provider_is_unavailable_without_cross_region_fallback(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(
        repository,
        registry=_registry(mainland_available=False),
    )
    cn_video = next(
        item
        for item in service.inventory(project.id, CapabilityKind.VIDEO_GENERATIVE)
        if item.deployment_region is ProviderDeploymentRegion.MAINLAND_CHINA
    )
    service.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.VIDEO_GENERATIVE: cn_video.id},
    )

    resolution = service.resolve(
        project.id,
        CapabilityKind.VIDEO_GENERATIVE,
        require_available=True,
    )
    assert resolution.state is ProviderSelectionState.UNAVAILABLE
    assert resolution.profile.provider_id.startswith("CN_")
    assert "fallback" in resolution.detail
    with pytest.raises(CapabilityUnavailable, match="fallback"):
        service.select(project.id, CapabilityKind.VIDEO_GENERATIVE)


def test_project_default_beats_global_and_job_override_beats_project(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(repository, registry=_registry())
    service.save_settings(project_id=None, preset=ProviderPreset.INTERNATIONAL)
    service.save_settings(project_id=project.id, preset=ProviderPreset.MAINLAND)

    project_selection = service.resolve(project.id, CapabilityKind.VIDEO_GENERATIVE)
    assert project_selection.source == "PROJECT_DEFAULT"
    assert project_selection.profile.deployment_region is ProviderDeploymentRegion.MAINLAND_CHINA

    international = next(
        item
        for item in service.inventory(project.id, CapabilityKind.VIDEO_GENERATIVE)
        if item.deployment_region is ProviderDeploymentRegion.INTERNATIONAL
    )
    job_selection = service.resolve(
        project.id,
        CapabilityKind.VIDEO_GENERATIVE,
        endpoint_profile_id=international.endpoint_profile_id,
    )
    assert job_selection.source == "JOB_OVERRIDE"
    assert job_selection.profile.endpoint_profile_id == international.endpoint_profile_id


def test_shared_endpoint_requires_exact_model_profile_identity(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(repository)
    qwen = service.register(
        capability=CapabilityKind.IMAGE,
        provider_id="ALIBABA_MODEL_STUDIO",
        model_id="qwen-image-3.0",
        project_id=project.id,
        endpoint_profile_id="DASHSCOPE_IMAGE_SHARED",
    )
    z_image = service.register(
        capability=CapabilityKind.IMAGE,
        provider_id="ALIBABA_MODEL_STUDIO",
        model_id="z-image-turbo",
        project_id=project.id,
        endpoint_profile_id="DASHSCOPE_IMAGE_SHARED",
    )

    with pytest.raises(ProviderProfileError, match="exact manifest/profile ID"):
        service.resolve(
            project.id,
            CapabilityKind.IMAGE,
            endpoint_profile_id="DASHSCOPE_IMAGE_SHARED",
        )
    with pytest.raises(ProviderProfileError, match="exact manifest/profile ID"):
        service.resolve(
            project.id,
            CapabilityKind.IMAGE,
            provider_id="ALIBABA_MODEL_STUDIO",
        )

    service.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.IMAGE: z_image.id},
    )
    assert (
        service.resolve(project.id, CapabilityKind.IMAGE).profile.model_id
        == "z-image-turbo"
    )
    assert (
        service.resolve(
            project.id,
            CapabilityKind.IMAGE,
            endpoint_profile_id=qwen.id,
        ).profile.model_id
        == "qwen-image-3.0"
    )


def test_profile_registration_rejects_nested_secrets_and_unsafe_endpoints(tmp_path):
    repository, project = _context(tmp_path)
    service = ProviderProfileService(repository)

    with pytest.raises(ProviderProfileError, match="secret"):
        service.register(
            capability=CapabilityKind.VISION,
            provider_id="SAFE",
            model_id="model",
            deployment_region=ProviderDeploymentRegion.INTERNATIONAL,
            endpoint_class="PUBLIC",
            profile={"headers": {"access_token": "must-not-persist"}},
        )
    with pytest.raises(ProviderProfileError, match="HTTPS"):
        service.register(
            capability=CapabilityKind.VISION,
            provider_id="SAFE",
            model_id="model",
            deployment_region=ProviderDeploymentRegion.INTERNATIONAL,
            endpoint_class="PUBLIC",
            endpoint_url="http://user:pass@example.test/v1?token=leak",
        )

    safe = service.register(
        capability=CapabilityKind.VISION,
        provider_id="SAFE",
        model_id="model",
        project_id=project.id,
        deployment_region=ProviderDeploymentRegion.INTERNATIONAL,
        endpoint_class="PUBLIC",
        endpoint_url="https://api.example.test/v1",
        credential_reference="SAFE_API_KEY",
        profile={"maximum_duration_seconds": 30},
    )
    assert safe.credential_reference == "SAFE_API_KEY"
    with sqlite3.connect(repository.paths.database) as connection:
        persisted = " ".join(
            str(value)
            for row in connection.execute(
                "SELECT credential_reference,profile_json,endpoint_url FROM provider_capability_profiles"
            )
            for value in row
            if value is not None
        )
    assert "must-not-persist" not in persisted
    assert "user:pass" not in persisted


def test_preset_with_no_configured_provider_remains_unavailable(tmp_path):
    repository, project = _context(tmp_path)
    registry = CapabilityRegistry(
        [
            _Provider(
                CapabilityKind.IMAGE,
                "ONLY_INTL_IMAGE",
                ProviderDeploymentRegion.INTERNATIONAL,
                "INTL_IMAGE",
            )
        ]
    )
    service = ProviderProfileService(repository, registry=registry)
    service.save_settings(project_id=None, preset=ProviderPreset.MAINLAND)

    result = service.resolve(project.id, CapabilityKind.IMAGE)
    assert result.state is ProviderSelectionState.UNAVAILABLE
    assert result.profile is None
    assert result.as_public_dict()["provider_id"] == "UNAVAILABLE"
