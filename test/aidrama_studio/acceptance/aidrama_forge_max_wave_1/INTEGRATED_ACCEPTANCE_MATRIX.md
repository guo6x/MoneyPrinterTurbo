# AIDrama Forge MAX Wave 1 — Integrated Full-AI Acceptance Matrix

## Purpose and decision

This specification decides whether Wave 1 is one coherent, offline-operable product rather than a collection of independently passing features. It is an acceptance specification only: it neither merges feature branches nor changes production behavior.

```text
PHASE=AIDRAMA_FORGE_MAX_WAVE_1_INTEGRATED_FULL_AI_E2E_TEST_MATRIX
REFERENCE_BASE=557b9af74a9ccb3e5f02445d9bb1fccd71a021f2
INTEGRATION_CANDIDATE=integration/aidrama-forge-max-wave-1
PRODUCT_CODE_MODIFIED=NO
LIVE_PROVIDER_CALLS=0
```

The runner and fixture live only under this directory. `matrix.json` is the machine-readable suite manifest; `canonical_project.json` is the only allowed golden input. A result is valid only when every listed test has used a fresh explicit `DatabasePaths` rooted below that run's temporary directory.

`PASS` for this matrix means the unified integration candidate has passed the stated offline acceptance tests. It does not prove a live provider, paid provider, or a release has passed.

## Wave 1 source heads read for this matrix

| Capability | Ref and exact head | Integration contract exercised |
| --- | --- | --- |
| Universal runtime core | `feat/aidrama-universal-model-runtime-core-v1` `20239cbcbf78dc441f4e86533d58c32b527857f9` | immutable manifest, resolver, readiness, async create identity/poll/reconcile |
| Mainland runtime | `feat/aidrama-universal-runtime-mainland-providers-v1` `e9c895c3b618a440425e929985b3e494fc4f55e6` | capability-specific mainland manifest/codec selection |
| International runtime | `feat/aidrama-universal-runtime-international-providers-v1` `a30fd64d92d5883de8eb52e383e9a41076ab0271` | international manifest/bridge is reached only through universal runtime |
| Creative AI | `feat/aidrama-studio-v1-full-ai-creative-pipeline` `8c9a0f854af873f3a37e73ae83261397759e7e86` | `CreativePipelineService.execute`, durable operations, approved upstream chain |
| Settings | `feat/aidrama-studio-v1-settings-universal-runtime-wiring` `9c5d1f0843ecc609d83757636675499c5f865fd1` | `SettingsRuntimeProjectionService.save_scheme`, frozen versus future resolution |
| Settings UI precursor | `feat/aidrama-studio-v1-universal-multi-provider-settings-ui` `c0bebb222475f442f6600ac77802b6ce9432e2e9` | all five selector surfaces and provider-profile projection remain connected to the selected universal identity |
| Reference Agent | `feat/aidrama-studio-v1-autonomous-reference-agent` `7bb10cfc01b28e169dce9e88f44b69b898a34fac` | discover, reuse/dedupe, candidate, human bind, human lock |
| AUTO | `feat/aidrama-studio-v1-auto-mode-orchestrator` `25e23855222e43589c1654b9408eb73187014751` | `next_action`, `step`, `run_until_boundary`, `resume` read persisted truth |
| Reliability/cost guard | `feat/aidrama-studio-v1-production-reliability-cost-guard` `adf2c9c661aaa85c92eeb1ad1bc67f469521c3db` | one durable create intent/task/artifact; recovery and paid ledger |
| Vision QC | `feat/aidrama-studio-v1-vision-qc-universal-runtime` `3f3595dbe2c398e3d17ec3e6296caccacf0780a2` | real frame sampling, universal vision request, persisted advisory analysis |
| Continuity | `feat/aidrama-studio-v1-continuity-engine` `9bf2bbd498f50a1e2eec4a0c49bbe243bf1643f2` | source precedence, persisted issue and recommendation-only repair |
| Human/final governance | `fix/aidrama-studio-v1-human-review-final-governance` `ad77d139158a66bcfd9c2ec2711af81f8925aa6c` | review bound to exact candidate; final source eligibility |
| Audio/subtitle/post | `feat/aidrama-studio-v1-audio-subtitle-postproduction` `301368fb2197bb44795d0eb4b7ce6a4ad05376b0` | real WAV, SRT, mux and delivery playback |
| Director Workspace | `feat/aidrama-studio-v1-director-workspace` `c3ef941eca88bffc535a69d00caf45cc37e16ebb` | `DirectorWorkspaceProjectionService.project` reads formal projections only |
| Creative Workspace UI | `feat/aidrama-studio-v1-creative-workspace-ux-final` `9dd45957e509c0bec48c7be667c2e336d4c91321` | page-level journey consumes the same creative, reference, production, review and post-production truth |
| Aggregate completion fix | `fix/aidrama-production-job-aggregate-completion` `4980b1a3e4a7b5c0c7d7bf179fcc3b8ad6287a40` | job remains non-terminal until every shot has a terminal outcome |
| Readiness baseline | `feat/aidrama-studio-v1-live-provider-readiness-remediation` `c45ca487551ee93872cd7ee51861b4e7a8abefc8` | configured, verified, runtime-available and explicit paid-create authorization remain independent, never implied |

