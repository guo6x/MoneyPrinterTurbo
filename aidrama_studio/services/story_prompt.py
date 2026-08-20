from __future__ import annotations

from aidrama_studio.domain import Project


def _duration_guidance(seconds: int) -> str:
    if seconds <= 45:
        return "核心人物控制在 1-3 人，核心场景控制在 1-3 个。"
    if seconds <= 90:
        return "核心人物控制在 1-4 人，核心场景控制在 1-4 个。"
    return "核心人物控制在 1-5 人，核心场景控制在 1-5 个。"


def build_story_bible_prompt(
    project: Project,
    *,
    brief: str,
    genre: str,
    tone: str,
    target_audience: str = "",
    creative_constraints: str = "",
) -> str:
    return f"""You are the story development engine for AIDrama Studio.

Return JSON only. Do not use Markdown fences. Do not add commentary.
Return exactly this structure:
{{
  "title": "string",
  "logline": "string",
  "premise": "string",
  "genre": "string",
  "tone": "string",
  "themes": ["string"],
  "world": {{"era": "string", "setting": "string", "rules": ["string"], "timeline_notes": "string"}},
  "characters": [{{"id": "char_001", "name": "string", "role": "string", "age_or_range": "string", "identity": "string", "personality": "string", "appearance": "string", "motivation": "string", "relationship_notes": "string", "speech_style": "string"}}],
  "locations": [{{"id": "loc_001", "name": "string", "function": "string", "environment": "string", "time_of_day": "string", "visual_style": "string", "key_props": ["string"]}}],
  "story_beats": [{{"id": "beat_001", "order": 1, "type": "OPENING", "summary": "string", "characters": ["char_001"], "location_id": "loc_001", "emotional_goal": "string"}}]
}}

Keep characters, locations, world rules and beats internally consistent. Use stable IDs
like char_001, loc_001 and beat_001. Beat types must be OPENING, DEVELOPMENT,
TURNING_POINT, CLIMAX or ENDING. Include at least 3 ordered beats. This is a
short-film bible, not a full television series. {_duration_guidance(project.target_duration_seconds)}
The target duration is {project.target_duration_seconds} seconds and the aspect ratio is {project.aspect_ratio.value}.

PROJECT TITLE: {project.title}
PROJECT DESCRIPTION: {project.description}
CREATIVE BRIEF: {brief}
GENRE: {genre}
TONE: {tone}
TARGET AUDIENCE: {target_audience or "not specified"}
CREATIVE CONSTRAINTS: {creative_constraints or "none"}
""".strip()


def build_repair_prompt(invalid_content: str, validation_errors: str) -> str:
    return f"""Repair the following Story Bible response into valid JSON.

Return JSON only, with no Markdown and no commentary. Do not rethink or rewrite the
story; only repair structure, IDs, references, required fields and JSON syntax so it
matches the required schema.

Validation errors:
{validation_errors[:6000]}

Required top-level fields: title, logline, premise, genre, tone, themes, world,
characters, locations, story_beats.
World fields: era, setting, rules, timeline_notes.
Character fields: id, name, role, age_or_range, identity, personality, appearance,
motivation, relationship_notes, speech_style.
Location fields: id, name, function, environment, time_of_day, visual_style, key_props.
Story beat fields: id, order, type, summary, characters, location_id, emotional_goal.

Invalid response:
{invalid_content[:14000]}
""".strip()
