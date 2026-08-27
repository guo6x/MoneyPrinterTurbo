from __future__ import annotations

from copy import deepcopy

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    Character,
    Location,
    StoryBible,
    StoryRevisionStatus,
    ScriptRevisionStatus,
    ScriptBeatType,
    InteriorExterior,
    TimeOfDay,
    Scene,
    ScriptBeat,
    StructuredScript,
)
from aidrama_studio.pages._shared import (
    current_project_or_stop,
    render_ai_readiness,
    render_project_context,
)
from aidrama_studio.storage import ProjectRepository
from aidrama_studio.services import (
    DependencyStatusService,
    ScriptService,
    StoryService,
    StoryServiceError,
)
from aidrama_studio.services.security import sanitize_error


BEAT_TYPES = ["OPENING", "DEVELOPMENT", "TURNING_POINT", "CLIMAX", "ENDING"]
BEAT_TYPE_LABELS = {
    "OPENING": "开端",
    "DEVELOPMENT": "发展",
    "TURNING_POINT": "转折",
    "CLIMAX": "高潮",
    "ENDING": "结尾",
}


def _safe_error(exc: object, *, fallback: str = "操作未完成") -> str:
    """Keep normal Story copy concise and redact paths/diagnostic payloads."""

    detail = sanitize_error(exc, max_length=180)
    return detail or fallback


def _status_value(value: object, default: str = "") -> str:
    """Normalize enum-like and fixture string statuses at the UI boundary."""

    raw = getattr(value, "value", value)
    return str(raw or default).strip().upper()


def _story_status(value: object) -> StoryRevisionStatus | None:
    try:
        return StoryRevisionStatus(_status_value(value))
    except (TypeError, ValueError):
        return None


def _script_status(value: object) -> ScriptRevisionStatus | None:
    try:
        return ScriptRevisionStatus(_status_value(value))
    except (TypeError, ValueError):
        return None


def _enqueue_activity(
    project, operation: str, payload: dict[str, object]
) -> tuple[bool, object | None]:
    """Use an optional durable activity adapter without inventing a runtime API.

    The runtime owner can install a callable in session state during
    integration.  In source mode, no callable means the request is clearly
    marked as pending/unavailable and no provider or synchronous facade is
    invoked.
    """
    adapter = st.session_state.get("_aidrama_activity_adapter")
    if not callable(adapter):
        _set_story_state(
            project,
            "activity",
            {
                "operation": operation,
                "state": "pending",
                "message": "后台生成能力尚未连接；当前编辑器保持可用。",
            },
        )
        return False, None
    try:
        result = adapter(project_id=project.id, operation=operation, payload=payload)
    except TypeError:
        # Keep the seam tolerant of the small positional fixture used by UI
        # tests; this still never falls back to a provider call.
        result = adapter(project.id, operation, payload)
    except Exception as exc:
        logger.exception("activity adapter rejected %s", operation)
        _set_story_state(
            project,
            "activity",
            {
                "operation": operation,
                "state": "failed",
                "message": _safe_error(exc, fallback="后台生成活动暂未完成"),
            },
        )
        return False, None
    _set_story_state(
        project,
        "activity",
        {
            "operation": operation,
            "state": "queued",
            "message": "请求已加入后台活动；你仍可继续编辑当前工作区。",
        },
    )
    return True, result


def _story_state(project, name: str, default=None):
    """Read project-scoped Story UI state with legacy-key compatibility."""

    scoped = f"story-{name}-{project.id}"
    if scoped not in st.session_state:
        legacy = {
            "revision": "story_revision_id",
            "script-revision": "script_revision_id",
            "workspace": "story_workspace",
            "activity": "story_activity",
        }.get(name)
        if legacy and legacy in st.session_state:
            st.session_state[scoped] = st.session_state[legacy]
    return st.session_state.get(scoped, default)


def _set_story_state(project, name: str, value) -> None:
    scoped = f"story-{name}-{project.id}"
    st.session_state[scoped] = value
    legacy = {
        "revision": "story_revision_id",
        "script-revision": "script_revision_id",
        "workspace": "story_workspace",
        "activity": "story_activity",
    }.get(name)
    if legacy:
        st.session_state[legacy] = value


def _render_activity_notice(project=None) -> None:
    activity = (
        _story_state(project, "activity")
        if project is not None
        else st.session_state.get("story_activity")
    )
    if not isinstance(activity, dict):
        return
    state = str(activity.get("state") or "pending")
    message = str(activity.get("message") or "后台活动状态待同步。")
    if state in {"queued", "running"}:
        st.info(message)
    elif state == "failed":
        st.warning("后台活动暂未完成：" + message)
    else:
        st.caption(message)


def _go_settings() -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation("settings")


def _working_key(revision_id: str) -> str:
    return f"story_working_{revision_id}"


def _revision_label(revision: dict) -> str:
    status_value = _status_value(revision.get("status"))
    label = {
        "DRAFT": "草稿",
        "APPROVED": "已确认",
        "SUPERSEDED": "需要更新",
    }.get(str(status_value), "状态未知")
    return f"第 {revision['version']} 版 · {label}"


def _get_working(revision: dict) -> dict:
    key = _working_key(revision["id"])
    if key not in st.session_state:
        content = revision.get("content")
        dumper = getattr(content, "model_dump", None)
        if callable(dumper):
            content = dumper(mode="python")
        elif isinstance(content, dict):
            content = deepcopy(content)
        else:
            content = {}
        st.session_state[key] = content
    return st.session_state[key]


def _input(label: str, value: str, key: str, *, area: bool = False, **kwargs) -> str:
    if area:
        return st.text_area(label, value=value, key=key, **kwargs)
    return st.text_input(label, value=value, key=key, **kwargs)


def _list_text(values: list[str], separator: str = ", ") -> str:
    return separator.join(values)


def _parse_list(value: str, separator: str = ",") -> list[str]:
    return [
        item.strip()
        for item in value.replace("，", separator).split(separator)
        if item.strip()
    ]