The integration candidate is allowed to refactor these interfaces, but only when the same externally observable contract is preserved and the tests remain green. A branch name, a service health check, or a source import is never evidence of integration.

## Test boundaries

| Layer | What is allowed to prove | What it must not claim |
| --- | --- | --- |
| Unit | immutable objects, state reducers, validation, redaction, resolver and migration functions | service wiring or media playback |
| Service | real repository plus one service with fake provider boundary | cross-feature product flow |
| Integration | persisted output from one feature is consumed by the next feature's formal service/projection | UI/browser playback or a real provider |
| Product E2E | canonical project traverses the complete offline product chain and cold reload | live-provider verification |
| Real-media E2E | local FFmpeg produces/probes MP4, WAV and muxed delivery media | visual aesthetic correctness or remote media creation |
| Live provider | separately collected evidence only | fake transport success, configured credentials, or offline readiness |

Every offline test installs all of the following before creating the repository:

1. a per-test `AIDRAMA_DATA_DIR`, `DatabasePaths`, projects root and archive root under `tmp_path`;
2. `AIDRAMA_SQLITE_WAL=0` for deterministic Windows cleanup;
3. a socket/DNS/connect guard that raises on the first network attempt;
4. fake LLM, image, video, Vision and TTS transports whose call logs are asserted; and
5. fake `LOCALAPPDATA` that must remain without `AIDramaStudio/aidrama.db` after every test.

An attempt to construct or open the default `%LOCALAPPDATA%\AIDramaStudio` database is `FAIL`, even if the test would otherwise succeed. The test fixture must also cold-reload the repository from the explicit temporary database before claiming persistence.

## Canonical golden project

`canonical_project.json` defines **雨夜来信：归还的红伞**:

- exactly 60 seconds, 6 ordered 10-second shots, 16:9, native 1280×720/24 H.264 and delivery 1920×1080/24 H.264/AAC MP4;
- Character A `林澈` in a black coat with a red umbrella; Character B `苏岚` in a blue scarf carrying a sealed letter;
- Location A is a rainy exterior of the old bookshop; Location B is its warm interior;
- every shot has dialogue; shots 1–6 form one causally ordered story;
- the existing locked references are Character A and Location A; Character B and Location B are missing;
- Shot 3's fake Vision observation deliberately reports white shirt plus no umbrella. The expected truth remains black coat plus red umbrella, which must yield both `WARDROBE_DRIFT` and `PROP_DRIFT`.

