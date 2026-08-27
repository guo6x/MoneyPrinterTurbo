from html import escape

import streamlit as st

from aidrama_studio.domain import ProjectStatus


STATUS_LABELS = {
    # Product-facing stage labels; compatibility enum values stay out of the
    # normal Workbench card and shell.
    ProjectStatus.DRAFT: "创意",
    ProjectStatus.STORY: "故事 / 剧本",
    ProjectStatus.PREPRODUCTION: "分镜",
    ProjectStatus.PRODUCTION: "制作",
    ProjectStatus.REVIEW: "审片",
    ProjectStatus.POSTPRODUCTION: "成片",
    ProjectStatus.COMPLETED: "成片",
}


def status_label(status: ProjectStatus | str | None) -> str:
    if status is None:
        return "暂不可用"
    if not isinstance(status, ProjectStatus):
        try:
            status = ProjectStatus(str(status).upper())
        except (TypeError, ValueError):
            # An unrecognised compatibility value must not be presented as a
            # real Creative stage.  Keeping it explicitly unavailable avoids
            # turning malformed/degraded state into a false progress signal.
            return "暂不可用"
    return STATUS_LABELS.get(status, "暂不可用")


def status_badge(status: ProjectStatus | str | None) -> None:
    if status is None:
        st.markdown(
            '<span class="aidrama-status aidrama-status-unknown">暂不可用</span>',
            unsafe_allow_html=True,
        )
        return
    try:
        status = status if isinstance(status, ProjectStatus) else ProjectStatus(str(status).upper())
    except (TypeError, ValueError):
        st.markdown(
            '<span class="aidrama-status aidrama-status-unknown">暂不可用</span>',
            unsafe_allow_html=True,
        )
        return
    css_status = status.value.lower()
    st.markdown(
        f'<span class="aidrama-status aidrama-status-{escape(css_status)}">'
        f"{escape(status_label(status))}</span>",
        unsafe_allow_html=True,
    )
