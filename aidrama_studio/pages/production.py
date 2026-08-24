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
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionOrchestrator,
    ProductionOrchestratorError,
    ProductionQCService,
    ProductionQCServiceError,
    ProductionService,
    ProductionServiceError,
)
from aidrama_studio.services.adapters import MPTProductionAdapter


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


def _safe_failure_reason(value: object, default: str = "runtime execution failed") -> str:
    """Keep operator-facing errors concise and never render a traceback."""
    text = str(value or default).replace("\r", " ").replace("\n", " ").strip()
    if "Traceback (most recent call last)" in text:
        text = text.split("Traceback (most recent call last)", 1)[0].strip() or default
    return text[:300]


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

    reasons = _readiness_reasons(readiness)
    if ready:
        st.success("制作准备已完成，可以开始整剧制作。")
    else:
        st.warning("Production 尚未就绪。请先完成以下前置条件：")
        for reason in reasons or ["请完成 Story Bible、Structured Script、Shot Plan 与参考资产准备"]:
            st.markdown(f"- {reason}")
        missing = [reason for reason in reasons if "reference" in reason.lower() or "参考" in reason]
        if missing:
            st.info("参考资产缺失时，请前往「创意与剧本 → Reference Assets」补齐人物或场景覆盖。")


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


def _render_shot_board(
    production_service: ProductionService,
    execution_service: ProductionExecutionService,
    qc_service: ProductionQCService,
    project,
    job,
    progress: Mapping[str, object],
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
    st.caption(f"当前镜头 · {current_shot_id or '—'}")
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
        label = f"镜头 {_value(shot, 'order_index', '—')} · {_value(entry, 'scene_name', _value(entry, 'scene_id', '—'))}"
        with st.container(border=True):
            if highlighted:
                st.markdown(f"#### ▶ {label}")
                st.info("当前镜头")
            else:
                st.markdown(f"#### {label}")
            description = str(_value(entry, "description", "") or "").strip()
            if description:
                st.caption(description[:240])
            cols = st.columns(3)
            cols[0].markdown(f"制作：**{_display_status(execution_status if execution is not None else shot_status, _SHOT_STATUS_LABELS)}**")
            cols[1].markdown(f"QC：**{_display_status(qc_status, _QC_STATUS_LABELS)}**")
            cols[2].markdown(f"人审：**{_review_status(reviews) if review_status == 'PENDING' else ('已拒绝' if review_status == 'REJECTED' else '已接受')}**")
            if execution_status == ProductionExecutionStatus.FAILED.value:
                reason = "runtime failed"
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
                    st.caption("失败指标：" + ", ".join(map(str, failed_metrics)))
                if st.button("查看 QC 详情", key=f"view-qc-{shot_id}"):
                    st.session_state[f"production-advanced-{_value(job, 'id')}"] = True
            if review_status == "REJECTED":
                st.warning("人审拒绝，后续镜头已阻塞；请修订后创建新的 attempt。")
    return shown_executions, {"executions": executions, "by_shot": by_shot}


def _make_orchestrator(production_service, execution_service, qc_service):
    return ProductionOrchestrator(
        production_service=production_service,
        execution_service=execution_service,
        qc_service=qc_service,
        adapter=MPTProductionAdapter(),
    )


class _UnavailableOrchestrator:
    """Small fallback used by isolated page tests with service fakes."""

    def get_job_progress(self, project_id, job_id):
        return {"total_shots": 0, "completed_shots": 0, "failed_shots": 0, "pending_shots": 0, "percent_complete": 0, "current_shot_id": None}

    def run_job(self, *args, **kwargs):
        raise ProductionOrchestratorError("Production orchestrator is unavailable")

    resume_job = run_job
    cancel_job = run_job


def _orchestrator_action(orchestrator, action: str, project, job=None, *, ensure_job=None) -> None:
    try:
        if action == "start":
            if job is None:
                job = ensure_job() if callable(ensure_job) else None
            if job is None:
                raise ProductionOrchestratorError("无法创建 ProductionJob")
            orchestrator.run_job(project.id, _value(job, "id"))
        elif action == "resume":
            if job is None:
                raise ProductionOrchestratorError("继续制作需要一个已存在的 ProductionJob")
            orchestrator.resume_job(project.id, _value(job, "id"))
        elif action == "cancel":
            if job is None:
                raise ProductionOrchestratorError("停止制作需要一个正在运行的 ProductionJob")
            orchestrator.cancel_job(project.id, _value(job, "id"), reason="user")
    except (ProductionOrchestratorError, ProductionServiceError, ProductionExecutionServiceError, NotImplementedError) as exc:
        st.error(f"Production action unavailable: {exc}")
        return
    st.rerun()


def _render_primary_action(orchestrator, production_service, project, job, readiness, progress, *, ensure_job=None) -> None:
    status = _job_status(job) if job is not None else ("READY" if readiness.get("ready") else "DRAFT")
    ready = bool(readiness.get("ready"))
    if not ready:
        st.button("开始整剧制作", type="primary", disabled=True, key=f"start-blocked-{project.id}")
        return
    if status in {"QUEUED", "RUNNING"}:
        cols = st.columns(2)
        if cols[0].button("刷新制作状态", key=f"refresh-job-{_value(job, 'id', project.id)}"):
            try:
                progress = orchestrator.get_job_progress(project.id, _value(job, "id"))
            except Exception:
                pass
            st.rerun()
        if cols[1].button("停止制作", key=f"cancel-job-{_value(job, 'id', project.id)}"):
            _orchestrator_action(orchestrator, "cancel", project, job)
        return
    if status == "SUCCEEDED":
        st.success("制作完成")
        return
    if status == "FAILED":
        st.error("制作失败，当前不会自动重新生成或重试。请查看失败镜头与高级信息。")
        return
    label = "继续制作" if status == "CANCELLED" or int(progress.get("completed_shots", 0) or 0) > 0 else "开始整剧制作"
    if st.button(label, type="primary", key=f"primary-production-{_value(job, 'id', project.id)}"):
        _orchestrator_action(
            orchestrator,
            "resume" if label == "继续制作" else "start",
            project,
            job,
            ensure_job=ensure_job,
        )


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


def _render_empty_shot_board(readiness: Mapping[str, object]) -> None:
    st.markdown("### 镜头生产")
    st.caption(f"总镜头 · {int(readiness.get('shot_count') or 0)} · 制作开始后将显示每个镜头的进度、QC 与人审状态。")


def render() -> None:
    page_header("整剧制作", "DIRECTOR PRODUCTION CONSOLE", "从制作准备到镜头生产、QC 与完成的多镜头工作台。")
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

    # Legacy label retained for discoverability: Production Readiness Check.
    _render_readiness_console(readiness)

    selected_job = _select_default_job(project.id, jobs)
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
        st.warning(f"制作进度暂不可用：{exc}")
        progress = {"total_shots": job_readiness.get("shot_count", 0), "completed_shots": 0, "failed_shots": 0, "pending_shots": job_readiness.get("shot_count", 0), "percent_complete": 0, "current_shot_id": None}

    # Orchestration remains canonical; the page only supplies an existing job
    # or asks ProductionService to create one at the moment of the CTA.
    orchestrator = _make_orchestrator(production_service, execution_service, qc_service) if hasattr(production_service, "repository") else _UnavailableOrchestrator()

    def ensure_job():
        if selected_job is not None:
            return selected_job
        return production_service.create_production_job(project.id)

    _render_primary_action(orchestrator, production_service, project, selected_job, job_readiness, progress, ensure_job=ensure_job)
    if selected_job is not None:
        try:
            _render_shot_board(production_service, execution_service, qc_service, project, selected_job, progress)
        except (ProductionServiceError, ProductionExecutionServiceError) as exc:
            st.warning(f"镜头生产暂不可用：{exc}")
    else:
        _render_empty_shot_board(job_readiness)

    # Keep low-level IDs, events and artifact metadata available without
    # overwhelming the default director view.
    advanced_key = f"production-advanced-{project.id}"
    with st.expander("高级信息 / 调试信息", expanded=bool(st.session_state.get(advanced_key))):
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
