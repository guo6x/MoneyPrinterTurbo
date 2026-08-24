"""Persistent, bounded AI Director decision service.

The Director is an advisory control plane.  It inspects canonical Story,
Script, Shot, Reference, Production, QC and Post records, then records one
bounded recommendation.  It never performs the recommended creative or
provider action itself.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import (
    DirectorDecision,
    DirectorDecisionEvent,
    DirectorDecisionStatus,
    DirectorGoal,
    DirectorGoalKind,
    DirectorGoalStatus,
    DirectorRecommendation,
    DirectorSession,
    DirectorSessionStatus,
    ScriptRevisionStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .current_state import CurrentProductionStateService
from .production import ProductionService
from .production_qc import ProductionQCService
from .reference_assets import ReferenceAssetService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DirectorServiceError(RuntimeError):
    pass


class DirectorService:
    """Inspect canonical state and persist one bounded recommendation."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        reference_service: ReferenceAssetService | None = None,
        qc_service: ProductionQCService | None = None,
        current_state_service: CurrentProductionStateService | None = None,
    ):
        self.repository = repository or ProjectRepository()
        self.reference_service = reference_service or ReferenceAssetService(self.repository)
        self.production_service = production_service or ProductionService(
            self.repository, reference_service=self.reference_service
        )
        self.qc_service = qc_service or ProductionQCService(self.repository)
        self.current_state_service = current_state_service or CurrentProductionStateService(self.repository)

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise DirectorServiceError(f"项目不存在: {project_id}")
        return project

    def _get_session(self, session_id: str, project_id: str | None = None) -> DirectorSession:
        session = self.repository.get_director_session(session_id)
        if session is None or (project_id is not None and session.project_id != project_id):
            raise DirectorServiceError("DirectorSession 不属于该项目")
        return session

    def _get_decision(self, project_id: str, decision_id: str) -> DirectorDecision:
        self._require_project(project_id)
        decision = self.repository.get_director_decision(decision_id)
        if decision is None or decision.project_id != project_id:
            raise DirectorServiceError("DirectorDecision 不属于该项目")
        session = self._get_session(decision.session_id, project_id)
        goal = self.repository.get_director_goal(decision.goal_id)
        if goal is None or goal.project_id != project_id or goal.session_id != session.id:
            raise DirectorServiceError("DirectorDecision goal provenance 无效")
        return decision

    def start_session(
        self,
        project_id: str,
        goal: DirectorGoalKind = DirectorGoalKind.MAKE_PRODUCTION_READY,
        *,
        max_steps: int = 1,
    ) -> DirectorSession:
        self._require_project(project_id)
        if max_steps < 1 or max_steps > 100:
            raise DirectorServiceError("Director goal max_steps 必须在 1 到 100 之间")
        now = _now()
        session = DirectorSession(
            id=uuid4().hex,
            project_id=project_id,
            current_goal=goal,
            created_at=now,
            updated_at=now,
        )
        self.repository.create_director_session(session)
        self.repository.create_director_goal(
            DirectorGoal(
                id=uuid4().hex,
                session_id=session.id,
                project_id=project_id,
                goal=goal,
                max_steps=max_steps,
                created_at=now,
            )
        )
        return session

    def list_sessions(self, project_id: str) -> list[DirectorSession]:
        self._require_project(project_id)
        return self.repository.list_director_sessions(project_id)

    def get_session(self, project_id: str, session_id: str) -> DirectorSession:
        self._require_project(project_id)
        return self._get_session(session_id, project_id)

    def list_decisions(self, project_id: str, session_id: str | None = None) -> list[DirectorDecision]:
        self._require_project(project_id)
        if session_id is not None:
            self._get_session(session_id, project_id)
        return self.repository.list_director_decisions(project_id, session_id)

    def list_decision_events(self, project_id: str, decision_id: str | None = None) -> list[DirectorDecisionEvent]:
        if decision_id is not None:
            self._get_decision(project_id, decision_id)
        else:
            self._require_project(project_id)
        return self.repository.list_director_decision_events(project_id, decision_id)

    def reconstruct(self, project_id: str, session_id: str | None = None) -> dict[str, object]:
        """Rebuild the Director view after a cold process restart."""

        self._require_project(project_id)
        if session_id:
            session = self._get_session(session_id, project_id)
        else:
            sessions = self.list_sessions(project_id)
            session = sessions[0] if sessions else None
        if session is None:
            return {"session": None, "goal": None, "last_decision": None, "decisions": [], "events": []}
        goals = self.repository.list_director_goals(session.id)
        decisions = self.repository.list_director_decisions(project_id, session.id)
        events = self.repository.list_director_decision_events(project_id)
        return {
            "session": session,
            "goal": goals[-1] if goals else None,
            "last_decision": decisions[-1] if decisions else None,
            "decisions": decisions,
            "events": [event for event in events if event.session_id == session.id],
        }

    @staticmethod
    def _latest_approved(revisions: list[dict[str, object]], status) -> dict[str, object] | None:
        return next((item for item in revisions if item["status"] is status), None)

    def inspect_project(self, project_id: str) -> dict[str, object]:
        project = self._require_project(project_id)
        stories = self.repository.list_story_revisions(project_id)
        scripts = self.repository.list_script_revisions(project_id)
        plans = self.repository.list_shot_revisions(project_id)
        story = self._latest_approved(stories, StoryRevisionStatus.APPROVED)
        script = self._latest_approved(scripts, ScriptRevisionStatus.APPROVED)
        plan = self._latest_approved(plans, ShotRevisionStatus.APPROVED)
        readiness = self.production_service.calculate_production_readiness(project_id, plan["id"] if plan else None)
        current = self.current_state_service.derive(project_id)
        jobs = self.production_service.list_jobs(project_id)
        current_job = current.job
        current_executions = self.repository.list_production_executions(current_job.id) if current_job else []
        current_qcs = [
            result
            for execution in current_executions
            for result in self.repository.list_production_qc_results(project_id, execution.id)
        ]
        assemblies = self.repository.list_final_assemblies(project_id)
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "project_state": self._state_name(story, script, plan, readiness, current),
            "story_revision_id": story["id"] if story else None,
            "script_revision_id": script["id"] if script else None,
            "shot_plan_revision_id": plan["id"] if plan else None,
            "readiness": readiness,
            "jobs": jobs,
            "current_job_id": current.current_job_id,
            "current_executions": current_executions,
            "executions": current_executions,
            "current_state": current,
            "qc_failures": [result for result in current_qcs if result.status.value == "QC_FAILED"],
            "current_qc_blockers": list(current.qc_blockers),
            "historical_qc_failures": current.historical_qc_failures,
            "final_assemblies": assemblies,
        }

    @staticmethod
    def _state_name(story, script, plan, readiness, current) -> str:
        if story is None:
            return "STORY"
        if script is None or script.get("source_story_revision_id") != story["id"]:
            return "SCRIPT"
        if plan is None or plan.get("source_script_revision_id") != script["id"]:
            return "SHOT_PLAN"
        if not readiness.get("ready"):
            return "REFERENCES"
        if current.job is None or not current.production_complete:
            return "PRODUCTION"
        if current.qc_blockers:
            return "QC"
        if current.final_readiness is None or not current.final_readiness.ready:
            return "FINAL_ASSEMBLY"
        if not current.post_production_ready:
            return "POST_PRODUCTION"
        return "COMPLETED"

    @staticmethod
    def _goal_satisfied(state: dict[str, object], goal: DirectorGoalKind) -> bool:
        readiness = state["readiness"]
        current = state["current_state"]
        if goal is DirectorGoalKind.COMPLETE_STORY:
            return state["story_revision_id"] is not None
        if goal is DirectorGoalKind.COMPLETE_SCRIPT:
            return state["script_revision_id"] is not None and state["project_state"] not in {"STORY", "SCRIPT"}
        if goal is DirectorGoalKind.COMPLETE_SHOT_PLAN:
            return state["shot_plan_revision_id"] is not None and state["project_state"] not in {"STORY", "SCRIPT", "SHOT_PLAN"}
        if goal in {DirectorGoalKind.COMPLETE_REFERENCES, DirectorGoalKind.MAKE_PRODUCTION_READY}:
            return bool(readiness.get("ready"))
        if goal is DirectorGoalKind.COMPLETE_PRODUCTION:
            return bool(current.production_complete)
        if goal is DirectorGoalKind.RESOLVE_QC_BLOCKER:
            return not bool(current.qc_blockers)
        if goal is DirectorGoalKind.MAKE_FINAL_ASSEMBLY_READY:
            return bool(current.final_readiness and current.final_readiness.ready)
        if goal is DirectorGoalKind.COMPLETE_POST_PRODUCTION:
            return bool(current.post_production_ready)
        return False

    def _recommend(self, state: dict[str, object], goal: DirectorGoalKind) -> DirectorRecommendation:
        readiness = state["readiness"]
        current = state["current_state"]
        if goal is DirectorGoalKind.COMPLETE_STORY:
            return DirectorRecommendation(action="APPROVE_STORY_BIBLE", reason="请先完成并批准 Story Bible", requires_human_approval=True)
        if goal is DirectorGoalKind.COMPLETE_SCRIPT:
            if state["story_revision_id"] is None:
                return DirectorRecommendation(action="APPROVE_STORY_BIBLE", reason="Structured Script 依赖 APPROVED Story Bible", requires_human_approval=True)
            return DirectorRecommendation(action="APPROVE_STRUCTURED_SCRIPT", reason="请批准当前 Structured Script", requires_human_approval=True)
        if goal is DirectorGoalKind.COMPLETE_SHOT_PLAN:
            if state["story_revision_id"] is None:
                return DirectorRecommendation(action="APPROVE_STORY_BIBLE", reason="Shot Plan 依赖 APPROVED Story Bible", requires_human_approval=True)
            if state["script_revision_id"] is None:
                return DirectorRecommendation(action="APPROVE_STRUCTURED_SCRIPT", reason="Shot Plan 依赖 APPROVED Structured Script", requires_human_approval=True)
            return DirectorRecommendation(action="APPROVE_SHOT_PLAN", reason="请批准当前 Shot Plan", requires_human_approval=True)
        if state["story_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_STORY_BIBLE", reason="请先完成并批准 Story Bible", requires_human_approval=True)
        if state["script_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_STRUCTURED_SCRIPT", reason="请先完成并批准 Structured Script", requires_human_approval=True)
        if state["shot_plan_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_SHOT_PLAN", reason="请先完成并批准 Shot Plan", requires_human_approval=True)
        if goal in {DirectorGoalKind.COMPLETE_REFERENCES, DirectorGoalKind.MAKE_PRODUCTION_READY}:
            missing_chars = readiness.get("missing_character_references", [])
            if missing_chars:
                return DirectorRecommendation(action="LOCK_CHARACTER_REFERENCE", target_id=str(missing_chars[0]), reason="生产准备缺少已锁定的角色参考资产", requires_human_approval=True)
            missing_locations = readiness.get("missing_location_references", [])
            if missing_locations:
                return DirectorRecommendation(action="LOCK_LOCATION_REFERENCE", target_id=str(missing_locations[0]), reason="生产准备缺少已锁定的场景参考资产", requires_human_approval=True)
        if goal is DirectorGoalKind.COMPLETE_REFERENCES:
            return DirectorRecommendation(action="REVIEW_REFERENCE_READINESS", reason="参考资产覆盖已满足，确认后结束该目标", requires_human_approval=True)
        if goal is DirectorGoalKind.MAKE_PRODUCTION_READY:
            if current.job is None:
                return DirectorRecommendation(action="START_PRODUCTION", reason="项目已满足生产准备条件，可创建 Production Job", requires_human_approval=True)
            return DirectorRecommendation(action="CONFIRM_PRODUCTION_READINESS", reason="当前 Production Job 已具备生产前置条件", requires_human_approval=True)
        if goal is DirectorGoalKind.COMPLETE_PRODUCTION:
            if current.job is None:
                return DirectorRecommendation(action="START_PRODUCTION", reason="尚未创建当前 Production Job", requires_human_approval=True)
            if current.qc_blockers:
                return DirectorRecommendation(action="REVIEW_QC_FAILURE", target_id=str(current.qc_blockers[0]), reason="当前镜头没有 qualified QC source，需要人工审查", requires_human_approval=True)
            if not current.production_complete:
                return DirectorRecommendation(action="RESUME_PRODUCTION", reason="当前 Production Job 仍有待完成镜头", requires_human_approval=True)
        if goal is DirectorGoalKind.RESOLVE_QC_BLOCKER:
            if current.qc_blockers:
                return DirectorRecommendation(action="REVIEW_QC_FAILURE", target_id=str(current.qc_blockers[0]), reason="当前 QC blocker 需要人工审查或重试", requires_human_approval=True)
            return DirectorRecommendation(action="CONFIRM_QC_RESOLUTION", reason="当前没有未解决的 QC blocker", requires_human_approval=True)
        if goal is DirectorGoalKind.MAKE_FINAL_ASSEMBLY_READY:
            if not current.production_complete:
                return DirectorRecommendation(action="RESUME_PRODUCTION", reason="必须先完成当前 Production Job", requires_human_approval=True)
            if current.qc_blockers:
                return DirectorRecommendation(action="REVIEW_QC_FAILURE", target_id=str(current.qc_blockers[0]), reason="当前 QC blocker 阻止 Final Assembly", requires_human_approval=True)
            return DirectorRecommendation(action="CREATE_FINAL_ASSEMBLY", reason="当前镜头已 qualified，可创建不可变 Final Assembly manifest", requires_human_approval=True)
        if goal is DirectorGoalKind.COMPLETE_POST_PRODUCTION:
            if not current.final_readiness or not current.final_readiness.ready:
                return DirectorRecommendation(action="MAKE_FINAL_ASSEMBLY_READY", reason="必须先冻结 qualified Final Assembly", requires_human_approval=True)
            return DirectorRecommendation(action="START_POST_PRODUCTION", reason="Final Assembly 已就绪，可进入后期流程", requires_human_approval=True)
        return DirectorRecommendation(action="REVIEW_PROJECT_STATE", reason="请人工检查当前项目状态", requires_human_approval=True)

    def run(self, project_id: str, session_id: str) -> DirectorDecision:
        session = self._get_session(session_id, project_id)
        if session.status is not DirectorSessionStatus.ACTIVE:
            raise DirectorServiceError("DirectorSession 已暂停、阻塞或完成")
        goals = self.repository.list_director_goals(session.id)
        goal = goals[-1] if goals else self.repository.create_director_goal(
            DirectorGoal(id=uuid4().hex, session_id=session.id, project_id=project_id, goal=session.current_goal, created_at=_now())
        )
        if goal.status in {DirectorGoalStatus.COMPLETED, DirectorGoalStatus.CANCELLED}:
            raise DirectorServiceError("当前 Director goal 已结束")
        if goal.completed_steps >= goal.max_steps:
            raise DirectorServiceError("bounded Director goal 已达到 max_steps；请显式 resume 开启下一段 bounded goal")
        state = self.inspect_project(project_id)
        satisfied = self._goal_satisfied(state, goal.goal)
        recommendation = DirectorRecommendation(action="GOAL_COMPLETE", reason=f"{goal.goal.value} 已满足完成条件", requires_human_approval=False) if satisfied else self._recommend(state, goal.goal)
        now = _now()
        decision = self.repository.create_director_decision(
            DirectorDecision(id=uuid4().hex, session_id=session.id, project_id=project_id, goal_id=goal.id, status=DirectorDecisionStatus.RECOMMENDED, project_state=str(state["project_state"]), recommendation=recommendation, state_snapshot=self._json_safe(state), created_at=now)
        )
        steps = goal.completed_steps + 1
        if satisfied:
            updated_goal = goal.model_copy(update={"completed_steps": steps, "status": DirectorGoalStatus.COMPLETED, "finished_at": now})
            updated_session = session.model_copy(update={"status": DirectorSessionStatus.COMPLETED, "blocking_reason": "", "pending_recommendation": None, "updated_at": now})
        elif recommendation.requires_human_approval:
            updated_goal = goal.model_copy(update={"completed_steps": steps, "status": DirectorGoalStatus.BLOCKED})
            updated_session = session.model_copy(update={"status": DirectorSessionStatus.BLOCKED, "blocking_reason": recommendation.reason, "pending_recommendation": recommendation, "updated_at": now})
        else:
            updated_goal = goal.model_copy(update={"completed_steps": steps, "status": DirectorGoalStatus.ACTIVE})
            updated_session = session.model_copy(update={"status": DirectorSessionStatus.ACTIVE, "blocking_reason": "", "pending_recommendation": None, "updated_at": now})
        self.repository.update_director_goal(updated_goal)
        self.repository.update_director_session(updated_session)
        return decision

    def _transition(self, project_id: str, decision_id: str, to_status: DirectorDecisionStatus, event_type: str, *, metadata: dict[str, object] | None = None) -> DirectorDecision:
        decision = self._get_decision(project_id, decision_id)
        current = decision.status
        if to_status is DirectorDecisionStatus.APPROVED:
            if current is DirectorDecisionStatus.APPROVED:
                return decision
            if current is not DirectorDecisionStatus.RECOMMENDED:
                raise DirectorServiceError("只有 RECOMMENDED decision 可以批准")
        elif to_status is DirectorDecisionStatus.REJECTED:
            if current is DirectorDecisionStatus.REJECTED:
                return decision
            if current is not DirectorDecisionStatus.RECOMMENDED:
                raise DirectorServiceError("只有 RECOMMENDED decision 可以拒绝")
        elif to_status is DirectorDecisionStatus.COMPLETED:
            if current is DirectorDecisionStatus.COMPLETED:
                return decision
            if current is not DirectorDecisionStatus.APPROVED:
                raise DirectorServiceError("只有 APPROVED decision 可以标记完成")
        else:
            raise DirectorServiceError("不支持的 Director decision transition")
        if to_status is DirectorDecisionStatus.COMPLETED:
            state = self.inspect_project(project_id)
            if not self._canonical_action_satisfied(decision, state):
                raise DirectorServiceError(
                    "canonical action 尚未完成；APPROVED 只表示人工同意，不能替代真实业务操作"
                )
        now = _now()
        session = self._get_session(decision.session_id, project_id)
        goal = self.repository.get_director_goal(decision.goal_id)
        if goal is None:
            raise DirectorServiceError("Director goal 不存在")
        if to_status is DirectorDecisionStatus.COMPLETED:
            goal_complete = self._goal_satisfied(self.inspect_project(project_id), goal.goal)
            new_goal = goal.model_copy(update={"status": DirectorGoalStatus.COMPLETED if goal_complete else DirectorGoalStatus.ACTIVE, "finished_at": now if goal_complete else None})
            new_session_status = DirectorSessionStatus.COMPLETED if goal_complete else DirectorSessionStatus.ACTIVE
            new_blocking_reason = ""
            new_pending = None
        elif to_status is DirectorDecisionStatus.APPROVED:
            # Approval is review-only.  Keep the session blocked until the
            # user performs the canonical action and explicitly completes the
            # decision.
            new_goal = goal.model_copy(update={"status": DirectorGoalStatus.BLOCKED})
            new_session_status = DirectorSessionStatus.BLOCKED
            new_blocking_reason = decision.recommendation.reason
            new_pending = decision.recommendation
        else:
            new_goal = goal.model_copy(update={"status": DirectorGoalStatus.ACTIVE})
            new_session_status = DirectorSessionStatus.ACTIVE
            new_blocking_reason = ""
            new_pending = None
        event = DirectorDecisionEvent(
            id=uuid4().hex,
            decision_id=decision.id,
            session_id=decision.session_id,
            project_id=project_id,
            from_status=current,
            to_status=to_status,
            event_type=event_type,
            metadata=metadata or {},
            created_at=now,
        )
        updated_session = session.model_copy(
            update={
                "status": new_session_status,
                "blocking_reason": new_blocking_reason,
                "pending_recommendation": new_pending,
                "updated_at": now,
            }
        )
        return self.repository.transition_director(
            decision=decision,
            event=event,
            goal=new_goal,
            session=updated_session,
        )

    def _canonical_action_satisfied(self, decision: DirectorDecision, state: dict[str, object]) -> bool:
        """Check the real domain fact named by a Director recommendation."""
        action = str(decision.recommendation.action or "").upper()
        readiness = state["readiness"]
        current = state["current_state"]
        if action in {"GOAL_COMPLETE", "REVIEW_PROJECT_STATE"}:
            goal = self.repository.get_director_goal(decision.goal_id)
            return bool(goal and self._goal_satisfied(state, goal.goal))
        if action in {"APPROVE_STORY_BIBLE", "APPROVE_STRUCTURED_SCRIPT", "APPROVE_SHOT_PLAN"}:
            return {
                "APPROVE_STORY_BIBLE": state.get("story_revision_id"),
                "APPROVE_STRUCTURED_SCRIPT": state.get("script_revision_id"),
                "APPROVE_SHOT_PLAN": state.get("shot_plan_revision_id"),
            }[action] is not None
        if action == "LOCK_CHARACTER_REFERENCE":
            target = decision.recommendation.target_id
            return target is not None and target not in set(readiness.get("missing_character_references", []))
        if action == "LOCK_LOCATION_REFERENCE":
            target = decision.recommendation.target_id
            return target is not None and target not in set(readiness.get("missing_location_references", []))
        if action in {"REVIEW_REFERENCE_READINESS", "CONFIRM_PRODUCTION_READINESS"}:
            return bool(readiness.get("ready")) and (
                action != "CONFIRM_PRODUCTION_READINESS" or current.job is not None
            )
        if action == "START_PRODUCTION":
            return current.job is not None
        if action in {"RESUME_PRODUCTION", "COMPLETE_PRODUCTION"}:
            return bool(current.production_complete)
        if action in {"REVIEW_QC_FAILURE", "CONFIRM_QC_RESOLUTION"}:
            return not bool(current.qc_blockers)
        if action in {"CREATE_FINAL_ASSEMBLY", "MAKE_FINAL_ASSEMBLY_READY"}:
            if not current.final_readiness or not current.final_readiness.ready or current.job is None:
                return False
            return any(
                item.status.value in {"READY", "SUCCEEDED"}
                for item in self.repository.list_final_assemblies(state["project_id"], current.job.id)
            )
        if action == "START_POST_PRODUCTION":
            return bool(current.post_production_ready)
        return self._goal_satisfied(state, self.repository.get_director_goal(decision.goal_id).goal)

    def approve_decision(self, project_id: str, decision_id: str) -> DirectorDecision:
        return self._transition(project_id, decision_id, DirectorDecisionStatus.APPROVED, "APPROVED")

    def reject_decision(self, project_id: str, decision_id: str, *, reason: str = "") -> DirectorDecision:
        return self._transition(project_id, decision_id, DirectorDecisionStatus.REJECTED, "REJECTED", metadata={"reason": reason[:4000]})

    def complete_decision(self, project_id: str, decision_id: str) -> DirectorDecision:
        return self._transition(project_id, decision_id, DirectorDecisionStatus.COMPLETED, "COMPLETED")

    def resume(self, project_id: str, session_id: str) -> DirectorDecision:
        session = self._get_session(session_id, project_id)
        if session.status in {DirectorSessionStatus.COMPLETED, DirectorSessionStatus.PAUSED}:
            raise DirectorServiceError("DirectorSession 已结束或暂停")
        if session.status is DirectorSessionStatus.BLOCKED and session.pending_recommendation is not None:
            raise DirectorServiceError("等待人工处理当前建议后才能 resume DirectorSession")
        goals = self.repository.list_director_goals(session.id)
        goal = goals[-1] if goals else None
        if goal is not None and goal.completed_steps >= goal.max_steps and goal.status is not DirectorGoalStatus.COMPLETED:
            goal = self.repository.create_director_goal(DirectorGoal(id=uuid4().hex, session_id=session.id, project_id=project_id, goal=goal.goal, max_steps=goal.max_steps, created_at=_now()))
        if session.status is DirectorSessionStatus.BLOCKED:
            session = self.repository.update_director_session(session.model_copy(update={"status": DirectorSessionStatus.ACTIVE, "blocking_reason": "", "pending_recommendation": None, "updated_at": _now()}))
        return self.run(project_id, session.id)

    start_next_goal = resume

    @staticmethod
    def _json_safe(state: dict[str, object]) -> dict[str, object]:
        def default(value: object):
            if is_dataclass(value):
                return asdict(value)
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if hasattr(value, "value"):
                return value.value
            return str(value)

        return json.loads(json.dumps(state, default=default, ensure_ascii=False))


__all__ = ["DirectorService", "DirectorServiceError"]
