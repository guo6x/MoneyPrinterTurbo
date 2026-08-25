# AIDrama Studio V1.0 Master Goal State

This is the durable, non-secret resume state for the V1.0 release-closure
goal. `HEAD` means the commit containing this document; obtain the exact SHA
with `git rev-parse HEAD` after checkout.

## Current state

- Target branch: `goal/aidrama-studio-v1-0-final-product-release`
- Goal base: `3ce90aad6a70e6173a3826bd4e8eb6c039e0221b`
- Current head: `HEAD`
- Active checkpoint: regional provider preset/per-capability switching and
  canonical selection precedence.
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
- Current validation: Python compile PASS, `git diff --check` PASS, focused
  provider/runtime/security tests PASS, complete AIDrama suite
  `252 passed, 10 warnings`.

## Externally blocked gates

- Live LLM, image, video, Vision and TTS gates require credentials plus
  explicit paid/live authorization.
- Real multi-shot paid E2E is not authorized.
- PyInstaller desktop build and Windows installer execution require missing
  build tools; none may be installed silently.
- Code signing requires an external signing certificate.

## Known remaining work

- Implement the newly required Mainland/International/Custom provider preset
  layer, per-capability selection, region/endpoint metadata, deterministic
  precedence and no-silent-cross-region-fallback policy without creating a
  second provider truth.
- Finish canonical LLM invocation-ledger integration.
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

Add provider region/endpoint profiles and preset resolution on top of the
canonical CapabilityRegistry/provider inventory. Prove preset switching affects
only new RuntimePlans, mixed per-capability selection works, missing providers
remain unavailable, and no cross-region fallback occurs without explicit user
approval.
