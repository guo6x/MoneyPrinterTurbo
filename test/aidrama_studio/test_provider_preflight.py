from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from aidrama_studio.domain import ProviderDeploymentRegion, ProviderPreset
from aidrama_studio.services import ProjectService
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
)
from aidrama_studio.services.provider_preflight import OfflineLivePreflightService
from aidrama_studio.services.provider_profiles import ProviderProfileService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


@dataclass
class _OfflineProvider:
    capability: CapabilityKind
    provider_name: str
    model: str
    endpoint: str
    credential_reference: str | None
    available: bool = True
    configured: bool = True
    live_authorized: bool | None = True
    deployment_region: str = "INTERNATIONAL"
    endpoint_class: str = "TEST_PUBLIC"
    metadata_overrides: dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        self.network_calls = 0

    @property
    def status(self):
        metadata = {
            "model": self.model,
            "endpoint_profile_id": self.endpoint,
            "endpoint_class": self.endpoint_class,
            "deployment_region": self.deployment_region,
            "credential_reference": self.credential_reference,
            "credential_present": self.configured,
            "configured": self.configured,
            "provider_constraints_valid": True,
            **self.metadata_overrides,
        }
        if self.live_authorized is not None:
            metadata["live_authorized"] = self.live_authorized
        return CapabilityStatus(
            self.capability,
            self.provider_name,
            self.available,
            "configured" if self.available else "configuration required",
            metadata,
            configured=self.configured,
        )


class _ExplodingValidator:
    """A third-party-looking config whose validator must never be called."""

    model = "test-model"
    base_url = "https://example.test/v1"

    def __init__(self):
        self.calls = 0

    def validate(self):
        self.calls += 1
        raise AssertionError("arbitrary validator invoked during offline preflight")


def _context(tmp_path):
    repository = ProjectRepository(
        DatabasePaths(
            tmp_path / "data" / "aidrama.db",
            tmp_path / "data" / "projects",
            tmp_path / "data" / "archived",
        )
    )
    project = ProjectService(repository).create(title="Offline preflight")
    return repository, project


def test_offline_preflight_reports_five_exact_selected_profiles_without_calls(
    tmp_path,
):
    repository, project = _context(tmp_path)
    providers = []
    selections = {}
    for capability, prefix in (
        (CapabilityKind.LLM, "LLM"),
        (CapabilityKind.IMAGE, "IMAGE"),
        (CapabilityKind.VIDEO_GENERATIVE, "VIDEO"),
        (CapabilityKind.VISION, "VISION"),
        (CapabilityKind.TTS, "TTS"),
    ):
        endpoint = f"runtime:{capability.value}:TEST_{prefix}:TEST_PUBLIC"
        provider = _OfflineProvider(
            capability,
            f"TEST_{prefix}",
            f"test-{prefix.casefold()}-model",
            endpoint,
            f"{prefix}_API_KEY",
            live_authorized=True,
        )
        providers.append(provider)
        selections[capability] = endpoint
    registry = CapabilityRegistry(providers)
    providers[0].config = _ExplodingValidator()
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections=selections,
    )

    service = OfflineLivePreflightService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )
    snapshot = service.snapshot(project.id)

    assert snapshot["LLM_PROFILE_READY"] is True
    assert snapshot["IMAGE_PROFILE_READY"] is True
    assert snapshot["VIDEO_PROFILE_READY"] is True
    assert snapshot["VISION_PROFILE_READY"] is True
    assert snapshot["TTS_PROFILE_READY"] is True
    assert snapshot["profiles"]["VIDEO_GENERATIVE"]["provider_id"] == "TEST_VIDEO"
    assert snapshot["profiles"]["TTS"]["credential"] == {
        "name": "TTS_API_KEY",
        "status": "PRESENT",
    }
    assert service.report_lines(project.id) == (
        "LLM_PROFILE_READY=PASS",
        "IMAGE_PROFILE_READY=PASS",
        "VIDEO_PROFILE_READY=PASS",
        "VISION_PROFILE_READY=PASS",
        "TTS_PROFILE_READY=PASS",
    )
    assert all(provider.network_calls == 0 for provider in providers)
    assert providers[0].config.calls == 0


