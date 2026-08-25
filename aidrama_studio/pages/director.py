from __future__ import annotations
import sys
from pathlib import Path

# Streamlit may execute navigation pages with ``pages/`` as sys.path[0].
# Keep package imports stable across reruns and page switches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st
from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    ScriptService,
    DirectorService,
    DirectorServiceError,
    ProducerService,
    ProducerServiceError,
    ShotServiceError,
)
from aidrama_studio.domain import DirectorGoalKind

def _shot_service():
    try:
        from aidrama_studio.services import ShotService
        return ShotService()
    except (ImportError, AttributeError):
        return None

def _call(service, name, *args, **kwargs):
    fn = getattr(service, name, None)
    return fn(*args, **kwargs) if fn else None

def _value(obj, key, default=""):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

def _status(obj):
    return getattr(_value(obj, "status", "DRAFT"), "value", _value(obj, "status", "DRAFT"))


_DECISION_STATUS_LABELS = {
    "RECOMMENDED": "待人工处理",
    "APPROVED": "已批准",
    "REJECTED": "已拒绝",
    "COMPLETED": "已完成",
}


def _decision_status_label(decision) -> str:
    status = _value(decision, "status", "RECOMMENDED")
    value = getattr(status, "value", status)
    return _DECISION_STATUS_LABELS.get(str(value), str(value))

def _editor(service, project, plan):
    pid = _value(plan, "id", "plan"); shots = _value(plan, "shots", []) or []
    labels = [f"Shot {_value(s, 'id', i+1)} · Scene {_value(s, 'scene_id', '—')}" for i, s in enumerate(shots)]
    st.markdown("#### Scene / Shot Navigator")
    selected = st.selectbox("选择镜头", labels or ["暂无 Shot"], key=f"shot-select-{pid}", label_visibility="collapsed")
    idx = labels.index(selected) if labels and selected in labels else 0
    if st.button("＋ 新增 Shot", key=f"add-shot-{pid}"):
        _call(service, "add_shot", pid) or _call(service, "create_shot", pid); st.rerun()
    if not shots: st.info("还没有镜头。点击「新增 Shot」开始手动规划。"); return
    shot = shots[idx]
    st.markdown("#### Shot Inspector")
    with st.container(border=True):
        left, right = st.columns(2)
        with left:
            for key, label in (("scene_id", "Scene ID"), ("shot_size", "景别"), ("camera_angle", "机位角度"), ("camera_movement", "运镜"), ("lens", "镜头焦段"), ("composition", "构图")):
                shot[key] = st.text_input(label, str(_value(shot, key, "")), key=f"{pid}-{idx}-{key}")
            shot["duration_seconds"] = st.number_input("时长（秒）", min_value=0.1, value=float(_value(shot, "duration_seconds", 1) or 1), key=f"{pid}-{idx}-duration")
            risk_options = ["LOW", "MEDIUM", "HIGH"]
            current_risk = getattr(_value(shot,"risk_level","LOW"),"value",_value(shot,"risk_level","LOW"))
            shot["risk_level"] = st.selectbox(
                "风险等级", risk_options,
                index=risk_options.index(current_risk) if current_risk in risk_options else 0,
                key=f"{pid}-{idx}-risk",
            )
            shot["risk_reasons"] = st.text_area("风险原因", str(_value(shot, "risk_reasons", "")), key=f"{pid}-{idx}-risk-reasons", height=70)
        with right:
            for key, label in (("subject", "主体"), ("action", "动作"), ("expression", "表情"), ("eyeline", "视线"), ("lighting", "灯光"), ("blocking", "调度"), ("dialogue_or_narration", "对白 / 旁白"), ("visual_intent", "视觉意图"), ("transition_hint", "转场提示")):
                value = _value(shot,key,"")
                if key == "lighting": value = _value(value,"quality","")
                if key == "blocking": value = _value(value,"movement","")
                shot[key] = st.text_area(label, str(value), key=f"{pid}-{idx}-{key}", height=55)
        shot["status"] = "LOCKED" if st.checkbox("锁定镜头", value=_value(shot, "status", "PLANNED") == "LOCKED", key=f"{pid}-{idx}-locked") else "PLANNED"
        shot["risk_override"] = st.checkbox(
            "人工风险判定",
            value=bool(_value(shot,"risk_override",False)),
            key=f"{pid}-{idx}-risk-override",
        )
        if shot["risk_override"]:
            shot["risk_override_note"] = st.text_input(
                "人工风险判定说明",
                str(_value(shot,"risk_override_note","")),
                key=f"{pid}-{idx}-risk-override-note",
            )
        a,b,c,d = st.columns(4)
        if a.button("↑ 上移", key=f"up-{pid}-{idx}"): _call(service, "move_shot", pid, idx, -1); st.rerun()
        if b.button("↓ 下移", key=f"down-{pid}-{idx}"): _call(service, "move_shot", pid, idx, 1); st.rerun()
        if c.button("保存 Draft", type="primary", key=f"save-{pid}"):
            try:
                fields = (
                    "scene_id","shot_size","camera_angle","camera_movement","lens",
                    "composition","duration_seconds","risk_level","risk_reasons","subject",
                    "action","expression","eyeline","lighting","blocking",
                    "dialogue_or_narration","visual_intent","transition_hint","status",
                    "risk_override","risk_override_note",
                )
                service.update_shot_fields(
                    project.id,pid,_value(shot,"id"),
                    {field:shot[field] for field in fields if field in shot},
                )
                st.toast("Shot Plan Draft 已保存")
            except (ShotServiceError, ValueError, KeyError) as exc:
                st.error(f"保存失败：{exc}")
        if d.button(
            "选择性再生成",
            key=f"regenerate-{pid}-{idx}",
            disabled=shot.get("status") == "LOCKED",
            help="只创建这个镜头的新 DRAFT 候选；不会覆盖其他镜头或历史批准版本。",
        ):
            try:
                result = service.regenerate_shot(project, pid, _value(shot,"id"))
            except (ShotServiceError, ValueError, KeyError) as exc:
                st.error(f"选择性再生成失败：{exc}")
            else:
                st.session_state.director_plan_id = result["id"]
                st.success("已创建新的 Shot Plan DRAFT；原 revision 与锁定镜头保持不变。")
                st.rerun()


