"""Deterministic final-assembly manifest domain objects.

The manifest is deliberately a small snapshot of production truth.  It does
not copy shots, executions, artifacts, QC reports, or reviews; it only stores
the identities needed to reconstruct the exact inputs selected for a future
assembly run.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FinalAssemblyStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    ASSEMBLING = "ASSEMBLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FinalAssemblyRenderAttemptStatus(str, Enum):
    """Durable state for one fallible final-render attempt."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FinalAssembly(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    production_job_id: str = Field(min_length=1, max_length=64)
    output_profile_id: str | None = Field(default=None, max_length=80)
    output_profile_hash: str | None = Field(default=None, max_length=64)
    status: FinalAssemblyStatus = FinalAssemblyStatus.DRAFT
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class FinalAssemblyItem(BaseModel):
    """One immutable, ordered source selection in a final manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    final_assembly_id: str = Field(min_length=1, max_length=64)
    order_index: int = Field(ge=0)
    production_shot_id: str = Field(min_length=1, max_length=64)
    production_execution_id: str = Field(min_length=1, max_length=64)
    production_artifact_id: str = Field(min_length=1, max_length=64)
    qc_result_id: str = Field(min_length=1, max_length=64)
    review_id: str | None = Field(default=None, max_length=64)
    source_decision_id: str | None = Field(default=None, max_length=64)
    source_path: str = Field(min_length=1, max_length=1000)
    source_sha256: str | None = Field(default=None, max_length=64)
    source_duration_seconds: float | None = Field(default=None, ge=0)
    timeline_start_seconds: float | None = Field(default=None, ge=0)
    timeline_end_seconds: float | None = Field(default=None, ge=0)
    trimmed_duration_seconds: float | None = Field(default=None, ge=0)
    created_at: str = Field(min_length=1, max_length=80)


class FinalAssemblyManifest(BaseModel):
    """Read model returned by :meth:`FinalAssemblyService.get_manifest`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    production_job_id: str = Field(min_length=1, max_length=64)
    status: FinalAssemblyStatus
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)
    items: list[FinalAssemblyItem] = Field(default_factory=list)

    @classmethod
    def from_assembly(cls, assembly: FinalAssembly, items: list[FinalAssemblyItem]) -> "FinalAssemblyManifest":
        return cls(
            id=assembly.id,
            project_id=assembly.project_id,
            production_job_id=assembly.production_job_id,
            status=assembly.status,
            created_at=assembly.created_at,
            updated_at=assembly.updated_at,
            items=list(items),
        )


class FinalAssemblySource(BaseModel):
    """A qualified source candidate selected from canonical production data.

    ``__getitem__``/``get`` keep the object convenient for service callers
    that use the dictionary-shaped readiness APIs elsewhere in AIDrama.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    production_shot_id: str = Field(min_length=1, max_length=64)
    production_execution_id: str = Field(min_length=1, max_length=64)
    production_artifact_id: str = Field(min_length=1, max_length=64)
    qc_result_id: str = Field(min_length=1, max_length=64)
    review_id: str | None = Field(default=None, max_length=64)
    source_decision_id: str | None = Field(default=None, max_length=64)
    source_path: str = Field(min_length=1, max_length=1000)
    source_sha256: str | None = Field(default=None, max_length=64)
    source_duration_seconds: float | None = Field(default=None, ge=0)
    estimated_duration: float = Field(default=0.0, ge=0)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class FinalAssemblyReadiness(BaseModel):
    """Derived readiness, never persisted as a second source of truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_shots: int = Field(ge=0)
    eligible_shots: int = Field(ge=0)
    blocked_shots: int = Field(ge=0)
    estimated_duration: float = Field(default=0.0, ge=0)
    blocked_reasons: list[str] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.total_shots > 0 and self.blocked_shots == 0

    def __getitem__(self, key: str) -> Any:
        if key == "ready":
            return self.ready
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "ready":
            return self.ready
        return getattr(self, key, default)


class FinalAssemblyRenderAttempt(BaseModel):
    """Append-only history record for a final assembly render.

    The manifest remains the source of truth for inputs.  This record only
    describes the runtime attempt and the immutable output it produced.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    final_assembly_id: str = Field(min_length=1, max_length=64)
    attempt_number: int = Field(ge=1)
    status: FinalAssemblyRenderAttemptStatus = FinalAssemblyRenderAttemptStatus.PENDING
    adapter_name: str = Field(min_length=1, max_length=120)
    heavy_job_id: str | None = Field(default=None, max_length=64)
    output_relative_path: str | None = Field(default=None, max_length=1000)
    metadata_json: dict[str, object] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=4000)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)
