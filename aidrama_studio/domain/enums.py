from enum import Enum


class ProjectStatus(str, Enum):
    DRAFT = "DRAFT"
    STORY = "STORY"
    PREPRODUCTION = "PREPRODUCTION"
    PRODUCTION = "PRODUCTION"
    REVIEW = "REVIEW"
    POSTPRODUCTION = "POSTPRODUCTION"
    COMPLETED = "COMPLETED"


class AspectRatio(str, Enum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"
    SQUARE = "1:1"
