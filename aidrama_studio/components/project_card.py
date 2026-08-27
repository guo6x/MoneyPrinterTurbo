from __future__ import annotations

from datetime import datetime
from html import escape

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
_UNSET = object()


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "—")


def _canonical_status(project: Project, state_service=None) -> ProjectStatus | None:
    """Read the shared canonical stage, never the compatibility row status."""
    if state_service is None:
        try:
            from aidrama_studio.pages._shared import workflow_stage_projection

            return workflow_stage_projection(project).status
        except Exception:
            # Keep a recoverable card visible, but do not turn an unknown
            # canonical read into a false 创意 state.
            return None
    try:
        from aidrama_studio.pages._shared import workflow_stage_projection

        return workflow_stage_projection(project, state_service=state_service).status
    except Exception:
        return None


def project_card(
    project: Project,
    *,
    workflow_stage: ProjectStatus | str | None | object = _UNSET,
    state_service=None,
    primary: bool = False,
) -> str | None:
    if workflow_stage is _UNSET:
        display_status = _canonical_status(project, state_service)
    elif workflow_stage is None:
        display_status = None
    elif isinstance(workflow_stage, ProjectStatus):
        display_status = workflow_stage
    else:
        raw_stage = str(workflow_stage).strip()
        label_to_status = {
            "创意": ProjectStatus.DRAFT,
            "创意输入": ProjectStatus.DRAFT,
            "故事 / 剧本": ProjectStatus.STORY,
            "故事与剧本": ProjectStatus.STORY,
            "分镜": ProjectStatus.PREPRODUCTION,
            "制作": ProjectStatus.PRODUCTION,
            "审片": ProjectStatus.REVIEW,
            "成片": ProjectStatus.POSTPRODUCTION,
            "已完成": ProjectStatus.COMPLETED,
        }
        try:
            if raw_stage in label_to_status:
                display_status = label_to_status[raw_stage]
            else:
                display_status = ProjectStatus(raw_stage.upper())
        except ValueError:
            # Do not turn an unknown/degraded stage into a false Creative
            # state; the card should make the unavailable projection explicit.
            display_status = None
    with st.container(border=True):
        st.markdown(
            f'<article class="aidrama-project-card" data-stage="{display_status.value.lower() if display_status else "unknown"}">',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="aidrama-cover aidrama-cover-empty">AIDRAMA · SHORT DRAMA</div>', unsafe_allow_html=True
        )
        left, right = st.columns([3, 1])
        with left:
            st.markdown(f"### {escape(str(project.title or '未命名项目'))}")
        with right:
            status_badge(display_status)
        description = project.description or "尚未添加项目描述。"
        st.caption(escape(str(description)))
        st.markdown(
            f"**{escape(str(project.aspect_ratio.value))}** · {project.target_duration_seconds} 秒  "
            f"\n更新于 {escape(_display_time(project.updated_at))}"
        )
        if display_status is None:
            st.caption("当前阶段暂时无法读取")
        else:
            st.progress(PROGRESS.get(display_status, PROGRESS[ProjectStatus.DRAFT]))
        open_col, edit_col, delete_col = st.columns([2, 1, 1])
        if open_col.button(
            "继续创作",
            type="primary" if primary else "secondary",
            key=f"open-{project.id}",
            use_container_width=True,
        ):
            return "open"
        if edit_col.button("编辑项目", key=f"edit-{project.id}", use_container_width=True):
            return "edit"
        if delete_col.button(
            "删除", key=f"delete-{project.id}", use_container_width=True
        ):
            return "delete"
        st.markdown("</article>", unsafe_allow_html=True)
    return None
