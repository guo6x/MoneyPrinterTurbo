"""Minimal durable AUTO Mode control surface."""

from __future__ import annotations

from html import escape

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import AutoRunStatus
from aidrama_studio.pages._shared import current_project_or_stop, render_project_context
from aidrama_studio.services import AutoOrchestratorError, AutoOrchestratorService


_STAGE_LABELS = {
    "CREATIVE": "创意",
    "STORY": "故事",
    "SCRIPT": "剧本",
    "SHOT_PLAN": "分镜方案",
    "REFERENCES": "角色与场景参考",
    "PRODUCTION": "制作",
    "QC": "技术检查",
    "REVIEW": "人工审片",
    "FINAL": "最终合成",
    "COMPLETED": "已完成",
}

_HUMAN_ROUTES = {
    "APPROVE_STORY": ("确认故事", "story"),
    "APPROVE_SCRIPT": ("确认剧本", "story"),
    "APPROVE_SHOT_PLAN": ("确认分镜方案", "director"),
    "PROMOTE_BIND_AND_LOCK_REFERENCE": ("确认并锁定参考图", "assets"),
    "BIND_AND_LOCK_REFERENCE": ("绑定并锁定参考图", "assets"),
    "APPROVE_OR_REJECT_PRODUCTION_REVIEW": ("前往人工审片", "review"),
    "INSPECT_FAILURE_AND_RESUME": ("检查失败原因", "settings"),
    "INSPECT_BLOCKER": ("检查阻塞项", "production"),
}


def _navigate(page_key: str) -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation(page_key)


def _stage_label(value: object) -> str:
    raw = str(getattr(value, "value", value))
    return _STAGE_LABELS.get(raw, raw)


def _run(service: AutoOrchestratorService, project_id: str, *, one_step: bool) -> None:
    try:
        state = service.step(project_id) if one_step else service.resume(project_id)
    except Exception:
        st.error("AUTO Mode 未能执行当前步骤。详细原因已保留在本地诊断中。")
        return
    if state.status is AutoRunStatus.FAILED:
        st.error("当前步骤失败，请检查阻塞原因后再继续。")
    else:
        st.rerun()


def _render_paid_gate(
    service: AutoOrchestratorService, project_id: str, decision
) -> None:
    st.warning("此步骤可能创建付费 Provider 任务。AUTO Mode 不会默认授权或消费预算。")
    try:
        preview = service.preview_paid_authorization(project_id)
    except AutoOrchestratorError:
        st.error("无法生成精确授权预览；没有发起 Provider 请求。")
        return
    left, middle, right = st.columns(3)
    left.metric("全局最大 create", preview.required_create_count)
    middle.metric("每项最大 create", preview.per_item_max)
    right.metric("自动重试", preview.retry_limit)
    st.caption(f"运行目标 · {preview.provider_label}")
    confirmed = st.checkbox(
        "我确认仅授权以上精确输入和上限",
        value=False,
        key=f"auto-paid-confirm-{project_id}-{preview.authorization_fingerprint}",
    )
    if st.button(
        "授权有界付费 create",
        type="primary",
        disabled=not confirmed,
        key=f"auto-paid-grant-{project_id}",
        use_container_width=True,
    ):
        try:
            service.grant_paid_authorization(
                project_id,
                authorization_fingerprint=preview.authorization_fingerprint,
                global_max=preview.required_create_count,
                per_item_max=1,
                retry_limit=0,
            )
        except AutoOrchestratorError:
            st.error("授权预览已失效，请刷新后重新确认。没有发起 Provider 请求。")
            return
        st.success("有界授权已持久化；尚未发起 Provider create。")
        st.rerun()


def _render_human_gate(decision) -> None:
    requested = str(decision.requested_action or "")
    label, page = _HUMAN_ROUTES.get(requested, ("处理人工操作", "dashboard"))
    st.warning(decision.why)
    if st.button(label, type="primary", use_container_width=True):
        _navigate(page)
    with st.expander("恢复状态", expanded=False):
        st.caption("完成正式人工操作后返回本页，AUTO Mode 将从 SQLite 中的产品状态继续。")
        st.code(decision.resume_token or "首次落盘后生成 resume token", language=None)


def render() -> None:
    project = current_project_or_stop()
    page_header(
        "自动制作",
        "AUTO MODE / PRODUCT AGENT",
        "根据当前项目真值选择下一项正式服务；人工门禁与付费授权不会被越过。",
        stage="AUTO",
    )
    render_project_context(project, suppress_next=True)

    service = AutoOrchestratorService(drive_background=True)
    persisted = service.get_state(project.id)
    decision = service.next_action(project.id)
    display = (
        persisted
        if persisted is not None
        and persisted.input_state_hash == decision.input_state_hash
        else decision
    )

    cols = st.columns(3)
    cols[0].metric("当前阶段", _stage_label(decision.current_stage))
    cols[1].metric("AUTO 状态", decision.status.value)
    cols[2].metric("下一步", decision.next_action.value)

    with st.container(border=True):
        st.markdown("### 正在做什么")
        st.write(decision.why)
        if decision.blocking_reason:
            st.markdown("### 为什么停住")
            st.code(decision.blocking_reason, language=None)
        st.markdown("### 已完成阶段")
        if decision.completed_stages:
            st.write(" → ".join(_stage_label(item) for item in decision.completed_stages))
        else:
            st.caption("尚无已完成阶段")

    if decision.requires_paid_authorization:
        _render_paid_gate(service, project.id, decision)
    elif decision.requires_human:
        _render_human_gate(display)
    elif decision.status is AutoRunStatus.WAITING_PROVIDER:
        if st.button("轮询现有任务", type="primary", use_container_width=True):
            _run(service, project.id, one_step=True)
    elif decision.status is AutoRunStatus.SUCCEEDED:
        st.success("AUTO 制作流程已完成。")
    elif decision.status is AutoRunStatus.CANCELLED:
        st.info("AUTO Mode 已取消。正式项目内容保持不变。")
    else:
        if st.button("开始 / 继续自动制作", type="primary", use_container_width=True):
            _run(service, project.id, one_step=False)

    if decision.status not in {
        AutoRunStatus.SUCCEEDED,
        AutoRunStatus.CANCELLED,
    }:
        if st.button("取消 AUTO Mode", type="secondary", use_container_width=True):
            service.cancel(project.id, reason="user_cancelled_from_auto_ui")
            st.rerun()

    events = service.list_events(project.id)
    with st.expander("Agent 决策记录", expanded=False):
        if not events:
            st.caption("AUTO Mode 尚未执行。")
        for event in reversed(events[-20:]):
            st.markdown(
                f"**{escape(event.action)}** · {escape(event.result)}  \n"
                f"{escape(event.reason)}  \n"
                f"{escape(event.timestamp)} · {escape(event.actor)}"
            )


__all__ = ["render"]
