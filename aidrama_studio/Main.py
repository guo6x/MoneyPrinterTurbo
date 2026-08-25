from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from loguru import logger


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aidrama_studio.components.navigation import build_navigation  # noqa: E402
from aidrama_studio.branding import BRAND  # noqa: E402
from aidrama_studio.storage.database import get_default_paths, initialize_database  # noqa: E402
from aidrama_studio.services.security import configure_runtime_logging  # noqa: E402


st.set_page_config(
    page_title=BRAND.product_name,
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": f"{BRAND.product_name}\n\n{BRAND.tagline}\n\nMIT License"},
)


def _load_styles() -> None:
    style_path = Path(__file__).with_name("styles.css")
    st.markdown(
        f"<style>{style_path.read_text(encoding='utf-8')}</style>",
        unsafe_allow_html=True,
    )


def main() -> None:
    configure_runtime_logging(get_default_paths().root)
    _load_styles()
    st.session_state.setdefault("current_project_id", None)
    if not st.session_state.get("current_project_id"):
        project_from_url = st.query_params.get("project")
        if project_from_url:
            st.session_state.current_project_id = project_from_url
    try:
        initialize_database()
    except Exception:
        logger.exception("failed to initialize AIDrama Studio storage")
        st.error("AIDrama Studio 无法初始化本地存储，请检查目录写入权限。")
        st.stop()

    with st.sidebar:
        st.markdown(
            f'<div class="aidrama-brand">{BRAND.product_name}</div>', unsafe_allow_html=True
        )
        st.caption(BRAND.tagline)

    navigation = build_navigation()
    navigation.run()


if __name__ == "__main__":
    main()
