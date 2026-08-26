# AIDrama Studio V1 — Live Provider Readiness Remediation

Audit/implementation record: 2026-08-26 (Asia/Shanghai)

BASE_HEAD=7e0aa75d4faf9f24b5a4df185b99280360bf8196
BRANCH=feat/aidrama-studio-v1-live-provider-readiness-remediation

This checkpoint closes only the provider-readiness defects identified by the
V1 provider-readiness audit.  It does not make a live request, add a provider,
change the provider architecture, alter database schema, or change desktop
build/release files.  Credentials are represented only by safe alias and
presence/status metadata.

## Acceptance results

The fields below are the machine-readable acceptance summary requested by the
remediation brief.  “PASS” means that the bounded, offline-tested product
path and its safety gate exist; it does not claim that a credential, quota, or
provider account was live-verified in this checkpoint.

```text
GPT_IMAGE_REQUEST_CONTRACT=PASS
IMAGE_CANDIDATE_RECORDING=PASS
IMAGE_HUMAN_PROMOTION_REQUIRED=PASS
IMAGE_AUTO_LOCK=NO

SEEDANCE_DURATION_PROFILE=PASS
SEEDANCE_EXPLICIT_SELECTION=PASS

WAN_PAID_CREATE_GATE=PASS
WAN_UNAUTHORIZED_CREATE_CALLS=0
WAN_RECONCILIATION_WITHOUT_RESUBMIT=PASS

AZURE_TTS_CREDENTIAL_READINESS=PASS
AZURE_TTS_REGION_READINESS=PASS
AZURE_TTS_METADATA=PASS
TTS_SINGLE_PAID_SMOKE_MODE=PASS

LLM_SINGLE_CALL_SMOKE_PATH=PASS
VISION_SINGLE_INTERACTION_SMOKE_PATH=PASS

CAPABILITY_READINESS_UI=PASS
FALSE_READY_STATES=0

OFFLINE_LIVE_PREFLIGHT=PASS

LLM_READINESS=PASS
IMAGE_READINESS=PASS
VIDEO_READINESS=PASS
VISION_READINESS=PASS
TTS_READINESS=PASS

LIVE_SMOKE_READY_CAPABILITIES=NONE (no live credentials/authorization exercised)

AIDRAMA_TESTS=440 passed, 11 warnings
FULL_PROJECT_TESTS=1073 passed, 11 skipped, 14 warnings, 4406 subtests
NEW_REGRESSIONS=0 (no failures; targeted remediation run passed)
SECRET_SCAN=PASS (no secret-pattern matches)

LIVE_CALLS=0
PAID_CALLS=0

FINAL_HEAD=HEAD (verified after commit; exact SHA reported with this artifact)
WORKTREE=DIRTY (two unrelated untracked audit documents preserved)
```

## What changed

### GPT Image request contract

`OpenAIImageProvider` now sends exactly `model`, `prompt`, and `n: 1` to the
GPT Image generation endpoint.  The DALL-E-only `response_format` parameter is
omitted.  The API-key field is excluded from configuration `repr`, and an
explicitly injected empty environment cannot fall back to an ambient process
secret.  Tests capture the outgoing JSON body and assert the absence of
`response_format`.

### Image candidate lifecycle

The reference workspace now uses the canonical runtime boundary:

```text
generate → physical image validation → durable DRAFT candidate
         → human Promote → Draft version → human Lock
```

Candidate bytes, SHA-256, prompt/request provenance, provider disclosure,
source-story revision, and parent/regeneration identity remain immutable.  A
generated candidate is never promoted or locked automatically.  The UI offers
explicit Promote and Reject actions and makes the separate Lock step visible.

### Seedance selection and duration

The explicit Seedance profile is pinned to the official integer duration set
4–30 seconds and carries an explicit-selection marker.  Runtime-plan and
offline-preflight checks validate both the persisted profile and runtime
metadata, so a tampered profile cannot inherit the generic 2–15-second video
fallback.  Registry order and availability alone cannot select Seedance.

### Wan paid-create boundary

