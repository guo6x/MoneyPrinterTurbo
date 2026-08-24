# AIDrama Quick Task011 — Real Provider Live E2E Smoke

## Scope and outcome

This phase is an evidence-only audit. No provider, adapter, migration, UI,
dependency, or MPT-core implementation was added. The repository does not
contain a verified generative-video provider that can be invoked through the
frozen AIDrama production boundary, and the effective local configuration has
no usable video-provider credential. Consequently no paid or external
generative-video request was made.

## Capability audit

| Capability | Implementation | Credential/config | Media semantics | AIDrama reference input | Local artifact |
| --- | --- | --- | --- | --- | --- |
| LLM story/script/terms | `app/services/llm.py`, `app/models/llm_provider.py` | provider-specific `*_api_key`, model and base URL in `config.toml` | text generation only | not accepted by the adapter | text response, not a ProductionArtifact |
| Pexels | `app/services/material.py` | `app.pexels_api_keys` | stock-video search and download | no | downloaded local clips |
| Pixabay | `app/services/material.py` | `app.pixabay_api_keys` | stock-video search and download | no | downloaded local clips |
| Coverr | `app/services/material.py` | `app.coverr_api_keys` | stock-video search and download | no | downloaded local clips |
| LoomLoom video SkillBot | `app/services/loomloom.py`, `app/services/task.py` | `loomloom_api_token` (or Shengsuan key when selected) and paid quote confirmation | remote SkillBot returns MP4 video-material rows; the repository exposes no model identity or generative-media contract | no; rows contain only `scenePrompt`, `aspectRatio`, and `sceneIndex` | downloaded MP4 material files |
| Generative image provider | no implementation found | none | unavailable | unavailable | unavailable |
| Generative video provider | no verified implementation found | none | unavailable as a classified provider | unavailable | unavailable |

LoomLoom is intentionally not classified as a verified generative-video
provider here. Its adapter is a paid, opaque Market SkillBot material source;
the submitted prompt explicitly requests “stock-footage-style video”, and no
model, reference-conditioning, or image-to-video contract is present in the
repository. It must not be presented as evidence of AI video generation.

## Current MPT production classification

`CURRENT_MPT_PRODUCTION_CLASS=STOCK_MEDIA_COMPOSITION`

The effective default is `video_source = "pexels"`. In
`app/services/task.py`, `_run_pipeline()` obtains script/terms, downloads
remote or local material in `get_video_materials()`, and then calls the local
`app.services.video.generate_video()` compositor. The Pexels/Pixabay/Coverr
branches retrieve stock media; they do not synthesize frames. The optional
LoomLoom branch downloads remote MP4 material before the same local pipeline.

## Credential readiness and live smoke decision

The loaded `config.toml` has empty values for the LoomLoom token, Pexels,
Pixabay, Coverr, and all registered LLM provider credentials. The effective
configuration loader reads `config.toml`; no credential was copied, persisted,
printed, or written by this phase. Therefore:

```text
GENERATIVE_VIDEO_PROVIDER_AVAILABLE=NO
LIVE_PROVIDER_CREDENTIAL_READY=NO
LIVE_PROVIDER_SELECTED=NONE
LIVE_GENERATIVE_VIDEO_SMOKE=NOT_RUN
LIVE_E2E_GATE=BLOCKED_NO_PROVIDER
```

The existing AIDrama adapter boundary is also not a live-provider wiring:
`aidrama_studio/services/adapters/mpt_runtime.py` requires an injected runtime
client and raises `NotImplementedError` when one is absent. The adapter does
not call `app.services.task.start()` implicitly, and the existing MPT task
pipeline has no mapper from `ProductionInputSnapshot` to a one-shot provider
request. Wiring that boundary would be a future implementation task and is
outside this audit.
## Reference support

```text
REFERENCE_INPUT_SUPPORTED=NO
REFERENCE_INPUT_ACTUALLY_USED_IN_SMOKE=NO
```

Stock providers receive search terms only. LoomLoom receives the three fields
listed above and has no character/location reference input. No claim of
reference consistency is made.

## Live execution and artifacts

No live `ProductionJob`, `ProductionExecution`, provider run, or
`ProductionArtifact` was created. There is therefore no provider state,
artifact file, probe result, SHA-256, or QC result to report. Existing local
fixtures and FFmpeg outputs are not counted as provider evidence.

The separate LLM live smoke was also not run: the effective selected provider
(`moonshot`) has no configured key/model override in `config.toml`, and the
Task011 audit does not inject unrelated process credentials into the app
configuration.

```text
LIVE_EXECUTION_CREATED=NO
LIVE_EXECUTION_FINAL_STATE=NOT_RUN
LIVE_ARTIFACT_CREATED=NO
LIVE_ARTIFACT_PROBE=NOT_APPLICABLE
LIVE_PROVIDER_QC_RESULT=NOT_APPLICABLE
LIVE_LLM_SMOKE=NOT_RUN
LIVE_ONE_SHOT_FINAL_ASSEMBLY=NOT_RUN
```

## Smallest future seam

The next provider-specific phase should add one concrete
`ProductionRuntimeAdapter` implementation that maps an immutable
`ProductionInputSnapshot` (including shot identity and reference-version
IDs) to a documented provider request, keeps provider status/events durable,
downloads the provider artifact through project-isolated storage, and
preserves the existing worker/QC boundary. It must be explicitly authorized
before implementation and must not alter MPT core behavior.
