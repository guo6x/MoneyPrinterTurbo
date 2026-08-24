"""Structured AI Producer projections and bounded policy records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ProducerPolicy:
    max_generation_attempts_per_shot: int = 3
    max_qc_retry_recommendations: int = 2
    automatic_retry_enabled: bool = False

    def __post_init__(self) -> None:
        if self.max_generation_attempts_per_shot < 1:
            raise ValueError("max_generation_attempts_per_shot must be positive")
        if self.max_qc_retry_recommendations < 0:
            raise ValueError("max_qc_retry_recommendations cannot be negative")


@dataclass(frozen=True, slots=True)
class ProductionProgress:
    project_id: str
    job_id: str | None
    total_shots: int = 0
    completed_shots: int = 0
    pending_shots: int = 0
    failed_shots: int = 0
    blocked_shots: int = 0
    current_shot_id: str | None = None
    high_risk_shots: tuple[str, ...] = ()
    qc_failures: int = 0
    final_assembly_ready: bool = False
    post_production_ready: bool = False


@dataclass(frozen=True, slots=True)
class ProducerRecommendation:
    action: str
    reason: str
    target_id: str | None = None
    requires_human_approval: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
