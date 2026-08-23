from __future__ import annotations
import json
def build_shot_prompt(project, script, story):
    return "Generate a structured shot plan from ONLY this APPROVED Structured Script and Story Bible. Return JSON only, no Markdown. Keep scene_id and source_script_beat_ids exact; do not rewrite dialogue. Cover every scene and dialogue/inner-monologue beat. Use stable shot_001 IDs, positive durations totaling near target ±15%. Normalize risk LOW/MEDIUM/HIGH and include risk_reasons for MEDIUM/HIGH. Script="+json.dumps(script.model_dump(mode="json"),ensure_ascii=False)+" StoryBible="+json.dumps(story.model_dump(mode="json"),ensure_ascii=False)+f" Target={project.target_duration_seconds} Aspect={project.aspect_ratio.value}"
def build_shot_repair_prompt(raw,errors): return f"Repair this Shot Plan JSON only; preserve story, IDs and dialogue. Fix schema, references, durations, enum and risk reasons. Errors: {errors[:6000]} Invalid: {raw[:14000]}"