def _latest_normalized_brief(project_id: str, repository=None):
    """Read the durable Creative hand-off, never rely on session-only fields."""
    try:
        briefs = (repository or ProjectRepository()).list_normalized_creative_briefs(
            project_id
        )
        return briefs[-1] if briefs else None
    except Exception:
        # Creative is optional while a project is being bootstrapped.  Story
        # remains usable with a blank/manual draft if the hand-off is absent.
        logger.exception("failed to read normalized Creative Brief")
        return None


def _brief_generation_input(project, normalized) -> dict[str, object]:
    """Build a safe generation input projection from the durable Brief."""
    def value(name: str, default=None):
        if normalized is None:
            return default
        if isinstance(normalized, dict):
            return normalized.get(name, default)
        return getattr(normalized, name, default)

    premise = value("premise", "")
    genre = value("genre", "")
    tone = value("tone", "")
    information = (
        value("story_information", {})
    )
    if not isinstance(information, dict):
        information = {}
    constraints = (
        value("constraints", ())
    )
    return {
        "brief": str(premise or project.description or "").strip(),
        "genre": str(genre or "").strip(),
        "tone": str(tone or "").strip(),
        "target_audience": str(information.get("audience") or "").strip(),
        "creative_constraints": "\n".join(str(item) for item in (constraints or ())),
        "source_ids": tuple(value("source_ids", ()) or ()),
        "normalized_brief_id": value("id"),
    }