The fixture factory must create story, script and shot-plan revisions through product services, rather than insert an approved downstream revision directly. It captures the generated IDs at runtime and asserts the exact chain:

```text
approved creative intake ID
  -> Story.source/intake revision ID
  -> Script.story revision ID
  -> ShotPlan.script revision ID
  -> Reference requirement provenance
  -> frozen production runtime plan / GenerationBrief
  -> candidate artifact SHA + review ID
  -> frozen final manifest source SHA
  -> audio timeline / subtitle track / post-render attempt
```

For every AI operation, persist and assert its provider, model, capability, manifest hash or frozen identity, input revision(s), output revision, and operation ID. The fake LLM must be reached through the universal LLM runtime; monkeypatching a legacy direct provider entry point to raise is mandatory. The same principle applies to image, video, Vision and TTS.

## Required test implementation layout

The files named below are deliberately required by `matrix.json`; the runner fails closed if one is absent. A future test implementation may split tests further but may not weaken or omit a named behavior.

| File | Required responsibility |
| --- | --- |
| `conftest.py` | temporary database fixture, default-DB sentinel, socket/provider hard-stop, fixture loader, cold reload, recursive secret scanner and FFmpeg locator |
| `test_00_environment_and_db_isolation.py` | isolation guard, no-network guard, fake transport counters, canonical-fixture structural validation |
| `test_10_creative_and_settings.py` | creative revisions/provenance/direct-bypass, invalid output, model selection/freeze/redaction |
| `test_20_reference_and_auto.py` | reference agent lifecycle; full AUTO state matrix; resume/idempotency |
| `test_30_production_and_reliability.py` | 6-shot async fake video flow; duplicate/create/restart/reconciliation/final interruption matrix |
| `test_40_technical_vision_continuity.py` | real MP4 QC, invalid media, frame manifest, Vision persistence/projection, continuity drift |
| `test_50_human_final_audio_post.py` | exact human governance, real WAV/SRT/mux, final/delivery media validation |
| `test_60_director_migration_security.py` | Director formal projections, fresh/legacy/future database migration, canary redaction |
| `test_70_full_product_e2e.py` | one complete canonical chain including cold reload and Director delivery playback probe |
| `test_71_negative_e2e.py` | eight named fail-closed/resume paths |

## Stage matrix

All 26 stages are blocking. A test may cover more than one stage, but the report must preserve every stage identifier and its first failure.

