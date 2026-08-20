from html import escape

import streamlit as st

from aidrama_studio.domain import ProjectStatus


STATUS_LABELS = {
    ProjectStatus.DRAFT: "草稿",
    ProjectStatus.STORY: "剧本中",
    ProjectStatus.PREPRODUCTION: "前期制作",
    ProjectStatus.PRODUCTION: "制作中",
    ProjectStatus.REVIEW: "审核中",
    ProjectStatus.POSTPRODUCTION: "后期制作",
    ProjectStatus.COMPLETED: "已完成",
}


def status_label(status: ProjectStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def status_badge(status: ProjectStatus) -> None:
    css_status = status.value.lower()
    st.markdown(
        f'<span class="aidrama-status aidrama-status-{escape(css_status)}">'
        f"{escape(status_label(status))}</span>",
        unsafe_allow_html=True,
    )
