from __future__ import annotations

from collections.abc import Mapping

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionService,
    ProductionServiceError,
)


def _value(item, key: str, default=None):
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _status_value(item, key: str = "status", default: str = "UNKNOWN") -> str:
    value = _value(item, key, default)
    return str(getattr(value, "value", value))


def _event_label(event) -> str:
    event_type = _status_value(event, "event_type")
    return {
        "QUEUED": "Execution queued",
        "STARTED": "Runtime started",
        "PROGRESS": "Progress update",
        "SHOT_COMPLETED": "Shot completed",
        "FAILED": "Execution failed",
        "CANCELLED": "Execution cancelled",
        "FINISHED": "Execution finished",
    }.get(event_type, event_type)


def _readiness_reasons(readiness: Mapping[str, object]) -> list[str]:
    return [str(reason) for reason in (readiness.get("blocked_reasons") or [])]


def _render_readiness(readiness: Mapping[str, object]) -> None:
    ready = bool(readiness.get("ready"))
    shot_count = int(readiness.get("shot_count") or 0)
    status_col, count_col = st.columns(2)
    status_col.metric("Production readiness", "READY" if ready else "BLOCKED")
    count_col.metric("Approved shots", shot_count)
    reasons = _readiness_reasons(readiness)
    if ready:
        st.success("Story Bible、Structured Script、Shot Plan 与 Reference Assets 已满足执行前置条件。")
    else:
        st.warning("Production 尚未就绪。请先完成以下前置条件：")
        for reason in reasons:
            st.markdown(f"- {reason}")


def _render_job_row(job, readiness: Mapping[str, object]) -> None:
    with st.container(border=True):
        columns = st.columns(4)
        columns[0].markdown(f"**{_status_value(job, 'status')}**")
        columns[1].caption(f"Created · {_value(job, 'created_at', '—')}")
        columns[2].caption(f"Shots · {int(readiness.get('shot_count') or 0)}")
        columns[3].caption("READY" if readiness.get("ready") else "BLOCKED")


def _artifact_runtime(artifact) -> str:
    metadata = _value(artifact, "metadata_json", {}) or {}
    if isinstance(metadata, Mapping):
        return str(metadata.get("runtime") or metadata.get("engine") or metadata.get("adapter") or "—")
    return "—"


def _render_execution_detail(execution_service: ProductionExecutionService, project, execution) -> None:
    status = _status_value(execution)
    st.markdown(f"### Execution · {status}")
    st.caption(f"Worker · {_value(execution, 'worker_type', '—')} · Created · {_value(execution, 'created_at', '—')}")
    if _value(execution, "started_at"):
        st.caption(f"Started · {_value(execution, 'started_at')}")
    if _value(execution, "finished_at"):
        st.caption(f"Finished · {_value(execution, 'finished_at')}")

    st.markdown("#### Event timeline")
    events = execution_service.list_events(project.id, execution.id)
    if not events:
        st.info("暂无 execution events。")
    for event in events:
        payload = _value(event, "payload_json", {}) or {}
        detail = ""
        if isinstance(payload, Mapping):
            if "progress" in payload:
                detail = f" · {payload['progress']}%"
            elif payload.get("error"):
                detail = f" · {payload['error']}"
            elif payload.get("shot_id"):
                detail = f" · {payload['shot_id']}"
        st.markdown(f"- **{_event_label(event)}**{detail}  \n  `{_value(event, 'created_at', '—')}`")

    st.markdown("#### Artifact metadata")
    artifacts = execution_service.list_artifacts(project.id, execution.id)
    if not artifacts:
        st.info("暂无 artifact metadata。真实文件生成不在当前阶段。")
        return
    for artifact in artifacts:
        columns = st.columns(4)
        columns[0].caption(f"Type · {_value(artifact, 'artifact_type', '—')}")
        columns[1].caption(f"Path · {_value(artifact, 'path', '—')}")
        columns[2].caption(f"Runtime · {_artifact_runtime(artifact)}")
        columns[3].caption(f"Timestamp · {_value(artifact, 'created_at', '—')}")


def _create_job(production_service: ProductionService, project) -> None:
    try:
        job = production_service.create_production_job(project.id)
    except ProductionServiceError as exc:
        st.error(str(exc))
        return
    st.session_state[f"production-job-{project.id}"] = job.id
    st.success("Production Job 已创建。")
    st.rerun()


def _queue_execution(execution_service: ProductionExecutionService, project, job) -> None:
    try:
        execution_service.enqueue_job(project.id, job.id)
    except ProductionExecutionServiceError as exc:
        st.error(str(exc))
        return
    st.success("Execution 已进入 QUEUED。Runtime adapter 由后续执行层负责提交。")
    st.rerun()


def render() -> None:
    page_header("制作执行", "PRODUCTION EXECUTION", "检查制作就绪度、排队执行并查看不可变的事件与 artifact metadata。")
    project = current_project_or_stop()
    production_service = ProductionService()
    execution_service = ProductionExecutionService(production_service=production_service)

    try:
        readiness = production_service.validate_job_readiness(project.id)
        jobs = production_service.list_jobs(project.id)
    except ProductionServiceError as exc:
        st.error(str(exc))
        return

    st.markdown("## Production Readiness Check")
    _render_readiness(readiness)

    st.markdown("## Production Jobs")
    if st.button(
        "Create Production Job",
        type="primary",
        disabled=not bool(readiness.get("ready")),
        key=f"create-production-job-{project.id}",
    ):
        _create_job(production_service, project)

    if not jobs:
        st.info("当前项目还没有 Production Job。")
        return

    job_options = {str(_value(job, "id")): job for job in jobs}
    selected_job_id = st.selectbox(
        "选择 Production Job",
        list(job_options),
        key=f"production-job-select-{project.id}",
        format_func=lambda job_id: f"{_status_value(job_options[job_id])} · {job_id[:10]}",
    )
    selected_job = job_options[selected_job_id]
    try:
        job_readiness = production_service.validate_job_readiness(project.id, _value(selected_job, "shot_plan_revision_id"))
        executions = execution_service.list_executions(project.id, selected_job.id)
    except (ProductionServiceError, ProductionExecutionServiceError) as exc:
        st.error(str(exc))
        return
    _render_job_row(selected_job, job_readiness)

    active = any(_status_value(item) in {"QUEUED", "RUNNING"} for item in executions)
    if st.button(
        "Submit Execution",
        disabled=not bool(job_readiness.get("ready")) or active,
        key=f"submit-execution-{selected_job.id}",
    ):
        _queue_execution(execution_service, project, selected_job)

    if not executions:
        st.info("暂无 execution。满足 readiness 后可提交执行。")
        return

    execution_options = {str(_value(item, "id")): item for item in executions}
    selected_execution_id = st.selectbox(
        "选择 Execution",
        list(execution_options),
        key=f"production-execution-select-{selected_job.id}",
        format_func=lambda execution_id: f"{_status_value(execution_options[execution_id])} · {execution_id[:10]}",
    )
    if st.button("Refresh execution", key=f"refresh-execution-{selected_execution_id}"):
        st.rerun()
    try:
        _render_execution_detail(execution_service, project, execution_options[selected_execution_id])
    except ProductionExecutionServiceError as exc:
        st.error(str(exc))
