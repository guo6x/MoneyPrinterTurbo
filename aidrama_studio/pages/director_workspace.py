"""Interactive Director Workspace built on the canonical read projection."""

from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st

from aidrama_studio.services.director_workspace import (
    DirectorWorkspaceProjection,
    DirectorWorkspaceProjectionService,
    WorkspaceBeat,
    WorkspaceCandidate,
    WorkspaceReference,
    WorkspaceShot,
)


_STATE_LABELS = {
    "READY": "READY",
    "GENERATING": "GENERATING",
    "QC": "QC",
    "WAITING_HUMAN": "WAITING HUMAN",
    "ACCEPTED": "ACCEPTED",
    "BLOCKED": "BLOCKED",
}


def _selected_shot_key(project_id: str) -> str:
    return f"director-selected-shot-{project_id}"


def _selected_beat_key(project_id: str) -> str:
    return f"director-selected-beat-{project_id}"


def _candidate_key(project_id: str, shot_id: str) -> str:
    return f"director-candidate-{project_id}-{shot_id}"


def _select_shot(project_id: str, shot_id: str, beat_id: str | None = None) -> None:
    """Synchronize the Script, Shot Grid, Preview and Timeline selection."""

    st.session_state[_selected_shot_key(project_id)] = shot_id
    st.session_state["director_selected_shot"] = shot_id
    if beat_id:
        st.session_state[_selected_beat_key(project_id)] = beat_id


def _select_beat(project_id: str, beat: WorkspaceBeat) -> None:
    st.session_state[_selected_beat_key(project_id)] = beat.beat_id
    if beat.shot_ids:
        _select_shot(project_id, beat.shot_ids[0], beat.beat_id)


def _selected_shot(
    project_id: str, projection: DirectorWorkspaceProjection
) -> WorkspaceShot | None:
    if not projection.shots:
        return None
    selected_id = st.session_state.get(_selected_shot_key(project_id))
    selected = next(
        (item for item in projection.shots if item.shot_id == selected_id), None
    )
    if selected is None:
        selected = projection.shots[0]
        _select_shot(
            project_id,
            selected.shot_id,
            selected.beat_ids[0] if selected.beat_ids else None,
        )
    return selected


def candidate_for_preview(
    shot: WorkspaceShot, requested_candidate_id: str | None = None
) -> WorkspaceCandidate | None:
    """Honor an explicit comparison choice, otherwise use formal source priority."""

    if requested_candidate_id:
        requested = next(
            (
                item
                for item in shot.candidates
                if item.candidate_id == requested_candidate_id
            ),
            None,
        )
        if requested is not None:
            return requested
    default = next(
        (
            item
            for item in shot.candidates
            if item.candidate_id == shot.preview_candidate_id
        ),
        None,
    )
    return default or next(
        (item for item in shot.candidates if item.is_selected_source),
        shot.candidates[-1] if shot.candidates else None,
    )


def _first_beat_for_shot(
    projection: DirectorWorkspaceProjection, shot: WorkspaceShot
) -> str | None:
    exact = next(
        (item.beat_id for item in projection.beats if item.beat_id in shot.beat_ids),
        None,
    )
    if exact is not None:
        return exact
    exact = next(
        (item.beat_id for item in projection.beats if shot.shot_id in item.shot_ids),
        None,
    )
    return exact


def render_mode_selector(project_id: str) -> str:
    """Mount the AUTO / PRO / DIRECTOR product mode foundation."""

    key = f"director-workspace-mode-{project_id}"
    if key not in st.session_state:
        st.session_state[key] = "DIRECTOR"
    mode = st.segmented_control(
        "创作模式",
        ["AUTO", "PRO", "DIRECTOR"],
        key=key,
        selection_mode="single",
        help="AUTO 使用现有自动编排；PRO 是精简工作台；DIRECTOR 展开完整创作控制。",
    )
    return str(mode or "DIRECTOR")


