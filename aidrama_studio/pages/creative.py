"""Unified Creative Intake workspace.

The Creative page is deliberately a single canvas.  Source Pack import,
local analysis and brief normalization are all local, project-scoped actions;
the page does not call a provider or pretend that an AI job is running.  A
normalized brief is the hand-off artifact consumed by the Story workspace.
"""

from __future__ import annotations

from collections.abc import Iterable

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import (
    current_project_or_stop,
    render_automation_mode,
    render_project_context,
)
from aidrama_studio.services import (
    CreativeIntakeError,
    CreativeIntakeService,
)


_SUPPORTED_UPLOADS = [
    "txt",
    "md",
    "pdf",
    "docx",
    "pptx",
    "png",
    "jpg",
    "jpeg",
    "webp",
]

_SOURCE_KIND_LABELS = {
    "TEXT_BRIEF": "创意文本",
    "DOCUMENT": "文档",
    "IMAGE": "视觉参考",
    "STORYBOARD_IMAGE": "分镜参考",
    "OTHER_SUPPORTED_SOURCE": "素材",
}
_EXTRACTION_LABELS = {
    "PENDING": "待提取",
    "EXTRACTED": "已读取",
    "WARNING": "需要留意",
    "FAILED": "读取失败",
}
_CLASSIFICATION_LABELS = {
    "VISUAL_REFERENCE": "视觉参考",
    "SCRIPT": "剧本内容",
    "SHOT_LIST": "镜头清单",
    "CHARACTER_BIBLE": "角色设定",
    "CREATIVE_BRIEF": "创意 Brief",
    "UNKNOWN": "待人工判断",
}


def _key(project_id: str, name: str) -> str:
    return f"creative-{project_id}-{name}"


def _enum_value(value: object, default: str = "") -> str:
    raw = getattr(value, "value", value)
    return str(raw or default)


def _safe_error(value: object, default: str = "操作未完成，请稍后重试。") -> str:
    """Keep parser/service diagnostics out of the normal creative surface."""

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    lowered = text.casefold()
    if any(marker in lowered for marker in ("provider", "endpoint", "api key", "traceback")):
        return default
    if ":\\" in text or ":/" in text or "sha-256" in lowered:
        return default
    return text[:180] or default


def _source_name(item: object) -> str:
    return str(getattr(item, "display_filename", "未命名素材"))


def _latest_brief(service: CreativeIntakeService, project_id: str):
    try:
        briefs = list(service.repository.list_normalized_creative_briefs(project_id))
    except Exception:
        logger.exception("failed to read normalized creative briefs")
        return None
    return briefs[-1] if briefs else None


def _render_activity_strip(project) -> None:
    """Show durable/local activity without replacing the active workspace.

    Story/Script generation will gain a durable activity adapter later.  Until
    then this small strip only reflects explicit local actions recorded by this
    page; it never fabricates a percentage or provider state.
    """

    activity = st.session_state.get(_key(project.id, "activity"))
    if not isinstance(activity, dict):
        return
    label = str(activity.get("label") or "创意工作区")
    state = str(activity.get("state") or "saved")
    state_label = {
        "saved": "已保存",
        "ready": "可继续",
        "failed": "需要处理",
    }.get(state, "已记录")
    with st.container(border=True):
        left, right = st.columns([5, 1])
        left.markdown(f"**{label}**")
        left.caption("工作区保持可编辑；本地来源和 Brief 不会被覆盖。")
        right.metric("状态", state_label)


def _remember_activity(project, label: str, state: str = "saved") -> None:
    st.session_state[_key(project.id, "activity")] = {
        "label": label,
        "state": state,
    }


