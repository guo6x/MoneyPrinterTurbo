from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotStatus,
)
from aidrama_studio.pages._shared import (
    current_project_or_stop,
    render_automation_mode,
    render_background_activity,
    render_project_context,
)
from aidrama_studio.services.current_state import CurrentProductionStateService
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
    FinalAssemblyServiceError,
    ProductionOrchestratorError,
    ProductionQueueError,
    ProductionQueueService,
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


def _safe_failure_reason(value: object, default: str = "制作任务暂未完成") -> str:
    """Keep operator-facing errors concise and never render a traceback."""
    text = str(value or default).replace("\r", " ").replace("\n", " ").strip()
    if "Traceback (most recent call last)" in text:
        text = text.split("Traceback (most recent call last)", 1)[0].strip() or default
    # Normal production cards should explain what happened without exposing a
    # local path, hash, provider task id, or a raw worker exception.  The full
    # event payload remains available under the single Advanced drawer.
    import re

    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]*", "相关文件", text)
    text = re.sub(r"\b[0-9a-f]{32,}\b", "相关记录", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)\b(runtime|provider|endpoint|model[_ -]?id|task[_ -]?id)\b", "制作服务", text)
    return text[:300]


def _safe_ui_error(_exc: object, fallback: str) -> str:
    """Return creator-facing copy while keeping raw diagnostics out of normal UI."""

    return fallback


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
    snapshot = _value(execution, "input_snapshot")
    if snapshot is not None:
        st.caption(
            "Snapshot revisions · "
            f"story={_value(snapshot, 'story_revision_id', '—')} · "
            f"script={_value(snapshot, 'script_revision_id', '—')} · "
            f"shot-plan={_value(snapshot, 'shot_plan_revision_id', '—')}"
        )
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
                detail = f" · {_safe_failure_reason(payload['error'])}"
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


# ---------------------------------------------------------------------------
# Director-facing multi-shot console

_JOB_STATUS_LABELS = {
    "DRAFT": "草稿",
    "READY": "待开始",
    "QUEUED": "排队中",
    "RUNNING": "制作中",
    "SUCCEEDED": "制作完成",
    "FAILED": "制作失败",
    "CANCELLED": "已停止",
}
_SHOT_STATUS_LABELS = {
    "PENDING": "待制作",
    "RUNNING": "制作中",
    "SUCCEEDED": "制作完成",
    "FAILED": "制作失败",
    "CANCELLED": "已停止",
    "SKIPPED": "已跳过",
}
_QC_STATUS_LABELS = {
    "QC_PASS": "QC通过",
    "QC_FAILED": "QC未通过",
    "QC_PENDING": "待质检",
    "QC_RUNNING": "质检中",
}


def _job_status(job) -> str:
    return _status_value(job, "status", "DRAFT")


def _shot_status(shot) -> str:
    return _status_value(shot, "status", "PENDING")


def _display_status(status: str, labels: Mapping[str, str]) -> str:
    return labels.get(str(status), str(status))


def _readiness_check(readiness: Mapping[str, object], key: str, reason_tokens: tuple[str, ...]) -> bool:
    """Read only the service's readiness projection; never recalculate it."""
    explicit = readiness.get(key)
    if explicit is not None:
        return bool(explicit)
    reasons = " ".join(_readiness_reasons(readiness)).lower()
    return not any(token.lower() in reasons for token in reason_tokens)


def _render_readiness_console(readiness: Mapping[str, object]) -> None:
    ready = bool(readiness.get("ready"))
    if not ready:
        _render_locked_production_workspace(readiness)
        return
    st.markdown("### 制作准备度")
    st.markdown("项目 → 制作准备 → 镜头生产 → QC → 完成")
    status_col, story_col = st.columns(2)
    status_col.metric("状态", "已就绪" if ready else "未就绪")
    story_col.metric("故事设定", "已确认" if _readiness_check(readiness, "story_bible_approved", ("story bible", "story_bible")) else "未确认")
    script_col, plan_col = st.columns(2)
    script_col.metric("结构化剧本", "已确认" if _readiness_check(readiness, "structured_script_approved", ("structured script", "script")) else "未确认")
    plan_col.metric("分镜方案", "已确认" if _readiness_check(readiness, "shot_plan_approved", ("shot plan", "shot_plan")) else "未确认")

    character_coverage = readiness.get("character_reference_coverage")
    location_coverage = readiness.get("location_reference_coverage")
    coverage_col, location_col = st.columns(2)
    coverage_col.metric("人物参考资产", str(character_coverage if character_coverage is not None else ("已满足" if ready else "需检查")))
    location_col.metric("场景参考资产", str(location_coverage if location_coverage is not None else ("已满足" if ready else "需检查")))

    st.success("制作准备已完成，可以开始整剧制作。")


def _coverage_complete(value: object) -> bool:
    """Interpret the service's canonical coverage projection without recomputing it."""

    current, separator, required = str(value or "").partition("/")
    if not separator:
        return False
    try:
        return int(current) == int(required)
    except ValueError:
        return False


def _locked_prerequisites(readiness: Mapping[str, object]) -> list[tuple[str, str, str, bool]]:
    story_ready = _readiness_check(readiness, "story_bible_approved", ("story bible", "story_bible"))
    script_ready = _readiness_check(readiness, "structured_script_approved", ("structured script", "script"))
    plan_ready = _readiness_check(readiness, "shot_plan_approved", ("shot plan", "shot_plan"))
    references_ready = (
        plan_ready
        and _coverage_complete(readiness.get("character_reference_coverage"))
        and _coverage_complete(readiness.get("location_reference_coverage"))
    )
    return [
        ("确认故事设定", "Story Bible 已批准后才能固定制作基础。", "story", story_ready),
        ("确认结构化剧本", "剧本需与当前故事版本保持一致。", "story", script_ready),
        ("确认分镜方案", "镜头顺序与时长必须经过人工确认。", "director", plan_ready),
        ("锁定角色与场景参考", "生产只使用已明确锁定的参考资产。", "assets", references_ready),
    ]


