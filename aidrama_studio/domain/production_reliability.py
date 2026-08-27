"""Durable paid-create accounting for production reliability."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaidCreateStatus(StrEnum):
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    UNCERTAIN = "UNCERTAIN"


class PaidBudgetLedger(BaseModel):
    """Immutable authorization bound plus durable lifecycle timestamps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str = Field(min_length=1, max_length=80)
    authorization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_creates: int = Field(ge=0, le=10000)
    authorized_max: int = Field(ge=0, le=10000)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_bound(self) -> "PaidBudgetLedger":
        if self.authorized_max < self.planned_creates:
            raise ValueError("authorized_max 不能小于 planned_creates")
        return self


class PaidCreateReservation(BaseModel):
    """Exactly one paid create gate for one immutable execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    ledger_id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str = Field(min_length=1, max_length=80)
    execution_id: str = Field(min_length=1, max_length=80)
    provider_task_record_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=240)
    status: PaidCreateStatus
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class PaidBudgetProjection(BaseModel):
    """Provider-neutral create-count projection; never a currency estimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=80)
    production_job_id: str = Field(min_length=1, max_length=80)
    execution_id: str | None = Field(default=None, max_length=80)
    planned_creates: int = Field(ge=0)
    authorized_max: int = Field(ge=0)
    consumed_creates: int = Field(ge=0)
    reserved_creates: int = Field(ge=0)
    uncertain_creates: int = Field(ge=0)
    remaining_creates: int = Field(ge=0)


__all__ = [
    "PaidBudgetLedger",
    "PaidBudgetProjection",
    "PaidCreateReservation",
    "PaidCreateStatus",
]
