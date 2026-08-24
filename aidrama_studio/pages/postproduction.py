"""Product-facing Final Assembly and MP4 export page.

The page is intentionally thin: readiness, manifest freezing, rendering,
output validation, and path security remain owned by the canonical services.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    FinalAssemblyRuntimeService,
    FinalAssemblyRuntimeServiceError,
    FinalAssemblyService,
    FinalAssemblyServiceError,
    ProductionService,
    PostProductionService,
    PostProductionServiceError,
    ProviderReadinessService,
)
from aidrama_studio.domain import AudioMixConfig


_ASSEMBLY_LABELS = {
    "DRAFT": "草稿",
    "READY": "已就绪",
    "ASSEMBLING": "生成中",
    "SUCCEEDED": "制作完成",
    "FAILED": "制作失败",
    "CANCELLED": "已停止",
}
_ATTEMPT_LABELS = {
    "PENDING": "等待生成",
    "RUNNING": "生成中",
    "SUCCEEDED": "制作完成",
    "FAILED": "制作失败",
    "CANCELLED": "已停止",
}


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _status_value(item: Any, key: str = "status", default: str = "UNKNOWN") -> str:
    value = _value(item, key, default)
    return str(getattr(value, "value", value)).strip().upper()


def _safe_error(value: object, default: str = "成片制作失败，请重试。") -> str:
    text = str(value or default).replace("\r", " ").replace("\n", " ").strip()
    if "Traceback (most recent call last)" in text:
        text = text.split("Traceback (most recent call last)", 1)[0].strip()
    if ":\\" in text or ":/" in text or "\\" in text:
        return default
    return text[:240] or default


def _format_duration(value: object) -> str:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if seconds <= 0:
        return "—"
    if seconds >= 3600:
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _format_timestamp(value: object) -> str:
    return str(value or "—").replace("T", " ").replace("+00:00", " UTC")[:32]


def _format_size(value: object) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _reason_label(reason: object) -> str:
    """Turn service readiness reasons into concise product language."""
    text = str(reason or "").strip()
    lower = text.lower()
    if "qc_pass" in lower or "qc result" in lower:
        return "镜头尚未通过 QC"
    if "source 文件不存在" in text or "source file" in lower:
        return "成片文件缺失"
    if "succeeded" in lower:
        return "镜头尚未完成生产"
    if "rejected" in lower:
        return "人审拒绝了该镜头"
    if ":" in text:
        return text.split(":", 1)[-1].strip()[:180]
    return text[:180] or "存在未满足的成片前置条件"


def _readiness_value(readiness: Any, key: str, default: object = 0) -> object:
    value = _value(readiness, key, default)
    return default if value is None else value


def _is_ready(readiness: Any) -> bool:
    value = _value(readiness, "ready", None)
    if value is not None:
        return bool(value)
    blocked = int(_readiness_value(readiness, "blocked_shots", 1) or 0)
    total = int(_readiness_value(readiness, "total_shots", 0) or 0)
    return total > 0 and blocked == 0


def _render_readiness(readiness: Any) -> None:
    """Render the default mental model: readiness before internal IDs."""
    total = int(_readiness_value(readiness, "total_shots", 0) or 0)
    eligible = int(_readiness_value(readiness, "eligible_shots", 0) or 0)
    blocked = int(_readiness_value(readiness, "blocked_shots", max(0, total - eligible)) or 0)
    expected = _readiness_value(readiness, "estimated_duration", 0)
    st.subheader("成片准备度")
    columns = st.columns(4)
    columns[0].metric("总镜头", total)
    columns[1].metric("可用镜头", eligible)
    columns[2].metric("阻塞镜头", blocked)
    columns[3].metric("预计时长", _format_duration(expected))
    if _is_ready(readiness):
        st.success("✓ 所有镜头均已有有效成片素材，QC 已通过，素材顺序已确认。")
    else:
        st.warning("成片尚未就绪")
        reasons = _value(readiness, "blocked_reasons", []) or []
        for reason in reasons:
            st.markdown(f"- {_reason_label(reason)}")
        if not reasons:
            st.caption("请先完成镜头生产与 QC。")


def _job_label(job: Any, readiness: Any) -> str:
    status = _status_value(job)
    total = int(_readiness_value(readiness, "total_shots", 0) or 0)
    return f"{_ASSEMBLY_LABELS.get(status, status)} · {total} 镜头"


def _select_job(production_service: ProductionService, project: Any) -> tuple[Any | None, Any | None, list[Any]]:
    try:
        jobs = list(production_service.list_jobs(project.id) or [])
    except Exception:
        return None, None, []
    if not jobs:
        return None, None, []
    readiness_by_job: dict[str, Any] = {}
    for job in jobs:
        try:
            readiness_by_job[str(_value(job, "id", ""))] = production_service.validate_job_readiness(
                project.id, _value(job, "shot_plan_revision_id", None)
            )
        except Exception:
            readiness_by_job[str(_value(job, "id", ""))] = {
                "total_shots": 0,
                "eligible_shots": 0,
                "blocked_shots": 1,
                "blocked_reasons": ["Production Job 尚未就绪"],
                "ready": False,
            }
    selected_key = f"postproduction-job-{project.id}"
    options = [str(_value(job, "id", "")) for job in jobs]
    current = st.session_state.get(selected_key)
    if current not in options:
        ready_job = next(
            (job for job in jobs if _is_ready(readiness_by_job[str(_value(job, "id", ""))])),
            jobs[-1],
        )
        current = str(_value(ready_job, "id", ""))
        st.session_state[selected_key] = current
    if len(jobs) > 1:
        selected = st.selectbox(
            "制作任务",
            options,
            index=options.index(current),
            format_func=lambda job_id: _job_label(
                next(job for job in jobs if str(_value(job, "id", "")) == job_id),
                readiness_by_job[job_id],
            ),
            key=f"postproduction-job-select-{project.id}",
        )
        st.session_state[selected_key] = selected
        current = selected
    selected_job = next((job for job in jobs if str(_value(job, "id", "")) == current), jobs[-1])
    return selected_job, readiness_by_job[str(_value(selected_job, "id", ""))], jobs


def _assembly_status(assembly: Any) -> str:
    return _status_value(assembly, default="DRAFT")


def _select_assembly(project: Any, assemblies: list[Any]) -> Any | None:
    """Select a human-readable成片版本 without exposing IDs by default."""
    if not assemblies:
        return None
    if len(assemblies) == 1:
        return assemblies[0]
    options = list(range(len(assemblies)))
    key = f"postproduction-assembly-{project.id}"
    current = st.session_state.get(key, len(options) - 1)
    if current not in options:
        current = len(options) - 1
    selected = st.selectbox(
        "成片版本",
        options,
        index=current,
        format_func=lambda index: (
            f"成片版本 {index + 1} · "
            f"{_ASSEMBLY_LABELS.get(_assembly_status(assemblies[index]), _assembly_status(assemblies[index]))}"
        ),
        key=key,
    )
    return assemblies[int(selected)]


def _render_action(
    project: Any,
    job: Any,
    readiness: Any,
    assembly: Any | None,
    manifest_service: FinalAssemblyService,
    runtime_service: FinalAssemblyRuntimeService,
) -> Any | None:
    ready = _is_ready(readiness)
    status = _assembly_status(assembly) if assembly is not None else None
    if assembly is None:
        if not ready:
            st.info("完成准备度中的阻塞项后，即可生成成片。")
            return None
        if st.button("生成成片", type="primary", key=f"generate-final-{project.id}"):
            try:
                with st.spinner("正在生成成片…"):
                    created = manifest_service.create_assembly(project.id, _value(job, "id"), freeze=True)
                    runtime_service.render(project.id, created.id)
                st.success("成片制作完成")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc))
        return None

    if status in {"READY", "FAILED", "CANCELLED"}:
        if status == "READY":
            st.info("成片素材已就绪，可以开始生成。")
        elif status == "FAILED":
            st.error("成片制作失败")
            attempts = runtime_service.list_attempts(project.id, assembly.id)
            failed = next((item for item in reversed(attempts) if _status_value(item) == "FAILED"), None)
            if failed is not None:
                st.caption(_safe_error(_value(failed, "error_message", "请重试。")))
        else:
            st.warning("成片生成已停止。")
        if st.button(
            "重新尝试生成" if status == "FAILED" else "生成成片",
            type="primary",
            key=f"render-final-{assembly.id}",
        ):
            try:
                with st.spinner("正在生成成片…"):
                    if status == "FAILED":
                        runtime_service.retry(project.id, assembly.id)
                    else:
                        runtime_service.render(project.id, assembly.id)
                st.success("成片制作完成")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc))
        return assembly
    if status == "ASSEMBLING":
        st.info("正在生成成片…")
    elif status == "SUCCEEDED":
        st.success("成片制作完成")
    return assembly


def _safe_download_name(title: object) -> str:
    text = "".join(char for char in str(title or "AIDrama") if char.isalnum() or char in {" ", "-", "_"}).strip()
    return f"{text or 'AIDrama'}-final.mp4"


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "—"
    normalized = value.strip().replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        normalized.startswith("/")
        or PureWindowsPath(value).drive
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return "[项目相对路径不可用]"
    return PurePosixPath(*parts).as_posix()


def _render_metadata(attempt: Any) -> None:
    metadata = _value(attempt, "metadata_json", {}) or {}
    columns = st.columns(5)
    columns[0].metric("时长", _format_duration(metadata.get("duration_seconds", metadata.get("duration"))))
    resolution = metadata.get("resolution") or (
        f"{metadata.get('width')} × {metadata.get('height')}"
        if metadata.get("width") and metadata.get("height") else "—"
    )
    columns[1].metric("分辨率", str(resolution).replace("x", " × "))
    columns[2].metric("编码", str(metadata.get("codec") or "—").upper())
    columns[3].metric("音频", "有" if metadata.get("audio_stream") else "无")
    columns[4].metric("文件大小", _format_size(metadata.get("size_bytes")))


def _render_preview_and_export(project: Any, assembly: Any, runtime_service: FinalAssemblyRuntimeService, attempts: list[Any]) -> None:
    successful = [item for item in attempts if _status_value(item) == "SUCCEEDED"]
    if not successful:
        return
    selected_key = f"postproduction-attempt-{assembly.id}"
    selected_id = st.session_state.get(selected_key) or _value(successful[-1], "id")
    selected = next((item for item in successful if _value(item, "id") == selected_id), successful[-1])
    try:
        output_path = runtime_service.resolve_output_path(project.id, assembly.id, _value(selected, "id"))
    except Exception as exc:
        st.warning(_safe_error(exc, "成片文件不可用"))
        return
    st.subheader("成片")
    if output_path is None:
        st.warning("成片文件不可用")
        return
    st.video(str(output_path))
    _render_metadata(selected)
    try:
        content = output_path.read_bytes()
    except OSError:
        st.warning("成片文件不可用")
        return
    st.download_button(
        "导出 MP4",
        data=content,
        file_name=_safe_download_name(project.title),
        mime="video/mp4",
        key=f"download-final-{assembly.id}-{_value(selected, 'id')}",
    )


def _render_history(project: Any, assembly: Any | None, runtime_service: FinalAssemblyRuntimeService) -> list[Any]:
    st.subheader("成片历史")
    if assembly is None:
        st.info("尚未生成成片版本。")
        return []
    try:
        attempts = runtime_service.list_attempts(project.id, assembly.id)
    except Exception as exc:
        st.warning(_safe_error(exc))
        return []
    if not attempts:
        st.info("成片版本已就绪，尚未开始生成。")
        return []
    for attempt in reversed(attempts):
        status = _status_value(attempt)
        label = _ATTEMPT_LABELS.get(status, status)
        with st.container(border=True):
            st.markdown(f"**成片版本 {_value(attempt, 'attempt_number', '—')}** · {label}")
            st.caption(_format_timestamp(_value(attempt, "finished_at", _value(attempt, "created_at"))))
            if status == "SUCCEEDED":
                metadata = _value(attempt, "metadata_json", {}) or {}
                st.caption(_format_duration(metadata.get("duration_seconds")) + " · " + str(metadata.get("resolution") or "—"))
                if st.button("查看此版本", key=f"view-final-attempt-{_value(attempt, 'id')}"):
                    st.session_state[f"postproduction-attempt-{assembly.id}"] = _value(attempt, "id")
                    st.rerun()
            elif status == "FAILED":
                st.caption(_safe_error(_value(attempt, "error_message", "请重试。")))
    return attempts


def _render_advanced(project: Any, assembly: Any | None, attempts: list[Any]) -> None:
    with st.expander("高级信息 / 调试信息", expanded=False):
        if assembly is None:
            st.caption("尚无 FinalAssembly manifest。")
            return
        st.caption(f"FinalAssembly ID · {_value(assembly, 'id', '—')}")
        st.caption(f"FinalAssembly status · {_status_value(assembly)}")
        st.caption(f"Manifest item count · {_value(assembly, 'item_count', '由 manifest 服务提供')}")
        for attempt in attempts:
            st.markdown(f"**RenderAttempt {_value(attempt, 'attempt_number', '—')}** · {_value(attempt, 'id', '—')}")
            st.caption(f"Adapter · {_value(attempt, 'adapter_name', '—')}")
            st.caption(f"Output · {_safe_relative_path(_value(attempt, 'output_relative_path'))}")
            metadata = _value(attempt, "metadata_json", {}) or {}
            if metadata.get("sha256"):
                st.caption(f"SHA-256 · {metadata['sha256']}")
            source_items = metadata.get("source_items") if isinstance(metadata, Mapping) else None
            if source_items:
                st.caption(f"Frozen source count · {len(source_items)}")


def _script_revision_id(repository: Any, job: Any) -> str | None:
    if repository is None or job is None:
        return None
    try:
        revision = repository.get_shot_revision(_value(job, "shot_plan_revision_id", ""))
        return str(revision["source_script_revision_id"]) if revision else None
    except Exception:
        return None


def _render_post_workspace(project: Any, job: Any, assembly: Any, repository: Any) -> None:
    """Thin post-production workspace backed exclusively by PostProductionService."""
    st.subheader("最终后期")
    st.caption("字幕、配音状态与本地 BGM 会进入新的 project-scoped PostProductionPlan；基础成片保持不可变。")
    if repository is None:
        st.info("后期服务尚未连接到项目存储。")
        return
    service = PostProductionService(repository=repository)
    plans = service.list_plans(project.id)
    plan = plans[-1] if plans else None
    if plan is None:
        if st.button("开始后期", type="primary", key=f"post-start-{project.id}-{assembly.id}"):
            try:
                plan = service.create_plan(project.id, assembly.id)
                st.success("后期计划已创建")
                st.rerun()
            except Exception as exc:
                st.warning(_safe_error(exc, "无法创建后期计划"))
        st.caption("后期计划会引用当前 FinalAssembly，不会覆盖基础成片。")
        return

    st.caption(f"PostProductionPlan · 已连接到当前成片版本")
    subtitle_revision_id = _script_revision_id(repository, job)
    subtitle_tracks = service.list_subtitle_tracks(project.id, plan.id)
    subtitle_track = subtitle_tracks[-1] if subtitle_tracks else None
    with st.container(border=True):
        st.markdown("### 字幕")
        if subtitle_track is None and subtitle_revision_id:
            if st.button("从结构化剧本生成字幕时间线", key=f"post-subtitles-{plan.id}"):
                try:
                    service.build_subtitle_timeline(project.id, subtitle_revision_id, plan_id=plan.id)
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "字幕时间线生成失败"))
        elif subtitle_track is None:
            st.caption("没有可用的结构化剧本修订版，暂时无法生成字幕。")
        else:
            enabled = st.checkbox("启用字幕（可在最终渲染前关闭）", value=bool(subtitle_track.enabled), key=f"subtitle-enabled-{subtitle_track.id}")
            cue_text = "\n".join(cue.text for cue in subtitle_track.cues)
            edited_text = st.text_area("字幕文本（每行一个 cue，时间轴保持来自剧本）", value=cue_text, key=f"subtitle-edit-{subtitle_track.id}", height=130)
            if st.button("保存字幕 Draft", key=f"subtitle-save-{subtitle_track.id}"):
                try:
                    lines = edited_text.splitlines()
                    cues = [cue.model_copy(update={"text": lines[index].strip()}) for index, cue in enumerate(subtitle_track.cues) if index < len(lines) and lines[index].strip()]
                    service.update_subtitle_track(project.id, subtitle_track.id, cues=cues, enabled=enabled)
                    service.update_plan(project.id, plan.id, subtitle_enabled=enabled)
                    st.success("字幕 Draft 已保存")
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "字幕保存失败"))
            srt = service.to_srt(subtitle_track)
            st.download_button("导出 SRT", data=srt, file_name=f"{project.title or 'aidrama'}.srt", mime="application/x-subrip", key=f"srt-download-{subtitle_track.id}")

    with st.container(border=True):
        st.markdown("### 配音 / TTS")
        tts = next((item for item in ProviderReadinessService().list_capabilities() if item.capability == "TTS"), None)
        if tts and tts.ready:
            st.success("TTS runtime 已就绪，可通过 VoiceTrack 接入。")
        else:
            st.info("TTS Provider 未配置；当前不会伪造音频。可稍后通过 VoiceTrack 接入本地或已配置的音频。")
        voice_tracks = service.list_voice_tracks(project.id, plan.id)
        if voice_tracks:
            st.caption(f"已有 VoiceTrack：{len(voice_tracks)}")
        else:
            st.caption("尚无 VoiceTrack")

    with st.container(border=True):
        st.markdown("### BGM / 基础混音")
        uploaded = st.file_uploader("选择本地 BGM（MP3/WAV/M4A/AAC/OGG/FLAC）", type=["mp3", "wav", "m4a", "aac", "ogg", "flac"], key=f"bgm-upload-{plan.id}")
        if uploaded is not None and st.button("导入 BGM", key=f"bgm-import-{plan.id}"):
            try:
                service.import_bgm_bytes(project.id, plan.id, uploaded.getvalue(), filename=uploaded.name)
                st.success("BGM 已复制到项目存储")
                st.rerun()
            except Exception as exc:
                st.warning(_safe_error(exc, "BGM 导入失败"))
        music_tracks = service.list_music_tracks(project.id, plan.id)
        music = music_tracks[-1] if music_tracks else None
        gain = st.slider("BGM 音量", min_value=0.0, max_value=1.5, value=float(plan.audio_mix.music_gain), step=0.05, key=f"bgm-gain-{plan.id}")
        if st.button("保存混音设置", key=f"mix-save-{plan.id}"):
            try:
                service.configure_audio_mix(project.id, plan.id, AudioMixConfig(source_gain=plan.audio_mix.source_gain, voice_gain=plan.audio_mix.voice_gain, music_gain=gain, normalize=plan.audio_mix.normalize))
                st.success("混音设置已保存")
            except Exception as exc:
                st.warning(_safe_error(exc, "混音设置保存失败"))
        if music:
            st.caption(f"当前 BGM：{_safe_relative_path(music.path)} · gain {music.gain:g}")
        else:
            st.caption("尚未选择 BGM（可选）")

    if st.button("渲染最终后期成片", type="primary", key=f"post-render-{plan.id}"):
        try:
            attempt = service.render(project.id, plan.id, subtitle_track_id=subtitle_track.id if subtitle_track else None, music_track_id=music.id if music else None)
            st.success("最终后期成片已生成")
            st.session_state[f"post-latest-attempt-{plan.id}"] = attempt.id
            st.rerun()
        except PostProductionServiceError as exc:
            st.warning(_safe_error(exc, "后期渲染未完成"))

    latest = st.session_state.get(f"post-latest-attempt-{plan.id}")
    attempts = service.list_render_attempts(project.id, plan.id)
    successful = [item for item in attempts if _status_value(item) == "SUCCEEDED"]
    selected = next((item for item in successful if _value(item, "id") == latest), successful[-1] if successful else None)
    if selected:
        output = service.resolve_output_path(project.id, plan.id, _value(selected, "id"))
        if output:
            st.video(str(output))
            st.download_button("导出最终后期 MP4", data=output.read_bytes(), file_name=_safe_download_name(project.title).replace("-final", "-post"), mime="video/mp4", key=f"post-export-{selected.id}")
    with st.expander("后期渲染历史", expanded=False):
        for attempt in reversed(attempts):
            st.caption(f"Attempt {_value(attempt, 'attempt_number', '—')} · {_status_value(attempt)}")


def render() -> None:
    page_header("后期与成片", "POSTPRODUCTION", "完成已经通过 QC 的镜头合成，并预览最终视频。")
    project = current_project_or_stop()
    st.caption(f"当前项目 · {project.title}")

    production_service = ProductionService()
    manifest_service = FinalAssemblyService()
    runtime_service = FinalAssemblyRuntimeService(repository=getattr(manifest_service, "repository", None))
    job, readiness, _jobs = _select_job(production_service, project)
    if job is None:
        st.subheader("成片准备度")
        st.warning("暂无可用于成片的制作任务，请先完成镜头生产与 QC。")
        _render_history(project, None, runtime_service)
        _render_advanced(project, None, [])
        return

    assemblies = manifest_service.list_assemblies(project.id, _value(job, "id"))
    assembly = _select_assembly(project, assemblies)
    _render_readiness(readiness)
    assembly = _render_action(project, job, readiness, assembly, manifest_service, runtime_service)

    # If current production outputs changed, offer an explicit new immutable
    # manifest version.  The old successful output remains historical.
    if assembly is not None and _is_ready(readiness) and _assembly_status(assembly) == "SUCCEEDED":
        if st.button("使用当前最新合格镜头创建新成片版本", key=f"new-final-version-{project.id}"):
            try:
                with st.spinner("正在创建新的成片版本…"):
                    created = manifest_service.create_assembly(project.id, _value(job, "id"), freeze=True)
                    runtime_service.render(project.id, created.id)
                st.success("新的成片版本已制作完成")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc))

    attempts = _render_history(project, assembly, runtime_service)
    if assembly is not None and _assembly_status(assembly) == "SUCCEEDED":
        _render_preview_and_export(project, assembly, runtime_service, attempts)
        _render_post_workspace(project, job, assembly, getattr(manifest_service, "repository", None))
    _render_advanced(project, assembly, attempts)


__all__ = [
    "render",
    "_render_readiness",
    "_render_action",
    "_render_history",
    "_render_preview_and_export",
    "_safe_download_name",
]