def _render_locked_shot_board(readiness: Mapping[str, object]) -> None:
    shot_count = int(readiness.get("shot_count") or 0)
    if shot_count:
        visible = min(shot_count, 10)
        slots = "".join(
            '<div class="aidrama-shot-slot">'
            f'<strong>镜头 {index:02d}</strong><span>等待制作</span></div>'
            for index in range(1, visible + 1)
        )
        if shot_count > visible:
            slots += (
                '<div class="aidrama-shot-slot"><strong>更多镜头</strong>'
                f'<span>另有 {shot_count - visible} 个已规划镜头</span></div>'
            )
        body = f'<div class="aidrama-shot-slot-grid">{slots}</div>'
        summary = f"已确认的 Shot Plan · {shot_count} 个镜头"
    else:
        body = (
            '<div class="aidrama-production-empty-board">'
            '<strong>镜头生产区等待 Shot Plan</strong>'
            '<span>确认分镜后，真实镜头会在这里进入制作队列。</span></div>'
        )
        summary = "尚无可用于制作的已确认镜头"
    st.markdown(
        '<section class="aidrama-production-locked-board">'
        '<div class="aidrama-production-board-head">'
        '<strong>Shot Production Board</strong>'
        f'<span>{summary}</span></div>{body}</section>',
        unsafe_allow_html=True,
    )


def _render_locked_production_workspace(readiness: Mapping[str, object]) -> None:
    st.warning("Production 尚未就绪。请先完成下方前置步骤后再创建制作任务。")
    _render_locked_shot_board(readiness)
    prerequisites = _locked_prerequisites(readiness)
    rows = "".join(
        '<div class="aidrama-prereq-row{}">'
        '<span class="aidrama-prereq-mark">{}</span>'
        '<div class="aidrama-prereq-copy"><strong>{}</strong><span>{}</span></div></div>'.format(
            " is-complete" if complete else "",
            "✓" if complete else "○",
            title,
            detail,
        )
        for title, detail, _page, complete in prerequisites
    )
    st.markdown(
        '<section class="aidrama-production-prereqs">'
        '<h3>开始制作前</h3>' + rows + '</section>',
        unsafe_allow_html=True,
    )
    st.button(
        "Create Production Job",
        disabled=True,
        key=f"create-production-job-{readiness.get('project_id') or 'project'}",
    )
    next_item = next((item for item in prerequisites if not item[3]), None)
    if next_item is None:
        return
    title, _detail, page, _complete = next_item
    if st.button(
        f"去完成：{title}",
        type="primary",
        key=f"production-next-prerequisite-{readiness.get('project_id') or 'project'}-{page}",
        use_container_width=True,
    ):
        from aidrama_studio.components.navigation import request_navigation

        request_navigation(page)


def _board_entries(production_service: ProductionService, project, job) -> list[dict[str, object]]:
    method = getattr(production_service, "get_shot_board", None)
    if callable(method):
        try:
            return list(method(project.id, _value(job, "id")))
        except (ProductionServiceError, AttributeError, KeyError):
            pass
    status_method = getattr(production_service, "get_job_status", None)
    if callable(status_method):
        payload = status_method(project.id, _value(job, "id"))
        shots = payload.get("shots", []) if isinstance(payload, Mapping) else []
        return [{"production_shot": shot, "scene_id": "—", "scene_name": "—", "description": ""} for shot in shots]
    return []


def _execution_shot_id(execution) -> str | None:
    snapshot = _value(execution, "input_snapshot")
    params = _value(snapshot, "shot_parameters", {}) if snapshot is not None else {}
    if isinstance(params, Mapping) and len(params) == 1:
        return str(next(iter(params)))
    return None


def _latest_qc(qc_service: ProductionQCService, project, execution):
    if execution is None:
        return None, []
    try:
        results = list(qc_service.list_results(project.id, _value(execution, "id")))
    except (ProductionQCServiceError, AttributeError):
        return None, []
    if not results:
        return None, []
    result = results[-1]
    try:
        reviews = list(qc_service.list_reviews(project.id, _value(result, "id")))
    except (ProductionQCServiceError, AttributeError):
        reviews = []
    return result, reviews


def _review_status(reviews: list[object]) -> str:
    for review in reversed(reviews):
        decision = _status_value(review, "decision", "PENDING")
        if decision == ProductionReviewDecision.REJECTED.value:
            return "REJECTED"
        if decision == ProductionReviewDecision.APPROVED.value:
            return "APPROVED"
    return "PENDING"


def _source_decisions(source_service, project, production_shot_id: str) -> list[object]:
    """Read append-only source decisions through the assembly service boundary.

    ``FinalAssemblyService`` historically exposed source selection but not a
    list projection.  Newer facades may provide ``list_source_decisions``;
    the compatibility fallback only calls the repository's typed read method
    (never SQL from this page), so older installations continue to render the
    board while still showing history when the projection is available.
    """
    if source_service is None:
        return []
    list_method = getattr(source_service, "list_source_decisions", None)
    if callable(list_method):
        try:
            return list(list_method(project.id, production_shot_id))
        except (FinalAssemblyServiceError, AttributeError, KeyError):
            return []
    repository = getattr(source_service, "repository", None)
    list_method = getattr(repository, "list_production_shot_source_decisions", None)
    if callable(list_method):
        try:
            return list(list_method(project.id, production_shot_id))
        except (AttributeError, KeyError, TypeError):
            return []
    return []


def _candidate_qc_review(qc_service, project, qc_result) -> tuple[list[object], str]:
    if qc_result is None:
        return [], "PENDING"
    try:
        reviews = list(qc_service.list_reviews(project.id, _value(qc_result, "id")))
    except (ProductionQCServiceError, AttributeError):
        reviews = []
    return reviews, _review_status(reviews)


