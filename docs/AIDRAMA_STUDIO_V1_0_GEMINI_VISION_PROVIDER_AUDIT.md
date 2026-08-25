# AIDrama Studio V1.0 — Gemini Vision Provider Audit

Audit date: 2026-08-25 (Asia/Shanghai)

This audit records the official, non-live contract used by AIDrama Studio's
primary V1 semantic video-analysis provider. No API request, file upload, model
invocation, or paid action was performed during the audit or implementation.

## Provider decision

- Provider: Google Gemini API
- Capability: `VISION`
- Exact V1 model: `gemini-3.7-flash`
- API: Gemini Interactions API at
  `https://generativelanguage.googleapis.com/v1beta/interactions`
- Local implementation: direct HTTPS/Files API boundary using the existing
  HTTP dependency; no Google SDK dependency was added.

The official model page identifies `gemini-3.7-flash` as a stable model,
updated in August 2026. It accepts text, image, video, audio and PDF input,
supports structured outputs, has a 1,048,576-token input limit and a
65,536-token output limit. That makes it suitable for one bounded shot video,
sampled frames, exact reference images and a structured semantic-QC result.

## Official contract evidence

The implementation was checked against these official sources:

- [Gemini 3.7 Flash model](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)
- [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Image understanding](https://ai.google.dev/gemini-api/docs/image-understanding)
- [Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [Files API](https://ai.google.dev/gemini-api/docs/files)
- [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Interactions API reference](https://ai.google.dev/api/interactions-api)

Relevant verified facts:

- Interactions is the recommended current API for the latest Gemini models and
  multimodal capabilities.
- File API input is recommended for substantial/reused video. The official
  REST flow uses resumable upload and a returned file URI.
- Image inputs may use File API URIs with `type=image`; video uses
  `type=video`.
- Structured output uses `response_format` with MIME
  `application/json` and a JSON schema.
- Interactions are stored by default for server-side state. AIDrama explicitly
  sends `store=false` because Vision QC is a stateless one-shot analysis.
- Uploaded File API objects expire automatically after 48 hours and can be
  deleted explicitly. AIDrama attempts explicit deletion in `finally`; a
  failed delete is persisted only as a count plus the truthful 48-hour
  auto-expiry fallback, never as a remote URI.
- Provider-native video understanding samples roughly one frame per second by
  default and may miss rapid motion or quick scene changes. AIDrama therefore
  supplies a deterministic local first/middle/last/interval frame manifest and
  denser samples around suspicious deterministic-QC windows.

## Frozen input contract

One Vision request is compiled from canonical execution truth only:

1. physical generated `ProductionArtifact`, with SHA-256 reverified;
2. immutable `VisionFrameManifest` and physical sampled frames;
3. exact ordered `RuntimePlan.reference_version_ids` and their roles;
4. exact reference files, with stored SHA-256 reverified;
5. frozen `GenerationBrief` content/hash when the execution has one;
6. a versioned prompt template and JSON response schema.

The adapter cannot query latest Story/Script/Reference state or substitute a
different reference. Absolute local paths are used only in memory to open
validated project-local files; persisted provenance contains IDs, roles,
MIME types and hashes, not paths.

## Output and authority

The required structured metrics are:

- `CHARACTER_CONSISTENCY`
- `SCENE_CONSISTENCY`
- `SHOT_COMPLIANCE`
- `VISUAL_DEFECTS`
- `ACTION_COMPLIANCE`
- `STYLE_CONSISTENCY`
- `CONTINUITY`

Every result is labelled `AI_ANALYSIS` and append-only. It is persisted with
provider, exact model, artifact, frame manifest, reference version IDs,
prompt-template hash, safe input provenance, usage metadata in the canonical
invocation ledger, and a safe interaction ID. Deterministic technical QC and
the latest human review remain authoritative; Vision never silently approves
a failed or human-rejected shot.

## Privacy, safety and lifecycle

- A key plus `AIDRAMA_ALLOW_PAID_LIVE_TESTS=1` is required before any network
  action; detecting a credential alone is insufficient.
- The API base and returned file/upload URIs must use the exact official HTTPS
  host and cannot contain userinfo or non-default ports.
- Uploads are size-bounded and streamed from disk; files/hashes are validated
  before the first network side effect.
- Remote upload URLs and File API URIs remain memory-only.
- Provider response bodies are never embedded in persisted transport errors.
- Creative context is explicitly delimited as untrusted data; secret-shaped
  fields and private paths are removed before prompt compilation.
- Remote files are deleted once per request in reverse creation order. If an
  upload is known but processing/response parsing fails, cleanup still runs.

## Known live limitations

- No live Gemini credential or paid request was authorized in this checkpoint.
- Account/region availability, quotas, real latency, billing and actual model
  output quality remain live gates.
- No price is displayed or persisted because this checkpoint did not verify a
  reliable account-specific currency estimate.
- A File API response lost after a successful remote upload may leave no local
  remote identity to delete; the provider's documented automatic expiry is the
  remaining safety net. The adapter never retries an analysis automatically.

## Non-live verification

Contract tests prove exact media order, JSON schema, `store=false`, stable
model pinning, explicit authorization, SHA mismatch rejection, remote cleanup
on processing/parse failures, truthful delete-failure retention metadata,
reference-provenance rejection, append-only frame/analysis history, project
isolation and invocation-ledger linkage. The complete AIDrama suite after this
checkpoint is `252 passed, 10 warnings`.
