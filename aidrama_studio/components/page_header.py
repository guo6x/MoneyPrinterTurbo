from html import escape

import streamlit as st


def page_header(
    title: str,
    eyebrow: str,
    description: str,
    *,
    stage: str | None = None,
    activity: str | None = None,
) -> None:
    """Render one product heading without introducing a duplicate titlebar."""
    stage_chip = (
        f'<span class="aidrama-page-stage" data-stage="{escape(stage)}">'
        f'{escape(stage)}</span>'
        if stage
        else ""
    )
    activity_chip = (
        f'<span class="aidrama-page-activity">{escape(activity)}</span>'
        if activity
        else ""
    )
    st.markdown(
        f"""
        <section class="aidrama-page-header">
          <div class="aidrama-header-meta"><div class="aidrama-eyebrow">{escape(eyebrow)}</div>{stage_chip}{activity_chip}</div>
          <h1>{escape(title)}</h1>
          <p>{escape(description)}</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