def _shot_source_candidates(
    execution_service: ProductionExecutionService,
    qc_service: ProductionQCService,
    project,
    executions: list[object],
    shot,
) -> list[dict[str, object]]:
    """Build a read-only source projection from execution service facades.

    The page never opens files or constructs a runtime snapshot.  Artifact,
    QC and review records remain owned by their respective services; this
    projection only joins them for operator-facing source selection.
    """
    shot_id = str(_value(shot, "shot_id", ""))
    production_shot_id = str(_value(shot, "id", shot_id))
    candidates: list[dict[str, object]] = []
    for execution in executions:
        if _execution_shot_id(execution) not in {shot_id, production_shot_id}:
            continue
        execution_id = _value(execution, "id")
        try:
            artifacts = list(execution_service.list_artifacts(project.id, execution_id))
        except (ProductionExecutionServiceError, AttributeError):
            artifacts = []
        try:
            qc_results = list(qc_service.list_results(project.id, execution_id))
        except (ProductionQCServiceError, AttributeError):
            qc_results = []
        for artifact in artifacts:
            artifact_id = _value(artifact, "id")
            matching = [item for item in qc_results if _value(item, "artifact_id") in {None, artifact_id}]
            qc_result = matching[-1] if matching else None
            reviews, review_status = _candidate_qc_review(qc_service, project, qc_result)
            metadata = _value(artifact, "metadata_json", {}) or {}
            role = str(metadata.get("artifact_role") or "FINAL") if isinstance(metadata, Mapping) else "FINAL"
            candidates.append(
                {
                    "execution": execution,
                    "artifact": artifact,
                    "qc_result": qc_result,
                    "reviews": reviews,
                    "review_status": review_status,
                    "is_preview": role.upper() == "PREVIEW",
                    "technically_qualified": (
                        _status_value(execution) == ProductionExecutionStatus.SUCCEEDED.value
                        and _status_value(qc_result, "status", "QC_PENDING") == ProductionQCStatus.QC_PASS.value
                    ),
                    "qualified": (
                        _status_value(execution) == ProductionExecutionStatus.SUCCEEDED.value
                        and _status_value(qc_result, "status", "QC_PENDING") == ProductionQCStatus.QC_PASS.value
                        and review_status == ProductionReviewDecision.APPROVED.value
                    ),
                }
            )
    return candidates


def _render_source_history(source_service, project, shot) -> list[object]:
    history = _source_decisions(source_service, project, str(_value(shot, "id", _value(shot, "shot_id", ""))))
    if not history:
        st.caption("Source decision history · 暂无显式选择（qualified source 仍可预览）。")
        return history
    latest = history[-1]
    st.caption(
        f"Current decision · {_status_value(latest, 'decision_type', 'UNKNOWN')} · "
        f"{_status_value(latest, 'selection_kind', '—')} · "
        f"execution={str(_value(latest, 'production_execution_id', '—'))[:12]} · "
        f"artifact={str(_value(latest, 'production_artifact_id', '—'))[:12]}"
    )
    st.markdown("**Source decision history（append-only）**")
    for decision in history:
        sequence = _value(decision, "sequence_number", "—")
        decision_type = _status_value(decision, "decision_type", "UNKNOWN")
        selection_kind = _status_value(decision, "selection_kind", "—")
        st.caption(
            f"#{sequence} · {decision_type} · {selection_kind} · "
            f"execution={str(_value(decision, 'production_execution_id', '—'))[:12]} · "
            f"artifact={str(_value(decision, 'production_artifact_id', '—'))[:12]}"
        )
        if _value(decision, "notes"):
            st.caption(f"Note · {_value(decision, 'notes')}")
    return history


def _source_action(
    source_service,
    project,
    job,
    shot,
    candidate: Mapping[str, object],
    *,
    promote_preview: bool,
) -> None:
    if source_service is None:
        st.warning("Shot source selection service unavailable.")
        return
    select_method = getattr(source_service, "select_shot_source", None)
    if not callable(select_method):
        st.warning("Shot source selection service unavailable.")
        return
    execution = candidate["execution"]
    artifact = candidate["artifact"]
    try:
        select_method(
            project.id,
            _value(job, "id"),
            _value(shot, "id", _value(shot, "shot_id")),
            production_execution_id=_value(execution, "id"),
            production_artifact_id=_value(artifact, "id"),
            selected_by="user",
            promote_preview=promote_preview,
        )
    except (FinalAssemblyServiceError, AttributeError, TypeError) as exc:
        st.error(f"Shot source selection unavailable: {str(exc)[:240]}")
        return
    st.success("Preview 已显式 Promote；新的 source decision 已追加，历史未被覆盖。" if promote_preview else "Shot source 已选择；历史未被覆盖。")
    st.rerun()


def _creative_regeneration_action(execution_service, project, job, shot, candidate: Mapping[str, object]) -> None:
    """Append a creative attempt using the canonical execution service.

    The existing immutable snapshot is passed through unchanged.  The page
    does not compose a new snapshot and never invokes a provider directly.
    """
    execution = candidate["execution"]
    reviews = list(candidate.get("reviews") or [])
    rejected_review = next(
        (item for item in reversed(reviews) if _status_value(item, "decision", "PENDING") == ProductionReviewDecision.REJECTED.value),
        None,
    )
    snapshot = _value(execution, "input_snapshot")
    request_method = getattr(execution_service, "request_creative_regeneration", None)
    if rejected_review is None or snapshot is None or not callable(request_method):
        st.warning("Creative regeneration requires a rejected review and an immutable execution snapshot.")
        return
    try:
        request_method(
            project.id,
            _value(job, "id"),
            _value(shot, "id", _value(shot, "shot_id")),
            _value(rejected_review, "id"),
            snapshot,
            worker_type=str(_value(execution, "worker_type", "mpt")),
            runtime_plan_id=_value(execution, "runtime_plan_id"),
            generation_brief_id=_value(execution, "generation_brief_id"),
        )
    except (ProductionExecutionServiceError, AttributeError, TypeError) as exc:
        st.error(f"Creative regeneration unavailable: {str(exc)[:240]}")
        return
    st.success("Creative regeneration attempt 已追加到队列；原 execution、artifact 与 review 保持不变。")
    st.rerun()


