from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReferenceAssetType(str, Enum):
    CHARACTER_REFERENCE = "CHARACTER_REFERENCE"
    LOCATION_REFERENCE = "LOCATION_REFERENCE"
    STYLE_REFERENCE = "STYLE_REFERENCE"
    PROP_REFERENCE = "PROP_REFERENCE"


class ReferenceBindingType(str, Enum):
    CHARACTER = "CHARACTER"
    LOCATION = "LOCATION"
    SHOT = "SHOT"


class ReferenceImageCandidateStatus(str, Enum):
    DRAFT = "DRAFT"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


class ReferenceImageCandidateEventType(str, Enum):
    CREATED = "CREATED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    asset_type: ReferenceAssetType
    current_version_id: str | None = Field(default=None, max_length=64)
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class ReferenceAssetVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    asset_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    version_number: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_path: str = Field(min_length=1, max_length=500)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)

    @field_validator("storage_path")
    @classmethod
    def validate_relative_storage_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or normalized.startswith("/")
            or (len(path.parts) > 0 and len(path.parts[0]) == 2 and path.parts[0][1] == ":")
            or "://" in normalized
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("storage_path must be a safe relative path")
        return normalized


class ReferenceAssetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    asset_version_id: str = Field(min_length=1, max_length=64)
    binding_type: ReferenceBindingType
    binding_id: str = Field(min_length=1, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)


class ReferenceImageCandidate(BaseModel):
    """Immutable generated-image payload plus its explicit human lifecycle."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    asset_id: str = Field(min_length=1, max_length=64)
    source_story_revision_id: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=160)
    endpoint_profile_id: str = Field(min_length=1, max_length=240)
    deployment_region: str = Field(min_length=1, max_length=80)
    prompt_text: str = Field(min_length=1, max_length=20000)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_path: str = Field(min_length=1, max_length=500)
    status: ReferenceImageCandidateStatus = ReferenceImageCandidateStatus.DRAFT
    parent_candidate_id: str | None = Field(default=None, max_length=64)
    promoted_version_id: str | None = Field(default=None, max_length=64)
    created_at: str = Field(min_length=1, max_length=80)
    decided_at: str | None = Field(default=None, max_length=80)

    @field_validator("storage_path")
    @classmethod
    def validate_relative_storage_path(cls, value: str) -> str:
        return ReferenceAssetVersion.validate_relative_storage_path(value)


class ReferenceImageCandidateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=64)
    sequence_number: int = Field(ge=1)
    event_type: ReferenceImageCandidateEventType
    actor: str = Field(default="user", min_length=1, max_length=160)
    notes: str = Field(default="", max_length=4000)
    promoted_version_id: str | None = Field(default=None, max_length=64)
    created_at: str = Field(min_length=1, max_length=80)
