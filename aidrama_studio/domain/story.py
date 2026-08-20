from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=200)
    age_or_range: str = Field(default="", max_length=80)
    identity: str = Field(default="", max_length=500)
    personality: str = Field(default="", max_length=1000)
    appearance: str = Field(default="", max_length=1000)
    motivation: str = Field(default="", max_length=1000)
    relationship_notes: str = Field(default="", max_length=1000)
    speech_style: str = Field(default="", max_length=500)


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    function: str = Field(default="", max_length=500)
    environment: str = Field(default="", max_length=1000)
    time_of_day: str = Field(default="", max_length=120)
    visual_style: str = Field(default="", max_length=1000)
    key_props: list[str] = Field(default_factory=list, max_length=20)


class World(BaseModel):
    model_config = ConfigDict(extra="forbid")

    era: str = Field(default="", max_length=200)
    setting: str = Field(default="", max_length=1000)
    rules: list[str] = Field(default_factory=list, max_length=30)
    timeline_notes: str = Field(default="", max_length=1500)


class StoryBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    order: int = Field(ge=1, le=100)
    type: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=1500)
    characters: list[str] = Field(default_factory=list, max_length=20)
    location_id: str | None = Field(default=None, max_length=64)
    emotional_goal: str = Field(default="", max_length=500)

    @field_validator("type")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"OPENING", "DEVELOPMENT", "TURNING_POINT", "CLIMAX", "ENDING"}
        if normalized not in allowed:
            raise ValueError(f"story beat type must be one of {sorted(allowed)}")
        return normalized


class StoryBible(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    logline: str = Field(min_length=1, max_length=1000)
    premise: str = Field(min_length=1, max_length=2000)
    genre: str = Field(min_length=1, max_length=120)
    tone: str = Field(min_length=1, max_length=120)
    themes: list[str] = Field(default_factory=list, max_length=20)
    world: World
    characters: list[Character] = Field(min_length=1, max_length=20)
    locations: list[Location] = Field(min_length=1, max_length=20)
    story_beats: list[StoryBeat] = Field(min_length=3, max_length=30)

    @model_validator(mode="after")
    def validate_references(self) -> StoryBible:
        character_ids = [character.id for character in self.characters]
        location_ids = [location.id for location in self.locations]
        beat_ids = [beat.id for beat in self.story_beats]
        beat_orders = [beat.order for beat in self.story_beats]
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("character IDs must be unique")
        if len(set(location_ids)) != len(location_ids):
            raise ValueError("location IDs must be unique")
        if len(set(beat_ids)) != len(beat_ids):
            raise ValueError("story beat IDs must be unique")
        if len(set(beat_orders)) != len(beat_orders):
            raise ValueError("story beat order must be unique")
        character_set = set(character_ids)
        location_set = set(location_ids)
        for beat in self.story_beats:
            missing_characters = set(beat.characters) - character_set
            if missing_characters:
                raise ValueError(
                    f"story beat {beat.id} references unknown characters: "
                    f"{sorted(missing_characters)}"
                )
            if beat.location_id is not None and beat.location_id not in location_set:
                raise ValueError(
                    f"story beat {beat.id} references unknown location: {beat.location_id}"
                )
        return self

    @property
    def ordered_beats(self) -> list[StoryBeat]:
        return sorted(self.story_beats, key=lambda beat: beat.order)


class StoryRevisionStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
