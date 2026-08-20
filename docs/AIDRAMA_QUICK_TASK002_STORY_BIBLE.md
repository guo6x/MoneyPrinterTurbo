# AIDrama Studio Task002 — Story Bible

## Architecture

Task002 turns the existing `aidrama_studio/pages/story.py` placeholder into the first usable creative workflow:

```text
Project Brief
   ↓
MPT LLM provider seam (frozen config snapshot)
   ↓
JSON-only Story Bible prompt
   ↓
safe JSON extraction + Pydantic validation
   ↓ (at most one repair)
Story Bible DRAFT revision
   ↓
human edit → validation → APPROVED
```

The task does not implement Structured Script, Shot List, production, QC or desktop packaging. `webui/Main.py`, the MPT backend and the original provider configuration remain unchanged.

## StoryBible schema

The domain models live in `aidrama_studio/domain/story.py`:

- `StoryBible`: `title`, `logline`, `premise`, `genre`, `tone`, `themes`, `world`, `characters`, `locations`, `story_beats`.
- `Character`: stable `id` plus `name`, `role`, `age_or_range`, `identity`, `personality`, `appearance`, `motivation`, `relationship_notes`, `speech_style`.
- `Location`: stable `id` plus `name`, `function`, `environment`, `time_of_day`, `visual_style`, `key_props`.
- `World`: `era`, `setting`, explicit `rules`, `timeline_notes`.
- `StoryBeat`: stable `id`, deterministic `order`, constrained `type`, `summary`, character references, optional `location_id`, and `emotional_goal`.

Pydantic validation requires at least one character, one location and three beats; all IDs and beat orders are unique; character and location references must resolve. Project `target_duration_seconds` and `aspect_ratio` remain canonical in the Project table and are generation context only.

## Revision model

Migration 002 creates `story_bible_revisions` with:

- `id`, `project_id`, `version`, `status`, `content_json`, `generation_input_json`, `created_at`, `updated_at`;
- `UNIQUE(project_id, version)`;
- a partial unique index enforcing at most one `APPROVED` revision per project;
- foreign key to `projects(id)`.

Statuses are `DRAFT`, `APPROVED`, and `SUPERSEDED`.

- A successful generation creates the next DRAFT version and never overwrites an old revision.
- Approving a revision supersedes the previous approved revision in one SQLite transaction.
- Approval advances a DRAFT Project to `STORY`; later Project statuses are never downgraded.
- Editing an APPROVED revision creates a new DRAFT instead of changing approved content in place.
- A blank, valid editable Draft is available without an LLM.

## LLM reuse seam

`aidrama_studio/services/ai.py` calls the existing `app.services.llm._generate_response(prompt, app_config=...)` implementation. This is deliberately documented as:

```text
PRIVATE_UPSTREAM_COUPLING=KNOWN_ACCEPTED_FOR_QUICK_DEMO
```

The adapter owns no persistence and no Story domain knowledge. It freezes the MPT `config.app` snapshot once per generation and passes that same object to normal generation and the one repair attempt. API keys, tokens and credential-bearing configuration are never included in `generation_input_json`, Story content, UI errors or logs.

## Prompt, parser and repair

- `aidrama_studio/services/story_prompt.py` owns the JSON-only prompt and duration-aware guidance (`<=45s`, `60–90s`, `120s`).
- `aidrama_studio/services/story_parser.py` uses `json.loads`, safe first-object extraction and Pydantic validation. It does not use `eval` or `ast.literal_eval`.
- A parse/schema failure allows exactly one repair call with the invalid content, validation errors and required schema. A network/provider failure does not trigger an unbounded retry.
- The UI shows a Chinese, actionable error while detailed exceptions stay in logs.

## Manual fallback

When the MPT provider is not configured, Story displays a clear “AI 服务尚未配置” state and a Settings route. The user can still select “创建空白 Story Bible” and edit the valid template manually. No second API-key configuration is introduced.

## UI

The Story page uses the Task001 dark production-studio tokens and a desktop two-column layout:

- left: Brief, AI generation, manual fallback and revision history;
- right: Story Bible revision status and four tabs — overview, characters, locations and story structure.

Characters and locations are card-like editable blocks. Removal is blocked when a beat still references the entity. Story beats expose stable order, type, summary, references and emotional goal. JSON is not shown as the product UI.

## Run and test commands

```powershell
.venv\Scripts\python.exe -m streamlit run aidrama_studio\Main.py --server.address=127.0.0.1 --server.port=8513
.venv\Scripts\python.exe -m pytest -q test\aidrama_studio
.venv\Scripts\python.exe -m pytest -q test
```

The current full-suite baseline remains the known Windows path separator failure in `test_worker_logs_are_available_without_streamlit_session_state`; Task002 adds no new existing-test regression.

## Known limitations

- Live LLM smoke was not run because the current MPT provider has no configured API key. The real page displays this state; generation/parser/repair are covered with mocked adapters.
- Story Beat add/remove controls and drag sorting are intentionally deferred; editing and stable order validation are available.
- Revision history is a lightweight viewer/fork action, not a full diff viewer.
- Screenshots used for the 1920×1080 and 1366×768 browser checks are temporary files under `.tmp/aidrama-task002-browser/` and are excluded from Git.
- Structured Script, Shot List, asset generation, production, QC and desktop packaging remain outside Task002.