| ID | Layer | Gate and required assertion |
| --- | --- | --- |
| `W1-01` | Unit | fixture has exactly 6 unique ordered shots, two characters, two locations, two props, dialogue, 60.0 seconds and the deliberate Shot 3 drift |
| `W1-02` | Service | every test gets a new explicit temporary DB; default LocalAppData database, network, and live provider transports have zero opens/calls |
| `W1-03` | Integration | creative intake → fake universal LLM Story → human approve → Script → human approve → Shot Plan → human approve; each exact upstream revision ID is persisted |
| `W1-04` | Unit/Service | malformed Story/Script/Shot output fails after bounded repair, preserves previous approved revision, and legacy direct LLM provider raises before any call |
| `W1-05` | Integration | save LLM/IMAGE/VIDEO/VISION/TTS selection; future resolution uses the new frozen identity while old RuntimePlan/GenerationBrief remains byte-for-byte unchanged; secret canaries are absent from settings projections |
| `W1-06` | Integration | Reference Agent derives four requirements from approved shot plan, reuses two locked refs, dedupes duplicate requirement, generates candidates only for two missing refs, then requires human promote/bind/lock before `Production Reference Ready` |
| `W1-07` | Service | AUTO reads persisted project state and returns the exact boundary for: empty, creative-ready, Story approval, Script approval, Shot Plan approval, references missing, reference human gate, production ready, provider pending, QC pending, review pending, final ready and delivery-final ready |
| `W1-08` | Integration | repeated AUTO `step`, `resume` after cold reload and an already-recorded action produce one semantic action/event and never create another paid/video intent |
| `W1-09` | Integration | fake async video produces one create intent, one durable task ID, one artifact, QC result, review candidate and explicit source decision for each of six shots; shot order remains 1…6 |
| `W1-10` | Service | double click, double worker step, restart after submit, restart after task ID, poll timeout, download failure and artifact reconciliation all reuse the original task and have `duplicate_create=0` |
| `W1-11` | Service | interrupt final assembly after its frozen source manifest is persisted; resume reuses exactly the six frozen source SHAs and performs no new video create |
| `W1-12` | Real-media | locally FFmpeg-generated valid MP4 has video stream, 10-second duration, 1280×720, H.264, MP4 container and nonzero bytes; truncated/wrong-codec/no-video media fails Technical QC |
| `W1-13` | Integration | real frame extraction creates a persisted ordered manifest tied to artifact SHA; fake universal Vision receives locked refs and GenerationBrief; advisory analysis and failure are persisted and Review projection shows them |
| `W1-14` | Integration | Vision `PASS` neither mutates a Human decision nor grants final eligibility; Vision failures are sanitized and fail closed only where Vision is required by policy |
| `W1-15` | Integration | fake Vision observation for Shot 3 detects `WARDROBE_DRIFT` and `PROP_DRIFT`; expected human/locked truth wins; recommendations are persisted but no repair/create transport is called |
| `W1-16` | Service | QC pass with no review, a latest rejection, an artifact/review identity mismatch, or a severe unresolved continuity policy blocks final readiness; only exact artifact plus latest human `APPROVED` is eligible |
| `W1-17` | Real-media | after all six sources are eligible, final manifest keeps deterministic 1…6 order, concat is a playable valid MP4 and target duration is 60 seconds within the declared probe tolerance; one unapproved source blocks it |
| `W1-18` | Service/Real-media | fake TTS emits parseable 48kHz stereo WAV for every dialogue task, each task has frozen TTS provenance, and the persisted audio timeline covers its dialogue without silent truncation |
| `W1-19` | Real-media | audio timeline produces SRT with exact cue/timeline correspondence; subtitle plus WAV mux yields Delivery Final with a video stream, an audio stream, successful FFmpeg decode/probe and project-relative source provenance |
| `W1-20` | Integration | Director Workspace reads actual project/service projections for Shot Grid, Timeline, Script/Shot selection, previews, locked Reference Board, Technical QC, Vision, Continuity, candidates, final source and Delivery Final; no private UI state is used as truth |
| `W1-21` | Migration | a fresh explicit DB applies all migrations once, then zero more; versions are unique/increasing and all five Wave 1 migration payloads exist (creative operations, AUTO state/events, paid-create/artifact ledger, continuity truth/repair, audiovisual delivery) |
| `W1-22` | Migration | a fixture frozen at reference-base schema 033 upgrades to latest without modifying the fixture; approved revision chain, reference bindings, final manifest and source selection survive; rerun is idempotent |
| `W1-23` | Migration | a database recording a schema version above the integrated supported maximum raises `UnsupportedDatabaseSchemaError` before data/schema mutation |
| `W1-24` | Security | recursively scan public UI projections, activity/event records, persistent diagnostics, artifact metadata, operation requests and exported report for every fixture canary: API key, Bearer, Authorization, signed/temporary URL and absolute private path; none may appear outside explicitly named controlled internal credential fields |
| `W1-25` | Product E2E | execute the canonical chain in the stated product order, cold reload, probe final delivery and open the Director projection; `FULL_AI_OFFLINE_PRODUCT_E2E=PASS` only if W1-01…W1-24 pass in the same run |
| `W1-26` | Product E2E | execute the eight negative paths below, assert exact fail-closed boundary and a safe resume path, and make no fake paid create for blocked states |

### Creative and settings details