def _render_shot_sources(source_service, execution_service, qc_service, project, job, shot, executions) -> None:
    """Render candidates, current decision and append-only controls for a Shot."""
    st.markdown("**Shot sources**")
    history = _render_source_history(source_service, project, shot)
    current = history[-1] if history else None
    candidates = _shot_source_candidates(execution_service, qc_service, project, executions, shot)
    if not candidates:
        st.caption("暂无可用 source candidate。")
        return
    for index, candidate in enumerate(candidates, start=1):
        execution = candidate["execution"]
        artifact = candidate["artifact"]
        artifact_id = _value(artifact, "id", index)
        is_current = bool(
            current is not None
            and _status_value(current, "decision_type") == "SELECTED"
            and _value(current, "production_execution_id") == _value(execution, "id")
            and _value(current, "production_artifact_id") == artifact_id
        )
        role_label = "PREVIEW" if candidate["is_preview"] else "FINAL"
        status_label = "CURRENT" if is_current else (
            "FINAL ELIGIBLE" if candidate["qualified"] else (
                "WAITING HUMAN REVIEW"
                if candidate["technically_qualified"]
                and candidate["review_status"] == ProductionReviewDecision.PENDING.value
                else "NOT QUALIFIED"
            )
        )
        with st.container(border=True):
            st.markdown(f"**Candidate #{index} · {role_label} · {status_label}**")
            st.caption(
                f"Execution · {_value(execution, 'id', '—')} · "
                f"Artifact · {artifact_id} · Path · {_relative_path(_value(artifact, 'path'))}"
            )
            st.caption(
                f"QC · {_status_value(candidate.get('qc_result'), 'status', 'QC_PENDING')} · "
                f"Review · {candidate['review_status']}"
            )
            if (
                candidate["technically_qualified"]
                and candidate["review_status"] == ProductionReviewDecision.PENDING.value
            ):
                st.info("技术检查通过，等待人工审片；不能选择为最终来源。")
            if candidate["is_preview"]:
                st.info("Preview source 不能自动进入最终成片，必须显式 Promote。")
            action_cols = st.columns(2)
            with action_cols[0]:
                if candidate["qualified"] and not is_current:
                    label = "Promote Preview" if candidate["is_preview"] else "Select source"
                    if st.button(label, key=f"source-select-{_value(job, 'id')}-{_value(shot, 'id')}-{artifact_id}"):
                        _source_action(
                            source_service,
                            project,
                            job,
                            shot,
                            candidate,
                            promote_preview=bool(candidate["is_preview"]),
                        )
            with action_cols[1]:
                if (
                    candidate["review_status"] == ProductionReviewDecision.REJECTED.value
                    and candidate["technically_qualified"]
                ):
                    if st.button("Regenerate creative attempt", key=f"creative-regenerate-{_value(job, 'id')}-{_value(shot, 'id')}-{artifact_id}"):
                        _creative_regeneration_action(execution_service, project, job, shot, candidate)


def _render_shot_board(
    production_service: ProductionService,
    execution_service: ProductionExecutionService,
    qc_service: ProductionQCService,
    project,
    job,
    progress: Mapping[str, object],
    source_service=None,
    *,
    show_source_controls: bool = False,
) -> tuple[list[object], dict[str, object]]:
    entries = _board_entries(production_service, project, job)
    try:
        executions = execution_service.list_executions(project.id, _value(job, "id"))
    except (ProductionExecutionServiceError, AttributeError):
        executions = []
    by_shot: dict[str, object] = {}
    for execution in executions:
        shot_id = _execution_shot_id(execution)
        if shot_id:
            by_shot[shot_id] = execution

    current_shot_id = str(progress.get("current_shot_id") or "")
    st.markdown("### 镜头生产 Board")
    total = int(progress.get("total_shots", len(entries)) or 0)
    completed = int(progress.get("completed_shots", 0) or 0)
    failed = int(progress.get("failed_shots", 0) or 0)
    pending = int(progress.get("pending_shots", max(total - completed - failed, 0)) or 0)
    progress_cols = st.columns(2)
    progress_cols[0].metric("总镜头", total)
    progress_cols[1].metric("已完成", completed)
    progress_cols = st.columns(2)
    progress_cols[0].metric("失败", failed)
    progress_cols[1].metric("待制作", pending)
    # Keep the internal shot key for highlighting, but present an ordinal to
    # creators instead of a database identifier.
    current_shot_label = None
    if current_shot_id:
        for item in entries:
            candidate_shot = _value(item, "production_shot")
            if str(_value(candidate_shot, "shot_id", "")) == current_shot_id:
                current_shot_label = _value(candidate_shot, "order_index", None)
                break
    st.caption(f"当前镜头 · {('镜头 ' + str(current_shot_label)) if current_shot_label is not None else '等待下一镜头'}")
    st.caption(f"{completed}/{total} 个镜头完成 · {progress.get('percent_complete', 0)}%")
    if progress.get("total_shots"):
        st.progress(min(100, max(0, int(float(progress.get("percent_complete", 0))))))

    shown_executions: list[object] = []
    for entry in sorted(entries, key=lambda item: (_value(_value(item, "production_shot"), "order_index", 0), str(_value(_value(item, "production_shot"), "id", "")))):
        shot = _value(entry, "production_shot")
        shot_id = str(_value(shot, "shot_id", "—"))
        execution = by_shot.get(shot_id)
        if execution is not None:
            shown_executions.append(execution)
        execution_status = _status_value(execution, "status", "PENDING") if execution is not None else "PENDING"
        shot_status = _shot_status(shot)
        qc_result, reviews = _latest_qc(qc_service, project, execution)
        qc_status = _qc_status_value(qc_result) if qc_result is not None else "QC_PENDING"
        review_status = _review_status(reviews)
        highlighted = shot_id == current_shot_id or shot_status == ProductionShotStatus.RUNNING.value
        scene_label = str(_value(entry, "scene_name", "") or "未命名场景").strip()
        label = f"镜头 {_value(shot, 'order_index', '—')} · {scene_label}"
        with st.container(border=True):
            if highlighted:
                st.markdown(f"#### ▶ {label}")
                st.info("当前镜头")
            else:
                st.markdown(f"#### {label}")
            description = str(_value(entry, "description", "") or "").strip()
            # The board is media-first even when a real thumbnail is not yet
            # available.  A provider/path identifier is never shown here.
            st.caption("镜头缩略图 · 媒体完成后可在审片预览")
            if description:
                st.caption(description[:240])
            cols = st.columns(3)
            cols[0].markdown(f"制作：**{_display_status(execution_status if execution is not None else shot_status, _SHOT_STATUS_LABELS)}**")
            cols[1].markdown(f"QC：**{_display_status(qc_status, _QC_STATUS_LABELS)}**")
            review_display = {
                "PENDING": "待审片",
                "REJECTED": "需要修改",
                "APPROVED": "已通过",
            }.get(review_status, "待审片")
            cols[2].markdown(f"人审：**{review_display}**")
            if execution_status == ProductionExecutionStatus.FAILED.value:
                reason = "制作服务未完成"
                try:
                    events = execution_service.list_events(project.id, _value(execution, "id"))
                    for event in reversed(events):
                        payload = _value(event, "payload_json", {}) or {}
                        if isinstance(payload, Mapping) and payload.get("error"):
                            reason = _safe_failure_reason(payload["error"])
                            break
                except (ProductionExecutionServiceError, AttributeError):
                    pass
                st.error(f"镜头制作失败：{reason}")
            if qc_status == ProductionQCStatus.QC_FAILED.value:
                st.error("镜头 QC 未通过。请查看 QC 详情；不会自动重新生成。")
                summary = _value(qc_result, "summary_json", {}) or {}
                failed_metrics = summary.get("failed_metrics") if isinstance(summary, Mapping) else None
                if not failed_metrics:
                    try:
                        failed_metrics = [
                            _qc_metric_label(metric)
                            for metric in qc_service.list_metrics(project.id, _value(qc_result, "id"))
                            if _status_value(metric, "status") == "FAIL"
                        ]
                    except (ProductionQCServiceError, AttributeError):
                        failed_metrics = []
                if failed_metrics:
                    st.caption(f"有 {len(failed_metrics)} 项技术检查未通过；请在审片页查看明细。")
                if st.button("查看 QC 详情", key=f"view-qc-{shot_id}"):
                    st.session_state[f"production-advanced-{_value(job, 'id')}"] = True
            if review_status == "REJECTED":
                st.warning("人审要求修改；请先修订生成意图，再明确创建新版本。")
            next_action = _shot_next_action(
                execution_status=execution_status,
                shot_status=shot_status,
                qc_status=qc_status,
                review_status=review_status,
            )
            st.caption(f"下一步 · {next_action}")
            # Candidate/source controls belong to Review.  Keep this switch
            # only for compatibility callers and focused diagnostics tests.
            if show_source_controls:
                _render_shot_sources(
                    source_service,
                    execution_service,
                    qc_service,
                    project,
                    job,
                    shot,
                    executions,
                )
    return shown_executions, {"executions": executions, "by_shot": by_shot}


