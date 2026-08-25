# AIDrama Studio V1.0 Master Goal State

This is the durable, non-secret resume state for the V1.0 release-closure
goal. `HEAD` means the commit containing this document; obtain the exact SHA
with `git rev-parse HEAD` after checkout.

## Current state

- Target branch: `goal/aidrama-studio-v1-0-final-product-release`
- Goal base: `3ce90aad6a70e6173a3826bd4e8eb6c039e0221b`
- Current head: `HEAD`
- Active checkpoint: reconcile the narrowly audited upstream MPT security
  commits after portable recovery/startup-reconciliation closure.
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

## Externally blocked gates

- Live LLM, image, video, Vision and TTS gates require credentials plus
  explicit paid/live authorization.
- Real multi-shot paid E2E is not authorized.
- PyInstaller desktop build and Windows installer execution require missing
  build tools; none may be installed silently.
- Code signing requires an external signing certificate.

## Known remaining work

- Close provider-specific cloud disclosure/input provenance and remote-file
  lifecycle evidence that remain incomplete for Wan/Gemini paths.
- Complete upstream MPT security delta review, distribution license/SBOM and
  build/installer definitions without installing tools.
- Run full non-live E2E, portable restore E2E, browser acceptance at both
  required resolutions, full project regression, startup/desktop smokes,
  security matrix and final self-audit.
- Update the final closure report with evidence and truthful external blockers.

## Next safe implementation step

Reconcile the six already-audited upstream MPT security commits as narrow,
reviewable patches (no wholesale upstream merge), run their focused regressions
and the full project suite, then continue release/legal/build definitions.

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
- `REGION_AWARE_PRIVACY_DISCLOSURE=PASS`
- `PROVIDER_SWITCH_NEW_TASKS_ONLY=PASS`
- `PROVIDER_SELECTION_PRECEDENCE=PASS`
- `REGIONAL_PROVIDER_SETTINGS_UI=PASS`
- `PAID_PROVIDER_SELECTION_CONFIRMATION=PASS`
- `MAINLAND_PROVIDER_PRESET=PASS`
- `INTERNATIONAL_PROVIDER_PRESET=PASS`
- `CUSTOM_PROVIDER_MIX=PASS`
- `PROVIDER_SWITCHING=PASS`
