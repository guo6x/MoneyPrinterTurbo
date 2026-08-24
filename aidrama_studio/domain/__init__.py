"""Lightweight product-domain models."""

from .enums import AspectRatio, ProjectStatus
from .project import Project
from .story import Character, Location, StoryBeat, StoryBible, StoryRevisionStatus, World
from .script import InteriorExterior, Scene, ScriptBeat, ScriptBeatType, ScriptRevisionStatus, StructuredScript, TimeOfDay
from .shot import Blocking, CameraAngle, CameraMovement, Lens, Lighting, RiskLevel, Shot, ShotPlan, ShotSize, ShotStatus, ShotRevisionStatus, Eyeline
from .reference_asset import ReferenceAsset, ReferenceAssetType, ReferenceAssetVersion, ReferenceAssetBinding, ReferenceBindingType

__all__ = [
    "AspectRatio",
    "Character",
    "Location",
    "Project",
    "ProjectStatus",
    "StoryBeat",
    "StoryBible",
    "StoryRevisionStatus",
    "World",
    "InteriorExterior", "TimeOfDay", "ScriptBeatType", "ScriptRevisionStatus", "ScriptBeat", "Scene", "StructuredScript",
    "ShotPlan", "Shot", "ShotSize", "CameraAngle", "CameraMovement", "Lens", "Eyeline", "Lighting", "Blocking", "RiskLevel", "ShotStatus", "ShotRevisionStatus",
    "ReferenceAsset", "ReferenceAssetType", "ReferenceAssetVersion", "ReferenceAssetBinding", "ReferenceBindingType",
]