def test_offline_preflight_reports_missing_secret_by_name_and_status_only(tmp_path):
    repository, project = _context(tmp_path)
    secret = "super-secret-value-that-must-never-appear"
    endpoint = "runtime:TTS:MPT_TTS:AZURE_SPEECH_PUBLIC"
    provider = _OfflineProvider(
        CapabilityKind.TTS,
        "MPT_TTS",
        "AZURE_SPEECH_NEURAL_TTS",
        endpoint,
        "AZURE_SPEECH_KEY",
        available=False,
        configured=False,
        live_authorized=False,
        metadata_overrides={
            "upstream_provider_id": "AZURE_SPEECH",
            "region_configured": False,
            "runtime_ready": True,
            "voice": "zh-CN-XiaoxiaoMultilingualNeural-V2-Female",
        },
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.TTS: endpoint},
    )

    snapshot = OfflineLivePreflightService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    ).snapshot(project.id)

    tts = snapshot["profiles"]["TTS"]
    assert snapshot["TTS_PROFILE_READY"] is False
    assert tts["credential"] == {
        "name": "AZURE_SPEECH_KEY",
        "status": "MISSING",
    }
    assert tts["paid_authorization"] == "MISSING"
    assert secret not in repr(snapshot)
    assert str(len(secret)) not in repr(tts["credential"])
    assert provider.network_calls == 0


@pytest.mark.parametrize(
    ("deployment_region", "credential_reference", "expected_ready"),
    [
        ("INTERNATIONAL", "REMOTE_API_KEY", False),
        ("LOCAL", None, True),
    ],
)
def test_offline_preflight_requires_explicit_authorization_for_remote_profiles(
    tmp_path, deployment_region, credential_reference, expected_ready
):
    repository, project = _context(tmp_path)
    endpoint = f"runtime:LLM:TEST_LLM:{deployment_region}"
    provider = _OfflineProvider(
        CapabilityKind.LLM,
        "TEST_LLM",
        "test-llm-model",
        endpoint,
        credential_reference,
        deployment_region=deployment_region,
        endpoint_class="TEST_PUBLIC" if deployment_region != "LOCAL" else "TEST_LOCAL",
        live_authorized=None,
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.LLM: endpoint},
    )

    result = next(
        item
        for item in OfflineLivePreflightService(
            repository,
            registry=registry,
            provider_profiles=profiles,
        ).run(project.id)
        if item.capability == CapabilityKind.LLM.value
    )

    assert result.ready is expected_ready
    assert result.paid_authorization_status == (
        "NOT_REQUIRED" if deployment_region == "LOCAL" else "MISSING"
    )
    assert provider.network_calls == 0


@pytest.mark.parametrize("runtime_endpoint", ["", "runtime:IMAGE:TEST_IMAGE:OTHER"])
def test_offline_preflight_requires_exact_runtime_endpoint_identity(
    tmp_path, runtime_endpoint
):
    repository, project = _context(tmp_path)
    selected_endpoint = "runtime:IMAGE:TEST_IMAGE:EXPECTED"
    provider = _OfflineProvider(
        CapabilityKind.IMAGE,
        "TEST_IMAGE",
        "test-image-model",
        runtime_endpoint,
        "IMAGE_API_KEY",
        metadata_overrides={"endpoint_profile_id": runtime_endpoint},
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    # Persist a project-scoped profile so the runtime metadata is checked
    # against an independently selected endpoint identity.
    profiles.register(
        project_id=project.id,
        capability=CapabilityKind.IMAGE,
        provider_id="TEST_IMAGE",
        model_id="test-image-model",
        endpoint_profile_id=selected_endpoint,
        deployment_region=ProviderDeploymentRegion.INTERNATIONAL,
        endpoint_class="TEST_PUBLIC",
        credential_reference="IMAGE_API_KEY",
    )
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.IMAGE: selected_endpoint},
    )

    image = next(
        item
        for item in OfflineLivePreflightService(
            repository,
            registry=registry,
            provider_profiles=profiles,
        ).run(project.id)
        if item.capability == CapabilityKind.IMAGE.value
    )
    assert image.ready is False
    assert image.provider_constraints_status == "ERROR"
    assert provider.network_calls == 0


