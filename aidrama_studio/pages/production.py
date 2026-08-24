from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
)
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionQCService,
    ProductionQCServiceError,
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


def _relative_path(value, default: str = "—") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or PureWindowsPath(value).drive:
        return "[project-relative path unavailable]"
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return "[project-relative path unavailable]"
    return PurePosixPath(*parts).as_posix()


def _qc_status_value(result) -> str:
    return _status_value(result, "status", "QC_PENDING")


def _qc_metric_icon(metric) -> str:
    status = _status_value(metric, "status")
    return {"PASS": "✓", "FAIL": "✗", "SKIPPED": "–"}.get(status, "·")


def _qc_metric_label(metric) -> str:
    return str(_value(metric, "metric_name", _value(metric, "name", "check"))).replace("_", " ")


def _review_label(review) -> str:
    decision = _status_value(review, "decision", "PENDING")
    return {"APPROVED": "ACCEPTED", "REJECTED": "REJECTED", "PENDING": "PENDING"}.get(decision, decision)


def _qc_traceability(execution, artifact) -> None:
    st.markdown("**Traceability**")
    metadata = _value(artifact, "metadata_json", {}) or {}
    shot_id = metadata.get("shot_id", "—") if isinstance(metadata, Mapping) else "—"
    references = metadata.get("reference_versions", metadata.get("reference_asset_versions", {})) if isinstance(metadata, Mapping) else {}
    st.caption(f"Execution · {_value(execution, 'id', '—')}")
    st.caption(f"Shot · {shot_id}")
    if isinstance(references, Mapping):
        st.caption("Reference versions · " + (", ".join(f"{key}={value}" for key, value in references.items()) or "—"))


def _render_qc_metrics(qc_service: ProductionQCService, project, result) -> None:
    try:
        metrics = qc_service.list_metrics(project.id, result.id)
    except ProductionQCServiceError as exc:
        st.warning(f"QC unavailable: {exc}")
        return
    if not metrics:
        st.info("No QC metrics recorded.")
        return
    for metric in metrics:
        message = str(_value(metric, "message", ""))
        st.markdown(f"{_qc_metric_icon(metric)} **{_qc_metric_label(metric)}**" + (f" · {message}" if message else ""))


def _submit_review(qc_service: ProductionQCService, project, result, decision: str, notes: str) -> None:
    try:
        qc_service.create_review(project.id, result.id, ProductionReviewDecision(decision), notes=notes)
    except (ProductionQCServiceError, ValueError) as exc:
        st.error(f"Review unavailable: {exc}")
        return
    st.success("Human review 已保存。历史 QC metrics 未被修改。")
    st.rerun()


def _run_qc(qc_service: ProductionQCService, project, execution, artifact, *, retry: bool = False) -> None:
    if _status_value(execution) != ProductionExecutionStatus.SUCCEEDED.value or artifact is None:
        st.warning("QC unavailable: production artifact does not exist or execution is not complete.")
        return
    try:
        if retry:
            qc_service.retry_qc(project.id, execution.id, _value(artifact, "id"))
        else:
            qc_service.run_qc(project.id, execution.id, _value(artifact, "id"))
    except ProductionQCServiceError as exc:
        st.error(f"QC unavailable: {exc}")
        return
    st.success("QC run 已完成。")
    st.rerun()


def _render_qc_section(qc_service: ProductionQCService, project, execution, artifact) -> None:
    st.markdown("#### Quality Control")
    if artifact is None:
        st.warning("QC unavailable: production artifact does not exist.")
        return
    artifact_id = _value(artifact, "id")
    try:
        all_results = qc_service.list_results(project.id, execution.id)
    except ProductionQCServiceError as exc:
        st.warning(f"QC unavailable: {exc}")
        all_results = []
    results = [result for result in all_results if _value(result, "artifact_id") in {None, artifact_id}]
    completed = _status_value(execution) == ProductionExecutionStatus.SUCCEEDED.value
    action_cols = st.columns(3)
    with action_cols[0]:
        if st.button("Run QC", disabled=not completed, key=f"run-qc-{execution.id}-{artifact_id}"):
            _run_qc(qc_service, project, execution, artifact)
    with action_cols[1]:
        if st.button("Retry QC", disabled=not completed or not bool(results), key=f"retry-qc-{execution.id}-{artifact_id}"):
            _run_qc(qc_service, project, execution, artifact, retry=True)
    with action_cols[2]:
        st.caption("Deterministic checks only")

    if not results:
        st.info("No QC result yet. Run QC after the execution succeeds.")
        return
    st.markdown("**QC History**")
    for index, result in enumerate(results, start=1):
        status = _qc_status_value(result)
        with st.container(border=True):
            st.markdown(f"**QC Run #{index} · {status}**")
            summary = _value(result, "summary_json", {}) or {}
            if isinstance(summary, Mapping):
                st.caption(f"Checks · {summary.get('passed', 0)} passed · {summary.get('failed', 0)} failed · {summary.get('skipped', 0)} skipped")
            if status == ProductionQCStatus.QC_FAILED.value:
                st.error("QC failed. See failed checks below.")
            elif status == ProductionQCStatus.QC_PASS.value:
                st.success("QC passed.")
            _render_qc_metrics(qc_service, project, result)
            st.caption(f"QC report · {_relative_path(_value(result, 'report_path'))}")
            _qc_traceability(execution, artifact)

            try:
                reviews = qc_service.list_reviews(project.id, result.id)
            except ProductionQCServiceError as exc:
                st.warning(f"Review unavailable: {exc}")
                reviews = []
            for review in reviews:
                st.caption(f"Human Review · {_review_label(review)} · {_value(review, 'reviewer', 'system')}")
                if _value(review, "notes"):
                    st.caption(f"Note · {_value(review, 'notes')}")
            decision = st.selectbox(
                "Human review decision",
                [ProductionReviewDecision.APPROVED.value, ProductionReviewDecision.REJECTED.value],
                key=f"qc-review-decision-{result.id}",
            )
            notes = st.text_input("Review note (optional)", key=f"qc-review-note-{result.id}")
            if st.button("Submit Review", key=f"submit-qc-review-{result.id}"):
                _submit_review(qc_service, project, result, decision, notes)


def _render_execution_detail(execution_service: ProductionExecutionService, project, execution, qc_service: ProductionQCService | None = None) -> None:
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
    qc_service = qc_service or ProductionQCService()
    for artifact in artifacts:
        columns = st.columns(4)
        columns[0].caption(f"Type · {_value(artifact, 'artifact_type', '—')}")
        columns[1].caption(f"Path · {_relative_path(_value(artifact, 'path'))}")
        columns[2].caption(f"Runtime · {_artifact_runtime(artifact)}")
        columns[3].caption(f"Timestamp · {_value(artifact, 'created_at', '—')}")
        _render_qc_section(qc_service, project, execution, artifact)


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
    qc_service = ProductionQCService()

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
        _render_execution_detail(execution_service, project, execution_options[selected_execution_id], qc_service)
    except ProductionExecutionServiceError as exc:
        st.error(str(exc))
