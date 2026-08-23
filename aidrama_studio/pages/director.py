from __future__ import annotations
import sys
import ast
from pathlib import Path

# Streamlit may execute navigation pages with ``pages/`` as sys.path[0].
# Keep package imports stable across reruns and page switches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st
from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import ScriptService
from aidrama_studio.domain import ShotStatus

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
            shot["risk_level"] = st.selectbox("风险等级", ["LOW", "MEDIUM", "HIGH"], key=f"{pid}-{idx}-risk")
            shot["risk_reasons"] = st.text_area("风险原因", str(_value(shot, "risk_reasons", "")), key=f"{pid}-{idx}-risk-reasons", height=70)
        with right:
            for key, label in (("subject", "主体"), ("action", "动作"), ("expression", "表情"), ("eyeline", "视线"), ("lighting", "灯光"), ("blocking", "调度"), ("dialogue_or_narration", "对白 / 旁白"), ("visual_intent", "视觉意图"), ("transition_hint", "转场提示")):
                shot[key] = st.text_area(label, str(_value(shot, key, "")), key=f"{pid}-{idx}-{key}", height=55)
        shot["status"] = "LOCKED" if st.checkbox("锁定镜头", value=_value(shot, "status", "PLANNED") == "LOCKED", key=f"{pid}-{idx}-locked") else "PLANNED"
        a,b,c = st.columns(3)
        if a.button("↑ 上移", key=f"up-{pid}-{idx}"): _call(service, "move_shot", pid, idx, -1); st.rerun()
        if b.button("↓ 下移", key=f"down-{pid}-{idx}"): _call(service, "move_shot", pid, idx, 1); st.rerun()
        if c.button("保存 Draft", type="primary", key=f"save-{pid}"):
            try:
                canonical = service.get_revision(pid)["content"]
                existing = canonical.shots[idx]
                for field in ("scene_id", "composition", "action", "expression", "dialogue_or_narration", "visual_intent", "transition_hint"):
                    if field in shot: setattr(existing, field, shot[field])
                existing.duration_seconds = float(shot.get("duration_seconds", existing.duration_seconds))
                existing.status = ShotStatus.LOCKED if shot.get("status") == "LOCKED" else ShotStatus.PLANNED
                for field in ("subject", "risk_reasons"):
                    if isinstance(shot.get(field), str):
                        raw = shot[field].replace("，", ",").strip()
                        try:
                            parsed = ast.literal_eval(raw) if raw.startswith("[") else None
                        except (ValueError, SyntaxError):
                            parsed = None
                        values = parsed if isinstance(parsed, list) else raw.split(",")
                        setattr(existing, field, [str(x).strip() for x in values if str(x).strip()])
                service.save_draft(project.id, canonical, revision_id=pid)
                st.toast("Shot Plan Draft 已保存")
            except Exception as exc:
                st.error(f"保存失败：{exc}")


def render() -> None:
    page_header("分镜导演台", "SHOT DIRECTOR", "把剧本拆解为可执行、可审核的镜头列表。")
    project = current_project_or_stop()
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
    m1,m2,m3,m4 = st.columns(4); m1.metric("Total Shots", len(shots_now)); m2.metric("Total Duration", f"{sum(float(_value(s,'duration_seconds',0) or 0) for s in shots_now):g}s"); m3.metric("Target Duration", f"{project.target_duration_seconds}s"); m4.metric("Status", _status(plan))
    st.caption(f"LOW {sum(1 for s in shots_now if _status({'status':_value(s,'risk_level','LOW')}) == 'LOW')} · MEDIUM {sum(1 for s in shots_now if _value(s,'risk_level','LOW') == 'MEDIUM')} · HIGH {sum(1 for s in shots_now if _value(s,'risk_level','LOW') == 'HIGH')} · Locked {sum(1 for s in shots_now if _value(s,'status','PLANNED') == 'LOCKED')}")
    _editor(service, project, plan)
    st.markdown("#### Preview / 版本历史")
    if st.button("打开 Shot List Preview", key=f"preview-{current}"):
        for i, shot in enumerate(_value(plan, "shots", []) or [], 1): st.markdown(f"**{i}. {_value(shot, 'shot_size', '—')} · {_value(shot, 'subject', '—')}** — {_value(shot, 'action', '')}")
    for rev in (_call(service, "list_revisions", project.id) or plans): st.caption(f"v{_value(rev, 'version', '—')} · {_status(rev)}")
    if _status(plan) == "DRAFT" and st.button("Approve Shot Plan", key=f"approve-{current}"):
        try: _call(service, "approve_plan", current) or _call(service, "approve_revision", current); st.success("Shot Plan 已批准"); st.rerun()
        except Exception as exc: st.error(str(exc))
