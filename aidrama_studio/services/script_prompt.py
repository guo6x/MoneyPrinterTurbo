from __future__ import annotations
import json
from aidrama_studio.domain import Project, StoryBible

def build_script_prompt(project: Project, story: StoryBible, *, dialogue_density="standard", narration="少量", pacing="standard") -> str:
    bible = json.dumps(story.model_dump(mode="json"), ensure_ascii=False)
    return f"""Generate a structured scene script from ONLY this APPROVED Story Bible. Return JSON only, no Markdown and no prose outside JSON. Reuse character.id, location.id and story_beats.id exactly; never invent references. Every scene has at least one beat, dialogue and inner monologue require character_id. Estimate positive scene durations totaling near target (±15%). Schema: {{\"title\":\"string\",\"summary\":\"string\",\"scenes\":[{{\"id\":\"scene_001\",\"order\":1,\"title\":\"string\",\"location_id\":\"loc_001\",\"interior_exterior\":\"INT\",\"time_of_day\":\"UNSPECIFIED\",\"character_ids\":[],\"purpose\":\"\",\"summary\":\"\",\"emotion\":\"\",\"estimated_duration_seconds\":1,\"source_story_beat_ids\":[],\"beats\":[{{\"id\":\"beat_001\",\"order\":1,\"type\":\"ACTION\",\"character_id\":null,\"text\":\"\",\"emotion\":null,\"stage_direction\":null,\"estimated_duration_seconds\":1}}]}}]}}. Project target duration: {project.target_duration_seconds}; aspect ratio: {project.aspect_ratio.value}. Controls: dialogue density={dialogue_density}, narration={narration}, pacing={pacing}. Approved Story Bible: {bible}"""

def build_script_repair_prompt(raw: str, errors: str) -> str:
    return f"Repair this structured script JSON only. Preserve story and fix syntax, schema, IDs and references; do not rewrite. Validation errors: {errors[:6000]} Invalid response: {raw[:14000]}"
