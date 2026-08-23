# AIDrama Quick Task004 — Shot Director and Structured Shot List

## Scope

Task004 adds the director planning layer between an **APPROVED Structured Script** and later production work. A Shot Plan is canonical structured data (not markdown or a presentation table) and records its source script revision. This task does not generate images or video and does not implement Production, QC, or Desktop workflows.

## Domain and revision model

- `ShotPlan`: title, summary, source script revision ID, and an ordered collection of shots.
- `Shot`: stable ID (`shot_001`, ...), order, scene and optional script-beat traceability, duration, cinematic controls (size, angle, movement, lens, composition), subject/action/expression/eyeline, lighting/blocking, dialogue or narration, visual intent, transition hint, risk level/reasons, and `PLANNED`/`LOCKED` status.
- Validation requires at least one shot, unique IDs and orders, positive duration, valid scene/beat references (beats must belong to the shot's scene), valid enum values, and reasons for MEDIUM/HIGH risks. A beat-less establishing/transition shot must explain its visual intent.
- Revision lifecycle mirrors Story Bible and Structured Script: `DRAFT → APPROVED`; approving a replacement supersedes the previous approved revision. A plan based on a non-current approved script is `OUTDATED` and cannot be approved, while historical revisions remain viewable.

## Persistence and dependencies

Migration 004 creates `shot_plan_revisions` with project/version uniqueness, source script revision, JSON content and generation input, timestamps, and the standard revision status. Canonical project data remains SQLite. Shot planning is permitted only when an approved Structured Script exists; AI generation uses the existing AIDrama/MPT adapter seam and must never bypass it.

## Risk and duration

Shot duration is explicit and derived totals are deterministic. Risk is a review signal only (`LOW`, `MEDIUM`, `HIGH`); Task004 does not execute shots or perform QC. Typical plans contain roughly 5–35 shots depending on target duration, but the editor remains human-controlled.

## Validation closure (VALIDATION_CLOSURE)

The closure run records migration 001–004 ordering, schema and idempotency checks; the complete `test/aidrama_studio/` suite; full-project regression (including the known Windows worker-log baseline); Python compilation and `git diff --check`; AIDrama and original MPT startup smoke; and browser checks at 1920×1080 and 1366×768. Browser acceptance covers the approved-script prerequisite, manual/AI plan creation, shot and scene traceability, edit/reorder/lock behavior, draft persistence, preview/history, approval, outdated detection and approval blocking, and Streamlit session-state stability. Live LLM testing is reported as not run when no provider key is configured.

## Known limitations and risks

- No image/video generation, asset management, production queue, QC, or desktop export is included.
- AI output remains advisory and must pass domain validation plus human approval.
- Existing Windows path-separator worker-log test may remain a baseline failure; it is unrelated to Task004 and must not be “fixed” here.
- Responsive browser screenshots belong under `.tmp/aidrama-task004-browser/` and are intentionally not committed.

## Test evidence

Record exact totals and pass/fail counts in the final task handoff. Any failure must distinguish a pre-existing baseline from a Task004 regression; no dependency installation is required or permitted.
