# AIDrama Studio V1.0 Master Goal State

This is the durable, non-secret resume state for the V1.0 release-closure
goal. `HEAD` means the commit containing this document; obtain the exact SHA
with `git rev-parse HEAD` after checkout.

## Current state

- Target branch: `goal/aidrama-studio-v1-0-final-product-release`
- Goal base: `3ce90aad6a70e6173a3826bd4e8eb6c039e0221b`
- Current head: `HEAD`
- Active checkpoint: close browser acceptance, final security/self-audit and
  truthful externally blocked desktop/live release gates.
- No dependency was installed and no live provider request was made.

## Completed checkpoints with current local evidence

- The existing correctness, intake, output-profile, RuntimePlan,
  GenerationBrief, AppData, diagnostics, credential, final-assembly, TTS and
  security foundations from prior release checkpoints remain present.
- Production UI enqueues a bounded, explicitly authorized provider/model plan
  and returns without running provider polling in the Streamlit lifecycle.
- The desktop process owns one background runner and one writable data-root
  lock; frozen provider tasks survive Streamlit reruns and desktop restarts.
- The Seedance adapter now targets the officially documented
  `doubao-seedance-2-5-260628` Ark task endpoint, emits typed text/image
  content from an immutable RuntimePlan and GenerationBrief, preserves exact
  ordered reference trace, and implements result retrieval.
- Seedance and Wan provider results use one HTTPS-only, exact-host,
  public-DNS, redirect-revalidated, size-bounded streaming download boundary.
- Large provider video results flow directly into project-local temporary
  files, then flush/fsync, hash, atomic-finalize and DB persistence. Interrupted
  writes remove temporary files; a DB insert failure compensates the finalized
  file.
- Provider success plus artifact-download failure remains a recoverable
  artifact-pending state tied to the original provider task; it does not
  submit another paid generation.
- Provider polling now distinguishes transient 429/5xx/network interruption
  from definitive provider failure, honors numeric or HTTP-date Retry-After,
  applies bounded backoff, and cold-resumes the original task. Unknown states
  require reconciliation instead of producing a false FAILED result or a new
  paid submission.
- Nested provider metadata and operator errors drop credentials and result
  URLs before persistence.
- The primary Vision boundary now pins the officially documented stable
  `gemini-3.7-flash` Interactions API contract. It sends one physical video,
  immutable deterministic sampled frames, the frozen GenerationBrief and exact
  ordered RuntimePlan reference versions; it requests schema-constrained
  `AI_ANALYSIS` output with `store=false`.
- Vision File API inputs are streaming/size/hash checked, memory-only remote
  URIs are explicitly deleted in `finally`, and delete failure records only a
  count plus the documented 48-hour expiry fallback. Analysis, frame manifests
  and canonical AI invocation-ledger states remain append-only and
  project-scoped.
- Migration 024 adds exact Vision reference IDs, prompt-template hash, safe
  cloud-input provenance and interaction identity without storing credentials,
  local absolute paths or remote file URLs.
- Migration 025 extends the canonical capability profiles with explicit
  Mainland-China / international / local deployment metadata, endpoint class,
  non-secret credential reference and verification state. A scope-aware
  selection setting stores only preset intent and exact profile references; it
  does not duplicate provider/model truth.
- Settings now offers 中国大陆 / 国际 / 自定义 model schemes and independent
  LLM, image, generative-video, Vision and TTS choices. Resolution is
  deterministic: explicit job endpoint > project default > global default >
  legacy compatibility only when no policy exists. A missing or failed
  selected provider remains UNAVAILABLE; it never crosses region or provider
  automatically.
- Production authorization freezes provider, model, deployment region,
  endpoint profile/class, exact reference count, bounded request count and
  transmitted content types. The disclosure checkbox is keyed and
  server-validated by a selection fingerprint, so a region/model change
  invalidates stale consent.
- Existing RuntimePlans remain immutable after Settings changes. New plans pin
  the newly resolved endpoint and selection source, and the background resolver
  requires the exact frozen provider/endpoint/model identity.
- Story Bible, Structured Script and Shot Plan generation now resolve through
  one canonical LLM capability gateway. Each structured operation freezes the
  exact provider/model/endpoint once, performs domain validation before a
  terminal success is recorded, and permits at most one repair on that same
  frozen provider.
- The LLM invocation ledger records the actual upstream provider, exact
  effective model, endpoint/region, operation, prompt hash/length and source
  revision IDs without persisting raw prompts, responses, secrets, signed URLs
  or local absolute paths. Invalid structured output is truthfully recorded as
  FAILED/OUTPUT_INVALID before repair.