def _shot_next_action(
    *,
    execution_status: str,
    shot_status: str,
    qc_status: str,
    review_status: str,
) -> str:
    """Translate canonical shot state into one creator-facing next action."""

    if execution_status in {ProductionExecutionStatus.QUEUED.value, ProductionExecutionStatus.RUNNING.value}:
        return "等待生成完成"
    if execution_status == ProductionExecutionStatus.FAILED.value or shot_status == ProductionShotStatus.FAILED.value:
        return "恢复已有结果"
    if qc_status == ProductionQCStatus.QC_FAILED.value:
        return "查看审片中的技术检查"
    if review_status == ProductionReviewDecision.REJECTED.value:
        return "修改生成意图后重做"
    if qc_status == ProductionQCStatus.QC_PASS.value:
        return "进入审片"
    if execution_status == ProductionExecutionStatus.SUCCEEDED.value or shot_status == ProductionShotStatus.SUCCEEDED.value:
        return "运行技术检查"
    return "等待提交"


def _make_orchestrator(production_service, execution_service, qc_service):
    # The Streamlit request only persists a durable queue intent.  Provider
    # resolution, submit/poll/QC and retry live in the background runner.
    return ProductionQueueService(production_service=production_service)


class _UnavailableOrchestrator:
    """Small fallback used by isolated page tests with service fakes."""

    def get_job_progress(self, project_id, job_id):
        return {"total_shots": 0, "completed_shots": 0, "failed_shots": 0, "pending_shots": 0, "percent_complete": 0, "current_shot_id": None}

    def run_job(self, *args, **kwargs):
        raise ProductionOrchestratorError("Production orchestrator is unavailable")

    resume_job = run_job
    cancel_job = run_job


def _orchestrator_action(
    orchestrator,
    action: str,
    project,
    job=None,
    *,
    ensure_job=None,
    authorization: Mapping[str, object] | None = None,
    endpoint_profile_id: str | None = None,
) -> None:
    try:
        if action == "start":
            if job is None:
                job = ensure_job() if callable(ensure_job) else None
            if job is None:
                raise ProductionOrchestratorError("无法创建 ProductionJob")
            orchestrator.run_job(
                project.id,
                _value(job, "id"),
                authorization=authorization,
                endpoint_profile_id=endpoint_profile_id,
            )
        elif action == "resume":
            if job is None:
                raise ProductionOrchestratorError("继续制作需要一个已存在的 ProductionJob")
            orchestrator.resume_job(
                project.id,
                _value(job, "id"),
                authorization=authorization,
                endpoint_profile_id=endpoint_profile_id,
            )
        elif action == "cancel":
            if job is None:
                raise ProductionOrchestratorError("停止制作需要一个正在运行的 ProductionJob")
            orchestrator.cancel_job(project.id, _value(job, "id"), reason="user")
    except (ProductionQueueError, ProductionOrchestratorError, ProductionServiceError, ProductionExecutionServiceError, NotImplementedError) as exc:
        st.error(_safe_ui_error(exc, "制作操作未完成，请检查准备项后重试。"))
        return
    st.rerun()


