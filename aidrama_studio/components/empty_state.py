from html import escape

import streamlit as st


def empty_state(
    title: str,
    description: str,
    label: str = "当前阶段尚未开始",
    *,
    tone: str = "neutral",
) -> None:
    """Render a calm, human empty state; actions remain owned by the caller."""
    safe_tone = escape(str(tone).strip().lower() or "neutral")
    st.markdown(
        f"""
        <div class="aidrama-empty-state aidrama-empty-{safe_tone}">
          <div class="aidrama-empty-label">{escape(label)}</div>
          <h3>{escape(title)}</h3>
          <p>{escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