- MPT LLM readiness and execution now use the same frozen configuration.
  Moonshot China and Global endpoints have distinct identities and deployment
  classifications; custom/unknown endpoints remain UNSPECIFIED rather than
  being guessed.
- FinalAssembly rendering now verifies every frozen source hash before invoking
  its adapter and persists the actual probed source durations, hashes and
  cumulative timeline in the immutable render attempt.
- Final subtitles now derive from the exact successful FinalAssembly render
  attempt pinned by the current PostProductionPlan. Project/assembly/job/shot
  plan/script provenance is fail-closed; non-contiguous repeated beats become
  separate cues; overlapping, incomplete or out-of-duration timelines are
  rejected.
- TTS requires a persisted SubtitleTrack from the current chain, preserves cue
  gaps and absolute timing through a real FFmpeg timeline, and stores physical
  output hash, size, duration and cue fingerprint. Post rendering rejects a
  changed or mismatched source subtitle track.
- Post render attempts freeze the FinalAssembly, subtitle, voice, music and
  AudioMix fingerprints. Frozen input files and completed final/post outputs
  are hash-checked before use; deleted or tampered outputs cannot satisfy
  current workflow completion.
- FFmpeg audio mixing pads/trims each input to the source-video duration and
  verifies the rendered duration, so short voice/music no longer truncates the
  final video. A real local media test covers this path.
- The Post UI only selects a plan belonging to its visible immutable
  FinalAssembly, preventing an older production chain from being reused.
- Portable `.aidrama` export now uses a schema-complete project-owned table
  allowlist, closed FK/soft-FK graph, exact content and file hashes, Windows
  path-alias/ZIP-member defenses and a bounded manifest. Export publishes only
  after an isolated safe-import verification; import restores into same-volume
  staging and atomically finalizes beside the SQLite row transaction.
- Project archives preserve canonical immutable rows exactly. If legacy
  operational metadata would require secret/path redaction, export fails
  closed instead of silently changing frozen RuntimePlan, snapshot or hash
  provenance. Authored creative text is never keyword-redacted.
- Dashboard now gives normal users verified `.aidrama` export and canonical-ID
  restore. Restore never approximates a project clone by rewriting only
  project_id columns; an existing canonical ID is rejected without overwrite.
- Project deletion creates and validates a restorable Recovery Archive first.
  One shared fail-closed active-work predicate protects provider tasks,
  Production jobs/executions/shots/attempts, QC, FinalAssembly and Post work,
  and is rechecked inside the deletion transaction.
- Diagnostics covers FK integrity, FFmpeg/provider readiness and canonical
  Source Pack/reference/production/final/post/voice/music media. Cleanup only
  considers stale application-shaped hidden temporary names, then rechecks
  active work and canonical references under `BEGIN IMMEDIATE` before unlink.
- Startup reconciliation resumes RUNNING executions through the original
  provider identity, retries artifact download without another provider POST,
  and treats persisted `SUBMITTING`/submission-uncertain work as manual
  reconciliation. A crash-window test proves no second paid submit occurs.
- Current validation: Python compile PASS, `git diff --check` PASS, focused
  recovery/diagnostics/worker tests `55 passed, 1 expected duplicate-ZIP
  warning`, complete AIDrama suite `313 passed, 11 warnings`, and
  AIDrama/original-MPT HTTP startup smokes PASS. No live request was made.
- Six narrowly audited upstream commits are reconciled with exact `-x`
  provenance: custom-audio confinement, `/tasks` symlink defense, safe local
  material upload, request-ID sanitization, configured API-key enforcement and
  Windows/mapped-drive logging-path handling. No wholesale upstream merge was
  performed.
- Focused upstream/MPT regressions pass with `149 passed, 3 skipped, 79
  subtests passed`; `test_webui_task.py` passes all 16 tests and closes the
  historical Windows separator baseline.
- Complete repository regression after reconciliation: `944 passed, 11
  skipped, 14 warnings, 4402 subtests passed`; new regressions: `0`.
- Wan request trace now persists the SHA-256 of the exact prompt, canonical
  provider request and actually transmitted reference media plus its frozen
  asset-version identity. Raw prompts and media bytes are not durable provider
  metadata.
- Gemini remote File API cleanup evidence is retained for successful and
  failed analysis: upload/delete/failure counts, `store=false`, and the
  documented 48-hour automatic-expiry fallback when deletion fails. Remote
  file names and URIs are never persisted.
- Provider-provenance focused tests pass with `24 passed`; the complete
  AIDrama suite passes with `314 passed, 11 warnings`, and Python compile plus
  `git diff --check` pass.