def _render_shot_plan(project) -> None:
    """Keep the canonical Shot Plan editor available below the product console."""
    page_header("分镜导演台", "SHOT DIRECTOR", "把剧本拆解为可执行、可审核的镜头列表。")
    approved = ScriptService().get_approved_revision(project.id)
    if not approved:
        st.warning("请先完成并确认结构化剧本。导演台只接受 APPROVED Structured Script。"); return
    service = _shot_service()
    if service is None:
        st.error("ShotService 尚未初始化，请先完成 Task004 服务层。"); return
    plans = _call(service, "list_plans", project.id) or _call(service, "list_revisions", project.id) or []
    st.caption(f"Source Structured Script · v{approved['version']} · APPROVED")
    if service.is_outdated(service.get_revision(st.session_state.get("director_plan_id", ""))) if st.session_state.get("director_plan_id") else False:
        st.warning("该分镜基于旧版剧本（OUTDATED），需要重新同步后才能批准。")
    if st.button("创建手动 Shot Plan", type="primary", key=f"new-plan-{project.id}"):
        plan = _call(service, "create_manual_plan", project, approved) or _call(service, "create_plan", project.id, approved["id"])
        if plan: st.session_state.director_plan_id = _value(plan, "id"); st.rerun()
    if not plans: st.info("暂无 Shot Plan。创建后可在此编辑镜头、风险和调度。"); return
    ids = [_value(p, "id") for p in plans]; current = st.session_state.get("director_plan_id", ids[0]); current = current if current in ids else ids[0]
    plan = plans[ids.index(current)]; st.session_state.director_plan_id = current
    shots_now = _value(plan, "shots", []) or []
    planned_duration = sum(float(_value(s,'duration_seconds',0) or 0) for s in shots_now)
    difference = float(project.target_duration_seconds) - planned_duration
    m1,m2,m3,m4,m5 = st.columns(5)
    m1.metric("Total Shots", len(shots_now))
    m2.metric("Current", f"{planned_duration:g}s")
    m3.metric("Target", f"{project.target_duration_seconds}s")
    m4.metric("Remaining" if difference >= 0 else "Over target", f"{abs(difference):g}s")
    m5.metric("Status", _status(plan))
    st.caption(f"LOW {sum(1 for s in shots_now if _status({'status':_value(s,'risk_level','LOW')}) == 'LOW')} · MEDIUM {sum(1 for s in shots_now if _value(s,'risk_level','LOW') == 'MEDIUM')} · HIGH {sum(1 for s in shots_now if _value(s,'risk_level','LOW') == 'HIGH')} · Locked {sum(1 for s in shots_now if _value(s,'status','PLANNED') == 'LOCKED')}")
    if st.button("AI 时长重平衡建议", key=f"duration-rebalance-{current}"):
        try:
            proposal = service.recommend_duration_rebalance(current, project.target_duration_seconds)
            st.session_state[f"duration-proposal-{current}"] = proposal
        except (ShotServiceError, ValueError, KeyError) as exc:
            st.error(str(exc))
    proposal = st.session_state.get(f"duration-proposal-{current}")
    if proposal:
        if not proposal.get("feasible"):
            st.warning("锁定镜头时长已占满目标预算；建议保持为只读，先显式解锁或调整目标时长。")
        elif not proposal.get("suggestions"):
            st.success("当前时长已经接近目标，无需调整。")
        else:
            st.info("以下仅为建议，不会覆盖手工编辑、锁定镜头或历史批准版本。")
            for item in proposal["suggestions"]:
                st.caption(f"{item['shot_id']} · {item['from_seconds']:g}s → {item['to_seconds']:g}s")
    _editor(service, project, plan)
    st.markdown("#### Preview / 版本历史")
    if st.button("打开 Shot List Preview", key=f"preview-{current}"):
        for i, shot in enumerate(_value(plan, "shots", []) or [], 1): st.markdown(f"**{i}. {_value(shot, 'shot_size', '—')} · {_value(shot, 'subject', '—')}** — {_value(shot, 'action', '')}")
    for rev in (_call(service, "list_revisions", project.id) or plans): st.caption(f"v{_value(rev, 'version', '—')} · {_status(rev)}")
    if _status(plan) == "DRAFT" and st.button("Approve Shot Plan", key=f"approve-{current}"):
        try: _call(service, "approve_plan", current) or _call(service, "approve_revision", current); st.success("Shot Plan 已批准"); st.rerun()
        except (ShotServiceError, ValueError, KeyError) as exc: st.error(str(exc))


