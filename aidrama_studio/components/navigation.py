from __future__ import annotations

import streamlit as st

from aidrama_studio.branding import BRAND
from aidrama_studio.pages import (
    assets,
    dashboard,
    director,
    postproduction,
    production,
    review,
    settings,
    story,
)


PAGE_DEFINITIONS = (
    ("dashboard", "工作台", "dashboard", dashboard.render),
    ("story", "创意与剧本", "story", story.render),
    ("assets", "角色与场景", "assets", assets.render),
    ("director", "分镜", "director", director.render),
    ("production", "制作", "production", production.render),
    ("review", "审片", "review", review.render),
    ("postproduction", "成片", "postproduction", postproduction.render),
    ("settings", "设置", "settings", settings.render),
)


def build_navigation():
    pages = {
        key: st.Page(render, title=title, url_path=url_path)
        for key, title, url_path, render in PAGE_DEFINITIONS
    }
    st.session_state["_aidrama_pages"] = pages
    navigation = st.navigation(
        {
            BRAND.product_name: [
                pages["dashboard"],
                pages["story"],
                pages["assets"],
                pages["director"],
                pages["production"],
                pages["review"],
                pages["postproduction"],
            ],
            "工具": [pages["settings"]],
        },
        position="sidebar",
    )
    requested = st.session_state.pop("_aidrama_next_page", None)
    if requested in pages:
        project_id = st.session_state.get("current_project_id")
        if project_id:
            # Keep the selected project in the URL while switching pages so a
            # browser refresh/cold Streamlit session reconstructs the same
            # project instead of silently returning to NO PROJECT.
            st.query_params["project"] = project_id
        st.switch_page(
            pages[requested],
            query_params={"project": project_id} if project_id else None,
        )
    return navigation


def request_navigation(page_key: str) -> None:
    project_id = st.session_state.get("current_project_id")
    if project_id:
        st.query_params["project"] = project_id
    st.session_state["_aidrama_next_page"] = page_key
    st.rerun()


def set_current_project(project_id: str | None) -> None:
    st.session_state.current_project_id = project_id