def _render_primary_action(orchestrator, production_service, project, job, readiness, progress, *, ensure_job=None) -> None:
    """Render one state-dependent production action and paid disclosure."""

    status = _job_status(job) if job is not None else ("READY" if readiness.get("ready") else "DRAFT")
    ready = bool(readiness.get("ready"))
    if not ready:
        st.button("开始整剧制作", type="primary", disabled=True, key=f"start-blocked-{project.id}")
        return
    if status in {"QUEUED", "RUNNING"}:
        st.caption("制作在后台进行；当前页面仍可查看已完成镜头。")
        cols = st.columns(2)
        if cols[0].button("刷新制作状态", key=f"refresh-job-{_value(job, 'id', project.id)}"):
            try:
                orchestrator.get_job_progress(project.id, _value(job, "id"))
            except Exception:
                pass
            st.rerun()
        if cols[1].button("停止制作", key=f"cancel-job-{_value(job, 'id', project.id)}"):
            _orchestrator_action(orchestrator, "cancel", project, job)
        return
    if status == "SUCCEEDED":
        st.success("制作完成，可以进入审片。")
        if st.button("进入审片", type="primary", key=f"go-review-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("review")
        return
    if status == "FAILED":
        st.error("制作遇到问题。系统不会自动重新生成或付费重试；请先查看失败镜头。")
        return

    label = "继续制作" if status == "CANCELLED" or int(progress.get("completed_shots", 0) or 0) > 0 else "开始整剧制作"
    preview_method = getattr(orchestrator, "preview_authorization", None)
    if job is None and not callable(preview_method):
        # A missing preview is an unavailable authorization surface, not an
        # implicit approval.  Keep the CTA visibly disabled until the runtime
        # can disclose the bounded work and transfer scope.
        st.info("制作授权预览暂不可用；准备完成后才能确认并开始制作。")
        st.button(label, type="primary", disabled=True, key=f"prepare-production-{project.id}")
        return
    if callable(preview_method) and job is None:
        st.info("开始前会先准备制作版本，并显示新建任务数量与素材传输范围供你确认。")
        if st.button(label, type="primary", key=f"prepare-production-{project.id}"):
            if not callable(ensure_job):
                st.error("暂时无法准备制作版本。")
                return
            ensure_job()
            st.rerun()
        return

    authorization = None
    disabled = False
    endpoint_profile_id = None
    if callable(preview_method) and job is not None:
        try:
            preview = preview_method(
                project.id,
                _value(job, "id"),
                endpoint_profile_id=None,
                max_paid_attempts=1,
            )
        except ProductionQueueError as exc:
            st.warning(_safe_ui_error(exc, "视频生成能力尚未就绪，请前往设置检查能力状态。"))
            st.button(label, type="primary", disabled=True, key=f"primary-production-{_value(job, 'id', project.id)}")
            return
        except Exception as exc:
            st.warning(_safe_ui_error(exc, "制作授权预览暂不可用，请稍后重试。"))
            st.button(label, type="primary", disabled=True, key=f"primary-production-{_value(job, 'id', project.id)}")
            return

        st.markdown("### 付费确认")
        st.caption("只确认本次新建任务；恢复已有任务不会重复提交。")
        cols = st.columns(2)
        cols[0].metric("镜头数量", int(getattr(preview, "shot_count", 0) or 0))
        cols[1].metric("参考图数量", int(getattr(preview, "reference_count", 0) or 0))
        cols = st.columns(2)
        target_duration = float(getattr(preview, "target_episode_duration_seconds", 0) or 0)
        cols[0].metric("目标时长", f"{target_duration:g} 秒")
        request_count = int(getattr(preview, "estimated_provider_requests", 0) or 0)
        cols[1].metric("最多新建", f"{request_count} 个视频任务")

        content_labels = {
            "TEXT": "文本",
            "REFERENCE_IMAGE": "参考图片",
            "VIDEO": "视频",
            "AUDIO": "音频",
        }
        transmitted = tuple(getattr(preview, "transmitted_content_types", ()) or ())
        content_summary = "、".join(content_labels.get(str(item), "创作素材") for item in transmitted) or "创作文本"
        region = str(getattr(preview, "deployment_region", "") or "").upper()
        region_label = {
            "LOCAL": "本机",
            "MAINLAND": "中国大陆",
            "MAINLAND_CHINA": "中国大陆",
            "INTERNATIONAL": "国际",
        }.get(region, "已配置的云端区域")
        if region == "LOCAL":
            st.info(f"处理位置 · {region_label}；素材不会上传到云端。")
        else:
            st.warning(f"素材传输 · {region_label}；将发送：{content_summary}。")
        native = str(getattr(preview, "native_generation_resolution", "") or "")
        delivery = str(getattr(preview, "delivery_resolution", "") or "")
        if native and delivery and native != delivery:
            st.caption(f"生成画面 {native} → 最终交付 {delivery}；交付缩放不会被描述为原生生成。")
        st.caption("当前没有可靠的实时价格，因此不会猜测金额。")
        fingerprint = str(getattr(preview, "authorization_fingerprint", "authorization"))
        approved = st.checkbox(
            f"我已确认素材传输范围，并批准本次最多新建 {request_count} 个视频任务",
            key=f"paid-authorization-{_value(job, 'id')}-{fingerprint}",
        )
        disabled = not approved
        if approved:
            authorization = {
                "approved": True,
                "provider_profile_id": getattr(preview, "provider_profile_id", None),
                "provider_id": getattr(preview, "provider_id", None),
                "model_id": getattr(preview, "model_id", None),
                "manifest_id": getattr(preview, "manifest_id", None),
                "manifest_hash": getattr(preview, "manifest_hash", None),
                "codec_id": getattr(preview, "codec_id", None),
                "deployment_region": getattr(preview, "deployment_region", None),
                "endpoint_profile_id": getattr(preview, "endpoint_profile_id", None),
                "endpoint_class": getattr(preview, "endpoint_class", None),
                "reference_count": getattr(preview, "reference_count", 0),
                "max_paid_attempts": getattr(preview, "max_paid_attempts", 1),
                "estimated_provider_requests": request_count,
                "target_episode_duration_seconds": target_duration,
                "native_generation_resolution": getattr(preview, "native_generation_resolution", None),
                "native_generation_fps": getattr(preview, "native_generation_fps", None),
                "delivery_resolution": getattr(preview, "delivery_resolution", None),
                "target_fps": getattr(preview, "target_fps", None),
                "delivery_strategy": getattr(preview, "delivery_strategy", None),
                "quality_mode": getattr(preview, "quality_mode", None),
                "authorization_fingerprint": fingerprint,
            }
    if not callable(preview_method) and job is not None:
        st.warning("制作授权预览暂不可用；系统不会在未确认范围时提交新任务。")
        approved = st.checkbox(
            "我已确认本次会创建新的视频任务，并同意继续",
            key=f"generic-paid-authorization-{_value(job, 'id', project.id)}",
        )
        disabled = not approved
        authorization = {"approved": True} if approved else None
    if st.button(label, type="primary", disabled=disabled, key=f"primary-production-{_value(job, 'id', project.id)}"):
        _orchestrator_action(
            orchestrator,
            "resume" if label == "继续制作" else "start",
            project,
            job,
            ensure_job=ensure_job,
            authorization=authorization,
            endpoint_profile_id=endpoint_profile_id,
        )


def _render_generation_brief_editor(orchestrator, project, job) -> None:
    prepare = getattr(orchestrator, "prepare_generation_briefs", None)
    save_override = getattr(orchestrator, "save_generation_brief_override", None)
    if not callable(prepare) or not callable(save_override):
        return
    try:
        briefs = prepare(project.id, _value(job, "id"))
    except Exception as exc:
        st.warning(_safe_ui_error(exc, "镜头生成意图暂不可编辑，请稍后重试。"))
        return
    with st.expander("镜头生成意图 · 付费前可编辑", expanded=False):
        st.caption(
            "这里编辑画面、动作、运镜与连续性要求。保存会创建新版本，"
            "已经进入制作的版本不会被改写。"
        )
        for brief_index, brief in enumerate(briefs, start=1):
            characters = "、".join(
                str(item.get("name") or item.get("id") or "")
                for item in brief.character_context
                if isinstance(item, Mapping)
            ) or "—"
            st.markdown(f"#### 镜头 {brief_index} · {characters}")
            st.caption("当前可编辑版本")
            with st.form(f"generation-brief-{brief.id}", clear_on_submit=False):
                action = st.text_area("人物 / 动作", value=brief.action, key=f"brief-action-{brief.id}")
                framing = st.text_input("镜头语言 / 景别", value=brief.framing, key=f"brief-framing-{brief.id}")
                composition = st.text_input("构图", value=brief.composition, key=f"brief-composition-{brief.id}")
                camera = st.text_input("运镜", value=brief.camera_movement, key=f"brief-camera-{brief.id}")
                lens = st.text_input("镜头焦段意图", value=brief.lens_intent, key=f"brief-lens-{brief.id}")
                lighting = st.text_area(
                    "光线",
                    value=str(brief.lighting.get("quality") or brief.lighting.get("notes") or ""),
                    key=f"brief-lighting-{brief.id}",
                )
                mood = st.text_input("情绪", value=brief.mood, key=f"brief-mood-{brief.id}")
                continuity = st.text_area(
                    "视觉连续性（逗号分隔）",
                    value="，".join(brief.continuity_constraints),
                    key=f"brief-continuity-{brief.id}",
                )
                negative = st.text_area(
                    "负向约束（逗号分隔）",
                    value="，".join(brief.negative_constraints),
                    key=f"brief-negative-{brief.id}",
                )
                dialogue = st.text_area(
                    "对白 / 声音意图",
                    value=brief.dialogue_audio_intent,
                    key=f"brief-dialogue-{brief.id}",
                )
                duration = st.number_input(
                    "镜头创作时长（秒）",
                    min_value=0.1,
                    value=float(brief.target_duration_seconds),
                    key=f"brief-duration-{brief.id}",
                )
                saved = st.form_submit_button("保存镜头生成意图", type="primary")
            if saved:
                try:
                    save_override(
                        project.id,
                        _value(job, "id"),
                        brief.shot_id,
                        {
                            "action": action,
                            "framing": framing,
                            "composition": composition,
                            "camera_movement": camera,
                            "lens_intent": lens,
                            "lighting": dict(brief.lighting) | {"quality": lighting},
                            "mood": mood,
                            "continuity_constraints": continuity,
                            "negative_constraints": negative,
                            "dialogue_audio_intent": dialogue,
                            "target_duration_seconds": float(duration),
                        },
                        base_brief_id=brief.id,
                    )
                except Exception as exc:
                    st.error(_safe_ui_error(exc, "镜头生成意图保存失败，请检查内容后重试。"))
                else:
                    st.success("镜头生成意图已保存；开始制作前需要重新确认本次任务。")
                    st.rerun()


def _select_default_job(project_id: str, jobs: list[object]):
    """Choose an active/latest job without making its identity part of UX."""
    if not jobs:
        return None
    selected_id = st.session_state.get(f"production-job-select-{project_id}")
    if selected_id:
        selected = next((job for job in jobs if str(_value(job, "id")) == str(selected_id)), None)
        if selected is not None:
            return selected
    active = [job for job in jobs if _job_status(job) in {"QUEUED", "RUNNING"}]
    return active[0] if active else jobs[-1]


def _select_current_job(
    project_id: str,
    jobs: list[object],
    production_service: ProductionService,
):
    """Use canonical current-production truth with a fixture-safe fallback."""

    try:
        state = CurrentProductionStateService(
            getattr(production_service, "repository", None)
        ).derive(project_id)
        if state.job is not None:
            return state.job
    except Exception:
        # Isolated page tests intentionally provide only the page facade.  The
        # compatibility selector is never a second production authority in a
        # real repository-backed render.
        pass
    return _select_default_job(project_id, jobs)


def _production_activity(job, progress: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Project persisted job state into the shared non-blocking activity strip."""

    if job is None:
        return ()
    status = _job_status(job)
    if status not in {"QUEUED", "RUNNING", "FAILED", "CANCELLED"}:
        return ()
    total = int(progress.get("total_shots", 0) or 0)
    completed = int(progress.get("completed_shots", 0) or 0)
    percent = progress.get("percent_complete")
    try:
        numeric_percent = float(percent) if percent is not None else None
        if numeric_percent is not None and numeric_percent > 1:
            numeric_percent /= 100
        fraction = max(0.0, min(1.0, numeric_percent)) if numeric_percent is not None else None
    except (TypeError, ValueError):
        fraction = None
    state = {
        "QUEUED": "queued",
        "RUNNING": "running",
        "FAILED": "failed",
        "CANCELLED": "interrupted",
    }[status]
    detail = (
        f"已完成 {completed}/{total} 个镜头；工作区仍可查看。"
        if total
        else "制作状态已持久保存；工作区仍可查看。"
    )
    if status == "FAILED":
        detail = "制作遇到问题；不会自动重新付费提交，请先查看失败镜头。"
    elif status == "CANCELLED":
        detail = "制作已暂停；已有结果保留，继续前会重新确认。"
    return (
        {
            "activity_id": _value(job, "id"),
            "title": "镜头制作后台活动",
            "detail": detail,
            "state": state,
            "progress": fraction,
        },
    )


def _render_budget_summary(
    progress: Mapping[str, object], budget: object | None = None
) -> None:
    """Show creator-facing request bounds without guessing a currency price."""

    total = int(progress.get("total_shots", 0) or 0)
    planned = int(_value(budget, "planned_creates", total) or 0)
    authorized = int(_value(budget, "authorized_max", 0) or 0)
    consumed = int(_value(budget, "consumed_creates", 0) or 0)
    reserved = int(_value(budget, "reserved_creates", 0) or 0)
    uncertain = int(_value(budget, "uncertain_creates", 0) or 0)
    remaining = int(_value(budget, "remaining_creates", 0) or 0)
    with st.container(border=True):
        st.markdown("### 成本与预算")
        left, right = st.columns(2)
        left.metric("计划调用", planned)
        right.metric("已消费", consumed)
        left, right = st.columns(2)
        left.metric("剩余授权", remaining)
        right.metric("不确定", uncertain)
        st.caption(f"授权上限 · {authorized} · 已保留未提交 · {reserved}")
        st.caption("恢复已有任务不会创建新的付费请求；任何新视频任务都需要再次明确确认。")
        st.caption("当前没有可靠的实时价格，因此不会猜测金额。")


def _render_empty_shot_board(readiness: Mapping[str, object]) -> None:
    st.markdown("### 镜头生产")
    st.caption(f"总镜头 · {int(readiness.get('shot_count') or 0)} · 制作开始后将显示每个镜头的进度、QC 与人审状态。")


def render() -> None:
    page_header("制作", "PRODUCTION WORKSPACE", "查看镜头进度，处理失败项，并在明确授权后开始制作。")
    project = current_project_or_stop()
    render_project_context(
        project,
        stage="制作",
        next_action="查看制作进度",
        next_page="production",
        suppress_next=True,
    )
    production_service = ProductionService()
    execution_service = ProductionExecutionService(production_service=production_service)
    qc_service = ProductionQCService()
    # Source/candidate selection is a Review concern.  Do not even construct
    # the FinalAssembly facade on the normal Production path: besides keeping
    # the board focused, this prevents a hidden source read from becoming a
    # second current-state authority.  The compatibility helpers above remain
    # available to explicit diagnostic callers that opt in with a service.
    source_service = None

    try:
        readiness = production_service.validate_job_readiness(project.id)
        jobs = production_service.list_jobs(project.id)
    except ProductionServiceError as exc:
        st.error(_safe_ui_error(exc, "制作任务暂时无法读取，请稍后重试。"))
        return

    # Legacy label retained for discoverability: Production Readiness Check.
    _render_readiness_console(readiness)
    if not bool(readiness.get("ready")):
        render_automation_mode(project.id, compact=True)
        return
    render_automation_mode(project.id, compact=True)

    selected_job = _select_current_job(project.id, jobs, production_service)
    job_readiness = readiness
    try:
        if selected_job is not None:
            job_readiness = production_service.validate_job_readiness(project.id, _value(selected_job, "shot_plan_revision_id"))
            # Materialize canonical ProductionShot rows through the service so
            # a READY job has a visible board before the first runtime starts.
            ensure_shots = getattr(production_service, "create_production_shots", None)
            if bool(job_readiness.get("ready")) and callable(ensure_shots):
                ensure_shots(project.id, _value(selected_job, "id"))
        if selected_job is not None and hasattr(production_service, "repository"):
            progress = _make_orchestrator(production_service, execution_service, qc_service).get_job_progress(project.id, selected_job.id)
        else:
            progress = {
                "total_shots": job_readiness.get("shot_count", 0),
                "completed_shots": 0,
                "failed_shots": 0,
                "pending_shots": job_readiness.get("shot_count", 0),
                "percent_complete": 0,
                "current_shot_id": None,
            }
    except (ProductionServiceError, ProductionExecutionServiceError) as exc:
        st.warning(_safe_ui_error(exc, "制作进度暂不可用；工作区仍可继续查看。"))
        progress = {"total_shots": job_readiness.get("shot_count", 0), "completed_shots": 0, "failed_shots": 0, "pending_shots": job_readiness.get("shot_count", 0), "percent_complete": 0, "current_shot_id": None}

    # Orchestration remains canonical; the page only supplies an existing job
    # or asks ProductionService to create one at the moment of the CTA.
    orchestrator = _make_orchestrator(production_service, execution_service, qc_service) if hasattr(production_service, "repository") else _UnavailableOrchestrator()

    def ensure_job():
        if selected_job is not None:
            return selected_job
        return production_service.create_production_job(project.id)

    # Keep durable queue activity visible without replacing the board.
    render_background_activity(_production_activity(selected_job, progress), compact=True)
    budget = None
    if selected_job is not None:
        try:
            budget = orchestrator.budget_projection(
                project.id, _value(selected_job, "id")
            )
        except (ProductionQueueError, ValueError, AttributeError):
            budget = None
    _render_budget_summary(progress, budget)
    _render_primary_action(orchestrator, production_service, project, selected_job, job_readiness, progress, ensure_job=ensure_job)
    if selected_job is not None:
        try:
            _render_shot_board(
                production_service,
                execution_service,
                qc_service,
                project,
                selected_job,
                progress,
                source_service,
                show_source_controls=False,
            )
        except (ProductionServiceError, ProductionExecutionServiceError) as exc:
            st.warning(_safe_ui_error(exc, "镜头生产信息暂不可用；请稍后刷新。"))
    else:
        _render_empty_shot_board(job_readiness)

    # Keep low-level IDs, events and artifact metadata available without
    # overwhelming the default director view.
    advanced_key = f"production-advanced-{project.id}"
    with st.expander("执行详情（高级）", expanded=bool(st.session_state.get(advanced_key))):
        st.caption("Submit Execution is delegated to ProductionOrchestrator; execution details remain available here.")
        st.markdown("#### Production Jobs")
        if st.button(
            "Create Production Job",
            disabled=not bool(readiness.get("ready")),
            key=f"create-production-job-{project.id}",
        ):
            _create_job(production_service, project)
        if not jobs:
            st.info("当前项目还没有 Production Job。开始整剧制作会自动创建。")
            return

        job_options = {str(_value(job, "id")): job for job in jobs}
        selected_job_id = st.selectbox(
            "选择 Production Job",
            list(job_options),
            index=list(job_options).index(str(_value(selected_job, "id"))) if selected_job is not None else 0,
            key=f"production-job-select-{project.id}",
            format_func=lambda job_id: f"{_status_value(job_options[job_id])} · {job_id[:10]}",
        )
        diagnostic_job = job_options[selected_job_id]
        _render_job_row(diagnostic_job, production_service.validate_job_readiness(project.id, _value(diagnostic_job, "shot_plan_revision_id")))
        st.caption(f"ProductionJob ID · {diagnostic_job.id}")
        try:
            executions = execution_service.list_executions(project.id, diagnostic_job.id)
        except ProductionExecutionServiceError as exc:
            st.warning(str(exc))
            executions = []
        if not executions:
            st.info("暂无 execution。")
        else:
            execution_options = {str(_value(item, "id")): item for item in executions}
            selected_execution_id = st.selectbox(
                "选择 Execution",
                list(execution_options),
                key=f"production-execution-select-{diagnostic_job.id}",
                format_func=lambda execution_id: f"{_status_value(execution_options[execution_id])} · {execution_id[:10]}",
            )
            st.caption(f"Execution ID · {selected_execution_id}")
            if st.button("Refresh execution", key=f"refresh-execution-{selected_execution_id}"):
                st.rerun()
            try:
                _render_execution_detail(execution_service, project, execution_options[selected_execution_id], qc_service)
            except ProductionExecutionServiceError as exc:
                st.error(str(exc))
