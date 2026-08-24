from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ReferenceProfileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    profile_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=80)
    order_index: int = Field(ge=0)
    created_at: str = Field(min_length=1, max_length=80)


class ReferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    binding_type: str = Field(min_length=1, max_length=40)
    binding_id: str = Field(min_length=1, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


__all__ = ["ReferenceProfile", "ReferenceProfileItem"]
