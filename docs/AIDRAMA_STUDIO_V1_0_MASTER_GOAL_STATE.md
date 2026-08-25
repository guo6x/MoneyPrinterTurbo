# AIDrama Studio V1.0 Master Goal State

This is the durable, non-secret resume state for the V1.0 release-closure
goal. `HEAD` means the commit containing this document; obtain the exact SHA
with `git rev-parse HEAD` after checkout.

## Current state

- Target branch: `goal/aidrama-studio-v1-0-final-product-release`
- Goal base: `3ce90aad6a70e6173a3826bd4e8eb6c039e0221b`
- Current head: `HEAD`
- Active checkpoint: final subtitle timing and current-chain post-production
  correctness after canonical LLM integration closure.
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
- Current validation: Python compile PASS, `git diff --check` PASS, focused
  provider/runtime/security/LLM tests PASS, complete AIDrama suite
  `273 passed, 10 warnings`. No live request was made.

## Externally blocked gates

- Live LLM, image, video, Vision and TTS gates require credentials plus
  explicit paid/live authorization.
- Real multi-shot paid E2E is not authorized.
- PyInstaller desktop build and Windows installer execution require missing
  build tools; none may be installed silently.
- Code signing requires an external signing certificate.

## Known remaining work

- Close final subtitle timing, cloud disclosure/input provenance, remote file
  lifecycle, portable project restore/delete recovery and diagnostics repair
  actions.
- Complete upstream MPT security delta review, distribution license/SBOM and
  build/installer definitions without installing tools.
- Run full non-live E2E, portable restore E2E, browser acceptance at both
  required resolutions, full project regression, startup/desktop smokes,
  security matrix and final self-audit.
- Update the final closure report with evidence and truthful external blockers.

## Next safe implementation step

Audit and close final subtitle timing plus current-chain post-production
derivation without changing media engines or making a live provider call.

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