- Creative Intake is exposed as a normal-user Source Pack tab with one-line
  text, multi-document and multi-image import, deterministic advisory
  classification, local normalization and explicit image promotion. Story
  generation persists the exact Source Pack IDs and normalized-brief ID in
  both the LLM invocation provenance and Story revision input snapshot.
- Source Pack image promotion validates the approved Story target before any
  mutation, verifies the immutable source file by size/SHA and image decoder,
  then writes asset/version/binding/lock as one SQLite transaction. Injected
  failure rolls back all canonical rows and compensates a newly finalized
  unreferenced blob.
- FinalAssembly now maps its immutable source identities and original source
  durations onto the probed final-container clock. This closes the real FFmpeg
  frame-timebase drift that otherwise made a valid final subtitle extend past
  the actual final MP4.
- A real non-live end-to-end acceptance covers mixed Source Pack input,
  canonical fake-LLM planning boundaries, Story/Script/Shot approval, two
  promoted and locked references, one mock-external production submission
  returning a real local MP4, deterministic physical-media QC, human review,
  real FinalAssembly, subtitle+BGM FFmpeg post render, cold repository reload,
  and `.aidrama` export/import into a clean data root with identity and SHA
  verification. Provider submission count remains exactly one.
- Release engineering now freezes product version `1.0.0`, retains verified
  preview-archive import compatibility, includes LICENSE/NOTICE/third-party
  notices in the build definition, generates a lock-scoped CycloneDX 1.5 SBOM,
  streaming checksums and build provenance, and supplies a stable per-user
  Inno Setup definition that preserves AppData on upgrade/uninstall.
- Release auditing fails closed on unapproved Microsoft YaHei/STHeiti fonts
  and reports every packaged FFmpeg executable for exact-binary review.
- Current checkpoint validation: release/archive/desktop/Story focused tests
  `53 passed`; Creative Intake/reference/Story focused tests `35 passed`;
  non-live E2E plus FinalAssembly runtime `9 passed`; complete AIDrama suite
  `333 passed, 11 warnings`; Python compile and `git diff --check` pass.

## Externally blocked gates

- Live LLM, image, video, Vision and TTS gates require credentials plus
  explicit paid/live authorization.
- Real multi-shot paid E2E is not authorized.
- PyInstaller desktop build and Windows installer execution require missing
  build tools; none may be installed silently.
- Code signing requires an external signing certificate.
- The currently available imageio-ffmpeg binary is GPLv3-or-later and includes
  GPL codecs; redistribution remains blocked on a release-owner legal/compliance
  decision for that exact binary and its obligations.

## Known remaining work

- Run browser acceptance at both required resolutions, full project
  regression, startup/desktop source smokes, security matrix and final
  self-audit.
- Update the final closure report with evidence and truthful external blockers.

## Next safe implementation step

Run browser acceptance and security self-audit, then refresh the complete
regression/startup evidence and write the final closure report.

## Provider provenance and remote lifecycle acceptance

- `PROVIDER_REQUEST_REPRODUCIBILITY=PASS`
- `ACTUAL_PROVIDER_REFERENCE_TRACE=PASS`
- `CLOUD_INPUT_PROVENANCE=PASS`
- `REMOTE_FILE_LIFECYCLE=PASS`
- Wan raw prompt persisted: `NO`
- Gemini remote identities/URIs persisted: `NO`

## Upstream reconciliation acceptance

- `UPSTREAM_MPT_DELTA_AUDIT=PASS`
- `UPSTREAM_SECURITY_FIXES_RECONCILED=PASS`
- `WINDOWS_PATH_BASELINE_FIXED=PASS`
- `FULL_PROJECT_TEST_RESULT=944 passed, 11 skipped, 4402 subtests passed`
- `NEW_REGRESSIONS=0`
- MPT core change files: `app/asgi.py`, `app/controllers/base.py`,
  `app/controllers/v1/llm.py`, `app/controllers/v1/video.py`,
  `app/models/exception.py`, `app/services/material_upload.py`,
  `app/services/task.py`, `app/utils/logging_utils.py`, `cli.py`, and
  `config.example.toml`.
- MPT core change reason: narrowly reconciled upstream security fixes and one
  proven Windows logging-path defect; no broad refactor.

## Project recovery and startup acceptance

- `PROJECT_EXPORT=PASS`
- `PROJECT_IMPORT=PASS`
- `PROJECT_BACKUP_RESTORE=PASS`
- `PROJECT_ACTIVE_TASK_DELETE_GUARD=PASS`
- `PROJECT_DELETE_RECOVERY=PASS`
- `PROVENANCE_AWARE_CLEANUP=PASS`
- `DIAGNOSTICS_CENTER=PASS`
- `STARTUP_RECONCILIATION=PASS`
- Paid `SUBMITTING` crash window: FAIL-CLOSED / no automatic resubmit

