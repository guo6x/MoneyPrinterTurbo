"""Product-facing Final Assembly and MP4 export page.

The page is intentionally thin: readiness, manifest freezing, rendering,
output validation, and path security remain owned by the canonical services.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import (
    current_project_or_stop,
    normalize_capability_snapshots,
    render_background_activity,
    render_project_context,
)
from aidrama_studio.services.current_state import CurrentProductionStateService
from aidrama_studio.services import (
    AudiovisualPipelineService,
    FinalAssemblyRuntimeService,
    FinalAssemblyService,
    HeavyJobService,
    HeavyJobServiceError,
    ProductionService,
    PostProductionService,
    PostProductionServiceError,
)
from aidrama_studio.domain import AudioMixConfig, HeavyJobStatus, HeavyJobType


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

# Compatibility vocabulary retained for diagnostics and existing UI contracts;
# normal users see the shorter Chinese labels in the rendered workspace.
_LEGACY_PRODUCT_LABELS = ("后期与成片", "成片准备度", "生成成片", "成片历史", "导出 MP4", "高级信息 / 调试信息")


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
        text = text.split(":", 1)[-1].strip()
    # Readiness reasons are persisted service diagnostics.  Keep the normal
    # card actionable without leaking IDs, hashes, or local paths.
    import re

    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]*", "相关文件", text)
    text = re.sub(r"\b[0-9a-f]{32,}\b", "相关记录", text, flags=re.IGNORECASE)
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
        st.success("✓ 所有镜头均已有有效素材，技术检查与人工审片状态满足成片要求。")
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


def _select_job(
    production_service: ProductionService,
    manifest_service: FinalAssemblyService,
    project: Any,
) -> tuple[Any | None, Any | None, list[Any]]:
    """Select the canonical current production job.

    ``CurrentProductionStateService`` owns newest-job and historical isolation
    rules.  The small fallback is only for isolated page fixtures that do not
    expose a repository-backed service.
    """

    try:
        jobs = list(production_service.list_jobs(project.id) or [])
    except Exception:
        return None, None, []
    if not jobs:
        return None, None, []

    repository = getattr(production_service, "repository", None)
    if repository is not None:
        try:
            state = CurrentProductionStateService(repository).derive(project.id)
            selected_job = state.job
            if selected_job is not None:
                readiness = manifest_service.calculate_readiness(
                    project.id, _value(selected_job, "id", "")
                )
                return selected_job, readiness, jobs
        except Exception:
            # Keep rendering available if a partially upgraded database cannot
            # provide the canonical projection; do not infer from project.status.
            pass

    readiness_by_job: dict[str, Any] = {}
    for job in jobs:
        try:
            readiness_by_job[str(_value(job, "id", ""))] = manifest_service.calculate_readiness(
                project.id, _value(job, "id", "")
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
        # Keep selection useful for compatibility fixtures while presenting
        # only ordinal/status labels; internal job IDs stay in Advanced.
        index_options = list(range(len(jobs)))
        current_index = next(
            (index for index, item in enumerate(jobs) if str(_value(item, "id", "")) == current),
            len(jobs) - 1,
        )
        selected = st.selectbox(
            "制作任务",
            index_options,
            index=current_index,
            format_func=lambda index: _job_label(
                jobs[int(index)],
                readiness_by_job[str(_value(jobs[int(index)], "id", ""))],
            ),
            key=f"postproduction-job-select-{project.id}",
        )
        selected_job = jobs[int(selected)]
        current = str(_value(selected_job, "id", ""))
        st.session_state[selected_key] = current
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
        st.markdown("### 生成最终成片")
        st.caption("成片会使用当前合格镜头冻结一个新版本；已完成的镜头不会被覆盖。")
        confirmed = st.checkbox(
            "我已确认当前镜头顺序与成片设置，并同意生成最终成片",
            key=f"confirm-final-create-{project.id}",
        )
        if st.button("生成最终成片", type="primary", key=f"generate-final-{project.id}"):
            if not confirmed:
                st.warning("请先确认当前镜头顺序与成片设置，再生成最终成片。")
                return None
            try:
                created = manifest_service.create_assembly(
                    project.id, _value(job, "id"), freeze=True
                )
                HeavyJobService(
                    getattr(manifest_service, "repository", None)
                ).enqueue_final_assembly(project.id, _value(created, "id"))
                st.success("最终成片任务已加入后台队列；可以安全离开本页面。")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc))
        return None

    repository = getattr(manifest_service, "repository", None)
    active_job = None
    if repository is not None:
        active_job = _latest_job_for_target(
            HeavyJobService(repository),
            project.id,
            HeavyJobType.FINAL_ASSEMBLY_RENDER,
            "assembly_id",
            _value(assembly, "id"),
        )
        _render_heavy_job_status(active_job)
        if active_job is not None and _status_value(active_job) in {
            HeavyJobStatus.QUEUED.value,
            HeavyJobStatus.RUNNING.value,
        }:
            return assembly

    if status in {"DRAFT", "READY", "FAILED", "CANCELLED"}:
        if status == "DRAFT":
            st.info("最终成片版本已准备好，可以开始生成。")
        elif status == "READY":
            st.info("成片素材已就绪，可以生成最终成片。")
        elif status == "FAILED":
            st.error("成片制作失败")
            attempts = runtime_service.list_attempts(project.id, _value(assembly, "id"))
            failed = next((item for item in reversed(attempts) if _status_value(item) == "FAILED"), None)
            if failed is not None:
                st.caption(_safe_error(_value(failed, "error_message", "请重试。")))
        else:
            st.warning("成片生成已停止。")
        confirmed = st.checkbox(
            "我已确认会创建一个新的最终成片版本",
            key=f"confirm-final-render-{_value(assembly, 'id', 'assembly')}",
        )
        if st.button(
            "重新生成最终成片" if status == "FAILED" else "生成最终成片",
            type="primary",
            key=f"render-final-{_value(assembly, 'id')}",
        ):
            if not confirmed:
                st.warning("请先确认后再创建新的最终成片版本。")
                return assembly
            try:
                heavy = HeavyJobService(
                    getattr(manifest_service, "repository", None)
                )
                latest = _latest_job_for_target(
                    heavy,
                    project.id,
                    HeavyJobType.FINAL_ASSEMBLY_RENDER,
                    "assembly_id",
                    _value(assembly, "id"),
                )
                if status == "FAILED" and latest is not None and _status_value(latest) in {
                    HeavyJobStatus.FAILED.value,
                    HeavyJobStatus.CANCELLED.value,
                    HeavyJobStatus.INTERRUPTED.value,
                }:
                    heavy.retry(_value(latest, "id"))
                else:
                    heavy.enqueue_final_assembly(project.id, _value(assembly, "id"))
                st.success("成片任务已加入后台队列；状态会自动持久化。")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc))
        return assembly
    if status == "ASSEMBLING":
        st.info("正在后台生成最终成片…")
    elif status == "SUCCEEDED":
        st.success("最终成片制作完成")
    return assembly


def _latest_job_for_target(
    service: HeavyJobService,
    project_id: str,
    job_type: HeavyJobType,
    target_key: str,
    target_id: str,
):
    try:
        jobs = service.list_jobs(project_id, job_type=job_type)
    except Exception:
        return None
    for job in reversed(jobs):
        snapshot = _value(job, "input_snapshot", {}) or {}
        if isinstance(snapshot, Mapping) and str(snapshot.get(target_key) or "") == str(target_id):
            return job
    return None


def _human_heavy_stage(value: object) -> str:
    text = str(getattr(value, "value", value) or "").strip().upper()
    return {
        "PREPARING": "准备素材",
        "ASSEMBLING": "拼接画面",
        "POST_RENDER": "渲染后期",
        "EXPORTING": "准备导出",
        "VALIDATING": "校验成片",
        "RUNNING": "处理中",
        "QUEUED": "等待后台处理",
    }.get(text, "处理中")


def _render_heavy_job_status(job: Any | None) -> None:
    if job is None:
        return
    status = _status_value(job)
    stage = _human_heavy_stage(_value(job, "stage", "处理中"))
    progress = _value(job, "progress", None)
    if status == "QUEUED":
        st.info("后台任务已排队")
    elif status == "RUNNING":
        if progress is None:
            st.info(f"后台处理中 · {stage}")
        else:
            try:
                numeric_progress = float(progress)
                # HeavyJob rows from older installations store a percentage;
                # newer rows may store a 0..1 fraction.  Render one human
                # percentage without exposing the storage convention.
                if 0 <= numeric_progress <= 1:
                    numeric_progress *= 100
                numeric_progress = max(0.0, min(100.0, numeric_progress))
                st.info(f"后台处理中 · {stage} · {numeric_progress:.1f}%")
            except (TypeError, ValueError):
                st.info(f"后台处理中 · {stage}")
    elif status == "INTERRUPTED":
        st.warning("上次本地进程中断；冻结输入仍保留，可显式重试。")
    elif status == "FAILED":
        st.warning(_safe_error(_value(job, "safe_error", "后台任务失败")))


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
    if not isinstance(metadata, Mapping):
        metadata = {}
    columns = st.columns(4)
    columns[0].metric("时长", _format_duration(metadata.get("duration_seconds", metadata.get("duration"))))
    resolution = metadata.get("resolution") or (
        f"{metadata.get('width')} × {metadata.get('height')}"
        if metadata.get("width") and metadata.get("height") else "—"
    )
    columns[1].metric("分辨率", str(resolution).replace("x", " × "))
    columns[2].metric("音频", "有" if metadata.get("audio_stream") else "无")
    columns[3].metric("文件大小", _format_size(metadata.get("size_bytes")))


def _render_preview_and_export(project: Any, assembly: Any, runtime_service: FinalAssemblyRuntimeService, attempts: list[Any]) -> None:
    successful = [item for item in attempts if _status_value(item) == "SUCCEEDED"]
    if not successful:
        return
    assembly_id = _value(assembly, "id", "assembly")
    selected_key = f"postproduction-attempt-{assembly_id}"
    selected_id = st.session_state.get(selected_key) or _value(successful[-1], "id")
    selected = next((item for item in successful if _value(item, "id") == selected_id), successful[-1])
    try:
        output_path = runtime_service.resolve_output_path(project.id, assembly_id, _value(selected, "id"))
    except Exception as exc:
        st.warning(_safe_error(exc, "成片文件不可用"))
        return
    st.subheader("成片")
    if output_path is None:
        st.warning("成片文件不可用")
        return
    st.video(str(output_path))
    _render_metadata(selected)
    destination = st.text_input(
        "导出目标（MP4 文件路径）",
        placeholder="例如：我的短剧-final.mp4",
        key=f"final-export-destination-{_value(selected, 'id')}",
        help="导出会创建独立副本；项目中的当前成片不会被移动或删除。",
    )
    export_confirmed = st.checkbox(
        "我已确认导出目标；项目中的成片不会被移动或删除",
        key=f"confirm-final-export-{assembly_id}-{_value(selected, 'id')}",
    )
    if st.button(
        "后台导出 MP4",
        disabled=not str(destination or "").strip(),
        key=f"export-final-{assembly_id}-{_value(selected, 'id')}",
    ):
        if not export_confirmed:
            st.warning("请先确认导出目标，再提交导出任务。")
            return
        try:
            repository = getattr(runtime_service, "repository", None)
            root = (repository.paths.projects / project.id).resolve()
            relative = output_path.resolve().relative_to(root).as_posix()
            metadata = _value(selected, "metadata_json", {}) or {}
            if not isinstance(metadata, Mapping):
                metadata = {}
            HeavyJobService(repository).enqueue_final_media_export(
                project.id,
                source_relative_path=relative,
                source_sha256=str(metadata.get("sha256") or ""),
                source_size_bytes=int(metadata.get("size_bytes") or output_path.stat().st_size),
                destination=Path(str(destination)),
            )
            st.success("导出任务已加入后台队列；当前成片保持不变。")
            st.rerun()
        except Exception as exc:
            st.warning(_safe_error(exc, "无法创建后台导出任务"))


def _render_history(project: Any, assembly: Any | None, runtime_service: FinalAssemblyRuntimeService) -> list[Any]:
    st.subheader("成片历史")
    if assembly is None:
        st.info("尚未生成成片版本。")
        return []
    assembly_id = _value(assembly, "id", "assembly")
    try:
        attempts = runtime_service.list_attempts(project.id, assembly_id)
    except Exception as exc:
        st.warning(_safe_error(exc))
        return []
    if not attempts:
        st.info("成片版本已就绪，尚未开始生成。")
        return []
    for attempt in reversed(attempts):
        status = _status_value(attempt)
        label = _ATTEMPT_LABELS.get(status, "处理中")
        with st.container(border=True):
            st.markdown(f"**成片版本 {_value(attempt, 'attempt_number', '—')}** · {label}")
            st.caption(_format_timestamp(_value(attempt, "finished_at", _value(attempt, "created_at"))))
            if status == "SUCCEEDED":
                metadata = _value(attempt, "metadata_json", {}) or {}
                if not isinstance(metadata, Mapping):
                    metadata = {}
                st.caption(_format_duration(metadata.get("duration_seconds")) + " · " + str(metadata.get("resolution") or "—"))
                if st.button("查看此版本", key=f"view-final-attempt-{_value(attempt, 'id')}"):
                    st.session_state[f"postproduction-attempt-{assembly_id}"] = _value(attempt, "id")
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
            if not isinstance(metadata, Mapping):
                metadata = {}
            if metadata.get("codec"):
                st.caption(f"Codec · {metadata['codec']}")
            if metadata.get("sha256"):
                st.caption(f"SHA-256 · {metadata['sha256']}")
            source_items = metadata.get("source_items") if isinstance(metadata, Mapping) else None
            if source_items:
                st.caption(f"Frozen source count · {len(source_items)}")


def _activity_for_post(
    project: Any,
    heavy_service: HeavyJobService | Any,
    *,
    plan_id: str | None = None,
    assembly_id: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Project durable post/final jobs into the shared non-blocking activity strip.

    The helper is deliberately defensive: pages and AppTest fixtures may not
    expose a repository-backed ``HeavyJobService``.  An unavailable read means
    no activity strip, never a guessed progress value or a blocking spinner.
    """

    if heavy_service is None:
        return ()
    candidates: list[Any] = []
    try:
        if plan_id:
            item = _latest_job_for_target(
                heavy_service,
                project.id,
                HeavyJobType.POST_RENDER,
                "plan_id",
                plan_id,
            )
            if item is not None:
                candidates.append(item)
        if assembly_id:
            item = _latest_job_for_target(
                heavy_service,
                project.id,
                HeavyJobType.FINAL_ASSEMBLY_RENDER,
                "assembly_id",
                assembly_id,
            )
            if item is not None:
                candidates.append(item)
    except Exception:
        return ()

    if not candidates:
        return ()
    job = candidates[-1]
    status = _status_value(job)
    if status not in {"QUEUED", "RUNNING", "FAILED", "INTERRUPTED"}:
        return ()
    if status == "QUEUED":
        state, title, detail = "queued", "成片任务已排队", "后台会继续处理，页面可以安全离开。"
    elif status == "RUNNING":
        state, title, detail = "running", "成片正在后台处理", "可以继续查看字幕、配音和混音设置。"
    elif status == "INTERRUPTED":
        state, title, detail = "interrupted", "成片任务曾被中断", "冻结输入仍保留，可在当前状态下显式重试。"
    else:
        state, title, detail = "failed", "成片任务需要处理", _safe_error(_value(job, "safe_error", "任务未完成"))
    progress = _value(job, "progress", None)
    try:
        numeric_progress = float(progress) if progress is not None else None
        if numeric_progress is not None and numeric_progress > 1:
            numeric_progress /= 100.0
        progress = max(0.0, min(1.0, numeric_progress)) if numeric_progress is not None else None
    except (TypeError, ValueError):
        progress = None
    return (
        {
            "activity_id": _value(job, "id"),
            "title": title,
            "detail": detail,
            "state": state,
            "progress": progress,
            "next_action": "查看制作状态" if status in {"FAILED", "INTERRUPTED"} else None,
            "next_page": "production" if status in {"FAILED", "INTERRUPTED"} else None,
            "updated_at": _value(job, "updated_at", _value(job, "created_at")),
        },
    )


