"""Untrusted creative source and normalized intake models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceKind(str, Enum):
    TEXT_BRIEF = "TEXT_BRIEF"
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"
    STORYBOARD_IMAGE = "STORYBOARD_IMAGE"
    OTHER_SUPPORTED_SOURCE = "OTHER_SUPPORTED_SOURCE"


class ExtractionState(str, Enum):
    PENDING = "PENDING"
    EXTRACTED = "EXTRACTED"
    WARNING = "WARNING"
    FAILED = "FAILED"


class SourcePackItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    source_kind: SourceKind
    display_filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_path: str = Field(min_length=1, max_length=1000)
    version_of_id: str | None = Field(default=None, max_length=80)
    extraction_state: ExtractionState = ExtractionState.PENDING
    extracted_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1, max_length=80)


class NormalizedCreativeBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    status: str = "DRAFT"
    title_candidate: str = ""
    premise: str = ""
    genre: str = ""
    tone: str = ""
    themes: tuple[str, ...] = ()
    characters: tuple[dict[str, Any], ...] = ()
    locations: tuple[dict[str, Any], ...] = ()
    story_information: dict[str, Any] = Field(default_factory=dict)
    visual_direction: dict[str, Any] = Field(default_factory=dict)
    existing_script_maturity: str = "UNKNOWN"
    existing_shot_maturity: str = "UNKNOWN"
    constraints: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    created_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)


class IntakeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=80)
    classifications: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    warnings: tuple[str, ...] = ()
    created_at: str = Field(min_length=1, max_length=80)


__all__ = ["ExtractionState", "IntakeAnalysis", "NormalizedCreativeBrief", "SourceKind", "SourcePackItem"]
