from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreativeLock(BaseModel):
    """Durable user authority over one stable creative decision/path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    entity_kind: str = Field(min_length=1, max_length=40)
    stable_entity_id: str = Field(min_length=1, max_length=120)
    field_path: str = Field(min_length=1, max_length=240)
    source_revision_id: str | None = Field(default=None, max_length=80)
    reason: str = Field(default="", max_length=500)
    active: bool = True
    created_at: str = Field(min_length=1, max_length=80)
    released_at: str | None = Field(default=None, max_length=80)


__all__ = ["CreativeLock"]
