"""Product-facing deterministic QC and human review gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.pages._shared import render_project_context
from aidrama_studio.services import ProductionExecutionService, ProductionQCService, ProductionService


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _status(item: Any, key: str = "status") -> str:
    value = _value(item, key, "UNKNOWN")
    return str(getattr(value, "value", value)).upper()


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "—"
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized or ".." in normalized.split("/"):
        return "项目相对路径"
    return normalized


def _render_result(service: ProductionQCService, project: Any, result: Any) -> None:
    result_id = _value(result, "id", "")
    status = _status(result)
    summary = _value(result, "summary_json", {}) or {}
    with st.container(border=True):
        left, right = st.columns([3, 1])
        with left:
            shot_number = _value(result, "shot_number", _value(result, "shot_id", "—"))
            st.markdown(f"### Shot {shot_number} · 审片")
            st.caption("确定性检查已完成；请用画面和人审决定是否进入成片。")
            media_path = _value(result, "media_path", _value(result, "artifact_path"))
            if media_path:
                try:
                    st.video(str(media_path))
                except Exception:
                    st.caption("预览暂不可用")
            st.write(f"技术检查 {'通过' if status == 'QC_PASS' else '需要处理'} · 通过 {summary.get('passed', 0)} · 失败 {summary.get('failed', 0)}")
            report_path = _value(result, "report_path")
            if report_path:
                st.caption(f"报告：{_safe_path(report_path)}")
        with right:
            if st.button("重新生成", key=f"rerun-qc-{result_id}", type="primary"):
                try:
                    service.retry_qc(project.id, _value(result, "execution_id"), _value(result, "artifact_id"))
                    st.rerun()
                except Exception as exc:
                    st.error(f"QC 重试失败：{str(exc)[:180]}")

        metrics = service.list_metrics(project.id, result_id)
        with st.expander("技术检查明细", expanded=False):
            for metric in metrics:
                metric_status = _status(metric)
                icon = "✓" if metric_status == "PASS" else ("!" if metric_status == "FAIL" else "·")
                st.markdown(f"{icon} **{_value(metric, 'metric_name', 'metric')}** · {metric_status} · {_value(metric, 'message', '')}")

        reviews = service.list_reviews(project.id, result_id)
        latest = reviews[-1] if reviews else None
        if latest:
            st.caption(f"最近人审：{_status(latest, 'decision')} · {_value(latest, 'reviewer', 'system')} · {_value(latest, 'notes', '')}")
        if status == "QC_PASS":
            with st.form(f"review-form-{result_id}"):
                decision = st.selectbox("人工决定", ["APPROVED", "REJECTED"], key=f"review-decision-{result_id}")
                notes = st.text_area("备注", key=f"review-notes-{result_id}", height=70)
                if st.form_submit_button("通过 / 退回", type="primary"):
                    try:
                        service.create_review(project.id, result_id, decision, reviewer="AIDrama user", notes=notes)
                        st.success("Review 决定已追加保存")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Review 保存失败：{str(exc)[:180]}")


def render() -> None:
    page_header("审片", "REVIEW WORKSPACE", "用画面检查每个镜头，并明确通过或退回重做。")
    project = current_project_or_stop()
    render_project_context(project, stage="审片", next_action="完成审片", next_page="postproduction")
    qc_service = ProductionQCService()
    production_service = ProductionService(qc_service.repository)
    execution_service = ProductionExecutionService(qc_service.repository)
    st.caption(f"当前项目 · {project.title}")
    jobs = production_service.list_jobs(project.id)
    if not jobs:
        st.info("还没有可审片的制作结果。请先完成制作。")
        if st.button("去制作", type="primary", key=f"review-production-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation
            request_navigation("production")
        return
    job_options = [str(_value(job, "id")) for job in jobs]
    selected_job_id = st.selectbox(
        "选择制作结果", job_options,
        format_func=lambda value: f"制作结果 · {_status(next(job for job in jobs if _value(job, 'id') == value))}",
    )
    executions = execution_service.list_executions(project.id, selected_job_id)
    if not executions:
        st.info("该任务还没有 execution。")
        return
    execution_options = [str(_value(execution, "id")) for execution in executions]
    selected_execution_id = st.selectbox("选择镜头批次", execution_options, format_func=lambda value: "最近一次制作")
    selected_execution = next(execution for execution in executions if _value(execution, "id") == selected_execution_id)
    artifacts = execution_service.list_artifacts(project.id, selected_execution.id)
    if st.button("运行确定性 QC", type="primary", key=f"run-qc-{selected_execution.id}", disabled=not artifacts):
        try:
            qc_service.run_qc(project.id, selected_execution.id, artifacts[0].id if artifacts else None)
            st.rerun()
        except Exception as exc:
            st.error(f"QC 运行失败：{str(exc)[:180]}")
    if not artifacts:
        st.warning("该 execution 尚无 artifact，无法运行 QC。")
    results = qc_service.list_results(project.id, selected_execution.id)
    if not results:
        st.info("尚无 QC 历史。")
        return
    for result in reversed(results):
        _render_result(qc_service, project, result)


__all__ = ["render"]
