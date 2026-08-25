from __future__ import annotations

import io
import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.components.project_card import project_card
from aidrama_studio.domain import AspectRatio, Project, ProjectStatus
from aidrama_studio.services import (
    CurrentProductionStateService,
    HeavyJobService,
    ProjectArchiveError,
    ProjectArchiveService,
    ProjectService,
)
from aidrama_studio.domain import HeavyJobStatus, HeavyJobType


def _export_archive_path(
    archive_service: ProjectArchiveService, project: Project
) -> Path:
    archive_root = archive_service.repository.paths.archived_projects
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / f"{project.id}-{uuid4().hex[:8]}.aidrama"
    archive_service.export_project(project.id, target)
    return target


def _import_archive_stream(
    archive_service: ProjectArchiveService, upload: BinaryIO | bytes | bytearray
) -> str:
    stream: BinaryIO
    if isinstance(upload, (bytes, bytearray)):
        stream = io.BytesIO(bytes(upload))
    elif hasattr(upload, "read"):
        stream = upload
    else:
        raise ProjectArchiveError("项目归档无效")
    if hasattr(stream, "seek"):
        stream.seek(0)
    archive_root = archive_service.repository.paths.archived_projects
    archive_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dashboard-import-", dir=archive_root) as directory:
        source = Path(directory) / "uploaded.aidrama"
        total = 0
        with source.open("xb") as destination:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > archive_service.MAX_ARCHIVE_BYTES:
                    raise ProjectArchiveError("项目归档超过大小限制")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if total == 0:
            raise ProjectArchiveError("项目归档为空")
        archive_service.verify_importable(source)
        # Restore the archive's canonical project identity. Copying a live
        # project under a random ID would require a complete PK/FK/provenance
        # remap and must not be approximated by rewriting project_id columns.
        return archive_service.import_project(source)


def _stage_import_archive(
    archive_service: ProjectArchiveService,
    upload: BinaryIO | bytes | bytearray,
) -> tuple[Path, str, int]:
    """Persist uploaded bytes only; validation/restore remains background work."""
    stream: BinaryIO
    if isinstance(upload, (bytes, bytearray)):
        stream = io.BytesIO(bytes(upload))
    elif hasattr(upload, "read"):
        stream = upload
    else:
        raise ProjectArchiveError("项目归档无效")
    if hasattr(stream, "seek"):
        stream.seek(0)
    root = archive_service.repository.paths.archived_projects / "import-staging"
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{uuid4().hex}.uploading"
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("xb") as destination:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > archive_service.MAX_ARCHIVE_BYTES:
                    raise ProjectArchiveError("项目归档超过大小限制")
                destination.write(chunk)
                digest.update(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if total == 0:
            raise ProjectArchiveError("项目归档为空")
        sha256 = digest.hexdigest()
        staged = root / f"{sha256}.aidrama"
        if staged.exists():
            if staged.stat().st_size != total:
                raise ProjectArchiveError("项目导入 staging hash collision")
            temporary.unlink()
        else:
            os.replace(temporary, staged)
        return staged, sha256, total
    finally:
        if temporary.exists():
            temporary.unlink()


def _archive_download_name(title: str) -> str:
    safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in str(title).strip()).strip("._-")
    return f"{(safe[:60] or 'project')}.aidrama"


def _render_recovery_notice() -> None:
    notice = st.session_state.get("project_recovery_archive")
    if not isinstance(notice, dict):
        return
    archive = Path(str(notice.get("path") or ""))
    with st.container(border=True):
        st.success("项目已删除；已创建并验证可恢复的 Recovery Archive。")
        st.caption(f"Verified Recovery Archive · {archive.name}")
        if archive.is_file():
            with archive.open("rb") as handle:
                st.download_button(
                    "下载 Recovery Archive (.aidrama)", data=handle,
                    file_name=archive.name, mime="application/zip", key="download-delete-recovery",
                )
        if st.button("关闭提示", key="dismiss-delete-recovery"):
            st.session_state.pop("project_recovery_archive", None)
            st.rerun()


