from __future__ import annotations
from datetime import datetime,timezone
import ast
import json
from typing import Mapping
from uuid import uuid4
from aidrama_studio.domain import *
from aidrama_studio.services.duration_planning import (
    DurationPlanningError,
    DurationPlanningService,
)
from aidrama_studio.services.llm_runtime import LLMInvocationError,LLMInvocationGateway
from aidrama_studio.services.shot_parser import parse_shot_plan
from aidrama_studio.services.shot_prompt import build_shot_prompt,build_shot_repair_prompt
from aidrama_studio.storage import ProjectRepository
def _now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds")
class ShotServiceError(RuntimeError): pass
class ShotService:
    def __init__(
        self,
        repository=None,
        *,
        llm_gateway: LLMInvocationGateway | None = None,
        duration_planner: DurationPlanningService | None = None,
    ):
        self.repository = repository or ProjectRepository()
        self._llm_gateway = llm_gateway or LLMInvocationGateway(self.repository)
        self._duration_planner = duration_planner or DurationPlanningService(
            self.repository
        )
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
        if content.source_script_revision_id != source:
            raise ShotServiceError("Shot Plan source Script provenance 不一致")
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
    def update_shot_fields(self,project_id,revision_id,shot_id,changes):
        rev=self.get_revision(revision_id)
        if not rev or rev["project_id"]!=project_id: raise ShotServiceError("Shot Plan revision 不属于该项目")
        if rev["status"] is not ShotRevisionStatus.DRAFT: raise ShotServiceError("只能编辑 DRAFT Shot Plan")
        plan=rev["content"].model_copy(deep=True)
        index=next((i for i,item in enumerate(plan.shots) if item.id==shot_id),None)
        if index is None: raise ShotServiceError("Shot 不属于该 revision")
        allowed={
            "scene_id","shot_size","camera_angle","camera_movement","movement_notes","lens",
            "focal_length_hint_mm","composition","subject","action","expression","eyeline",
            "lighting","blocking","dialogue_or_narration","visual_intent","transition_hint",
            "duration_seconds","risk_level","risk_reasons","risk_override","risk_override_note","status",
        }
        unknown=set(changes)-allowed
        if unknown: raise ShotServiceError(f"不支持的 Shot 字段: {', '.join(sorted(unknown))}")
        raw=plan.shots[index].model_dump(mode="json")
        for key,value in dict(changes).items():
            if key in {"subject","risk_reasons"} and isinstance(value,str):
                text=value.replace("，",",").strip()
                try: parsed=ast.literal_eval(text) if text.startswith("[") else None
                except (ValueError,SyntaxError): parsed=None
                value=[str(item).strip() for item in (parsed if isinstance(parsed,list) else text.split(",")) if str(item).strip()]
            if key=="lighting" and isinstance(value,str): value={"quality":value,"direction":"","tone":"","notes":""}
            if key=="blocking" and isinstance(value,str): value={"positions":{},"movement":value,"notes":""}
            if key in {"shot_size","camera_angle","camera_movement","lens","eyeline","risk_level","status"} and isinstance(value,str) and "." in value:
                value=value.split(".")[-1]
            raw[key]=value
        try:
            plan.shots[index]=Shot.model_validate(raw)
            script=self.repository.get_script_revision(rev["source_script_revision_id"])
            story=next((item for item in self.repository.list_story_revisions(project_id) if item["status"] is StoryRevisionStatus.APPROVED),None)
            if script is None: raise ShotServiceError("Shot Plan source script revision 不存在")
            plan.validate_against(script["content"],story["content"] if story else None)
            self.recalculate_risk_if_needed(plan)
        except ShotServiceError: raise
        except Exception as exc: raise ShotServiceError(f"Shot 编辑无效: {exc}") from exc
        saved=self.repository.update_shot_revision(revision_id,content=plan,updated_at=_now())
        from .creative_control import CreativeLockService
        locks=CreativeLockService(self.repository)
        if plan.shots[index].status is ShotStatus.LOCKED:
            locks.lock(project_id,"SHOT",shot_id,"*",source_revision_id=revision_id,reason="用户在 Shot Director 手工锁定")
        else:
            locks.release_path(project_id,"SHOT",shot_id,"*")
        return saved
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
            except Exception as exc:
                raise ShotServiceError(f"Shot Plan Draft 无效: {exc}") from exc
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
    def generate_shot_plan(
        self,
        project,
        *,
        source_script_revision_id: str | None = None,
        generation_provenance: Mapping[str, object] | None = None,
    ):
        script = (
            self.repository.get_script_revision(source_script_revision_id)
            if source_script_revision_id
            else self._story_script(project.id)[0]
        )
        if script is not None and script["project_id"] != project.id:
            raise ShotServiceError("Structured Script revision 不属于该项目")
        if not script: raise ShotServiceError("请先完成并确认结构化剧本。")
        if script["status"] is not ScriptRevisionStatus.APPROVED:
            raise ShotServiceError("Shot Planner AI 必须使用 APPROVED Structured Script")
        story=self.repository.get_story_revision(script["source_story_revision_id"])
        if story is None or story["project_id"] != project.id:
            raise ShotServiceError("Shot Planner 缺少 source Story Bible provenance")
        try:
            duration_plan = self._duration_planner.plan(
                project.id, project.target_duration_seconds
            )
        except DurationPlanningError:
            duration_plan = None
        prompt = build_shot_prompt(
            project,
            script["content"],
            story["content"],
            duration_plan,
            source_script_revision_id=script["id"],
        )
        input_source_ids = (script["id"], story["id"])
        def validate(raw):
            plan=parse_shot_plan(
                raw,
                expected_source_script_revision_id=script["id"],
            )
            if plan.source_script_revision_id != script["id"]:
                raise ValueError("Shot Plan source Script provenance 不一致")
            plan.validate_against(script["content"],story["content"]); self.recalculate_risk_if_needed(plan); return plan
        try:
            plan=self._llm_gateway.generate_validated_json(project.id,prompt,operation="SHOT_PLAN_GENERATION",validator=validate,repair_prompt_builder=lambda raw,exc: build_shot_repair_prompt(raw,str(exc),source_script_revision_id=script["id"]),input_source_ids=input_source_ids,provenance=generation_provenance)
        except LLMInvocationError as e: raise ShotServiceError(str(e)) from e
        except Exception as e: raise ShotServiceError("Shot Plan 生成失败，请稍后重试。") from e
        latest=self.get_latest_revision(project.id)
        if latest and latest["source_script_revision_id"] == script["id"]:
            locked={item.id:item.model_copy(deep=True) for item in latest["content"].shots if item.status is ShotStatus.LOCKED}
            generated={item.id:item for item in plan.shots}
            missing=sorted(set(locked)-set(generated))
            if missing: raise ShotServiceError(f"AI proposal 丢失锁定镜头: {', '.join(missing)}")
            plan.shots=[locked.get(item.id,item) for item in plan.shots]
            self.recalculate_risk_if_needed(plan)
        duration_provenance = (
            {
                "duration_provider_id": duration_plan.provider_id,
                "duration_model_id": duration_plan.model_id,
                "planned_shot_count": duration_plan.planned_shot_count,
                "planned_shot_durations": list(
                    duration_plan.planned_shot_durations
                ),
                "expected_video_create_count": (
                    duration_plan.expected_video_create_count
                ),
                "max_execution_batch_size": duration_plan.max_batch_size,
            }
            if duration_plan is not None
            else {}
        )
        return self._create(
            project.id,
            script["id"],
            plan,
            {
                "target_duration_seconds": project.target_duration_seconds,
                "aspect_ratio": project.aspect_ratio.value,
            }
            | duration_provenance
            | dict(generation_provenance or {}),
        )

    def regenerate_shot(self,project,revision_id,shot_id):
        """Regenerate exactly one unlocked shot into a new DRAFT revision."""
        rev=self.get_revision(revision_id)
        if not rev or rev["project_id"]!=project.id: raise ShotServiceError("Shot Plan revision 不属于该项目")
        source_plan=rev["content"]
        index=next((i for i,item in enumerate(source_plan.shots) if item.id==shot_id),None)
        if index is None: raise ShotServiceError("Shot 不属于该 revision")
        original=source_plan.shots[index]
        if original.status is ShotStatus.LOCKED: raise ShotServiceError("锁定镜头不能被 AI 再生成；请先显式解锁")
        script=self.repository.get_script_revision(rev["source_script_revision_id"])
        story=next((item for item in self.repository.list_story_revisions(project.id) if item["status"] is StoryRevisionStatus.APPROVED),None)
        if script is None or story is None: raise ShotServiceError("选择性再生成缺少 APPROVED Story/Script provenance")
        prompt=(
            "Regenerate ONLY the selected shot as one Shot JSON object. Preserve id, order, scene_id "
            "and source_script_beat_ids exactly. Do not return Markdown. Selected shot="
            +json.dumps(original.model_dump(mode="json"),ensure_ascii=False,sort_keys=True)
            +" Script="+json.dumps(script["content"].model_dump(mode="json"),ensure_ascii=False,sort_keys=True)
        )
        def validate(raw):
            try: payload=json.loads(raw) if isinstance(raw,str) else raw
            except json.JSONDecodeError as exc: raise ValueError("selected shot JSON invalid") from exc
            candidate=Shot.model_validate(payload)
            if (candidate.id,candidate.order,candidate.scene_id,tuple(candidate.source_script_beat_ids)) != (original.id,original.order,original.scene_id,tuple(original.source_script_beat_ids)):
                raise ValueError("selected shot identity/provenance changed")
            proposal=source_plan.model_copy(deep=True)
            proposal.shots[index]=candidate
            proposal.validate_against(script["content"],story["content"])
            self.recalculate_risk_if_needed(proposal)
            # Deep equality outside the selected index is the selective-regeneration gate.
            if any(proposal.shots[i] != source_plan.shots[i] for i in range(len(proposal.shots)) if i!=index):
                raise ValueError("non-target shot changed")
            return proposal
        try:
            proposal=self._llm_gateway.generate_validated_json(
                project.id,prompt,operation="SHOT_SELECTIVE_REGENERATION",validator=validate,
                repair_prompt_builder=lambda raw,exc: (
                    "Repair ONLY the selected Shot JSON. Preserve exact identity/provenance. Error="
                    +str(exc)+" Raw="+str(raw)
                ),
                input_source_ids=(rev["id"],script["id"],story["id"],shot_id),
            )
        except LLMInvocationError as exc: raise ShotServiceError(str(exc)) from exc
        except Exception as exc: raise ShotServiceError(f"镜头选择性再生成失败: {exc}") from exc
        return self._create(
            project.id,rev["source_script_revision_id"],proposal,
            {"operation":"SHOT_SELECTIVE_REGENERATION","parent_revision_id":rev["id"],"target_shot_id":shot_id},
        )
