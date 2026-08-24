from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ProductionQCStatus(str, Enum):
    QC_PENDING = "QC_PENDING"
    QC_RUNNING = "QC_RUNNING"
    QC_PASS = "QC_PASS"
    QC_FAILED = "QC_FAILED"


class ProductionQCMetricStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class ProductionReviewDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ProductionQCResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=64)
    artifact_id: str | None = Field(default=None, max_length=64)
    status: ProductionQCStatus = ProductionQCStatus.QC_PENDING
    report_path: str | None = Field(default=None, max_length=1000)
    summary_json: dict[str, object] = Field(default_factory=dict)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    created_at: str = Field(min_length=1, max_length=80)


class ProductionQCMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    result_id: str = Field(min_length=1, max_length=64)
    metric_name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    status: ProductionQCMetricStatus
    value_json: dict[str, object] = Field(default_factory=dict)
    message: str = ""
    created_at: str = Field(min_length=1, max_length=80)

    @property
    def name(self) -> str:
        return self.metric_name


class ProductionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    qc_result_id: str = Field(min_length=1, max_length=64)
    decision: ProductionReviewDecision = ProductionReviewDecision.PENDING
    reviewer: str = Field(default="system", max_length=160)
    notes: str = Field(default="", max_length=4000)
    created_at: str = Field(min_length=1, max_length=80)