def _render_archive_workspace(service: ProjectService, projects: list[Project]) -> None:
    archive_service = ProjectArchiveService(service.repository)
    heavy_service = HeavyJobService(service.repository)
    with st.expander("导出 / 导入 / 恢复项目 (.aidrama)", expanded=not projects):
        st.caption(".aidrama 会通过后台一致快照、文件哈希与恢复验证生成；不包含 API 凭据。")
        if projects:
            selected = st.selectbox(
                "导出项目", projects, format_func=lambda item: item.title,
                key="dashboard-export-project",
            )
            export_destination = st.text_input(
                "项目归档导出目标（绝对 .aidrama 路径）",
                placeholder=r"D:\Backups\我的项目.aidrama",
                key="dashboard-export-destination",
            )
            if st.button(
                "后台生成已验证的 .aidrama",
                key="dashboard-export-archive",
                disabled=not str(export_destination or "").strip(),
            ):
                try:
                    heavy_service.enqueue_project_export(
                        selected.id,
                        destination=Path(str(export_destination)),
                    )
                except Exception:
                    logger.exception("failed to export project archive")
                    st.error("无法创建项目导出任务；未生成未经验证的归档。")
                else:
                    st.success("项目导出已加入后台队列；页面可以安全关闭。")
                    st.rerun()
            export_jobs = heavy_service.list_jobs(
                selected.id, job_type=HeavyJobType.PROJECT_EXPORT
            )
            if export_jobs:
                latest_export = export_jobs[-1]
                if latest_export.status is HeavyJobStatus.RUNNING:
                    st.info(f"项目导出处理中 · {latest_export.stage}")
                elif latest_export.status is HeavyJobStatus.QUEUED:
                    st.info("项目导出已排队")
                elif latest_export.status is HeavyJobStatus.SUCCEEDED:
                    st.success(
                        f"项目归档已验证：{latest_export.output_provenance.get('archive_name', '完成')}"
                    )
                elif latest_export.status in {
                    HeavyJobStatus.FAILED,
                    HeavyJobStatus.INTERRUPTED,
                }:
                    st.warning(latest_export.safe_error or "项目导出未完成，可显式重试。")
        uploaded = st.file_uploader(
            "导入或恢复 .aidrama", type=["aidrama"], key="dashboard-import-archive",
            help="恢复保留原项目 ID；如果该项目仍存在，会安全拒绝且绝不覆盖。",
        )
        if uploaded is not None and st.button("验证并恢复为新项目", type="primary", key="dashboard-import-project"):
            try:
                staged, sha256, size = _stage_import_archive(
                    archive_service, uploaded
                )
                relative = staged.relative_to(
                    archive_service.repository.paths.archived_projects
                ).as_posix()
                heavy_service.enqueue_project_import(
                    staged_archive_relative_path=relative,
                    archive_sha256=sha256,
                    archive_size_bytes=size,
                )
            except Exception:
                logger.exception("failed to queue project archive import")
                st.error("项目归档 staging 失败；现有项目未被覆盖。")
            else:
                st.success("项目归档已加入后台验证与恢复队列。")
                st.rerun()
        imports = heavy_service.list_project_imports()
        if imports:
            latest_import = imports[-1]
            if latest_import.status is HeavyJobStatus.SUCCEEDED:
                imported_id = str(
                    latest_import.output_provenance.get("imported_project_id") or ""
                )
                imported = service.get(imported_id) if imported_id else None
                st.success(
                    f"已验证并恢复项目：{imported.title if imported else imported_id}"
                )
                if imported_id and st.button(
                    "打开已恢复项目", key=f"open-imported-{latest_import.id}"
                ):
                    st.session_state.current_project_id = imported_id
                    st.query_params["project"] = imported_id
                    _navigate("story")
            elif latest_import.status is HeavyJobStatus.RUNNING:
                st.info(f"项目恢复处理中 · {latest_import.stage}")
            elif latest_import.status is HeavyJobStatus.QUEUED:
                st.info("项目恢复已排队")
            elif latest_import.status in {
                HeavyJobStatus.FAILED,
                HeavyJobStatus.INTERRUPTED,
            }:
                st.warning(latest_import.safe_error or "项目恢复未完成，可显式重试。")


def _navigate(page: str) -> None:
    # ``st.switch_page`` otherwise clears non-embed query parameters. Carry
    # the selected project explicitly so a subsequent refresh reconstructs the
    # same project instead of opening a new NO PROJECT session.
    pages = st.session_state.get("_aidrama_pages", {})
    project_id = st.session_state.get("current_project_id")
    target = pages.get(page)
    if target is not None:
        st.switch_page(target, query_params={"project": project_id} if project_id else None)
    from aidrama_studio.components.navigation import request_navigation
    request_navigation(page)


