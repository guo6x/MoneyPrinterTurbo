# First real 60-second AIDrama Studio E2E · pass/fail checklist

Fixture: `AIDRAMA_V1_60S_REAL_E2E_ACCEPTANCE_FIXTURE`
Title: `雨夜来信`
Target: `60.000 s · 16:9 · native 720p · delivery 1080p · 24 fps`

Record `PASS`, `FAIL`, or `N/A` beside every item. A `FAIL` on a blocking item
fails the run even if the encoded file opens. `N/A` is allowed only for a
non-blocking optional style reference; never use it for character/location
references, timing, subtitle, audio, QC, or human review.

## 1. Fixture and source lock (blocking)

- [ ] PASS/FAIL — `acceptance_manifest.json` identity and title match every canonical file.
- [ ] PASS/FAIL — Story Bible is approved and unchanged from `story_bible.json`.
- [ ] PASS/FAIL — Structured Script is approved and validates against the Story Bible.
- [ ] PASS/FAIL — Exactly 2 characters and 2 locations are present; no extras are introduced.
- [ ] PASS/FAIL — Four required reference roles are locked to one current local version each.
- [ ] PASS/FAIL — Reference images meet PNG/JPEG/WebP, at least 1024x576, 16:9 tolerance, and no watermark/logo.
- [ ] PASS/FAIL — Reference SHA-256 values are recorded in the paid-run provenance record.

## 2. Shot generation (blocking)

- [ ] PASS/FAIL — Exactly 12 shots exist, ordered `shot_01` through `shot_12`.
- [ ] PASS/FAIL — Shot durations are `5,5,4,6,5,6,5,6,4,5,5,4` seconds and sum to `60.000`.
- [ ] PASS/FAIL — Each shot uses only its listed scene, character, props, and matching reference roles.
- [ ] PASS/FAIL — Native output from each shot is 1280x720, 16:9, 24 fps.
- [ ] PASS/FAIL — No generated shot contains an extra person, new location, logo, readable private address, weapon, or fire imagery.
- [ ] PASS/FAIL — Rain direction, wetness, wardrobe, prop geometry, and screen direction pass the per-shot continuity requirements.
- [ ] PASS/FAIL — Generation brief and request hash are retained for every shot; no secret-bearing parameter is persisted.

## 3. Deterministic and Vision QC (blocking)

- [ ] PASS/FAIL — Every shot duration is within ±0.04 seconds of its creative duration.
- [ ] PASS/FAIL — No shot has a black/frozen frame longer than 0.04 seconds or a dropped-frame cadence.
- [ ] PASS/FAIL — Deterministic QC confirms no audio clipping and no missing video/audio stream.
- [ ] PASS/FAIL — Vision QC confirms 林夏 and 林父 identity anchors in every appearance.
- [ ] PASS/FAIL — Vision QC confirms street/house geography, lighting palette, and rain continuity.
- [ ] PASS/FAIL — Vision QC confirms the reveal reads as “father protects witness,” never “father committed crime.”
- [ ] PASS/FAIL — Human review accepts each shot’s listed acceptance criteria; any rejected shot is regenerated before assembly.

## 4. Dialogue, TTS, and subtitles (blocking)

- [ ] PASS/FAIL — Exactly 7 TTS cues and 7 subtitle cues exist, with identical text and order.
- [ ] PASS/FAIL — Every cue is inside its shot, monotonic, non-overlapping, and within 0.000-60.000 seconds.
- [ ] PASS/FAIL — TTS voice mapping preserves 林夏’s young restrained reporter tone and 林父’s restrained 54-year-old tone.
- [ ] PASS/FAIL — Mandarin pronunciation, mouth sync, and punctuation pauses pass human listening review.
- [ ] PASS/FAIL — Subtitle glyphs are legible on 16:9 safe margins; no extra or invented Chinese characters appear.
- [ ] PASS/FAIL — Rain bed is continuous; dialogue ducking is approximately -10 dB and tail is at least 0.7 seconds.

## 5. Final assembly and delivery (blocking)

- [ ] PASS/FAIL — Assembly order is exactly `shot_01`…`shot_12`; no implicit sort or retry candidate is used.
- [ ] PASS/FAIL — Timeline is contiguous 0.000-60.000 seconds with no gap, overlap, or transition duration drift.
- [ ] PASS/FAIL — Audio order is rain bed, restrained music, TTS cues, then rain tail.
- [ ] PASS/FAIL — Delivery is 1920x1080, 16:9, 24 fps, H.264 yuv420p, MP4 fast-start.
- [ ] PASS/FAIL — Delivery audio is AAC, 48 kHz stereo, 192 kbps, -16 LUFS target, true peak no higher than -1 dBTP.
- [ ] PASS/FAIL — Final file duration is 60.000 seconds ±0.04 seconds and opens in a clean player.
- [ ] PASS/FAIL — No watermark, debug overlay, provider UI, temporary path, or test slate is present.

## 6. Run accounting and sign-off (blocking)

- [ ] PASS/FAIL — Fixture validation ran with `LIVE_CALLS=0` and `PAID_CALLS=0`.
- [ ] PASS/FAIL — No external API key or network access was required for fixture validation.
- [ ] PASS/FAIL — Paid-run provider/model/region, request hashes, artifact hashes, QC report, and reviewer are recorded.
- [ ] PASS/FAIL — A human reviewer watched the full 60 seconds with audio and accepted the story reveal.
- [ ] PASS/FAIL — Final output filename and SHA-256 are recorded alongside the assembly manifest.

**Run decision:** `PASS` only when every blocking item is `PASS`.
**Reviewer:** ____________________  **UTC timestamp:** ____________________
**Final artifact SHA-256:** ______________________________________________
