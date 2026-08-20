from __future__ import annotations

import importlib

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.storage import get_default_paths


CORE_MODULES = (
    "app.services.task",
    "app.services.video",
    "app.services.llm",
    "app.services.material",
    "app.services.voice",
)


def check_media_engine() -> tuple[bool, str]:
    try:
        for module in CORE_MODULES:
            importlib.import_module(module)
    except Exception as exc:
        logger.exception("MoneyPrinterTurbo media engine import health failed")
        return False, f"核心模块加载失败：{type(exc).__name__}"
    return True, "核心媒体模块已就绪"


def render() -> None:
    page_header("设置", "SYSTEM", "查看产品、存储与媒体执行内核状态。")
    paths = get_default_paths()
    ready, detail = check_media_engine()

    info_col, engine_col = st.columns(2)
    with info_col:
        with st.container(border=True):
            st.markdown("### 产品信息")
            st.markdown("**AIDrama Studio**")
            st.caption("AI 短剧全链路制作工作台")
            st.markdown("Built on MoneyPrinterTurbo  ")
            st.markdown("MIT License")
    with engine_col:
        with st.container(border=True):
            st.markdown("### Media Engine")
            if ready:
                st.success("READY")
            else:
                st.error("ERROR")
            st.caption(detail)

    with st.container(border=True):
        st.markdown("### 本地存储")
        st.text_input("SQLite 数据库", value=str(paths.database), disabled=True)
        st.text_input("项目存储目录", value=str(paths.projects), disabled=True)
        st.caption("项目数据使用本机 SQLite；Redis 不作为项目 canonical DB。")
