# AIDrama Quick Task010B — Real Final Assembly Runtime

Task010B adds the first real, deterministic final render. A
`FinalAssemblyRuntimeService` loads the persisted READY manifest once and
passes only its immutable `FinalAssemblyItem` rows to the
`MPTFinalAssemblyAdapter`. The adapter reuses the existing narrow
`app.services.video.concat_video_clips_with_ffmpeg` seam; it does not invoke
the MPT task pipeline or query production state.

## Runtime contract

- Sources are resolved as project-relative paths below
  `storage/aidrama/projects/<project_id>/` and are checked for traversal,
  regular-file status, non-zero size, and supported video suffix.
- Items are rendered by `order_index`; filenames, timestamps, and latest
  production retries are never consulted.
- The render writes a temporary MP4, probes it with the existing FFmpeg
  runtime, verifies a video stream, non-zero size, positive duration, and
  duration against the frozen source sum, computes SHA-256, then atomically
  renames it into project-isolated storage.
- The first successful output uses
  `final/<assembly_id>/episode.mp4`. A later retry never overwrites it and is
  written under `final/<assembly_id>/attempts/<attempt_id>/episode.mp4`.

## Attempt history

Migration 011 adds `final_assembly_render_attempts`. Each retry receives a new
attempt number and preserves status, adapter, output-relative path, output
metadata, SHA-256, source provenance, and sanitized failure information.
Manifest item rows are never changed by rendering. Aggregate assembly status
may progress through `ASSEMBLING`, `SUCCEEDED`, or `FAILED` while the frozen
items remain unchanged. The synchronous media seam does not support safe
cancellation, so cancellation is intentionally not faked.

## Validation

The focused suite includes a real local three-shot MP4 smoke using existing
FFmpeg/imageio-ffmpeg runtime binaries, plus DRAFT blocking, missing-source
failure history, output immutability, metadata/hash persistence, migration
idempotency, and project/path-boundary coverage.

Task010C UI and general post-production (subtitles, TTS, BGM, transitions,
titles, watermarks, AI editing/QC, and desktop) are outside this phase.