def _render_director_console(project) -> None:
    """Render a bounded, durable AI Director / Producer control plane.

    This console is deliberately advisory: it reconstructs canonical project
    state through services and exposes one primary next action.  It never
    invokes a provider or mutates creative truth from the page.
    """
    st.subheader("AI 导演 / 制片")
    st.caption(f"项目：{project.title} · 状态由 Story、Script、Shot、资产、生产与 QC 汇总")
    director = DirectorService()
    producer = ProducerService()
    try:
        state = director.inspect_project(project.id)
    except (DirectorServiceError, ProducerServiceError) as exc:
        st.warning("暂时无法读取导演状态，请检查项目数据后重试。")
        st.caption(str(exc)[:180])
        return

    state_label = str(state.get("project_state", "UNKNOWN"))
    readiness = state.get("readiness") or {}
    try:
        producer_recommendations = producer.recommendations(project.id)
    except ProducerServiceError as exc:
        producer_recommendations = []
        st.caption(f"Producer 建议暂不可用：{str(exc)[:180]}")

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
    reason = getattr(recommendation, "reason", None) or "先运行一次有界的导演检查，获得结构化下一步建议。"
    target_id = getattr(recommendation, "target_id", None)
    pending_decision = None
    approved_decision = None
    latest_decision = None
    if session is not None:
        session_decisions = director.list_decisions(project.id, session.id)
        latest_decision = session_decisions[-1] if session_decisions else None
        pending_decision = next((item for item in reversed(session_decisions) if item.status.value == "RECOMMENDED"), None)
        approved_decision = next((item for item in reversed(session_decisions) if item.status.value == "APPROVED"), None)

    metric_cols = st.columns(4)
    metric_cols[0].metric("当前阶段", state_label)
    metric_cols[1].metric("生产就绪", "是" if bool(readiness.get("ready")) else "否")
    metric_cols[2].metric("高风险镜头", len(producer.high_risk_shots(project.id)))
    metric_cols[3].metric("QC 失败", len(state.get("qc_failures", [])))

    with st.container(border=True):
        st.markdown("### 下一步建议")
        st.markdown(f"**{action}**")
        st.write(reason)
        if target_id:
            st.caption(f"目标：{target_id}")
        st.info("导演建议不会绕过 Story、Script、Shot Plan、资产锁定或人审批准 gates。")
        if latest_decision is not None:
            latest_status = _decision_status_label(latest_decision)
            if latest_decision.status.value == "APPROVED":
                st.success("最近建议已批准：批准只记录人工审核，不会自动执行建议；你可以标记完成或继续分析。")
            elif latest_decision.status.value == "REJECTED":
                st.warning("最近建议已拒绝：未执行任何自动动作；你可以继续分析当前项目。")
            elif latest_decision.status.value == "COMPLETED":
                st.success("最近建议已完成：Director 可以继续分析当前项目。")
            else:
                st.caption(f"最近建议状态：{latest_status}，需要人工处理后才能继续。")
        if st.button("分析当前项目", type="primary", key=f"director-run-{project.id}"):
            try:
                if session is None:
                    session = director.start_session(project.id, DirectorGoalKind.MAKE_PRODUCTION_READY, max_steps=1)
                    st.session_state[f"director-session-{project.id}"] = session.id
                decision = director.run(project.id, session.id)
                st.session_state[f"director-last-action-{project.id}"] = decision.recommendation.action
                st.success("导演决策已保存，可在下方查看。")
                st.rerun()
            except (DirectorServiceError, ValueError, KeyError) as exc:
                st.warning("导演检查未完成，请先处理当前阻塞项。")
                st.caption(str(exc)[:180])
        if pending_decision is not None:
            st.caption("该建议需要人工确认；批准只记录审核，不会自动批准 Story、Script、资产或调用 Provider。")
            approve_col, reject_col = st.columns(2)
            if approve_col.button("确认已处理 / 批准建议", key=f"director-approve-{pending_decision.id}"):
                try:
                    director.approve_decision(project.id, pending_decision.id)
                    st.success("建议已记录为批准，Director 现可继续分析。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(str(exc))
            if reject_col.button("拒绝建议", key=f"director-reject-{pending_decision.id}"):
                try:
                    director.reject_decision(project.id, pending_decision.id)
                    st.info("建议已拒绝；未执行任何自动动作。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(str(exc))
        elif approved_decision is not None:
            complete_col, continue_col = st.columns(2)
            if complete_col.button("标记已处理 / 完成建议", key=f"director-complete-{approved_decision.id}"):
                try:
                    director.complete_decision(project.id, approved_decision.id)
                    st.success("建议生命周期已完成。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(str(exc))
            if continue_col.button("继续分析", key=f"director-resume-approved-{project.id}"):
                try:
                    director.resume(project.id, session.id)
                    st.success("Director 已继续并保存新的建议。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(str(exc))
        elif session is not None and session.status.value == "ACTIVE":
            if st.button("继续分析", key=f"director-resume-{project.id}"):
                try:
                    director.resume(project.id, session.id)
                    st.success("Director 已继续并保存新的建议。")
                    st.rerun()
                except DirectorServiceError as exc:
                    st.error(str(exc))

    blockers = readiness.get("blocked_reasons", []) or []
    with st.container(border=True):
        st.markdown("### 当前阻塞")
        if blockers:
            for blocker in blockers[:5]:
                st.markdown(f"- {str(blocker)[:220]}")
        elif state.get("qc_failures"):
            st.markdown("- 存在 QC 失败，需要人工审查。")
        else:
            st.success("当前没有已知阻塞。")

    high_risk = producer.high_risk_shots(project.id)
    with st.container(border=True):
        st.markdown("### 制作风险")
        if high_risk:
            st.write(f"高风险镜头 {len(high_risk)} 个")
            st.caption("、".join(high_risk[:8]))
        else:
            st.caption("暂无标记为 HIGH 的镜头。")

    with st.expander("最近导演决策", expanded=False):
        if session is None:
            st.caption("尚未运行导演检查。")
        else:
            for decision in reversed(director.list_decisions(project.id, session.id)[-10:]):
                rec = decision.recommendation
                st.markdown(f"**{rec.action}** · {_decision_status_label(decision)} · {decision.project_state}")
                st.caption(rec.reason[:220])


def render() -> None:
    project = current_project_or_stop()
    tabs = st.tabs(["AI 导演 / 制片", "Shot Plan / 分镜编辑"])
    with tabs[0]:
        _render_director_console(project)
    with tabs[1]:
        _render_shot_plan(project)
