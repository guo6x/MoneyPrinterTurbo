from __future__ import annotations

import importlib

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.services import (
    CredentialReadinessService,
    CredentialStoreError,
    DiagnosticsService,
    DiskSpaceService,
    OutputProfileService,
    ProjectService,
    WindowsCredentialStore,
)
from aidrama_studio.storage import get_default_paths
from aidrama_studio.pages._shared import (
    normalize_capability_snapshots,
    render_capability_cards,
    render_project_context,
)


CORE_MODULES = (
    "app.services.task",
    "app.services.video",
    "app.services.llm",
    "app.services.material",
    "app.services.voice",
)

def _human_capability_state(public: dict[str, object]) -> str:
    """Map either legacy or neutral readiness data to human status labels.

    The legacy provider-profile helper below is retained for old integrations,
    but the normal Settings page passes the provider-neutral dimensions from
    ``CapabilitySnapshot``.  Supporting both shapes keeps existing AppTest
    fixtures useful while the universal runtime bridge rolls out.
    """

    raw_state = str(public.get("state") or "").upper()
    runtime_available = public.get("runtime_available", public.get("available"))
    # Neutral snapshots may already expose the final human state names.
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
    # A configured-but-unavailable profile can be missing paid authorization,
    # runtime support, or another local prerequisite.  Unless its detail is an
    # explicit configuration error, direct the user to configuration rather
    # than claiming the capability is ready.
    error_markers = (
        "invalid",
        "error",
        "failed",
        "mismatch",
        "无效",
        "错误",
        "失败",
        "不匹配",
    )
    detail = str(public.get("detail") or "").casefold()
    if raw_state == "ERROR" or any(marker in detail for marker in error_markers):
        return "配置有误"
    if raw_state:
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
        # Unknown/contradictory state values are diagnostic errors, never a
        # green normal-user readiness claim.
        return "配置有误"
    if (
        bool(public.get("configured"))
        and bool(public.get("verified", True))
        and bool(runtime_available)
    ):
        return "已配置"
    return "需要配置"


def _render_provider_model_settings(
    selection_service: object | None = None,
    *,
    project_id: str | None = None,
) -> None:
    """Compatibility seam; normal Settings never invokes the legacy editor."""

    from aidrama_studio.pages._settings_legacy import render_provider_model_settings

    render_provider_model_settings(selection_service, project_id=project_id)


_OUTPUT_RESOLUTION_OPTIONS = ("720p", "1080p", "1440p", "4K")
_OUTPUT_FPS_OPTIONS = (24.0, 25.0, 30.0, 50.0, 60.0)
_OUTPUT_ASPECT_OPTIONS = ("16:9", "9:16", "1:1", "4:3")
_OUTPUT_QUALITY_OPTIONS = ("PREVIEW", "STANDARD", "HIGH", "FINAL")
_OUTPUT_QUALITY_LABELS = {
    "PREVIEW": "预览",
    "STANDARD": "标准",
    "HIGH": "高质量",
    "FINAL": "最终交付",
}


def _snapshot_public(snapshot: object) -> dict[str, object]:
    """Read the neutral capability contract without assuming a concrete class."""

    if isinstance(snapshot, dict):
        return dict(snapshot)
    as_public = getattr(snapshot, "as_public_dict", None)
    if callable(as_public):
        try:
            value = as_public()
            if isinstance(value, dict):
                return dict(value)
        except Exception:
            pass
    fields = (
        "capability",
        "model_or_profile",
        "configured",
        "verified",
        "runtime_available",
        "create_authorized",
        "authorization_required",
        "safe_reason",
        "state",
    )
    return {key: getattr(snapshot, key, None) for key in fields}


def _render_capability_workspace(project_id: str | None = None) -> tuple[object, ...]:
    """Render capability status from the provider-neutral runtime projection."""

    try:
        snapshots = normalize_capability_snapshots(project_id=project_id)
    except Exception as exc:
        logger.warning("capability projection unavailable: {}", exc)
        snapshots = ()
    st.subheader("能力状态")
    st.caption("状态来自本地运行时声明；查看状态不会发起生成或付费请求。")
    normalized = render_capability_cards(
        snapshots,
        project_id=project_id,
        compact=False,
        show_diagnostics=False,
    )
    with st.expander("能力诊断（高级）", expanded=False):
        st.caption("仅显示安全的能力摘要；凭据、端点和任务标识不会出现在日常设置里。")
        st.json([_snapshot_public(item) for item in normalized])
    return normalized