def test_offline_preflight_rejects_tampered_seedance_duration_profile(tmp_path):
    repository, project = _context(tmp_path)
    endpoint = "runtime:VIDEO_GENERATIVE:SEEDANCE:TEST_PUBLIC"
    provider = _OfflineProvider(
        CapabilityKind.VIDEO_GENERATIVE,
        "SEEDANCE",
        "seedance-test-model",
        endpoint,
        "ARK_API_KEY",
        live_authorized=True,
        metadata_overrides={
            "requires_explicit_selection": True,
            "minimum_duration_seconds": 4,
            "maximum_duration_seconds": 30,
            "supported_durations": list(range(4, 31)),
        },
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.VIDEO_GENERATIVE: endpoint},
    )
    service = OfflineLivePreflightService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    assert service.snapshot(project.id)["VIDEO_PROFILE_READY"] is True
    provider.metadata_overrides["maximum_duration_seconds"] = 15
    failed = service.snapshot(project.id)
    assert failed["VIDEO_PROFILE_READY"] is False
    assert (
        failed["profiles"]["VIDEO_GENERATIVE"]["provider_constraints"]
        == "ERROR"
    )
    assert provider.network_calls == 0


def test_offline_preflight_requires_literal_credential_presence_proof(tmp_path):
    repository, project = _context(tmp_path)
    endpoint = "runtime:IMAGE:TEST_IMAGE:TEST_PUBLIC"
    provider = _OfflineProvider(
        CapabilityKind.IMAGE,
        "TEST_IMAGE",
        "test-image-model",
        endpoint,
        "IMAGE_API_KEY",
        metadata_overrides={"credential_present": None},
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.IMAGE: endpoint},
    )

    result = next(
        item
        for item in OfflineLivePreflightService(
            repository,
            registry=registry,
            provider_profiles=profiles,
        ).run(project.id)
        if item.capability == CapabilityKind.IMAGE.value
    )

    assert result.ready is False
    assert result.credential_status == "MISSING"
    assert provider.network_calls == 0


def test_offline_preflight_applies_seedance_constraints_to_provider_alias(tmp_path):
    repository, project = _context(tmp_path)
    endpoint = "runtime:VIDEO_GENERATIVE:SEEDANCE_VIDEO:TEST_PUBLIC"
    provider = _OfflineProvider(
        CapabilityKind.VIDEO_GENERATIVE,
        "SEEDANCE_VIDEO",
        "seedance-test-model",
        endpoint,
        "ARK_API_KEY",
        live_authorized=True,
        metadata_overrides={
            "requires_explicit_selection": True,
            "minimum_duration_seconds": 4,
            "maximum_duration_seconds": 30,
            "supported_durations": list(range(4, 31)),
        },
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.VIDEO_GENERATIVE: endpoint},
    )
    service = OfflineLivePreflightService(
        repository,
        registry=registry,
        provider_profiles=profiles,
    )

    assert service.snapshot(project.id)["VIDEO_PROFILE_READY"] is True
    provider.metadata_overrides["supported_durations"] = list(range(2, 16))
    assert service.snapshot(project.id)["VIDEO_PROFILE_READY"] is False
    assert provider.network_calls == 0


def test_offline_preflight_rejects_unknown_configless_provider_without_proof(
    tmp_path,
):
    repository, project = _context(tmp_path)
    endpoint = "runtime:IMAGE:UNKNOWN_IMAGE:TEST_PUBLIC"
    provider = _OfflineProvider(
        CapabilityKind.IMAGE,
        "UNKNOWN_IMAGE",
        "unknown-image-model",
        endpoint,
        "IMAGE_API_KEY",
        metadata_overrides={"provider_constraints_valid": None},
    )
    registry = CapabilityRegistry([provider])
    profiles = ProviderProfileService(repository, registry=registry)
    profiles.save_settings(
        project_id=project.id,
        preset=ProviderPreset.CUSTOM,
        selections={CapabilityKind.IMAGE: endpoint},
    )

    result = next(
        item
        for item in OfflineLivePreflightService(
            repository,
            registry=registry,
            provider_profiles=profiles,
        ).run(project.id)
        if item.capability == CapabilityKind.IMAGE.value
    )

    assert result.ready is False
    assert result.provider_constraints_status == "ERROR"
    assert provider.network_calls == 0
