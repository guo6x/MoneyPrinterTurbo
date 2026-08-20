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
