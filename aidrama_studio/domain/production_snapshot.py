"""Immutable input snapshots handed to production runtime adapters."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw(item) for item in value]
    return value


class FrozenDict(Mapping[str, object]):
    """A recursively frozen, JSON-shaped mapping."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, object] | None = None):
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise TypeError("snapshot mapping values must be mappings")
        object.__setattr__(self, "_data", MappingProxyType({str(key): _freeze(item) for key, item in value.items()}))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("FrozenDict is immutable")

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._data)!r})"


class ProductionInputSnapshot(BaseModel):
    """Frozen, project-scoped input contract for a runtime submission."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_id: str = Field(min_length=1, max_length=64)
    story_revision_id: str = Field(min_length=1, max_length=64)
    script_revision_id: str = Field(min_length=1, max_length=64)
    shot_plan_revision_id: str = Field(min_length=1, max_length=64)
    runtime_plan_id: str | None = Field(default=None, max_length=80)
    generation_brief_id: str | None = Field(default=None, max_length=80)
    runtime_plan_hash: str | None = Field(default=None, max_length=64)
    reference_asset_versions: FrozenDict = Field(default_factory=FrozenDict)
    shot_parameters: FrozenDict = Field(default_factory=FrozenDict)

    @field_validator("reference_asset_versions", "shot_parameters", mode="before")
    @classmethod
    def _freeze_mapping(cls, value: Any) -> FrozenDict:
        if isinstance(value, FrozenDict):
            return value
        if isinstance(value, Mapping):
            return FrozenDict(value)
        if isinstance(value, (list, tuple)):
            return FrozenDict({str(index): item for index, item in enumerate(value)})
        raise TypeError("snapshot values must be mappings or sequences")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "story_revision_id": self.story_revision_id,
            "script_revision_id": self.script_revision_id,
            "shot_plan_revision_id": self.shot_plan_revision_id,
            "runtime_plan_id": self.runtime_plan_id,
            "generation_brief_id": self.generation_brief_id,
            "runtime_plan_hash": self.runtime_plan_hash,
            "reference_asset_versions": _thaw(self.reference_asset_versions),
            "shot_parameters": _thaw(self.shot_parameters),
        }

    @field_serializer("reference_asset_versions", "shot_parameters")
    def _serialize_frozen(self, value: FrozenDict) -> dict[str, object]:
        return _thaw(value)