def _render_brief(project, service: StoryService) -> None:
    """Show the Creative hand-off and the Story Bible creation action.

    Creative owns editable intake fields.  Story only consumes the durable
    normalized Brief and keeps a small, read-only summary here so a cold
    navigation/reload cannot lose the hand-off.
    """
    st.markdown("### 创作起点")
    normalized = _latest_normalized_brief(project.id, service.repository)
    if normalized is None:
        st.warning(
            "还没有已保存的 Creative Brief。先在“创意”工作区整理并确认创作起点。"
        )
        if st.button("去整理 Creative Brief", key=f"story-open-creative-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("creative")
        generation_input = _brief_generation_input(project, None)
    else:
        st.success("Creative Brief 已保存，可用于建立 Story Bible。")
        with st.container(border=True):
            st.markdown(
                f"**{getattr(normalized, 'title_candidate', '') or '未命名创意'}**"
            )
            st.write(getattr(normalized, "premise", "") or "暂无故事梗概")
            summary_cols = st.columns(4)
            summary_cols[0].metric("类型", getattr(normalized, "genre", "") or "未填写")
            summary_cols[1].metric("基调", getattr(normalized, "tone", "") or "未填写")
            information = getattr(normalized, "story_information", {}) or {}
            if not isinstance(information, dict):
                information = {}
            summary_cols[2].metric("目标受众", information.get("audience") or "未填写")
            summary_cols[3].metric(
                "来源",
                f"{len(getattr(normalized, 'source_ids', ()) or ())} 项",
            )
            with st.expander("查看 Brief 约束与视觉方向", expanded=False):
                constraints = getattr(normalized, "constraints", ()) or ()
                st.write("\n".join(f"- {item}" for item in constraints) or "未填写约束")
                direction = getattr(normalized, "visual_direction", {}) or {}
                st.caption(
                    str(
                        direction.get("keywords")
                        or direction.get("description")
                        or "未填写视觉方向"
                    )
                )
        generation_input = _brief_generation_input(project, normalized)

    ready, detail = service.llm_readiness(project.id)
    revisions = service.list_revisions(project.id)
    if not ready:
        st.info(
            "文本生成能力尚未配置。你仍可创建空白 Story Bible 手动编辑；需要 AI 草稿时再去设置。"
        )
        if st.button("去设置能力", key=f"story-settings-{project.id}"):
            _go_settings()
    if detail:
        st.caption("生成能力状态：请在设置中查看可用能力。")

    has_draft = any(
        _story_status(item.get("status")) is StoryRevisionStatus.DRAFT
        for item in revisions
    )
    if not revisions:
        primary_label = (
            "生成 Story Bible 草稿"
            if ready and generation_input["brief"]
            else "创建空白 Story Bible"
        )
    elif has_draft:
        primary_label = "继续编辑当前 Story Bible"
    else:
            primary_label = "从已确认版本创建新草稿"
    if st.button(
        primary_label,
        type="primary",
        use_container_width=True,
        key=f"story-primary-{project.id}",
    ):
        try:
            if not revisions:
                if ready and generation_input["brief"]:
                    queued, result = _enqueue_activity(
                        project,
                        "STORY_BIBLE_GENERATION",
                        generation_input,
                    )
                    # An integrated activity adapter may return a durable
                    # revision immediately (for deterministic/offline
                    # fixtures).  Otherwise the editor stays mounted while
                    # the runtime owner completes the queued work.
                    if not queued:
                        _render_activity_notice(project)
                        return
                    if isinstance(result, dict) and result.get("id"):
                        _set_story_state(project, "revision", result["id"])
                        st.toast("Story Bible 草稿已加入后台活动")
                        st.rerun()
                    st.toast("Story Bible 生成请求已加入后台活动")
                    return
                else:
                    revision = service.create_blank_draft(project)
                _set_story_state(project, "revision", revision["id"])
                st.toast("Story Bible 草稿已准备")
                st.rerun()
            elif has_draft:
                draft = service.get_latest_draft(project.id)
                if draft:
                    _set_story_state(project, "revision", draft["id"])
                    st.rerun()
            else:
                approved = next(
                    item
                    for item in revisions
                    if _story_status(item.get("status")) is StoryRevisionStatus.APPROVED
                )
                forked = service.create_revision_from_approved(approved["id"])
                _set_story_state(project, "revision", forked["id"])
                st.rerun()
        except StoryServiceError as exc:
            st.error(_safe_error(exc, fallback="Story Bible 草稿准备失败，请稍后重试。"))
        except Exception:
            logger.exception("story bible preparation failed")
            st.error("Story Bible 草稿准备失败，请稍后重试。")


def _render_overview(working: dict, revision_id: str) -> None:
    st.markdown("#### 故事概览")
    working["title"] = _input("标题", working["title"], f"{revision_id}-title")
    working["logline"] = _input(
        "Logline", working["logline"], f"{revision_id}-logline", area=True, height=90
    )
    working["premise"] = _input(
        "故事前提", working["premise"], f"{revision_id}-premise", area=True, height=115
    )
    left, right = st.columns(2)
    with left:
        working["genre"] = _input("类型", working["genre"], f"{revision_id}-genre")
        working["tone"] = _input("基调", working["tone"], f"{revision_id}-tone")
    with right:
        themes = _input(
            "主题（逗号分隔）", _list_text(working["themes"]), f"{revision_id}-themes"
        )
        working["themes"] = _parse_list(themes)
    st.markdown("#### 世界观")
    world = working["world"]
    left, right = st.columns(2)
    with left:
        world["era"] = _input("时代", world["era"], f"{revision_id}-world-era")
        world["setting"] = _input(
            "世界设定",
            world["setting"],
            f"{revision_id}-world-setting",
            area=True,
            height=100,
        )
    with right:
        rules = _input(
            "世界规则（每行一条）",
            "\n".join(world["rules"]),
            f"{revision_id}-world-rules",
            area=True,
            height=100,
        )
        world["rules"] = [line.strip() for line in rules.splitlines() if line.strip()]
        world["timeline_notes"] = _input(
            "时间线备注",
            world["timeline_notes"],
            f"{revision_id}-world-timeline",
            area=True,
            height=80,
        )


def _render_characters(working: dict, revision_id: str) -> None:
    st.markdown("#### 人物")
    st.caption("人物被剧情节拍引用时不能直接删除，以免留下悬空引用。")
    remove_id = None
    for character in working["characters"]:
        with st.container(border=True):
            top_left, top_right = st.columns([4, 1])
            top_left.markdown(f"**{character['name'] or '未命名人物'}**")
            if top_right.button(
                "移除", key=f"remove-char-{revision_id}-{character['id']}"
            ):
                remove_id = character["id"]
            character["name"] = _input(
                "姓名", character["name"], f"{revision_id}-{character['id']}-name"
            )
            character["role"] = _input(
                "角色定位", character["role"], f"{revision_id}-{character['id']}-role"
            )
            character["identity"] = _input(
                "身份",
                character["identity"],
                f"{revision_id}-{character['id']}-identity",
                area=True,
                height=70,
            )
            character["personality"] = _input(
                "性格",
                character["personality"],
                f"{revision_id}-{character['id']}-personality",
                area=True,
                height=70,
            )
            character["appearance"] = _input(
                "外观",
                character["appearance"],
                f"{revision_id}-{character['id']}-appearance",
                area=True,
                height=70,
            )
            character["motivation"] = _input(
                "动机",
                character["motivation"],
                f"{revision_id}-{character['id']}-motivation",
                area=True,
                height=70,
            )
            character["relationship_notes"] = _input(
                "关系备注",
                character["relationship_notes"],
                f"{revision_id}-{character['id']}-relationships",
                area=True,
                height=70,
            )
            character["speech_style"] = _input(
                "说话方式",
                character["speech_style"],
                f"{revision_id}-{character['id']}-speech",
            )
            character["age_or_range"] = _input(
                "年龄段",
                character["age_or_range"],
                f"{revision_id}-{character['id']}-age",
            )
    if remove_id:
        referenced = any(
            remove_id in beat["characters"] for beat in working["story_beats"]
        )
        if referenced:
            st.error("该人物仍被剧情节拍引用，请先解除引用后再移除。")
        elif len(working["characters"]) <= 1:
            st.error("Story Bible 至少需要保留一个人物。")
        else:
            working["characters"] = [
                item for item in working["characters"] if item["id"] != remove_id
            ]
            st.rerun()
    if st.button("+ 添加人物", key=f"add-char-{revision_id}"):
        index = len(working["characters"]) + 1
        working["characters"].append(
            Character(id=f"char_{index:03d}", name=f"新人物 {index}").model_dump()
        )
        st.rerun()


def _render_locations(working: dict, revision_id: str) -> None:
    st.markdown("#### 场景")
    st.caption("被剧情节拍引用的场景不能直接删除，避免留下悬空引用。")
    remove_id = None
    for location in working["locations"]:
        with st.container(border=True):
            top_left, top_right = st.columns([4, 1])
            top_left.markdown(f"**{location['name'] or '未命名场景'}**")
            if top_right.button(
                "移除", key=f"remove-loc-{revision_id}-{location['id']}"
            ):
                remove_id = location["id"]
            location["name"] = _input(
                "名称", location["name"], f"{revision_id}-{location['id']}-name"
            )
            location["function"] = _input(
                "叙事功能",
                location["function"],
                f"{revision_id}-{location['id']}-function",
                area=True,
                height=70,
            )
            left, right = st.columns(2)
            with left:
                location["environment"] = _input(
                    "环境",
                    location["environment"],
                    f"{revision_id}-{location['id']}-environment",
                    area=True,
                    height=80,
                )
                location["visual_style"] = _input(
                    "视觉风格",
                    location["visual_style"],
                    f"{revision_id}-{location['id']}-visual",
                    area=True,
                    height=80,
                )
            with right:
                location["time_of_day"] = _input(
                    "时间",
                    location["time_of_day"],
                    f"{revision_id}-{location['id']}-time",
                )
                props = _input(
                    "关键道具（逗号分隔）",
                    _list_text(location["key_props"]),
                    f"{revision_id}-{location['id']}-props",
                )
                location["key_props"] = _parse_list(props)
    if remove_id:
        referenced = any(
            remove_id == beat["location_id"] for beat in working["story_beats"]
        )
        if referenced:
            st.error("该场景仍被剧情节拍引用，请先解除引用后再移除。")
        elif len(working["locations"]) <= 1:
            st.error("Story Bible 至少需要保留一个场景。")
        else:
            working["locations"] = [
                item for item in working["locations"] if item["id"] != remove_id
            ]
            st.rerun()
    if st.button("+ 添加场景", key=f"add-loc-{revision_id}"):
        index = len(working["locations"]) + 1
        working["locations"].append(
            Location(id=f"loc_{index:03d}", name=f"新场景 {index}").model_dump()
        )
        st.rerun()


def _render_beats(working: dict, revision_id: str) -> None:
    st.markdown("#### 剧情结构")
    st.caption("顺序可直接编辑；保存时会检查人物和场景引用。")
    character_options = [item["id"] for item in working["characters"]]
    location_options = [item["id"] for item in working["locations"]]
    character_labels = {
        item["id"]: item.get("name") or "未命名人物" for item in working["characters"]
    }
    location_labels = {
        item["id"]: item.get("name") or "未命名场景" for item in working["locations"]
    }
    for beat in sorted(working["story_beats"], key=lambda item: item["order"]):
        with st.container(border=True):
            st.markdown(f"**Beat {beat['order']}**")
            left, middle, right = st.columns([1, 2, 2])
            with left:
                beat["order"] = st.number_input(
                    "顺序",
                    min_value=1,
                    max_value=100,
                    value=int(beat["order"]),
                    key=f"{revision_id}-{beat['id']}-order",
                )
            with middle:
                raw_type = getattr(
                    beat.get("type"), "value", beat.get("type", "OPENING")
                )
                type_options = list(BEAT_TYPE_LABELS)
                selected_type = st.selectbox(
                    "类型",
                    type_options,
                    index=type_options.index(raw_type)
                    if raw_type in type_options
                    else 0,
                    format_func=lambda value: BEAT_TYPE_LABELS.get(value, "开端"),
                    key=f"{revision_id}-{beat['id']}-type",
                )
                beat["type"] = selected_type
            with right:
                selected_location = st.selectbox(
                    "场景",
                    ["（无）"] + location_options,
                    index=(location_options.index(beat["location_id"]) + 1)
                    if beat["location_id"] in location_options
                    else 0,
                    format_func=lambda value: (
                        "（无）"
                        if value == "（无）"
                        else location_labels.get(value, "未命名场景")
                    ),
                    key=f"{revision_id}-{beat['id']}-location",
                )
                beat["location_id"] = (
                    None if selected_location == "（无）" else selected_location
                )
            beat["summary"] = _input(
                "摘要",
                beat["summary"],
                f"{revision_id}-{beat['id']}-summary",
                area=True,
                height=80,
            )
            beat["characters"] = st.multiselect(
                "出场人物",
                character_options,
                default=[
                    item for item in beat["characters"] if item in character_options
                ],
                format_func=lambda value: character_labels.get(value, "未命名人物"),
                key=f"{revision_id}-{beat['id']}-characters",
            )
            beat["emotional_goal"] = _input(
                "情绪目标",
                beat["emotional_goal"],
                f"{revision_id}-{beat['id']}-emotion",
            )


def _build_content(working: dict) -> StoryBible:
    return StoryBible.model_validate(deepcopy(working))


def _render_bible_editor(project, service: StoryService, revision: dict) -> None:
    working = _get_working(revision)
    revision_id = revision["id"]
    status = _story_status(revision.get("status")) or StoryRevisionStatus.DRAFT
    status_text = {
        StoryRevisionStatus.DRAFT: "草稿",
        StoryRevisionStatus.APPROVED: "已确认",
        StoryRevisionStatus.SUPERSEDED: "下游内容需要更新",
    }.get(status, "状态待确认")
    st.markdown(
        f'<div class="story-revision-bar"><strong>Story Bible v{revision["version"]}</strong><span class="story-revision-status">{status_text}</span></div>',
        unsafe_allow_html=True,
    )
    with st.expander("故事概览", expanded=True):
        _render_overview(working, revision_id)
    with st.expander("人物", expanded=False):
        _render_characters(working, revision_id)
    with st.expander("场景", expanded=False):
        _render_locations(working, revision_id)
    with st.expander("剧情结构", expanded=False):
        _render_beats(working, revision_id)

    state = service.draft_state(revision, working)
    if state.dirty:
        st.warning("当前草稿有未保存修改；请先点击‘保存修改’，离开页面前不要关闭应用。")
    elif status is StoryRevisionStatus.DRAFT:
        st.caption(
            f"草稿已保存 · 最近保存 {state.updated_at.replace('T', ' ').split('.')[0]}"
        )

    save_label = "保存修改" if status is StoryRevisionStatus.DRAFT else "保存为新草稿"
    save_col, approve_col = st.columns([2, 1])
    if save_col.button(
        save_label,
        type="primary",
        use_container_width=True,
        key=f"save-story-{revision_id}",
    ):
        try:
            saved = service.save_draft(
                project.id, _build_content(working), revision_id=revision_id
            )
            _set_story_state(project, "revision", saved["id"])
            st.toast(f"Story Bible 第 {saved['version']} 版已保存")
            st.rerun()
        except ValueError as exc:
            st.error(f"无法保存：{_safe_error(exc)}")
        except Exception:
            logger.exception("failed to save Story Bible draft")
            st.error("保存失败，请检查必填字段和引用关系。")
    if status is StoryRevisionStatus.DRAFT:
        if approve_col.button(
            "确认 Story Bible",
            use_container_width=True,
            key=f"approve-story-{revision_id}",
        ):
            try:
                saved = service.save_draft(
                    project.id, _build_content(working), revision_id=revision_id
                )
                approved = service.approve_revision(saved["id"])
                _set_story_state(project, "revision", approved["id"])
                st.success("Story Bible 已确认。结构化剧本将在下一阶段生成。")
                st.rerun()
            except ValueError as exc:
                st.error(f"确认前校验失败：{_safe_error(exc)}")
            except Exception:
                logger.exception("failed to approve Story Bible")
                st.error("确认失败，请稍后重试。")
    elif status is StoryRevisionStatus.APPROVED:
        approve_col.success("已确认")


def _script_working_key(revision_id: str) -> str:
    return f"script_working_{revision_id}"


def _render_script_editor(
    project,
    story_revision: dict,
    script_service: ScriptService,
    dependency_service: DependencyStatusService | None = None,
) -> None:
    ready, detail = script_service.llm_readiness(project.id)
    if ready and st.button("准备结构化剧本草稿", key=f"generate-script-{project.id}"):
        queued, result = _enqueue_activity(
            project,
            "STRUCTURED_SCRIPT_GENERATION",
            {"source_story_revision_id": story_revision["id"]},
        )
        if queued and isinstance(result, dict) and result.get("id"):
            _set_story_state(project, "script-revision", result["id"])
            st.toast("结构化剧本草稿已加入后台活动")
            st.rerun()
        _render_activity_notice(project)
    elif not ready:
        st.caption("文本生成能力尚未配置；你仍可手动创建剧本，或前往设置能力。")
    revisions = script_service.list_revisions(project.id)
    if not revisions:
        st.info("还没有剧本。请先确认故事设定，再创建或生成结构化剧本。")
        if _story_status(story_revision.get("status")) is StoryRevisionStatus.APPROVED:
            if st.button(
                "创建空白剧本", type="primary", key=f"create-script-{project.id}"
            ):
                try:
                    script = script_service.create_manual_script(
                        project, story_revision
                    )
                    _set_story_state(project, "script-revision", script["id"])
                    st.rerun()
                except Exception as exc:
                    st.error(f"创建失败：{_safe_error(exc)}")
        return
    current_id = _story_state(project, "script-revision")
    revision = (
        script_service.get_revision(current_id)
        if current_id
        else (script_service.get_latest_draft(project.id) or revisions[0])
    )
    if revision is None or revision.get("project_id") != project.id:
        revision = revisions[0]
    _set_story_state(project, "script-revision", revision["id"])
    history = st.selectbox(
        "剧本历史",
        revisions,
        index=next(
            (i for i, x in enumerate(revisions) if x["id"] == revision["id"]), 0
        ),
        format_func=lambda x: (
            f"第 {x['version']} 版 · "
            f"{ {ScriptRevisionStatus.DRAFT: '草稿', ScriptRevisionStatus.APPROVED: '已确认', ScriptRevisionStatus.SUPERSEDED: '下游内容需要更新'}.get(_script_status(x.get('status')), '状态待确认') }"
        ),
        key=f"script-history-{project.id}",
    )
    if history["id"] != revision["id"]:
        _set_story_state(project, "script-revision", history["id"])
        st.rerun()
    if _script_status(revision.get("status")) is ScriptRevisionStatus.APPROVED and st.button(
        "从此版本创建新草稿", key=f"fork-script-{revision['id']}"
    ):
        try:
            forked = script_service.create_revision_from_approved(revision["id"])
            _set_story_state(project, "script-revision", forked["id"])
            st.rerun()
        except Exception as exc:
            st.error(f"无法创建草稿：{_safe_error(exc)}")
    status = _script_status(revision.get("status")) or ScriptRevisionStatus.DRAFT
    dependency_service = dependency_service or DependencyStatusService(
        script_service.repository
    )
    dependency = dependency_service.status_for_script(project.id, revision)
    status_text = {
        ScriptRevisionStatus.DRAFT: "草稿",
        ScriptRevisionStatus.APPROVED: "已确认",
        ScriptRevisionStatus.SUPERSEDED: "下游内容需要更新",
    }.get(status, "状态待确认")
    st.markdown(
        f'<div class="story-revision-bar"><strong>结构化剧本 · 第 {revision["version"]} 版</strong><span class="story-revision-status">{status_text}</span></div>',
        unsafe_allow_html=True,
    )
    working_key = _script_working_key(revision["id"])
    if working_key not in st.session_state:
        st.session_state[working_key] = revision["content"].model_dump(mode="python")
    working = st.session_state[working_key]
    if dependency.source_revision_id:
        st.caption(
            "故事设定版本已同步"
            if not dependency.outdated
            else "故事设定已更新，需要重新确认剧本"
        )
        with st.expander("高级来源信息", expanded=False):
            st.caption("内部来源版本链仅供排障，不影响正常编辑。")
    if dependency.outdated:
        st.warning(
            "当前剧本基于旧版故事设定，需要重新确认后再进入下游。"
            "下游内容会在确认后重新检查。"
        )
        with st.expander("查看下游同步详情（高级）", expanded=False):
            affected = getattr(dependency, "affected_downstream", ()) or ()
            st.caption("、".join(str(item) for item in affected) or "暂无记录")
    working["title"] = _input(
        "剧本标题", working["title"], f"{revision['id']}-script-title"
    )
    working["summary"] = _input(
        "剧本摘要",
        working.get("summary", ""),
        f"{revision['id']}-script-summary",
        area=True,
        height=80,
    )
    character_rows = story_revision["content"].model_dump(mode="python")["characters"]
    location_rows = story_revision["content"].model_dump(mode="python")["locations"]
    character_options = [c["id"] for c in character_rows]
    location_options = [location["id"] for location in location_rows]
    character_labels = {c["id"]: c.get("name") or "未命名角色" for c in character_rows}
    location_labels = {
        location["id"]: location.get("name") or "未命名场景"
        for location in location_rows
    }
    st.markdown("#### 场景导航")
    scene_options = sorted(working["scenes"], key=lambda x: x["order"])
    st.selectbox(
        "当前场景",
        scene_options,
        format_func=lambda x: (
            f"场景 {int(x['order']):02d} · {x['title']} · {location_labels.get(x['location_id'], '未命名场景')} · {x.get('estimated_duration_seconds', 0):g}s"
        ),
        key=f"scene-navigator-{revision['id']}",
    )
    st.caption("场景导航保持当前选择；下方编辑器展示全部场景，便于快速浏览与编辑。")
    for scene in sorted(working["scenes"], key=lambda x: x["order"]):
        with st.container(border=True):
            st.markdown(f"**场景 {scene['order']} · {scene['title']}**")
            scene_move_left, scene_move_right = st.columns([1, 1])
            if scene_move_left.button(
                "场景上移", key=f"scene-up-{revision['id']}-{scene['id']}"
            ):
                previous = next(
                    (x for x in working["scenes"] if x["order"] == scene["order"] - 1),
                    None,
                )
                if previous:
                    previous["order"], scene["order"] = (
                        scene["order"],
                        previous["order"],
                    )
                    st.rerun()
            if scene_move_right.button(
                "场景下移", key=f"scene-down-{revision['id']}-{scene['id']}"
            ):
                following = next(
                    (x for x in working["scenes"] if x["order"] == scene["order"] + 1),
                    None,
                )
                if following:
                    following["order"], scene["order"] = (
                        scene["order"],
                        following["order"],
                    )
                    st.rerun()
            c1, c2, c3 = st.columns(3)
            scene["order"] = c1.number_input(
                "顺序",
                1,
                999,
                int(scene["order"]),
                key=f"{revision['id']}-{scene['id']}-order",
            )
            scene["title"] = c2.text_input(
                "场景标题", scene["title"], key=f"{revision['id']}-{scene['id']}-title"
            )
            scene["location_id"] = c3.selectbox(
                "地点",
                location_options,
                index=max(
                    0,
                    location_options.index(scene["location_id"])
                    if scene["location_id"] in location_options
                    else 0,
                ),
                format_func=lambda value: location_labels.get(value, "未命名场景"),
                key=f"{revision['id']}-{scene['id']}-loc",
            )
            ie_value = getattr(
                scene.get("interior_exterior"),
                "value",
                scene.get("interior_exterior", "INT"),
            )
            tod_value = getattr(
                scene.get("time_of_day"),
                "value",
                scene.get("time_of_day", "UNSPECIFIED"),
            )
            scene["interior_exterior"] = st.selectbox(
                "内外景",
                [x.value for x in InteriorExterior],
                index=[x.value for x in InteriorExterior].index(ie_value),
                format_func=lambda value: {"INT": "室内", "EXT": "室外"}.get(
                    value, value
                ),
                key=f"{revision['id']}-{scene['id']}-ie",
            )
            scene["time_of_day"] = st.selectbox(
                "时间",
                [x.value for x in TimeOfDay],
                index=[x.value for x in TimeOfDay].index(tod_value),
                format_func=lambda value: {
                    "DAY": "白天",
                    "NIGHT": "夜晚",
                    "DAWN": "黎明",
                    "DUSK": "黄昏",
                    "UNSPECIFIED": "未指定",
                }.get(value, "未指定"),
                key=f"{revision['id']}-{scene['id']}-tod",
            )
            scene["character_ids"] = st.multiselect(
                "出场人物",
                character_options,
                default=[
                    x for x in scene.get("character_ids", []) if x in character_options
                ],
                format_func=lambda value: character_labels.get(value, "未命名角色"),
                key=f"{revision['id']}-{scene['id']}-chars",
            )
            scene["purpose"] = _input(
                "叙事目的",
                scene.get("purpose", ""),
                f"{revision['id']}-{scene['id']}-purpose",
            )
            scene["summary"] = _input(
                "场景摘要",
                scene.get("summary", ""),
                f"{revision['id']}-{scene['id']}-summary",
                area=True,
                height=70,
            )
            scene["emotion"] = _input(
                "情绪",
                scene.get("emotion", ""),
                f"{revision['id']}-{scene['id']}-emotion",
            )
            scene["estimated_duration_seconds"] = st.number_input(
                "预计时长（秒）",
                min_value=0.1,
                value=float(scene.get("estimated_duration_seconds", 1)),
                key=f"{revision['id']}-{scene['id']}-duration",
            )
            for beat in sorted(scene["beats"], key=lambda x: x["order"]):
                b1, b2, b3 = st.columns([1, 2, 3])
                move_up, move_down = st.columns([1, 1])
                if move_up.button(
                    "节拍上移",
                    key=f"beat-up-{revision['id']}-{scene['id']}-{beat['id']}",
                ):
                    previous = next(
                        (x for x in scene["beats"] if x["order"] == beat["order"] - 1),
                        None,
                    )
                    if previous:
                        previous["order"], beat["order"] = (
                            beat["order"],
                            previous["order"],
                        )
                        st.rerun()
                if move_down.button(
                    "节拍下移",
                    key=f"beat-down-{revision['id']}-{scene['id']}-{beat['id']}",
                ):
                    following = next(
                        (x for x in scene["beats"] if x["order"] == beat["order"] + 1),
                        None,
                    )
                    if following:
                        following["order"], beat["order"] = (
                            beat["order"],
                            following["order"],
                        )
                        st.rerun()
                beat["order"] = b1.number_input(
                    "Beat 顺序",
                    1,
                    999,
                    int(beat["order"]),
                    key=f"{revision['id']}-{beat['id']}-order",
                )
                beat_type = getattr(
                    beat.get("type"), "value", beat.get("type", "ACTION")
                )
                beat["type"] = b2.selectbox(
                    "类型",
                    [x.value for x in ScriptBeatType],
                    index=[x.value for x in ScriptBeatType].index(beat_type),
                    format_func=lambda value: {
                        "ACTION": "动作",
                        "DIALOGUE": "对白",
                        "TRANSITION": "转场",
                    }.get(value, "动作"),
                    key=f"{revision['id']}-{beat['id']}-type",
                )
                beat["character_id"] = b3.selectbox(
                    "人物",
                    ["（无）"] + character_options,
                    index=(
                        character_options.index(beat["character_id"]) + 1
                        if beat.get("character_id") in character_options
                        else 0
                    ),
                    format_func=lambda value: (
                        "（无）"
                        if value == "（无）"
                        else character_labels.get(value, "未命名角色")
                    ),
                    key=f"{revision['id']}-{beat['id']}-char",
                )
                beat["character_id"] = (
                    None if beat["character_id"] == "（无）" else beat["character_id"]
                )
                beat["text"] = _input(
                    "Beat 文本",
                    beat.get("text", ""),
                    f"{revision['id']}-{beat['id']}-text",
                    area=True,
                    height=70,
                )
                beat["emotion"] = (
                    _input(
                        "Beat 情绪",
                        beat.get("emotion") or "",
                        f"{revision['id']}-{beat['id']}-emotion",
                    )
                    or None
                )
                beat["stage_direction"] = (
                    _input(
                        "舞台指示",
                        beat.get("stage_direction") or "",
                        f"{revision['id']}-{beat['id']}-direction",
                    )
                    or None
                )
            if st.button("+ 添加 Beat", key=f"add-beat-{revision['id']}-{scene['id']}"):
                idx = len(scene["beats"]) + 1
                scene["beats"].append(
                    ScriptBeat(
                        id=f"{scene['id']}_beat_{idx:03d}",
                        order=idx,
                        type=ScriptBeatType.ACTION,
                        text="待填写",
                    ).model_dump(mode="python")
                )
                st.rerun()
    if st.button("+ 添加 Scene", key=f"add-scene-{revision['id']}"):
        idx = len(working["scenes"]) + 1
        loc = location_options[0]
        working["scenes"].append(
            Scene(
                id=f"scene_{idx:03d}",
                order=idx,
                title=f"Scene {idx}",
                location_id=loc,
                estimated_duration_seconds=1,
                beats=[
                    ScriptBeat(
                        id=f"beat_{idx:03d}_001",
                        order=1,
                        type=ScriptBeatType.ACTION,
                        text="待填写",
                    )
                ],
            ).model_dump(mode="python")
        )
        st.rerun()
    state = script_service.draft_state(revision, working)
    if state.dirty:
        st.warning("当前剧本有未保存修改；保存后可在冷启动时恢复。")
    elif status is ScriptRevisionStatus.DRAFT:
        st.caption(
            f"草稿已保存 · 最近保存 {state.updated_at.replace('T', ' ').split('.')[0]}"
        )
    save, approve = st.columns(2)
    if save.button("保存剧本", type="primary", key=f"save-script-{revision['id']}"):
        try:
            saved = script_service.save_draft(
                project.id,
                StructuredScript.model_validate(deepcopy(working)),
                revision_id=revision["id"],
            )
            _set_story_state(project, "script-revision", saved["id"])
            st.toast("Structured Script 已保存")
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{_safe_error(exc)}")
    if status is ScriptRevisionStatus.DRAFT and approve.button(
        "确认剧本", key=f"approve-script-{revision['id']}"
    ):
        try:
            saved = script_service.save_draft(
                project.id,
                StructuredScript.model_validate(deepcopy(working)),
                revision_id=revision["id"],
            )
            script_service.approve_revision(saved["id"])
            _set_story_state(project, "script-revision", saved["id"])
            st.success("Structured Script 已确认")
            st.rerun()
        except Exception as exc:
            st.error(f"确认失败：{_safe_error(exc)}")
    if dependency.outdated and dependency.current_revision_id:
        if st.button(
            "修复依赖：从当前 Story Bible 创建新 Draft",
            key=f"repair-script-dependency-{revision['id']}",
        ):
            try:
                repaired = script_service.create_revision_from_story(
                    project.id,
                    dependency.current_revision_id,
                    content=StructuredScript.model_validate(deepcopy(working)),
                )
                _set_story_state(project, "script-revision", repaired["id"])
                st.success("已创建新的结构化剧本草稿；旧版本保持不变。")
                st.rerun()
            except Exception as exc:
                st.error(f"依赖修复失败：{_safe_error(exc)}")
    with st.expander("阅读模式 / Script Preview"):
        st.caption("预览由结构化数据即时渲染；正式文本仍以已保存版本为准。")
        for scene in sorted(working["scenes"], key=lambda x: x["order"]):
            st.markdown(
                f"**场景 {int(scene['order']):02d} — {scene['title']} — { {'INT': '室内', 'EXT': '室外', 'INT_EXT': '室内外'}.get(scene.get('interior_exterior', 'INT'), '室内') } — { {'DAWN': '黎明', 'DAY': '白天', 'DUSK': '黄昏', 'NIGHT': '夜晚', 'UNSPECIFIED': '未指定'}.get(scene.get('time_of_day', 'UNSPECIFIED'), '未指定') }**"
            )
            for beat in sorted(scene["beats"], key=lambda x: x["order"]):
                label = beat.get("type", "ACTION")
                if hasattr(label, "value"):
                    label = label.value
                label = {
                    "ACTION": "动作",
                    "DIALOGUE": "对白",
                    "NARRATION": "旁白",
                    "INNER_MONOLOGUE": "内心独白",
                    "TRANSITION": "转场",
                }.get(label, "动作")
                st.markdown(f"**{label}**  {beat.get('text', '')}")


def _status_label(status: object) -> str:
    value = getattr(status, "value", status)
    return {
        "DRAFT": "草稿",
        "APPROVED": "已确认",
        "SUPERSEDED": "需要更新",
    }.get(str(value), "状态未知")


def _render_versions_approval(
    project,
    story_service: StoryService,
    script_service: ScriptService,
    dependency_service: DependencyStatusService,
) -> None:
    """A concise, human-readable revision and approval workspace."""

    st.markdown("### 版本与审批")
    st.caption("在这里查看保存、确认和下游同步状态；详细编辑回到对应工作区。")
    story_revisions = story_service.list_revisions(project.id)
    script_revisions = script_service.list_revisions(project.id)

    if not story_revisions:
        st.info("还没有 Story Bible 版本。先整理 Creative Brief，再创建故事设定草稿。")
        if st.button("去创意工作区", key=f"versions-open-creative-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("creative")
    else:
        st.markdown("#### Story Bible")
        for revision in story_revisions:
            status = _story_status(revision.get("status"))
            label = _status_label(status)
            with st.container(border=True):
                left, middle, right = st.columns([3, 2, 1])
                left.markdown(f"**第 {revision['version']} 版**")
                left.caption(
                    f"已保存 · {str(revision.get('updated_at', '')).replace('T', ' ').split('.')[0]}"
                )
                middle.markdown(f"状态 · **{label}**")
                if status is StoryRevisionStatus.APPROVED:
                    middle.caption("后续剧本以此版为依据；若内容变更，下游会提示更新。")
                elif status is StoryRevisionStatus.SUPERSEDED:
                    middle.caption("这是历史版本，不能作为新的下游依据。")
                else:
                    middle.caption("保存后仍可继续编辑，确认前不会影响下游。")
                if right.button("打开", key=f"versions-story-open-{revision['id']}"):
                    _set_story_state(project, "revision", revision["id"])
                    _set_story_state(project, "workspace", "Story Bible")
                    st.rerun()
                if status is StoryRevisionStatus.DRAFT and right.button(
                    "确认",
                    key=f"versions-story-approve-{revision['id']}",
                ):
                    try:
                        story_service.approve_revision(revision["id"])
                        st.success("Story Bible 已确认。")
                        st.rerun()
                    except Exception:
                        st.error("确认失败，请检查内容后重试。")
                if status is StoryRevisionStatus.APPROVED and right.button(
                    "新 Draft",
                    key=f"versions-story-fork-{revision['id']}",
                ):
                    try:
                        forked = story_service.create_revision_from_approved(
                            revision["id"]
                        )
                        _set_story_state(project, "revision", forked["id"])
                        _set_story_state(project, "workspace", "Story Bible")
                        st.rerun()
                    except Exception:
                        st.error("无法创建草稿，请稍后重试。")

    st.markdown("#### 结构化剧本")
    approved_story = next(
        (
            item
            for item in story_revisions
            if _story_status(item.get("status")) is StoryRevisionStatus.APPROVED
        ),
        None,
    )
    if not script_revisions:
        st.info("还没有结构化剧本。确认 Story Bible 后可创建或生成剧本。")
    for revision in script_revisions:
        status = _script_status(revision.get("status"))
        dependency = dependency_service.status_for_script(project.id, revision)
        current_state = (
            "下游与故事同步" if not dependency.outdated else "故事已更新，需要重新确认"
        )
        with st.container(border=True):
            left, middle, right = st.columns([3, 2, 1])
            left.markdown(f"**第 {revision['version']} 版**")
            left.caption(
                f"已保存 · {str(revision.get('updated_at', '')).replace('T', ' ').split('.')[0]}"
            )
            middle.markdown(f"状态 · **{_status_label(status)}**")
            middle.caption(current_state)
            if right.button("打开", key=f"versions-script-open-{revision['id']}"):
                _set_story_state(project, "script-revision", revision["id"])
                _set_story_state(project, "workspace", "结构化剧本")
                st.rerun()
            if (
                status is ScriptRevisionStatus.DRAFT
                and not dependency.outdated
                and right.button(
                    "确认",
                    key=f"versions-script-approve-{revision['id']}",
                )
            ):
                try:
                    script_service.approve_revision(revision["id"])
                    st.success("结构化剧本已确认。")
                    st.rerun()
                except Exception:
                    st.error("确认失败，请检查内容和依赖状态后重试。")
            if status is ScriptRevisionStatus.APPROVED and right.button(
                    "新草稿",
                key=f"versions-script-fork-{revision['id']}",
            ):
                try:
                    forked = script_service.create_revision_from_approved(
                        revision["id"]
                    )
                    _set_story_state(project, "script-revision", forked["id"])
                    _set_story_state(project, "workspace", "结构化剧本")
                    st.rerun()
                except Exception:
                    st.error("无法创建草稿，请稍后重试。")

    if approved_story is not None:
        approved_script = next(
            (
                item
                for item in script_revisions
                if _script_status(item.get("status")) is ScriptRevisionStatus.APPROVED
            ),
            None,
        )
        if approved_script is None:
            st.info("Story Bible 已确认，但结构化剧本还没有已确认版本。")
        elif any(
            dependency_service.status_for_script(project.id, item).outdated
            for item in script_revisions
            if _script_status(item.get("status")) is ScriptRevisionStatus.APPROVED
        ):
            st.warning("故事设定已有新版本；请在结构化剧本工作区创建并确认更新版本。")


def render() -> None:
    page_header(
        "故事 / 剧本",
        "STORY & SCRIPT WORKSPACE",
        "在 Story Bible、结构化剧本与版本审批之间推进可生产的故事。",
    )
    project = current_project_or_stop()
    render_project_context(
        project, stage="故事 / 剧本", next_action="继续故事与剧本", next_page="story"
    )
    render_ai_readiness(project_id=project.id, compact=True)
    _render_activity_notice(project)
    service = StoryService()
    script_service = ScriptService()
    dependency_service = DependencyStatusService(service.repository)
    revisions = service.list_revisions(project.id)
    current_id = _story_state(project, "revision")
    revision = service.get_revision(current_id) if current_id else None
    if revision is not None and revision.get("project_id") != project.id:
        revision = None
    if revision is None and revisions:
        # Prefer the latest durable Draft so cold restart returns the human's
        # last saved work instead of an older approved snapshot.
        revision = service.get_latest_draft(project.id) or revisions[0]
        _set_story_state(project, "revision", revision["id"])

    options = ["Story Bible", "结构化剧本", "版本与审批"]
    workspace = _story_state(project, "workspace", options[0])
    if workspace not in options:
        workspace = options[0]
    selected_workspace = st.radio(
        "故事工作区",
        options,
        index=options.index(workspace),
        horizontal=True,
        # Keep the widget key separate from the project-scoped durable UI
        # preference. Buttons below may switch workspaces during the same
        # Streamlit run; writing a widget-owned key after instantiation raises.
        key=f"story-workspace-selector-{project.id}",
        label_visibility="collapsed",
    )
    _set_story_state(project, "workspace", selected_workspace)
    if selected_workspace == "Story Bible":
        if revision is None:
            _render_brief(project, service)
        else:
            _render_bible_editor(project, service, revision)
    elif selected_workspace == "结构化剧本":
        approved_story = next(
            (
                item
                for item in service.list_revisions(project.id)
                if _story_status(item.get("status")) is StoryRevisionStatus.APPROVED
            ),
            None,
        )
        if approved_story is None:
            st.warning("请先确认 Story Bible，确认后就可以继续编辑结构化剧本。")
            if st.button(
                "打开 Story Bible", key=f"story-script-open-bible-{project.id}"
            ):
                _set_story_state(project, "workspace", "Story Bible")
                st.rerun()
        else:
            _render_script_editor(
                project, approved_story, script_service, dependency_service
            )
    else:
        _render_versions_approval(project, service, script_service, dependency_service)