def _render_source_item(project, service: CreativeIntakeService, item, analysis) -> None:
    kind = _SOURCE_KIND_LABELS.get(_enum_value(getattr(item, "source_kind", "")), "素材")
    extraction = _EXTRACTION_LABELS.get(
        _enum_value(getattr(item, "extraction_state", "")), "状态未知"
    )
    with st.container(border=True):
        title_col, action_col = st.columns([5, 1])
        title_col.markdown(f"**{_source_name(item)}**")
        title_col.caption(f"{kind} · {extraction}")
        if analysis is not None:
            classes = getattr(analysis, "classifications", ()) or ()
            if classes:
                title_col.caption(
                    "已识别 · "
                    + " / ".join(_CLASSIFICATION_LABELS.get(str(value), "素材") for value in classes)
                )
        if action_col.button("分析", key=_key(project.id, f"analyze-{item.id}")):
            try:
                service.analyzer.analyze(project.id, item.id)
                _remember_activity(project, f"已分析「{_source_name(item)}」", "ready")
                st.rerun()
            except Exception as exc:
                st.error(_safe_error(exc, "素材分析失败，请稍后重试。"))

        extracted = getattr(item, "extracted_text", None)
        if extracted:
            # Source text is creative content and useful in the normal UI; the
            # parser/provenance details remain below the Advanced disclosure.
            with st.expander("查看来源文本（仅本地）", expanded=False):
                st.text(str(extracted)[:8000])
        with st.expander("高级来源信息", expanded=False):
            size = getattr(item, "size_bytes", "—")
            digest = str(getattr(item, "sha256", ""))
            mime = getattr(item, "mime_type", "—")
            storage = getattr(item, "storage_path", "—")
            st.caption(f"类型 {kind} · MIME {mime} · {size} bytes")
            if digest:
                st.caption(f"SHA-256 {digest[:12]}…")
            st.caption(f"Source id {getattr(item, 'id', '—')} · 路径 {storage}")


def _render_source_pack(project, service: CreativeIntakeService) -> list[object]:
    """Render project-isolated Source Pack controls and return its items."""

    st.markdown("### Source Pack")
    st.caption("文档、图片和已有故事集中保留在当前项目；原始来源不会被 AI 草稿覆盖。")

    idea_key = _key(project.id, "idea")
    idea = st.text_area(
        "一句话创意",
        value=st.session_state.get(idea_key, project.description or ""),
        key=idea_key,
        height=110,
        placeholder="例如：失忆的末班车司机，在终点站遇见未来的自己。",
    )
    add_col, hint_col = st.columns([1, 2])
    if add_col.button(
        "加入来源",
        key=_key(project.id, "add-idea"),
        disabled=not idea.strip(),
        use_container_width=True,
    ):
        try:
            service.source_pack.import_text(project.id, idea, filename="creative-idea.txt")
            _remember_activity(project, "一句话创意已加入 Source Pack")
            st.toast("创意文本已安全加入 Source Pack")
            st.rerun()
        except Exception as exc:
            st.error(_safe_error(exc, "创意文本导入失败，请检查内容后重试。"))
    hint_col.caption("可以先写一句话，也可以直接导入已有剧本、策划文档或参考图片。")

    uploads = st.file_uploader(
        "导入规划文档 / 剧本 / 分镜 / 参考图片",
        type=_SUPPORTED_UPLOADS,
        accept_multiple_files=True,
        key=_key(project.id, "files"),
        help="支持多选；文件会在本机校验、提取并保存到当前项目。",
    )
    if st.button(
        "导入所选文件",
        key=_key(project.id, "import-files"),
        disabled=not uploads,
        use_container_width=True,
    ):
        imported = 0
        failures: list[str] = []
        for upload in uploads or []:
            try:
                service.source_pack.import_bytes(
                    project.id,
                    upload.name,
                    upload.getvalue(),
                    mime_type=upload.type or None,
                )
                imported += 1
            except Exception as exc:
                failures.append(
                    f"{upload.name}：{_safe_error(exc, '文件暂时无法导入。')}"
                )
        if imported:
            _remember_activity(project, f"已导入 {imported} 项素材")
            st.toast(f"已处理 {imported} 个 Source Pack 文件")
        for failure in failures:
            st.warning(failure)
        if imported:
            st.rerun()

    try:
        items = list(service.source_pack.list(project.id))
    except Exception as exc:
        st.error(_safe_error(exc, "来源暂时无法读取，请稍后重试。"))
        return []
    if not items:
        st.info("Source Pack 还是空的。先输入一句创意，或导入已有素材。")
        return []

    analyses = {}
    try:
        analyses = {
            item.source_id: item
            for item in service.repository.list_intake_analyses(project.id)
        }
    except Exception:
        logger.exception("failed to read intake analyses")
    st.markdown(f"#### 已加入的素材 · {len(items)} 项")
    for item in items:
        _render_source_item(project, service, item, analyses.get(item.id))
    return items