The creative test must verify operation records use the approved upstream IDs rather than merely comparing output text. Change all five model selections after one plan is frozen. The new LLM selection must serve a new Story operation; new IMAGE/VIDEO/VISION/TTS selections must serve only new corresponding plans/tasks. The old plans must retain their original provider/model/manifest identity/hash. The public Settings, AUTO and Director projections must expose readiness but never raw credentials.

### Reference, AUTO, production and reliability details

The Reference Agent must demonstrate all five states: existing locked reference reuse, requirement dedupe, missing reference detection, fake candidate generation, and human promotion/bind/lock. A candidate is not a locked reference and must not make production ready.

AUTO state tests use the real persisted repository state, not a handcrafted AUTO-only state enum. They should advance the canonical project only through formal service operations and observe each named boundary. `resume` must be tested after closing and reconstructing the repository.

The fake video adapter is asynchronous by design: `create` returns a stable task identity once, `poll` progresses `SUBMITTED → RUNNING → SUCCEEDED`, and `download` returns a unique real synthetic MP4. Its call log is a hard assertion: six authorized shots means six creates total; every retry/restart/reconcile path preserves that total. It must support injection immediately after submit, after task identity persistence, during poll, at download, after file write/before DB record, and during final assembly.

## Technical QC, Vision and continuity evidence

Synthetic media must be generated by local FFmpeg, never a fake byte string. The fixture helper uses a deterministic command equivalent to:

```text
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=24 -t 10 -c:v libx264 -pix_fmt yuv420p shot.mp4
ffmpeg -f lavfi -i sine=frequency=440:sample_rate=48000 -t <dialogue_duration> -ac 2 dialogue.wav
```

Probe via the product Technical QC service and `ffprobe`/FFmpeg decode. Validations include stream existence, codec, container, dimensions, frame rate, duration and nonzero bytes. The invalid fixtures must be a truncated MP4, an audio-only file relabelled `.mp4`, and a valid video whose profile violates the required QC profile.

Vision frame extraction must consume the actual produced shot artifact. The Vision request must contain a frame manifest with artifact identity, approved locked references and the persisted GenerationBrief. Its result is advisory: it may project warnings but cannot set, change, or substitute a Human Review decision. Continuity evaluation consumes the Vision observation plus formal expected state; it yields `WARDROBE_DRIFT` and `PROP_DRIFT` on Shot 3, records provenance and a recommendation. The test asserts `video_create_calls_after_recommendation == video_create_calls_before_recommendation`.

## Human governance, final and delivery rules

For every candidate source, final eligibility is the conjunction:

```text
technical_qc == PASS
AND review.candidate_artifact_sha256 == source.artifact_sha256
AND latest_human_review == APPROVED
AND source_decision is explicit
AND continuity policy has no unresolved blocking issue
```

Vision success, a historical approval superseded by a rejection, a review for a different artifact, or a QC pass with no review must all fail this conjunction. The final manifest freezes only eligible source identities and must not silently substitute a newer candidate. Final assembly is valid only after all six eligible shots have been selected in authored order.

The delivery test builds dialogue plan → voice assignment → TTS task → WAV → audio timeline → SRT → source-final-pinned post plan → mux → delivery final. It asserts audio/video decode, SRT timing equality, delivery duration, output profile and project-relative paths. The Director test must display that actual Delivery Final through its formal projection, not synthesize an optimistic “done” state.

## Negative E2E matrix

