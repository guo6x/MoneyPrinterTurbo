from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .enums import AspectRatio, ProjectStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    title: str
    description: str
    status: ProjectStatus
    aspect_ratio: AspectRatio
    target_duration_seconds: int
    created_at: str
    updated_at: str

    def validate(self) -> Project:
        if not self.id.strip():
            raise ValueError("项目 ID 不能为空")
        if not self.title.strip():
            raise ValueError("项目名称不能为空")
        if len(self.title.strip()) > 120:
            raise ValueError("项目名称不能超过 120 个字符")
        if len(self.description.strip()) > 1000:
            raise ValueError("项目描述不能超过 1000 个字符")
        if not 1 <= self.target_duration_seconds <= 3600:
            raise ValueError("目标时长必须在 1 到 3600 秒之间")
        if not self.created_at or not self.updated_at:
            raise ValueError("项目时间字段不能为空")
        return self

    def with_updates(self, **changes) -> Project:
        return replace(self, **changes, updated_at=utc_now_iso()).validate()
