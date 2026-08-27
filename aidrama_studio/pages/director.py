from __future__ import annotations
from html import escape
import sys
from pathlib import Path

# Streamlit may execute navigation pages with ``pages/`` as sys.path[0].
# Keep package imports stable across reruns and page switches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st  # noqa: E402
from aidrama_studio.components.page_header import page_header  # noqa: E402
from aidrama_studio.pages._shared import (  # noqa: E402
    current_project_or_stop,
    render_actionable_blockers,
    render_project_context,
)
from aidrama_studio.services import (  # noqa: E402
    ScriptService,
    DirectorService,
    DirectorServiceError,
    ProducerService,
    ProducerServiceError,
    ShotServiceError,
)
from aidrama_studio.services.security import sanitize_error  # noqa: E402
from aidrama_studio.domain import DirectorGoalKind  # noqa: E402


def _shot_service():
    try:
        from aidrama_studio.services import ShotService

        return ShotService()
    except (ImportError, AttributeError):
        return None


def _call(service, name, *args, **kwargs):
    fn = getattr(service, name, None)
    return fn(*args, **kwargs) if fn else None


def _call_first(service, names, *args, **kwargs):
    """Invoke the first available compatibility method exactly once."""

    for name in names:
        fn = getattr(service, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
    return None


def _value(obj, key, default=""):
    return (
        obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    )


def _set_value(obj, key, value) -> None:
    """Update a mapping or model-like fixture without assuming one shape."""

    if isinstance(obj, dict):
        obj[key] = value
    else:
        try:
            setattr(obj, key, value)
        except (AttributeError, TypeError):
            # Immutable service models are still valid read-only fixtures;
            # the save action will simply receive the original field values.
            return


def _ordered_shots(shots):
    """Return shots in their authored order without assuming an ``order`` field.

    Older/offline fixtures can contain a plain list of shot dictionaries that
    omit ``order``.  The previous inline sort lambdas closed over the loop
    variable used by ``enumerate`` and could raise ``NameError`` in that case.
    Keeping this tiny normalizer here also makes the storyboard and duration
    views agree on ordering for model and mapping objects alike.
    """

    def sort_key(item):
        raw = _value(item, "order", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            # Preserve list order for malformed/missing order values while
            # still placing explicitly ordered shots first.
            return 0

    return sorted(list(shots or []), key=sort_key)


def _director_state(project, name: str, default=None):
    """Read a project-scoped Director UI value with legacy compatibility.

    Streamlit session state survives project switches.  The old page stored
    plan/shot selections under global keys, which could make a second project
    open the first project's plan.  New writes use a project key; a one-time
    fallback keeps existing deep links and tests that seed the legacy keys
    working.
    """

    scoped = f"director-{name}-{project.id}"
    if scoped not in st.session_state:
        legacy = {
            "plan": "director_plan_id",
            "selected-shot": "director_selected_shot",
            "workspace": "director_workspace",
        }.get(name)
        if legacy and legacy in st.session_state:
            st.session_state[scoped] = st.session_state[legacy]
    return st.session_state.get(scoped, default)


def _set_director_state(project, name: str, value) -> None:
    scoped = f"director-{name}-{project.id}"
    st.session_state[scoped] = value
    # Keep the legacy aliases synchronized for callers that still inspect
    # them, while all page reads remain project-scoped.
    legacy = {
        "plan": "director_plan_id",
        "selected-shot": "director_selected_shot",
        "workspace": "director_workspace",
    }.get(name)
    if legacy:
        st.session_state[legacy] = value


def _status(obj):
    value = _value(obj, "status", "DRAFT")
    return str(getattr(value, "value", value) or "DRAFT").strip().upper()


def _safe_error(exc: object, *, fallback: str = "操作未完成") -> str:
    """Keep normal Director copy concise and redact paths/diagnostic payloads."""

    detail = sanitize_error(exc, max_length=180)
    return detail or fallback


_DECISION_STATUS_LABELS = {
    "RECOMMENDED": "待人工处理",
    "APPROVED": "已批准",
    "REJECTED": "已拒绝",
    "COMPLETED": "已完成",
}

_RISK_REASON_LABELS = {
    "EMOTIONAL_CLOSEUP": "近景情绪表演",
    "MULTI_CHARACTER": "多人同框",
    "CAMERA_MOTION": "复杂运镜",
    "KEY_STORY_BEAT": "关键剧情节拍",
}


def _risk_reason_label(reason: object) -> str:
    value = getattr(reason, "value", reason)
    text = str(value)
    return _RISK_REASON_LABELS.get(text, "其他风险提示")


def _human_blocker(reason: object) -> str:
    """Translate common service blocker phrases for the normal Director rail."""

    text = str(reason or "").strip()
    lowered = text.casefold()
    if "approved story bible" in lowered:
        return "请先确认 Story Bible"
    if "approved structured script" in lowered:
        return "请先确认结构化剧本"
    if "approved shot plan" in lowered:
        return "请先确认分镜"
    if "missing reference" in lowered or "reference asset" in lowered:
        return "还有角色或场景参考图未锁定"
    if "qc" in lowered and ("fail" in lowered or "block" in lowered):
        return "有镜头检查未通过，需要审片"
    return "还有一项准备工作需要处理"


def _decision_status_label(decision) -> str:
    return _DECISION_STATUS_LABELS.get(_status(decision), "待人工处理")


def _director_user_copy(action: str, reason: str) -> tuple[str, str]:
    """Keep canonical Director vocabulary out of the creative surface."""
    action_text = {
        "STOP_AND_REVIEW": "完成准备清单",
        "REVIEW_PROJECT_STATE": "检查当前准备度",
        "LOCK_CHARACTER_REFERENCE": "锁定角色参考图",
        "LOCK_LOCATION_REFERENCE": "锁定场景参考图",
        "REVIEW_SHOT_PLAN": "检查分镜",
        "MAKE_PRODUCTION_READY": "完成制作准备",
    }.get(str(action), "继续处理当前阶段")
    lowered = str(reason).lower()
    if (
        "approved story bible" in lowered
        or "approved structured script" in lowered
        or "approved shot plan" in lowered
    ):
        return action_text, "故事设定、剧本和分镜还没有全部确认。"
    return action_text, _human_blocker(reason)


def _enqueue_shot_activity(
    project, operation: str, payload: dict[str, object]
) -> object | None:
    """Queue shot AI work through an optional runtime-owned activity seam."""
    adapter = st.session_state.get("_aidrama_activity_adapter")
    if not callable(adapter):
        st.session_state[f"director-activity-{project.id}"] = {
            "state": "pending",
            "message": "分镜后台生成能力尚未连接；当前镜头编辑器保持可用。",
        }
        return None
    try:
        result = adapter(project_id=project.id, operation=operation, payload=payload)
    except TypeError:
        result = adapter(project.id, operation, payload)
    except Exception as exc:
        st.session_state[f"director-activity-{project.id}"] = {
            "state": "failed",
            "message": _safe_error(exc, fallback="后台分镜活动暂未完成"),
        }
        return None
    st.session_state[f"director-activity-{project.id}"] = {
        "state": "queued",
        "message": "分镜请求已加入后台活动；你仍可继续编辑当前工作区。",
    }
    return result


def _render_shot_activity(project) -> None:
    activity = st.session_state.get(f"director-activity-{project.id}")
    if not isinstance(activity, dict):
        return
    state = str(activity.get("state") or "pending")
    message = str(activity.get("message") or "后台分镜活动状态待同步。")
    if state in {"pending", "queued", "running"}:
        st.info(message)
    elif state == "failed":
        st.warning("分镜活动暂未完成：" + message)


def _editor(service, project, plan):
    pid = _value(plan, "id", "plan")
    shots = _value(plan, "shots", []) or []
    labels = [f"镜头 {i + 1:02d}" for i, _ in enumerate(shots)]
    st.markdown("#### 镜头导航")
    selected_shot_id = _director_state(project, "selected-shot")
    selected_index = next(
        (i for i, shot in enumerate(shots) if _value(shot, "id", "") == selected_shot_id),
        0,
    )
    selected = st.selectbox(
        "选择镜头",
        labels or ["暂无镜头"],
        index=selected_index if labels else 0,
        key=f"shot-select-{pid}",
        label_visibility="collapsed",
    )
    idx = labels.index(selected) if labels and selected in labels else 0
    if shots:
        _set_director_state(project, "selected-shot", _value(shots[idx], "id", ""))
    if st.button("＋ 新增镜头", key=f"add-shot-{pid}"):
        _call_first(service, ("add_shot", "create_shot"), pid)
        st.rerun()
    if not shots:
        st.info("还没有镜头。点击「新增镜头」开始手动规划。")
        return
    shot = shots[idx]
    st.markdown("#### 镜头编辑器")
    with st.container(border=True):
        left, right = st.columns(2)
        with left:
            for key, label in (
                ("shot_size", "景别"),
                ("camera_angle", "机位角度"),
                ("camera_movement", "运镜"),
                ("lens", "镜头焦段"),
                ("composition", "构图"),
            ):
                _set_value(
                    shot,
                    key,
                    st.text_input(
                        label, str(_value(shot, key, "")), key=f"{pid}-{idx}-{key}"
                    ),
                )
            st.caption("所属场景已从结构化剧本继承；如需更换，请回到剧本工作区调整。")
            _set_value(
                shot,
                "duration_seconds",
                st.number_input(
                    "时长（秒）",
                    min_value=0.1,
                    value=float(_value(shot, "duration_seconds", 1) or 1),
                    key=f"{pid}-{idx}-duration",
                ),
            )
            risk_options = ["LOW", "MEDIUM", "HIGH"]
            current_risk = getattr(
                _value(shot, "risk_level", "LOW"),
                "value",
                _value(shot, "risk_level", "LOW"),
            )
            risk_labels = {"LOW": "低", "MEDIUM": "中", "HIGH": "高"}
            _set_value(
                shot,
                "risk_level",
                st.selectbox(
                    "风险等级",
                    risk_options,
                    index=risk_options.index(current_risk)
                    if current_risk in risk_options
                    else 0,
                    format_func=lambda value: risk_labels.get(value, "低"),
                    key=f"{pid}-{idx}-risk",
                ),
            )
            reasons = _value(shot, "risk_reasons", []) or []
            if reasons:
                st.caption(
                    "自动识别提示："
                    + "、".join(_risk_reason_label(reason) for reason in reasons)
                )
        with right:
            for key, label in (
                ("action", "动作"),
                ("expression", "表情"),
                ("lighting", "灯光"),
                ("blocking", "调度"),
                ("dialogue_or_narration", "对白 / 旁白"),
                ("visual_intent", "视觉意图"),
                ("transition_hint", "转场提示"),
            ):
                value = _value(shot, key, "")
                if key == "lighting":
                    value = _value(value, "quality", "")
                if key == "blocking":
                    value = _value(value, "movement", "")
                _set_value(
                    shot,
                    key,
                    st.text_area(
                        label, str(value), key=f"{pid}-{idx}-{key}", height=55
                    ),
                )
            linked_subjects = _value(shot, "subject", []) or []
            st.caption(f"已关联角色：{len(linked_subjects)} 个（由剧本引用维护）")
        _set_value(
            shot,
            "status",
            "LOCKED"
            if st.checkbox(
                "锁定镜头",
                value=str(getattr(_value(shot, "status", "PLANNED"), "value", _value(shot, "status", "PLANNED"))).upper()
                == "LOCKED",
                key=f"{pid}-{idx}-locked",
            )
            else "PLANNED",
        )
        _set_value(
            shot,
            "risk_override",
            st.checkbox(
                "人工风险判定",
                value=bool(_value(shot, "risk_override", False)),
                key=f"{pid}-{idx}-risk-override",
            ),
        )
        if bool(_value(shot, "risk_override", False)):
            _set_value(
                shot,
                "risk_override_note",
                st.text_input(
                    "人工风险判定说明",
                    str(_value(shot, "risk_override_note", "")),
                    key=f"{pid}-{idx}-risk-override-note",
                ),
            )
        a, b, c, d = st.columns(4)
        if a.button("↑ 上移", key=f"up-{pid}-{idx}"):
            _call(service, "move_shot", pid, idx, -1)
            st.rerun()
        if b.button("↓ 下移", key=f"down-{pid}-{idx}"):
            _call(service, "move_shot", pid, idx, 1)
            st.rerun()
        if c.button("保存草稿", type="primary", key=f"save-{pid}"):
            try:
                fields = (
                    "scene_id",
                    "shot_size",
                    "camera_angle",
                    "camera_movement",
                    "lens",
                    "composition",
                    "duration_seconds",
                    "risk_level",
                    "risk_reasons",
                    "subject",
                    "action",
                    "expression",
                    "eyeline",
                    "lighting",
                    "blocking",
                    "dialogue_or_narration",
                    "visual_intent",
                    "transition_hint",
                    "status",
                    "risk_override",
                    "risk_override_note",
                )
                service.update_shot_fields(
                    project.id,
                    pid,
                    _value(shot, "id"),
                    {
                        field: _value(shot, field)
                        for field in fields
                        if _value(shot, field, None) is not None
                    },
                )
                st.toast("Shot Plan Draft 已保存")
            except (ShotServiceError, ValueError, KeyError) as exc:
                st.error(f"保存失败：{_safe_error(exc)}")
        if d.button(
            "请求重新生成",
            key=f"regenerate-{pid}-{idx}",
            disabled=_status(shot) == "LOCKED",
            help="只创建这个镜头的新 DRAFT 候选；不会覆盖其他镜头或历史批准版本。",
        ):
            result = _enqueue_shot_activity(
                project,
                "SHOT_SELECTIVE_REGENERATION",
                {"plan_id": pid, "shot_id": _value(shot, "id")},
            )
            _render_shot_activity(project)
            if isinstance(result, dict) and result.get("id"):
                _set_director_state(project, "plan", result["id"])
                st.rerun()


def _shot_plan_context(project):
    """Resolve the project-scoped approved script and current shot plan."""
    approved = ScriptService().get_approved_revision(project.id)
    if not approved:
        return None, None, [], None
    service = _shot_service()
    if service is None:
        return approved, None, [], None
    plans = (
        _call(service, "list_plans", project.id)
        or _call(service, "list_revisions", project.id)
        or []
    )
    ids = [_value(item, "id") for item in plans if _value(item, "id")]
    current_id = _director_state(project, "plan")
    if current_id not in ids:
        current_id = ids[0] if ids else None
    plan = plans[ids.index(current_id)] if current_id else None
    if current_id:
        _set_director_state(project, "plan", current_id)
    return approved, service, plans, plan


def _shot_metrics(project, plan) -> None:
    shots = _value(plan, "shots", []) or []
    total = sum(float(_value(shot, "duration_seconds", 0) or 0) for shot in shots)
    target = float(_value(project, "target_duration_seconds", 0) or 0)
    delta = target - total
    columns = st.columns(4)
    columns[0].metric("镜头数", len(shots))
    columns[1].metric("当前总时长", f"{total:g} 秒")
    columns[2].metric("目标总时长", f"{target:g} 秒")
    columns[3].metric("差值", f"{delta:+g} 秒")
    locked = sum(1 for shot in shots if _status(shot) == "LOCKED")
    high = sum(
        1
        for shot in shots
        if str(getattr(_value(shot, "risk_level", "LOW"), "value", _value(shot, "risk_level", "LOW"))).upper()
        == "HIGH"
    )
    st.caption(f"已锁定 {locked} 个镜头 · 高风险 {high} 个 · 时长修改不会自动生成视频")


def _render_storyboard_board(
    project, service, plans, plan, approved, *, key_suffix: str = ""
) -> None:
    st.markdown("### Storyboard")
    st.caption(
        f"结构化剧本已确认 · 第 {approved['version']} 版；镜头数量随当前分镜动态变化。"
    )
    if plan is None:
        st.info("还没有分镜。先创建一个手动分镜草稿，再逐镜补充画面和时长。")
        if st.button(
            "创建分镜草稿", type="primary", key=f"new-plan-{project.id}{key_suffix}"
        ):
            if callable(getattr(service, "create_manual_plan", None)):
                created = service.create_manual_plan(project, approved)
            elif callable(getattr(service, "create_plan", None)):
                created = service.create_plan(project.id, approved["id"])
            else:
                created = None
            if created:
                _set_director_state(project, "plan", _value(created, "id"))
                st.rerun()
        return
    _shot_metrics(project, plan)
    shots = _value(plan, "shots", []) or []
    if not shots:
        st.info("当前分镜还没有镜头。")
        return
    # A compact storyboard list keeps all shots visible without assuming a
    # product limit (12 is only a fixture, not a constraint).
    for index, shot in enumerate(_ordered_shots(shots), 1):
        duration = float(_value(shot, "duration_seconds", 0) or 0)
        status = "已锁定" if _status(shot) == "LOCKED" else "草稿"
        with st.container(border=True):
            left, middle, right = st.columns([1, 5, 1])
            left.markdown(f"**{index:02d}**")
            middle.markdown(
                f"**{_value(shot, 'visual_intent', '') or '待填写镜头意图'}**"
            )
            middle.caption(
                f"{duration:g} 秒 · {_value(shot, 'action', '') or '尚未填写动作'} · {status}"
            )
            if right.button("编辑", key=f"board-edit-{_value(shot, 'id', index)}"):
                _set_director_state(
                    project, "selected-shot", _value(shot, "id", "")
                )
                _set_director_state(project, "workspace", "镜头编辑器")
                st.rerun()
    if _status(plan) == "DRAFT" and st.button(
        "确认分镜", type="primary", key=f"approve-board-{_value(plan, 'id', 'plan')}"
    ):
        try:
            _call_first(
                service,
                ("approve_plan", "approve_revision"),
                _value(plan, "id"),
            )
            st.success("分镜已确认。")
            st.rerun()
        except (ShotServiceError, ValueError, KeyError) as exc:
            st.error(_safe_error(exc, fallback="分镜确认失败，请检查镜头内容后重试。"))


def _render_duration_planning(project, service, plan) -> None:
    st.markdown("### 时长规划")
    if plan is None:
        st.info("创建分镜后，这里会显示创作、模型和交付时长。")
        return
    _shot_metrics(project, plan)
    plan_id = _value(plan, "id", "plan")
    if st.button("获取时长平衡建议", key=f"duration-rebalance-{plan_id}"):
        try:
            st.session_state[f"duration-proposal-{plan_id}"] = (
                service.recommend_duration_rebalance(
                    plan_id, project.target_duration_seconds
                )
            )
        except (ShotServiceError, ValueError, KeyError) as exc:
            st.error(_safe_error(exc, fallback="时长建议暂不可用，请稍后重试。"))
    proposal = st.session_state.get(f"duration-proposal-{plan_id}")
    if proposal:
        if not proposal.get("feasible"):
            st.warning("锁定镜头已占满目标时长；请先显式解锁或调整目标。")
        elif not proposal.get("suggestions"):
            st.success("当前时长已经接近目标，无需调整。")
        else:
            st.info("以下是建议，不会覆盖手工编辑、锁定镜头或已确认版本。")
            for item in proposal["suggestions"]:
                st.caption(
                    f"镜头 · {item['from_seconds']:g} 秒 → {item['to_seconds']:g} 秒"
                )
    st.markdown("#### 镜头时长明细")
    for index, shot in enumerate(
        _ordered_shots(_value(plan, "shots", []) or []),
        1,
    ):
        st.write(
            f"镜头 {index:02d} · {float(_value(shot, 'duration_seconds', 0) or 0):g} 秒"
        )


def _render_generation_brief(project, plan) -> None:
    """Provider-neutral natural-language intent for the selected shot."""
    st.markdown("### Generation Brief")
    st.caption("这是可编辑的创作意图；保存不会提交生成请求，也不会自动生成视频。")
    shots = _value(plan, "shots", []) if plan else []
    if not shots:
        st.info("创建分镜后选择一个镜头，再编辑 Generation Brief。")
        return
    shot_ids = [_value(shot, "id", str(index + 1)) for index, shot in enumerate(shots)]
    selected_id = _director_state(project, "selected-shot", shot_ids[0])
    if selected_id not in shot_ids:
        selected_id = shot_ids[0]
    selected = shots[shot_ids.index(selected_id)]
    labels = {sid: f"镜头 {index + 1:02d}" for index, sid in enumerate(shot_ids)}
    selected_id = st.selectbox(
        "选择镜头",
        shot_ids,
        index=shot_ids.index(selected_id),
        format_func=lambda value: labels[value],
        key=f"brief-shot-{_value(plan, 'id', 'plan')}",
    )
    selected = shots[shot_ids.index(selected_id)]
    key = f"generation-brief-{_value(plan, 'id', 'plan')}-{selected_id}"
    default = st.session_state.get(key, "")
    if not default:
        default = "。".join(
            value
            for value in (
                str(_value(selected, "visual_intent", "")),
                str(_value(selected, "action", "")),
                f"创作时长 {_value(selected, 'duration_seconds', 0)} 秒",
            )
            if value and value != "None"
        )
    brief = st.text_area("镜头生成意图", value=default, height=230, key=key)
    st.caption("继承约束：已锁定的角色与场景参考、当前镜头时长和故事连续性。")
    if st.button(
        "保存 Generation Brief",
        type="primary",
        key=f"save-brief-{_value(plan, 'id', 'plan')}-{selected_id}",
    ):
        st.session_state[key] = brief
        st.toast("Generation Brief 已保存")


def _render_shot_plan(project, *, view: str = "Storyboard") -> None:
    """Render one of the four Storyboard workspace states."""
    approved, service, plans, plan = _shot_plan_context(project)
    if not approved:
        st.warning("请先完成并确认结构化剧本，确认后才能建立分镜。")
        return
    if service is None:
        st.error("分镜服务暂不可用，请稍后重试。")
        return
    st.caption(f"当前依据：已确认的结构化剧本第 {approved['version']} 版")
    if view == "Storyboard":
        _render_storyboard_board(project, service, plans, plan, approved)
    elif view == "镜头编辑器":
        if plan is None:
            _render_storyboard_board(
                project, service, plans, plan, approved, key_suffix="-editor-empty"
            )
        else:
            _editor(service, project, plan)
    elif view == "时长规划":
        _render_duration_planning(project, service, plan)
    else:
        _render_generation_brief(project, plan)


def _render_director_compact(project) -> None:
    """Small contextual rail used beside the storyboard canvas."""
    director = DirectorService()
    producer = ProducerService()
    try:
        state = director.inspect_project(project.id)
    except (DirectorServiceError, ProducerServiceError):
        st.warning("暂时无法读取导演建议。")
        return
    readiness = state.get("readiness") or {}
    try:
        recommendations = producer.recommendations(project.id)
    except ProducerServiceError:
        recommendations = []
    recommendation = recommendations[0] if recommendations else None
    action = _value(recommendation, "action", "REVIEW_PROJECT_STATE")
    reason = _value(recommendation, "reason", "先检查当前项目准备度。")
    user_action, user_reason = _director_user_copy(str(action), str(reason))
    state_label = {
        "STORY": "故事设定",
        "SCRIPT": "结构化剧本",
        "REFERENCES": "角色与场景",
        "SHOT_PLAN": "分镜",
        "PRODUCTION": "制作",
        "REVIEW": "审片",
        "POST": "成片",
    }.get(str(state.get("project_state", "")), "准备中")
    with st.container(border=True):
        st.markdown("### AI 导演")
        st.caption("根据当前工作区给出建议；所有确认仍由你完成。")
        st.metric("当前阶段", state_label)
        st.markdown(f"**下一步：{user_action}**")
        st.caption(user_reason)
        if not bool(readiness.get("ready")):
            st.warning("还有准备项未完成。")
        if st.button("分析当前项目", key=f"director-compact-run-{project.id}"):
            try:
                sessions = director.list_sessions(project.id)
                session = (
                    sessions[0]
                    if sessions
                    else director.start_session(
                        project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=1
                    )
                )
                director.run(project.id, session.id)
                st.success("建议已更新。")
                st.rerun()
            except (DirectorServiceError, ValueError, KeyError):
                st.warning("导演检查未完成，请稍后重试。")
        with st.expander("查看阻塞项", expanded=False):
            blockers = readiness.get("blocked_reasons", []) or []
            if blockers:
                for blocker in blockers:
                    st.markdown(f"- {_human_blocker(blocker)}")
            else:
                st.caption("当前没有已知阻塞。")


def _render_director_expanded_summary(project) -> None:
    """Render an always-open 1920 rail without duplicating widget keys."""

    director = DirectorService()
    producer = ProducerService()
    try:
        state = director.inspect_project(project.id) or {}
        recommendations = list(producer.recommendations(project.id) or [])
    except (DirectorServiceError, ProducerServiceError, ValueError, KeyError):
        state = {}
        recommendations = []
    readiness = _value(state, "readiness", {}) or {}
    recommendation = recommendations[0] if recommendations else None
    action = _value(recommendation, "action", "REVIEW_PROJECT_STATE")
    reason = _value(recommendation, "reason", "先检查当前项目准备度。")
    user_action, user_reason = _director_user_copy(str(action), str(reason))
    state_label = {
        "STORY": "故事设定",
        "SCRIPT": "结构化剧本",
        "REFERENCES": "角色与场景",
        "SHOT_PLAN": "分镜",
        "PRODUCTION": "制作",
        "REVIEW": "审片",
        "POST": "成片",
    }.get(_status(_value(state, "project_state")), "准备中")
    blockers = list(_value(readiness, "blocked_reasons", []) or [])
    blocker_copy = f"还有 {len(blockers)} 项准备事项" if blockers else "当前没有已知阻塞"
    st.markdown(
        '<section class="aidrama-director-expanded" aria-label="AI 导演">'
        '<span class="aidrama-section-kicker">AI DIRECTOR</span>'
        '<h3>AI 导演建议</h3>'
        f'<p class="aidrama-director-stage">当前阶段 · {escape(state_label)}</p>'
        f'<strong>{escape(user_action)}</strong>'
        f'<p>{escape(user_reason)}</p>'
        f'<small>{escape(blocker_copy)}</small>'
        '<small>建议不会自动执行；修改、锁定、付费和审批都需要你的明确操作。</small>'
        '</section>',
        unsafe_allow_html=True,
    )


def _render_director_console(project, *, compact: bool = False) -> None:
    """Render a bounded, durable AI Director / Producer control plane.

    This console is deliberately advisory: it reconstructs canonical project
    state through services and exposes one primary next action.  It never
    invokes a provider or mutates creative truth from the page.
    """
    if compact:
        _render_director_compact(project)
        return
    st.subheader("AI 导演建议")
    st.caption(
        f"项目：{project.title} · 状态由 Story、Script、Shot、资产、生产与 QC 汇总"
    )
    director = DirectorService()
    producer = ProducerService()
    try:
        state = director.inspect_project(project.id)
    except (DirectorServiceError, ProducerServiceError):
        st.warning("暂时无法读取导演状态，请检查项目数据后重试。")
        st.caption("可在高级诊断中查看详细原因。")
        return

    state_label = {
        "STORY": "故事设定",
        "SCRIPT": "结构化剧本",
        "REFERENCES": "角色与场景",
        "SHOT_PLAN": "分镜",
        "PRODUCTION": "制作",
        "REVIEW": "审片",
        "POST": "成片",
    }.get(str(state.get("project_state", "")), "准备中")
    readiness = state.get("readiness") or {}
    try:
        producer_recommendations = producer.recommendations(project.id)
    except ProducerServiceError:
        producer_recommendations = []
        st.caption("导演建议暂不可用；不会影响已保存的创作内容。")

    # A durable session is created only by explicit user action; no provider
    # call is implied by showing this page.
    sessions = director.list_sessions(project.id)
    session_id = st.session_state.get(f"director-session-{project.id}")
    session = next((item for item in sessions if item.id == session_id), None)
    if session is None and sessions:
        session = sessions[0]
        session_id = session.id
        st.session_state[f"director-session-{project.id}"] = session_id

    recommendation = session.pending_recommendation if session else None
    if recommendation is None and producer_recommendations:
        recommendation = producer_recommendations[0]
    action = getattr(recommendation, "action", None) or "REVIEW_PROJECT_STATE"
    reason = (
        getattr(recommendation, "reason", None)
        or "先运行一次有界的导演检查，获得结构化下一步建议。"
    )
    user_action, user_reason = _director_user_copy(str(action), str(reason))
    target_id = getattr(recommendation, "target_id", None)
    pending_decision = None
    approved_decision = None
    latest_decision = None
    if session is not None:
        session_decisions = director.list_decisions(project.id, session.id)
        latest_decision = session_decisions[-1] if session_decisions else None
        pending_decision = next(
            (
                item
                for item in reversed(session_decisions)
                if _status(item) == "RECOMMENDED"
            ),
            None,
        )
        approved_decision = next(
            (
                item
                for item in reversed(session_decisions)
                if _status(item) == "APPROVED"
            ),
            None,
        )

    metric_cols = st.columns(4)
    metric_cols[0].metric("当前阶段", state_label)
    metric_cols[1].metric("生产就绪", "是" if bool(readiness.get("ready")) else "否")
    metric_cols[2].metric("高风险镜头", len(producer.high_risk_shots(project.id)))
    metric_cols[3].metric("QC 失败", len(state.get("qc_failures", [])))

    with st.container(border=True):
        st.markdown("### 下一步建议")
        st.markdown(f"**{user_action}**")
        st.write(user_reason)
        st.info("导演建议不会绕过故事、剧本、分镜、资产锁定或人工审片确认。")
        if latest_decision is not None:
            latest_status = _decision_status_label(latest_decision)
            if _status(latest_decision) == "APPROVED":
                st.success(
                    "最近建议已批准：批准只记录人工审核，不会自动执行建议；你可以标记完成或继续分析。"
                )
            elif _status(latest_decision) == "REJECTED":
                st.warning(
                    "最近建议已拒绝：未执行任何自动动作；你可以继续分析当前项目。"
                )
            elif _status(latest_decision) == "COMPLETED":
                st.success("最近建议已完成：Director 可以继续分析当前项目。")
            else:
                st.caption(f"最近建议状态：{latest_status}，需要人工处理后才能继续。")
        if st.button("分析当前项目", type="primary", key=f"director-run-{project.id}"):
            try:
                if session is None:
                    session = director.start_session(
                        project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=1
                    )
                    st.session_state[f"director-session-{project.id}"] = session.id
                decision = director.run(project.id, session.id)
                st.session_state[f"director-last-action-{project.id}"] = (
                    decision.recommendation.action
                )
                st.success("导演决策已保存，可在下方查看。")
                st.rerun()
            except (DirectorServiceError, ValueError, KeyError):
                st.warning("导演检查未完成，请先处理当前阻塞项。")
                st.caption("可在高级诊断中查看详细原因。")
        if pending_decision is not None:
            st.caption(
                "该建议需要人工确认；批准只记录审核，不会自动确认故事、剧本、资产或提交生成请求。"
            )
            approve_col, reject_col = st.columns(2)
            if approve_col.button(
                "确认已处理 / 批准建议", key=f"director-approve-{pending_decision.id}"
            ):
                try:
                    director.approve_decision(project.id, pending_decision.id)
                    st.success("建议已记录为批准，Director 现可继续分析。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(_safe_error(exc, fallback="建议批准失败，请稍后重试。"))
            if reject_col.button(
                "拒绝建议", key=f"director-reject-{pending_decision.id}"
            ):
                try:
                    director.reject_decision(project.id, pending_decision.id)
                    st.info("建议已拒绝；未执行任何自动动作。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(_safe_error(exc, fallback="建议拒绝失败，请稍后重试。"))
        elif approved_decision is not None:
            complete_col, continue_col = st.columns(2)
            if complete_col.button(
                "标记已处理 / 完成建议", key=f"director-complete-{approved_decision.id}"
            ):
                try:
                    director.complete_decision(project.id, approved_decision.id)
                    st.success("建议生命周期已完成。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(_safe_error(exc, fallback="建议完成状态更新失败，请稍后重试。"))
            if continue_col.button(
                "继续分析", key=f"director-resume-approved-{project.id}"
            ):
                try:
                    director.resume(project.id, session.id)
                    st.success("Director 已继续并保存新的建议。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(_safe_error(exc, fallback="导演分析未能继续，请稍后重试。"))
        elif session is not None and _status(session) == "ACTIVE":
            if st.button("继续分析", key=f"director-resume-{project.id}"):
                try:
                    director.resume(project.id, session.id)
                    st.success("Director 已继续并保存新的建议。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(_safe_error(exc, fallback="导演分析未能继续，请稍后重试。"))

        with st.expander("高级诊断", expanded=False):
            st.caption(f"Canonical action · {action}")
            st.caption(f"原始建议 · {reason}")
            if target_id:
                st.caption(f"Target id · {target_id}")

    blockers = readiness.get("blocked_reasons", []) or []
    if state.get("qc_failures"):
        blockers = list(blockers) + ["qc failures require review"]
    if blockers:
        render_actionable_blockers(blockers, project_id=project.id)
    else:
        st.success("当前没有已知阻塞。")

    high_risk = producer.high_risk_shots(project.id)
    with st.container(border=True):
        st.markdown("### 制作风险")
        if high_risk:
            st.write(f"高风险镜头 {len(high_risk)} 个")
            st.caption("已标记的高风险镜头会在镜头编辑器中逐项显示。")
        else:
            st.caption("暂无标记为 HIGH 的镜头。")

    with st.expander("高级信息 · 最近导演决策", expanded=False):
        if session is None:
            st.caption("尚未运行导演检查。")
        else:
            for decision in reversed(
                director.list_decisions(project.id, session.id)[-10:]
            ):
                rec = decision.recommendation
                st.markdown(
                    f"**{rec.action}** · {_decision_status_label(decision)} · {decision.project_state}"
                )
                st.caption(rec.reason[:220])


def render() -> None:
    page_header(
        "分镜",
        "STORYBOARD WORKSPACE",
        "把已确认的结构化剧本拆成可编辑、可审核、可执行的镜头序列。",
    )
    project = current_project_or_stop()
    render_project_context(
        project, stage="分镜", next_action="检查并确认分镜", next_page="production"
    )
    _render_shot_activity(project)
    st.markdown('<span class="aidrama-storyboard-workstation-marker" aria-hidden="true"></span>', unsafe_allow_html=True)
    main_col, rail_col = st.columns([3.1, 1], gap="large")
    with main_col:
        tabs = st.tabs(["Storyboard", "镜头编辑器", "时长规划", "Generation Brief"])
        with tabs[0]:
            _render_shot_plan(project, view="Storyboard")
        with tabs[1]:
            _render_shot_plan(project, view="镜头编辑器")
        with tabs[2]:
            _render_shot_plan(project, view="时长规划")
        with tabs[3]:
            _render_shot_plan(project, view="Generation Brief")
    with rail_col:
        _render_director_expanded_summary(project)
        # Closed is the safe 1366 default. CSS expands this rail at desktop
        # workstation widths without relying on a guessed server viewport.
        with st.expander("AI 导演", expanded=False):
            st.markdown(
                '<span class="aidrama-director-collapsed-marker" aria-hidden="true"></span>',
                unsafe_allow_html=True,
            )
            _render_director_console(project, compact=True)