def _script_revision_id(repository: Any, job: Any) -> str | None:
    if repository is None or job is None:
        return None
    try:
        revision = repository.get_shot_revision(_value(job, "shot_plan_revision_id", ""))
        return str(revision["source_script_revision_id"]) if revision else None
    except Exception:
        return None


def _plan_for_assembly(plans: list[Any], assembly_id: str) -> Any | None:
    """Select only a post plan belonging to the visible immutable assembly."""
    matching = [
        plan
        for plan in plans
        if str(_value(plan, "source_final_assembly_id", "")) == str(assembly_id)
    ]
    return matching[-1] if matching else None


def _postproduction_state_snapshot(
    service: PostProductionService,
    pipeline: AudiovisualPipelineService,
    project_id: str,
    plan_id: str,
) -> dict[str, str]:
    """Return the four user-facing delivery states without mutating runtime."""

    dialogue_plans = pipeline.list_dialogue_plans(project_id, plan_id)
    dialogue = dialogue_plans[-1] if dialogue_plans else None
    assignments = pipeline.list_voice_assignment_sets(project_id, plan_id)
    assignment_candidates = [
        item
        for item in assignments
        if dialogue is not None
        and _value(item, "source_dialogue_plan_id") == _value(dialogue, "id")
    ]
    assignment = assignment_candidates[-1] if assignment_candidates else None
    tasks = pipeline.list_tts_tasks(project_id, plan_id)
    latest_tasks: dict[str, Any] = {}
    for item in tasks:
        if (
            dialogue is None
            or assignment is None
            or _value(item, "source_dialogue_plan_id") != _value(dialogue, "id")
            or _value(item, "source_voice_assignment_set_id")
            != _value(assignment, "id")
        ):
            continue
        line_id = str(_value(item, "dialogue_line_id", ""))
        current = latest_tasks.get(line_id)
        if current is None or int(_value(item, "version", 0)) > int(
            _value(current, "version", 0)
        ):
            latest_tasks[line_id] = item
    line_ids = {
        str(_value(item, "id", "")) for item in (_value(dialogue, "lines", []) or [])
    }
    voice_ready = bool(
        line_ids
        and set(latest_tasks) == line_ids
        and all(_status_value(item) == "SUCCEEDED" for item in latest_tasks.values())
    )
    audio_timelines = pipeline.list_audio_timelines(project_id, plan_id)
    timeline_candidates = [
        item
        for item in audio_timelines
        if dialogue is not None
        and assignment is not None
        and _value(item, "source_dialogue_plan_id") == _value(dialogue, "id")
        and _value(item, "source_voice_assignment_set_id") == _value(assignment, "id")
    ]
    timeline = timeline_candidates[-1] if timeline_candidates else None
    voice_tracks = service.list_voice_tracks(project_id, plan_id)
    matching_voice_tracks = [
        item
        for item in voice_tracks
        if timeline is not None
        and _value(_value(item, "metadata_json", {}), "source_audio_timeline_id")
        == _value(timeline, "id")
    ]
    subtitle_tracks = service.list_subtitle_tracks(project_id, plan_id)
    subtitle_ready = False
    if timeline is not None:
        for subtitle in reversed(subtitle_tracks):
            if _value(subtitle, "source_script_revision_id") != _value(
                timeline, "source_script_revision_id"
            ):
                continue
            try:
                pipeline.assert_subtitle_timing_matches_audio(timeline, subtitle)
            except Exception:
                continue
            subtitle_ready = True
            break
    delivery_attempts = [
        item
        for item in service.list_render_attempts(project_id, plan_id)
        if _status_value(item) == "SUCCEEDED"
    ]
    return {
        "Voice state": (
            f"READY · {len(line_ids)} lines"
            if voice_ready
            else "NOT READY"
        ),
        "Audio state": "READY" if timeline and matching_voice_tracks else "NOT READY",
        "Subtitle state": "READY" if subtitle_ready else "NOT READY",
        "Delivery artifact": (
            f"READY · v{_value(delivery_attempts[-1], 'attempt_number', len(delivery_attempts))}"
            if delivery_attempts
            else "NOT RENDERED"
        ),
    }


