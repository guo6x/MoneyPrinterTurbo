from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InteriorExterior(str, Enum):
    INT = "INT"
    EXT = "EXT"
    INT_EXT = "INT_EXT"


class TimeOfDay(str, Enum):
    DAWN = "DAWN"
    DAY = "DAY"
    DUSK = "DUSK"
    NIGHT = "NIGHT"
    UNSPECIFIED = "UNSPECIFIED"


_TIME_OF_DAY_EXACT_ALIASES = {
    "深夜": TimeOfDay.NIGHT,
    "夜晚": TimeOfDay.NIGHT,
    "夜间": TimeOfDay.NIGHT,
    "晚上": TimeOfDay.NIGHT,
    "清晨": TimeOfDay.DAWN,
    "黎明": TimeOfDay.DAWN,
    "白天": TimeOfDay.DAY,
    "日间": TimeOfDay.DAY,
    "黄昏": TimeOfDay.DUSK,
    "傍晚": TimeOfDay.DUSK,
    "未指定": TimeOfDay.UNSPECIFIED,
    "不确定": TimeOfDay.UNSPECIFIED,
}


class ScriptBeatType(str, Enum):
    ACTION = "ACTION"
    DIALOGUE = "DIALOGUE"
    NARRATION = "NARRATION"
    INNER_MONOLOGUE = "INNER_MONOLOGUE"
    TRANSITION = "TRANSITION"


class ScriptRevisionStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class ScriptBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    type: ScriptBeatType
    character_id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=5000)
    emotion: str | None = Field(default=None, max_length=500)
    stage_direction: str | None = Field(default=None, max_length=1000)
    estimated_duration_seconds: float | None = Field(default=None, gt=0)


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    location_id: str = Field(min_length=1, max_length=64)
    interior_exterior: InteriorExterior = InteriorExterior.INT
    time_of_day: TimeOfDay = TimeOfDay.UNSPECIFIED
    character_ids: list[str] = Field(default_factory=list, max_length=30)
    purpose: str = Field(default="", max_length=1000)
    summary: str = Field(default="", max_length=2000)
    emotion: str = Field(default="", max_length=500)
    estimated_duration_seconds: float = Field(gt=0)
    beats: list[ScriptBeat] = Field(min_length=1, max_length=100)
    source_story_beat_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("time_of_day", mode="before")
    @classmethod
    def normalize_time_of_day(cls, value: object) -> object:
        if isinstance(value, TimeOfDay):
            return value
        if isinstance(value, str):
            exact = value.strip()
            return _TIME_OF_DAY_EXACT_ALIASES.get(exact, exact)
        return value

    @model_validator(mode="after")
    def unique_beats(self) -> "Scene":
        if len({b.id for b in self.beats}) != len(self.beats):
            raise ValueError("beat IDs must be unique within scene")
        if len({b.order for b in self.beats}) != len(self.beats):
            raise ValueError("beat order must be unique within scene")
        return self


class StructuredScript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=3000)
    scenes: list[Scene] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_scenes(self) -> "StructuredScript":
        if len({s.id for s in self.scenes}) != len(self.scenes):
            raise ValueError("scene IDs must be unique")
        if len({s.order for s in self.scenes}) != len(self.scenes):
            raise ValueError("scene order must be unique")
        beat_ids = [beat.id for scene in self.scenes for beat in scene.beats]
        if len(set(beat_ids)) != len(beat_ids):
            raise ValueError("beat IDs must be unique within script")
        return self

    @property
    def total_estimated_duration_seconds(self) -> float:
        return sum(scene.estimated_duration_seconds for scene in self.scenes)

    def validate_against(self, story_bible) -> "StructuredScript":
        character_ids = {c.id for c in story_bible.characters}
        location_ids = {l.id for l in story_bible.locations}
        beat_ids = {b.id for b in story_bible.story_beats}
        for scene in self.scenes:
            if scene.location_id not in location_ids:
                raise ValueError(f"scene {scene.id} references unknown location: {scene.location_id}")
            if not set(scene.character_ids) <= character_ids:
                raise ValueError(f"scene {scene.id} references unknown character")
            if not set(scene.source_story_beat_ids) <= beat_ids:
                raise ValueError(f"scene {scene.id} references unknown story beat")
            for beat in scene.beats:
                if beat.character_id and beat.character_id not in character_ids:
                    raise ValueError(f"beat {beat.id} references unknown character")
                if beat.type in (ScriptBeatType.DIALOGUE, ScriptBeatType.INNER_MONOLOGUE) and not beat.character_id:
                    raise ValueError(f"{beat.type.value} beat {beat.id} requires character_id")
        return self
