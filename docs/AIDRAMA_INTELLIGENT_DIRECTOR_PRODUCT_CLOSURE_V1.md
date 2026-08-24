# AIDrama Intelligent Director Product Closure

This document records the release-candidate closure for the product layer
around the canonical AIDrama production backbone.  The implementation keeps
Story, Structured Script, Shot Plan, Reference Assets, Production, QC, Human
Review and Final Assembly as the sources of truth.

## Architecture

`AIDrama Studio` now presents one guided workflow: creative brief → approved
Story Bible → approved Structured Script → approved Shot Plan → locked
references → bounded production → deterministic QC and Human Review →
immutable Final Assembly → Post Production.  Pages call services; they do not
execute SQL or provider HTTP directly.

The Director and Producer are advisory control-plane layers.  They inspect
canonical state, emit structured recommendations and preserve approval gates.
They do not rewrite creative revisions or silently execute paid work.

## AI capability layer

`services/ai_capabilities.py` defines separate `LLMProvider`,
`ImageGenerationProvider`, `VideoGenerationProvider` and
`VisionAnalysisProvider` contracts.  The registry exposes Wan as
`VIDEO_GENERATIVE` and the existing MPT stock runtime as `VIDEO_STOCK`.
Image generation is an unavailable boundary; a future candidate is required
to enter `ReferenceAssetVersion` as `DRAFT` before human locking.  Vision is
similarly unavailable live, with a deterministic mock provider only for tests.
No credentials are included in snapshots, metadata or the readiness UI.

## Director / Producer

`DirectorService.run()` performs exactly one bounded inspection step.  Durable
`director_sessions`, `director_goals` and append-only `director_decisions`
support cold resume and reconstruct the pending recommendation, blocker and
state snapshot after restart.  Goals have explicit max steps and creative
actions remain approval-gated.

`ProducerService` derives shot progress, high-risk shots, QC blockers and final
assembly readiness.  `ProducerPolicy` defaults to three generation attempts
per shot and two QC retry recommendations; automatic retry is disabled.  A
failure produces a bounded recommendation, never an infinite loop.

## QC boundary

Deterministic technical QC remains canonical.  `VisionQCService` is an
optional layer above it and returns explicitly labelled `AI_ANALYSIS` metrics.
When no Vision provider is configured it reports `NOT_RUN`, never a false
pass.  Human Review and Producer decisions remain the authority for creative
acceptance.

## Post-production MVP

Migration 012 persists `PostProductionPlan`, subtitle, voice, music and
append-only render attempts.  The service provides:

- script-derived, scene/beat traceable subtitle timelines and SRT export;
- editable subtitle text and enable/disable state;
- a VoiceTrack boundary with truthful TTS readiness;
- project-isolated local BGM import, safe relative paths and gain/loop metadata;
- a basic source/voice/BGM mix through the existing FFmpeg seam;
- atomic project-relative post MP4 output and immutable render history.

The `后期与成片` page exposes these controls without becoming a nonlinear
editor.  The source Final Assembly is never overwritten.

## Desktop and branding

`desktop/launcher.py` starts Streamlit on loopback, waits for health and
cleans up its child process.  It provides browser fallback and a `--smoke`
mode.  PyWebView and PyInstaller are optional packaging prerequisites and were
not installed in this closure.  `branding.py` centralizes the replaceable
AIDrama Studio mark and product metadata; upstream MIT attribution is retained
in `LICENSE` and `NOTICE`.

## Migrations and compatibility

The fresh-install path applies migrations 001–014 in order and repeated
initialization is idempotent.  Migration 014 repairs databases created by the
early reference-set prototype by creating the canonical reference asset
tables and projecting legacy rows without deleting the legacy tables.

## Validation evidence

- AIDrama suite: **160 passed**, 10 warnings.
- Full repository: **758 passed, 10 skipped, 1 known baseline failure** in
  `test_worker_logs_are_available_without_streamlit_session_state` (Windows
  path separator formatting only); no new regressions.
- Fresh migration and idempotency tests pass; the repaired default database
  reaches migration 014.
- Python compile and `git diff --check` pass.
- AIDrama startup and original MPT startup smoke are available; desktop
  launcher `--smoke` reached loopback health and cleaned up.
- Real local final assembly and post-render paths use the existing FFmpeg
  seam; subtitle/SRT and BGM-only/combined media smokes are covered by tests.
- Browser acceptance was exercised at 1920×1080 and 1366×768 for Dashboard,
  Story/Script, Reference Assets, AI Director/Producer, Production, QC /
  Review, Post / Final and Settings.  No horizontal overflow was observed;
  sidebar, tabs, readiness cards, blockers, preview/export and post controls
  remained reachable.
- Secret scan confirms no API key or token is rendered or committed.

## Live-provider status and limitations

The product is model-ready but this closure does not fabricate external model
success.  Current gates are:

| Capability | State |
| --- | --- |
| LLM | deferred when no configured key |
| Image generation | deferred / unavailable boundary |
| Wan video | blocked when `DASHSCOPE_API_KEY` is absent |
| Vision | deferred / unavailable boundary |

The separate `AIDRAMA_FINAL_LIVE_MODEL_ACCEPTANCE_V1` goal may later run one
real LLM, image, video and Vision end-to-end acceptance.  No Seedance or GPT
Image API is guessed or claimed here.
