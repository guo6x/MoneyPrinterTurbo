from __future__ import annotations

import importlib

import streamlit as st
from loguru import logger

from aidrama_studio.branding import BRAND
from aidrama_studio.components.page_header import page_header
from aidrama_studio.services.provider_readiness import ProviderReadinessService, ReadinessState
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
        logger.exception("AIDrama media engine import health failed")
        return False, f"核心模块加载失败：{type(exc).__name__}"
    return True, "核心媒体模块已就绪"


def render() -> None:
    page_header("设置", "SYSTEM", "查看产品能力、Provider 就绪状态与本地存储。")
    paths = get_default_paths()
    ready, detail = check_media_engine()

    info_col, engine_col = st.columns(2)
    with info_col:
        with st.container(border=True):
            st.markdown("### 产品信息")
            st.markdown(f"**{BRAND.product_name}**")
            st.caption(f"{BRAND.tagline} · v{BRAND.version}")
            st.markdown("MIT License")
            st.caption("上游组件归属与许可证详见仓库 NOTICE。")
    with engine_col:
        with st.container(border=True):
            st.markdown("### 本地媒体内核")
            if ready:
                st.success("READY")
            else:
                st.error("ERROR")
            st.caption(detail)

    with st.container(border=True):
        st.markdown("### Provider readiness")
        st.caption("仅显示能力状态，不显示 API Key、Token 或其它 secret。")
        readiness = ProviderReadinessService().list_capabilities()
        columns = st.columns(len(readiness))
        for column, capability in zip(columns, readiness):
            with column:
                if capability.state is ReadinessState.READY:
                    st.success(capability.capability)
                elif capability.state is ReadinessState.ERROR:
                    st.error(capability.capability)
                else:
                    st.warning(capability.capability)
                st.caption(capability.provider)
                st.caption(capability.detail)

    with st.container(border=True):
        st.markdown("### 本地存储")
        st.caption("项目数据使用本机 SQLite；Redis 不作为项目 canonical DB。")
        with st.expander("高级信息 / 调试信息"):
            st.text_input("SQLite 数据库", value=str(paths.database), disabled=True)
            st.text_input("项目存储目录", value=str(paths.projects), disabled=True)
