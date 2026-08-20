from __future__ import annotations

from datetime import datetime

import streamlit as st

from aidrama_studio.domain import Project, ProjectStatus

from .status_badge import status_badge


PROGRESS = {
    ProjectStatus.DRAFT: 0.08,
    ProjectStatus.STORY: 0.2,
    ProjectStatus.PREPRODUCTION: 0.35,
    ProjectStatus.PRODUCTION: 0.6,
    ProjectStatus.REVIEW: 0.75,
    ProjectStatus.POSTPRODUCTION: 0.88,
    ProjectStatus.COMPLETED: 1.0,
}


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def project_card(project: Project) -> str | None:
    with st.container(border=True):
        st.markdown(
            '<div class="aidrama-cover">PROJECT FRAME</div>', unsafe_allow_html=True
        )
        left, right = st.columns([3, 1])
        with left:
            st.markdown(f"### {project.title}")
        with right:
            status_badge(project.status)
        description = project.description or "尚未添加项目描述。"
        st.caption(description)
        st.markdown(
            f"**{project.aspect_ratio.value}** · {project.target_duration_seconds} 秒  "
            f"\n更新于 {_display_time(project.updated_at)}"
        )
        st.progress(PROGRESS[project.status])
        open_col, edit_col, delete_col = st.columns(3)
        if open_col.button("打开", key=f"open-{project.id}", use_container_width=True):
            return "open"
        if edit_col.button("编辑", key=f"edit-{project.id}", use_container_width=True):
            return "edit"
        if delete_col.button(
            "删除", key=f"delete-{project.id}", use_container_width=True
        ):
            return "delete"
    return None
