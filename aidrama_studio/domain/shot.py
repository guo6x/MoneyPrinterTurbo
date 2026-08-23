from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ShotSize(str, Enum):
    EXTREME_WIDE="EXTREME_WIDE"; WIDE="WIDE"; FULL="FULL"; MEDIUM="MEDIUM"; MEDIUM_CLOSE="MEDIUM_CLOSE"; CLOSE_UP="CLOSE_UP"; EXTREME_CLOSE_UP="EXTREME_CLOSE_UP"; OVER_SHOULDER="OVER_SHOULDER"; INSERT="INSERT"
class CameraAngle(str, Enum):
    EYE_LEVEL="EYE_LEVEL"; HIGH_ANGLE="HIGH_ANGLE"; LOW_ANGLE="LOW_ANGLE"; TOP_DOWN="TOP_DOWN"; DUTCH="DUTCH"; POV="POV"; OVERHEAD="OVERHEAD"; OTHER="OTHER"
class CameraMovement(str, Enum):
    STATIC="STATIC"; PAN="PAN"; TILT="TILT"; PUSH_IN="PUSH_IN"; PULL_OUT="PULL_OUT"; TRUCK="TRUCK"; DOLLY="DOLLY"; TRACK="TRACK"; HANDHELD="HANDHELD"; ORBIT="ORBIT"; CRANE="CRANE"
class Lens(str, Enum):
    ULTRA_WIDE="ULTRA_WIDE"; WIDE="WIDE"; NORMAL="NORMAL"; PORTRAIT="PORTRAIT"; TELEPHOTO="TELEPHOTO"
class Eyeline(str, Enum):
    CAMERA="camera"; LEFT="left"; RIGHT="right"; ANOTHER_CHARACTER="another_character"; OBJECT="object"; OFFSCREEN="offscreen"; NOT_APPLICABLE="not_applicable"
class RiskLevel(str, Enum): LOW="LOW"; MEDIUM="MEDIUM"; HIGH="HIGH"
class ShotStatus(str, Enum): PLANNED="PLANNED"; LOCKED="LOCKED"
class ShotRevisionStatus(str, Enum): DRAFT="DRAFT"; APPROVED="APPROVED"; SUPERSEDED="SUPERSEDED"
class Lighting(BaseModel):
    model_config=ConfigDict(extra="forbid")
    quality: str="soft"; direction: str=""; tone: str=""; notes: str=""
class Blocking(BaseModel):
    model_config=ConfigDict(extra="forbid")
    positions: dict[str,str]=Field(default_factory=dict); movement: str=""; notes: str=""
class Shot(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id: str=Field(min_length=1,max_length=80); order: int=Field(ge=1); scene_id: str=Field(min_length=1)
    source_script_beat_ids: list[str]=Field(default_factory=list)
    duration_seconds: float=Field(gt=0); shot_size: ShotSize=ShotSize.MEDIUM; camera_angle: CameraAngle=CameraAngle.EYE_LEVEL
    camera_movement: CameraMovement=CameraMovement.STATIC; movement_notes: str=""; lens: Lens=Lens.NORMAL
    focal_length_hint_mm: int|None=Field(default=None,gt=0,le=500); composition: str="single"
    subject: list[str]=Field(default_factory=list); action: str=""; expression: str=""; eyeline: Eyeline=Eyeline.NOT_APPLICABLE
    lighting: Lighting=Field(default_factory=Lighting); blocking: Blocking=Field(default_factory=Blocking)
    dialogue_or_narration: str=""; visual_intent: str=Field(min_length=1); transition_hint: str=""
    risk_level: RiskLevel=RiskLevel.LOW; risk_reasons: list[str]=Field(default_factory=list)
    risk_override: bool=False; risk_override_note: str=""; status: ShotStatus=ShotStatus.PLANNED
    @model_validator(mode="after")
    def risk_reason_required(self):
        if self.risk_level != RiskLevel.LOW and not self.risk_reasons: raise ValueError("MEDIUM/HIGH shots require risk_reasons")
        if self.risk_override and not self.risk_override_note.strip(): raise ValueError("risk override requires note")
        return self
class ShotPlan(BaseModel):
    model_config=ConfigDict(extra="forbid")
    title: str=Field(min_length=1,max_length=200); summary: str=""; source_script_revision_id: str=Field(min_length=1); shots: list[Shot]=Field(min_length=1,max_length=500)
    @model_validator(mode="after")
    def unique_identity(self):
        if len({x.id for x in self.shots}) != len(self.shots): raise ValueError("shot IDs must be unique")
        if len({x.order for x in self.shots}) != len(self.shots): raise ValueError("shot orders must be unique")
        return self
    @property
    def total_duration_seconds(self): return sum(x.duration_seconds for x in self.shots)
    def validate_against(self, script, story=None):
        scenes={s.id:s for s in script.scenes}; chars={c.id for c in story.characters} if story else None
        for shot in self.shots:
            if shot.scene_id not in scenes: raise ValueError(f"shot {shot.id} references unknown scene")
            scene=scenes[shot.scene_id]; beat_ids={b.id for b in scene.beats}
            if not set(shot.source_script_beat_ids) <= beat_ids: raise ValueError(f"shot {shot.id} references beat from another/unknown scene")
            if chars is not None and not set(shot.subject) <= chars: raise ValueError(f"shot {shot.id} references unknown character")
        return self
