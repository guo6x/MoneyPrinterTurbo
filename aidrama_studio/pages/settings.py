from __future__ import annotations

import importlib

import streamlit as st
from loguru import logger

from aidrama_studio.branding import BRAND
from aidrama_studio.components.page_header import page_header
from aidrama_studio.services.provider_readiness import ProviderReadinessService, ReadinessState
from aidrama_studio.services import CredentialReadinessService, CredentialStoreError, DiagnosticsService, DiskSpaceService, WindowsCredentialStore
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
        st.markdown("### Provider 安全配置")
        st.caption("凭据使用当前 Windows 用户的 DPAPI 加密；不会写入 SQLite、项目包、日志或截图。配置凭据不会自动发起付费请求。")
        try:
            store = WindowsCredentialStore(paths.root)
            configured = CredentialReadinessService(store).status(["OPENAI_API_KEY", "ARK_API_KEY", "DASHSCOPE_API_KEY"])
            labels = {
                "OPENAI_API_KEY": "OpenAI Image",
                "ARK_API_KEY": "Seedance / Ark",
                "DASHSCOPE_API_KEY": "Wan / DashScope",
            }
            for provider_id, label in labels.items():
                state = "已配置" if configured[provider_id]["configured"] else "未配置"
                with st.expander(f"{label} · {state}"):
                    secret = st.text_input(f"{label} API Key", type="password", key=f"credential-{provider_id}", help="保存后输入框会清空；完整值不会再次显示。")
                    save, remove = st.columns(2)
                    if save.button("安全保存", key=f"save-{provider_id}", disabled=not bool(secret)):
                        store.set(provider_id, secret)
                        st.success("已使用 Windows DPAPI 保存。")
                        st.rerun()
                    if remove.button("移除凭据", key=f"delete-{provider_id}", disabled=not configured[provider_id]["configured"]):
                        store.delete(provider_id)
                        st.success("凭据已移除。")
                        st.rerun()
        except CredentialStoreError as exc:
            st.warning(f"Windows 安全凭据存储当前不可用：{exc}")

    with st.container(border=True):
        st.markdown("### 本地存储")
        st.caption("项目数据使用本机 SQLite；Redis 不作为项目 canonical DB。")
        disk = DiskSpaceService().usage()
        storage_columns = st.columns(2)
        storage_columns[0].metric("AIDrama 数据", f"{int(disk['used_bytes'] or 0) / (1024 ** 2):.1f} MB")
        storage_columns[1].metric("磁盘可用", f"{int(disk['free_bytes'] or 0) / (1024 ** 3):.1f} GB")
        with st.expander("高级信息 / 调试信息"):
            st.text_input("SQLite 数据库", value="<AIDramaData>/aidrama.db", disabled=True)
            st.text_input("项目存储目录", value="<AIDramaData>/projects/", disabled=True)
            if st.button("重新扫描诊断", key="diagnostics-rescan"):
                report = DiagnosticsService().scan()
                if report["sqlite_integrity"] == "ok":
                    st.success("SQLite integrity: OK")
                else:
                    st.error("SQLite integrity check failed")
                st.json({"schema_version": report["schema_version"], "disk": report["disk"], "projects": report["projects"]})
            if st.button("清理安全临时文件", key="diagnostics-clean-temp"):
                removed = DiagnosticsService().cleanup_safe_temporary_files()
                st.success(f"已清理 {len(removed)} 个安全临时文件；未删除 canonical media。")