| Case | Set-up | Required fail-closed result | Resume assertion |
| --- | --- | --- | --- |
| `NEG-A` missing human approval | QC passes, no review | final readiness blocked with human-review reason | exact candidate can be approved; no new video create |
| `NEG-B` missing reference | leave Character B or Location B unbound/unlocked | Reference/AUTO boundary blocks production | bind and lock then resume uses same approved creative chain |
| `NEG-C` provider unavailable | fake video runtime reports unavailable | no create attempt; state is provider-pending/blocked | make runtime available then one authorized create |
| `NEG-D` paid auth missing | available paid fake runtime but no explicit authorization | no transport call | grant scoped authorization then exactly one create |
| `NEG-E` Vision failure | fake transport returns malformed/error result | sanitized persisted failure; human approval is still not fabricated | retry advisory analysis without video create |
| `NEG-F` severe continuity issue | policy marks Shot 3 drift blocking | source/final blocked; recommendation only | human resolves/overrides through formal decision; no auto repair |
| `NEG-G` TTS failure | fake TTS fails one task | no partial `TTS_READY` or Delivery Final; error sanitized | retry only failed task and preserve completed task identities |
| `NEG-H` final interruption | fail after frozen manifest or during mux | attempt is resumable; no alternate source selection | resumed assembly uses same source SHAs and order |

## Migration reconciliation contract

Reference base already exposes schema 033. The following heads each independently add a migration named 033, so merging them verbatim is invalid:

```text
creative pipeline          _migration_033_creative_pipeline_operations
AUTO                       _migration_033_auto_mode_orchestrator
reliability                _migration_033_paid_create_ledger_and_artifact_identity
continuity                 _migration_033_continuity_truth_and_repair_policy
audiovisual delivery       _migration_033_audiovisual_delivery_pipeline
```

The Integration Writer must append five unique post-033 migration versions (or semantically equivalent unique later versions) and retain every payload. The test does not prescribe their individual numbers, but it requires a supported maximum of at least 38, a unique increasing migration list, the five independently verifiable schema payloads, an upgrade from a physical 033 fixture, idempotent rerun, and fail-closed handling of a higher version. No test may use a real LocalAppData database or mutate the historical fixture in place.

## Security oracle

Use all values under `security_canaries` in the canonical fixture as literal scan needles. Search serialized values recursively in:

- Settings, AUTO, Review and Director public projections;
- persisted operation/event/error/diagnostic records;
- all artifact, candidate, Vision, continuity, final, audio and post-production metadata;
- rendered/exported diagnostics and exception messages.

An allowlist must be narrow and explicit: encrypted credential storage or an internal provider request object may retain a secret only if it is not serialized to an inspected public/persistent field. A URL may be reduced to sanitized host/path only. Absolute paths must be converted to project-relative identifiers before persistence.

## Suite execution

After the Integration Writer has landed all Wave 1 code and the listed test implementations exist, execute from the repository root:

```powershell
.\test\aidrama_studio\acceptance\aidrama_forge_max_wave_1\run_wave_1_matrix.ps1 -Suite FAST_GATE
.\test\aidrama_studio\acceptance\aidrama_forge_max_wave_1\run_wave_1_matrix.ps1 -Suite MEDIUM_GATE
.\test\aidrama_studio\acceptance\aidrama_forge_max_wave_1\run_wave_1_matrix.ps1 -Suite FULL_GATE
```

The runner creates a unique temporary root, redirects `AIDRAMA_DATA_DIR` and `LOCALAPPDATA`, runs the manifest's explicit pytest targets serially, then refuses success if the default-DB sentinel was touched. It has no URL, credential, or live-provider path. Keep FFmpeg tests serial; do not introduce xdist for shared media/subprocess lifecycle tests.

### Gate contents

- `FAST_GATE`: contracts, services, migration collision/future-schema, security and deterministic state transitions. It intentionally skips real-media delivery and full browser/playback chain.
- `MEDIUM_GATE`: every service/integration stage including synthetic MP4/WAV, FFmpeg probe and Director formal projections; still no full chained product test.
- `FULL_GATE`: `MEDIUM_GATE` plus canonical cold-reload product E2E, all negative E2E cases, and an actual browser/desktop-player playback of the Director-projected Delivery Final. FFmpeg decode/probe is necessary but cannot substitute for that playback assertion. If no browser/desktop player harness is available, report `BROWSER_PLAYBACK_UNAVAILABLE` and do not mark `FULL_GATE=PASS`.

