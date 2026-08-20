import streamlit as st


def page_header(title: str, eyebrow: str, description: str) -> None:
    st.markdown(
        f"""
        <section class="aidrama-page-header">
          <div class="aidrama-eyebrow">{eyebrow}</div>
          <h1>{title}</h1>
          <p>{description}</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
