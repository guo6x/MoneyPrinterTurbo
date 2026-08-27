"""Compatibility-only provider profile editor.

This module is intentionally not imported by the normal Settings render path.
It exists for older integrations and AppTest fixtures that still call the
pre-universal-runtime provider-profile editor directly.  New Settings UI must
consume the neutral capability projection instead.
"""

from __future__ import annotations

from collections.abc import Mapping

import streamlit as st

from aidrama_studio.domain import ProviderPreset
from aidrama_studio.services.ai_capabilities import CapabilityKind, default_capability_registry
from aidrama_studio.services.provider_profiles import ProviderProfileError, ProviderProfileService


_CAPABILITY_LABELS = {
    CapabilityKind.LLM: "文本生成",
    CapabilityKind.IMAGE: "参考图生成",
    CapabilityKind.VIDEO_GENERATIVE: "视频生成",
    CapabilityKind.VISION: "画面分析",
    CapabilityKind.TTS: "配音",
}

_PRESET_LABELS = {
    ProviderPreset.MAINLAND: "中国大陆",
    ProviderPreset.INTERNATIONAL: "国际",
    ProviderPreset.CUSTOM: "自定义",
}


def _profile_label(profile: object) -> str:
    region = getattr(getattr(profile, "deployment_region", None), "value", "")
    return (
        f"{getattr(profile, 'provider_id', '—')} / "
        f"{getattr(profile, 'model_id', '—')} · {region} · "
        f"{getattr(profile, 'endpoint_class', '—')}"
    )


def _human_state(public: Mapping[str, object]) -> str:
    raw_state = str(public.get("state") or "").upper()
    runtime_available = public.get("runtime_available", public.get("available"))
    if raw_state.casefold() in {
        "ready",
        "needs_setup",
        "needs_verification",
        "unavailable",
        "needs_confirmation",
        "error",
    }:
        return {
            "ready": "已配置",
            "needs_setup": "需要配置",
            "needs_verification": "待验证",
            "unavailable": "运行不可用",
            "needs_confirmation": "需要确认",
            "error": "配置有误",
        }[raw_state.casefold()]
    detail = str(public.get("detail") or "").casefold()
    if raw_state == "ERROR" or any(
        marker in detail
        for marker in ("invalid", "error", "failed", "mismatch", "无效", "错误", "失败", "不匹配")
    ):
        return "配置有误"
    if raw_state == "READY":
        return (
            "已配置"
            if public.get("configured") is True
            and public.get("verified", True) is True
            and runtime_available is True
            else "配置有误"
        )
    if raw_state in {"UNAVAILABLE", "CONFIGURED"}:
        return "需要配置"
    if raw_state:
        return "配置有误"
    return (
        "已配置"
        if public.get("configured")
        and public.get("verified", True)
        and runtime_available
        else "需要配置"
    )


def render_provider_model_settings(
    selection_service: ProviderProfileService | None = None,
    *,
    project_id: str | None = None,
) -> None:
    """Render the pre-V1 provider editor for compatibility callers only."""

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
                    "未配置" if not item else _profile_label(choices[item])
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
    st.caption("配置状态和验证状态分开显示；未主动验证不会发起网络请求。")
    for capability, label in _CAPABILITY_LABELS.items():
        try:
            resolution = service.resolve(scope_project_id, capability)
        except ProviderProfileError as exc:
            st.warning(f"{label} · 未配置")
            with st.expander("高级诊断", expanded=False):
                st.caption(str(exc))
            continue
        public = resolution.as_public_dict()
        with st.container(border=True):
            st.markdown(f"**{label}**")
            state = _human_state(public)
            verified = "已验证" if public["verified"] else "未验证"
            st.caption(f"{state} · {verified}")
            profile = resolution.profile
            with st.expander("高级模型信息"):
                st.caption(f"Provider · {public['provider_id']} / {public['model_id']}")
                if public["deployment_region"]:
                    st.caption(
                        f"区域：{public['deployment_region']} · Endpoint：{public['endpoint_class']}"
                    )
                st.caption(f"状态 · {public['state']} · {public['detail']}")
                if profile is not None:
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

    with st.expander("高级信息 · 模型版本边界", expanded=False):
        st.info("更改模型方案只影响新 RuntimePlan；已排队、已提交、运行中和历史执行保持原冻结选择。")


__all__ = ["render_provider_model_settings"]
