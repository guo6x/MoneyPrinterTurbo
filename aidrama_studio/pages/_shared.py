from __future__ import annotations

import streamlit as st
from loguru import logger

from aidrama_studio.components.empty_state import empty_state
from aidrama_studio.domain import ProjectStatus
from aidrama_studio.services import ProjectService


STAGE_LABELS = {
    ProjectStatus.DRAFT: "创意输入",
    ProjectStatus.STORY: "故事与剧本",
    ProjectStatus.PREPRODUCTION: "分镜",
    ProjectStatus.PRODUCTION: "制作",
    ProjectStatus.REVIEW: "审片",
    ProjectStatus.POSTPRODUCTION: "成片",
    ProjectStatus.COMPLETED: "已完成",
}

STAGE_NEXT = {
    ProjectStatus.DRAFT: ("开始创作", "story"),
    ProjectStatus.STORY: ("确认故事并进入剧本", "story"),
    ProjectStatus.PREPRODUCTION: ("检查分镜", "director"),
    ProjectStatus.PRODUCTION: ("查看制作进度", "production"),
    ProjectStatus.REVIEW: ("完成审片", "review"),
    ProjectStatus.POSTPRODUCTION: ("生成最终成片", "postproduction"),
    ProjectStatus.COMPLETED: ("播放成片", "postproduction"),
}


def get_project_service() -> ProjectService:
    return ProjectService()


def _navigate(page_key: str) -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation(page_key)


def project_stage(project) -> str:
    """Return a human-readable stage without exposing enum values."""
    return STAGE_LABELS.get(getattr(project, "status", None), "创意输入")


def render_project_context(project, *, stage: str | None = None, next_action: str | None = None, next_page: str | None = None) -> None:
    """Persistent product shell context shared by every project page."""
    stage = stage or project_stage(project)
    default_action, default_page = STAGE_NEXT.get(getattr(project, "status", None), ("继续创作", "story"))
    next_action = next_action or default_action
    next_page = next_page or default_page
    st.markdown(
        f'<div class="aidrama-workspace-context"><div><span class="aidrama-context-kicker">当前项目</span>'
        f'<strong>{project.title}</strong></div><div><span class="aidrama-context-kicker">当前阶段</span>'
        f'<strong>{stage}</strong></div></div>',
        unsafe_allow_html=True,
    )
    action_col, status_col = st.columns([2, 5])
    with action_col:
        if st.button(next_action, type="primary", key=f"workspace-next-{getattr(project, 'id', 'project')}-{next_page}", use_container_width=True):
            _navigate(next_page)
    with status_col:
        st.caption("下一步 · " + next_action)


def render_ai_readiness(*, project_id: str | None = None, compact: bool = False) -> None:
    """Human-readable capability status; provider internals stay advanced-only."""
    try:
        from aidrama_studio.services.provider_readiness import ProviderReadinessService

        readiness = ProviderReadinessService().snapshot(project_id=project_id)
    except Exception:
        readiness = {}
    labels = (("text", "文本生成"), ("image", "参考图生成"), ("video", "视频生成"), ("vision", "画面分析"), ("tts", "配音"))
    missing = []
    with st.container(border=True):
        st.markdown("### AI 能力状态")
        cols = st.columns(len(labels))
        for col, (key, label) in zip(cols, labels):
            capability_key = {"text": "LLM", "image": "IMAGE", "video": "VIDEO_GENERATIVE", "vision": "VISION", "tts": "TTS"}[key]
            value = readiness.get(capability_key, {}) if isinstance(readiness, dict) else {}
            if isinstance(value, dict):
                raw_state = value.get("state")
                # Older/embedded readiness providers may expose only the
                # boolean ``ready`` field.  Preserve that contract while
                # keeping unknown or malformed payloads fail-closed.
                if raw_state:
                    state = str(raw_state).upper()
                    ready_value = value.get("ready")
                    # A contradictory payload must never render a green
                    # normal-user status.  ``state=READY`` without the
                    # canonical boolean proof is treated as a configuration
                    # error; the same applies to any known state claiming the
                    # opposite readiness value.
                    if state == "READY" and ready_value is not True:
                        state = "ERROR"
                    elif state == "UNAVAILABLE" and ready_value is True:
                        state = "ERROR"
                    elif state == "ERROR" and ready_value is True:
                        state = "ERROR"
                    elif state not in {"READY", "UNAVAILABLE", "ERROR"}:
                        state = "ERROR"
                elif value.get("ready") is True:
                    state = "READY"
                elif value.get("ready") is False:
                    state = "UNAVAILABLE"
                else:
                    state = "UNAVAILABLE"
            else:
                state = "UNAVAILABLE"
            display_state = {
                "READY": "已配置",
                "ERROR": "配置有误",
                "UNAVAILABLE": "需要配置",
            }.get(state, "配置有误")
            col.metric(label, display_state)
            if state != "READY":
                missing.append(label)
        if missing:
            st.info("AI 功能尚未配置。你仍可先编辑内容；需要生成时再配置对应模型。")
            if st.button("去设置模型", type="primary", key=f"readiness-settings-{project_id or 'global'}", use_container_width=not compact):
                _navigate("settings")
        with st.expander("高级诊断", expanded=False):
            st.caption("仅供排障使用；普通创作不需要理解 Provider、端点或运行计划。")
            if readiness:
                st.json(readiness)


