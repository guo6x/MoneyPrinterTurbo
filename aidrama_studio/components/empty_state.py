import streamlit as st


def empty_state(title: str, description: str, label: str = "当前阶段尚未开始") -> None:
    st.markdown(
        f"""
        <div class="aidrama-empty-state">
          <div class="aidrama-empty-label">{label}</div>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