def _brief_defaults(project, latest) -> dict[str, object]:
    content = {
        "premise": project.description or "",
        "genre": "",
        "tone": "",
        "audience": "",
        "constraints": "",
        "visual": "",
        "duration": int(getattr(project, "target_duration_seconds", 60) or 60),
        "ratio": _enum_value(getattr(project, "aspect_ratio", "16:9"), "16:9"),
    }
    if latest is not None:
        content["premise"] = getattr(latest, "premise", "") or content["premise"]
        content["genre"] = getattr(latest, "genre", "") or ""
        content["tone"] = getattr(latest, "tone", "") or ""
        constraints = getattr(latest, "constraints", ()) or ()
        content["constraints"] = "\n".join(str(value) for value in constraints)
        direction = getattr(latest, "visual_direction", {}) or {}
        if isinstance(direction, dict):
            content["visual"] = str(
                direction.get("keywords")
                or direction.get("description")
                or direction.get("visual")
                or ""
            )
        information = getattr(latest, "story_information", {}) or {}
        if isinstance(information, dict):
            content["audience"] = str(information.get("audience") or "")
            if information.get("target_duration_seconds"):
                try:
                    content["duration"] = int(information["target_duration_seconds"])
                except (TypeError, ValueError):
                    pass
            content["ratio"] = str(information.get("aspect_ratio") or content["ratio"])
    return content


def _render_brief(project, service: CreativeIntakeService, items: Iterable[object], latest) -> None:
    defaults = _brief_defaults(project, latest)
    st.markdown("### Creative Brief")
    st.caption("Brief 是故事设定的人工确认入口；规范化只在本机整理来源，不会提交生成请求。")

    # Keep widget defaults project-scoped so switching projects cannot leak a
    # previous project's creative text into the current workspace.
    premise = st.text_area(
        "故事 / 大纲",
        value=str(defaults["premise"]),
        key=_key(project.id, "premise"),
        height=145,
        placeholder="可以粘贴已有故事，也可以从一句话继续展开。",
    )
    c1, c2, c3 = st.columns(3)
    genre = c1.text_input("类型", value=str(defaults["genre"]), key=_key(project.id, "genre"))
    tone = c2.text_input("基调", value=str(defaults["tone"]), key=_key(project.id, "tone"))
    audience = c3.text_input(
        "目标受众（可选）",
        value=str(defaults["audience"]),
        key=_key(project.id, "audience"),
    )
    c4, c5 = st.columns([1, 2])
    duration = c4.number_input(
        "目标时长（秒）",
        min_value=1,
        max_value=3600,
        value=int(defaults["duration"]),
        key=_key(project.id, "duration"),
    )
    ratio = c5.selectbox(
        "画幅",
        options=["16:9", "9:16", "1:1"],
        index=(
            ["16:9", "9:16", "1:1"].index(str(defaults["ratio"]))
            if str(defaults["ratio"]) in {"16:9", "9:16", "1:1"}
            else 0
        ),
        key=_key(project.id, "ratio"),
    )
    c6, c7 = st.columns(2)
    conflict = c6.text_area(
        "核心冲突（可选）",
        value=st.session_state.get(_key(project.id, "conflict"), ""),
        key=_key(project.id, "conflict"),
        height=95,
    )
    visual = c7.text_area(
        "视觉关键词（可选）",
        value=str(defaults["visual"]),
        key=_key(project.id, "visual"),
        height=95,
    )
    constraints = st.text_area(
        "创作约束（可选，每行一条）",
        value=str(defaults["constraints"]),
        key=_key(project.id, "constraints"),
        height=90,
    )

    selected_ids = [getattr(item, "id", "") for item in items]
    labels = {getattr(item, "id", ""): _source_name(item) for item in items}
    if selected_ids:
        selected = st.multiselect(
            "用于整理 Brief 的来源",
            options=selected_ids,
            default=selected_ids,
            format_func=lambda source_id: labels.get(source_id, "未命名素材"),
            key=_key(project.id, "selected-sources"),
        )
    else:
        selected = []
        st.caption("添加至少一项来源后可规范化 Brief；一句话创意可先加入 Source Pack。")

    # The dominant action changes with state: normalize first, then hand off
    # the confirmed draft.  Import, source selection and diagnostics remain
    # contextual secondary actions.
    force_normalize = bool(st.session_state.get(_key(project.id, "force-normalize"), False))
    normalized = latest is not None and not force_normalize
    action_label = "确认创意并进入故事" if normalized else "规范化创意"
    action_disabled = not premise.strip() or not selected
    if st.button(
        action_label,
        type="primary",
        use_container_width=True,
        key=_key(project.id, "primary"),
        disabled=action_disabled,
    ):
        if not normalized:
            try:
                brief = service.normalize(
                    project.id,
                    source_ids=selected,
                    overrides={
                        "premise": premise.strip(),
                        "genre": genre.strip(),
                        "tone": tone.strip(),
                        "constraints": tuple(
                            line.strip() for line in constraints.splitlines() if line.strip()
                        ),
                        "story_information": {
                            "audience": audience.strip(),
                            "conflict": conflict.strip(),
                            "target_duration_seconds": int(duration),
                            "aspect_ratio": ratio,
                        },
                        "visual_direction": {"keywords": visual.strip()},
                    },
                )
                st.session_state[_key(project.id, "normalized-id")] = brief.id
                st.session_state[_key(project.id, "force-normalize")] = False
                _remember_activity(project, "Creative Brief 草稿已准备", "ready")
                st.toast("Creative Brief 已规范化；请确认后进入故事设定")
                st.rerun()
            except (CreativeIntakeError, ValueError) as exc:
                st.error(_safe_error(exc, "规范化失败，请检查来源和 Brief 内容。"))
            except Exception:
                logger.exception("creative brief normalization failed")
                st.error("规范化失败，请检查来源和 Brief 内容。")
        else:
            st.session_state[_key(project.id, "confirmed")] = True
            _remember_activity(project, "Creative Brief 已确认", "ready")
            st.toast("创意已确认，可以继续建立 Story Bible")
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("story")

    if normalized:
        st.caption("已保存的 Brief 可在这里继续调整；修改后再次点击“规范化创意”会创建新的本地版本。")
        if st.button("重新整理为新版本", key=_key(project.id, "renormalize")):
            # Reset only the local view marker; the next primary action will
            # create a new immutable normalized-brief record.
            st.session_state[_key(project.id, "force-normalize")] = True
            st.rerun()


