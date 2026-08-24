# AIDrama Intelligent Director Product Closure — Remediation V2

This document records the remediation of the existing product-closure
surface. It does not add a new model provider, a live Vision provider, paid
generation, or a new product domain.

## Defects fixed

- Director recommendations now have a durable `RECOMMENDED → APPROVED /
  REJECTED → COMPLETED` lifecycle. The original recommendation row remains
  immutable; transitions are append-only `director_decision_events`.
- Approval and rejection are review records only. They do not approve Story,
  Script, Shot Plan, references, call a provider, or spend generation credits.
- A blocked Director session becomes resumable after a decision is handled.
  Cold restart reconstructs the effective decision status from the event
  history. Bounded goal segments remain limited by `max_steps`; an explicit
  resume opens a new bounded segment when the previous segment is exhausted.
- Director goal kinds have separate completion predicates rather than sharing
  one generic post-production pipeline.
- Director and Producer now consume a single current Production/QC projection.
  Historical failed executions remain readable but do not block a newer
  qualified attempt.
- Final Assembly human review uses the latest review decision for a QC result.
  A later approval supersedes an earlier rejection for current qualification,
  without deleting either review.
- Producer QC retry recommendations are persisted in append-only
  `producer_recommendation_events`; the configured recommendation budget is
  therefore real and cannot be bypassed by repeatedly reloading the page.
- Capability readiness is projected from the capability registry. Multiple
  providers may coexist under a capability while a deterministic configured
  provider is selected for the existing UI/API shape.
- Legacy reference repair migration 015 projects every recoverable legacy
  image row, including multi-image sets, malformed hashes and unsafe paths.
- Desktop browser fallback now keeps the loopback Streamlit child alive until
  the browser session/child exits and cleans up on interruption. The frozen
  package entry point is `desktop/launcher.py`; it does not freeze the page
  script as the application entry point.
- Post Production pins the exact successful Final Assembly render attempt.
  Render retries create new immutable Post attempts and never silently switch
  the source media.
- FFmpeg Post output validation now probes video stream, duration, dimensions,
  and expected audio. Subtitle SRT generation, BGM mixing, loudness
  normalization, output SHA-256 and project-relative output are covered by a
  real local media smoke.

## Director state machine

The immutable recommendation is persisted as `RECOMMENDED`. Human handling
appends one transition event:

```
RECOMMENDED ──approve──> APPROVED ──complete──> COMPLETED
       └─────reject────> REJECTED
```

Only the service can create transitions. Project, session and goal ownership
is checked for every operation. Approval does not execute the recommendation;
the canonical Story/Script/Shot/Reference/Production services remain the only
place where creative truth changes.

## Goal semantics

- `COMPLETE_STORY`: current approved Story Bible exists.
- `COMPLETE_SCRIPT`: current approved Script exists and is chained to the
  approved Story revision.
- `COMPLETE_SHOT_PLAN`: current approved Shot Plan exists and is chained to
  the approved Script revision.
- `COMPLETE_REFERENCES`: canonical reference readiness is satisfied.
- `MAKE_PRODUCTION_READY`: canonical Story → Script → Shot Plan → Reference
  readiness is satisfied.
- `COMPLETE_PRODUCTION`: the deterministic current Production Job has all
  shots completed and qualified.
- `RESOLVE_QC_BLOCKER`: no current shot remains without a qualified QC path.
- `MAKE_FINAL_ASSEMBLY_READY`: canonical Final Assembly readiness is ready.
- `COMPLETE_POST_PRODUCTION`: a current successful, project-scoped Post
  output exists.

## Current Production/QC selection rule

When no job is explicitly selected, `CurrentProductionStateService` chooses
the newest non-cancelled Production Job by `(created_at, id)`. Historical jobs
are not merged into the current state. For each shot, Final Assembly selects
the newest qualified persisted source; a current QC failure or latest explicit
rejected review blocks that shot. A later qualified execution can supersede an
older failed/rejected attempt while preserving the old records.

## Producer budget semantics

Generation attempts are counted from immutable `ProductionAttempt` history and
are bounded by `max_generation_attempts_per_shot`. QC retry recommendations
are counted from `producer_recommendation_events` for the current job/shot and
are bounded by `max_qc_retry_recommendations`. `automatic_retry_enabled` stays
`False`; the Producer never submits a runtime request.

## Capability readiness truth

`CapabilityRegistry` is the inventory for LLM, image, generative video, stock
video and Vision boundaries. `ProviderReadinessService` is a presentation
projection over that registry plus the non-AI TTS check. The default inventory
keeps `WAN_VIDEO` and `MPT_STOCK` separate. Image and live Vision remain
unavailable boundaries; deterministic mock Vision is test-only.

## Migration repair strategy

The forward migrations are:

| Version | Purpose |
| --- | --- |
| 015 | Complete legacy reference repair after already-applied 014; preserve every legacy image row and deterministic lock/binding history. |
| 016 | Append-only Director decision lifecycle events. |
| 017 | Exact Post source FinalAssembly render-attempt pinning. |
| 018 | Durable Producer retry-recommendation history. |

Migration 014 is not rewritten as the only upgrade path. Fresh databases and
databases already recorded through 014 both advance idempotently to the same
latest schema. Legacy tables remain intact for historical safety.

## Desktop lifecycle and build architecture

The launcher binds only to loopback, starts Streamlit, waits for health, then
opens PyWebView or a browser fallback. Browser fallback remains in its owner
loop until the child exits or the process is interrupted; `finally` performs
termination/kill cleanup. In a PyInstaller build, `AIDramaStudio.exe` launches
the desktop launcher, which starts the local Streamlit child in explicit
`--streamlit-child` mode. Runtime data includes the AIDrama page, CSS, brand
assets, Streamlit package data, AIDrama modules and existing MPT media seams.
If PyInstaller is unavailable, build creation remains deferred and no package
is installed by this remediation.

## Post source provenance and media smoke

`PostProductionPlan.source_final_assembly_render_attempt_id` freezes the exact
successful Final Assembly attempt used as input. A later Final Assembly retry
cannot mutate that plan. Subtitle timing remains a script-derived draft and
preserves script/scene/beat/shot provenance where available; it is not claimed
to be perfect editorial synchronization.

The real local smoke creates a deterministic MP4, renders the real Final
Assembly, generates an SRT subtitle track and deterministic BGM with FFmpeg,
renders the Post output, probes the output and verifies video/audio streams,
SHA-256 and project-relative storage.

## Deferred live gates

No live external model call is required for this remediation. LLM, image,
video and Vision live gates remain truthful according to local credentials and
configuration. In particular, no Seedance, GPT Image, Gemini/GPT Vision,
automatic provider fallback, or paid regeneration is introduced.

## Remaining non-blocking limitations

- PyInstaller artifact creation is deferred when the optional PyInstaller tool
  is not installed.
- Live external provider and live Vision acceptance remain separate deferred
  gates and are not represented as passing merely because an adapter boundary
  exists.
- Subtitle timing is deterministic draft timing from script beat estimates.
- The desktop shell is a lifecycle/package foundation, not a new visual theme
  or a full desktop production editor.
