"""Provider-neutral episode duration and bounded execution planning."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from aidrama_studio.services.model_runtime import CapabilityKind
from aidrama_studio.storage.repositories import ProjectRepository

from .model_settings import SettingsModelService


class DurationPlanningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DurationExecutionBatch:
    index: int
    first_shot_index: int
    last_shot_index: int
    shot_durations: tuple[float, ...]

    @property
    def expected_video_create_count(self) -> int:
        return len(self.shot_durations)


@dataclass(frozen=True, slots=True)
class EpisodeDurationPlan:
    target_duration_seconds: float
    provider_id: str
    model_id: str
    endpoint_profile_id: str
    planned_shot_count: int
    planned_shot_durations: tuple[float, ...]
    total_native_seconds: float
    expected_video_create_count: int
    max_batch_size: int
    batches: tuple[DurationExecutionBatch, ...]


class DurationPlanningService:
    """Plan shots from the selected VIDEO manifest without provider constants.

    The product target has no maximum. Provider-native durations remain bound
    by the selected manifest, while ``max_batch_size`` bounds each execution
    wave. This method only produces a plan; it never authorizes or submits a
    provider create.
    """

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        settings_service: SettingsModelService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.settings = settings_service or SettingsModelService(self.repository)

    def plan(
        self,
        project_id: str,
        target_duration_seconds: float | None = None,
        *,
        max_batch_size: int = 8,
    ) -> EpisodeDurationPlan:
        project = self.repository.get_project(project_id)
        if project is None:
            raise DurationPlanningError(f"项目不存在: {project_id}")
        target = float(
            project.target_duration_seconds
            if target_duration_seconds is None
            else target_duration_seconds
        )
        if not math.isfinite(target) or target <= 0:
            raise DurationPlanningError("目标时长必须大于 0")
        if isinstance(max_batch_size, bool) or int(max_batch_size) <= 0:
            raise DurationPlanningError("执行 batch 大小必须大于 0")
        batch_size = int(max_batch_size)

        try:
            resolved = self.settings.resolve(project_id, CapabilityKind.VIDEO)
        except Exception as exc:
            raise DurationPlanningError("无法解析当前 VIDEO Provider/Model selection") from exc
        manifest = resolved.option.manifest
        duration = manifest.duration
        minimum = float(duration.minimum or 0)
        maximum = float(duration.maximum or 0)
        if minimum <= 0 or maximum < minimum:
            raise DurationPlanningError("当前 VIDEO manifest 缺少有效 duration contract")
        allowed = self._allowed_durations(manifest, minimum, maximum)
        preferred = self._preferred_duration(manifest, allowed, minimum, maximum)
        shot_durations = self._distribute(target, allowed, minimum, maximum, preferred)
        batches = tuple(
            DurationExecutionBatch(
                index=batch_index + 1,
                first_shot_index=start + 1,
                last_shot_index=start + len(items),
                shot_durations=items,
            )
            for batch_index, start in enumerate(range(0, len(shot_durations), batch_size))
            for items in (shot_durations[start : start + batch_size],)
        )
        return EpisodeDurationPlan(
            target_duration_seconds=target,
            provider_id=resolved.option.provider_id,
            model_id=resolved.option.model_id,
            endpoint_profile_id=resolved.option.endpoint_profile_id,
            planned_shot_count=len(shot_durations),
            planned_shot_durations=shot_durations,
            total_native_seconds=round(sum(shot_durations), 6),
            expected_video_create_count=len(shot_durations),
            max_batch_size=batch_size,
            batches=batches,
        )

    @staticmethod
    def _allowed_durations(manifest, minimum: float, maximum: float) -> tuple[float, ...]:
        discrete = tuple(float(item) for item in manifest.duration.discrete_values)
        if discrete:
            return tuple(sorted(set(discrete)))
        limits = manifest.limits if isinstance(manifest.limits, Mapping) else {}
        if (
            limits.get("duration_integer_only") is True
            and minimum.is_integer()
            and maximum.is_integer()
        ):
            return tuple(float(item) for item in range(int(minimum), int(maximum) + 1))
        return ()

    @staticmethod
    def _preferred_duration(
        manifest,
        allowed: tuple[float, ...],
        minimum: float,
        maximum: float,
    ) -> float:
        limits = manifest.limits if isinstance(manifest.limits, Mapping) else {}
        raw = limits.get("preferred_shot_duration_seconds", maximum)
        try:
            preferred = float(raw)
        except (TypeError, ValueError) as exc:
            raise DurationPlanningError(
                "VIDEO manifest preferred_shot_duration_seconds 无效"
            ) from exc
        if not math.isfinite(preferred) or not minimum <= preferred <= maximum:
            raise DurationPlanningError(
                "VIDEO manifest preferred shot duration 超出 provider contract"
            )
        if allowed:
            return min(allowed, key=lambda item: (abs(item - preferred), -item))
        return preferred

    @classmethod
    def _distribute(
        cls,
        target: float,
        allowed: tuple[float, ...],
        minimum: float,
        maximum: float,
        preferred: float,
    ) -> tuple[float, ...]:
        if target <= minimum:
            if allowed:
                return (next((item for item in allowed if item >= target), allowed[-1]),)
            return (minimum,)

        count = max(1, math.ceil(target / preferred))
        count = max(count, math.ceil(target / maximum))
        while count > 1 and target / count < minimum:
            count -= 1
        average = target / count

        if not allowed:
            if minimum <= average <= maximum:
                return tuple(round(average, 6) for _ in range(count))
            # Only a sub-minimum remainder can reach this branch. Provider
            # duration is rounded up and Final Assembly trims to the target.
            return tuple([maximum] * (count - 1) + [minimum])

        low = max((item for item in allowed if item <= average), default=allowed[0])
        high = min((item for item in allowed if item >= average), default=allowed[-1])
        if high == low:
            durations = [high] * count
        else:
            high_count = math.ceil((target - low * count) / (high - low))
            high_count = max(0, min(count, high_count))
            durations = [high] * high_count + [low] * (count - high_count)
        while sum(durations) + 1e-9 < target:
            count += 1
            average = target / count
            low = max((item for item in allowed if item <= average), default=allowed[0])
            high = min((item for item in allowed if item >= average), default=allowed[-1])
            high_count = (
                count
                if high == low
                else max(
                    0,
                    min(count, math.ceil((target - low * count) / (high - low))),
                )
            )
            durations = [high] * high_count + [low] * (count - high_count)
        return tuple(float(item) for item in durations)


__all__ = [
    "DurationExecutionBatch",
    "DurationPlanningError",
    "DurationPlanningService",
    "EpisodeDurationPlan",
]