def _render_model_scheme(snapshots: tuple[object, ...], project_id: str | None = None) -> None:
    """Show the current model/profile choices without provider inventory UI."""

    st.subheader("模型方案")
    st.caption("模型与配置由通用运行时声明；已提交的制作版本保持原冻结选择。")
    if not snapshots:
        st.info("暂时无法读取模型方案；完成运行时初始化后可再次查看。")
        return
    for snapshot in snapshots:
        public = _snapshot_public(snapshot)
        capability = str(public.get("capability") or "能力").upper()
        label = {
            "LLM": "文本生成",
            "IMAGE": "参考图生成",
            "VIDEO": "视频生成",
            "VISION": "画面分析",
            "TTS": "配音",
        }.get(capability, capability)
        model = public.get("model_or_profile") or "尚未选择"
        state = _human_capability_state(public)
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(f"当前方案 · {model} · {state}")
            if public.get("authorization_required") and not public.get("create_authorized"):
                st.caption("首次创建任务前需要明确确认；不会自动提交。")
    st.info("要更换模型方案，请先让运行时声明可用选项；这里不会猜测或自动跨区切换。")


def _credential_requirements() -> tuple[dict[str, str], ...]:
    """Return runtime-declared credential requirements, if supplied.

    Universal runtimes can install a list in session state while retaining
    ownership of credential identifiers.  With no declaration we show a safe
    summary rather than hard-coding provider-specific API keys in the page.
    """

    source = st.session_state.get("_aidrama_credential_requirements", ())
    if callable(source):
        try:
            source = source()
        except Exception:
            source = ()
    if isinstance(source, dict):
        source = tuple(source.values())
    if isinstance(source, (str, bytes)) or source is None:
        source = ()
    requirements: list[dict[str, str]] = []
    for index, item in enumerate(source or ()):
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("id") or "").strip()
            label = str(item.get("label") or item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
        else:
            key = str(getattr(item, "key", getattr(item, "id", "")) or "").strip()
            label = str(getattr(item, "label", getattr(item, "name", "")) or "").strip()
            description = str(getattr(item, "description", "") or "").strip()
        if not key:
            continue
        requirements.append({
            "key": key,
            "label": label or f"安全连接 {index + 1}",
            "description": description,
        })
    return tuple(requirements)


def _render_credentials(paths: object, snapshots: tuple[object, ...]) -> None:
    """Render generic secure credentials/connections, driven by declarations."""

    st.subheader("凭据与连接")
    st.caption("凭据只保存在当前 Windows 用户的安全存储中；保存状态不会显示完整值，也不会自动发起请求。")
    requirements = _credential_requirements()
    try:
        store = WindowsCredentialStore(getattr(paths, "root", None))
    except CredentialStoreError:
        st.warning("安全凭据存储当前不可用；不会读取或显示已有凭据。")
        return
    except Exception:
        st.warning("安全凭据存储当前不可用；不会读取或显示已有凭据。")
        return

    if not requirements:
        try:
            configured_count = len(store.configured_providers())
        except Exception:
            configured_count = 0
        if configured_count:
            st.success(f"已保存 {configured_count} 项安全连接（具体名称由运行时管理）。")
        else:
            st.info("当前运行时尚未声明需要配置的连接。需要生成时，系统会提示缺少哪项能力。")
        with st.expander("连接诊断（高级）", expanded=False):
            st.caption("运行时尚未提供可编辑的连接声明；不会在这里猜测供应商或 API 字段。")
        return

    keys = [item["key"] for item in requirements]
    try:
        status = CredentialReadinessService(store).status(keys)
    except Exception:
        st.warning("暂时无法读取连接状态；不会自动发起请求。")
        return
    for item in requirements:
        key = item["key"]
        configured = bool(status.get(key, {}).get("configured"))
        with st.container(border=True):
            st.markdown(f"**{item['label']}**")
            st.caption("已配置" if configured else "需要配置")
            if item.get("description"):
                st.caption(item["description"][:180])
            with st.form(
                f"runtime-credential-form-{key}", clear_on_submit=True
            ):
                secret = st.text_input(
                    "安全凭据",
                    type="password",
                    key=f"runtime-credential-{key}",
                    help="保存后输入框会清空；完整值不会再次显示。",
                )
                save = st.form_submit_button("安全保存", type="primary")
            if save:
                if not secret:
                    st.warning("请输入安全凭据后再保存。")
                    continue
                try:
                    store.set(key, secret)
                    st.success("连接已安全保存。")
                    st.rerun()
                except Exception:
                    st.warning("连接保存失败；原有连接状态保持不变。")
            if st.button(
                "移除连接",
                key=f"runtime-credential-remove-{key}",
                disabled=not configured,
            ):
                try:
                    store.delete(key)
                    st.success("连接已移除。")
                    st.rerun()
                except Exception:
                    st.warning("连接移除失败；原有连接状态保持不变。")


def _project_aspect(project: object | None) -> str:
    value = getattr(project, "aspect_ratio", "16:9") if project is not None else "16:9"
    return str(getattr(value, "value", value) or "16:9")


def _render_output_defaults(project_id: str | None, project: object | None) -> None:
    """Edit future output defaults through the versioned profile service."""

    st.subheader("默认输出")
    st.caption("分辨率、帧率、画幅和质量只影响之后新建的制作版本；已冻结的成片不会改变。")
    profile = None
    profile_service = None
    if project_id:
        try:
            profile_service = OutputProfileService()
            profile = profile_service.current(project_id)
        except Exception as exc:
            logger.debug("output profile unavailable: {}", exc)
    aspect_default = str(getattr(profile, "aspect_ratio", None) or _project_aspect(project))
    if aspect_default not in _OUTPUT_ASPECT_OPTIONS:
        aspect_default = _project_aspect(project)
    resolution_default = str(getattr(profile, "delivery_resolution_label", None) or "1080p")
    if resolution_default not in _OUTPUT_RESOLUTION_OPTIONS:
        resolution_default = "1080p"
    fps_default = float(getattr(profile, "target_fps", None) or 24.0)
    if fps_default not in _OUTPUT_FPS_OPTIONS:
        fps_default = 24.0
    quality_default = str(getattr(profile, "quality_mode", None) or "STANDARD").upper()
    if quality_default not in _OUTPUT_QUALITY_OPTIONS:
        quality_default = "STANDARD"
    duration_default = float(
        getattr(profile, "target_episode_duration_seconds", None)
        or getattr(project, "target_duration_seconds", None)
        or 60
    )
    codec_default = str(getattr(profile, "target_video_codec", None) or "h264").lower()
    audio_rate_default = int(getattr(profile, "target_audio_sample_rate", None) or 48000)
    audio_channels_default = int(getattr(profile, "target_audio_channels", None) or 2)

    with st.form(f"output-defaults-{project_id or 'global'}"):
        first, second = st.columns(2)
        with first:
            aspect = st.selectbox(
                "画幅",
                list(_OUTPUT_ASPECT_OPTIONS),
                index=list(_OUTPUT_ASPECT_OPTIONS).index(aspect_default),
                key=f"output-aspect-{project_id or 'global'}",
            )
            resolution = st.selectbox(
                "交付分辨率",
                list(_OUTPUT_RESOLUTION_OPTIONS),
                index=list(_OUTPUT_RESOLUTION_OPTIONS).index(resolution_default),
                key=f"output-resolution-{project_id or 'global'}",
            )
            duration = st.number_input(
                "目标时长（秒）",
                min_value=1.0,
                max_value=3600.0,
                value=max(1.0, min(3600.0, duration_default)),
                step=1.0,
                key=f"output-duration-{project_id or 'global'}",
            )
        with second:
            fps = st.selectbox(
                "帧率",
                list(_OUTPUT_FPS_OPTIONS),
                index=list(_OUTPUT_FPS_OPTIONS).index(fps_default),
                key=f"output-fps-{project_id or 'global'}",
            )
            quality = st.selectbox(
                "质量",
                list(_OUTPUT_QUALITY_OPTIONS),
                index=list(_OUTPUT_QUALITY_OPTIONS).index(quality_default),
                format_func=lambda value: _OUTPUT_QUALITY_LABELS.get(value, value),
                key=f"output-quality-{project_id or 'global'}",
            )
            codec = st.selectbox(
                "视频格式",
                ["h264", "hevc"],
                index=0 if codec_default not in {"h264", "hevc"} else ["h264", "hevc"].index(codec_default),
                key=f"output-codec-{project_id or 'global'}",
            )
            audio = st.selectbox(
                "音频",
                ["立体声 · 48 kHz", "单声道 · 48 kHz"],
                index=0 if audio_channels_default != 1 else 1,
                key=f"output-audio-{project_id or 'global'}",
            )
        submitted = st.form_submit_button("保存默认输出", type="primary")
    if not submitted:
        return
    channels = 1 if str(audio).startswith("单声道") else 2
    if not project_id or profile_service is None:
        st.session_state["_aidrama_output_defaults"] = {
            "aspect_ratio": aspect,
            "resolution": resolution,
            "duration": float(duration),
            "fps": float(fps),
            "quality": quality,
            "codec": codec,
            "audio_channels": channels,
        }
        st.success("默认输出已保存到当前工作区；选择项目后会写入项目版本。")
        return
    try:
        profile_service.create(
            project_id,
            aspect_ratio=str(aspect),
            target_episode_duration_seconds=float(duration),
            delivery_resolution_label=str(resolution),
            target_fps=float(fps),
            target_video_codec=str(codec),
            target_audio_sample_rate=audio_rate_default,
            target_audio_channels=channels,
            quality_mode=str(quality),
            make_project_default=True,
        )
    except Exception:
        st.warning("默认输出保存失败；已有制作版本不会受到影响。")
    else:
        st.success("默认输出已保存；只影响之后新建的制作版本。")
        st.rerun()


def _render_archive_workspace(service: object | None, projects: list[object]) -> None:
    """Compatibility bridge for the archive workspace moved from Workbench."""

    try:
        from aidrama_studio.pages.dashboard import _render_archive_workspace as legacy_workspace

        legacy_workspace(service, projects)
    except Exception as exc:
        logger.warning("archive workspace unavailable: {}", exc)
        st.info("项目归档服务暂不可用；现有项目不会受到影响。")


def _render_storage_backup(project_id: str | None, project: object | None, paths: object) -> None:
    st.subheader("存储 / 备份")
    st.caption("项目文件与备份保留在本机；导出会生成可验证的项目归档，不包含凭据。")
    try:
        disk = DiskSpaceService().usage()
    except Exception:
        disk = {}
    used = float(disk.get("used_bytes", 0) or 0) if isinstance(disk, dict) else 0.0
    free = float(disk.get("free_bytes", 0) or 0) if isinstance(disk, dict) else 0.0
    storage_columns = st.columns(2)
    storage_columns[0].metric("项目数据占用", f"{used / (1024 ** 2):.1f} MB")
    storage_columns[1].metric("本机可用空间", f"{free / (1024 ** 3):.1f} GB")
    try:
        service = ProjectService()
        projects = list(service.list() or [])
    except Exception:
        service, projects = None, []
    _render_archive_workspace(service, projects)
    try:
        from aidrama_studio.pages.dashboard import _render_recovery_notice

        _render_recovery_notice()
    except Exception:
        pass


def _render_diagnostics(paths: object, ready: bool, detail: str) -> None:
    """Keep technical paths, integrity checks, and cleanup in one disclosure."""

    with st.expander("高级诊断", expanded=False):
        st.markdown("### 本地媒体能力")
        st.success("可用") if ready else st.warning("不可用")
        st.caption(detail)
        with st.expander("存储与模块详情", expanded=False):
            root = getattr(paths, "root", None)
            if root:
                st.caption(f"数据目录 · {root}")
            st.caption("以下信息仅用于排障，不参与创作决策。")
            if st.button("重新扫描诊断", key="settings-diagnostics-rescan"):
                try:
                    report = DiagnosticsService().scan()
                except Exception as exc:
                    st.warning(f"诊断扫描失败：{type(exc).__name__}")
                else:
                    st.json(report)
            if st.button("清理安全临时文件", key="settings-diagnostics-clean-temp"):
                try:
                    removed = DiagnosticsService().cleanup_safe_temporary_files()
                    st.success(f"已清理 {len(removed)} 个安全临时文件；未删除项目成片。")
                except Exception as exc:
                    st.warning(f"清理失败：{type(exc).__name__}")


def check_media_engine() -> tuple[bool, str]:
    try:
        for module in CORE_MODULES:
            importlib.import_module(module)
    except Exception as exc:
        logger.exception("AIDrama media engine import health failed")
        return False, f"核心模块加载失败：{type(exc).__name__}"
    return True, "核心媒体模块已就绪"


def render() -> None:
    page_header("设置", "SETUP", "配置模型能力、输出默认值与本地存储；高级诊断不会打扰日常创作。")
    project_id = st.session_state.get("current_project_id")
    project = None
    if project_id:
        try:
            project = ProjectService().get(project_id)
        except Exception:
            project = None
        if project is not None:
            # Settings is a utility destination.  Keep only a quiet return
            # affordance in the shell; it must not masquerade as a workflow
            # stage or compete with a settings action.
            render_project_context(
                project,
                next_action="返回创作",
                next_page="story",
                quiet=True,
            )

    paths = get_default_paths()
    ready, detail = check_media_engine()
    snapshots = _render_capability_workspace(project_id)
    _render_model_scheme(snapshots, project_id)
    _render_credentials(paths, snapshots)
    _render_output_defaults(project_id, project)
    _render_storage_backup(project_id, project, paths)
    _render_diagnostics(paths, ready, detail)
