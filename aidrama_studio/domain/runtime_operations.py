"""Durable runtime-operation and provider capability records."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str | None = Field(default=None, max_length=80)
    capability: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=240)
    profile: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
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


__all__ = ["CapabilityProfile", "ProviderTask", "VisionAnalysisRecord", "VisionFrameManifest"]