def _create_project_form(service: ProjectService) -> None:
    with st.expander("+ 新建短剧项目", expanded=not service.list()):
        with st.form("create-project", clear_on_submit=False):
            title = st.text_input(
                "项目名称", max_chars=120, placeholder="例如：霓虹雨夜"
            )
            description = st.text_area(
                "一句话描述", max_chars=1000, placeholder="用一句话描述故事核心冲突"
            )
            aspect = st.selectbox("画幅", [item.value for item in AspectRatio], index=1)
            duration = st.number_input(
                "目标时长（秒）",
                min_value=1,
                max_value=3600,
                value=60,
                step=15,
                help="常用：30 / 45 / 60 / 90 / 120 秒，也可自定义。",
            )
            delivery_resolution = st.selectbox(
                "最终画质", ["720p", "1080p", "1440p", "4K"], index=1,
                help="这是最终交付分辨率；若 Provider 原生分辨率较低，会明确显示并走确定性放大。",
            )
            target_fps = st.selectbox("最终帧率", [24, 25, 30, 60], index=2)
            quality_mode = st.selectbox(
                "生成质量",
                ["PREVIEW", "STANDARD", "HIGH", "FINAL"],
                index=1,
                format_func=lambda value: {
                    "PREVIEW": "快速预览", "STANDARD": "标准制作",
                    "HIGH": "高质量", "FINAL": "最终成片",
                }[value],
                help="质量模式会冻结到新 RuntimePlan；预览产物不会自动成为最终成片来源。",
            )
            submitted = st.form_submit_button(
                "创建并进入创意与剧本", type="primary", use_container_width=True
            )
        if submitted:
            try:
                project = service.create(
                    title=title,
                    description=description,
                    aspect_ratio=aspect,
                    target_duration_seconds=int(duration),
                    delivery_resolution_label=delivery_resolution,
                    target_fps=float(target_fps),
                    quality_mode=quality_mode,
                )
            except (ValueError, OSError) as exc:
                logger.warning(f"invalid AIDrama project creation: {exc}")
                st.error(str(exc))
            except Exception:
                logger.exception("failed to create AIDrama project")
                st.error("项目创建失败，请检查数据库与项目目录权限。")
            else:
                st.session_state.current_project_id = project.id
                st.query_params["project"] = project.id
                st.toast("项目已创建")
                _navigate("story")


def _edit_project(
    service: ProjectService,
    project: Project,
    canonical_status: ProjectStatus | None = None,
) -> None:
    from aidrama_studio.services.runtime_foundation import OutputProfileService

    output_profile = OutputProfileService(service.repository).ensure_for_project(project.id)
    with st.container(border=True):
        st.markdown(f"#### 编辑项目 · {project.title}")
        with st.form(f"edit-project-{project.id}"):
            title = st.text_input("项目名称", value=project.title, max_chars=120)
            description = st.text_area(
                "一句话描述", value=project.description, max_chars=1000
            )
            aspects = list(AspectRatio)
            stage = canonical_status or CurrentProductionStateService(
                service.repository
            ).workflow_stage(project.id)
            st.info(f"当前制作阶段（系统派生） · {stage.value}")
            aspect = st.selectbox(
                "画幅",
                aspects,
                index=aspects.index(project.aspect_ratio),
                format_func=lambda item: item.value,
            )
            duration = st.number_input(
                "目标时长（秒）",
                min_value=1,
                max_value=3600,
                value=project.target_duration_seconds,
                step=15,
            )
            resolutions = ["720p", "1080p", "1440p", "4K"]
            delivery_resolution = st.selectbox(
                "最终画质",
                resolutions,
                index=(
                    resolutions.index(output_profile.delivery_resolution_label)
                    if output_profile.delivery_resolution_label in resolutions else 1
                ),
            )
            frame_rates = [24, 25, 30, 60]
            current_fps = int(output_profile.target_fps)
            target_fps = st.selectbox(
                "最终帧率",
                frame_rates,
                index=frame_rates.index(current_fps) if current_fps in frame_rates else 2,
            )
            quality_modes = ["PREVIEW", "STANDARD", "HIGH", "FINAL"]
            quality_mode = st.selectbox(
                "生成质量",
                quality_modes,
                index=quality_modes.index(output_profile.quality_mode),
                format_func=lambda value: {
                    "PREVIEW": "快速预览", "STANDARD": "标准制作",
                    "HIGH": "高质量", "FINAL": "最终成片",
                }[value],
            )
            st.caption(
                f"当前 OutputProfile v{output_profile.version_number} · "
                f"{output_profile.delivery_width}×{output_profile.delivery_height} · "
                "保存变更会创建新版本，只影响新的 ProductionJob / RuntimePlan。"
            )
            save_col, cancel_col = st.columns(2)
            save = save_col.form_submit_button(
                "保存", type="primary", use_container_width=True
            )
            cancel = cancel_col.form_submit_button("取消", use_container_width=True)
        if cancel:
            st.session_state.pop("editing_project_id", None)
            st.rerun()
        if save:
            try:
                service.update(
                    project.id,
                    title=title,
                    description=description,
                    aspect_ratio=aspect,
                    target_duration_seconds=int(duration),
                    delivery_resolution_label=delivery_resolution,
                    target_fps=float(target_fps),
                    quality_mode=quality_mode,
                )
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
            except Exception:
                logger.exception("failed to update AIDrama project")
                st.error("项目保存失败，请稍后重试。")
            else:
                st.session_state.pop("editing_project_id", None)
                st.toast("项目已更新")
                st.rerun()