## Final media and post-production acceptance

- `ARTIFACT_HASH_VERIFY_BEFORE_ASSEMBLY=PASS`
- `FINAL_TIMELINE_MAP=PASS`
- `FINAL_MEDIA_SUBTITLE_TIMING=PASS`
- `TTS_TIMELINE_ALIGNMENT=PASS`
- `BGM=PASS`
- `AUDIO_MIX=PASS`
- `REAL_POST_RENDER=PASS`
- `POST_OUTPUT_PROBE=PASS`
- `POST_OUTPUT_SHA256=PASS`
- `POST_CURRENT_CHAIN_DERIVATION=PASS`

## Canonical LLM integration acceptance

- `LLM_CAPABILITY=PASS`
- `LLM_CANONICAL_CAPABILITY_INTEGRATION=PASS`
- `AI_INVOCATION_LEDGER=PASS`
- Structured PRIMARY/REPAIR provider freeze: PASS
- Structured validation before ledger success: PASS
- Story/Script/Shot source provenance: PASS
- Missing selected LLM fail-closed: PASS
- Moonshot China/Global endpoint identity: PASS
- Secret/raw prompt/raw response persistence: NONE FOUND

## Regional provider switching acceptance

- `PER_CAPABILITY_PROVIDER_SELECTION=PASS`
- `PROVIDER_REGION_CLASSIFICATION=PASS`
- `REGIONAL_PROVIDER_ENDPOINTS=PASS`
- `PROVIDER_PRESET_RESOLUTION=PASS`
- `NO_SILENT_CROSS_REGION_FALLBACK=PASS`
- `REGION_AWARE_PRIVACY_DISCLOSURE=PARTIAL` (VIDEO is enforced; Story/Script
  remote LLM and IMAGE/VISION/TTS still need the same first-transmission gate)
- `PROVIDER_SWITCH_NEW_TASKS_ONLY=PASS`
- `PROVIDER_SELECTION_PRECEDENCE=PASS`
- `REGIONAL_PROVIDER_SETTINGS_UI=PASS`
- `PAID_PROVIDER_SELECTION_CONFIRMATION=PASS`
- `MAINLAND_PROVIDER_PRESET=PASS`
- `INTERNATIONAL_PROVIDER_PRESET=PASS`
- `CUSTOM_PROVIDER_MIX=PASS`
- `PROVIDER_SWITCHING=PARTIAL` (configuration is complete; IMAGE/VISION/TTS
  runtime enforcement and no-fallback evidence remain open)

## Duration/editability addendum — durable heavy-work checkpoint

This checkpoint absorbs Part 13–16 of the final consolidated addendum into
the same V1 architecture. It does not create a competing production queue:
paid Provider submission/poll/download truth remains in the existing
`ProviderTask` lifecycle, while one desktop-owned host now dispatches that
runner and the canonical local `HeavyJob` runner from the same durable loop.

- Migration 028 adds project-scoped `heavy_jobs` and append-only
  `heavy_job_events`. Immutable input JSON/SHA, idempotency, retry ancestry,
  stage/progress, safe errors, cancellation truth and terminal timestamps are
  persisted in SQLite. FinalAssembly/Post attempts are atomically bound to the
  HeavyJob that owns them.
- FinalAssembly, its real 1080p→4K deterministic normalization, Post render
  and media probe/hash/finalize work no longer execute inside a Streamlit
  button request. UI actions enqueue and return immediately, then display
  durable queued/running/stage/terminal truth.
- A process restart converts abandoned local `RUNNING` work to
  `INTERRUPTED`, closes its bound render attempt without publishing partial
  media, and permits only an explicit retry that creates a new job and attempt
  from frozen inputs.
- Unknown-duration operations expose a truthful stage with no invented
  percentage. Measurable copies persist current/total/unit evidence and derive
  percentage from those values.
- Final and Post MP4 export no longer call `Path.read_bytes()`. A user-selected
  absolute destination is served by a chunked, fsync'd, SHA/size-verified,
  atomic no-overwrite delivery copy. The canonical project artifact is never
  moved, renamed or deleted.
- Local preflight checks required source paths and the actual destination
  volume's free space. Hardware acceleration is reported as `NOT_USED` for
  copy/archive work and `NOT_ASSERTED` for media work; no acceleration claim is
  fabricated.