def render_auto_foundation(
    project: object, projection: DirectorWorkspaceProjection
) -> None:
    """Link to, but never reimplement, the existing Auto Orchestrator."""

    with st.container(border=True, key="director-auto-foundation"):
        st.markdown(
            '<span class="aidrama-director-mode-marker" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        st.markdown("### AUTO · 正式编排状态")
        st.caption("自动生成与停止门由现有 Production Orchestrator 负责。")
        columns = st.columns(3)
        columns[0].metric("Workspace", projection.state)
        columns[1].metric("镜头", len(projection.shots))
        columns[2].metric("正式时长", f"{projection.total_duration_seconds:g} 秒")
        if st.button("打开制作编排", type="primary", key=f"auto-open-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("production")


def render(
    project: object,
    *,
    compact: bool = False,
    projection_service: DirectorWorkspaceProjectionService | None = None,
) -> DirectorWorkspaceProjection | None:
    adapter = st.session_state.get("_aidrama_continuity_projection_adapter")
    service = projection_service or DirectorWorkspaceProjectionService(
        continuity_adapter=adapter if callable(adapter) else None
    )
    try:
        projection = service.project(project.id)
    except (ValueError, KeyError) as exc:
        st.error(f"Director Workspace 暂不可用：{str(exc)[:180]}")
        return None
    render_projection(project, projection, compact=compact)
    return projection


def render_projection(
    project: object,
    projection: DirectorWorkspaceProjection,
    *,
    compact: bool = False,
) -> None:
    """Render one supplied projection; useful for component/AppTest coverage."""

    st.markdown(
        '<span class="aidrama-director-workspace-marker" aria-hidden="true"></span>',
        unsafe_allow_html=True,
    )
    _render_workspace_summary(projection)
    if projection.state == "EMPTY" or not projection.shots:
        _render_empty_state(projection)
        return
    if projection.diagnostic:
        st.warning(projection.diagnostic)
    selected = _selected_shot(project.id, projection)
    if selected is None:
        _render_empty_state(projection)
        return
    with st.container(key="director-workspace-shell"):
        left_ratio = 1.25 if compact else 1.65
        right_ratio = 1.75 if compact else 2.25
        left, center, right = st.columns(
            [left_ratio, 4.2, right_ratio], gap="small", vertical_alignment="top"
        )
        with left:
            if not compact:
                _render_script_navigator(project.id, projection, selected)
            _render_shot_grid(project.id, projection, selected)
        with center:
            _render_preview(project.id, selected)
            _render_timeline(project.id, projection, selected)
        with right:
            _render_inspector(project.id, selected)
    if projection.state == "BLOCKED":
        st.warning(
            "当前工作台含 BLOCKED 镜头；状态来自正式 Production/QC/Review 记录。"
        )
    elif projection.state == "FINISHED":
        st.success("所有镜头已具备人工接受或显式选择的正式来源。")


def _render_workspace_summary(projection: DirectorWorkspaceProjection) -> None:
    gap_count = len(projection.gaps)
    st.markdown(
        '<section class="aidrama-director-workspace-summary" '
        'aria-label="Director Workspace summary">'
        "<div><span>DIRECTOR WORKSPACE</span>"
        f"<strong>{escape(projection.state)}</strong></div>"
        f"<div><span>SHOTS</span><strong>{len(projection.shots)}</strong></div>"
        "<div><span>DURATION</span>"
        f"<strong>{projection.total_duration_seconds:g}s / "
        f"{projection.target_duration_seconds:g}s</strong></div>"
        f"<div><span>GAPS</span><strong>{gap_count}</strong></div>"
        "</section>",
        unsafe_allow_html=True,
    )


def _render_empty_state(projection: DirectorWorkspaceProjection) -> None:
    with st.container(border=True, key="director-workspace-empty"):
        st.markdown("### Director Workspace 尚未建立")
        st.caption(
            "确认结构化剧本和 Shot Plan 后，这里会投影真实镜头、参考与制作状态。"
        )
        if projection.diagnostic:
            st.warning(projection.diagnostic)


def _render_script_navigator(
    project_id: str,
    projection: DirectorWorkspaceProjection,
    selected: WorkspaceShot,
) -> None:
    st.markdown("#### Script / Scenes")
    selected_beat = st.session_state.get(_selected_beat_key(project_id))
    scenes: dict[str, list[WorkspaceBeat]] = {}
    for beat in projection.beats:
        scenes.setdefault(beat.scene_id, []).append(beat)
    for scene_beats in scenes.values():
        scene_title = scene_beats[0].scene_title
        with st.expander(
            scene_title,
            expanded=any(selected.shot_id in beat.shot_ids for beat in scene_beats),
        ):
            for beat in scene_beats:
                label = beat.text.strip() or beat.beat_type
                if len(label) > 48:
                    label = label[:45] + "…"
                st.button(
                    label,
                    key=f"director-beat-{project_id}-{beat.beat_id}",
                    type="primary" if beat.beat_id == selected_beat else "secondary",
                    width="stretch",
                    help=(
                        f"{beat.beat_type} · 联动 {len(beat.shot_ids)} 个镜头"
                        + (" · 场景级映射" if beat.mapping_kind != "EXACT" else "")
                    ),
                    on_click=_select_beat,
                    args=(project_id, beat),
                )


def _render_shot_grid(
    project_id: str,
    projection: DirectorWorkspaceProjection,
    selected: WorkspaceShot,
) -> None:
    st.markdown("#### Shot Grid")
    for start in range(0, len(projection.shots), 2):
        columns = st.columns(2, gap="small")
        for column, shot in zip(columns, projection.shots[start : start + 2]):
            with column.container(border=True):
                state_class = shot.workspace_state.casefold().replace("_", "-")
                st.markdown(
                    f'<span class="aidrama-shot-card-marker state-{state_class}" '
                    'aria-hidden="true"></span>',
                    unsafe_allow_html=True,
                )
                st.button(
                    f"{shot.number:02d} · {shot.duration_seconds:g}s",
                    key=f"director-shot-{project_id}-{shot.shot_id}",
                    type="primary" if shot.shot_id == selected.shot_id else "secondary",
                    width="stretch",
                    on_click=_select_shot,
                    args=(
                        project_id,
                        shot.shot_id,
                        _first_beat_for_shot(projection, shot),
                    ),
                )
                st.caption(shot.scene_label)
                characters = " / ".join(shot.character_labels) or "—"
                st.caption(f"Characters · {characters}")
                st.markdown(
                    '<div class="aidrama-shot-state-stack">'
                    f"<strong>{escape(_STATE_LABELS.get(shot.workspace_state, shot.workspace_state))}</strong>"
                    f"<span>Production · {escape(shot.production_status)}</span>"
                    f"<span>QC · {escape(shot.qc_status)}</span>"
                    f"<span>Review · {escape(shot.review_status)}</span>"
                    f"<span>Final · {escape(shot.final_source_status)}</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )


def _render_preview(project_id: str, shot: WorkspaceShot) -> None:
    st.markdown("#### Preview")
    requested = st.session_state.get(_candidate_key(project_id, shot.shot_id))
    candidate = candidate_for_preview(shot, requested)
    with st.container(border=True, key="director-preview-stage"):
        st.markdown(
            '<span class="aidrama-preview-stage-marker" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        if candidate is None or candidate.preview_path is None:
            st.markdown(
                '<div class="aidrama-preview-empty" role="status">'
                "<span>NO FORMAL ARTIFACT</span>"
                "<strong>当前镜头还没有可播放的正式制作产物</strong>"
                "<small>Preview 不会从目录中猜测“最新文件”</small>"
                "</div>",
                unsafe_allow_html=True,
            )
        elif candidate.mime_type.casefold().startswith("image/"):
            st.image(str(candidate.preview_path), width="stretch")
        else:
            st.video(str(candidate.preview_path))
        preview_kind = "NO SOURCE"
        if candidate is not None:
            if candidate.is_selected_source:
                preview_kind = (
                    "FINAL SOURCE"
                    if candidate.source_decision_id
                    else "QUALIFIED SOURCE"
                )
            elif requested == candidate.candidate_id:
                preview_kind = "CANDIDATE COMPARISON"
            else:
                preview_kind = "PRODUCTION ARTIFACT"
        st.markdown(
            '<div class="aidrama-preview-overlay">'
            f"<span>{escape(preview_kind)}</span>"
            f"<strong>SHOT {shot.number:02d}</strong>"
            f"<small>{shot.timeline_start_seconds:g}s → "
            f"{shot.timeline_end_seconds:g}s</small>"
            "</div>",
            unsafe_allow_html=True,
        )


def _render_inspector(project_id: str, shot: WorkspaceShot) -> None:
    st.markdown("#### Inspector")
    candidate_key = _candidate_key(project_id, shot.shot_id)
    if candidate_key not in st.session_state and shot.preview_candidate_id:
        st.session_state[candidate_key] = shot.preview_candidate_id
    requested = st.session_state.get(candidate_key)
    candidate = candidate_for_preview(shot, requested)
    references_tab, qc_tab, candidates_tab, source_tab = st.tabs(
        ["References", "QC", "Candidates", "Source"]
    )
    with references_tab:
        references = (
            candidate.references
            if candidate and candidate.references
            else shot.references
        )
        _render_references(references)
    with qc_tab:
        _render_qc(shot, candidate)
    with candidates_tab:
        if not shot.candidates:
            st.info("当前镜头还没有 Production candidate。")
        else:
            labels = {
                item.candidate_id: (
                    f"{item.label} · "
                    + (
                        "SELECTED"
                        if item.is_selected_source
                        else item.technical_qc_status
                    )
                )
                for item in shot.candidates
            }
            st.selectbox(
                "比较候选",
                [item.candidate_id for item in shot.candidates],
                format_func=lambda value: labels[value],
                key=candidate_key,
                help="只切换预览；不会写数据库或改变正式 source decision。",
            )
            current = candidate_for_preview(shot, st.session_state.get(candidate_key))
            if current:
                st.caption(f"Technical QC · {current.technical_qc_status}")
                st.caption(f"Human Review · {current.review_status}")
                st.caption(f"Vision · {current.vision_status}")
                if current.is_selected_source:
                    st.success("当前正式来源")
                else:
                    st.info("只读候选比较；未改变正式来源")
    with source_tab:
        st.metric("Final source", shot.final_source_status)
        if candidate and candidate.is_selected_source:
            label = (
                "显式 Source Decision"
                if candidate.source_decision_id
                else "正式 qualified source"
            )
            st.success(label)
            st.caption(f"QC · {candidate.technical_qc_status}")
            st.caption(f"Review · {candidate.review_status}")
        else:
            st.info("当前没有满足正式 source priority 的来源。")


def _render_references(references: tuple[WorkspaceReference, ...]) -> None:
    if not references:
        st.info("当前镜头没有可投影的角色或场景参考。")
        return
    for reference in references:
        with st.container(border=True):
            if reference.thumbnail_path:
                st.image(str(reference.thumbnail_path), width="stretch")
            locked = "LOCKED" if reference.locked else "UNLOCKED"
            st.markdown(f"**{reference.binding_kind} · {reference.label}**")
            st.caption(
                f"{locked} · Version {reference.version_number} · {reference.provenance}"
            )
            with st.expander("Exact version identity", expanded=False):
                st.code(reference.version_id, language=None)


def _render_qc(shot: WorkspaceShot, candidate: WorkspaceCandidate | None) -> None:
    if candidate is None:
        st.info("Technical QC 尚未开始。")
    else:
        st.markdown(f"**Technical QC · {candidate.technical_qc_status}**")
        if candidate.technical_qc_metrics:
            for metric in candidate.technical_qc_metrics:
                st.caption(f"{metric.status} · {metric.name} · {metric.message}")
        else:
            st.caption("暂无技术指标明细。")
        st.markdown(f"**Human Review · {candidate.review_status}**")
        st.markdown(f"**Vision advisory · {candidate.vision_status}**")
        if candidate.vision_metrics:
            for name, value in list(candidate.vision_metrics.items())[:8]:
                st.caption(f"{name} · {_display_value(value)}")
        else:
            st.caption("没有可用的 Vision advisory 记录。")
    if shot.continuity.available:
        st.markdown(f"**Continuity · {shot.continuity.status}**")
        if shot.continuity.warnings:
            for warning in shot.continuity.warnings:
                st.warning(warning)
        else:
            st.caption("未投影到连续性警告。")
    else:
        st.markdown("**Continuity · NOT AVAILABLE**")
        st.caption("已保留 optional projection adapter；未复制连续性业务逻辑。")


def _display_value(value: object) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in list(value.items())[:4])[
            :180
        ]
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value[:4])[:180]
    return str(value)[:180]


def _render_timeline(
    project_id: str,
    projection: DirectorWorkspaceProjection,
    selected: WorkspaceShot,
) -> None:
    st.markdown("#### Timeline / Shot Strip")
    timeline_html = ['<div class="aidrama-director-timeline" role="list">']
    for segment in projection.timeline:
        active = " is-selected" if segment.shot_id == selected.shot_id else ""
        grow = max(segment.duration_seconds, 0.1)
        timeline_html.append(
            f'<div class="aidrama-timeline-segment{active}" role="listitem" '
            f'style="flex-grow:{grow:g}">'
            f"<strong>{segment.order:02d}</strong>"
            f"<span>{segment.duration_seconds:g}s</span>"
            f"<small>{segment.start_seconds:g}–{segment.end_seconds:g}</small>"
            "</div>"
        )
    for gap in projection.gaps:
        if gap.duration_seconds > 0:
            timeline_html.append(
                '<div class="aidrama-timeline-gap" role="listitem" '
                f'style="flex-grow:{max(gap.duration_seconds, 0.1):g}">'
                f"<strong>GAP</strong><span>{gap.duration_seconds:g}s</span></div>"
            )
    timeline_html.append("</div>")
    st.markdown("".join(timeline_html), unsafe_allow_html=True)
    st.caption(
        f"Shot ordering + duration · Total {projection.total_duration_seconds:g}s"
    )
    with st.container(
        key="director-timeline-buttons", horizontal=True, gap="small"
    ):
        for shot in projection.shots:
            st.button(
                f"{shot.number:02d} · {shot.timeline_start_seconds:g}s",
                key=f"timeline-shot-{project_id}-{shot.shot_id}",
                type="primary" if shot.shot_id == selected.shot_id else "secondary",
                width="content",
                on_click=_select_shot,
                args=(
                    project_id,
                    shot.shot_id,
                    _first_beat_for_shot(projection, shot),
                ),
            )
    if projection.gaps:
        with st.expander(f"Gaps · {len(projection.gaps)}", expanded=True):
            for gap in projection.gaps:
                st.warning(gap.label)


def is_media_file(path: Path | None) -> bool:
    """Small component seam retained for focused path/empty-state tests."""

    return bool(path and path.is_file() and path.stat().st_size > 0)


__all__ = [
    "candidate_for_preview",
    "is_media_file",
    "render",
    "render_auto_foundation",
    "render_mode_selector",
    "render_projection",
]
