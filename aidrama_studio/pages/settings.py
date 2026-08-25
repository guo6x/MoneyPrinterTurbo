from __future__ import annotations

import importlib

import streamlit as st
from loguru import logger

from aidrama_studio.branding import BRAND
from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import ProviderPreset
from aidrama_studio.services import CredentialReadinessService, CredentialStoreError, DiagnosticsService, DiskSpaceService, WindowsCredentialStore
from aidrama_studio.services.ai_capabilities import CapabilityKind, default_capability_registry
from aidrama_studio.services.provider_profiles import ProviderProfileError, ProviderProfileService
from aidrama_studio.storage import get_default_paths


CORE_MODULES = (
    "app.services.task",
    "app.services.video",
    "app.services.llm",
    "app.services.material",
    "app.services.voice",
)

_CAPABILITY_LABELS = {
    CapabilityKind.LLM: "LLM",
    CapabilityKind.IMAGE: "图片生成",
    CapabilityKind.VIDEO_GENERATIVE: "视频生成",
    CapabilityKind.VISION: "视觉理解",
    CapabilityKind.TTS: "语音",
}

_PRESET_LABELS = {
    ProviderPreset.MAINLAND: "中国大陆",
    ProviderPreset.INTERNATIONAL: "国际",
    ProviderPreset.CUSTOM: "自定义",
}


def _profile_label(profile) -> str:
    region = profile.deployment_region.value
    return f"{profile.provider_id} / {profile.model_id} · {region} · {profile.endpoint_class}"


def _render_provider_model_settings(
    selection_service: ProviderProfileService | None = None,
    *,
    project_id: str | None = None,
) -> None:
    service = selection_service or ProviderProfileService(
        registry=default_capability_registry()
    )
    scope_options = ["全局默认"]
    if project_id:
        scope_options.append("当前项目默认")
    scope_label = st.radio(
        "设置作用域",
        scope_options,
        horizontal=True,
        key="provider-selection-scope",
    )
    scope_project_id = project_id if scope_label == "当前项目默认" else None
    current = service.get_settings(scope_project_id)
    current_preset = current.preset if current else ProviderPreset.CUSTOM
    preset_labels = list(_PRESET_LABELS.values())
    selected_label = st.radio(
        "模型方案",
        preset_labels,
        index=preset_labels.index(_PRESET_LABELS[current_preset]),
        horizontal=True,
        key=f"provider-preset-{scope_project_id or 'global'}",
    )
    selected_preset = next(
        preset for preset, label in _PRESET_LABELS.items() if label == selected_label
    )

    selections = dict(current.selections) if current else {}
    if selected_preset is ProviderPreset.CUSTOM:
        st.caption("每种能力独立选择；未选择的能力保持 UNAVAILABLE，不会自动换区。")
        for capability, label in _CAPABILITY_LABELS.items():
            try:
                profiles = service.inventory(scope_project_id, capability)
            except ProviderProfileError as exc:
                st.warning(str(exc))
                profiles = ()
            option_ids = [""] + [profile.id for profile in profiles]
            by_id = {profile.id: profile for profile in profiles}
            current_id = selections.get(capability.value, "")
            index = option_ids.index(current_id) if current_id in option_ids else 0
            selected_id = st.selectbox(
                label,
                option_ids,
                index=index,
                key=f"provider-custom-{scope_project_id or 'global'}-{capability.value}",
                format_func=lambda item, choices=by_id: (
                    "UNAVAILABLE" if not item else _profile_label(choices[item])
                ),
            )
            if selected_id:
                selections[capability.value] = selected_id
            else:
                selections.pop(capability.value, None)

    if st.button(
        "保存模型方案",
        type="primary",
        key=f"save-provider-selection-{scope_project_id or 'global'}",
    ):
        try:
            service.save_settings(
                project_id=scope_project_id,
                preset=selected_preset,
                selections=selections,
            )
        except ProviderProfileError as exc:
            st.error(str(exc))
        else:
            st.success("模型方案已保存；只影响新建 RuntimePlan。")
            st.rerun()

    st.markdown("#### 当前解析结果")
    st.caption("configured 与 verified 独立展示；未进行 live 验证不会标记为已验证。")
    for capability, label in _CAPABILITY_LABELS.items():
        try:
            resolution = service.resolve(scope_project_id, capability)
        except ProviderProfileError as exc:
            st.warning(f"{label} · UNAVAILABLE · {exc}")
            continue
        public = resolution.as_public_dict()
        with st.container(border=True):
            st.markdown(f"**{label}** · {public['provider_id']} / {public['model_id']}")
            state = "已配置" if public["configured"] else "未配置"
            verified = "已验证" if public["verified"] else "未验证"
            st.caption(f"{state} · {verified} · {public['state']}")
            if public["deployment_region"]:
                st.caption(
                    f"区域：{public['deployment_region']} · Endpoint：{public['endpoint_class']}"
                )
            if public["state"] == "UNAVAILABLE":
                st.warning(str(public["detail"]))
            profile = resolution.profile
            if profile is not None:
                with st.expander("高级模型信息"):
                    st.caption(f"Endpoint profile · {profile.endpoint_profile_id}")
                    st.caption(f"模型快照 · {profile.model_id}")
                    limits = {
                        key: profile.profile[key]
                        for key in (
                            "supported_durations",
                            "minimum_duration_seconds",
                            "maximum_duration_seconds",
                            "resolution",
                            "poll_interval_seconds",
                        )
                        if key in profile.profile
                    }
                    if limits:
                        st.json(limits)

    st.info("更改模型方案只影响新 RuntimePlan；已排队、已提交、运行中和历史执行保持原冻结选择。")


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
        st.markdown("### 模型方案")
        st.caption("中国大陆 / 国际 / 自定义均解析到同一 CapabilityRegistry 与 Provider inventory；不会创建第二套 Provider 真相。")
        _render_provider_model_settings(
            project_id=st.session_state.get("current_project_id")
        )

    with st.container(border=True):
        st.markdown("### Provider 安全配置")
        st.caption("凭据使用当前 Windows 用户的 DPAPI 加密；不会写入 SQLite、项目包、日志或截图。配置凭据不会自动发起付费请求。")
        try:
            store = WindowsCredentialStore(paths.root)
            configured = CredentialReadinessService(store).status(
                [
                    "OPENAI_API_KEY",
                    "ARK_API_KEY",
                    "DASHSCOPE_API_KEY",
                    "GEMINI_API_KEY",
                ]
            )
            labels = {
                "OPENAI_API_KEY": "OpenAI Image",
                "ARK_API_KEY": "Seedance / Ark",
                "DASHSCOPE_API_KEY": "Wan / DashScope",
                "GEMINI_API_KEY": "Google Gemini Vision",
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
                if report["sqlite_foreign_key_violations"]:
                    st.error(f"SQLite foreign-key violations: {len(report['sqlite_foreign_key_violations'])}")
                else:
                    st.success("SQLite foreign keys: OK")
                st.caption("FFmpeg: " + ("READY" if report["ffmpeg_readiness"]["ready"] else "UNAVAILABLE"))
                st.json({"schema_version": report["schema_version"], "disk": report["disk"], "projects": report["projects"]})
            if st.button("清理安全临时文件", key="diagnostics-clean-temp"):
                removed = DiagnosticsService().cleanup_safe_temporary_files()
                st.success(f"已清理 {len(removed)} 个安全临时文件；未删除 canonical media。")