def render_actionable_blockers(blockers: list[str] | tuple[str, ...] | None, *, project_id: str | None = None) -> None:
    """Translate machine gates into direct, human actions."""
    blockers = [str(item) for item in (blockers or [])]
    if not blockers:
        return
    normalized = " ".join(blockers).lower()
    items: list[tuple[str, str, str]] = []
    if "story" in normalized or "故事" in normalized:
        items.append(("确认故事设定", "先确认故事设定，后续角色与剧本才能保持一致。", "story"))
    if "script" in normalized or "剧本" in normalized:
        items.append(("确认剧本", "确认结构化剧本后，才能进入分镜。", "story"))
    if "shot" in normalized or "分镜" in normalized:
        items.append(("确认分镜", "确认镜头顺序和时长后，才能开始制作。", "director"))
    if "reference" in normalized or "asset" in normalized or "参考" in normalized:
        items.append(("补齐参考图", "为主要角色和场景选择或上传参考图。", "assets"))
    if not items:
        items.append(("查看当前准备项", "完成准备清单后即可继续。", "story"))
    with st.container(border=True):
        st.markdown("### 当前还不能继续")
        st.caption("还需要完成：")
        for title, detail, page in items:
            left, right = st.columns([4, 1])
            left.markdown(f"○ **{title}**")
            left.caption(detail)
            if right.button("去处理", key=f"blocker-{project_id or 'project'}-{page}-{title}", use_container_width=True):
                _navigate(page)


def current_project_or_stop():
    project_id = st.session_state.get("current_project_id")
    if not project_id:
        empty_state(
            "未选择项目",
            "请选择最近项目继续，或创建一个新项目。",
            label="WORKSPACE / 需要项目",
        )
        back_col, create_col = st.columns(2)
        if back_col.button("返回工作台选择项目", type="primary", use_container_width=True):
            _navigate("dashboard")
        if create_col.button("创建新项目", use_container_width=True):
            _navigate("dashboard")
        try:
            recent = get_project_service().list()[:3]
        except Exception:
            recent = []
        if recent:
            st.markdown("#### 最近项目")
            for item in recent:
                if st.button(f"继续 · {item.title}", key=f"recover-{item.id}", use_container_width=True):
                    st.session_state.current_project_id = item.id
                    st.query_params["project"] = item.id
                    st.rerun()
        st.stop()
    # Keep deep links/reloads project-scoped even when Streamlit's navigation
    # component rewrites the path without carrying query parameters.
    if st.query_params.get("project") != project_id:
        st.query_params["project"] = project_id
    try:
        project = get_project_service().get(project_id)
    except Exception:
        logger.exception("failed to load current AIDrama project")
        st.error("项目读取失败，请返回工作台后重试。")
        st.stop()
    if project is None:
        st.session_state.current_project_id = None
        st.warning("当前项目已经不存在，请重新选择项目。")
        if st.button("返回工作台", type="primary"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("dashboard")
        st.stop()
    return project


def coming_soon(title: str, description: str, future_items: list[str]) -> None:
    project = current_project_or_stop()
    st.caption(f"当前项目 · {project.title}")
    empty_state(title, description, label="COMING SOON / 当前阶段尚未开始")
    st.markdown("#### 后续将在这里完成")
    for item in future_items:
        st.markdown(f"- {item}")