def _delete_project(service: ProjectService, project: Project) -> None:
    with st.container(border=True):
        st.warning(
            f"确认删除项目“{project.title}”？删除前会先创建并验证可恢复的 .aidrama Recovery Archive；非空素材目录也会安全归档。"
        )
        confirmed = st.checkbox("我确认删除这个项目", key=f"confirm-{project.id}")
        delete_col, cancel_col = st.columns(2)
        if delete_col.button(
            "确认删除",
            key=f"confirm-delete-{project.id}",
            disabled=not confirmed,
            type="primary",
            use_container_width=True,
        ):
            try:
                result = service.delete(project.id, confirmed=True)
            except Exception:
                logger.exception("failed to delete AIDrama project")
                st.error("项目删除失败，数据和素材已尽量保持原状。")
            else:
                if st.session_state.get("current_project_id") == project.id:
                    st.session_state.current_project_id = None
                st.session_state.pop("deleting_project_id", None)
                if result.recovery_archive_to:
                    st.session_state["project_recovery_archive"] = {
                        "path": str(result.recovery_archive_to), "project_title": project.title,
                    }
                if result.archived_artifacts_to:
                    st.info(f"非空素材目录已归档：{result.archived_artifacts_to.name}")
                st.toast("项目已删除")
                st.rerun()
        if cancel_col.button(
            "取消", key=f"cancel-delete-{project.id}", use_container_width=True
        ):
            st.session_state.pop("deleting_project_id", None)
            st.rerun()


def render() -> None:
    page_header(
        "工作台",
        "PRODUCTION DESK",
        "集中管理短剧项目、制作阶段与最近更新。",
    )
    try:
        service = ProjectService()
        projects = service.list()
    except Exception:
        logger.exception("failed to initialize AIDrama dashboard")
        st.error("工作台初始化失败，请检查数据库目录权限。")
        return

    current_state_service = CurrentProductionStateService(service.repository)
    canonical_statuses: dict[str, ProjectStatus] = {}
    for project in projects:
        try:
            canonical_statuses[project.id] = current_state_service.workflow_stage(
                project.id
            )
        except Exception:
            # Keep the dashboard readable if a legacy record is malformed, but
            # make the fallback explicit rather than treating the DB column as
            # a second workflow authority.
            logger.exception("failed to derive canonical workflow stage")
            canonical_statuses[project.id] = project.status

    active_count = sum(
        canonical_statuses[project.id]
        not in {ProjectStatus.DRAFT, ProjectStatus.COMPLETED}
        for project in projects
    )
    completed_count = sum(
        canonical_statuses[project.id] is ProjectStatus.COMPLETED
        for project in projects
    )
    metric_cols = st.columns(3)
    metric_cols[0].metric("项目总数", len(projects))
    metric_cols[1].metric("进行中", active_count)
    metric_cols[2].metric("已完成", completed_count)

    _render_recovery_notice()
    imported_title = st.session_state.pop("archive_import_result", None)
    if imported_title:
        st.success(f"项目恢复完成：{imported_title}")

    _create_project_form(service)
    _render_archive_workspace(service, projects)

    if not projects:
        st.markdown(
            """
            <div class="aidrama-empty-state">
              <div class="aidrama-empty-label">EMPTY</div>
              <h3>还没有短剧项目</h3>
              <p>创建第一个项目，或载入一个明确标注为 DEMO 的演示项目。</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("载入演示项目", use_container_width=False):
            try:
                demo = service.create_demo()
            except Exception:
                logger.exception("failed to seed demo project")
                st.error("演示项目创建失败。")
            else:
                st.session_state.current_project_id = demo.id
                st.query_params["project"] = demo.id
                st.rerun()
        return

    editing_id = st.session_state.get("editing_project_id")
    deleting_id = st.session_state.get("deleting_project_id")
    if editing_id:
        editing = next((item for item in projects if item.id == editing_id), None)
        if editing:
            _edit_project(service, editing, canonical_statuses[editing.id])
    if deleting_id:
        deleting = next((item for item in projects if item.id == deleting_id), None)
        if deleting:
            _delete_project(service, deleting)

    st.markdown("## 最近更新项目")
    columns = st.columns(3)
    for index, project in enumerate(projects):
        with columns[index % 3]:
            action = project_card(
                project, workflow_stage=canonical_statuses[project.id]
            )
            if action == "open":
                st.session_state.current_project_id = project.id
                st.query_params["project"] = project.id
                _navigate("story")
            elif action == "edit":
                st.session_state.editing_project_id = project.id
                st.rerun()
            elif action == "delete":
                st.session_state.deleting_project_id = project.id
                st.rerun()
