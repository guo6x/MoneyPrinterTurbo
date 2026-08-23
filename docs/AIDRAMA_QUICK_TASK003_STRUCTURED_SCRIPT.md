# AIDrama Studio · Task003 Structured Script

Task003 extends the approved Story Bible flow with a persistent, editable
Structured Scene Script. The Story page exposes two tabs: **Story Bible** and
**Structured Script**. Script data is only available when an APPROVED Story
Bible exists; a draft or superseded Bible is never used as canonical input.

## User flow

1. Confirm a Story Bible in the Story Bible tab.
2. Open Structured Script and create a manual script (or select an existing
   revision).
3. Edit scene metadata and individual beats. Scenes and beats can be added;
   IDs and order values are retained and validated on save.
4. Save a DRAFT, then confirm it. A confirmed revision can be forked into a
   new DRAFT for further edits.

The history selector shows every script revision and its status (DRAFT,
APPROVED, or SUPERSEDED). When the source Story Bible changed, the editor
shows an **outdated** warning and approval is rejected until a script is
created from the current approved Bible.

## Editing and validation

Each scene includes location, INT/EXT, time of day, characters, purpose,
summary, emotion, duration, and ordered beats. Beats contain type, optional
character, text, emotion, stage direction, and optional duration. Pydantic
validation enforces unique scene/beat IDs and orders, valid Story Bible
references, and character references for DIALOGUE and
INNER_MONOLOGUE beats.

Task003 deliberately does not create shot lists, images, video, production,
or QC workflows, and does not modify the MoneyPrinterTurbo LLM core.

## VALIDATION_CLOSURE

- Migration tests now verify ordered versions 1/2/3, recorded timestamps, all
  three tables, and idempotent repeated initialization. AIDrama suite: 19
  passed, 0 failed.
- Full project regression excluding the documented Windows path-separator
  baseline passed 617 tests with 10 skipped. The single baseline test
  `test_worker_logs_are_available_without_streamlit_session_state` remains the
  only failure: Loguru emits `test\\services\\...` on Windows while its test
  pattern expects `test/services/...`. No MPT code or test was changed.
- Python compile and `git diff --check` pass. Both `streamlit run
  webui/Main.py` and `streamlit run aidrama_studio/Main.py` returned HTTP 200
  startup smoke responses.
- Browser acceptance used a local test project. Dashboard, project opening,
  Story Bible approval, Structured Script tab, manual script creation, Scene
  Navigator, scene add/edit, beat add, beat reorder controls, draft save,
  preview, history, approval, PREPRODUCTION advancement, Story Bible v2
  approval, and the old-script OUTDATED warning were observed. Approval of an
  invalid dialogue without a character was blocked. Screenshots were saved to
  `.tmp/aidrama-task003-browser/structured-script-1920.png` and
  `.tmp/aidrama-task003-browser/structured-script-1366.png` (not committed).
- Live LLM was not run because no API key is configured. Responsive checks at
  1920x1080 and 1366x768 showed the sidebar, tabs, navigator, editor, history,
  and approval controls without severe overlap or horizontal overflow.