Wan task creation requires both the credential and the canonical
`AIDRAMA_ALLOW_PAID_LIVE_TESTS=1` authorization.  Polling an existing task uses
the read-only `GET` reconciliation path and remains available when create
authorization is absent; it never calls `POST` or silently submits a second
task.  Tests assert zero unauthorized create calls and preserve injected poll
transport identity.

### Azure TTS readiness and bounded smoke

Azure voices are reported ready only when the selected voice, speech key,
speech region, and local runtime seam are all present.  Their metadata now
identifies Azure Speech as a remote provider with an international or mainland
deployment class and region, rather than mislabelling it `LOCAL`.  The
acceptance-only TTS smoke path requires explicit authorization and delegates to
a provider method that disables internal retry; the canonical runtime accepts
at most one synthesis submission.

### LLM and Vision smoke paths

`LLMInvocationGateway.run_live_smoke` freezes the selected provider/profile and
sends the exact prompt `Reply with exactly: OK` once, without entering the
creative five-attempt/repair loop.  Invalid output is terminal for that smoke.

Gemini Vision retains its existing provider architecture and bounded direct
path: one persisted MP4 is hash-verified, media uploads/status polling are
bounded, one Interaction is submitted, the response is validated against the
exact seven-metric schema, and uploaded remote files are deleted in `finally`
(with only a bounded 48-hour expiry fallback recorded if deletion fails).
There is no automatic interaction retry.

### Settings readiness and offline preflight

Settings and the shared readiness surface expose only the five normal-user
capabilities—`文本生成`, `参考图生成`, `视频生成`, `画面分析`, and `配音`—with
`已配置`, `需要配置`, or `配置有误`.  Status is derived from the selected
runtime profile and provider metadata, not adapter importability; contradictory
or malformed boolean/constraint metadata fails closed.  API-key values and
secret-derived characteristics are never rendered.

`OfflineLivePreflightService` evaluates exact provider, model, endpoint
identity, endpoint host/path, deployment/provider region, credential
name/presence, paid authorization, and provider-specific constraints without
calling a provider or making network I/O.  Missing credentials are reported as
an alias plus `MISSING`/`NOT_CHECKED` status only—never value, length, prefix,
suffix, or hash.  It emits the five required fields:

```text
LLM_PROFILE_READY=
IMAGE_PROFILE_READY=
VIDEO_PROFILE_READY=
VISION_PROFILE_READY=
TTS_PROFILE_READY=
```

## Verification evidence

Focused provider, selection, candidate-lifecycle, Settings, preflight, LLM,
Vision, Wan, Seedance, and TTS tests passed in the latest targeted run (`38
passed`).  The final full runs collected 440 AIDrama tests and 1,084
repository tests; all collected tests passed (with 11 repository skips).
Changed-file Ruff, Python compilation, `git diff --check`, and a secret-pattern
scan also passed.  No provider endpoint was contacted, and no paid task,
upload, synthesis, or interaction was submitted.

Relevant implementation boundaries:

- `aidrama_studio/services/providers/openai_image.py`
- `aidrama_studio/services/image_runtime.py`
- `aidrama_studio/services/reference_assets.py`
- `aidrama_studio/pages/assets.py`
- `aidrama_studio/services/adapters/seedance_video.py`
- `aidrama_studio/services/provider_profiles.py`
- `aidrama_studio/services/adapters/wan_video.py`
- `aidrama_studio/services/ai_capabilities.py`
- `aidrama_studio/services/tts_runtime.py`
- `aidrama_studio/services/llm_runtime.py`
- `aidrama_studio/services/providers/gemini_vision.py`
- `aidrama_studio/services/provider_readiness.py`
- `aidrama_studio/services/provider_preflight.py`
- `aidrama_studio/pages/_shared.py` and `aidrama_studio/pages/settings.py`

## Scope and next gate

This is an offline readiness checkpoint.  The subsequent bounded live-smoke
phase must be separately authorized, use one request per capability, and stop
on the first failure.  It must not infer readiness from this report or bypass
the runtime gates.  The dedicated remediation branch must be pushed without
merging `main`.
