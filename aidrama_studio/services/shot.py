from __future__ import annotations
from datetime import datetime,timezone
import ast
from uuid import uuid4
from aidrama_studio.domain import *
from aidrama_studio.services.llm_runtime import LLMInvocationError,LLMInvocationGateway
from aidrama_studio.services.shot_parser import parse_shot_plan
from aidrama_studio.services.shot_prompt import build_shot_prompt,build_shot_repair_prompt
from aidrama_studio.storage import ProjectRepository
def _now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds")
class ShotServiceError(RuntimeError): pass
class ShotService:
    def __init__(self,repository=None,*,llm_gateway: LLMInvocationGateway | None = None): self.repository=repository or ProjectRepository(); self._llm_gateway=llm_gateway or LLMInvocationGateway(self.repository)
    def llm_readiness(self,project_id): return self._llm_gateway.readiness(project_id)
    def list_revisions(self,pid): return self.repository.list_shot_revisions(pid)
    def list_plans(self,pid):
        out=[]
        for rev in self.list_revisions(pid):
            item={"id":rev["id"],"project_id":rev["project_id"],"version":rev["version"],"status":rev["status"],"source_script_revision_id":rev["source_script_revision_id"]}
            item.update(rev["content"].model_dump(mode="python")); out.append(item)
        return out
    def get_revision(self,rid): return self.repository.get_shot_revision(rid)
    def get_latest_revision(self,pid):
        x=self.list_revisions(pid); return x[0] if x else None
    def get_approved_revision(self,pid): return next((x for x in self.list_revisions(pid) if x["status"] is ShotRevisionStatus.APPROVED),None)
    def _next(self,pid): x=self.get_latest_revision(pid); return x["version"]+1 if x else 1
    def _create(self,pid,source,content,generation_input=None):
        n=_now(); return self.repository.create_shot_revision(revision_id=uuid4().hex,project_id=pid,version=self._next(pid),status=ShotRevisionStatus.DRAFT,source_script_revision_id=source,content=content,generation_input=generation_input,created_at=n,updated_at=n)
    def _story_script(self,pid):
        stories=self.repository.list_story_revisions(pid); scripts=self.repository.list_script_revisions(pid); return next((x for x in scripts if x["status"] is ScriptRevisionStatus.APPROVED),None),next((x for x in stories if x["status"] is StoryRevisionStatus.APPROVED),None)
    def recalculate_risk_if_needed(self,plan):
        for s in plan.shots:
            if s.risk_override: continue
            reasons=[]
            if s.shot_size in (ShotSize.CLOSE_UP,ShotSize.EXTREME_CLOSE_UP): reasons.append("EMOTIONAL_CLOSEUP")
            if len(s.subject)>1: reasons.append("MULTI_CHARACTER")
            if s.camera_movement is not CameraMovement.STATIC: reasons.append("CAMERA_MOTION")
            if s.source_script_beat_ids: reasons.append("KEY_STORY_BEAT")
            s.risk_level=RiskLevel.HIGH if len(reasons)>=2 else RiskLevel.MEDIUM if reasons else RiskLevel.LOW; s.risk_reasons=reasons
        return plan
    def recommend_duration_rebalance(self,rev_or_id,target_duration_seconds):
        """Return an advisory duration proposal without mutating the revision.

        Manually locked shots are immutable inputs to the recommendation.
        Applying a proposal remains a separate explicit Save Draft action.
        """
        rev=self.get_revision(rev_or_id) if isinstance(rev_or_id,str) else rev_or_id
        if not rev: raise ShotServiceError("Shot Plan revision 不存在")
        target=float(target_duration_seconds)
        if target<=0: raise ShotServiceError("目标时长必须大于 0")
        shots=rev["content"].shots
        locked_total=sum(float(s.duration_seconds) for s in shots if s.status is ShotStatus.LOCKED)
        unlocked=[s for s in shots if s.status is not ShotStatus.LOCKED]
        unlocked_total=sum(float(s.duration_seconds) for s in unlocked)
        available=target-locked_total
        if available<=0 or not unlocked or unlocked_total<=0:
            return {"target":target,"current":sum(float(s.duration_seconds) for s in shots),"locked_total":locked_total,"feasible":False,"suggestions":[]}
        scale=available/unlocked_total
        suggestions=[]
        for shot in unlocked:
            proposed=max(0.1,round(float(shot.duration_seconds)*scale,1))
            if abs(proposed-float(shot.duration_seconds))>=0.05:
                suggestions.append({"shot_id":shot.id,"from_seconds":float(shot.duration_seconds),"to_seconds":proposed})
        return {"target":target,"current":sum(float(s.duration_seconds) for s in shots),"locked_total":locked_total,"feasible":True,"suggestions":suggestions}
    def validate_plan(self,rev_or_plan,project=None,*,block_extreme=False):
        rev=rev_or_plan if isinstance(rev_or_plan,dict) else None; plan=rev["content"] if rev else rev_or_plan; script,story=self._story_script(rev["project_id"] if rev else project.id); plan.validate_against(script["content"],story["content"]); self.recalculate_risk_if_needed(plan)
        if block_extreme and project and abs(plan.total_duration_seconds-project.target_duration_seconds)>project.target_duration_seconds*.30: raise ValueError("Shot duration exceeds ±30% blocking threshold")
        return plan
    def create_manual_shot_plan(self,project,script_revision=None):
        if script_revision is None: script_revision,_=self._story_script(project.id)
        if not script_revision or script_revision["status"] is not ScriptRevisionStatus.APPROVED: raise ShotServiceError("请先完成并确认结构化剧本。")
        shots=[]
        for scene in script_revision["content"].scenes:
            beat=scene.beats[0]; shots.append(Shot(id=f"shot_{len(shots)+1:03d}",order=len(shots)+1,scene_id=scene.id,source_script_beat_ids=[beat.id] if beat.type.value in ("DIALOGUE","INNER_MONOLOGUE") else [],duration_seconds=scene.estimated_duration_seconds,subject=scene.character_ids,action=beat.text,dialogue_or_narration=beat.text if beat.type.value in ("DIALOGUE","NARRATION","INNER_MONOLOGUE") else "",visual_intent=f"建立 {scene.title} 的视觉空间"))
        plan=ShotPlan(title=script_revision["content"].title,summary="",source_script_revision_id=script_revision["id"],shots=shots); self.recalculate_risk_if_needed(plan); return self._create(project.id,script_revision["id"],plan)
    create_manual_plan = create_manual_shot_plan
    def create_plan(self, project_id, script_revision_id):
        project=self.repository.get_project(project_id); script=self.repository.get_script_revision(script_revision_id); return self.create_manual_shot_plan(project,script)
    def add_shot(self, revision_id):
        rev=self.get_revision(revision_id)
        if not rev or rev["status"] is not ShotRevisionStatus.DRAFT: raise ValueError("只能向 DRAFT Shot Plan 添加镜头")
        plan=rev["content"]; scene_id=plan.shots[-1].scene_id if plan.shots else "scene_001"; n=len(plan.shots)+1
        plan.shots.append(Shot(id=f"shot_{n:03d}",order=n,scene_id=scene_id,duration_seconds=1,visual_intent="补充空间与动作信息")); self.recalculate_risk_if_needed(plan)
        return self.repository.update_shot_revision(revision_id,content=plan,updated_at=_now())
    def move_shot(self, revision_id, index, delta):
        rev=self.get_revision(revision_id)
        if not rev or rev["status"] is not ShotRevisionStatus.DRAFT: raise ValueError("只能调整 DRAFT Shot Plan")
        plan=rev["content"]; target=index+delta
        if not 0<=target<len(plan.shots): return rev
        plan.shots[index],plan.shots[target]=plan.shots[target],plan.shots[index]
        plan.shots[index].order,plan.shots[target].order=index+1,target+1
        return self.repository.update_shot_revision(revision_id,content=plan,updated_at=_now())
    def save_draft(self,pid,content,*,revision_id=None,generation_input=None):
        rev=self.get_revision(revision_id) if revision_id else None; source=rev["source_script_revision_id"] if rev else None
        if isinstance(content,dict):
            content = content.get("content") if isinstance(content.get("content"),dict) else content
            content=dict(content); normalized=[]
            for key in ("id","project_id","version","status"):
                content.pop(key, None)
            for raw_shot in content.get("shots",[]):
                raw_shot=dict(raw_shot)
                for key in ("shot_size","camera_angle","camera_movement","lens","eyeline","status","risk_level"):
                    if hasattr(raw_shot.get(key), "value"): raw_shot[key]=raw_shot[key].value
                for key in ("shot_size","camera_angle","camera_movement","lens","eyeline","status"):
                    if isinstance(raw_shot.get(key),str) and "." in raw_shot[key]: raw_shot[key]=raw_shot[key].split(".")[-1]
                if isinstance(raw_shot.get("eyeline"),str) and raw_shot["eyeline"] in Eyeline.__members__: raw_shot["eyeline"] = Eyeline[raw_shot["eyeline"]].value
                if isinstance(raw_shot.get("risk_level"),str): raw_shot["risk_level"]=raw_shot["risk_level"].split(".")[-1]
                for list_key in ("subject", "risk_reasons"):
                    if isinstance(raw_shot.get(list_key), str):
                        text = raw_shot[list_key].replace("，", ",").strip()
                        try:
                            parsed = ast.literal_eval(text) if text.startswith("[") else None
                        except (ValueError, SyntaxError):
                            parsed = None
                        raw_shot[list_key] = [str(x).strip() for x in (parsed if isinstance(parsed, list) else text.split(",")) if str(x).strip()]
                if isinstance(raw_shot.get("lighting"),str): raw_shot["lighting"]={"quality":raw_shot["lighting"],"direction":"","tone":"","notes":""}
                if isinstance(raw_shot.get("blocking"),str): raw_shot["blocking"]={"positions":{},"movement":raw_shot["blocking"],"notes":""}
                normalized.append(raw_shot)
            content["shots"]=normalized
            try:
                content=ShotPlan.model_validate(content)
            except Exception:
                if rev is None: raise
                content=rev["content"]
        if source:
            script=self.repository.get_script_revision(source); story=next((x for x in self.repository.list_story_revisions(pid) if x["status"] is StoryRevisionStatus.APPROVED),None); content.validate_against(script["content"],story["content"] if story else None); self.recalculate_risk_if_needed(content)
        if rev and rev["status"] is ShotRevisionStatus.DRAFT: return self.repository.update_shot_revision(revision_id,content=content,updated_at=_now(),generation_input=generation_input)
        if rev and rev["status"] is ShotRevisionStatus.APPROVED: return self._create(pid,source,content,generation_input)
        latest=self.get_latest_revision(pid)
        if latest and latest["status"] is ShotRevisionStatus.DRAFT: return self.repository.update_shot_revision(latest["id"],content=content,updated_at=_now(),generation_input=generation_input)
        raise ValueError("Shot Plan source script revision required")
    def is_outdated(self,rev_or_id):
        rev=self.get_revision(rev_or_id) if isinstance(rev_or_id,str) else rev_or_id
        if not rev:return False
        script,_=self._story_script(rev["project_id"]); return script is not None and script["id"]!=rev["source_script_revision_id"]
    def approve_revision(self,rid):
        rev=self.get_revision(rid)
        if not rev: raise KeyError("Shot Plan revision 不存在")
        if self.is_outdated(rev): raise ValueError("该分镜基于旧版剧本，需重新同步后才能批准")
        self.validate_plan(rev,block_extreme=False); return self.repository.approve_shot_revision(rid,updated_at=_now())
    approve_plan = approve_revision
    def generate_shot_plan(self,project):
        script,story=self._story_script(project.id)
        if not script: raise ShotServiceError("请先完成并确认结构化剧本。")
        prompt=build_shot_prompt(project,script["content"],story["content"]); input_source_ids=(script["id"],story["id"])
        def validate(raw):
            plan=parse_shot_plan(raw); plan.validate_against(script["content"],story["content"]); self.recalculate_risk_if_needed(plan); return plan
        try:
            plan=self._llm_gateway.generate_validated_json(project.id,prompt,operation="SHOT_PLAN_GENERATION",validator=validate,repair_prompt_builder=lambda raw,exc: build_shot_repair_prompt(raw,str(exc)),input_source_ids=input_source_ids)
        except LLMInvocationError as e: raise ShotServiceError(str(e)) from e
        except Exception as e: raise ShotServiceError("Shot Plan 生成失败，请稍后重试。") from e
        return self._create(project.id,script["id"],plan,{"target_duration_seconds":project.target_duration_seconds,"aspect_ratio":project.aspect_ratio.value})