## Live provider status matrix

This is reporting-only and does not make a provider call. No fake result may be reported as live verification.

| Capability | Offline wired after FULL_GATE | Real provider ready | Live verified |
| --- | --- | --- | --- |
| LLM | `PASS` only if W1-03/W1-05 pass | not assessed by this matrix | no integrated Wave 1 evidence in this matrix |
| IMAGE | `PASS` only if W1-05/W1-06 pass | not assessed by this matrix | no integrated Wave 1 evidence in this matrix |
| VIDEO | `PASS` only if W1-09/W1-10 pass | not assessed by this matrix | no integrated Wave 1 evidence in this matrix |
| VISION | `PASS` only if W1-13/W1-14 pass | not assessed by this matrix | no integrated Wave 1 evidence in this matrix |
| TTS | `PASS` only if W1-18/W1-19 pass | not assessed by this matrix | no integrated Wave 1 evidence in this matrix |

## Acceptance report template

```text
PHASE=AIDRAMA_FORGE_MAX_WAVE_1_INTEGRATED_FULL_AI_E2E_TEST_MATRIX
REFERENCE_BASE=557b9af74a9ccb3e5f02445d9bb1fccd71a021f2
TEST_STAGE_COUNT=26
UNIT_TEST_MATRIX=PASS|SPEC_COMPLETE|FAIL
SERVICE_TEST_MATRIX=PASS|SPEC_COMPLETE|FAIL
INTEGRATION_TEST_MATRIX=PASS|SPEC_COMPLETE|FAIL
PRODUCT_E2E_MATRIX=PASS|SPEC_COMPLETE|FAIL
NEGATIVE_E2E_MATRIX=PASS|SPEC_COMPLETE|FAIL
MIGRATION_MATRIX=PASS|SPEC_COMPLETE|FAIL
SECURITY_MATRIX=PASS|SPEC_COMPLETE|FAIL
CREATIVE_AI_GATE=DEFINED|PASS|FAIL
SETTINGS_GATE=DEFINED|PASS|FAIL
REFERENCE_AGENT_GATE=DEFINED|PASS|FAIL
AUTO_GATE=DEFINED|PASS|FAIL
PRODUCTION_GATE=DEFINED|PASS|FAIL
RELIABILITY_GATE=DEFINED|PASS|FAIL
TECHNICAL_QC_GATE=DEFINED|PASS|FAIL
VISION_GATE=DEFINED|PASS|FAIL
CONTINUITY_GATE=DEFINED|PASS|FAIL
HUMAN_GOVERNANCE_GATE=DEFINED|PASS|FAIL
FINAL_ASSEMBLY_GATE=DEFINED|PASS|FAIL
TTS_GATE=DEFINED|PASS|FAIL
POSTPRODUCTION_GATE=DEFINED|PASS|FAIL
DIRECTOR_GATE=DEFINED|PASS|FAIL
TEMP_DB_ISOLATION_GATE=DEFINED|PASS|FAIL
FULL_AI_OFFLINE_PRODUCT_E2E_GATE=DEFINED|PASS|FAIL
FAST_GATE=<pytest targets and result>
MEDIUM_GATE=<pytest targets and result>
FULL_GATE=<pytest targets and result>
LIVE_PROVIDER_CALLS=0
PRODUCT_CODE_MODIFIED=NO
TEST_PLAN_READY_FOR_INTEGRATION_WRITER=YES|NO
FIRST_FAILED_STAGE=NONE|W1-xx|NEG-x
FINAL_GATE=PASS|FAIL
```

For this specification-authoring task, report the seven matrices as `SPEC_COMPLETE`, every named product gate as `DEFINED`, `FIRST_FAILED_STAGE=NONE`, and `FINAL_GATE=PASS`. That reports matrix readiness, not an unrun integration candidate.
