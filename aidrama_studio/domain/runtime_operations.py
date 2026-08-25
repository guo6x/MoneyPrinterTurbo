"""Durable runtime-operation and provider capability records."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderDeploymentRegion(StrEnum):
    MAINLAND_CHINA = "MAINLAND_CHINA"
    INTERNATIONAL = "INTERNATIONAL"
    LOCAL = "LOCAL"
    UNSPECIFIED = "UNSPECIFIED"


class ProviderPreset(StrEnum):
    MAINLAND = "MAINLAND"
    INTERNATIONAL = "INTERNATIONAL"
    CUSTOM = "CUSTOM"


class ProviderVerificationState(StrEnum):
    NOT_VERIFIED = "NOT_VERIFIED"
    VERIFIED = "VERIFIED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class CapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str | None = Field(default=None, max_length=80)
    capability: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=240)
    endpoint_profile_id: str = Field(default="LEGACY", min_length=1, max_length=160)
    deployment_region: ProviderDeploymentRegion = ProviderDeploymentRegion.UNSPECIFIED
    endpoint_class: str = Field(default="UNSPECIFIED", min_length=1, max_length=160)
    endpoint_url: str | None = Field(default=None, max_length=500)
    credential_reference: str | None = Field(default=None, max_length=160)
    verification_state: ProviderVerificationState = ProviderVerificationState.NOT_VERIFIED
    verified_at: str | None = Field(default=None, max_length=80)
    selection_priority: int = Field(default=100, ge=0, le=10000)
    profile: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class ProviderSelectionSettings(BaseModel):
    """Scope-aware convenience choices that only reference canonical profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=160)
    project_id: str | None = Field(default=None, max_length=80)
    preset: ProviderPreset = ProviderPreset.CUSTOM
    selections: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class ProviderTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    execution_id: str | None = Field(default=None, max_length=80)
    capability: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=1, max_length=240)
    provider_task_id: str | None = Field(default=None, max_length=240)
    state: str = Field(min_length=1, max_length=40)
    request_summary: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    submitted_at: str | None = None
    last_polled_at: str | None = None
    next_poll_at: str | None = None
    error_message: str | None = None
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class VisionFrameManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    execution_id: str = Field(min_length=1, max_length=80)
    artifact_id: str | None = None
    frame_count: int = Field(ge=0)
    samples: tuple[dict[str, Any], ...] = ()
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=80)


class VisionAnalysisRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    execution_id: str = Field(min_length=1, max_length=80)
    artifact_id: str | None = None
    frame_manifest_id: str | None = None
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=240)
    status: str = Field(min_length=1, max_length=40)
    metrics: dict[str, Any] = Field(default_factory=dict)
    reference_comparison: dict[str, Any] = Field(default_factory=dict)
    reference_version_ids: tuple[str, ...] = ()
    prompt_template_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    input_provenance: dict[str, Any] = Field(default_factory=dict)
    provider_interaction_id: str | None = Field(default=None, max_length=240)
    created_at: str = Field(min_length=1, max_length=80)


__all__ = [
    "CapabilityProfile",
    "ProviderDeploymentRegion",
    "ProviderPreset",
    "ProviderSelectionSettings",
    "ProviderTask",
    "ProviderVerificationState",
    "VisionAnalysisRecord",
    "VisionFrameManifest",
]
