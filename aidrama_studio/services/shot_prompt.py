from __future__ import annotations

import json

from aidrama_studio.domain import ShotPlan


def _shot_plan_schema(source_script_revision_id: str) -> str:
    source_id = str(source_script_revision_id or "").strip()
    if not source_id:
        raise ValueError("source_script_revision_id is required")
    schema = ShotPlan.model_json_schema()
    source_schema = dict(schema["properties"]["source_script_revision_id"])
    source_schema["const"] = source_id
    schema["properties"]["source_script_revision_id"] = source_schema
    return json.dumps(schema, ensure_ascii=False, sort_keys=True)


def build_shot_prompt(
    project,
    script,
    story,
    duration_plan=None,
    *,
    source_script_revision_id: str,
):
    source_id = str(source_script_revision_id or "").strip()
    schema = _shot_plan_schema(source_id)
    planning = ""
    if duration_plan is not None:
        planning = (
            " Provider-neutral VIDEO manifest plan: "
            f"provider={duration_plan.provider_id}; model={duration_plan.model_id}; "
            f"planned_shot_count={duration_plan.planned_shot_count}; "
            "planned_shot_durations_seconds="
            f"{json.dumps(duration_plan.planned_shot_durations)}; "
            f"execution_batch_size<={duration_plan.max_batch_size}. "
            "Use this count and duration sequence so every shot remains one "
            "provider-valid bounded create."
        )
    return (
        "Generate a SHOT PLAN from ONLY this APPROVED Structured Script and Story "
        "Bible. Return exactly one JSON object and no Markdown or commentary. "
        "The top-level object may contain ONLY title, summary, "
        "source_script_revision_id and shots; title, source_script_revision_id "
        "and shots are required. DO NOT return a top-level scenes field and DO "
        "NOT return another screenplay or StructuredScript. shots is the only "
        "shot collection. source_script_revision_id MUST equal the authoritative "
        f"product revision {source_id!r}. Every shot must explicitly populate "
        "id, order, scene_id, source_script_beat_ids, duration_seconds, shot_size, "
        "camera_angle, camera_movement, composition, subject, action and "
        "visual_intent. Reuse existing Script scene IDs and beat IDs exactly; "
        "never invent them. Subject IDs may only reuse Story Bible character IDs. "
        "A shot's location is inherited from its referenced Script scene; never "
        "invent Character or Location IDs. Do not rewrite dialogue. Use positive "
        "durations totaling near target ±15%. Follow every field, enum, required "
        "value and additionalProperties=false constraint in this canonical "
        f"ShotPlan JSON Schema: {schema}."
        + planning
        + " Script="
        + json.dumps(script.model_dump(mode="json"), ensure_ascii=False)
        + " StoryBible="
        + json.dumps(story.model_dump(mode="json"), ensure_ascii=False)
        + f" Target={project.target_duration_seconds}"
        + f" Aspect={project.aspect_ratio.value}"
    )


def build_shot_repair_prompt(
    raw,
    errors,
    *,
    source_script_revision_id: str,
):
    source_id = str(source_script_revision_id or "").strip()
    schema = _shot_plan_schema(source_id)
    return (
        "Repair this Shot Plan JSON only; preserve story, IDs and dialogue. Return "
        "exactly one top-level object containing only title, summary, "
        "source_script_revision_id and shots. DO NOT return scenes or another "
        "screenplay structure. shots is the only shot collection. "
        f"source_script_revision_id MUST equal {source_id!r}. Fix schema, "
        "references, durations, enums and risk reasons using this canonical "
        f"ShotPlan JSON Schema: {schema}. Errors: {str(errors)[:6000]} "
        f"Invalid: {str(raw)[:14000]}"
    )
