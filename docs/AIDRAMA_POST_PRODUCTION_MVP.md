# AIDrama Studio post-production MVP

The post-production layer consumes a successful, immutable `FinalAssembly`
output.  It does not modify the assembly manifest or discover newer
production artifacts while rendering.

## Boundaries

- `PostProductionPlan` records the source assembly, subtitle preference, and
  audio mix gains.
- `SubtitleTrack` is derived from approved or draft Structured Script beats
  (`DIALOGUE`, `NARRATION`, and `INNER_MONOLOGUE`) and carries scene/beat
  provenance.  `PostProductionService.subtitle_to_srt` exports deterministic
  SRT timestamps.
- `VoiceTrack` is metadata/provider-ready.  No fake TTS audio is generated;
  an existing project-relative audio file may be supplied later.
- `MusicTrack` references a validated project-relative audio file.  Local BGM
  import copies the source into `storage/aidrama/projects/<project>/post/<plan>/audio/`
  with a generated filename.
- `PostRenderAttempt` is append-only history.  Retries receive a new attempt
  id and output directory; successful outputs are never overwritten.

`FFmpegPostProductionAdapter` is the only renderer seam.  It uses the existing
MPT FFmpeg resolver and can mix source/voice/music tracks or burn in SRT.  A
deterministic adapter can be injected in tests.  SQLite migration 012 stores
plans, tracks, and attempts; every foreign-key edge includes project identity.

## Runtime gates

Post rendering requires an existing successful FinalAssembly MP4.  External
TTS and AI music providers are not required for the MVP and are not claimed as
live capabilities.  Provider failures are persisted on the corresponding
attempt while the immutable source assembly remains inspectable.