def _render_post_workspace(project: Any, job: Any, assembly: Any, repository: Any) -> None:
    """Thin post-production workspace backed exclusively by PostProductionService."""
    st.subheader("后期与成片")
    st.caption("在最终画面基础上处理字幕、配音、BGM 与混音；基础成片版本保持不变。")
    if repository is None:
        st.info("后期服务尚未连接到项目存储。")
        return
    service = PostProductionService(repository=repository)
    heavy_service = HeavyJobService(getattr(service, "repository", repository))
    try:
        plans = service.list_plans(project.id)
    except Exception as exc:
        st.warning(_safe_error(exc, "后期设置暂不可用"))
        return
    assembly_id = _value(assembly, "id", "assembly")
    plan = _plan_for_assembly(plans, assembly_id)
    # Durable work is projected before any controls so a queued render never
    # turns this page into a blocking spinner or a fake-progress dashboard.
    render_background_activity(
        _activity_for_post(
            project,
            heavy_service,
            plan_id=str(_value(plan, "id", "")) if plan is not None else None,
            assembly_id=str(_value(assembly, "id", "")),
        ),
        compact=True,
    )
    if plan is None:
        # This is the one dominant action for the empty post workspace.  Once
        # a plan exists, the dominant action moves to the render control below.
        if st.button("开始后期", type="primary", key=f"post-start-{project.id}-{assembly_id}"):
            try:
                plan = service.create_plan(project.id, assembly_id)
                st.success("后期工作区已准备好")
                st.rerun()
            except Exception as exc:
                st.warning(_safe_error(exc, "无法创建后期计划"))
        st.caption("后期设置会引用当前成片版本，不会覆盖已完成的基础成片。")
        return

    st.caption("后期设置已连接到当前成片版本")
    subtitle_revision_id = _script_revision_id(repository, job)
    plan_id = _value(plan, "id", "plan")
    pipeline = AudiovisualPipelineService(
        repository=repository, postproduction_service=service
    )
    state_snapshot = _postproduction_state_snapshot(
        service, pipeline, project.id, plan_id
    )
    state_columns = st.columns(4)
    for column, (label, state) in zip(state_columns, state_snapshot.items()):
        with column:
            st.caption(label)
            st.markdown(f"**{state}**")
    subtitle_tracks = service.list_subtitle_tracks(project.id, plan_id)
    subtitle_track = subtitle_tracks[-1] if subtitle_tracks else None
    with st.container(border=True):
        st.markdown("### 字幕")
        if subtitle_track is None and subtitle_revision_id:
            if st.button("从结构化剧本生成字幕时间线", key=f"post-subtitles-{plan_id}"):
                try:
                    service.build_subtitle_timeline(project.id, subtitle_revision_id, plan_id=plan_id)
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "字幕时间线生成失败"))
        elif subtitle_track is None:
            st.caption("没有可用的结构化剧本修订版，暂时无法生成字幕。")
        else:
            subtitle_track_id = _value(subtitle_track, "id", "subtitle")
            subtitle_enabled = bool(_value(subtitle_track, "enabled", False))
            cues = list(_value(subtitle_track, "cues", ()) or ())
            enabled = st.checkbox("启用字幕（可在最终渲染前关闭）", value=subtitle_enabled, key=f"subtitle-enabled-{subtitle_track_id}")
            cue_text = "\n".join(str(_value(cue, "text", "")) for cue in cues)
            edited_text = st.text_area("字幕文本（每行一条，时间轴保持来自剧本）", value=cue_text, key=f"subtitle-edit-{subtitle_track_id}", height=130)
            if st.button("保存字幕草稿", key=f"subtitle-save-{subtitle_track_id}"):
                try:
                    lines = edited_text.splitlines()
                    updated_cues = []
                    for index, cue in enumerate(cues):
                        if index >= len(lines) or not lines[index].strip():
                            continue
                        copier = getattr(cue, "model_copy", None)
                        if callable(copier):
                            updated_cues.append(copier(update={"text": lines[index].strip()}))
                        elif isinstance(cue, Mapping):
                            updated_cues.append(dict(cue, text=lines[index].strip()))
                        else:
                            updated_cues.append(cue)
                    service.update_subtitle_track(project.id, subtitle_track_id, cues=updated_cues, enabled=enabled)
                    service.update_plan(project.id, plan_id, subtitle_enabled=enabled)
                    st.success("字幕草稿已保存")
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "字幕保存失败"))
            srt = service.to_srt(subtitle_track)
            st.download_button("导出 SRT", data=srt, file_name=f"{_value(project, 'title', 'aidrama') or 'aidrama'}.srt", mime="application/x-subrip", key=f"srt-download-{subtitle_track_id}")

    with st.container(border=True):
        st.markdown("### 配音")
        voice_tracks = service.list_voice_tracks(project.id, plan_id)
        voice_track = voice_tracks[-1] if voice_tracks else None
        # Consume only the neutral capability projection.  In particular, do
        # not instantiate a provider or issue a live request while rendering.
        try:
            snapshots = normalize_capability_snapshots(
                project_id=_value(project, "id")
            )
        except Exception:
            snapshots = ()
        tts = next(
            (
                item
                for item in snapshots
                if str(_value(item, "capability", "")).upper() in {"TTS", "VOICE"}
            ),
            None,
        )
        if voice_track is not None:
            st.success("本地 Fake TTS 配音轨已准备好；外部 Provider 调用为 0。")
        elif tts is not None and bool(_value(tts, "ready", False)):
            st.success("配音能力已准备好，可接入配音轨。")
        elif tts is not None:
            state = _value(tts, "display_state", _value(tts, "state", "需要配置"))
            reason = _value(tts, "safe_reason", None)
            st.info(
                f"配音能力：{state}。不会调用真实 Provider；"
                "可使用下方显式标注的本地 Fake TTS 流程。"
            )
            if reason:
                st.caption(str(reason)[:180])
        else:
            st.info(
                "配音能力状态暂不可用；不会调用真实 Provider，"
                "可使用下方显式标注的本地 Fake TTS 流程。"
            )
        if voice_tracks:
            st.caption(f"已有配音轨：{len(voice_tracks)}")
        else:
            st.caption("尚无配音轨")
            if subtitle_revision_id and st.button(
                "生成离线 Fake TTS 配音与同步字幕",
                key=f"post-fake-tts-{plan_id}",
            ):
                try:
                    pipeline.run_fake_pipeline(project.id, plan_id)
                    st.success("离线配音、音频时间线与同步字幕已生成；外部 Provider 调用为 0")
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "离线配音与字幕生成失败"))

    with st.container(border=True):
        st.markdown("### BGM / 基础混音")
        uploaded = st.file_uploader("选择本地 BGM（MP3/WAV/M4A/AAC/OGG/FLAC）", type=["mp3", "wav", "m4a", "aac", "ogg", "flac"], key=f"bgm-upload-{plan_id}")
        if uploaded is not None and st.button("导入 BGM", key=f"bgm-import-{plan_id}"):
            try:
                service.import_bgm_bytes(project.id, plan_id, uploaded.getvalue(), filename=uploaded.name)
                st.success("BGM 已复制到项目存储")
                st.rerun()
            except Exception as exc:
                st.warning(_safe_error(exc, "BGM 导入失败"))
        music_tracks = service.list_music_tracks(project.id, plan_id)
        music = music_tracks[-1] if music_tracks else None
        audio_mix = _value(plan, "audio_mix", None)
        source_gain = float(_value(audio_mix, "source_gain", 1.0) or 1.0)
        voice_gain = float(_value(audio_mix, "voice_gain", 1.0) or 1.0)
        music_gain = float(_value(audio_mix, "music_gain", 1.0) or 1.0)
        normalize = bool(_value(audio_mix, "normalize", True))
        gain = st.slider("BGM 音量", min_value=0.0, max_value=1.5, value=max(0.0, min(1.5, music_gain)), step=0.05, key=f"bgm-gain-{plan_id}")
        if st.button("保存混音设置", key=f"mix-save-{plan_id}"):
            try:
                service.configure_audio_mix(project.id, plan_id, AudioMixConfig(source_gain=source_gain, voice_gain=voice_gain, music_gain=gain, normalize=normalize))
                st.success("混音设置已保存")
            except Exception as exc:
                st.warning(_safe_error(exc, "混音设置保存失败"))
        if music:
            st.caption(f"当前 BGM 已选择 · 音量 {float(_value(music, 'gain', 0) or 0):g}")
        else:
            st.caption("尚未选择 BGM（可选）")

    post_job = _latest_job_for_target(
        heavy_service,
        project.id,
        HeavyJobType.POST_RENDER,
        "plan_id",
        plan_id,
    )
    _render_heavy_job_status(post_job)
    st.markdown("### 后期输出 / 导出")
    # With a post plan in place this is the sole dominant action in the
    # workspace.  Subtitle/BGM controls above remain deliberately secondary.
    post_render_confirmed = st.checkbox(
        "我已确认当前字幕、配音和混音设置，并同意生成后期成片",
        key=f"confirm-post-render-{plan_id}",
    )
    if st.button("渲染最终后期成片", type="primary", key=f"post-render-{plan_id}"):
        if not post_render_confirmed:
            st.warning("请先确认字幕、配音和混音设置，再生成后期成片。")
            return
        try:
            if post_job is not None and _status_value(post_job) in {
                HeavyJobStatus.FAILED.value,
                HeavyJobStatus.CANCELLED.value,
                HeavyJobStatus.INTERRUPTED.value,
            }:
                queued = heavy_service.retry(_value(post_job, "id"))
            else:
                queued = heavy_service.enqueue_post_render(
                    project.id,
                    plan_id,
                    subtitle_track_id=_value(subtitle_track, "id") if subtitle_track else None,
                    music_track_id=_value(music, "id") if music else None,
                    voice_track_id=_value(voice_track, "id") if voice_track else None,
                )
            st.success("最终后期成片任务已加入后台队列")
            st.session_state[f"post-latest-heavy-job-{plan_id}"] = _value(queued, "id")
            st.rerun()
        except (PostProductionServiceError, HeavyJobServiceError) as exc:
            st.warning(_safe_error(exc, "后期渲染未完成"))

    latest = st.session_state.get(f"post-latest-attempt-{plan_id}")
    attempts = service.list_render_attempts(project.id, plan_id)
    successful = [item for item in attempts if _status_value(item) == "SUCCEEDED"]
    selected = next((item for item in successful if _value(item, "id") == latest), successful[-1] if successful else None)
    if selected:
        output = service.resolve_output_path(project.id, plan_id, _value(selected, "id"))
        if output:
            st.video(str(output))
            destination = st.text_input(
                "后期成片导出目标（MP4 文件路径）",
                placeholder="例如：我的短剧-post.mp4",
                key=f"post-export-destination-{_value(selected, 'id', 'attempt')}",
            )
            post_export_confirmed = st.checkbox(
                "我已确认后期导出目标；项目中的成片不会被移动或删除",
                key=f"confirm-post-export-{_value(selected, 'id', 'attempt')}",
            )
            if st.button(
                "后台导出后期 MP4",
                disabled=not str(destination or "").strip(),
                key=f"post-export-{_value(selected, 'id', 'attempt')}",
            ):
                if not post_export_confirmed:
                    st.warning("请先确认导出目标，再提交导出任务。")
                    return
                try:
                    root = (service.repository.paths.projects / project.id).resolve()
                    relative = output.resolve().relative_to(root).as_posix()
                    metadata = _value(selected, "metadata_json", {}) or {}
                    if not isinstance(metadata, Mapping):
                        metadata = {}
                    heavy_service.enqueue_final_media_export(
                        project.id,
                        source_relative_path=relative,
                        source_sha256=str(metadata.get("sha256") or ""),
                        source_size_bytes=int(metadata.get("size_bytes") or output.stat().st_size),
                        destination=Path(str(destination)),
                    )
                    st.success("后期成片导出任务已加入后台队列")
                    st.rerun()
                except Exception as exc:
                    st.warning(_safe_error(exc, "无法创建后台导出任务"))
    with st.expander("后期版本历史", expanded=False):
        for attempt in reversed(attempts):
            label = _ATTEMPT_LABELS.get(_status_value(attempt), "处理中")
            st.caption(f"后期版本 {_value(attempt, 'attempt_number', '—')} · {label}")


