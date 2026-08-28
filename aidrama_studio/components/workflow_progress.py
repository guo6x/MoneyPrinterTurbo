"""Creator-facing AUTO workflow progress rail.

The component deliberately contains no repository or service calls.  It only
projects the durable ``AutoDecision`` shape that the AUTO orchestrator already
publishes.  Keeping this projection here makes the rail usable from AppTest
and prevents page code from growing a second workflow state machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

import streamlit as st


# The order is the product-facing rail.  ``COMPLETED`` is a terminal marker in
# the service enum and intentionally maps to the final ``成片`` node instead of
# creating an extra technical node in the creator UI.
PIPELINE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("CREATIVE", "创意"),
    ("STORY", "故事"),
    ("SCRIPT", "剧本"),
    ("SHOT_PLAN", "分镜"),
    ("REFERENCES", "参考资产"),
    ("KEYFRAMES", "镜头首帧 / 视觉预演"),
    ("PRODUCTION", "视频制作"),
    ("QC", "技术质检"),
    ("REVIEW", "人工审片"),
    ("FINAL", "成片"),
)

PIPELINE_STAGE_LABELS: dict[str, str] = dict(PIPELINE_DEFINITIONS)
PIPELINE_STAGE_ALIASES: dict[str, str] = {"COMPLETED": "FINAL"}
PIPELINE_STATUS_LABELS: dict[str, str] = {
    "COMPLETED": "已完成",
    "CURRENT": "当前",
    "PENDING": "待处理",
    "BLOCKED": "需处理",
}


def _value(value: Any, *names: str, default: Any = None) -> Any:
    """Read a field from either a pydantic object or a mapping test double."""

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def _enum_text(value: Any) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate or "").strip().upper()


@dataclass(frozen=True, slots=True)
class PipelineStageProjection:
    """One safe rail node.

    ``raw_stage`` is retained for diagnostics/tests but is never rendered in
    normal mode.  ``status`` is one of the four stable UI states requested by
    the product contract.
    """

    key: str
    label: str
    status: str
    raw_stage: str

    @property
    def status_label(self) -> str:
        return PIPELINE_STATUS_LABELS.get(self.status, "待处理")

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "raw_stage": self.raw_stage,
        }

    # Mapping-style access keeps the projection convenient for lightweight
    # callers that do not want to import the dataclass.
    def __getitem__(self, key: str) -> str:
        return self.as_dict()[key]


def pipeline_stage_projections(
    decision: Any,
    *,
    keyframe_readiness: Any | None = None,
) -> tuple[PipelineStageProjection, ...]:
    """Project durable AUTO and keyframe truth into ten creator-facing nodes.

    Only ``completed_stages``, ``current_stage``, and ``status`` are used.
    ``blocking_reason`` is consulted solely to make an explicitly uncertain or
    reconciliation-required current stage visible as blocked; it does not
    create progress or query storage.
    """

    completed_raw = _value(decision, "completed_stages", default=()) or ()
    completed = {
        PIPELINE_STAGE_ALIASES.get(_enum_text(item), _enum_text(item))
        for item in completed_raw
    }
    current_raw = _enum_text(_value(decision, "current_stage", default=""))
    current = PIPELINE_STAGE_ALIASES.get(current_raw, current_raw)
    run_status = _enum_text(_value(decision, "status", default=""))
    blocking_reason = _enum_text(
        _value(decision, "blocking_reason", default="")
    )
    uncertain = any(
        marker in blocking_reason
        for marker in (
            "UNCERTAIN_CREATE",
            "RECONCILIATION_REQUIRED",
            "SUBMISSION_UNCERTAIN",
        )
    )
    # A listed FINAL stage is evidence that only that stage is complete; it is
    # not, by itself, permission to paint every preceding node green.  The
    # orchestrator's terminal status (or explicit COMPLETED marker) is the
    # only source that can close the whole rail.
    completed_markers = {_enum_text(item) for item in completed_raw}
    # A contradictory BLOCKED/FAILED snapshot must remain visibly blocked even
    # if a stale row still carries a COMPLETED marker.  Terminal markers close
    # the rail only when the durable run status is not an active failure gate.
    terminal = run_status == "SUCCEEDED" or (
        run_status not in {"BLOCKED", "FAILED"}
        and (current_raw == "COMPLETED" or "COMPLETED" in completed_markers)
    )
    keyframe_gate = _enum_text(
        _value(keyframe_readiness, "gate", default="PENDING")
    )
    downstream_of_keyframes = {"PRODUCTION", "QC", "REVIEW", "FINAL"}

    result: list[PipelineStageProjection] = []
    for key, label in PIPELINE_DEFINITIONS:
        if terminal:
            state = "COMPLETED"
        elif key == "KEYFRAMES":
            if keyframe_gate == "PASS":
                state = "COMPLETED"
            elif keyframe_gate == "BLOCKED":
                state = "BLOCKED"
            else:
                state = "PENDING"
        elif keyframe_gate == "BLOCKED" and key in downstream_of_keyframes:
            state = "PENDING"
        elif run_status in {"BLOCKED", "FAILED"} and key == current:
            state = "BLOCKED"
        elif uncertain and key == current:
            state = "BLOCKED"
        elif key in completed:
            state = "COMPLETED"
        elif key == current:
            state = "CURRENT"
        else:
            state = "PENDING"
        result.append(
            PipelineStageProjection(
                key=key,
                label=label,
                status=state,
                raw_stage=current_raw,
            )
        )
    return tuple(result)


# Descriptive aliases used by page owners and tests.
project_pipeline = pipeline_stage_projections
workflow_pipeline_projection = pipeline_stage_projections


def render_pipeline(
    decision: Any,
    *,
    keyframe_readiness: Any | None = None,
    title: str = "制作流程",
) -> tuple[PipelineStageProjection, ...]:
    """Render the rail and return its pure projection for tests/callers."""

    projections = pipeline_stage_projections(
        decision, keyframe_readiness=keyframe_readiness
    )
    nodes: list[str] = []
    for index, item in enumerate(projections, start=1):
        status_class = item.status.casefold()
        nodes.append(
            '<div class="aidrama-auto-stage '
            f'aidrama-auto-stage-{escape(status_class)}" '
            f'data-stage="{escape(item.key.casefold())}" '
            f'data-state="{escape(item.status.casefold())}">'
            f'<span class="aidrama-auto-stage-index">{index:02d}</span>'
            f'<strong>{escape(item.label)}</strong>'
            f'<small>{escape(item.status_label)}</small>'
            "</div>"
        )
    html = (
        '<section class="aidrama-auto-pipeline" '
        'aria-label="AI 制作流程">'
        f'<div class="aidrama-auto-pipeline-head"><span class="aidrama-section-kicker">'
        f'AI PRODUCTION PIPELINE</span><h3>{escape(title)}</h3></div>'
        f'<div class="aidrama-auto-stage-rail">{"".join(nodes)}</div>'
        "</section>"
    )
    st.markdown(html, unsafe_allow_html=True)
    return projections


__all__ = [
    "PIPELINE_DEFINITIONS",
    "PIPELINE_STAGE_ALIASES",
    "PIPELINE_STAGE_LABELS",
    "PIPELINE_STATUS_LABELS",
    "PipelineStageProjection",
    "pipeline_stage_projections",
    "project_pipeline",
    "workflow_pipeline_projection",
    "render_pipeline",
]
