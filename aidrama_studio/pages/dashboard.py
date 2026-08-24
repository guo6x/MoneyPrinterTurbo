from __future__ import annotations

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.components.project_card import project_card
from aidrama_studio.domain import AspectRatio, Project, ProjectStatus
from aidrama_studio.services import ProjectService


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


def _edit_project(service: ProjectService, project: Project) -> None:
    with st.container(border=True):
        st.markdown(f"#### 编辑项目 · {project.title}")
        with st.form(f"edit-project-{project.id}"):
            title = st.text_input("项目名称", value=project.title, max_chars=120)
            description = st.text_area(
                "一句话描述", value=project.description, max_chars=1000
            )
            statuses = list(ProjectStatus)
            aspects = list(AspectRatio)
            status = st.selectbox(
                "项目状态",
                statuses,
                index=statuses.index(project.status),
                format_func=lambda item: item.value,
            )
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
                    status=status,
                    aspect_ratio=aspect,
                    target_duration_seconds=int(duration),
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
            f"确认删除项目“{project.title}”？数据库记录会删除；非空素材目录会安全归档，不会静默清除。"
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
                if result.archived_artifacts_to:
                    st.info(f"非空素材已归档到：{result.archived_artifacts_to}")
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

    active_count = sum(
        project.status not in {ProjectStatus.DRAFT, ProjectStatus.COMPLETED}
        for project in projects
    )
    completed_count = sum(
        project.status is ProjectStatus.COMPLETED for project in projects
    )
    metric_cols = st.columns(3)
    metric_cols[0].metric("项目总数", len(projects))
    metric_cols[1].metric("进行中", active_count)
    metric_cols[2].metric("已完成", completed_count)

    _create_project_form(service)

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
            _edit_project(service, editing)
    if deleting_id:
        deleting = next((item for item in projects if item.id == deleting_id), None)
        if deleting:
            _delete_project(service, deleting)

    st.markdown("## 最近更新项目")
    columns = st.columns(3)
    for index, project in enumerate(projects):
        with columns[index % 3]:
            action = project_card(project)
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