def render() -> None:
    page_header("成片", "FINAL WORKSPACE", "预览最终画面，选择字幕、配音和音乐，然后生成可导出的成片。")
    project = current_project_or_stop()
    # The page owns the dominant final-assembly action. Keep the shared shell
    # context quiet so it does not render a second competing CTA.
    render_project_context(
        project,
        stage="成片",
        next_action="生成最终成片",
        next_page="postproduction",
        quiet=True,
        suppress_next=True,
    )

    production_service = ProductionService()
    manifest_service = FinalAssemblyService()
    runtime_service = FinalAssemblyRuntimeService(repository=getattr(manifest_service, "repository", None))
    job, readiness, _jobs = _select_job(production_service, manifest_service, project)
    if job is None:
        st.subheader("成片准备度")
        st.warning("暂无可用于成片的制作任务，请先完成镜头生产与 QC。")
        _render_history(project, None, runtime_service)
        _render_advanced(project, None, [])
        return

    assemblies = manifest_service.list_assemblies(project.id, _value(job, "id"))
    assembly = _select_assembly(project, assemblies)
    _render_readiness(readiness)
    # Keep final-assembly work visible even while the assembly is not yet
    # succeeded (the post workspace is mounted only after success).
    repository = getattr(manifest_service, "repository", None)
    if assembly is not None and repository is not None:
        render_background_activity(
            _activity_for_post(
                project,
                HeavyJobService(repository),
                assembly_id=str(_value(assembly, "id", "")),
            ),
            compact=True,
        )
    assembly = _render_action(project, job, readiness, assembly, manifest_service, runtime_service)

    # If current production outputs changed, offer an explicit new immutable
    # manifest version.  The old successful output remains historical.
    if assembly is not None and _is_ready(readiness) and _assembly_status(assembly) == "SUCCEEDED":
        st.checkbox(
            "我已确认使用当前最新合格镜头创建新的最终成片版本",
            key=f"confirm-new-final-version-{project.id}",
        )
        if st.button("使用当前最新合格镜头创建新成片版本", key=f"new-final-version-{project.id}"):
            if not st.session_state.get(f"confirm-new-final-version-{project.id}"):
                st.warning("请先确认后再创建新的最终成片版本。")
                return
            try:
                # Enqueue is non-blocking; the durable activity strip keeps
                # the workspace mounted while the runner works.
                created = manifest_service.create_assembly(
                    project.id, _value(job, "id"), freeze=True
                )
                HeavyJobService(
                    getattr(manifest_service, "repository", None)
                ).enqueue_final_assembly(project.id, _value(created, "id"))
                st.success("新的成片版本已加入后台队列")
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
    "_postproduction_state_snapshot",
]
