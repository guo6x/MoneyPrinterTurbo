"""Persistent, bounded AI Director decision service."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import (
    DirectorDecision,
    DirectorDecisionStatus,
    DirectorGoal,
    DirectorGoalKind,
    DirectorGoalStatus,
    DirectorRecommendation,
    DirectorSession,
    DirectorSessionStatus,
    ProjectStatus,
    ShotRevisionStatus,
    StoryRevisionStatus,
    ScriptRevisionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .production import ProductionService
from .production_qc import ProductionQCService
from .reference_assets import ReferenceAssetService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DirectorServiceError(RuntimeError):
    pass


class DirectorService:
    """Inspect canonical project state and persist one bounded recommendation.

    ``run`` performs exactly one inspection/decision step.  It never executes
    a provider call or silently changes creative truth; approval-gated actions
    are left as structured recommendations for the user.
    """

    def __init__(self, repository: ProjectRepository | None = None, *, production_service: ProductionService | None = None, reference_service: ReferenceAssetService | None = None, qc_service: ProductionQCService | None = None):
        self.repository = repository or ProjectRepository()
        self.reference_service = reference_service or ReferenceAssetService(self.repository)
        self.production_service = production_service or ProductionService(self.repository, reference_service=self.reference_service)
        self.qc_service = qc_service or ProductionQCService(self.repository)

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

    def start_session(self, project_id: str, goal: DirectorGoalKind = DirectorGoalKind.MAKE_PRODUCTION_READY, *, max_steps: int = 1) -> DirectorSession:
        self._require_project(project_id)
        if max_steps < 1 or max_steps > 100:
            raise DirectorServiceError("Director goal max_steps 必须在 1 到 100 之间")
        now = _now()
        session = DirectorSession(id=uuid4().hex, project_id=project_id, current_goal=goal, created_at=now, updated_at=now)
        self.repository.create_director_session(session)
        self.repository.create_director_goal(DirectorGoal(id=uuid4().hex, session_id=session.id, project_id=project_id, goal=goal, max_steps=max_steps, created_at=now))
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

    def reconstruct(self, project_id: str, session_id: str | None = None) -> dict[str, object]:
        """Rebuild the Director view after a cold process restart."""
        self._require_project(project_id)
        session = self._get_session(session_id, project_id) if session_id else (self.list_sessions(project_id)[0] if self.list_sessions(project_id) else None)
        if session is None:
            return {"session": None, "goal": None, "last_decision": None, "decisions": []}
        goals = self.repository.list_director_goals(session.id)
        decisions = self.repository.list_director_decisions(project_id, session.id)
        return {"session": session, "goal": goals[-1] if goals else None, "last_decision": decisions[-1] if decisions else None, "decisions": decisions}

    def inspect_project(self, project_id: str) -> dict[str, object]:
        project = self._require_project(project_id)
        stories = self.repository.list_story_revisions(project_id)
        scripts = self.repository.list_script_revisions(project_id)
        plans = self.repository.list_shot_revisions(project_id)
        story = next((item for item in stories if item["status"] is StoryRevisionStatus.APPROVED), None)
        script = next((item for item in scripts if item["status"] is ScriptRevisionStatus.APPROVED), None)
        plan = next((item for item in plans if item["status"] is ShotRevisionStatus.APPROVED), None)
        readiness = self.production_service.calculate_production_readiness(project_id, plan["id"] if plan else None)
        jobs = self.production_service.list_jobs(project_id)
        executions = [execution for job in jobs for execution in self.repository.list_production_executions(job.id)]
        qcs = [result for execution in executions for result in self.repository.list_production_qc_results(project_id, execution.id)]
        assemblies = self.repository.list_final_assemblies(project_id)
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "project_state": self._state_name(project, story, script, plan, readiness, jobs, qcs, assemblies),
            "story_revision_id": story["id"] if story else None,
            "script_revision_id": script["id"] if script else None,
            "shot_plan_revision_id": plan["id"] if plan else None,
            "readiness": readiness,
            "jobs": jobs,
            "executions": executions,
            "qc_failures": [item for item in qcs if item.status.value == "QC_FAILED"],
            "final_assemblies": assemblies,
        }

    @staticmethod
    def _state_name(project, story, script, plan, readiness, jobs, qcs, assemblies) -> str:
        if story is None: return "STORY"
        if script is None: return "SCRIPT"
        if plan is None: return "SHOT_PLAN"
        if not readiness.get("ready"): return "REFERENCES"
        if not jobs: return "PRODUCTION"
        if any(getattr(item, "status", None).value == "QC_FAILED" for item in qcs): return "QC"
        if not any(getattr(item, "status", None).value == "SUCCEEDED" for item in assemblies): return "FINAL_ASSEMBLY"
        return "POST_PRODUCTION"

    def _recommend(self, state: dict[str, object], goal: DirectorGoalKind) -> DirectorRecommendation:
        readiness = state["readiness"]
        if state["story_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_STORY_BIBLE", reason="请先完成并批准 Story Bible", requires_human_approval=True)
        if state["script_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_STRUCTURED_SCRIPT", reason="请先完成并批准 Structured Script", requires_human_approval=True)
        if state["shot_plan_revision_id"] is None:
            return DirectorRecommendation(action="APPROVE_SHOT_PLAN", reason="请先完成并批准 Shot Plan", requires_human_approval=True)
        missing_chars = readiness.get("missing_character_references", [])
        if missing_chars:
            return DirectorRecommendation(action="LOCK_CHARACTER_REFERENCE", target_id=str(missing_chars[0]), reason="生产准备缺少已锁定的角色参考资产", requires_human_approval=True)
        missing_locations = readiness.get("missing_location_references", [])
        if missing_locations:
            return DirectorRecommendation(action="LOCK_LOCATION_REFERENCE", target_id=str(missing_locations[0]), reason="生产准备缺少已锁定的场景参考资产", requires_human_approval=True)
        jobs = state["jobs"]
        if not jobs:
            return DirectorRecommendation(action="START_PRODUCTION", reason="项目已满足生产准备条件，可创建 Production Job", requires_human_approval=True)
        qcs = state["qc_failures"]
        if qcs:
            return DirectorRecommendation(action="REVIEW_QC_FAILURE", target_id=qcs[0].id, reason="检测到 QC_FAILED，需要人工审查后决定是否重试", requires_human_approval=True)
        assemblies = state["final_assemblies"]
        if not any(item.status.value == "SUCCEEDED" for item in assemblies):
            return DirectorRecommendation(action="CREATE_FINAL_ASSEMBLY", reason="生产结果可进入成片清单准备", requires_human_approval=True)
        return DirectorRecommendation(action="START_POST_PRODUCTION", reason="基础成片已完成，可进入后期流程", requires_human_approval=True)

    def run(self, project_id: str, session_id: str) -> DirectorDecision:
        session = self._get_session(session_id, project_id)
        if session.status is not DirectorSessionStatus.ACTIVE:
            raise DirectorServiceError("DirectorSession 已暂停、阻塞或完成")
        goals = self.repository.list_director_goals(session.id)
        goal = goals[-1] if goals else self.repository.create_director_goal(DirectorGoal(id=uuid4().hex, session_id=session.id, project_id=project_id, goal=session.current_goal, created_at=_now()))
        if goal.completed_steps >= goal.max_steps:
            raise DirectorServiceError("bounded Director goal 已达到 max_steps")
        state = self.inspect_project(project_id)
        recommendation = self._recommend(state, goal.goal)
        now = _now()
        decision = DirectorDecision(id=uuid4().hex, session_id=session.id, project_id=project_id, goal_id=goal.id, status=DirectorDecisionStatus.RECOMMENDED, project_state=str(state["project_state"]), recommendation=recommendation, state_snapshot=self._json_safe(state), created_at=now)
        self.repository.create_director_decision(decision)
        updated_goal = goal.model_copy(update={"completed_steps": goal.completed_steps + 1, "status": DirectorGoalStatus.BLOCKED if recommendation.requires_human_approval else DirectorGoalStatus.ACTIVE})
        self.repository.update_director_goal(updated_goal)
        updated_session = session.model_copy(update={"status": DirectorSessionStatus.BLOCKED if recommendation.requires_human_approval else DirectorSessionStatus.ACTIVE, "blocking_reason": recommendation.reason if recommendation.requires_human_approval else "", "pending_recommendation": recommendation, "updated_at": now})
        self.repository.update_director_session(updated_session)
        return decision

    @staticmethod
    def _json_safe(state: dict[str, object]) -> dict[str, object]:
        # Canonical pydantic/dataclass records are represented as JSON-shaped
        # values for durable reconstruction, without persisting provider secrets.
        import json
        return json.loads(json.dumps(state, default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else value.value if hasattr(value, "value") else str(value), ensure_ascii=False))

    def resume(self, project_id: str, session_id: str) -> DirectorDecision:
        session = self._get_session(session_id, project_id)
        if session.status is DirectorSessionStatus.BLOCKED:
            raise DirectorServiceError("等待人工审批后才能 resume DirectorSession")
        return self.run(project_id, session_id)


__all__ = ["DirectorService", "DirectorServiceError"]