- Project export/import use HeavyJob handlers. Export rejects all other active
  project work, holds a database write snapshot while copying live files once
  into staging, excludes `.tmp`/`.partial`/`.in-progress`, and builds/verifies
  the archive only from staging. This removes the prior hash-then-reread-live
  TOCTOU window.
- Focused validation covers migration constraints, event/input immutability,
  idempotent enqueue, project isolation, ordered events, null versus measurable
  progress, real/unsupported cancellation, cold recovery, retry history,
  linked FinalAssembly attempt recovery, Unicode/space/parenthesis large-media
  copy, no-overwrite/cancel cleanup, active-work archive blocking, staging
  consistency and background project export→import. Result: `57 passed, 11
  warnings`; the complete AIDrama suite passes with `358 passed, 11
  warnings`; Python compile and `git diff --check` pass.

Checkpoint acceptance:

- `HEAVY_JOB_BACKGROUND_EXECUTION=PASS`
- `DURABLE_HEAVY_JOB_MODEL=PASS`
- `FINAL_ASSEMBLY_NONBLOCKING=PASS`
- `POST_RENDER_NONBLOCKING=PASS`
- `FOUR_K_RENDER_NONBLOCKING=PASS`
- `LOCAL_RENDER_INTERRUPTION_RECOVERY=PASS`
- `TRUTHFUL_HEAVY_JOB_PROGRESS=PASS`
- `LARGE_FINAL_VIDEO_EXPORT=PASS`
- `USER_SELECTED_EXPORT_DESTINATION=PASS`
- `LOCAL_RESOURCE_PREFLIGHT=PASS`
- `HARDWARE_CAPABILITY_TRUTH=PASS`
- `CONSISTENT_PROJECT_EXPORT_SNAPSHOT=PASS`

Remaining addendum engineering work after this checkpoint includes canonical
image candidate selection/promotion, creative video regeneration and current
qualified-shot-source truth, dependency-aware outdated propagation, Preview
promotion, provider `CONTENT_REJECTED`, exact final target-duration control,
and the four required addendum E2E acceptances. Browser, full-repository and
release/installer gates remain pending or externally blocked as recorded
above.

## Duration/editability addendum — candidate and shot-source checkpoint

Migration 029 extends the existing ReferenceAsset and Production truth rather
than introducing parallel candidate or retry systems.

- Generated reference images now persist as project-isolated immutable DRAFT
  candidates with exact Provider/model/endpoint/region, prompt/request hashes,
  source Story revision, SHA/size/MIME/path, regeneration parent and append-only
  decision events. A candidate does not create a `ReferenceAssetVersion` and
  never changes `current_version_id` merely because generation succeeded.
- The Reference Asset Center can cold-reload, compare/preview, reject and
  explicitly promote persisted candidates. Promotion atomically creates one
  Draft `ReferenceAssetVersion`; the existing separate Lock action remains the
  only operation that makes it production-qualified. Rejected candidates and
  historical blobs remain immutable.
- Each Shot now has append-only explicit source decisions over the existing
  Execution → physical Artifact → real QC → latest Review chain. Decisions
  freeze current GenerationBrief ID/hash when present. FinalAssembly respects
  that selection, rejects stale/invalid selected provenance instead of silently
  falling back, and freezes the exact decision ID in its immutable manifest.
- Preview video remains ineligible by default. A distinct explicit
  `PREVIEW_PROMOTED` source decision is required before the same physically
  valid, QC-passed artifact can enter FinalAssembly.
- A human `REJECTED` review never auto-submits. The explicit creative
  regeneration service validates the latest rejection and its exact successful
  Execution/Artifact/QC/Shot chain, then appends Attempt/Execution 2 with
  immutable retry/review ancestry. Attempt 1 remains unchanged.
- Project archive allowlists include candidate/event and shot-source decision
  history, and FinalAssembly freeze now revalidates the complete durable source
  chain inside its transaction.
- Non-live acceptance proves candidate cold recovery/reject/regenerate/promote,
  no auto-lock, tamper rejection without partial promotion, explicit Preview
  promotion, append-only source replacement, and Attempt 1 success → QC PASS →
  REJECTED → explicit Attempt 2 → QC PASS → APPROVED → current source resolves
  Attempt 2.
- Focused candidate/source/migration/reference/FinalAssembly/Execution/archive
  validation passes with `73 passed, 1 warning`; the complete AIDrama suite
  passes with `365 passed, 11 warnings`. Python compile and
  `git diff --check` pass.

The remaining UI/provider work is explicit: the real IMAGE generation request
still needs the canonical ProviderProfile/disclosure/background gateway, and
the Production page still needs to expose the new targeted creative-regenerate
and source-selection actions. No live request is claimed by this checkpoint.