def _render_provenance(project, service: CreativeIntakeService, latest) -> None:
    if latest is None:
        return
    source_ids = tuple(getattr(latest, "source_ids", ()) or ())
    with st.expander("来源记录 / 版本历史", expanded=False):
        status = _enum_value(getattr(latest, "status", "DRAFT"), "DRAFT")
        status_label = {"DRAFT": "草稿", "APPROVED": "已确认"}.get(status, "已保存")
        st.caption(f"Creative Brief · {len(source_ids)} 项来源 · {status_label}")
        try:
            source_items = {item.id: item for item in service.source_pack.list(project.id)}
        except Exception:
            source_items = {}
        for source_id in source_ids:
            item = source_items.get(source_id)
            if item is not None:
                st.markdown(f"- {_source_name(item)}")
            else:
                st.markdown("- 来源已归档")
        st.caption("更详细的来源 ID、解析状态和校验摘要位于每个素材卡片的高级信息中。")


def render() -> None:
    page_header(
        "创意",
        "CREATIVE INTAKE WORKSPACE",
        "把一句话、已有故事、文档和视觉参考整理成可确认的创作起点。",
    )
    project = current_project_or_stop()
    render_project_context(
        project,
        stage="创意",
        next_action="整理 Creative Brief",
        next_page="creative",
    )
    _render_activity_strip(project)
    render_automation_mode(project.id, compact=True)

    service = CreativeIntakeService()
    latest = _latest_brief(service, project.id)
    left, right = st.columns([1.03, 0.97], gap="large")
    with left:
        items = _render_source_pack(project, service)
    with right:
        _render_brief(project, service, items, latest)
    _render_provenance(project, service, latest)


__all__ = ["render"]
