# AIDrama Quick Task011B — Wan image-to-video provider adapter

Task011B adds the first concrete generative-video provider behind the frozen
`ProductionRuntimeAdapter` contract.  It supports one approved AIDrama
`ProductionShot` at a time and leaves the existing MoneyPrinterTurbo stock
media runtime untouched.

## Provider and API mode

- Provider: Alibaba Cloud Model Studio / DashScope
- Mode: `IMAGE_TO_VIDEO` (Wan 2.7 first-frame-to-video)
- Verified model: `wan2.7-i2v-2026-04-25`
- HTTP create endpoint: `/api/v1/services/aigc/video-generation/video-synthesis`
- HTTP polling endpoint: `/api/v1/tasks/{task_id}`
- API reference: [Wan 2.7 image-to-video API](https://www.alibabacloud.com/help/en/model-studio/image-to-video-general-api-reference)

The model, endpoint, and API key must be from the same Model Studio region.
The configurable base URL defaults to the existing DashScope `/api/v1` host;
workspace-specific Beijing or Singapore domains can be supplied with
`DASHSCOPE_BASE_URL` when required by the account.

The implementation uses the already available `requests` HTTP client.  The
installed DashScope SDK was audited but is not used because the generic HTTP
boundary makes the request/response contract explicit without adding a
dependency.

## Request mapping

`WanInputMapper` maps one immutable `ProductionInputSnapshot` to:

```json
{
  "model": "wan2.7-i2v-2026-04-25",
  "input": {
    "prompt": "<deterministic structured-shot prompt>",
    "media": [{"type": "first_frame", "url": "data:image/jpeg;base64,..."}]
  },
  "parameters": {"duration": 5, "resolution": "720P"}
}
```

The `media` shape and `first_frame` type are the current Wan 2.7 protocol.
Base64 data-URI input is explicitly supported by the API, so the adapter does
not expose a project-local filesystem path or upload a reference to a public
URL.  Wan 2.7 accepts `720P` and `1080P` and an integer duration from 2 to 15
seconds; the adapter defaults to 5 seconds at 720P for the lowest practical
smoke cost.

The prompt is deterministic and is assembled from canonical shot fields:
visual intent, subject, action, framing, camera angle/movement, lens,
movement notes, expression, dialogue/narration, and lighting.  The adapter is
not an LLM and does not invent scene facts.

## Exact reference rule

The resolver reads only `ProductionInputSnapshot.reference_asset_versions`.
It never queries “latest” during execution.  For a one-shot image-to-video
request it chooses exactly one image:

1. the first frozen `CHARACTER:<id>` reference matching the shot subject;
2. otherwise the deterministic first frozen `LOCATION:<id>` reference;
3. otherwise validation fails.

The selected `ReferenceAssetVersion` must belong to the snapshot project, be
the asset's current (locked) version, carry the same source Story revision as
the snapshot, resolve under the project storage root, exist on disk, and have
a valid JPEG, PNG, or WebP signature matching its MIME type.  Character and
location references are not composited, and unused snapshot references are not
claimed as conditioning inputs.  The selected version ID and binding key are
persisted in provider metadata.

## Async lifecycle, timeout, and cancellation

`WanVideoClient.create_task()` sends `X-DashScope-Async: enable` and stores the
provider `task_id` as the AIDrama runtime reference.  `get_task()` maps
`PENDING` to `QUEUED`, `RUNNING` to `RUNNING`, `SUCCEEDED` to `SUCCEEDED`, and
`FAILED`/`UNKNOWN` to `FAILED`; `CANCELED` maps to AIDrama `CANCELLED`.

The existing `ProductionWorker` supplies bounded polling (`max_polls`) and an
explicit `poll_interval`.  For Wan's documented one-to-five-minute jobs, a
live invocation should construct the worker with a finite interval (for
example 15 seconds) and a finite poll budget (for example 40 polls).  A
budget exhaustion is recorded as a truthful AIDrama failure while preserving
the provider task reference.  Unit tests never call the live API.

Wan's current video-synthesis API does not expose a safe cancellation request
for this adapter.  `cancel()` therefore raises an explicit unsupported error;
the execution is not falsely marked cancelled by the provider boundary.

## Artifact download and persistence

After a `SUCCEEDED` task, the client downloads the temporary `video_url`
immediately.  The adapter rejects an empty result and verifies an MP4/MOV
`ftyp` container marker before returning bytes.  It records SHA-256, size,
MIME, provider task ID, shot ID, model, prompt, resolution, duration, and the
exact selected reference version as non-secret metadata.

`ProductionWorker` then hands the bytes to the existing
`ProductionArtifactStorageService`, which writes a unique file below:

`storage/aidrama/projects/<project_id>/production/<execution_id>/`

Only the project-relative path is persisted in SQLite.  Existing deterministic
`ProductionQCService` remains responsible for duration, codec, stream, black
frame, and audio checks; Task011B adds no AI QC and no new storage or migration.

The provider task and request metadata are also retained in the immutable
`STARTED` execution event.  Secret-bearing keys are filtered at that event
boundary, and `DASHSCOPE_API_KEY` is read only from the process environment;
it is never written to snapshots, events, artifacts, logs, or this document.

## Live smoke and cost control

The current environment has no `DASHSCOPE_API_KEY`, so no paid request was
made:

```text
LIVE_PROVIDER_CREDENTIAL_READY=NO
LIVE_WAN_SMOKE=NOT_RUN
LIVE_E2E_GATE=BLOCKED_NO_CREDENTIAL
```

When a credential is deliberately configured, the permitted smoke is exactly
one simple shot, one provider request, the shortest practical duration, and
720P.  There is no automatic retry, fallback, router, or full-episode live
generation.

## Known limitations

- Only one first-frame image and one shot are supported in this phase.
- Character + location compositing, style conditioning, audio driving, and
  video continuation are future work.
- Provider result URLs and task IDs are retained by Wan for approximately 24
  hours; the adapter downloads the result immediately to permanent storage.
- Provider cancellation is explicitly unsupported.
- Image dimensions/aspect ratio are left to provider validation; the adapter
  enforces project isolation, size, signature, and MIME safety before submit.
