from __future__ import annotations

import streamlit as st
from loguru import logger

from aidrama_studio.components.empty_state import empty_state
from aidrama_studio.services import ProjectService


def get_project_service() -> ProjectService:
    return ProjectService()


def current_project_or_stop():
    project_id = st.session_state.get("current_project_id")
    if not project_id:
        empty_state(
            "请先选择一个项目",
            "从工作台打开已有项目，或创建新的短剧项目后再进入这个阶段。",
            label="NO PROJECT",
        )
        if st.button("返回工作台", type="primary"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("dashboard")
        st.stop()
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
